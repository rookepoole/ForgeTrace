from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forgetrace.app import build_application
from forgetrace.web import create_server
from tests.browser_smoke_test import CDP, get_json, wait_for


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not chromium:
        raise SystemExit("Chromium is required")
    temp_dir = tempfile.TemporaryDirectory(prefix="forgetrace-collaboration-browser-")
    root = Path(temp_dir.name)
    app = build_application(ROOT, root / "data")
    record = app.registry.register_repository(
        path=str(root / "repository"),
        name="Collaboration Browser",
        description="Live pull request browser test",
        author="Local Owner",
        initialize=True,
        create_directory=True,
    )
    repository_id = record["id"]
    repository = app.registry.repository_service(repository_id)
    repository.write_file("src/app.txt", b"owner baseline\n", "Local Owner", "seed")
    invite = app.collaboration.create_invite(repository_id, label="Browser contributor", max_uses=1)
    token = invite["token"]

    server = create_server(app, "127.0.0.1", 0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = int(server.server_address[1])
    debug_port = free_port()
    chrome = None
    cdp = None
    try:
        chrome = subprocess.Popen([
            chromium, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            "--remote-allow-origins=*", f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={root / 'chrome'}", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        targets = wait_for(lambda: get_json(f"http://127.0.0.1:{debug_port}/json/list"), timeout=15)
        page = next(target for target in targets if target.get("type") == "page")
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")
        cdp.call("Log.enable")

        contributor_url = f"http://127.0.0.1:{port}/contribute.html#{token}"
        navigation = cdp.call("Page.navigate", {"url": contributor_url})
        if navigation.get("errorText") == "net::ERR_BLOCKED_BY_ADMINISTRATOR":
            print("ForgeTrace live collaboration browser test: SKIP (managed Chromium blocks localhost navigation)")
            return
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoCard')?.textContent.includes('Collaboration Browser')"), timeout=15)
        cdp.evaluate("document.querySelector('#authorName').value='Outside Browser'; document.querySelector('#title').value='Improve browser text'; document.querySelector('#description').value='Live remote contribution test'; document.querySelector('#createForm').requestSubmit();")
        wait_for(lambda: cdp.evaluate("!document.querySelector('#prPanel').classList.contains('hidden')"), timeout=15)
        cdp.evaluate("(async()=>{const blob=new Blob(['owner baseline\\noutside change\\n'],{type:'text/plain'});state.pr=await api(`/api/v1/collaboration/pull-requests/${encodeURIComponent(state.pr.id)}/files?path=${encodeURIComponent('src/app.txt')}`,{method:'POST',body:blob});renderPr();})()")
        wait_for(lambda: cdp.evaluate("document.querySelector('#changeList')?.textContent.includes('src/app.txt')"), timeout=15)
        cdp.evaluate("window.confirm=()=>true; document.querySelector('#submitPr').click();")
        wait_for(lambda: cdp.evaluate("document.querySelector('#prStatus')?.textContent.trim()==='open'"), timeout=15)

        cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoTitle')?.textContent==='Collaboration Browser'"), timeout=15)
        wait_for(lambda: cdp.evaluate("document.querySelector('#prTabCount')?.textContent.includes('1')"), timeout=15)
        cdp.evaluate("document.querySelector('[data-tab=\"pullrequests\"]').click(); document.querySelector('[data-pr-id]').click();")
        wait_for(lambda: cdp.evaluate("document.querySelector('#pullRequestDetail')?.textContent.includes('outside change')"), timeout=15)
        cdp.evaluate("window.prompt=()=> 'Reviewed in browser'; document.querySelector('[data-pr-review=\"approved\"]').click();")
        wait_for(lambda: cdp.evaluate("document.querySelector('#pullRequestDetail .pr-status')?.textContent.trim()==='approved'"), timeout=15)
        cdp.evaluate("window.prompt=()=> 'MERGE #1'; window.confirm=()=>true; document.querySelector('[data-pr-merge]').click();")
        wait_for(lambda: (root / "repository" / "src" / "app.txt").read_text() == "owner baseline\noutside change\n", timeout=20)
        wait_for(lambda: cdp.evaluate("document.querySelector('#pullRequestDetail')?.textContent.includes('merged')"), timeout=15)

        errors = []
        for event in cdp.events:
            if event.get("method") == "Runtime.exceptionThrown":
                errors.append(event.get("params", {}))
            if event.get("method") == "Log.entryAdded" and event.get("params", {}).get("entry", {}).get("level") == "error":
                errors.append(event.get("params", {}))
        if errors:
            raise AssertionError(json.dumps(errors, indent=2))
        print("ForgeTrace live collaboration browser test: PASS")
    finally:
        if cdp:
            cdp.close()
        if chrome:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome.kill()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        temp_dir.cleanup()


if __name__ == "__main__":
    main()
