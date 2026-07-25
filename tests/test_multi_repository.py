from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

from forgetrace.app import build_application
from forgetrace.registry import RepositoryRegistry
from forgetrace.web import create_server


ROOT = Path(__file__).resolve().parents[1]


class MultiRepositoryRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-test-"))
        self.data_dir = self.temp / "app-data"
        self.repo_a = self.temp / "repositories" / "alpha"
        self.repo_b = self.temp / "other-drive" / "beta"
        self.registry = RepositoryRegistry(ROOT, self.data_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_registry_handles_one_hundred_repository_paths(self) -> None:
        identifiers = set()
        for index in range(100):
            record = self.registry.register_repository(
                path=str(self.temp / "many" / f"repo-{index:03d}"),
                name=f"Repository {index:03d}",
                author="Rooke Poole",
                create_directory=True,
            )
            identifiers.add(record["id"])
        listing = self.registry.list_repositories()["repositories"]
        self.assertEqual(100, len(listing))
        self.assertEqual(100, len(identifiers))
        self.assertTrue(all(record["status"] == "online" for record in listing))

    def test_managed_repository_names_are_cross_platform_safe(self) -> None:
        self.assertEqual("CON.txt-repository", self.registry._managed_repository_slug("CON.txt"))
        self.assertEqual("Project-Name", self.registry._managed_repository_slug("Project: Name"))
        self.assertEqual("repository", self.registry._managed_repository_slug("..."))

    def test_managed_repository_paths_are_local_unique_and_relinkable(self) -> None:
        first = self.registry.create_managed_repository(
            name="Uploaded Project", author="Rooke Poole"
        )
        second = self.registry.create_managed_repository(
            name="Uploaded Project", author="Rooke Poole"
        )
        managed_root = (self.data_dir / "managed-repositories").resolve()
        first_path = Path(first["path"]).resolve()
        second_path = Path(second["path"]).resolve()
        self.assertIn(managed_root, first_path.parents)
        self.assertIn(managed_root, second_path.parents)
        self.assertNotEqual(first_path, second_path)
        self.assertTrue((first_path / ".forgetrace" / "state.json").is_file())
        self.assertTrue((second_path / ".forgetrace" / "state.json").is_file())

    def test_isolation_restart_offline_and_relink(self) -> None:
        alpha = self.registry.register_repository(
            path=str(self.repo_a), name="Alpha", author="Rooke Poole", create_directory=True
        )
        beta = self.registry.register_repository(
            path=str(self.repo_b), name="Beta", author="Rooke Poole", create_directory=True
        )
        self.assertNotEqual(alpha["id"], beta["id"])

        alpha_service = self.registry.repository_service(alpha["id"])
        beta_service = self.registry.repository_service(beta["id"])
        alpha_service.write_file("alpha-only.txt", b"alpha", "Rooke Poole", "alpha upload", uploaded=True)
        self.assertTrue((self.repo_a / "alpha-only.txt").is_file())
        self.assertFalse((self.repo_b / "alpha-only.txt").exists())

        beta_service.write_file("beta-only.txt", b"beta", "Rooke Poole", "beta upload", uploaded=True)
        commit = beta_service.create_commit("Beta baseline", "Rooke Poole")
        self.assertFalse((self.repo_a / "beta-only.txt").exists())

        restarted = RepositoryRegistry(ROOT, self.data_dir)
        records = restarted.list_repositories()["repositories"]
        self.assertEqual(2, len(records))

        moved = self.temp / "relocated" / "beta"
        moved.parent.mkdir(parents=True)
        self.repo_b.rename(moved)
        self.assertEqual("offline", restarted.get_repository(beta["id"])["status"])

        relinked = restarted.relink(beta["id"], str(moved))
        self.assertEqual("online", relinked["status"])
        restored_state = restarted.repository_service(beta["id"]).load_state()
        self.assertEqual(commit["id"], restored_state["commits"][0]["id"])

        result = restarted.unregister(alpha["id"])
        self.assertFalse(result["filesDeleted"])
        self.assertTrue((self.repo_a / "alpha-only.txt").is_file())


class MultiRepositoryApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-api-test-"))
        app = build_application(ROOT, self.temp / "app-data")
        self.server = create_server(app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.temp, ignore_errors=True)

    def request(self, method: str, path: str, payload=None, body: bytes | None = None):
        headers = {}
        data = body
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read()
            return json.loads(raw) if "application/json" in content_type else raw

    def test_managed_repository_api_accepts_uploaded_files_and_folder_paths(self) -> None:
        record = self.request("POST", "/api/v1/repositories/managed", {
            "name": "Browser Import", "description": "Created without an absolute path",
            "author": "Rooke Poole",
        })
        managed_root = (self.temp / "app-data" / "managed-repositories").resolve()
        repository_path = Path(record["path"]).resolve()
        self.assertIn(managed_root, repository_path.parents)

        for path, body in (("single.txt", b"one"), ("src/nested.txt", b"two")):
            query = urllib.parse.urlencode({"path": path, "author": "Rooke Poole"})
            self.request("POST", f"/api/v1/repositories/{record['id']}/upload?{query}", body=body)

        state = self.request("GET", f"/api/v1/repositories/{record['id']}/state")
        paths = {entry["path"] for entry in state["tree"]}
        self.assertIn("single.txt", paths)
        self.assertIn("src/nested.txt", paths)
        self.assertEqual(b"two", (repository_path / "src" / "nested.txt").read_bytes())

    def test_repository_scoped_api_does_not_leak_state(self) -> None:
        path_a = self.temp / "A"
        path_b = self.temp / "B"
        alpha = self.request("POST", "/api/v1/repositories", {
            "path": str(path_a), "name": "A", "author": "Rooke Poole", "createDirectory": True,
        })
        beta = self.request("POST", "/api/v1/repositories", {
            "path": str(path_b), "name": "B", "author": "Rooke Poole", "createDirectory": True,
        })

        query = urllib.parse.urlencode({"path": "only-a.txt", "author": "Rooke Poole"})
        self.request("POST", f"/api/v1/repositories/{alpha['id']}/upload?{query}", body=b"A")
        state_a = self.request("GET", f"/api/v1/repositories/{alpha['id']}/state")
        state_b = self.request("GET", f"/api/v1/repositories/{beta['id']}/state")
        self.assertIn("only-a.txt", [entry["path"] for entry in state_a["tree"]])
        self.assertNotIn("only-a.txt", [entry["path"] for entry in state_b["tree"]])

        self.request("PUT", f"/api/v1/repositories/{beta['id']}/file", {
            "path": "only-b.md", "content": "# Beta", "author": "Rooke Poole", "message": "create beta",
        })
        self.request("POST", f"/api/v1/repositories/{beta['id']}/commit", {
            "message": "Snapshot B", "author": "Rooke Poole",
        })
        state_a_after = self.request("GET", f"/api/v1/repositories/{alpha['id']}/state")
        state_b_after = self.request("GET", f"/api/v1/repositories/{beta['id']}/state")
        self.assertEqual(0, len(state_a_after["commits"]))
        self.assertEqual(1, len(state_b_after["commits"]))
        self.assertFalse((path_a / "only-b.md").exists())

        export_a = self.request("GET", f"/api/v1/repositories/{alpha['id']}/export")
        export_b = self.request("GET", f"/api/v1/repositories/{beta['id']}/export")
        with zipfile.ZipFile(BytesIO(export_a)) as archive_a:
            self.assertIn("only-a.txt", archive_a.namelist())
            self.assertNotIn("only-b.md", archive_a.namelist())
        with zipfile.ZipFile(BytesIO(export_b)) as archive_b:
            self.assertIn("only-b.md", archive_b.namelist())
            self.assertNotIn("only-a.txt", archive_b.namelist())


if __name__ == "__main__":
    unittest.main(verbosity=2)
