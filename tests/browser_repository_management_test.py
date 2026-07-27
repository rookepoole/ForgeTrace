from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forgetrace.app import build_application
from forgetrace.web import create_server


def find_chromium() -> str:
    candidates = [
        os.environ.get("CHROMIUM_PATH", ""),
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Chromium executable was not found.")


def read_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def delete_json(url: str) -> tuple[int, dict]:
    request = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def script_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def main() -> None:
    from playwright.sync_api import sync_playwright

    temp = Path(tempfile.mkdtemp(prefix="forgetrace-browser-repository-management-"))
    data_dir = temp / "data"
    app = build_application(ROOT, data_dir)
    target = app.registry.create_managed_repository(
        name="Browser Delete Repository",
        description="permanent deletion browser fixture",
        author="Rooke Poole",
    )
    survivor = app.registry.create_managed_repository(
        name="Browser Survivor",
        description="replacement active repository",
        author="Rooke Poole",
    )
    target_path = Path(target["path"])
    service = app.registry.repository_service(target["id"])
    for index in range(90):
        service.write_file(
            f"group-{index // 15:02d}/file-{index:03d}.txt",
            f"browser file {index}".encode("utf-8"),
            "Rooke Poole",
            "Populate enlarged tree",
            uploaded=True,
        )
    app.registry.set_active(target["id"])

    # Preserve a UUID-identical legacy copy in another startup discovery root.
    # The permanent-deletion tombstone must prevent it from reappearing.
    ghost = data_dir / "repositories" / "legacy-empty-copy"
    ghost.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(target_path, ghost)
    server = create_server(app, "127.0.0.1", 0, surface="owner")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        initial_version = read_json(base + "/api/v1/version")
        initial_library = read_json(base + "/api/v1/library")
        initial_repositories = read_json(base + "/api/v1/repositories")
        target_state = read_json(base + f"/api/v1/repositories/{target['id']}/state")
        survivor_state = read_json(base + f"/api/v1/repositories/{survivor['id']}/state")
        target_prs = read_json(base + f"/api/v1/repositories/{target['id']}/pull-requests")
        survivor_prs = read_json(base + f"/api/v1/repositories/{survivor['id']}/pull-requests")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=find_chromium(),
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page(viewport={"width": 1680, "height": 1200})
            page.set_default_timeout(30000)
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def dialog_handler(dialog):
                if dialog.type == "prompt":
                    dialog.accept(target["name"])
                else:
                    dialog.accept()

            page.on("dialog", dialog_handler)

            mock = rf"""<script>
            (() => {{
              const version = {script_json(initial_version)};
              const library = {script_json(initial_library)};
              let repositories = {script_json(initial_repositories['repositories'])};
              let activeRepositoryId = {script_json(target['id'])};
              const states = {{
                {script_json(target['id'])}: {script_json(target_state)},
                {script_json(survivor['id'])}: {script_json(survivor_state)}
              }};
              const pullRequests = {{
                {script_json(target['id'])}: {script_json(target_prs)},
                {script_json(survivor['id'])}: {script_json(survivor_prs)}
              }};
              const response = (payload, status = 200) => Promise.resolve(new Response(
                JSON.stringify(payload), {{status, headers: {{'Content-Type': 'application/json'}}}}
              ));
              window.__deleteRequest = null;
              window.__completeManagedDelete = (payload, status) => {{
                repositories = repositories.filter(item => item.id !== payload.deleted);
                activeRepositoryId = payload.activeRepositoryId || {script_json(survivor['id'])};
                const resolver = window.__resolveManagedDelete;
                window.__resolveManagedDelete = null;
                resolver(new Response(JSON.stringify(payload), {{status, headers: {{'Content-Type':'application/json'}}}}));
              }};
              window.fetch = async (input, init = {{}}) => {{
                const url = new URL(typeof input === 'string' ? input : input.url, 'http://forgetrace.local');
                const method = (init.method || 'GET').toUpperCase();
                if (url.pathname === '/api/v1/version' && method === 'GET') return response(version);
                if (url.pathname === '/api/v1/library' && method === 'GET') return response(library);
                if (url.pathname === '/api/v1/repositories' && method === 'GET') {{
                  return response({{activeRepositoryId, repositories: repositories.map(item => ({{...item, active: item.id === activeRepositoryId}}))}});
                }}
                if (url.pathname === '/api/v1/active-repository' && method === 'POST') {{
                  activeRepositoryId = JSON.parse(init.body || '{{}}').repositoryId || activeRepositoryId;
                  return response(repositories.find(item => item.id === activeRepositoryId));
                }}
                const stateMatch = url.pathname.match(/^\/api\/v1\/repositories\/([^/]+)\/state$/);
                if (stateMatch && method === 'GET') return response(states[stateMatch[1]]);
                const prMatch = url.pathname.match(/^\/api\/v1\/repositories\/([^/]+)\/pull-requests$/);
                if (prMatch && method === 'GET') return response(pullRequests[prMatch[1]] || {{pullRequests:[]}});
                const deleteMatch = url.pathname.match(/^\/api\/v1\/repositories\/([^/]+)\/delete-managed$/);
                if (deleteMatch && method === 'DELETE') {{
                  window.__deleteRequest = {{repositoryId: deleteMatch[1], search: url.search}};
                  return new Promise(resolve => {{ window.__resolveManagedDelete = resolve; }});
                }}
                return response({{error: `Unexpected browser-harness request: ${{method}} ${{url.pathname}}`}}, 500);
              }};
            }})();
            </script>"""
            html = (ROOT / "index.html").read_text(encoding="utf-8")
            html = html.replace("<script>", mock + "<script>", 1)
            page.set_content(html, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelector('#repoTitle')?.textContent.includes('Browser Delete Repository')"
            )
            page.fill("#fileSearch", "file-")
            page.wait_for_function("document.querySelectorAll('#tree .file-row').length > 10")
            dimensions = page.evaluate(
                """()=>{const tree=document.querySelector('#tree').getBoundingClientRect();const pane=document.querySelector('.file-pane').getBoundingClientRect();const layout=document.querySelector('.files-layout').getBoundingClientRect();return {treeWidth:tree.width,treeHeight:tree.height,paneWidth:pane.width,layoutWidth:layout.width}}"""
            )
            assert dimensions["treeHeight"] >= 500, dimensions
            assert dimensions["paneWidth"] >= 500, dimensions
            assert dimensions["paneWidth"] / dimensions["layoutWidth"] >= 0.44, dimensions
            page.click("#settingsBtn")
            page.wait_for_function("document.querySelector('#settingsModalBackdrop').classList.contains('open')")
            assert page.locator("#managedRepositoryDangerZone").is_visible()
            assert page.locator("#deleteManagedRepositoryBtn").is_enabled()
            page.click("#deleteManagedRepositoryBtn")
            page.wait_for_function("window.__deleteRequest?.repositoryId", timeout=30000)
            delete_request = page.evaluate("window.__deleteRequest")
            assert delete_request["repositoryId"] == target["id"], delete_request
            status, payload = delete_json(
                base
                + f"/api/v1/repositories/{target['id']}/delete-managed"
                + (delete_request.get("search") or "")
            )
            assert status == 200, payload
            assert payload["deleted"] == target["id"], payload
            assert payload["filesDeleted"] is True, payload
            page.evaluate(
                "([payload,status])=>window.__completeManagedDelete(payload,status)",
                [payload, status],
            )

            page.wait_for_function(
                "!appState.repositories.some(item=>item.name==='Browser Delete Repository')"
            )
            page.wait_for_function(
                "document.querySelector('#repoTitle')?.textContent.includes('Browser Survivor')"
            )
            assert not target_path.exists()
            assert page.locator("#repoList", has_text="Browser Delete Repository").count() == 0
            assert not errors, errors
            browser.close()
        actions = {
            event["action"]
            for event in app.security_events.query(repository_id=target["id"], limit=100)["events"]
        }
        assert {"managed_repository_delete_authorized", "managed_repository_deleted"}.issubset(actions), actions
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if app.gateway:
            app.gateway.stop()
    reopened = build_application(ROOT, data_dir)
    try:
        assert target["id"] not in {
            item["id"] for item in reopened.registry.list_repositories()["repositories"]
        }
        assert reopened.registry.startup_recovery_report["tombstoned"] >= 1
        assert survivor["id"] in {
            item["id"] for item in reopened.registry.list_repositories()["repositories"]
        }
        print("ForgeTrace enlarged file tree and permanent managed-repository deletion browser workflow: PASS")
    finally:
        if reopened.gateway:
            reopened.gateway.stop()
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
