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
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .constants import APP_SCHEMA_VERSION, APP_VERSION, MAX_REQUEST_BYTES
from .collaboration import CollaborationService
from .errors import ForgeTraceError
from .health import HEALTH_REPORT_SCHEMA_VERSION, HealthDashboardService
from .git_intelligence import GIT_INTELLIGENCE_SCHEMA_VERSION, GitIntelligenceService
from .git_switch import GitSwitchService
from .git_writes import GIT_WRITE_SCHEMA_VERSION, GitWriteService
from .native_picker import NativeFolderPickerUnavailable, pick_local_folder
from .project_coordination import PROJECT_COORDINATION_SCHEMA_VERSION, ProjectCoordinationService
from .project_boards import PROJECT_BOARDS_SCHEMA_VERSION, ProjectBoardService
from .releases import RELEASES_SCHEMA_VERSION, ReleaseService
from .jobs import OperationManager
from .registry import RepositoryRegistry
from .security_events import (
    SECURITY_ANCHOR_SCHEMA_VERSION,
    SECURITY_EVENT_SCHEMA_VERSION,
    SECURITY_RETENTION_POLICY_SCHEMA_VERSION,
    SECURITY_ROTATION_JOURNAL_SCHEMA_VERSION,
    SECURITY_SEGMENT_SCHEMA_VERSION,
    SecurityEventError,
    SecurityEventLedger,
)

LOGGER = logging.getLogger("forgetrace")


