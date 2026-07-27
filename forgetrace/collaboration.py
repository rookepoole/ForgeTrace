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

from .conflict_resolution import ConflictResolutionStore
from .errors import ForgeTraceError
from .registry import RepositoryRegistry
from .review_conversations import ReviewConversationStore
from .security_events import SecurityEventError, SecurityEventLedger
from .utils import utc_now

COLLABORATION_SCHEMA_VERSION = 6
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

    def __init__(self, registry: RepositoryRegistry, security_events: SecurityEventLedger | None = None) -> None:
        self.registry = registry
        self.security_events = security_events
        self.data_dir = registry.data_dir / "collaboration"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "collaboration.sqlite3"
        self.quarantine_dir = self.data_dir / "quarantine"
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._migrate()
        self.review_conversations = ReviewConversationStore(
            registry=self.registry,
            db_path=self.db_path,
            data_dir=self.data_dir,
            lock=self.lock,
            invite_resolver=self._invite_row_for_token,
            pr_for_token=self._pr_for_token,
            owner_pr_resolver=self._owner_pr_row,
            staged_path=self._staged_path,
            audit=self._audit,
            token_fingerprint=self._token_fingerprint,
        )
        self.review_conversations.backfill_current_revisions()
        self.conflict_resolutions = ConflictResolutionStore(
            registry=self.registry,
            db_path=self.db_path,
            data_dir=self.data_dir,
            lock=self.lock,
            review_store=self.review_conversations,
            owner_pr_resolver=self._owner_pr_row,
            file_rows=self._file_rows,
            deletion_rows=self._deletion_rows,
            audit=self._audit,
        )
        self.cleanup_retention()

    def _audit(
        self,
        *,
        required: bool = False,
        category: str = "collaboration",
        action: str,
        outcome: str,
        severity: str = "info",
        repository_id: str = "",
        actor: str = "",
        subject_id: str = "",
        details: dict[str, Any] | None = None,
        surface: str = "gateway",
    ) -> dict[str, Any] | None:
        if self.security_events is None:
            return None
        try:
            if required:
                self.security_events.assert_writable()
            return self.security_events.append(
                category=category,
                action=action,
                outcome=outcome,
                severity=severity,
                surface=surface,
                repository_id=repository_id,
                actor=actor,
                subject_id=subject_id,
                details=details or {},
            )
        except SecurityEventError as exc:
            if required:
                raise ForgeTraceError(
                    "The security event ledger is unavailable or failed integrity verification. The protected collaboration action was blocked.",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "security_event_ledger_unavailable",
                    {"reason": str(exc)},
                ) from exc
            return None

    @staticmethod
    def _token_fingerprint(token: str) -> str:
        return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:16]

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
                    allow_project_participation INTEGER NOT NULL DEFAULT 0 CHECK(allow_project_participation IN (0,1)),
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
            if "allow_project_participation" not in invite_columns:
                connection.execute(
                    "ALTER TABLE collaboration_invites "
                    "ADD COLUMN allow_project_participation INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(allow_project_participation IN (0,1))"
                )
            ReviewConversationStore.migrate_schema(connection)
            ConflictResolutionStore.migrate_schema(connection)
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
            self._audit(
                action="invite_required",
                outcome="denied",
                severity="warning",
                details={"inviteFingerprint": self._token_fingerprint(token) if token else ""},
            )
            raise ForgeTraceError("A valid collaboration invite is required.", HTTPStatus.UNAUTHORIZED, "invite_required")
        row = connection.execute(
            "SELECT * FROM collaboration_invites WHERE token_hash = ?",
            (self._hash_token(token),),
        ).fetchone()
        if not row:
            self._audit(
                action="invalid_invite",
                outcome="denied",
                severity="warning",
                details={"inviteFingerprint": self._token_fingerprint(token)},
            )
            raise ForgeTraceError("Collaboration invite is invalid.", HTTPStatus.UNAUTHORIZED, "invalid_invite")
        if row["revoked"]:
            self._audit(
                action="invite_revoked_access",
                outcome="denied",
                severity="warning",
                repository_id=row["repository_id"],
                subject_id=row["id"],
                details={"inviteId": row["id"], "inviteFingerprint": self._token_fingerprint(token)},
            )
            raise ForgeTraceError("Collaboration invite has been revoked.", HTTPStatus.FORBIDDEN, "invite_revoked")
        if _parse_utc(row["expires_at"]) <= datetime.now(timezone.utc):
            self._audit(
                action="invite_expired_access",
                outcome="denied",
                severity="warning",
                repository_id=row["repository_id"],
                subject_id=row["id"],
                details={"inviteId": row["id"], "inviteFingerprint": self._token_fingerprint(token)},
            )
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
        allow_project_participation: bool = False,
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
        self._audit(
            required=True,
            action="invite_create_authorized",
            outcome="authorized",
            severity="warning" if allow_sensitive_source else "info",
            repository_id=repository_id,
            surface="owner",
            details={
                "expiresInHours": expires_in_hours,
                "maxUses": max_uses,
                "maxFileBytes": max_file_bytes,
                "maxTotalBytes": max_total_bytes,
                "allowDeletes": bool(allow_deletes),
                "allowSourceDownload": bool(allow_source_download),
                "allowSensitiveSource": bool(allow_sensitive_source),
                "allowProjectParticipation": bool(allow_project_participation),
            },
        )
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
                    allow_source_download, allow_sensitive_source, allow_project_participation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invite_id, repository_id, self._hash_token(token),
                    self._clean_text(label, label="Invite label", maximum=120),
                    now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    max_uses, max_file_bytes, max_total_bytes, int(bool(allow_deletes)),
                    int(bool(allow_source_download)), int(bool(allow_sensitive_source)),
                    int(bool(allow_project_participation)),
                ),
            )
        result = {
            "invite": self.get_invite(invite_id),
            "token": token,
            "sharePath": f"/contribute.html#{token}",
            "security": "The token is shown once and stored only as a SHA-256 hash.",
        }
        self._audit(
            action="invite_created",
            outcome="success",
            severity="warning" if allow_sensitive_source else "info",
            repository_id=repository_id,
            surface="owner",
            subject_id=invite_id,
            details={
                "inviteId": invite_id,
                "inviteFingerprint": self._token_fingerprint(token),
                "expiresAt": result["invite"]["expiresAt"],
                "allowSensitiveSource": bool(allow_sensitive_source),
                "allowProjectParticipation": bool(allow_project_participation),
            },
        )
        return result

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
            "allowProjectParticipation": bool(row["allow_project_participation"]),
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
        result = self.get_invite(invite_id)
        self._audit(
            action="invite_revoked",
            outcome="success",
            severity="warning",
            repository_id=repository_id,
            surface="owner",
            subject_id=invite_id,
            details={"inviteId": invite_id},
        )
        return result

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
                    "projectParticipation": bool(invite["allow_project_participation"]),
                    "protectedPaths": sorted(PROTECTED_SEGMENTS),
                },
            }


    def project_participant(self, token: str) -> dict[str, str]:
        """Resolve an explicitly permissioned project-layer contributor.

        Ordinary source-sharing or pull-request invitations do not imply project
        participation. The raw token is never returned or persisted.
        """
        with self.connect() as connection:
            invite = self._invite_row_for_token(connection, token)
            if not bool(invite["allow_project_participation"]):
                self._audit(
                    action="project_participation_denied",
                    outcome="denied",
                    severity="warning",
                    repository_id=invite["repository_id"],
                    subject_id=invite["id"],
                    details={
                        "inviteId": invite["id"],
                        "inviteFingerprint": self._token_fingerprint(token),
                    },
                )
                raise ForgeTraceError(
                    "This invitation does not permit issue or discussion participation.",
                    HTTPStatus.FORBIDDEN,
                    "project_participation_not_allowed",
                )
            return {
                "repositoryId": str(invite["repository_id"]),
                "inviteId": str(invite["id"]),
                "inviteFingerprint": self._token_fingerprint(token),
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
            invite_id = invite["id"]
            include_sensitive = bool(invite["allow_sensitive_source"])
        if include_sensitive:
            self._audit(
                required=True,
                category="export",
                action="sensitive_source_export_authorized",
                outcome="authorized",
                severity="warning",
                repository_id=repository_id,
                subject_id=invite_id,
                details={"inviteId": invite_id, "inviteFingerprint": self._token_fingerprint(token)},
            )
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
            self._audit(
                category="export",
                action="collaboration_source_export_generated",
                outcome="success",
                severity="warning" if include_sensitive else "info",
                repository_id=repository_id,
                subject_id=invite_id,
                details={
                    "inviteId": invite_id,
                    "inviteFingerprint": self._token_fingerprint(token),
                    "includeSensitive": include_sensitive,
                    "archiveBytes": archive_bytes,
                },
            )
            return archive_path, (safe or "repository") + "-source.zip"
        except Exception as exc:
            self._audit(
                category="export",
                action="collaboration_source_export_failed",
                outcome="failure",
                severity="error",
                repository_id=repository_id,
                subject_id=invite_id,
                details={
                    "inviteId": invite_id,
                    "inviteFingerprint": self._token_fingerprint(token),
                    "includeSensitive": include_sensitive,
                    "errorType": type(exc).__name__,
                },
            )
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
        dirty = latest is None or any(repository.diff_manifests(latest["manifest"], current).values())
        if dirty and not repository.access_policy(state)["writable"]:
            material = json.dumps(current, sort_keys=True, separators=(",", ":")).encode("utf-8")
            return "readonly-" + hashlib.sha256(material).hexdigest()[:12], current
        if dirty:
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
        result = self.get_pull_request_for_token(token, pull_request_id)
        self._audit(
            action="pull_request_created",
            outcome="success",
            repository_id=result["repositoryId"],
            actor=author_name,
            subject_id=pull_request_id,
            details={
                "pullRequestId": pull_request_id,
                "number": result["number"],
                "inviteId": invite["id"],
                "inviteFingerprint": self._token_fingerprint(token),
            },
        )
        return result

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
            "reviewConversation": self.review_conversations.summary(
                connection, row["id"], int(row["revision"])
            ),
            "submittedRevisions": self.review_conversations.revisions(connection, row["id"]),
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
            submitted_revision = int(row["revision"]) + 1
            files = self._file_rows(connection, pull_request_id)
            deletions = self._deletion_rows(connection, pull_request_id)
            self.review_conversations.capture_submission(
                connection,
                row,
                revision=submitted_revision,
                files=files,
                deletions=deletions,
                strict=True,
                submitted_at=now,
            )
            connection.execute(
                "UPDATE pull_requests SET status = 'open', submitted_at = ?, updated_at = ?, revision = ? WHERE id = ?",
                (now, now, submitted_revision, pull_request_id),
            )
        result = self.get_pull_request_for_token(token, pull_request_id)
        self._audit(
            action="pull_request_submitted",
            outcome="success",
            repository_id=result["repositoryId"],
            actor=result.get("authorName", ""),
            subject_id=pull_request_id,
            details={
                "pullRequestId": pull_request_id,
                "number": result["number"],
                "revision": result["revision"],
                "changeCount": result["changeCount"],
                "inviteFingerprint": self._token_fingerprint(token),
            },
        )
        return result

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

    def _conflicts(self, repository, files: list[dict[str, Any]], deletions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        current = repository.manifest(store_objects=False, persist_index=False)
        return ConflictResolutionStore._conflicts_for_manifest(current, files, deletions)

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
            if object_path.exists():
                if object_path.stat().st_size > MAX_DIFF_BYTES or not repository.is_text(object_path):
                    return result
                old_text = object_path.read_text(encoding="utf-8", errors="replace")
            else:
                try:
                    _rel, live_path = repository.resolve_path(item["path"])
                    live_manifest = repository.manifest(store_objects=False)
                    if str(live_manifest.get(item["path"], {}).get("hash") or "") != base_hash:
                        return result
                    if not live_path.exists() or live_path.stat().st_size > MAX_DIFF_BYTES or not repository.is_text(live_path):
                        return result
                    live_bytes = live_path.read_bytes()
                    if len(live_bytes) > MAX_DIFF_BYTES or hashlib.sha256(live_bytes).hexdigest() != base_hash:
                        return result
                    old_text = live_bytes.decode("utf-8", errors="replace")
                except (ForgeTraceError, OSError):
                    return result
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
            payload["conflictResolution"] = self.conflict_resolutions.summary_owner(
                repository_id, pull_request_id
            )
            if (
                payload["conflicts"]
                and not bool(payload["conflictResolution"].get("complete"))
                and row["status"] in {"open", "approved", "changes_requested", "conflict"}
            ):
                payload["effectiveStatus"] = "conflict"
            else:
                payload["effectiveStatus"] = row["status"]
            return payload

    def review_pull_request(
        self,
        repository_id: str,
        pull_request_id: str,
        *,
        reviewer: str,
        verdict: str,
        comment: str = "",
        expected_revision: int | None = None,
        request_id: str = "",
    ) -> dict[str, Any]:
        verdict = str(verdict).strip().lower()
        if verdict not in {"approved", "changes_requested", "comment"}:
            raise ForgeTraceError("Review verdict is invalid.", code="invalid_review_verdict")
        reviewer = self._clean_text(reviewer, label="Reviewer", maximum=120, required=True)
        comment = self._clean_text(comment, label="Review comment", maximum=8000)
        with self.lock, self.connect() as connection:
            row = self._owner_pr_row(connection, repository_id, pull_request_id)
            if row["status"] not in {"open", "approved", "changes_requested", "conflict"}:
                raise ForgeTraceError("This pull request cannot be reviewed in its current state.", HTTPStatus.CONFLICT, "pull_request_not_reviewable")
            if expected_revision is not None and int(expected_revision) != int(row["revision"]):
                raise ForgeTraceError(
                    "Pull request changed after it was loaded. Refresh before reviewing.",
                    HTTPStatus.CONFLICT,
                    "pull_request_revision_changed",
                    {"currentRevision": int(row["revision"])},
                )
            if verdict == "approved":
                unresolved = self.review_conversations.unresolved_current_count(
                    connection, pull_request_id, int(row["revision"])
                )
                if unresolved:
                    raise ForgeTraceError(
                        "Resolve every thread on the current submitted revision before approval.",
                        HTTPStatus.CONFLICT,
                        "unresolved_review_threads",
                        {"unresolvedThreadCount": unresolved},
                    )
                repository = self.registry.repository_service(repository_id)
                files = self._file_rows(connection, pull_request_id)
                deletions = self._deletion_rows(connection, pull_request_id)
                with repository.lock:
                    self.conflict_resolutions.require_resolutions_for_approval_locked(
                        connection, pr=row, repository=repository, files=files, deletions=deletions
                    )
            now = utc_now()
            connection.execute(
                """
                INSERT INTO pull_request_reviews(
                    id, pull_request_id, reviewer, verdict, comment, created_at, revision, request_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "rev_" + uuid.uuid4().hex[:16], pull_request_id, reviewer, verdict, comment,
                    now, int(row["revision"]), str(request_id or "")[:120],
                ),
            )
            if verdict != "comment":
                connection.execute(
                    "UPDATE pull_requests SET status = ?, updated_at = ? WHERE id = ?",
                    (verdict, now, pull_request_id),
                )
        result = self.get_pull_request(repository_id, pull_request_id)
        self._audit(
            action="pull_request_reviewed",
            outcome="success",
            repository_id=repository_id,
            actor=reviewer,
            subject_id=pull_request_id,
            surface="owner",
            details={
                "pullRequestId": pull_request_id,
                "verdict": verdict,
                "revision": result["revision"],
                "requestId": str(request_id or "")[:120],
            },
        )
        return result

    def merge_pull_request(
        self,
        repository_id: str,
        pull_request_id: str,
        *,
        merged_by: str,
        confirmation: str,
        expected_revision: int,
        allow_risky_files: bool = False,
        request_id: str = "",
    ) -> dict[str, Any]:
        merged_by = self._clean_text(merged_by, label="Merger", maximum=120, required=True)
        result: dict[str, Any]
        resolution_draft_ids: list[str] = []
        resolved_conflict_count = 0
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
                raise ForgeTraceError(
                    "Pull request changed after review. Refresh before merging.",
                    HTTPStatus.CONFLICT,
                    "pull_request_revision_changed",
                )
            files = self._file_rows(connection, pull_request_id)
            deletions = self._deletion_rows(connection, pull_request_id)
            if any(bool(item["risky"]) for item in files) and not allow_risky_files:
                raise ForgeTraceError(
                    "This pull request contains executable or script-like files. Explicit risky-file approval is required.",
                    HTTPStatus.CONFLICT,
                    "risky_files_require_confirmation",
                )
            repository = self.registry.repository_service(repository_id)
            try:
                with repository.lock:
                    repository.require_writable("pull request merge")
                    try:
                        plan = self.conflict_resolutions.build_merge_plan_locked(
                            connection, pr=row, repository=repository, files=files, deletions=deletions
                        )
                    except ForgeTraceError as exc:
                        if exc.code == "conflict_resolution_required":
                            details = dict(exc.details or {})
                            conflicts = list(details.get("conflicts") or [])
                            connection.execute(
                                "UPDATE pull_requests SET status='conflict', updated_at=? WHERE id=?",
                                (utc_now(), pull_request_id),
                            )
                            self._audit(
                                action="pull_request_merge_conflict",
                                outcome="denied",
                                severity="warning",
                                repository_id=repository_id,
                                actor=merged_by,
                                subject_id=pull_request_id,
                                surface="owner",
                                details={
                                    "pullRequestId": pull_request_id,
                                    "conflictCount": len(conflicts),
                                    "resolutionRequired": True,
                                    "requestId": str(request_id or "")[:120],
                                },
                            )
                            raise ForgeTraceError(
                                "Pull request conflicts with changes made after it was opened. Prepare and confirm quarantine-side resolutions before approval and merge.",
                                HTTPStatus.CONFLICT,
                                "pull_request_conflict",
                                details,
                            ) from exc
                        raise
                    resolution_draft_ids = list(plan["resolutionDraftIds"])
                    resolved_conflict_count = int(plan["resolvedConflictCount"])
                    self._audit(
                        required=True,
                        action="pull_request_merge_authorized",
                        outcome="authorized",
                        severity="warning" if any(bool(item["risky"]) for item in files) else "info",
                        repository_id=repository_id,
                        actor=merged_by,
                        subject_id=pull_request_id,
                        surface="owner",
                        details={
                            "pullRequestId": pull_request_id,
                            "number": int(row["number"]),
                            "revision": int(row["revision"]),
                            "fileCount": len(files),
                            "deletionCount": len(deletions),
                            "allowRiskyFiles": bool(allow_risky_files),
                            "resolvedConflictCount": resolved_conflict_count,
                            "resolutionDraftIds": resolution_draft_ids,
                            "repositoryDigest": plan["binding"]["repositoryDigest"],
                            "requestId": str(request_id or "")[:120],
                        },
                    )
                    result = repository.merge_pull_request(
                        pull_request_id=pull_request_id,
                        pull_request_number=int(row["number"]),
                        title=row["title"],
                        contributor=row["author_name"],
                        merged_by=merged_by,
                        staged_changes=plan["stagedChanges"],
                        deletions=plan["deletions"],
                        expected_base_hashes=plan["expectedHashes"],
                    )
                    now = utc_now()
                    self.conflict_resolutions.mark_applied(
                        connection, resolution_draft_ids, actor_name=merged_by, request_id=request_id
                    )
                    connection.execute(
                        """
                        UPDATE pull_requests SET status='merged', merged_at=?, merged_by=?, merge_commit_id=?, updated_at=?
                        WHERE id=?
                        """,
                        (now, merged_by, result["commit"]["id"], now, pull_request_id),
                    )
            except Exception as exc:
                if not isinstance(exc, ForgeTraceError) or exc.code not in {
                    "pull_request_conflict", "pull_request_not_approved", "unresolved_review_threads",
                    "conflict_resolution_required", "repository_read_only",
                }:
                    self._audit(
                        action="pull_request_merge_failed",
                        outcome="failure",
                        severity="error",
                        repository_id=repository_id,
                        actor=merged_by,
                        subject_id=pull_request_id,
                        surface="owner",
                        details={
                            "pullRequestId": pull_request_id,
                            "number": int(row["number"]),
                            "errorType": type(exc).__name__,
                            "requestId": str(request_id or "")[:120],
                        },
                    )
                raise
        payload = self.get_pull_request(repository_id, pull_request_id)
        payload["merge"] = result
        self._audit(
            action="pull_request_merged",
            outcome="success",
            severity="warning" if allow_risky_files else "info",
            repository_id=repository_id,
            actor=merged_by,
            subject_id=pull_request_id,
            surface="owner",
            details={
                "pullRequestId": pull_request_id,
                "number": payload["number"],
                "mergeCommitId": payload["mergeCommitId"],
                "allowRiskyFiles": bool(allow_risky_files),
                "resolvedConflictCount": resolved_conflict_count,
                "resolutionDraftIds": resolution_draft_ids,
                "requestId": str(request_id or "")[:120],
            },
        )
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
        self._audit(
            action="pull_request_closed",
            outcome="success",
            severity="warning",
            repository_id=repository_id,
            subject_id=pull_request_id,
            surface="owner",
            details={"pullRequestId": pull_request_id, "number": payload["number"]},
        )
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
        review_cleanup = self.review_conversations.cleanup_retention()
        resolution_cleanup = self.conflict_resolutions.cleanup_retention()
        return {
            "transfers": removed_transfers,
            "quarantine": removed_quarantine,
            **review_cleanup,
            **resolution_cleanup,
        }

    def storage_metrics(self, *, max_files: int | None = None) -> dict[str, Any]:
        quarantine_bytes = 0
        quarantine_files = 0
        complete = True
        limit = None if max_files is None else max(1, min(int(max_files), 1_000_000))
        for path in self.quarantine_dir.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    if limit is not None and quarantine_files >= limit:
                        complete = False
                        break
                    quarantine_bytes += path.stat().st_size
                    quarantine_files += 1
            except OSError:
                continue
        with self.connect() as connection:
            statuses = {
                str(row["status"]): int(row["count"])
                for row in connection.execute("SELECT status, COUNT(*) AS count FROM pull_requests GROUP BY status")
            }
        review_metrics = self.review_conversations.storage_metrics(max_files=max_files)
        resolution_metrics = self.conflict_resolutions.storage_metrics(max_files=max_files)
        return {
            "quarantineBytes": quarantine_bytes,
            "quarantineFiles": quarantine_files,
            "pullRequestsByStatus": statuses,
            "reviewConversations": review_metrics,
            "conflictResolutions": resolution_metrics,
            "retention": {
                "closedAndMergedQuarantine": "purged immediately",
                "staleTransfersHours": 24,
                "terminalReviewDays": review_metrics["terminalRetentionDays"],
                "terminalConflictResolutionDays": resolution_metrics["terminalRetentionDays"],
            },
            "complete": complete and review_metrics.get("complete", True) and resolution_metrics.get("complete", True),
        }

    def health_assessment(
        self,
        *,
        max_revisions: int = 100,
        max_evidence_files: int = 1000,
        max_drafts: int = 200,
        max_storage_files: int = 5000,
    ) -> dict[str, Any]:
        """Compose collaboration database, retention, review, and conflict health."""

        with self.lock, self.connect() as connection:
            integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            sqlite_integrity = "ok" if integrity_rows == ["ok"] else "; ".join(integrity_rows)
            foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
            schema_row = connection.execute(
                "SELECT value FROM collaboration_meta WHERE key='schema_version'"
            ).fetchone()
            try:
                schema_version = int(schema_row[0]) if schema_row else 0
            except (TypeError, ValueError):
                schema_version = 0
            pull_requests = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM pull_requests GROUP BY status"
                )
            }
            known_prs = {
                (str(row["repository_id"]), str(row["id"]))
                for row in connection.execute("SELECT repository_id,id FROM pull_requests")
            }

        orphan_quarantine = 0
        scanned_quarantine = 0
        orphan_scan_complete = True
        bounded_dirs = max(1, min(int(max_drafts), 5000)) * 2
        for path in self.quarantine_dir.glob("*/*"):
            if scanned_quarantine >= bounded_dirs:
                orphan_scan_complete = False
                break
            scanned_quarantine += 1
            try:
                key = (path.parent.name, path.name)
                if path.is_dir() and key not in known_prs:
                    orphan_quarantine += 1
            except OSError:
                continue

        review = self.review_conversations.health_assessment(
            max_revisions=max_revisions,
            max_files=max_evidence_files,
        )
        resolutions = self.conflict_resolutions.health_assessment(max_drafts=max_drafts)
        storage = self.storage_metrics(max_files=max_storage_files)
        return {
            "schemaVersion": schema_version,
            "expectedSchemaVersion": COLLABORATION_SCHEMA_VERSION,
            "sqliteIntegrity": sqlite_integrity,
            "foreignKeyIssueCount": len(foreign_keys),
            "foreignKeyIssues": foreign_keys[:100],
            "pullRequestsByStatus": pull_requests,
            "orphanQuarantineDirectoryCount": orphan_quarantine,
            "orphanQuarantineScanComplete": orphan_scan_complete,
            "reviewConversations": review,
            "conflictResolutions": resolutions,
            "storage": storage,
        }

    def purge_closed_quarantine(self, repository_id: str, pull_request_id: str) -> None:
        root = self.quarantine_dir / repository_id / pull_request_id
        if root.exists():
            shutil.rmtree(root)
