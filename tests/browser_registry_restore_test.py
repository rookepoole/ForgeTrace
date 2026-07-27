from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from forgetrace.app import build_application
from forgetrace.web import ForgeTraceHTTPServer, make_handler


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not chromium:
        raise SystemExit("Chromium is required for registry-restore browser testing")

    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v042-restore-browser-"))
    app = build_application(ROOT, temp / "data")
    first = app.registry.register_repository(
        path=str(temp / "first"),
        name="First Browser Repository",
        author="Rooke Poole",
        initialize=True,
        create_directory=True,
    )
    backup = app.registry.create_backup("browser-restore")
    second = app.registry.register_repository(
        path=str(temp / "second"),
        name="Second Browser Repository",
        author="Rooke Poole",
        initialize=True,
        create_directory=True,
    )

    handler = make_handler(app)
    # Managed Chromium blocks direct localhost navigation. Load the exact owner HTML
    # through set_content and bridge fetches to the real loopback service.
    handler.enforce_owner_request_origin = lambda self, path: self.require_local_owner()
    server = ForgeTraceHTTPServer(("127.0.0.1", 0), handler)
    server.forgetrace_surface = "owner"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=chromium,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-web-security"],
            )
            context = browser.new_context(viewport={"width": 1440, "height": 1100})
            page = context.new_page()

            def rewrite_owner_request(route):
                headers = dict(route.request.headers)
                headers.pop("sec-fetch-site", None)
                headers.pop("origin", None)
                route.continue_(headers=headers)

            page.route("**/*", rewrite_owner_request)
            page.on("dialog", lambda dialog: dialog.accept())
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            html = (ROOT / "index.html").read_text(encoding="utf-8")
            bridge = (
                "<script>(()=>{const realFetch=window.fetch.bind(window);"
                f"window.fetch=(input,init)=>{{const raw=typeof input==='string'?input:input.url;"
                f"return realFetch(new URL(raw,'{base}/').href,init);}};}})();</script>"
            )
            html = html.replace("<head>", f'<head><base href="{base}/">', 1).replace(
                "<script>", bridge + "<script>", 1
            )
            page.set_content(html, wait_until="networkidle")

            page.click("#registryToolsBtn")
            page.wait_for_function(
                "document.querySelector('#registryBackupSelect')?.textContent.includes('browser-restore')"
            )
            page.select_option("#registryBackupSelect", backup["name"])
            page.select_option("#registryRestoreMode", "replace")
            page.click("#previewRegistryRestoreBtn")
            page.wait_for_function(
                "document.querySelector('#registryRestorePreview')?.textContent.includes('VALIDATED REPLACE PREVIEW') "
                "&& document.querySelector('#registryRestorePreview')?.textContent.includes('2 → 1')"
            )
            assert page.locator("#executeRegistryRestoreBtn").is_enabled()
            page.click("#executeRegistryRestoreBtn")
            page.wait_for_function(
                "document.querySelector('#registryRestorePreview')?.textContent.includes('completed and verified')"
            )
            assert {item["id"] for item in app.registry.list_repositories()["repositories"]} == {first["id"]}
            page.wait_for_function(
                "document.querySelector('#registryRestoreHistory [data-registry-rollback]') !== null"
            )

            page.click("#registryRestoreHistory [data-registry-rollback]")
            page.wait_for_function(
                "document.querySelector('#registryRestorePreview')?.textContent.includes('rolled back and verified')"
            )
            assert {item["id"] for item in app.registry.list_repositories()["repositories"]} == {
                first["id"],
                second["id"],
            }
            page.wait_for_function(
                "document.querySelector('#registryRestoreHistory')?.textContent.includes('rolled_back')"
            )
            events = app.security_events.query(category="recovery", limit=100)["events"]
            actions = {event["action"] for event in events}
            assert "registry_restore_previewed" in actions
            assert "registry_restore" in actions
            assert "registry_restore_rollback" in actions
            assert not errors, errors
            browser.close()
        print("ForgeTrace validated registry restore owner browser workflow: PASS")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
