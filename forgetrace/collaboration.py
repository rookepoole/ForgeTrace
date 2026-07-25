from __future__ import annotations

import difflib
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterator

from .errors import ForgeTraceError
from .registry import RepositoryRegistry
from .utils import utc_now

COLLABORATION_SCHEMA_VERSION = 3
DEFAULT_INVITE_HOURS = 72
MAX_INVITE_HOURS = 24 * 30
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_PR_FILES = 500
MAX_DIFF_BYTES = 512 * 1024
MAX_SOURCE_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
RISKY_SUFFIXES = {
    ".exe", ".dll", ".com", ".scr", ".msi", ".msp", ".jar", ".app", ".dmg",
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".js", ".mjs", ".cjs", ".jscript",
    ".py", ".pyw", ".rb", ".pl", ".php", ".sh", ".bash", ".zsh", ".fish",
    ".run", ".deb", ".rpm", ".pkg", ".apk", ".ipa", ".wasm", ".hta",
    ".html", ".htm", ".svg",
}
PROTECTED_SEGMENTS = {".forgetrace", ".git"}


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CollaborationService:
    """Quarantined pull-request service.

    Remote contributors only receive token-scoped access to an isolated staging area
    under ForgeTrace's application-data directory. The service never accepts shell
    commands, never extracts archives, and never exposes arbitrary filesystem paths.
    """

    def __init__(self, registry: RepositoryRegistry) -> None:
        self.registry = registry
        self.data_dir = registry.data_dir / "collaboration"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "collaboration.sqlite3"
        self.quarantine_dir = self.data_dir / "quarantine"
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._migrate()
        self.cleanup_retention()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self.lock, self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS collaboration_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS collaboration_invites (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    max_uses INTEGER NOT NULL DEFAULT 1,
                    uses INTEGER NOT NULL DEFAULT 0,
                    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
                    max_file_bytes INTEGER NOT NULL,
                    max_total_bytes INTEGER NOT NULL,
                    allow_deletes INTEGER NOT NULL DEFAULT 1 CHECK(allow_deletes IN (0,1)),
                    allow_source_download INTEGER NOT NULL DEFAULT 1 CHECK(allow_source_download IN (0,1)),
                    allow_sensitive_source INTEGER NOT NULL DEFAULT 0 CHECK(allow_sensitive_source IN (0,1)),
                    last_used_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_collaboration_invites_repository
                    ON collaboration_invites(repository_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS pull_requests (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    invite_id TEXT NOT NULL REFERENCES collaboration_invites(id) ON DELETE RESTRICT,
                    number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    author_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','open','approved','changes_requested','conflict','merged','closed')),
                    base_commit_id TEXT NOT NULL,
                    base_manifest_json TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    submitted_at TEXT NOT NULL DEFAULT '',
                    merged_at TEXT NOT NULL DEFAULT '',
                    merged_by TEXT NOT NULL DEFAULT '',
                    merge_commit_id TEXT NOT NULL DEFAULT '',
                    closed_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(repository_id, number)
                );
                CREATE INDEX IF NOT EXISTS idx_pull_requests_repository
                    ON pull_requests(repository_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS pull_request_files (
                    pull_request_id TEXT NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    base_hash TEXT NOT NULL DEFAULT '',
                    risky INTEGER NOT NULL DEFAULT 0 CHECK(risky IN (0,1)),
                    uploaded_at TEXT NOT NULL,
                    PRIMARY KEY(pull_request_id, path)
                );

                CREATE TABLE IF NOT EXISTS pull_request_deletions (
                    pull_request_id TEXT NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    base_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(pull_request_id, path)
                );

                CREATE TABLE IF NOT EXISTS pull_request_reviews (
                    id TEXT PRIMARY KEY,
                    pull_request_id TEXT NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
                    reviewer TEXT NOT NULL,
                    verdict TEXT NOT NULL CHECK(verdict IN ('approved','changes_requested','comment')),
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )
            invite_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(collaboration_invites)")
            }
            if "allow_source_download" not in invite_columns:
                connection.execute(
                    "ALTER TABLE collaboration_invites "
                    "ADD COLUMN allow_source_download INTEGER NOT NULL DEFAULT 1 "
                    "CHECK(allow_source_download IN (0,1))"
                )
            if "allow_sensitive_source" not in invite_columns:
                connection.execute(
                    "ALTER TABLE collaboration_invites "
                    "ADD COLUMN allow_sensitive_source INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(allow_sensitive_source IN (0,1))"
                )
            connection.execute(
                """
                INSERT INTO collaboration_meta(key, value, updated_at) VALUES ('schema_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (str(COLLABORATION_SCHEMA_VERSION), utc_now()),
            )

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _clean_text(value: Any, *, label: str, maximum: int, required: bool = False) -> str:
        result = str(value or "").strip()
        if required and not result:
            raise ForgeTraceError(f"{label} is required.", code=f"{label.lower().replace(' ', '_')}_required")
        if len(result) > maximum:
            raise ForgeTraceError(f"{label} may not exceed {maximum} characters.", code="value_too_long")
        return result

    @staticmethod
    def _validate_path(repository, raw_path: str) -> str:
        rel = repository.normalize_rel(raw_path)
        segments = {segment.casefold() for segment in Path(rel).parts}
        if segments & PROTECTED_SEGMENTS:
            raise ForgeTraceError(
                "Pull requests cannot modify .git or .forgetrace metadata.",
                HTTPStatus.FORBIDDEN,
                "protected_collaboration_path",
            )
        return rel

    def _staged_path(self, repository_id: str, pull_request_id: str, rel: str) -> Path:
        root = (self.quarantine_dir / repository_id / pull_request_id / "files").resolve()
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise ForgeTraceError("Staged path escapes quarantine.", HTTPStatus.FORBIDDEN, "quarantine_path_escape")
        return target

    @staticmethod
    def _is_risky_file(rel: str, content: bytes) -> bool:
        if Path(rel).suffix.casefold() in RISKY_SUFFIXES:
            return True
        prefix = content[:8]
        executable_magic = (
            b"MZ",
            b"\x7fELF",
            b"\xfe\xed\xfa\xce",
            b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xcf\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe",
        )
        return content.startswith(b"#!") or any(prefix.startswith(magic) for magic in executable_magic)

    def _invite_row_for_token(self, connection: sqlite3.Connection, token: str) -> sqlite3.Row:
        if not token or len(token) < 32:
            raise ForgeTraceError("A valid collaboration invite is required.", HTTPStatus.UNAUTHORIZED, "invite_required")
        row = connection.execute(
            "SELECT * FROM collaboration_invites WHERE token_hash = ?",
            (self._hash_token(token),),
        ).fetchone()
        if not row:
            raise ForgeTraceError("Collaboration invite is invalid.", HTTPStatus.UNAUTHORIZED, "invalid_invite")
        if row["revoked"]:
            raise ForgeTraceError("Collaboration invite has been revoked.", HTTPStatus.FORBIDDEN, "invite_revoked")
        if _parse_utc(row["expires_at"]) <= datetime.now(timezone.utc):
            raise ForgeTraceError("Collaboration invite has expired.", HTTPStatus.GONE, "invite_expired")
        return row

    def _pr_for_token(self, connection: sqlite3.Connection, token: str, pull_request_id: str) -> tuple[sqlite3.Row, sqlite3.Row]:
        invite = self._invite_row_for_token(connection, token)
        row = connection.execute("SELECT * FROM pull_requests WHERE id = ?", (pull_request_id,)).fetchone()
        if not row or row["invite_id"] != invite["id"]:
            raise ForgeTraceError("Pull request not found for this invite.", HTTPStatus.NOT_FOUND, "pull_request_not_found")
        return invite, row

    def _repository_public(self, repository_id: str) -> dict[str, Any]:
        record = self.registry.get_repository(repository_id)
        return {
            "id": record["id"],
            "name": record["name"],
            "description": record["description"],
            "status": record["status"],
        }

    def create_invite(
        self,
        repository_id: str,
        *,
        label: str = "",
        expires_in_hours: int = DEFAULT_INVITE_HOURS,
        max_uses: int = 1,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        allow_deletes: bool = True,
        allow_source_download: bool = True,
        allow_sensitive_source: bool = False,
    ) -> dict[str, Any]:
        repository = self.registry.repository_service(repository_id)
        try:
            expires_in_hours = int(expires_in_hours)
            max_uses = int(max_uses)
            max_file_bytes = int(max_file_bytes)
            max_total_bytes = int(max_total_bytes)
        except (TypeError, ValueError) as exc:
            raise ForgeTraceError("Invite limits must be whole numbers.", code="invalid_invite_limits") from exc
        if not 1 <= expires_in_hours <= MAX_INVITE_HOURS:
            raise ForgeTraceError("Invite lifetime must be between 1 hour and 30 days.", code="invite_lifetime_out_of_range")
        if not 1 <= max_uses <= 100:
            raise ForgeTraceError("Invite uses must be between 1 and 100.", code="invite_uses_out_of_range")
        max_file_bytes = min(max_file_bytes, repository.upload_limit_bytes)
        if max_file_bytes < 1024:
            raise ForgeTraceError("Maximum file size must be at least 1 KB.", code="invite_file_limit_too_small")
        if max_total_bytes < max_file_bytes or max_total_bytes > 4 * 1024 * 1024 * 1024:
            raise ForgeTraceError("Total pull-request size must be at least the file limit and no more than 4 GB.", code="invite_total_limit_out_of_range")
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=expires_in_hours)
        token = secrets.token_urlsafe(32)
        invite_id = "inv_" + uuid.uuid4().hex[:16]
        with self.lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO collaboration_invites(
                    id, repository_id, token_hash, label, created_at, expires_at,
                    max_uses, max_file_bytes, max_total_bytes, allow_deletes,
                    allow_source_download, allow_sensitive_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invite_id, repository_id, self._hash_token(token),
                    self._clean_text(label, label="Invite label", maximum=120),
                    now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    max_uses, max_file_bytes, max_total_bytes, int(bool(allow_deletes)),
                    int(bool(allow_source_download)), int(bool(allow_sensitive_source)),
                ),
            )
        return {
            "invite": self.get_invite(invite_id),
            "token": token,
            "sharePath": f"/contribute.html#{token}",
            "security": "The token is shown once and stored only as a SHA-256 hash.",
        }

    def _public_invite(self, row: sqlite3.Row) -> dict[str, Any]:
        expired = _parse_utc(row["expires_at"]) <= datetime.now(timezone.utc)
        return {
            "id": row["id"],
            "repositoryId": row["repository_id"],
            "label": row["label"],
            "createdAt": row["created_at"],
            "expiresAt": row["expires_at"],
            "maxUses": int(row["max_uses"]),
            "uses": int(row["uses"]),
            "revoked": bool(row["revoked"]),
            "expired": expired,
            "active": not row["revoked"] and not expired and int(row["uses"]) < int(row["max_uses"]),
            "maxFileBytes": int(row["max_file_bytes"]),
            "maxTotalBytes": int(row["max_total_bytes"]),
            "allowDeletes": bool(row["allow_deletes"]),
            "allowSourceDownload": bool(row["allow_source_download"]),
            "allowSensitiveSource": bool(row["allow_sensitive_source"]),
            "lastUsedAt": row["last_used_at"],
        }

    def get_invite(self, invite_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM collaboration_invites WHERE id = ?", (invite_id,)).fetchone()
            if not row:
                raise ForgeTraceError("Invite not found.", HTTPStatus.NOT_FOUND, "invite_not_found")
            return self._public_invite(row)

    def list_invites(self, repository_id: str) -> list[dict[str, Any]]:
        self.registry.get_repository(repository_id)
        with self.connect() as connection:
            return [
                self._public_invite(row)
                for row in connection.execute(
                    "SELECT * FROM collaboration_invites WHERE repository_id = ? ORDER BY created_at DESC",
                    (repository_id,),
                )
            ]

    def revoke_invite(self, repository_id: str, invite_id: str) -> dict[str, Any]:
        with self.lock, self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM collaboration_invites WHERE id = ? AND repository_id = ?",
                (invite_id, repository_id),
            ).fetchone()
            if not row:
                raise ForgeTraceError("Invite not found.", HTTPStatus.NOT_FOUND, "invite_not_found")
            connection.execute("UPDATE collaboration_invites SET revoked = 1 WHERE id = ?", (invite_id,))
        return self.get_invite(invite_id)

    def invite_context(self, token: str) -> dict[str, Any]:
        with self.connect() as connection:
            invite = self._invite_row_for_token(connection, token)
            return {
                "repository": self._repository_public(invite["repository_id"]),
                "invite": self._public_invite(invite),
                "canCreatePullRequest": int(invite["uses"]) < int(invite["max_uses"]),
                "rules": {
                    "directWorkspaceAccess": False,
                    "archiveExtraction": False,
                    "commandsAllowed": False,
                    "sourceDownload": bool(invite["allow_source_download"]),
                    "protectedPaths": sorted(PROTECTED_SEGMENTS),
                },
            }

    def source_archive_file(self, token: str) -> tuple[Path, str]:
        """Build a token-scoped source-only archive on disk for streamed transfer."""
        with self.connect() as connection:
            invite = self._invite_row_for_token(connection, token)
            if not invite["allow_source_download"]:
                raise ForgeTraceError(
                    "This invitation does not permit source downloads.",
                    HTTPStatus.FORBIDDEN,
                    "source_download_not_allowed",
                )
            repository_id = invite["repository_id"]
        repository = self.registry.repository_service(repository_id)
        summary = repository.summary()
        source_bytes = int(summary.get("stats", {}).get("bytes", 0))
        if source_bytes > MAX_SOURCE_ARCHIVE_BYTES:
            raise ForgeTraceError(
                "Repository source is too large for the collaboration download gateway.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "source_archive_too_large",
                {"limitBytes": MAX_SOURCE_ARCHIVE_BYTES, "repositoryBytes": source_bytes},
            )
        transfer_dir = self.data_dir / "transfers"
        transfer_dir.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix="source-", suffix=".zip", dir=transfer_dir)
        os.close(fd)
        archive_path = Path(raw_path)
        try:
            repository.export_zip_to_path(
                archive_path,
                include_history=False,
                include_vcs_metadata=False,
                include_sensitive=bool(invite["allow_sensitive_source"]),
            )
            archive_bytes = archive_path.stat().st_size
            if archive_bytes > MAX_SOURCE_ARCHIVE_BYTES:
                raise ForgeTraceError(
                    "Compressed repository source exceeds the collaboration download limit.",
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "source_archive_too_large",
                    {"limitBytes": MAX_SOURCE_ARCHIVE_BYTES, "archiveBytes": archive_bytes},
                )
            record = self.registry.get_repository(repository_id)
            safe = "".join(ch for ch in record["name"].replace(" ", "-") if ch.isalnum() or ch in "-_")
            return archive_path, (safe or "repository") + "-source.zip"
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise

    def source_archive(self, token: str) -> tuple[bytes, str]:
        """Compatibility helper for tests and small in-process consumers."""
        path, filename = self.source_archive_file(token)
        try:
            return path.read_bytes(), filename
        finally:
            path.unlink(missing_ok=True)

    def _ensure_baseline(self, repository) -> tuple[str, dict[str, Any]]:
        state = repository.load_state()
        current = repository.manifest(store_objects=False)
        latest = state["commits"][-1] if state["commits"] else None
        if latest is None or any(repository.diff_manifests(latest["manifest"], current).values()):
            author = state.get("repository", {}).get("defaultAuthor") or "Repository Owner"
            repository.create_commit("Collaboration baseline", author)
            state = repository.load_state()
            latest = state["commits"][-1]
        return latest["id"], latest["manifest"]

    def create_pull_request(self, token: str, *, title: str, description: str, author_name: str) -> dict[str, Any]:
        title = self._clean_text(title, label="Title", maximum=180, required=True)
        description = self._clean_text(description, label="Description", maximum=8000)
        author_name = self._clean_text(author_name, label="Contributor name", maximum=120, required=True)
        with self.lock, self.connect() as connection:
            invite = self._invite_row_for_token(connection, token)
            if int(invite["uses"]) >= int(invite["max_uses"]):
                raise ForgeTraceError("This invite has reached its pull-request limit.", HTTPStatus.GONE, "invite_exhausted")
            repository = self.registry.repository_service(invite["repository_id"])
            base_commit_id, base_manifest = self._ensure_baseline(repository)
            number_row = connection.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 AS next_number FROM pull_requests WHERE repository_id = ?",
                (invite["repository_id"],),
            ).fetchone()
            number = int(number_row["next_number"])
            pull_request_id = "pr_" + uuid.uuid4().hex[:16]
            now = utc_now()
            connection.execute(
                """
                INSERT INTO pull_requests(
                    id, repository_id, invite_id, number, title, description, author_name,
                    base_commit_id, base_manifest_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pull_request_id, invite["repository_id"], invite["id"], number,
                    title, description, author_name, base_commit_id,
                    json.dumps(base_manifest, separators=(",", ":")), now, now,
                ),
            )
            connection.execute(
                "UPDATE collaboration_invites SET uses = uses + 1, last_used_at = ? WHERE id = ?",
                (now, invite["id"]),
            )
        return self.get_pull_request_for_token(token, pull_request_id)

    def _file_rows(self, connection: sqlite3.Connection, pull_request_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM pull_request_files WHERE pull_request_id = ? ORDER BY path COLLATE NOCASE",
            (pull_request_id,),
        )]

    def _deletion_rows(self, connection: sqlite3.Connection, pull_request_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM pull_request_deletions WHERE pull_request_id = ? ORDER BY path COLLATE NOCASE",
            (pull_request_id,),
        )]

    def _review_rows(self, connection: sqlite3.Connection, pull_request_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM pull_request_reviews WHERE pull_request_id = ? ORDER BY created_at",
            (pull_request_id,),
        )]

    def _public_pr(self, connection: sqlite3.Connection, row: sqlite3.Row, *, include_changes: bool = False) -> dict[str, Any]:
        files = self._file_rows(connection, row["id"])
        deletions = self._deletion_rows(connection, row["id"])
        payload: dict[str, Any] = {
            "id": row["id"],
            "repositoryId": row["repository_id"],
            "number": int(row["number"]),
            "title": row["title"],
            "description": row["description"],
            "authorName": row["author_name"],
            "status": row["status"],
            "baseCommitId": row["base_commit_id"],
            "revision": int(row["revision"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "submittedAt": row["submitted_at"],
            "mergedAt": row["merged_at"],
            "mergedBy": row["merged_by"],
            "mergeCommitId": row["merge_commit_id"],
            "closedAt": row["closed_at"],
            "changeCount": len(files) + len(deletions),
            "fileCount": len(files),
            "deletionCount": len(deletions),
            "totalBytes": sum(int(item["size"]) for item in files),
            "riskyFileCount": sum(1 for item in files if item["risky"]),
            "reviews": self._review_rows(connection, row["id"]),
        }
        if include_changes:
            payload["files"] = [
                {
                    "path": item["path"], "size": int(item["size"]), "sha256": item["sha256"],
                    "baseHash": item["base_hash"], "risky": bool(item["risky"]), "uploadedAt": item["uploaded_at"],
                }
                for item in files
            ]
            payload["deletions"] = [
                {"path": item["path"], "baseHash": item["base_hash"], "createdAt": item["created_at"]}
                for item in deletions
            ]
        return payload

    def get_pull_request_for_token(self, token: str, pull_request_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            _invite, row = self._pr_for_token(connection, token, pull_request_id)
            return self._public_pr(connection, row, include_changes=True)

    def list_pull_requests_for_token(self, token: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            invite = self._invite_row_for_token(connection, token)
            rows = connection.execute(
                "SELECT * FROM pull_requests WHERE invite_id = ? ORDER BY created_at DESC",
                (invite["id"],),
            )
            return [self._public_pr(connection, row) for row in rows]

    def pull_request_upload_limit(self, token: str, pull_request_id: str) -> int:
        with self.connect() as connection:
            invite, row = self._pr_for_token(connection, token, pull_request_id)
            if row["status"] not in {"draft", "changes_requested"}:
                raise ForgeTraceError("Only draft or changes-requested pull requests can receive files.", HTTPStatus.CONFLICT, "pull_request_not_editable")
            return int(invite["max_file_bytes"])

    def upload_pull_request_file_from_path(
        self, token: str, pull_request_id: str, raw_path: str, source_path: Path
    ) -> dict[str, Any]:
        source = source_path.expanduser().resolve()
        size = source.stat().st_size
        with self.lock, self.connect() as connection:
            invite, row = self._pr_for_token(connection, token, pull_request_id)
            if row["status"] not in {"draft", "changes_requested"}:
                raise ForgeTraceError("Only draft or changes-requested pull requests can be changed.", HTTPStatus.CONFLICT, "pull_request_not_editable")
            repository = self.registry.repository_service(row["repository_id"])
            rel = self._validate_path(repository, raw_path)
            if size > int(invite["max_file_bytes"]):
                raise ForgeTraceError(
                    "File exceeds the invite's file limit.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "pull_request_file_too_large", {"limitBytes": int(invite["max_file_bytes"]), "fileBytes": size},
                )
            count_row = connection.execute(
                "SELECT COUNT(*) AS count FROM pull_request_files WHERE pull_request_id = ? AND path != ?",
                (pull_request_id, rel),
            ).fetchone()
            if int(count_row["count"]) >= MAX_PR_FILES:
                raise ForgeTraceError("Pull request file-count limit reached.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "pull_request_too_many_files")
            total_row = connection.execute(
                "SELECT COALESCE(SUM(size),0) AS total FROM pull_request_files WHERE pull_request_id = ? AND path != ?",
                (pull_request_id, rel),
            ).fetchone()
            proposed_total = int(total_row["total"]) + size
            if proposed_total > int(invite["max_total_bytes"]):
                raise ForgeTraceError(
                    "Pull request exceeds the invite's total-size limit.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "pull_request_total_too_large", {"limitBytes": int(invite["max_total_bytes"]), "totalBytes": proposed_total},
                )
            staged = self._staged_path(row["repository_id"], pull_request_id, rel)
            staged.parent.mkdir(parents=True, exist_ok=True)
            tmp = staged.with_name(f".{staged.name}.{uuid.uuid4().hex}.tmp")
            digest = hashlib.sha256()
            prefix = b""
            try:
                with source.open("rb") as input_handle, tmp.open("wb") as output_handle:
                    while True:
                        chunk = input_handle.read(1024 * 1024)
                        if not chunk:
                            break
                        if len(prefix) < 8:
                            prefix += chunk[: 8 - len(prefix)]
                        digest.update(chunk)
                        output_handle.write(chunk)
                    output_handle.flush()
                    os.fsync(output_handle.fileno())
                os.replace(tmp, staged)
            finally:
                tmp.unlink(missing_ok=True)
            base_manifest = json.loads(row["base_manifest_json"])
            base_hash = str(base_manifest.get(rel, {}).get("hash") or "")
            digest_hex = digest.hexdigest()
            if base_hash and digest_hex == base_hash:
                connection.execute("DELETE FROM pull_request_files WHERE pull_request_id = ? AND path = ?", (pull_request_id, rel))
                connection.execute("DELETE FROM pull_request_deletions WHERE pull_request_id = ? AND path = ?", (pull_request_id, rel))
                staged.unlink(missing_ok=True)
                now = utc_now()
                connection.execute(
                    "UPDATE pull_requests SET status='draft', submitted_at='', revision = revision + 1, updated_at = ? WHERE id = ?",
                    (now, pull_request_id),
                )
            else:
                risky = int(self._is_risky_file(rel, prefix))
                now = utc_now()
                connection.execute("DELETE FROM pull_request_deletions WHERE pull_request_id = ? AND path = ?", (pull_request_id, rel))
                connection.execute(
                    """
                    INSERT INTO pull_request_files(pull_request_id, path, size, sha256, base_hash, risky, uploaded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pull_request_id, path) DO UPDATE SET
                        size=excluded.size, sha256=excluded.sha256, base_hash=excluded.base_hash,
                        risky=excluded.risky, uploaded_at=excluded.uploaded_at
                    """,
                    (pull_request_id, rel, size, digest_hex, base_hash, risky, now),
                )
                connection.execute(
                    "UPDATE pull_requests SET status='draft', submitted_at='', revision = revision + 1, updated_at = ? WHERE id = ?",
                    (now, pull_request_id),
                )
        return self.get_pull_request_for_token(token, pull_request_id)

    def upload_pull_request_file(self, token: str, pull_request_id: str, raw_path: str, content: bytes) -> dict[str, Any]:
        transfer_dir = self.data_dir / "transfers"
        transfer_dir.mkdir(parents=True, exist_ok=True)
        fd, raw_temp = tempfile.mkstemp(prefix="pr-upload-", dir=transfer_dir)
        os.close(fd)
        temp_path = Path(raw_temp)
        try:
            temp_path.write_bytes(content)
            return self.upload_pull_request_file_from_path(token, pull_request_id, raw_path, temp_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def add_pull_request_deletion(self, token: str, pull_request_id: str, raw_path: str) -> dict[str, Any]:
        with self.lock, self.connect() as connection:
            invite, row = self._pr_for_token(connection, token, pull_request_id)
            if row["status"] not in {"draft", "changes_requested"}:
                raise ForgeTraceError("Only draft or changes-requested pull requests can be changed.", HTTPStatus.CONFLICT, "pull_request_not_editable")
            if not invite["allow_deletes"]:
                raise ForgeTraceError("This invite does not permit deletions.", HTTPStatus.FORBIDDEN, "deletions_not_allowed")
            repository = self.registry.repository_service(row["repository_id"])
            rel = self._validate_path(repository, raw_path)
            base_manifest = json.loads(row["base_manifest_json"])
            base_hash = str(base_manifest.get(rel, {}).get("hash") or "")
            if not base_hash:
                raise ForgeTraceError("Only files present in the pull request baseline can be deleted.", code="deletion_not_in_baseline")
            connection.execute("DELETE FROM pull_request_files WHERE pull_request_id = ? AND path = ?", (pull_request_id, rel))
            staged = self._staged_path(row["repository_id"], pull_request_id, rel)
            if staged.exists():
                staged.unlink()
            now = utc_now()
            connection.execute(
                "INSERT OR REPLACE INTO pull_request_deletions(pull_request_id, path, base_hash, created_at) VALUES (?, ?, ?, ?)",
                (pull_request_id, rel, base_hash, now),
            )
            connection.execute("UPDATE pull_requests SET status='draft', submitted_at='', revision = revision + 1, updated_at = ? WHERE id = ?", (now, pull_request_id))
        return self.get_pull_request_for_token(token, pull_request_id)

    def submit_pull_request(self, token: str, pull_request_id: str) -> dict[str, Any]:
        with self.lock, self.connect() as connection:
            _invite, row = self._pr_for_token(connection, token, pull_request_id)
            if row["status"] != "draft":
                raise ForgeTraceError("Pull request has already been submitted.", HTTPStatus.CONFLICT, "pull_request_already_submitted")
            count = connection.execute(
                "SELECT (SELECT COUNT(*) FROM pull_request_files WHERE pull_request_id = ?) + "
                "(SELECT COUNT(*) FROM pull_request_deletions WHERE pull_request_id = ?) AS count",
                (pull_request_id, pull_request_id),
            ).fetchone()["count"]
            if int(count) == 0:
                raise ForgeTraceError("Add at least one file change before submitting.", code="empty_pull_request")
            now = utc_now()
            connection.execute(
                "UPDATE pull_requests SET status = 'open', submitted_at = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
                (now, now, pull_request_id),
            )
        return self.get_pull_request_for_token(token, pull_request_id)

    def list_pull_requests(self, repository_id: str, status: str = "") -> list[dict[str, Any]]:
        self.registry.get_repository(repository_id)
        with self.connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM pull_requests WHERE repository_id = ? AND status = ? ORDER BY number DESC",
                    (repository_id, status),
                )
            else:
                rows = connection.execute(
                    "SELECT * FROM pull_requests WHERE repository_id = ? ORDER BY number DESC",
                    (repository_id,),
                )
            return [self._public_pr(connection, row) for row in rows]

    def _owner_pr_row(self, connection: sqlite3.Connection, repository_id: str, pull_request_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM pull_requests WHERE id = ? AND repository_id = ?",
            (pull_request_id, repository_id),
        ).fetchone()
        if not row:
            raise ForgeTraceError("Pull request not found.", HTTPStatus.NOT_FOUND, "pull_request_not_found")
        return row

    def _conflicts(self, repository, files: list[dict[str, Any]], deletions: list[dict[str, Any]]) -> list[dict[str, str]]:
        current = repository.manifest(store_objects=False)
        conflicts: list[dict[str, str]] = []
        for item in [*files, *deletions]:
            path = item["path"]
            base_hash = item.get("base_hash") or item.get("baseHash") or ""
            current_hash = str(current.get(path, {}).get("hash") or "")
            if base_hash:
                if current_hash != base_hash:
                    conflicts.append({"path": path, "reason": "changed_since_pull_request_started", "baseHash": base_hash, "currentHash": current_hash})
            elif current_hash:
                conflicts.append({"path": path, "reason": "new_path_now_exists", "baseHash": "", "currentHash": current_hash})
        return conflicts

    def _diff_for_file(self, repository, pull_request_id: str, item: dict[str, Any]) -> dict[str, Any]:
        staged = self._staged_path(repository.repository_id or "", pull_request_id, item["path"])
        result = {
            "path": item["path"], "size": int(item["size"]), "sha256": item["sha256"],
            "baseHash": item["base_hash"], "risky": bool(item["risky"]), "diff": "", "diffTruncated": False,
        }
        if not staged.exists() or staged.stat().st_size > MAX_DIFF_BYTES or not repository.is_text(staged):
            return result
        old_text = ""
        base_hash = item["base_hash"]
        if base_hash:
            object_path = repository.object_path(base_hash)
            if not object_path.exists() or object_path.stat().st_size > MAX_DIFF_BYTES or not repository.is_text(object_path):
                return result
            old_text = object_path.read_text(encoding="utf-8", errors="replace")
        new_text = staged.read_text(encoding="utf-8", errors="replace")
        diff_lines = list(difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile=f"a/{item['path']}", tofile=f"b/{item['path']}", lineterm="",
        ))
        if len(diff_lines) > 2000:
            diff_lines = diff_lines[:2000] + ["... diff truncated by ForgeTrace ..."]
            result["diffTruncated"] = True
        result["diff"] = "\n".join(diff_lines)
        return result

    def get_pull_request(self, repository_id: str, pull_request_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = self._owner_pr_row(connection, repository_id, pull_request_id)
            payload = self._public_pr(connection, row, include_changes=True)
            repository = self.registry.repository_service(repository_id)
            files = self._file_rows(connection, pull_request_id)
            deletions = self._deletion_rows(connection, pull_request_id)
            payload["files"] = [self._diff_for_file(repository, pull_request_id, item) for item in files]
            payload["conflicts"] = self._conflicts(repository, files, deletions)
            if payload["conflicts"] and row["status"] in {"open", "approved", "changes_requested"}:
                payload["effectiveStatus"] = "conflict"
            else:
                payload["effectiveStatus"] = row["status"]
            return payload

    def review_pull_request(
        self, repository_id: str, pull_request_id: str, *, reviewer: str, verdict: str, comment: str = ""
    ) -> dict[str, Any]:
        verdict = str(verdict).strip().lower()
        if verdict not in {"approved", "changes_requested", "comment"}:
            raise ForgeTraceError("Review verdict is invalid.", code="invalid_review_verdict")
        reviewer = self._clean_text(reviewer, label="Reviewer", maximum=120, required=True)
        comment = self._clean_text(comment, label="Review comment", maximum=8000)
        with self.lock, self.connect() as connection:
            row = self._owner_pr_row(connection, repository_id, pull_request_id)
            if row["status"] not in {"open", "approved", "changes_requested"}:
                raise ForgeTraceError("This pull request cannot be reviewed in its current state.", HTTPStatus.CONFLICT, "pull_request_not_reviewable")
            connection.execute(
                "INSERT INTO pull_request_reviews(id, pull_request_id, reviewer, verdict, comment, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("rev_" + uuid.uuid4().hex[:16], pull_request_id, reviewer, verdict, comment, utc_now()),
            )
            if verdict != "comment":
                connection.execute(
                    "UPDATE pull_requests SET status = ?, updated_at = ? WHERE id = ?",
                    (verdict, utc_now(), pull_request_id),
                )
        return self.get_pull_request(repository_id, pull_request_id)

    def merge_pull_request(
        self,
        repository_id: str,
        pull_request_id: str,
        *,
        merged_by: str,
        confirmation: str,
        expected_revision: int,
        allow_risky_files: bool = False,
    ) -> dict[str, Any]:
        merged_by = self._clean_text(merged_by, label="Merger", maximum=120, required=True)
        with self.lock, self.connect() as connection:
            row = self._owner_pr_row(connection, repository_id, pull_request_id)
            if row["status"] != "approved":
                raise ForgeTraceError(
                    "Pull request must be explicitly approved before merging.",
                    HTTPStatus.CONFLICT,
                    "pull_request_not_approved",
                )
            expected_phrase = f"MERGE #{int(row['number'])}"
            if confirmation.strip() != expected_phrase:
                raise ForgeTraceError(f"Type {expected_phrase} to confirm the merge.", code="merge_confirmation_failed")
            if int(expected_revision) != int(row["revision"]):
                raise ForgeTraceError("Pull request changed after review. Refresh before merging.", HTTPStatus.CONFLICT, "pull_request_revision_changed")
            files = self._file_rows(connection, pull_request_id)
            deletions = self._deletion_rows(connection, pull_request_id)
            if any(bool(item["risky"]) for item in files) and not allow_risky_files:
                raise ForgeTraceError(
                    "This pull request contains executable or script-like files. Explicit risky-file approval is required.",
                    HTTPStatus.CONFLICT, "risky_files_require_confirmation",
                )
            repository = self.registry.repository_service(repository_id)
            conflicts = self._conflicts(repository, files, deletions)
            if conflicts:
                connection.execute("UPDATE pull_requests SET status = 'conflict', updated_at = ? WHERE id = ?", (utc_now(), pull_request_id))
                raise ForgeTraceError(
                    "Pull request conflicts with changes made after it was opened.", HTTPStatus.CONFLICT,
                    "pull_request_conflict", {"conflicts": conflicts},
                )
            staged_changes = {
                item["path"]: self._staged_path(repository_id, pull_request_id, item["path"])
                for item in files
            }
            expected_hashes = {
                item["path"]: str(item.get("base_hash") or "")
                for item in [*files, *deletions]
            }
            result = repository.merge_pull_request(
                pull_request_id=pull_request_id,
                pull_request_number=int(row["number"]),
                title=row["title"],
                contributor=row["author_name"],
                merged_by=merged_by,
                staged_changes=staged_changes,
                deletions=[item["path"] for item in deletions],
                expected_base_hashes=expected_hashes,
            )
            now = utc_now()
            connection.execute(
                """
                UPDATE pull_requests SET status='merged', merged_at=?, merged_by=?, merge_commit_id=?, updated_at=?
                WHERE id=?
                """,
                (now, merged_by, result["commit"]["id"], now, pull_request_id),
            )
        payload = self.get_pull_request(repository_id, pull_request_id)
        payload["merge"] = result
        self.purge_closed_quarantine(repository_id, pull_request_id)
        return payload

    def close_pull_request(self, repository_id: str, pull_request_id: str) -> dict[str, Any]:
        with self.lock, self.connect() as connection:
            row = self._owner_pr_row(connection, repository_id, pull_request_id)
            if row["status"] == "merged":
                raise ForgeTraceError("Merged pull requests cannot be closed.", HTTPStatus.CONFLICT, "pull_request_already_merged")
            now = utc_now()
            connection.execute("UPDATE pull_requests SET status='closed', closed_at=?, updated_at=? WHERE id=?", (now, now, pull_request_id))
        payload = self.get_pull_request(repository_id, pull_request_id)
        self.purge_closed_quarantine(repository_id, pull_request_id)
        return payload

    def cleanup_retention(self) -> dict[str, int]:
        """Remove safe expired transfer files and terminal pull-request quarantine."""
        removed_transfers = 0
        removed_quarantine = 0
        cutoff = datetime.now(timezone.utc).timestamp() - 24 * 3600
        transfer_dir = self.data_dir / "transfers"
        if transfer_dir.is_dir():
            for item in transfer_dir.iterdir():
                try:
                    if item.stat().st_mtime < cutoff:
                        item.unlink(missing_ok=True)
                        removed_transfers += 1
                except OSError:
                    pass
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT repository_id, id FROM pull_requests WHERE status IN ('merged','closed')"
                ).fetchall()
            for row in rows:
                root = self.quarantine_dir / row["repository_id"] / row["id"]
                if root.exists():
                    shutil.rmtree(root, ignore_errors=True)
                    removed_quarantine += 1
        except sqlite3.Error:
            pass
        return {"transfers": removed_transfers, "quarantine": removed_quarantine}

    def storage_metrics(self) -> dict[str, Any]:
        quarantine_bytes = 0
        quarantine_files = 0
        for path in self.quarantine_dir.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    quarantine_bytes += path.stat().st_size
                    quarantine_files += 1
            except OSError:
                continue
        with self.connect() as connection:
            statuses = {
                str(row["status"]): int(row["count"])
                for row in connection.execute("SELECT status, COUNT(*) AS count FROM pull_requests GROUP BY status")
            }
        return {
            "quarantineBytes": quarantine_bytes,
            "quarantineFiles": quarantine_files,
            "pullRequestsByStatus": statuses,
            "retention": {"closedAndMergedQuarantine": "purged immediately", "staleTransfersHours": 24},
        }

    def purge_closed_quarantine(self, repository_id: str, pull_request_id: str) -> None:
        root = self.quarantine_dir / repository_id / pull_request_id
        if root.exists():
            shutil.rmtree(root)
