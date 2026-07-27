from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from forgetrace.app import build_application
from forgetrace.web import ForgeTraceHTTPServer, make_handler


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not chromium:
        raise SystemExit("Chromium is required for read-only repository browser testing")

    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v043-read-only-browser-"))
    app = build_application(ROOT, temp / "data")
    record = app.registry.register_repository(
        path=str(temp / "repository"), name="Read-only Browser Repository",
        author="Rooke Poole", initialize=True, create_directory=True,
    )
    service = app.registry.repository_service(record["id"])
    service.write_file("alpha.txt", b"alpha before\n", "Rooke Poole", "Initial file")
    service.create_commit("Initial snapshot", "Rooke Poole")

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
                headless=True, executable_path=chromium,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-web-security"],
            )
            context = browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
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
            page.wait_for_function("document.querySelector('#repoTitle')?.textContent.includes('Read-only Browser')")

            page.click("#settingsBtn")
            page.select_option("#settingsAccessMode", "read_only")
            page.click("#applyAccessModeBtn")
            page.wait_for_function(
                "!document.querySelector('#readOnlyBanner').classList.contains('hidden') "
                "&& document.querySelector('#readOnlyBanner').textContent.includes('Service-enforced read-only')"
            )
            assert page.locator("#uploadFilesBtn").is_disabled()
            assert page.locator("#newFileBtn").is_disabled()
            assert page.locator("#snapshotBtn").is_disabled()
            assert page.locator("#settingsSaveBtn").is_disabled()
            assert app.registry.repository_service(record["id"]).access_policy()["effectiveMode"] == "read_only"

            page.click('[data-close-modal="settingsModalBackdrop"]')
            page.click('[data-file-path="alpha.txt"]')
            page.wait_for_function("document.querySelector('#code')?.value.includes('alpha before')")
            assert page.locator("#code").get_attribute("readonly") is not None
            assert page.locator("#saveBtn").is_disabled()
            assert page.locator("#downloadBtn").is_enabled()
            safe = page.evaluate(
                """async ({base,id}) => {
                    const preview=await fetch(`${base}/api/v1/repositories/${id}/export-preview?vcs=0`);
                    const raw=await fetch(`${base}/api/v1/repositories/${id}/raw?path=alpha.txt`);
                    const blocked=await fetch(`${base}/api/v1/repositories/${id}/file`,{
                      method:'PUT',headers:{'Content-Type':'application/json'},
                      body:JSON.stringify({path:'alpha.txt',content:'forbidden',author:'Browser'})
                    });
                    return {preview:preview.status,raw:raw.status,rawText:await raw.text(),blocked:blocked.status,blockedBody:await blocked.json()};
                }""",
                {"base": base, "id": record["id"]},
            )
            assert safe["preview"] == 200
            assert safe["raw"] == 200 and safe["rawText"] == "alpha before\n"
            assert safe["blocked"] == 423 and safe["blockedBody"]["code"] == "repository_read_only"
            assert service.read_file("alpha.txt")["content"] == "alpha before\n"

            page.click("#settingsBtn")
            page.select_option("#settingsAccessMode", "read_write")
            page.click("#applyAccessModeBtn")
            page.wait_for_function("document.querySelector('#readOnlyBanner').classList.contains('hidden')")
            page.click('[data-close-modal="settingsModalBackdrop"]')
            with page.expect_response(
                lambda response: response.request.method == "GET"
                and f"/api/v1/repositories/{record['id']}/file?path=alpha.txt" in response.url
            ) as file_response_info:
                page.click('[data-file-path="alpha.txt"]')
            assert file_response_info.value.status == 200
            page.fill("#code", "alpha after")
            assert page.input_value("#code") == "alpha after"
            with page.expect_response(
                lambda response: response.request.method == "PUT"
                and f"/api/v1/repositories/{record['id']}/file" in response.url
            ) as response_info:
                page.click("#saveBtn")
            assert response_info.value.status == 200
            for _ in range(100):
                if app.registry.repository_service(record["id"]).read_file("alpha.txt")["content"] == "alpha after":
                    break
                time.sleep(0.02)
            assert app.registry.repository_service(record["id"]).read_file("alpha.txt")["content"] == "alpha after"
            assert app.registry.repository_service(record["id"]).access_policy()["writable"]

            events = app.security_events.query(category="repository_access", limit=50)["events"]
            successful = [event for event in events if event["action"] == "repository_access_mode_change" and event["outcome"] == "success"]
            assert {event["details"].get("accessMode") for event in successful} >= {"read_only", "read_write"}
            assert not errors, errors
            browser.close()
        print("ForgeTrace service-enforced read-only owner browser workflow: PASS")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if app.gateway:
            app.gateway.stop()
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
