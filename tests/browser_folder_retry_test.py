from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from browser_smoke_test import CDP, get_json, mock_transport_script, wait_for


def main() -> None:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not chromium:
        raise SystemExit("Chromium is required for folder retry browser testing")
    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v035-retry-"))
    source = temp / "RetryRoot" / "a" / "b" / "c"
    source.mkdir(parents=True)
    (source / "retry.txt").write_text("retry me", encoding="utf-8")
    chrome = None
    cdp = None
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            debug_port = int(sock.getsockname()[1])
        chrome = subprocess.Popen([
            chromium, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            "--remote-allow-origins=*", f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={temp / 'chrome'}", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        targets = wait_for(lambda: get_json(f"http://127.0.0.1:{debug_port}/json/list"), timeout=15)
        page = next(target for target in targets if target.get("type") == "page")
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")
        cdp.call("DOM.enable")
        mock = mock_transport_script() + r'''
        (() => {
          const originalFetch = window.fetch;
          window.__folderRetryCount = 0;
          window.fetch = async (input, init={}) => {
            const url = new URL(typeof input === 'string' ? input : input.url, 'http://forgetrace.local');
            const path = url.searchParams.get('path') || '';
            if (url.pathname.endsWith('/upload') && path.endsWith('/retry.txt') && window.__folderRetryCount++ === 0) {
              return new Response(JSON.stringify({error:'simulated interrupted nested upload'}), {status:503,headers:{'Content-Type':'application/json'}});
            }
            return originalFetch(input, init);
          };
        })();
        '''
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        html = html.replace("<script>", f"<script>{mock}</script><script>", 1)
        frame_id = cdp.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        cdp.call("Page.setDocumentContent", {"frameId": frame_id, "html": html})
        wait_for(lambda: cdp.evaluate("document.readyState === 'complete' && document.querySelector('#repoTitle')?.textContent === 'Alpha'"))
        document = cdp.call("DOM.getDocument", {"depth": -1, "pierce": True})
        node_id = cdp.call("DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": "#folderInput"})["nodeId"]
        cdp.call("DOM.setFileInputFiles", {"files": [str(temp / 'RetryRoot')], "nodeId": node_id})
        wait_for(lambda: cdp.evaluate("document.querySelector('#folderImportReport')?.textContent.includes('Import verified: all 1 file')"), timeout=15)
        assert cdp.evaluate("window.__folderRetryCount") >= 2
        assert cdp.evaluate("Boolean(document.querySelector('[data-file-path=\"RetryRoot/a/b/c/retry.txt\"]'))")
        print("Folder verification retry browser test: PASS")
    finally:
        if cdp is not None:
            cdp.close()
        if chrome is not None:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome.kill()
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
