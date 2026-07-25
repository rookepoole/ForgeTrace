#!/usr/bin/env python3
"""ForgeTrace local repository server.

No third-party packages are required. The server stores real files on disk,
records contribution events, creates content-addressed commit snapshots, and
can restore any saved snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 250 * 1024 * 1024
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".html", ".htm", ".css", ".js", ".mjs", ".cjs",
    ".json", ".jsonc", ".xml", ".svg", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".py", ".pyw", ".rb", ".php", ".java", ".kt", ".kts", ".swift",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs", ".go", ".rs", ".sh",
    ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd", ".sql", ".graphql",
    ".gql", ".vue", ".svelte", ".jsx", ".tsx", ".ts", ".env", ".gitignore",
    ".dockerignore", ".editorconfig", ".lock", ".csv", ".tsv", ".log",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def human_action_title(action: str, path: str = "") -> str:
    name = Path(path).name or path or "repository"
    labels = {
        "repository_created": "Created repository",
        "file_uploaded": f"Uploaded {name}",
        "file_saved": f"Updated {name}",
        "file_created": f"Created {name}",
        "folder_created": f"Created folder {name}",
        "path_renamed": f"Renamed {name}",
        "path_deleted": f"Deleted {name}",
        "commit_created": "Created repository snapshot",
        "commit_restored": "Restored repository snapshot",
    }
    return labels.get(action, action.replace("_", " ").title())


class RepositoryError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = int(status)


class ForgeTraceRepository:
    def __init__(self, project_root: Path, workspace: Path | None = None):
        self.project_root = project_root.resolve()
        configured = os.environ.get("FORGETRACE_WORKSPACE")
        self.workspace = (workspace or (Path(configured) if configured else self.project_root / "workspace")).resolve()
        self.meta_dir = self.workspace / ".forgetrace"
        self.objects_dir = self.meta_dir / "objects"
        self.state_path = self.meta_dir / "state.json"
        self.lock = threading.RLock()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def initialized(self) -> bool:
        return self.state_path.is_file()

    def default_state(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "repository": {
                "name": "",
                "description": "",
                "createdAt": "",
                "defaultAuthor": "",
            },
            "contributions": [],
            "commits": [],
        }

    def load_state(self, require_initialized: bool = True) -> dict[str, Any]:
        if not self.state_path.exists():
            if require_initialized:
                raise RepositoryError("Repository has not been initialized.", HTTPStatus.CONFLICT)
            return self.default_state()
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RepositoryError(f"Repository metadata is unreadable: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        state.setdefault("contributions", [])
        state.setdefault("commits", [])
        state.setdefault("repository", {})
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

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
            state["repository"] = {
                "name": name,
                "description": (description or "").strip(),
                "createdAt": now,
                "defaultAuthor": author,
            }
            self.save_state(state)
            readme = self.workspace / "README.md"
            if not readme.exists():
                summary = description.strip() if description else "Created with ForgeTrace."
                readme.write_text(f"# {name}\n\n{summary}\n", encoding="utf-8")
            self.record_contribution(
                state,
                action="repository_created",
                author=author,
                path="README.md",
                description=f"Initialized {name} and created the repository README.",
                impact=70,
            )
            self.save_state(state)
            return self.summary(state)

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

    def tree(self) -> list[dict[str, Any]]:
        if not self.initialized():
            return []
        entries: list[dict[str, Any]] = []
        for root, dirs, files in os.walk(self.workspace):
            root_path = Path(root)
            dirs[:] = sorted([d for d in dirs if not (root_path == self.workspace and d == ".forgetrace")], key=str.lower)
            for dirname in dirs:
                entries.append(self.file_entry(root_path / dirname))
            for filename in sorted(files, key=str.lower):
                entries.append(self.file_entry(root_path / filename))
        return sorted(entries, key=lambda item: (item["path"].count("/"), item["type"] != "folder", item["path"].lower()))

    def read_file(self, raw_path: str) -> dict[str, Any]:
        rel, path = self.resolve_path(raw_path)
        if not path.exists() or not path.is_file():
            raise RepositoryError("File not found.", HTTPStatus.NOT_FOUND)
        entry = self.file_entry(path)
        if entry["text"]:
            if entry["size"] > 5 * 1024 * 1024:
                entry["editable"] = False
                entry["message"] = "Text files larger than 5 MB can be downloaded but are not opened in the editor."
            else:
                entry["editable"] = True
                entry["content"] = path.read_text(encoding="utf-8", errors="replace")
        else:
            entry["editable"] = False
        entry["downloadUrl"] = f"/api/raw?path={urllib.parse.quote(rel)}&download=1"
        entry["rawUrl"] = f"/api/raw?path={urllib.parse.quote(rel)}"
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
            "type": "commit" if action in {"commit_created", "commit_restored"} else ("folder" if "folder" in action else "file"),
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
        if len(content) > MAX_REQUEST_BYTES:
            raise RepositoryError("File exceeds the 250 MB upload limit.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        with self.lock:
            state = self.load_state()
            existed = path.exists()
            if path.exists() and path.is_dir():
                raise RepositoryError("A folder already exists at that path.", HTTPStatus.CONFLICT)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
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
            return self.read_file(rel)

    def create_folder(self, raw_path: str, author: str) -> dict[str, Any]:
        rel, path = self.resolve_path(raw_path)
        with self.lock:
            state = self.load_state()
            if path.exists():
                raise RepositoryError("A file or folder already exists at that path.", HTTPStatus.CONFLICT)
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
            return self.file_entry(path)

    def rename_path(self, raw_path: str, raw_new_path: str, author: str) -> dict[str, Any]:
        rel, path = self.resolve_path(raw_path)
        new_rel, new_path = self.resolve_path(raw_new_path)
        with self.lock:
            state = self.load_state()
            if not path.exists():
                raise RepositoryError("Source path not found.", HTTPStatus.NOT_FOUND)
            if new_path.exists():
                raise RepositoryError("Destination path already exists.", HTTPStatus.CONFLICT)
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
            return self.file_entry(new_path)

    def delete_path(self, raw_path: str, author: str) -> dict[str, Any]:
        rel, path = self.resolve_path(raw_path)
        with self.lock:
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
            return {"deleted": rel, "affected": len(affected)}

    def manifest(self, *, store_objects: bool = False) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for entry in self.tree():
            if entry["type"] != "file":
                continue
            path = self.workspace / entry["path"]
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            hexdigest = digest.hexdigest()
            result[entry["path"]] = {"hash": hexdigest, "size": entry["size"]}
            if store_objects:
                object_path = self.objects_dir / hexdigest[:2] / hexdigest[2:]
                if not object_path.exists():
                    object_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = object_path.with_suffix(".tmp")
                    shutil.copyfile(path, tmp)
                    os.replace(tmp, object_path)
        return result

    @staticmethod
    def diff_manifests(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, list[str]]:
        old_paths, new_paths = set(previous), set(current)
        added = sorted(new_paths - old_paths)
        deleted = sorted(old_paths - new_paths)
        modified = sorted(path for path in old_paths & new_paths if previous[path]["hash"] != current[path]["hash"])
        return {"added": added, "modified": modified, "deleted": deleted}

    def create_commit(self, message: str, author: str) -> dict[str, Any]:
        message = (message or "").strip()
        if not message:
            raise RepositoryError("A commit message is required.")
        with self.lock:
            state = self.load_state()
            current = self.manifest(store_objects=True)
            previous = state["commits"][-1]["manifest"] if state["commits"] else {}
            changes = self.diff_manifests(previous, current)
            if not any(changes.values()) and state["commits"]:
                raise RepositoryError("No file changes exist since the previous snapshot.", HTTPStatus.CONFLICT)
            timestamp = utc_now()
            parent = state["commits"][-1]["id"] if state["commits"] else None
            raw_id = json.dumps(current, sort_keys=True) + timestamp + message + (parent or "")
            commit_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:12]
            last_commit_time = state["commits"][-1]["timestamp"] if state["commits"] else ""
            pending = [
                c["id"] for c in state["contributions"]
                if c.get("action") not in {"commit_created", "commit_restored"}
                and c.get("timestamp", "") > last_commit_time
            ]
            commit = {
                "id": commit_id,
                "parent": parent,
                "message": message,
                "author": (author or state["repository"].get("defaultAuthor") or "Unknown Contributor").strip(),
                "timestamp": timestamp,
                "manifest": current,
                "changes": changes,
                "fileCount": len(current),
                "totalBytes": sum(item["size"] for item in current.values()),
            }
            state["commits"].append(commit)
            contribution = self.record_contribution(
                state,
                action="commit_created",
                author=commit["author"],
                paths=sorted(set(changes["added"] + changes["modified"] + changes["deleted"])),
                description=message,
                impact=min(100, 65 + len(changes["added"]) * 2 + len(changes["modified"]) * 2 + len(changes["deleted"])),
                parents=pending[-40:],
                commit_id=commit_id,
                metadata={"changes": changes},
            )
            commit["contributionId"] = contribution["id"]
            self.save_state(state)
            return self.public_commit(commit)

    def public_commit(self, commit: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in commit.items() if key != "manifest"}

    def restore_commit(self, commit_id: str, author: str) -> dict[str, Any]:
        with self.lock:
            state = self.load_state()
            commit = next((c for c in state["commits"] if c["id"] == commit_id), None)
            if not commit:
                raise RepositoryError("Commit not found.", HTTPStatus.NOT_FOUND)
            for child in list(self.workspace.iterdir()):
                if child.name == ".forgetrace":
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            for rel, data in commit["manifest"].items():
                destination = self.workspace / rel
                object_path = self.objects_dir / data["hash"][:2] / data["hash"][2:]
                if not object_path.exists():
                    raise RepositoryError(f"Snapshot object missing for {rel}.", HTTPStatus.INTERNAL_SERVER_ERROR)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(object_path, destination)
            contribution = self.record_contribution(
                state,
                action="commit_restored",
                author=author,
                paths=list(commit["manifest"].keys()),
                description=f"Restored snapshot {commit_id}: {commit['message']}",
                impact=88,
                parents=[commit.get("contributionId")] if commit.get("contributionId") else [],
                commit_id=commit_id,
                metadata={"restoredCommit": commit_id},
            )
            self.save_state(state)
            return {"restored": commit_id, "files": len(commit["manifest"]), "contribution": contribution}

    def summary(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        state = state or self.load_state(require_initialized=False)
        if not self.initialized():
            return {"initialized": False}
        entries = self.tree()
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
        current_manifest = self.manifest(store_objects=False) if files else {}
        last_manifest = state["commits"][-1]["manifest"] if state["commits"] else {}
        dirty = self.diff_manifests(last_manifest, current_manifest)
        return {
            "initialized": True,
            "repository": state["repository"],
            "stats": {
                "files": len(files),
                "folders": len(folders),
                "bytes": total_bytes,
                "commits": len(state["commits"]),
                "contributions": len(state["contributions"]),
                "contributors": len(contributors),
                "dirtyFiles": sum(len(v) for v in dirty.values()),
            },
            "dirty": dirty,
            "contributors": sorted(contributors.values(), key=lambda p: (-p["impact"], p["name"].lower())),
            "latestCommit": self.public_commit(state["commits"][-1]) if state["commits"] else None,
        }

    def api_state(self) -> dict[str, Any]:
        state = self.load_state()
        return {
            "summary": self.summary(state),
            "tree": self.tree(),
            "contributions": list(reversed(state["contributions"])),
            "commits": [self.public_commit(c) for c in reversed(state["commits"])],
        }

    def export_zip(self, include_history: bool = True) -> bytes:
        state = self.load_state()
        output = BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for entry in self.tree():
                if entry["type"] == "file":
                    archive.write(self.workspace / entry["path"], entry["path"])
            if include_history:
                public_state = {
                    "schemaVersion": state.get("schemaVersion", 1),
                    "repository": state["repository"],
                    "contributions": state["contributions"],
                    "commits": [self.public_commit(c) for c in state["commits"]],
                }
                archive.writestr("FORGETRACE_HISTORY.json", json.dumps(public_state, indent=2, ensure_ascii=False))
        return output.getvalue()


class ForgeTraceHandler(BaseHTTPRequestHandler):
    server_version = "ForgeTrace/1.0"
    repository: ForgeTraceRepository

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, message: str, status: int) -> None:
        self.send_json({"error": message}, status)

    def read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise RepositoryError("Invalid Content-Length header.")
        if length > MAX_REQUEST_BYTES:
            raise RepositoryError("Request exceeds the 250 MB limit.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        return self.rfile.read(length) if length else b""

    def read_json(self) -> dict[str, Any]:
        body = self.read_body()
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RepositoryError("Request body must be valid JSON.")
        if not isinstance(value, dict):
            raise RepositoryError("JSON request body must be an object.")
        return value

    def parsed(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def q(self, query: dict[str, list[str]], key: str, default: str = "") -> str:
        return query.get(key, [default])[0]

    def do_GET(self) -> None:
        try:
            path, query = self.parsed()
            if path == "/api/status":
                self.send_json(self.repository.summary())
            elif path == "/api/state":
                self.send_json(self.repository.api_state())
            elif path == "/api/file":
                self.send_json(self.repository.read_file(self.q(query, "path")))
            elif path == "/api/raw":
                file_path, rel, mime = self.repository.raw_file(self.q(query, "path"))
                data = file_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                if self.q(query, "download") == "1":
                    safe_name = Path(rel).name.replace('"', "")
                    self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/export":
                data = self.repository.export_zip(include_history=self.q(query, "history", "1") != "0")
                state = self.repository.load_state()
                name = state["repository"].get("name", "repository").replace(" ", "-")
                safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_") or "repository"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f'attachment; filename="{safe}-export.zip"')
                self.end_headers()
                self.wfile.write(data)
            elif path.startswith("/api/"):
                raise RepositoryError("API route not found.", HTTPStatus.NOT_FOUND)
            else:
                self.serve_static(path)
        except RepositoryError as exc:
            self.send_error_json(str(exc), exc.status)
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover - defensive boundary
            self.send_error_json(f"Unexpected server error: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            path, query = self.parsed()
            if path == "/api/repository":
                data = self.read_json()
                self.send_json(self.repository.initialize(data.get("name", ""), data.get("description", ""), data.get("author", "")), HTTPStatus.CREATED)
            elif path == "/api/upload":
                rel = self.q(query, "path")
                author = self.q(query, "author")
                message = self.q(query, "message")
                result = self.repository.write_file(rel, self.read_body(), author, message, uploaded=True)
                self.send_json(result, HTTPStatus.CREATED)
            elif path == "/api/folder":
                data = self.read_json()
                self.send_json(self.repository.create_folder(data.get("path", ""), data.get("author", "")), HTTPStatus.CREATED)
            elif path == "/api/rename":
                data = self.read_json()
                self.send_json(self.repository.rename_path(data.get("path", ""), data.get("newPath", ""), data.get("author", "")))
            elif path == "/api/commit":
                data = self.read_json()
                self.send_json(self.repository.create_commit(data.get("message", ""), data.get("author", "")), HTTPStatus.CREATED)
            elif path == "/api/checkout":
                data = self.read_json()
                self.send_json(self.repository.restore_commit(data.get("commitId", ""), data.get("author", "")))
            else:
                raise RepositoryError("API route not found.", HTTPStatus.NOT_FOUND)
        except RepositoryError as exc:
            self.send_error_json(str(exc), exc.status)
        except Exception as exc:  # pragma: no cover
            self.send_error_json(f"Unexpected server error: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        try:
            path, _query = self.parsed()
            if path != "/api/file":
                raise RepositoryError("API route not found.", HTTPStatus.NOT_FOUND)
            data = self.read_json()
            content = data.get("content", "")
            if not isinstance(content, str):
                raise RepositoryError("File content must be text.")
            result = self.repository.write_file(
                data.get("path", ""),
                content.encode("utf-8"),
                data.get("author", ""),
                data.get("message", ""),
                uploaded=False,
            )
            self.send_json(result)
        except RepositoryError as exc:
            self.send_error_json(str(exc), exc.status)
        except Exception as exc:  # pragma: no cover
            self.send_error_json(f"Unexpected server error: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:
        try:
            path, query = self.parsed()
            if path != "/api/path":
                raise RepositoryError("API route not found.", HTTPStatus.NOT_FOUND)
            self.send_json(self.repository.delete_path(self.q(query, "path"), self.q(query, "author")))
        except RepositoryError as exc:
            self.send_error_json(str(exc), exc.status)
        except Exception as exc:  # pragma: no cover
            self.send_error_json(f"Unexpected server error: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static(self, request_path: str) -> None:
        decoded = urllib.parse.unquote(request_path)
        rel = decoded.lstrip("/") or "index.html"
        target = (self.repository.project_root / rel).resolve()
        if target != self.repository.project_root and self.repository.project_root not in target.parents:
            raise RepositoryError("Static path traversal denied.", HTTPStatus.FORBIDDEN)
        if target.is_dir():
            target = target / "index.html"
        if not target.exists() or not target.is_file() or target.name == "server.py" or ".git" in target.parts:
            target = self.repository.project_root / "index.html"
        data = target.read_bytes()
        mime, _ = mimetypes.guess_type(target.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", (mime or "application/octet-stream") + ("; charset=utf-8" if (mime or "").startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if target.name == "index.html" else "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)


def make_handler(repository: ForgeTraceRepository):
    class BoundHandler(ForgeTraceHandler):
        pass
    BoundHandler.repository = repository
    return BoundHandler


def run(host: str, port: int, workspace: Path | None = None) -> None:
    root = Path(__file__).resolve().parent
    repository = ForgeTraceRepository(root, workspace=workspace)
    server = ThreadingHTTPServer((host, port), make_handler(repository))
    print(f"ForgeTrace running at http://{host}:{port}")
    print(f"Repository workspace: {repository.workspace}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping ForgeTrace.")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ForgeTrace local repository server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=None, help="Optional repository workspace path")
    args = parser.parse_args()
    run(args.host, args.port, args.workspace)


if __name__ == "__main__":
    main()
