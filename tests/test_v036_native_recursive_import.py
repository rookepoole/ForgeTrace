from __future__ import annotations

import os
import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from forgetrace.app import build_application
from forgetrace.errors import RepositoryError
from forgetrace.native_picker import pick_local_folder
from forgetrace.repository import ForgeTraceRepository
from forgetrace.web import create_server

ROOT = Path(__file__).resolve().parents[1]


class NativeRecursiveImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="forgetrace-v036-")
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.repository = ForgeTraceRepository(ROOT, self.workspace, "repo-v036")
        self.repository.initialize("Native Import", "", "Tester")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_source(self) -> tuple[Path, dict[str, bytes]]:
        source = self.root / "SelectedProject"
        expected = {
            "root.txt": b"root",
            "src/main.py": b"main",
            "src/features/auth/templates/forms/sign-in.html": b"deep",
            "assets/icons/ui/toolbar/actions/add.svg": b"<svg/>",
            "docs/reference/api/v1/endpoints/upload.md": b"# upload",
            "one/two/three/four/five/six/deep.bin": b"deepest",
        }
        for relative, content in expected.items():
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (source / "empty" / "nested" / "folder").mkdir(parents=True)
        (source / ".forgetrace").mkdir()
        (source / ".forgetrace" / "state.json").write_text("must not import", encoding="utf-8")
        return source, expected

    def test_native_import_preserves_every_descendant_with_root(self) -> None:
        source, expected = self.make_source()
        result = self.repository.import_local_folder(str(source), "Tester", include_root=True)
        self.assertEqual(len(expected), result["fileCount"])
        self.assertIn("SelectedProject/empty/nested/folder", result["folders"])
        self.assertIn("SelectedProject/.forgetrace", result["skippedMetadata"])
        for relative, content in expected.items():
            destination = self.workspace / "SelectedProject" / relative
            self.assertTrue(destination.is_file(), relative)
            self.assertEqual(content, destination.read_bytes())
        self.assertFalse((self.workspace / "SelectedProject" / ".forgetrace").exists())
        tree = {item["path"]: item["type"] for item in self.repository.tree()}
        self.assertEqual("file", tree["SelectedProject/one/two/three/four/five/six/deep.bin"])
        self.assertEqual("folder", tree["SelectedProject/empty/nested/folder"])

    def test_native_import_without_root_populates_new_repository(self) -> None:
        source, expected = self.make_source()
        result = self.repository.import_local_folder(str(source), "Tester", include_root=False)
        self.assertEqual(len(expected), result["fileCount"])
        for relative, content in expected.items():
            self.assertEqual(content, (self.workspace / relative).read_bytes())
        self.assertFalse((self.workspace / "SelectedProject").exists())

    def test_import_rejects_repository_importing_itself(self) -> None:
        with self.assertRaises(RepositoryError):
            self.repository.import_local_folder(str(self.workspace), "Tester", include_root=True)

    def test_folder_manifest_is_created_in_one_operation(self) -> None:
        result = self.repository.ensure_folders(
            ["Project", "Project/a", "Project/a/b", "Project/a/b/c"], "Tester"
        )
        self.assertEqual(4, result["folderCount"])
        self.assertTrue((self.workspace / "Project" / "a" / "b" / "c").is_dir())
        state = self.repository.load_state()
        events = [item for item in state["contributions"] if item["action"] == "folders_imported"]
        self.assertEqual(1, len(events))

    def test_headless_picker_override_returns_exact_folder(self) -> None:
        source, _expected = self.make_source()
        with mock.patch.dict(os.environ, {"FORGETRACE_TEST_PICK_FOLDER": str(source)}):
            self.assertEqual(str(source.resolve()), pick_local_folder())


class NativeImportSurfaceTest(unittest.TestCase):
    def test_native_import_is_primary_and_browser_import_is_fallback(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="importLocalFolderBtn"', html)
        self.assertIn('id="uploadFolderBtn"', html)
        self.assertIn("importCompleteFolder", html)
        self.assertIn("/api/v1/system/pick-folder", html)
        self.assertIn("/import-jobs", html)
        self.assertLess(
            html.index("if(typeof window.showDirectoryPicker==='function')"),
            html.index("if('webkitdirectory' in fallbackInput)"),
        )
        self.assertIn("expectedRepositoryFolders", html)
        self.assertIn("`${base}/folder`", html)
        self.assertIn("Imported and verified", html)


class NativeImportApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-v036-api-"))
        self.source = self.temp / "PickedFolder"
        deepest = self.source / "a" / "b" / "c" / "d" / "deep.txt"
        deepest.parent.mkdir(parents=True)
        deepest.write_text("deep", encoding="utf-8")
        (self.source / "root.txt").write_text("root", encoding="utf-8")
        self.environment = mock.patch.dict(
            os.environ, {"FORGETRACE_TEST_PICK_FOLDER": str(self.source)}
        )
        self.environment.start()
        app = build_application(ROOT, self.temp / "app-data")
        self.server = create_server(app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.environment.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def request(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_owner_picker_and_direct_disk_import_routes(self) -> None:
        picked = self.request("/api/v1/system/pick-folder", {})
        self.assertTrue(picked["available"])
        self.assertEqual(str(self.source.resolve()), picked["path"])
        record = self.request("/api/v1/repositories/managed", {
            "name": "Native API", "description": "", "author": "Tester"
        })
        result = self.request(
            f"/api/v1/repositories/{record['id']}/import-local-folder",
            {"path": picked["path"], "includeRoot": False, "author": "Tester"},
        )
        self.assertEqual(2, result["fileCount"])
        destination = Path(record["path"])
        self.assertEqual("deep", (destination / "a/b/c/d/deep.txt").read_text(encoding="utf-8"))
        self.assertEqual("root", (destination / "root.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
