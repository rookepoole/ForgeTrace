from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import threading
import tempfile
import urllib.parse
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from .constants import (
    MAX_EDITABLE_TEXT_BYTES,
    MAX_REQUEST_BYTES,
    REPOSITORY_ACCESS_READ_ONLY,
    REPOSITORY_ACCESS_READ_WRITE,
    REPOSITORY_SCHEMA_VERSION,
    TEXT_EXTENSIONS,
    normalize_repository_access_mode,
)
from .errors import RepositoryError
from .locks import InterProcessRLock
from .policies import path_policy_warnings
from .transactions import FilesystemTransaction, inspect_transactions, recover_transactions
from .utils import utc_now


def human_action_title(action: str, path: str = "") -> str:
    name = Path(path).name or path or "repository"
    labels = {
        "repository_created": "Created repository",
        "file_uploaded": f"Uploaded {name}",
        "file_saved": f"Updated {name}",
        "file_created": f"Created {name}",
        "folder_created": f"Created folder {name}",
        "folders_imported": "Prepared imported folder hierarchy",
        "folder_imported": f"Imported folder {name}",
        "path_renamed": f"Renamed {name}",
        "path_deleted": f"Deleted {name}",
        "commit_created": "Created repository snapshot",
        "commit_restored": "Restored repository snapshot",
        "pull_request_merged": "Merged pull request",
    }
    return labels.get(action, action.replace("_", " ").title())


