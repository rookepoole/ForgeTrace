from __future__ import annotations

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
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterable

from .errors import ForgeTraceError
from .git_intelligence import CONTROL_PATTERN, GitIntelligenceService
from .git_writes import GitWriteService
from .locks import InterProcessRLock
from .utils import utc_now

GIT_SWITCH_PLAN_SCHEMA_VERSION = 1
GIT_SWITCH_PREVIEW_TTL_SECONDS = 5 * 60
MAX_TOUCHED_PATHS = 10_000
MAX_UNTRACKED_ENTRIES = 5_000
MAX_CAPTURE_BYTES = 512 * 1024 * 1024
MINIMUM_FREE_SPACE_RESERVE_BYTES = 64 * 1024 * 1024
MAX_PLAN_BYTES = 8 * 1024 * 1024
MAX_LOCAL_BRANCHES = 1_000
MAX_SCAN_ENTRIES = 100_000
PLAN_ID_RE = re.compile(r"switch_plan_[0-9a-f]{32}\Z")
DIRECT_HEAD_RE = re.compile(r"refs/heads/(.+)\Z")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
REGULAR_GIT_MODES = {"100644", "100755"}
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
CHECKOUT_ATTRIBUTE_NAMES = ("filter", "working-tree-encoding", "text", "eol", "ident")


@dataclass(frozen=True)
class GitTreeEntry:
    path: str
    mode: str
    oid: str
    object_type: str


