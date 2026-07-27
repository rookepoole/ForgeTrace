from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from .constants import APP_VERSION
from .collaboration import CollaborationService
from .errors import ForgeTraceError
from .locks import FileLock, LockUnavailable
from .project_coordination import ProjectCoordinationService
from .project_boards import ProjectBoardService
from .releases import ReleaseService
from .registry import RepositoryRegistry
from .security_events import SecurityEventLedger
from .utils import platform_data_dir, utc_now
from .web import ForgeTraceApplication, configure_logging, create_server


def build_application(project_root: Path, data_dir: Path | None = None) -> ForgeTraceApplication:
    resolved_data_dir = platform_data_dir(data_dir)
    registry = RepositoryRegistry(project_root, resolved_data_dir)
    security_events = SecurityEventLedger(resolved_data_dir)
    collaboration = CollaborationService(registry, security_events=security_events)
    project_coordination = ProjectCoordinationService(
        registry=registry, collaboration=collaboration, security_events=security_events
    )
    project_boards = ProjectBoardService(
        registry=registry, project_coordination=project_coordination,
        collaboration=collaboration, security_events=security_events
    )
    releases = ReleaseService(registry=registry, collaboration=collaboration, security_events=security_events)
    application = ForgeTraceApplication(
        project_root, registry, collaboration, security_events, project_coordination, project_boards, releases
    )
    application.gateway = CollaborationGatewayManager(application)
    application.audit(
        category="application",
        action="owner_application_initialized",
        outcome="success",
        surface="system",
        details={
            "version": APP_VERSION,
            "registryDatabase": registry.db_path.name,
            "securityLedgerIntegrity": security_events.startup_integrity,
        },
    )
    security_rotation_recovery = security_events.startup_rotation_recovery
    if security_rotation_recovery.get("actions"):
        application.audit(
            category="recovery",
            action="startup_security_rotation_recovery",
            outcome="attention_required" if security_rotation_recovery.get("failed") else "success",
            severity="warning" if not security_rotation_recovery.get("failed") else "critical",
            surface="system",
            details={
                "checked": security_rotation_recovery.get("checked", 0),
                "rolledBack": security_rotation_recovery.get("rolledBack", 0),
                "completed": security_rotation_recovery.get("completed", 0),
                "failed": security_rotation_recovery.get("failed", 0),
            },
        )
    restore_recovery = registry.startup_restore_recovery_report
    if restore_recovery.get("actions"):
        application.audit(
            category="recovery",
            action="startup_registry_restore_recovery",
            outcome="success" if not any(item.get("action") == "recovery_failed" for item in restore_recovery.get("actions", [])) else "attention_required",
            severity="warning",
            surface="system",
            details={
                "checked": restore_recovery.get("checked", 0),
                "finalized": restore_recovery.get("finalized", 0),
                "rolledBack": restore_recovery.get("rolledBack", 0),
                "abandoned": restore_recovery.get("abandoned", 0),
                "actionCount": len(restore_recovery.get("actions", [])),
            },
        )
    deletion_recovery = registry.startup_repository_deletion_recovery_report
    if deletion_recovery.get("actions"):
        application.audit(
            category="recovery",
            action="startup_managed_repository_deletion_recovery",
            outcome="attention_required" if deletion_recovery.get("retained") else "success",
            severity="warning" if deletion_recovery.get("retained") else "info",
            surface="system",
            details={
                "checked": deletion_recovery.get("checked", 0),
                "rolledBack": deletion_recovery.get("rolledBack", 0),
                "finalized": deletion_recovery.get("finalized", 0),
                "retained": deletion_recovery.get("retained", 0),
            },
        )
    git_write_recovery = application.git_writes.startup_recovery_report
    if git_write_recovery.get("actions"):
        application.audit(
            category="recovery",
            action="startup_git_write_recovery_summary",
            outcome="attention_required" if git_write_recovery.get("retained") else "success",
            severity="warning" if git_write_recovery.get("retained") else "info",
            surface="system",
            details={
                "checked": git_write_recovery.get("checked", 0),
                "rolledBack": git_write_recovery.get("rolledBack", 0),
                "cleanedCommitted": git_write_recovery.get("cleanedCommitted", 0),
                "recoveredReceipts": git_write_recovery.get("recoveredReceipts", 0),
                "retained": git_write_recovery.get("retained", 0),
                "deferred": git_write_recovery.get("deferred", 0),
                "manualInspection": git_write_recovery.get("manualInspection", 0),
            },
        )
    cleanup = registry.startup_cleanup_report
    if cleanup.get("removedCount"):
        application.audit(
            category="recovery",
            action="startup_artifacts_cleaned",
            outcome="success",
            surface="system",
            details={"removedCount": cleanup.get("removedCount", 0)},
        )
    recovery = registry.startup_recovery_report
    if any(int(recovery.get(key, 0) or 0) for key in ("registered", "relinked")):
        application.audit(
            category="recovery",
            action="startup_repository_recovery",
            outcome="success",
            surface="system",
            details={"registered": recovery.get("registered", 0), "relinked": recovery.get("relinked", 0)},
        )
    return application


