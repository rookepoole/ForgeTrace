from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

from .errors import ForgeTraceError
from .registry import RepositoryRegistry

GIT_INTELLIGENCE_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 6.0
DEFAULT_OUTPUT_LIMIT = 2 * 1024 * 1024
DIFF_OUTPUT_LIMIT = 512 * 1024
MAX_COMMITS = 200
MAX_REFS = 500
OID_PATTERN = re.compile(r"[0-9a-fA-F]{40,64}\Z")
CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
FIELD_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class GitCommandResult:
    stdout: bytes
    stderr: bytes
    returncode: int
    duration_ms: int
    truncated: bool = False


class GitIntelligenceService:
    """Strictly read-only local Git inspection for owner routes.

    Commands are whitelisted by method, run with an absolute executable, a sanitized
    environment, disabled prompts/pagers/optional locks, bounded output, and timeout.
    The service never invokes network, credential, hook, checkout, index, or ref-writing
    commands.
    """

    def __init__(
        self,
        registry: RepositoryRegistry,
        *,
        git_executable: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        output_limit: int = DEFAULT_OUTPUT_LIMIT,
    ) -> None:
        self.registry = registry
        discovered = git_executable or shutil.which("git", path=os.environ.get("PATH") or os.defpath)
        self.git_executable = str(Path(discovered).resolve()) if discovered else ""
        self.timeout_seconds = max(0.1, min(float(timeout_seconds), 30.0))
        self.output_limit = max(4096, min(int(output_limit), 8 * 1024 * 1024))

    @staticmethod
    def _clean_text(data: bytes, *, limit: int = 4096) -> str:
        value = data.decode("utf-8", errors="replace")[:limit]
        return CONTROL_PATTERN.sub("�", value).strip()

    def _safe_environment(self) -> dict[str, str]:
        keep = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "TMP", "TEMP", "TMPDIR") if key in os.environ}
        keep.update({
            "PATH": os.path.dirname(self.git_executable) or os.defpath,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GCM_INTERACTIVE": "Never",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
        })
        return keep

    def _run(
        self,
        root: Path,
        args: list[str],
        *,
        timeout: float | None = None,
        output_limit: int | None = None,
        allow_truncate: bool = False,
        accepted_codes: set[int] | None = None,
    ) -> GitCommandResult:
        if not self.git_executable or not Path(self.git_executable).is_file():
            raise ForgeTraceError("Git executable was not found.", HTTPStatus.NOT_IMPLEMENTED, "git_unavailable")
        limit = max(4096, min(int(output_limit or self.output_limit), 8 * 1024 * 1024))
        command = [
            self.git_executable,
            "--no-pager",
            "-c", "core.hooksPath=" + os.devnull,
            "-c", "credential.helper=",
            "-c", "core.askPass=",
            "-c", "core.pager=cat",
            "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false",
            "-c", "submodule.recurse=false",
            *args,
        ]
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._safe_environment(),
            shell=False,
        )
        chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        sizes = {"stdout": 0, "stderr": 0}
        truncated = threading.Event()

        def reader(name: str, stream: Any) -> None:
            while True:
                block = stream.read(65536)
                if not block:
                    break
                remaining = limit - sizes[name]
                if remaining > 0:
                    chunks[name].append(block[:remaining])
                    sizes[name] += min(len(block), remaining)
                if len(block) > remaining:
                    truncated.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    break

        threads = [
            threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            process.wait(timeout=timeout or self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=1)
            for stream in (process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            raise ForgeTraceError(
                "Git inspection timed out.", HTTPStatus.GATEWAY_TIMEOUT, "git_command_timeout",
                {"timeoutSeconds": timeout or self.timeout_seconds},
            ) from exc
        for thread in threads:
            thread.join(timeout=2)
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        duration = int((time.monotonic() - started) * 1000)
        result = GitCommandResult(
            b"".join(chunks["stdout"]), b"".join(chunks["stderr"]), int(process.returncode), duration, truncated.is_set()
        )
        if result.truncated and not allow_truncate:
            raise ForgeTraceError(
                "Git command output exceeded the safe inspection limit.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "git_output_limit",
                {"limitBytes": limit},
            )
        accepted = accepted_codes or {0}
        if result.returncode not in accepted and not (result.truncated and allow_truncate):
            raise ForgeTraceError(
                "Git inspection command failed.", HTTPStatus.CONFLICT, "git_command_failed",
                {"returnCode": result.returncode, "stderr": self._clean_text(result.stderr)},
            )
        return result

    @staticmethod
    def _field(value: str, *, limit: int = 4096) -> str:
        return FIELD_CONTROL_PATTERN.sub("�", str(value or ""))[:limit]

    def _administrative_preflight(self, root: Path, marker: Path) -> tuple[bool, str]:
        if not marker.is_dir():
            return True, ""
        try:
            resolved_marker = marker.resolve(strict=True)
        except OSError as exc:
            return False, f"The .git administrative directory could not be resolved: {exc}"
        if root != resolved_marker and root not in resolved_marker.parents:
            return False, "The .git administrative directory resolves outside the registered repository root."
        for name in ("config", "HEAD", "index", "packed-refs", "commondir"):
            candidate = marker / name
            if candidate.is_symlink():
                return False, f"The .git/{name} administrative path is a symbolic link."
        for name in ("objects", "refs", "logs"):
            candidate = marker / name
            if candidate.exists():
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError as exc:
                    return False, f"The .git/{name} path could not be resolved: {exc}"
                if resolved_marker != resolved and resolved_marker not in resolved.parents:
                    return False, f"The .git/{name} path resolves outside the registered repository root."
        if (marker / "commondir").exists():
            return False, "Linked-worktree common administrative directories are reported but not traversed."
        if (marker / "objects" / "info" / "alternates").exists():
            return False, "Alternate Git object stores are reported but not traversed."
        config = marker / "config"
        if config.exists():
            try:
                if config.stat().st_size > 1024 * 1024:
                    return False, "The local Git configuration exceeds the safe inspection limit."
                text = config.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return False, f"The local Git configuration could not be read: {exc}"
            if re.search(r"(?im)^\s*\[\s*include(?:if\b[^]]*)?\s*\]", text) or re.search(r"(?im)^\s*include(?:if\.[^=]+)?\.path\s*=", text):
                return False, "Local Git configuration includes external configuration and is not inspected."
        return True, ""

    def _context(self, repository_id: str, *, require_git: bool = True) -> dict[str, Any]:
        record = self.registry.get_repository(repository_id)
        if record.get("status") != "online":
            raise ForgeTraceError("Repository is not online.", HTTPStatus.CONFLICT, "repository_offline")
        root = Path(str(record["path"])).resolve()
        marker = root / ".git"
        context: dict[str, Any] = {
            "repositoryId": repository_id,
            "root": root,
            "marker": marker,
            "detected": False,
            "supported": False,
            "kind": "none",
            "reason": "No .git entry exists at the registered repository root.",
        }
        if not marker.exists() and not marker.is_symlink():
            if require_git:
                raise ForgeTraceError(context["reason"], HTTPStatus.CONFLICT, "git_not_detected")
            return context
        context["detected"] = True
        if marker.is_symlink():
            context.update(kind="symlink", reason="The .git entry is a symbolic link and is not inspected.")
        elif marker.is_dir():
            safe, reason = self._administrative_preflight(root, marker)
            context.update(supported=safe, kind="worktree", reason=reason)
        elif marker.is_file():
            try:
                text = marker.read_text(encoding="utf-8", errors="replace")[:4096].strip()
            except OSError as exc:
                context.update(kind="gitfile", reason=f"The .git file could not be read: {exc}")
            else:
                match = re.fullmatch(r"gitdir:\s*(.+)", text, flags=re.IGNORECASE)
                if not match:
                    context.update(kind="gitfile", reason="The .git file has an unsupported format.")
                else:
                    target = Path(match.group(1).strip())
                    if not target.is_absolute():
                        target = (root / target).resolve()
                    else:
                        target = target.resolve()
                    if root != target and root not in target.parents:
                        context.update(
                            kind="external_worktree",
                            reason="This Git worktree stores its administrative directory outside the registered repository root and is reported but not inspected.",
                        )
                    else:
                        context.update(supported=True, kind="worktree_gitfile", reason="")
        else:
            context.update(kind="special", reason="The .git entry is not a regular file or directory.")
        if require_git and not context["supported"]:
            raise ForgeTraceError(context["reason"], HTTPStatus.CONFLICT, "git_layout_unsupported", {"kind": context["kind"]})
        return context

    @staticmethod
    def _sanitize_remote_url(raw: str) -> dict[str, Any]:
        value = CONTROL_PATTERN.sub("", str(raw or "").strip())[:4096]
        redacted = False
        if not value:
            return {"url": "", "redacted": False}
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError:
            parsed = None
        if parsed and parsed.scheme:
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            userinfo = bool(parsed.username or parsed.password)
            redacted = userinfo or bool(parsed.query or parsed.fragment)
            netloc = ("<redacted>@" if userinfo else "") + host + port
            value = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        elif re.match(r"^[^/@\s]+@[^:\s]+:.+", value):
            value = re.sub(r"^[^@]+@", "<redacted>@", value, count=1)
            redacted = True
        return {"url": value, "redacted": redacted}

    def probe(self, repository_id: str) -> dict[str, Any]:
        context = self._context(repository_id, require_git=False)
        result = {key: value for key, value in context.items() if key not in {"root", "marker"}}
        result["gitExecutableAvailable"] = bool(self.git_executable and Path(self.git_executable).is_file())
        result["schemaVersion"] = GIT_INTELLIGENCE_SCHEMA_VERSION
        if context["detected"] and not result["gitExecutableAvailable"]:
            result.update(supported=False, reason="Git executable was not found.", errorCode="git_unavailable")
        if not result.get("supported"):
            return result
        try:
            command = self._run(context["root"], ["rev-parse", "--is-inside-work-tree", "--show-toplevel", "--is-bare-repository"])
            lines = self._clean_text(command.stdout, limit=8192).splitlines()
            top = Path(lines[1]).resolve() if len(lines) > 1 and lines[1] else context["root"]
            safe_top = top == context["root"]
            is_bare = bool(len(lines) > 2 and lines[2] == "true")
            inside = bool(lines and lines[0] == "true")
            result.update(
                commandAvailable=True,
                insideWorkTree=inside,
                topLevelMatchesRegisteredRoot=safe_top,
                bare=is_bare,
                durationMs=command.duration_ms,
            )
            if is_bare or not inside:
                result.update(supported=False, reason="The registered path is not a supported Git working tree.")
            elif not safe_top:
                result.update(supported=False, reason="Git reported a top-level directory different from the registered repository root.")
        except ForgeTraceError as exc:
            result.update(commandAvailable=True, supported=False, reason=str(exc), errorCode=exc.code)
        return result

    @staticmethod
    def _decode_z(data: bytes) -> list[str]:
        return [part.decode("utf-8", errors="replace") for part in data.split(b"\0") if part]

    def _status(self, root: Path) -> dict[str, Any]:
        result = self._run(root, ["status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all", "--ignore-submodules=all"])
        parts = self._decode_z(result.stdout)
        branch: dict[str, Any] = {"head": "", "oid": "", "upstream": "", "ahead": 0, "behind": 0, "detached": False}
        changes: list[dict[str, Any]] = []
        index = 0
        while index < len(parts):
            entry = parts[index]
            if entry.startswith("# branch.oid "):
                branch["oid"] = self._field(entry[13:])
            elif entry.startswith("# branch.head "):
                branch["head"] = self._field(entry[14:])
                branch["detached"] = branch["head"] == "(detached)"
            elif entry.startswith("# branch.upstream "):
                branch["upstream"] = self._field(entry[18:])
            elif entry.startswith("# branch.ab "):
                match = re.search(r"\+(\d+)\s+-(\d+)", entry)
                if match:
                    branch["ahead"], branch["behind"] = int(match.group(1)), int(match.group(2))
            elif entry.startswith("1 "):
                fields = entry.split(" ", 8)
                if len(fields) == 9:
                    xy, sub, path = fields[1], fields[2], self._field(fields[8])
                    changes.append({"path": path, "index": xy[0], "worktree": xy[1], "submodule": sub, "kind": "ordinary"})
            elif entry.startswith("2 "):
                fields = entry.split(" ", 9)
                original = self._field(parts[index + 1]) if index + 1 < len(parts) else ""
                index += 1
                if len(fields) == 10:
                    changes.append({"path": self._field(fields[9]), "originalPath": original, "index": fields[1][0], "worktree": fields[1][1], "submodule": fields[2], "kind": "rename", "score": fields[8]})
            elif entry.startswith("u "):
                fields = entry.split(" ", 10)
                changes.append({"path": self._field(fields[-1]), "index": "U", "worktree": "U", "kind": "unmerged", "submodule": fields[2] if len(fields)>2 else ""})
            elif entry.startswith("? "):
                changes.append({"path": self._field(entry[2:]), "index": "?", "worktree": "?", "kind": "untracked", "submodule": ""})
            index += 1
        return {
            "branch": branch,
            "changes": changes,
            "dirty": bool(changes),
            "stagedCount": sum(1 for item in changes if item["index"] not in {".", "?"}),
            "unstagedCount": sum(1 for item in changes if item["worktree"] not in {"."}),
            "untrackedCount": sum(1 for item in changes if item["kind"] == "untracked"),
        }

    def _refs(self, root: Path, namespace: str, limit: int = MAX_REFS) -> list[dict[str, Any]]:
        fmt = "%(refname:short)%00%(objectname)%00%(HEAD)%00%(upstream:short)%00%(upstream:trackshort)%00%(committerdate:iso-strict)%00%(subject)"
        result = self._run(root, ["for-each-ref", f"--count={limit}", f"--format={fmt}", namespace])
        refs = []
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            fields = line.split("\0")
            if len(fields) >= 7:
                refs.append({"name": self._field(fields[0]), "oid": self._field(fields[1]), "current": fields[2] == "*", "upstream": self._field(fields[3]), "tracking": self._field(fields[4]), "committedAt": self._field(fields[5]), "subject": self._field(fields[6])})
        return refs

    def _commits(self, root: Path, limit: int = 50) -> list[dict[str, Any]]:
        count = max(1, min(int(limit), MAX_COMMITS))
        fmt = "%H%x00%h%x00%P%x00%an%x00%ae%x00%ad%x00%s"
        result = self._run(root, ["log", "--all", f"--max-count={count}", "--date=iso-strict", f"--format={fmt}"])
        commits = []
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            fields = line.split("\0")
            if len(fields) >= 7:
                commits.append({"oid": self._field(fields[0]), "shortOid": self._field(fields[1]), "parents": [self._field(item) for item in fields[2].split()] if fields[2] else [], "authorName": self._field(fields[3]), "authorEmail": self._field(fields[4]), "authoredAt": self._field(fields[5]), "subject": self._field(fields[6])})
        return commits

    def _remotes(self, root: Path) -> list[dict[str, Any]]:
        result = self._run(root, ["config", "--local", "--no-includes", "--null", "--get-regexp", r"^remote\..*\.url$"], accepted_codes={0, 1})
        if result.returncode == 1:
            return []
        parts = self._decode_z(result.stdout)
        remotes: list[dict[str, Any]] = []
        for entry in parts:
            if "\n" in entry:
                key, value = entry.split("\n", 1)
            elif " " in entry:
                key, value = entry.split(" ", 1)
            else:
                continue
            match = re.fullmatch(r"remote\.(.+)\.url", key)
            if not match:
                continue
            sanitized = self._sanitize_remote_url(value)
            remotes.append({"name": self._field(match.group(1)), **sanitized})
        return remotes

    def overview(self, repository_id: str, *, commit_limit: int = 50) -> dict[str, Any]:
        probe = self.probe(repository_id)
        payload: dict[str, Any] = {"schemaVersion": GIT_INTELLIGENCE_SCHEMA_VERSION, "repositoryId": repository_id, "probe": probe}
        if not probe.get("supported"):
            return payload
        root = self._context(repository_id)["root"]
        payload.update(
            status=self._status(root),
            branches=self._refs(root, "refs/heads/"),
            tags=self._refs(root, "refs/tags/"),
            remotes=self._remotes(root),
            commits=self._commits(root, commit_limit),
            limits={"commitLimit": max(1, min(int(commit_limit), MAX_COMMITS)), "refLimit": MAX_REFS, "diffBytes": DIFF_OUTPUT_LIMIT},
        )
        return payload

    def commit_detail(self, repository_id: str, oid: str) -> dict[str, Any]:
        value = str(oid or "").strip()
        if not OID_PATTERN.fullmatch(value):
            raise ForgeTraceError("Commit identifier must be a full hexadecimal object ID.", code="invalid_git_object_id")
        root = self._context(repository_id)["root"]
        fmt = "%H%x00%h%x00%P%x00%an%x00%ae%x00%ad%x00%cn%x00%ce%x00%cd%x00%B"
        meta = self._run(root, ["show", "-s", "--date=iso-strict", f"--format={fmt}", value])
        fields = meta.stdout.decode("utf-8", errors="replace").split("\0", 9)
        if len(fields) < 10:
            raise ForgeTraceError("Commit metadata was incomplete.", HTTPStatus.CONFLICT, "git_commit_unreadable")
        changed = self._run(root, ["diff-tree", "--root", "--no-commit-id", "--name-status", "--no-textconv", "--ignore-submodules=all", "-r", "-z", value])
        parts = self._decode_z(changed.stdout)
        files = []
        index = 0
        while index < len(parts):
            status = parts[index]; index += 1
            if index >= len(parts): break
            path = parts[index]; index += 1
            item = {"status": self._field(status), "path": self._field(path)}
            if status.startswith(("R", "C")) and index < len(parts):
                item["newPath"] = self._field(parts[index]); index += 1
            files.append(item)
        return {"schemaVersion": GIT_INTELLIGENCE_SCHEMA_VERSION, "repositoryId": repository_id, "commit": {"oid": self._field(fields[0]), "shortOid": self._field(fields[1]), "parents": [self._field(item) for item in fields[2].split()] if fields[2] else [], "authorName": self._field(fields[3]), "authorEmail": self._field(fields[4]), "authoredAt": self._field(fields[5]), "committerName": self._field(fields[6]), "committerEmail": self._field(fields[7]), "committedAt": self._field(fields[8]), "message": CONTROL_PATTERN.sub("�", fields[9].rstrip())[:65536]}, "files": files[:500], "truncated": len(files) > 500}

    def diff(self, repository_id: str, *, scope: str, path: str = "", commit: str = "") -> dict[str, Any]:
        selected = str(scope or "working").strip().lower()
        if selected not in {"working", "staged", "commit"}:
            raise ForgeTraceError("Git diff scope is invalid.", code="invalid_git_diff_scope")
        root = self._context(repository_id)["root"]
        path_args: list[str] = []
        normalized = ""
        if path:
            normalized, _ = self.registry.repository_service(repository_id).resolve_path(path)
            path_args = ["--", normalized]
        common = ["--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "--no-renames", "--no-color", "--unified=3"]
        if selected == "commit":
            value = str(commit or "").strip()
            if not OID_PATTERN.fullmatch(value):
                raise ForgeTraceError("Commit identifier must be a full hexadecimal object ID.", code="invalid_git_object_id")
            args = ["show", "--format=", *common, value, *path_args]
        else:
            args = ["diff", *common]
            if selected == "staged":
                args.append("--cached")
            args.extend(path_args)
        result = self._run(root, args, output_limit=DIFF_OUTPUT_LIMIT, allow_truncate=True)
        text = result.stdout.decode("utf-8", errors="replace")
        binary = "Binary files " in text or "GIT binary patch" in text
        if binary:
            text = ""
        return {"schemaVersion": GIT_INTELLIGENCE_SCHEMA_VERSION, "repositoryId": repository_id, "scope": selected, "path": normalized, "commit": commit if selected == "commit" else "", "binary": binary, "truncated": result.truncated, "bytes": len(result.stdout), "durationMs": result.duration_ms, "text": text}
