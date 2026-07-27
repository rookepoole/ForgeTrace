from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable

from .errors import ForgeTraceError
from .git_intelligence import CONTROL_PATTERN, GitIntelligenceService
from .locks import InterProcessRLock
from .security_events import SecurityEventError, SecurityEventLedger
from .utils import utc_now

GIT_WRITE_SCHEMA_VERSION = 1
PREVIEW_TTL_SECONDS = 10 * 60
MAX_STAGE_PATHS = 500
MAX_MESSAGE_CHARS = 64_000
MAX_AUTHOR_CHARS = 200
MAX_EMAIL_CHARS = 320
MAX_REF_NAME_CHARS = 240
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_CAPTURE_BYTES = 512 * 1024 * 1024
MAX_RECEIPTS = 5_000
RECEIPT_RETENTION_SECONDS = 180 * 24 * 60 * 60
FILE_RETRY_DELAYS_SECONDS = (0.01, 0.025, 0.05, 0.1, 0.2)
TRANSIENT_FILE_ERRNOS = {errno.EACCES, errno.EBUSY, errno.EPERM}
TRANSIENT_WINDOWS_ERRORS = {5, 32, 33, 145}
OID_RE = re.compile(r"[0-9a-fA-F]{40,64}\Z")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
EMAIL_RE = re.compile(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+\Z")
ZERO_OIDS = {"sha1": "0" * 40, "sha256": "0" * 64}
OPERATIONS = {"stage", "commit", "create_branch", "create_tag"}
CONFIRMATIONS = {
    "stage": "STAGE",
    "commit": "COMMIT",
    "create_branch": "CREATE BRANCH",
    "create_tag": "CREATE TAG",
}
GIT_WRITE_RECOVERY_CHECKPOINTS = {
    "captures_sealed",
    "stage_index_installed",
    "commit_tree_object_created",
    "commit_object_created",
    "commit_ref_installed",
    "branch_ref_installed",
    "tag_ref_installed",
    "rollback_files_restored",
    "terminal_journal_rolled_back",
    "rollback_receipt_written",
    "terminal_journal_committed",
    "terminal_receipt_written",
}


class GitWriteInjectedCrash(BaseException):
    """Test-only abrupt-stop signal used by the constructor-injected crash harness.

    This intentionally inherits from BaseException so the normal transactional
    exception handler does not perform an in-process rollback. The durable journal
    is left behind exactly as it would be after a process termination, allowing a
    fresh service instance to prove startup recovery.
    """

    def __init__(self, checkpoint: str) -> None:
        super().__init__(f"Injected transactional Git crash at {checkpoint}")
        self.checkpoint = checkpoint


@dataclass
class GitWriteCommandResult:
    stdout: bytes
    stderr: bytes
    returncode: int
    duration_ms: int
    truncated: bool = False


class GitWriteTransaction:
    """Durable exact-file rollback journal for one local Git metadata mutation."""

    def __init__(
        self,
        service: "GitWriteService",
        *,
        repository_id: str,
        repository_path: Path,
        git_dir: Path,
        operation: str,
        preview_id: str,
        preview_digest: str,
    ) -> None:
        self.service = service
        self.repository_id = repository_id
        self.repository_path = repository_path.resolve()
        self.git_dir = git_dir.resolve()
        self.operation = operation
        self.preview_id = preview_id
        self.preview_digest = preview_digest
        self.id = "git_txn_" + uuid.uuid4().hex
        self.root = service.transactions_dir / self.id
        self.backups = self.root / "backups"
        self.journal_path = self.root / "journal.json"
        self.records: list[dict[str, Any]] = []
        self.created_objects: list[str] = []
        self.started_at = utc_now()
        self.status = "prepared"
        self.last_checkpoint = ""
        self.last_checkpoint_at = ""
        self.checkpoint_details: dict[str, Any] = {}
        self.root.mkdir(parents=True, exist_ok=False)
        self.backups.mkdir(parents=True, exist_ok=True)
        service._fsync_directory(self.root.parent)
        self._write("prepared")

    @classmethod
    def from_journal(cls, service: "GitWriteService", root: Path, payload: dict[str, Any]) -> "GitWriteTransaction":
        digest = str(payload.get("journalDigest") or "")
        unsigned = dict(payload)
        unsigned.pop("journalDigest", None)
        if not digest or digest != service._canonical_digest(unsigned):
            raise ForgeTraceError(
                "Git write transaction journal failed integrity verification.",
                HTTPStatus.CONFLICT,
                "git_write_journal_integrity_failed",
                {"transactionId": root.name},
            )
        if int(payload.get("schemaVersion") or 0) != GIT_WRITE_SCHEMA_VERSION:
            raise ForgeTraceError(
                "Git write transaction journal schema is unsupported.",
                HTTPStatus.CONFLICT,
                "git_write_journal_schema_unsupported",
                {"transactionId": root.name, "schemaVersion": payload.get("schemaVersion")},
            )
        if str(payload.get("transactionId") or "") != root.name:
            raise ForgeTraceError(
                "Git write transaction journal identity does not match its directory.",
                HTTPStatus.CONFLICT,
                "git_write_journal_identity_mismatch",
                {"transactionId": root.name},
            )
        instance = cls.__new__(cls)
        instance.service = service
        instance.repository_id = str(payload.get("repositoryId") or "")
        instance.repository_path = Path(str(payload.get("repositoryPath") or "")).resolve()
        instance.git_dir = Path(str(payload.get("gitDir") or "")).resolve()
        instance.operation = str(payload.get("operation") or "")
        instance.preview_id = str(payload.get("previewId") or "")
        instance.preview_digest = str(payload.get("previewDigest") or "")
        instance.id = root.name
        instance.root = root
        instance.backups = root / "backups"
        instance.journal_path = root / "journal.json"
        records = payload.get("captures")
        instance.records = records if isinstance(records, list) else []
        created = payload.get("createdObjects")
        instance.created_objects = [str(value) for value in created] if isinstance(created, list) else []
        instance.started_at = str(payload.get("createdAt") or "")
        instance.status = str(payload.get("status") or "prepared")
        instance.last_checkpoint = str(payload.get("lastCheckpoint") or "")
        instance.last_checkpoint_at = str(payload.get("lastCheckpointAt") or "")
        checkpoint_details = payload.get("checkpointDetails")
        instance.checkpoint_details = checkpoint_details if isinstance(checkpoint_details, dict) else {}
        return instance

    @staticmethod
    def _safe_relative(value: str) -> str:
        rel = str(value or "").replace("\\", "/").strip("/")
        if not rel or rel == ".." or rel.startswith("../") or "/../" in f"/{rel}/":
            raise ForgeTraceError(
                "Git transaction backup path is invalid.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "git_write_journal_path_invalid",
            )
        return rel

    def _payload(self, status: str, **extra: Any) -> dict[str, Any]:
        return {
            "schemaVersion": GIT_WRITE_SCHEMA_VERSION,
            "transactionId": self.id,
            "repositoryId": self.repository_id,
            "repositoryPath": str(self.repository_path),
            "gitDir": str(self.git_dir),
            "operation": self.operation,
            "previewId": self.preview_id,
            "previewDigest": self.preview_digest,
            "status": status,
            "createdAt": self.started_at,
            "updatedAt": utc_now(),
            "lastCheckpoint": self.last_checkpoint,
            "lastCheckpointAt": self.last_checkpoint_at,
            "checkpointDetails": self.checkpoint_details,
            "captures": self.records,
            "createdObjects": self.created_objects,
            **extra,
        }

    def _write(self, status: str, **extra: Any) -> None:
        self.status = status
        payload = self._payload(status, **extra)
        payload["journalDigest"] = self.service._canonical_digest(payload)
        self.service._atomic_write_json(self.journal_path, payload)

    def checkpoint(self, checkpoint: str, **details: Any) -> None:
        point = str(checkpoint or "").strip()
        if point not in GIT_WRITE_RECOVERY_CHECKPOINTS:
            raise ForgeTraceError(
                "Transactional Git recovery checkpoint is invalid.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "git_write_checkpoint_invalid",
                {"checkpoint": point},
            )
        self.last_checkpoint = point
        self.last_checkpoint_at = utc_now()
        self.checkpoint_details = {
            str(key): value
            for key, value in details.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        self._write(self.status)
        self.service._inject_failure(point, self, self.checkpoint_details)

    def capture(self, relative: str) -> None:
        rel = self._safe_relative(relative)
        if any(str(record.get("path")) == rel for record in self.records):
            return
        source = self.git_dir / rel
        try:
            source.relative_to(self.git_dir)
        except ValueError as exc:
            raise ForgeTraceError(
                "Git transaction capture escaped the administrative directory.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "git_write_journal_path_escape",
            ) from exc
        record: dict[str, Any] = {"path": rel, "existed": False, "sizeBytes": 0, "sha256": ""}
        if source.exists() or source.is_symlink():
            if source.is_symlink() or not source.is_file():
                raise ForgeTraceError(
                    "A Git administrative path required for rollback is not a regular file.",
                    HTTPStatus.CONFLICT,
                    "git_write_admin_path_unsafe",
                    {"path": rel},
                )
            size = source.stat().st_size
            if size > MAX_CAPTURE_BYTES:
                raise ForgeTraceError(
                    "Git rollback backup exceeds the safe transaction limit.",
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "git_write_backup_too_large",
                    {"path": rel, "sizeBytes": size, "limitBytes": MAX_CAPTURE_BYTES},
                )
            destination = self.backups / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with source.open("rb") as source_handle, destination.open("wb") as target_handle:
                while True:
                    chunk = source_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    target_handle.write(chunk)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            self.service._fsync_directory(destination.parent)
            record.update(existed=True, sizeBytes=size, sha256=digest.hexdigest())
        self.records.append(record)
        self._write("prepared")

    def add_created_object(self, oid: str) -> None:
        value = str(oid or "").strip().lower()
        if OID_RE.fullmatch(value) and value not in self.created_objects:
            self.created_objects.append(value)
            self._write("applying")

    def applying(self) -> None:
        self._write("applying", applyingAt=utc_now())

    def _restore_record(self, record: dict[str, Any]) -> None:
        rel = self._safe_relative(str(record.get("path") or ""))
        destination = self.git_dir / rel
        try:
            destination.relative_to(self.git_dir)
        except ValueError as exc:
            raise ForgeTraceError(
                "Git transaction restore escaped the administrative directory.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "git_write_journal_path_escape",
            ) from exc
        if not record.get("existed"):
            if destination.exists() or destination.is_symlink():
                if destination.is_dir() and not destination.is_symlink():
                    raise ForgeTraceError(
                        "Rollback refused to remove an unexpected Git directory.",
                        HTTPStatus.CONFLICT,
                        "git_write_rollback_path_unsafe",
                        {"path": rel},
                    )
                self.service._unlink_required(destination)
                self.service._fsync_directory(destination.parent)
                parent = destination.parent
                while parent != self.git_dir and parent.is_relative_to(self.git_dir):
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    self.service._fsync_directory(parent.parent)
                    parent = parent.parent
            return
        backup = self.backups / rel
        if not backup.is_file() or backup.is_symlink():
            raise ForgeTraceError(
                "Git rollback backup is missing or unsafe.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "git_write_backup_missing",
                {"path": rel},
            )
        expected_size = int(record.get("sizeBytes") or 0)
        expected_digest = str(record.get("sha256") or "")
        actual_size, actual_digest = self.service._hash_file(backup)
        if actual_size != expected_size or actual_digest != expected_digest:
            raise ForgeTraceError(
                "Git rollback backup failed integrity verification.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "git_write_backup_integrity_failed",
                {"path": rel},
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.git-rollback.tmp")
        try:
            with backup.open("rb") as source_handle, temporary.open("wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            self.service._replace_with_retry(temporary, destination)
            self.service._fsync_directory(destination.parent)
        finally:
            try:
                self.service._unlink_required(temporary)
            except OSError:
                pass

    def rollback(self, *, reason: str, recovered: bool = False) -> dict[str, Any]:
        try:
            recovery_origin_checkpoint = self.last_checkpoint
            for record in reversed(self.records):
                self._restore_record(record)
            self.checkpoint(
                "rollback_files_restored",
                captureCount=len(self.records),
                recovered=bool(recovered),
            )
            self.last_checkpoint = "terminal_journal_rolled_back"
            self.last_checkpoint_at = utc_now()
            self.checkpoint_details = {"recovered": bool(recovered)}
            self._write(
                "rolled_back",
                rolledBackAt=utc_now(),
                rollbackReason=str(reason or "")[:1024],
                recovered=bool(recovered),
                recoveryOriginCheckpoint=recovery_origin_checkpoint,
            )
            self.service._inject_failure(
                "terminal_journal_rolled_back",
                self,
                {"recovered": bool(recovered)},
            )
            receipt = self.service._write_receipt(
                self,
                outcome="recovered_rollback" if recovered else "rolled_back",
                details={
                    "reason": str(reason or "")[:1024],
                    "recoveryOriginCheckpoint": recovery_origin_checkpoint,
                },
            )
            self.service._inject_failure(
                "rollback_receipt_written",
                self,
                {"receiptDigest": str(receipt.get("receiptDigest") or "")},
            )
            self.service._best_effort_remove_tree(
                self.root,
                action="cleanup_terminal_git_write_transaction",
                repository_id=self.repository_id,
            )
            return receipt
        except Exception as exc:
            try:
                self._write(
                    "rollback_failed",
                    rollbackFailedAt=utc_now(),
                    rollbackReason=str(reason or "")[:1024],
                    rollbackError=f"{type(exc).__name__}: {exc}"[:2048],
                    recovered=bool(recovered),
                )
            except Exception:
                pass
            raise

    def commit(self, *, before_digest: str, after_digest: str, result: dict[str, Any]) -> dict[str, Any]:
        self.last_checkpoint = "terminal_journal_committed"
        self.last_checkpoint_at = utc_now()
        self.checkpoint_details = {
            "beforeStateDigest": before_digest,
            "afterStateDigest": after_digest,
        }
        self._write(
            "committed",
            committedAt=utc_now(),
            beforeStateDigest=before_digest,
            afterStateDigest=after_digest,
            result=result,
        )
        self.service._inject_failure(
            "terminal_journal_committed",
            self,
            {"beforeStateDigest": before_digest, "afterStateDigest": after_digest},
        )
        receipt = self.service._write_receipt(
            self,
            outcome="committed",
            details={
                "beforeStateDigest": before_digest,
                "afterStateDigest": after_digest,
                "result": result,
            },
        )
        self.service._inject_failure(
            "terminal_receipt_written",
            self,
            {"receiptDigest": str(receipt.get("receiptDigest") or "")},
        )
        self.service._best_effort_remove_tree(
            self.root,
            action="cleanup_terminal_git_write_transaction",
            repository_id=self.repository_id,
        )
        return receipt


class GitWriteService:
    """Owner-only, preview-bound, transactional local Git metadata writes.

    This authority is intentionally separate from GitIntelligenceService. It exposes
    no arbitrary command execution, remotes, credentials, helpers, hooks, checkout,
    merge, reset, fetch, pull, push, clone, or public-hosting capability.
    """

    def __init__(
        self,
        *,
        registry,
        git_intelligence: GitIntelligenceService,
        security_events: SecurityEventLedger | None = None,
        timeout_seconds: float = 15.0,
        failure_injector: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.registry = registry
        self.git_intelligence = git_intelligence
        self.security_events = security_events
        self.git_executable = git_intelligence.git_executable
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))
        self._failure_injector = failure_injector
        self.root = registry.data_dir / "git-writes"
        self.previews_dir = self.root / "previews"
        self.transactions_dir = self.root / "transactions"
        self.receipts_dir = self.root / "receipts"
        self.locks_dir = self.root / "locks"
        for directory in (self.previews_dir, self.transactions_dir, self.receipts_dir, self.locks_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._locks_guard = threading.Lock()
        self._locks: dict[str, InterProcessRLock] = {}
        self._maintenance_guard = threading.Lock()
        self._maintenance_warnings: list[dict[str, Any]] = []
        self._cleanup_previews()
        self._cleanup_receipts()
        self.startup_recovery_report = self.recover_pending_transactions()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    @staticmethod
    def _is_transient_file_error(exc: OSError) -> bool:
        return bool(
            getattr(exc, "winerror", None) in TRANSIENT_WINDOWS_ERRORS
            or getattr(exc, "errno", None) in TRANSIENT_FILE_ERRNOS
        )

    @classmethod
    def _replace_with_retry(cls, source: Path, destination: Path) -> None:
        for attempt in range(len(FILE_RETRY_DELAYS_SECONDS) + 1):
            try:
                os.replace(source, destination)
                return
            except OSError as exc:
                if attempt >= len(FILE_RETRY_DELAYS_SECONDS) or not cls._is_transient_file_error(exc):
                    raise
                time.sleep(FILE_RETRY_DELAYS_SECONDS[attempt])

    @classmethod
    def _unlink_required(cls, path: Path) -> None:
        for attempt in range(len(FILE_RETRY_DELAYS_SECONDS) + 1):
            try:
                path.unlink(missing_ok=True)
                return
            except OSError as exc:
                if attempt >= len(FILE_RETRY_DELAYS_SECONDS) or not cls._is_transient_file_error(exc):
                    raise
                time.sleep(FILE_RETRY_DELAYS_SECONDS[attempt])

    def _record_maintenance_warning(
        self,
        *,
        action: str,
        path: Path,
        exc: OSError,
        repository_id: str = "",
    ) -> None:
        warning = {
            "action": str(action or "maintenance_cleanup"),
            "repositoryId": str(repository_id or ""),
            "path": str(path),
            "errorType": type(exc).__name__,
            "errorCode": getattr(exc, "winerror", None) or getattr(exc, "errno", None),
            "message": str(exc)[:1024],
            "recordedAt": utc_now(),
            "nonCritical": True,
        }
        with self._maintenance_guard:
            self._maintenance_warnings.append(warning)
            del self._maintenance_warnings[:-100]

    def _best_effort_unlink(
        self,
        path: Path,
        *,
        action: str,
        repository_id: str = "",
    ) -> bool:
        try:
            self._unlink_required(path)
            return True
        except OSError as exc:
            self._record_maintenance_warning(
                action=action,
                path=path,
                exc=exc,
                repository_id=repository_id,
            )
            return False

    def _best_effort_remove_tree(
        self,
        path: Path,
        *,
        action: str,
        repository_id: str = "",
    ) -> bool:
        for attempt in range(len(FILE_RETRY_DELAYS_SECONDS) + 1):
            try:
                shutil.rmtree(path)
                self._fsync_directory(path.parent)
                return True
            except FileNotFoundError:
                return True
            except OSError as exc:
                if attempt < len(FILE_RETRY_DELAYS_SECONDS) and self._is_transient_file_error(exc):
                    time.sleep(FILE_RETRY_DELAYS_SECONDS[attempt])
                    continue
                self._record_maintenance_warning(
                    action=action,
                    path=path,
                    exc=exc,
                    repository_id=repository_id,
                )
                return False

    def _inject_failure(
        self,
        checkpoint: str,
        transaction: GitWriteTransaction,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self._failure_injector is None:
            return
        context = {
            "checkpoint": checkpoint,
            "transactionId": transaction.id,
            "repositoryId": transaction.repository_id,
            "repositoryPath": str(transaction.repository_path),
            "operation": transaction.operation,
            "journalPath": str(transaction.journal_path),
            "details": dict(details or {}),
        }
        self._failure_injector(checkpoint, context)

    @classmethod
    def _atomic_write_json(cls, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            cls._replace_with_retry(temporary, path)
            cls._fsync_directory(path.parent)
        finally:
            try:
                cls._unlink_required(temporary)
            except OSError:
                pass

    @staticmethod
    def _hash_file(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    @staticmethod
    def _canonical_digest(payload: Any) -> str:
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _clean_output(data: bytes, limit: int = 4096) -> str:
        return CONTROL_PATTERN.sub("�", data.decode("utf-8", errors="replace")[:limit]).strip()

    def _lock(self, repository_id: str) -> InterProcessRLock:
        key = hashlib.sha256(str(repository_id).encode("utf-8")).hexdigest()
        with self._locks_guard:
            return self._locks.setdefault(
                repository_id,
                InterProcessRLock(self.locks_dir / f"{key}.lock", timeout=60.0),
            )

    def _safe_environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        keep = {
            key: os.environ[key]
            for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "TMP", "TEMP", "TMPDIR")
            if key in os.environ
        }
        keep.update(
            {
                "PATH": os.path.dirname(self.git_executable) or os.defpath,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "1",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_ASKPASS": "",
                "SSH_ASKPASS": "",
                "GCM_INTERACTIVE": "Never",
                "GIT_PAGER": "cat",
                "PAGER": "cat",
                "GIT_EDITOR": "true",
                "GIT_SEQUENCE_EDITOR": "true",
                "GIT_MERGE_AUTOEDIT": "no",
            }
        )
        if extra:
            keep.update({str(key): str(value) for key, value in extra.items()})
        return keep

    def _run(
        self,
        root: Path,
        args: list[str],
        *,
        accepted_codes: set[int] | None = None,
        input_data: bytes | None = None,
        env_extra: dict[str, str] | None = None,
        timeout: float | None = None,
        output_limit: int = MAX_OUTPUT_BYTES,
        operation: str = "inspect",
    ) -> GitWriteCommandResult:
        if not self.git_executable or not Path(self.git_executable).is_file():
            raise ForgeTraceError("Git executable was not found.", HTTPStatus.NOT_IMPLEMENTED, "git_unavailable")
        command = [
            self.git_executable,
            "--no-pager",
            "--literal-pathspecs",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=",
            "-c",
            "core.pager=cat",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "submodule.recurse=false",
            "-c",
            "fetch.recurseSubmodules=false",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=never",
            *args,
        ]
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._safe_environment(env_extra),
            shell=False,
        )
        try:
            stdout, stderr = process.communicate(input=input_data, timeout=timeout or self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            stdout, stderr = process.communicate(timeout=3)
            raise ForgeTraceError(
                "Transactional Git command timed out.",
                HTTPStatus.GATEWAY_TIMEOUT,
                "git_write_command_timeout",
                {"operation": operation, "timeoutSeconds": timeout or self.timeout_seconds},
            ) from exc
        duration = int((time.monotonic() - started) * 1000)
        truncated = len(stdout) > output_limit or len(stderr) > output_limit
        stdout = stdout[:output_limit]
        stderr = stderr[:output_limit]
        if truncated:
            raise ForgeTraceError(
                "Transactional Git command output exceeded the safe limit.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "git_write_output_limit",
                {"operation": operation, "limitBytes": output_limit},
            )
        result = GitWriteCommandResult(stdout, stderr, int(process.returncode), duration, False)
        accepted = accepted_codes or {0}
        if result.returncode not in accepted:
            raise ForgeTraceError(
                "Transactional Git command failed.",
                HTTPStatus.CONFLICT,
                "git_write_command_failed",
                {
                    "operation": operation,
                    "returnCode": result.returncode,
                    "stderr": self._clean_output(result.stderr),
                },
            )
        return result

    def _context(self, repository_id: str) -> tuple[Any, Path, Path]:
        repository = self.registry.repository_service(repository_id)
        context = self.git_intelligence._context(repository_id)
        root = Path(context["root"]).resolve()
        marker = Path(context["marker"])
        if context.get("kind") != "worktree" or not marker.is_dir():
            raise ForgeTraceError(
                "Transactional Git writes currently require a repository-root .git directory. Linked worktrees, gitfiles, bare repositories, symlinks, alternates, and external administrative paths remain read-only.",
                HTTPStatus.CONFLICT,
                "git_write_layout_unsupported",
                {"kind": context.get("kind", "")},
            )
        git_dir = marker.resolve(strict=True)
        if root != git_dir and root not in git_dir.parents:
            raise ForgeTraceError(
                "Git administrative directory escapes the registered repository.",
                HTTPStatus.CONFLICT,
                "git_write_layout_unsupported",
            )
        return repository, root, git_dir

    @staticmethod
    def _protected_path(rel: str) -> bool:
        value = rel.replace("\\", "/").strip("/")
        return value in {".git", ".forgetrace"} or value.startswith(".git/") or value.startswith(".forgetrace/")

    def _normalize_stage_paths(self, repository: Any, root: Path, values: Any) -> list[str]:
        if not isinstance(values, list) or not values:
            raise ForgeTraceError("Select at least one changed path to stage.", code="git_stage_paths_required")
        if len(values) > MAX_STAGE_PATHS:
            raise ForgeTraceError(
                "Too many paths were selected for one staging transaction.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "git_stage_path_limit",
                {"limit": MAX_STAGE_PATHS},
            )
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            raw_value = urllib.parse.unquote(str(raw or "")).replace("\\", "/").strip().lstrip("/")
            raw_normalized = os.path.normpath(raw_value).replace("\\", "/") if raw_value else ""
            if self._protected_path(raw_normalized):
                raise ForgeTraceError(
                    "Git and ForgeTrace administrative paths cannot be staged.",
                    HTTPStatus.FORBIDDEN,
                    "git_stage_protected_path",
                    {"path": raw_normalized},
                )
            rel = repository.normalize_rel(str(raw or ""))
            if self._protected_path(rel):
                raise ForgeTraceError(
                    "Git and ForgeTrace administrative paths cannot be staged.",
                    HTTPStatus.FORBIDDEN,
                    "git_stage_protected_path",
                    {"path": rel},
                )
            candidate = root / rel
            if candidate.exists() and candidate.is_dir() and not candidate.is_symlink():
                raise ForgeTraceError(
                    "Stage individual changed files rather than directories.",
                    code="git_stage_directory_unsupported",
                    details={"path": rel},
                )
            if rel not in seen:
                normalized.append(rel)
                seen.add(rel)
        return normalized

    def _native_locks(self, git_dir: Path) -> list[str]:
        candidates = [git_dir / "index.lock", git_dir / "HEAD.lock", git_dir / "packed-refs.lock"]
        for namespace in (git_dir / "refs" / "heads", git_dir / "refs" / "tags"):
            if not namespace.is_dir():
                continue
            count = 0
            for candidate in namespace.rglob("*.lock"):
                candidates.append(candidate)
                count += 1
                if count >= 1000:
                    break
        found = []
        for candidate in candidates:
            if candidate.exists() or candidate.is_symlink():
                try:
                    found.append(candidate.relative_to(git_dir).as_posix())
                except ValueError:
                    found.append(candidate.name)
        return sorted(set(found))

    @staticmethod
    def _active_administrative_paths(git_dir: Path) -> list[str]:
        active = []
        for name in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "BISECT_LOG",
            "rebase-apply",
            "rebase-merge",
            "sequencer",
        ):
            path = git_dir / name
            if path.exists() or path.is_symlink():
                active.append(name)
        return active

    def _assert_operation_state(self, git_dir: Path) -> None:
        active = self._active_administrative_paths(git_dir)
        if active:
            raise ForgeTraceError(
                "A merge, rebase, cherry-pick, revert, or bisect state is active. Finish or abort it outside ForgeTrace before using transactional Git writes.",
                HTTPStatus.LOCKED,
                "git_operation_in_progress",
                {"administrativePaths": active},
            )
        locks = self._native_locks(git_dir)
        if locks:
            raise ForgeTraceError(
                "Git administrative lock files are present. Finish the external Git operation or remove only confirmed-stale locks before retrying.",
                HTTPStatus.LOCKED,
                "git_native_lock_present",
                {"locks": locks},
            )

    def _object_format(self, root: Path) -> str:
        result = self._run(root, ["rev-parse", "--show-object-format"], operation="object_format")
        value = self._clean_output(result.stdout, 32).lower()
        if value not in ZERO_OIDS:
            raise ForgeTraceError(
                "Git object format is unsupported.",
                HTTPStatus.CONFLICT,
                "git_object_format_unsupported",
                {"format": value},
            )
        return value

    def _head_state(self, root: Path) -> dict[str, str]:
        oid_result = self._run(
            root,
            ["rev-parse", "--verify", "HEAD"],
            accepted_codes={0, 128},
            operation="head_probe",
        )
        oid = self._clean_output(oid_result.stdout, 128) if oid_result.returncode == 0 else ""
        ref_result = self._run(
            root,
            ["symbolic-ref", "-q", "HEAD"],
            accepted_codes={0, 1},
            operation="head_ref_probe",
        )
        ref = self._clean_output(ref_result.stdout, 512) if ref_result.returncode == 0 else ""
        if oid and not OID_RE.fullmatch(oid):
            raise ForgeTraceError("Git HEAD returned an invalid object ID.", HTTPStatus.CONFLICT, "git_head_invalid")
        if ref and not ref.startswith("refs/heads/"):
            raise ForgeTraceError(
                "Git HEAD points outside the supported local branch namespace.",
                HTTPStatus.CONFLICT,
                "git_head_ref_unsupported",
                {"headRef": ref},
            )
        return {"oid": oid.lower(), "ref": ref}

    def _status_bytes(self, root: Path) -> bytes:
        return self._run(
            root,
            ["status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all", "--ignore-submodules=all"],
            operation="status",
        ).stdout

    def _staged_summary(self, root: Path) -> dict[str, Any]:
        result = self._run(
            root,
            ["diff", "--cached", "--name-status", "-z", "--no-renames", "--ignore-submodules=all"],
            operation="staged_summary",
        )
        parts = [part.decode("utf-8", errors="replace") for part in result.stdout.split(b"\0") if part]
        entries: list[dict[str, str]] = []
        index = 0
        while index + 1 < len(parts):
            status_value = CONTROL_RE.sub("�", parts[index])[:16]
            path_value = CONTROL_RE.sub("�", parts[index + 1])[:4096]
            entries.append({"status": status_value, "path": path_value})
            index += 2
        return {
            "count": len(entries),
            "entries": entries[:MAX_STAGE_PATHS],
            "truncated": len(entries) > MAX_STAGE_PATHS,
            "digest": hashlib.sha256(result.stdout).hexdigest(),
            "bytes": len(result.stdout),
        }

    def _ref_digest(self, root: Path) -> str:
        result = self._run(
            root,
            ["for-each-ref", "--format=%(refname)%00%(objectname)", "refs/heads/", "refs/tags/"],
            operation="ref_digest",
        )
        return hashlib.sha256(result.stdout).hexdigest()

    def _base_state(self, root: Path, git_dir: Path) -> dict[str, Any]:
        head = self._head_state(root)
        status = self._status_bytes(root)
        index_path = git_dir / "index"
        if index_path.is_symlink():
            raise ForgeTraceError(
                "The Git index is a symbolic link and cannot be mutated.",
                HTTPStatus.CONFLICT,
                "git_write_admin_path_unsafe",
                {"path": "index"},
            )
        if index_path.is_file():
            index_size, index_digest = self._hash_file(index_path)
        elif index_path.exists():
            raise ForgeTraceError(
                "The Git index is not a regular file.",
                HTTPStatus.CONFLICT,
                "git_write_admin_path_unsafe",
                {"path": "index"},
            )
        else:
            index_size, index_digest = 0, ""
        return {
            "objectFormat": self._object_format(root),
            "headOid": head["oid"],
            "headRef": head["ref"],
            "indexSha256": index_digest,
            "indexBytes": index_size,
            "statusSha256": hashlib.sha256(status).hexdigest(),
            "statusBytes": len(status),
            "refsSha256": self._ref_digest(root),
        }

    @staticmethod
    def _lstat_state(path: Path) -> dict[str, Any]:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return {"kind": "missing", "sizeBytes": 0, "mode": 0, "sha256": ""}
        mode = stat.S_IFMT(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            data = target.encode("utf-8", errors="surrogateescape")
            return {
                "kind": "symlink",
                "sizeBytes": len(data),
                "mode": int(info.st_mode & 0o7777),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        if not stat.S_ISREG(info.st_mode):
            raise ForgeTraceError(
                "Only regular files, safe symbolic links, and deleted paths can be staged.",
                HTTPStatus.CONFLICT,
                "git_stage_path_kind_unsupported",
                {"path": str(path), "mode": mode},
            )
        size, digest = GitWriteService._hash_file(path)
        return {
            "kind": "file",
            "sizeBytes": size,
            "mode": int(info.st_mode & 0o7777),
            "sha256": digest,
        }

    def _assert_no_external_filters(self, root: Path, paths: list[str]) -> None:
        result = self._run(
            root,
            ["check-attr", "-z", "filter", "working-tree-encoding", "--", *paths],
            operation="attribute_preflight",
        )
        fields = [part.decode("utf-8", errors="replace") for part in result.stdout.split(b"\0") if part]
        blocked: list[dict[str, str]] = []
        for offset in range(0, len(fields) - 2, 3):
            path, attribute, value = fields[offset : offset + 3]
            if value not in {"", "unspecified", "unset"}:
                blocked.append({"path": path[:4096], "attribute": attribute[:128], "value": value[:256]})
        if blocked:
            raise ForgeTraceError(
                "Selected paths use Git clean filters or working-tree encodings. ForgeTrace will not invoke external content helpers or silently change those semantics.",
                HTTPStatus.CONFLICT,
                "git_stage_external_filter_blocked",
                {"attributes": blocked[:50]},
            )

    def _changed_paths(self, root: Path) -> set[str]:
        status = self.git_intelligence._status(root)
        changed: set[str] = set()
        for item in status.get("changes", []):
            path = str(item.get("path") or "")
            original = str(item.get("originalPath") or "")
            if path:
                changed.add(path)
            if original:
                changed.add(original)
        return changed

    def _stage_state(self, repository: Any, root: Path, git_dir: Path, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        paths = self._normalize_stage_paths(repository, root, data.get("paths"))
        changed = self._changed_paths(root)
        missing = [path for path in paths if path not in changed]
        if missing:
            raise ForgeTraceError(
                "Every staged path must be present in the current bounded Git change set.",
                HTTPStatus.CONFLICT,
                "git_stage_path_not_changed",
                {"paths": missing[:50]},
            )
        self._assert_no_external_filters(root, paths)
        selected = [{"path": path, **self._lstat_state(root / path)} for path in paths]
        state = {"base": self._base_state(root, git_dir), "selected": selected}
        return state, {"paths": paths}

    @staticmethod
    def _clean_field(value: Any, *, label: str, limit: int, required: bool = True) -> str:
        text = str(value or "").strip()
        if required and not text:
            raise ForgeTraceError(f"{label} is required.", code="git_write_field_required", details={"field": label})
        if len(text) > limit or CONTROL_RE.search(text):
            raise ForgeTraceError(
                f"{label} is invalid or too long.",
                code="git_write_field_invalid",
                details={"field": label, "limit": limit},
            )
        return text

    def _commit_input(self, data: dict[str, Any]) -> dict[str, str]:
        message = str(data.get("message") or "").strip()
        if not message:
            raise ForgeTraceError("Commit message is required.", code="git_commit_message_required")
        if len(message) > MAX_MESSAGE_CHARS or "\x00" in message:
            raise ForgeTraceError(
                "Commit message is invalid or too long.",
                code="git_commit_message_invalid",
                details={"limit": MAX_MESSAGE_CHARS},
            )
        name = self._clean_field(data.get("authorName"), label="Author name", limit=MAX_AUTHOR_CHARS)
        email = self._clean_field(data.get("authorEmail"), label="Author email", limit=MAX_EMAIL_CHARS)
        if not EMAIL_RE.fullmatch(email):
            raise ForgeTraceError("Author email is invalid.", code="git_author_email_invalid")
        return {"message": message, "authorName": name, "authorEmail": email}

    def _commit_state(self, root: Path, git_dir: Path, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        clean = self._commit_input(data)
        status = self.git_intelligence._status(root)
        conflicts = [item for item in status.get("changes", []) if item.get("kind") == "unmerged"]
        if conflicts:
            raise ForgeTraceError(
                "Unmerged paths are present. Merge conflict commits are outside v0.5.2.",
                HTTPStatus.CONFLICT,
                "git_commit_unmerged_paths",
                {"paths": [item.get("path") for item in conflicts[:50]]},
            )
        staged = self._staged_summary(root)
        if staged["count"] == 0:
            raise ForgeTraceError("There are no staged changes to commit.", HTTPStatus.CONFLICT, "git_commit_nothing_staged")
        state = {"base": self._base_state(root, git_dir), "staged": staged}
        return state, clean

    def _validate_ref_name(self, root: Path, namespace: str, raw: Any) -> tuple[str, str]:
        name = self._clean_field(raw, label="Reference name", limit=MAX_REF_NAME_CHARS)
        if name.startswith("refs/"):
            raise ForgeTraceError(
                "Enter a local branch or tag name without the refs/ prefix.",
                code="git_ref_name_invalid",
            )
        # Keep the owner workflow inside a deliberately conservative local-ref
        # alphabet. Git accepts shell metacharacters in reference names, but
        # ForgeTrace never needs them and rejecting them keeps receipts, logs,
        # and future UI rendering unambiguous.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", name):
            raise ForgeTraceError(
                "Reference names may contain only letters, numbers, dots, underscores, slashes, and hyphens, and must begin with a letter or number.",
                code="git_ref_name_invalid",
            )
        full = f"refs/{namespace}/{name}"
        self._run(root, ["check-ref-format", full], operation="ref_name_validation")
        return name, full

    def _resolve_commit_oid(self, root: Path, raw: Any) -> str:
        value = str(raw or "").strip()
        if not value:
            head = self._head_state(root)["oid"]
            if not head:
                raise ForgeTraceError("A target commit is required because HEAD is unborn.", code="git_target_oid_required")
            return head
        if not OID_RE.fullmatch(value):
            raise ForgeTraceError(
                "Target commit must be a full hexadecimal object ID.",
                code="invalid_git_object_id",
            )
        result = self._run(
            root,
            ["rev-parse", "--verify", f"{value}^{{commit}}"],
            operation="target_commit_validation",
        )
        resolved = self._clean_output(result.stdout, 128).lower()
        if not OID_RE.fullmatch(resolved):
            raise ForgeTraceError("Target commit could not be resolved.", HTTPStatus.CONFLICT, "git_target_invalid")
        return resolved

    def _ref_exists(self, root: Path, full_ref: str) -> bool:
        result = self._run(
            root,
            ["show-ref", "--verify", "--quiet", full_ref],
            accepted_codes={0, 1},
            operation="ref_exists",
        )
        return result.returncode == 0

    def _ref_state(self, root: Path, git_dir: Path, data: dict[str, Any], *, namespace: str) -> tuple[dict[str, Any], dict[str, Any]]:
        name, full_ref = self._validate_ref_name(root, namespace, data.get("name"))
        if self._ref_exists(root, full_ref):
            raise ForgeTraceError(
                "That local Git reference already exists.",
                HTTPStatus.CONFLICT,
                "git_ref_exists",
                {"ref": full_ref},
            )
        target = self._resolve_commit_oid(root, data.get("targetOid"))
        state = {
            "base": self._base_state(root, git_dir),
            "ref": {"name": name, "fullRef": full_ref, "targetOid": target, "exists": False},
        }
        return state, {"name": name, "fullRef": full_ref, "targetOid": target}

    def _operation_state(
        self,
        repository: Any,
        root: Path,
        git_dir: Path,
        operation: str,
        data: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._assert_operation_state(git_dir)
        if operation == "stage":
            return self._stage_state(repository, root, git_dir, data)
        if operation == "commit":
            return self._commit_state(root, git_dir, data)
        if operation == "create_branch":
            return self._ref_state(root, git_dir, data, namespace="heads")
        if operation == "create_tag":
            return self._ref_state(root, git_dir, data, namespace="tags")
        raise ForgeTraceError("Git write operation is invalid.", code="git_write_operation_invalid")

    def _preview_path(self, preview_id: str) -> Path:
        if not re.fullmatch(r"git_preview_[0-9a-f]{32}", str(preview_id or "")):
            raise ForgeTraceError("Git write preview identifier is invalid.", code="git_write_preview_invalid")
        return self.previews_dir / f"{preview_id}.json"

    def preview(self, repository_id: str, data: dict[str, Any]) -> dict[str, Any]:
        operation = str(data.get("operation") or "").strip().lower()
        if operation not in OPERATIONS:
            raise ForgeTraceError(
                "Git write operation must be stage, commit, create_branch, or create_tag.",
                code="git_write_operation_invalid",
                details={"allowed": sorted(OPERATIONS)},
            )
        repository, root, git_dir = self._context(repository_id)
        with repository.lock, self._lock(repository_id):
            repository.require_writable("transactional local Git write preview")
            state, clean_input = self._operation_state(repository, root, git_dir, operation, data)
            state_digest = self._canonical_digest(state)
            preview_id = "git_preview_" + uuid.uuid4().hex
            now = int(time.time())
            payload = {
                "schemaVersion": GIT_WRITE_SCHEMA_VERSION,
                "previewId": preview_id,
                "repositoryId": repository_id,
                "repositoryPath": str(root),
                "operation": operation,
                "input": clean_input,
                "state": state,
                "stateDigest": state_digest,
                "createdAt": utc_now(),
                "createdAtEpoch": now,
                "expiresAtEpoch": now + PREVIEW_TTL_SECONDS,
                "expiresInSeconds": PREVIEW_TTL_SECONDS,
                "requiredConfirmation": CONFIRMATIONS[operation],
                "authority": {
                    "ownerOnly": True,
                    "network": False,
                    "credentials": False,
                    "hooks": False,
                    "helpers": False,
                    "shell": False,
                    "readOnlyEnforced": True,
                },
            }
            payload["previewDigest"] = self._canonical_digest(payload)
            self._atomic_write_json(self._preview_path(preview_id), payload)
            return payload

    def _load_preview(self, repository_id: str, preview_id: str) -> dict[str, Any]:
        path = self._preview_path(preview_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ForgeTraceError(
                "Git write preview was not found or has expired.",
                HTTPStatus.NOT_FOUND,
                "git_write_preview_not_found",
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ForgeTraceError(
                "Git write preview is unreadable.",
                HTTPStatus.CONFLICT,
                "git_write_preview_unreadable",
            ) from exc
        if not isinstance(payload, dict):
            raise ForgeTraceError("Git write preview is invalid.", HTTPStatus.CONFLICT, "git_write_preview_invalid")
        digest = str(payload.get("previewDigest") or "")
        unsigned = dict(payload)
        unsigned.pop("previewDigest", None)
        if not digest or digest != self._canonical_digest(unsigned):
            raise ForgeTraceError(
                "Git write preview failed integrity verification.",
                HTTPStatus.CONFLICT,
                "git_write_preview_integrity_failed",
            )
        if payload.get("repositoryId") != repository_id:
            raise ForgeTraceError(
                "Git write preview belongs to a different repository.",
                HTTPStatus.CONFLICT,
                "git_write_preview_repository_mismatch",
            )
        if int(payload.get("expiresAtEpoch") or 0) < int(time.time()):
            self._best_effort_unlink(
                path,
                action="cleanup_expired_git_write_preview",
                repository_id=repository_id,
            )
            raise ForgeTraceError(
                "Git write preview expired. Generate a new preview.",
                HTTPStatus.CONFLICT,
                "git_write_preview_expired",
            )
        return payload

    def _audit(
        self,
        *,
        action: str,
        outcome: str,
        repository_id: str,
        subject_id: str,
        actor: str,
        request_id: str,
        surface: str,
        details: dict[str, Any],
        required: bool,
    ) -> None:
        if self.security_events is None:
            if required:
                raise ForgeTraceError(
                    "The security event ledger is unavailable. The protected Git action was blocked.",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "security_event_ledger_unavailable",
                )
            return
        try:
            if required:
                self.security_events.assert_writable()
            self.security_events.append(
                category="git_write",
                action=action,
                outcome=outcome,
                severity="warning" if outcome in {"failure", "rolled_back"} else "info",
                surface=surface or "owner",
                repository_id=repository_id,
                request_id=request_id,
                actor=actor,
                subject_id=subject_id,
                details=details,
            )
        except SecurityEventError as exc:
            if required:
                raise ForgeTraceError(
                    "The security event ledger is unavailable or failed integrity verification. The protected Git action was blocked.",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "security_event_ledger_unavailable",
                    {"reason": str(exc)},
                ) from exc

    def _transaction_captures(self, transaction: GitWriteTransaction, operation: str, input_data: dict[str, Any], state: dict[str, Any]) -> None:
        if operation in {"stage", "commit"}:
            transaction.capture("index")
        if operation == "commit":
            head_ref = str(state.get("base", {}).get("headRef") or "")
            transaction.capture("HEAD")
            transaction.capture("logs/HEAD")
            if head_ref:
                transaction.capture(head_ref)
                transaction.capture("logs/" + head_ref)
        elif operation in {"create_branch", "create_tag"}:
            full_ref = str(input_data.get("fullRef") or "")
            transaction.capture(full_ref)
            transaction.capture("logs/" + full_ref)

    def _execute_stage(self, root: Path, input_data: dict[str, Any]) -> dict[str, Any]:
        paths = list(input_data.get("paths") or [])
        result = self._run(root, ["add", "--all", "--", *paths], operation="stage")
        staged = self._staged_summary(root)
        staged_paths = {str(item.get("path") or "") for item in staged.get("entries", [])}
        absent = [path for path in paths if path not in staged_paths]
        # A selected path can disappear from name-status when the index already matched HEAD;
        # preview forbids unchanged paths, so disappearance here signals a race or no-op.
        if absent:
            raise ForgeTraceError(
                "Git staging did not produce the expected index entries.",
                HTTPStatus.CONFLICT,
                "git_stage_post_verify_failed",
                {"paths": absent[:50]},
            )
        return {
            "operation": "stage",
            "paths": paths,
            "stagedCount": staged["count"],
            "durationMs": result.duration_ms,
        }

    def _execute_commit(
        self,
        root: Path,
        transaction: GitWriteTransaction,
        input_data: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        environment = {
            "GIT_AUTHOR_NAME": str(input_data["authorName"]),
            "GIT_AUTHOR_EMAIL": str(input_data["authorEmail"]),
            "GIT_COMMITTER_NAME": str(input_data["authorName"]),
            "GIT_COMMITTER_EMAIL": str(input_data["authorEmail"]),
        }
        tree_result = self._run(root, ["write-tree"], operation="commit_write_tree")
        tree_oid = self._clean_output(tree_result.stdout, 128).lower()
        if not OID_RE.fullmatch(tree_oid):
            raise ForgeTraceError("Git write-tree returned an invalid object ID.", HTTPStatus.CONFLICT, "git_commit_tree_invalid")
        transaction.add_created_object(tree_oid)
        transaction.checkpoint("commit_tree_object_created", treeOid=tree_oid)
        head_oid = str(state.get("base", {}).get("headOid") or "")
        arguments = ["commit-tree", tree_oid]
        if head_oid:
            arguments.extend(["-p", head_oid])
        message = str(input_data["message"]).rstrip() + "\n"
        commit_result = self._run(
            root,
            arguments,
            input_data=message.encode("utf-8"),
            env_extra=environment,
            operation="commit_create_object",
        )
        commit_oid = self._clean_output(commit_result.stdout, 128).lower()
        if not OID_RE.fullmatch(commit_oid):
            raise ForgeTraceError("Git commit-tree returned an invalid object ID.", HTTPStatus.CONFLICT, "git_commit_object_invalid")
        transaction.add_created_object(commit_oid)
        transaction.checkpoint("commit_object_created", commitOid=commit_oid)
        object_format = str(state.get("base", {}).get("objectFormat") or "sha1")
        zero = ZERO_OIDS[object_format]
        head_ref = str(state.get("base", {}).get("headRef") or "")
        target_ref = head_ref or "HEAD"
        subject = CONTROL_RE.sub(" ", message.splitlines()[0])[:120]
        self._run(
            root,
            ["update-ref", "-m", f"ForgeTrace commit: {subject}", target_ref, commit_oid, head_oid or zero],
            operation="commit_update_ref",
        )
        transaction.checkpoint(
            "commit_ref_installed",
            ref=target_ref,
            commitOid=commit_oid,
        )
        after_head = self._head_state(root)
        if after_head["oid"] != commit_oid:
            raise ForgeTraceError(
                "Git commit reference failed post-write verification.",
                HTTPStatus.CONFLICT,
                "git_commit_post_verify_failed",
            )
        staged_after = self._staged_summary(root)
        if staged_after["count"] != 0:
            raise ForgeTraceError(
                "Git commit left unexpected staged differences after reference update.",
                HTTPStatus.CONFLICT,
                "git_commit_index_post_verify_failed",
            )
        return {
            "operation": "commit",
            "commitOid": commit_oid,
            "treeOid": tree_oid,
            "parentOid": head_oid,
            "headRef": target_ref,
            "subject": subject,
            "stagedPathCount": int(state.get("staged", {}).get("count") or 0),
        }

    def _execute_ref_create(
        self,
        root: Path,
        transaction: GitWriteTransaction,
        state: dict[str, Any],
        input_data: dict[str, Any],
        *,
        operation: str,
    ) -> dict[str, Any]:
        full_ref = str(input_data["fullRef"])
        target = str(input_data["targetOid"])
        zero = ZERO_OIDS[str(state.get("base", {}).get("objectFormat") or "sha1")]
        description = "branch" if operation == "create_branch" else "tag"
        self._run(
            root,
            ["update-ref", "--create-reflog", "-m", f"ForgeTrace create {description}", full_ref, target, zero],
            operation=operation,
        )
        transaction.checkpoint(
            "branch_ref_installed" if operation == "create_branch" else "tag_ref_installed",
            ref=full_ref,
            targetOid=target,
        )
        result = self._run(root, ["rev-parse", "--verify", full_ref], operation=f"{operation}_verify")
        actual = self._clean_output(result.stdout, 128).lower()
        if actual != target:
            raise ForgeTraceError(
                "Created Git reference failed post-write verification.",
                HTTPStatus.CONFLICT,
                "git_ref_post_verify_failed",
                {"ref": full_ref, "expected": target, "actual": actual},
            )
        return {
            "operation": operation,
            "name": str(input_data["name"]),
            "ref": full_ref,
            "targetOid": target,
        }

    def _apply(
        self,
        root: Path,
        transaction: GitWriteTransaction,
        operation: str,
        input_data: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        if operation == "stage":
            result = self._execute_stage(root, input_data)
            transaction.checkpoint(
                "stage_index_installed",
                pathCount=len(input_data.get("paths") or []),
            )
            return result
        if operation == "commit":
            return self._execute_commit(root, transaction, input_data, state)
        if operation in {"create_branch", "create_tag"}:
            return self._execute_ref_create(root, transaction, state, input_data, operation=operation)
        raise ForgeTraceError("Git write operation is invalid.", code="git_write_operation_invalid")

    def execute(
        self,
        repository_id: str,
        *,
        preview_id: str,
        confirmation: str,
        actor: str,
        request_id: str,
        surface: str = "owner",
    ) -> dict[str, Any]:
        preview = self._load_preview(repository_id, preview_id)
        operation = str(preview.get("operation") or "")
        required_confirmation = str(preview.get("requiredConfirmation") or "")
        if str(confirmation or "").strip() != required_confirmation:
            raise ForgeTraceError(
                f"Type {required_confirmation} exactly to authorize this Git write.",
                HTTPStatus.CONFLICT,
                "git_write_confirmation_mismatch",
                {"requiredConfirmation": required_confirmation},
            )
        actor_value = self._clean_field(actor or "Repository Owner", label="Actor", limit=MAX_AUTHOR_CHARS)
        repository, root, git_dir = self._context(repository_id)
        preview_path = self._preview_path(preview_id)
        transaction: GitWriteTransaction | None = None
        with repository.lock, self._lock(repository_id):
            repository.require_writable("transactional local Git write")
            if str(preview.get("repositoryPath") or "") != str(root):
                raise ForgeTraceError(
                    "Repository path changed after the Git write preview.",
                    HTTPStatus.CONFLICT,
                    "git_write_preview_stale",
                )
            current_state, clean_input = self._operation_state(
                repository,
                root,
                git_dir,
                operation,
                dict(preview.get("input") or {}),
            )
            current_digest = self._canonical_digest(current_state)
            if current_digest != str(preview.get("stateDigest") or ""):
                self._best_effort_unlink(
                    preview_path,
                    action="cleanup_stale_git_write_preview",
                    repository_id=repository_id,
                )
                raise ForgeTraceError(
                    "Git state changed after the preview. Generate a new preview before writing.",
                    HTTPStatus.CONFLICT,
                    "git_write_preview_stale",
                    {
                        "previewStateDigest": preview.get("stateDigest", ""),
                        "currentStateDigest": current_digest,
                    },
                )
            details = {
                "operation": operation,
                "previewId": preview_id,
                "previewDigest": preview.get("previewDigest", ""),
                "stateDigest": current_digest,
            }
            if operation == "stage":
                details["pathCount"] = len(clean_input.get("paths") or [])
                details["pathsDigest"] = self._canonical_digest(clean_input.get("paths") or [])
            elif operation == "commit":
                details["messageSha256"] = hashlib.sha256(str(clean_input.get("message") or "").encode("utf-8")).hexdigest()
                details["stagedPathCount"] = int(current_state.get("staged", {}).get("count") or 0)
                details["authorName"] = str(clean_input.get("authorName") or "")
                details["authorEmailSha256"] = hashlib.sha256(str(clean_input.get("authorEmail") or "").encode("utf-8")).hexdigest()
            else:
                details["ref"] = str(clean_input.get("fullRef") or "")
                details["targetOid"] = str(clean_input.get("targetOid") or "")
            self._audit(
                action=f"git_{operation}_authorized",
                outcome="authorized",
                repository_id=repository_id,
                subject_id=preview_id,
                actor=actor_value,
                request_id=request_id,
                surface=surface,
                details=details,
                required=True,
            )
            transaction = GitWriteTransaction(
                self,
                repository_id=repository_id,
                repository_path=root,
                git_dir=git_dir,
                operation=operation,
                preview_id=preview_id,
                preview_digest=str(preview.get("previewDigest") or ""),
            )
            self._transaction_captures(transaction, operation, clean_input, current_state)
            transaction.applying()
            transaction.checkpoint(
                "captures_sealed",
                captureCount=len(transaction.records),
                operation=operation,
            )
            try:
                result = self._apply(root, transaction, operation, clean_input, current_state)
                after_state = self._base_state(root, git_dir)
                after_digest = self._canonical_digest(after_state)
                success_details = {**details, **result, "transactionId": transaction.id, "afterStateDigest": after_digest}
                self._audit(
                    action=f"git_{operation}_completed",
                    outcome="success",
                    repository_id=repository_id,
                    subject_id=transaction.id,
                    actor=actor_value,
                    request_id=request_id,
                    surface=surface,
                    details=success_details,
                    required=True,
                )
                receipt = transaction.commit(
                    before_digest=current_digest,
                    after_digest=after_digest,
                    result=result,
                )
                self._best_effort_unlink(
                    preview_path,
                    action="cleanup_consumed_git_write_preview",
                    repository_id=repository_id,
                )
                return {
                    "schemaVersion": GIT_WRITE_SCHEMA_VERSION,
                    "repositoryId": repository_id,
                    "transactionId": transaction.id,
                    "previewId": preview_id,
                    "result": result,
                    "receipt": receipt,
                    "rolledBack": False,
                    "networkUsed": False,
                    "hooksExecuted": False,
                    "credentialsUsed": False,
                }
            except Exception as exc:
                try:
                    transaction.rollback(reason=f"{type(exc).__name__}: {exc}")
                except Exception as rollback_exc:
                    self._audit(
                        action=f"git_{operation}_rollback_failed",
                        outcome="failure",
                        repository_id=repository_id,
                        subject_id=transaction.id,
                        actor=actor_value,
                        request_id=request_id,
                        surface=surface,
                        details={
                            **details,
                            "transactionId": transaction.id,
                            "errorType": type(exc).__name__,
                            "rollbackErrorType": type(rollback_exc).__name__,
                        },
                        required=False,
                    )
                    raise ForgeTraceError(
                        "Transactional Git write failed and exact rollback could not be completed. The durable journal was retained for startup recovery and manual inspection.",
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "git_write_rollback_failed",
                        {"transactionId": transaction.id},
                    ) from rollback_exc
                self._audit(
                    action=f"git_{operation}_rolled_back",
                    outcome="rolled_back",
                    repository_id=repository_id,
                    subject_id=transaction.id,
                    actor=actor_value,
                    request_id=request_id,
                    surface=surface,
                    details={
                        **details,
                        "transactionId": transaction.id,
                        "errorType": type(exc).__name__,
                        "errorCode": getattr(exc, "code", ""),
                    },
                    required=False,
                )
                self._best_effort_unlink(
                    preview_path,
                    action="cleanup_failed_git_write_preview",
                    repository_id=repository_id,
                )
                raise

    def _receipt_path(self, transaction_id: str) -> Path:
        return self.receipts_dir / f"{transaction_id}.json"

    def _write_receipt(self, transaction: GitWriteTransaction, *, outcome: str, details: dict[str, Any]) -> dict[str, Any]:
        capture_manifest = [
            {
                "path": str(record.get("path") or ""),
                "existed": bool(record.get("existed")),
                "sizeBytes": int(record.get("sizeBytes") or 0),
                "sha256": str(record.get("sha256") or ""),
            }
            for record in transaction.records
        ]
        payload = {
            "schemaVersion": GIT_WRITE_SCHEMA_VERSION,
            "transactionId": transaction.id,
            "repositoryId": transaction.repository_id,
            "repositoryPath": str(transaction.repository_path),
            "operation": transaction.operation,
            "previewId": transaction.preview_id,
            "previewDigest": transaction.preview_digest,
            "outcome": outcome,
            "createdAt": transaction.started_at,
            "completedAt": utc_now(),
            "lastCheckpoint": transaction.last_checkpoint,
            "lastCheckpointAt": transaction.last_checkpoint_at,
            "captureManifest": capture_manifest,
            "captureManifestDigest": self._canonical_digest(capture_manifest),
            "createdObjects": list(transaction.created_objects),
            "details": details,
            "limitations": {
                "unreachableCreatedObjectsMayRemainAfterRollback": bool(transaction.created_objects and outcome != "committed"),
                "remotePublicationVerified": False,
            },
        }
        payload["receiptDigest"] = self._canonical_digest(payload)
        self._atomic_write_json(self._receipt_path(transaction.id), payload)
        self._cleanup_receipts()
        return payload

    def _receipt_integrity(self, transaction_id: str) -> tuple[str, dict[str, Any] | None]:
        path = self._receipt_path(transaction_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "missing", None
        except (OSError, json.JSONDecodeError):
            return "invalid", None
        if not isinstance(payload, dict):
            return "invalid", None
        digest = str(payload.get("receiptDigest") or "")
        unsigned = dict(payload)
        unsigned.pop("receiptDigest", None)
        if not digest or digest != self._canonical_digest(unsigned):
            return "invalid", payload
        if str(payload.get("transactionId") or "") != transaction_id:
            return "invalid", payload
        return "verified", payload

    def _ensure_terminal_receipt(
        self, transaction: GitWriteTransaction, journal: dict[str, Any], *, outcome: str
    ) -> bool:
        integrity, _ = self._receipt_integrity(transaction.id)
        if integrity == "verified":
            return False
        if integrity == "invalid":
            raise ForgeTraceError(
                "Terminal Git write receipt exists but failed integrity verification; the journal was retained.",
                HTTPStatus.CONFLICT,
                "git_write_receipt_integrity_failed",
                {"transactionId": transaction.id},
            )
        if outcome == "committed":
            details = {
                "beforeStateDigest": str(journal.get("beforeStateDigest") or ""),
                "afterStateDigest": str(journal.get("afterStateDigest") or ""),
                "result": dict(journal.get("result") or {}),
                "receiptRecoveredAtStartup": True,
            }
            receipt_outcome = "committed"
        else:
            details = {
                "reason": str(journal.get("rollbackReason") or "")[:1024],
                "recoveryOriginCheckpoint": str(journal.get("recoveryOriginCheckpoint") or ""),
                "receiptRecoveredAtStartup": True,
            }
            receipt_outcome = "recovered_rollback" if journal.get("recovered") else "rolled_back"
        self._write_receipt(transaction, outcome=receipt_outcome, details=details)
        return True

    def _maintenance_snapshot(self) -> list[dict[str, Any]]:
        with self._maintenance_guard:
            return [dict(item) for item in self._maintenance_warnings]

    def _transaction_diagnostic(
        self,
        root: Path,
        *,
        expected_repository_id: str | None = None,
        expected_root: Path | None = None,
        expected_git_dir: Path | None = None,
    ) -> dict[str, Any] | None:
        journal_path = root / "journal.json"
        diagnostic: dict[str, Any] = {
            "transactionId": root.name,
            "journalPath": str(journal_path),
            "integrity": "unreadable",
            "repositoryId": "",
            "repositoryPath": "",
            "gitDir": "",
            "operation": "",
            "status": "unknown",
            "lastCheckpoint": "",
            "lastCheckpointAt": "",
            "captureCount": 0,
            "createdObjectCount": 0,
            "receiptIntegrity": "missing",
            "terminal": False,
            "pathMatchesRegistration": None,
            "blockingNativeLocks": [],
            "activeAdministrativePaths": [],
            "recoveryDisposition": "manual_inspection_required",
            "recoverable": False,
            "requiresManualInspection": True,
            "nextStep": "Preserve this transaction directory and inspect the journal before any manual Git repair.",
        }
        try:
            diagnostic["ageSeconds"] = max(0, int(time.time() - journal_path.stat().st_mtime))
        except OSError:
            diagnostic["ageSeconds"] = None
        try:
            payload = json.loads(journal_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("journal is not an object")
        except Exception as exc:
            diagnostic.update(
                errorType=type(exc).__name__,
                errorCode="git_write_journal_unreadable",
                message=f"{type(exc).__name__}: {exc}"[:2048],
            )
            return diagnostic if expected_repository_id is None else None

        repository_id = str(payload.get("repositoryId") or "")
        diagnostic.update(
            repositoryId=repository_id,
            repositoryPath=str(payload.get("repositoryPath") or ""),
            gitDir=str(payload.get("gitDir") or ""),
            operation=str(payload.get("operation") or ""),
            status=str(payload.get("status") or "prepared"),
            updatedAt=str(payload.get("updatedAt") or ""),
            lastCheckpoint=str(payload.get("lastCheckpoint") or ""),
            lastCheckpointAt=str(payload.get("lastCheckpointAt") or ""),
            captureCount=len(payload.get("captures") or []) if isinstance(payload.get("captures"), list) else 0,
            createdObjectCount=len(payload.get("createdObjects") or []) if isinstance(payload.get("createdObjects"), list) else 0,
        )
        if expected_repository_id is not None and repository_id != expected_repository_id:
            return None
        try:
            transaction = GitWriteTransaction.from_journal(self, root, payload)
            diagnostic["integrity"] = "verified"
        except ForgeTraceError as exc:
            diagnostic.update(
                integrity="invalid",
                errorType=type(exc).__name__,
                errorCode=exc.code,
                message=str(exc)[:2048],
                nextStep="Do not delete or edit this journal. Preserve it with the security ledger for manual integrity review.",
            )
            return diagnostic
        except Exception as exc:
            diagnostic.update(
                integrity="invalid",
                errorType=type(exc).__name__,
                errorCode="git_write_journal_integrity_failed",
                message=f"{type(exc).__name__}: {exc}"[:2048],
            )
            return diagnostic

        receipt_integrity, _ = self._receipt_integrity(transaction.id)
        diagnostic["receiptIntegrity"] = receipt_integrity
        status = str(payload.get("status") or "prepared")
        terminal = status in {"committed", "rolled_back"}
        diagnostic["terminal"] = terminal
        path_matches: bool | None = None
        if expected_root is not None and expected_git_dir is not None:
            path_matches = (
                transaction.repository_path == expected_root.resolve()
                and transaction.git_dir == expected_git_dir.resolve()
            )
        diagnostic["pathMatchesRegistration"] = path_matches

        git_dir = transaction.git_dir
        if git_dir.is_dir():
            diagnostic["blockingNativeLocks"] = self._native_locks(git_dir)
            diagnostic["activeAdministrativePaths"] = self._active_administrative_paths(git_dir)
        blockers = bool(
            diagnostic["blockingNativeLocks"] or diagnostic["activeAdministrativePaths"]
        )

        if path_matches is False:
            diagnostic.update(
                recoveryDisposition="manual_inspection_required",
                nextStep="The registered repository path no longer matches this sealed journal. Preserve both and resolve the path mismatch before recovery.",
            )
        elif status == "rollback_failed":
            diagnostic.update(
                recoveryDisposition="manual_inspection_required",
                nextStep="Exact rollback previously failed. Preserve all captures and inspect the recorded rollback error before further writes.",
            )
        elif terminal and receipt_integrity == "invalid":
            diagnostic.update(
                recoveryDisposition="manual_inspection_required",
                nextStep="The terminal receipt failed verification. Preserve both receipt and journal; ForgeTrace will not replace conflicting evidence.",
            )
        elif terminal and receipt_integrity == "missing":
            diagnostic.update(
                recoveryDisposition="reconstruct_receipt_then_cleanup",
                recoverable=True,
                requiresManualInspection=False,
                nextStep="Restart ForgeTrace. It can reconstruct the missing terminal receipt from the sealed journal, verify it, and then retry cleanup.",
            )
        elif terminal:
            diagnostic.update(
                recoveryDisposition="cleanup_terminal_journal",
                recoverable=True,
                requiresManualInspection=False,
                nextStep="Restart ForgeTrace to retry non-destructive cleanup of this already-receipted terminal journal.",
            )
        elif blockers:
            diagnostic.update(
                recoveryDisposition="deferred_external_git_state",
                nextStep="Finish the external Git operation or remove only a confirmed-stale native lock, then restart ForgeTrace for exact rollback.",
            )
        else:
            diagnostic.update(
                recoveryDisposition="rollback_on_restart",
                recoverable=True,
                requiresManualInspection=False,
                nextStep="Restart ForgeTrace to restore the exact captured pre-write Git state and issue a recovered-rollback receipt.",
            )
        return diagnostic

    def _cleanup_previews(self) -> None:
        now = int(time.time())
        for path in self.previews_dir.glob("git_preview_*.json"):
            payload: dict[str, Any] = {}
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                expired = int(payload.get("expiresAtEpoch") or 0) < now
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                expired = True
            if expired:
                repository_id = str(payload.get("repositoryId") or "") if isinstance(payload, dict) else ""
                self._best_effort_unlink(
                    path,
                    action="cleanup_expired_git_write_preview",
                    repository_id=repository_id,
                )

    def _cleanup_receipts(self) -> None:
        now = time.time()
        dated_paths: list[tuple[float, Path]] = []
        for item in self.receipts_dir.glob("git_txn_*.json"):
            try:
                dated_paths.append((item.stat().st_mtime, item))
            except OSError:
                continue
        paths = [item for _, item in sorted(dated_paths, key=lambda pair: pair[0], reverse=True)]
        for index, path in enumerate(paths):
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if index >= MAX_RECEIPTS or age > RECEIPT_RETENTION_SECONDS:
                repository_id = ""
                try:
                    receipt_payload = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(receipt_payload, dict):
                        repository_id = str(receipt_payload.get("repositoryId") or "")
                except (OSError, json.JSONDecodeError):
                    pass
                self._best_effort_unlink(
                    path,
                    action="cleanup_retired_git_write_receipt",
                    repository_id=repository_id,
                )

    def recover_pending_transactions(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "checked": 0,
            "rolledBack": 0,
            "cleanedCommitted": 0,
            "recoveredReceipts": 0,
            "retained": 0,
            "deferred": 0,
            "manualInspection": 0,
            "actions": [],
        }
        for root in sorted(self.transactions_dir.glob("git_txn_*")):
            report["checked"] += 1
            journal = root / "journal.json"
            diagnostic = self._transaction_diagnostic(root)
            try:
                payload = json.loads(journal.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("journal is not an object")
                transaction = GitWriteTransaction.from_journal(self, root, payload)
            except Exception as exc:
                report["retained"] += 1
                report["manualInspection"] += 1
                report["actions"].append(
                    {
                        "transactionId": root.name,
                        "action": "retained_unreadable",
                        "message": str(exc)[:2048],
                        "diagnostic": diagnostic,
                    }
                )
                continue
            status = str(payload.get("status") or "prepared")
            if status in {"committed", "rolled_back"}:
                try:
                    recovered_receipt = self._ensure_terminal_receipt(
                        transaction, payload, outcome=status
                    )
                    if recovered_receipt:
                        report["recoveredReceipts"] += 1
                    cleaned = self._best_effort_remove_tree(
                        root,
                        action="cleanup_recovered_terminal_git_write_transaction",
                        repository_id=transaction.repository_id,
                    )
                    if cleaned and status == "committed":
                        report["cleanedCommitted"] += 1
                    if cleaned:
                        report["actions"].append(
                            {
                                "transactionId": root.name,
                                "action": f"cleaned_{status}",
                                "receiptRecovered": recovered_receipt,
                                "transactionDirectoryCleaned": True,
                                "diagnostic": diagnostic,
                            }
                        )
                    else:
                        report["retained"] += 1
                        report["deferred"] += 1
                        report["actions"].append(
                            {
                                "transactionId": root.name,
                                "action": "retained_terminal_cleanup_blocked",
                                "receiptRecovered": recovered_receipt,
                                "transactionDirectoryCleaned": False,
                                "diagnostic": self._transaction_diagnostic(root),
                            }
                        )
                except Exception as exc:
                    report["retained"] += 1
                    report["manualInspection"] += 1
                    report["actions"].append(
                        {
                            "transactionId": root.name,
                            "action": "retained_terminal_evidence_failure",
                            "message": f"{type(exc).__name__}: {exc}"[:2048],
                            "errorCode": getattr(exc, "code", ""),
                            "diagnostic": self._transaction_diagnostic(root),
                        }
                    )
                continue
            try:
                repository, repository_root, git_dir = self._context(transaction.repository_id)
                if repository_root != transaction.repository_path or git_dir != transaction.git_dir:
                    raise ForgeTraceError(
                        "Registered repository path no longer matches the Git transaction journal.",
                        HTTPStatus.CONFLICT,
                        "git_write_recovery_path_mismatch",
                    )
                with repository.lock, self._lock(transaction.repository_id):
                    # Never overwrite Git metadata while an external Git operation
                    # advertises an administrative state or native lock. Retain the
                    # journal and retry on a later startup instead.
                    self._assert_operation_state(git_dir)
                    # Recovery restores the pre-transaction state and must remain
                    # possible after an owner changes the repository to read-only.
                    # New writes are still blocked by require_writable at preview
                    # and execution time.
                    transaction.rollback(reason="startup recovery of incomplete Git write", recovered=True)
                report["rolledBack"] += 1
                report["actions"].append(
                    {
                        "transactionId": root.name,
                        "action": "rolled_back_pending",
                        "repositoryId": transaction.repository_id,
                        "operation": transaction.operation,
                        "lastCheckpoint": transaction.last_checkpoint,
                    }
                )
                self._audit(
                    action="startup_git_write_recovery",
                    outcome="success",
                    repository_id=transaction.repository_id,
                    subject_id=transaction.id,
                    actor="ForgeTrace Recovery",
                    request_id="",
                    surface="system",
                    details={"operation": transaction.operation, "recoveryAction": "rolled_back_pending"},
                    required=False,
                )
            except Exception as exc:
                report["retained"] += 1
                code = getattr(exc, "code", "")
                if code in {"git_native_lock_present", "git_operation_in_progress"}:
                    report["deferred"] += 1
                else:
                    report["manualInspection"] += 1
                report["actions"].append(
                    {
                        "transactionId": root.name,
                        "action": "retained_recovery_failed",
                        "repositoryId": transaction.repository_id,
                        "message": f"{type(exc).__name__}: {exc}"[:2048],
                        "errorCode": code,
                        "errorDetails": dict(getattr(exc, "details", {}) or {}),
                        "diagnostic": self._transaction_diagnostic(root),
                    }
                )
        return report

    def list_receipts(self, repository_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 200))
        records: list[dict[str, Any]] = []
        dated_paths: list[tuple[float, Path]] = []
        for item in self.receipts_dir.glob("git_txn_*.json"):
            try:
                dated_paths.append((item.stat().st_mtime, item))
            except OSError:
                continue
        for _, path in sorted(dated_paths, key=lambda pair: pair[0], reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("repositoryId") != repository_id:
                continue
            digest = str(payload.get("receiptDigest") or "")
            unsigned = dict(payload)
            unsigned.pop("receiptDigest", None)
            payload["verified"] = bool(digest and digest == self._canonical_digest(unsigned))
            records.append(payload)
            if len(records) >= bounded:
                break
        return records

    def status(self, repository_id: str, *, receipt_limit: int = 25) -> dict[str, Any]:
        repository, root, git_dir = self._context(repository_id)
        with repository.lock, self._lock(repository_id):
            policy = repository.access_policy()
            pending: list[dict[str, Any]] = []
            unassigned: list[dict[str, Any]] = []
            for transaction_root in self.transactions_dir.glob("git_txn_*"):
                diagnostic = self._transaction_diagnostic(
                    transaction_root,
                    expected_repository_id=repository_id,
                    expected_root=root,
                    expected_git_dir=git_dir,
                )
                if diagnostic is not None:
                    pending.append(diagnostic)
                    continue
                generic = self._transaction_diagnostic(transaction_root)
                if generic and not generic.get("repositoryId"):
                    unassigned.append(generic)
            pending.sort(key=lambda item: (str(item.get("updatedAt") or ""), str(item.get("transactionId") or "")), reverse=True)
            maintenance = [
                item
                for item in self._maintenance_snapshot()
                if not item.get("repositoryId") or item.get("repositoryId") == repository_id
            ]
            recovery_summary = {
                "pendingCount": len(pending),
                "recoverableCount": sum(1 for item in pending if item.get("recoverable")),
                "deferredCount": sum(
                    1 for item in pending if item.get("recoveryDisposition") == "deferred_external_git_state"
                ),
                "manualInspectionCount": sum(1 for item in pending if item.get("requiresManualInspection")),
                "terminalCleanupCount": sum(1 for item in pending if item.get("terminal")),
                "unassignedJournalCount": len(unassigned),
                "maintenanceWarningCount": len(maintenance),
            }
            return {
                "schemaVersion": GIT_WRITE_SCHEMA_VERSION,
                "repositoryId": repository_id,
                "repositoryPath": str(root),
                "supported": True,
                "writable": bool(policy.get("writable")),
                "accessPolicy": policy,
                "operations": sorted(OPERATIONS),
                "previewTtlSeconds": PREVIEW_TTL_SECONDS,
                "pendingTransactions": pending,
                "recoverySummary": recovery_summary,
                "unassignedTransactions": unassigned,
                "maintenanceWarnings": maintenance[-25:],
                "receipts": self.list_receipts(repository_id, limit=receipt_limit),
                "nativeLocks": self._native_locks(git_dir),
                "startupRecovery": self.startup_recovery_report,
                "restrictions": {
                    "ownerOnly": True,
                    "network": False,
                    "credentials": False,
                    "hooks": False,
                    "helpers": False,
                    "checkout": False,
                    "switch": False,
                    "merge": False,
                    "fetch": False,
                    "pull": False,
                    "push": False,
                    "clone": False,
                    "signedCommits": False,
                    "annotatedOrSignedTags": False,
                    "externalCleanFilters": False,
                    "linkedWorktrees": False,
                },
            }
