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
        raise SystemExit("Chromium is required for segmented-security-history browser testing")

    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v047-security-browser-"))
    app = build_application(ROOT, temp / "data")
    app.owner_instance_lock_held = True
    for index in range(14):
        app.security_events.append(
            category="retention",
            action="browser_rotation_fixture",
            outcome="success",
            surface="system",
            subject_id=f"browser-{index}",
            details={"index": index},
        )

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
            context = browser.new_context(viewport={"width": 1600, "height": 1200}, accept_downloads=True)
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

            page.click("#securityEventsBtn")
            page.wait_for_selector("#securityEventsBackdrop.open")
            page.wait_for_function("document.querySelector('#securityEventIntegrity')?.textContent.includes('Chain verified')")
            page.fill("#securityRotateCount", "6")
            page.click("#securityRotationPreviewBtn")
            page.wait_for_function(
                "document.querySelector('#securityRotationPreview')?.textContent.includes('Seal #1–#6')"
            )
            assert page.locator("#securityRotationExecuteBtn").is_enabled()
            page.click("#securityRotationExecuteBtn")
            page.wait_for_function(
                "document.querySelectorAll('#securitySegmentList [data-security-segment]').length === 1"
                " && document.querySelector('#securityRotationPreview')?.textContent.includes('completed')",
                timeout=30000,
            )
            segment_text = page.locator("#securitySegmentList").inner_text()
            assert "#1–#6" in segment_text
            assert "no recorded external receipt" in segment_text

            with page.expect_download(timeout=20000) as download_info:
                page.click("#securityAnchorExportBtn")
            download = download_info.value
            request_payload = json.loads(Path(download.path()).read_text(encoding="utf-8"))
            assert download.suggested_filename == f"forgetrace-{request_payload['anchorId']}.json"
            assert request_payload["format"] == "forgetrace-security-anchor-request"
            assert request_payload["externalPublicationVerified"] is False
            assert request_payload["segmentHashes"]
            page.wait_for_function("!document.querySelector('#securityAnchorReceiptBtn').disabled")

            page.fill("#securityAnchorMechanism", "browser-test-signed-file")
            page.fill("#securityAnchorReference", "browser-receipt-001")
            page.fill("#securityAnchorEvidence", "owner supplied external receipt evidence")
            page.click("#securityAnchorReceiptBtn")
            page.wait_for_function(
                "document.querySelector('#securityAnchorStatus')?.textContent.includes('Receipt recorded')"
                " && document.querySelector('#securitySegmentList')?.textContent.includes('receipt recorded')",
                timeout=30000,
            )

            anchors = app.security_events.list_anchors()
            assert anchors["unanchoredSegmentCount"] == 0
            assert anchors["anchors"][0]["externalPublicationVerified"] is False
            integrity = app.security_events.verify_integrity()
            assert integrity["healthy"] is True
            assert integrity["segmentCount"] == 1
            events = app.security_events.query(limit=250)["events"]
            actions = {event["action"] for event in events}
            assert {
                "security_rotation_authorized",
                "security_rotation_completed",
                "security_anchor_export_authorized",
                "security_anchor_request_created",
                "security_anchor_receipt_authorized",
                "security_anchor_receipt_recorded",
            }.issubset(actions)
            assert not errors, errors
            browser.close()
        print("ForgeTrace segmented security retention owner browser workflow: PASS")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
