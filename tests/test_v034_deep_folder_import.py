from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from forgetrace.app import build_application
from forgetrace.web import create_server

ROOT = Path(__file__).resolve().parents[1]


class DeepFolderImportApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-v034-deep-folder-"))
        self.app = build_application(ROOT, self.temp / "data")
        self.server = create_server(self.app, "127.0.0.1", 0, surface="owner")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.temp, ignore_errors=True)

    def request(self, method: str, path: str, *, payload=None, body: bytes | None = None):
        headers = {}
        data = body
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            if "application/json" in response.headers.get("Content-Type", ""):
                return json.loads(raw)
            return raw

    def test_every_descendant_file_and_folder_is_preserved(self) -> None:
        record = self.request(
            "POST",
            "/api/v1/repositories/managed",
            payload={"name": "Recursive Import", "author": "Rooke Poole"},
        )
        expected = {
            "root.txt": b"root",
            "src/main.py": b"print('root')\n",
            "src/features/auth/login.py": b"def login(): pass\n",
            "src/features/auth/templates/forms/sign-in.html": b"<form></form>\n",
            "assets/icons/ui/toolbar/actions/add.svg": b"<svg></svg>\n",
            "docs/reference/api/v1/endpoints/upload.md": b"# Upload\n",
        }
        for relative_path, content in expected.items():
            query = urllib.parse.urlencode(
                {"path": relative_path, "author": "Rooke Poole", "message": "Recursive folder import"}
            )
            self.request(
                "POST",
                f"/api/v1/repositories/{record['id']}/upload?{query}",
                body=content,
            )

        state = self.request("GET", f"/api/v1/repositories/{record['id']}/state")
        tree = {entry["path"]: entry["type"] for entry in state["tree"]}
        expected_folders = {
            "src",
            "src/features",
            "src/features/auth",
            "src/features/auth/templates",
            "src/features/auth/templates/forms",
            "assets",
            "assets/icons",
            "assets/icons/ui",
            "assets/icons/ui/toolbar",
            "assets/icons/ui/toolbar/actions",
            "docs",
            "docs/reference",
            "docs/reference/api",
            "docs/reference/api/v1",
            "docs/reference/api/v1/endpoints",
        }
        self.assertTrue(expected_folders.issubset(tree.keys()))
        self.assertTrue(all(tree[path] == "folder" for path in expected_folders))
        self.assertTrue(all(tree[path] == "file" for path in expected))

        repository_path = Path(record["path"])
        for relative_path, content in expected.items():
            self.assertEqual(content, (repository_path / relative_path).read_bytes())


class DeepFolderImportSurfaceTest(unittest.TestCase):
    def test_recursive_picker_verification_and_single_folder_toggle_are_present(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        for expected in (
            "collectDirectoryRecursively(rootHandle)",
            "for await (const pair of directoryHandle.entries())",
            "if('webkitdirectory' in fallbackInput){fallbackInput.click();return;}",
            "const files=Array.from(input.files||[])",
            "await uploadSelection(selection)",
            "verifyRepositoryImport(repositoryId,filePaths,folderPaths=[])",
            "automatic verification retry",
            "Import verified: all",
            "Every discovered nested path was confirmed in the repository tree.",
            "ForgeTrace recursively imports every discovered descendant path",
        ):
            self.assertIn(expected, html)

        folder_branch = html.split("if (type==='folder') {", 1)[1].split("try { appState.selectedPath", 1)[0]
        self.assertEqual(1, folder_branch.count("appState.expandedFolders.has(path)"))
        self.assertIn("input.value='';input.disabled=false", html)
        self.assertIn("rememberExpandedImport(repositoryId", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
