from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterator

from .constants import APP_SCHEMA_VERSION, MAX_REQUEST_BYTES
from .errors import ForgeTraceError
from .forking import CollaborationForkClient
from .repository import ForgeTraceRepository
from .utils import normalize_repository_path, utc_now


MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (
        1,
        "0001_repository_registry",
        """
        CREATE TABLE IF NOT EXISTS application_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS repositories (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL,
            canonical_path TEXT NOT NULL UNIQUE,
            metadata_mode TEXT NOT NULL DEFAULT 'embedded'
                CHECK(metadata_mode IN ('embedded', 'external')),
            default_author TEXT NOT NULL DEFAULT '',
            favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0, 1)),
            tags_json TEXT NOT NULL DEFAULT '[]',
            collection_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_opened_at TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_repositories_last_opened
            ON repositories(last_opened_at DESC);
        CREATE INDEX IF NOT EXISTS idx_repositories_favorite
            ON repositories(favorite DESC, name COLLATE NOCASE);
        """,
    ),
    (
        2,
        "0002_registry_organization_and_limits",
        f"""
        ALTER TABLE repositories
            ADD COLUMN upload_limit_bytes INTEGER NOT NULL DEFAULT {MAX_REQUEST_BYTES};
        ALTER TABLE repositories
            ADD COLUMN metadata_path TEXT NOT NULL DEFAULT '';

        CREATE TABLE IF NOT EXISTS repository_tags (
            repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            tag TEXT NOT NULL COLLATE NOCASE,
            created_at TEXT NOT NULL,
            PRIMARY KEY(repository_id, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_repository_tags_tag
            ON repository_tags(tag COLLATE NOCASE);

        CREATE TABLE IF NOT EXISTS collections (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS repository_collections (
            repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            added_at TEXT NOT NULL,
            PRIMARY KEY(repository_id, collection_id)
        );
        CREATE INDEX IF NOT EXISTS idx_repository_collections_collection
            ON repository_collections(collection_id, repository_id);

        CREATE TABLE IF NOT EXISTS saved_filters (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            query_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
)


class RepositoryRegistry:
    """Persistent application-level registry for all ForgeTrace repositories."""

    def __init__(self, project_root: Path, data_dir: Path) -> None:
        self.project_root = project_root.resolve()
        self.data_dir = data_dir.expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "registry.sqlite3"
        self.backups_dir = self.data_dir / "backups"
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._migrate()
        self._backfill_legacy_organization()
        self.startup_cleanup_report = self.cleanup_stale_application_artifacts()
        self.startup_recovery_report = self.recover_startup_repositories()

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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version, name, sql in MIGRATIONS:
                if version in applied:
                    continue
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, utc_now()),
                )
            self._set_state(connection, "schema_version", str(APP_SCHEMA_VERSION))

    def _backfill_legacy_organization(self) -> None:
        """Normalize v0.2.0 tags/collection fields without losing older registry data."""
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT id, tags_json, collection_name FROM repositories"
            ).fetchall()
            for row in rows:
                try:
                    tags = json.loads(row["tags_json"] or "[]")
                except json.JSONDecodeError:
                    tags = []
                for tag in tags if isinstance(tags, list) else []:
                    cleaned = self._clean_tag(tag)
                    if cleaned:
                        connection.execute(
                            "INSERT OR IGNORE INTO repository_tags(repository_id, tag, created_at) VALUES (?, ?, ?)",
                            (row["id"], cleaned, utc_now()),
                        )
                collection_name = str(row["collection_name"] or "").strip()
                if collection_name:
                    existing = connection.execute(
                        "SELECT id FROM collections WHERE name = ? COLLATE NOCASE", (collection_name,)
                    ).fetchone()
                    collection_id = existing["id"] if existing else str(uuid.uuid4())
                    if not existing:
                        now = utc_now()
                        connection.execute(
                            "INSERT INTO collections(id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                            (collection_id, collection_name, now, now),
                        )
                    connection.execute(
                        "INSERT OR IGNORE INTO repository_collections(repository_id, collection_id, added_at) VALUES (?, ?, ?)",
                        (row["id"], collection_id, utc_now()),
                    )

    @staticmethod
    def _set_state(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            """
            INSERT INTO application_state(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, utc_now()),
        )

    @staticmethod
    def _get_state(connection: sqlite3.Connection, key: str, default: str = "") -> str:
        row = connection.execute("SELECT value FROM application_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    @staticmethod
    def _clean_tag(value: Any) -> str:
        tag = " ".join(str(value or "").strip().split())
        if not tag:
            return ""
        if len(tag) > 48:
            raise ForgeTraceError("Tags may not exceed 48 characters.", code="tag_too_long")
        return tag

    @staticmethod
    def _managed_repository_slug(value: Any) -> str:
        """Return a cross-platform directory name for a ForgeTrace-managed repository."""
        name = " ".join(str(value or "").strip().split())
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", name)
        cleaned = re.sub(r"\s+", "-", cleaned)
        cleaned = re.sub(r"-+", "-", cleaned).strip(" .-_")
        cleaned = cleaned[:80].rstrip(" .-_") or "repository"
        reserved = {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{index}" for index in range(1, 10)),
            *(f"LPT{index}" for index in range(1, 10)),
        }
        if cleaned.split(".", 1)[0].upper() in reserved:
            cleaned = f"{cleaned}-repository"
        return cleaned

    @staticmethod
    def _validate_upload_limit(value: Any) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise ForgeTraceError("Upload limit must be a whole number of bytes.", code="invalid_upload_limit") from exc
        minimum = 1024 * 1024
        if limit < minimum or limit > MAX_REQUEST_BYTES:
            raise ForgeTraceError(
                "Upload limit must be between 1 MB and 1,024 MB.",
                code="upload_limit_out_of_range",
                details={"minimum": minimum, "maximum": MAX_REQUEST_BYTES},
            )
        return limit

    @staticmethod
    def _status_for_path(path_value: str) -> tuple[str, str]:
        path = Path(path_value)
        if not path.exists():
            return "offline", "Repository path is unavailable. Relink it when the drive or folder returns."
        if not path.is_dir():
            return "invalid", "Registered path is not a directory."
        if not (path / ".forgetrace" / "state.json").is_file():
            return "uninitialized", "Folder exists but does not contain ForgeTrace metadata."
        return "online", ""

    @staticmethod
    def _path_capabilities(path_value: str) -> dict[str, Any]:
        path = Path(path_value)
        display = str(path)
        exists = path.exists()
        is_directory = exists and path.is_dir()
        readable = is_directory and os.access(path, os.R_OK)
        writable = is_directory and os.access(path, os.W_OK)
        metadata_writable = writable and os.access(path / ".forgetrace", os.W_OK) if (path / ".forgetrace").exists() else writable
        free_bytes: int | None = None
        try:
            probe = path if exists else path.parent
            free_bytes = shutil.disk_usage(probe).free
        except OSError:
            pass
        is_unc = display.startswith("\\\\") or display.startswith("//")
        return {
            "exists": exists,
            "directory": is_directory,
            "readable": readable,
            "writable": writable,
            "metadataWritable": metadata_writable,
            "freeBytes": free_bytes,
            "pathKind": "network" if is_unc else "local-or-mounted",
        }

    def _tags_for(self, connection: sqlite3.Connection, repository_id: str) -> list[str]:
        return [
            str(row["tag"])
            for row in connection.execute(
                "SELECT tag FROM repository_tags WHERE repository_id = ? ORDER BY tag COLLATE NOCASE",
                (repository_id,),
            )
        ]

    def _collections_for(self, connection: sqlite3.Connection, repository_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT c.id, c.name, c.description, c.color
                FROM collections c
                JOIN repository_collections rc ON rc.collection_id = c.id
                WHERE rc.repository_id = ?
                ORDER BY c.name COLLATE NOCASE
                """,
                (repository_id,),
            )
        ]

    def _row_to_public(
        self, connection: sqlite3.Connection, row: sqlite3.Row, *, active_id: str = ""
    ) -> dict[str, Any]:
        status, status_message = self._status_for_path(row["path"])
        collections = self._collections_for(connection, row["id"])
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "path": row["path"],
            "metadataMode": row["metadata_mode"],
            "metadataPath": row["metadata_path"],
            "defaultAuthor": row["default_author"],
            "uploadLimitBytes": int(row["upload_limit_bytes"]),
            "favorite": bool(row["favorite"]),
            "pinned": bool(row["favorite"]),
            "tags": self._tags_for(connection, row["id"]),
            "collections": collections,
            "collection": collections[0]["name"] if collections else "",
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "lastOpenedAt": row["last_opened_at"],
            "status": status,
            "statusMessage": status_message,
            "capabilities": self._path_capabilities(row["path"]),
            "active": row["id"] == active_id,
        }


    def cleanup_stale_application_artifacts(self, *, max_age_seconds: int = 24 * 60 * 60) -> dict[str, Any]:
        """Remove application-data leftovers that are safe to discard after a crash.

        Active repository transactions are recovered by ForgeTraceRepository. This
        cleaner is limited to request/export scratch files and never deletes a
        registered repository. Managed ``.importing-*`` directories are unregistered
        staging areas and are safe to remove at process startup.
        """
        now = time.time()
        removed: list[str] = []
        retained: list[str] = []
        transfer_dir = self.data_dir / "transfers"
        transfer_dir.mkdir(parents=True, exist_ok=True)
        for entry in sorted(transfer_dir.iterdir()):
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                retained.append(str(entry))
                continue
            if age < max_age_seconds:
                retained.append(str(entry))
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink(missing_ok=True)
                removed.append(str(entry))
            except OSError:
                retained.append(str(entry))
        managed = self.managed_repositories_dir
        managed.mkdir(parents=True, exist_ok=True)
        for entry in sorted(managed.glob(".importing-*")):
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink(missing_ok=True)
                removed.append(str(entry))
            except OSError:
                retained.append(str(entry))
        return {"removed": removed, "retained": retained, "removedCount": len(removed)}

    def active_repository_id(self) -> str:
        with self.connect() as connection:
            return self._get_state(connection, "active_repository_id")

    def list_repositories(
        self,
        *,
        query: str = "",
        tag: str = "",
        collection_id: str = "",
        status: str = "",
        favorite: bool | None = None,
    ) -> dict[str, Any]:
        query = query.strip().casefold()
        tag = tag.strip().casefold()
        collection_id = collection_id.strip()
        status = status.strip().lower()
        with self.connect() as connection:
            active_id = self._get_state(connection, "active_repository_id")
            rows = connection.execute(
                """
                SELECT * FROM repositories
                ORDER BY favorite DESC,
                         CASE WHEN last_opened_at = '' THEN 1 ELSE 0 END,
                         last_opened_at DESC,
                         name COLLATE NOCASE
                """
            ).fetchall()
            if active_id and not any(row["id"] == active_id for row in rows):
                active_id = ""
                self._set_state(connection, "active_repository_id", "")
            records = [self._row_to_public(connection, row, active_id=active_id) for row in rows]
            if query:
                records = [
                    record for record in records
                    if query in " ".join([
                        record["name"], record["description"], record["path"], *record["tags"],
                        *(collection["name"] for collection in record["collections"]),
                    ]).casefold()
                ]
            if tag:
                records = [record for record in records if tag in {item.casefold() for item in record["tags"]}]
            if collection_id:
                records = [
                    record for record in records
                    if collection_id in {item["id"] for item in record["collections"]}
                ]
            if status:
                records = [record for record in records if record["status"] == status]
            if favorite is not None:
                records = [record for record in records if record["favorite"] is favorite]
            return {"activeRepositoryId": active_id, "repositories": records}

    def list_library(self) -> dict[str, Any]:
        with self.connect() as connection:
            collections = [dict(row) for row in connection.execute(
                """
                SELECT c.*, COUNT(rc.repository_id) AS repositoryCount
                FROM collections c
                LEFT JOIN repository_collections rc ON rc.collection_id = c.id
                GROUP BY c.id
                ORDER BY c.name COLLATE NOCASE
                """
            )]
            tags = [dict(row) for row in connection.execute(
                """
                SELECT tag, COUNT(*) AS repositoryCount
                FROM repository_tags
                GROUP BY tag COLLATE NOCASE
                ORDER BY tag COLLATE NOCASE
                """
            )]
            filters = []
            for row in connection.execute("SELECT * FROM saved_filters ORDER BY name COLLATE NOCASE"):
                try:
                    query = json.loads(row["query_json"])
                except json.JSONDecodeError:
                    query = {}
                filters.append({
                    "id": row["id"], "name": row["name"], "query": query,
                    "createdAt": row["created_at"], "updatedAt": row["updated_at"],
                })
            return {"collections": collections, "tags": tags, "savedFilters": filters}

    def _fetch_row(self, connection: sqlite3.Connection, repository_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM repositories WHERE id = ?", (repository_id,)).fetchone()
        if not row:
            raise ForgeTraceError(
                "Repository is not registered.", HTTPStatus.NOT_FOUND, "repository_not_found"
            )
        return row

    def get_repository(self, repository_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            active_id = self._get_state(connection, "active_repository_id")
            return self._row_to_public(connection, self._fetch_row(connection, repository_id), active_id=active_id)

    @property
    def managed_repositories_dir(self) -> Path:
        return self.data_dir / "managed-repositories"

    def _next_managed_repository_path(self, name: str) -> Path:
        root = self.managed_repositories_dir
        root.mkdir(parents=True, exist_ok=True)
        slug = self._managed_repository_slug(name)
        candidate = root / slug
        suffix = 2
        while candidate.exists():
            candidate = root / f"{slug}-{suffix}"
            suffix += 1
        return candidate

    def _allocate_managed_repository_path(self, name: str) -> Path:
        candidate = self._next_managed_repository_path(name)
        candidate.mkdir(parents=False, exist_ok=False)
        return candidate

    def create_managed_repository(
        self,
        *,
        name: str = "",
        description: str = "",
        author: str = "",
        upload_limit_bytes: int = MAX_REQUEST_BYTES,
    ) -> dict[str, Any]:
        """Create a normal repository inside ForgeTrace's managed workspace root.

        This supports browser uploads, which cannot reveal or write an arbitrary absolute
        filesystem path. The resulting files remain ordinary local files and can be
        moved or relinked like any other ForgeTrace repository.
        """
        clean_name = " ".join(str(name or "").strip().split()) or "Imported repository"
        if len(clean_name) > 120:
            raise ForgeTraceError(
                "Repository name may not exceed 120 characters.",
                code="repository_name_too_long",
            )
        with self.lock:
            candidate = self._allocate_managed_repository_path(clean_name)
            try:
                return self.register_repository(
                    path=str(candidate),
                    name=clean_name,
                    description=description,
                    author=author,
                    initialize=True,
                    create_directory=False,
                    metadata_mode="embedded",
                    upload_limit_bytes=upload_limit_bytes,
                )
            except Exception:
                shutil.rmtree(candidate, ignore_errors=True)
                raise

    def preview_managed_repository_import(
        self,
        *,
        source_path: str,
        upload_limit_bytes: int = MAX_REQUEST_BYTES,
    ) -> dict[str, Any]:
        from .importing import build_folder_import_plan

        limit = self._validate_upload_limit(upload_limit_bytes)
        preview_root = self.data_dir / "transfers" / f"import-preview-{uuid.uuid4().hex}"
        preview_root.mkdir(parents=True, exist_ok=False)
        try:
            service = ForgeTraceRepository(self.project_root, preview_root, str(uuid.uuid4()), upload_limit_bytes=limit)
            return build_folder_import_plan(
                service,
                source_path,
                include_root=False,
                conflict_policy="skip",
            ).public()
        finally:
            shutil.rmtree(preview_root, ignore_errors=True)

    def create_managed_repository_from_local_folder(
        self,
        *,
        source_path: str,
        name: str = "",
        description: str = "",
        author: str = "",
        upload_limit_bytes: int = MAX_REQUEST_BYTES,
        conflict_policy: str = "abort",
        progress: Any = None,
        cancelled: Any = None,
    ) -> dict[str, Any]:
        """Create, import, snapshot, move, and register a repository as one operation."""
        source = Path(str(source_path or "")).expanduser().resolve()
        if not source.is_dir():
            raise ForgeTraceError("The selected source folder is unavailable.", HTTPStatus.NOT_FOUND, "source_folder_missing")
        clean_name = " ".join(str(name or source.name or "Imported repository").strip().split())
        limit = self._validate_upload_limit(upload_limit_bytes)
        repository_id = str(uuid.uuid4())
        root = self.managed_repositories_dir
        root.mkdir(parents=True, exist_ok=True)
        staging = root / f".importing-{uuid.uuid4().hex}"
        final_path: Path | None = None
        registered: dict[str, Any] | None = None
        staging.mkdir(parents=False, exist_ok=False)
        try:
            service = ForgeTraceRepository(self.project_root, staging, repository_id, upload_limit_bytes=limit)
            service.initialize(clean_name, str(description or "").strip(), str(author or "Repository Owner").strip())
            result = service.import_local_folder(
                str(source),
                str(author or "Repository Owner").strip(),
                include_root=False,
                conflict_policy=conflict_policy,
                progress=progress,
                cancelled=cancelled,
            )
            service.create_commit(f"Imported complete folder {source.name}", str(author or "Repository Owner").strip())
            with self.lock:
                final_path = self._next_managed_repository_path(clean_name)
                os.replace(staging, final_path)
                registered = self.register_repository(
                    path=str(final_path),
                    name=clean_name,
                    description=str(description or "").strip(),
                    author=str(author or "Repository Owner").strip(),
                    initialize=False,
                    create_directory=False,
                    metadata_mode="embedded",
                    upload_limit_bytes=limit,
                )
                self.set_active(registered["id"])
            payload = self.get_repository(registered["id"])
            payload["import"] = result
            return payload
        except Exception:
            if registered is not None:
                try:
                    self.unregister(registered["id"])
                except Exception:
                    pass
            if final_path is not None:
                shutil.rmtree(final_path, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def discard_managed_repository(self, repository_id: str) -> dict[str, Any]:
        """Remove a failed/provisional managed repository and its registry record."""
        record = self.get_repository(repository_id)
        path = Path(record["path"]).resolve()
        managed = self.managed_repositories_dir.resolve()
        if managed not in path.parents:
            raise ForgeTraceError(
                "Only ForgeTrace-managed repositories can be discarded.",
                HTTPStatus.FORBIDDEN,
                "repository_not_managed",
            )
        result = self.unregister(repository_id)
        shutil.rmtree(path, ignore_errors=True)
        return {"discarded": repository_id, "path": str(path), "registry": result}

    def fork_from_collaboration_link(
        self,
        *,
        share_url: str,
        name: str = "",
        description: str = "",
        author: str = "",
        upload_limit_bytes: int = MAX_REQUEST_BYTES,
    ) -> dict[str, Any]:
        """Create and register a managed local fork from a token-scoped share link."""
        client = CollaborationForkClient(self.data_dir / "transfers")
        base_url, token = client.parse_share_link(share_url)
        context = client.fetch_context(base_url, token)
        upstream = context["repository"]
        clean_name = " ".join(str(name or "").strip().split())
        if not clean_name:
            clean_name = f"{str(upstream.get('name') or 'Shared repository').strip()} Fork"
        if len(clean_name) > 120:
            raise ForgeTraceError("Repository name may not exceed 120 characters.", code="repository_name_too_long")
        archive_path = client.download_source(base_url, token)
        candidate: Path | None = None
        record: dict[str, Any] | None = None
        try:
            with self.lock:
                candidate = self._allocate_managed_repository_path(clean_name)
            extraction = client.extract_source(archive_path, candidate)
            record = self.register_repository(
                path=str(candidate),
                name=clean_name,
                description=str(description or upstream.get("description") or "").strip(),
                author=str(author or "Repository Owner").strip(),
                initialize=True,
                create_directory=False,
                metadata_mode="embedded",
                upload_limit_bytes=upload_limit_bytes,
            )
            repository = self.repository_service(record["id"])
            invite = context.get("invite", {})
            provenance = repository.set_upstream({
                "baseUrl": base_url,
                "repositoryId": str(upstream.get("id") or ""),
                "repositoryName": str(upstream.get("name") or ""),
                "inviteId": str(invite.get("id") or ""),
                "forkedAt": utc_now(),
                "tokenFingerprint": hashlib.sha256(token.encode("utf-8")).hexdigest()[:16],
                "archiveSha256": extraction["archiveSha256"],
                "sourceFiles": extraction["files"],
            })
            repository.create_commit(
                f"Forked from {str(upstream.get('name') or 'shared repository')}",
                str(author or "Repository Owner").strip() or "Repository Owner",
            )
            result = self.get_repository(record["id"])
            result["forkedFrom"] = provenance
            result["importedFiles"] = extraction["files"]
            return result
        except Exception:
            if record is not None:
                try:
                    self.unregister(record["id"])
                except Exception:
                    pass
            if candidate is not None:
                shutil.rmtree(candidate, ignore_errors=True)
            raise
        finally:
            archive_path.unlink(missing_ok=True)

    def register_repository(
        self,
        *,
        path: str,
        name: str = "",
        description: str = "",
        author: str = "",
        initialize: bool = True,
        create_directory: bool = False,
        metadata_mode: str = "embedded",
        upload_limit_bytes: int = MAX_REQUEST_BYTES,
    ) -> dict[str, Any]:
        if metadata_mode != "embedded":
            raise ForgeTraceError(
                "External metadata mode is reserved until identity-safe relinking is implemented.",
                HTTPStatus.NOT_IMPLEMENTED,
                "metadata_mode_not_implemented",
            )
        upload_limit_bytes = self._validate_upload_limit(upload_limit_bytes)
        try:
            display_path, canonical_path = normalize_repository_path(path)
        except ValueError as exc:
            raise ForgeTraceError(str(exc), code="repository_path_required") from exc
        workspace = Path(display_path)
        if create_directory:
            workspace.mkdir(parents=True, exist_ok=True)
        if not workspace.exists():
            raise ForgeTraceError(
                "Repository path does not exist.", HTTPStatus.NOT_FOUND, "repository_path_missing",
                {"path": display_path},
            )
        if not workspace.is_dir():
            raise ForgeTraceError("Repository path must be a directory.", code="repository_path_not_directory")

        probe = ForgeTraceRepository(self.project_root, workspace, None, upload_limit_bytes=upload_limit_bytes)
        stored_id = ""
        if probe.initialized():
            stored_id = str(probe.load_state().get("repository", {}).get("id") or "").strip()
        repository_id = stored_id or str(uuid.uuid4())

        with self.lock, self.connect() as connection:
            duplicate_path = connection.execute(
                "SELECT id, name FROM repositories WHERE canonical_path = ?", (canonical_path,)
            ).fetchone()
            if duplicate_path:
                raise ForgeTraceError(
                    f"That path is already registered as {duplicate_path['name']}.",
                    HTTPStatus.CONFLICT,
                    "duplicate_repository_path",
                    {"repositoryId": duplicate_path["id"]},
                )
            duplicate_identity = connection.execute(
                "SELECT id, name, path FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
            if duplicate_identity:
                raise ForgeTraceError(
                    f"This repository identity is already registered as {duplicate_identity['name']}. Use relink instead.",
                    HTTPStatus.CONFLICT,
                    "duplicate_repository_identity",
                    {"repositoryId": duplicate_identity["id"], "path": duplicate_identity["path"]},
                )

        service = ForgeTraceRepository(
            self.project_root, workspace, repository_id, upload_limit_bytes=upload_limit_bytes
        )
        if service.initialized():
            service.ensure_identity(repository_id)
            state = service.load_state()
            repo_meta = state.get("repository", {})
            resolved_name = (name or repo_meta.get("name") or workspace.name or "Repository").strip()
            resolved_description = (description or repo_meta.get("description") or "").strip()
            resolved_author = (author or repo_meta.get("defaultAuthor") or "Repository Owner").strip()
        elif initialize:
            resolved_name = (name or workspace.name or "Repository").strip()
            resolved_description = description.strip()
            resolved_author = (author or "Repository Owner").strip()
            service.initialize(resolved_name, resolved_description, resolved_author)
        else:
            resolved_name = (name or workspace.name or "Repository").strip()
            resolved_description = description.strip()
            resolved_author = (author or "Repository Owner").strip()

        now = utc_now()
        with self.lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO repositories(
                    id, name, description, path, canonical_path, metadata_mode,
                    default_author, upload_limit_bytes, created_at, updated_at, last_opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repository_id, resolved_name, resolved_description, display_path, canonical_path,
                    metadata_mode, resolved_author, upload_limit_bytes, now, now, now,
                ),
            )
            current_active = self._get_state(connection, "active_repository_id")
            if not current_active:
                self._set_state(connection, "active_repository_id", repository_id)
            row = self._fetch_row(connection, repository_id)
            active_id = self._get_state(connection, "active_repository_id")
            return self._row_to_public(connection, row, active_id=active_id)

    def initialize_registered(
        self, repository_id: str, *, name: str = "", description: str = "", author: str = ""
    ) -> dict[str, Any]:
        record = self.get_repository(repository_id)
        path = Path(record["path"])
        if not path.exists() or not path.is_dir():
            raise ForgeTraceError(
                "Repository path is unavailable.", HTTPStatus.SERVICE_UNAVAILABLE, "repository_offline"
            )
        service = ForgeTraceRepository(
            self.project_root, path, repository_id, upload_limit_bytes=record["uploadLimitBytes"]
        )
        if service.initialized():
            service.ensure_identity(repository_id)
        else:
            service.initialize(
                (name or record["name"] or path.name or "Repository").strip(),
                (description or record["description"] or "").strip(),
                (author or record["defaultAuthor"] or "Repository Owner").strip(),
            )
        state = service.load_state()
        meta = state.get("repository", {})
        with self.lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE repositories
                SET name = ?, description = ?, default_author = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    meta.get("name") or record["name"], meta.get("description") or "",
                    meta.get("defaultAuthor") or "Repository Owner", utc_now(), repository_id,
                ),
            )
            active_id = self._get_state(connection, "active_repository_id")
            return self._row_to_public(
                connection, self._fetch_row(connection, repository_id), active_id=active_id
            )

    def update_settings(
        self,
        repository_id: str,
        *,
        name: str,
        description: str = "",
        default_author: str = "",
        upload_limit_bytes: Any = MAX_REQUEST_BYTES,
    ) -> dict[str, Any]:
        clean_name = " ".join(str(name or "").strip().split())
        if not clean_name:
            raise ForgeTraceError("Repository name is required.", code="repository_name_required")
        if len(clean_name) > 120:
            raise ForgeTraceError("Repository name may not exceed 120 characters.", code="repository_name_too_long")
        clean_description = str(description or "").strip()
        if len(clean_description) > 2000:
            raise ForgeTraceError("Description may not exceed 2,000 characters.", code="description_too_long")
        clean_author = " ".join(str(default_author or "Repository Owner").strip().split()) or "Repository Owner"
        limit = self._validate_upload_limit(upload_limit_bytes)
        record = self.get_repository(repository_id)
        if record["status"] != "online":
            raise ForgeTraceError(
                "Repository settings can only be changed while the repository is online.",
                HTTPStatus.SERVICE_UNAVAILABLE,
                "repository_offline",
                {"path": record["path"], "status": record["status"]},
            )
        service = ForgeTraceRepository(
            self.project_root, Path(record["path"]), repository_id, upload_limit_bytes=limit
        )
        service.ensure_identity(repository_id)
        service.update_repository_metadata(clean_name, clean_description, clean_author)
        with self.lock, self.connect() as connection:
            self._fetch_row(connection, repository_id)
            connection.execute(
                """
                UPDATE repositories
                SET name = ?, description = ?, default_author = ?, upload_limit_bytes = ?, updated_at = ?
                WHERE id = ?
                """,
                (clean_name, clean_description, clean_author, limit, utc_now(), repository_id),
            )
            active_id = self._get_state(connection, "active_repository_id")
            return self._row_to_public(
                connection, self._fetch_row(connection, repository_id), active_id=active_id
            )

    def set_repository_organization(
        self, repository_id: str, *, tags: list[Any], collection_ids: list[Any]
    ) -> dict[str, Any]:
        cleaned_tags = []
        seen_tags: set[str] = set()
        for raw in tags:
            tag = self._clean_tag(raw)
            key = tag.casefold()
            if tag and key not in seen_tags:
                cleaned_tags.append(tag)
                seen_tags.add(key)
        if len(cleaned_tags) > 24:
            raise ForgeTraceError("A repository may have at most 24 tags.", code="too_many_tags")
        cleaned_collection_ids = list(dict.fromkeys(str(value).strip() for value in collection_ids if str(value).strip()))
        with self.lock, self.connect() as connection:
            self._fetch_row(connection, repository_id)
            if cleaned_collection_ids:
                placeholders = ",".join("?" for _ in cleaned_collection_ids)
                found = {
                    row["id"] for row in connection.execute(
                        f"SELECT id FROM collections WHERE id IN ({placeholders})", cleaned_collection_ids
                    )
                }
                missing = [value for value in cleaned_collection_ids if value not in found]
                if missing:
                    raise ForgeTraceError(
                        "One or more collections do not exist.", HTTPStatus.NOT_FOUND,
                        "collection_not_found", {"collectionIds": missing},
                    )
            connection.execute("DELETE FROM repository_tags WHERE repository_id = ?", (repository_id,))
            connection.executemany(
                "INSERT INTO repository_tags(repository_id, tag, created_at) VALUES (?, ?, ?)",
                [(repository_id, tag, utc_now()) for tag in cleaned_tags],
            )
            connection.execute("DELETE FROM repository_collections WHERE repository_id = ?", (repository_id,))
            connection.executemany(
                "INSERT INTO repository_collections(repository_id, collection_id, added_at) VALUES (?, ?, ?)",
                [(repository_id, collection_id, utc_now()) for collection_id in cleaned_collection_ids],
            )
            connection.execute(
                "UPDATE repositories SET tags_json = ?, collection_name = '', updated_at = ? WHERE id = ?",
                (json.dumps(cleaned_tags), utc_now(), repository_id),
            )
            active_id = self._get_state(connection, "active_repository_id")
            return self._row_to_public(
                connection, self._fetch_row(connection, repository_id), active_id=active_id
            )

    def create_collection(self, *, name: str, description: str = "", color: str = "") -> dict[str, Any]:
        clean_name = " ".join(str(name or "").strip().split())
        if not clean_name:
            raise ForgeTraceError("Collection name is required.", code="collection_name_required")
        if len(clean_name) > 80:
            raise ForgeTraceError("Collection name may not exceed 80 characters.", code="collection_name_too_long")
        clean_description = str(description or "").strip()
        clean_color = str(color or "").strip()[:32]
        collection_id = str(uuid.uuid4())
        now = utc_now()
        try:
            with self.lock, self.connect() as connection:
                connection.execute(
                    "INSERT INTO collections(id, name, description, color, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (collection_id, clean_name, clean_description, clean_color, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ForgeTraceError(
                "A collection with that name already exists.", HTTPStatus.CONFLICT,
                "duplicate_collection_name",
            ) from exc
        return {
            "id": collection_id, "name": clean_name, "description": clean_description,
            "color": clean_color, "repositoryCount": 0, "created_at": now, "updated_at": now,
        }

    def update_collection(
        self, collection_id: str, *, name: str, description: str = "", color: str = ""
    ) -> dict[str, Any]:
        clean_name = " ".join(str(name or "").strip().split())
        if not clean_name:
            raise ForgeTraceError("Collection name is required.", code="collection_name_required")
        try:
            with self.lock, self.connect() as connection:
                existing = connection.execute("SELECT id FROM collections WHERE id = ?", (collection_id,)).fetchone()
                if not existing:
                    raise ForgeTraceError(
                        "Collection not found.", HTTPStatus.NOT_FOUND, "collection_not_found"
                    )
                connection.execute(
                    "UPDATE collections SET name = ?, description = ?, color = ?, updated_at = ? WHERE id = ?",
                    (clean_name, str(description or "").strip(), str(color or "").strip()[:32], utc_now(), collection_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ForgeTraceError(
                "A collection with that name already exists.", HTTPStatus.CONFLICT,
                "duplicate_collection_name",
            ) from exc
        return next(item for item in self.list_library()["collections"] if item["id"] == collection_id)

    def delete_collection(self, collection_id: str) -> dict[str, Any]:
        with self.lock, self.connect() as connection:
            row = connection.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
            if not row:
                raise ForgeTraceError("Collection not found.", HTTPStatus.NOT_FOUND, "collection_not_found")
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM repository_collections WHERE collection_id = ?", (collection_id,)
            ).fetchone()["count"]
            connection.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
            return {"deleted": collection_id, "name": row["name"], "repositoriesUnassigned": count}

    def save_filter(self, *, name: str, query: dict[str, Any]) -> dict[str, Any]:
        clean_name = " ".join(str(name or "").strip().split())
        if not clean_name:
            raise ForgeTraceError("Saved filter name is required.", code="filter_name_required")
        allowed = {"query", "tag", "collectionId", "status", "favorite"}
        clean_query = {key: value for key, value in query.items() if key in allowed and value not in {"", None}}
        filter_id = str(uuid.uuid4())
        now = utc_now()
        try:
            with self.lock, self.connect() as connection:
                connection.execute(
                    "INSERT INTO saved_filters(id, name, query_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (filter_id, clean_name, json.dumps(clean_query), now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ForgeTraceError(
                "A saved filter with that name already exists.", HTTPStatus.CONFLICT,
                "duplicate_filter_name",
            ) from exc
        return {"id": filter_id, "name": clean_name, "query": clean_query, "createdAt": now, "updatedAt": now}

    def delete_filter(self, filter_id: str) -> dict[str, Any]:
        with self.lock, self.connect() as connection:
            row = connection.execute("SELECT name FROM saved_filters WHERE id = ?", (filter_id,)).fetchone()
            if not row:
                raise ForgeTraceError("Saved filter not found.", HTTPStatus.NOT_FOUND, "filter_not_found")
            connection.execute("DELETE FROM saved_filters WHERE id = ?", (filter_id,))
            return {"deleted": filter_id, "name": row["name"]}

    def set_active(self, repository_id: str) -> dict[str, Any]:
        with self.lock, self.connect() as connection:
            self._fetch_row(connection, repository_id)
            now = utc_now()
            connection.execute(
                "UPDATE repositories SET last_opened_at = ?, updated_at = ? WHERE id = ?",
                (now, now, repository_id),
            )
            self._set_state(connection, "active_repository_id", repository_id)
            updated = self._fetch_row(connection, repository_id)
            return self._row_to_public(connection, updated, active_id=repository_id)

    def set_favorite(self, repository_id: str, favorite: bool) -> dict[str, Any]:
        with self.lock, self.connect() as connection:
            self._fetch_row(connection, repository_id)
            connection.execute(
                "UPDATE repositories SET favorite = ?, updated_at = ? WHERE id = ?",
                (1 if favorite else 0, utc_now(), repository_id),
            )
            active_id = self._get_state(connection, "active_repository_id")
            return self._row_to_public(
                connection, self._fetch_row(connection, repository_id), active_id=active_id
            )

    def unregister(self, repository_id: str) -> dict[str, Any]:
        """Remove only the registry entry. Never delete repository files."""
        with self.lock, self.connect() as connection:
            row = self._fetch_row(connection, repository_id)
            connection.execute("DELETE FROM repositories WHERE id = ?", (repository_id,))
            active_id = self._get_state(connection, "active_repository_id")
            if active_id == repository_id:
                replacement = connection.execute(
                    "SELECT id FROM repositories ORDER BY favorite DESC, last_opened_at DESC LIMIT 1"
                ).fetchone()
                self._set_state(connection, "active_repository_id", replacement["id"] if replacement else "")
            return {
                "unregistered": repository_id, "name": row["name"], "path": row["path"],
                "filesDeleted": False,
            }

    def relink(self, repository_id: str, new_path: str) -> dict[str, Any]:
        try:
            display_path, canonical_path = normalize_repository_path(new_path)
        except ValueError as exc:
            raise ForgeTraceError(str(exc), code="repository_path_required") from exc
        workspace = Path(display_path)
        if not workspace.exists() or not workspace.is_dir():
            raise ForgeTraceError(
                "Relink path must be an existing directory.", HTTPStatus.NOT_FOUND,
                "relink_path_missing", {"path": display_path},
            )
        record = self.get_repository(repository_id)
        service = ForgeTraceRepository(
            self.project_root, workspace, repository_id, upload_limit_bytes=record["uploadLimitBytes"]
        )
        if not service.initialized():
            raise ForgeTraceError(
                "Relink path does not contain ForgeTrace repository metadata.", HTTPStatus.CONFLICT,
                "relink_metadata_missing",
            )
        service.ensure_identity(repository_id)
        state = service.load_state()
        repo_meta = state.get("repository", {})
        with self.lock, self.connect() as connection:
            self._fetch_row(connection, repository_id)
            duplicate = connection.execute(
                "SELECT id FROM repositories WHERE canonical_path = ? AND id <> ?",
                (canonical_path, repository_id),
            ).fetchone()
            if duplicate:
                raise ForgeTraceError(
                    "Relink path is already registered to another repository.", HTTPStatus.CONFLICT,
                    "duplicate_repository_path", {"repositoryId": duplicate["id"]},
                )
            connection.execute(
                """
                UPDATE repositories
                SET path = ?, canonical_path = ?, name = ?, description = ?,
                    default_author = ?, updated_at = ?, last_opened_at = ?
                WHERE id = ?
                """,
                (
                    display_path, canonical_path, repo_meta.get("name") or workspace.name,
                    repo_meta.get("description") or "", repo_meta.get("defaultAuthor") or "Repository Owner",
                    utc_now(), utc_now(), repository_id,
                ),
            )
            active_id = self._get_state(connection, "active_repository_id")
            return self._row_to_public(
                connection, self._fetch_row(connection, repository_id), active_id=active_id
            )

    def repository_service(self, repository_id: str) -> ForgeTraceRepository:
        record = self.get_repository(repository_id)
        if record["status"] == "offline":
            raise ForgeTraceError(
                "Repository path is offline. Relink it or reconnect the drive.",
                HTTPStatus.SERVICE_UNAVAILABLE, "repository_offline", {"path": record["path"]},
            )
        if record["status"] == "invalid":
            raise ForgeTraceError(
                "Registered repository path is invalid.", HTTPStatus.CONFLICT,
                "repository_path_invalid",
            )
        service = ForgeTraceRepository(
            self.project_root, Path(record["path"]), repository_id,
            upload_limit_bytes=record["uploadLimitBytes"],
        )
        if record["status"] == "uninitialized":
            raise ForgeTraceError(
                "Repository is registered but not initialized.", HTTPStatus.CONFLICT,
                "repository_uninitialized",
            )
        service.ensure_identity(repository_id)
        return service

    def active_service(self) -> ForgeTraceRepository:
        repository_id = self.active_repository_id()
        if not repository_id:
            raise ForgeTraceError(
                "No active repository is selected.", HTTPStatus.CONFLICT,
                "active_repository_missing",
            )
        return self.repository_service(repository_id)

    def create_backup(self, label: str = "manual") -> dict[str, Any]:
        clean_label = "".join(ch for ch in str(label or "manual") if ch.isalnum() or ch in "-_")[:40] or "manual"
        stamp = utc_now().replace(":", "").replace("-", "")
        path = self.backups_dir / f"registry-{stamp}-{clean_label}-{uuid.uuid4().hex[:8]}.sqlite3"
        with self.lock:
            source = sqlite3.connect(self.db_path)
            destination = sqlite3.connect(path)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
        self._prune_backups(20)
        return {"path": str(path), "name": path.name, "bytes": path.stat().st_size, "createdAt": utc_now()}

    def _prune_backups(self, keep: int) -> None:
        backups = sorted(self.backups_dir.glob("registry-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in backups[keep:]:
            path.unlink(missing_ok=True)

    def list_backups(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.backups_dir.glob("registry-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True):
            result.append({"name": path.name, "path": str(path), "bytes": path.stat().st_size, "modified": path.stat().st_mtime})
        return result

    def export_registry(self) -> dict[str, Any]:
        with self.connect() as connection:
            repositories = []
            for row in connection.execute("SELECT * FROM repositories ORDER BY created_at"):
                record = dict(row)
                record["tags"] = self._tags_for(connection, row["id"])
                record["collection_ids"] = [item["id"] for item in self._collections_for(connection, row["id"])]
                repositories.append(record)
            collections = [dict(row) for row in connection.execute("SELECT * FROM collections ORDER BY created_at")]
            filters = [dict(row) for row in connection.execute("SELECT * FROM saved_filters ORDER BY created_at")]
            active_id = self._get_state(connection, "active_repository_id")
        return {
            "format": "forgetrace-registry-export",
            "version": 1,
            "applicationSchemaVersion": APP_SCHEMA_VERSION,
            "exportedAt": utc_now(),
            "activeRepositoryId": active_id,
            "repositories": repositories,
            "collections": collections,
            "savedFilters": filters,
        }

    def import_registry(self, payload: dict[str, Any], *, update_paths: bool = False) -> dict[str, Any]:
        if payload.get("format") != "forgetrace-registry-export":
            raise ForgeTraceError("Unsupported registry export format.", code="invalid_registry_export")
        repositories = payload.get("repositories")
        collections = payload.get("collections", [])
        filters = payload.get("savedFilters", [])
        if not isinstance(repositories, list) or not isinstance(collections, list) or not isinstance(filters, list):
            raise ForgeTraceError("Registry export contains invalid collections.", code="invalid_registry_export")
        backup = self.create_backup("pre-import")
        report = {"added": 0, "updated": 0, "skipped": 0, "collections": 0, "filters": 0, "conflicts": [], "backup": backup}
        with self.lock, self.connect() as connection:
            for item in collections:
                try:
                    collection_id = str(item.get("id") or uuid.uuid4())
                    name = " ".join(str(item.get("name") or "").strip().split())
                    if not name:
                        continue
                    existing = connection.execute(
                        "SELECT id FROM collections WHERE id = ? OR name = ? COLLATE NOCASE",
                        (collection_id, name),
                    ).fetchone()
                    if existing:
                        connection.execute(
                            "UPDATE collections SET description = ?, color = ?, updated_at = ? WHERE id = ?",
                            (str(item.get("description") or ""), str(item.get("color") or "")[:32], utc_now(), existing["id"]),
                        )
                    else:
                        now = utc_now()
                        connection.execute(
                            "INSERT INTO collections(id, name, description, color, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (collection_id, name, str(item.get("description") or ""), str(item.get("color") or "")[:32], now, now),
                        )
                    report["collections"] += 1
                except (sqlite3.IntegrityError, AttributeError) as exc:
                    report["conflicts"].append({"type": "collection", "item": item, "error": str(exc)})

            collection_name_to_id = {
                row["name"].casefold(): row["id"]
                for row in connection.execute("SELECT id, name FROM collections")
            }
            imported_collection_map = {
                str(item.get("id")): collection_name_to_id.get(str(item.get("name") or "").casefold(), str(item.get("id")))
                for item in collections if isinstance(item, dict)
            }

            for item in repositories:
                if not isinstance(item, dict):
                    report["skipped"] += 1
                    continue
                repository_id = str(item.get("id") or "").strip()
                raw_path = str(item.get("path") or "").strip()
                if not repository_id or not raw_path:
                    report["skipped"] += 1
                    continue
                try:
                    display_path, canonical_path = normalize_repository_path(raw_path)
                except ValueError as exc:
                    report["conflicts"].append({"type": "repository", "id": repository_id, "error": str(exc)})
                    report["skipped"] += 1
                    continue
                existing_id = connection.execute("SELECT * FROM repositories WHERE id = ?", (repository_id,)).fetchone()
                path_owner = connection.execute("SELECT id FROM repositories WHERE canonical_path = ?", (canonical_path,)).fetchone()
                if path_owner and path_owner["id"] != repository_id:
                    report["conflicts"].append({
                        "type": "repository", "id": repository_id,
                        "error": "Path is already owned by another registered repository.",
                        "owner": path_owner["id"],
                    })
                    report["skipped"] += 1
                    continue
                limit = self._validate_upload_limit(item.get("upload_limit_bytes", MAX_REQUEST_BYTES))
                now = utc_now()
                if existing_id:
                    path_values = (display_path, canonical_path) if update_paths else (existing_id["path"], existing_id["canonical_path"])
                    connection.execute(
                        """
                        UPDATE repositories SET name = ?, description = ?, path = ?, canonical_path = ?,
                            default_author = ?, favorite = ?, upload_limit_bytes = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            str(item.get("name") or existing_id["name"]), str(item.get("description") or ""),
                            path_values[0], path_values[1], str(item.get("default_author") or "Repository Owner"),
                            1 if item.get("favorite") else 0, limit, now, repository_id,
                        ),
                    )
                    report["updated"] += 1
                else:
                    connection.execute(
                        """
                        INSERT INTO repositories(
                            id, name, description, path, canonical_path, metadata_mode, metadata_path,
                            default_author, favorite, upload_limit_bytes, created_at, updated_at, last_opened_at
                        ) VALUES (?, ?, ?, ?, ?, 'embedded', '', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            repository_id, str(item.get("name") or Path(display_path).name or "Repository"),
                            str(item.get("description") or ""), display_path, canonical_path,
                            str(item.get("default_author") or "Repository Owner"), 1 if item.get("favorite") else 0,
                            limit, str(item.get("created_at") or now), now, str(item.get("last_opened_at") or ""),
                        ),
                    )
                    report["added"] += 1
                connection.execute("DELETE FROM repository_tags WHERE repository_id = ?", (repository_id,))
                for tag in item.get("tags", []):
                    clean_tag = self._clean_tag(tag)
                    if clean_tag:
                        connection.execute(
                            "INSERT OR IGNORE INTO repository_tags(repository_id, tag, created_at) VALUES (?, ?, ?)",
                            (repository_id, clean_tag, now),
                        )
                connection.execute("DELETE FROM repository_collections WHERE repository_id = ?", (repository_id,))
                for imported_id in item.get("collection_ids", []):
                    collection_id = imported_collection_map.get(str(imported_id), str(imported_id))
                    if connection.execute("SELECT 1 FROM collections WHERE id = ?", (collection_id,)).fetchone():
                        connection.execute(
                            "INSERT OR IGNORE INTO repository_collections(repository_id, collection_id, added_at) VALUES (?, ?, ?)",
                            (repository_id, collection_id, now),
                        )

            for item in filters:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                raw_query = item.get("query_json")
                if not name or not isinstance(raw_query, str):
                    continue
                existing = connection.execute(
                    "SELECT id FROM saved_filters WHERE id = ? OR name = ? COLLATE NOCASE",
                    (str(item.get("id") or ""), name),
                ).fetchone()
                if existing:
                    connection.execute(
                        "UPDATE saved_filters SET query_json = ?, updated_at = ? WHERE id = ?",
                        (raw_query, utc_now(), existing["id"]),
                    )
                else:
                    now = utc_now()
                    connection.execute(
                        "INSERT INTO saved_filters(id, name, query_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (str(item.get("id") or uuid.uuid4()), name, raw_query, now, now),
                    )
                report["filters"] += 1

            active_id = str(payload.get("activeRepositoryId") or "")
            current_active = self._get_state(connection, "active_repository_id")
            if not current_active and active_id and connection.execute(
                "SELECT 1 FROM repositories WHERE id = ?", (active_id,)
            ).fetchone():
                self._set_state(connection, "active_repository_id", active_id)
        return report

    def _startup_recovery_roots(self) -> list[Path]:
        roots = [
            self.managed_repositories_dir,
            self.data_dir / "repositories",
            self.project_root / "workspace",
            self.project_root / "managed-repositories",
        ]
        # Older extracted packages sometimes kept workspaces next to the executable.
        # Inspect only ForgeTrace-named siblings and known child directories; never scan
        # an entire Downloads/home directory recursively.
        try:
            for sibling in self.project_root.parent.glob("ForgeTrace*"):
                if not sibling.is_dir() or sibling.resolve() == self.project_root:
                    continue
                roots.extend([
                    sibling / "workspace",
                    sibling / "managed-repositories",
                    sibling / "data" / "managed-repositories",
                    sibling / "app-data" / "managed-repositories",
                ])
        except OSError:
            pass
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            try:
                resolved = root.expanduser().resolve()
            except OSError:
                continue
            key = os.path.normcase(os.path.normpath(str(resolved)))
            if key not in seen:
                unique.append(resolved)
                seen.add(key)
        return unique

    def recover_startup_repositories(self) -> dict[str, Any]:
        """Repopulate/relink the registry from UUID-bearing managed repositories."""
        roots = self._startup_recovery_roots()
        discovered = self._scan_embedded_repositories(roots, max_depth=6)
        by_id: dict[str, list[dict[str, Any]]] = {}
        for item in discovered:
            by_id.setdefault(str(item.get("id") or ""), []).append(item)
        report: dict[str, Any] = {
            "scannedRoots": [str(root) for root in roots if root.exists()],
            "discovered": len(discovered),
            "registered": 0,
            "relinked": 0,
            "skipped": 0,
            "ambiguous": 0,
            "errors": [],
        }
        with self.connect() as connection:
            rows = connection.execute("SELECT id, path, canonical_path FROM repositories").fetchall()
            existing_by_id = {str(row["id"]): dict(row) for row in rows}
            existing_paths = {str(row["canonical_path"]) for row in rows}

        def path_identity(path: Path) -> str:
            state_path = path / ".forgetrace" / "state.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                return str(state.get("repository", {}).get("id") or "").strip()
            except (OSError, json.JSONDecodeError):
                return ""

        handled_ids: set[str] = set()
        for repository_id, candidates in by_id.items():
            if not repository_id:
                continue
            existing = existing_by_id.get(repository_id)
            if existing:
                old_path = Path(str(existing["path"]))
                old_matches = path_identity(old_path) == repository_id
                distinct = []
                for item in candidates:
                    display_path, canonical = normalize_repository_path(item["path"])
                    if canonical != str(existing["canonical_path"]):
                        distinct.append((item, display_path, canonical))
                if not old_matches and len(distinct) == 1:
                    _item, display_path, _canonical = distinct[0]
                    try:
                        self.relink(repository_id, display_path)
                        report["relinked"] += 1
                    except Exception as exc:
                        report["errors"].append({"path": display_path, "error": str(exc)})
                elif not old_matches and len(distinct) > 1:
                    report["ambiguous"] += 1
                    report["errors"].append({
                        "repositoryId": repository_id,
                        "error": "Multiple UUID-matching repository paths were discovered; automatic relink was withheld.",
                        "paths": [item[1] for item in distinct],
                    })
                else:
                    report["skipped"] += 1
                handled_ids.add(repository_id)
                continue
            if len(candidates) > 1:
                report["ambiguous"] += 1
                report["errors"].append({
                    "repositoryId": repository_id,
                    "error": "Multiple copies of an unregistered repository UUID were discovered.",
                    "paths": [str(item["path"]) for item in candidates],
                })
                continue
            item = candidates[0]
            display_path, canonical_path = normalize_repository_path(item["path"])
            if canonical_path in existing_paths:
                report["skipped"] += 1
                continue
            try:
                record = self.register_repository(
                    path=display_path,
                    name=str(item.get("name") or Path(display_path).name),
                    description=str(item.get("description") or ""),
                    author=str(item.get("defaultAuthor") or "Repository Owner"),
                    initialize=False,
                    create_directory=False,
                )
                existing_paths.add(canonical_path)
                existing_by_id[record["id"]] = {"id": record["id"], "path": record["path"], "canonical_path": canonical_path}
                report["registered"] += 1
            except Exception as exc:
                report["errors"].append({"path": display_path, "error": str(exc)})
        try:
            with self.connect() as connection:
                self._set_state(connection, "last_startup_recovery", json.dumps(report, ensure_ascii=False))
        except Exception:
            pass
        return report

    @staticmethod
    def _scan_embedded_repositories(roots: list[Path], max_depth: int = 8) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        ignored = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}
        seen: set[str] = set()
        for root in roots:
            root = root.expanduser().resolve()
            if not root.exists() or not root.is_dir():
                continue
            for current, dirs, files in os.walk(root):
                current_path = Path(current)
                try:
                    depth = len(current_path.relative_to(root).parts)
                except ValueError:
                    continue
                dirs[:] = [name for name in dirs if name not in ignored]
                if depth >= max_depth:
                    dirs[:] = []
                if current_path.name == ".forgetrace" and "state.json" in files:
                    state_path = current_path / "state.json"
                    try:
                        state = json.loads(state_path.read_text(encoding="utf-8"))
                        meta = state.get("repository", {})
                        repository_id = str(meta.get("id") or "").strip()
                        workspace = current_path.parent.resolve()
                        canonical = os.path.normcase(os.path.normpath(str(workspace)))
                        if repository_id and canonical not in seen:
                            found.append({
                                "id": repository_id, "path": str(workspace),
                                "name": str(meta.get("name") or workspace.name),
                                "description": str(meta.get("description") or ""),
                                "defaultAuthor": str(meta.get("defaultAuthor") or "Repository Owner"),
                            })
                            seen.add(canonical)
                    except (OSError, json.JSONDecodeError):
                        pass
                    dirs[:] = []
        return found

    def _restore_repository_state_backup(self, row: Any, state_path: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        backup_path = state_path.with_suffix(".json.bak")
        if not backup_path.is_file():
            return None, {"valid": False, "reason": "backup_missing"}
        try:
            payload = json.loads(backup_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, {"valid": False, "reason": "backup_unreadable", "message": str(exc)}
        stored_id = str(payload.get("repository", {}).get("id") or "")
        if stored_id != str(row["id"]):
            return None, {"valid": False, "reason": "backup_identity_mismatch", "found": stored_id}
        if not isinstance(payload.get("contributions"), list) or not isinstance(payload.get("commits"), list):
            return None, {"valid": False, "reason": "backup_schema_invalid"}
        return payload, {"valid": True, "path": str(backup_path), "revision": int(payload.get("revision") or 0)}

    def doctor(self, *, repair: bool = False, scan_roots: list[Path] | None = None) -> dict[str, Any]:
        scan_roots = scan_roots or []
        issues: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        backup = self.create_backup("pre-doctor") if repair else None
        with self.lock, self.connect() as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            active_id = self._get_state(connection, "active_repository_id")
            rows = connection.execute("SELECT * FROM repositories ORDER BY name COLLATE NOCASE").fetchall()
        if integrity.lower() != "ok":
            issues.append({"severity": "critical", "code": "sqlite_integrity", "message": integrity})
        if active_id and not any(str(row["id"]) == active_id for row in rows):
            issues.append({"severity": "error", "code": "invalid_active_repository", "repositoryId": active_id})
            if repair:
                with self.lock, self.connect() as connection:
                    self._set_state(connection, "active_repository_id", "")
                actions.append({"action": "cleared_invalid_active_repository", "repositoryId": active_id})

        for row in rows:
            status, message = self._status_for_path(row["path"])
            if status != "online":
                issues.append({
                    "severity": "warning" if status == "offline" else "error",
                    "code": f"repository_{status}", "repositoryId": row["id"],
                    "path": row["path"], "message": message,
                })
                continue
            state_path = Path(row["path"]) / ".forgetrace" / "state.json"
            state: dict[str, Any] | None = None
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                backup_state, backup_info = self._restore_repository_state_backup(row, state_path)
                issues.append({
                    "severity": "critical", "code": "metadata_unreadable",
                    "repositoryId": row["id"], "path": str(state_path), "message": str(exc),
                    "backupAvailable": bool(backup_info.get("valid")), "backup": backup_info,
                })
                if repair and backup_state is not None:
                    corrupt_copy = state_path.with_name(f"state.corrupt-{utc_now().replace(':','').replace('-','')}.json")
                    try:
                        shutil.copy2(state_path, corrupt_copy)
                    except OSError:
                        pass
                    temp = state_path.with_name(f"state-recovery-{uuid.uuid4().hex}.tmp")
                    temp.write_text(json.dumps(backup_state, indent=2, ensure_ascii=False), encoding="utf-8")
                    os.replace(temp, state_path)
                    state = backup_state
                    actions.append({"action": "restored_repository_metadata_backup", "repositoryId": row["id"], "path": str(state_path), "corruptCopy": str(corrupt_copy)})
                else:
                    continue
            meta = state.get("repository", {}) if state else {}
            stored_id = str(meta.get("id") or "")
            if stored_id != row["id"]:
                issues.append({"severity": "critical", "code": "repository_identity_mismatch", "repositoryId": row["id"], "found": stored_id, "path": row["path"]})
                continue
            desired = {
                "name": str(meta.get("name") or row["name"]),
                "description": str(meta.get("description") or ""),
                "default_author": str(meta.get("defaultAuthor") or "Repository Owner"),
            }
            if any(str(row[key]) != value for key, value in desired.items()):
                issues.append({"severity": "info", "code": "registry_metadata_drift", "repositoryId": row["id"], "path": row["path"]})
                if repair:
                    with self.lock, self.connect() as connection:
                        connection.execute(
                            "UPDATE repositories SET name = ?, description = ?, default_author = ?, updated_at = ? WHERE id = ?",
                            (desired["name"], desired["description"], desired["default_author"], utc_now(), row["id"]),
                        )
                    actions.append({"action": "synchronized_repository_metadata", "repositoryId": row["id"]})

            service = ForgeTraceRepository(self.project_root, Path(row["path"]), row["id"], upload_limit_bytes=int(row["upload_limit_bytes"]))
            for recovery in service._recovery_actions:
                actions.append({"action": recovery.get("action", "repository_recovery"), "repositoryId": row["id"], **recovery})
            for commit in (state or {}).get("commits", []):
                verification = service.verify_snapshot_objects(commit)
                if verification["valid"]:
                    continue
                issue = {
                    "severity": "critical", "code": "snapshot_object_integrity",
                    "repositoryId": row["id"], "commitId": commit.get("id"),
                    "errors": verification["errors"],
                }
                issues.append(issue)
                if repair:
                    repaired = 0
                    for rel, data in commit.get("manifest", {}).items():
                        expected = str(data.get("hash") or "")
                        live = Path(row["path"]) / rel
                        if not live.is_file():
                            continue
                        hasher = hashlib.sha256()
                        with live.open("rb") as handle:
                            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                                hasher.update(chunk)
                        if hasher.hexdigest() != expected:
                            continue
                        object_path = service.object_path(expected)
                        object_path.parent.mkdir(parents=True, exist_ok=True)
                        temp = object_path.with_name(f".{object_path.name}.{uuid.uuid4().hex}.repair.tmp")
                        shutil.copy2(live, temp)
                        os.replace(temp, object_path)
                        repaired += 1
                    if repaired:
                        actions.append({"action": "reconstructed_snapshot_objects", "repositoryId": row["id"], "commitId": commit.get("id"), "count": repaired})

        with self.connect() as connection:
            registered_ids = {str(row["id"]) for row in connection.execute("SELECT id FROM repositories")}
            registered_paths = {str(row["canonical_path"]) for row in connection.execute("SELECT canonical_path FROM repositories")}
        discovered = self._scan_embedded_repositories(scan_roots)
        for item in discovered:
            _, canonical = normalize_repository_path(item["path"])
            if item["id"] in registered_ids or canonical in registered_paths:
                continue
            issues.append({"severity": "info", "code": "unregistered_repository_discovered", "repositoryId": item["id"], "path": item["path"], "name": item["name"]})
            if repair:
                now = utc_now()
                with self.lock, self.connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO repositories(
                            id, name, description, path, canonical_path, metadata_mode, metadata_path,
                            default_author, favorite, upload_limit_bytes, created_at, updated_at, last_opened_at
                        ) VALUES (?, ?, ?, ?, ?, 'embedded', '', ?, 0, ?, ?, ?, '')
                        """,
                        (item["id"], item["name"], item["description"], item["path"], canonical, item["defaultAuthor"], MAX_REQUEST_BYTES, now, now),
                    )
                registered_ids.add(item["id"])
                registered_paths.add(canonical)
                actions.append({"action": "registered_discovered_repository", "repositoryId": item["id"], "path": item["path"]})
        with self.connect() as connection:
            repository_count = connection.execute("SELECT COUNT(*) FROM repositories").fetchone()[0]
        critical = sum(1 for issue in issues if issue["severity"] == "critical")
        errors = sum(1 for issue in issues if issue["severity"] == "error")
        warnings = sum(1 for issue in issues if issue["severity"] == "warning")
        return {
            "healthy": integrity.lower() == "ok" and critical == 0 and errors == 0,
            "integrity": integrity,
            "repositoryCount": repository_count,
            "issues": issues,
            "actions": actions,
            "summary": {"critical": critical, "errors": errors, "warnings": warnings, "total": len(issues)},
            "backup": backup,
            "checkedAt": utc_now(),
        }
