from __future__ import annotations

import ipaddress
import json
import logging
import mimetypes
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .constants import APP_SCHEMA_VERSION, APP_VERSION, MAX_REQUEST_BYTES
from .collaboration import CollaborationService
from .errors import ForgeTraceError
from .native_picker import NativeFolderPickerUnavailable, pick_local_folder
from .jobs import OperationManager
from .registry import RepositoryRegistry

LOGGER = logging.getLogger("forgetrace")


class ForgeTraceApplication:
    def __init__(self, project_root: Path, registry: RepositoryRegistry, collaboration: CollaborationService) -> None:
        self.project_root = project_root.resolve()
        self.registry = registry
        self.collaboration = collaboration
        self.jobs = OperationManager(history_path=registry.data_dir / "operation-jobs.json")
        self.gateway = None

    def repository(self, repository_id: str):
        if repository_id == "active":
            return self.registry.active_service()
        return self.registry.repository_service(repository_id)


class ForgeTraceHandler(BaseHTTPRequestHandler):
    server_version = f"ForgeTrace/{APP_VERSION}"
    app: ForgeTraceApplication
    _remote_rate_lock = threading.Lock()
    _remote_rate_windows: dict[str, list[float]] = {}
    _remote_rate_limit = 60
    _remote_rate_period_seconds = 60.0
    _source_rate_lock = threading.Lock()
    _source_rate_windows: dict[str, list[float]] = {}
    _source_rate_limit = 6

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info(
            "%s | %s",
            self.requestline,
            fmt % args,
            extra={
                "client": self.client_address[0] if self.client_address else "",
                "request": self.requestline,
            },
        )

    def send_security_headers(self, *, static_document: bool = False, sandbox_content: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        if sandbox_content:
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; frame-ancestors 'none'")
        elif static_document:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'",
            )

    def send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-ForgeTrace-Version", APP_VERSION)
        self.send_security_headers()
        if getattr(self, "legacy_route", False):
            self.send_header("Deprecation", "true")
            self.send_header("Link", '</api/v1/repositories>; rel="successor-version"')
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)

    def send_error_json(self, error: ForgeTraceError) -> None:
        payload = {
            "error": str(error),
            "code": error.code,
        }
        if error.details:
            payload["details"] = error.details
        self.send_json(payload, error.status)

    def read_body(self, max_bytes: int | None = None) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ForgeTraceError("Invalid Content-Length header.", code="invalid_content_length") from exc
        if length < 0:
            raise ForgeTraceError("Invalid Content-Length header.", code="invalid_content_length")
        effective_limit = min(int(max_bytes or MAX_REQUEST_BYTES), MAX_REQUEST_BYTES)
        if length > effective_limit:
            limit_mb = effective_limit / (1024 * 1024)
            raise ForgeTraceError(
                f"Request exceeds the {limit_mb:g} MB limit for this operation.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_too_large",
                {"limitBytes": effective_limit, "requestBytes": length},
            )
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            raise ForgeTraceError("Request body ended unexpectedly.", code="incomplete_request_body")
        return body

    def read_body_to_temp(self, max_bytes: int | None = None) -> Path:
        """Stream a request body to application-data storage instead of RAM."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ForgeTraceError("Invalid Content-Length header.", code="invalid_content_length") from exc
        if length < 0:
            raise ForgeTraceError("Invalid Content-Length header.", code="invalid_content_length")
        effective_limit = min(int(max_bytes or MAX_REQUEST_BYTES), MAX_REQUEST_BYTES)
        if length > effective_limit:
            raise ForgeTraceError(
                f"Request exceeds the {effective_limit / (1024 * 1024):g} MB limit for this operation.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_too_large",
                {"limitBytes": effective_limit, "requestBytes": length},
            )
        transfer_dir = self.app.registry.data_dir / "transfers"
        transfer_dir.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix="request-", dir=transfer_dir)
        os.close(fd)
        target = Path(raw_path)
        remaining = length
        try:
            with target.open("wb") as handle:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ForgeTraceError("Request body ended unexpectedly.", code="incomplete_request_body")
                    handle.write(chunk)
                    remaining -= len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            return target
        except Exception:
            target.unlink(missing_ok=True)
            raise

    def read_json(self) -> dict[str, Any]:
        body = self.read_body()
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ForgeTraceError("Request body must be valid JSON.", code="invalid_json") from exc
        if not isinstance(value, dict):
            raise ForgeTraceError("JSON request body must be an object.", code="invalid_json_shape")
        return value

    def parsed(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    @staticmethod
    def q(query: dict[str, list[str]], key: str, default: str = "") -> str:
        return query.get(key, [default])[0]

    @staticmethod
    def repository_route(path: str) -> tuple[str, str] | None:
        match = re.fullmatch(r"/api/v1/repositories/([^/]+)(?:/(.*))?", path)
        if not match:
            return None
        return urllib.parse.unquote(match.group(1)), (match.group(2) or "")

    def invite_token(self) -> str:
        return str(self.headers.get("X-ForgeTrace-Invite", "")).strip()

    def is_loopback_client(self) -> bool:
        host = self.client_address[0] if self.client_address else ""
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def request_surface(self) -> str:
        return str(getattr(self.server, "forgetrace_surface", "combined"))

    def require_local_owner(self) -> None:
        if not self.is_loopback_client():
            raise ForgeTraceError(
                "Repository-owner actions are available only from the local machine.",
                HTTPStatus.FORBIDDEN,
                "owner_action_local_only",
            )
        host_header = str(self.headers.get("Host", ""))
        parsed_host = urllib.parse.urlsplit(f"//{host_header}").hostname or ""
        if parsed_host.casefold() not in {"localhost", "127.0.0.1", "::1"}:
            raise ForgeTraceError(
                "Open the owner workspace through localhost or 127.0.0.1.",
                HTTPStatus.FORBIDDEN,
                "owner_host_not_local",
            )

    def enforce_remote_boundary(self, path: str) -> None:
        surface = self.request_surface()
        if surface == "gateway":
            if path in {"/", "/contribute.html"} or path.startswith("/api/v1/collaboration/"):
                if path.startswith("/api/v1/collaboration/"):
                    self.enforce_remote_rate_limit()
                return
            raise ForgeTraceError(
                "This listener exposes only the quarantined contribution portal.",
                HTTPStatus.FORBIDDEN,
                "remote_owner_api_blocked",
            )
        if surface == "owner":
            if self.is_loopback_client():
                return
            raise ForgeTraceError(
                "The owner workspace is available only from the local machine.",
                HTTPStatus.FORBIDDEN,
                "owner_action_local_only",
            )
        if self.is_loopback_client():
            return
        if path in {"/", "/contribute.html"} or path.startswith("/api/v1/collaboration/"):
            if path.startswith("/api/v1/collaboration/"):
                self.enforce_remote_rate_limit()
            return
        raise ForgeTraceError(
            "Remote clients can access only the quarantined contribution portal.",
            HTTPStatus.FORBIDDEN,
            "remote_owner_api_blocked",
        )

    @classmethod
    def _prune_rate_windows(cls, windows: dict[str, list[float]], cutoff: float, *, cap: int = 4096) -> None:
        for key in list(windows):
            active = [stamp for stamp in windows[key] if stamp > cutoff]
            if active:
                windows[key] = active
            else:
                windows.pop(key, None)
        if len(windows) > cap:
            oldest = sorted(windows, key=lambda key: windows[key][-1] if windows[key] else 0.0)
            for key in oldest[: len(windows) - cap]:
                windows.pop(key, None)

    def enforce_remote_rate_limit(self) -> None:
        client = self.client_address[0] if self.client_address else "unknown"
        now = time.monotonic()
        cutoff = now - self._remote_rate_period_seconds
        with self._remote_rate_lock:
            self._prune_rate_windows(self._remote_rate_windows, cutoff)
            window = [stamp for stamp in self._remote_rate_windows.get(client, []) if stamp > cutoff]
            if len(window) >= self._remote_rate_limit:
                raise ForgeTraceError(
                    "Too many collaboration requests. Try again shortly.",
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "collaboration_rate_limited",
                    {"limit": self._remote_rate_limit, "periodSeconds": int(self._remote_rate_period_seconds)},
                )
            window.append(now)
            self._remote_rate_windows[client] = window

    def enforce_source_rate_limit(self) -> None:
        if self.request_surface() != "gateway" and self.is_loopback_client():
            return
        client = self.client_address[0] if self.client_address else "unknown"
        now = time.monotonic()
        cutoff = now - self._remote_rate_period_seconds
        with self._source_rate_lock:
            self._prune_rate_windows(self._source_rate_windows, cutoff)
            window = [stamp for stamp in self._source_rate_windows.get(client, []) if stamp > cutoff]
            if len(window) >= self._source_rate_limit:
                raise ForgeTraceError(
                    "Too many source archive requests. Try again shortly.",
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "source_download_rate_limited",
                    {"limit": self._source_rate_limit, "periodSeconds": int(self._remote_rate_period_seconds)},
                )
            window.append(now)
            self._source_rate_windows[client] = window

    def enforce_owner_request_origin(self, path: str) -> None:
        if path.startswith("/api/v1/collaboration/"):
            return
        self.require_local_owner()
        fetch_site = str(self.headers.get("Sec-Fetch-Site", "")).lower()
        if fetch_site == "cross-site":
            raise ForgeTraceError("Cross-site owner requests are blocked.", HTTPStatus.FORBIDDEN, "cross_site_request_blocked")
        origin = str(self.headers.get("Origin", "")).strip()
        if origin:
            origin_host = urllib.parse.urlsplit(origin).netloc.casefold()
            request_host = str(self.headers.get("Host", "")).casefold()
            if origin_host != request_host:
                raise ForgeTraceError("Request origin does not match ForgeTrace.", HTTPStatus.FORBIDDEN, "origin_mismatch")

    @staticmethod
    def owner_collaboration_route(path: str) -> tuple[str, str, str] | None:
        invite_match = re.fullmatch(r"/api/v1/repositories/([^/]+)/collaboration/invites(?:/([^/]+))?", path)
        if invite_match:
            return urllib.parse.unquote(invite_match.group(1)), "invite", urllib.parse.unquote(invite_match.group(2) or "")
        pr_match = re.fullmatch(r"/api/v1/repositories/([^/]+)/pull-requests(?:/([^/]+))?(?:/(review|merge|close))?", path)
        if pr_match:
            return urllib.parse.unquote(pr_match.group(1)), urllib.parse.unquote(pr_match.group(3) or "pull-request"), urllib.parse.unquote(pr_match.group(2) or "")
        return None

    @staticmethod
    def contributor_collaboration_route(path: str) -> tuple[str, str] | None:
        if path == "/api/v1/collaboration/source":
            return "source", ""
        if path == "/api/v1/collaboration/pull-requests":
            return "collection", ""
        match = re.fullmatch(r"/api/v1/collaboration/pull-requests/([^/]+)(?:/(files|deletions|submit))?", path)
        if match:
            return urllib.parse.unquote(match.group(2) or "pull-request"), urllib.parse.unquote(match.group(1))
        return None

    def send_file_path(
        self, file_path: Path, *, content_type: str, filename: str, delete_after: bool = False
    ) -> None:
        try:
            size = file_path.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            encoded = urllib.parse.quote(filename.replace('"', ''))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-ForgeTrace-Version", APP_VERSION)
            self.send_security_headers()
            self.end_headers()
            if getattr(self, "_head_only", False):
                return
            with file_path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        finally:
            if delete_after:
                file_path.unlink(missing_ok=True)

    def send_collaboration_source(self, token: str) -> None:
        self.enforce_source_rate_limit()
        archive_path, filename = self.app.collaboration.source_archive_file(token)
        self.send_file_path(
            archive_path, content_type="application/zip", filename=filename, delete_after=True
        )

    def send_raw(self, repository_id: str, query: dict[str, list[str]]) -> None:
        service = self.app.repository(repository_id)
        file_path, rel, mime = service.raw_file(self.q(query, "path"))
        size = file_path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-ForgeTrace-Version", APP_VERSION)
        self.send_security_headers(sandbox_content=True)
        if getattr(self, "legacy_route", False):
            self.send_header("Deprecation", "true")
            self.send_header("Link", '</api/v1/repositories>; rel="successor-version"')
        active_suffixes = {".html", ".htm", ".svg", ".xml", ".js", ".mjs", ".cjs"}
        if self.q(query, "download") == "1" or Path(rel).suffix.lower() in active_suffixes:
            safe_name = Path(rel).name.replace('"', "")
            encoded = urllib.parse.quote(safe_name)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
        self.end_headers()
        if getattr(self, "_head_only", False):
            return
        with file_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def send_export(self, repository_id: str, query: dict[str, list[str]]) -> None:
        service = self.app.repository(repository_id)
        include_sensitive = self.q(query, "confirmSensitive", "0") == "1"
        preview = service.sensitive_file_preview(
            include_vcs_metadata=self.q(query, "vcs", "1") != "0"
        )
        if preview.get("sensitiveCount", 0) and not include_sensitive:
            raise ForgeTraceError(
                "This export contains files that may include secrets or private metadata. Preview and explicitly confirm the export first.",
                HTTPStatus.CONFLICT,
                "sensitive_export_confirmation_required",
                preview,
            )
        state = service.load_state()
        name = state["repository"].get("name", "repository").replace(" ", "-")
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_") or "repository"
        transfer_dir = self.app.registry.data_dir / "transfers"
        transfer_dir.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix="export-", suffix=".zip", dir=transfer_dir)
        os.close(fd)
        archive_path = Path(raw_path)
        try:
            service.export_zip_to_path(
                archive_path,
                include_history=self.q(query, "history", "1") != "0",
                include_sensitive=include_sensitive,
            )
            self.send_file_path(
                archive_path, content_type="application/zip",
                filename=f"{safe}-export.zip", delete_after=True,
            )
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise

    def do_HEAD(self) -> None:
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self) -> None:
        try:
            path, query = self.parsed()
            self.enforce_remote_boundary(path)
            if path.startswith("/api/") and not path.startswith("/api/v1/collaboration/"):
                self.require_local_owner()
            self.legacy_route = path.startswith("/api/") and not path.startswith("/api/v1/")
            if path in {"/api/v1/version", "/api/version"}:
                self.send_json(
                    {
                        "name": "ForgeTrace",
                        "version": APP_VERSION,
                        "applicationSchemaVersion": APP_SCHEMA_VERSION,
                    }
                )
                return
            if path == "/api/v1/repositories":
                favorite_raw = self.q(query, "favorite")
                favorite = None if favorite_raw == "" else favorite_raw.lower() in {"1", "true", "yes"}
                self.send_json(self.app.registry.list_repositories(
                    query=self.q(query, "query"),
                    tag=self.q(query, "tag"),
                    collection_id=self.q(query, "collectionId"),
                    status=self.q(query, "status"),
                    favorite=favorite,
                ))
                return
            if path == "/api/v1/library":
                self.send_json(self.app.registry.list_library())
                return
            if path == "/api/v1/registry/export":
                self.send_json(self.app.registry.export_registry())
                return
            if path == "/api/v1/registry/backups":
                self.send_json({"backups": self.app.registry.list_backups()})
                return
            if path == "/api/v1/doctor":
                roots = [Path(value) for value in query.get("scanRoot", []) if value]
                self.send_json(self.app.registry.doctor(repair=False, scan_roots=roots))
                return
            if path == "/api/v1/active-repository":
                repository_id = self.app.registry.active_repository_id()
                self.send_json({"repositoryId": repository_id})
                return
            if path == "/api/v1/sharing":
                if not self.app.gateway:
                    raise ForgeTraceError(
                        "Sharing controller is unavailable.",
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "sharing_unavailable",
                    )
                self.send_json(self.app.gateway.status())
                return
            if path == "/api/v1/collaboration/invite":
                self.send_json(self.app.collaboration.invite_context(self.invite_token()))
                return
            if path == "/api/v1/collaboration/storage":
                self.require_local_owner()
                self.send_json(self.app.collaboration.storage_metrics())
                return

            job_match = re.fullmatch(r"/api/v1/jobs/([^/]+)", path)
            if job_match:
                try:
                    self.send_json(self.app.jobs.get(urllib.parse.unquote(job_match.group(1))))
                except KeyError as exc:
                    raise ForgeTraceError("Operation job not found.", HTTPStatus.NOT_FOUND, "job_not_found") from exc
                return

            contributor_route = self.contributor_collaboration_route(path)
            if contributor_route:
                action, pull_request_id = contributor_route
                if action == "source":
                    self.send_collaboration_source(self.invite_token())
                    return
                if action == "collection":
                    self.send_json({
                        "pullRequests": self.app.collaboration.list_pull_requests_for_token(
                            self.invite_token()
                        )
                    })
                    return
                if action == "pull-request" and pull_request_id:
                    self.send_json(self.app.collaboration.get_pull_request_for_token(
                        self.invite_token(), pull_request_id
                    ))
                    return
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")

            owner_route = self.owner_collaboration_route(path)
            if owner_route:
                self.require_local_owner()
                repository_id, action, resource_id = owner_route
                if action == "invite" and not resource_id:
                    self.send_json({"invites": self.app.collaboration.list_invites(repository_id)})
                elif action == "pull-request" and resource_id:
                    self.send_json(self.app.collaboration.get_pull_request(repository_id, resource_id))
                elif action == "pull-request" and not resource_id:
                    self.send_json({"pullRequests": self.app.collaboration.list_pull_requests(
                        repository_id, self.q(query, "status")
                    )})
                else:
                    raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
                return

            routed = self.repository_route(path)
            if routed:
                repository_id, action = routed
                if action == "":
                    self.send_json(self.app.registry.get_repository(repository_id))
                elif action == "state":
                    self.send_json(self.app.repository(repository_id).api_state())
                elif action == "file":
                    self.send_json(self.app.repository(repository_id).read_file(self.q(query, "path")))
                elif action == "raw":
                    self.send_raw(repository_id, query)
                elif action == "export":
                    self.send_export(repository_id, query)
                elif action == "export-preview":
                    self.send_json(self.app.repository(repository_id).sensitive_file_preview(
                        include_vcs_metadata=self.q(query, "vcs", "1") != "0"
                    ))
                else:
                    raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
                return

            # Backward-compatible active-repository routes.
            if path == "/api/status":
                self.send_json(self.app.registry.active_service().summary())
            elif path == "/api/state":
                self.send_json(self.app.registry.active_service().api_state())
            elif path == "/api/file":
                self.send_json(self.app.registry.active_service().read_file(self.q(query, "path")))
            elif path == "/api/raw":
                self.send_raw("active", query)
            elif path == "/api/export":
                self.send_export("active", query)
            elif path.startswith("/api/"):
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
            else:
                gateway_root = self.request_surface() == "gateway" and path == "/"
                combined_remote_root = (
                    self.request_surface() == "combined"
                    and not self.is_loopback_client()
                    and path == "/"
                )
                self.serve_static("/contribute.html" if gateway_root or combined_remote_root else path)
        except ForgeTraceError as exc:
            self.send_error_json(exc)
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover - defensive boundary
            LOGGER.exception("unexpected_server_error")
            self.send_error_json(
                ForgeTraceError(
                    f"Unexpected server error: {exc}",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                )
            )

    def _post_global(self, path: str, query: dict[str, list[str]]) -> bool:
        if path == "/api/v1/repositories/import-local/preview":
            self.require_local_owner()
            data = self.read_json()
            self.send_json(self.app.registry.preview_managed_repository_import(
                source_path=str(data.get("path", "")),
                upload_limit_bytes=data.get("uploadLimitBytes", MAX_REQUEST_BYTES),
            ))
            return True
        if path == "/api/v1/repositories/import-local":
            self.require_local_owner()
            data = self.read_json()
            source_path = str(data.get("path", ""))
            name = str(data.get("name", ""))
            description = str(data.get("description", ""))
            author = str(data.get("author", ""))
            upload_limit = data.get("uploadLimitBytes", MAX_REQUEST_BYTES)
            conflict_policy = str(data.get("conflictPolicy", "abort"))
            job = self.app.jobs.start(
                "managed_folder_import",
                lambda context: self.app.registry.create_managed_repository_from_local_folder(
                    source_path=source_path,
                    name=name,
                    description=description,
                    author=author,
                    upload_limit_bytes=upload_limit,
                    conflict_policy=conflict_policy,
                    progress=context.update,
                    cancelled=context.cancelled,
                ),
            )
            self.send_json(job, HTTPStatus.ACCEPTED)
            return True
        if path == "/api/v1/repositories/managed":
            data = self.read_json()
            result = self.app.registry.create_managed_repository(
                name=str(data.get("name", "")),
                description=str(data.get("description", "")),
                author=str(data.get("author", "")),
                upload_limit_bytes=data.get("uploadLimitBytes", MAX_REQUEST_BYTES),
            )
            self.send_json(result, HTTPStatus.CREATED)
            return True
        if path == "/api/v1/system/pick-folder":
            self.require_local_owner()
            self.read_body(1024)
            try:
                selected = pick_local_folder()
            except NativeFolderPickerUnavailable as exc:
                self.send_json({"available": False, "cancelled": False, "reason": str(exc)})
                return True
            self.send_json({
                "available": True,
                "cancelled": selected is None,
                "path": selected or "",
                "name": Path(selected).name if selected else "",
            })
            return True
        if path == "/api/v1/repositories/fork":
            data = self.read_json()
            result = self.app.registry.fork_from_collaboration_link(
                share_url=str(data.get("shareUrl", "")),
                name=str(data.get("name", "")),
                description=str(data.get("description", "")),
                author=str(data.get("author", "")),
                upload_limit_bytes=data.get("uploadLimitBytes", MAX_REQUEST_BYTES),
            )
            self.send_json(result, HTTPStatus.CREATED)
            return True
        if path == "/api/v1/repositories":
            data = self.read_json()
            result = self.app.registry.register_repository(
                path=str(data.get("path", "")),
                name=str(data.get("name", "")),
                description=str(data.get("description", "")),
                author=str(data.get("author", "")),
                initialize=bool(data.get("initialize", True)),
                create_directory=bool(data.get("createDirectory", False)),
                metadata_mode=str(data.get("metadataMode", "embedded")),
                upload_limit_bytes=data.get("uploadLimitBytes", MAX_REQUEST_BYTES),
            )
            self.send_json(result, HTTPStatus.CREATED)
            return True
        if path == "/api/v1/active-repository":
            data = self.read_json()
            self.send_json(self.app.registry.set_active(str(data.get("repositoryId", ""))))
            return True
        if path == "/api/v1/collections":
            data = self.read_json()
            self.send_json(self.app.registry.create_collection(
                name=str(data.get("name", "")),
                description=str(data.get("description", "")),
                color=str(data.get("color", "")),
            ), HTTPStatus.CREATED)
            return True
        if path == "/api/v1/saved-filters":
            data = self.read_json()
            query_data = data.get("query", {})
            if not isinstance(query_data, dict):
                raise ForgeTraceError("Saved filter query must be an object.", code="invalid_filter_query")
            self.send_json(self.app.registry.save_filter(
                name=str(data.get("name", "")), query=query_data
            ), HTTPStatus.CREATED)
            return True
        if path == "/api/v1/registry/backup":
            data = self.read_json()
            self.send_json(self.app.registry.create_backup(str(data.get("label", "manual"))), HTTPStatus.CREATED)
            return True
        if path == "/api/v1/registry/import":
            data = self.read_json()
            export_data = data.get("registry")
            if not isinstance(export_data, dict):
                raise ForgeTraceError("Import requires a registry export object.", code="invalid_registry_import")
            self.send_json(self.app.registry.import_registry(
                export_data, update_paths=bool(data.get("updatePaths", False))
            ))
            return True
        if path == "/api/v1/doctor":
            data = self.read_json()
            roots = data.get("scanRoots", [])
            if not isinstance(roots, list):
                raise ForgeTraceError("scanRoots must be a list of paths.", code="invalid_scan_roots")
            self.send_json(self.app.registry.doctor(
                repair=bool(data.get("repair", False)),
                scan_roots=[Path(str(value)) for value in roots if str(value).strip()],
            ))
            return True
        if path == "/api/v1/sharing/start":
            data = self.read_json()
            if not self.app.gateway:
                raise ForgeTraceError(
                    "Sharing controller is unavailable.",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "sharing_unavailable",
                )
            self.send_json(self.app.gateway.start(port=data.get("port", 8766)))
            return True
        if path == "/api/v1/sharing/stop":
            self.read_body(1024)
            if not self.app.gateway:
                raise ForgeTraceError(
                    "Sharing controller is unavailable.",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "sharing_unavailable",
                )
            self.send_json(self.app.gateway.stop())
            return True
        return False

    def _post_contributor(self, path: str, query: dict[str, list[str]]) -> bool:
        contributor_route = self.contributor_collaboration_route(path)
        if contributor_route:
            action, pull_request_id = contributor_route
            token = self.invite_token()
            if action == "collection":
                data = self.read_json()
                self.send_json(self.app.collaboration.create_pull_request(
                    token,
                    title=str(data.get("title", "")),
                    description=str(data.get("description", "")),
                    author_name=str(data.get("authorName", "")),
                ), HTTPStatus.CREATED)
            elif action == "files" and pull_request_id:
                limit = self.app.collaboration.pull_request_upload_limit(token, pull_request_id)
                upload_path = self.read_body_to_temp(limit)
                try:
                    self.send_json(self.app.collaboration.upload_pull_request_file_from_path(
                        token, pull_request_id, self.q(query, "path"), upload_path
                    ), HTTPStatus.CREATED)
                finally:
                    upload_path.unlink(missing_ok=True)
            elif action == "deletions" and pull_request_id:
                data = self.read_json()
                self.send_json(self.app.collaboration.add_pull_request_deletion(
                    token, pull_request_id, str(data.get("path", ""))
                ))
            elif action == "submit" and pull_request_id:
                self.read_body(1024)
                self.send_json(self.app.collaboration.submit_pull_request(token, pull_request_id))
            else:
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
            return True
        return False

    def _post_owner(self, path: str, query: dict[str, list[str]]) -> bool:
        owner_route = self.owner_collaboration_route(path)
        if owner_route:
            self.require_local_owner()
            repository_id, action, resource_id = owner_route
            if action == "invite" and not resource_id:
                data = self.read_json()
                self.send_json(self.app.collaboration.create_invite(
                    repository_id,
                    label=str(data.get("label", "")),
                    expires_in_hours=data.get("expiresInHours", 72),
                    max_uses=data.get("maxUses", 1),
                    max_file_bytes=data.get("maxFileBytes", 100 * 1024 * 1024),
                    max_total_bytes=data.get("maxTotalBytes", 1024 * 1024 * 1024),
                    allow_deletes=bool(data.get("allowDeletes", True)),
                    allow_source_download=bool(data.get("allowSourceDownload", True)),
                    allow_sensitive_source=bool(data.get("allowSensitiveSource", False)),
                ), HTTPStatus.CREATED)
            elif action == "review" and resource_id:
                data = self.read_json()
                self.send_json(self.app.collaboration.review_pull_request(
                    repository_id, resource_id,
                    reviewer=str(data.get("reviewer", "")),
                    verdict=str(data.get("verdict", "comment")),
                    comment=str(data.get("comment", "")),
                ))
            elif action == "merge" and resource_id:
                data = self.read_json()
                try:
                    expected_revision = int(data.get("expectedRevision", 0))
                except (TypeError, ValueError) as exc:
                    raise ForgeTraceError(
                        "expectedRevision must be a whole number.",
                        code="invalid_expected_revision",
                    ) from exc
                self.send_json(self.app.collaboration.merge_pull_request(
                    repository_id, resource_id,
                    merged_by=str(data.get("mergedBy", "")),
                    confirmation=str(data.get("confirmation", "")),
                    expected_revision=expected_revision,
                    allow_risky_files=bool(data.get("allowRiskyFiles", False)),
                ))
            elif action == "close" and resource_id:
                self.read_body(1024)
                self.send_json(self.app.collaboration.close_pull_request(repository_id, resource_id))
            else:
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
            return True
        return False

    def _post_repository(self, path: str, query: dict[str, list[str]]) -> bool:
        routed = self.repository_route(path)
        if routed:
            repository_id, action = routed
            if action == "upload":
                service = self.app.repository(repository_id)
                upload_path = self.read_body_to_temp(service.upload_limit_bytes)
                try:
                    result = service.write_file_from_path(
                        self.q(query, "path"), upload_path,
                        self.q(query, "author"), self.q(query, "message"),
                        uploaded=True,
                    )
                finally:
                    upload_path.unlink(missing_ok=True)
                self.send_json(result, HTTPStatus.CREATED)
            elif action == "folder":
                data = self.read_json()
                self.send_json(
                    self.app.repository(repository_id).create_folder(
                        str(data.get("path", "")), str(data.get("author", ""))
                    ),
                    HTTPStatus.CREATED,
                )
            elif action == "folders":
                data = self.read_json()
                paths = data.get("paths", [])
                if not isinstance(paths, list):
                    raise ForgeTraceError("paths must be a list.", code="invalid_folder_manifest")
                self.send_json(
                    self.app.repository(repository_id).ensure_folders(
                        [str(item) for item in paths], str(data.get("author", ""))
                    ),
                    HTTPStatus.CREATED,
                )
            elif action == "import-preview":
                self.require_local_owner()
                data = self.read_json()
                self.send_json(self.app.repository(repository_id).preview_local_folder_import(
                    str(data.get("path", "")),
                    include_root=bool(data.get("includeRoot", True)),
                    conflict_policy=str(data.get("conflictPolicy", "abort")),
                ))
            elif action == "import-jobs":
                self.require_local_owner()
                data = self.read_json()
                source_path = str(data.get("path", ""))
                import_author = str(data.get("author", ""))
                include_root = bool(data.get("includeRoot", True))
                conflict_policy = str(data.get("conflictPolicy", "abort"))
                from .importing import run_import_job
                service = self.app.repository(repository_id)
                job = self.app.jobs.start(
                    "folder_import",
                    lambda context: run_import_job(
                        service, source_path, import_author, include_root, conflict_policy, context
                    ),
                )
                self.send_json(job, HTTPStatus.ACCEPTED)
            elif action == "import-local-folder":
                self.require_local_owner()
                data = self.read_json()
                self.send_json(
                    self.app.repository(repository_id).import_local_folder(
                        str(data.get("path", "")),
                        str(data.get("author", "")),
                        include_root=bool(data.get("includeRoot", True)),
                        conflict_policy=str(data.get("conflictPolicy", "abort")),
                    ),
                    HTTPStatus.CREATED,
                )
            elif action == "rename":
                data = self.read_json()
                self.send_json(
                    self.app.repository(repository_id).rename_path(
                        str(data.get("path", "")),
                        str(data.get("newPath", "")),
                        str(data.get("author", "")),
                    )
                )
            elif action == "commit":
                data = self.read_json()
                self.send_json(
                    self.app.repository(repository_id).create_commit(
                        str(data.get("message", "")), str(data.get("author", ""))
                    ),
                    HTTPStatus.CREATED,
                )
            elif action == "checkout":
                data = self.read_json()
                self.send_json(
                    self.app.repository(repository_id).restore_commit(
                        str(data.get("commitId", "")), str(data.get("author", ""))
                    )
                )
            elif action == "relink":
                data = self.read_json()
                self.send_json(self.app.registry.relink(repository_id, str(data.get("path", ""))))
            elif action == "initialize":
                data = self.read_json()
                self.send_json(
                    self.app.registry.initialize_registered(
                        repository_id,
                        name=str(data.get("name", "")),
                        description=str(data.get("description", "")),
                        author=str(data.get("author", "")),
                    )
                )
            elif action == "favorite":
                data = self.read_json()
                self.send_json(self.app.registry.set_favorite(repository_id, bool(data.get("favorite", True))))
            elif action == "settings":
                data = self.read_json()
                self.send_json(self.app.registry.update_settings(
                    repository_id,
                    name=str(data.get("name", "")),
                    description=str(data.get("description", "")),
                    default_author=str(data.get("defaultAuthor", "")),
                    upload_limit_bytes=data.get("uploadLimitBytes", MAX_REQUEST_BYTES),
                ))
            elif action == "organization":
                data = self.read_json()
                tags = data.get("tags", [])
                collection_ids = data.get("collectionIds", [])
                if not isinstance(tags, list) or not isinstance(collection_ids, list):
                    raise ForgeTraceError("Tags and collectionIds must be lists.", code="invalid_organization")
                self.send_json(self.app.registry.set_repository_organization(
                    repository_id, tags=tags, collection_ids=collection_ids
                ))
            else:
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
            return True
        return False

    def _post_legacy(self, path: str, query: dict[str, list[str]]) -> bool:
        # Backward-compatible active-repository routes.
        if path == "/api/repository":
            data = self.read_json()
            default_workspace = self.app.project_root / "workspace"
            result = self.app.registry.register_repository(
                path=str(default_workspace),
                name=str(data.get("name", "")),
                description=str(data.get("description", "")),
                author=str(data.get("author", "")),
                initialize=True,
                create_directory=True,
            )
            self.send_json(result, HTTPStatus.CREATED)
        elif path == "/api/upload":
            service = self.app.registry.active_service()
            upload_path = self.read_body_to_temp(service.upload_limit_bytes)
            try:
                result = service.write_file_from_path(
                    self.q(query, "path"), upload_path, self.q(query, "author"),
                    self.q(query, "message"), uploaded=True
                )
            finally:
                upload_path.unlink(missing_ok=True)
            self.send_json(result, HTTPStatus.CREATED)
        elif path == "/api/folder":
            data = self.read_json()
            self.send_json(
                self.app.registry.active_service().create_folder(
                    str(data.get("path", "")), str(data.get("author", ""))
                ),
                HTTPStatus.CREATED,
            )
        elif path == "/api/rename":
            data = self.read_json()
            self.send_json(
                self.app.registry.active_service().rename_path(
                    str(data.get("path", "")), str(data.get("newPath", "")), str(data.get("author", ""))
                )
            )
        elif path == "/api/commit":
            data = self.read_json()
            self.send_json(
                self.app.registry.active_service().create_commit(
                    str(data.get("message", "")), str(data.get("author", ""))
                ),
                HTTPStatus.CREATED,
            )
        elif path == "/api/checkout":
            data = self.read_json()
            self.send_json(
                self.app.registry.active_service().restore_commit(
                    str(data.get("commitId", "")), str(data.get("author", ""))
                )
            )
        else:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        return True

    def do_POST(self) -> None:
        try:
            path, query = self.parsed()
            self.enforce_remote_boundary(path)
            if not path.startswith("/api/v1/collaboration/"):
                self.enforce_owner_request_origin(path)
            self.legacy_route = path.startswith("/api/") and not path.startswith("/api/v1/")
            if self._post_global(path, query):
                return
            if self._post_contributor(path, query):
                return
            if self._post_owner(path, query):
                return
            if self._post_repository(path, query):
                return
            if path.startswith("/api/") and self._post_legacy(path, query):
                return
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        except ForgeTraceError as exc:
            self.send_error_json(exc)
        except Exception as exc:  # pragma: no cover
            LOGGER.exception("unexpected_server_error")
            self.send_error_json(
                ForgeTraceError(
                    f"Unexpected server error: {exc}",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                )
            )

    def do_PUT(self) -> None:
        try:
            path, _query = self.parsed()
            self.enforce_remote_boundary(path)
            self.enforce_owner_request_origin(path)
            self.legacy_route = path.startswith("/api/") and not path.startswith("/api/v1/")
            collection_match = re.fullmatch(r"/api/v1/collections/([^/]+)", path)
            if collection_match:
                data = self.read_json()
                self.send_json(self.app.registry.update_collection(
                    urllib.parse.unquote(collection_match.group(1)),
                    name=str(data.get("name", "")),
                    description=str(data.get("description", "")),
                    color=str(data.get("color", "")),
                ))
                return
            routed = self.repository_route(path)
            if routed and routed[1] == "file":
                service = self.app.repository(routed[0])
            elif path == "/api/file":
                service = self.app.registry.active_service()
            else:
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
            data = self.read_json()
            content = data.get("content", "")
            if not isinstance(content, str):
                raise ForgeTraceError("File content must be text.", code="file_content_not_text")
            result = service.write_file(
                str(data.get("path", "")),
                content.encode("utf-8"),
                str(data.get("author", "")),
                str(data.get("message", "")),
                uploaded=False,
            )
            self.send_json(result)
        except ForgeTraceError as exc:
            self.send_error_json(exc)
        except Exception as exc:  # pragma: no cover
            LOGGER.exception("unexpected_server_error")
            self.send_error_json(
                ForgeTraceError(
                    f"Unexpected server error: {exc}",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                )
            )

    def do_DELETE(self) -> None:
        try:
            path, query = self.parsed()
            self.enforce_remote_boundary(path)
            self.enforce_owner_request_origin(path)
            self.legacy_route = path.startswith("/api/") and not path.startswith("/api/v1/")
            collection_match = re.fullmatch(r"/api/v1/collections/([^/]+)", path)
            if collection_match:
                self.send_json(self.app.registry.delete_collection(urllib.parse.unquote(collection_match.group(1))))
                return
            filter_match = re.fullmatch(r"/api/v1/saved-filters/([^/]+)", path)
            if filter_match:
                self.send_json(self.app.registry.delete_filter(urllib.parse.unquote(filter_match.group(1))))
                return
            job_match = re.fullmatch(r"/api/v1/jobs/([^/]+)", path)
            if job_match:
                try:
                    self.send_json(self.app.jobs.cancel(urllib.parse.unquote(job_match.group(1))))
                except KeyError as exc:
                    raise ForgeTraceError("Operation job not found.", HTTPStatus.NOT_FOUND, "job_not_found") from exc
                return
            owner_route = self.owner_collaboration_route(path)
            if owner_route:
                self.require_local_owner()
                repository_id, action, resource_id = owner_route
                if action == "invite" and resource_id:
                    self.send_json(self.app.collaboration.revoke_invite(repository_id, resource_id))
                else:
                    raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
                return
            routed = self.repository_route(path)
            if routed and routed[1] == "":
                self.send_json(self.app.registry.unregister(routed[0]))
            elif routed and routed[1] == "discard":
                self.send_json(self.app.registry.discard_managed_repository(routed[0]))
            elif routed and routed[1] == "path":
                self.send_json(
                    self.app.repository(routed[0]).delete_path(
                        self.q(query, "path"), self.q(query, "author")
                    )
                )
            elif path == "/api/path":
                self.send_json(
                    self.app.registry.active_service().delete_path(
                        self.q(query, "path"), self.q(query, "author")
                    )
                )
            else:
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        except ForgeTraceError as exc:
            self.send_error_json(exc)
        except Exception as exc:  # pragma: no cover
            LOGGER.exception("unexpected_server_error")
            self.send_error_json(
                ForgeTraceError(
                    f"Unexpected server error: {exc}",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                )
            )

    def serve_static(self, request_path: str) -> None:
        decoded = urllib.parse.unquote(request_path)
        rel = decoded.lstrip("/") or "index.html"
        target = (self.app.project_root / rel).resolve()
        if target != self.app.project_root and self.app.project_root not in target.parents:
            raise ForgeTraceError("Static path traversal denied.", HTTPStatus.FORBIDDEN, "static_path_traversal")
        if target.is_dir():
            target = target / "index.html"
        protected_parts = {".git", ".forgetrace", "forgetrace", "tests", "__pycache__"}
        if (
            not target.exists()
            or not target.is_file()
            or any(part in protected_parts for part in target.parts)
            or target.suffix.lower() in {".py", ".sqlite3", ".db"}
        ):
            target = self.app.project_root / "index.html"
        data = target.read_bytes()
        mime, _ = mimetypes.guess_type(target.name)
        content_type = mime or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if target.name == "index.html" else "public, max-age=3600")
        self.send_header("X-ForgeTrace-Version", APP_VERSION)
        self.send_security_headers(static_document=target.suffix.lower() in {".html", ".htm"})
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)


def make_handler(app: ForgeTraceApplication):
    class BoundHandler(ForgeTraceHandler):
        pass

    BoundHandler.app = app
    return BoundHandler


class ForgeTraceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(30.0)
        return request, address


def create_server(
    app: ForgeTraceApplication,
    host: str,
    port: int,
    *,
    surface: str = "owner",
) -> ThreadingHTTPServer:
    if surface not in {"owner", "gateway", "combined"}:
        raise ValueError(f"Unknown ForgeTrace server surface: {surface}")
    server = ForgeTraceHTTPServer((host, port), make_handler(app))
    server.forgetrace_surface = surface
    return server


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
