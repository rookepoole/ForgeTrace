from __future__ import annotations

import hashlib
import os
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable

from .constants import MAX_IMPORT_TOTAL_BYTES
from .errors import RepositoryError
from .jobs import JobContext
from .policies import is_protected_metadata_path, path_policy_warnings
from .transactions import FilesystemTransaction


@dataclass
class ImportFile:
    relative_path: str
    source: Path
    size: int
    sha256: str
    mode: int
    mtime_ns: int
    destination_path: str = ""
    conflict: str = "none"


@dataclass
class ImportFolder:
    relative_path: str
    mode: int
    mtime_ns: int
    destination_path: str = ""


@dataclass
class FolderImportPlan:
    source: Path
    source_name: str
    include_root: bool
    conflict_policy: str
    files: list[ImportFile] = field(default_factory=list)
    folders: list[ImportFolder] = field(default_factory=list)
    skipped_symlinks: list[str] = field(default_factory=list)
    skipped_metadata: list[str] = field(default_factory=list)
    sensitive: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    total_bytes: int = 0
    required_free_bytes: int = 0
    free_bytes: int = 0

    def public(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "sourceName": self.source_name,
            "includeRoot": self.include_root,
            "conflictPolicy": self.conflict_policy,
            "fileCount": len(self.files),
            "folderCount": len(self.folders),
            "totalBytes": self.total_bytes,
            "requiredFreeBytes": self.required_free_bytes,
            "freeBytes": self.free_bytes,
            "conflicts": list(self.conflicts),
            "sensitiveFiles": list(self.sensitive),
            "skippedSymlinks": list(self.skipped_symlinks),
            "skippedMetadata": list(self.skipped_metadata),
            "files": [item.destination_path for item in self.files],
            "folders": [item.destination_path for item in self.folders],
        }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_destination(repository: Any, desired: str, reserved: set[str]) -> str:
    path = Path(desired)
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    counter = 2
    candidate = desired
    while candidate.casefold() in reserved or repository.resolve_path(candidate)[1].exists():
        candidate = (parent / f"{stem}-{counter}{suffix}").as_posix()
        counter += 1
    reserved.add(candidate.casefold())
    return candidate


