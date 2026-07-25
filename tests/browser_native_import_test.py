from __future__ import annotations

import base64
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
        raise SystemExit("Chromium is required for native import UI testing")
    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v036-native-ui-"))
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
        page = next(item for item in targets if item.get("type") == "page")
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")
        cdp.call("DOM.enable")
        cdp.call("Log.enable")

        mock = mock_transport_script() + r'''
        (() => {
          const originalFetch = window.fetch;
          window.__nativeImportDone = false;
          window.fetch = async (input, init={}) => {
            const url = new URL(typeof input === 'string' ? input : input.url, 'http://forgetrace.local');
            if (url.pathname === '/api/v1/system/pick-folder') {
              return new Response(JSON.stringify({available:true,cancelled:false,path:'C:\\Projects\\CompleteProject',name:'CompleteProject'}), {status:200,headers:{'Content-Type':'application/json'}});
            }
            if (url.pathname.endsWith('/import-preview')) {
              return new Response(JSON.stringify({sourceName:'CompleteProject',conflicts:[],sensitiveFiles:[],fileCount:2,folderCount:7,totalBytes:8}), {status:200,headers:{'Content-Type':'application/json'}});
            }
            if (url.pathname.endsWith('/import-jobs')) {
              return new Response(JSON.stringify({id:'job-native',status:'queued'}), {status:202,headers:{'Content-Type':'application/json'}});
            }
            if (url.pathname === '/api/v1/jobs/job-native') {
              window.__nativeImportDone = true;
              return new Response(JSON.stringify({
                id:'job-native',status:'completed',phase:'completed',message:'Folder import verified and committed.',progress:{filesApplied:2,totalFiles:2},
                result:{sourceName:'CompleteProject',importedFiles:['CompleteProject/root.txt','CompleteProject/a/b/c/d/e/deep.txt'],createdFolders:['CompleteProject','CompleteProject/a','CompleteProject/a/b','CompleteProject/a/b/c','CompleteProject/a/b/c/d','CompleteProject/a/b/c/d/e','CompleteProject/empty']}
              }), {status:200,headers:{'Content-Type':'application/json'}});
            }
            if (window.__nativeImportDone && url.pathname === '/api/v1/repositories/alpha-id/state') {
              const response = await originalFetch(input, init);
              const payload = await response.json();
              payload.tree = [
                {type:'folder',name:'CompleteProject',path:'CompleteProject'},
                {type:'folder',name:'a',path:'CompleteProject/a'},
                {type:'folder',name:'b',path:'CompleteProject/a/b'},
                {type:'folder',name:'c',path:'CompleteProject/a/b/c'},
                {type:'folder',name:'d',path:'CompleteProject/a/b/c/d'},
                {type:'folder',name:'e',path:'CompleteProject/a/b/c/d/e'},
                {type:'folder',name:'empty',path:'CompleteProject/empty'},
                {type:'file',name:'root.txt',path:'CompleteProject/root.txt',size:4},
                {type:'file',name:'deep.txt',path:'CompleteProject/a/b/c/d/e/deep.txt',size:4}
              ];
              payload.summary.stats.files = 2;
              payload.summary.stats.folders = 7;
              payload.summary.stats.bytes = 8;
              return new Response(JSON.stringify(payload), {status:200,headers:{'Content-Type':'application/json'}});
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
        cdp.evaluate("document.querySelector('#importLocalFolderBtn').click()")
        wait_for(lambda: cdp.evaluate("window.__nativeImportDone === true"), timeout=15)
        deepest = "CompleteProject/a/b/c/d/e/deep.txt"
        wait_for(lambda: cdp.evaluate(f"Boolean(document.querySelector('[data-file-path=\"{deepest}\"]'))"))
        assert cdp.evaluate("document.querySelector('[data-file-path=\"CompleteProject\"]')?.getAttribute('aria-expanded')") == "true"
        assert cdp.evaluate("document.querySelector('#statFiles').textContent") == "2"
        screenshot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        (ROOT / "assets" / "preview-complete-folder-import.png").write_bytes(base64.b64decode(screenshot["data"]))
        errors = [event for event in cdp.events if event.get("method") in {"Runtime.exceptionThrown", "Log.entryAdded"} and "favicon" not in str(event).lower()]
        if errors:
            raise AssertionError(f"Native import UI emitted runtime errors: {errors}")
        print("ForgeTrace native direct-folder UI test: PASS")
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
