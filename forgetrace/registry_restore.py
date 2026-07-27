from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .constants import (
    APP_SCHEMA_VERSION,
    MAX_REQUEST_BYTES,
    REPOSITORY_ACCESS_READ_WRITE,
    normalize_repository_access_mode,
)
from .errors import ForgeTraceError
from .utils import utc_now

if TYPE_CHECKING:  # pragma: no cover
    from .registry import RepositoryRegistry


RESTORE_JOURNAL_FORMAT = "forgetrace-registry-restore-journal"
RESTORE_JOURNAL_VERSION = 1
RESTORE_MODES = {"merge", "replace"}
BACKUP_NAME_PATTERN = re.compile(r"registry-[A-Za-z0-9_.-]+\.sqlite3\Z")
RESTORE_ID_PATTERN = re.compile(r"restore_[0-9a-f]{32}\Z")
REQUIRED_REGISTRY_TABLES = {
    "application_state",
    "collections",
    "repositories",
    "repository_collections",
    "repository_tags",
    "saved_filters",
    "schema_migrations",
}
CANONICAL_TABLE_ORDER: tuple[tuple[str, str], ...] = (
    ("schema_migrations", "version"),
    ("application_state", "key"),
    ("repositories", "id"),
    ("repository_tags", "repository_id, tag COLLATE NOCASE"),
    ("collections", "id"),
    ("repository_collections", "repository_id, collection_id"),
    ("saved_filters", "id"),
)


