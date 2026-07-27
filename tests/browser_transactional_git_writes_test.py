from __future__ import annotations

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
        raise SystemExit("Chromium and Git are required for transactional Git write browser testing")

    temp = Path(tempfile.mkdtemp(prefix="forgetrace-v052-git-write-browser-"))
    app = build_application(ROOT, temp / "data")
    app.owner_instance_lock_held = True
    record = app.registry.register_repository(
        path=str(temp / "repository"),
        name="Transactional Git Write Browser",
        description="v0.5.2 browser fixture",
        author="Rooke Poole",
        initialize=True,
        create_directory=True,
    )
    repository = Path(record["path"])

    def run_git(*args: str) -> str:
        return subprocess.run([git, *args], cwd=repository, text=True, capture_output=True, check=True).stdout.strip()

    run_git("init")
    run_git("config", "user.name", "External Config Must Not Be Used")
    run_git("config", "user.email", "external@example.invalid")
    run_git("add", "README.md")
    run_git("commit", "-m", "Initial browser commit")
    (repository / "browser-write.txt").write_text("transactional browser write\n", encoding="utf-8")

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
            context = browser.new_context(viewport={"width": 1700, "height": 1250})
            page = context.new_page()

            def rewrite_owner_request(route):
                headers = dict(route.request.headers)
                headers.pop("sec-fetch-site", None)
                headers.pop("origin", None)
                route.continue_(headers=headers)

            page.route("**/*", rewrite_owner_request)
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def accept_write_dialog(dialog):
                message = dialog.message
                if "Commit message" in message:
                    dialog.accept("Commit from transactional browser UI")
                elif "Commit author email" in message:
                    dialog.accept("browser-owner@example.invalid")
                elif "New local branch name" in message:
                    dialog.accept("feature/browser-transaction")
                elif "New local lightweight tag name" in message:
                    dialog.accept("browser-v0.5.2")
                elif "Type STAGE" in message:
                    dialog.accept("STAGE")
                elif "Type COMMIT" in message:
                    dialog.accept("COMMIT")
                elif "Type CREATE BRANCH" in message:
                    dialog.accept("CREATE BRANCH")
                elif "Type CREATE TAG" in message:
                    dialog.accept("CREATE TAG")
                else:
                    dialog.dismiss()

            page.on("dialog", accept_write_dialog)
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
                "document.querySelector('#gitOverview')?.textContent.includes('Transactional local writes')"
                " && document.querySelector('[data-git-stage-path=\"browser-write.txt\"]')",
                timeout=30000,
            )
            overview = page.locator("#gitOverview").inner_text()
            assert "No network" in overview or "no network" in overview
            assert "No transactional Git write receipts yet" in overview

            page.check('[data-git-stage-path="browser-write.txt"]')
            page.click('[data-git-write="stage"]')
            page.wait_for_function(
                "document.querySelector('#gitOverview')?.textContent.includes('stage')"
                " && document.querySelector('#gitOverview')?.textContent.includes('receipt verified')",
                timeout=30000,
            )
            assert run_git("diff", "--cached", "--name-only") == "browser-write.txt"

            page.click('[data-git-write="commit"]')
            page.wait_for_function(
                "document.querySelector('#gitOverview')?.textContent.includes('Commit from transactional browser UI')",
                timeout=30000,
            )
            assert run_git("log", "-1", "--format=%s") == "Commit from transactional browser UI"
            assert run_git("log", "-1", "--format=%an <%ae>") == "Rooke Poole <browser-owner@example.invalid>"

            page.click('[data-git-write="create_branch"]')
            page.wait_for_function(
                "document.querySelector('#gitOverview')?.textContent.includes('feature/browser-transaction')",
                timeout=30000,
            )
            current_branch = run_git("branch", "--show-current")
            assert current_branch != "feature/browser-transaction"
            assert run_git("rev-parse", "feature/browser-transaction") == run_git("rev-parse", "HEAD")

            page.click('[data-git-write="create_tag"]')
            page.wait_for_function(
                "document.querySelector('#gitOverview')?.textContent.includes('browser-v0.5.2')",
                timeout=30000,
            )
            assert run_git("rev-parse", "browser-v0.5.2") == run_git("rev-parse", "HEAD")
            assert len(app.git_writes.list_receipts(record["id"], limit=10)) == 4
            assert not errors, errors
            browser.close()
        print("ForgeTrace v0.5.2 transactional local Git write owner browser workflow: PASS")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