def discover_lan_addresses() -> list[str]:
    addresses: list[str] = []

    def add(address: str) -> None:
        value = str(address or "").strip()
        if not value or value.startswith("127.") or value == "0.0.0.0":
            return
        if value not in addresses:
            addresses.append(value)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            add(probe.getsockname()[0])
    except OSError:
        pass
    try:
        for address in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(address)
    except OSError:
        pass
    return addresses


def discover_lan_address() -> str:
    """Backward-compatible single-address helper."""
    addresses = discover_lan_addresses()
    return addresses[0] if addresses else "YOUR-LAN-IP"


class CollaborationGatewayManager:
    """Own the optional restricted contributor listener for one ForgeTrace process.

    The owner server always remains loopback-only. Enabling sharing creates a second
    HTTP listener whose handler surface is permanently restricted to contributor
    routes, including for requests originating from the owner machine itself.
    """

    def __init__(self, application: ForgeTraceApplication) -> None:
        self.application = application
        self._lock = threading.RLock()
        self._server = None
        self._thread: threading.Thread | None = None
        self._started_at = ""

    def status(self) -> dict[str, object]:
        with self._lock:
            enabled = bool(self._server and self._thread and self._thread.is_alive())
            if not enabled:
                return {
                    "enabled": False,
                    "mode": "quarantined-pull-requests",
                    "bindHost": "0.0.0.0",
                    "port": None,
                    "addresses": discover_lan_addresses(),
                    "baseUrls": [],
                    "publicBaseUrl": "",
                    "startedAt": "",
                    "ownerWorkspace": getattr(self.application, "owner_url", "http://127.0.0.1:8765"),
                }

            bind_host = str(self._server.server_address[0])
            port = int(self._server.server_address[1])
            addresses = discover_lan_addresses() if bind_host in {"0.0.0.0", "::"} else [bind_host]
            base_urls = [f"http://{address}:{port}" for address in addresses]
            return {
                "enabled": True,
                "mode": "quarantined-pull-requests",
                "bindHost": bind_host,
                "port": port,
                "addresses": addresses,
                "baseUrls": base_urls,
                "publicBaseUrl": base_urls[0] if base_urls else "",
                "startedAt": self._started_at,
                "ownerWorkspace": getattr(self.application, "owner_url", "http://127.0.0.1:8765"),
            }

    def start(self, *, port: int = 8766, host: str = "0.0.0.0") -> dict[str, object]:
        try:
            requested_port = int(port)
        except (TypeError, ValueError) as exc:
            raise ForgeTraceError("Sharing port must be a whole number.", code="invalid_sharing_port") from exc
        if requested_port < 0 or requested_port > 65535:
            raise ForgeTraceError("Sharing port must be between 0 and 65535; 0 selects an available port.", code="invalid_sharing_port")

        with self._lock:
            current = self.status()
            if current["enabled"]:
                if requested_port in {0, int(current["port"])}:
                    return current
                raise ForgeTraceError(
                    "Secure sharing is already running. Stop it before changing ports.",
                    409,
                    "sharing_already_enabled",
                    {"port": current["port"]},
                )
            self.application.audit(
                required=True,
                category="sharing",
                action="gateway_start_authorized",
                outcome="authorized",
                surface="owner",
                details={"bindHost": host, "requestedPort": requested_port},
            )
            try:
                server = create_server(self.application, host, requested_port, surface="gateway")
            except OSError as exc:
                self.application.audit(
                    category="sharing",
                    action="gateway_start",
                    outcome="failure",
                    severity="error",
                    surface="owner",
                    details={"bindHost": host, "requestedPort": requested_port, "errorType": type(exc).__name__},
                )
                raise ForgeTraceError(
                    f"Could not start secure sharing on port {requested_port}: {exc}",
                    409,
                    "sharing_bind_failed",
                    {"port": requested_port},
                ) from exc
            thread = threading.Thread(
                target=server.serve_forever,
                name="ForgeTraceContributionGateway",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            self._started_at = utc_now()
            thread.start()
            status = self.status()
            self.application.audit(
                category="sharing",
                action="gateway_started",
                outcome="success",
                surface="owner",
                subject_id=str(status.get("port") or ""),
                details={"bindHost": status.get("bindHost"), "port": status.get("port")},
            )
            return status

    def stop(self) -> dict[str, object]:
        previous = self.status()
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._started_at = ""
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        status = self.status()
        if previous.get("enabled"):
            self.application.audit(
                category="sharing",
                action="gateway_stopped",
                outcome="success",
                surface="owner",
                subject_id=str(previous.get("port") or ""),
                details={"port": previous.get("port")},
            )
        return status


def run(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    data_dir: Path | None = None,
    workspace: Path | None = None,
    verbose: bool = False,
    surface: str = "owner",
    open_browser: bool = False,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    configure_logging(verbose)
    resolved_data_dir = platform_data_dir(data_dir)
    instance_lock = FileLock(resolved_data_dir / "owner.instance.lock", timeout=0.25)
    app = None
    server = None
    try:
        try:
            instance_lock.acquire()
        except LockUnavailable as exc:
            raise SystemExit(
                "Another ForgeTrace owner process is already using this application-data directory. "
                "Close the older ForgeTrace window before launching this package."
            ) from exc
        app = build_application(project_root, resolved_data_dir)
        app.owner_instance_lock_path = instance_lock.path
        app.owner_instance_lock_held = True
        app.owner_url = f"http://{host}:{port}"
        if workspace is not None:
            resolved_workspace = workspace.expanduser().resolve()
            try:
                record = app.registry.register_repository(
                    path=str(resolved_workspace),
                    name=resolved_workspace.name,
                    initialize=True,
                    create_directory=True,
                )
                app.registry.set_active(record["id"])
            except Exception as exc:
                records = app.registry.list_repositories()["repositories"]
                match = next((record for record in records if Path(record["path"]) == resolved_workspace), None)
                if match:
                    app.registry.set_active(match["id"])
                else:
                    raise exc
        try:
            server = create_server(app, host, port, surface=surface)
        except OSError as exc:
            if getattr(exc, "errno", None) in {48, 98, 10048}:
                raise SystemExit(
                    f"ForgeTrace could not start because port {port} is already in use. "
                    "Close the older ForgeTrace window/server, then launch this package again."
                ) from exc
            raise
        if surface == "combined":
            lan_address = discover_lan_address()
            print(f"ForgeTrace {APP_VERSION} legacy combined sharing mode")
            print(f"Owner workspace: http://127.0.0.1:{port}")
            print(f"Contributor portal: http://{lan_address}:{port}/contribute.html#<invite-token>")
            print("Remote clients are blocked from repository and owner APIs.")
            print("This mode is retained for compatibility; the normal UI can now enable sharing itself.")
        else:
            print(f"ForgeTrace {APP_VERSION} running at http://{host}:{port}")
            print("Open Collaborate in the UI to enable or stop secure sharing and generate links.")
        print(f"Registry: {app.registry.db_path}")
        if open_browser and surface == "owner":
            url = f"http://{host}:{port}"
            timer = threading.Timer(0.35, lambda: webbrowser.open(url, new=2))
            timer.daemon = True
            timer.start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping ForgeTrace.")
    finally:
        if app is not None:
            app.owner_instance_lock_held = False
        if app is not None and app.gateway:
            app.gateway.stop()
        if server is not None:
            server.server_close()
        instance_lock.release()


def _registry(data_dir: Path | None) -> RepositoryRegistry:
    project_root = Path(__file__).resolve().parents[1]
    return RepositoryRegistry(project_root, platform_data_dir(data_dir))


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _serve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the ForgeTrace local repository server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=None, help="Override ForgeTrace application-data directory")
    parser.add_argument("--workspace", type=Path, default=None, help="Register and activate a repository path")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--open-browser", action="store_true", help="Open the owner UI after the server binds successfully")
    return parser


def _share_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="server.py share",
        description="Expose only ForgeTrace's quarantined contributor portal to the local network.",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    return parser


def _doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="server.py doctor", description="Inspect and optionally repair the ForgeTrace registry.")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--scan-root", type=Path, action="append", default=[], help="Search a folder for embedded .forgetrace repositories")
    parser.add_argument("--repair", action="store_true", help="Apply safe repairs after creating a registry backup")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args(argv)
    result = _registry(args.data_dir).doctor(repair=args.repair, scan_roots=args.scan_root)
    if args.json:
        _print_json(result)
    else:
        summary = result["summary"]
        state = "HEALTHY" if result["healthy"] else "ATTENTION REQUIRED"
        print(f"ForgeTrace doctor: {state}")
        print(f"SQLite integrity: {result['integrity']}")
        print(f"Repositories: {result['repositoryCount']}")
        print(
            f"Issues: {summary['total']} "
            f"({summary['critical']} critical, {summary['errors']} errors, {summary['warnings']} warnings)"
        )
        for issue in result["issues"]:
            location = issue.get("path") or issue.get("repositoryId") or "registry"
            print(f"- [{issue['severity'].upper()}] {issue['code']}: {location}")
            if issue.get("message"):
                print(f"  {issue['message']}")
        for action in result["actions"]:
            print(f"+ repaired: {action['action']} ({action.get('repositoryId', 'registry')})")
        if result.get("backup"):
            print(f"Backup: {result['backup']['path']}")
    return 0 if result["healthy"] else 2


