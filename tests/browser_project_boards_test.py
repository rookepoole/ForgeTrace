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
        raise SystemExit("Chromium is required for board browser testing")
    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v050-board-browser-"))
    app = build_application(ROOT, temp / "data")
    record = app.registry.register_repository(path=str(temp / "repository"), name="Boards Browser", author="Rooke Poole", initialize=True, create_directory=True)
    repository_id = record["id"]; repository_path = Path(record["path"])
    issue = app.project.create_topic(repository_id, kind="issue", title="Ship project boards", body="No repository mutation")
    before_readme = sha256(repository_path / "README.md"); before_state = sha256(repository_path / ".forgetrace" / "state.json")
    invite = app.collaboration.create_invite(repository_id, allow_project_participation=True, max_uses=2)

    owner_handler = make_handler(app); owner_handler.enforce_owner_request_origin = lambda self, path: self.require_local_owner()
    owner_server = ForgeTraceHTTPServer(("127.0.0.1", 0), owner_handler); owner_server.forgetrace_surface = "owner"
    gateway_server = create_server(app, "127.0.0.1", 0, surface="gateway")
    owner_thread=threading.Thread(target=owner_server.serve_forever,daemon=True);gateway_thread=threading.Thread(target=gateway_server.serve_forever,daemon=True);owner_thread.start();gateway_thread.start()
    owner_base=f"http://127.0.0.1:{owner_server.server_address[1]}"; gateway_base=f"http://127.0.0.1:{gateway_server.server_address[1]}"
    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True,executable_path=chromium,args=["--no-sandbox","--disable-dev-shm-usage","--disable-web-security"])
            context=browser.new_context(viewport={"width":1600,"height":1200});owner=context.new_page();contributor=context.new_page();errors=[]
            owner.on("pageerror",lambda error:errors.append(f"owner: {error}"));contributor.on("pageerror",lambda error:errors.append(f"contributor: {error}"))
            owner.set_default_timeout(25000);contributor.set_default_timeout(25000)
            prompts=iter(["Delivery roadmap","Cross-team delivery"])
            def dialog(dialog):
                if dialog.type == "prompt": dialog.accept(next(prompts, ""))
                else: dialog.accept()
            owner.on("dialog",dialog)
            owner.route("**/*",lambda route:route.continue_(headers={k:v for k,v in route.request.headers.items() if k not in {"origin","sec-fetch-site"}}))
            html=(ROOT/"index.html").read_text();bridge=("<script>(()=>{const f=window.fetch.bind(window);"+f"window.fetch=(i,n)=>f(new URL(typeof i==='string'?i:i.url,'{owner_base}/').href,n);"+"})();</script>")
            html=html.replace("<head>",f'<head><base href="{owner_base}/">',1).replace("<script>",bridge+"<script>",1)
            owner.set_content(html,wait_until="domcontentloaded");owner.wait_for_function("document.querySelector('#repoTitle')?.textContent.includes('Boards Browser')")
            owner.click('[data-tab="project"]');owner.wait_for_function("document.querySelector('#projectList')?.textContent.includes('Ship project boards')")
            owner.locator('[data-project-item]',has_text="Ship project boards").click();owner.wait_for_function("appState.project.selected?.title==='Ship project boards'")
            owner.click('#boardNewBtn');owner.wait_for_function("appState.boards.items.length===1 && appState.boards.detail?.columns?.length===3")
            assert owner.locator('.board-column').count()==3
            owner.click('#boardAddCardBtn');owner.wait_for_function("appState.boards.detail?.cards?.length===1")
            owner.click('[data-board-move]');owner.wait_for_function("appState.boards.detail.cards[0].columnId===appState.boards.detail.columns[1].id")
            owner.select_option('#boardView','table');owner.wait_for_function("document.querySelector('.board-table')!==null")
            owner.select_option('#boardView','roadmap');owner.wait_for_function("document.querySelector('.board-roadmap')!==null")
            owner.select_option('#boardView','kanban')

            chtml=(ROOT/"contribute.html").read_text().replace("let token=(location.hash||'').slice(1).trim();\n    if(token){sessionStorage.setItem('forgetrace-invite-token',token);history.replaceState(null,'',location.pathname+location.search)}\n    else token=sessionStorage.getItem('forgetrace-invite-token')||'';",f"let token={json.dumps(invite['token'])};",1)
            cbridge=("<script>(()=>{const f=window.fetch.bind(window);"+f"window.fetch=(i,n)=>f(new URL(typeof i==='string'?i:i.url,'{gateway_base}/').href,n);"+"})();</script>")
            chtml=chtml.replace("<head>",f'<head><base href="{gateway_base}/">',1).replace("<script>",cbridge+"<script>",1)
            contributor.set_content(chtml,wait_until="domcontentloaded");contributor.wait_for_function("document.querySelector('#repoCard')?.textContent.includes('Boards Browser')")
            contributor.wait_for_function("state.boards.items.length===1")
            contributor.select_option('#boardSelect',label='Delivery roadmap');contributor.wait_for_function("state.boards.detail?.cards?.length===1")
            contributor.fill('#projectAuthor','Outside Planner')
            contributor.click('[data-board-move]');contributor.wait_for_function("state.boards.detail.cards[0].columnId===state.boards.detail.columns.find(c=>c.name==='Backlog').id")

            assert sha256(repository_path/"README.md")==before_readme
            assert sha256(repository_path/".forgetrace"/"state.json")==before_state
            assert not (repository_path/".git").exists()
            actions={e["action"] for e in app.security_events.query(repository_id=repository_id,limit=200)["events"]}
            assert {"board_created","board_create_authorized"}.issubset(actions),actions
            assert not errors,errors
            browser.close()
        print("ForgeTrace Project Boards and Roadmaps browser workflow: PASS")
    finally:
        owner_server.shutdown();gateway_server.shutdown();owner_thread.join(timeout=5);gateway_thread.join(timeout=5);owner_server.server_close();gateway_server.server_close();shutil.rmtree(temp,ignore_errors=True)


if __name__ == "__main__":
    main()
