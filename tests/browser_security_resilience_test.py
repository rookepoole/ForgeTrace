from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from forgetrace.app import build_application
from forgetrace.security_events import SecurityEventError
from forgetrace.web import ForgeTraceHTTPServer, make_handler

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not chromium:
        raise SystemExit("Chromium is required for security resilience browser testing")

    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v0512-security-browser-"))
    app = build_application(ROOT, temp / "data")
    app.security_events.append(
        category="security",
        action="security_history_degraded_fixture",
        outcome="success",
        surface="owner",
        details={"fixture": True},
    )

    def unavailable_history():
        raise SecurityEventError("simulated auxiliary history failure")

    app.security_events.operational_status = unavailable_history
    handler = make_handler(app)
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
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.route("**/*", lambda route: route.continue_(headers={
                key: value for key, value in route.request.headers.items()
                if key not in {"sec-fetch-site", "origin"}
            }))
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
            page.click("#securityEventsBtn")
            page.wait_for_function(
                "document.querySelector('#securityEventList')?.textContent.includes('security_history_degraded_fixture')"
            )
            page.wait_for_function(
                "document.querySelector('#securityAnchorStatus')?.textContent.includes('unavailable')"
            )
            assert "security_history_degraded_fixture" in page.locator("#securityEventList").inner_text()
            assert "primary event list remains available" in page.locator("#securitySegmentList").inner_text().lower()
            assert not errors, errors
            browser.close()
        print("ForgeTrace security viewer degraded-history workflow: PASS")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