class RegistryRestoreService:
    """Validated, journaled recovery authority for registry SQLite backups.

    The service never touches repository workspaces or the separate security-event
    ledger. Every restore is prepared in application-data staging, validated before
    mutation, installed under the registry operation lock, and backed by an exact
    pre-restore SQLite backup that can be used for automatic or explicit rollback.
    """

    def __init__(self, registry: "RepositoryRegistry") -> None:
        self.registry = registry
        self.root = registry.data_dir / "registry-restores"
        self.staging_dir = self.root / "staging"
        self.journals_dir = self.root / "journals"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.journals_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sha256_file(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def _fsync_file(path: Path) -> None:
        try:
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        except OSError:
            pass

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

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        self._fsync_directory(path.parent)

    @staticmethod
    def _open_database(path: Path, *, readonly: bool) -> sqlite3.Connection:
        if readonly:
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0)
        else:
            connection = sqlite3.connect(path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if not readonly:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _table_names(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    @staticmethod
    def _migration_version(connection: sqlite3.Connection) -> int:
        tables = RegistryRestoreService._table_names(connection)
        if "schema_migrations" not in tables:
            return 0
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    @staticmethod
    def _application_schema_version(connection: sqlite3.Connection) -> int:
        tables = RegistryRestoreService._table_names(connection)
        if "application_state" not in tables:
            return RegistryRestoreService._migration_version(connection)
        row = connection.execute(
            "SELECT value FROM application_state WHERE key = 'schema_version'"
        ).fetchone()
        if not row:
            return RegistryRestoreService._migration_version(connection)
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _integrity(connection: sqlite3.Connection) -> tuple[str, list[dict[str, Any]]]:
        rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        integrity = "ok" if rows == ["ok"] else "; ".join(rows)
        foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
        return integrity, foreign_keys

    def _resolve_backup(self, backup_name: Any) -> Path:
        raw = str(backup_name or "").strip()
        if not raw or Path(raw).name != raw or not BACKUP_NAME_PATTERN.fullmatch(raw):
            raise ForgeTraceError(
                "Select a ForgeTrace registry backup by its backup name.",
                code="invalid_registry_backup_name",
            )
        candidate = self.registry.backups_dir / raw
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ForgeTraceError(
                "Registry backup was not found.",
                404,
                "registry_backup_not_found",
                {"backupName": raw},
            ) from exc
        if resolved.parent != self.registry.backups_dir.resolve() or candidate.is_symlink() or not resolved.is_file():
            raise ForgeTraceError(
                "Registry backup path is not eligible for restore.",
                code="invalid_registry_backup_path",
            )
        return resolved

    @staticmethod
    def _validate_mode(mode: Any) -> str:
        value = str(mode or "").strip().lower()
        if value not in RESTORE_MODES:
            raise ForgeTraceError(
                "Registry restore mode must be merge or replace.",
                code="invalid_registry_restore_mode",
            )
        return value

    def _preflight_source(self, source: Path) -> dict[str, Any]:
        try:
            connection = self._open_database(source, readonly=True)
        except sqlite3.Error as exc:
            raise ForgeTraceError(
                "Registry backup could not be opened as SQLite.",
                code="registry_backup_unreadable",
                details={"backupName": source.name},
            ) from exc
        try:
            tables = self._table_names(connection)
            if not {"schema_migrations", "application_state", "repositories"}.issubset(tables):
                raise ForgeTraceError(
                    "Registry backup is missing required ForgeTrace tables.",
                    code="registry_backup_schema_invalid",
                    details={"backupName": source.name},
                )
            migration_version = self._migration_version(connection)
            application_schema = self._application_schema_version(connection)
            if migration_version <= 0 or application_schema <= 0:
                raise ForgeTraceError(
                    "Registry backup does not declare a supported schema version.",
                    code="registry_backup_schema_invalid",
                    details={"backupName": source.name},
                )
            supported_migration = max(version for version, _name, _sql in self.registry.migrations)
            if migration_version > supported_migration or application_schema > APP_SCHEMA_VERSION:
                raise ForgeTraceError(
                    "Registry backup was created by a newer, unsupported ForgeTrace schema.",
                    409,
                    "registry_backup_schema_newer",
                    {
                        "backupSchemaVersion": application_schema,
                        "backupMigrationVersion": migration_version,
                        "supportedSchemaVersion": APP_SCHEMA_VERSION,
                        "supportedMigrationVersion": supported_migration,
                    },
                )
            integrity, foreign_keys = self._integrity(connection)
            if integrity != "ok" or foreign_keys:
                raise ForgeTraceError(
                    "Registry backup failed SQLite integrity verification.",
                    409,
                    "registry_backup_integrity_failed",
                    {
                        "backupName": source.name,
                        "integrity": integrity,
                        "foreignKeyErrors": len(foreign_keys),
                    },
                )
            return {
                "schemaVersion": application_schema,
                "migrationVersion": migration_version,
                "integrity": integrity,
                "foreignKeyErrors": len(foreign_keys),
            }
        except sqlite3.Error as exc:
            raise ForgeTraceError(
                "Registry backup validation failed.",
                409,
                "registry_backup_integrity_failed",
                {"backupName": source.name, "reason": str(exc)},
            ) from exc
        finally:
            connection.close()

    def _prepare_database(
        self, source: Path, destination: Path, *, expected_source_sha256: str = ""
    ) -> dict[str, Any]:
        source_info = self._preflight_source(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        self._fsync_file(destination)
        copied_sha256 = self._sha256_file(destination)
        if expected_source_sha256 and copied_sha256 != expected_source_sha256:
            raise ForgeTraceError(
                "The selected registry backup changed while it was being staged.",
                409,
                "registry_restore_preview_stale",
            )
        connection = self._open_database(destination, readonly=False)
        try:
            applied = self.registry._apply_migrations(connection)
            self.registry._backfill_legacy_organization_connection(connection)
            connection.commit()
            integrity, foreign_keys = self._integrity(connection)
            tables = self._table_names(connection)
            missing = sorted(REQUIRED_REGISTRY_TABLES - tables)
            schema_version = self._application_schema_version(connection)
            migration_version = self._migration_version(connection)
            supported_migration = max(version for version, _name, _sql in self.registry.migrations)
            if (
                integrity != "ok"
                or foreign_keys
                or missing
                or schema_version != APP_SCHEMA_VERSION
                or migration_version != supported_migration
            ):
                raise ForgeTraceError(
                    "Prepared registry restore failed post-migration verification.",
                    409,
                    "registry_restore_integrity_failed",
                    {
                        "integrity": integrity,
                        "foreignKeyErrors": len(foreign_keys),
                        "missingTables": missing,
                        "schemaVersion": schema_version,
                        "migrationVersion": migration_version,
                    },
                )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        for suffix in ("-wal", "-shm", "-journal"):
            destination.with_name(destination.name + suffix).unlink(missing_ok=True)
        self._fsync_file(destination)
        return {
            "sourceSchemaVersion": source_info["schemaVersion"],
            "preparedSchemaVersion": APP_SCHEMA_VERSION,
            "preparedMigrationVersion": max(version for version, _name, _sql in self.registry.migrations),
            "migrationsApplied": applied,
            "sourceSha256": copied_sha256,
        }

    @staticmethod
    def _canonical_rows(connection: sqlite3.Connection, table: str, order_by: str) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order_by}")]

    def _inspect_connection(self, connection: sqlite3.Connection) -> dict[str, Any]:
        tables = self._table_names(connection)
        missing = sorted(REQUIRED_REGISTRY_TABLES - tables)
        if missing:
            raise ForgeTraceError(
                "Registry database is missing required tables.",
                409,
                "registry_restore_integrity_failed",
                {"missingTables": missing},
            )
        integrity, foreign_keys = self._integrity(connection)
        if integrity != "ok" or foreign_keys:
            raise ForgeTraceError(
                "Registry database failed integrity verification.",
                409,
                "registry_restore_integrity_failed",
                {"integrity": integrity, "foreignKeyErrors": len(foreign_keys)},
            )
        canonical = {
            table: self._canonical_rows(connection, table, order_by)
            for table, order_by in CANONICAL_TABLE_ORDER
        }
        # Schema application timestamps are migration bookkeeping rather than user
        # registry state. Excluding them makes previews deterministic for an older
        # backup that must be staged and migrated more than once.
        canonical["schema_migrations"] = [
            {"version": item.get("version"), "name": item.get("name")}
            for item in canonical["schema_migrations"]
        ]
        canonical["application_state"] = [
            {"key": item.get("key"), "value": item.get("value")}
            for item in canonical["application_state"]
        ]
        canonical_bytes = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        logical_digest = hashlib.sha256(canonical_bytes).hexdigest()
        repositories = {str(item["id"]): item for item in canonical["repositories"]}
        collections = {str(item["id"]): item for item in canonical["collections"]}
        filters = {str(item["id"]): item for item in canonical["saved_filters"]}
        tags_by_repository: dict[str, list[str]] = {}
        for item in canonical["repository_tags"]:
            tags_by_repository.setdefault(str(item["repository_id"]), []).append(str(item["tag"]))
        collections_by_repository: dict[str, list[str]] = {}
        for item in canonical["repository_collections"]:
            collections_by_repository.setdefault(str(item["repository_id"]), []).append(str(item["collection_id"]))
        for repository_id, item in repositories.items():
            item["_tags"] = sorted(tags_by_repository.get(repository_id, []), key=str.casefold)
            item["_collection_ids"] = sorted(collections_by_repository.get(repository_id, []))
        active_id = ""
        for item in canonical["application_state"]:
            if item.get("key") == "active_repository_id":
                active_id = str(item.get("value") or "")
                break
        path_status = {"online": 0, "offline": 0, "invalid": 0, "uninitialized": 0}
        for item in repositories.values():
            status, _message = self.registry._status_for_path(str(item.get("path") or ""))
            path_status[status] = path_status.get(status, 0) + 1
        return {
            "integrity": integrity,
            "foreignKeyErrors": len(foreign_keys),
            "schemaVersion": self._application_schema_version(connection),
            "migrationVersion": self._migration_version(connection),
            "logicalDigest": logical_digest,
            "repositoryCount": len(repositories),
            "collectionCount": len(collections),
            "savedFilterCount": len(filters),
            "activeRepositoryId": active_id,
            "pathStatus": path_status,
            "repositories": repositories,
            "collections": collections,
            "savedFilters": filters,
        }

    def _inspect_path(self, path: Path) -> dict[str, Any]:
        connection = self._open_database(path, readonly=True)
        try:
            return self._inspect_connection(connection)
        finally:
            connection.close()

    def _inspect_live(self) -> dict[str, Any]:
        with self.registry.connect() as connection:
            return self._inspect_connection(connection)

    @staticmethod
    def _public_inspection(inspection: dict[str, Any]) -> dict[str, Any]:
        return {
            "integrity": inspection["integrity"],
            "foreignKeyErrors": inspection["foreignKeyErrors"],
            "schemaVersion": inspection["schemaVersion"],
            "migrationVersion": inspection["migrationVersion"],
            "logicalDigest": inspection["logicalDigest"],
            "repositoryCount": inspection["repositoryCount"],
            "collectionCount": inspection["collectionCount"],
            "savedFilterCount": inspection["savedFilterCount"],
            "activeRepositoryId": inspection["activeRepositoryId"],
            "pathStatus": inspection["pathStatus"],
        }

    @staticmethod
    def _repository_preview(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "path": str(item.get("path") or ""),
            "accessMode": normalize_repository_access_mode(
                item.get("access_mode", REPOSITORY_ACCESS_READ_WRITE), fail_closed=True
            ),
        }

    @staticmethod
    def _repository_compare_record(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: item.get(key)
            for key in (
                "name",
                "description",
                "path",
                "canonical_path",
                "metadata_mode",
                "metadata_path",
                "default_author",
                "favorite",
                "upload_limit_bytes",
                "access_mode",
                "_tags",
                "_collection_ids",
            )
        }

    def _impact(self, current: dict[str, Any], restored: dict[str, Any], mode: str) -> dict[str, Any]:
        current_repositories = current["repositories"]
        restored_repositories = restored["repositories"]
        current_ids = set(current_repositories)
        restored_ids = set(restored_repositories)
        current_path_owners = {
            str(item.get("canonical_path") or ""): repository_id
            for repository_id, item in current_repositories.items()
        }
        added: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for repository_id in sorted(restored_ids - current_ids):
            item = restored_repositories[repository_id]
            owner = current_path_owners.get(str(item.get("canonical_path") or ""))
            if mode == "merge" and owner and owner != repository_id:
                conflicts.append(
                    {
                        "type": "repository_path",
                        "repository": self._repository_preview(item),
                        "existingRepositoryId": owner,
                    }
                )
            else:
                added.append(self._repository_preview(item))
        removed = [
            self._repository_preview(current_repositories[repository_id])
            for repository_id in sorted(current_ids - restored_ids)
        ]
        changed = [
            self._repository_preview(restored_repositories[repository_id])
            for repository_id in sorted(current_ids & restored_ids)
            if self._repository_compare_record(current_repositories[repository_id])
            != self._repository_compare_record(restored_repositories[repository_id])
        ]
        current_collection_names = {
            str(item.get("name") or "").casefold() for item in current["collections"].values()
        }
        current_filter_names = {
            str(item.get("name") or "").casefold() for item in current["savedFilters"].values()
        }
        new_collections = sum(
            1
            for collection_id, item in restored["collections"].items()
            if collection_id not in current["collections"]
            and str(item.get("name") or "").casefold() not in current_collection_names
        )
        new_filters = sum(
            1
            for filter_id, item in restored["savedFilters"].items()
            if filter_id not in current["savedFilters"]
            and str(item.get("name") or "").casefold() not in current_filter_names
        )
        return {
            "repositoriesAdded": len(added),
            "repositoriesRemoved": len(removed) if mode == "replace" else 0,
            "repositoriesPreserved": len(current_ids & restored_ids) if mode == "merge" else 0,
            "repositoriesChanged": len(changed) if mode == "replace" else 0,
            "pathConflicts": len(conflicts),
            "collectionsAdded": new_collections if mode == "merge" else restored["collectionCount"],
            "savedFiltersAdded": new_filters if mode == "merge" else restored["savedFilterCount"],
            "activeRepositoryAction": (
                "replace"
                if mode == "replace" and current["activeRepositoryId"] != restored["activeRepositoryId"]
                else "adopt-if-empty"
                if mode == "merge" and not current["activeRepositoryId"] and restored["activeRepositoryId"]
                else "preserve"
            ),
            "addedRepositories": added[:100],
            "removedRepositories": removed[:100] if mode == "replace" else [],
            "changedRepositories": changed[:100] if mode == "replace" else [],
            "conflicts": conflicts[:100],
            "truncated": any(len(items) > 100 for items in (added, removed, changed, conflicts)),
        }

    def _new_stage(self, prefix: str) -> tuple[Path, Path]:
        directory = self.staging_dir / f"{prefix}-{uuid.uuid4().hex}"
        directory.mkdir(parents=True, exist_ok=False)
        return directory, directory / "registry.sqlite3"

    def _preview_locked(self, backup_name: Any, mode: Any) -> dict[str, Any]:
        selected_mode = self._validate_mode(mode)
        source = self._resolve_backup(backup_name)
        source_sha256 = self._sha256_file(source)
        stage_dir, staged = self._new_stage("preview")
        try:
            preparation = self._prepare_database(
                source, staged, expected_source_sha256=source_sha256
            )
            restored = self._inspect_path(staged)
            current = self._inspect_live()
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)
        impact = self._impact(current, restored, selected_mode)
        preview_material = {
            "backupName": source.name,
            "backupSha256": source_sha256,
            "mode": selected_mode,
            "currentDigest": current["logicalDigest"],
            "restoredDigest": restored["logicalDigest"],
            "schemaVersion": APP_SCHEMA_VERSION,
        }
        preview_id = hashlib.sha256(
            json.dumps(preview_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        warnings: list[str] = []
        if selected_mode == "replace" and impact["repositoriesRemoved"]:
            warnings.append(
                f"Replace will remove {impact['repositoriesRemoved']} registration(s) from the live registry. Repository folders are not deleted."
            )
        if impact["pathConflicts"]:
            warnings.append(
                f"Merge will skip {impact['pathConflicts']} repository registration(s) whose path is already owned by another ID."
            )
        unavailable = restored["pathStatus"].get("offline", 0) + restored["pathStatus"].get("invalid", 0)
        if unavailable:
            warnings.append(
                f"The backup references {unavailable} repository path(s) that are currently offline or invalid on this machine."
            )
        semantics = (
            "Add missing repositories, collections, and saved filters; union tags and collection memberships; preserve existing repository settings, paths, filters, collections, and active selection."
            if selected_mode == "merge"
            else "Replace registry.sqlite3 with the fully validated backup. Repository folders and the separate security-event ledger are not modified."
        )
        return {
            "format": "forgetrace-registry-restore-preview",
            "version": 1,
            "previewId": preview_id,
            "backupName": source.name,
            "backupSha256": source_sha256,
            "backupBytes": source.stat().st_size,
            "backupModified": source.stat().st_mtime,
            "mode": selected_mode,
            "semantics": semantics,
            "preparation": preparation,
            "current": self._public_inspection(current),
            "restored": self._public_inspection(restored),
            "impact": impact,
            "warnings": warnings,
            "canRestore": True,
            "previewedAt": utc_now(),
        }

    def preview(self, backup_name: Any, mode: Any) -> dict[str, Any]:
        with self.registry.lock, self.registry.operation_lock:
            return self._preview_locked(backup_name, mode)

    def _journal_path(self, restore_id: str) -> Path:
        if not RESTORE_ID_PATTERN.fullmatch(restore_id):
            raise ForgeTraceError("Registry restore record was not found.", 404, "registry_restore_not_found")
        return self.journals_dir / f"{restore_id}.json"

    def _write_journal(self, journal: dict[str, Any]) -> None:
        self._atomic_write_json(self._journal_path(str(journal["restoreId"])), journal)

    def _load_journal(self, restore_id: str) -> dict[str, Any]:
        path = self._journal_path(restore_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ForgeTraceError("Registry restore record was not found.", 404, "registry_restore_not_found") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ForgeTraceError(
                "Registry restore journal is unreadable.",
                409,
                "registry_restore_journal_invalid",
            ) from exc
        if (
            payload.get("format") != RESTORE_JOURNAL_FORMAT
            or int(payload.get("version", 0) or 0) != RESTORE_JOURNAL_VERSION
            or payload.get("restoreId") != restore_id
        ):
            raise ForgeTraceError(
                "Registry restore journal has an invalid format.",
                409,
                "registry_restore_journal_invalid",
            )
        return payload

    @staticmethod
    def _public_journal(journal: dict[str, Any]) -> dict[str, Any]:
        before = journal.get("before", {})
        after = journal.get("after", {})
        rollback = journal.get("rollback", {})
        return {
            "restoreId": journal.get("restoreId", ""),
            "backupName": journal.get("backupName", ""),
            "mode": journal.get("mode", ""),
            "state": journal.get("state", ""),
            "createdAt": journal.get("createdAt", ""),
            "completedAt": journal.get("completedAt", ""),
            "repositoryCountBefore": before.get("repositoryCount"),
            "repositoryCountAfter": after.get("repositoryCount"),
            "postRestoreDigest": after.get("logicalDigest", ""),
            "rollbackAvailable": bool(journal.get("rollbackAvailable")),
            "rollback": {
                "state": rollback.get("state", ""),
                "completedAt": rollback.get("completedAt", ""),
            },
            "recovery": journal.get("recovery", {}),
        }

    def protected_backup_names(self) -> set[str]:
        protected: set[str] = set()
        for path in self.journals_dir.glob("restore_*.json"):
            try:
                journal = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if journal.get("rollbackAvailable") or journal.get("state") in {
                "prepared", "installing", "installed"
            }:
                name = str(journal.get("preRestoreBackup", {}).get("name") or "")
                if BACKUP_NAME_PATTERN.fullmatch(name):
                    protected.add(name)
            rollback = journal.get("rollback", {})
            if rollback.get("state") == "installing":
                name = str(rollback.get("preRollbackBackup", {}).get("name") or "")
                if BACKUP_NAME_PATTERN.fullmatch(name):
                    protected.add(name)
        return protected

    def list_journals(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        with self.registry.lock, self.registry.operation_lock:
            paths = sorted(self.journals_dir.glob("restore_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
            for path in paths[:50]:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if payload.get("format") == RESTORE_JOURNAL_FORMAT:
                        result.append(self._public_journal(payload))
                except (OSError, json.JSONDecodeError):
                    result.append(
                        {
                            "restoreId": path.stem,
                            "state": "journal_unreadable",
                            "rollbackAvailable": False,
                        }
                    )
        return result

    def _checkpoint_live(self) -> None:
        connection = sqlite3.connect(self.registry.db_path, timeout=30.0)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        for suffix in ("-wal", "-shm", "-journal"):
            self.registry.db_path.with_name(self.registry.db_path.name + suffix).unlink(missing_ok=True)

    def _replace_live(self, prepared: Path) -> None:
        self._checkpoint_live()
        os.replace(prepared, self.registry.db_path)
        self._fsync_file(self.registry.db_path)
        self._fsync_directory(self.registry.db_path.parent)

    def _merge_prepared(self, prepared: Path) -> dict[str, Any]:
        source = self._open_database(prepared, readonly=True)
        report: dict[str, Any] = {
            "repositoriesAdded": 0,
            "repositoriesPreserved": 0,
            "pathConflicts": [],
            "collectionsAdded": 0,
            "collectionsPreserved": 0,
            "savedFiltersAdded": 0,
            "savedFiltersPreserved": 0,
            "tagsAdded": 0,
            "collectionMembershipsAdded": 0,
            "activeRepositoryAction": "preserved",
        }
        try:
            with self.registry.connect() as destination:
                collection_map: dict[str, str] = {}
                for item in source.execute("SELECT * FROM collections ORDER BY created_at, id"):
                    backup_id = str(item["id"])
                    existing = destination.execute(
                        "SELECT id FROM collections WHERE id = ? OR name = ? COLLATE NOCASE",
                        (backup_id, item["name"]),
                    ).fetchone()
                    if existing:
                        collection_map[backup_id] = str(existing["id"])
                        report["collectionsPreserved"] += 1
                    else:
                        destination.execute(
                            "INSERT INTO collections(id, name, description, color, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                backup_id,
                                str(item["name"]),
                                str(item["description"] or ""),
                                str(item["color"] or "")[:32],
                                str(item["created_at"] or utc_now()),
                                str(item["updated_at"] or utc_now()),
                            ),
                        )
                        collection_map[backup_id] = backup_id
                        report["collectionsAdded"] += 1

                existing_paths = {
                    str(row["canonical_path"]): str(row["id"])
                    for row in destination.execute("SELECT id, canonical_path FROM repositories")
                }
                existing_ids = {
                    str(row["id"])
                    for row in destination.execute("SELECT id FROM repositories")
                }
                added_ids: set[str] = set()
                preserved_ids: set[str] = set()
                for item in source.execute("SELECT * FROM repositories ORDER BY created_at, id"):
                    repository_id = str(item["id"])
                    canonical_path = str(item["canonical_path"])
                    if repository_id in existing_ids:
                        preserved_ids.add(repository_id)
                        report["repositoriesPreserved"] += 1
                        continue
                    owner = existing_paths.get(canonical_path)
                    if owner and owner != repository_id:
                        report["pathConflicts"].append(
                            {
                                "repositoryId": repository_id,
                                "name": str(item["name"]),
                                "path": str(item["path"]),
                                "existingRepositoryId": owner,
                            }
                        )
                        continue
                    metadata_mode = str(item["metadata_mode"] or "embedded")
                    if metadata_mode not in {"embedded", "external"}:
                        metadata_mode = "embedded"
                    upload_limit = self.registry._validate_upload_limit(
                        item["upload_limit_bytes"] if "upload_limit_bytes" in item.keys() else MAX_REQUEST_BYTES
                    )
                    destination.execute(
                        """
                        INSERT INTO repositories(
                            id, name, description, path, canonical_path, metadata_mode, metadata_path,
                            default_author, favorite, tags_json, collection_name, upload_limit_bytes, access_mode,
                            created_at, updated_at, last_opened_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            repository_id,
                            str(item["name"]),
                            str(item["description"] or ""),
                            str(item["path"]),
                            canonical_path,
                            metadata_mode,
                            str(item["metadata_path"] or ""),
                            str(item["default_author"] or "Repository Owner"),
                            1 if item["favorite"] else 0,
                            str(item["tags_json"] or "[]"),
                            str(item["collection_name"] or ""),
                            upload_limit,
                            normalize_repository_access_mode(
                                item["access_mode"] if "access_mode" in item.keys() else REPOSITORY_ACCESS_READ_WRITE,
                                fail_closed=True,
                            ),
                            str(item["created_at"] or utc_now()),
                            str(item["updated_at"] or utc_now()),
                            str(item["last_opened_at"] or ""),
                        ),
                    )
                    existing_ids.add(repository_id)
                    existing_paths[canonical_path] = repository_id
                    added_ids.add(repository_id)
                    report["repositoriesAdded"] += 1

                eligible_ids = added_ids | preserved_ids
                for item in source.execute("SELECT * FROM repository_tags ORDER BY repository_id, tag"):
                    repository_id = str(item["repository_id"])
                    if repository_id not in eligible_ids:
                        continue
                    before = destination.total_changes
                    destination.execute(
                        "INSERT OR IGNORE INTO repository_tags(repository_id, tag, created_at) VALUES (?, ?, ?)",
                        (repository_id, str(item["tag"]), str(item["created_at"] or utc_now())),
                    )
                    if destination.total_changes > before:
                        report["tagsAdded"] += 1
                for item in source.execute(
                    "SELECT * FROM repository_collections ORDER BY repository_id, collection_id"
                ):
                    repository_id = str(item["repository_id"])
                    if repository_id not in eligible_ids:
                        continue
                    mapped_collection = collection_map.get(str(item["collection_id"]))
                    if not mapped_collection:
                        continue
                    before = destination.total_changes
                    destination.execute(
                        "INSERT OR IGNORE INTO repository_collections(repository_id, collection_id, added_at) VALUES (?, ?, ?)",
                        (repository_id, mapped_collection, str(item["added_at"] or utc_now())),
                    )
                    if destination.total_changes > before:
                        report["collectionMembershipsAdded"] += 1

                for item in source.execute("SELECT * FROM saved_filters ORDER BY created_at, id"):
                    backup_id = str(item["id"])
                    existing = destination.execute(
                        "SELECT id FROM saved_filters WHERE id = ? OR name = ? COLLATE NOCASE",
                        (backup_id, item["name"]),
                    ).fetchone()
                    if existing:
                        report["savedFiltersPreserved"] += 1
                    else:
                        destination.execute(
                            "INSERT INTO saved_filters(id, name, query_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                            (
                                backup_id,
                                str(item["name"]),
                                str(item["query_json"]),
                                str(item["created_at"] or utc_now()),
                                str(item["updated_at"] or utc_now()),
                            ),
                        )
                        report["savedFiltersAdded"] += 1

                current_active = self.registry._get_state(destination, "active_repository_id")
                if not current_active:
                    source_active = self.registry._get_state(source, "active_repository_id")
                    if source_active and destination.execute(
                        "SELECT 1 FROM repositories WHERE id = ?", (source_active,)
                    ).fetchone():
                        self.registry._set_state(destination, "active_repository_id", source_active)
                        report["activeRepositoryAction"] = "adopted-from-backup"
        finally:
            source.close()
        report["pathConflictCount"] = len(report["pathConflicts"])
        return report


    def _reconcile_embedded_access_modes(self) -> dict[str, Any]:
        """Apply the installed registry mode to online embedded repository metadata.

        The registry database remains the restore authority, but every repository keeps
        a second copy so stale processes fail closed. Offline or unreadable repositories
        remain safely mismatched/read-only until an owner reconnects and applies a mode.
        """
        from .repository import ForgeTraceRepository

        connection = self._open_database(self.registry.db_path, readonly=True)
        try:
            rows = [dict(row) for row in connection.execute(
                "SELECT id, path, access_mode, upload_limit_bytes FROM repositories ORDER BY id"
            )]
        finally:
            connection.close()
        report: dict[str, Any] = {"checked": len(rows), "reconciled": 0, "unchanged": 0, "pending": []}
        for row in rows:
            repository_id = str(row.get("id") or "")
            workspace = Path(str(row.get("path") or "")).expanduser()
            target = normalize_repository_access_mode(row.get("access_mode"), fail_closed=True)
            state_path = workspace / ".forgetrace" / "state.json"
            if not workspace.is_dir() or not state_path.is_file():
                report["pending"].append({"repositoryId": repository_id, "reason": "repository_offline_or_uninitialized", "targetMode": target})
                continue
            try:
                service = ForgeTraceRepository(
                    self.registry.project_root, workspace, repository_id,
                    upload_limit_bytes=int(row.get("upload_limit_bytes") or MAX_REQUEST_BYTES),
                    access_mode_getter=lambda selected=target: selected,
                )
                service.ensure_identity(repository_id)
                state = service.load_state()
                policy = service.access_policy(state)
                if policy["embeddedValid"] and policy["embeddedMode"] == target and not state.get("_needsSchemaUpgrade"):
                    report["unchanged"] += 1
                    continue
                service.set_embedded_access_mode(target)
                report["reconciled"] += 1
            except Exception as exc:
                report["pending"].append({
                    "repositoryId": repository_id, "reason": "reconciliation_failed",
                    "targetMode": target, "error": str(exc),
                })
        report["pendingCount"] = len(report["pending"])
        return report

    def _restore_exact_from_backup(
        self, backup_name: str, expected_digest: str = "", expected_sha256: str = ""
    ) -> dict[str, Any]:
        source = self._resolve_backup(backup_name)
        stage_dir, staged = self._new_stage("rollback")
        try:
            self._prepare_database(
                source, staged, expected_source_sha256=expected_sha256
            )
            target = self._inspect_path(staged)
            if expected_digest and target["logicalDigest"] != expected_digest:
                raise ForgeTraceError(
                    "Rollback backup no longer matches the recorded pre-restore registry.",
                    409,
                    "registry_restore_rollback_digest_mismatch",
                )
            self._replace_live(staged)
            access_reconciliation = self._reconcile_embedded_access_modes()
            restored = self._inspect_live()
            restored["accessModeReconciliation"] = access_reconciliation
            if restored["logicalDigest"] != target["logicalDigest"]:
                raise ForgeTraceError(
                    "Rollback verification did not reproduce the expected registry state.",
                    500,
                    "registry_restore_rollback_failed",
                )
            return restored
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

    def restore(self, backup_name: Any, mode: Any, preview_id: Any) -> dict[str, Any]:
        supplied_preview_id = str(preview_id or "").strip()
        if not supplied_preview_id:
            raise ForgeTraceError(
                "A current registry restore preview is required.",
                409,
                "registry_restore_preview_required",
            )
        with self.registry.lock, self.registry.operation_lock:
            preview = self._preview_locked(backup_name, mode)
            if supplied_preview_id != preview["previewId"]:
                raise ForgeTraceError(
                    "The registry or selected backup changed after preview. Generate a new preview before restoring.",
                    409,
                    "registry_restore_preview_stale",
                    {"currentPreviewId": preview["previewId"]},
                )
            restore_id = "restore_" + uuid.uuid4().hex
            pre_restore_backup = self.registry.create_backup(f"pre-restore-{restore_id[-8:]}")
            before = self._inspect_live()
            stage_dir, staged = self._new_stage(restore_id)
            journal: dict[str, Any] = {
                "format": RESTORE_JOURNAL_FORMAT,
                "version": RESTORE_JOURNAL_VERSION,
                "restoreId": restore_id,
                "state": "prepared",
                "mode": preview["mode"],
                "backupName": preview["backupName"],
                "backupSha256": preview["backupSha256"],
                "previewId": preview["previewId"],
                "createdAt": utc_now(),
                "before": self._public_inspection(before),
                "preRestoreBackup": {
                    "name": pre_restore_backup["name"],
                    "sha256": pre_restore_backup.get("sha256", ""),
                },
                "stageDirectory": stage_dir.name,
                "rollbackAvailable": False,
            }
            self._write_journal(journal)
            mutation_started = False
            try:
                source = self._resolve_backup(preview["backupName"])
                if self._sha256_file(source) != preview["backupSha256"]:
                    raise ForgeTraceError(
                        "The selected backup changed while restore preparation was running.",
                        409,
                        "registry_restore_preview_stale",
                    )
                preparation = self._prepare_database(
                    source, staged, expected_source_sha256=preview["backupSha256"]
                )
                prepared_inspection = self._inspect_path(staged)
                journal["preparation"] = preparation
                journal["target"] = self._public_inspection(prepared_inspection)
                journal["state"] = "installing"
                journal["installingAt"] = utc_now()
                self._write_journal(journal)
                mutation_started = True
                if preview["mode"] == "replace":
                    self._replace_live(staged)
                    report: dict[str, Any] = {
                        "mode": "replace",
                        "repositoriesInstalled": prepared_inspection["repositoryCount"],
                    }
                else:
                    report = self._merge_prepared(staged)
                    report["mode"] = "merge"
                report["accessModeReconciliation"] = self._reconcile_embedded_access_modes()
                after = self._inspect_live()
                if preview["mode"] == "replace" and after["logicalDigest"] != prepared_inspection["logicalDigest"]:
                    raise ForgeTraceError(
                        "Installed registry did not match the prepared replacement.",
                        500,
                        "registry_restore_post_verify_failed",
                    )
                journal["state"] = "installed"
                journal["installedAt"] = utc_now()
                journal["after"] = self._public_inspection(after)
                journal["report"] = report
                self._write_journal(journal)
                # Re-run all structural checks after installation and only then expose rollback authority.
                verified = self._inspect_live()
                if verified["logicalDigest"] != after["logicalDigest"]:
                    raise ForgeTraceError(
                        "Registry changed during post-restore verification.",
                        409,
                        "registry_restore_post_verify_failed",
                    )
                journal["state"] = "completed"
                journal["completedAt"] = utc_now()
                journal["after"] = self._public_inspection(verified)
                journal["rollbackAvailable"] = True
                journal["verification"] = {
                    "integrity": verified["integrity"],
                    "foreignKeyErrors": verified["foreignKeyErrors"],
                    "schemaVersion": verified["schemaVersion"],
                    "pathStatus": verified["pathStatus"],
                }
                self._write_journal(journal)
                self._prune_journals()
                return {
                    **self._public_journal(journal),
                    "before": journal["before"],
                    "after": journal["after"],
                    "report": report,
                    "verification": journal["verification"],
                    "preRestoreBackup": {"name": pre_restore_backup["name"]},
                }
            except Exception as exc:
                rollback_result: dict[str, Any] = {"attempted": False, "succeeded": False}
                if mutation_started:
                    rollback_result["attempted"] = True
                    try:
                        restored = self._restore_exact_from_backup(
                            str(pre_restore_backup["name"]),
                            before["logicalDigest"],
                            str(pre_restore_backup.get("sha256", "")),
                        )
                        rollback_result.update(
                            {
                                "succeeded": True,
                                "restoredDigest": restored["logicalDigest"],
                                "completedAt": utc_now(),
                            }
                        )
                    except Exception as rollback_exc:  # pragma: no cover - catastrophic path
                        rollback_result["error"] = str(rollback_exc)
                journal["state"] = "failed_rolled_back" if rollback_result.get("succeeded") else "failed"
                journal["failedAt"] = utc_now()
                journal["failure"] = {"type": type(exc).__name__, "message": str(exc)}
                journal["automaticRollback"] = rollback_result
                journal["rollbackAvailable"] = False
                self._write_journal(journal)
                if isinstance(exc, ForgeTraceError):
                    if rollback_result.get("attempted") and not rollback_result.get("succeeded"):
                        raise ForgeTraceError(
                            "Registry restore failed and automatic rollback also failed. Use the recorded pre-restore backup for manual recovery.",
                            500,
                            "registry_restore_and_rollback_failed",
                            {"restoreId": restore_id, "preRestoreBackup": pre_restore_backup["name"]},
                        ) from exc
                    raise
                raise ForgeTraceError(
                    "Registry restore failed. The live registry was rolled back to its pre-restore backup."
                    if rollback_result.get("succeeded")
                    else "Registry restore failed before completion.",
                    500,
                    "registry_restore_failed",
                    {"restoreId": restore_id, "automaticRollback": rollback_result},
                ) from exc
            finally:
                shutil.rmtree(stage_dir, ignore_errors=True)

    def rollback(self, restore_id: Any) -> dict[str, Any]:
        selected_id = str(restore_id or "").strip()
        with self.registry.lock, self.registry.operation_lock:
            journal = self._load_journal(selected_id)
            if journal.get("state") != "completed" or not journal.get("rollbackAvailable"):
                raise ForgeTraceError(
                    "This registry restore does not have an available rollback.",
                    409,
                    "registry_restore_rollback_unavailable",
                )
            current = self._inspect_live()
            expected_post = str(journal.get("after", {}).get("logicalDigest") or "")
            if not expected_post or current["logicalDigest"] != expected_post:
                raise ForgeTraceError(
                    "The registry changed after this restore. Rollback is blocked to avoid discarding later work.",
                    409,
                    "registry_restore_rollback_stale",
                    {"currentDigest": current["logicalDigest"], "expectedDigest": expected_post},
                )
            pre_rollback_backup = self.registry.create_backup(f"pre-rollback-{selected_id[-8:]}")
            rollback = {
                "state": "installing",
                "startedAt": utc_now(),
                "preRollbackBackup": {
                    "name": pre_rollback_backup["name"],
                    "sha256": pre_rollback_backup.get("sha256", ""),
                },
            }
            journal["rollback"] = rollback
            self._write_journal(journal)
            try:
                restored = self._restore_exact_from_backup(
                    str(journal["preRestoreBackup"]["name"]),
                    str(journal.get("before", {}).get("logicalDigest") or ""),
                    str(journal.get("preRestoreBackup", {}).get("sha256") or ""),
                )
                rollback.update(
                    {
                        "state": "completed",
                        "completedAt": utc_now(),
                        "restoredDigest": restored["logicalDigest"],
                    }
                )
                journal["state"] = "rolled_back"
                journal["rollbackAvailable"] = False
                journal["rollback"] = rollback
                self._write_journal(journal)
                return {
                    **self._public_journal(journal),
                    "restored": self._public_inspection(restored),
                    "preRollbackBackup": {
                    "name": pre_rollback_backup["name"],
                    "sha256": pre_rollback_backup.get("sha256", ""),
                },
                }
            except Exception as exc:
                recovery: dict[str, Any] = {"attempted": True, "succeeded": False}
                try:
                    recovered = self._restore_exact_from_backup(
                        str(pre_rollback_backup["name"]),
                        current["logicalDigest"],
                        str(pre_rollback_backup.get("sha256", "")),
                    )
                    recovery.update({"succeeded": True, "restoredDigest": recovered["logicalDigest"]})
                except Exception as recovery_exc:  # pragma: no cover - catastrophic path
                    recovery["error"] = str(recovery_exc)
                rollback.update(
                    {
                        "state": "failed",
                        "failedAt": utc_now(),
                        "error": str(exc),
                        "automaticRecovery": recovery,
                    }
                )
                journal["rollback"] = rollback
                self._write_journal(journal)
                raise ForgeTraceError(
                    "Registry rollback failed; the post-restore registry was recovered from its safety backup."
                    if recovery.get("succeeded")
                    else "Registry rollback and automatic recovery failed.",
                    500,
                    "registry_restore_rollback_failed",
                    {"restoreId": selected_id, "automaticRecovery": recovery},
                ) from exc

    def _prune_journals(self, keep: int = 50) -> None:
        paths = sorted(self.journals_dir.glob("restore_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in paths[keep:]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("rollbackAvailable"):
                continue
            path.unlink(missing_ok=True)

    def recover_startup(self) -> dict[str, Any]:
        report: dict[str, Any] = {"checked": 0, "finalized": 0, "rolledBack": 0, "abandoned": 0, "actions": []}
        with self.registry.lock, self.registry.operation_lock:
            paths = sorted(self.journals_dir.glob("restore_*.json"), key=lambda path: path.stat().st_mtime)
            for path in paths:
                try:
                    journal = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                state = str(journal.get("state") or "")
                if state not in {"prepared", "installing", "installed"}:
                    continue
                report["checked"] += 1
                restore_id = str(journal.get("restoreId") or path.stem)
                try:
                    current = self._inspect_live()
                    before_digest = str(journal.get("before", {}).get("logicalDigest") or "")
                    target_digest = str(
                        journal.get("after", {}).get("logicalDigest")
                        or journal.get("target", {}).get("logicalDigest")
                        or ""
                    )
                    if state == "prepared" or (before_digest and current["logicalDigest"] == before_digest):
                        journal["state"] = "abandoned_before_install"
                        journal["recovery"] = {"action": "abandoned_before_install", "recoveredAt": utc_now()}
                        journal["rollbackAvailable"] = False
                        report["abandoned"] += 1
                        report["actions"].append({"restoreId": restore_id, "action": "abandoned_before_install"})
                    elif state == "installed" and target_digest and current["logicalDigest"] == target_digest:
                        reconciliation = self._reconcile_embedded_access_modes()
                        journal.setdefault("report", {})["accessModeReconciliation"] = reconciliation
                        journal["state"] = "completed"
                        journal["completedAt"] = utc_now()
                        journal["after"] = self._public_inspection(current)
                        journal["rollbackAvailable"] = True
                        journal["recovery"] = {"action": "finalized_installed_restore", "recoveredAt": utc_now()}
                        report["finalized"] += 1
                        report["actions"].append({"restoreId": restore_id, "action": "finalized_installed_restore"})
                    else:
                        restored = self._restore_exact_from_backup(
                            str(journal.get("preRestoreBackup", {}).get("name") or ""),
                            before_digest,
                            str(journal.get("preRestoreBackup", {}).get("sha256") or ""),
                        )
                        journal["state"] = "recovered_rolled_back"
                        journal["rollbackAvailable"] = False
                        journal["recovery"] = {
                            "action": "rolled_back_interrupted_restore",
                            "recoveredAt": utc_now(),
                            "restoredDigest": restored["logicalDigest"],
                        }
                        report["rolledBack"] += 1
                        report["actions"].append({"restoreId": restore_id, "action": "rolled_back_interrupted_restore"})
                    self._atomic_write_json(path, journal)
                except Exception as exc:  # pragma: no cover - startup catastrophic path
                    report["actions"].append(
                        {"restoreId": restore_id, "action": "recovery_failed", "error": str(exc)}
                    )
                stage_name = str(journal.get("stageDirectory") or "")
                if stage_name and Path(stage_name).name == stage_name:
                    shutil.rmtree(self.staging_dir / stage_name, ignore_errors=True)
        return report
