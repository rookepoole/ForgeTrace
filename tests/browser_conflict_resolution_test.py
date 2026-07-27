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
        raise SystemExit("Chromium is required for conflict-resolution browser testing")

    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v045-conflict-browser-"))
    app = build_application(ROOT, temp / "data")
    record = app.registry.register_repository(
        path=str(temp / "repository"),
        name="Conflict Resolution Browser",
        description="Quarantine-only visual resolver workflow",
        author="Rooke Poole",
        initialize=True,
        create_directory=True,
    )
    repository_id = record["id"]
    service = app.registry.repository_service(repository_id)
    service.write_file(
        "src/app.txt",
        b"base line\nshared line\n",
        "Rooke Poole",
        "Seed conflict browser fixture",
    )
    invite = app.collaboration.create_invite(repository_id, label="Conflict browser", max_uses=2)
    token = invite["token"]
    pr = app.collaboration.create_pull_request(
        token,
        title="Resolve browser conflict",
        description="Exercise immutable three-way evidence and merge-time revalidation",
        author_name="Outside Browser",
    )
    app.collaboration.upload_pull_request_file(
        token, pr["id"], "src/app.txt", b"submitted line\nshared line\n"
    )
    submitted = app.collaboration.submit_pull_request(token, pr["id"])
    service.write_file(
        "src/app.txt",
        b"current line\nshared line\n",
        "Rooke Poole",
        "Create conflict for browser fixture",
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
            context = browser.new_context(viewport={"width": 1600, "height": 1300})
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            merge_attempts = 0

            def dialog_handler(dialog):
                nonlocal merge_attempts
                if "Type MERGE #1" in dialog.message:
                    merge_attempts += 1
                    dialog.accept("MERGE #1")
                elif "Approval note" in dialog.message:
                    dialog.accept("Conflict evidence verified in browser")
                else:
                    dialog.accept()

            page.on("dialog", dialog_handler)
            page.set_default_timeout(20000)

            def rewrite_owner_request(route):
                headers = dict(route.request.headers)
                headers.pop("sec-fetch-site", None)
                headers.pop("origin", None)
                route.continue_(headers=headers)

            page.route("**/*", rewrite_owner_request)
            owner_html = (ROOT / "index.html").read_text(encoding="utf-8")
            bridge = (
                "<script>(()=>{const realFetch=window.fetch.bind(window);"
                f"window.fetch=(input,init)=>{{const raw=typeof input==='string'?input:input.url;"
                f"return realFetch(new URL(raw,'{base}/').href,init);}};}})();</script>"
            )
            owner_html = owner_html.replace("<head>", f'<head><base href="{base}/">', 1).replace(
                "<script>", bridge + "<script>", 1
            )
            page.set_content(owner_html, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelector('#repoTitle')?.textContent.includes('Conflict Resolution Browser')"
            )
            page.click('[data-tab="pullrequests"]')
            page.wait_for_selector("[data-pr-id]")
            page.click("[data-pr-id]")
            page.wait_for_function(
                "document.querySelector('#pullRequestDetail')?.textContent.includes('Visual conflict resolution')"
            )
            page.wait_for_function(
                "document.querySelector('#pullRequestDetail')?.textContent.includes('resolution required')"
            )
            assert page.locator("[data-pr-review='approved']").is_disabled()

            with page.expect_response(
                lambda response: response.request.method == "POST" and response.url.endswith("/conflict-resolutions")
            ) as prepare_response:
                page.click("[data-conflict-prepare]")
            assert prepare_response.value.status == 201, prepare_response.value.text()
            page.wait_for_function(
                "document.querySelector('.conflict-evidence-grid')?.textContent.includes('base line')"
            )
            detail_text = page.locator("#pullRequestDetail").text_content()
            assert "current line" in detail_text
            assert "submitted line" in detail_text
            assert page.locator(".conflict-evidence-pane script").count() == 0

            card = page.locator("[data-conflict-draft-id]").first
            card.locator("[data-conflict-decision]").select_option("manual")
            card.locator(".conflict-manual-text").fill("resolved line\nshared line\n")
            with page.expect_response(
                lambda response: response.request.method == "POST" and response.url.endswith("/decision")
            ) as save_response:
                card.locator("[data-conflict-save]").click()
            assert save_response.value.status == 200, save_response.value.text()
            page.wait_for_function(
                "document.querySelector('[data-conflict-confirm]') && !document.querySelector('[data-conflict-confirm]').disabled"
            )
            with page.expect_response(
                lambda response: response.request.method == "POST" and response.url.endswith("/confirm")
            ) as confirm_response:
                page.click("[data-conflict-confirm]")
            assert confirm_response.value.status == 200, confirm_response.value.text()
            page.wait_for_function(
                "document.querySelector('.conflict-resolution-card .review-thread-badge')?.textContent.trim()==='confirmed'"
            )
            assert not page.locator("[data-pr-review='approved']").is_disabled()

            with page.expect_response(
                lambda response: response.request.method == "POST" and response.url.endswith("/review")
            ) as approval_response:
                page.click("[data-pr-review='approved']")
            assert approval_response.value.status == 200, approval_response.value.text()
            page.wait_for_function(
                "document.querySelector('#pullRequestDetail .pr-status')?.textContent.trim()==='approved'"
            )
            page.wait_for_selector("[data-pr-merge]")

            # Invalidate the confirmed repository digest after approval. The browser still has
            # a visible merge control, so only the backend lock-time check can safely stop it.
            service.write_file(
                "unrelated.txt",
                b"merge-time drift\n",
                "Rooke Poole",
                "Invalidate confirmed conflict evidence",
            )
            before = (temp / "repository" / "src" / "app.txt").read_text(encoding="utf-8")
            with page.expect_response(
                lambda response: response.request.method == "POST" and response.url.endswith("/merge")
            ) as stale_merge_response:
                page.click("[data-pr-merge]")
            assert stale_merge_response.value.status == 409, stale_merge_response.value.text()
            page.wait_for_function(
                "document.querySelector('#pullRequestDetail')?.textContent.includes('Stale draft')"
            )
            assert (temp / "repository" / "src" / "app.txt").read_text(encoding="utf-8") == before

            page.click("[data-conflict-prepare]")
            page.wait_for_function(
                "document.querySelector('[data-conflict-draft-id]')?.querySelector('.review-thread-badge')?.textContent.trim()==='draft'"
            )
            card = page.locator("[data-conflict-draft-id]").first
            card.locator("[data-conflict-decision]").select_option("manual")
            card.locator(".conflict-manual-text").fill("resolved line\nshared line\n")
            card.locator("[data-conflict-save]").click()
            page.wait_for_function(
                "document.querySelector('[data-conflict-confirm]') && !document.querySelector('[data-conflict-confirm]').disabled"
            )
            page.click("[data-conflict-confirm]")
            page.wait_for_function(
                "document.querySelector('.conflict-resolution-card .review-thread-badge')?.textContent.trim()==='confirmed'"
            )
            with page.expect_response(
                lambda response: response.request.method == "POST" and response.url.endswith("/review")
            ) as second_approval_response:
                page.click("[data-pr-review='approved']")
            assert second_approval_response.value.status == 200, second_approval_response.value.text()
            page.wait_for_selector("[data-pr-merge]")
            page.click("[data-pr-merge]")

            expected = "resolved line\nshared line\n"
            for _ in range(250):
                if (temp / "repository" / "src" / "app.txt").read_text(encoding="utf-8") == expected:
                    break
                time.sleep(0.02)
            assert (temp / "repository" / "src" / "app.txt").read_text(encoding="utf-8") == expected
            page.wait_for_function(
                "document.querySelector('#pullRequestDetail .pr-status')?.textContent.trim()==='merged'"
            )
            assert merge_attempts == 2

            events = app.security_events.query(repository_id=repository_id, limit=300)["events"]
            actions = {event["action"] for event in events}
            assert {
                "conflict_resolution_prepared",
                "conflict_resolution_decision_saved",
                "conflict_resolution_confirmed",
                "pull_request_merge_conflict",
                "pull_request_merged",
            }.issubset(actions)
            assert not errors, errors
            browser.close()
        print("ForgeTrace quarantine-only visual conflict resolution browser workflow: PASS")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if app.gateway:
            app.gateway.stop()
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
