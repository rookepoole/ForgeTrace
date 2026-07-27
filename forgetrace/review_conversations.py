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

DEFAULT_THREAD_PAGE = 50
MAX_THREAD_PAGE = 100
DEFAULT_COMMENT_PAGE = 50
MAX_COMMENT_PAGE = 100
MAX_REVIEW_BODY = 8000
MAX_LINE_SPAN = 200
MAX_CONTEXT_BYTES = 1024 * 1024
MAX_THREADS_PER_PULL_REQUEST = 500
MAX_COMMENTS_PER_THREAD = 500
MAX_COMMENTS_PER_PULL_REQUEST = 5000
REVIEW_RETENTION_DAYS = 180
PROTECTED_SEGMENTS = {".forgetrace", ".git"}
REVIEWABLE_STATUSES = {"open", "approved", "changes_requested", "conflict"}
TERMINAL_STATUSES = {"merged", "closed"}


class ReviewConversationStore:
    """Revision-bound review conversations stored outside live repositories.

    The store shares ForgeTrace's collaboration SQLite database but keeps immutable
    submitted-revision bytes under application data. It never writes repository
    content and never renders submitted content as active HTML/SVG/JavaScript.
    """

    def __init__(
        self,
        *,
        registry,
        db_path: Path,
        data_dir: Path,
        lock,
        invite_resolver: Callable[[sqlite3.Connection, str], sqlite3.Row],
        pr_for_token: Callable[[sqlite3.Connection, str, str], tuple[sqlite3.Row, sqlite3.Row]],
        owner_pr_resolver: Callable[[sqlite3.Connection, str, str], sqlite3.Row],
        staged_path: Callable[[str, str, str], Path],
        audit: Callable[..., dict[str, Any] | None],
        token_fingerprint: Callable[[str], str],
    ) -> None:
        self.registry = registry
        self.db_path = db_path
        self.data_dir = data_dir
        self.lock = lock
        self.invite_resolver = invite_resolver
        self.pr_for_token = pr_for_token
        self.owner_pr_resolver = owner_pr_resolver
        self.staged_path = staged_path
        self.audit = audit
        self.token_fingerprint = token_fingerprint
        self.revisions_dir = data_dir / "review-revisions"
        self.revisions_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def migrate_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS pull_request_revisions (
                pull_request_id TEXT NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
                repository_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                snapshot_state TEXT NOT NULL CHECK(snapshot_state IN ('complete','metadata_only')),
                file_count INTEGER NOT NULL,
                deletion_count INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL,
                submitted_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(pull_request_id, revision)
            );
            CREATE INDEX IF NOT EXISTS idx_pull_request_revisions_repository
                ON pull_request_revisions(repository_id, pull_request_id, revision DESC);

            CREATE TABLE IF NOT EXISTS review_threads (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                repository_id TEXT NOT NULL,
                pull_request_id TEXT NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
                submitted_revision INTEGER NOT NULL,
                path TEXT NOT NULL,
                start_line INTEGER,
                end_line INTEGER,
                created_by_role TEXT NOT NULL CHECK(created_by_role IN ('owner','contributor')),
                created_by_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_request_id TEXT NOT NULL DEFAULT '',
                resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0,1)),
                resolved_by_name TEXT NOT NULL DEFAULT '',
                resolved_at TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                FOREIGN KEY(pull_request_id, submitted_revision)
                    REFERENCES pull_request_revisions(pull_request_id, revision) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_review_threads_pr_sequence
                ON review_threads(pull_request_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_review_threads_pr_revision
                ON review_threads(pull_request_id, submitted_revision, resolved, sequence);

            CREATE TABLE IF NOT EXISTS review_comments (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                thread_id TEXT NOT NULL REFERENCES review_threads(id) ON DELETE CASCADE,
                repository_id TEXT NOT NULL,
                pull_request_id TEXT NOT NULL,
                submitted_revision INTEGER NOT NULL,
                author_role TEXT NOT NULL CHECK(author_role IN ('owner','contributor')),
                author_name TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                request_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_review_comments_thread_sequence
                ON review_comments(thread_id, sequence);

            CREATE TABLE IF NOT EXISTS review_thread_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                thread_id TEXT NOT NULL REFERENCES review_threads(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL CHECK(event_type IN ('created','resolved','reopened')),
                actor_role TEXT NOT NULL CHECK(actor_role IN ('owner','contributor')),
                actor_name TEXT NOT NULL,
                thread_version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                request_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_review_thread_events_thread_sequence
                ON review_thread_events(thread_id, sequence);
            """
        )
        review_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(pull_request_reviews)")
        }
        if "revision" not in review_columns:
            connection.execute(
                "ALTER TABLE pull_request_reviews ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
            )
        if "request_id" not in review_columns:
            connection.execute(
                "ALTER TABLE pull_request_reviews ADD COLUMN request_id TEXT NOT NULL DEFAULT ''"
            )

    def connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _clean_text(value: Any, *, label: str, maximum: int, required: bool = False) -> str:
        result = str(value or "").strip()
        if required and not result:
            raise ForgeTraceError(f"{label} is required.", code=f"{label.lower().replace(' ', '_')}_required")
        if len(result) > maximum:
            raise ForgeTraceError(f"{label} may not exceed {maximum} characters.", code="value_too_long")
        return result

    @staticmethod
    def _whole_number(value: Any, *, label: str, minimum: int = 0, maximum: int | None = None) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ForgeTraceError(f"{label} must be a whole number.", code="invalid_whole_number") from exc
        if result < minimum or (maximum is not None and result > maximum):
            bounds = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
            raise ForgeTraceError(f"{label} must be {bounds}.", code="whole_number_out_of_range")
        return result

    @staticmethod
    def _canonical_json(payload: Any) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _revision_root(self, repository_id: str, pull_request_id: str, revision: int) -> Path:
        base = self.revisions_dir.resolve()
        target = (base / repository_id / pull_request_id / f"rev-{revision:010d}").resolve()
        if target != base and base not in target.parents:
            raise ForgeTraceError("Review revision path escapes application data.", HTTPStatus.FORBIDDEN, "review_revision_path_escape")
        return target

    def _revision_file(self, repository_id: str, pull_request_id: str, revision: int, rel: str) -> Path:
        root = self._revision_root(repository_id, pull_request_id, revision)
        target = (root / "files" / rel).resolve()
        files_root = (root / "files").resolve()
        if target != files_root and files_root not in target.parents:
            raise ForgeTraceError("Review file path escapes revision storage.", HTTPStatus.FORBIDDEN, "review_revision_path_escape")
        return target

    def _revision_base_file(self, repository_id: str, pull_request_id: str, revision: int, rel: str) -> Path:
        root = self._revision_root(repository_id, pull_request_id, revision)
        target = (root / "base-files" / rel).resolve()
        files_root = (root / "base-files").resolve()
        if target != files_root and files_root not in target.parents:
            raise ForgeTraceError("Review base-file path escapes revision storage.", HTTPStatus.FORBIDDEN, "review_revision_path_escape")
        return target

    def _validated_manifest_path(self, repository_id: str, raw_path: str) -> str:
        repository = self.registry.repository_service(repository_id)
        rel = repository.normalize_rel(raw_path)
        segments = {segment.casefold() for segment in Path(rel).parts}
        if segments & PROTECTED_SEGMENTS:
            raise ForgeTraceError(
                "Review conversations cannot target .git or .forgetrace metadata.",
                HTTPStatus.FORBIDDEN,
                "protected_review_path",
            )
        return rel

    @staticmethod
    def _line_metadata(repository, path: Path, size: int) -> tuple[bool, int]:
        if size > MAX_CONTEXT_BYTES or not repository.is_text(path):
            return False, 0
        data = path.read_bytes()
        if len(data) != size or b"\x00" in data:
            return False, 0
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return False, 0
        if not text:
            return True, 0
        return True, len(text.splitlines())

    def _copy_verified(self, source: Path, destination: Path, *, expected_size: int, expected_hash: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as input_handle, destination.open("wb") as output_handle:
            while True:
                chunk = input_handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
                output_handle.write(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        if size != expected_size or digest.hexdigest() != expected_hash:
            destination.unlink(missing_ok=True)
            raise ForgeTraceError(
                "Submitted revision bytes changed while ForgeTrace was preserving review context.",
                HTTPStatus.CONFLICT,
                "review_revision_bytes_changed",
                {"expectedBytes": expected_size, "actualBytes": size},
            )

    def _capture_base_evidence(
        self,
        repository,
        *,
        destination: Path,
        rel: str,
        base_hash: str,
        expected_size: int,
    ) -> tuple[bool, int, bool, int]:
        if not base_hash:
            return True, 0, True, 0
        candidates: list[Path] = []
        try:
            candidates.append(repository.object_path(base_hash))
        except Exception:
            pass
        for source in candidates:
            if not source.is_file() or source.is_symlink():
                continue
            try:
                size = expected_size or source.stat().st_size
                self._copy_verified(source, destination, expected_size=size, expected_hash=base_hash)
                text, lines = self._line_metadata(repository, destination, size)
                return True, size, text, lines
            except (ForgeTraceError, OSError):
                destination.unlink(missing_ok=True)
        try:
            with repository.lock:
                live_manifest = repository.manifest(store_objects=False, persist_index=False)
                entry = live_manifest.get(rel) or {}
                if str(entry.get("hash") or "") != base_hash:
                    return False, expected_size, False, 0
                _normalized, source = repository.resolve_path(rel)
                size = int(entry.get("size") or expected_size or 0)
                self._copy_verified(source, destination, expected_size=size, expected_hash=base_hash)
                text, lines = self._line_metadata(repository, destination, size)
                return True, size, text, lines
        except (ForgeTraceError, OSError):
            destination.unlink(missing_ok=True)
            return False, expected_size, False, 0

    def capture_submission(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | dict[str, Any],
        *,
        revision: int,
        files: list[dict[str, Any]],
        deletions: list[dict[str, Any]],
        strict: bool = True,
        submitted_at: str = "",
    ) -> dict[str, Any]:
        pull_request_id = str(row["id"])
        repository_id = str(row["repository_id"])
        revision = self._whole_number(revision, label="Submitted revision", minimum=1)
        existing = connection.execute(
            "SELECT * FROM pull_request_revisions WHERE pull_request_id=? AND revision=?",
            (pull_request_id, revision),
        ).fetchone()
        if existing:
            return self._public_revision(existing)

        final_root = self._revision_root(repository_id, pull_request_id, revision)
        final_root.parent.mkdir(parents=True, exist_ok=True)
        tmp_root = final_root.with_name(f".{final_root.name}.{uuid.uuid4().hex}.tmp")
        shutil.rmtree(tmp_root, ignore_errors=True)
        tmp_root.mkdir(parents=True, exist_ok=False)
        repository = self.registry.repository_service(repository_id)
        try:
            base_manifest = json.loads(str(row["base_manifest_json"]))
        except (json.JSONDecodeError, TypeError) as exc:
            raise ForgeTraceError(
                "Pull-request baseline metadata is invalid.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "review_revision_integrity_failed",
            ) from exc
        file_manifest: dict[str, dict[str, Any]] = {}
        deletion_manifest: dict[str, dict[str, Any]] = {}
        complete = True
        total_bytes = 0
        try:
            for item in files:
                rel = self._validated_manifest_path(repository_id, str(item["path"]))
                size = int(item["size"])
                expected_hash = str(item["sha256"])
                source = self.staged_path(repository_id, pull_request_id, rel)
                destination = tmp_root / "files" / rel
                available = source.is_file() and not source.is_symlink()
                if available:
                    try:
                        self._copy_verified(source, destination, expected_size=size, expected_hash=expected_hash)
                    except (ForgeTraceError, OSError):
                        if strict:
                            raise
                        available = False
                        destination.unlink(missing_ok=True)
                elif strict:
                    raise ForgeTraceError(
                        "A staged pull-request file is missing; submission was not recorded.",
                        HTTPStatus.CONFLICT,
                        "review_revision_file_missing",
                        {"path": rel},
                    )
                line_context = False
                line_count = 0
                if available:
                    line_context, line_count = self._line_metadata(repository, destination, size)
                else:
                    complete = False
                base_hash = str(item.get("base_hash") or item.get("baseHash") or "")
                base_size_expected = int((base_manifest.get(rel) or {}).get("size") or 0)
                base_available, base_size, base_text, base_lines = self._capture_base_evidence(
                    repository,
                    destination=tmp_root / "base-files" / rel,
                    rel=rel,
                    base_hash=base_hash,
                    expected_size=base_size_expected,
                )
                file_manifest[rel] = {
                    "kind": "file",
                    "path": rel,
                    "size": size,
                    "sha256": expected_hash,
                    "baseHash": base_hash,
                    "baseSnapshotAvailable": bool(base_available),
                    "baseSize": int(base_size),
                    "baseText": bool(base_text),
                    "baseLineCount": int(base_lines),
                    "risky": bool(item.get("risky")),
                    "snapshotAvailable": bool(available),
                    "lineContextAvailable": bool(line_context),
                    "lineCount": int(line_count),
                }
                total_bytes += size
            for item in deletions:
                rel = self._validated_manifest_path(repository_id, str(item["path"]))
                base_hash = str(item.get("base_hash") or item.get("baseHash") or "")
                base_size_expected = int((base_manifest.get(rel) or {}).get("size") or 0)
                base_available, base_size, base_text, base_lines = self._capture_base_evidence(
                    repository,
                    destination=tmp_root / "base-files" / rel,
                    rel=rel,
                    base_hash=base_hash,
                    expected_size=base_size_expected,
                )
                deletion_manifest[rel] = {
                    "kind": "deletion",
                    "path": rel,
                    "baseHash": base_hash,
                    "baseSnapshotAvailable": bool(base_available),
                    "baseSize": int(base_size),
                    "baseText": bool(base_text),
                    "baseLineCount": int(base_lines),
                    "snapshotAvailable": False,
                    "lineContextAvailable": False,
                    "lineCount": 0,
                }
            created_at = utc_now()
            manifest = {
                "repositoryId": repository_id,
                "pullRequestId": pull_request_id,
                "revision": revision,
                "baseCommitId": str(row["base_commit_id"]),
                "files": dict(sorted(file_manifest.items())),
                "deletions": dict(sorted(deletion_manifest.items())),
                "submittedAt": submitted_at or str(row["submitted_at"] or created_at),
                "createdAt": created_at,
                "activeContentRendering": False,
            }
            manifest_json = self._canonical_json(manifest)
            manifest_sha256 = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
            manifest_path = tmp_root / "manifest.json"
            with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(manifest_json)
                handle.flush()
                os.fsync(handle.fileno())
            if final_root.exists():
                raise ForgeTraceError(
                    "Submitted revision storage already exists unexpectedly.",
                    HTTPStatus.CONFLICT,
                    "review_revision_already_exists",
                )
            os.replace(tmp_root, final_root)
            connection.execute(
                """
                INSERT INTO pull_request_revisions(
                    pull_request_id, repository_id, revision, manifest_json, manifest_sha256,
                    snapshot_state, file_count, deletion_count, total_bytes, submitted_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pull_request_id,
                    repository_id,
                    revision,
                    manifest_json,
                    manifest_sha256,
                    "complete" if complete else "metadata_only",
                    len(file_manifest),
                    len(deletion_manifest),
                    total_bytes,
                    manifest["submittedAt"],
                    created_at,
                ),
            )
            return {
                "pullRequestId": pull_request_id,
                "repositoryId": repository_id,
                "revision": revision,
                "manifestSha256": manifest_sha256,
                "snapshotState": "complete" if complete else "metadata_only",
            }
        except Exception:
            shutil.rmtree(tmp_root, ignore_errors=True)
            # If the DB insert failed after the atomic rename, the directory is an orphan
            # and can be safely removed because no thread can reference it yet.
            exists = connection.execute(
                "SELECT 1 FROM pull_request_revisions WHERE pull_request_id=? AND revision=?",
                (pull_request_id, revision),
            ).fetchone()
            if not exists:
                shutil.rmtree(final_root, ignore_errors=True)
            raise

    def backfill_current_revisions(self) -> dict[str, int]:
        created = 0
        metadata_only = 0
        with self.lock:
            connection = self.connect()
            try:
                rows = connection.execute(
                    """
                    SELECT * FROM pull_requests
                    WHERE status IN ('open','approved','changes_requested','conflict')
                    ORDER BY repository_id, number
                    """
                ).fetchall()
                for row in rows:
                    if connection.execute(
                        "SELECT 1 FROM pull_request_revisions WHERE pull_request_id=? AND revision=?",
                        (row["id"], int(row["revision"])),
                    ).fetchone():
                        continue
                    files = [dict(item) for item in connection.execute(
                        "SELECT * FROM pull_request_files WHERE pull_request_id=? ORDER BY path COLLATE NOCASE",
                        (row["id"],),
                    )]
                    deletions = [dict(item) for item in connection.execute(
                        "SELECT * FROM pull_request_deletions WHERE pull_request_id=? ORDER BY path COLLATE NOCASE",
                        (row["id"],),
                    )]
                    result = self.capture_submission(
                        connection,
                        row,
                        revision=int(row["revision"]),
                        files=files,
                        deletions=deletions,
                        strict=False,
                        submitted_at=str(row["submitted_at"] or row["updated_at"]),
                    )
                    created += 1
                    if result.get("snapshotState") == "metadata_only":
                        metadata_only += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        self._cleanup_orphan_revision_dirs()
        return {"created": created, "metadataOnly": metadata_only}

    def _cleanup_orphan_revision_dirs(self) -> int:
        removed = 0
        known: set[Path] = set()
        connection = self.connect()
        try:
            for row in connection.execute(
                "SELECT repository_id, pull_request_id, revision FROM pull_request_revisions"
            ):
                known.add(self._revision_root(row["repository_id"], row["pull_request_id"], int(row["revision"])))
        finally:
            connection.close()
        if not self.revisions_dir.exists():
            return 0
        for path in self.revisions_dir.glob("*/*/rev-*"):
            try:
                resolved = path.resolve()
                if path.is_dir() and resolved not in known:
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        for path in self.revisions_dir.rglob(".*.tmp"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        return removed

    @staticmethod
    def _public_revision(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "pullRequestId": row["pull_request_id"],
            "repositoryId": row["repository_id"],
            "revision": int(row["revision"]),
            "manifestSha256": row["manifest_sha256"],
            "snapshotState": row["snapshot_state"],
            "fileCount": int(row["file_count"]),
            "deletionCount": int(row["deletion_count"]),
            "totalBytes": int(row["total_bytes"]),
            "submittedAt": row["submitted_at"],
            "createdAt": row["created_at"],
        }

    def revisions(self, connection: sqlite3.Connection, pull_request_id: str) -> list[dict[str, Any]]:
        return [
            self._public_revision(row)
            for row in connection.execute(
                "SELECT * FROM pull_request_revisions WHERE pull_request_id=? ORDER BY revision DESC",
                (pull_request_id,),
            )
        ]

    def summary(self, connection: sqlite3.Connection, pull_request_id: str, current_revision: int) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END),0) AS unresolved,
                COALESCE(SUM(CASE WHEN resolved=0 AND submitted_revision=? THEN 1 ELSE 0 END),0) AS unresolved_current,
                COALESCE(SUM(CASE WHEN submitted_revision=? THEN 1 ELSE 0 END),0) AS current_total
            FROM review_threads WHERE pull_request_id=?
            """,
            (current_revision, current_revision, pull_request_id),
        ).fetchone()
        comment_count = connection.execute(
            "SELECT COUNT(*) AS count FROM review_comments WHERE pull_request_id=?",
            (pull_request_id,),
        ).fetchone()["count"]
        return {
            "threadCount": int(row["total"]),
            "unresolvedThreadCount": int(row["unresolved"]),
            "currentRevisionThreadCount": int(row["current_total"]),
            "unresolvedCurrentRevisionCount": int(row["unresolved_current"]),
            "commentCount": int(comment_count),
        }

    def unresolved_current_count(self, connection: sqlite3.Connection, pull_request_id: str, revision: int) -> int:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM review_threads WHERE pull_request_id=? AND submitted_revision=? AND resolved=0",
            (pull_request_id, revision),
        ).fetchone()
        return int(row["count"])

    def _load_revision(self, connection: sqlite3.Connection, pull_request_id: str, revision: int) -> tuple[sqlite3.Row, dict[str, Any]]:
        row = connection.execute(
            "SELECT * FROM pull_request_revisions WHERE pull_request_id=? AND revision=?",
            (pull_request_id, revision),
        ).fetchone()
        if not row:
            raise ForgeTraceError(
                "The requested submitted revision is not available for review.",
                HTTPStatus.NOT_FOUND,
                "review_revision_not_found",
            )
        manifest_json = str(row["manifest_json"])
        if hashlib.sha256(manifest_json.encode("utf-8")).hexdigest() != row["manifest_sha256"]:
            raise ForgeTraceError(
                "Review revision metadata failed integrity verification.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "review_revision_integrity_failed",
            )
        try:
            manifest = json.loads(manifest_json)
        except json.JSONDecodeError as exc:
            raise ForgeTraceError(
                "Review revision metadata is invalid.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "review_revision_integrity_failed",
            ) from exc
        return row, manifest

    def _path_context(
        self,
        connection: sqlite3.Connection,
        *,
        repository_id: str,
        pull_request_id: str,
        revision: int,
        raw_path: str,
        start_line: Any,
        end_line: Any,
    ) -> tuple[str, int | None, int | None, dict[str, Any]]:
        rel = self._validated_manifest_path(repository_id, raw_path)
        _revision_row, manifest = self._load_revision(connection, pull_request_id, revision)
        entry = (manifest.get("files") or {}).get(rel)
        if entry is None:
            entry = (manifest.get("deletions") or {}).get(rel)
        if entry is None:
            raise ForgeTraceError(
                "Review path is not part of the selected quarantined revision.",
                HTTPStatus.CONFLICT,
                "review_path_not_in_revision",
                {"path": rel, "revision": revision},
            )
        start: int | None = None
        end: int | None = None
        if start_line not in (None, "") or end_line not in (None, ""):
            if entry.get("kind") != "file" or not entry.get("lineContextAvailable"):
                raise ForgeTraceError(
                    "Line context is unavailable for this quarantined path.",
                    HTTPStatus.CONFLICT,
                    "review_line_context_unavailable",
                )
            start = self._whole_number(start_line, label="Start line", minimum=1)
            end = self._whole_number(end_line if end_line not in (None, "") else start, label="End line", minimum=start)
            if end - start + 1 > MAX_LINE_SPAN:
                raise ForgeTraceError(
                    f"A review range may span at most {MAX_LINE_SPAN} lines.",
                    code="review_line_range_too_large",
                )
            line_count = int(entry.get("lineCount") or 0)
            if start > line_count or end > line_count:
                raise ForgeTraceError(
                    "Review line range is outside the preserved submitted file.",
                    HTTPStatus.CONFLICT,
                    "review_line_range_out_of_bounds",
                    {"lineCount": line_count},
                )
        return rel, start, end, dict(entry)

    def _context_excerpt(self, thread: sqlite3.Row, entry: dict[str, Any]) -> dict[str, Any]:
        result = {
            "kind": entry.get("kind", "file"),
            "path": thread["path"],
            "size": int(entry.get("size") or 0),
            "sha256": str(entry.get("sha256") or ""),
            "baseHash": str(entry.get("baseHash") or ""),
            "risky": bool(entry.get("risky")),
            "snapshotAvailable": bool(entry.get("snapshotAvailable")),
            "lineContextAvailable": bool(entry.get("lineContextAvailable")),
            "lineCount": int(entry.get("lineCount") or 0),
            "excerpt": [],
            "activeContentRendered": False,
        }
        if thread["start_line"] is None or not entry.get("snapshotAvailable"):
            return result
        path = self._revision_file(
            thread["repository_id"], thread["pull_request_id"], int(thread["submitted_revision"]), thread["path"]
        )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ForgeTraceError(
                "Preserved review bytes are unavailable.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "review_revision_file_missing",
            ) from exc
        if len(data) != int(entry.get("size") or 0) or hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise ForgeTraceError(
                "Preserved review bytes failed integrity verification.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "review_revision_integrity_failed",
            )
        text = data.decode("utf-8")
        lines = text.splitlines()
        start = int(thread["start_line"])
        end = int(thread["end_line"])
        result["excerpt"] = [
            {"line": number, "text": lines[number - 1]}
            for number in range(start, end + 1)
        ]
        return result

    def _thread_row(
        self,
        connection: sqlite3.Connection,
        *,
        repository_id: str,
        pull_request_id: str,
        thread_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM review_threads WHERE id=? AND repository_id=? AND pull_request_id=?",
            (thread_id, repository_id, pull_request_id),
        ).fetchone()
        if not row:
            raise ForgeTraceError("Review thread not found.", HTTPStatus.NOT_FOUND, "review_thread_not_found")
        return row

    @staticmethod
    def _assert_version(thread: sqlite3.Row, expected_version: Any) -> int:
        try:
            expected = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise ForgeTraceError("expectedVersion must be a whole number.", code="invalid_expected_version") from exc
        if expected != int(thread["version"]):
            raise ForgeTraceError(
                "Review thread changed. Refresh before continuing.",
                HTTPStatus.CONFLICT,
                "review_thread_version_changed",
                {"expectedVersion": expected, "currentVersion": int(thread["version"])},
            )
        return expected

    def _comments(
        self,
        connection: sqlite3.Connection,
        thread_id: str,
        *,
        cursor: int = 0,
        limit: int = DEFAULT_COMMENT_PAGE,
    ) -> tuple[list[dict[str, Any]], int | None]:
        cursor = self._whole_number(cursor, label="Comment cursor", minimum=0)
        limit = self._whole_number(limit, label="Comment limit", minimum=1, maximum=MAX_COMMENT_PAGE)
        rows = connection.execute(
            "SELECT * FROM review_comments WHERE thread_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (thread_id, cursor, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        comments = [
            {
                "id": row["id"],
                "sequence": int(row["sequence"]),
                "threadId": row["thread_id"],
                "repositoryId": row["repository_id"],
                "pullRequestId": row["pull_request_id"],
                "submittedRevision": int(row["submitted_revision"]),
                "authorRole": row["author_role"],
                "authorName": row["author_name"],
                "body": row["body"],
                "createdAt": row["created_at"],
                "requestId": row["request_id"],
            }
            for row in rows
        ]
        return comments, int(rows[-1]["sequence"]) if has_more and rows else None

    def _public_thread(
        self,
        connection: sqlite3.Connection,
        thread: sqlite3.Row,
        *,
        current_revision: int,
        comment_cursor: int = 0,
        comment_limit: int = DEFAULT_COMMENT_PAGE,
    ) -> dict[str, Any]:
        _revision, manifest = self._load_revision(connection, thread["pull_request_id"], int(thread["submitted_revision"]))
        entry = (manifest.get("files") or {}).get(thread["path"]) or (manifest.get("deletions") or {}).get(thread["path"])
        if entry is None:
            raise ForgeTraceError(
                "Review thread context no longer matches its immutable revision manifest.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "review_revision_integrity_failed",
            )
        comments, next_comment_cursor = self._comments(
            connection, thread["id"], cursor=comment_cursor, limit=comment_limit
        )
        event_rows = connection.execute(
            "SELECT * FROM review_thread_events WHERE thread_id=? ORDER BY sequence",
            (thread["id"],),
        ).fetchall()
        return {
            "id": thread["id"],
            "sequence": int(thread["sequence"]),
            "repositoryId": thread["repository_id"],
            "pullRequestId": thread["pull_request_id"],
            "submittedRevision": int(thread["submitted_revision"]),
            "currentRevision": int(thread["submitted_revision"]) == int(current_revision),
            "outdated": int(thread["submitted_revision"]) != int(current_revision),
            "path": thread["path"],
            "startLine": int(thread["start_line"]) if thread["start_line"] is not None else None,
            "endLine": int(thread["end_line"]) if thread["end_line"] is not None else None,
            "createdByRole": thread["created_by_role"],
            "createdByName": thread["created_by_name"],
            "createdAt": thread["created_at"],
            "createdRequestId": thread["created_request_id"],
            "resolved": bool(thread["resolved"]),
            "resolvedByName": thread["resolved_by_name"],
            "resolvedAt": thread["resolved_at"],
            "version": int(thread["version"]),
            "updatedAt": thread["updated_at"],
            "lastActivityAt": thread["last_activity_at"],
            "commentCount": int(connection.execute(
                "SELECT COUNT(*) AS count FROM review_comments WHERE thread_id=?", (thread["id"],)
            ).fetchone()["count"]),
            "comments": comments,
            "nextCommentCursor": next_comment_cursor,
            "events": [
                {
                    "id": row["id"],
                    "eventType": row["event_type"],
                    "actorRole": row["actor_role"],
                    "actorName": row["actor_name"],
                    "threadVersion": int(row["thread_version"]),
                    "createdAt": row["created_at"],
                    "requestId": row["request_id"],
                }
                for row in event_rows
            ],
            "context": self._context_excerpt(thread, dict(entry)),
        }

    def _list_threads(
        self,
        connection: sqlite3.Connection,
        *,
        pull_request_id: str,
        current_revision: int,
        cursor: Any = 0,
        limit: Any = DEFAULT_THREAD_PAGE,
        revision: Any = 0,
        comment_limit: Any = DEFAULT_COMMENT_PAGE,
    ) -> dict[str, Any]:
        cursor_int = self._whole_number(cursor, label="Thread cursor", minimum=0)
        limit_int = self._whole_number(limit, label="Thread limit", minimum=1, maximum=MAX_THREAD_PAGE)
        revision_int = self._whole_number(revision or 0, label="Revision filter", minimum=0)
        comment_limit_int = self._whole_number(comment_limit, label="Comment limit", minimum=1, maximum=MAX_COMMENT_PAGE)
        params: list[Any] = [pull_request_id, cursor_int]
        sql = "SELECT * FROM review_threads WHERE pull_request_id=? AND sequence>?"
        if revision_int:
            sql += " AND submitted_revision=?"
            params.append(revision_int)
        sql += " ORDER BY sequence LIMIT ?"
        params.append(limit_int + 1)
        rows = connection.execute(sql, params).fetchall()
        has_more = len(rows) > limit_int
        rows = rows[:limit_int]
        threads = [
            self._public_thread(
                connection,
                row,
                current_revision=current_revision,
                comment_limit=comment_limit_int,
            )
            for row in rows
        ]
        return {
            "threads": threads,
            "nextCursor": int(rows[-1]["sequence"]) if has_more and rows else None,
            "limit": limit_int,
            "revisionFilter": revision_int or None,
        }

    def list_for_owner(self, repository_id: str, pull_request_id: str, **page: Any) -> dict[str, Any]:
        connection = self.connect()
        try:
            row = self.owner_pr_resolver(connection, repository_id, pull_request_id)
            return self._list_threads(
                connection,
                pull_request_id=pull_request_id,
                current_revision=int(row["revision"]),
                **page,
            )
        finally:
            connection.close()

    def list_for_token(self, token: str, pull_request_id: str, **page: Any) -> dict[str, Any]:
        connection = self.connect()
        try:
            _invite, row = self.pr_for_token(connection, token, pull_request_id)
            return self._list_threads(
                connection,
                pull_request_id=pull_request_id,
                current_revision=int(row["revision"]),
                **page,
            )
        finally:
            connection.close()

    def get_for_owner(
        self,
        repository_id: str,
        pull_request_id: str,
        thread_id: str,
        *,
        comment_cursor: Any = 0,
        comment_limit: Any = DEFAULT_COMMENT_PAGE,
    ) -> dict[str, Any]:
        connection = self.connect()
        try:
            pr = self.owner_pr_resolver(connection, repository_id, pull_request_id)
            thread = self._thread_row(
                connection, repository_id=repository_id, pull_request_id=pull_request_id, thread_id=thread_id
            )
            return self._public_thread(
                connection,
                thread,
                current_revision=int(pr["revision"]),
                comment_cursor=self._whole_number(comment_cursor, label="Comment cursor", minimum=0),
                comment_limit=self._whole_number(comment_limit, label="Comment limit", minimum=1, maximum=MAX_COMMENT_PAGE),
            )
        finally:
            connection.close()

    def get_for_token(
        self,
        token: str,
        pull_request_id: str,
        thread_id: str,
        *,
        comment_cursor: Any = 0,
        comment_limit: Any = DEFAULT_COMMENT_PAGE,
    ) -> dict[str, Any]:
        connection = self.connect()
        try:
            _invite, pr = self.pr_for_token(connection, token, pull_request_id)
            thread = self._thread_row(
                connection,
                repository_id=pr["repository_id"],
                pull_request_id=pull_request_id,
                thread_id=thread_id,
            )
            return self._public_thread(
                connection,
                thread,
                current_revision=int(pr["revision"]),
                comment_cursor=self._whole_number(comment_cursor, label="Comment cursor", minimum=0),
                comment_limit=self._whole_number(comment_limit, label="Comment limit", minimum=1, maximum=MAX_COMMENT_PAGE),
            )
        finally:
            connection.close()

    def _create_thread(
        self,
        connection: sqlite3.Connection,
        *,
        pr: sqlite3.Row,
        actor_role: str,
        actor_name: str,
        body: str,
        submitted_revision: Any,
        expected_pull_request_revision: Any,
        path: str,
        start_line: Any,
        end_line: Any,
        request_id: str,
        request_changes: bool,
        invite_fingerprint: str = "",
    ) -> str:
        if pr["status"] not in REVIEWABLE_STATUSES:
            raise ForgeTraceError(
                "Review conversations are available only on submitted pull requests.",
                HTTPStatus.CONFLICT,
                "pull_request_not_reviewable",
            )
        current_revision = int(pr["revision"])
        expected = self._whole_number(expected_pull_request_revision, label="Expected pull-request revision", minimum=1)
        revision = self._whole_number(submitted_revision, label="Submitted revision", minimum=1)
        if expected != current_revision or revision != current_revision:
            raise ForgeTraceError(
                "Pull request changed. Refresh before creating a review thread.",
                HTTPStatus.CONFLICT,
                "pull_request_revision_changed",
                {"currentRevision": current_revision},
            )
        thread_count = int(connection.execute(
            "SELECT COUNT(*) AS count FROM review_threads WHERE pull_request_id=?", (pr["id"],)
        ).fetchone()["count"])
        comment_count = int(connection.execute(
            "SELECT COUNT(*) AS count FROM review_comments WHERE pull_request_id=?", (pr["id"],)
        ).fetchone()["count"])
        if thread_count >= MAX_THREADS_PER_PULL_REQUEST:
            raise ForgeTraceError(
                "This pull request has reached its review-thread limit.",
                HTTPStatus.TOO_MANY_REQUESTS,
                "review_thread_limit_reached",
            )
        if comment_count >= MAX_COMMENTS_PER_PULL_REQUEST:
            raise ForgeTraceError(
                "This pull request has reached its review-comment limit.",
                HTTPStatus.TOO_MANY_REQUESTS,
                "review_comment_limit_reached",
            )
        actor_name = self._clean_text(actor_name, label="Review author", maximum=120, required=True)
        body = self._clean_text(body, label="Review comment", maximum=MAX_REVIEW_BODY, required=True)
        request_id = self._clean_text(request_id, label="Request ID", maximum=120)
        rel, start, end, _entry = self._path_context(
            connection,
            repository_id=pr["repository_id"],
            pull_request_id=pr["id"],
            revision=revision,
            raw_path=path,
            start_line=start_line,
            end_line=end_line,
        )
        if request_changes and actor_role != "owner":
            raise ForgeTraceError("Only the local owner can request changes.", HTTPStatus.FORBIDDEN, "owner_required")
        if request_changes:
            self.audit(
                required=True,
                action="review_changes_requested_authorized",
                outcome="authorized",
                severity="warning",
                repository_id=pr["repository_id"],
                actor=actor_name,
                subject_id=pr["id"],
                surface="owner",
                details={"pullRequestId": pr["id"], "revision": revision, "path": rel},
            )
        thread_id = "thr_" + uuid.uuid4().hex[:20]
        comment_id = "cmt_" + uuid.uuid4().hex[:20]
        now = utc_now()
        connection.execute(
            """
            INSERT INTO review_threads(
                id, repository_id, pull_request_id, submitted_revision, path, start_line, end_line,
                created_by_role, created_by_name, created_at, created_request_id,
                updated_at, last_activity_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                pr["repository_id"],
                pr["id"],
                revision,
                rel,
                start,
                end,
                actor_role,
                actor_name,
                now,
                request_id,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO review_comments(
                id, thread_id, repository_id, pull_request_id, submitted_revision,
                author_role, author_name, body, created_at, request_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                comment_id,
                thread_id,
                pr["repository_id"],
                pr["id"],
                revision,
                actor_role,
                actor_name,
                body,
                now,
                request_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO review_thread_events(
                id, thread_id, event_type, actor_role, actor_name, thread_version, created_at, request_id
            ) VALUES (?, ?, 'created', ?, ?, 1, ?, ?)
            """,
            ("evt_" + uuid.uuid4().hex[:20], thread_id, actor_role, actor_name, now, request_id),
        )
        if request_changes or pr["status"] == "approved":
            next_status = "changes_requested" if request_changes else "open"
            connection.execute(
                "UPDATE pull_requests SET status=?, updated_at=? WHERE id=?",
                (next_status, now, pr["id"]),
            )
        if request_changes:
            connection.execute(
                """
                INSERT INTO pull_request_reviews(
                    id, pull_request_id, reviewer, verdict, comment, created_at, revision, request_id
                ) VALUES (?, ?, ?, 'changes_requested', ?, ?, ?, ?)
                """,
                (
                    "rev_" + uuid.uuid4().hex[:16],
                    pr["id"],
                    actor_name,
                    f"Changes requested through inline thread {thread_id}.",
                    now,
                    revision,
                    request_id,
                ),
            )
        self.audit(
            action="review_thread_created",
            outcome="success",
            severity="warning" if request_changes else "info",
            repository_id=pr["repository_id"],
            actor=actor_name,
            subject_id=thread_id,
            surface="owner" if actor_role == "owner" else "gateway",
            details={
                "pullRequestId": pr["id"],
                "threadId": thread_id,
                "revision": revision,
                "path": rel,
                "hasLineRange": start is not None,
                "requestChanges": bool(request_changes),
                "inviteFingerprint": invite_fingerprint,
                "requestId": request_id,
            },
        )
        return thread_id

    def create_for_owner(self, repository_id: str, pull_request_id: str, **values: Any) -> dict[str, Any]:
        with self.lock:
            connection = self.connect()
            try:
                pr = self.owner_pr_resolver(connection, repository_id, pull_request_id)
                thread_id = self._create_thread(
                    connection,
                    pr=pr,
                    actor_role="owner",
                    actor_name=values.get("actor_name", ""),
                    body=values.get("body", ""),
                    submitted_revision=values.get("submitted_revision"),
                    expected_pull_request_revision=values.get("expected_pull_request_revision"),
                    path=values.get("path", ""),
                    start_line=values.get("start_line"),
                    end_line=values.get("end_line"),
                    request_id=values.get("request_id", ""),
                    request_changes=bool(values.get("request_changes", False)),
                )
                connection.commit()
                return self.get_for_owner(repository_id, pull_request_id, thread_id)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def create_for_token(self, token: str, pull_request_id: str, **values: Any) -> dict[str, Any]:
        with self.lock:
            connection = self.connect()
            try:
                _invite, pr = self.pr_for_token(connection, token, pull_request_id)
                thread_id = self._create_thread(
                    connection,
                    pr=pr,
                    actor_role="contributor",
                    actor_name=pr["author_name"],
                    body=values.get("body", ""),
                    submitted_revision=values.get("submitted_revision"),
                    expected_pull_request_revision=values.get("expected_pull_request_revision"),
                    path=values.get("path", ""),
                    start_line=values.get("start_line"),
                    end_line=values.get("end_line"),
                    request_id=values.get("request_id", ""),
                    request_changes=False,
                    invite_fingerprint=self.token_fingerprint(token),
                )
                connection.commit()
                return self.get_for_token(token, pull_request_id, thread_id)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _reply(
        self,
        connection: sqlite3.Connection,
        *,
        pr: sqlite3.Row,
        thread: sqlite3.Row,
        actor_role: str,
        actor_name: str,
        body: str,
        expected_version: Any,
        request_id: str,
        invite_fingerprint: str = "",
    ) -> None:
        if pr["status"] in TERMINAL_STATUSES:
            raise ForgeTraceError("Closed or merged pull requests cannot receive comments.", HTTPStatus.CONFLICT, "pull_request_not_reviewable")
        if thread["resolved"]:
            raise ForgeTraceError("Resolved threads must be reopened before replying.", HTTPStatus.CONFLICT, "review_thread_resolved")
        version = self._assert_version(thread, expected_version)
        thread_comments = int(connection.execute(
            "SELECT COUNT(*) AS count FROM review_comments WHERE thread_id=?", (thread["id"],)
        ).fetchone()["count"])
        pull_request_comments = int(connection.execute(
            "SELECT COUNT(*) AS count FROM review_comments WHERE pull_request_id=?",
            (thread["pull_request_id"],),
        ).fetchone()["count"])
        if thread_comments >= MAX_COMMENTS_PER_THREAD:
            raise ForgeTraceError(
                "This review thread has reached its comment limit.",
                HTTPStatus.TOO_MANY_REQUESTS,
                "review_thread_comment_limit_reached",
            )
        if pull_request_comments >= MAX_COMMENTS_PER_PULL_REQUEST:
            raise ForgeTraceError(
                "This pull request has reached its review-comment limit.",
                HTTPStatus.TOO_MANY_REQUESTS,
                "review_comment_limit_reached",
            )
        actor_name = self._clean_text(actor_name, label="Review author", maximum=120, required=True)
        body = self._clean_text(body, label="Review comment", maximum=MAX_REVIEW_BODY, required=True)
        request_id = self._clean_text(request_id, label="Request ID", maximum=120)
        now = utc_now()
        connection.execute(
            """
            INSERT INTO review_comments(
                id, thread_id, repository_id, pull_request_id, submitted_revision,
                author_role, author_name, body, created_at, request_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cmt_" + uuid.uuid4().hex[:20],
                thread["id"],
                thread["repository_id"],
                thread["pull_request_id"],
                int(thread["submitted_revision"]),
                actor_role,
                actor_name,
                body,
                now,
                request_id,
            ),
        )
        updated = connection.execute(
            """
            UPDATE review_threads SET version=version+1, updated_at=?, last_activity_at=?
            WHERE id=? AND version=?
            """,
            (now, now, thread["id"], version),
        )
        if updated.rowcount != 1:
            raise ForgeTraceError(
                "Review thread changed. Refresh before continuing.",
                HTTPStatus.CONFLICT,
                "review_thread_version_changed",
            )
        self.audit(
            action="review_thread_commented",
            outcome="success",
            repository_id=thread["repository_id"],
            actor=actor_name,
            subject_id=thread["id"],
            surface="owner" if actor_role == "owner" else "gateway",
            details={
                "pullRequestId": thread["pull_request_id"],
                "threadId": thread["id"],
                "revision": int(thread["submitted_revision"]),
                "path": thread["path"],
                "inviteFingerprint": invite_fingerprint,
                "requestId": request_id,
            },
        )

    def reply_owner(self, repository_id: str, pull_request_id: str, thread_id: str, **values: Any) -> dict[str, Any]:
        with self.lock:
            connection = self.connect()
            try:
                pr = self.owner_pr_resolver(connection, repository_id, pull_request_id)
                thread = self._thread_row(
                    connection, repository_id=repository_id, pull_request_id=pull_request_id, thread_id=thread_id
                )
                self._reply(
                    connection,
                    pr=pr,
                    thread=thread,
                    actor_role="owner",
                    actor_name=values.get("actor_name", ""),
                    body=values.get("body", ""),
                    expected_version=values.get("expected_version"),
                    request_id=values.get("request_id", ""),
                )
                connection.commit()
                return self.get_for_owner(repository_id, pull_request_id, thread_id)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def reply_token(self, token: str, pull_request_id: str, thread_id: str, **values: Any) -> dict[str, Any]:
        with self.lock:
            connection = self.connect()
            try:
                _invite, pr = self.pr_for_token(connection, token, pull_request_id)
                thread = self._thread_row(
                    connection,
                    repository_id=pr["repository_id"],
                    pull_request_id=pull_request_id,
                    thread_id=thread_id,
                )
                self._reply(
                    connection,
                    pr=pr,
                    thread=thread,
                    actor_role="contributor",
                    actor_name=pr["author_name"],
                    body=values.get("body", ""),
                    expected_version=values.get("expected_version"),
                    request_id=values.get("request_id", ""),
                    invite_fingerprint=self.token_fingerprint(token),
                )
                connection.commit()
                return self.get_for_token(token, pull_request_id, thread_id)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _set_resolution(
        self,
        connection: sqlite3.Connection,
        *,
        pr: sqlite3.Row,
        thread: sqlite3.Row,
        actor_name: str,
        expected_version: Any,
        resolved: bool,
        request_id: str,
    ) -> None:
        if pr["status"] in TERMINAL_STATUSES:
            raise ForgeTraceError("Closed or merged pull requests cannot be moderated.", HTTPStatus.CONFLICT, "pull_request_not_reviewable")
        version = self._assert_version(thread, expected_version)
        if bool(thread["resolved"]) == resolved:
            state = "resolved" if resolved else "open"
            raise ForgeTraceError(f"Review thread is already {state}.", HTTPStatus.CONFLICT, "review_thread_state_unchanged")
        actor_name = self._clean_text(actor_name, label="Reviewer", maximum=120, required=True)
        request_id = self._clean_text(request_id, label="Request ID", maximum=120)
        action = "review_thread_resolve" if resolved else "review_thread_reopen"
        self.audit(
            required=True,
            action=f"{action}_authorized",
            outcome="authorized",
            severity="warning",
            repository_id=thread["repository_id"],
            actor=actor_name,
            subject_id=thread["id"],
            surface="owner",
            details={
                "pullRequestId": thread["pull_request_id"],
                "threadId": thread["id"],
                "revision": int(thread["submitted_revision"]),
                "path": thread["path"],
                "requestId": request_id,
            },
        )
        now = utc_now()
        updated = connection.execute(
            """
            UPDATE review_threads SET
                resolved=?, resolved_by_name=?, resolved_at=?, version=version+1,
                updated_at=?, last_activity_at=?
            WHERE id=? AND version=?
            """,
            (
                int(resolved),
                actor_name if resolved else "",
                now if resolved else "",
                now,
                now,
                thread["id"],
                version,
            ),
        )
        if updated.rowcount != 1:
            raise ForgeTraceError(
                "Review thread changed. Refresh before continuing.",
                HTTPStatus.CONFLICT,
                "review_thread_version_changed",
            )
        next_version = version + 1
        connection.execute(
            """
            INSERT INTO review_thread_events(
                id, thread_id, event_type, actor_role, actor_name, thread_version, created_at, request_id
            ) VALUES (?, ?, ?, 'owner', ?, ?, ?, ?)
            """,
            (
                "evt_" + uuid.uuid4().hex[:20],
                thread["id"],
                "resolved" if resolved else "reopened",
                actor_name,
                next_version,
                now,
                request_id,
            ),
        )
        if not resolved and int(thread["submitted_revision"]) == int(pr["revision"]) and pr["status"] == "approved":
            connection.execute("UPDATE pull_requests SET status='open', updated_at=? WHERE id=?", (now, pr["id"]))
        self.audit(
            action=f"{action}d" if resolved else "review_thread_reopened",
            outcome="success",
            severity="warning",
            repository_id=thread["repository_id"],
            actor=actor_name,
            subject_id=thread["id"],
            surface="owner",
            details={
                "pullRequestId": thread["pull_request_id"],
                "threadId": thread["id"],
                "revision": int(thread["submitted_revision"]),
                "path": thread["path"],
                "threadVersion": next_version,
                "requestId": request_id,
            },
        )

    def resolve_owner(self, repository_id: str, pull_request_id: str, thread_id: str, **values: Any) -> dict[str, Any]:
        return self._resolution_owner(repository_id, pull_request_id, thread_id, resolved=True, **values)

    def reopen_owner(self, repository_id: str, pull_request_id: str, thread_id: str, **values: Any) -> dict[str, Any]:
        return self._resolution_owner(repository_id, pull_request_id, thread_id, resolved=False, **values)

    def _resolution_owner(
        self, repository_id: str, pull_request_id: str, thread_id: str, *, resolved: bool, **values: Any
    ) -> dict[str, Any]:
        with self.lock:
            connection = self.connect()
            try:
                pr = self.owner_pr_resolver(connection, repository_id, pull_request_id)
                thread = self._thread_row(
                    connection, repository_id=repository_id, pull_request_id=pull_request_id, thread_id=thread_id
                )
                self._set_resolution(
                    connection,
                    pr=pr,
                    thread=thread,
                    actor_name=values.get("actor_name", ""),
                    expected_version=values.get("expected_version"),
                    resolved=resolved,
                    request_id=values.get("request_id", ""),
                )
                connection.commit()
                return self.get_for_owner(repository_id, pull_request_id, thread_id)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def cleanup_retention(self) -> dict[str, int]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=REVIEW_RETENTION_DAYS)).isoformat(timespec="seconds").replace("+00:00", "Z")
        removed_revisions = 0
        removed_threads = 0
        paths: list[Path] = []
        with self.lock:
            connection = self.connect()
            try:
                rows = connection.execute(
                    """
                    SELECT pr.repository_id, pr.id AS pull_request_id, r.revision,
                           (SELECT COUNT(*) FROM review_threads t WHERE t.pull_request_id=pr.id AND t.submitted_revision=r.revision) AS thread_count
                    FROM pull_requests pr
                    JOIN pull_request_revisions r ON r.pull_request_id=pr.id
                    WHERE pr.status IN ('merged','closed') AND pr.updated_at < ?
                    """,
                    (cutoff,),
                ).fetchall()
                for row in rows:
                    removed_threads += int(row["thread_count"])
                    paths.append(self._revision_root(row["repository_id"], row["pull_request_id"], int(row["revision"])))
                if rows:
                    connection.execute(
                        """
                        DELETE FROM pull_request_revisions
                        WHERE pull_request_id IN (
                            SELECT id FROM pull_requests
                            WHERE status IN ('merged','closed') AND updated_at < ?
                        )
                        """,
                        (cutoff,),
                    )
                    removed_revisions = len(rows)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        for path in paths:
            shutil.rmtree(path, ignore_errors=True)
        orphans = self._cleanup_orphan_revision_dirs()
        return {
            "reviewRevisions": removed_revisions,
            "reviewThreads": removed_threads,
            "orphanRevisionDirectories": orphans,
        }

    def storage_metrics(self, *, max_files: int | None = None) -> dict[str, Any]:
        bytes_total = 0
        files_total = 0
        complete = True
        limit = None if max_files is None else max(1, min(int(max_files), 1_000_000))
        if self.revisions_dir.exists():
            for path in self.revisions_dir.rglob("*"):
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
            thread_count = int(connection.execute("SELECT COUNT(*) AS count FROM review_threads").fetchone()["count"])
            unresolved = int(connection.execute("SELECT COUNT(*) AS count FROM review_threads WHERE resolved=0").fetchone()["count"])
            comment_count = int(connection.execute("SELECT COUNT(*) AS count FROM review_comments").fetchone()["count"])
            revision_count = int(connection.execute("SELECT COUNT(*) AS count FROM pull_request_revisions").fetchone()["count"])
        finally:
            connection.close()
        return {
            "revisionBytes": bytes_total,
            "revisionFiles": files_total,
            "revisionCount": revision_count,
            "threadCount": thread_count,
            "unresolvedThreadCount": unresolved,
            "commentCount": comment_count,
            "terminalRetentionDays": REVIEW_RETENTION_DAYS,
            "complete": complete,
        }

    def health_assessment(
        self, *, max_revisions: int = 100, max_files: int = 1000
    ) -> dict[str, Any]:
        """Bounded, read-only verification of immutable submitted revisions."""

        revision_limit = max(1, min(int(max_revisions), 2000))
        file_limit = max(1, min(int(max_files), 100_000))
        issues: list[dict[str, Any]] = []
        verified_revisions = 0
        verified_files = 0
        file_scan_truncated = False
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=REVIEW_RETENTION_DAYS)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.lock:
            connection = self.connect()
            try:
                integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
                sqlite_integrity = "ok" if integrity_rows == ["ok"] else "; ".join(integrity_rows)
                foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
                total_revisions = int(
                    connection.execute("SELECT COUNT(*) FROM pull_request_revisions").fetchone()[0]
                )
                rows = connection.execute(
                    "SELECT * FROM pull_request_revisions ORDER BY created_at DESC LIMIT ?",
                    (revision_limit + 1,),
                ).fetchall()
                unresolved_current = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM review_threads t
                        JOIN pull_requests pr ON pr.id=t.pull_request_id
                        WHERE t.resolved=0 AND t.submitted_revision=pr.revision
                          AND pr.status IN ('open','approved','changes_requested','conflict')
                        """
                    ).fetchone()[0]
                )
                retention_eligible = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM pull_request_revisions r
                        JOIN pull_requests pr ON pr.id=r.pull_request_id
                        WHERE pr.status IN ('merged','closed') AND pr.updated_at < ?
                        """,
                        (cutoff,),
                    ).fetchone()[0]
                )
                for row in rows[:revision_limit]:
                    try:
                        _stored, manifest = self._load_revision(
                            connection, str(row["pull_request_id"]), int(row["revision"])
                        )
                    except ForgeTraceError as exc:
                        issues.append(
                            {
                                "code": exc.code,
                                "repositoryId": str(row["repository_id"]),
                                "pullRequestId": str(row["pull_request_id"]),
                                "revision": int(row["revision"]),
                                "message": str(exc),
                            }
                        )
                        continue
                    verified_revisions += 1
                    candidates: list[tuple[str, dict[str, Any], bool]] = []
                    for rel, entry in sorted((manifest.get("files") or {}).items()):
                        if bool(entry.get("snapshotAvailable")):
                            candidates.append((str(rel), dict(entry), False))
                        if bool(entry.get("baseSnapshotAvailable")):
                            candidates.append((str(rel), dict(entry), True))
                    for rel, entry in sorted((manifest.get("deletions") or {}).items()):
                        if bool(entry.get("baseSnapshotAvailable")):
                            candidates.append((str(rel), dict(entry), True))
                    for rel, entry, base in candidates:
                        if verified_files >= file_limit:
                            file_scan_truncated = True
                            break
                        path = (
                            self._revision_base_file(
                                str(row["repository_id"]), str(row["pull_request_id"]), int(row["revision"]), rel
                            )
                            if base
                            else self._revision_file(
                                str(row["repository_id"]), str(row["pull_request_id"]), int(row["revision"]), rel
                            )
                        )
                        expected_size = int((entry.get("baseSize") if base else entry.get("size")) or 0)
                        expected_hash = str(entry.get("baseHash") if base else entry.get("sha256") or "")
                        try:
                            if path.is_symlink() or not path.is_file():
                                raise OSError("evidence file is missing or not regular")
                            data = path.read_bytes()
                        except OSError as exc:
                            issues.append(
                                {
                                    "code": "review_revision_file_missing",
                                    "repositoryId": str(row["repository_id"]),
                                    "pullRequestId": str(row["pull_request_id"]),
                                    "revision": int(row["revision"]),
                                    "path": rel,
                                    "evidenceRole": "base" if base else "submitted",
                                    "message": str(exc),
                                }
                            )
                            verified_files += 1
                            continue
                        actual_hash = hashlib.sha256(data).hexdigest()
                        if len(data) != expected_size or actual_hash != expected_hash:
                            issues.append(
                                {
                                    "code": "review_revision_integrity_failed",
                                    "repositoryId": str(row["repository_id"]),
                                    "pullRequestId": str(row["pull_request_id"]),
                                    "revision": int(row["revision"]),
                                    "path": rel,
                                    "evidenceRole": "base" if base else "submitted",
                                }
                            )
                        verified_files += 1
                known_roots = {
                    (
                        str(row["repository_id"]),
                        str(row["pull_request_id"]),
                        f"rev-{int(row['revision']):010d}",
                    )
                    for row in connection.execute(
                        "SELECT repository_id,pull_request_id,revision FROM pull_request_revisions"
                    )
                }
            finally:
                connection.close()

        orphan_count = 0
        scanned_directories = 0
        orphan_scan_complete = True
        for path in self.revisions_dir.glob("*/*/rev-*"):
            if scanned_directories >= revision_limit * 2:
                orphan_scan_complete = False
                break
            scanned_directories += 1
            try:
                key = (path.parent.parent.name, path.parent.name, path.name)
                if path.is_dir() and key not in known_roots:
                    orphan_count += 1
            except OSError:
                continue
        revision_scan_complete = len(rows) <= revision_limit
        file_scan_complete = not file_scan_truncated
        return {
            "sqliteIntegrity": sqlite_integrity,
            "foreignKeyIssueCount": len(foreign_keys),
            "foreignKeyIssues": foreign_keys[:100],
            "revisionCount": total_revisions,
            "verifiedRevisionCount": verified_revisions,
            "verifiedFileCount": verified_files,
            "complete": revision_scan_complete and file_scan_complete and orphan_scan_complete,
            "orphanScanComplete": orphan_scan_complete,
            "unresolvedCurrentThreadCount": unresolved_current,
            "retentionEligibleRevisionCount": retention_eligible,
            "orphanRevisionDirectoryCount": orphan_count,
            "issues": issues,
        }
