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


def free_debug_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_deep_folder(root: Path, name: str, deepest: str) -> Path:
    folder = root / name
    files = {
        "root.txt": "root",
        "src/main.py": "print(1)",
        deepest: "deep",
        "assets/icons/ui/toolbar/actions/add.svg": "<svg></svg>",
        "docs/reference/api/v1/endpoints/upload.md": "# Upload\n",
    }
    for relative, content in files.items():
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return folder


def set_directory_input(cdp: CDP, selector: str, directory: Path) -> None:
    document = cdp.call("DOM.getDocument", {"depth": -1, "pierce": True})
    node_id = cdp.call("DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": selector})["nodeId"]
    if not node_id:
        raise AssertionError(f"Could not locate {selector}")
    cdp.call("DOM.setFileInputFiles", {"files": [str(directory)], "nodeId": node_id})


def main() -> None:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not chromium:
        raise SystemExit("Chromium is required for recursive folder browser testing")

    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v035-browser-folder-"))
    existing_folder = make_deep_folder(
        temp,
        "MainFolder",
        "src/features/auth/templates/forms/sign-in.html",
    )
    new_folder = make_deep_folder(
        temp,
        "NewProject",
        "src/modules/account/views/profile.js",
    )
    chrome = None
    cdp = None
    try:
        debug_port = free_debug_port()
        chrome = subprocess.Popen(
            [
                chromium,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--remote-allow-origins=*",
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={temp / 'chrome'}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        targets = wait_for(lambda: get_json(f"http://127.0.0.1:{debug_port}/json/list"), timeout=15)
        page = next(target for target in targets if target.get("type") == "page")
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")
        cdp.call("DOM.enable")
        cdp.call("Log.enable")
        cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
        )

        html = (ROOT / "index.html").read_text(encoding="utf-8")
        html = html.replace("<script>", f"<script>{mock_transport_script()}</script><script>", 1)
        frame_id = cdp.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        cdp.call("Page.setDocumentContent", {"frameId": frame_id, "html": html})
        wait_for(lambda: cdp.evaluate("document.readyState === 'complete' && document.querySelector('#repoTitle')?.textContent === 'Alpha'"))

        # This is a real Chromium directory input, not a mocked File System Access API.
        set_directory_input(cdp, "#folderInput", existing_folder)
        wait_for(
            lambda: cdp.evaluate(
                "document.querySelector('#folderImportReport')?.textContent.includes('Import verified: all 5 files')"
            ),
            timeout=20,
        )
        deepest = "MainFolder/src/features/auth/templates/forms/sign-in.html"
        wait_for(lambda: cdp.evaluate(f"Boolean(document.querySelector('[data-file-path=\"{deepest}\"]'))"))
        assert cdp.evaluate("document.querySelector('[data-file-path=\"MainFolder\"]')?.getAttribute('aria-expanded')") == "true"
        assert cdp.evaluate("document.querySelector('#folderInput').files.length") == 0

        # Verify new-repository folder onboarding also uses the native recursive FileList.
        cdp.evaluate("document.querySelector('#addRepoBtn').click()")
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoModalBackdrop').classList.contains('open')"))
        set_directory_input(cdp, "#newRepoFolderInput", new_folder)
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoImportSummary').textContent.includes('5 files selected')"))
        assert cdp.evaluate("document.querySelector('#repoNameInput').value") == "NewProject"
        cdp.evaluate("document.querySelector('#repoForm').requestSubmit()")
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoTitle')?.textContent === 'NewProject'"), timeout=20)
        new_deepest = "src/modules/account/views/profile.js"
        wait_for(lambda: cdp.evaluate(f"Boolean(document.querySelector('[data-file-path=\"{new_deepest}\"]'))"))
        assert not cdp.evaluate("Boolean(document.querySelector('[data-file-path^=\"NewProject/\"]'))")

        screenshot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        (ROOT / "assets" / "preview-verified-folder-import.png").write_bytes(base64.b64decode(screenshot["data"]))

        runtime_errors = [
            event
            for event in cdp.events
            if event.get("method") in {"Runtime.exceptionThrown", "Log.entryAdded"}
            and "favicon" not in str(event).lower()
        ]
        if runtime_errors:
            raise AssertionError(f"Browser emitted runtime errors: {runtime_errors}")
        print("Verified native folder browser test: PASS")
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
