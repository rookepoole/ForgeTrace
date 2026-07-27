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
        raise SystemExit("Chromium is required for health-dashboard browser testing")

    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v046-health-browser-"))
    app = build_application(ROOT, temp / "data")
    app.owner_instance_lock_held = True
    record = app.registry.register_repository(
        path=str(temp / "repository"),
        name="Health Browser Registry Name",
        description="v0.4.6 browser fixture",
        author="Rooke Poole",
        initialize=True,
        create_directory=True,
    )
    repository = app.registry.repository_service(record["id"])
    repository.write_file("health.txt", b"health browser\n", "Rooke Poole", "seed")
    repository.create_commit("health baseline", "Rooke Poole")

    # Create a real, safely repairable Doctor finding before the browser opens.
    state = json.loads(repository.state_path.read_text(encoding="utf-8"))
    state["repository"]["name"] = "Health Browser Embedded Name"
    repository.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

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
            context = browser.new_context(viewport={"width": 1600, "height": 1100}, accept_downloads=True)
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

            page.click("#healthDashboardBtn")
            page.wait_for_selector("#healthDashboardBackdrop.open")
            page.select_option("#healthRepository", record["id"])
            page.click("#healthGenerateBtn")
            page.wait_for_function(
                "document.querySelector('#healthFindings')?.textContent.includes('registry_metadata_drift')"
            )
            page.wait_for_function(
                "document.querySelector('#healthSummary')?.textContent.includes('Findings')"
            )
            assert page.locator('[data-health-section="registry"]').count() == 1
            page.click('[data-health-section="registry"] summary')
            assert "sqliteIntegrity" in page.locator('[data-health-section="registry"] .health-section-body').inner_text()
            assert page.locator("#healthDoctorRepairBtn").is_enabled()

            with page.expect_download(timeout=15000) as download_info:
                page.click("#healthExportBtn")
            download = download_info.value
            payload = json.loads(Path(download.path()).read_text(encoding="utf-8"))
            assert payload["format"] == "forgetrace-health-report-export"
            assert payload["report"]["requestId"].startswith("req_")
            assert payload["report"]["reportHash"]
            assert any(
                finding["code"] == "registry_metadata_drift"
                for section in payload["report"]["sections"].values()
                for finding in section.get("findings", [])
            )

            page.click("#healthDoctorRepairBtn")
            page.wait_for_function(
                "!document.querySelector('#healthFindings')?.textContent.includes('registry_metadata_drift')"
                " && document.querySelectorAll('#healthHistory [data-health-report-id]').length >= 2",
                timeout=30000,
            )
            assert app.registry.get_repository(record["id"])["name"] == "Health Browser Embedded Name"
            report_files = list((app.registry.data_dir / "health-reports").glob("health_*.json"))
            assert len(report_files) >= 2
            events = app.security_events.query(limit=200)["events"]
            actions = {event["action"] for event in events}
            assert "health_report_generated" in actions
            assert "health_report_exported" in actions
            assert "doctor_repair_authorized" in actions
            assert "doctor_repair" in actions
            assert not errors, errors
            browser.close()
        print("ForgeTrace unified health dashboard owner browser workflow: PASS")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
