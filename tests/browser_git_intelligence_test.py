from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from forgetrace.app import build_application
from forgetrace.web import ForgeTraceHTTPServer, make_handler

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    git = shutil.which("git")
    if not chromium or not git:
        raise SystemExit("Chromium and Git are required for Git intelligence browser testing")

    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v048-git-browser-"))
    app = build_application(ROOT, temp / "data")
    app.owner_instance_lock_held = True
    record = app.registry.register_repository(
        path=str(temp / "repository"),
        name="Git Intelligence Browser",
        description="v0.4.8 browser fixture",
        author="Rooke Poole",
        initialize=True,
        create_directory=True,
    )
    repository = Path(record["path"])

    def run_git(*args: str) -> str:
        return subprocess.run([git, *args], cwd=repository, text=True, capture_output=True, check=True).stdout.strip()

    run_git("init")
    run_git("config", "user.name", "ForgeTrace Browser")
    run_git("config", "user.email", "browser@example.invalid")
    run_git("add", "README.md")
    run_git("commit", "-m", "Initial browser commit")
    run_git("branch", "feature/browser-readonly")
    run_git("tag", "browser-v1")
    run_git("remote", "add", "origin", "https://browser:supersecret@example.invalid/owner/repo.git?token=hidden")
    (repository / "README.md").write_text("# Git Intelligence Browser\n\nStaged change.\n", encoding="utf-8")
    run_git("add", "README.md")
    (repository / "working.txt").write_text("working tree change\n", encoding="utf-8")

    index_path = repository / ".git" / "index"
    before_hash = hashlib.sha256(index_path.read_bytes()).hexdigest()
    before_mtime = index_path.stat().st_mtime_ns

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
            context = browser.new_context(viewport={"width": 1700, "height": 1200})
            page = context.new_page()

            def rewrite_owner_request(route):
                headers = dict(route.request.headers)
                headers.pop("sec-fetch-site", None)
                headers.pop("origin", None)
                route.continue_(headers=headers)

            page.route("**/*", rewrite_owner_request)
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            html = (ROOT / "index.html").read_text(encoding="utf-8")
            bridge = (
                "<script>(()=>{const realFetch=window.fetch.bind(window);"
                f"window.fetch=(input,init)=>{{const raw=typeof input==='string'?input:input.url;"
                f"return realFetch(new URL(raw,'{base}/').href,init);}};}})();</script>"
            )
            html = html.replace("<head>", f'<head><base href="{base}/">', 1).replace(
                "<script>", bridge + "<script>", 1
            )
            page.set_content(html, wait_until="networkidle")

            page.click('[data-tab="git"]')
            page.wait_for_function(
                "document.querySelector('#gitOverview')?.textContent.includes('Initial browser commit')"
                " && document.querySelector('#gitOverview')?.textContent.includes('feature/browser-readonly')",
                timeout=30000,
            )
            text = page.locator("#gitOverview").inner_text()
            assert "Dirty" in text
            assert "browser-v1" in text
            assert "<redacted>@example.invalid/owner/repo.git" in text
            assert "supersecret" not in text
            assert "token=hidden" not in text

            page.click('[data-git-diff-path="README.md"]')
            page.wait_for_function("document.querySelector('#gitDetail')?.textContent.includes('Staged change.')")
            diff_text = page.locator("#gitDetail").inner_text()
            assert "staged diff" in diff_text.lower()
            assert "Staged change." in diff_text

            page.click('[data-git-commit]')
            page.wait_for_function("document.querySelector('#gitDetail')?.textContent.includes('Initial browser commit')")
            assert "README.md" in page.locator("#gitDetail").inner_text()

            page.click("#gitRefreshBtn")
            page.wait_for_function("document.querySelector('#gitOverview')?.textContent.includes('Transactional local only')")
            assert hashlib.sha256(index_path.read_bytes()).hexdigest() == before_hash
            assert index_path.stat().st_mtime_ns == before_mtime
            assert not errors, errors
            browser.close()
        print("ForgeTrace Git intelligence and branch explorer owner browser workflow: PASS")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
