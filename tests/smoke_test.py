from pathlib import Path
import re

root = Path(__file__).resolve().parents[1]
html = (root / "index.html").read_text(encoding="utf-8")
server = (root / "server.py").read_text(encoding="utf-8")
registry = (root / "forgetrace" / "registry.py").read_text(encoding="utf-8")
repository = (root / "forgetrace" / "repository.py").read_text(encoding="utf-8")
web = (root / "forgetrace" / "web.py").read_text(encoding="utf-8")

for element_id in [
    "activeRepoSelect", "repoForm", "fileInput", "folderInput", "tree", "code",
    "repoUploadFilesChoice", "repoUploadFolderChoice", "repoPathChoice",
    "newRepoFilesInput", "newRepoFolderInput", "repoPathInput",
    "saveBtn", "snapshotBtn", "activity", "commits", "relinkBtn", "unregisterBtn",
]:
    assert f'id="{element_id}"' in html, f"missing UI element #{element_id}"

for route in [
    "/api/v1/repositories", "/api/v1/repositories/managed", "/api/v1/active-repository", "/state", "/upload",
    "/file", "/folder", "/rename", "/commit", "/checkout", "/relink", "/export",
]:
    assert route in web or route in html, f"missing API route fragment {route}"

for capability in [
    "write_file", "create_folder", "rename_path", "delete_path", "create_commit",
    "restore_commit", "export_zip", "ensure_identity",
]:
    assert re.search(rf"def\s+{capability}\s*\(", repository), f"missing repository capability {capability}"

for capability in [
    "create_managed_repository", "register_repository", "list_repositories", "set_active", "unregister", "relink",
    "repository_service",
]:
    assert re.search(rf"def\s+{capability}\s*\(", registry), f"missing registry capability {capability}"

assert "webkitdirectory" in html
assert "sqlite3" in registry
assert "0001_repository_registry" in registry
assert "APP_VERSION" in web
assert "from forgetrace.app import main" in server
assert ".forgetrace" in repository
assert "hashlib.sha256" in repository
print("ForgeTrace multi-repository smoke test: PASS")
