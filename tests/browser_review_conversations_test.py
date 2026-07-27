from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from forgetrace.app import build_application
from forgetrace.web import ForgeTraceHTTPServer, create_server, make_handler

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not chromium:
        raise SystemExit("Chromium is required for inline review conversation browser testing")

    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v044-review-browser-"))
    app = build_application(ROOT, temp / "data")
    record = app.registry.register_repository(
        path=str(temp / "repository"),
        name="Review Conversation Browser",
        description="Owner and contributor review workflow",
        author="Rooke Poole",
        initialize=True,
        create_directory=True,
    )
    repository_id = record["id"]
    service = app.registry.repository_service(repository_id)
    service.write_file(
        "src/app.txt",
        b"alpha baseline\nbeta baseline\ngamma baseline\n",
        "Rooke Poole",
        "Seed review browser fixture",
    )
    invite = app.collaboration.create_invite(
        repository_id,
        label="Review browser contributor",
        max_uses=2,
        max_file_bytes=1024 * 1024,
        max_total_bytes=4 * 1024 * 1024,
    )
    token = invite["token"]

    owner_handler = make_handler(app)
    owner_handler.enforce_owner_request_origin = lambda self, path: self.require_local_owner()
    owner_server = ForgeTraceHTTPServer(("127.0.0.1", 0), owner_handler)
    owner_server.forgetrace_surface = "owner"
    gateway_server = create_server(app, "127.0.0.1", 0, surface="gateway")
    owner_thread = threading.Thread(target=owner_server.serve_forever, daemon=True)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    owner_thread.start()
    gateway_thread.start()
    owner_base = f"http://127.0.0.1:{owner_server.server_address[1]}"
    gateway_base = f"http://127.0.0.1:{gateway_server.server_address[1]}"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=chromium,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-web-security"],
            )
            context = browser.new_context(viewport={"width": 1500, "height": 1200}, accept_downloads=True)
            contributor = context.new_page()
            owner = context.new_page()
            errors: list[str] = []
            contributor.on("pageerror", lambda error: errors.append(f"contributor: {error}"))
            owner.on("pageerror", lambda error: errors.append(f"owner: {error}"))
            contributor.on("dialog", lambda dialog: dialog.accept())

            def owner_dialog(dialog):
                message = dialog.message
                if "MERGE #1" in message:
                    dialog.accept("MERGE #1")
                elif "Approval note" in message:
                    dialog.accept("Revision two approved in browser")
                else:
                    dialog.accept()

            owner.on("dialog", owner_dialog)

            contributor.set_default_timeout(15000)
            owner.set_default_timeout(15000)
            contributor_html = (ROOT / "contribute.html").read_text(encoding="utf-8")
            contributor_html = contributor_html.replace(
                "let token=(location.hash||'').slice(1).trim();\n    if(token){sessionStorage.setItem('forgetrace-invite-token',token);history.replaceState(null,'',location.pathname+location.search)}\n    else token=sessionStorage.getItem('forgetrace-invite-token')||'';",
                f"let token={json.dumps(token)};",
                1,
            )
            contributor_html = contributor_html.replace(
                "async function savePrId(id){sessionStorage.setItem(await storageKey(),id)}\n    async function savedPrId(){return sessionStorage.getItem(await storageKey())||''}",
                "let browserPrId=''; async function savePrId(id){browserPrId=id}\n    async function savedPrId(){return browserPrId}",
                1,
            ).replace("sessionStorage.removeItem(await storageKey())", "browserPrId='' ")
            contributor_bridge = (
                "<script>(()=>{const realFetch=window.fetch.bind(window);"
                f"window.fetch=(input,init)=>{{const raw=typeof input==='string'?input:input.url;"
                f"return realFetch(new URL(raw,'{gateway_base}/').href,init);}};}})();</script>"
            )
            contributor_html = contributor_html.replace("<head>", f'<head><base href="{gateway_base}/">', 1).replace(
                "<script>", contributor_bridge + "<script>", 1
            )
            contributor.set_content(contributor_html, wait_until="domcontentloaded")
            contributor.wait_for_function(
                "document.querySelector('#repoCard')?.textContent.includes('Review Conversation Browser')"
            )
            contributor.wait_for_function("!document.querySelector('#createPanel').classList.contains('hidden') || !document.querySelector('#prPanel').classList.contains('hidden') || document.querySelector('#fatalNotice').textContent")
            contributor.fill("#authorName", "Outside Browser")
            contributor.fill("#title", "Improve review workflow")
            contributor.fill("#description", "Exercise revision-bound owner and contributor conversations")
            contributor.click("#createForm button[type=submit]")
            contributor.wait_for_function("!document.querySelector('#prPanel').classList.contains('hidden')")
            contributor.evaluate(
                """async()=>{
                    const blob=new Blob(['alpha revision one\\nbeta revision one\\ngamma revision one\\n'],{type:'text/plain'});
                    state.pr=await api(`/api/v1/collaboration/pull-requests/${encodeURIComponent(state.pr.id)}/files?path=${encodeURIComponent('src/app.txt')}`,{method:'POST',body:blob});
                    renderPr();
                }"""
            )
            contributor.wait_for_function("document.querySelector('#changeList')?.textContent.includes('src/app.txt')")
            contributor.click("#submitPr")
            contributor.wait_for_function("document.querySelector('#prStatus')?.textContent.trim()==='open'")
            first_submitted_revision = int(contributor.evaluate("state.pr.revision"))
            contributor.wait_for_function("!document.querySelector('#reviewCreate').classList.contains('hidden')")
            contributor.select_option("#reviewPath", "src/app.txt")
            contributor.fill("#reviewStartLine", "2")
            contributor.fill("#reviewEndLine", "2")
            contributor.fill("#reviewBody", "<img src=x onerror=window.__reviewXss=1> Why retain beta semantics?")
            contributor.click("#createReviewThread")
            contributor.wait_for_function("document.querySelector('#reviewThreads')?.textContent.includes('Why retain beta semantics?')")
            assert contributor.locator("#reviewThreads img").count() == 0
            assert contributor.evaluate("window.__reviewXss||0") == 0

            def rewrite_owner_request(route):
                headers = dict(route.request.headers)
                headers.pop("sec-fetch-site", None)
                headers.pop("origin", None)
                route.continue_(headers=headers)

            owner.route("**/*", rewrite_owner_request)
            owner_html = (ROOT / "index.html").read_text(encoding="utf-8")
            owner_bridge = (
                "<script>(()=>{const realFetch=window.fetch.bind(window);"
                f"window.fetch=(input,init)=>{{const raw=typeof input==='string'?input:input.url;"
                f"return realFetch(new URL(raw,'{owner_base}/').href,init);}};}})();</script>"
            )
            owner_html = owner_html.replace("<head>", f'<head><base href="{owner_base}/">', 1).replace(
                "<script>", owner_bridge + "<script>", 1
            )
            owner.set_content(owner_html, wait_until="domcontentloaded")
            owner.wait_for_function("document.querySelector('#repoTitle')?.textContent.includes('Review Conversation Browser')")
            owner.click('[data-tab="pullrequests"]')
            owner.wait_for_selector("[data-pr-id]")
            owner.click("[data-pr-id]")
            owner.wait_for_function("document.querySelector('#pullRequestDetail')?.textContent.includes('Why retain beta semantics?')")
            assert owner.locator("#pullRequestDetail img").count() == 0
            assert owner.locator(".review-thread-context").first.text_content().strip().endswith("beta revision one")

            first_thread = owner.locator(".review-thread").first
            first_thread.locator(".owner-review-reply-body").fill("Keep beta behavior, then add the new branch.")
            with owner.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/comments")) as owner_reply_response:
                first_thread.locator("[data-owner-review-reply]").click()
            assert owner_reply_response.value.status == 201, owner_reply_response.value.text()
            owner.wait_for_function("document.querySelector('#pullRequestDetail')?.textContent.includes('Keep beta behavior')")
            owner.locator(".review-thread").first.locator('[data-owner-review-state="resolve"]').click()
            owner.wait_for_function("document.querySelector('.review-thread .review-thread-badge')?.textContent.trim()==='resolved'")

            owner.select_option("#ownerReviewPath", "src/app.txt")
            owner.fill("#ownerReviewStartLine", "3")
            owner.fill("#ownerReviewEndLine", "3")
            owner.fill("#ownerReviewBody", "Replace gamma with the verified revision-two behavior.")
            owner.check("#ownerReviewRequestChanges")
            with owner.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/review-threads")) as owner_create_response:
                owner.click("[data-owner-review-create]")
            assert owner_create_response.value.status == 201, owner_create_response.value.text()
            owner.wait_for_function("document.querySelector('#pullRequestDetail .pr-status')?.textContent.trim()==='changes requested'")
            owner.wait_for_function("document.querySelector('#pullRequestDetail')?.textContent.includes('Replace gamma with the verified revision-two behavior.')")

            contributor.click("#refreshPr")
            contributor.wait_for_function("document.querySelector('#prStatus')?.textContent.trim()==='changes requested'")
            contributor.wait_for_function("document.querySelector('#reviewThreads')?.textContent.includes('Replace gamma with the verified revision-two behavior.')")
            request_thread = contributor.locator(".review-thread", has_text="Replace gamma with the verified revision-two behavior.")
            request_thread.locator(".review-reply-body").fill("Addressed in revision two; gamma now has the requested behavior.")
            request_thread.locator(".review-reply-btn").click()
            contributor.wait_for_function("document.querySelector('#reviewThreads')?.textContent.includes('Addressed in revision two')")

            contributor.evaluate(
                """async()=>{
                    const blob=new Blob(['alpha revision two\\nbeta revision one\\ngamma verified revision two\\n'],{type:'text/plain'});
                    state.pr=await api(`/api/v1/collaboration/pull-requests/${encodeURIComponent(state.pr.id)}/files?path=${encodeURIComponent('src/app.txt')}`,{method:'POST',body:blob});
                    renderPr();
                }"""
            )
            contributor.click("#submitPr")
            contributor.wait_for_function(
                "previous => document.querySelector('#prStatus')?.textContent.trim()==='open' && Number(state.pr?.revision||0)>previous",
                arg=first_submitted_revision,
            )
            second_submitted_revision = int(contributor.evaluate("state.pr.revision"))

            owner.click("#refreshPrBtn")
            owner.wait_for_function("document.querySelector('[data-pr-id]')?.textContent.includes('Improve review workflow')")
            owner.click("[data-pr-id]")
            owner.wait_for_function(
                "revision => document.querySelector('#pullRequestDetail')?.textContent.includes(`revision ${revision}`)",
                arg=second_submitted_revision,
            )
            owner.wait_for_function("document.querySelector('#pullRequestDetail')?.textContent.includes('outdated revision')")
            owner.click('[data-pr-review="approved"]')
            owner.wait_for_function("document.querySelector('#pullRequestDetail .pr-status')?.textContent.trim()==='approved'")
            owner.click("[data-pr-merge]")
            expected = "alpha revision two\nbeta revision one\ngamma verified revision two\n"
            for _ in range(200):
                if (temp / "repository" / "src" / "app.txt").read_text(encoding="utf-8") == expected:
                    break
                time.sleep(0.02)
            assert (temp / "repository" / "src" / "app.txt").read_text(encoding="utf-8") == expected
            owner.wait_for_function("document.querySelector('#pullRequestDetail')?.textContent.includes('merged')")

            events = app.security_events.query(repository_id=repository_id, limit=200)["events"]
            actions = {event["action"] for event in events}
            assert {
                "review_thread_created",
                "review_thread_commented",
                "review_thread_resolved",
                "review_changes_requested_authorized",
            }.issubset(actions)
            assert not errors, errors
            browser.close()
        print("ForgeTrace inline owner/contributor review browser workflow: PASS")
    finally:
        owner_server.shutdown()
        gateway_server.shutdown()
        owner_server.server_close()
        gateway_server.server_close()
        owner_thread.join(timeout=5)
        gateway_thread.join(timeout=5)
        if app.gateway:
            app.gateway.stop()
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