class ForgeTraceApplication:
    def __init__(
        self,
        project_root: Path,
        registry: RepositoryRegistry,
        collaboration: CollaborationService,
        security_events: SecurityEventLedger,
        project_coordination: ProjectCoordinationService,
        project_boards: ProjectBoardService,
        releases: ReleaseService,
    ) -> None:
        self.project_root = project_root.resolve()
        self.registry = registry
        self.collaboration = collaboration
        self.security_events = security_events
        self.project = project_coordination
        self.boards = project_boards
        self.releases = releases
        self.jobs = OperationManager(history_path=registry.data_dir / "operation-jobs.json")
        self.git = GitIntelligenceService(registry)
        self.git_writes = GitWriteService(
            registry=registry, git_intelligence=self.git, security_events=security_events
        )
        self.git_switches = GitSwitchService(
            registry=registry, git_intelligence=self.git, git_writes=self.git_writes
        )
        self.gateway = None
        self.owner_instance_lock_path = registry.data_dir / "owner.instance.lock"
        self.owner_instance_lock_held = False
        self.health = HealthDashboardService(
            registry=registry,
            collaboration=collaboration,
            security_events=security_events,
            runtime_status=self.health_runtime_status,
            git_intelligence=self.git,
            git_writes=self.git_writes,
            project_coordination=self.project,
            project_boards=self.boards,
            releases=self.releases,
        )
        self._reported_recovery_actions: set[tuple[str, str, str]] = set()

    def audit(self, *, required: bool = False, **event: Any) -> dict[str, Any] | None:
        try:
            if required:
                self.security_events.assert_writable()
            return self.security_events.append(**event)
        except SecurityEventError as exc:
            if required:
                raise ForgeTraceError(
                    "The security event ledger is unavailable or failed integrity verification. The protected action was blocked.",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "security_event_ledger_unavailable",
                    {"reason": str(exc)},
                ) from exc
            LOGGER.error("security_event_append_failed: %s", exc)
            return None

    def health_runtime_status(self) -> dict[str, Any]:
        return {
            "ownerInstanceLockPath": str(self.owner_instance_lock_path),
            "ownerInstanceLockHeld": bool(self.owner_instance_lock_held),
            "gateway": self.gateway.status() if self.gateway else {},
        }

    def repository(self, repository_id: str):
        service = self.registry.active_service() if repository_id == "active" else self.registry.repository_service(repository_id)
        resolved_id = service.repository_id or repository_id
        for recovery in service._recovery_actions:
            action = str(recovery.get("action") or "unknown")
            artifact = Path(str(recovery.get("artifact") or "")).name
            key = (str(resolved_id), action, artifact)
            if key in self._reported_recovery_actions:
                continue
            self._reported_recovery_actions.add(key)
            self.audit(
                category="recovery",
                action="repository_recovery_action",
                outcome="success",
                severity="warning",
                surface="system",
                repository_id=str(resolved_id),
                subject_id=action,
                details={"recoveryAction": action, "artifactName": artifact},
            )
        return service


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

    def request_id(self) -> str:
        value = getattr(self, "_forgetrace_request_id", "")
        if not value:
            value = "req_" + uuid.uuid4().hex
            self._forgetrace_request_id = value
        return value

    def audit_security_event(
        self,
        *,
        category: str,
        action: str,
        outcome: str,
        severity: str = "info",
        repository_id: str = "",
        actor: str = "",
        subject_id: str = "",
        details: dict[str, Any] | None = None,
        required: bool = False,
    ) -> dict[str, Any] | None:
        request_details = {
            "method": str(getattr(self, "command", "")),
            "path": urllib.parse.urlparse(str(getattr(self, "path", ""))).path,
            "clientAddress": self.client_address[0] if self.client_address else "",
        }
        if details:
            request_details.update(details)
        return self.app.audit(
            required=required,
            category=category,
            action=action,
            outcome=outcome,
            severity=severity,
            surface=self.request_surface(),
            repository_id=repository_id,
            request_id=self.request_id(),
            actor=actor,
            subject_id=subject_id,
            details=request_details,
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
        self.send_header("X-ForgeTrace-Request-Id", self.request_id())
        self.send_security_headers()
        if getattr(self, "legacy_route", False):
            self.send_header("Deprecation", "true")
            self.send_header("Link", '</api/v1/repositories>; rel="successor-version"')
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)

    def send_json_download(self, payload: Any, *, filename: str) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-ForgeTrace-Version", APP_VERSION)
        self.send_header("X-ForgeTrace-Request-Id", self.request_id())
        encoded = urllib.parse.quote(filename.replace('"', ""))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
        self.send_security_headers()
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)

    def send_error_json(self, error: ForgeTraceError) -> None:
        if error.code in {
            "snapshot_integrity_failed",
            "repository_revision_conflict",
            "pull_request_conflict",
            "security_event_ledger_unavailable",
            "registry_restore_integrity_failed",
            "registry_restore_post_verify_failed",
            "registry_restore_and_rollback_failed",
            "registry_restore_rollback_failed",
            "health_report_integrity_failed",
        }:
            severity = "critical" if error.code in {
                "snapshot_integrity_failed",
                "security_event_ledger_unavailable",
                "health_report_integrity_failed",
            } else "warning"
            self.audit_security_event(
                category="integrity" if "integrity" in error.code or "revision" in error.code else "collaboration",
                action=error.code,
                outcome="denied" if error.status < 500 else "failure",
                severity=severity,
                details={"status": int(error.status), "errorCode": error.code},
            )
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
            self.audit_security_event(
                category="access",
                action="owner_action_local_only",
                outcome="denied",
                severity="warning",
            )
            raise ForgeTraceError(
                "Repository-owner actions are available only from the local machine.",
                HTTPStatus.FORBIDDEN,
                "owner_action_local_only",
            )
        host_header = str(self.headers.get("Host", ""))
        parsed_host = urllib.parse.urlsplit(f"//{host_header}").hostname or ""
        if parsed_host.casefold() not in {"localhost", "127.0.0.1", "::1"}:
            self.audit_security_event(
                category="access",
                action="owner_host_not_local",
                outcome="denied",
                severity="warning",
                details={"hostHeader": host_header},
            )
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
            self.audit_security_event(
                category="access",
                action="gateway_owner_route_blocked",
                outcome="denied",
                severity="warning",
            )
            raise ForgeTraceError(
                "This listener exposes only the quarantined contribution portal.",
                HTTPStatus.FORBIDDEN,
                "remote_owner_api_blocked",
            )
        if surface == "owner":
            if self.is_loopback_client():
                return
            self.audit_security_event(
                category="access",
                action="owner_workspace_remote_blocked",
                outcome="denied",
                severity="warning",
            )
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
        self.audit_security_event(
            category="access",
            action="combined_owner_route_blocked",
            outcome="denied",
            severity="warning",
        )
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
                self.audit_security_event(
                    category="rate_limit",
                    action="collaboration_rate_limited",
                    outcome="denied",
                    severity="warning",
                    details={"limit": self._remote_rate_limit, "periodSeconds": int(self._remote_rate_period_seconds)},
                )
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
                self.audit_security_event(
                    category="rate_limit",
                    action="source_download_rate_limited",
                    outcome="denied",
                    severity="warning",
                    details={"limit": self._source_rate_limit, "periodSeconds": int(self._remote_rate_period_seconds)},
                )
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
            self.audit_security_event(
                category="access",
                action="cross_site_request_blocked",
                outcome="denied",
                severity="warning",
            )
            raise ForgeTraceError("Cross-site owner requests are blocked.", HTTPStatus.FORBIDDEN, "cross_site_request_blocked")
        origin = str(self.headers.get("Origin", "")).strip()
        if origin:
            origin_host = urllib.parse.urlsplit(origin).netloc.casefold()
            request_host = str(self.headers.get("Host", "")).casefold()
            if origin_host != request_host:
                self.audit_security_event(
                    category="access",
                    action="origin_mismatch",
                    outcome="denied",
                    severity="warning",
                    details={"originHost": origin_host, "requestHost": request_host},
                )
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

    @staticmethod
    def owner_review_conversation_route(path: str) -> tuple[str, str, str, str] | None:
        match = re.fullmatch(
            r"/api/v1/repositories/([^/]+)/pull-requests/([^/]+)/review-threads"
            r"(?:/([^/]+))?(?:/(comments|resolve|reopen))?",
            path,
        )
        if not match:
            return None
        return (
            urllib.parse.unquote(match.group(1)),
            urllib.parse.unquote(match.group(2)),
            urllib.parse.unquote(match.group(3) or ""),
            urllib.parse.unquote(match.group(4) or ("thread" if match.group(3) else "collection")),
        )

    @staticmethod
    def owner_conflict_resolution_route(path: str) -> tuple[str, str, str, str] | None:
        match = re.fullmatch(
            r"/api/v1/repositories/([^/]+)/pull-requests/([^/]+)/conflict-resolutions"
            r"(?:/([^/]+))?(?:/(decision|confirm))?",
            path,
        )
        if not match:
            return None
        return (
            urllib.parse.unquote(match.group(1)),
            urllib.parse.unquote(match.group(2)),
            urllib.parse.unquote(match.group(3) or ""),
            urllib.parse.unquote(match.group(4) or ("draft" if match.group(3) else "collection")),
        )

    @staticmethod
    def contributor_review_conversation_route(path: str) -> tuple[str, str, str] | None:
        match = re.fullmatch(
            r"/api/v1/collaboration/pull-requests/([^/]+)/review-threads"
            r"(?:/([^/]+))?(?:/(comments))?",
            path,
        )
        if not match:
            return None
        return (
            urllib.parse.unquote(match.group(1)),
            urllib.parse.unquote(match.group(2) or ""),
            urllib.parse.unquote(match.group(3) or ("thread" if match.group(2) else "collection")),
        )

    @staticmethod
    def owner_project_route(path: str) -> tuple[str, str, str, str] | None:
        root = re.fullmatch(r"/api/v1/repositories/([^/]+)/project", path)
        if root:
            return urllib.parse.unquote(root.group(1)), "overview", "", ""
        match = re.fullmatch(
            r"/api/v1/repositories/([^/]+)/project/(labels|milestones|issues|discussions|comments)"
            r"(?:/([^/]+))?(?:/(comments|moderate))?",
            path,
        )
        if not match:
            return None
        return (
            urllib.parse.unquote(match.group(1)),
            urllib.parse.unquote(match.group(2)),
            urllib.parse.unquote(match.group(3) or ""),
            urllib.parse.unquote(match.group(4) or ""),
        )

    @staticmethod
    def contributor_project_route(path: str) -> tuple[str, str, str] | None:
        if path == "/api/v1/collaboration/project":
            return "overview", "", ""
        match = re.fullmatch(
            r"/api/v1/collaboration/project/(issues|discussions)(?:/([^/]+))?(?:/(comments))?",
            path,
        )
        if not match:
            return None
        return (
            urllib.parse.unquote(match.group(1)),
            urllib.parse.unquote(match.group(2) or ""),
            urllib.parse.unquote(match.group(3) or ""),
        )

    @staticmethod
    def owner_release_route(path: str) -> tuple[str, str, str] | None:
        root = re.fullmatch(r"/api/v1/repositories/([^/]+)/releases", path)
        if root:
            return urllib.parse.unquote(root.group(1)), "", "collection"
        match = re.fullmatch(r"/api/v1/repositories/([^/]+)/releases/([^/]+)(?:/(assets|publish|export))?(?:/([^/]+))?(?:/(download))?", path)
        if not match:
            return None
        repository_id = urllib.parse.unquote(match.group(1)); release_id = urllib.parse.unquote(match.group(2))
        resource = urllib.parse.unquote(match.group(3) or "release")
        item = urllib.parse.unquote(match.group(4) or "")
        if match.group(5): resource = "asset_download"
        return repository_id, release_id, resource + ((":" + item) if item else "")

    @staticmethod
    def contributor_release_route(path: str) -> tuple[str, str, str] | None:
        if path == "/api/v1/collaboration/releases":
            return "collection", "", ""
        match = re.fullmatch(r"/api/v1/collaboration/releases/([^/]+)/assets/([^/]+)/download", path)
        if not match:
            return None
        return "asset_download", urllib.parse.unquote(match.group(1)), urllib.parse.unquote(match.group(2))

    def _get_releases(self, path: str, query: dict[str, list[str]]) -> bool:
        contributor = self.contributor_release_route(path)
        if contributor:
            resource, release_id, asset_id = contributor
            token = self.invite_token()
            if resource == "collection":
                self.send_json(self.app.releases.list_for_token(token))
            else:
                context = self.app.collaboration.invite_context(token)
                repository_id = context["repository"]["id"]
                file_path, filename, content_type = self.app.releases.asset_path(repository_id, release_id, asset_id, token=token)
                self.send_file_path(file_path, content_type=content_type, filename=filename)
            return True
        owner = self.owner_release_route(path)
        if not owner:
            return False
        self.require_local_owner()
        repository_id, release_id, resource = owner
        if resource == "collection":
            self.send_json(self.app.releases.list(repository_id))
        elif resource == "release":
            self.send_json(self.app.releases.get(repository_id, release_id))
        elif resource == "export":
            archive, filename = self.app.releases.export_release(repository_id, release_id)
            self.send_file_path(archive, content_type="application/zip", filename=filename, delete_after=True)
        elif resource.startswith("asset_download:"):
            asset_id = resource.split(":", 1)[1]
            file_path, filename, content_type = self.app.releases.asset_path(repository_id, release_id, asset_id)
            self.send_file_path(file_path, content_type=content_type, filename=filename)
        else:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        return True

    def _post_releases(self, path: str) -> bool:
        owner = self.owner_release_route(path)
        if not owner:
            return False
        self.require_local_owner()
        repository_id, release_id, resource = owner
        data = self.read_json()
        if resource == "collection":
            result = self.app.releases.create(repository_id, name=data.get("name"), version=data.get("version"), notes=data.get("notes", ""), tag_ref=data.get("tagRef", ""), commit_ref=data.get("commitRef", ""), contributor_access=data.get("contributorAccess", False), actor=str(data.get("actor") or "Repository Owner"))
            self.send_json(result, HTTPStatus.CREATED)
        elif resource == "assets":
            result = self.app.releases.add_asset_base64(repository_id, release_id, filename=data.get("filename"), content_base64=data.get("contentBase64"), content_type=data.get("contentType", "application/octet-stream"))
            self.send_json(result, HTTPStatus.CREATED)
        elif resource == "publish":
            self.send_json(self.app.releases.publish(repository_id, release_id, expected_version=data.get("expectedVersion")))
        else:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        return True

    def _put_releases(self, path: str) -> bool:
        owner = self.owner_release_route(path)
        if not owner:
            return False
        self.require_local_owner()
        repository_id, release_id, resource = owner
        if resource != "release":
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        data = self.read_json()
        self.send_json(self.app.releases.update(repository_id, release_id, expected_version=data.get("expectedVersion"), name=data.get("name"), notes=data.get("notes"), tag_ref=data.get("tagRef"), commit_ref=data.get("commitRef"), contributor_access=data.get("contributorAccess")))
        return True

    @staticmethod
    def owner_board_route(path: str) -> tuple[str, str, str, str] | None:
        root = re.fullmatch(r"/api/v1/repositories/([^/]+)/boards", path)
        if root:
            return urllib.parse.unquote(root.group(1)), "", "boards", ""
        match = re.fullmatch(
            r"/api/v1/repositories/([^/]+)/boards/([^/]+)(?:/(columns|cards|fields|views|dependencies))?(?:/([^/]+))?(?:/(move|fields))?",
            path,
        )
        if not match:
            return None
        repository_id = urllib.parse.unquote(match.group(1))
        board_id = urllib.parse.unquote(match.group(2))
        resource = urllib.parse.unquote(match.group(3) or "board")
        item_id = urllib.parse.unquote(match.group(4) or "")
        action = urllib.parse.unquote(match.group(5) or "")
        return repository_id, board_id, resource, ":".join(part for part in (item_id, action) if part)

    @staticmethod
    def contributor_board_route(path: str) -> tuple[str, str, str] | None:
        if path == "/api/v1/collaboration/boards":
            return "boards", "", ""
        match = re.fullmatch(r"/api/v1/collaboration/boards/([^/]+)(?:/cards/([^/]+)/move)?", path)
        if not match:
            return None
        return "board", urllib.parse.unquote(match.group(1)), urllib.parse.unquote(match.group(2) or "")

    def _get_project_boards(self, path: str, query: dict[str, list[str]]) -> bool:
        contributor = self.contributor_board_route(path)
        if contributor:
            resource, board_id, _card_id = contributor
            if resource == "boards":
                self.send_json(self.app.boards.list_boards_for_token(self.invite_token()))
            else:
                self.send_json(self.app.boards.get_board_for_token(self.invite_token(), board_id))
            return True
        owner = self.owner_board_route(path)
        if not owner:
            return False
        self.require_local_owner()
        repository_id, board_id, resource, item = owner
        if resource == "boards" and not board_id:
            self.send_json(self.app.boards.list_boards(repository_id))
        elif resource == "board" and not item:
            self.send_json(self.app.boards.get_board(repository_id, board_id))
        else:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        return True

    def _post_project_boards(self, path: str) -> bool:
        contributor = self.contributor_board_route(path)
        if contributor:
            resource, board_id, card_id = contributor
            if resource == "board" and card_id:
                data = self.read_json()
                self.send_json(self.app.boards.move_card_for_token(
                    self.invite_token(), board_id, card_id,
                    column_id=data.get("columnId"), before_card_id=data.get("beforeCardId", ""),
                    expected_version=data.get("expectedVersion"), actor_name=data.get("actorName"),
                ))
                return True
            return False
        owner = self.owner_board_route(path)
        if not owner:
            return False
        self.require_local_owner()
        repository_id, board_id, resource, item = owner
        data = self.read_json(); actor = str(data.get("actor") or "Repository Owner")
        if resource == "boards" and not board_id:
            self.send_json(self.app.boards.create_board(
                repository_id, name=data.get("name"), description=data.get("description", ""),
                default_view=data.get("defaultView", "kanban"), contributor_view=data.get("contributorView", False),
                contributor_move=data.get("contributorMove", False), actor=actor, request_id=self.request_id(),
            ), HTTPStatus.CREATED)
        elif resource == "columns" and not item:
            self.send_json(self.app.boards.create_column(repository_id, board_id, name=data.get("name"), color=data.get("color", "#7a8799"), actor=actor), HTTPStatus.CREATED)
        elif resource == "cards" and not item:
            self.send_json(self.app.boards.add_card(repository_id, board_id, topic_id=data.get("topicId"), column_id=data.get("columnId", ""), actor=actor), HTTPStatus.CREATED)
        elif resource == "cards" and item.endswith(":move"):
            card_id = item.split(":", 1)[0]
            self.send_json(self.app.boards.move_card(repository_id, board_id, card_id, column_id=data.get("columnId"), before_card_id=data.get("beforeCardId", ""), expected_version=data.get("expectedVersion"), actor=actor))
        elif resource == "cards" and item.endswith(":fields"):
            card_id = item.split(":", 1)[0]
            self.send_json(self.app.boards.set_card_fields(repository_id, board_id, card_id, values=data.get("values", {}), expected_version=data.get("expectedVersion"), actor=actor))
        elif resource == "fields" and not item:
            self.send_json(self.app.boards.create_field(repository_id, board_id, name=data.get("name"), field_type=data.get("fieldType"), options=data.get("options", []), actor=actor), HTTPStatus.CREATED)
        elif resource == "views" and not item:
            self.send_json(self.app.boards.create_saved_view(repository_id, board_id, name=data.get("name"), view_type=data.get("viewType"), filters=data.get("filters", {}), actor=actor), HTTPStatus.CREATED)
        elif resource == "dependencies" and not item:
            self.send_json(self.app.boards.add_dependency(repository_id, source_topic_id=data.get("sourceTopicId"), target_topic_id=data.get("targetTopicId"), kind=data.get("kind", "blocks"), actor=actor), HTTPStatus.CREATED)
        else:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        return True

    def _put_project_boards(self, path: str) -> bool:
        owner = self.owner_board_route(path)
        if not owner:
            return False
        self.require_local_owner()
        repository_id, board_id, resource, item = owner
        if resource != "board" or item:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        data = self.read_json()
        self.send_json(self.app.boards.update_board(
            repository_id, board_id, expected_version=data.get("expectedVersion"),
            name=data.get("name"), description=data.get("description"), default_view=data.get("defaultView"),
            contributor_view=data.get("contributorView"), contributor_move=data.get("contributorMove"),
            actor=str(data.get("actor") or "Repository Owner"), request_id=self.request_id(),
        ))
        return True

    def _delete_project_boards(self, path: str) -> bool:
        owner = self.owner_board_route(path)
        if not owner:
            return False
        self.require_local_owner()
        repository_id, board_id, resource, item = owner
        if resource != "board" or item:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.send_json(self.app.boards.delete_board(repository_id, board_id, expected_version=self.q(query, "expectedVersion"), actor=self.q(query, "actor", "Repository Owner"), request_id=self.request_id()))
        return True

    def _get_project_coordination(self, path: str, query: dict[str, list[str]]) -> bool:
        contributor = self.contributor_project_route(path)
        if contributor:
            resource, item_id, action = contributor
            token = self.invite_token()
            if resource == "overview":
                self.send_json(self.app.project.overview_for_token(token))
            elif resource in {"issues", "discussions"} and not item_id:
                self.send_json(self.app.project.list_topics_for_token(
                    token, kind="issue" if resource == "issues" else "discussion",
                    state=self.q(query, "state"), query=self.q(query, "query"),
                    label_id=self.q(query, "labelId"), milestone_id=self.q(query, "milestoneId"),
                    limit=self.q(query, "limit", "50"), offset=self.q(query, "offset", "0"),
                ))
            elif resource in {"issues", "discussions"} and item_id and not action:
                self.send_json(self.app.project.get_topic_for_token(
                    token, item_id, comment_limit=self.q(query, "commentLimit", "100"),
                    comment_offset=self.q(query, "commentOffset", "0"),
                ))
            else:
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
            return True
        owner = self.owner_project_route(path)
        if not owner:
            return False
        self.require_local_owner()
        repository_id, resource, item_id, action = owner
        if resource == "overview":
            self.send_json(self.app.project.overview(repository_id))
        elif resource == "labels" and not item_id:
            self.send_json({"labels": self.app.project.list_labels(repository_id)})
        elif resource == "milestones" and not item_id:
            self.send_json({"milestones": self.app.project.list_milestones(repository_id)})
        elif resource in {"issues", "discussions"} and not item_id:
            self.send_json(self.app.project.list_topics(
                repository_id, kind="issue" if resource == "issues" else "discussion",
                state=self.q(query, "state"), query=self.q(query, "query"),
                label_id=self.q(query, "labelId"), milestone_id=self.q(query, "milestoneId"),
                limit=self.q(query, "limit", "50"), offset=self.q(query, "offset", "0"),
            ))
        elif resource in {"issues", "discussions"} and item_id and not action:
            self.send_json(self.app.project.get_topic(
                repository_id, item_id, comment_limit=self.q(query, "commentLimit", "100"),
                comment_offset=self.q(query, "commentOffset", "0"),
            ))
        else:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        return True

    def _post_project_coordination(self, path: str) -> bool:
        contributor = self.contributor_project_route(path)
        if contributor:
            resource, item_id, action = contributor
            data = self.read_json()
            if resource in {"issues", "discussions"} and not item_id:
                self.send_json(self.app.project.create_topic_for_token(
                    self.invite_token(), kind="issue" if resource == "issues" else "discussion",
                    title=data.get("title"), body=data.get("body", ""),
                    references=data.get("references", []), actor_name=data.get("authorName"),
                    request_id=self.request_id(),
                ), HTTPStatus.CREATED)
            elif resource in {"issues", "discussions"} and item_id and action == "comments":
                self.send_json(self.app.project.add_comment_for_token(
                    self.invite_token(), item_id, body=data.get("body"),
                    references=data.get("references", []), expected_version=data.get("expectedVersion"),
                    actor_name=data.get("authorName"), request_id=self.request_id(),
                ), HTTPStatus.CREATED)
            else:
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
            return True
        owner = self.owner_project_route(path)
        if not owner:
            return False
        self.require_local_owner()
        repository_id, resource, item_id, action = owner
        data = self.read_json()
        if resource == "labels" and not item_id:
            self.send_json(self.app.project.create_label(
                repository_id, name=data.get("name"), color=data.get("color", "#6e7681"),
                description=data.get("description", ""), actor=str(data.get("actor") or "Repository Owner"),
                request_id=self.request_id(),
            ), HTTPStatus.CREATED)
        elif resource == "milestones" and not item_id:
            self.send_json(self.app.project.create_milestone(
                repository_id, title=data.get("title"), description=data.get("description", ""),
                due_at=data.get("dueAt", ""), actor=str(data.get("actor") or "Repository Owner"),
                request_id=self.request_id(),
            ), HTTPStatus.CREATED)
        elif resource in {"issues", "discussions"} and not item_id:
            self.send_json(self.app.project.create_topic(
                repository_id, kind="issue" if resource == "issues" else "discussion",
                title=data.get("title"), body=data.get("body", ""), references=data.get("references", []),
                actor_role="owner", actor_name=data.get("authorName") or "Repository Owner",
                request_id=self.request_id(),
            ), HTTPStatus.CREATED)
        elif resource in {"issues", "discussions"} and item_id and action == "comments":
            self.send_json(self.app.project.add_comment(
                repository_id, item_id, body=data.get("body"), references=data.get("references", []),
                expected_version=data.get("expectedVersion"), actor_role="owner",
                actor_name=data.get("authorName") or "Repository Owner", request_id=self.request_id(),
            ), HTTPStatus.CREATED)
        elif resource == "comments" and item_id and action == "moderate":
            self.send_json(self.app.project.moderate_comment(
                repository_id, item_id, expected_version=data.get("expectedVersion"),
                actor=str(data.get("actor") or "Repository Owner"), reason=data.get("reason", ""),
                request_id=self.request_id(),
            ))
        else:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        return True

    def _put_project_coordination(self, path: str) -> bool:
        owner = self.owner_project_route(path)
        if not owner:
            return False
        self.require_local_owner()
        repository_id, resource, item_id, action = owner
        if not item_id or action:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        data = self.read_json()
        if resource == "labels":
            self.send_json(self.app.project.update_label(
                repository_id, item_id, expected_version=data.get("expectedVersion"),
                name=data.get("name"), color=data.get("color"), description=data.get("description"),
                actor=str(data.get("actor") or "Repository Owner"), request_id=self.request_id(),
            ))
        elif resource == "milestones":
            self.send_json(self.app.project.update_milestone(
                repository_id, item_id, expected_version=data.get("expectedVersion"),
                title=data.get("title"), description=data.get("description"), due_at=data.get("dueAt"),
                state=data.get("state"), actor=str(data.get("actor") or "Repository Owner"),
                request_id=self.request_id(),
            ))
        elif resource in {"issues", "discussions"}:
            self.send_json(self.app.project.update_topic(
                repository_id, item_id, expected_version=data.get("expectedVersion"),
                title=data.get("title"), body=data.get("body"), references=data.get("references"),
                state=data.get("state"), milestone_id=data.get("milestoneId"), assignee=data.get("assignee"),
                due_at=data.get("dueAt"), locked=data.get("locked"), pinned=data.get("pinned"),
                label_ids=data.get("labelIds"), accepted_comment_id=data.get("acceptedCommentId"),
                actor=str(data.get("actor") or "Repository Owner"), request_id=self.request_id(),
            ))
        else:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        return True

    def _delete_project_coordination(self, path: str) -> bool:
        owner = self.owner_project_route(path)
        if not owner:
            return False
        self.require_local_owner()
        repository_id, resource, item_id, action = owner
        if not item_id or action:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        actor = self.q(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query), "actor", "Repository Owner")
        if resource == "labels":
            self.send_json(self.app.project.delete_label(repository_id, item_id, actor=actor, request_id=self.request_id()))
        elif resource == "milestones":
            self.send_json(self.app.project.delete_milestone(repository_id, item_id, actor=actor, request_id=self.request_id()))
        elif resource in {"issues", "discussions"}:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self.send_json(self.app.project.delete_topic(
                repository_id, item_id, actor=actor, reason=self.q(query, "reason"), request_id=self.request_id(),
            ))
        else:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        return True

    def _security_history_error(self, exc: SecurityEventError) -> ForgeTraceError:
        return ForgeTraceError(
            str(exc),
            HTTPStatus.CONFLICT,
            "security_history_operation_failed",
        )

    def _get_security_history(self, path: str, query: dict[str, list[str]]) -> bool:
        try:
            if path == "/api/v1/security-events/segments":
                self.require_local_owner()
                self.send_json(self.app.security_events.operational_status())
                return True
            if path == "/api/v1/security-events/retention-policy":
                self.require_local_owner()
                self.send_json(self.app.security_events.get_retention_policy())
                return True
            if path == "/api/v1/security-events/rotations":
                self.require_local_owner()
                self.send_json({"rotations": self.app.security_events.list_rotation_journals()})
                return True
            if path == "/api/v1/security-events/anchors":
                self.require_local_owner()
                self.send_json(self.app.security_events.list_anchors())
                return True
            anchor_export = re.fullmatch(r"/api/v1/security-events/anchors/(anchor_[0-9a-f]{32})/export", path)
            if anchor_export:
                self.require_local_owner()
                anchor_id = anchor_export.group(1)
                self.send_json_download(
                    self.app.security_events.get_anchor_request(anchor_id),
                    filename=f"forgetrace-{anchor_id}.json",
                )
                return True
        except SecurityEventError as exc:
            raise self._security_history_error(exc) from exc
        return False

    def _post_security_history(self, path: str) -> bool:
        try:
            if path == "/api/v1/security-events/rotation-preview":
                self.require_local_owner()
                data = self.read_json()
                self.send_json(self.app.security_events.preview_rotation(rotate_count=data.get("rotateCount")))
                return True
            if path == "/api/v1/security-events/rotate":
                self.require_local_owner()
                data = self.read_json()
                self.send_json(self.app.security_events.execute_rotation(
                    preview_id=str(data.get("previewId", "")),
                    rotate_count=data.get("rotateCount"),
                    request_id=self.request_id(),
                    actor="owner",
                ))
                return True
            if path == "/api/v1/security-events/retention-policy":
                self.require_local_owner()
                data = self.read_json()
                self.send_json(self.app.security_events.update_retention_policy(
                    data, request_id=self.request_id(), actor="owner"
                ))
                return True
            if path == "/api/v1/security-events/anchors":
                self.require_local_owner()
                self.read_body(1024)
                self.send_json(
                    self.app.security_events.create_anchor_request(
                        request_id=self.request_id(), actor="owner"
                    ),
                    HTTPStatus.CREATED,
                )
                return True
            receipt_match = re.fullmatch(
                r"/api/v1/security-events/anchors/(anchor_[0-9a-f]{32})/receipt", path
            )
            if receipt_match:
                self.require_local_owner()
                data = self.read_json()
                self.send_json(self.app.security_events.record_anchor_receipt(
                    receipt_match.group(1),
                    anchored_digest=str(data.get("anchoredDigest", "")),
                    mechanism=str(data.get("mechanism", "")),
                    external_reference=str(data.get("externalReference", "")),
                    evidence=str(data.get("evidence", "")),
                    published_at=str(data.get("publishedAt", "")),
                    request_id=self.request_id(),
                    actor="owner",
                ))
                return True
        except SecurityEventError as exc:
            raise self._security_history_error(exc) from exc
        return False

    def _get_git_intelligence(self, path: str, query: dict[str, list[str]]) -> bool:
        route = self.repository_route(path)
        if not route:
            return False
        repository_id, action = route
        if action == "git":
            self.require_local_owner()
            self.send_json(self.app.git.overview(
                repository_id, commit_limit=self.q(query, "commitLimit", "50")
            ))
            return True
        if action == "git/diff":
            self.require_local_owner()
            self.send_json(self.app.git.diff(
                repository_id,
                scope=self.q(query, "scope", "working"),
                path=self.q(query, "path"),
                commit=self.q(query, "commit"),
            ))
            return True
        if action == "git/writes":
            self.require_local_owner()
            self.send_json(self.app.git_writes.status(
                repository_id, receipt_limit=self.q(query, "receiptLimit", "25")
            ))
            return True
        commit_match = re.fullmatch(r"git/commits/([0-9a-fA-F]{40,64})", action)
        if commit_match:
            self.require_local_owner()
            self.send_json(self.app.git.commit_detail(repository_id, commit_match.group(1)))
            return True
        return False

    def _get_health(self, path: str, query: dict[str, list[str]]) -> bool:
        if path == "/api/v1/health/reports":
            self.require_local_owner()
            self.send_json(self.app.health.list_reports(
                limit=self.q(query, "limit", "25"),
                offset=self.q(query, "offset", "0"),
            ))
            return True
        export_match = re.fullmatch(r"/api/v1/health/reports/(health_[0-9a-f]{32})/export", path)
        if export_match:
            self.require_local_owner()
            report_id = export_match.group(1)
            self.send_json_download(
                self.app.health.export_report(report_id, request_id=self.request_id()),
                filename=f"forgetrace-{report_id}.json",
            )
            return True
        report_match = re.fullmatch(r"/api/v1/health/reports/(health_[0-9a-f]{32})", path)
        if report_match:
            self.require_local_owner()
            self.send_json(self.app.health.get_report(report_match.group(1)))
            return True
        return False

    def _post_health(self, path: str) -> bool:
        if path != "/api/v1/health/reports":
            return False
        self.require_local_owner()
        data = self.read_json()
        self.send_json(
            self.app.health.generate(
                request_id=self.request_id(),
                repository_id=str(data.get("repositoryId", "")),
                scope=str(data.get("scope", "standard")),
                limits=data.get("limits"),
            ),
            HTTPStatus.CREATED,
        )
        return True

    def _get_conflict_resolutions(self, path: str, query: dict[str, list[str]]) -> bool:
        route = self.owner_conflict_resolution_route(path)
        if not route:
            return False
        self.require_local_owner()
        repository_id, pull_request_id, draft_id, action = route
        store = self.app.collaboration.conflict_resolutions
        if action == "collection" and not draft_id:
            include_content = self.q(query, "content", "1").lower() not in {"0", "false", "no"}
            self.send_json(store.list_owner(
                repository_id, pull_request_id, include_content=include_content
            ))
        elif action == "draft" and draft_id:
            self.send_json(store.get_owner(repository_id, pull_request_id, draft_id))
        else:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        return True

    def _post_conflict_resolutions(self, path: str) -> bool:
        route = self.owner_conflict_resolution_route(path)
        if not route:
            return False
        self.require_local_owner()
        repository_id, pull_request_id, draft_id, action = route
        data = self.read_json()
        store = self.app.collaboration.conflict_resolutions
        if action == "collection" and not draft_id:
            self.send_json(store.prepare_owner(
                repository_id,
                pull_request_id,
                actor_name=str(data.get("actorName", "")),
                expected_pull_request_revision=data.get("expectedPullRequestRevision"),
                request_id=self.request_id(),
            ), HTTPStatus.CREATED)
        elif action == "decision" and draft_id:
            self.send_json(store.save_decision_owner(
                repository_id,
                pull_request_id,
                draft_id,
                actor_name=str(data.get("actorName", "")),
                decision=str(data.get("decision", "")),
                manual_text=data.get("manualText"),
                expected_version=data.get("expectedVersion"),
                request_id=self.request_id(),
            ))
        elif action == "confirm" and draft_id:
            self.send_json(store.confirm_owner(
                repository_id,
                pull_request_id,
                draft_id,
                actor_name=str(data.get("actorName", "")),
                expected_version=data.get("expectedVersion"),
                request_id=self.request_id(),
            ))
        else:
            raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
        return True

    def _get_review_conversations(self, path: str, query: dict[str, list[str]]) -> bool:
        owner_route = self.owner_review_conversation_route(path)
        if owner_route:
            self.require_local_owner()
            repository_id, pull_request_id, thread_id, action = owner_route
            store = self.app.collaboration.review_conversations
            if action == "collection" and not thread_id:
                self.send_json(store.list_for_owner(
                    repository_id, pull_request_id,
                    cursor=self.q(query, "cursor", "0"),
                    limit=self.q(query, "limit", "50"),
                    revision=self.q(query, "revision", "0"),
                    comment_limit=self.q(query, "commentLimit", "50"),
                ))
            elif action == "thread" and thread_id:
                self.send_json(store.get_for_owner(
                    repository_id, pull_request_id, thread_id,
                    comment_cursor=self.q(query, "commentCursor", "0"),
                    comment_limit=self.q(query, "commentLimit", "50"),
                ))
            else:
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
            return True
        contributor_route = self.contributor_review_conversation_route(path)
        if contributor_route:
            pull_request_id, thread_id, action = contributor_route
            store = self.app.collaboration.review_conversations
            token = self.invite_token()
            if action == "collection" and not thread_id:
                self.send_json(store.list_for_token(
                    token, pull_request_id,
                    cursor=self.q(query, "cursor", "0"),
                    limit=self.q(query, "limit", "50"),
                    revision=self.q(query, "revision", "0"),
                    comment_limit=self.q(query, "commentLimit", "50"),
                ))
            elif action == "thread" and thread_id:
                self.send_json(store.get_for_token(
                    token, pull_request_id, thread_id,
                    comment_cursor=self.q(query, "commentCursor", "0"),
                    comment_limit=self.q(query, "commentLimit", "50"),
                ))
            else:
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
            return True
        return False

    def _post_review_conversations(self, path: str) -> bool:
        owner_route = self.owner_review_conversation_route(path)
        if owner_route:
            self.require_local_owner()
            repository_id, pull_request_id, thread_id, action = owner_route
            data = self.read_json()
            store = self.app.collaboration.review_conversations
            if action == "collection" and not thread_id:
                self.send_json(store.create_for_owner(
                    repository_id, pull_request_id,
                    actor_name=str(data.get("actorName", "")),
                    body=str(data.get("body", "")),
                    submitted_revision=data.get("submittedRevision"),
                    expected_pull_request_revision=data.get("expectedPullRequestRevision"),
                    path=str(data.get("path", "")),
                    start_line=data.get("startLine"),
                    end_line=data.get("endLine"),
                    request_changes=bool(data.get("requestChanges", False)),
                    request_id=self.request_id(),
                ), HTTPStatus.CREATED)
            elif action == "comments" and thread_id:
                self.send_json(store.reply_owner(
                    repository_id, pull_request_id, thread_id,
                    actor_name=str(data.get("actorName", "")),
                    body=str(data.get("body", "")),
                    expected_version=data.get("expectedVersion"),
                    request_id=self.request_id(),
                ), HTTPStatus.CREATED)
            elif action in {"resolve", "reopen"} and thread_id:
                method = store.resolve_owner if action == "resolve" else store.reopen_owner
                self.send_json(method(
                    repository_id, pull_request_id, thread_id,
                    actor_name=str(data.get("actorName", "")),
                    expected_version=data.get("expectedVersion"),
                    request_id=self.request_id(),
                ))
            else:
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
            return True
        contributor_route = self.contributor_review_conversation_route(path)
        if contributor_route:
            pull_request_id, thread_id, action = contributor_route
            data = self.read_json()
            store = self.app.collaboration.review_conversations
            token = self.invite_token()
            if action == "collection" and not thread_id:
                self.send_json(store.create_for_token(
                    token, pull_request_id,
                    body=str(data.get("body", "")),
                    submitted_revision=data.get("submittedRevision"),
                    expected_pull_request_revision=data.get("expectedPullRequestRevision"),
                    path=str(data.get("path", "")),
                    start_line=data.get("startLine"),
                    end_line=data.get("endLine"),
                    request_id=self.request_id(),
                ), HTTPStatus.CREATED)
            elif action == "comments" and thread_id:
                self.send_json(store.reply_token(
                    token, pull_request_id, thread_id,
                    body=str(data.get("body", "")),
                    expected_version=data.get("expectedVersion"),
                    request_id=self.request_id(),
                ), HTTPStatus.CREATED)
            else:
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
            return True
        return False

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
            self.send_header("X-ForgeTrace-Request-Id", self.request_id())
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
        self.send_header("X-ForgeTrace-Request-Id", self.request_id())
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
            self.audit_security_event(
                category="export",
                action="sensitive_export_confirmation_required",
                outcome="denied",
                severity="warning",
                repository_id=service.repository_id or repository_id,
                details={"sensitiveCount": preview.get("sensitiveCount", 0)},
            )
            raise ForgeTraceError(
                "This export contains files that may include secrets or private metadata. Preview and explicitly confirm the export first.",
                HTTPStatus.CONFLICT,
                "sensitive_export_confirmation_required",
                preview,
            )
        if include_sensitive:
            self.audit_security_event(
                required=True,
                category="export",
                action="sensitive_export_authorized",
                outcome="authorized",
                severity="warning",
                repository_id=service.repository_id or repository_id,
                details={"sensitiveCount": preview.get("sensitiveCount", 0)},
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
            self.audit_security_event(
                category="export",
                action="repository_export_generated",
                outcome="success",
                severity="warning" if include_sensitive else "info",
                repository_id=service.repository_id or repository_id,
                details={
                    "includeHistory": self.q(query, "history", "1") != "0",
                    "includeSensitive": include_sensitive,
                    "archiveBytes": archive_path.stat().st_size,
                },
            )
            self.send_file_path(
                archive_path, content_type="application/zip",
                filename=f"{safe}-export.zip", delete_after=True,
            )
        except Exception as exc:
            self.audit_security_event(
                category="export",
                action="repository_export_failed",
                outcome="failure",
                severity="error",
                repository_id=service.repository_id or repository_id,
                details={
                    "includeHistory": self.q(query, "history", "1") != "0",
                    "includeSensitive": include_sensitive,
                    "errorType": type(exc).__name__,
                },
            )
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
                        "securityEventSchemaVersion": SECURITY_EVENT_SCHEMA_VERSION,
                        "securitySegmentSchemaVersion": SECURITY_SEGMENT_SCHEMA_VERSION,
                        "securityRotationJournalSchemaVersion": SECURITY_ROTATION_JOURNAL_SCHEMA_VERSION,
                        "securityRetentionPolicySchemaVersion": SECURITY_RETENTION_POLICY_SCHEMA_VERSION,
                        "securityAnchorSchemaVersion": SECURITY_ANCHOR_SCHEMA_VERSION,
                        "healthReportSchemaVersion": HEALTH_REPORT_SCHEMA_VERSION,
                        "gitIntelligenceSchemaVersion": GIT_INTELLIGENCE_SCHEMA_VERSION,
                        "gitWriteSchemaVersion": GIT_WRITE_SCHEMA_VERSION,
                        "projectCoordinationSchemaVersion": PROJECT_COORDINATION_SCHEMA_VERSION,
                        "projectBoardsSchemaVersion": PROJECT_BOARDS_SCHEMA_VERSION,
                        "releasesSchemaVersion": RELEASES_SCHEMA_VERSION,
                    }
                )
                return
            if path in {"/api/v1/security-events", "/api/v1/security-events/export"}:
                filters = {
                    "category": self.q(query, "category"),
                    "severity": self.q(query, "severity"),
                    "action": self.q(query, "action"),
                    "outcome": self.q(query, "outcome"),
                    "surface": self.q(query, "surface"),
                    "repository_id": self.q(query, "repositoryId"),
                    "search": self.q(query, "search"),
                    "since": self.q(query, "since"),
                    "until": self.q(query, "until"),
                }
                if path.endswith("/export"):
                    if not getattr(self, "_head_only", False):
                        self.audit_security_event(
                            category="security",
                            action="security_events_exported",
                            outcome="success",
                            subject_id="json",
                            details={
                                "filterFields": sorted(key for key, value in filters.items() if value),
                                "repositoryScoped": bool(filters.get("repository_id")),
                            },
                        )
                    self.send_json_download(
                        self.app.security_events.export(**filters),
                        filename="forgetrace-security-events.json",
                    )
                else:
                    self.send_json(self.app.security_events.query(
                        **filters,
                        limit=self.q(query, "limit", "100"),
                        offset=self.q(query, "offset", "0"),
                    ))
                return
            if path == "/api/v1/security-events/integrity":
                self.send_json(self.app.security_events.verify_integrity())
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
            if path == "/api/v1/registry/restores":
                self.send_json({"restores": self.app.registry.list_registry_restores()})
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

            if self._get_releases(path, query):
                return
            if self._get_project_boards(path, query):
                return
            if self._get_project_coordination(path, query):
                return
            if self._get_security_history(path, query):
                return
            if self._get_git_intelligence(path, query):
                return
            if self._get_health(path, query):
                return
            if self._get_conflict_resolutions(path, query):
                return
            if self._get_review_conversations(path, query):
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

    def _post_registry_recovery(self, path: str, _query: dict[str, list[str]]) -> bool:
        if path == "/api/v1/registry/backup":
            data = self.read_json()
            result = self.app.registry.create_backup(str(data.get("label", "manual")))
            self.audit_security_event(
                category="recovery",
                action="registry_backup_created",
                outcome="success",
                subject_id=result.get("name", ""),
                details={"bytes": result.get("bytes", 0), "verified": bool(result.get("verified"))},
            )
            self.send_json(result, HTTPStatus.CREATED)
            return True
        if path == "/api/v1/registry/restore/preview":
            data = self.read_json()
            result = self.app.registry.preview_registry_restore(
                data.get("backupName", ""), data.get("mode", "")
            )
            self.audit_security_event(
                category="recovery",
                action="registry_restore_previewed",
                outcome="success",
                subject_id=result.get("backupName", ""),
                details={
                    "mode": result.get("mode", ""),
                    "previewIdPrefix": str(result.get("previewId", ""))[:16],
                    "repositoriesAdded": result.get("impact", {}).get("repositoriesAdded", 0),
                    "repositoriesRemoved": result.get("impact", {}).get("repositoriesRemoved", 0),
                    "pathConflicts": result.get("impact", {}).get("pathConflicts", 0),
                },
            )
            self.send_json(result)
            return True
        if path == "/api/v1/registry/restore":
            data = self.read_json()
            backup_name = str(data.get("backupName", ""))
            mode = str(data.get("mode", ""))
            self.audit_security_event(
                required=True,
                category="recovery",
                action="registry_restore_authorized",
                outcome="authorized",
                subject_id=backup_name,
                details={"mode": mode, "previewIdPrefix": str(data.get("previewId", ""))[:16]},
            )
            try:
                result = self.app.registry.restore_registry_backup(
                    backup_name, mode, data.get("previewId", "")
                )
            except ForgeTraceError as exc:
                self.audit_security_event(
                    category="recovery",
                    action="registry_restore",
                    outcome="failure",
                    severity="critical",
                    subject_id=backup_name,
                    details={"mode": mode, "errorCode": exc.code},
                )
                raise
            self.audit_security_event(
                category="recovery",
                action="registry_restore",
                outcome="success",
                severity="warning",
                subject_id=result.get("restoreId", ""),
                details={
                    "mode": result.get("mode", ""),
                    "backupName": result.get("backupName", ""),
                    "repositoryCountBefore": result.get("repositoryCountBefore"),
                    "repositoryCountAfter": result.get("repositoryCountAfter"),
                    "rollbackAvailable": bool(result.get("rollbackAvailable")),
                },
            )
            self.send_json(result)
            return True
        rollback_match = re.fullmatch(r"/api/v1/registry/restores/([^/]+)/rollback", path)
        if rollback_match:
            self.read_json()
            restore_id = urllib.parse.unquote(rollback_match.group(1))
            self.audit_security_event(
                required=True,
                category="recovery",
                action="registry_restore_rollback_authorized",
                outcome="authorized",
                subject_id=restore_id,
            )
            try:
                result = self.app.registry.rollback_registry_restore(restore_id)
            except ForgeTraceError as exc:
                self.audit_security_event(
                    category="recovery",
                    action="registry_restore_rollback",
                    outcome="failure",
                    severity="critical",
                    subject_id=restore_id,
                    details={"errorCode": exc.code},
                )
                raise
            self.audit_security_event(
                category="recovery",
                action="registry_restore_rollback",
                outcome="success",
                severity="warning",
                subject_id=restore_id,
                details={"state": result.get("state", "")},
            )
            self.send_json(result)
            return True
        return False

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
        if self._post_registry_recovery(path, query):
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
            repair = bool(data.get("repair", False))
            if repair:
                self.audit_security_event(
                    required=True,
                    category="recovery",
                    action="doctor_repair_authorized",
                    outcome="authorized",
                    severity="warning",
                    details={"scanRootCount": len(roots)},
                )
            result = self.app.registry.doctor(
                repair=repair,
                scan_roots=[Path(str(value)) for value in roots if str(value).strip()],
            )
            self.audit_security_event(
                category="recovery",
                action="doctor_repair" if repair else "doctor_check",
                outcome="success" if result.get("healthy") else "attention_required",
                severity="warning" if result.get("issues") else "info",
                details={
                    "repair": repair,
                    "issueCount": result.get("summary", {}).get("total", 0),
                    "actionCount": len(result.get("actions", [])),
                    "backupCreated": bool(result.get("backup")),
                },
            )
            self.send_json(result)
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
        if self.contributor_review_conversation_route(path):
            return self._post_review_conversations(path)
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
        if self.owner_conflict_resolution_route(path):
            return self._post_conflict_resolutions(path)
        if self.owner_review_conversation_route(path):
            return self._post_review_conversations(path)
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
                    allow_project_participation=bool(data.get("allowProjectParticipation", False)),
                ), HTTPStatus.CREATED)
            elif action == "review" and resource_id:
                data = self.read_json()
                self.send_json(self.app.collaboration.review_pull_request(
                    repository_id, resource_id,
                    reviewer=str(data.get("reviewer", "")),
                    verdict=str(data.get("verdict", "comment")),
                    comment=str(data.get("comment", "")),
                    expected_revision=data.get("expectedRevision"),
                    request_id=self.request_id(),
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
                    request_id=self.request_id(),
                ))
            elif action == "close" and resource_id:
                self.read_body(1024)
                self.send_json(self.app.collaboration.close_pull_request(repository_id, resource_id))
            else:
                raise ForgeTraceError("API route not found.", HTTPStatus.NOT_FOUND, "route_not_found")
            return True
        return False

    def _post_git_writes(self, path: str) -> bool:
        routed = self.repository_route(path)
        if not routed:
            return False
        repository_id, action = routed
        if action == "git/writes/preview":
            self.require_local_owner()
            data = self.read_json()
            self.send_json(self.app.git_writes.preview(repository_id, data))
            return True
        if action == "git/writes/execute":
            self.require_local_owner()
            data = self.read_json()
            self.send_json(self.app.git_writes.execute(
                repository_id,
                preview_id=str(data.get("previewId", "")),
                confirmation=str(data.get("confirmation", "")),
                actor=str(data.get("actor", "Repository Owner")),
                request_id=self.request_id(),
                surface=self.request_surface(),
            ))
            return True
        return False

    def _post_repository(self, path: str, query: dict[str, list[str]]) -> bool:
        routed = self.repository_route(path)
        if routed:
            repository_id, action = routed
            if action == "upload":
                service = self.app.repository(repository_id)
                service.require_writable("file upload")
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
                service.require_writable("local folder import")
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
                service = self.app.repository(repository_id)
                service.require_writable("local folder import")
                self.send_json(
                    service.import_local_folder(
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
            elif action == "access-mode":
                self.require_local_owner()
                data = self.read_json()
                selected_mode = str(data.get("mode", ""))
                actor = str(data.get("actor", "Repository Owner"))
                current = self.app.registry.get_repository(repository_id)
                self.audit_security_event(
                    required=True,
                    category="repository_access",
                    action="repository_access_mode_change_authorized",
                    outcome="authorized",
                    severity="warning" if selected_mode == "read_only" else "info",
                    repository_id=repository_id,
                    actor=actor,
                    subject_id=selected_mode,
                    details={
                        "previousMode": current.get("accessMode", ""),
                        "requestedMode": selected_mode,
                    },
                )
                try:
                    result = self.app.registry.set_access_mode(repository_id, selected_mode)
                except ForgeTraceError as exc:
                    self.audit_security_event(
                        category="repository_access",
                        action="repository_access_mode_change",
                        outcome="failure",
                        severity="error",
                        repository_id=repository_id,
                        actor=actor,
                        subject_id=selected_mode,
                        details={"errorCode": exc.code},
                    )
                    raise
                self.audit_security_event(
                    category="repository_access",
                    action="repository_access_mode_change",
                    outcome="success",
                    severity="warning" if result.get("accessMode") == "read_only" else "info",
                    repository_id=repository_id,
                    actor=actor,
                    subject_id=result.get("accessMode", ""),
                    details={
                        "previousMode": current.get("accessMode", ""),
                        "accessMode": result.get("accessMode", ""),
                        "effectiveMode": result.get("accessPolicy", {}).get("effectiveMode", ""),
                    },
                )
                self.send_json(result)
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
            service.require_writable("file upload")
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
            path, query = self.parsed(); self.enforce_remote_boundary(path)
            if not path.startswith("/api/v1/collaboration/"):
                self.enforce_owner_request_origin(path)
            self.legacy_route = path.startswith("/api/") and not path.startswith("/api/v1/")
            if self._post_releases(path) or self._post_project_boards(path) or self._post_project_coordination(path):
                return
            if self._post_security_history(path) or self._post_health(path) or self._post_global(path, query):
                return
            if self._post_contributor(path, query):
                return
            if self._post_owner(path, query) or self._post_git_writes(path) or self._post_repository(path, query):
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
            if self._put_releases(path) or self._put_project_boards(path) or self._put_project_coordination(path):
                return
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
            if self._delete_project_boards(path) or self._delete_project_coordination(path):
                return
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
            elif routed and routed[1] == "delete-managed":
                self.require_local_owner()
                repository_id = routed[0]
                actor = self.q(query, "actor")
                self.audit_security_event(
                    required=True,
                    category="repository",
                    action="managed_repository_delete_authorized",
                    outcome="authorized",
                    severity="critical",
                    repository_id=repository_id,
                    actor=actor,
                    details={"deletionScope": "managed_repository_directory_and_registry"},
                )
                try:
                    result = self.app.registry.delete_managed_repository(repository_id)
                except Exception as exc:
                    self.audit_security_event(
                        category="repository",
                        action="managed_repository_delete_failed",
                        outcome="failure",
                        severity="error",
                        repository_id=repository_id,
                        actor=actor,
                        details={
                            "errorType": type(exc).__name__,
                            "errorCode": getattr(exc, "code", ""),
                        },
                    )
                    raise
                self.audit_security_event(
                    category="repository",
                    action="managed_repository_deleted",
                    outcome="success",
                    severity="critical",
                    repository_id=repository_id,
                    actor=actor,
                    details={
                        "filesDeleted": bool(result.get("filesDeleted")),
                        "cleanupPending": bool(result.get("cleanupPending")),
                        "tombstoned": bool(result.get("tombstoned")),
                    },
                )
                self.send_json(result)
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
        self.send_header("X-ForgeTrace-Request-Id", self.request_id())
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
