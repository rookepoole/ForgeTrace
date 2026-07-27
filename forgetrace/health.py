from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable

from .collaboration import COLLABORATION_SCHEMA_VERSION, CollaborationService
from .constants import APP_SCHEMA_VERSION, APP_VERSION, REPOSITORY_SCHEMA_VERSION
from .errors import ForgeTraceError
from .git_intelligence import GIT_INTELLIGENCE_SCHEMA_VERSION, GitIntelligenceService
from .git_writes import GIT_WRITE_SCHEMA_VERSION, GitWriteService
from .locks import inspect_file_lock
from .project_coordination import PROJECT_COORDINATION_SCHEMA_VERSION, ProjectCoordinationService
from .registry import RepositoryRegistry
from .repository import ForgeTraceRepository, RepositoryError
from .security_events import (
    SECURITY_ANCHOR_SCHEMA_VERSION,
    SECURITY_EVENT_SCHEMA_VERSION,
    SECURITY_RETENTION_POLICY_SCHEMA_VERSION,
    SECURITY_ROTATION_JOURNAL_SCHEMA_VERSION,
    SECURITY_SEGMENT_SCHEMA_VERSION,
    SecurityEventError,
    SecurityEventLedger,
)
from .utils import utc_now


HEALTH_REPORT_FORMAT = "forgetrace-health-report"
HEALTH_REPORT_SCHEMA_VERSION = 1
HEALTH_REPORT_ID_PATTERN = re.compile(r"health_[0-9a-f]{32}\Z")
HEALTH_REPORT_LIMIT = 100
SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2, "critical": 3}

DEFAULT_LIMITS = {
    "repositories": 100,
    "commitsPerRepository": 50,
    "objects": 5000,
    "transactionJournalsPerRepository": 200,
    "hashIndexEntriesPerRepository": 5000,
    "reviewRevisions": 100,
    "reviewEvidenceFiles": 1000,
    "conflictDrafts": 200,
    "collaborationStorageFiles": 5000,
    "gitRepositories": 25,
}
MAX_LIMITS = {
    "repositories": 1000,
    "commitsPerRepository": 1000,
    "objects": 250_000,
    "transactionJournalsPerRepository": 2000,
    "hashIndexEntriesPerRepository": 100_000,
    "reviewRevisions": 2000,
    "reviewEvidenceFiles": 100_000,
    "conflictDrafts": 5000,
    "collaborationStorageFiles": 1_000_000,
    "gitRepositories": 200,
}


