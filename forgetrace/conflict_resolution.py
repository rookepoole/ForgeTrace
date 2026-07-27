from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable

from .errors import ForgeTraceError
from .utils import utc_now

MAX_INLINE_RESOLUTION_BYTES = 512 * 1024
MAX_RESOLUTION_DRAFTS_PER_PULL_REQUEST = 1000
MAX_RESOLUTION_TEXT_LINES = 20000
MAX_RESOLUTION_EVIDENCE_BYTES_PER_PULL_REQUEST = 4 * 1024 * 1024 * 1024
MIN_RESOLUTION_FREE_SPACE_RESERVE_BYTES = 16 * 1024 * 1024
CONFLICT_RESOLUTION_RETENTION_DAYS = 180
REVIEWABLE_STATUSES = {"open", "approved", "changes_requested", "conflict"}
ACTIVE_DRAFT_STATUSES = {"draft", "confirmed"}


class ConflictResolutionStore:
    """Owner-only, quarantine-side conflict resolution evidence.

    A draft preserves verified base/current/incoming bytes outside the repository.
    The live workspace is not mutated until CollaborationService invokes the existing
    transactional repository merge after every binding is revalidated under the
    repository lock.
    """

    def __init__(
        self,
        *,
        registry,
        db_path: Path,
        data_dir: Path,
        lock,
        review_store,
        owner_pr_resolver: Callable[[sqlite3.Connection, str, str], sqlite3.Row],
        file_rows: Callable[[sqlite3.Connection, str], list[dict[str, Any]]],
        deletion_rows: Callable[[sqlite3.Connection, str], list[dict[str, Any]]],
        audit: Callable[..., dict[str, Any] | None],
    ) -> None:
        self.registry = registry
        self.db_path = db_path
        self.data_dir = data_dir
        self.lock = lock
        self.review_store = review_store
        self.owner_pr_resolver = owner_pr_resolver
        self.file_rows = file_rows
        self.deletion_rows = deletion_rows
        self.audit = audit
        self.resolutions_dir = data_dir / "conflict-resolutions"
        self.resolutions_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_orphan_directories()

    @staticmethod
    def migrate_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conflict_resolution_drafts (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                repository_id TEXT NOT NULL,
                pull_request_id TEXT NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
                submitted_revision INTEGER NOT NULL,
                path TEXT NOT NULL,
                conflict_reason TEXT NOT NULL,
                submitted_kind TEXT NOT NULL CHECK(submitted_kind IN ('file','deletion')),
                repository_digest TEXT NOT NULL,
                access_mode TEXT NOT NULL,
                conflict_set_digest TEXT NOT NULL,
                review_gate_digest TEXT NOT NULL,
                unresolved_thread_count INTEGER NOT NULL,
                base_hash TEXT NOT NULL DEFAULT '',
                current_hash TEXT NOT NULL DEFAULT '',
                incoming_hash TEXT NOT NULL DEFAULT '',
                evidence_manifest_sha256 TEXT NOT NULL,
                inline_eligible INTEGER NOT NULL DEFAULT 0 CHECK(inline_eligible IN (0,1)),
                decision TEXT NOT NULL DEFAULT '',
                result_kind TEXT NOT NULL DEFAULT '' CHECK(result_kind IN ('','file','deletion')),
                resolved_hash TEXT NOT NULL DEFAULT '',
                resolved_size INTEGER NOT NULL DEFAULT 0,
                author_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','confirmed','stale','applied')),
                version INTEGER NOT NULL DEFAULT 1,
                created_request_id TEXT NOT NULL DEFAULT '',
                updated_request_id TEXT NOT NULL DEFAULT '',
                confirmed_request_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confirmed_at TEXT NOT NULL DEFAULT '',
                applied_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_conflict_resolution_pr_path
                ON conflict_resolution_drafts(pull_request_id, submitted_revision, path, sequence DESC);
            CREATE INDEX IF NOT EXISTS idx_conflict_resolution_pr_status
                ON conflict_resolution_drafts(pull_request_id, status, sequence DESC);

            CREATE TABLE IF NOT EXISTS conflict_resolution_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                draft_id TEXT NOT NULL REFERENCES conflict_resolution_drafts(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL CHECK(event_type IN ('created','decision_saved','confirmed','stale','applied')),
                actor_name TEXT NOT NULL,
                draft_version INTEGER NOT NULL,
                decision TEXT NOT NULL DEFAULT '',
                result_kind TEXT NOT NULL DEFAULT '',
                resolved_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                request_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_conflict_resolution_events_draft
                ON conflict_resolution_events(draft_id, sequence);
            """
        )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _canonical_json(payload: Any) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def manifest_digest(cls, manifest: dict[str, dict[str, Any]]) -> str:
        byte_manifest = {
            str(path): {
                "hash": str(entry.get("hash") or ""),
                "size": int(entry.get("size") or 0),
            }
            for path, entry in sorted(manifest.items())
        }
        return hashlib.sha256(cls._canonical_json(byte_manifest).encode("utf-8")).hexdigest()

    @staticmethod
    def _whole_number(value: Any, *, label: str, minimum: int = 0) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ForgeTraceError(f"{label} must be a whole number.", code="invalid_whole_number") from exc
        if result < minimum:
            raise ForgeTraceError(f"{label} must be at least {minimum}.", code="whole_number_out_of_range")
        return result

    @staticmethod
    def _clean_text(value: Any, *, label: str, maximum: int, required: bool = False) -> str:
        result = str(value or "").strip()
        if required and not result:
            raise ForgeTraceError(f"{label} is required.", code=f"{label.lower().replace(' ', '_')}_required")
        if len(result) > maximum:
            raise ForgeTraceError(f"{label} may not exceed {maximum} characters.", code="value_too_long")
        return result

    def _draft_root(self, repository_id: str, pull_request_id: str, draft_id: str) -> Path:
        base = self.resolutions_dir.resolve()
        target = (base / repository_id / pull_request_id / draft_id).resolve()
        if target != base and base not in target.parents:
            raise ForgeTraceError(
                "Conflict-resolution path escapes application data.",
                HTTPStatus.FORBIDDEN,
                "conflict_resolution_path_escape",
            )
        return target

    @staticmethod
    def _hash_file(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    @staticmethod
    def _regular_file_bytes(root: Path) -> int:
        total = 0
        if not root.exists():
            return 0
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_names[:] = [
                name for name in directory_names if not (Path(directory) / name).is_symlink()
            ]
            for name in file_names:
                candidate = Path(directory) / name
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                total += candidate.stat().st_size
        return total

    def _preflight_evidence_storage(
        self,
        repository_id: str,
        pull_request_id: str,
        *,
        required_bytes: int,
    ) -> None:
        required = max(0, int(required_bytes))
        pull_request_root = self._draft_root(repository_id, pull_request_id, "quota-probe").parent
        existing = self._regular_file_bytes(pull_request_root)
        projected = existing + required
        if projected > MAX_RESOLUTION_EVIDENCE_BYTES_PER_PULL_REQUEST:
            raise ForgeTraceError(
                "Conflict-resolution evidence storage limit reached for this pull request.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "conflict_resolution_evidence_limit_reached",
                {
                    "existingBytes": existing,
                    "requiredBytes": required,
                    "projectedBytes": projected,
                    "limitBytes": MAX_RESOLUTION_EVIDENCE_BYTES_PER_PULL_REQUEST,
                },
            )
        free = shutil.disk_usage(self.resolutions_dir).free
        minimum_free = required + MIN_RESOLUTION_FREE_SPACE_RESERVE_BYTES
        if free < minimum_free:
            raise ForgeTraceError(
                "Not enough free space to preserve conflict-resolution evidence safely.",
                HTTPStatus.INSUFFICIENT_STORAGE,
                "insufficient_conflict_resolution_space",
                {
                    "freeBytes": free,
                    "requiredBytes": required,
                    "reserveBytes": MIN_RESOLUTION_FREE_SPACE_RESERVE_BYTES,
                },
            )

    @classmethod
    def _copy_verified(
        cls,
        source: Path,
        destination: Path,
        *,
        expected_hash: str,
        expected_size: int | None = None,
    ) -> int:
        if not source.is_file() or source.is_symlink():
            raise ForgeTraceError(
                "Conflict-resolution evidence source is unavailable.",
                HTTPStatus.CONFLICT,
                "conflict_resolution_evidence_unavailable",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as input_handle, temporary.open("wb") as output_handle:
                for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
                    output_handle.write(chunk)
                output_handle.flush()
                os.fsync(output_handle.fileno())
            if expected_size is not None and size != int(expected_size):
                raise ForgeTraceError(
                    "Conflict-resolution evidence size changed while it was captured.",
                    HTTPStatus.CONFLICT,
                    "conflict_resolution_evidence_changed",
                    {"expectedBytes": int(expected_size), "actualBytes": size},
                )
            if digest.hexdigest() != str(expected_hash or ""):
                raise ForgeTraceError(
                    "Conflict-resolution evidence hash changed while it was captured.",
                    HTTPStatus.CONFLICT,
                    "conflict_resolution_evidence_changed",
                )
            os.replace(temporary, destination)
            return size
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_bytes(destination: Path, content: bytes) -> tuple[int, str]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256(content).hexdigest()
        try:
            with temporary.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            return len(content), digest
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _plain_text(path: Path, size: int) -> tuple[bool, int]:
        if size > MAX_INLINE_RESOLUTION_BYTES:
            return False, 0
        try:
            data = path.read_bytes()
            if len(data) != size or b"\x00" in data:
                return False, 0
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return False, 0
        lines = len(text.splitlines()) if text else 0
        if lines > MAX_RESOLUTION_TEXT_LINES:
            return False, lines
        return True, lines

    @staticmethod
    def _current_review_gate(connection: sqlite3.Connection, pull_request_id: str, revision: int) -> tuple[int, str]:
        rows = connection.execute(
            """
            SELECT id, version FROM review_threads
            WHERE pull_request_id=? AND submitted_revision=? AND resolved=0
            ORDER BY id
            """,
            (pull_request_id, revision),
        ).fetchall()
        payload = [{"id": str(row["id"]), "version": int(row["version"])} for row in rows]
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return len(payload), digest

    @staticmethod
    def _conflicts_for_manifest(
        current_manifest: dict[str, dict[str, Any]],
        files: list[dict[str, Any]],
        deletions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        deletion_by_hash = {
            str(item.get("base_hash") or item.get("baseHash") or ""): str(item["path"])
            for item in deletions
            if str(item.get("base_hash") or item.get("baseHash") or "")
        }
        incoming_by_hash = {
            str(item.get("sha256") or ""): str(item["path"])
            for item in files
            if not str(item.get("base_hash") or item.get("baseHash") or "") and str(item.get("sha256") or "")
        }
        for item in files:
            path = str(item["path"])
            base_hash = str(item.get("base_hash") or item.get("baseHash") or "")
            current_hash = str(current_manifest.get(path, {}).get("hash") or "")
            reason = ""
            if base_hash and current_hash != base_hash:
                reason = "changed_since_pull_request_started"
            elif not base_hash and current_hash:
                reason = "new_path_now_exists"
            if reason:
                incoming_hash = str(item.get("sha256") or "")
                conflicts.append({
                    "path": path,
                    "reason": reason,
                    "submittedKind": "file",
                    "baseHash": base_hash,
                    "currentHash": current_hash,
                    "incomingHash": incoming_hash,
                    "currentSize": int(current_manifest.get(path, {}).get("size") or 0),
                    "incomingSize": int(item.get("size") or 0),
                    "risky": bool(item.get("risky")),
                    "renameFrom": deletion_by_hash.get(incoming_hash, ""),
                    "renameTo": "",
                })
        for item in deletions:
            path = str(item["path"])
            base_hash = str(item.get("base_hash") or item.get("baseHash") or "")
            current_hash = str(current_manifest.get(path, {}).get("hash") or "")
            if base_hash and current_hash != base_hash:
                conflicts.append({
                    "path": path,
                    "reason": "changed_since_pull_request_started",
                    "submittedKind": "deletion",
                    "baseHash": base_hash,
                    "currentHash": current_hash,
                    "incomingHash": "",
                    "currentSize": int(current_manifest.get(path, {}).get("size") or 0),
                    "incomingSize": 0,
                    "risky": False,
                    "renameFrom": "",
                    "renameTo": incoming_by_hash.get(base_hash, ""),
                })
        return sorted(conflicts, key=lambda item: str(item["path"]).casefold())

    @classmethod
    def _conflict_set_digest(cls, conflicts: list[dict[str, Any]]) -> str:
        material = [
            {
                "path": item["path"],
                "reason": item["reason"],
                "submittedKind": item["submittedKind"],
                "baseHash": item["baseHash"],
                "currentHash": item["currentHash"],
                "incomingHash": item["incomingHash"],
                "renameFrom": item.get("renameFrom", ""),
                "renameTo": item.get("renameTo", ""),
            }
            for item in conflicts
        ]
        return hashlib.sha256(cls._canonical_json(material).encode("utf-8")).hexdigest()

    def _binding_locked(
        self,
        connection: sqlite3.Connection,
        pr: sqlite3.Row,
        repository,
        files: list[dict[str, Any]],
        deletions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        current_manifest = repository.manifest(store_objects=False, persist_index=False)
        conflicts = self._conflicts_for_manifest(current_manifest, files, deletions)
        unresolved_count, review_gate_digest = self._current_review_gate(
            connection, str(pr["id"]), int(pr["revision"])
        )
        policy = repository.access_policy()
        return {
            "currentManifest": current_manifest,
            "repositoryDigest": self.manifest_digest(current_manifest),
            "conflicts": conflicts,
            "conflictSetDigest": self._conflict_set_digest(conflicts),
            "accessMode": str(policy.get("effectiveMode") or "read_only"),
            "accessPolicy": policy,
            "unresolvedThreadCount": unresolved_count,
            "reviewGateDigest": review_gate_digest,
        }

    @staticmethod
    def _conflict_for_path(binding: dict[str, Any], path: str) -> dict[str, Any] | None:
        return next((item for item in binding["conflicts"] if item["path"] == path), None)

    @staticmethod
    def _stale_reasons(draft: sqlite3.Row, pr: sqlite3.Row, binding: dict[str, Any]) -> list[str]:
        # Applied drafts are immutable historical evidence. The successful merge necessarily
        # changes the live repository digest and terminal PR status, so re-binding them to the
        # post-merge workspace would incorrectly label valid evidence as stale.
        if str(draft["status"]) == "applied":
            return []
        reasons: list[str] = []
        if int(draft["submitted_revision"]) != int(pr["revision"]):
            reasons.append("pull_request_revision_changed")
        if str(draft["repository_digest"]) != str(binding["repositoryDigest"]):
            reasons.append("repository_digest_changed")
        if str(draft["access_mode"]) != str(binding["accessMode"]):
            reasons.append("access_mode_changed")
        if str(draft["conflict_set_digest"]) != str(binding["conflictSetDigest"]):
            reasons.append("conflict_set_changed")
        if str(draft["review_gate_digest"]) != str(binding["reviewGateDigest"]):
            reasons.append("review_threads_changed")
        conflict = ConflictResolutionStore._conflict_for_path(binding, str(draft["path"]))
        if conflict is None:
            reasons.append("path_no_longer_conflicts")
        else:
            pairs = (
                ("conflict_reason", "reason"),
                ("submitted_kind", "submittedKind"),
                ("base_hash", "baseHash"),
                ("current_hash", "currentHash"),
                ("incoming_hash", "incomingHash"),
            )
            if any(str(draft[left]) != str(conflict[right]) for left, right in pairs):
                reasons.append("path_conflict_changed")
        if str(pr["status"]) not in REVIEWABLE_STATUSES:
            reasons.append("pull_request_not_reviewable")
        return list(dict.fromkeys(reasons))

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        draft: sqlite3.Row | dict[str, Any],
        *,
        event_type: str,
        actor_name: str,
        request_id: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO conflict_resolution_events(
                id, draft_id, event_type, actor_name, draft_version, decision,
                result_kind, resolved_hash, created_at, request_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cre_" + uuid.uuid4().hex[:16], str(draft["id"]), event_type,
                str(actor_name or ""), int(draft["version"]), str(draft["decision"] or ""),
                str(draft["result_kind"] or ""), str(draft["resolved_hash"] or ""),
                utc_now(), str(request_id or "")[:120],
            ),
        )

    def _mark_stale(
        self,
        connection: sqlite3.Connection,
        draft: sqlite3.Row,
        *,
        actor_name: str = "ForgeTrace",
        request_id: str = "",
    ) -> sqlite3.Row:
        if str(draft["status"]) not in ACTIVE_DRAFT_STATUSES:
            return draft
        now = utc_now()
        connection.execute(
            "UPDATE conflict_resolution_drafts SET status='stale', version=version+1, updated_at=?, updated_request_id=? WHERE id=?",
            (now, str(request_id or "")[:120], draft["id"]),
        )
        updated = connection.execute(
            "SELECT * FROM conflict_resolution_drafts WHERE id=?", (draft["id"],)
        ).fetchone()
        self._event(connection, updated, event_type="stale", actor_name=actor_name, request_id=request_id)
        return updated

    def _latest_drafts(self, connection: sqlite3.Connection, pull_request_id: str, revision: int) -> dict[str, sqlite3.Row]:
        rows = connection.execute(
            """
            SELECT d.* FROM conflict_resolution_drafts d
            JOIN (
                SELECT path, MAX(sequence) AS max_sequence
                FROM conflict_resolution_drafts
                WHERE pull_request_id=? AND submitted_revision=?
                GROUP BY path
            ) latest ON latest.max_sequence=d.sequence
            ORDER BY d.path COLLATE NOCASE
            """,
            (pull_request_id, revision),
        ).fetchall()
        return {str(row["path"]): row for row in rows}

    def _revision_entry(
        self, connection: sqlite3.Connection, pull_request_id: str, revision: int, path: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        _revision_row, manifest = self.review_store._load_revision(connection, pull_request_id, revision)
        entry = (manifest.get("files") or {}).get(path)
        if entry is None:
            entry = (manifest.get("deletions") or {}).get(path)
        if entry is None:
            raise ForgeTraceError(
                "Conflict path is missing from immutable submitted-revision evidence.",
                HTTPStatus.CONFLICT,
                "conflict_resolution_revision_mismatch",
                {"path": path, "revision": revision},
            )
        return manifest, dict(entry)

    def _capture_role(
        self,
        *,
        role: str,
        source: Path | None,
        expected_hash: str,
        expected_size: int | None,
        root: Path,
        absent: bool = False,
    ) -> dict[str, Any]:
        if absent:
            return {
                "kind": "absent", "sha256": "", "size": 0,
                "snapshotAvailable": True, "text": True, "lineCount": 0,
            }
        if source is None:
            return {
                "kind": "unavailable", "sha256": str(expected_hash or ""), "size": int(expected_size or 0),
                "snapshotAvailable": False, "text": False, "lineCount": 0,
            }
        destination = root / f"{role}.bin"
        size = self._copy_verified(
            source, destination, expected_hash=expected_hash, expected_size=expected_size
        )
        text, line_count = self._plain_text(destination, size)
        return {
            "kind": "file", "sha256": expected_hash, "size": size,
            "snapshotAvailable": True, "text": text, "lineCount": line_count,
        }

    def _base_source(
        self,
        *,
        repository,
        pr: sqlite3.Row,
        revision_manifest: dict[str, Any],
        revision_entry: dict[str, Any],
        path: str,
        base_hash: str,
        current_manifest: dict[str, dict[str, Any]],
    ) -> tuple[Path | None, int | None]:
        if not base_hash:
            return None, 0
        if revision_entry.get("baseSnapshotAvailable"):
            candidate = self.review_store._revision_base_file(
                str(pr["repository_id"]), str(pr["id"]), int(pr["revision"]), path
            )
            return candidate, int(revision_entry.get("baseSize") or 0)
        try:
            object_path = repository.object_path(base_hash)
        except Exception:
            object_path = None
        if object_path is not None and object_path.is_file() and not object_path.is_symlink():
            base_manifest = json.loads(str(pr["base_manifest_json"]))
            return object_path, int((base_manifest.get(path) or {}).get("size") or object_path.stat().st_size)
        if str(current_manifest.get(path, {}).get("hash") or "") == base_hash:
            _rel, live = repository.resolve_path(path)
            return live, int(current_manifest[path].get("size") or 0)
        return None, None

    def _create_draft_locked(
        self,
        connection: sqlite3.Connection,
        *,
        pr: sqlite3.Row,
        repository,
        binding: dict[str, Any],
        conflict: dict[str, Any],
        actor_name: str,
        request_id: str,
    ) -> sqlite3.Row:
        count = int(connection.execute(
            "SELECT COUNT(*) AS count FROM conflict_resolution_drafts WHERE pull_request_id=?",
            (pr["id"],),
        ).fetchone()["count"])
        if count >= MAX_RESOLUTION_DRAFTS_PER_PULL_REQUEST:
            raise ForgeTraceError(
                "Conflict-resolution draft limit reached for this pull request.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "conflict_resolution_draft_limit_reached",
            )
        draft_id = "crd_" + uuid.uuid4().hex[:20]
        final_root = self._draft_root(str(pr["repository_id"]), str(pr["id"]), draft_id)
        final_root.parent.mkdir(parents=True, exist_ok=True)
        tmp_root = final_root.with_name(f".{draft_id}.{uuid.uuid4().hex}.tmp")
        shutil.rmtree(tmp_root, ignore_errors=True)
        tmp_root.mkdir(parents=True, exist_ok=False)
        revision_manifest, revision_entry = self._revision_entry(
            connection, str(pr["id"]), int(pr["revision"]), str(conflict["path"])
        )
        try:
            path = str(conflict["path"])
            base_hash = str(conflict["baseHash"])
            base_source, base_size = self._base_source(
                repository=repository,
                pr=pr,
                revision_manifest=revision_manifest,
                revision_entry=revision_entry,
                path=path,
                base_hash=base_hash,
                current_manifest=binding["currentManifest"],
            )
            current_hash = str(conflict["currentHash"])
            current_source = None
            current_size: int | None = 0
            if current_hash:
                _rel, current_source = repository.resolve_path(path)
                current_size = int(binding["currentManifest"].get(path, {}).get("size") or 0)

            incoming_hash = str(conflict["incomingHash"])
            incoming_source = None
            incoming_size: int | None = 0
            incoming_absent = str(conflict["submittedKind"]) == "deletion"
            if not incoming_absent:
                if not revision_entry.get("snapshotAvailable"):
                    raise ForgeTraceError(
                        "Immutable submitted bytes are unavailable for this conflict.",
                        HTTPStatus.CONFLICT,
                        "conflict_incoming_evidence_unavailable",
                        {"path": path},
                    )
                incoming_source = self.review_store._revision_file(
                    str(pr["repository_id"]), str(pr["id"]), int(pr["revision"]), path
                )
                incoming_size = int(revision_entry.get("size") or 0)

            required_bytes = sum(
                int(size or 0)
                for size, present in (
                    (base_size, bool(base_hash)),
                    (current_size, bool(current_hash)),
                    (incoming_size, not incoming_absent),
                )
                if present
            ) + 64 * 1024
            self._preflight_evidence_storage(
                str(pr["repository_id"]),
                str(pr["id"]),
                required_bytes=required_bytes,
            )

            base = self._capture_role(
                role="base", source=base_source, expected_hash=base_hash,
                expected_size=base_size, root=tmp_root, absent=not base_hash,
            )
            if base_hash and not base["snapshotAvailable"]:
                raise ForgeTraceError(
                    "Immutable base bytes are unavailable, so ForgeTrace cannot safely construct a three-way resolution draft.",
                    HTTPStatus.CONFLICT,
                    "conflict_base_evidence_unavailable",
                    {"path": path, "baseHash": base_hash},
                )
            current = self._capture_role(
                role="current", source=current_source, expected_hash=current_hash,
                expected_size=current_size, root=tmp_root, absent=not current_hash,
            )
            incoming = self._capture_role(
                role="incoming", source=incoming_source, expected_hash=incoming_hash,
                expected_size=incoming_size, root=tmp_root, absent=incoming_absent,
            )
            inline_eligible = all(
                item["kind"] in {"file", "absent"} and bool(item["text"])
                for item in (base, current, incoming)
            )
            evidence = {
                "schemaVersion": 1,
                "repositoryId": str(pr["repository_id"]),
                "pullRequestId": str(pr["id"]),
                "submittedRevision": int(pr["revision"]),
                "draftId": draft_id,
                "path": path,
                "reason": str(conflict["reason"]),
                "submittedKind": str(conflict["submittedKind"]),
                "repositoryDigest": str(binding["repositoryDigest"]),
                "accessMode": str(binding["accessMode"]),
                "conflictSetDigest": str(binding["conflictSetDigest"]),
                "reviewGateDigest": str(binding["reviewGateDigest"]),
                "unresolvedThreadCount": int(binding["unresolvedThreadCount"]),
                "renameFrom": str(conflict.get("renameFrom") or ""),
                "renameTo": str(conflict.get("renameTo") or ""),
                "base": base,
                "current": current,
                "incoming": incoming,
                "inlineEligible": inline_eligible,
                "activeContentRendering": False,
                "createdAt": utc_now(),
            }
            manifest_json = self._canonical_json(evidence)
            manifest_sha256 = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
            manifest_path = tmp_root / "manifest.json"
            with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(manifest_json)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_root, final_root)
            now = utc_now()
            connection.execute(
                """
                INSERT INTO conflict_resolution_drafts(
                    id, repository_id, pull_request_id, submitted_revision, path, conflict_reason,
                    submitted_kind, repository_digest, access_mode, conflict_set_digest,
                    review_gate_digest, unresolved_thread_count, base_hash, current_hash,
                    incoming_hash, evidence_manifest_sha256, inline_eligible, author_name,
                    created_request_id, updated_request_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id, pr["repository_id"], pr["id"], int(pr["revision"]), path,
                    conflict["reason"], conflict["submittedKind"], binding["repositoryDigest"],
                    binding["accessMode"], binding["conflictSetDigest"], binding["reviewGateDigest"],
                    int(binding["unresolvedThreadCount"]), base_hash, current_hash, incoming_hash,
                    manifest_sha256, int(inline_eligible), actor_name, str(request_id or "")[:120],
                    str(request_id or "")[:120], now, now,
                ),
            )
            draft = connection.execute(
                "SELECT * FROM conflict_resolution_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            self._event(connection, draft, event_type="created", actor_name=actor_name, request_id=request_id)
            return draft
        except Exception:
            shutil.rmtree(tmp_root, ignore_errors=True)
            shutil.rmtree(final_root, ignore_errors=True)
            raise

    def prepare_owner(
        self,
        repository_id: str,
        pull_request_id: str,
        *,
        actor_name: str,
        expected_pull_request_revision: Any,
        request_id: str = "",
    ) -> dict[str, Any]:
        actor_name = self._clean_text(actor_name, label="Owner name", maximum=120, required=True)
        expected_revision = self._whole_number(
            expected_pull_request_revision, label="Expected pull request revision", minimum=1
        )
        created_ids: list[str] = []
        with self.lock:
            connection = self.connect()
            try:
                pr = self.owner_pr_resolver(connection, repository_id, pull_request_id)
                if str(pr["status"]) not in REVIEWABLE_STATUSES:
                    raise ForgeTraceError(
                        "This pull request is not available for conflict resolution.",
                        HTTPStatus.CONFLICT,
                        "pull_request_not_reviewable",
                    )
                if int(pr["revision"]) != expected_revision:
                    raise ForgeTraceError(
                        "Pull request changed. Refresh before preparing conflict resolutions.",
                        HTTPStatus.CONFLICT,
                        "pull_request_revision_changed",
                        {"currentRevision": int(pr["revision"])},
                    )
                self.review_store._load_revision(connection, pull_request_id, expected_revision)
                files = self.file_rows(connection, pull_request_id)
                deletions = self.deletion_rows(connection, pull_request_id)
                repository = self.registry.repository_service(repository_id)
                with repository.lock:
                    binding = self._binding_locked(connection, pr, repository, files, deletions)
                    if not binding["conflicts"]:
                        connection.commit()
                        return self._model_from_state(connection, pr, repository, binding, include_content=True)
                    latest = self._latest_drafts(connection, pull_request_id, expected_revision)
                    for conflict in binding["conflicts"]:
                        prior = latest.get(str(conflict["path"]))
                        if prior is not None and not self._stale_reasons(prior, pr, binding):
                            continue
                        if prior is not None:
                            self._mark_stale(
                                connection, prior, actor_name=actor_name, request_id=request_id
                            )
                        draft = self._create_draft_locked(
                            connection,
                            pr=pr,
                            repository=repository,
                            binding=binding,
                            conflict=conflict,
                            actor_name=actor_name,
                            request_id=request_id,
                        )
                        created_ids.append(str(draft["id"]))
                connection.commit()
            except Exception:
                connection.rollback()
                for draft_id in created_ids:
                    shutil.rmtree(self._draft_root(repository_id, pull_request_id, draft_id), ignore_errors=True)
                raise
            finally:
                connection.close()
        self.audit(
            action="conflict_resolution_prepared",
            outcome="success",
            repository_id=repository_id,
            actor=actor_name,
            subject_id=pull_request_id,
            surface="owner",
            details={
                "pullRequestId": pull_request_id,
                "revision": expected_revision,
                "createdDraftCount": len(created_ids),
                "requestId": str(request_id or "")[:120],
            },
        )
        return self.list_owner(repository_id, pull_request_id, include_content=True)

    def _load_evidence(self, draft: sqlite3.Row, *, include_content: bool) -> dict[str, Any]:
        root = self._draft_root(str(draft["repository_id"]), str(draft["pull_request_id"]), str(draft["id"]))
        manifest_path = root / "manifest.json"
        try:
            manifest_json = manifest_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ForgeTraceError(
                "Conflict-resolution evidence is missing.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "conflict_resolution_integrity_failed",
            ) from exc
        if hashlib.sha256(manifest_json.encode("utf-8")).hexdigest() != str(draft["evidence_manifest_sha256"]):
            raise ForgeTraceError(
                "Conflict-resolution evidence metadata failed integrity verification.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "conflict_resolution_integrity_failed",
            )
        try:
            evidence = json.loads(manifest_json)
        except json.JSONDecodeError as exc:
            raise ForgeTraceError(
                "Conflict-resolution evidence metadata is invalid.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "conflict_resolution_integrity_failed",
            ) from exc
        for role in ("base", "current", "incoming"):
            item = evidence.get(role) or {}
            if item.get("kind") == "file":
                path = root / f"{role}.bin"
                try:
                    if path.is_symlink() or not path.is_file():
                        raise OSError("evidence file is missing or not regular")
                    size, digest = self._hash_file(path)
                except OSError as exc:
                    raise ForgeTraceError(
                        "Conflict-resolution evidence bytes are missing or unreadable.",
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "conflict_resolution_integrity_failed",
                        {"role": role, "path": str(draft["path"])},
                    ) from exc
                if size != int(item.get("size") or 0) or digest != str(item.get("sha256") or ""):
                    raise ForgeTraceError(
                        "Conflict-resolution evidence bytes failed integrity verification.",
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "conflict_resolution_integrity_failed",
                        {"role": role, "path": str(draft["path"])},
                    )
                if include_content and bool(evidence.get("inlineEligible")):
                    try:
                        item["textContent"] = path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError) as exc:
                        raise ForgeTraceError(
                            "Conflict-resolution evidence text failed integrity verification.",
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            "conflict_resolution_integrity_failed",
                            {"role": role, "path": str(draft["path"])},
                        ) from exc
        resolved_path = root / "resolved.bin"
        if str(draft["result_kind"]) == "file":
            try:
                if resolved_path.is_symlink() or not resolved_path.is_file():
                    raise OSError("resolved evidence file is missing or not regular")
                size, digest = self._hash_file(resolved_path)
            except OSError as exc:
                raise ForgeTraceError(
                    "Resolved draft bytes are missing or unreadable.",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "conflict_resolution_integrity_failed",
                ) from exc
            if size != int(draft["resolved_size"]) or digest != str(draft["resolved_hash"]):
                raise ForgeTraceError(
                    "Resolved draft bytes failed integrity verification.",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "conflict_resolution_integrity_failed",
                )
            if include_content and bool(draft["inline_eligible"]):
                evidence["resolvedTextContent"] = resolved_path.read_text(encoding="utf-8")
        return evidence

    def _events(self, connection: sqlite3.Connection, draft_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM conflict_resolution_events WHERE draft_id=? ORDER BY sequence",
            (draft_id,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "eventType": row["event_type"],
                "actorName": row["actor_name"],
                "draftVersion": int(row["draft_version"]),
                "decision": row["decision"],
                "resultKind": row["result_kind"],
                "resolvedHash": row["resolved_hash"],
                "createdAt": row["created_at"],
                "requestId": row["request_id"],
            }
            for row in rows
        ]

    def _public_draft(
        self,
        connection: sqlite3.Connection,
        draft: sqlite3.Row,
        *,
        include_content: bool,
        stale_reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        evidence = self._load_evidence(draft, include_content=include_content)
        return {
            "id": draft["id"],
            "sequence": int(draft["sequence"]),
            "repositoryId": draft["repository_id"],
            "pullRequestId": draft["pull_request_id"],
            "submittedRevision": int(draft["submitted_revision"]),
            "path": draft["path"],
            "conflictReason": draft["conflict_reason"],
            "submittedKind": draft["submitted_kind"],
            "repositoryDigest": draft["repository_digest"],
            "accessMode": draft["access_mode"],
            "conflictSetDigest": draft["conflict_set_digest"],
            "reviewGateDigest": draft["review_gate_digest"],
            "unresolvedThreadCount": int(draft["unresolved_thread_count"]),
            "baseHash": draft["base_hash"],
            "currentHash": draft["current_hash"],
            "incomingHash": draft["incoming_hash"],
            "inlineEligible": bool(draft["inline_eligible"]),
            "decision": draft["decision"],
            "resultKind": draft["result_kind"],
            "resolvedHash": draft["resolved_hash"],
            "resolvedSize": int(draft["resolved_size"]),
            "authorName": draft["author_name"],
            "status": draft["status"],
            "version": int(draft["version"]),
            "createdRequestId": draft["created_request_id"],
            "updatedRequestId": draft["updated_request_id"],
            "confirmedRequestId": draft["confirmed_request_id"],
            "createdAt": draft["created_at"],
            "updatedAt": draft["updated_at"],
            "confirmedAt": draft["confirmed_at"],
            "appliedAt": draft["applied_at"],
            "staleReasons": stale_reasons or [],
            "evidence": evidence,
            "events": self._events(connection, str(draft["id"])),
            "activeContentRendered": False,
        }

    def _model_from_state(
        self,
        connection: sqlite3.Connection,
        pr: sqlite3.Row,
        repository,
        binding: dict[str, Any],
        *,
        include_content: bool,
    ) -> dict[str, Any]:
        latest = self._latest_drafts(connection, str(pr["id"]), int(pr["revision"]))
        conflict_payload: list[dict[str, Any]] = []
        confirmed = 0
        for conflict in binding["conflicts"]:
            draft = latest.get(str(conflict["path"]))
            public_draft = None
            stale_reasons: list[str] = []
            if draft is not None:
                stale_reasons = self._stale_reasons(draft, pr, binding)
                if stale_reasons:
                    draft = self._mark_stale(connection, draft)
                public_draft = self._public_draft(
                    connection, draft, include_content=include_content, stale_reasons=stale_reasons
                )
                if str(draft["status"]) in {"confirmed", "applied"} and not stale_reasons:
                    confirmed += 1
            conflict_payload.append({**conflict, "draft": public_draft})
        complete = len(binding["conflicts"]) == confirmed
        return {
            "repositoryId": pr["repository_id"],
            "pullRequestId": pr["id"],
            "pullRequestStatus": pr["status"],
            "submittedRevision": int(pr["revision"]),
            "repositoryDigest": binding["repositoryDigest"],
            "accessMode": binding["accessMode"],
            "accessPolicy": binding["accessPolicy"],
            "conflictSetDigest": binding["conflictSetDigest"],
            "reviewGateDigest": binding["reviewGateDigest"],
            "unresolvedThreadCount": int(binding["unresolvedThreadCount"]),
            "conflictCount": len(binding["conflicts"]),
            "confirmedConflictCount": confirmed,
            "complete": complete,
            "readyForApproval": complete and int(binding["unresolvedThreadCount"]) == 0,
            "conflicts": conflict_payload,
            "activeContentRendered": False,
            "storageLocation": "application-data quarantine-side conflict-resolutions",
        }

    def list_owner(
        self, repository_id: str, pull_request_id: str, *, include_content: bool = True
    ) -> dict[str, Any]:
        with self.lock:
            connection = self.connect()
            try:
                pr = self.owner_pr_resolver(connection, repository_id, pull_request_id)
                files = self.file_rows(connection, pull_request_id)
                deletions = self.deletion_rows(connection, pull_request_id)
                repository = self.registry.repository_service(repository_id)
                with repository.lock:
                    binding = self._binding_locked(connection, pr, repository, files, deletions)
                    result = self._model_from_state(
                        connection, pr, repository, binding, include_content=include_content
                    )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def summary_owner(self, repository_id: str, pull_request_id: str) -> dict[str, Any]:
        model = self.list_owner(repository_id, pull_request_id, include_content=False)
        return {
            key: model[key]
            for key in (
                "submittedRevision", "repositoryDigest", "accessMode", "conflictSetDigest",
                "unresolvedThreadCount", "conflictCount", "confirmedConflictCount", "complete",
                "readyForApproval", "activeContentRendered",
            )
        }

    def _draft_row(
        self,
        connection: sqlite3.Connection,
        *,
        repository_id: str,
        pull_request_id: str,
        draft_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM conflict_resolution_drafts WHERE id=? AND repository_id=? AND pull_request_id=?",
            (draft_id, repository_id, pull_request_id),
        ).fetchone()
        if not row:
            raise ForgeTraceError(
                "Conflict-resolution draft not found.",
                HTTPStatus.NOT_FOUND,
                "conflict_resolution_not_found",
            )
        return row

    @staticmethod
    def _assert_version(draft: sqlite3.Row, expected_version: Any) -> None:
        try:
            expected = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise ForgeTraceError("expectedVersion must be a whole number.", code="invalid_expected_version") from exc
        if expected != int(draft["version"]):
            raise ForgeTraceError(
                "Conflict-resolution draft changed. Refresh before continuing.",
                HTTPStatus.CONFLICT,
                "conflict_resolution_version_changed",
                {"expectedVersion": expected, "currentVersion": int(draft["version"])},
            )

    def _validate_current_locked(
        self,
        connection: sqlite3.Connection,
        pr: sqlite3.Row,
        draft: sqlite3.Row,
        repository,
    ) -> tuple[dict[str, Any], list[str]]:
        files = self.file_rows(connection, str(pr["id"]))
        deletions = self.deletion_rows(connection, str(pr["id"]))
        binding = self._binding_locked(connection, pr, repository, files, deletions)
        reasons = self._stale_reasons(draft, pr, binding)
        if reasons:
            self._mark_stale(connection, draft)
        return binding, reasons

    def save_decision_owner(
        self,
        repository_id: str,
        pull_request_id: str,
        draft_id: str,
        *,
        actor_name: str,
        decision: str,
        manual_text: Any = None,
        expected_version: Any,
        request_id: str = "",
    ) -> dict[str, Any]:
        actor_name = self._clean_text(actor_name, label="Owner name", maximum=120, required=True)
        decision = str(decision or "").strip().lower()
        if decision not in {"current", "incoming", "manual", "delete"}:
            raise ForgeTraceError("Conflict-resolution decision is invalid.", code="invalid_conflict_resolution_decision")
        with self.lock:
            connection = self.connect()
            try:
                pr = self.owner_pr_resolver(connection, repository_id, pull_request_id)
                draft = self._draft_row(
                    connection, repository_id=repository_id, pull_request_id=pull_request_id, draft_id=draft_id
                )
                self._assert_version(draft, expected_version)
                if str(draft["status"]) in {"stale", "applied"}:
                    raise ForgeTraceError(
                        "This conflict-resolution draft is no longer editable.",
                        HTTPStatus.CONFLICT,
                        "conflict_resolution_not_editable",
                    )
                repository = self.registry.repository_service(repository_id)
                with repository.lock:
                    _binding, reasons = self._validate_current_locked(connection, pr, draft, repository)
                    if reasons:
                        connection.commit()
                        raise ForgeTraceError(
                            "Conflict-resolution draft is stale. Prepare a new draft from current evidence.",
                            HTTPStatus.CONFLICT,
                            "conflict_resolution_stale",
                            {"staleReasons": reasons},
                        )
                    evidence = self._load_evidence(draft, include_content=False)
                    root = self._draft_root(repository_id, pull_request_id, draft_id)
                    resolved = root / "resolved.bin"
                    result_kind = "file"
                    resolved_hash = ""
                    resolved_size = 0
                    if decision == "manual":
                        if not bool(draft["inline_eligible"]):
                            raise ForgeTraceError(
                                "Manual inline resolution is unavailable for binary or oversized evidence.",
                                HTTPStatus.CONFLICT,
                                "manual_conflict_resolution_unavailable",
                            )
                        if manual_text is None:
                            raise ForgeTraceError("Manual resolution text is required.", code="manual_resolution_required")
                        manual_value = str(manual_text)
                        line_count = len(manual_value.splitlines()) if manual_value else 0
                        if line_count > MAX_RESOLUTION_TEXT_LINES:
                            raise ForgeTraceError(
                                "Manual resolution exceeds the inline resolution line limit.",
                                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                "manual_resolution_too_many_lines",
                                {"limitLines": MAX_RESOLUTION_TEXT_LINES, "actualLines": line_count},
                            )
                        content = manual_value.encode("utf-8")
                        if len(content) > MAX_INLINE_RESOLUTION_BYTES:
                            raise ForgeTraceError(
                                "Manual resolution exceeds the inline resolution size limit.",
                                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                "manual_resolution_too_large",
                                {"limitBytes": MAX_INLINE_RESOLUTION_BYTES, "actualBytes": len(content)},
                            )
                        resolved_size, resolved_hash = self._write_bytes(resolved, content)
                    elif decision == "delete":
                        result_kind = "deletion"
                        resolved.unlink(missing_ok=True)
                    else:
                        role = "current" if decision == "current" else "incoming"
                        item = evidence.get(role) or {}
                        if item.get("kind") == "absent":
                            result_kind = "deletion"
                            resolved.unlink(missing_ok=True)
                        elif item.get("kind") == "file":
                            resolved_size = self._copy_verified(
                                root / f"{role}.bin",
                                resolved,
                                expected_hash=str(item.get("sha256") or ""),
                                expected_size=int(item.get("size") or 0),
                            )
                            resolved_hash = str(item.get("sha256") or "")
                        else:
                            raise ForgeTraceError(
                                f"The preserved {role} evidence is unavailable.",
                                HTTPStatus.CONFLICT,
                                "conflict_resolution_evidence_unavailable",
                            )
                    now = utc_now()
                    connection.execute(
                        """
                        UPDATE conflict_resolution_drafts
                        SET decision=?, result_kind=?, resolved_hash=?, resolved_size=?, author_name=?,
                            status='draft', version=version+1, updated_at=?, updated_request_id=?,
                            confirmed_at='', confirmed_request_id=''
                        WHERE id=?
                        """,
                        (
                            decision, result_kind, resolved_hash, resolved_size, actor_name,
                            now, str(request_id or "")[:120], draft_id,
                        ),
                    )
                    updated = connection.execute(
                        "SELECT * FROM conflict_resolution_drafts WHERE id=?", (draft_id,)
                    ).fetchone()
                    self._event(
                        connection, updated, event_type="decision_saved", actor_name=actor_name, request_id=request_id
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        self.audit(
            action="conflict_resolution_decision_saved",
            outcome="success",
            repository_id=repository_id,
            actor=actor_name,
            subject_id=pull_request_id,
            surface="owner",
            details={
                "pullRequestId": pull_request_id,
                "draftId": draft_id,
                "path": str(updated["path"]),
                "decision": decision,
                "resultKind": str(updated["result_kind"]),
                "resolvedHash": str(updated["resolved_hash"]),
                "resolvedBytes": int(updated["resolved_size"]),
                "requestId": str(request_id or "")[:120],
            },
        )
        return self.get_owner(repository_id, pull_request_id, draft_id)

    def confirm_owner(
        self,
        repository_id: str,
        pull_request_id: str,
        draft_id: str,
        *,
        actor_name: str,
        expected_version: Any,
        request_id: str = "",
    ) -> dict[str, Any]:
        actor_name = self._clean_text(actor_name, label="Owner name", maximum=120, required=True)
        with self.lock:
            connection = self.connect()
            try:
                pr = self.owner_pr_resolver(connection, repository_id, pull_request_id)
                draft = self._draft_row(
                    connection, repository_id=repository_id, pull_request_id=pull_request_id, draft_id=draft_id
                )
                self._assert_version(draft, expected_version)
                if str(draft["status"]) != "draft" or not str(draft["decision"]):
                    raise ForgeTraceError(
                        "Save an explicit resolution decision before confirmation.",
                        HTTPStatus.CONFLICT,
                        "conflict_resolution_decision_required",
                    )
                repository = self.registry.repository_service(repository_id)
                with repository.lock:
                    _binding, reasons = self._validate_current_locked(connection, pr, draft, repository)
                    if reasons:
                        connection.commit()
                        raise ForgeTraceError(
                            "Conflict-resolution draft is stale. Prepare a new draft from current evidence.",
                            HTTPStatus.CONFLICT,
                            "conflict_resolution_stale",
                            {"staleReasons": reasons},
                        )
                    self._load_evidence(draft, include_content=False)
                    self.audit(
                        required=True,
                        action="conflict_resolution_confirmation_authorized",
                        outcome="authorized",
                        severity="warning",
                        repository_id=repository_id,
                        actor=actor_name,
                        subject_id=pull_request_id,
                        surface="owner",
                        details={
                            "pullRequestId": pull_request_id,
                            "draftId": draft_id,
                            "path": str(draft["path"]),
                            "revision": int(pr["revision"]),
                            "decision": str(draft["decision"]),
                            "resultKind": str(draft["result_kind"]),
                            "resolvedHash": str(draft["resolved_hash"]),
                            "requestId": str(request_id or "")[:120],
                        },
                    )
                    now = utc_now()
                    connection.execute(
                        """
                        UPDATE conflict_resolution_drafts
                        SET status='confirmed', version=version+1, author_name=?, confirmed_at=?,
                            updated_at=?, confirmed_request_id=?, updated_request_id=?
                        WHERE id=?
                        """,
                        (
                            actor_name, now, now, str(request_id or "")[:120],
                            str(request_id or "")[:120], draft_id,
                        ),
                    )
                    updated = connection.execute(
                        "SELECT * FROM conflict_resolution_drafts WHERE id=?", (draft_id,)
                    ).fetchone()
                    self._event(
                        connection, updated, event_type="confirmed", actor_name=actor_name, request_id=request_id
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        self.audit(
            action="conflict_resolution_confirmed",
            outcome="success",
            severity="warning",
            repository_id=repository_id,
            actor=actor_name,
            subject_id=pull_request_id,
            surface="owner",
            details={
                "pullRequestId": pull_request_id,
                "draftId": draft_id,
                "path": str(updated["path"]),
                "decision": str(updated["decision"]),
                "resultKind": str(updated["result_kind"]),
                "resolvedHash": str(updated["resolved_hash"]),
                "requestId": str(request_id or "")[:120],
            },
        )
        return self.get_owner(repository_id, pull_request_id, draft_id)

    def get_owner(self, repository_id: str, pull_request_id: str, draft_id: str) -> dict[str, Any]:
        with self.lock:
            connection = self.connect()
            try:
                pr = self.owner_pr_resolver(connection, repository_id, pull_request_id)
                draft = self._draft_row(
                    connection, repository_id=repository_id, pull_request_id=pull_request_id, draft_id=draft_id
                )
                repository = self.registry.repository_service(repository_id)
                with repository.lock:
                    files = self.file_rows(connection, pull_request_id)
                    deletions = self.deletion_rows(connection, pull_request_id)
                    binding = self._binding_locked(connection, pr, repository, files, deletions)
                    reasons = self._stale_reasons(draft, pr, binding)
                    if reasons:
                        draft = self._mark_stale(connection, draft)
                    result = self._public_draft(
                        connection, draft, include_content=True, stale_reasons=reasons
                    )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _verified_revision_file(
        self,
        connection: sqlite3.Connection,
        pr: sqlite3.Row,
        item: dict[str, Any],
    ) -> Path:
        _manifest, entry = self._revision_entry(
            connection, str(pr["id"]), int(pr["revision"]), str(item["path"])
        )
        if entry.get("kind") != "file" or not entry.get("snapshotAvailable"):
            raise ForgeTraceError(
                "Immutable submitted bytes are unavailable for merge.",
                HTTPStatus.CONFLICT,
                "merge_revision_evidence_unavailable",
                {"path": str(item["path"])},
            )
        path = self.review_store._revision_file(
            str(pr["repository_id"]), str(pr["id"]), int(pr["revision"]), str(item["path"])
        )
        size, digest = self._hash_file(path)
        if size != int(item["size"]) or digest != str(item["sha256"]):
            raise ForgeTraceError(
                "Immutable submitted bytes failed merge-time integrity verification.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "merge_revision_integrity_failed",
                {"path": str(item["path"])},
            )
        return path

    def build_merge_plan_locked(
        self,
        connection: sqlite3.Connection,
        *,
        pr: sqlite3.Row,
        repository,
        files: list[dict[str, Any]],
        deletions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return verified staged inputs while the repository and collaboration locks are held."""
        binding = self._binding_locked(connection, pr, repository, files, deletions)
        if int(binding["unresolvedThreadCount"]):
            raise ForgeTraceError(
                "Resolve every thread on the current submitted revision before merging.",
                HTTPStatus.CONFLICT,
                "unresolved_review_threads",
                {"unresolvedThreadCount": int(binding["unresolvedThreadCount"])},
            )
        latest = self._latest_drafts(connection, str(pr["id"]), int(pr["revision"]))
        conflict_map = {str(item["path"]): item for item in binding["conflicts"]}
        resolution_rows: dict[str, sqlite3.Row] = {}
        missing: list[dict[str, Any]] = []
        for path, conflict in conflict_map.items():
            draft = latest.get(path)
            reasons = self._stale_reasons(draft, pr, binding) if draft is not None else ["resolution_missing"]
            if draft is None or str(draft["status"]) != "confirmed" or reasons:
                if draft is not None and reasons:
                    self._mark_stale(connection, draft)
                missing.append({"path": path, "reasons": reasons or ["resolution_not_confirmed"]})
                continue
            self._load_evidence(draft, include_content=False)
            resolution_rows[path] = draft
        if missing:
            raise ForgeTraceError(
                "Every current conflict requires a confirmed, current resolution before merge.",
                HTTPStatus.CONFLICT,
                "conflict_resolution_required",
                {"conflicts": binding["conflicts"], "missingResolutions": missing},
            )

        staged_changes: dict[str, Path] = {}
        merge_deletions: list[str] = []
        expected_hashes: dict[str, str] = {}
        draft_ids: list[str] = []
        for item in files:
            path = str(item["path"])
            if path in conflict_map:
                draft = resolution_rows[path]
                expected_hashes[path] = str(draft["current_hash"] or "")
                draft_ids.append(str(draft["id"]))
                if str(draft["result_kind"]) == "deletion":
                    merge_deletions.append(path)
                else:
                    staged_changes[path] = self._draft_root(
                        str(pr["repository_id"]), str(pr["id"]), str(draft["id"])
                    ) / "resolved.bin"
            else:
                expected_hashes[path] = str(item.get("base_hash") or "")
                staged_changes[path] = self._verified_revision_file(connection, pr, item)
        for item in deletions:
            path = str(item["path"])
            if path in conflict_map:
                draft = resolution_rows[path]
                expected_hashes[path] = str(draft["current_hash"] or "")
                draft_ids.append(str(draft["id"]))
                if str(draft["result_kind"]) == "deletion":
                    merge_deletions.append(path)
                else:
                    staged_changes[path] = self._draft_root(
                        str(pr["repository_id"]), str(pr["id"]), str(draft["id"])
                    ) / "resolved.bin"
            else:
                expected_hashes[path] = str(item.get("base_hash") or "")
                merge_deletions.append(path)
        return {
            "binding": binding,
            "stagedChanges": staged_changes,
            "deletions": merge_deletions,
            "expectedHashes": expected_hashes,
            "resolutionDraftIds": sorted(set(draft_ids)),
            "resolvedConflictCount": len(conflict_map),
        }

    def require_resolutions_for_approval_locked(
        self,
        connection: sqlite3.Connection,
        *,
        pr: sqlite3.Row,
        repository,
        files: list[dict[str, Any]],
        deletions: list[dict[str, Any]],
    ) -> None:
        binding = self._binding_locked(connection, pr, repository, files, deletions)
        if not binding["conflicts"]:
            return
        latest = self._latest_drafts(connection, str(pr["id"]), int(pr["revision"]))
        missing = []
        for conflict in binding["conflicts"]:
            draft = latest.get(str(conflict["path"]))
            reasons = self._stale_reasons(draft, pr, binding) if draft is not None else ["resolution_missing"]
            if draft is None or str(draft["status"]) != "confirmed" or reasons:
                missing.append({"path": conflict["path"], "reasons": reasons or ["resolution_not_confirmed"]})
        if missing:
            raise ForgeTraceError(
                "Confirm a current resolution for every conflict before approval.",
                HTTPStatus.CONFLICT,
                "conflict_resolution_required",
                {"conflicts": binding["conflicts"], "missingResolutions": missing},
            )

    def mark_applied(
        self,
        connection: sqlite3.Connection,
        draft_ids: list[str],
        *,
        actor_name: str,
        request_id: str = "",
    ) -> None:
        now = utc_now()
        for draft_id in draft_ids:
            row = connection.execute(
                "SELECT * FROM conflict_resolution_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if row is None or str(row["status"]) != "confirmed":
                continue
            connection.execute(
                "UPDATE conflict_resolution_drafts SET status='applied', version=version+1, applied_at=?, updated_at=?, updated_request_id=? WHERE id=?",
                (now, now, str(request_id or "")[:120], draft_id),
            )
            updated = connection.execute(
                "SELECT * FROM conflict_resolution_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            self._event(connection, updated, event_type="applied", actor_name=actor_name, request_id=request_id)

    def cleanup_retention(self) -> dict[str, int]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=CONFLICT_RESOLUTION_RETENTION_DAYS)).isoformat(timespec="seconds").replace("+00:00", "Z")
        removed_rows = 0
        paths: list[Path] = []
        with self.lock:
            connection = self.connect()
            try:
                rows = connection.execute(
                    """
                    SELECT d.id, d.repository_id, d.pull_request_id
                    FROM conflict_resolution_drafts d
                    JOIN pull_requests pr ON pr.id=d.pull_request_id
                    WHERE pr.status IN ('merged','closed') AND pr.updated_at < ?
                    """,
                    (cutoff,),
                ).fetchall()
                for row in rows:
                    paths.append(self._draft_root(row["repository_id"], row["pull_request_id"], row["id"]))
                if rows:
                    connection.execute(
                        """
                        DELETE FROM conflict_resolution_drafts
                        WHERE pull_request_id IN (
                            SELECT id FROM pull_requests
                            WHERE status IN ('merged','closed') AND updated_at < ?
                        )
                        """,
                        (cutoff,),
                    )
                    removed_rows = len(rows)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        for path in paths:
            shutil.rmtree(path, ignore_errors=True)
        return {
            "conflictResolutionDrafts": removed_rows,
            "orphanConflictResolutionDirectories": self._cleanup_orphan_directories(),
        }

    def _cleanup_orphan_directories(self) -> int:
        if not self.resolutions_dir.is_dir() or not self.db_path.exists():
            return 0
        connection = self.connect()
        try:
            known = {
                str(row["id"])
                for row in connection.execute("SELECT id FROM conflict_resolution_drafts")
            }
        except sqlite3.Error:
            return 0
        finally:
            connection.close()
        removed = 0
        for path in self.resolutions_dir.glob("*/*/*"):
            try:
                if path.is_dir() and path.name not in known:
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        return removed

    def storage_metrics(self, *, max_files: int | None = None) -> dict[str, Any]:
        bytes_total = 0
        files_total = 0
        complete = True
        limit = None if max_files is None else max(1, min(int(max_files), 1_000_000))
        for path in self.resolutions_dir.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    if limit is not None and files_total >= limit:
                        complete = False
                        break
                    bytes_total += path.stat().st_size
                    files_total += 1
            except OSError:
                continue
        connection = self.connect()
        try:
            rows = int(connection.execute("SELECT COUNT(*) AS count FROM conflict_resolution_drafts").fetchone()["count"])
            confirmed = int(connection.execute(
                "SELECT COUNT(*) AS count FROM conflict_resolution_drafts WHERE status='confirmed'"
            ).fetchone()["count"])
            stale = int(connection.execute(
                "SELECT COUNT(*) AS count FROM conflict_resolution_drafts WHERE status='stale'"
            ).fetchone()["count"])
        finally:
            connection.close()
        return {
            "bytes": bytes_total,
            "files": files_total,
            "draftCount": rows,
            "confirmedDraftCount": confirmed,
            "staleDraftCount": stale,
            "inlineResolutionLimitBytes": MAX_INLINE_RESOLUTION_BYTES,
            "terminalRetentionDays": CONFLICT_RESOLUTION_RETENTION_DAYS,
            "complete": complete,
        }

    def health_assessment(self, *, max_drafts: int = 200) -> dict[str, Any]:
        """Bounded, read-only integrity check for conflict-resolution evidence."""

        limit = max(1, min(int(max_drafts), 5000))
        issues: list[dict[str, Any]] = []
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=CONFLICT_RESOLUTION_RETENTION_DAYS)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.lock:
            connection = self.connect()
            try:
                total = int(
                    connection.execute("SELECT COUNT(*) FROM conflict_resolution_drafts").fetchone()[0]
                )
                rows = connection.execute(
                    "SELECT * FROM conflict_resolution_drafts ORDER BY sequence DESC LIMIT ?",
                    (limit + 1,),
                ).fetchall()
                statuses = {
                    str(row["status"]): int(row["count"])
                    for row in connection.execute(
                        "SELECT status, COUNT(*) AS count FROM conflict_resolution_drafts GROUP BY status"
                    )
                }
                retention_eligible = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM conflict_resolution_drafts d
                        JOIN pull_requests pr ON pr.id=d.pull_request_id
                        WHERE pr.status IN ('merged','closed') AND pr.updated_at < ?
                        """,
                        (cutoff,),
                    ).fetchone()[0]
                )
                known = {str(row["id"]) for row in connection.execute("SELECT id FROM conflict_resolution_drafts")}
                verified = 0
                for row in rows[:limit]:
                    try:
                        self._load_evidence(row, include_content=False)
                    except ForgeTraceError as exc:
                        issues.append(
                            {
                                "code": exc.code,
                                "repositoryId": str(row["repository_id"]),
                                "pullRequestId": str(row["pull_request_id"]),
                                "draftId": str(row["id"]),
                                "path": str(row["path"]),
                                "message": str(exc),
                            }
                        )
                    verified += 1
            finally:
                connection.close()

        orphan_count = 0
        scanned_directories = 0
        orphan_scan_complete = True
        for path in self.resolutions_dir.glob("*/*/*"):
            if scanned_directories >= limit * 2:
                orphan_scan_complete = False
                break
            scanned_directories += 1
            try:
                if path.is_dir() and path.name not in known:
                    orphan_count += 1
            except OSError:
                continue
        return {
            "draftCount": total,
            "verifiedDraftCount": verified,
            "complete": len(rows) <= limit and orphan_scan_complete,
            "orphanScanComplete": orphan_scan_complete,
            "byStatus": statuses,
            "confirmedDraftCount": int(statuses.get("confirmed", 0)),
            "staleDraftCount": int(statuses.get("stale", 0)),
            "retentionEligibleDraftCount": retention_eligible,
            "orphanConflictResolutionDirectoryCount": orphan_count,
            "issues": issues,
        }