class ForgeTraceRepository:
    _locks_guard = threading.Lock()
    _workspace_locks: dict[str, InterProcessRLock] = {}

    def __init__(
        self,
        project_root: Path,
        workspace: Path,
        repository_id: str | None = None,
        *,
        upload_limit_bytes: int = MAX_REQUEST_BYTES,
        access_mode_getter: Callable[[], str] | None = None,
        recover_on_open: bool = True,
        create_workspace: bool = True,
    ):
        self.project_root = project_root.resolve()
        self.workspace = workspace.expanduser().resolve()
        self.repository_id = repository_id
        self.upload_limit_bytes = max(1, min(int(upload_limit_bytes), MAX_REQUEST_BYTES))
        self._access_mode_getter = access_mode_getter
        self.meta_dir = self.workspace / ".forgetrace"
        self.objects_dir = self.meta_dir / "objects"
        self.state_path = self.meta_dir / "state.json"
        lock_key = os.path.normcase(str(self.workspace))
        with self._locks_guard:
            self.lock = self._workspace_locks.setdefault(
                lock_key, InterProcessRLock(self.meta_dir / "repository.lock", timeout=60.0)
            )
        if create_workspace:
            self.workspace.mkdir(parents=True, exist_ok=True)
        self._tree_cache: dict[str, Any] = {"signature": None, "entries": [], "manifest": {}}
        self._recovery_actions: list[dict[str, Any]] = []
        if recover_on_open and self.meta_dir.exists():
            with self.lock:
                self._recovery_actions = recover_transactions(
                    self.workspace, self.meta_dir, current_revision=self._read_disk_revision
                )
                self._cleanup_stale_operation_artifacts()

    def _read_disk_revision(self) -> int:
        if not self.state_path.is_file():
            return 0
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return int(payload.get("revision") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return 0

    @staticmethod
    def state_revision(state: dict[str, Any]) -> int:
        return int(state.get("revision") or state.get("_loadedRevision") or 0)

    def _cleanup_stale_operation_artifacts(self) -> None:
        now = datetime.now(timezone.utc).timestamp()
        policies = {
            "import-staging": 24 * 3600,
            "restore-staging": 24 * 3600,
            "merge-backups": 7 * 24 * 3600,
        }
        for dirname, max_age in policies.items():
            root = self.meta_dir / dirname
            if not root.is_dir():
                continue
            for child in root.iterdir():
                try:
                    age = now - child.stat().st_mtime
                except OSError:
                    continue
                if age > max_age:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                    self._recovery_actions.append({"artifact": str(child), "action": "cleaned_stale"})

    def invalidate_index(self) -> None:
        self._tree_cache = {"signature": None, "entries": [], "manifest": {}}

    def initialized(self) -> bool:
        return self.state_path.is_file()

    def default_state(self) -> dict[str, Any]:
        return {
            "schemaVersion": REPOSITORY_SCHEMA_VERSION,
            "revision": 0,
            "repository": {
                "id": self.repository_id or "",
                "name": "",
                "description": "",
                "createdAt": "",
                "defaultAuthor": "",
                "accessMode": REPOSITORY_ACCESS_READ_WRITE,
            },
            "contributions": [],
            "commits": [],
        }

    def load_state(self, require_initialized: bool = True) -> dict[str, Any]:
        if not self.state_path.exists():
            if require_initialized:
                raise RepositoryError("Repository has not been initialized.", HTTPStatus.CONFLICT)
            state = self.default_state()
            state["_loadedRevision"] = 0
            return state
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RepositoryError(f"Repository metadata is unreadable: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        state.setdefault("revision", 0)
        state.setdefault("contributions", [])
        state.setdefault("commits", [])
        repository = state.setdefault("repository", {})
        try:
            schema_version = int(state.get("schemaVersion") or 0)
        except (TypeError, ValueError):
            schema_version = 0
        raw_mode = repository.get("accessMode")
        mode_present = raw_mode is not None and str(raw_mode).strip() != ""
        legacy_missing_mode = not mode_present and schema_version < REPOSITORY_SCHEMA_VERSION
        embedded_mode = normalize_repository_access_mode(
            raw_mode, fail_closed=mode_present or schema_version >= REPOSITORY_SCHEMA_VERSION
        )
        repository["accessMode"] = embedded_mode
        if schema_version < REPOSITORY_SCHEMA_VERSION:
            state["schemaVersion"] = REPOSITORY_SCHEMA_VERSION
            state["_needsSchemaUpgrade"] = True
        state["_embeddedAccessModeValid"] = (
            legacy_missing_mode or str(raw_mode or "").strip().lower() in {
                REPOSITORY_ACCESS_READ_WRITE, REPOSITORY_ACCESS_READ_ONLY
            }
        )
        state["_embeddedAccessModeNeedsDefault"] = legacy_missing_mode
        state["_loadedRevision"] = int(state.get("revision") or 0)
        return state

    def embedded_access_mode(self, state: dict[str, Any] | None = None) -> str:
        payload = state or self.load_state(require_initialized=False)
        repository = payload.get("repository", {}) if isinstance(payload, dict) else {}
        raw_mode = repository.get("accessMode") if isinstance(repository, dict) else None
        try:
            schema_version = int(payload.get("schemaVersion") or 0) if isinstance(payload, dict) else 0
        except (TypeError, ValueError):
            schema_version = 0
        mode_present = raw_mode is not None and str(raw_mode).strip() != ""
        return normalize_repository_access_mode(
            raw_mode, fail_closed=mode_present or schema_version >= REPOSITORY_SCHEMA_VERSION
        )

    def registry_access_mode(self) -> str:
        if self._access_mode_getter is None:
            return self.embedded_access_mode()
        try:
            return normalize_repository_access_mode(self._access_mode_getter(), fail_closed=True)
        except Exception:
            return REPOSITORY_ACCESS_READ_ONLY

    def access_policy(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = state or self.load_state(require_initialized=False)
        embedded = self.embedded_access_mode(payload)
        repository = payload.get("repository", {}) if isinstance(payload, dict) else {}
        raw_mode = repository.get("accessMode") if isinstance(repository, dict) else None
        embedded_valid = bool(payload.get("_embeddedAccessModeValid")) if isinstance(payload, dict) and "_embeddedAccessModeValid" in payload else (
            str(raw_mode or "").strip().lower() in {REPOSITORY_ACCESS_READ_WRITE, REPOSITORY_ACCESS_READ_ONLY}
        )
        registry = self.registry_access_mode()
        effective = (
            REPOSITORY_ACCESS_READ_WRITE
            if embedded_valid and embedded == registry == REPOSITORY_ACCESS_READ_WRITE
            else REPOSITORY_ACCESS_READ_ONLY
        )
        return {
            "registryMode": registry,
            "embeddedMode": embedded,
            "embeddedValid": embedded_valid,
            "effectiveMode": effective,
            "consistent": embedded_valid and registry == embedded,
            "writable": effective == REPOSITORY_ACCESS_READ_WRITE,
        }

    def require_writable(self, operation: str = "repository mutation") -> None:
        policy = self.access_policy()
        if not policy["writable"]:
            raise RepositoryError(
                "Repository is read-only. Return it to read-write mode before changing repository content or embedded settings.",
                HTTPStatus.LOCKED,
                "repository_read_only",
                {"operation": operation, "accessPolicy": policy},
            )

    @contextmanager
    def mutation(self, operation: str):
        """Serialize a mutation and verify access mode while the repository lock is held."""
        with self.lock:
            self.require_writable(operation)
            yield

    def set_embedded_access_mode(self, mode: str) -> dict[str, Any]:
        selected = str(mode or "").strip().lower()
        if selected not in {REPOSITORY_ACCESS_READ_WRITE, REPOSITORY_ACCESS_READ_ONLY}:
            raise RepositoryError("Repository access mode is invalid.", code="invalid_repository_access_mode")
        with self.lock:
            state = self.load_state()
            state.setdefault("repository", {})["accessMode"] = selected
            state["schemaVersion"] = REPOSITORY_SCHEMA_VERSION
            state["_embeddedAccessModeValid"] = True
            state["_embeddedAccessModeNeedsDefault"] = False
            self.save_state(state, bypass_access_policy=True)
            return self.access_policy(state)

    def reconcile_access_mode(self, registry_mode: str) -> dict[str, Any]:
        selected = normalize_repository_access_mode(registry_mode, fail_closed=True)
        with self.lock:
            state = self.load_state()
            repository = state.setdefault("repository", {})
            embedded = self.embedded_access_mode(state)
            needs_upgrade = bool(state.get("_needsSchemaUpgrade"))
            mode_missing = bool(state.get("_embeddedAccessModeNeedsDefault"))
            should_tighten = selected == REPOSITORY_ACCESS_READ_ONLY and embedded != REPOSITORY_ACCESS_READ_ONLY
            if mode_missing or needs_upgrade or should_tighten:
                if mode_missing or should_tighten:
                    repository["accessMode"] = selected
                    state["_embeddedAccessModeValid"] = True
                    state["_embeddedAccessModeNeedsDefault"] = False
                state["schemaVersion"] = REPOSITORY_SCHEMA_VERSION
                self.save_state(state, bypass_access_policy=True)
            return self.access_policy(state)

    def save_state(self, state: dict[str, Any], *, bypass_access_policy: bool = False) -> None:
        with self.lock:
            if not bypass_access_policy:
                self.require_writable("repository metadata persistence")
            self.meta_dir.mkdir(parents=True, exist_ok=True)
            self.objects_dir.mkdir(parents=True, exist_ok=True)
            expected_revision = int(state.get("_loadedRevision", state.get("revision", 0)) or 0)
            current_revision = self._read_disk_revision()
            if self.state_path.exists() and expected_revision != current_revision:
                raise RepositoryError(
                    "Repository metadata changed in another ForgeTrace process. Reload and retry.",
                    HTTPStatus.CONFLICT,
                    "repository_revision_conflict",
                    {"expectedRevision": expected_revision, "currentRevision": current_revision},
                )
            next_revision = current_revision + 1
            persisted = {key: value for key, value in state.items() if not key.startswith("_")}
            persisted["revision"] = next_revision
            tmp = self.state_path.with_name(f"state.{uuid.uuid4().hex}.tmp")
            backup = self.state_path.with_suffix(".json.bak")
            payload = json.dumps(persisted, indent=2, ensure_ascii=False).encode("utf-8")
            try:
                with tmp.open("wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self.state_path.exists():
                    try:
                        json.loads(self.state_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        pass
                    else:
                        backup_tmp = backup.with_name(f"state-backup.{uuid.uuid4().hex}.tmp")
                        shutil.copy2(self.state_path, backup_tmp)
                        os.replace(backup_tmp, backup)
                os.replace(tmp, self.state_path)
            finally:
                tmp.unlink(missing_ok=True)
            state["revision"] = next_revision
            state["_loadedRevision"] = next_revision

    def _new_transaction(self, state: dict[str, Any], operation: str) -> FilesystemTransaction:
        return FilesystemTransaction(
            self.workspace,
            self.meta_dir,
            operation=operation,
            state_revision_before=self.state_revision(state),
        )

    def _capture_write_target(self, transaction: FilesystemTransaction, rel: str, path: Path) -> None:
        if path.exists() or path.is_symlink():
            transaction.capture(rel, path)
            return
        missing: list[Path] = []
        current = path.parent
        while current != self.workspace and not current.exists():
            missing.append(current)
            current = current.parent
        if missing:
            root = missing[-1]
            transaction.capture(root.relative_to(self.workspace).as_posix(), root)
        else:
            transaction.capture(rel, path)

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes, suffix: str = "write") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.{suffix}.tmp")
        try:
            with temp.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def normalize_rel(self, raw: str, *, allow_root: bool = False) -> str:
        value = urllib.parse.unquote(raw or "").replace("\\", "/").strip()
        value = value.lstrip("/")
        normalized = os.path.normpath(value).replace("\\", "/") if value else ""
        if normalized in {".", ""}:
            if allow_root:
                return ""
            raise RepositoryError("A repository path is required.")
        if normalized == ".forgetrace" or normalized.startswith(".forgetrace/"):
            raise RepositoryError("The .forgetrace metadata directory is protected.", HTTPStatus.FORBIDDEN)
        if normalized == ".." or normalized.startswith("../") or "/../" in f"/{normalized}/":
            raise RepositoryError("Path traversal is not allowed.", HTTPStatus.FORBIDDEN)
        return normalized

    def resolve_path(self, raw: str, *, allow_root: bool = False) -> tuple[str, Path]:
        rel = self.normalize_rel(raw, allow_root=allow_root)
        target = (self.workspace / rel).resolve()
        if target != self.workspace and self.workspace not in target.parents:
            raise RepositoryError("Path escapes the repository workspace.", HTTPStatus.FORBIDDEN)
        return rel, target

    def initialize(self, name: str, description: str, author: str) -> dict[str, Any]:
        name = (name or "").strip()
        author = (author or "").strip() or "Repository Owner"
        if not name:
            raise RepositoryError("Repository name is required.")
        with self.lock:
            if self.initialized():
                raise RepositoryError("Repository is already initialized.", HTTPStatus.CONFLICT)
            self.meta_dir.mkdir(parents=True, exist_ok=True)
            self.objects_dir.mkdir(parents=True, exist_ok=True)
            now = utc_now()
            state = self.default_state()
            state["_loadedRevision"] = 0
            state["repository"] = {
                "id": self.repository_id or str(uuid.uuid4()),
                "name": name,
                "description": (description or "").strip(),
                "createdAt": now,
                "defaultAuthor": author,
                "accessMode": REPOSITORY_ACCESS_READ_WRITE,
            }
            readme = self.workspace / "README.md"
            transaction = self._new_transaction(state, "repository_initialize")
            try:
                if not readme.exists():
                    transaction.capture("README.md", readme)
                    summary = description.strip() if description else "Created with ForgeTrace."
                    self._atomic_write_bytes(readme, f"# {name}\n\n{summary}\n".encode("utf-8"), "initialize")
                self.record_contribution(
                    state,
                    action="repository_created",
                    author=author,
                    path="README.md",
                    description=f"Initialized {name} and created the repository README.",
                    impact=70,
                )
                self.save_state(state)
                transaction.commit(self.state_revision(state))
                self.invalidate_index()
                return self.summary(state)
            except Exception:
                transaction.rollback()
                self.state_path.unlink(missing_ok=True)
                raise

    def update_repository_metadata(self, name: str, description: str, default_author: str) -> dict[str, Any]:
        with self.mutation("repository settings update"):
            state = self.load_state()
            state.setdefault("repository", {})
            state["repository"]["name"] = str(name).strip()
            state["repository"]["description"] = str(description).strip()
            state["repository"]["defaultAuthor"] = str(default_author).strip() or "Repository Owner"
            self.save_state(state)
            return state["repository"].copy()

    def set_upstream(self, upstream: dict[str, Any]) -> dict[str, Any]:
        """Persist non-secret fork provenance inside repository metadata.

        Raw collaboration tokens are intentionally excluded. Contributors paste the
        current invite link again when they need to authenticate to an upstream.
        """
        allowed = {
            "baseUrl", "repositoryId", "repositoryName", "inviteId",
            "forkedAt", "tokenFingerprint", "archiveSha256", "sourceFiles",
        }
        cleaned = {key: upstream[key] for key in allowed if key in upstream}
        with self.mutation("upstream metadata update"):
            state = self.load_state()
            state.setdefault("repository", {})["upstream"] = cleaned
            self.save_state(state)
            return cleaned.copy()

    def ensure_identity(self, repository_id: str) -> None:
        """Adopt an older repository or verify that a moved repository matches its registry record."""
        with self.lock:
            state = self.load_state()
            stored = str(state.get("repository", {}).get("id") or "").strip()
            if stored and stored != repository_id:
                raise RepositoryError(
                    "The selected folder belongs to a different ForgeTrace repository.",
                    HTTPStatus.CONFLICT,
                    "repository_identity_mismatch",
                    {"expected": repository_id, "found": stored},
                )
            self.repository_id = repository_id
            if not stored:
                state.setdefault("repository", {})["id"] = repository_id
                self.save_state(state, bypass_access_policy=True)

    def is_text(self, path: Path) -> bool:
        if path.suffix.lower() in TEXT_EXTENSIONS or path.name in {"Dockerfile", "Makefile", "Procfile", "LICENSE"}:
            return True
        mime, _ = mimetypes.guess_type(path.name)
        if mime and (mime.startswith("text/") or mime in {"application/json", "application/javascript", "application/xml"}):
            return True
        try:
            chunk = path.read_bytes()[:8192]
        except OSError:
            return False
        if b"\x00" in chunk:
            return False
        try:
            chunk.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False

    def file_entry(self, path: Path) -> dict[str, Any]:
        rel = path.relative_to(self.workspace).as_posix()
        stat = path.stat()
        entry: dict[str, Any] = {
            "path": rel,
            "name": path.name,
            "type": "folder" if path.is_dir() else "file",
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        if path.is_file():
            mime, _ = mimetypes.guess_type(path.name)
            entry.update({
                "size": stat.st_size,
                "mime": mime or "application/octet-stream",
                "text": self.is_text(path),
            })
        return entry

    @property
    def index_path(self) -> Path:
        return self.meta_dir / "file-index.json"

    def _load_hash_index(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def transaction_health(self, *, limit: int = 200) -> dict[str, Any]:
        return inspect_transactions(
            self.workspace,
            self.meta_dir,
            current_revision=self._read_disk_revision,
            limit=limit,
        )

    def hash_index_health(self, *, max_entries: int = 5000) -> dict[str, Any]:
        """Inspect the incremental hash index without refreshing or rewriting it."""

        bounded = max(1, min(int(max_entries), 100_000))
        if not self.index_path.exists():
            return {
                "state": "missing",
                "valid": True,
                "entryCount": 0,
                "scannedCount": 0,
                "complete": True,
                "staleCount": 0,
                "missingPathCount": 0,
                "invalidEntryCount": 0,
                "updatedAt": "",
            }
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "state": "invalid",
                "valid": False,
                "entryCount": 0,
                "scannedCount": 0,
                "complete": True,
                "staleCount": 0,
                "missingPathCount": 0,
                "invalidEntryCount": 1,
                "updatedAt": "",
                "message": str(exc),
            }
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, dict) or int(payload.get("schemaVersion") or 0) != 1:
            return {
                "state": "invalid",
                "valid": False,
                "entryCount": len(files) if isinstance(files, dict) else 0,
                "scannedCount": 0,
                "complete": True,
                "staleCount": 0,
                "missingPathCount": 0,
                "invalidEntryCount": 1,
                "updatedAt": str(payload.get("updatedAt") or "") if isinstance(payload, dict) else "",
                "message": "Hash index schema or file map is invalid.",
            }
        stale = 0
        missing = 0
        invalid = 0
        entries = sorted(files.items())
        for rel, item in entries[:bounded]:
            if not isinstance(item, dict):
                invalid += 1
                continue
            digest = str(item.get("hash") or "")
            signature = str(item.get("signature") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or not signature:
                invalid += 1
                continue
            try:
                normalized, path = self.resolve_path(str(rel))
            except RepositoryError:
                invalid += 1
                continue
            if normalized != str(rel).replace("\\", "/") or not path.is_file() or path.is_symlink():
                missing += 1
                continue
            try:
                info = path.stat()
            except OSError:
                missing += 1
                continue
            actual_signature = f"{info.st_size}:{info.st_mtime_ns}:{getattr(info, 'st_ino', 0)}"
            if actual_signature != signature:
                stale += 1
        complete = len(entries) <= bounded
        valid = invalid == 0
        state = "invalid" if not valid else "stale" if stale or missing else "current"
        return {
            "state": state,
            "valid": valid,
            "entryCount": len(entries),
            "scannedCount": min(len(entries), bounded),
            "complete": complete,
            "staleCount": stale,
            "missingPathCount": missing,
            "invalidEntryCount": invalid,
            "updatedAt": str(payload.get("updatedAt") or ""),
        }

    def _save_hash_index(self, payload: dict[str, Any]) -> None:
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        temp = self.index_path.with_name(f"file-index.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.index_path)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _metadata_for_stat(info: os.stat_result) -> dict[str, Any]:
        return {
            "mode": int(info.st_mode & 0o7777),
            "mtimeNs": int(info.st_mtime_ns),
        }

    def _scan_workspace(self, *, need_hashes: bool = True, persist_index: bool = True) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """Return depth-first tree rows, cached file manifest, and directory metadata."""
        prior = self._load_hash_index() if need_hashes else {}
        prior_files = prior.get("files") if isinstance(prior.get("files"), dict) else {}
        next_files: dict[str, Any] = {}
        entries: list[dict[str, Any]] = []
        manifest: dict[str, dict[str, Any]] = {}
        directories: list[dict[str, Any]] = []

        def visit(folder: Path, relative: Path) -> None:
            try:
                children = list(folder.iterdir())
            except OSError as exc:
                raise RepositoryError(f"Repository folder is unreadable: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
            folders = sorted(
                [child for child in children if child.is_dir() and not child.is_symlink() and not (relative == Path('.') and child.name == '.forgetrace')],
                key=lambda item: item.name.casefold(),
            )
            files = sorted(
                [child for child in children if child.is_file() and not child.is_symlink()],
                key=lambda item: item.name.casefold(),
            )
            for child in folders:
                entry = self.file_entry(child)
                entry["depth"] = entry["path"].count("/")
                entry["parentPath"] = Path(entry["path"]).parent.as_posix()
                if entry["parentPath"] == ".":
                    entry["parentPath"] = ""
                entries.append(entry)
                info = child.stat()
                directories.append({"path": entry["path"], **self._metadata_for_stat(info)})
                visit(child, relative / child.name)
            for child in files:
                entry = self.file_entry(child)
                entry["depth"] = entry["path"].count("/")
                entry["parentPath"] = Path(entry["path"]).parent.as_posix()
                if entry["parentPath"] == ".":
                    entry["parentPath"] = ""
                entries.append(entry)
                info = child.stat()
                signature = f"{info.st_size}:{info.st_mtime_ns}:{getattr(info, 'st_ino', 0)}"
                cached = prior_files.get(entry["path"], {})
                digest = str(cached.get("hash") or "") if cached.get("signature") == signature else ""
                if need_hashes and not digest:
                    hasher = hashlib.sha256()
                    with child.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            hasher.update(chunk)
                    digest = hasher.hexdigest()
                metadata = self._metadata_for_stat(info)
                if need_hashes:
                    manifest[entry["path"]] = {"hash": digest, "size": info.st_size, **metadata}
                    next_files[entry["path"]] = {"signature": signature, "hash": digest}

        visit(self.workspace, Path("."))
        if need_hashes and persist_index and self.access_policy().get("writable", False):
            self._save_hash_index({"schemaVersion": 1, "updatedAt": utc_now(), "files": next_files})
        self._tree_cache = {"signature": utc_now(), "entries": entries, "manifest": manifest, "directories": directories}
        return entries, manifest, directories

    def tree(self) -> list[dict[str, Any]]:
        if not self.initialized():
            return []
        entries, _manifest, _directories = self._scan_workspace(need_hashes=False)
        return entries

    def read_file(self, raw_path: str) -> dict[str, Any]:
        rel, path = self.resolve_path(raw_path)
        if not path.exists() or not path.is_file():
            raise RepositoryError("File not found.", HTTPStatus.NOT_FOUND)
        entry = self.file_entry(path)
        if entry["text"]:
            if entry["size"] > MAX_EDITABLE_TEXT_BYTES:
                entry["editable"] = False
                entry["message"] = "Text files larger than 5 MB can be downloaded but are not opened in the editor."
            else:
                entry["editable"] = True
                entry["content"] = path.read_text(encoding="utf-8", errors="replace")
        else:
            entry["editable"] = False
        base = f"/api/v1/repositories/{urllib.parse.quote(self.repository_id or 'active')}"
        entry["downloadUrl"] = f"{base}/raw?path={urllib.parse.quote(rel)}&download=1"
        entry["rawUrl"] = f"{base}/raw?path={urllib.parse.quote(rel)}"
        return entry

    def raw_file(self, raw_path: str) -> tuple[Path, str, str]:
        rel, path = self.resolve_path(raw_path)
        if not path.exists() or not path.is_file():
            raise RepositoryError("File not found.", HTTPStatus.NOT_FOUND)
        mime, _ = mimetypes.guess_type(path.name)
        return path, rel, mime or "application/octet-stream"

    def find_latest_for_path(self, state: dict[str, Any], paths: list[str]) -> list[str]:
        wanted = set(paths)
        parents: list[str] = []
        for contribution in reversed(state["contributions"]):
            touched = set(contribution.get("paths") or ([contribution["path"]] if contribution.get("path") else []))
            if wanted & touched:
                parents.append(contribution["id"])
                wanted -= touched
                if not wanted:
                    break
        return list(reversed(parents))

    def record_contribution(
        self,
        state: dict[str, Any],
        *,
        action: str,
        author: str,
        path: str = "",
        paths: list[str] | None = None,
        description: str = "",
        impact: int = 60,
        parents: list[str] | None = None,
        commit_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        touched = [p for p in (paths or ([path] if path else [])) if p]
        if parents is None and touched:
            parents = self.find_latest_for_path(state, touched)
        parents = list(dict.fromkeys(parents or []))
        event = {
            "id": "ct_" + uuid.uuid4().hex[:12],
            "action": action,
            "type": (
                "merge" if action == "pull_request_merged"
                else "commit" if action in {"commit_created", "commit_restored"}
                else "folder" if "folder" in action
                else "file"
            ),
            "title": human_action_title(action, path or (touched[0] if touched else "")),
            "description": description,
            "author": (author or state.get("repository", {}).get("defaultAuthor") or "Unknown Contributor").strip(),
            "timestamp": utc_now(),
            "path": path,
            "paths": touched,
            "impact": max(1, min(100, int(impact))),
            "parents": parents,
            "children": [],
            "commitId": commit_id,
            "metadata": metadata or {},
        }
        state["contributions"].append(event)
        parent_ids = set(parents)
        for prior in state["contributions"]:
            if prior["id"] in parent_ids and event["id"] not in prior.setdefault("children", []):
                prior["children"].append(event["id"])
        return event

    def write_file(self, raw_path: str, content: bytes, author: str, message: str, *, uploaded: bool = False) -> dict[str, Any]:
        rel, path = self.resolve_path(raw_path)
        if len(content) > self.upload_limit_bytes:
            limit_mb = self.upload_limit_bytes / (1024 * 1024)
            raise RepositoryError(
                f"File exceeds this repository's {limit_mb:g} MB upload limit.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "repository_upload_limit_exceeded",
                {"limitBytes": self.upload_limit_bytes, "fileBytes": len(content)},
            )
        with self.mutation("file write"):
            state = self.load_state()
            existed = path.exists()
            if path.exists() and path.is_dir():
                raise RepositoryError("A folder already exists at that path.", HTTPStatus.CONFLICT)
            transaction = self._new_transaction(state, "file_write")
            try:
                self._capture_write_target(transaction, rel, path)
                self._atomic_write_bytes(path, content, "file")
                action = "file_uploaded" if uploaded else ("file_saved" if existed else "file_created")
                default_desc = message.strip() if message else ("Uploaded file into the repository." if uploaded else "Saved file content.")
                self.record_contribution(
                    state,
                    action=action,
                    author=author,
                    path=rel,
                    description=default_desc,
                    impact=min(92, 48 + max(1, min(30, len(content) // 4096))),
                    metadata={"bytes": len(content)},
                )
                self.save_state(state)
                transaction.commit(self.state_revision(state))
                self.invalidate_index()
                return self.read_file(rel)
            except Exception:
                transaction.rollback()
                raise

    def write_file_from_path(
        self, raw_path: str, source_path: Path, author: str, message: str, *, uploaded: bool = False
    ) -> dict[str, Any]:
        """Stream a file from a temporary path into the repository transactionally."""
        rel, path = self.resolve_path(raw_path)
        source = source_path.expanduser().resolve()
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise RepositoryError(f"Uploaded file is unavailable: {exc}") from exc
        if size > self.upload_limit_bytes:
            limit_mb = self.upload_limit_bytes / (1024 * 1024)
            raise RepositoryError(
                f"File exceeds this repository's {limit_mb:g} MB upload limit.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "repository_upload_limit_exceeded",
                {"limitBytes": self.upload_limit_bytes, "fileBytes": size},
            )
        with self.mutation("file upload"):
            state = self.load_state()
            existed = path.exists()
            if path.exists() and path.is_dir():
                raise RepositoryError("A folder already exists at that path.", HTTPStatus.CONFLICT)
            transaction = self._new_transaction(state, "file_upload")
            try:
                self._capture_write_target(transaction, rel, path)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.upload.tmp")
                try:
                    with source.open("rb") as input_handle, tmp.open("wb") as output_handle:
                        shutil.copyfileobj(input_handle, output_handle, 1024 * 1024)
                        output_handle.flush()
                        os.fsync(output_handle.fileno())
                    os.replace(tmp, path)
                finally:
                    tmp.unlink(missing_ok=True)
                action = "file_uploaded" if uploaded else ("file_saved" if existed else "file_created")
                default_desc = message.strip() if message else ("Uploaded file into the repository." if uploaded else "Saved file content.")
                self.record_contribution(
                    state,
                    action=action,
                    author=author,
                    path=rel,
                    description=default_desc,
                    impact=min(92, 48 + max(1, min(30, size // 4096))),
                    metadata={"bytes": size},
                )
                self.save_state(state)
                transaction.commit(self.state_revision(state))
                self.invalidate_index()
                return self.read_file(rel)
            except Exception:
                transaction.rollback()
                raise

    def create_folder(self, raw_path: str, author: str) -> dict[str, Any]:
        rel, path = self.resolve_path(raw_path)
        with self.mutation("folder creation"):
            state = self.load_state()
            if path.exists():
                raise RepositoryError("A file or folder already exists at that path.", HTTPStatus.CONFLICT)
            transaction = self._new_transaction(state, "folder_create")
            try:
                self._capture_write_target(transaction, rel, path)
                path.mkdir(parents=True)
                self.record_contribution(
                    state,
                    action="folder_created",
                    author=author,
                    path=rel,
                    description="Created a repository folder.",
                    impact=35,
                )
                self.save_state(state)
                transaction.commit(self.state_revision(state))
                self.invalidate_index()
                return self.file_entry(path)
            except Exception:
                transaction.rollback()
                raise

    def ensure_folders(self, raw_paths: list[str], author: str) -> dict[str, Any]:
        normalized: list[tuple[str, Path]] = []
        seen: set[str] = set()
        for raw in raw_paths or []:
            rel, path = self.resolve_path(str(raw))
            if rel in seen:
                continue
            seen.add(rel)
            normalized.append((rel, path))
        normalized.sort(key=lambda item: (item[0].count("/"), item[0].casefold()))
        created: list[str] = []
        existing: list[str] = []
        with self.mutation("folder manifest creation"):
            state = self.load_state()
            transaction = self._new_transaction(state, "folder_manifest")
            try:
                for rel, path in normalized:
                    if path.exists():
                        if not path.is_dir():
                            raise RepositoryError(
                                f"A file already exists where the imported folder is required: {rel}",
                                HTTPStatus.CONFLICT,
                            )
                        existing.append(rel)
                        continue
                    self._capture_write_target(transaction, rel, path)
                    path.mkdir(parents=True, exist_ok=True)
                    created.append(rel)
                if created:
                    self.record_contribution(
                        state,
                        action="folders_imported",
                        author=author,
                        paths=created,
                        description=f"Prepared {len(created)} folders for a recursive import.",
                        impact=min(70, 30 + len(created)),
                        metadata={"folderCount": len(created)},
                    )
                    self.save_state(state)
                transaction.commit(self.state_revision(state))
                self.invalidate_index()
            except Exception:
                transaction.rollback()
                raise
        return {"created": created, "existing": existing, "folderCount": len(normalized)}

    def preview_local_folder_import(
        self, raw_source: str, *, include_root: bool = True, conflict_policy: str = "abort"
    ) -> dict[str, Any]:
        from .importing import build_folder_import_plan

        with self.lock:
            return build_folder_import_plan(
                self,
                raw_source,
                include_root=include_root,
                conflict_policy="skip" if str(conflict_policy).lower() == "abort" else conflict_policy,
            ).public()

    def import_local_folder(
        self,
        raw_source: str,
        author: str,
        *,
        include_root: bool = True,
        conflict_policy: str = "abort",
        progress: Any = None,
        cancelled: Any = None,
    ) -> dict[str, Any]:
        from .importing import apply_folder_import, build_folder_import_plan

        plan = build_folder_import_plan(
            self,
            raw_source,
            include_root=include_root,
            conflict_policy=conflict_policy,
            progress=progress,
            cancelled=cancelled,
        )
        return apply_folder_import(
            self,
            plan,
            author,
            progress=progress,
            cancelled=cancelled,
        )

    def rename_path(self, raw_path: str, raw_new_path: str, author: str) -> dict[str, Any]:
        rel, path = self.resolve_path(raw_path)
        new_rel, new_path = self.resolve_path(raw_new_path)
        with self.mutation("path rename"):
            state = self.load_state()
            if not path.exists():
                raise RepositoryError("Source path not found.", HTTPStatus.NOT_FOUND)
            if new_path.exists():
                raise RepositoryError("Destination path already exists.", HTTPStatus.CONFLICT)
            transaction = self._new_transaction(state, "path_rename")
            try:
                transaction.capture(rel, path)
                self._capture_write_target(transaction, new_rel, new_path)
                new_path.parent.mkdir(parents=True, exist_ok=True)
                path.rename(new_path)
                self.record_contribution(
                    state,
                    action="path_renamed",
                    author=author,
                    path=rel,
                    paths=[rel, new_rel],
                    description=f"Renamed {rel} to {new_rel}.",
                    impact=45,
                    metadata={"oldPath": rel, "newPath": new_rel},
                )
                self.save_state(state)
                transaction.commit(self.state_revision(state))
                self.invalidate_index()
                return self.file_entry(new_path)
            except Exception:
                transaction.rollback()
                raise

    def delete_path(self, raw_path: str, author: str) -> dict[str, Any]:
        rel, path = self.resolve_path(raw_path)
        with self.mutation("path deletion"):
            state = self.load_state()
            if not path.exists():
                raise RepositoryError("Path not found.", HTTPStatus.NOT_FOUND)
            affected = [rel]
            if path.is_dir():
                affected.extend(
                    child.relative_to(self.workspace).as_posix()
                    for child in path.rglob("*")
                    if child.is_file()
                )
            transaction = self._new_transaction(state, "path_delete")
            try:
                transaction.capture(rel, path)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                self.record_contribution(
                    state,
                    action="path_deleted",
                    author=author,
                    path=rel,
                    paths=affected,
                    description=f"Deleted {rel} from the working repository.",
                    impact=min(85, 40 + len(affected)),
                    metadata={"deletedCount": len(affected)},
                )
                self.save_state(state)
                transaction.commit(self.state_revision(state))
                self.invalidate_index()
                return {"deleted": rel, "affected": len(affected)}
            except Exception:
                transaction.rollback()
                raise

    def scan_index(self, *, store_objects: bool = False) -> dict[str, Any]:
        if store_objects:
            self.require_writable("snapshot object materialization")
        entries, manifest, directories = self._scan_workspace(need_hashes=True)
        if store_objects:
            for rel, data in manifest.items():
                source = self.workspace / rel
                object_path = self.object_path(data["hash"])
                valid = False
                if object_path.is_file() and object_path.stat().st_size == data["size"]:
                    hasher = hashlib.sha256()
                    with object_path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            hasher.update(chunk)
                    valid = hasher.hexdigest() == data["hash"]
                if not valid:
                    object_path.parent.mkdir(parents=True, exist_ok=True)
                    temp = object_path.with_name(f".{object_path.name}.{uuid.uuid4().hex}.tmp")
                    shutil.copy2(source, temp)
                    os.replace(temp, object_path)
        return {"tree": entries, "manifest": manifest, "directories": directories}

    def manifest(self, *, store_objects: bool = False, persist_index: bool = True) -> dict[str, dict[str, Any]]:
        if store_objects:
            return self.scan_index(store_objects=True)["manifest"]
        return self._scan_workspace(need_hashes=True, persist_index=persist_index)[1]

    def directory_manifest(self) -> list[dict[str, Any]]:
        return self.scan_index(store_objects=False)["directories"]

    @staticmethod
    def diff_manifests(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, list[str]]:
        old_paths, new_paths = set(previous), set(current)
        added = sorted(new_paths - old_paths)
        deleted = sorted(old_paths - new_paths)
        modified = sorted(path for path in old_paths & new_paths if previous[path]["hash"] != current[path]["hash"])
        return {"added": added, "modified": modified, "deleted": deleted}


    def object_path(self, digest: str) -> Path:
        value = str(digest or "").strip().lower()
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise RepositoryError("Snapshot object hash is invalid.", code="invalid_object_hash")
        return self.objects_dir / value[:2] / value[2:]

    @staticmethod
    def diff_directories(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, list[str]]:
        old = {item["path"]: item for item in previous or []}
        new = {item["path"]: item for item in current or []}
        added = sorted(set(new) - set(old))
        deleted = sorted(set(old) - set(new))
        modified = sorted(
            path for path in set(old) & set(new)
            if int(old[path].get("mode", 0)) != int(new[path].get("mode", 0))
        )
        return {"added": added, "modified": modified, "deleted": deleted}

    def ensure_snapshot(self, message: str, author: str) -> dict[str, Any]:
        """Return a snapshot representing the current workspace, creating one when dirty."""
        with self.lock:
            state = self.load_state()
            scan = self.scan_index(store_objects=False)
            latest = state["commits"][-1] if state["commits"] else None
            if latest is not None:
                file_changes = self.diff_manifests(latest.get("manifest", {}), scan["manifest"])
                directory_changes = self.diff_directories(latest.get("directoryManifest", []), scan["directories"])
                if not any(file_changes.values()) and not any(directory_changes.values()):
                    return self.public_commit(latest)
        return self.create_commit(message, author)

    def _append_commit_to_state(
        self,
        state: dict[str, Any],
        scan: dict[str, Any],
        message: str,
        author: str,
        *,
        extra_parents: list[str] | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        current = scan["manifest"]
        directories = scan["directories"]
        previous_commit = state["commits"][-1] if state["commits"] else {}
        changes = self.diff_manifests(previous_commit.get("manifest", {}), current)
        directory_changes = self.diff_directories(previous_commit.get("directoryManifest", []), directories)
        if not any(changes.values()) and not any(directory_changes.values()) and state["commits"] and not allow_empty:
            raise RepositoryError("No file or folder changes exist since the previous snapshot.", HTTPStatus.CONFLICT)
        timestamp = utc_now()
        parent = state["commits"][-1]["id"] if state["commits"] else None
        raw_id = json.dumps({"files": current, "directories": directories}, sort_keys=True) + timestamp + message + (parent or "")
        commit_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:12]
        last_commit_time = state["commits"][-1]["timestamp"] if state["commits"] else ""
        pending = [
            c["id"] for c in state["contributions"]
            if c.get("action") not in {"commit_created", "commit_restored"}
            and c.get("timestamp", "") > last_commit_time
        ]
        pending.extend(extra_parents or [])
        commit = {
            "id": commit_id,
            "parent": parent,
            "message": message,
            "author": (author or state["repository"].get("defaultAuthor") or "Unknown Contributor").strip(),
            "timestamp": timestamp,
            "manifest": current,
            "directoryManifest": directories,
            "changes": changes,
            "directoryChanges": directory_changes,
            "fileCount": len(current),
            "folderCount": len(directories),
            "totalBytes": sum(item["size"] for item in current.values()),
        }
        state["commits"].append(commit)
        touched = sorted(set(
            changes["added"] + changes["modified"] + changes["deleted"]
            + directory_changes["added"] + directory_changes["modified"] + directory_changes["deleted"]
        ))
        contribution = self.record_contribution(
            state,
            action="commit_created",
            author=commit["author"],
            paths=touched,
            description=message,
            impact=min(100, 65 + len(touched) * 2),
            parents=list(dict.fromkeys(pending[-40:])),
            commit_id=commit_id,
            metadata={"changes": changes, "directoryChanges": directory_changes},
        )
        commit["contributionId"] = contribution["id"]
        return commit

    def merge_pull_request(
        self,
        *,
        pull_request_id: str,
        pull_request_number: int,
        title: str,
        contributor: str,
        merged_by: str,
        staged_changes: dict[str, Path],
        deletions: list[str],
        expected_base_hashes: dict[str, str],
    ) -> dict[str, Any]:
        """Apply a reviewed quarantined change set as one filesystem/metadata transaction."""
        with self.mutation("pull request merge"):
            safety = self.ensure_snapshot(f"Safety snapshot before pull request #{pull_request_number}", merged_by)
            state = self.load_state()
            normalized_changes: dict[str, tuple[Path, Path]] = {}
            normalized_deletions: list[tuple[str, Path]] = []
            affected: list[str] = []
            for raw, staged in staged_changes.items():
                rel, destination = self.resolve_path(raw)
                if not staged.exists() or not staged.is_file():
                    raise RepositoryError(f"Staged pull-request file is missing: {rel}", HTTPStatus.INTERNAL_SERVER_ERROR, "staged_file_missing")
                normalized_changes[rel] = (staged, destination)
                affected.append(rel)
            for raw in deletions:
                rel, destination = self.resolve_path(raw)
                normalized_deletions.append((rel, destination))
                affected.append(rel)
            affected = sorted(set(affected))
            if not affected:
                raise RepositoryError("Pull request contains no changes.", code="empty_pull_request")
            current_manifest = self.manifest(store_objects=False)
            race_conflicts: list[dict[str, str]] = []
            for rel in affected:
                expected = str(expected_base_hashes.get(rel) or "")
                current_hash = str(current_manifest.get(rel, {}).get("hash") or "")
                if (expected and current_hash != expected) or (not expected and current_hash):
                    race_conflicts.append({"path": rel, "baseHash": expected, "currentHash": current_hash, "reason": "workspace_changed_before_merge"})
            if race_conflicts:
                raise RepositoryError("Repository changed while the pull request was being merged.", HTTPStatus.CONFLICT, "pull_request_merge_race", {"conflicts": race_conflicts})

            transaction = self._new_transaction(state, "pull_request_merge")
            try:
                for rel in affected:
                    _normalized, destination = self.resolve_path(rel)
                    if destination.exists() and not destination.is_file():
                        raise RepositoryError(f"Pull request path is not a file: {rel}", HTTPStatus.CONFLICT, "merge_path_not_file")
                    self._capture_write_target(transaction, rel, destination)
                for rel, (staged, destination) in normalized_changes.items():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.merge.tmp")
                    try:
                        with staged.open("rb") as source, temporary.open("wb") as target:
                            shutil.copyfileobj(source, target, 1024 * 1024)
                            target.flush()
                            os.fsync(target.fileno())
                        os.replace(temporary, destination)
                    finally:
                        temporary.unlink(missing_ok=True)
                for rel, destination in normalized_deletions:
                    if destination.exists():
                        destination.unlink()
                merge_contribution = self.record_contribution(
                    state,
                    action="pull_request_merged",
                    author=contributor,
                    paths=affected,
                    description=f"Merged pull request #{pull_request_number}: {title}",
                    impact=min(100, 72 + len(affected) * 2),
                    metadata={
                        "pullRequestId": pull_request_id,
                        "pullRequestNumber": pull_request_number,
                        "mergedBy": merged_by,
                        "contributor": contributor,
                        "safetyCommitId": safety["id"],
                    },
                )
                self.invalidate_index()
                scan = self.scan_index(store_objects=True)
                commit = self._append_commit_to_state(
                    state,
                    scan,
                    f"Merge pull request #{pull_request_number}: {title}",
                    merged_by,
                    extra_parents=[merge_contribution["id"]],
                    allow_empty=True,
                )
                self.save_state(state)
                transaction.commit(self.state_revision(state))
                self.invalidate_index()
                return {
                    "pullRequestId": pull_request_id,
                    "pullRequestNumber": pull_request_number,
                    "filesChanged": len(normalized_changes),
                    "filesDeleted": len(normalized_deletions),
                    "safetyCommit": safety,
                    "contribution": merge_contribution,
                    "commit": self.public_commit(commit),
                }
            except Exception:
                transaction.rollback()
                self.invalidate_index()
                raise

    def create_commit(self, message: str, author: str) -> dict[str, Any]:
        message = (message or "").strip()
        if not message:
            raise RepositoryError("A commit message is required.")
        with self.mutation("snapshot creation"):
            state = self.load_state()
            scan = self.scan_index(store_objects=True)
            commit = self._append_commit_to_state(state, scan, message, author)
            self.save_state(state)
            return self.public_commit(commit)

    def public_commit(self, commit: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in commit.items() if key not in {"manifest", "directoryManifest"}}

    def verify_snapshot_objects(
        self, commit: dict[str, Any], *, max_objects: int | None = None
    ) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        verified = 0
        total_bytes = 0
        manifest_items = sorted((commit.get("manifest", {}) or {}).items())
        limit = len(manifest_items) if max_objects is None else max(0, int(max_objects))
        for rel, data in manifest_items[:limit]:
            digest = str(data.get("hash") or "")
            expected_size = int(data.get("size") or 0)
            try:
                object_path = self.object_path(digest)
            except RepositoryError as exc:
                errors.append({"path": rel, "code": "invalid_hash", "message": str(exc)})
                continue
            if not object_path.is_file():
                errors.append({"path": rel, "code": "missing_object", "hash": digest})
                continue
            actual_size = object_path.stat().st_size
            if actual_size != expected_size:
                errors.append({"path": rel, "code": "object_size_mismatch", "expected": expected_size, "actual": actual_size})
                continue
            hasher = hashlib.sha256()
            with object_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            actual_hash = hasher.hexdigest()
            if actual_hash != digest:
                errors.append({"path": rel, "code": "object_hash_mismatch", "expected": digest, "actual": actual_hash})
                continue
            verified += 1
            total_bytes += actual_size
        complete = len(manifest_items) <= limit
        return {
            "valid": not errors,
            "complete": complete,
            "verifiedObjects": verified,
            "scannedObjects": min(len(manifest_items), limit),
            "objectCount": len(manifest_items),
            "remainingObjects": max(0, len(manifest_items) - limit),
            "totalBytes": total_bytes,
            "errors": errors,
        }

    def restore_commit(self, commit_id: str, author: str) -> dict[str, Any]:
        with self.mutation("snapshot restore"):
            state = self.load_state()
            commit = next((c for c in state["commits"] if c["id"] == commit_id), None)
            if not commit:
                raise RepositoryError("Commit not found.", HTTPStatus.NOT_FOUND)
            verification = self.verify_snapshot_objects(commit)
            if not verification["valid"]:
                raise RepositoryError(
                    "Snapshot restore was blocked because one or more content objects are missing or corrupt.",
                    HTTPStatus.CONFLICT,
                    "snapshot_integrity_failed",
                    verification,
                )
            current_bytes = 0
            for item in self.workspace.rglob("*"):
                if self.meta_dir == item or self.meta_dir in item.parents:
                    continue
                try:
                    if item.is_file() and not item.is_symlink():
                        current_bytes += item.stat().st_size
                except OSError:
                    pass
            required = verification["totalBytes"] + current_bytes + max(64 * 1024 * 1024, verification["totalBytes"] // 20)
            free = shutil.disk_usage(self.workspace).free
            if free < required:
                raise RepositoryError(
                    "Not enough free space to stage a safe snapshot restore.",
                    HTTPStatus.INSUFFICIENT_STORAGE,
                    "insufficient_restore_space",
                    {"requiredBytes": required, "freeBytes": free},
                )

            stage_root = self.meta_dir / "restore-staging" / f"restore-{commit_id}-{uuid.uuid4().hex}"
            stage_root.mkdir(parents=True, exist_ok=False)
            directories = list(commit.get("directoryManifest") or [])
            if not directories:
                inferred: set[str] = set()
                for rel in commit.get("manifest", {}):
                    parent = Path(rel).parent
                    while parent.as_posix() not in {".", ""}:
                        inferred.add(parent.as_posix())
                        parent = parent.parent
                directories = [{"path": path, "mode": 0o755, "mtimeNs": 0} for path in sorted(inferred, key=lambda value: (value.count("/"), value.casefold()))]
            try:
                for folder in sorted(directories, key=lambda item: (str(item.get("path", "")).count("/"), str(item.get("path", "")).casefold())):
                    rel = self.normalize_rel(str(folder.get("path") or ""))
                    destination = stage_root / rel
                    destination.mkdir(parents=True, exist_ok=True)
                for rel, data in commit.get("manifest", {}).items():
                    normalized = self.normalize_rel(rel)
                    destination = stage_root / normalized
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(self.object_path(data["hash"]), destination)
                    try:
                        os.chmod(destination, int(data.get("mode", 0o644)))
                        mtime_ns = int(data.get("mtimeNs") or 0)
                        if mtime_ns:
                            os.utime(destination, ns=(mtime_ns, mtime_ns))
                    except OSError:
                        pass
                for folder in sorted(directories, key=lambda item: str(item.get("path", "")).count("/"), reverse=True):
                    destination = stage_root / str(folder.get("path") or "")
                    try:
                        os.chmod(destination, int(folder.get("mode", 0o755)))
                        mtime_ns = int(folder.get("mtimeNs") or 0)
                        if mtime_ns:
                            os.utime(destination, ns=(mtime_ns, mtime_ns))
                    except OSError:
                        pass

                transaction = self._new_transaction(state, "snapshot_restore")
                try:
                    for child in list(self.workspace.iterdir()):
                        if child.name == ".forgetrace":
                            continue
                        transaction.capture(child.name, child)
                        if child.is_dir() and not child.is_symlink():
                            shutil.rmtree(child)
                        else:
                            child.unlink(missing_ok=True)
                    for child in stage_root.iterdir():
                        destination = self.workspace / child.name
                        if child.is_dir():
                            shutil.copytree(child, destination, copy_function=shutil.copy2)
                        else:
                            shutil.copy2(child, destination)
                    contribution = self.record_contribution(
                        state,
                        action="commit_restored",
                        author=author,
                        paths=list(commit.get("manifest", {}).keys()),
                        description=f"Restored snapshot {commit_id}: {commit['message']}",
                        impact=88,
                        parents=[commit.get("contributionId")] if commit.get("contributionId") else [],
                        commit_id=commit_id,
                        metadata={"restoredCommit": commit_id, "verifiedObjects": verification["verifiedObjects"]},
                    )
                    self.save_state(state)
                    transaction.commit(self.state_revision(state))
                    self.invalidate_index()
                    return {
                        "restored": commit_id,
                        "files": len(commit.get("manifest", {})),
                        "folders": len(directories),
                        "verifiedObjects": verification["verifiedObjects"],
                        "contribution": contribution,
                    }
                except Exception:
                    transaction.rollback()
                    raise
            finally:
                shutil.rmtree(stage_root, ignore_errors=True)

    def summary(self, state: dict[str, Any] | None = None, scan: dict[str, Any] | None = None) -> dict[str, Any]:
        state = state or self.load_state(require_initialized=False)
        if not self.initialized():
            return {"initialized": False}
        scan = scan or self.scan_index(store_objects=False)
        entries = scan["tree"]
        files = [entry for entry in entries if entry["type"] == "file"]
        folders = [entry for entry in entries if entry["type"] == "folder"]
        contributors: dict[str, dict[str, Any]] = {}
        for contribution in state["contributions"]:
            author = contribution.get("author") or "Unknown Contributor"
            person = contributors.setdefault(author, {"name": author, "contributions": 0, "impact": 0, "lastActive": "", "actions": {}})
            person["contributions"] += 1
            person["impact"] += int(contribution.get("impact", 0))
            person["lastActive"] = max(person["lastActive"], contribution.get("timestamp", ""))
            action = contribution.get("action", "unknown")
            person["actions"][action] = person["actions"].get(action, 0) + 1
        total_bytes = sum(entry.get("size", 0) for entry in files)
        current_manifest = scan["manifest"]
        last_commit = state["commits"][-1] if state["commits"] else {}
        last_manifest = last_commit.get("manifest", {})
        dirty = self.diff_manifests(last_manifest, current_manifest)
        directory_dirty = self.diff_directories(last_commit.get("directoryManifest", []), scan["directories"])
        return {
            "initialized": True,
            "id": self.repository_id or state["repository"].get("id", ""),
            "path": str(self.workspace),
            "repository": state["repository"],
            "accessPolicy": self.access_policy(state),
            "revision": self.state_revision(state),
            "stats": {
                "files": len(files),
                "folders": len(folders),
                "bytes": total_bytes,
                "commits": len(state["commits"]),
                "contributions": len(state["contributions"]),
                "contributors": len(contributors),
                "dirtyFiles": sum(len(v) for v in dirty.values()),
                "dirtyFolders": sum(len(v) for v in directory_dirty.values()),
            },
            "dirty": dirty,
            "directoryDirty": directory_dirty,
            "contributors": sorted(contributors.values(), key=lambda p: (-p["impact"], p["name"].casefold())),
            "latestCommit": self.public_commit(state["commits"][-1]) if state["commits"] else None,
            "recoveryActions": list(self._recovery_actions),
        }

    def api_state(self) -> dict[str, Any]:
        with self.lock:
            state = self.load_state()
            scan = self.scan_index(store_objects=False)
            return {
                "summary": self.summary(state, scan),
                "tree": scan["tree"],
                "contributions": list(reversed(state["contributions"])),
                "commits": [self.public_commit(c) for c in reversed(state["commits"])],
            }

    def sensitive_file_preview(self, *, include_vcs_metadata: bool = True) -> dict[str, Any]:
        scan = self.scan_index(store_objects=False)
        items: list[dict[str, Any]] = []
        total = 0
        for entry in scan["tree"]:
            if entry["type"] != "file":
                continue
            warnings = path_policy_warnings(entry["path"])
            if not include_vcs_metadata and "version_control_metadata" in warnings:
                continue
            total += int(entry.get("size", 0))
            if warnings:
                items.append({"path": entry["path"], "size": entry.get("size", 0), "warnings": warnings})
        return {"fileCount": len(scan["manifest"]), "totalBytes": total, "sensitiveFiles": items, "sensitiveCount": len(items)}

    @staticmethod
    def _zip_datetime(mtime_ns: int) -> tuple[int, int, int, int, int, int]:
        try:
            dt = datetime.fromtimestamp(max(0, mtime_ns) / 1_000_000_000)
            year = min(2107, max(1980, dt.year))
            return (year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
        except (OSError, OverflowError, ValueError):
            return (1980, 1, 1, 0, 0, 0)

    def _write_export_archive(
        self,
        archive: zipfile.ZipFile,
        *,
        include_history: bool,
        include_vcs_metadata: bool,
        include_sensitive: bool = True,
    ) -> None:
        state = self.load_state()
        scan = self.scan_index(store_objects=False)
        excluded_vcs_dirs = {".git", ".hg", ".svn", ".bzr"}
        included_files: set[str] = set()
        for rel, data in scan["manifest"].items():
            warnings = path_policy_warnings(rel)
            if not include_vcs_metadata and "version_control_metadata" in warnings:
                continue
            if not include_sensitive and "possible_secret_or_credential" in warnings:
                continue
            source_path = self.workspace / rel
            info = zipfile.ZipInfo(rel, date_time=self._zip_datetime(int(data.get("mtimeNs") or 0)))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (int(data.get("mode", 0o644)) & 0xFFFF) << 16
            hasher = hashlib.sha256()
            copied = 0
            try:
                with source_path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        hasher.update(chunk)
                        copied += len(chunk)
                        target.write(chunk)
            except OSError as exc:
                raise RepositoryError(
                    f"Repository export could not read {rel}: {exc}",
                    HTTPStatus.CONFLICT,
                    "export_source_changed",
                    {"path": rel},
                ) from exc
            if copied != int(data.get("size") or 0) or hasher.hexdigest() != str(data.get("hash") or ""):
                raise RepositoryError(
                    "Repository export was blocked because a source file changed during verification.",
                    HTTPStatus.CONFLICT,
                    "export_source_changed",
                    {"path": rel, "expectedSize": int(data.get("size") or 0), "actualSize": copied},
                )
            included_files.add(rel)
        # Preserve empty directories and portable mode metadata.
        for directory in scan["directories"]:
            rel = str(directory.get("path") or "").rstrip("/")
            if not rel:
                continue
            if not include_vcs_metadata and any(part.casefold() in excluded_vcs_dirs for part in Path(rel).parts):
                continue
            if any(path.startswith(rel + "/") for path in included_files):
                continue
            info = zipfile.ZipInfo(rel + "/", date_time=self._zip_datetime(int(directory.get("mtimeNs") or 0)))
            info.external_attr = ((int(directory.get("mode", 0o755)) | 0o040000) & 0xFFFF) << 16
            archive.writestr(info, b"")
        if include_history:
            public_state = {
                "schemaVersion": state.get("schemaVersion", 1),
                "revision": state.get("revision", 0),
                "repository": state["repository"],
                "contributions": state["contributions"],
                "commits": [self.public_commit(c) for c in state["commits"]],
            }
            archive.writestr("FORGETRACE_HISTORY.json", json.dumps(public_state, indent=2, ensure_ascii=False))

    def export_zip_to_path(
        self,
        destination: Path,
        include_history: bool = True,
        *,
        include_vcs_metadata: bool = True,
        include_sensitive: bool = True,
    ) -> Path:
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with self.lock:
                with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    self._write_export_archive(
                        archive,
                        include_history=include_history,
                        include_vcs_metadata=include_vcs_metadata,
                        include_sensitive=include_sensitive,
                    )
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
        return destination

    def export_zip(
        self,
        include_history: bool = True,
        *,
        include_vcs_metadata: bool = True,
        include_sensitive: bool = True,
    ) -> bytes:
        output = BytesIO()
        with self.lock:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                self._write_export_archive(
                    archive,
                    include_history=include_history,
                    include_vcs_metadata=include_vcs_metadata,
                    include_sensitive=include_sensitive,
                )
        return output.getvalue()
