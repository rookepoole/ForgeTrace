from __future__ import annotations

import json
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
        raise SystemExit("Chromium is required for security-event browser testing")

    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v041-security-browser-"))
    app = build_application(ROOT, temp / "data")
    handler = make_handler(app)
    # This managed Chromium environment blocks direct localhost navigation. Load the
    # exact owner HTML through set_content and rewrite fetches to the real loopback
    # server while retaining the owner-local Host/client boundary.
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
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
            page = context.new_page()

            def rewrite_owner_request(route):
                headers = dict(route.request.headers)
                headers.pop("sec-fetch-site", None)
                headers.pop("origin", None)
                route.continue_(headers=headers)

            page.route("**/*", rewrite_owner_request)
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            html = (ROOT / "index.html").read_text(encoding="utf-8")
            bridge = (
                "<script>(()=>{const realFetch=window.fetch.bind(window);"
                f"window.fetch=(input,init)=>{{const raw=typeof input==='string'?input:input.url;"
                f"return realFetch(new URL(raw,'{base}/').href,init);}};}})();</script>"
            )
            html = html.replace("<head>", f'<head><base href="{base}/">', 1).replace("<script>", bridge + "<script>", 1)
            page.set_content(html, wait_until="networkidle")

            # Generate a security event through a real owner browser workflow.
            page.click("#registryToolsBtn")
            page.click("#doctorCheckBtn")
            page.wait_for_function("document.querySelector('#doctorOutput')?.textContent.includes('HEALTHY')")
            page.click('[data-close-modal="registryToolsBackdrop"]')

            # Open, verify, filter, and inspect the append-only ledger.
            page.click("#securityEventsBtn")
            page.wait_for_function("document.querySelector('#securityEventIntegrity')?.textContent.includes('Chain verified')")
            page.select_option("#securityEventCategory", "recovery")
            page.click("#securityEventRefreshBtn")
            page.wait_for_function(
                "document.querySelector('#securityEventList')?.textContent.includes('doctor_check') "
                "&& document.querySelector('#securityEventSummary')?.textContent.includes('matching event')"
            )
            assert page.locator('[data-security-sequence]').count() >= 1
            assert "doctor_check" in page.locator("#securityEventList").inner_text()

            # Export the currently filtered evidence through the real download path.
            with page.expect_download(timeout=15000) as download_info:
                page.click("#securityEventExportBtn")
            download = download_info.value
            download_path = download.path()
            assert download.suggested_filename == "forgetrace-security-events.json"
            payload = json.loads(Path(download_path).read_text(encoding="utf-8"))
            assert payload["format"] == "ForgeTrace Security Event Export"
            assert payload["integrity"]["healthy"] is True
            assert payload["eventCount"] >= 1
            assert all(event["category"] == "recovery" for event in payload["events"])
            assert any(event["action"] == "doctor_check" for event in payload["events"])
            assert not errors, errors

            browser.close()
        print("ForgeTrace security event owner browser workflow: PASS")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