class GitSwitchService:
    """Read-only local-branch switch preflight and sealed capture planner.

    This service deliberately has no execute method. It never invokes ``git switch``
    or ``git checkout`` and is not exposed through an HTTP route or UI control. It
    shares the accepted repository and Git mutation locks so that its evidence is
    captured against a stable ForgeTrace state, while native Git activity still
    fails closed through lock/admin-state detection and final revalidation.
    """

    def __init__(
        self,
        *,
        registry,
        git_intelligence: GitIntelligenceService,
        git_writes: GitWriteService,
        timeout_seconds: float = 30.0,
        max_touched_paths: int = MAX_TOUCHED_PATHS,
        max_untracked_entries: int = MAX_UNTRACKED_ENTRIES,
        max_capture_bytes: int = MAX_CAPTURE_BYTES,
        minimum_free_space_reserve_bytes: int = MINIMUM_FREE_SPACE_RESERVE_BYTES,
    ) -> None:
        self.registry = registry
        self.git_intelligence = git_intelligence
        self.git_writes = git_writes
        self.git_executable = git_writes.git_executable
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 120.0))
        self.max_touched_paths = max(1, min(int(max_touched_paths), MAX_TOUCHED_PATHS))
        self.max_untracked_entries = max(1, min(int(max_untracked_entries), MAX_UNTRACKED_ENTRIES))
        self.max_capture_bytes = max(1, min(int(max_capture_bytes), MAX_CAPTURE_BYTES))
        self.minimum_free_space_reserve_bytes = max(
            0, min(int(minimum_free_space_reserve_bytes), MINIMUM_FREE_SPACE_RESERVE_BYTES)
        )
        self.root = registry.data_dir / "git-switches"
        self.plans_dir = self.root / "plans"
        self.staging_dir = self.root / "staging"
        self.locks_dir = self.root / "locks"
        for directory in (self.plans_dir, self.staging_dir, self.locks_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._locks_guard = threading.Lock()
        self._locks: dict[str, InterProcessRLock] = {}
        self._cleanup_incomplete_staging()

    @staticmethod
    def _canonical_digest(payload: Any) -> str:
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _state_digest(self, analysis: dict[str, Any]) -> str:
        payload = json.loads(json.dumps(analysis, ensure_ascii=False))
        payload.pop("stateDigest", None)
        estimate = payload.get("captureEstimate")
        if isinstance(estimate, dict):
            estimate.pop("availableFreeBytes", None)
        return self._canonical_digest(payload)

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

    @classmethod
    def _atomic_write_bytes(cls, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            cls._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _atomic_write_json(cls, path: Path, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        if len(data) > MAX_PLAN_BYTES:
            raise ForgeTraceError(
                "The sealed branch-switch plan exceeds the safe evidence limit.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "git_switch_plan_size_limit",
                {"limitBytes": MAX_PLAN_BYTES, "actualBytes": len(data)},
            )
        cls._atomic_write_bytes(path, data)

    def _lock(self, repository_id: str) -> InterProcessRLock:
        key = hashlib.sha256(str(repository_id).encode("utf-8")).hexdigest()
        with self._locks_guard:
            return self._locks.setdefault(
                repository_id,
                InterProcessRLock(self.locks_dir / f"{key}.lock", timeout=60.0),
            )

    def _cleanup_incomplete_staging(self) -> None:
        for path in sorted(self.staging_dir.iterdir() if self.staging_dir.exists() else []):
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                # Staging cleanup is non-authoritative. A future plan never trusts or
                # references an uninstalled staging directory.
                continue

    @staticmethod
    def _safe_path_text(raw: bytes, *, label: str) -> str:
        try:
            value = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ForgeTraceError(
                f"{label} contains a non-UTF-8 path and is outside the first switch slice.",
                HTTPStatus.CONFLICT,
                "git_switch_path_encoding_unsupported",
            ) from exc
        value = value.replace("\\", "/").strip("/")
        if (
            not value
            or value == ".."
            or value.startswith("../")
            or "/../" in f"/{value}/"
            or CONTROL_RE.search(value)
        ):
            raise ForgeTraceError(
                f"{label} contains an unsafe path.",
                HTTPStatus.CONFLICT,
                "git_switch_path_unsupported",
                {"path": value[:1024]},
            )
        return value

    @staticmethod
    def _protected_path(path: str) -> bool:
        value = str(path or "").replace("\\", "/").strip("/")
        return any(part in {".git", ".forgetrace"} for part in value.split("/") if part)

    @staticmethod
    def _is_reparse(stat_result: os.stat_result) -> bool:
        return bool(getattr(stat_result, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)

    @classmethod
    def _require_regular_lstat(cls, path: Path, *, code: str, label: str) -> os.stat_result:
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise ForgeTraceError(
                f"{label} disappeared during switch planning.",
                HTTPStatus.CONFLICT,
                "git_switch_state_changed",
                {"path": str(path)},
            ) from exc
        if cls._is_reparse(info) or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ForgeTraceError(
                f"{label} is not a supported regular file.",
                HTTPStatus.CONFLICT,
                code,
                {"path": str(path)},
            )
        return info

    @staticmethod
    def _stat_identity(info: os.stat_result) -> dict[str, Any]:
        return {
            "sizeBytes": int(info.st_size),
            "mode": stat.S_IMODE(info.st_mode),
            "mtimeNs": int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
            "device": int(getattr(info, "st_dev", 0)),
            "inode": int(getattr(info, "st_ino", 0)),
            "fileAttributes": int(getattr(info, "st_file_attributes", 0)),
        }

    def _run(
        self,
        root: Path,
        args: list[str],
        *,
        accepted_codes: set[int] | None = None,
        input_data: bytes | None = None,
        output_limit: int = 8 * 1024 * 1024,
        operation: str = "switch_preflight",
    ):
        try:
            return self.git_writes._run(
                root,
                args,
                accepted_codes=accepted_codes,
                input_data=input_data,
                env_extra={"GIT_OPTIONAL_LOCKS": "0"},
                timeout=self.timeout_seconds,
                output_limit=output_limit,
                operation=operation,
            )
        except ForgeTraceError as exc:
            if exc.code.startswith("git_write_"):
                code = exc.code.replace("git_write_", "git_switch_", 1)
                raise ForgeTraceError(str(exc), exc.status, code, dict(exc.details)) from exc
            raise

    def _context(self, repository_id: str):
        repository, root, git_dir = self.git_writes._context(repository_id)
        return repository, root, git_dir

    def _assert_authority(self, repository_id: str, repository: Any, git_dir: Path) -> None:
        repository.require_writable("transactional local branch-switch planning")
        self.registry._require_repository_not_deleting(repository_id)
        native = self.git_writes._native_locks(git_dir)
        if native:
            raise ForgeTraceError(
                "Native Git lock files are present. Complete or stop the external Git operation before planning a branch switch.",
                HTTPStatus.LOCKED,
                "git_switch_native_lock_present",
                {"paths": native},
            )
        administrative = self.git_writes._active_administrative_paths(git_dir)
        if administrative:
            raise ForgeTraceError(
                "Git merge, rebase, cherry-pick, revert, bisect, or sequencer state is active.",
                HTTPStatus.LOCKED,
                "git_switch_administrative_state_present",
                {"paths": administrative},
            )
        write_status = self.git_writes.status(repository_id, receipt_limit=1)
        pending = list(write_status.get("pendingTransactions") or [])
        if pending:
            raise ForgeTraceError(
                "A transactional Git write still has retained recovery evidence. Resolve it before planning a branch switch.",
                HTTPStatus.LOCKED,
                "git_switch_git_write_recovery_pending",
                {"transactionIds": [str(item.get("transactionId") or "") for item in pending[:20]]},
            )

    def _git_identity(self, root: Path) -> dict[str, Any]:
        executable = Path(str(self.git_executable or "")).resolve(strict=True)
        info = executable.stat()
        version = self._run(root, ["--version"], operation="git_identity").stdout.decode("utf-8", errors="replace").strip()
        return {
            "path": str(executable),
            "version": CONTROL_PATTERN.sub("�", version)[:512],
            "sizeBytes": int(info.st_size),
            "mtimeNs": int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
            "device": int(getattr(info, "st_dev", 0)),
            "inode": int(getattr(info, "st_ino", 0)),
        }

    def _local_config_state(self, root: Path, git_dir: Path) -> dict[str, Any]:
        result = self._run(
            root,
            ["config", "--local", "--no-includes", "--null", "--list"],
            accepted_codes={0, 1},
            operation="local_config",
        )
        values: dict[str, list[str]] = {}
        for item in [part for part in result.stdout.split(b"\0") if part]:
            if b"\n" in item:
                key_raw, value_raw = item.split(b"\n", 1)
            elif b" " in item:
                key_raw, value_raw = item.split(b" ", 1)
            else:
                key_raw, value_raw = item, b""
            key = key_raw.decode("utf-8", errors="replace").strip().lower()
            value = value_raw.decode("utf-8", errors="replace").strip()
            values.setdefault(key, []).append(value)

        def truthy(key: str) -> bool:
            return any(value.lower() in {"1", "true", "yes", "on"} for value in values.get(key, []))

        unsupported: list[str] = []
        for key in sorted(values):
            if key.startswith("filter."):
                unsupported.append("filter.*")
            elif key == "core.attributesfile" and any(values[key]):
                unsupported.append("core.attributesFile")
        if truthy("core.sparsecheckout") or (git_dir / "info" / "sparse-checkout").exists():
            unsupported.append("sparse_checkout")
        if truthy("index.sparse"):
            unsupported.append("sparse_index")
        if truthy("core.splitindex") or any(git_dir.glob("sharedindex.*")):
            unsupported.append("split_index")
        if truthy("extensions.worktreeconfig"):
            unsupported.append("worktree_config")
        auto_crlf = [value.lower() for value in values.get("core.autocrlf", []) if value]
        if any(value not in {"false", "0", "no", "off"} for value in auto_crlf):
            unsupported.append("core.autocrlf")
        if any(values.get("core.eol", [])):
            unsupported.append("core.eol")
        if (git_dir / "info" / "attributes").exists():
            unsupported.append("info_attributes")
        if unsupported:
            raise ForgeTraceError(
                "The repository has checkout-affecting configuration outside the deterministic first switch slice.",
                HTTPStatus.CONFLICT,
                "git_switch_checkout_configuration_unsupported",
                {"features": sorted(set(unsupported))},
            )
        relevant = {
            key: values.get(key, [])
            for key in (
                "core.autocrlf",
                "core.eol",
                "core.ignorecase",
                "core.precomposeunicode",
                "core.symlinks",
                "core.sparsecheckout",
                "core.splitindex",
                "index.sparse",
                "extensions.objectformat",
                "extensions.worktreeconfig",
            )
            if key in values
        }
        return {
            "relevant": relevant,
            "localConfigDigest": self._hash_bytes(result.stdout),
            "globalAndSystemConfigDisabled": True,
            "externalAttributesAbsent": True,
        }

    def _list_local_branches(self, root: Path) -> list[dict[str, str]]:
        fmt = "%(refname)%00%(objectname)%00%(objecttype)%00%(symref)"
        result = self._run(
            root,
            ["for-each-ref", f"--count={MAX_LOCAL_BRANCHES + 1}", f"--format={fmt}", "refs/heads"],
            operation="list_local_branches",
        )
        branches: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            fields = line.split(b"\0")
            if len(fields) < 4:
                continue
            full_ref = fields[0].decode("utf-8", errors="strict")
            oid = fields[1].decode("ascii", errors="strict")
            object_type = fields[2].decode("ascii", errors="replace")
            symref = fields[3].decode("utf-8", errors="replace")
            match = DIRECT_HEAD_RE.fullmatch(full_ref)
            if not match or CONTROL_RE.search(full_ref):
                raise ForgeTraceError(
                    "A local branch ref has an unsupported name.",
                    HTTPStatus.CONFLICT,
                    "git_switch_branch_ref_unsupported",
                    {"ref": full_ref[:1024]},
                )
            branches.append(
                {
                    "name": match.group(1),
                    "ref": full_ref,
                    "oid": oid.lower(),
                    "objectType": object_type,
                    "symref": symref,
                }
            )
        if len(branches) > MAX_LOCAL_BRANCHES:
            raise ForgeTraceError(
                "The local branch list exceeds the bounded switch read-model limit.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "git_switch_branch_limit",
                {"limit": MAX_LOCAL_BRANCHES},
            )
        return branches

    def _head_and_target(self, root: Path, target_name: str | None = None) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, str]]]:
        symbolic = self._run(
            root,
            ["symbolic-ref", "--quiet", "HEAD"],
            accepted_codes={0, 1},
            operation="source_head",
        )
        if symbolic.returncode != 0:
            raise ForgeTraceError(
                "The current HEAD is detached; the first branch-switch slice requires an attached local branch.",
                HTTPStatus.CONFLICT,
                "git_switch_detached_head",
            )
        source_ref = symbolic.stdout.decode("utf-8", errors="strict").strip()
        match = DIRECT_HEAD_RE.fullmatch(source_ref)
        if not match:
            raise ForgeTraceError(
                "HEAD is not attached directly to a local refs/heads branch.",
                HTTPStatus.CONFLICT,
                "git_switch_source_ref_unsupported",
                {"ref": source_ref[:1024]},
            )
        branches = self._list_local_branches(root)
        by_ref = {branch["ref"]: branch for branch in branches}
        source_branch = by_ref.get(source_ref)
        if source_branch is None or source_branch.get("symref"):
            raise ForgeTraceError(
                "The current local branch ref is missing or symbolic.",
                HTTPStatus.CONFLICT,
                "git_switch_source_ref_unsupported",
                {"ref": source_ref},
            )
        source_oid = self._run(
            root,
            ["rev-parse", "--verify", "HEAD^{commit}"],
            accepted_codes={0, 1},
            operation="source_commit",
        )
        if source_oid.returncode != 0:
            raise ForgeTraceError(
                "The current local branch is unborn.",
                HTTPStatus.CONFLICT,
                "git_switch_unborn_head",
            )
        resolved_source = source_oid.stdout.decode("ascii", errors="strict").strip().lower()
        if resolved_source != source_branch["oid"]:
            raise ForgeTraceError(
                "HEAD and its direct local branch ref disagree.",
                HTTPStatus.CONFLICT,
                "git_switch_source_ref_drift",
            )
        source_tree = self._run(
            root,
            ["rev-parse", "--verify", f"{resolved_source}^{{tree}}"],
            operation="source_tree",
        ).stdout.decode("ascii", errors="strict").strip().lower()
        source = {
            "name": match.group(1),
            "ref": source_ref,
            "oid": resolved_source,
            "treeOid": source_tree,
        }
        if target_name is None:
            return source, None, branches
        candidate_name = str(target_name or "").strip()
        if not candidate_name or CONTROL_RE.search(candidate_name) or candidate_name.startswith("refs/"):
            raise ForgeTraceError(
                "Choose an existing local branch from the bounded branch read model.",
                code="git_switch_target_invalid",
            )
        target_ref = f"refs/heads/{candidate_name}"
        target = by_ref.get(target_ref)
        if target is None:
            raise ForgeTraceError(
                "The selected target is not an existing direct local branch.",
                HTTPStatus.NOT_FOUND,
                "git_switch_target_not_found",
                {"targetBranch": candidate_name},
            )
        if target_ref == source_ref:
            raise ForgeTraceError(
                "Choose a different local branch.",
                HTTPStatus.CONFLICT,
                "git_switch_target_is_current",
            )
        if target.get("symref") or target.get("objectType") != "commit":
            raise ForgeTraceError(
                "The selected target is not a direct local branch pointing to a commit.",
                HTTPStatus.CONFLICT,
                "git_switch_target_ref_unsupported",
                {"targetRef": target_ref},
            )
        target_tree = self._run(
            root,
            ["rev-parse", "--verify", f"{target['oid']}^{{tree}}"],
            operation="target_tree",
        ).stdout.decode("ascii", errors="strict").strip().lower()
        return source, {
            "name": candidate_name,
            "ref": target_ref,
            "oid": target["oid"],
            "treeOid": target_tree,
        }, branches

    def _tree(self, root: Path, tree_oid: str, *, label: str) -> dict[str, GitTreeEntry]:
        result = self._run(
            root,
            ["ls-tree", "-r", "-z", "--full-tree", tree_oid],
            output_limit=64 * 1024 * 1024,
            operation=f"{label}_tree_manifest",
        )
        entries: dict[str, GitTreeEntry] = {}
        for raw in [item for item in result.stdout.split(b"\0") if item]:
            try:
                metadata, raw_path = raw.split(b"\t", 1)
                mode_raw, type_raw, oid_raw = metadata.split(b" ", 2)
            except ValueError as exc:
                raise ForgeTraceError(
                    f"The {label} tree contains an unsupported entry encoding.",
                    HTTPStatus.CONFLICT,
                    "git_switch_tree_manifest_invalid",
                ) from exc
            path = self._safe_path_text(raw_path, label=f"{label} tree")
            mode = mode_raw.decode("ascii", errors="strict")
            object_type = type_raw.decode("ascii", errors="strict")
            oid = oid_raw.decode("ascii", errors="strict").lower()
            if self._protected_path(path):
                raise ForgeTraceError(
                    "Git or ForgeTrace administrative paths cannot be materialized by a branch switch.",
                    HTTPStatus.FORBIDDEN,
                    "git_switch_protected_path",
                    {"path": path},
                )
            if mode not in REGULAR_GIT_MODES or object_type != "blob":
                raise ForgeTraceError(
                    "The source or target tree contains a symlink, gitlink, submodule, or other unsupported entry.",
                    HTTPStatus.CONFLICT,
                    "git_switch_tree_entry_unsupported",
                    {"path": path, "mode": mode, "type": object_type},
                )
            if path in entries:
                raise ForgeTraceError(
                    "The branch tree contains duplicate path entries.",
                    HTTPStatus.CONFLICT,
                    "git_switch_tree_manifest_invalid",
                    {"path": path},
                )
            entries[path] = GitTreeEntry(path, mode, oid, object_type)
        if any(path == ".gitattributes" or path.endswith("/.gitattributes") for path in entries):
            raise ForgeTraceError(
                "Versioned .gitattributes files are outside the deterministic first switch slice.",
                HTTPStatus.CONFLICT,
                "git_switch_checkout_attributes_unsupported",
                {"tree": label},
            )
        return entries

    def _index_entries(self, root: Path) -> dict[str, GitTreeEntry]:
        result = self._run(root, ["ls-files", "--stage", "-z"], operation="index_manifest")
        entries: dict[str, GitTreeEntry] = {}
        for raw in [item for item in result.stdout.split(b"\0") if item]:
            try:
                metadata, raw_path = raw.split(b"\t", 1)
                mode_raw, oid_raw, stage_raw = metadata.split(b" ", 2)
            except ValueError as exc:
                raise ForgeTraceError(
                    "The Git index contains an unsupported entry encoding.",
                    HTTPStatus.CONFLICT,
                    "git_switch_index_manifest_invalid",
                ) from exc
            path = self._safe_path_text(raw_path, label="Git index")
            mode = mode_raw.decode("ascii", errors="strict")
            oid = oid_raw.decode("ascii", errors="strict").lower()
            stage = stage_raw.decode("ascii", errors="strict")
            if stage != "0":
                raise ForgeTraceError(
                    "The Git index contains unmerged entries.",
                    HTTPStatus.CONFLICT,
                    "git_switch_unmerged_index",
                    {"path": path, "stage": stage},
                )
            if mode not in REGULAR_GIT_MODES:
                raise ForgeTraceError(
                    "The Git index contains a symlink, gitlink, or unsupported mode.",
                    HTTPStatus.CONFLICT,
                    "git_switch_index_entry_unsupported",
                    {"path": path, "mode": mode},
                )
            entries[path] = GitTreeEntry(path, mode, oid, "blob")

        flags = self._run(root, ["ls-files", "-v", "-z"], operation="index_flags")
        seen: set[str] = set()
        for raw in [item for item in flags.stdout.split(b"\0") if item]:
            if len(raw) < 3 or raw[1:2] != b" ":
                raise ForgeTraceError(
                    "The Git index flag readout is malformed.",
                    HTTPStatus.CONFLICT,
                    "git_switch_index_manifest_invalid",
                )
            tag = chr(raw[0])
            path = self._safe_path_text(raw[2:], label="Git index flags")
            seen.add(path)
            if tag != "H":
                raise ForgeTraceError(
                    "Skip-worktree, assume-unchanged, sparse, or other special index flags are unsupported.",
                    HTTPStatus.CONFLICT,
                    "git_switch_index_flags_unsupported",
                    {"path": path, "flag": tag},
                )
        if seen != set(entries):
            raise ForgeTraceError(
                "The Git index entry and flag manifests disagree.",
                HTTPStatus.CONFLICT,
                "git_switch_index_manifest_invalid",
            )
        return entries

    def _assert_clean_tracked_state(self, root: Path, source_oid: str, source_tree: dict[str, GitTreeEntry], index: dict[str, GitTreeEntry]) -> str:
        if source_tree != index:
            raise ForgeTraceError(
                "The Git index does not exactly match the current source branch tree.",
                HTTPStatus.CONFLICT,
                "git_switch_index_not_clean",
            )
        cached = self._run(
            root,
            ["diff-index", "--cached", "--quiet", source_oid, "--"],
            accepted_codes={0, 1},
            operation="clean_index_check",
        )
        if cached.returncode != 0:
            raise ForgeTraceError(
                "Staged changes must be committed or cleared before switching branches.",
                HTTPStatus.CONFLICT,
                "git_switch_index_not_clean",
            )
        tracked = self._run(
            root,
            ["diff-files", "--quiet", "--"],
            accepted_codes={0, 1},
            operation="clean_worktree_check",
        )
        if tracked.returncode != 0:
            raise ForgeTraceError(
                "Tracked worktree changes must be committed or cleared before switching branches.",
                HTTPStatus.CONFLICT,
                "git_switch_tracked_worktree_not_clean",
            )
        status = self._run(
            root,
            ["status", "--porcelain=v2", "-z", "--untracked-files=no", "--ignore-submodules=none"],
            operation="clean_tracked_status",
        )
        if status.stdout:
            raise ForgeTraceError(
                "The tracked worktree is not clean.",
                HTTPStatus.CONFLICT,
                "git_switch_tracked_worktree_not_clean",
            )
        return self._hash_bytes(status.stdout)

    def _hash_blob(self, root: Path, oid: str) -> tuple[int, str]:
        size_result = self._run(root, ["cat-file", "-s", oid], operation="blob_size")
        try:
            expected_size = int(size_result.stdout.decode("ascii", errors="strict").strip())
        except ValueError as exc:
            raise ForgeTraceError(
                "Git returned an invalid blob size.",
                HTTPStatus.CONFLICT,
                "git_switch_blob_invalid",
                {"oid": oid},
            ) from exc
        if expected_size > self.max_capture_bytes:
            raise ForgeTraceError(
                "An affected Git blob exceeds the maximum capture size.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "git_switch_capture_size_limit",
                {"oid": oid, "sizeBytes": expected_size, "limitBytes": self.max_capture_bytes},
            )
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
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "submodule.recurse=false",
            "-c",
            "fetch.recurseSubmodules=false",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=never",
            "cat-file",
            "blob",
            oid,
        ]
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.git_writes._safe_environment({"GIT_OPTIONAL_LOCKS": "0"}),
            shell=False,
        )
        digest = hashlib.sha256()
        actual_size = 0
        started = time.monotonic()
        assert process.stdout is not None
        while True:
            if time.monotonic() - started > self.timeout_seconds:
                process.kill()
                process.communicate()
                raise ForgeTraceError(
                    "Git blob hashing timed out.",
                    HTTPStatus.GATEWAY_TIMEOUT,
                    "git_switch_blob_timeout",
                    {"oid": oid},
                )
            chunk = process.stdout.read(1024 * 1024)
            if not chunk:
                break
            actual_size += len(chunk)
            if actual_size > self.max_capture_bytes:
                process.kill()
                process.communicate()
                raise ForgeTraceError(
                    "An affected Git blob exceeds the maximum capture size.",
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "git_switch_capture_size_limit",
                    {"oid": oid, "limitBytes": self.max_capture_bytes},
                )
            digest.update(chunk)
        stderr = process.stderr.read(8192) if process.stderr is not None else b""
        returncode = process.wait(timeout=3)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        if returncode != 0 or actual_size != expected_size:
            raise ForgeTraceError(
                "Git blob bytes could not be verified.",
                HTTPStatus.CONFLICT,
                "git_switch_blob_invalid",
                {
                    "oid": oid,
                    "expectedSize": expected_size,
                    "actualSize": actual_size,
                    "stderr": CONTROL_PATTERN.sub("�", stderr.decode("utf-8", errors="replace"))[:2048],
                },
            )
        return actual_size, digest.hexdigest()

    def _blob_metadata(self, root: Path, entries: Iterable[GitTreeEntry]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if entry.oid not in result:
                size, digest = self._hash_blob(root, entry.oid)
                result[entry.oid] = {"sizeBytes": size, "rawSha256": digest}
        return result

    def _scan_worktree(self, root: Path, tracked_paths: set[str]) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        directories: list[dict[str, Any]] = []
        scanned = 0

        def visit(directory: Path, rel_dir: str = "") -> None:
            nonlocal scanned
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
            except OSError as exc:
                raise ForgeTraceError(
                    "The worktree could not be scanned safely.",
                    HTTPStatus.CONFLICT,
                    "git_switch_worktree_scan_failed",
                    {"path": str(directory), "error": str(exc)[:1024]},
                ) from exc
            for entry in entries:
                rel = f"{rel_dir}/{entry.name}".strip("/").replace("\\", "/")
                if not rel_dir and entry.name in {".git", ".forgetrace"}:
                    continue
                scanned += 1
                if scanned > MAX_SCAN_ENTRIES:
                    raise ForgeTraceError(
                        "The worktree scan exceeds the safe entry limit.",
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        "git_switch_worktree_scan_limit",
                        {"limit": MAX_SCAN_ENTRIES},
                    )
                if self._protected_path(rel):
                    raise ForgeTraceError(
                        "Nested Git or ForgeTrace administrative path segments are unsupported.",
                        HTTPStatus.CONFLICT,
                        "git_switch_protected_path",
                        {"path": rel},
                    )
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ForgeTraceError(
                        "A worktree entry changed during scanning.",
                        HTTPStatus.CONFLICT,
                        "git_switch_state_changed",
                        {"path": rel, "error": str(exc)[:1024]},
                    ) from exc
                if self._is_reparse(info) or stat.S_ISLNK(info.st_mode):
                    raise ForgeTraceError(
                        "Symlinks, junctions, and reparse points are unsupported by the first switch slice.",
                        HTTPStatus.CONFLICT,
                        "git_switch_worktree_entry_unsupported",
                        {"path": rel, "kind": "reparse_or_symlink"},
                    )
                if stat.S_ISDIR(info.st_mode):
                    directories.append({"path": rel, "mode": stat.S_IMODE(info.st_mode)})
                    visit(Path(entry.path), rel)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ForgeTraceError(
                        "Devices, sockets, FIFOs, and special worktree entries are unsupported.",
                        HTTPStatus.CONFLICT,
                        "git_switch_worktree_entry_unsupported",
                        {"path": rel, "mode": int(info.st_mode)},
                    )
                if rel in tracked_paths:
                    continue
                files.append(
                    {
                        "path": rel,
                        "sizeBytes": int(info.st_size),
                        "mode": stat.S_IMODE(info.st_mode),
                        "statIdentity": self._stat_identity(info),
                    }
                )
                if len(files) > self.max_untracked_entries:
                    raise ForgeTraceError(
                        "The untracked and ignored file count exceeds the safe switch limit.",
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        "git_switch_untracked_entry_limit",
                        {"limit": self.max_untracked_entries},
                    )

        visit(root)
        ignored = self._ignored_paths(root, [item["path"] for item in files])
        for item in files:
            item["classification"] = "ignored" if item["path"] in ignored else "untracked"
        return {"files": files, "directories": directories, "scannedEntries": scanned}

    def _run_check_ignore(self, root: Path, payload: bytes):
        # ``git check-ignore --stdin`` treats input records as pathnames, but Git's
        # global ``--literal-pathspecs`` switch is rejected by this plumbing command.
        # Keep the same no-hook/no-helper/no-network environment while omitting only
        # that incompatible global option.
        command = [
            self.git_executable,
            "--no-pager",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "submodule.recurse=false",
            "-c",
            "fetch.recurseSubmodules=false",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=never",
            "check-ignore",
            "--no-index",
            "-z",
            "--stdin",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=str(root),
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.git_writes._safe_environment(),
                shell=False,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ForgeTraceError(
                "Ignored-path classification timed out.",
                HTTPStatus.GATEWAY_TIMEOUT,
                "git_switch_command_timeout",
            ) from exc
        if completed.returncode not in {0, 1}:
            raise ForgeTraceError(
                "Ignored-path classification failed.",
                HTTPStatus.CONFLICT,
                "git_switch_command_failed",
                {
                    "operation": "ignored_path_classification",
                    "returnCode": completed.returncode,
                    "stderr": CONTROL_PATTERN.sub("�", completed.stderr.decode("utf-8", errors="replace"))[:2048],
                },
            )
        if len(completed.stdout) > 8 * 1024 * 1024 or len(completed.stderr) > 8 * 1024 * 1024:
            raise ForgeTraceError(
                "Ignored-path classification output exceeded the safe limit.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "git_switch_output_limit",
            )
        return completed

    def _ignored_paths(self, root: Path, paths: list[str]) -> set[str]:
        ignored: set[str] = set()
        for offset in range(0, len(paths), 500):
            chunk = paths[offset : offset + 500]
            if not chunk:
                continue
            payload = b"\0".join(path.encode("utf-8") for path in chunk) + b"\0"
            result = self._run_check_ignore(root, payload)
            for raw in [part for part in result.stdout.split(b"\0") if part]:
                ignored.add(self._safe_path_text(raw, label="ignored path"))
        return ignored

    @staticmethod
    def _implicit_directories(paths: Iterable[str]) -> set[str]:
        result: set[str] = set()
        for path in paths:
            parent = Path(path).parent
            while str(parent) not in {"", "."}:
                result.add(parent.as_posix())
                parent = parent.parent
        return result

    def _collision_analysis(
        self,
        source_paths: set[str],
        target_paths: set[str],
        worktree_files: list[dict[str, Any]],
        worktree_directories: list[dict[str, Any]],
    ) -> dict[str, Any]:
        target_dirs = self._implicit_directories(target_paths)
        source_dirs = self._implicit_directories(source_paths)
        local_files = {str(item["path"]) for item in worktree_files}
        local_dirs = {str(item["path"]) for item in worktree_directories} - source_dirs
        collisions: list[dict[str, str]] = []

        for path in sorted(local_files):
            if path in target_paths:
                collisions.append({"path": path, "reason": "target_file_overwrites_local_file"})
            if path in target_dirs:
                collisions.append({"path": path, "reason": "target_directory_requires_local_file_path"})
            ancestor = Path(path).parent
            while str(ancestor) not in {"", "."}:
                ancestor_text = ancestor.as_posix()
                if ancestor_text in target_paths:
                    collisions.append({"path": path, "reason": "target_file_blocks_local_ancestor"})
                    break
                ancestor = ancestor.parent
        for path in sorted(local_dirs):
            if path in target_paths:
                collisions.append({"path": path, "reason": "target_file_overwrites_local_directory"})

        casefold_groups: dict[str, set[str]] = {}
        for namespace, values in (
            ("source", source_paths | source_dirs),
            ("target", target_paths | target_dirs),
            ("local", local_files | local_dirs),
        ):
            for path in values:
                casefold_groups.setdefault(path.casefold(), set()).add(f"{namespace}:{path}")
        casefold_collisions: list[dict[str, Any]] = []
        for folded, members in sorted(casefold_groups.items()):
            real_paths = {member.split(":", 1)[1] for member in members}
            # Reject every cross-spelling ambiguity, including source-versus-local
            # pairs that are representable on a case-sensitive host but collapse to
            # one path on Windows. Future rollback and verification must never have
            # to guess which spelling owns the bytes.
            if len(real_paths) > 1:
                casefold_collisions.append({"casefold": folded, "members": sorted(members)})
        if collisions:
            raise ForgeTraceError(
                "An untracked or ignored path collides with the target branch tree.",
                HTTPStatus.CONFLICT,
                "git_switch_target_collision",
                {"collisions": collisions[:100], "collisionCount": len(collisions)},
            )
        if casefold_collisions:
            raise ForgeTraceError(
                "The source, target, or preserved local paths contain a case-fold ambiguity.",
                HTTPStatus.CONFLICT,
                "git_switch_casefold_collision",
                {"collisions": casefold_collisions[:100], "collisionCount": len(casefold_collisions)},
            )
        return {
            "exactCollisionCount": 0,
            "caseFoldCollisionCount": 0,
            "analysisMode": "conservative_unicode_casefold",
            "targetDirectoryCount": len(target_dirs),
        }

    @staticmethod
    def _path_file_state(path: Path) -> dict[str, Any]:
        if not path.exists() and not path.is_symlink():
            return {"exists": False, "sizeBytes": 0, "sha256": "", "mode": 0}
        info = GitSwitchService._require_regular_lstat(
            path,
            code="git_switch_git_metadata_unsupported",
            label="Git metadata",
        )
        size, digest = GitSwitchService._hash_file(path)
        return {"exists": True, "sizeBytes": size, "sha256": digest, "mode": stat.S_IMODE(info.st_mode)}

    def _ref_verification_state(self, git_dir: Path, ref: str) -> dict[str, Any]:
        rel = ref.replace("\\", "/").strip("/")
        ref_path = git_dir / rel
        reflog_path = git_dir / "logs" / rel
        return {
            "ref": rel,
            "looseRef": self._path_file_state(ref_path),
            "reflog": self._path_file_state(reflog_path),
        }

    def _metadata_state(self, git_dir: Path, source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        return {
            "HEAD": self._path_file_state(git_dir / "HEAD"),
            "index": self._path_file_state(git_dir / "index"),
            "logsHEAD": self._path_file_state(git_dir / "logs" / "HEAD"),
            "packedRefs": self._path_file_state(git_dir / "packed-refs"),
            "sourceRef": self._ref_verification_state(git_dir, source["ref"]),
            "targetRef": self._ref_verification_state(git_dir, target["ref"]),
        }

    def _affected_manifest(
        self,
        root: Path,
        source_tree: dict[str, GitTreeEntry],
        target_tree: dict[str, GitTreeEntry],
    ) -> tuple[list[dict[str, Any]], int]:
        affected_paths = sorted(
            path
            for path in set(source_tree) | set(target_tree)
            if source_tree.get(path) != target_tree.get(path)
        )
        if len(affected_paths) > self.max_touched_paths:
            raise ForgeTraceError(
                "The branch transition affects too many tracked paths for one sealed plan.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "git_switch_touched_path_limit",
                {"limit": self.max_touched_paths, "actual": len(affected_paths)},
            )
        source_blobs = self._blob_metadata(root, (source_tree[path] for path in affected_paths if path in source_tree))
        target_blobs = self._blob_metadata(root, (target_tree[path] for path in affected_paths if path in target_tree))
        manifest: list[dict[str, Any]] = []
        source_capture_bytes = 0
        for path in affected_paths:
            source = source_tree.get(path)
            target = target_tree.get(path)
            source_payload = {"present": False, "oid": "", "mode": "", "sizeBytes": 0, "rawSha256": ""}
            target_payload = {"present": False, "oid": "", "mode": "", "sizeBytes": 0, "rawSha256": ""}
            worktree_state: dict[str, Any] = {"present": False}
            if source is not None:
                source_meta = source_blobs[source.oid]
                source_payload = {
                    "present": True,
                    "oid": source.oid,
                    "mode": source.mode,
                    **source_meta,
                }
                path_obj = root / path
                info = self._require_regular_lstat(
                    path_obj,
                    code="git_switch_tracked_worktree_entry_unsupported",
                    label="Affected tracked worktree path",
                )
                size, digest = self._hash_file(path_obj)
                if size != source_meta["sizeBytes"] or digest != source_meta["rawSha256"]:
                    raise ForgeTraceError(
                        "An affected tracked path does not match the source branch blob bytes.",
                        HTTPStatus.CONFLICT,
                        "git_switch_tracked_worktree_not_clean",
                        {"path": path},
                    )
                worktree_state = {
                    "present": True,
                    "sizeBytes": size,
                    "sha256": digest,
                    "mode": stat.S_IMODE(info.st_mode),
                    "statIdentity": self._stat_identity(info),
                }
                source_capture_bytes += size
            if target is not None:
                target_payload = {
                    "present": True,
                    "oid": target.oid,
                    "mode": target.mode,
                    **target_blobs[target.oid],
                }
            transition = "modify"
            if source is None:
                transition = "add"
            elif target is None:
                transition = "delete"
            elif source.oid == target.oid and source.mode != target.mode:
                transition = "mode_change"
            manifest.append(
                {
                    "path": path,
                    "transition": transition,
                    "source": source_payload,
                    "target": target_payload,
                    "sourceWorktree": worktree_state,
                }
            )
        return manifest, source_capture_bytes

    def _analysis(self, repository_id: str, repository: Any, root: Path, git_dir: Path, target_name: str) -> dict[str, Any]:
        self._assert_authority(repository_id, repository, git_dir)
        git_identity = self._git_identity(root)
        object_format = self.git_writes._object_format(root)
        config = self._local_config_state(root, git_dir)
        worktrees = self._run(root, ["worktree", "list", "--porcelain"], operation="worktree_layout")
        worktree_lines = [line for line in worktrees.stdout.decode("utf-8", errors="replace").splitlines() if line.startswith("worktree ")]
        if len(worktree_lines) != 1 or Path(worktree_lines[0][9:]).resolve() != root or (git_dir / "worktrees").exists():
            raise ForgeTraceError(
                "Linked or multiple worktrees are outside the first switch slice.",
                HTTPStatus.CONFLICT,
                "git_switch_multiple_worktrees_unsupported",
            )
        source, target, branches = self._head_and_target(root, target_name)
        assert target is not None
        source_tree = self._tree(root, source["treeOid"], label="source")
        target_tree = self._tree(root, target["treeOid"], label="target")
        index = self._index_entries(root)
        clean_status_digest = self._assert_clean_tracked_state(root, source["oid"], source_tree, index)
        affected, source_capture_bytes = self._affected_manifest(root, source_tree, target_tree)
        worktree = self._scan_worktree(root, set(index))
        collision = self._collision_analysis(
            set(source_tree),
            set(target_tree),
            worktree["files"],
            worktree["directories"],
        )
        untracked_bytes = sum(int(item["sizeBytes"]) for item in worktree["files"])
        metadata = self._metadata_state(git_dir, source, target)
        metadata_bytes = sum(
            int(metadata[name].get("sizeBytes") or 0)
            for name in ("HEAD", "index", "logsHEAD")
        )
        capture_bytes = source_capture_bytes + untracked_bytes + metadata_bytes
        if capture_bytes > self.max_capture_bytes:
            raise ForgeTraceError(
                "The exact switch capture exceeds the safe byte limit.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "git_switch_capture_size_limit",
                {"limitBytes": self.max_capture_bytes, "actualBytes": capture_bytes},
            )
        largest_capture = max(
            [int(item["sizeBytes"]) for item in worktree["files"]]
            + [int(item["sourceWorktree"].get("sizeBytes") or 0) for item in affected]
            + [int(metadata[name].get("sizeBytes") or 0) for name in ("HEAD", "index", "logsHEAD")]
            + [0]
        )
        disk = shutil.disk_usage(self.root)
        required_free = capture_bytes + largest_capture + self.minimum_free_space_reserve_bytes + MAX_PLAN_BYTES
        if int(disk.free) < required_free:
            raise ForgeTraceError(
                "Application-data storage does not have enough free space for the sealed capture and atomic temporary file.",
                HTTPStatus.INSUFFICIENT_STORAGE,
                "git_switch_free_space_insufficient",
                {
                    "freeBytes": int(disk.free),
                    "requiredBytes": required_free,
                    "reserveBytes": self.minimum_free_space_reserve_bytes,
                },
            )
        relevant_directories = sorted(
            self._implicit_directories(item["path"] for item in affected)
            | self._implicit_directories(item["path"] for item in worktree["files"])
        )
        analysis = {
            "repositoryId": repository_id,
            "repositoryPath": str(root),
            "gitDirectory": str(git_dir),
            "gitExecutable": git_identity,
            "objectFormat": object_format,
            "layout": {
                "rootLevelRegularGitDirectory": True,
                "singleWorktree": True,
                "worktreeCount": 1,
            },
            "source": source,
            "target": target,
            "branchReadModelDigest": self._canonical_digest(branches),
            "index": metadata["index"],
            "cleanTrackedStatusDigest": clean_status_digest,
            "gitMetadata": metadata,
            "localConfiguration": config,
            "affectedTrackedPaths": affected,
            "preservedLocalFiles": worktree["files"],
            "requiredDirectoryTopology": relevant_directories,
            "worktreeScan": {
                "scannedEntries": worktree["scannedEntries"],
                "directoryCount": len(worktree["directories"]),
                "untrackedCount": sum(1 for item in worktree["files"] if item["classification"] == "untracked"),
                "ignoredCount": sum(1 for item in worktree["files"] if item["classification"] == "ignored"),
            },
            "collisionAnalysis": collision,
            "captureEstimate": {
                "affectedTrackedPathCount": len(affected),
                "preservedFileCount": len(worktree["files"]),
                "sourceTrackedBytes": source_capture_bytes,
                "preservedFileBytes": untracked_bytes,
                "gitMetadataBytes": metadata_bytes,
                "totalCaptureBytes": capture_bytes,
                "largestAtomicTemporaryBytes": largest_capture,
                "minimumFreeSpaceReserveBytes": self.minimum_free_space_reserve_bytes,
                "requiredFreeBytes": required_free,
                "availableFreeBytes": int(disk.free),
            },
            "limits": {
                "maxTouchedPaths": self.max_touched_paths,
                "maxUntrackedEntries": self.max_untracked_entries,
                "maxCaptureBytes": self.max_capture_bytes,
                "maxPlanBytes": MAX_PLAN_BYTES,
                "minimumFreeSpaceReserveBytes": self.minimum_free_space_reserve_bytes,
            },
            "nativeGitLocks": [],
            "administrativeState": [],
            "requiredConfirmation": "SWITCH BRANCH",
        }
        analysis["stateDigest"] = self._state_digest(analysis)
        return analysis

    def read_model(self, repository_id: str) -> dict[str, Any]:
        repository, root, git_dir = self._context(repository_id)
        with repository.lock, self.git_writes._lock(repository_id), self._lock(repository_id):
            self._assert_authority(repository_id, repository, git_dir)
            source, _, branches = self._head_and_target(root)
            targets = [
                {
                    "name": item["name"],
                    "ref": item["ref"],
                    "oid": item["oid"],
                    "eligibleDirectCommitRef": not item.get("symref") and item.get("objectType") == "commit",
                }
                for item in branches
                if item["ref"] != source["ref"]
            ]
            return {
                "schemaVersion": GIT_SWITCH_PLAN_SCHEMA_VERSION,
                "repositoryId": repository_id,
                "repositoryPath": str(root),
                "source": source,
                "targets": targets,
                "operation": "switch_branch",
                "requiredConfirmation": "SWITCH BRANCH",
                "previewTtlSeconds": GIT_SWITCH_PREVIEW_TTL_SECONDS,
                "plannerOnly": True,
                "executionImplemented": False,
                "ownerOnly": True,
                "contributorAuthority": False,
                "network": False,
                "credentials": False,
                "hooks": False,
                "helpers": False,
                "shell": False,
            }

    def _capture_file(
        self,
        source: Path,
        destination: Path,
        *,
        label: str,
        expected_size: int,
        expected_sha256: str,
    ) -> dict[str, Any]:
        before = self._require_regular_lstat(
            source,
            code="git_switch_capture_source_unsupported",
            label=label,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as input_handle, temporary.open("wb") as output_handle:
                while True:
                    chunk = input_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_capture_bytes:
                        raise ForgeTraceError(
                            "A capture exceeded the safe switch byte limit.",
                            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            "git_switch_capture_size_limit",
                        )
                    digest.update(chunk)
                    output_handle.write(chunk)
                output_handle.flush()
                os.fsync(output_handle.fileno())
            after = self._require_regular_lstat(
                source,
                code="git_switch_capture_source_unsupported",
                label=label,
            )
            actual_digest = digest.hexdigest()
            if (
                self._stat_identity(before) != self._stat_identity(after)
                or size != int(expected_size)
                or actual_digest != str(expected_sha256)
            ):
                raise ForgeTraceError(
                    f"{label} changed while its sealed capture was being written.",
                    HTTPStatus.CONFLICT,
                    "git_switch_capture_source_changed",
                    {"path": str(source)},
                )
            os.replace(temporary, destination)
            if os.name != "nt":
                os.chmod(destination, stat.S_IMODE(before.st_mode))
            self._fsync_directory(destination.parent)
            backup_size, backup_digest = self._hash_file(destination)
            if backup_size != size or backup_digest != actual_digest:
                raise ForgeTraceError(
                    "A sealed switch backup failed immediate verification.",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "git_switch_capture_verification_failed",
                    {"path": str(destination)},
                )
            return {
                "sizeBytes": size,
                "sha256": actual_digest,
                "mode": stat.S_IMODE(before.st_mode),
                "sourceStatIdentity": self._stat_identity(before),
            }
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _backup_name(category: str, ordinal: int, original_path: str) -> str:
        key = hashlib.sha256(original_path.encode("utf-8")).hexdigest()[:24]
        return f"captures/{category}/{ordinal:06d}-{key}.bin"

    def _write_captures(self, staging: Path, analysis: dict[str, Any]) -> list[dict[str, Any]]:
        root = Path(str(analysis["repositoryPath"])).resolve()
        git_dir = Path(str(analysis["gitDirectory"])).resolve()
        records: list[dict[str, Any]] = []
        ordinal = 0
        for key, rel in (("HEAD", "HEAD"), ("index", "index"), ("logsHEAD", "logs/HEAD")):
            state = dict(analysis["gitMetadata"][key])
            if not state.get("exists"):
                continue
            ordinal += 1
            backup_rel = self._backup_name("git", ordinal, rel)
            captured = self._capture_file(
                git_dir / rel,
                staging / backup_rel,
                label=f"Git {rel}",
                expected_size=int(state["sizeBytes"]),
                expected_sha256=str(state["sha256"]),
            )
            records.append(
                {
                    "category": "git_metadata",
                    "path": rel,
                    "backupPath": backup_rel,
                    **captured,
                }
            )
        for item in analysis["affectedTrackedPaths"]:
            source = dict(item["source"])
            if not source.get("present"):
                continue
            ordinal += 1
            path = str(item["path"])
            backup_rel = self._backup_name("tracked", ordinal, path)
            captured = self._capture_file(
                root / path,
                staging / backup_rel,
                label="Affected tracked worktree file",
                expected_size=int(source["sizeBytes"]),
                expected_sha256=str(source["rawSha256"]),
            )
            records.append(
                {
                    "category": "tracked_source",
                    "path": path,
                    "backupPath": backup_rel,
                    "gitMode": source["mode"],
                    **captured,
                }
            )
        for item in analysis["preservedLocalFiles"]:
            ordinal += 1
            path = str(item["path"])
            expected_size = int(item["sizeBytes"])
            # The analysis hashes preserved files only while sealing, so calculate
            # the expected bytes immediately before the stable copy and bind them
            # into both the capture record and final analysis.
            current_size, current_digest = self._hash_file(root / path)
            if current_size != expected_size:
                raise ForgeTraceError(
                    "A preserved local file changed after preflight.",
                    HTTPStatus.CONFLICT,
                    "git_switch_capture_source_changed",
                    {"path": path},
                )
            item["sha256"] = current_digest
            backup_rel = self._backup_name("local", ordinal, path)
            captured = self._capture_file(
                root / path,
                staging / backup_rel,
                label="Preserved untracked or ignored file",
                expected_size=current_size,
                expected_sha256=current_digest,
            )
            records.append(
                {
                    "category": "preserved_local",
                    "classification": item["classification"],
                    "path": path,
                    "backupPath": backup_rel,
                    **captured,
                }
            )
        return records

    def _verify_capture_records(self, plan_root: Path, records: list[dict[str, Any]]) -> None:
        for record in records:
            rel = str(record.get("backupPath") or "").replace("\\", "/").strip("/")
            if not rel.startswith("captures/") or rel == ".." or "/../" in f"/{rel}/":
                raise ForgeTraceError(
                    "A sealed switch plan contains an invalid backup path.",
                    HTTPStatus.CONFLICT,
                    "git_switch_plan_integrity_failed",
                )
            path = (plan_root / rel).resolve()
            try:
                path.relative_to(plan_root.resolve())
            except ValueError as exc:
                raise ForgeTraceError(
                    "A sealed switch backup escapes its plan directory.",
                    HTTPStatus.CONFLICT,
                    "git_switch_plan_integrity_failed",
                ) from exc
            info = self._require_regular_lstat(
                path,
                code="git_switch_plan_integrity_failed",
                label="Sealed switch backup",
            )
            size, digest = self._hash_file(path)
            if size != int(record.get("sizeBytes") or -1) or digest != str(record.get("sha256") or ""):
                raise ForgeTraceError(
                    "A sealed switch backup failed integrity verification.",
                    HTTPStatus.CONFLICT,
                    "git_switch_plan_integrity_failed",
                    {"backupPath": rel},
                )
            if stat.S_IMODE(info.st_mode) != int(record.get("mode") or 0):
                # Windows may not preserve POSIX mode bits on application-data
                # backups. The source mode remains evidence; backup byte integrity
                # is authoritative. Only enforce a nonzero regular-file mode.
                if os.name != "nt":
                    raise ForgeTraceError(
                        "A sealed switch backup mode changed.",
                        HTTPStatus.CONFLICT,
                        "git_switch_plan_integrity_failed",
                        {"backupPath": rel},
                    )

    def _plan_path(self, plan_id: str) -> Path:
        if not PLAN_ID_RE.fullmatch(str(plan_id or "")):
            raise ForgeTraceError("Branch-switch plan identifier is invalid.", code="git_switch_plan_id_invalid")
        return self.plans_dir / plan_id

    def plan_capture(self, repository_id: str, target_branch: str) -> dict[str, Any]:
        repository, root, git_dir = self._context(repository_id)
        plan_id = "switch_plan_" + uuid.uuid4().hex
        staging = self.staging_dir / f"{plan_id}.{uuid.uuid4().hex}"
        final = self._plan_path(plan_id)
        with repository.lock, self.git_writes._lock(repository_id), self._lock(repository_id):
            analysis = self._analysis(repository_id, repository, root, git_dir, target_branch)
            staging.mkdir(parents=True, exist_ok=False)
            try:
                captures = self._write_captures(staging, analysis)
                # Preserved file hashes are added during stable capture; recompute the
                # canonical state digest before final revalidation.
                analysis["stateDigest"] = self._state_digest(analysis)
                revalidated = self._analysis(repository_id, repository, root, git_dir, target_branch)
                # Add exact preserved hashes to the revalidated state before comparing.
                captured_local = {
                    str(record["path"]): str(record["sha256"])
                    for record in captures
                    if record.get("category") == "preserved_local"
                }
                for item in revalidated["preservedLocalFiles"]:
                    path = str(item["path"])
                    size, digest = self._hash_file(root / path)
                    if size != int(item["sizeBytes"]) or digest != captured_local.get(path):
                        raise ForgeTraceError(
                            "A preserved local file changed before the plan could be sealed.",
                            HTTPStatus.CONFLICT,
                            "git_switch_capture_source_changed",
                            {"path": path},
                        )
                    item["sha256"] = digest
                revalidated["stateDigest"] = self._state_digest(revalidated)
                if revalidated["stateDigest"] != analysis["stateDigest"]:
                    raise ForgeTraceError(
                        "Git or worktree state changed while the branch-switch plan was being captured.",
                        HTTPStatus.CONFLICT,
                        "git_switch_state_changed",
                        {
                            "initialStateDigest": analysis["stateDigest"],
                            "currentStateDigest": revalidated["stateDigest"],
                        },
                    )
                now = int(time.time())
                capture_digest = self._canonical_digest(captures)
                payload = {
                    "schemaVersion": GIT_SWITCH_PLAN_SCHEMA_VERSION,
                    "planId": plan_id,
                    "status": "sealed_capture_plan",
                    "operation": "switch_branch",
                    "repositoryId": repository_id,
                    "repositoryPath": str(root),
                    "targetBranch": target_branch,
                    "createdAt": utc_now(),
                    "createdAtEpoch": now,
                    "expiresAtEpoch": now + GIT_SWITCH_PREVIEW_TTL_SECONDS,
                    "expiresInSeconds": GIT_SWITCH_PREVIEW_TTL_SECONDS,
                    "requiredConfirmation": "SWITCH BRANCH",
                    "plannerOnly": True,
                    "executionImplemented": False,
                    "authority": {
                        "ownerOnly": True,
                        "contributorAuthority": False,
                        "network": False,
                        "credentials": False,
                        "hooks": False,
                        "helpers": False,
                        "shell": False,
                        "repositoryMutation": False,
                    },
                    "analysis": analysis,
                    "stateDigest": analysis["stateDigest"],
                    "captures": captures,
                    "capturesDigest": capture_digest,
                }
                payload["planDigest"] = self._canonical_digest(payload)
                self._atomic_write_json(staging / "plan.json", payload)
                self._atomic_write_bytes(staging / "SEALED", (payload["planDigest"] + "\n").encode("ascii"))
                self._verify_capture_records(staging, captures)
                os.replace(staging, final)
                self._fsync_directory(final.parent)
                return self.load_plan(plan_id, verify_repository=False)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise

    def load_plan(self, plan_id: str, *, verify_repository: bool = False) -> dict[str, Any]:
        root = self._plan_path(plan_id)
        try:
            plan_path = root / "plan.json"
            if plan_path.stat().st_size > MAX_PLAN_BYTES:
                raise ForgeTraceError(
                    "The sealed branch-switch plan exceeds the safe evidence limit.",
                    HTTPStatus.CONFLICT,
                    "git_switch_plan_integrity_failed",
                )
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ForgeTraceError(
                "The sealed branch-switch plan was not found.",
                HTTPStatus.NOT_FOUND,
                "git_switch_plan_not_found",
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ForgeTraceError(
                "The sealed branch-switch plan could not be read.",
                HTTPStatus.CONFLICT,
                "git_switch_plan_integrity_failed",
            ) from exc
        digest = str(payload.get("planDigest") or "")
        unsigned = dict(payload)
        unsigned.pop("planDigest", None)
        if (
            int(payload.get("schemaVersion") or 0) != GIT_SWITCH_PLAN_SCHEMA_VERSION
            or str(payload.get("planId") or "") != plan_id
            or not digest
            or digest != self._canonical_digest(unsigned)
            or str(payload.get("capturesDigest") or "") != self._canonical_digest(payload.get("captures") or [])
        ):
            raise ForgeTraceError(
                "The sealed branch-switch plan failed canonical integrity verification.",
                HTTPStatus.CONFLICT,
                "git_switch_plan_integrity_failed",
                {"planId": plan_id},
            )
        try:
            sealed = (root / "SEALED").read_text(encoding="ascii").strip()
        except OSError as exc:
            raise ForgeTraceError(
                "The sealed branch-switch plan marker is missing or unreadable.",
                HTTPStatus.CONFLICT,
                "git_switch_plan_integrity_failed",
            ) from exc
        if sealed != digest:
            raise ForgeTraceError(
                "The sealed branch-switch plan marker does not match its digest.",
                HTTPStatus.CONFLICT,
                "git_switch_plan_integrity_failed",
            )
        captures = payload.get("captures")
        if not isinstance(captures, list):
            raise ForgeTraceError(
                "The sealed branch-switch capture manifest is invalid.",
                HTTPStatus.CONFLICT,
                "git_switch_plan_integrity_failed",
            )
        self._verify_capture_records(root, captures)
        if verify_repository:
            self.verify_plan(plan_id)
        return payload

    def verify_plan(self, plan_id: str) -> dict[str, Any]:
        payload = self.load_plan(plan_id, verify_repository=False)
        repository_id = str(payload.get("repositoryId") or "")
        repository, root, git_dir = self._context(repository_id)
        with repository.lock, self.git_writes._lock(repository_id), self._lock(repository_id):
            if str(root) != str(payload.get("repositoryPath") or ""):
                raise ForgeTraceError(
                    "The registered repository path changed after the switch plan was sealed.",
                    HTTPStatus.CONFLICT,
                    "git_switch_plan_stale",
                )
            current = self._analysis(
                repository_id,
                repository,
                root,
                git_dir,
                str(payload.get("targetBranch") or ""),
            )
            expected_local = {
                str(record["path"]): str(record["sha256"])
                for record in payload.get("captures") or []
                if record.get("category") == "preserved_local"
            }
            for item in current["preservedLocalFiles"]:
                path = str(item["path"])
                size, digest = self._hash_file(root / path)
                if size != int(item["sizeBytes"]) or digest != expected_local.get(path):
                    raise ForgeTraceError(
                        "A preserved local file changed after the switch plan was sealed.",
                        HTTPStatus.CONFLICT,
                        "git_switch_plan_stale",
                        {"path": path},
                    )
                item["sha256"] = digest
            current["stateDigest"] = self._state_digest(current)
            if current["stateDigest"] != str(payload.get("stateDigest") or ""):
                raise ForgeTraceError(
                    "Git or worktree state changed after the branch-switch plan was sealed.",
                    HTTPStatus.CONFLICT,
                    "git_switch_plan_stale",
                    {
                        "planStateDigest": payload.get("stateDigest", ""),
                        "currentStateDigest": current["stateDigest"],
                    },
                )
            if int(time.time()) > int(payload.get("expiresAtEpoch") or 0):
                raise ForgeTraceError(
                    "The sealed branch-switch plan expired. Create a new plan.",
                    HTTPStatus.CONFLICT,
                    "git_switch_plan_expired",
                )
            return {
                "valid": True,
                "planId": plan_id,
                "repositoryId": repository_id,
                "stateDigest": current["stateDigest"],
                "capturesDigest": payload["capturesDigest"],
                "expiresAtEpoch": payload["expiresAtEpoch"],
                "executionImplemented": False,
            }