class HealthDashboardService:
    """Owner-only, read-first aggregation over existing ForgeTrace authorities.

    Reports are immutable JSON evidence stored under application data. Generating a
    report never repairs repository, registry, ledger, or collaboration state. Any
    repair remains an explicit call to the original service authority (currently the
    Doctor route) and can then be followed by a fresh report.
    """

    def __init__(
        self,
        *,
        registry: RepositoryRegistry,
        collaboration: CollaborationService,
        security_events: SecurityEventLedger,
        runtime_status: Callable[[], dict[str, Any]] | None = None,
        git_intelligence: GitIntelligenceService | None = None,
        git_writes: GitWriteService | None = None,
        project_coordination: ProjectCoordinationService | None = None,
        project_boards: Any | None = None,
        releases: Any | None = None,
    ) -> None:
        self.registry = registry
        self.collaboration = collaboration
        self.security_events = security_events
        self.runtime_status = runtime_status or (lambda: {})
        self.git_intelligence = git_intelligence
        self.git_writes = git_writes
        self.project_coordination = project_coordination
        self.project_boards = project_boards
        self.releases = releases
        self.reports_dir = registry.data_dir / "health-reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self._prune_reports()

    @staticmethod
    def _bounded_int(value: Any, *, default: int, maximum: int) -> int:
        try:
            return max(1, min(int(value), maximum))
        except (TypeError, ValueError):
            return default

    def normalize_limits(self, supplied: Any = None) -> dict[str, int]:
        values = supplied if isinstance(supplied, dict) else {}
        return {
            key: self._bounded_int(values.get(key), default=default, maximum=MAX_LIMITS[key])
            for key, default in DEFAULT_LIMITS.items()
        }

    @staticmethod
    def _canonical_json(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _hash_report(cls, payload: dict[str, Any]) -> str:
        unsigned = {key: value for key, value in payload.items() if key != "reportHash"}
        return hashlib.sha256(cls._canonical_json(unsigned).encode("utf-8")).hexdigest()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _report_path(self, report_id: str) -> Path:
        value = str(report_id or "").strip()
        if not HEALTH_REPORT_ID_PATTERN.fullmatch(value):
            raise ForgeTraceError("Health report identifier is invalid.", code="invalid_health_report_id")
        path = (self.reports_dir / f"{value}.json").resolve()
        if path.parent != self.reports_dir.resolve():
            raise ForgeTraceError("Health report path is invalid.", code="invalid_health_report_path")
        return path

    def _write_report(self, report: dict[str, Any]) -> None:
        path = self._report_path(str(report["reportId"]))
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        data = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        self._fsync_directory(path.parent)
        self._prune_reports()

    def _prune_reports(self) -> None:
        try:
            paths = sorted(
                self.reports_dir.glob("health_*.json"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for path in paths[HEALTH_REPORT_LIMIT:]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

    def _load_report(self, report_id: str) -> dict[str, Any]:
        path = self._report_path(report_id)
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("report is missing or not a regular file")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ForgeTraceError("Health report was not found.", 404, "health_report_not_found") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ForgeTraceError(
                "Health report could not be read.",
                500,
                "health_report_integrity_failed",
                {"reportId": report_id, "reason": str(exc)},
            ) from exc
        if not isinstance(payload, dict) or payload.get("format") != HEALTH_REPORT_FORMAT:
            raise ForgeTraceError(
                "Health report format is invalid.", 500, "health_report_integrity_failed"
            )
        expected = str(payload.get("reportHash") or "")
        actual = self._hash_report(payload)
        if expected != actual:
            raise ForgeTraceError(
                "Health report failed integrity verification.",
                500,
                "health_report_integrity_failed",
                {"reportId": report_id, "expected": expected, "actual": actual},
            )
        return payload

    @staticmethod
    def _finding(
        *,
        section: str,
        severity: str,
        code: str,
        title: str,
        message: str,
        checked_at: str,
        request_id: str,
        next_step: str,
        repository_id: str = "",
        path: str = "",
        object_id: str = "",
        journal_id: str = "",
        pull_request_id: str = "",
        draft_id: str = "",
        repair: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = severity if severity in SEVERITY_ORDER else "warning"
        payload = {
            "id": "finding_" + uuid.uuid4().hex,
            "section": section,
            "severity": normalized,
            "code": code,
            "title": title,
            "message": message,
            "checkedAt": checked_at,
            "requestId": request_id,
            "nextStep": next_step,
            "repositoryId": repository_id,
            "path": path,
            "objectId": object_id,
            "journalId": journal_id,
            "pullRequestId": pull_request_id,
            "draftId": draft_id,
        }
        if repair:
            payload["repair"] = repair
        if details:
            payload["details"] = details
        return payload

    @staticmethod
    def _section(name: str, checked_at: str, data: dict[str, Any], findings: list[dict[str, Any]], *, complete: bool = True) -> dict[str, Any]:
        worst = max((SEVERITY_ORDER.get(item.get("severity", "warning"), 1) for item in findings), default=0)
        status = "critical" if worst >= 3 else "failed" if worst >= 2 else "attention" if worst >= 1 else "healthy"
        if not complete and status == "healthy":
            status = "partial"
        return {
            "name": name,
            "status": status,
            "complete": bool(complete),
            "checkedAt": checked_at,
            "findingCount": len(findings),
            "findings": findings,
            "data": data,
        }

    @staticmethod
    def _probe_lock(path: Path) -> dict[str, Any]:
        return dict(inspect_file_lock(path))

    def _registry_rows(self) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        with self.registry.connect() as connection:
            integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            integrity = "ok" if integrity_rows == ["ok"] else "; ".join(integrity_rows)
            foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
            schema_raw = self.registry._get_state(connection, "schema_version", "0")
            try:
                schema_version = int(schema_raw)
            except (TypeError, ValueError):
                schema_version = 0
            active_id = self.registry._get_state(connection, "active_repository_id")
            rows = connection.execute(
                "SELECT * FROM repositories ORDER BY favorite DESC, name COLLATE NOCASE"
            ).fetchall()
            records = [
                self.registry._row_to_public(connection, row, active_id=active_id) for row in rows
            ]
        return active_id, records, {
            "sqliteIntegrity": integrity,
            "foreignKeyIssues": foreign_keys,
            "schemaVersion": schema_version,
        }

    def _repository_assessment(
        self,
        record: dict[str, Any],
        *,
        checked_at: str,
        request_id: str,
        limits: dict[str, int],
        object_budget: list[int],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        findings: list[dict[str, Any]] = []
        repository_id = str(record["id"])
        status = str(record.get("status") or "invalid")
        result: dict[str, Any] = {
            "repositoryId": repository_id,
            "name": record.get("name", ""),
            "path": record.get("path", ""),
            "pathStatus": status,
            "pathMessage": record.get("statusMessage", ""),
            "capabilities": record.get("capabilities", {}),
            "accessMode": record.get("accessMode", "read_only"),
            "active": bool(record.get("active")),
        }
        if status != "online":
            severity = "warning" if status == "offline" else "error"
            findings.append(
                self._finding(
                    section="repositories",
                    severity=severity,
                    code=f"repository_{status}",
                    title=f"Repository is {status}",
                    message=str(record.get("statusMessage") or "Repository cannot be fully assessed."),
                    checked_at=checked_at,
                    request_id=request_id,
                    repository_id=repository_id,
                    path=str(record.get("path") or ""),
                    next_step="Relink, initialize, or correct the repository path before running a complete health scan.",
                )
            )
            result["complete"] = False
            return result, findings, False

        service = ForgeTraceRepository(
            self.registry.project_root,
            Path(str(record["path"])),
            repository_id,
            upload_limit_bytes=int(record.get("uploadLimitBytes") or 1),
            access_mode_getter=lambda repository_id=repository_id: self.registry.get_access_mode(repository_id),
            recover_on_open=False,
            create_workspace=False,
        )
        try:
            state = service.load_state()
        except RepositoryError as exc:
            findings.append(
                self._finding(
                    section="repositories",
                    severity="critical",
                    code="metadata_unreadable",
                    title="Repository metadata is unreadable",
                    message=str(exc),
                    checked_at=checked_at,
                    request_id=request_id,
                    repository_id=repository_id,
                    path=str(service.state_path),
                    next_step="Use the explicit Doctor repair only after reviewing the metadata backup evidence.",
                    repair={"authority": "doctor", "action": "repair", "confirmationRequired": True},
                )
            )
            result["complete"] = False
            return result, findings, False

        metadata = state.get("repository", {}) if isinstance(state, dict) else {}
        stored_id = str(metadata.get("id") or "")
        result["repositorySchemaVersion"] = int(state.get("schemaVersion") or 0)
        result["stateRevision"] = int(state.get("revision") or 0)
        result["embeddedRepositoryId"] = stored_id
        result["accessPolicy"] = service.access_policy(state)
        if stored_id != repository_id:
            findings.append(
                self._finding(
                    section="access",
                    severity="critical",
                    code="repository_identity_mismatch",
                    title="Repository identity mismatch",
                    message="The registry identity does not match embedded repository metadata.",
                    checked_at=checked_at,
                    request_id=request_id,
                    repository_id=repository_id,
                    path=str(service.state_path),
                    next_step="Do not mutate this repository. Relink the correct path or restore known-good metadata.",
                    details={"embeddedRepositoryId": stored_id},
                )
            )
        policy = result["accessPolicy"]
        if not policy.get("embeddedValid") or not policy.get("consistent"):
            findings.append(
                self._finding(
                    section="access",
                    severity="error",
                    code="repository_access_mode_mismatch",
                    title="Access-mode authority is inconsistent",
                    message="Registry and embedded access-mode copies do not validly agree; ForgeTrace fails closed to read-only.",
                    checked_at=checked_at,
                    request_id=request_id,
                    repository_id=repository_id,
                    path=str(service.state_path),
                    next_step="Review the two copies, then explicitly reapply the intended mode through the owner access-mode authority.",
                    details=policy,
                )
            )
        elif policy.get("effectiveMode") == "read_only":
            findings.append(
                self._finding(
                    section="access",
                    severity="info",
                    code="repository_read_only",
                    title="Repository is intentionally read-only",
                    message="Read and review operations are available; repository mutations and merges remain blocked.",
                    checked_at=checked_at,
                    request_id=request_id,
                    repository_id=repository_id,
                    path=str(record.get("path") or ""),
                    next_step="No action is required unless the owner intends to return the repository to read-write mode.",
                )
            )

        transactions = service.transaction_health(
            limit=limits["transactionJournalsPerRepository"]
        )
        result["transactions"] = transactions
        for journal in transactions.get("journals", []):
            assessment = str(journal.get("assessment") or "")
            if assessment == "recovery_required":
                findings.append(
                    self._finding(
                        section="recovery",
                        severity="critical",
                        code="pending_repository_transaction",
                        title="Repository transaction recovery is required",
                        message="A pending filesystem transaction journal exists. Opening the repository through its normal service authority will recover it.",
                        checked_at=checked_at,
                        request_id=request_id,
                        repository_id=repository_id,
                        path=str(journal.get("journalPath") or ""),
                        journal_id=str(journal.get("transaction") or ""),
                        next_step="Open the repository normally or run the explicit Doctor repair, then generate a fresh report.",
                        repair={"authority": "doctor", "action": "repair", "confirmationRequired": True},
                    )
                )
            elif assessment in {"attention_required", "unknown_status"}:
                findings.append(
                    self._finding(
                        section="recovery",
                        severity="error",
                        code="unreadable_repository_transaction",
                        title="Repository transaction journal is unreadable",
                        message=str(journal.get("message") or "The journal status cannot be trusted."),
                        checked_at=checked_at,
                        request_id=request_id,
                        repository_id=repository_id,
                        path=str(journal.get("journalPath") or ""),
                        journal_id=str(journal.get("transaction") or ""),
                        next_step="Preserve the journal and inspect it before attempting manual recovery.",
                    )
                )
            elif assessment == "cleanup_eligible":
                findings.append(
                    self._finding(
                        section="recovery",
                        severity="warning",
                        code="committed_transaction_artifact",
                        title="Committed transaction artifact remains",
                        message="A committed journal can be cleaned by the existing repository recovery authority.",
                        checked_at=checked_at,
                        request_id=request_id,
                        repository_id=repository_id,
                        path=str(journal.get("journalPath") or ""),
                        journal_id=str(journal.get("transaction") or ""),
                        next_step="Open the repository normally or use Doctor repair after confirmation.",
                        repair={"authority": "doctor", "action": "repair", "confirmationRequired": True},
                    )
                )

        hash_index = service.hash_index_health(
            max_entries=limits["hashIndexEntriesPerRepository"]
        )
        result["hashIndex"] = hash_index
        if not hash_index.get("valid"):
            findings.append(
                self._finding(
                    section="repositories",
                    severity="warning",
                    code="hash_index_invalid",
                    title="Incremental hash index is invalid",
                    message=str(hash_index.get("message") or "The cached hash index cannot be trusted."),
                    checked_at=checked_at,
                    request_id=request_id,
                    repository_id=repository_id,
                    path=str(service.index_path),
                    next_step="A future writable repository scan can rebuild the cache; repository content remains authoritative.",
                )
            )
        elif hash_index.get("state") == "stale":
            findings.append(
                self._finding(
                    section="repositories",
                    severity="warning",
                    code="hash_index_stale",
                    title="Incremental hash index contains stale entries",
                    message="Cached signatures no longer match one or more live paths.",
                    checked_at=checked_at,
                    request_id=request_id,
                    repository_id=repository_id,
                    path=str(service.index_path),
                    next_step="Run a normal repository scan when writable to refresh the cache.",
                    details={
                        "staleCount": hash_index.get("staleCount", 0),
                        "missingPathCount": hash_index.get("missingPathCount", 0),
                    },
                )
            )

        commits = list(state.get("commits") or [])
        commit_limit = limits["commitsPerRepository"]
        selected_commits = commits[-commit_limit:]
        invalid_commits = 0
        verified_commits = 0
        verified_objects = 0
        object_errors: list[dict[str, Any]] = []
        complete = len(commits) <= commit_limit
        for commit in reversed(selected_commits):
            if object_budget[0] <= 0:
                complete = False
                break
            verification = service.verify_snapshot_objects(
                commit, max_objects=object_budget[0]
            )
            object_budget[0] -= int(verification.get("scannedObjects") or 0)
            verified_objects += int(verification.get("verifiedObjects") or 0)
            verified_commits += 1
            if not verification.get("complete"):
                complete = False
            if not verification.get("valid"):
                invalid_commits += 1
                for error in verification.get("errors", [])[:100]:
                    object_errors.append({"commitId": commit.get("id"), **error})
                    findings.append(
                        self._finding(
                            section="repositories",
                            severity="critical",
                            code="snapshot_object_integrity",
                            title="Snapshot object failed integrity verification",
                            message=str(error.get("message") or error.get("code") or "Snapshot object is missing or corrupt."),
                            checked_at=checked_at,
                            request_id=request_id,
                            repository_id=repository_id,
                            path=str(error.get("path") or ""),
                            object_id=str(error.get("hash") or error.get("expected") or ""),
                            next_step="Do not restore this snapshot. Review Doctor reconstruction evidence before any explicit repair.",
                            repair={"authority": "doctor", "action": "repair", "confirmationRequired": True},
                            details={"commitId": commit.get("id"), "error": error},
                        )
                    )
        result["snapshotReadiness"] = {
            "commitCount": len(commits),
            "verifiedCommitCount": verified_commits,
            "invalidCommitCount": invalid_commits,
            "verifiedObjectCount": verified_objects,
            "complete": complete,
            "errors": object_errors,
        }
        if not complete:
            findings.append(
                self._finding(
                    section="repositories",
                    severity="warning",
                    code="snapshot_verification_partial",
                    title="Snapshot verification is partial",
                    message="The bounded object/commit budget was reached before every snapshot object was verified.",
                    checked_at=checked_at,
                    request_id=request_id,
                    repository_id=repository_id,
                    path=str(service.objects_dir),
                    next_step="Run a complete health report with a larger explicit verification budget if full assurance is required.",
                )
            )
        result["complete"] = complete and bool(transactions.get("complete")) and bool(hash_index.get("complete"))
        return result, findings, bool(result["complete"])

    def generate(
        self,
        *,
        request_id: str,
        repository_id: str = "",
        scope: str = "standard",
        limits: Any = None,
    ) -> dict[str, Any]:
        checked_at = utc_now()
        selected_scope = str(scope or "standard").strip().lower()
        if selected_scope not in {"standard", "complete"}:
            raise ForgeTraceError(
                "Health report scope must be standard or complete.", code="invalid_health_scope"
            )
        normalized_limits = self.normalize_limits(limits)
        if selected_scope == "complete":
            normalized_limits = dict(MAX_LIMITS)

        active_id, records, registry_state = self._registry_rows()
        requested_repository = str(repository_id or "").strip()
        if requested_repository:
            records = [item for item in records if str(item["id"]) == requested_repository]
            if not records:
                raise ForgeTraceError(
                    "Repository is not registered.", 404, "repository_not_found"
                )
        total_repository_count = len(records)
        records = records[: normalized_limits["repositories"]]
        repositories_complete = total_repository_count <= normalized_limits["repositories"]

        doctor = self.registry.doctor(
            repair=False,
            scan_roots=[],
            recover_repository_transactions=False,
            verify_snapshot_objects=False,
        )

        registry_findings: list[dict[str, Any]] = []
        if registry_state["sqliteIntegrity"].lower() != "ok":
            registry_findings.append(
                self._finding(
                    section="registry",
                    severity="critical",
                    code="sqlite_integrity",
                    title="Registry SQLite integrity failed",
                    message=registry_state["sqliteIntegrity"],
                    checked_at=checked_at,
                    request_id=request_id,
                    path=str(self.registry.db_path),
                    next_step="Stop registry mutations and restore a validated backup through the existing recovery authority.",
                )
            )
        if registry_state["foreignKeyIssues"]:
            registry_findings.append(
                self._finding(
                    section="registry",
                    severity="error",
                    code="registry_foreign_key_integrity",
                    title="Registry foreign-key integrity failed",
                    message=f"{len(registry_state['foreignKeyIssues'])} foreign-key issue(s) were reported.",
                    checked_at=checked_at,
                    request_id=request_id,
                    path=str(self.registry.db_path),
                    next_step="Preview a validated registry backup before any restore action.",
                    details={"issues": registry_state["foreignKeyIssues"][:100]},
                )
            )
        if int(registry_state["schemaVersion"]) != APP_SCHEMA_VERSION:
            registry_findings.append(
                self._finding(
                    section="registry",
                    severity="critical",
                    code="registry_schema_mismatch",
                    title="Registry schema version is unexpected",
                    message=f"Expected {APP_SCHEMA_VERSION}; found {registry_state['schemaVersion']}.",
                    checked_at=checked_at,
                    request_id=request_id,
                    path=str(self.registry.db_path),
                    next_step="Do not run an older package against this application-data directory.",
                )
            )
        for issue in doctor.get("issues", []):
            severity = str(issue.get("severity") or "warning")
            registry_findings.append(
                self._finding(
                    section="registry",
                    severity=severity,
                    code=str(issue.get("code") or "doctor_issue"),
                    title=str(issue.get("code") or "Doctor finding").replace("_", " ").title(),
                    message=str(issue.get("message") or "Doctor reported an issue."),
                    checked_at=checked_at,
                    request_id=request_id,
                    repository_id=str(issue.get("repositoryId") or ""),
                    path=str(issue.get("path") or ""),
                    object_id=str(issue.get("commitId") or ""),
                    next_step="Review the finding, then use the explicit Doctor repair only when the reported repair is appropriate.",
                    repair={"authority": "doctor", "action": "repair", "confirmationRequired": True},
                    details={key: value for key, value in issue.items() if key not in {"message"}},
                )
            )

        backups = self.registry.list_backups()
        restores = self.registry.list_registry_restores()
        registry_data = {
            **registry_state,
            "expectedSchemaVersion": APP_SCHEMA_VERSION,
            "activeRepositoryId": active_id,
            "repositoryCount": total_repository_count,
            "backupCount": len(backups),
            "latestBackup": backups[0] if backups else None,
            "doctor": doctor,
        }
        registry_section = self._section(
            "Registry",
            checked_at,
            registry_data,
            registry_findings,
            complete=True,
        )

        object_budget = [normalized_limits["objects"]]
        repository_payload: list[dict[str, Any]] = []
        repository_findings: list[dict[str, Any]] = []
        recovery_findings: list[dict[str, Any]] = []
        access_findings: list[dict[str, Any]] = []
        repository_complete = repositories_complete
        for record in records:
            payload, findings, complete = self._repository_assessment(
                record,
                checked_at=checked_at,
                request_id=request_id,
                limits=normalized_limits,
                object_budget=object_budget,
            )
            repository_payload.append(payload)
            repository_complete = repository_complete and complete
            for finding in findings:
                if finding["section"] == "recovery":
                    recovery_findings.append(finding)
                elif finding["section"] == "access":
                    access_findings.append(finding)
                else:
                    repository_findings.append(finding)
        if not repositories_complete:
            repository_findings.append(
                self._finding(
                    section="repositories",
                    severity="warning",
                    code="repository_scan_bounded",
                    title="Repository scan is bounded",
                    message="Not every registered repository fit within the explicit scan limit.",
                    checked_at=checked_at,
                    request_id=request_id,
                    next_step="Run a repository-scoped report or increase the repository limit.",
                    details={
                        "registered": total_repository_count,
                        "scanned": len(records),
                    },
                )
            )
        repositories_section = self._section(
            "Repositories",
            checked_at,
            {
                "registeredCount": total_repository_count,
                "scannedCount": len(repository_payload),
                "remainingObjectBudget": object_budget[0],
                "repositories": repository_payload,
            },
            repository_findings,
            complete=repository_complete,
        )

        git_findings: list[dict[str, Any]] = []
        git_records: list[dict[str, Any]] = []
        reported_unassigned_git_transactions: set[str] = set()
        git_candidates = [item for item in records if str(item.get("status")) == "online"]
        git_limit = normalized_limits.get("gitRepositories", 25)
        git_complete = len(git_candidates) <= git_limit
        if self.git_intelligence is not None:
            for record in git_candidates[:git_limit]:
                repository_key = str(record.get("id") or "")
                probe = self.git_intelligence.probe(repository_key)
                if probe.get("detected") and probe.get("supported") and self.git_writes is not None:
                    try:
                        write_status = self.git_writes.status(repository_key, receipt_limit=25)
                        receipts = list(write_status.get("receipts") or [])
                        pending = list(write_status.get("pendingTransactions") or [])
                        recovery_summary = dict(write_status.get("recoverySummary") or {})
                        maintenance_warnings = list(write_status.get("maintenanceWarnings") or [])
                        unassigned_transactions = list(write_status.get("unassignedTransactions") or [])
                        probe["writeAuthority"] = {
                            "schemaVersion": GIT_WRITE_SCHEMA_VERSION,
                            "supported": bool(write_status.get("supported")),
                            "writable": bool(write_status.get("writable")),
                            "pendingTransactionCount": len(pending),
                            "verifiedReceiptCount": sum(1 for item in receipts if item.get("verified")),
                            "receiptCount": len(receipts),
                            "nativeLocks": list(write_status.get("nativeLocks") or []),
                            "restrictions": dict(write_status.get("restrictions") or {}),
                            "startupRecovery": dict(write_status.get("startupRecovery") or {}),
                            "recoverySummary": recovery_summary,
                            "maintenanceWarningCount": len(maintenance_warnings),
                            "unassignedTransactionCount": len(unassigned_transactions),
                        }
                        if pending:
                            manual_count = int(recovery_summary.get("manualInspectionCount") or 0)
                            deferred_count = int(recovery_summary.get("deferredCount") or 0)
                            severity = "error" if manual_count else "warning"
                            next_steps = [
                                str(item.get("nextStep") or "")
                                for item in pending
                                if item.get("nextStep")
                            ]
                            git_findings.append(self._finding(
                                section="git",
                                severity=severity,
                                code="git_write_transaction_pending",
                                title="Transactional Git write journal requires attention",
                                message=(
                                    f"Repository has {len(pending)} pending transactional Git write journal(s): "
                                    f"{manual_count} manual-inspection, {deferred_count} externally deferred."
                                ),
                                checked_at=checked_at,
                                request_id=request_id,
                                repository_id=repository_key,
                                path=str(record.get("path") or ""),
                                next_step=next_steps[0] if next_steps else "Restart ForgeTrace to retry exact rollback recovery, then inspect the retained transaction journal and security history.",
                                details={"pendingTransactions": pending, "recoverySummary": recovery_summary},
                            ))
                        if maintenance_warnings:
                            git_findings.append(self._finding(
                                section="git",
                                severity="warning",
                                code="git_write_maintenance_cleanup_deferred",
                                title="Transactional Git evidence cleanup was deferred",
                                message=f"{len(maintenance_warnings)} non-critical application-data cleanup operation(s) were blocked, commonly by a Windows file handle or scanner.",
                                checked_at=checked_at,
                                request_id=request_id,
                                repository_id=repository_key,
                                path=str(record.get("path") or ""),
                                next_step="Close processes scanning ForgeTrace application data and restart. Committed or rolled-back Git state remains governed by the sealed journal and receipt.",
                                details={"maintenanceWarnings": maintenance_warnings[-10:]},
                            ))
                        new_unassigned_transactions = [
                            item
                            for item in unassigned_transactions
                            if str(item.get("transactionId") or "") not in reported_unassigned_git_transactions
                        ]
                        if new_unassigned_transactions:
                            reported_unassigned_git_transactions.update(
                                str(item.get("transactionId") or "")
                                for item in new_unassigned_transactions
                            )
                            git_findings.append(self._finding(
                                section="git",
                                severity="error",
                                code="git_write_unassigned_journal",
                                title="Unreadable transactional Git journal requires manual inspection",
                                message=f"{len(new_unassigned_transactions)} journal(s) could not be safely associated with a registered repository.",
                                checked_at=checked_at,
                                request_id=request_id,
                                next_step="Preserve the application-data git-writes transaction directories and inspect their journal integrity before deleting or repairing anything.",
                                details={"transactions": new_unassigned_transactions},
                            ))
                        unverified = [item for item in receipts if not item.get("verified")]
                        if unverified:
                            git_findings.append(self._finding(
                                section="git",
                                severity="error",
                                code="git_write_receipt_integrity_failed",
                                title="Transactional Git write receipt failed verification",
                                message=f"{len(unverified)} recent Git write receipt(s) failed digest verification.",
                                checked_at=checked_at,
                                request_id=request_id,
                                repository_id=repository_key,
                                path=str(record.get("path") or ""),
                                next_step="Do not discard the receipt evidence. Inspect application-data git-writes receipts and security history before further writes.",
                                details={"transactionIds": [str(item.get("transactionId") or "") for item in unverified]},
                            ))
                    except Exception as exc:
                        probe["writeAuthority"] = {
                            "schemaVersion": GIT_WRITE_SCHEMA_VERSION,
                            "supported": False,
                            "reason": f"{type(exc).__name__}: {exc}"[:2048],
                        }
                        git_findings.append(self._finding(
                            section="git",
                            severity="warning",
                            code="git_write_status_unavailable",
                            title="Transactional Git write status is unavailable",
                            message="Read-only Git inspection succeeded, but transactional write evidence could not be loaded.",
                            checked_at=checked_at,
                            request_id=request_id,
                            repository_id=repository_key,
                            path=str(record.get("path") or ""),
                            next_step="Inspect the transactional Git write status endpoint and application-data journals. Read-only Git inspection remains available.",
                            details={"errorType": type(exc).__name__},
                        ))
                git_records.append(probe)
                if probe.get("detected") and not probe.get("supported"):
                    git_findings.append(self._finding(
                        section="git",
                        severity="warning",
                        code=str(probe.get("errorCode") or "git_layout_unsupported"),
                        title="Git repository could not be inspected safely",
                        message=str(probe.get("reason") or "Git inspection is unavailable."),
                        checked_at=checked_at,
                        request_id=request_id,
                        repository_id=repository_key,
                        path=str(record.get("path") or ""),
                        next_step="Inspect the local .git layout and Git installation. ForgeTrace will not repair or mutate Git state.",
                        details={key: value for key, value in probe.items() if key != "reason"},
                    ))
        if not git_complete:
            git_findings.append(self._finding(
                section="git",
                severity="warning",
                code="git_scan_bounded",
                title="Git inspection is bounded",
                message="Not every online repository fit within the Git inspection limit.",
                checked_at=checked_at,
                request_id=request_id,
                next_step="Run a repository-scoped report or increase the Git repository limit.",
                details={"eligible": len(git_candidates), "scanned": len(git_records)},
            ))
        git_section = self._section(
            "Git",
            checked_at,
            {"schemaVersion": GIT_INTELLIGENCE_SCHEMA_VERSION, "gitWriteSchemaVersion": GIT_WRITE_SCHEMA_VERSION, "eligibleCount": len(git_candidates), "scannedCount": len(git_records), "repositories": git_records},
            git_findings,
            complete=git_complete,
        )

        pending_restores = [
            item for item in restores if str(item.get("state") or "") in {"prepared", "installing", "installed", "journal_unreadable", "failed"}
        ]
        rollback_authorities = [item for item in restores if item.get("rollbackAvailable")]
        for item in pending_restores:
            state = str(item.get("state") or "unknown")
            recovery_findings.append(
                self._finding(
                    section="recovery",
                    severity="critical" if state in {"journal_unreadable", "failed"} else "warning",
                    code="registry_restore_journal_attention",
                    title="Registry restore journal requires attention",
                    message=f"Restore {item.get('restoreId', 'unknown')} is in state {state}.",
                    checked_at=checked_at,
                    request_id=request_id,
                    journal_id=str(item.get("restoreId") or ""),
                    next_step="Use the existing registry recovery history to inspect or roll back this restore.",
                    details=item,
                )
            )
        recovery_section = self._section(
            "Recovery",
            checked_at,
            {
                "registryRestores": restores,
                "pendingRegistryRestoreCount": len(pending_restores),
                "rollbackAuthorityCount": len(rollback_authorities),
                "startupRegistryRestoreRecovery": self.registry.startup_restore_recovery_report,
                "startupRepositoryRecovery": self.registry.startup_recovery_report,
                "startupArtifactCleanup": self.registry.startup_cleanup_report,
            },
            recovery_findings,
            complete=repository_complete,
        )

        access_section = self._section(
            "Access",
            checked_at,
            {
                "readWriteCount": sum(
                    1 for item in repository_payload if item.get("accessPolicy", {}).get("writable")
                ),
                "readOnlyCount": sum(
                    1 for item in repository_payload if item.get("accessPolicy") and not item.get("accessPolicy", {}).get("writable")
                ),
                "inconsistentCount": sum(
                    1 for item in repository_payload if item.get("accessPolicy") and not item.get("accessPolicy", {}).get("consistent")
                ),
            },
            access_findings,
            complete=repository_complete,
        )

        security_history = self.security_events.operational_status()
        security_integrity = security_history.get("integrity", {})
        protected_available = False
        protected_reason = ""
        try:
            self.security_events.assert_writable()
            protected_available = True
        except SecurityEventError as exc:
            protected_reason = str(exc)
        security_findings: list[dict[str, Any]] = []
        for issue in security_integrity.get("issues", []):
            security_findings.append(
                self._finding(
                    section="security",
                    severity="critical",
                    code=str(issue.get("code") or "security_ledger_integrity"),
                    title="Security Event Ledger integrity failed",
                    message=str(issue.get("message") or issue.get("code") or "Ledger evidence is not trustworthy."),
                    checked_at=checked_at,
                    request_id=request_id,
                    path=str(self.security_events.db_path),
                    next_step="Protected mutations remain blocked. Preserve the ledger and investigate before attempting recovery.",
                    details=issue,
                )
            )
        if not protected_available and not security_findings:
            security_findings.append(
                self._finding(
                    section="security",
                    severity="critical",
                    code="security_ledger_not_writable",
                    title="Protected actions are unavailable",
                    message=protected_reason or "The ledger cannot authorize protected actions.",
                    checked_at=checked_at,
                    request_id=request_id,
                    path=str(self.security_events.db_path),
                    next_step="Resolve ledger writability without deleting or rewriting append-only evidence.",
                )
            )
        if security_history.get("policyError"):
            security_findings.append(self._finding(
                section="security", severity="critical", code="security_retention_policy_invalid",
                title="Security retention policy failed verification",
                message=str(security_history.get("policyError")), checked_at=checked_at,
                request_id=request_id, path=str(self.security_events.policy_path),
                next_step="Preserve the policy file and restore a verified owner-approved retention configuration before rotation.",
            ))
        if security_history.get("segmentError"):
            security_findings.append(self._finding(
                section="security", severity="critical", code="security_segment_inventory_invalid",
                title="Security segment inventory failed verification",
                message=str(security_history.get("segmentError")), checked_at=checked_at,
                request_id=request_id, path=str(self.security_events.segments_dir),
                next_step="Preserve all segment files and investigate missing, substituted, truncated, or reordered evidence.",
            ))
        for journal in security_history.get("incompleteRotationJournals", []):
            security_findings.append(self._finding(
                section="security", severity="critical", code="security_rotation_journal_incomplete",
                title="Security rotation journal requires recovery",
                message=f"Rotation {journal.get('rotationId', 'unknown')} is in state {journal.get('state', 'unknown')}.",
                checked_at=checked_at, request_id=request_id,
                path=str(self.security_events.rotations_dir),
                next_step="Do not delete the journal or its recovery files; restart ForgeTrace or investigate the recorded rollback failure.",
                details=journal,
            ))
        if security_history.get("pressure", {}).get("pressure"):
            security_findings.append(self._finding(
                section="security", severity="warning", code="security_retention_pressure",
                title="Security history exceeds a configured retention budget",
                message="One or more active-event, retained-event, or storage budgets are exceeded.",
                checked_at=checked_at, request_id=request_id,
                path=str(self.security_events.data_dir),
                next_step="Preview a journaled rotation. ForgeTrace will preserve the configured minimum evidence window.",
                details=security_history.get("pressure", {}),
            ))
        anchors = security_history.get("anchors", {})
        for invalid in anchors.get("invalid", []):
            security_findings.append(self._finding(
                section="security", severity="critical", code="security_anchor_receipt_invalid",
                title="Security anchor evidence failed local verification",
                message=str(invalid.get("message") or "Anchor request or receipt integrity failed."),
                checked_at=checked_at, request_id=request_id, path=str(self.security_events.anchors_dir),
                next_step="Preserve the request and receipt files and compare them with the externally retained copy.",
                details=invalid,
            ))
        if int(anchors.get("unanchoredSegmentCount", 0) or 0):
            security_findings.append(self._finding(
                section="security", severity="warning", code="security_segments_unanchored",
                title="Sealed security segments have no recorded external receipt",
                message=f"{anchors.get('unanchoredSegmentCount', 0)} verified segment(s) are not covered by a recorded owner-supplied receipt.",
                checked_at=checked_at, request_id=request_id, path=str(self.security_events.anchors_dir),
                next_step="Optionally export a chain-head digest, publish it through an owner-selected mechanism, and record the returned evidence.",
                details={"segments": anchors.get("unanchoredSegments", [])[:25]},
            ))
        missing_receipts = [item for item in anchors.get("anchors", []) if not item.get("receiptRecorded")]
        if missing_receipts:
            security_findings.append(self._finding(
                section="security", severity="warning", code="security_anchor_receipt_missing",
                title="Exported anchor requests have no recorded receipt",
                message=f"{len(missing_receipts)} local digest request(s) have not been paired with owner-supplied external evidence.",
                checked_at=checked_at, request_id=request_id, path=str(self.security_events.anchors_dir),
                next_step="Record a receipt only after the digest has been handled by the owner-selected external mechanism.",
            ))
        security_section = self._section(
            "Security",
            checked_at,
            {
                "integrity": security_integrity,
                "protectedActionsAvailable": protected_available,
                "protectedActionReason": protected_reason,
                "schemaVersion": SECURITY_EVENT_SCHEMA_VERSION,
                "segmentedHistory": security_history,
            },
            security_findings,
            complete=True,
        )

        collaboration_data = self.collaboration.health_assessment(
            max_revisions=normalized_limits["reviewRevisions"],
            max_evidence_files=normalized_limits["reviewEvidenceFiles"],
            max_drafts=normalized_limits["conflictDrafts"],
            max_storage_files=normalized_limits["collaborationStorageFiles"],
        )
        collaboration_findings: list[dict[str, Any]] = []
        if collaboration_data["sqliteIntegrity"].lower() != "ok":
            collaboration_findings.append(
                self._finding(
                    section="collaboration",
                    severity="critical",
                    code="collaboration_sqlite_integrity",
                    title="Collaboration database integrity failed",
                    message=collaboration_data["sqliteIntegrity"],
                    checked_at=checked_at,
                    request_id=request_id,
                    path=str(self.collaboration.db_path),
                    next_step="Stop collaboration mutations and preserve quarantine evidence for investigation.",
                )
            )
        if collaboration_data["schemaVersion"] != COLLABORATION_SCHEMA_VERSION:
            collaboration_findings.append(
                self._finding(
                    section="collaboration",
                    severity="critical",
                    code="collaboration_schema_mismatch",
                    title="Collaboration schema version is unexpected",
                    message=f"Expected {COLLABORATION_SCHEMA_VERSION}; found {collaboration_data['schemaVersion']}.",
                    checked_at=checked_at,
                    request_id=request_id,
                    path=str(self.collaboration.db_path),
                    next_step="Do not run an older ForgeTrace package against this application data.",
                )
            )
        if collaboration_data["foreignKeyIssueCount"]:
            collaboration_findings.append(
                self._finding(
                    section="collaboration",
                    severity="error",
                    code="collaboration_foreign_key_integrity",
                    title="Collaboration foreign-key integrity failed",
                    message=f"{collaboration_data['foreignKeyIssueCount']} foreign-key issue(s) were found.",
                    checked_at=checked_at,
                    request_id=request_id,
                    path=str(self.collaboration.db_path),
                    next_step="Preserve the database and quarantine evidence before any manual investigation.",
                )
            )
        review_health = collaboration_data["reviewConversations"]
        for issue in review_health.get("issues", []):
            collaboration_findings.append(
                self._finding(
                    section="collaboration",
                    severity="critical",
                    code=str(issue.get("code") or "review_revision_integrity_failed"),
                    title="Submitted revision evidence failed integrity verification",
                    message=str(issue.get("message") or "Immutable review evidence is missing or corrupt."),
                    checked_at=checked_at,
                    request_id=request_id,
                    repository_id=str(issue.get("repositoryId") or ""),
                    path=str(issue.get("path") or ""),
                    pull_request_id=str(issue.get("pullRequestId") or ""),
                    next_step="Do not approve or merge the affected revision. Preserve its quarantine evidence for investigation.",
                    details=issue,
                )
            )
        resolution_health = collaboration_data["conflictResolutions"]
        for issue in resolution_health.get("issues", []):
            collaboration_findings.append(
                self._finding(
                    section="collaboration",
                    severity="critical",
                    code=str(issue.get("code") or "conflict_resolution_integrity_failed"),
                    title="Conflict-resolution evidence failed integrity verification",
                    message=str(issue.get("message") or "Resolution evidence is missing or corrupt."),
                    checked_at=checked_at,
                    request_id=request_id,
                    repository_id=str(issue.get("repositoryId") or ""),
                    path=str(issue.get("path") or ""),
                    pull_request_id=str(issue.get("pullRequestId") or ""),
                    draft_id=str(issue.get("draftId") or ""),
                    next_step="Do not approve or merge the affected pull request. Regenerate evidence only from verified current sources.",
                    details=issue,
                )
            )
        orphan_total = (
            int(collaboration_data.get("orphanQuarantineDirectoryCount") or 0)
            + int(review_health.get("orphanRevisionDirectoryCount") or 0)
            + int(resolution_health.get("orphanConflictResolutionDirectoryCount") or 0)
        )
        if orphan_total:
            collaboration_findings.append(
                self._finding(
                    section="collaboration",
                    severity="warning",
                    code="collaboration_orphan_storage",
                    title="Orphaned collaboration storage was detected",
                    message=f"{orphan_total} orphaned quarantine/evidence directorie(s) were observed.",
                    checked_at=checked_at,
                    request_id=request_id,
                    path=str(self.collaboration.data_dir),
                    next_step="Use the existing retention cleanup authority during a controlled maintenance run.",
                )
            )
        collaboration_complete = (
            bool(collaboration_data.get("orphanQuarantineScanComplete", True))
            and bool(review_health.get("complete"))
            and bool(resolution_health.get("complete"))
            and bool(collaboration_data.get("storage", {}).get("complete"))
        )
        if not collaboration_complete:
            collaboration_findings.append(
                self._finding(
                    section="collaboration",
                    severity="warning",
                    code="collaboration_scan_partial",
                    title="Collaboration evidence scan is partial",
                    message="The explicit review, resolution, or storage verification limit was reached.",
                    checked_at=checked_at,
                    request_id=request_id,
                    path=str(self.collaboration.data_dir),
                    next_step="Run a complete report or increase only the required evidence limit.",
                )
            )
        collaboration_section = self._section(
            "Collaboration",
            checked_at,
            collaboration_data,
            collaboration_findings,
            complete=collaboration_complete,
        )

        project_findings: list[dict[str, Any]] = []
        project_data: dict[str, Any] = {"available": self.project_coordination is not None}
        project_complete = self.project_coordination is not None
        if self.project_coordination is not None:
            project_data = self.project_coordination.health_status(requested_repository)
            integrity = str(project_data.get("integrity") or "unknown").lower()
            if integrity != "ok":
                project_complete = False
                project_findings.append(
                    self._finding(
                        section="project",
                        severity="error",
                        code="project_coordination_integrity_failed",
                        title="Project coordination database integrity failed",
                        message=str(project_data.get("error") or integrity),
                        checked_at=checked_at,
                        request_id=request_id,
                        repository_id=requested_repository,
                        path=str(project_data.get("databasePath") or ""),
                        next_step="Stop project coordination mutations and preserve the application-data directory for recovery review.",
                    )
                )
            if self.project_boards is not None:
                board_data = self.project_boards.health_status(requested_repository)
                project_data["boards"] = board_data
                board_integrity = str(board_data.get("integrity") or "unknown").lower()
                if board_integrity != "ok":
                    project_complete = False
                    project_findings.append(
                        self._finding(
                            section="project",
                            severity="error",
                            code="project_boards_integrity_failed",
                            title="Project boards database integrity failed",
                            message=str(board_data.get("error") or board_integrity),
                            checked_at=checked_at,
                            request_id=request_id,
                            repository_id=requested_repository,
                            path=str(board_data.get("databasePath") or ""),
                            next_step="Stop board mutations and preserve the application-data directory for recovery review.",
                        )
                    )
            else:
                project_complete = False
                project_data["boards"] = {"available": False}
            if self.releases is not None:
                release_data = self.releases.health_status(requested_repository)
                project_data["releases"] = release_data
                for item in release_data.get("findings", []):
                    project_findings.append(self._finding(
                        section="project", severity=str(item.get("severity") or "warning"),
                        code=str(item.get("code") or "release_asset_integrity_failed"),
                        title="Release asset integrity finding", message=str(item.get("message") or "Release evidence failed verification."),
                        checked_at=checked_at, request_id=request_id, repository_id=requested_repository,
                        next_step="Preserve release application data and restore the verified asset from a trusted copy."
                    ))
                    project_complete = False
            else:
                project_data["releases"] = {"available": False}
        else:
            project_findings.append(
                self._finding(
                    section="project",
                    severity="warning",
                    code="project_coordination_unavailable",
                    title="Project coordination health authority is unavailable",
                    message="This runtime did not provide the project coordination service to Health.",
                    checked_at=checked_at,
                    request_id=request_id,
                    repository_id=requested_repository,
                    next_step="Launch ForgeTrace through the supported owner application entry point.",
                )
            )
        project_section = self._section(
            "Project",
            checked_at,
            project_data,
            project_findings,
            complete=project_complete,
        )

        storage_findings: list[dict[str, Any]] = []
        try:
            application_disk = shutil.disk_usage(self.registry.data_dir)
            application_disk_payload = {
                "totalBytes": application_disk.total,
                "usedBytes": application_disk.used,
                "freeBytes": application_disk.free,
            }
            if application_disk.free < 64 * 1024 * 1024:
                storage_findings.append(
                    self._finding(
                        section="storage",
                        severity="error",
                        code="application_data_low_space",
                        title="Application-data storage is critically low",
                        message="Less than 64 MiB of free space remains for journals, quarantine, and durable evidence.",
                        checked_at=checked_at,
                        request_id=request_id,
                        path=str(self.registry.data_dir),
                        next_step="Free space without deleting active journals, registry rollback backups, or current collaboration evidence.",
                        details=application_disk_payload,
                    )
                )
            elif application_disk.free < 512 * 1024 * 1024:
                storage_findings.append(
                    self._finding(
                        section="storage",
                        severity="warning",
                        code="application_data_space_warning",
                        title="Application-data free space is low",
                        message="Less than 512 MiB remains for future imports, evidence, and recovery artifacts.",
                        checked_at=checked_at,
                        request_id=request_id,
                        path=str(self.registry.data_dir),
                        next_step="Plan additional free space before large imports or collaboration submissions.",
                        details=application_disk_payload,
                    )
                )
        except OSError as exc:
            application_disk_payload = {"error": str(exc)}
            storage_findings.append(
                self._finding(
                    section="storage",
                    severity="warning",
                    code="application_data_disk_unavailable",
                    title="Application-data disk capacity could not be read",
                    message=str(exc),
                    checked_at=checked_at,
                    request_id=request_id,
                    path=str(self.registry.data_dir),
                    next_step="Verify the application-data mount and permissions.",
                )
            )
        report_files = list(self.reports_dir.glob("health_*.json"))
        report_bytes = 0
        for path in report_files:
            try:
                report_bytes += path.stat().st_size
            except OSError:
                continue
        storage_section = self._section(
            "Storage",
            checked_at,
            {
                "applicationData": application_disk_payload,
                "registryBytes": self.registry.db_path.stat().st_size if self.registry.db_path.exists() else 0,
                "securityLedgerBytes": self.security_events.db_path.stat().st_size if self.security_events.db_path.exists() else 0,
                "securitySegmentBytes": security_history.get("storage", {}).get("segmentBytes", 0),
                "securityHistoryBytes": security_history.get("storage", {}).get("totalSecurityHistoryBytes", 0),
                "collaborationBytes": self.collaboration.db_path.stat().st_size if self.collaboration.db_path.exists() else 0,
                "projectCoordinationBytes": int(project_data.get("storageBytes") or 0),
                "registryBackupBytes": sum(int(item.get("bytes") or 0) for item in backups),
                "healthReportCount": len(report_files),
                "healthReportBytes": report_bytes,
                "collaboration": collaboration_data.get("storage", {}),
            },
            storage_findings,
            complete=bool(collaboration_data.get("storage", {}).get("complete")),
        )

        runtime = dict(self.runtime_status() or {})
        owner_lock_path = Path(str(runtime.get("ownerInstanceLockPath") or (self.registry.data_dir / "owner.instance.lock")))
        owner_held = bool(runtime.get("ownerInstanceLockHeld"))
        registry_lock_probe = self._probe_lock(self.registry.operation_lock.path)
        system_findings: list[dict[str, Any]] = []
        if not owner_held:
            system_findings.append(
                self._finding(
                    section="system",
                    severity="warning",
                    code="owner_instance_lock_unconfirmed",
                    title="Owner-instance lock is not confirmed by this runtime",
                    message="The application was likely embedded by a test or alternate launcher rather than the normal single-owner launcher.",
                    checked_at=checked_at,
                    request_id=request_id,
                    path=str(owner_lock_path),
                    next_step="Use START_FORGETRACE or server.py for normal owner operation.",
                )
            )
        if not registry_lock_probe.get("available"):
            system_findings.append(
                self._finding(
                    section="system",
                    severity="warning",
                    code="registry_lock_busy",
                    title="Registry operation lock is currently busy",
                    message="Another protected registry operation may be active.",
                    checked_at=checked_at,
                    request_id=request_id,
                    path=str(self.registry.operation_lock.path),
                    next_step="Allow the current protected operation to finish before starting registry recovery or maintenance.",
                    details=registry_lock_probe,
                )
            )
        system_section = self._section(
            "System",
            checked_at,
            {
                "applicationVersion": APP_VERSION,
                "applicationSchemaVersion": APP_SCHEMA_VERSION,
                "repositorySchemaVersion": REPOSITORY_SCHEMA_VERSION,
                "collaborationSchemaVersion": COLLABORATION_SCHEMA_VERSION,
                "securityEventSchemaVersion": SECURITY_EVENT_SCHEMA_VERSION,
                "securitySegmentSchemaVersion": SECURITY_SEGMENT_SCHEMA_VERSION,
                "securityRotationJournalSchemaVersion": SECURITY_ROTATION_JOURNAL_SCHEMA_VERSION,
                "securityRetentionPolicySchemaVersion": SECURITY_RETENTION_POLICY_SCHEMA_VERSION,
                "securityAnchorSchemaVersion": SECURITY_ANCHOR_SCHEMA_VERSION,
                "gitIntelligenceSchemaVersion": GIT_INTELLIGENCE_SCHEMA_VERSION,
                "gitWriteSchemaVersion": GIT_WRITE_SCHEMA_VERSION,
                "projectCoordinationSchemaVersion": PROJECT_COORDINATION_SCHEMA_VERSION,
                "ownerInstance": {
                    "path": str(owner_lock_path),
                    "heldByCurrentProcess": owner_held,
                },
                "registryLock": registry_lock_probe,
                "gateway": runtime.get("gateway", {}),
            },
            system_findings,
            complete=True,
        )

        sections = {
            "system": system_section,
            "registry": registry_section,
            "repositories": repositories_section,
            "git": git_section,
            "recovery": recovery_section,
            "security": security_section,
            "access": access_section,
            "collaboration": collaboration_section,
            "project": project_section,
            "storage": storage_section,
        }
        all_findings = [
            finding for section in sections.values() for finding in section.get("findings", [])
        ]
        severity_counts = {
            severity: sum(1 for item in all_findings if item.get("severity") == severity)
            for severity in ("critical", "error", "warning", "info")
        }
        worst = max((SEVERITY_ORDER.get(item.get("severity", "warning"), 1) for item in all_findings), default=0)
        complete = all(bool(section.get("complete")) for section in sections.values())
        status = "critical" if worst >= 3 else "failed" if worst >= 2 else "attention" if worst >= 1 else "healthy"
        if not complete and status == "healthy":
            status = "partial"
        report = {
            "format": HEALTH_REPORT_FORMAT,
            "schemaVersion": HEALTH_REPORT_SCHEMA_VERSION,
            "reportId": "health_" + uuid.uuid4().hex,
            "requestId": str(request_id or "")[:120],
            "generatedAt": checked_at,
            "applicationVersion": APP_VERSION,
            "scope": selected_scope,
            "repositoryId": requested_repository,
            "limits": normalized_limits,
            "status": status,
            "complete": complete,
            "summary": {
                "findingCount": len(all_findings),
                "severity": severity_counts,
                "sectionCount": len(sections),
                "completeSectionCount": sum(1 for section in sections.values() if section.get("complete")),
            },
            "sections": sections,
        }
        report["reportHash"] = self._hash_report(report)
        self._write_report(report)
        try:
            self.security_events.append(
                category="health",
                action="health_report_generated",
                outcome="success" if status in {"healthy", "partial"} else "attention_required",
                severity="warning" if status not in {"healthy", "partial"} else "info",
                surface="owner",
                repository_id=requested_repository,
                request_id=str(request_id or ""),
                subject_id=str(report["reportId"]),
                details={
                    "reportHash": report["reportHash"],
                    "status": status,
                    "complete": complete,
                    "findingCount": len(all_findings),
                    "scope": selected_scope,
                },
            )
        except SecurityEventError:
            # A health report must remain available specifically when the ledger is
            # damaged. The report records that damage even if append evidence cannot.
            pass
        return report

    def list_reports(self, *, limit: Any = 25, offset: Any = 0) -> dict[str, Any]:
        bounded_limit = self._bounded_int(limit, default=25, maximum=100)
        try:
            bounded_offset = max(0, int(offset))
        except (TypeError, ValueError):
            bounded_offset = 0
        paths = sorted(
            self.reports_dir.glob("health_*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        reports: list[dict[str, Any]] = []
        for path in paths[bounded_offset : bounded_offset + bounded_limit]:
            try:
                report = self._load_report(path.stem)
                reports.append(
                    {
                        "reportId": report["reportId"],
                        "requestId": report.get("requestId", ""),
                        "generatedAt": report.get("generatedAt", ""),
                        "status": report.get("status", "unknown"),
                        "complete": bool(report.get("complete")),
                        "scope": report.get("scope", "standard"),
                        "repositoryId": report.get("repositoryId", ""),
                        "reportHash": report.get("reportHash", ""),
                        "summary": report.get("summary", {}),
                    }
                )
            except ForgeTraceError as exc:
                reports.append(
                    {
                        "reportId": path.stem,
                        "status": "integrity_failed",
                        "complete": False,
                        "errorCode": exc.code,
                    }
                )
        return {
            "reports": reports,
            "total": len(paths),
            "limit": bounded_limit,
            "offset": bounded_offset,
        }

    def get_report(self, report_id: str) -> dict[str, Any]:
        return self._load_report(report_id)

    def export_report(self, report_id: str, *, request_id: str) -> dict[str, Any]:
        report = self._load_report(report_id)
        exported_at = utc_now()
        try:
            self.security_events.append(
                category="health",
                action="health_report_exported",
                outcome="success",
                surface="owner",
                repository_id=str(report.get("repositoryId") or ""),
                request_id=str(request_id or ""),
                subject_id=report_id,
                details={
                    "reportHash": report.get("reportHash", ""),
                    "generatedRequestId": report.get("requestId", ""),
                },
            )
        except SecurityEventError:
            pass
        return {
            "format": "forgetrace-health-report-export",
            "schemaVersion": 1,
            "exportedAt": exported_at,
            "exportRequestId": str(request_id or "")[:120],
            "report": report,
        }