def _registry_export(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="server.py registry-export", description="Export the ForgeTrace registry as portable JSON.")
    parser.add_argument("output", type=Path)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    payload = _registry(args.data_dir).export_registry()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Registry export written to {output}")
    return 0


def _registry_import(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="server.py registry-import", description="Merge a ForgeTrace registry export into this installation.")
    parser.add_argument("input", type=Path)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--update-paths", action="store_true", help="Replace paths for matching repository UUIDs")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.input.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"Could not read registry export: {exc}")
    result = _registry(args.data_dir).import_registry(payload, update_paths=args.update_paths)
    _print_json(result)
    return 0


def _backup(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="server.py backup", description="Create an online SQLite backup of the ForgeTrace registry.")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--label", default="manual")
    args = parser.parse_args(argv)
    _print_json(_registry(args.data_dir).create_backup(args.label))
    return 0


def main() -> None:
    argv = sys.argv[1:]
    commands = {"serve", "share", "doctor", "registry-export", "registry-import", "backup"}
    command = argv[0] if argv and argv[0] in commands else "serve"
    command_args = argv[1:] if argv and argv[0] in commands else argv
    if command == "doctor":
        raise SystemExit(_doctor(command_args))
    if command == "registry-export":
        raise SystemExit(_registry_export(command_args))
    if command == "registry-import":
        raise SystemExit(_registry_import(command_args))
    if command == "backup":
        raise SystemExit(_backup(command_args))
    if command == "share":
        args = _share_parser().parse_args(command_args)
        run(
            "0.0.0.0",
            args.port,
            data_dir=args.data_dir,
            workspace=args.workspace,
            verbose=args.verbose,
            surface="combined",
        )
        return
    args = _serve_parser().parse_args(command_args)
    run(
        args.host,
        args.port,
        data_dir=args.data_dir,
        workspace=args.workspace,
        verbose=args.verbose,
        open_browser=args.open_browser,
    )


if __name__ == "__main__":
    main()
