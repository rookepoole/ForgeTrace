from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from forgetrace.app import build_application
from forgetrace.web import ForgeTraceHTTPServer, create_server, make_handler

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not chromium:
        raise SystemExit("Chromium is required for project coordination browser testing")

    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v049-project-browser-"))
    app = build_application(ROOT, temp / "data")
    record = app.registry.register_repository(
        path=str(temp / "repository"),
        name="Project Coordination Browser",
        description="Owner and invitation-scoped project workflow",
        author="Rooke Poole",
        initialize=True,
        create_directory=True,
    )
    repository_id = record["id"]
    repository_path = Path(record["path"])
    readme_path = repository_path / "README.md"
    state_path = repository_path / ".forgetrace" / "state.json"
    before_readme = sha256(readme_path)
    before_state = sha256(state_path)
    invite = app.collaboration.create_invite(
        repository_id,
        label="Project browser participant",
        max_uses=2,
        allow_project_participation=True,
    )
    token = invite["token"]

    owner_handler = make_handler(app)
    owner_handler.enforce_owner_request_origin = lambda self, path: self.require_local_owner()
    owner_server = ForgeTraceHTTPServer(("127.0.0.1", 0), owner_handler)
    owner_server.forgetrace_surface = "owner"
    gateway_server = create_server(app, "127.0.0.1", 0, surface="gateway")
    owner_thread = threading.Thread(target=owner_server.serve_forever, daemon=True)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    owner_thread.start(); gateway_thread.start()
    owner_base = f"http://127.0.0.1:{owner_server.server_address[1]}"
    gateway_base = f"http://127.0.0.1:{gateway_server.server_address[1]}"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=chromium,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-web-security"],
            )
            context = browser.new_context(viewport={"width": 1500, "height": 1200})
            owner = context.new_page(); contributor = context.new_page()
            errors: list[str] = []
            owner.on("pageerror", lambda error: errors.append(f"owner: {error}"))
            contributor.on("pageerror", lambda error: errors.append(f"contributor: {error}"))
            owner.set_default_timeout(20000); contributor.set_default_timeout(20000)

            dialog_values = {
                "Label name:": "browser-bug",
                "Six-digit label color:": "#ff5577",
                "Milestone title:": "v0.5 project layer",
                "Due date in ISO format (optional):": "",
                "New issue title:": "Browser issue",
                "Description (inert Markdown; optional):": "<img src=x onerror=window.__projectXss=1> **owner evidence**",
            }

            def owner_dialog(dialog):
                value = dialog_values.get(dialog.message)
                if value is None:
                    dialog.accept()
                else:
                    dialog.accept(value)

            owner.on("dialog", owner_dialog)

            def rewrite_owner_request(route):
                headers = dict(route.request.headers)
                headers.pop("sec-fetch-site", None); headers.pop("origin", None)
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
            owner.wait_for_function("document.querySelector('#repoTitle')?.textContent.includes('Project Coordination Browser')")
            owner.click('[data-tab="project"]')
            owner.wait_for_function("document.querySelector('#projectStats')?.textContent.includes('Open issues')")

            owner.click("#projectNewLabelBtn")
            owner.wait_for_function("appState.project.overview?.labels?.some(item=>item.name==='browser-bug')")
            owner.click("#projectNewMilestoneBtn")
            owner.wait_for_function("appState.project.overview?.milestones?.some(item=>item.title==='v0.5 project layer')")
            with owner.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/project/issues")) as created_response:
                owner.click("#projectNewBtn")
            assert created_response.value.status == 201, created_response.value.text()
            owner.wait_for_function("document.querySelector('#projectDetail')?.textContent.includes('Browser issue')")
            assert owner.locator("#projectDetail img").count() == 0
            assert owner.evaluate("window.__projectXss||0") == 0
            owner.fill("#projectAssignee", "Rooke Poole")
            owner.select_option("#projectMilestone", label="v0.5 project layer")
            owner.check('[data-project-label]')
            owner.click("[data-project-save-meta]")
            owner.wait_for_function("appState.project.selected?.assignee==='Rooke Poole' && appState.project.selected?.labels?.some(item=>item.name==='browser-bug')")
            owner.fill("#projectCommentBody", "Owner comment from the real browser workflow.")
            owner.click("[data-project-comment]")
            owner.wait_for_function("document.querySelector('#projectDetail')?.textContent.includes('Owner comment from the real browser workflow.')")

            contributor_html = (ROOT / "contribute.html").read_text(encoding="utf-8")
            contributor_html = contributor_html.replace(
                "let token=(location.hash||'').slice(1).trim();\n    if(token){sessionStorage.setItem('forgetrace-invite-token',token);history.replaceState(null,'',location.pathname+location.search)}\n    else token=sessionStorage.getItem('forgetrace-invite-token')||'';",
                f"let token={json.dumps(token)};",
                1,
            )
            contributor_bridge = (
                "<script>(()=>{const realFetch=window.fetch.bind(window);"
                f"window.fetch=(input,init)=>{{const raw=typeof input==='string'?input:input.url;"
                f"return realFetch(new URL(raw,'{gateway_base}/').href,init);}};}})();</script>"
            )
            contributor_html = contributor_html.replace("<head>", f'<head><base href="{gateway_base}/">', 1).replace(
                "<script>", contributor_bridge + "<script>", 1
            )
            contributor.set_content(contributor_html, wait_until="domcontentloaded")
            contributor.wait_for_function("document.querySelector('#repoCard')?.textContent.includes('Project Coordination Browser')")
            contributor.wait_for_function("!document.querySelector('#projectPanel').classList.contains('hidden')")
            contributor.fill("#projectAuthor", "Outside Browser")
            contributor.select_option("#projectKind", "discussion")
            contributor.wait_for_function("document.querySelector('#projectCreate')?.textContent.includes('discussion')")
            contributor.fill("#projectTitle", "Contributor discussion")
            contributor.fill("#projectBody", "<svg onload=window.__contributorProjectXss=1></svg> **question**")
            with contributor.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/project/discussions")) as discussion_response:
                contributor.click("#projectCreate")
            assert discussion_response.value.status == 201, discussion_response.value.text()
            contributor.wait_for_function("document.querySelector('#projectDetail')?.textContent.includes('Contributor discussion')")
            assert contributor.locator("#projectDetail svg").count() == 0
            assert contributor.evaluate("window.__contributorProjectXss||0") == 0
            contributor.fill("#projectComment", "Contributor follow-up from the invitation-scoped workspace.")
            contributor.click("#projectCommentBtn")
            contributor.wait_for_function("document.querySelector('#projectDetail')?.textContent.includes('Contributor follow-up from the invitation-scoped workspace.')")

            owner.click("#projectKind")
            owner.select_option("#projectKind", "discussion")
            owner.click("#projectRefreshBtn")
            owner.wait_for_function("document.querySelector('#projectList')?.textContent.includes('Contributor discussion')")
            owner.locator("[data-project-item]", has_text="Contributor discussion").click()
            owner.wait_for_function("document.querySelector('#projectDetail')?.textContent.includes('Contributor follow-up from the invitation-scoped workspace.')")
            owner.locator("[data-project-accept]").click()
            owner.wait_for_function("document.querySelector('#projectDetail')?.textContent.includes('accepted answer')")
            owner.locator("[data-project-pin]").click()
            owner.wait_for_function("appState.project.selected?.pinned===true")
            owner.locator("[data-project-lock]").click()
            owner.wait_for_function("appState.project.selected?.locked===true")

            contributor.click("#projectRefresh")
            contributor.wait_for_function("document.querySelector('#projectList')?.textContent.includes('Contributor discussion')")
            contributor.locator("[data-project-id]", has_text="Contributor discussion").click()
            contributor.wait_for_function("document.querySelector('#projectDetail')?.textContent.includes('locked')")
            assert contributor.locator("#projectCommentBtn").count() == 0
            blocked = contributor.evaluate(
                """async()=>{try{await api(`/api/v1/collaboration/project/discussions/${encodeURIComponent(state.project.selected.id)}/comments`,{method:'POST',body:{body:'blocked',authorName:'Outside Browser',expectedVersion:state.project.selected.version}});return {ok:true}}catch(error){return {ok:false,message:error.message}}}"""
            )
            assert blocked["ok"] is False and "locked" in blocked["message"].lower(), blocked

            assert sha256(readme_path) == before_readme
            assert sha256(state_path) == before_state
            assert not (repository_path / ".git").exists()
            events = app.security_events.query(repository_id=repository_id, limit=300)["events"]
            actions = {event["action"] for event in events}
            assert {
                "project_label_created",
                "project_milestone_created",
                "project_issue_created",
                "project_discussion_created",
                "project_comment_created",
                "project_moderation_authorized",
            }.issubset(actions), actions
            assert not errors, errors
            browser.close()
        print("ForgeTrace owner/contributor project coordination browser workflow: PASS")
    finally:
        owner_server.shutdown()
        gateway_server.shutdown()
        owner_thread.join(timeout=5)
        gateway_thread.join(timeout=5)
        owner_server.server_close()
        gateway_server.server_close()
        if app.gateway:
            app.gateway.stop()
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