def build_folder_import_plan(
    repository: Any,
    raw_source: str,
    *,
    include_root: bool,
    conflict_policy: str = "abort",
    progress: Callable[..., None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> FolderImportPlan:
    policy = str(conflict_policy or "abort").strip().lower()
    if policy not in {"abort", "skip", "overwrite", "rename"}:
        raise RepositoryError("Conflict policy must be abort, skip, overwrite, or rename.", code="invalid_conflict_policy")
    source_text = str(raw_source or "").strip()
    if not source_text:
        raise RepositoryError("A source folder is required.")
    source = Path(source_text).expanduser().resolve()
    if not source.is_dir():
        raise RepositoryError("The selected source folder is unavailable.", HTTPStatus.NOT_FOUND)
    destination_root = (repository.workspace / source.name if include_root else repository.workspace).resolve()
    if source == repository.workspace:
        raise RepositoryError("The repository cannot import itself.", HTTPStatus.CONFLICT)
    if destination_root == source or destination_root in source.parents:
        raise RepositoryError("The import destination cannot contain the source folder.", HTTPStatus.CONFLICT)
    if source in destination_root.parents:
        raise RepositoryError("The source folder cannot contain the import destination.", HTTPStatus.CONFLICT)

    plan = FolderImportPlan(source, source.name, include_root, policy)
    prefix = source.name if include_root else ""
    reserved: set[str] = set()
    scanned = 0

    def check_cancel() -> None:
        if cancelled and cancelled():
            from .jobs import JobCancelled
            raise JobCancelled("Folder import cancelled during discovery.")

    def on_error(error: OSError) -> None:
        raise RepositoryError(
            f"ForgeTrace could not read part of the selected folder: {error}",
            HTTPStatus.FORBIDDEN,
            "local_folder_unreadable",
        )

    for root, dirs, files in os.walk(source, followlinks=False, onerror=on_error):
        check_cancel()
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        filtered_dirs: list[str] = []
        for dirname in sorted(dirs, key=str.casefold):
            candidate = root_path / dirname
            rel_from_source = (relative_root / dirname).as_posix()
            destination_rel = "/".join(part for part in (prefix, rel_from_source) if part)
            if candidate.is_symlink():
                plan.skipped_symlinks.append(destination_rel)
                continue
            if is_protected_metadata_path(rel_from_source):
                plan.skipped_metadata.append(destination_rel)
                continue
            filtered_dirs.append(dirname)
            info = candidate.stat()
            plan.folders.append(ImportFolder(rel_from_source, stat.S_IMODE(info.st_mode), info.st_mtime_ns, destination_rel))
        dirs[:] = filtered_dirs
        for filename in sorted(files, key=str.casefold):
            check_cancel()
            candidate = root_path / filename
            rel_from_source = (relative_root / filename).as_posix()
            destination_rel = "/".join(part for part in (prefix, rel_from_source) if part)
            if candidate.is_symlink():
                plan.skipped_symlinks.append(destination_rel)
                continue
            if is_protected_metadata_path(rel_from_source):
                plan.skipped_metadata.append(destination_rel)
                continue
            info = candidate.stat()
            size = info.st_size
            if size > repository.upload_limit_bytes:
                raise RepositoryError(
                    f"{destination_rel} exceeds this repository's {repository.upload_limit_bytes / (1024 * 1024):g} MB file limit.",
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "repository_upload_limit_exceeded",
                    {"path": destination_rel, "limitBytes": repository.upload_limit_bytes, "fileBytes": size},
                )
            plan.total_bytes += size
            if plan.total_bytes > MAX_IMPORT_TOTAL_BYTES:
                raise RepositoryError(
                    "The selected folder exceeds ForgeTrace's total import limit.",
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "repository_import_total_limit_exceeded",
                    {"limitBytes": MAX_IMPORT_TOTAL_BYTES, "totalBytes": plan.total_bytes},
                )
            digest = _hash_file(candidate)
            warnings = path_policy_warnings(destination_rel)
            if warnings:
                plan.sensitive.append({"path": destination_rel, "warnings": warnings, "size": size})
            plan.files.append(
                ImportFile(
                    rel_from_source,
                    candidate,
                    size,
                    digest,
                    stat.S_IMODE(info.st_mode),
                    info.st_mtime_ns,
                    destination_rel,
                )
            )
            scanned += 1
            if progress:
                progress(phase="scanning", filesScanned=scanned, bytesScanned=plan.total_bytes, currentPath=rel_from_source)

    if include_root:
        source_info = source.stat()
        plan.folders.insert(0, ImportFolder("", stat.S_IMODE(source_info.st_mode), source_info.st_mtime_ns, source.name))

    # Resolve conflicts only after the complete source has been read and hashed.
    for folder in plan.folders:
        _rel, destination = repository.resolve_path(folder.destination_path)
        if destination.exists() and not destination.is_dir():
            plan.conflicts.append({"path": folder.destination_path, "kind": "folder_vs_file"})
    for item in plan.files:
        _rel, destination = repository.resolve_path(item.destination_path)
        if not destination.exists():
            reserved.add(item.destination_path.casefold())
            continue
        if destination.is_dir():
            item.conflict = "folder_vs_file"
        else:
            item.conflict = "existing_file"
        if policy == "rename":
            item.destination_path = _unique_destination(repository, item.destination_path, reserved)
            item.conflict = "renamed"
        elif policy == "skip":
            item.conflict = "skipped"
        plan.conflicts.append({"path": item.relative_path, "destination": item.destination_path, "kind": item.conflict})

    blocking = [item for item in plan.conflicts if item["kind"] in {"folder_vs_file", "existing_file"}]
    if policy == "abort" and blocking:
        raise RepositoryError(
            "Folder import would overwrite or conflict with existing repository paths.",
            HTTPStatus.CONFLICT,
            "folder_import_conflicts",
            {"conflicts": blocking[:500], "conflictCount": len(blocking)},
        )
    if any(item["kind"] == "folder_vs_file" for item in plan.conflicts):
        raise RepositoryError(
            "Folder import contains paths that conflict with existing folders or files.",
            HTTPStatus.CONFLICT,
            "folder_import_type_conflicts",
            {"conflicts": plan.conflicts[:500]},
        )

    overwrite_bytes = 0
    if policy == "overwrite":
        for item in plan.files:
            _rel, destination = repository.resolve_path(item.destination_path)
            if destination.is_file():
                overwrite_bytes += destination.stat().st_size
    plan.required_free_bytes = plan.total_bytes * 2 + overwrite_bytes + max(64 * 1024 * 1024, plan.total_bytes // 20)
    plan.free_bytes = shutil.disk_usage(repository.workspace).free
    if plan.free_bytes < plan.required_free_bytes:
        raise RepositoryError(
            "Not enough free space to stage and safely apply this folder import.",
            HTTPStatus.INSUFFICIENT_STORAGE,
            "insufficient_import_space",
            {"requiredBytes": plan.required_free_bytes, "freeBytes": plan.free_bytes, "totalBytes": plan.total_bytes},
        )
    return plan


def apply_folder_import(
    repository: Any,
    plan: FolderImportPlan,
    author: str,
    *,
    progress: Callable[..., None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    stage_root = repository.meta_dir / "import-staging" / f"import-{uuid.uuid4().hex}"
    stage_files = stage_root / "files"
    stage_files.mkdir(parents=True, exist_ok=False)

    def check_cancel() -> None:
        if cancelled and cancelled():
            from .jobs import JobCancelled
            raise JobCancelled("Folder import cancelled before repository changes were applied.")

    staged = 0
    staged_bytes = 0
    try:
        for item in plan.files:
            check_cancel()
            if item.conflict == "skipped":
                continue
            destination = stage_files / item.destination_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            digest = hashlib.sha256()
            with item.source.open("rb") as source_handle, temporary.open("wb") as target_handle:
                for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                    target_handle.write(chunk)
                    digest.update(chunk)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            if digest.hexdigest() != item.sha256 or temporary.stat().st_size != item.size:
                temporary.unlink(missing_ok=True)
                raise RepositoryError(
                    f"Staging verification failed for {item.destination_path}.",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "folder_import_staging_mismatch",
                )
            os.replace(temporary, destination)
            staged += 1
            staged_bytes += item.size
            if progress:
                progress(phase="staging", filesStaged=staged, bytesStaged=staged_bytes, totalFiles=len(plan.files), totalBytes=plan.total_bytes, currentPath=item.destination_path)

        check_cancel()
        with repository.mutation("local folder import"):
            state = repository.load_state()
            conflicts_overwritten = [item for item in plan.files if item.conflict == "existing_file"]
            safety = None
            if conflicts_overwritten and plan.conflict_policy == "overwrite":
                safety = repository.ensure_snapshot("Safety snapshot before folder overwrite", author)
                state = repository.load_state()
            revision_before = repository.state_revision(state)
            transaction = FilesystemTransaction(
                repository.workspace,
                repository.meta_dir,
                operation="folder_import",
                state_revision_before=revision_before,
            )
            imported_files: list[str] = []
            created_folders: list[str] = []
            try:
                for folder in sorted(plan.folders, key=lambda item: (item.destination_path.count("/"), item.destination_path.casefold())):
                    _rel, destination = repository.resolve_path(folder.destination_path)
                    if not destination.exists():
                        transaction.capture(folder.destination_path, destination)
                        destination.mkdir(parents=True, exist_ok=True)
                        created_folders.append(folder.destination_path)
                applied = 0
                applied_bytes = 0
                for item in plan.files:
                    if item.conflict == "skipped":
                        continue
                    _rel, destination = repository.resolve_path(item.destination_path)
                    transaction.capture(item.destination_path, destination)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    staged_path = stage_files / item.destination_path
                    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.import.tmp")
                    shutil.copyfile(staged_path, temporary)
                    with temporary.open("rb") as handle:
                        digest = hashlib.sha256()
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if temporary.stat().st_size != item.size or digest.hexdigest() != item.sha256:
                        temporary.unlink(missing_ok=True)
                        raise RepositoryError(
                            f"Destination verification failed for {item.destination_path}.",
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            "folder_import_destination_mismatch",
                        )
                    os.replace(temporary, destination)
                    try:
                        os.chmod(destination, item.mode)
                        os.utime(destination, ns=(item.mtime_ns, item.mtime_ns))
                    except OSError:
                        pass
                    imported_files.append(item.destination_path)
                    applied += 1
                    applied_bytes += item.size
                    if progress:
                        progress(phase="applying", filesApplied=applied, bytesApplied=applied_bytes, totalFiles=len(plan.files), totalBytes=plan.total_bytes, currentPath=item.destination_path)

                # Apply directory metadata from deepest to shallowest after files exist.
                for folder in sorted(plan.folders, key=lambda item: item.destination_path.count("/"), reverse=True):
                    _rel, destination = repository.resolve_path(folder.destination_path)
                    try:
                        os.chmod(destination, folder.mode)
                        os.utime(destination, ns=(folder.mtime_ns, folder.mtime_ns))
                    except OSError:
                        pass

                contribution = repository.record_contribution(
                    state,
                    action="folder_imported",
                    author=author,
                    path=plan.source_name if plan.include_root else "",
                    paths=imported_files[:1000],
                    description=f"Imported {len(imported_files)} verified files across {len(plan.folders)} folders.",
                    impact=min(100, 55 + min(40, len(imported_files))),
                    metadata={
                        "fileCount": len(imported_files),
                        "folderCount": len(plan.folders),
                        "totalBytes": sum(item.size for item in plan.files if item.conflict != "skipped"),
                        "conflictPolicy": plan.conflict_policy,
                        "conflictCount": len(plan.conflicts),
                        "sensitiveFileCount": len(plan.sensitive),
                        "skippedSymlinkCount": len(plan.skipped_symlinks),
                        "skippedMetadataCount": len(plan.skipped_metadata),
                        "safetySnapshot": safety,
                    },
                )
                repository.save_state(state)
                transaction.commit(repository.state_revision(state))
            except Exception:
                transaction.rollback()
                raise

        result = plan.public()
        result.update({
            "verified": True,
            "importedFiles": imported_files,
            "createdFolders": created_folders,
            "contribution": contribution,
            "safetySnapshot": safety,
        })
        return result
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def run_import_job(repository: Any, source: str, author: str, include_root: bool, conflict_policy: str, context: JobContext) -> dict[str, Any]:
    context.update(phase="scanning", message="Scanning and hashing the selected folder.")
    plan = build_folder_import_plan(
        repository,
        source,
        include_root=include_root,
        conflict_policy=conflict_policy,
        progress=context.update,
        cancelled=context.cancelled,
    )
    context.update(phase="staging", message="Staging verified copies outside the live repository.", preview=plan.public())
    result = apply_folder_import(repository, plan, author, progress=context.update, cancelled=context.cancelled)
    context.update(phase="completed", message="Folder import verified and committed.")
    return result
