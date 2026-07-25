from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from forgetrace.app import build_application
from forgetrace.web import ForgeTraceHTTPServer, make_handler

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not chromium:
        raise SystemExit("Chromium is required for black-box browser testing")
    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v040-blackbox-"))
    source = temp / "Complete Project"
    deepest = source / "src" / "features" / "auth" / "templates" / "forms" / "sign-in.html"
    deepest.parent.mkdir(parents=True)
    deepest.write_text("<form>real disk</form>", encoding="utf-8")
    (source / "root.txt").write_text("root", encoding="utf-8")
    (source / "empty" / "nested").mkdir(parents=True)
    (source / ".env").write_text("TOKEN=black-box", encoding="utf-8")
    previous = os.environ.get("FORGETRACE_TEST_PICK_FOLDER")
    os.environ["FORGETRACE_TEST_PICK_FOLDER"] = str(source)
    app = build_application(ROOT, temp / "data")
    handler = make_handler(app)
    # Browser navigation to localhost is blocked by this execution environment, so the
    # test loads the real HTML through set_content and removes only the browser-origin
    # guard in this test subclass. Loopback/Host boundaries and all real APIs remain active.
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
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            # A storage failure must never turn a completed import into a false failure.
            context.add_init_script("Storage.prototype.setItem=function(){throw new Error('quota denied for test')}")
            page = context.new_page()
            def rewrite_owner_request(route):
                headers=dict(route.request.headers)
                headers.pop("sec-fetch-site", None)
                headers.pop("origin", None)
                route.continue_(headers=headers)
            page.route("**/*", rewrite_owner_request)
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            html=(ROOT / "index.html").read_text(encoding="utf-8")
            bridge=f"""<script>(()=>{{const realFetch=window.fetch.bind(window);window.fetch=(input,init)=>{{const raw=typeof input==='string'?input:input.url;return realFetch(new URL(raw,'{base}/').href,init);}};}})();</script>"""
            html=html.replace("<head>", f"<head><base href=\"{base}/\">", 1).replace("<script>", bridge+"<script>", 1)
            page.set_content(html, wait_until="networkidle")
            page.click("#welcomeAddBtn")
            page.click("#repoImportLocalChoice")
            page.wait_for_function("document.querySelector('#repoImportSummary')?.textContent.includes('Complete Project')")
            page.fill("#repoNameInput", "Black Box Imported")
            page.once("dialog", lambda dialog: dialog.accept())
            page.click("#repoSubmitBtn")
            page.wait_for_function("document.querySelector('#repoTitle')?.textContent === 'Black Box Imported'", timeout=30000)
            page.wait_for_selector('[data-file-path="src/features/auth/templates/forms/sign-in.html"]', timeout=30000)
            page.click('[data-file-path="src/features/auth/templates/forms/sign-in.html"]')
            page.wait_for_function("document.querySelector('#code')?.value.includes('real disk')")
            record = app.registry.list_repositories()["repositories"][0]
            workspace = Path(record["path"])
            assert (workspace / "src/features/auth/templates/forms/sign-in.html").read_text(encoding="utf-8") == "<form>real disk</form>"
            assert (workspace / "empty/nested").is_dir()
            assert not errors, errors

            # Folder rename and delete operate on real server state.
            page.click('[data-file-path="src"]')
            page.once("dialog", lambda dialog: dialog.accept("source-renamed"))
            page.click("#renameBtn")
            page.wait_for_selector('[data-file-path="source-renamed"]', timeout=10000)
            assert (workspace / "source-renamed/features/auth/templates/forms/sign-in.html").is_file()
            page.click('[data-file-path="source-renamed"]')
            page.once("dialog", lambda dialog: dialog.accept())
            page.click("#deleteBtn")
            page.wait_for_function("!document.querySelector('[data-file-path=\"source-renamed\"]')")
            assert not (workspace / "source-renamed").exists()
            assert not errors, errors
            browser.close()
        print("ForgeTrace real server + real disk browser test: PASS")
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        if previous is None:
            os.environ.pop("FORGETRACE_TEST_PICK_FOLDER", None)
        else:
            os.environ["FORGETRACE_TEST_PICK_FOLDER"] = previous
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
