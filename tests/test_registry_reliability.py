from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from forgetrace.app import build_application
from forgetrace.constants import MAX_REQUEST_BYTES
from forgetrace.errors import ForgeTraceError
from forgetrace.registry import MIGRATIONS, RepositoryRegistry
from forgetrace.web import create_server


ROOT = Path(__file__).resolve().parents[1]


class RegistryReliabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-reliability-"))
        self.registry = RepositoryRegistry(ROOT, self.temp / "data")
        self.workspace = self.temp / "repos" / "alpha"
        self.record = self.registry.register_repository(
            path=str(self.workspace), name="Alpha", author="Rooke Poole", create_directory=True
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_v1_registry_migrates_tags_collection_and_limits(self) -> None:
        legacy_dir = self.temp / "legacy-data"
        legacy_dir.mkdir(parents=True)
        db_path = legacy_dir / "registry.sqlite3"
        connection = sqlite3.connect(db_path)
        connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL)")
        connection.executescript(MIGRATIONS[0][2])
        connection.execute("INSERT INTO schema_migrations(version, name, applied_at) VALUES (1, ?, ?)", (MIGRATIONS[0][1], "2026-07-24T00:00:00Z"))
        legacy_workspace = self.temp / "legacy-workspace"
        legacy_workspace.mkdir()
        repository_id = "legacy-repository-id"
        now = "2026-07-24T00:00:00Z"
        connection.execute(
            """INSERT INTO repositories(id,name,description,path,canonical_path,metadata_mode,default_author,favorite,tags_json,collection_name,created_at,updated_at,last_opened_at) VALUES (?,?,?,?,?,'embedded',?,1,?,?,?,?,'')""",
            (repository_id, "Legacy", "", str(legacy_workspace), str(legacy_workspace), "Rooke Poole", '["old-tag"]', "Legacy Collection", now, now),
        )
        connection.commit(); connection.close()
        migrated = RepositoryRegistry(ROOT, legacy_dir)
        record = migrated.get_repository(repository_id)
        self.assertEqual(MAX_REQUEST_BYTES, record["uploadLimitBytes"])
        self.assertEqual(["old-tag"], record["tags"])
        self.assertEqual("Legacy Collection", record["collections"][0]["name"])

    def test_settings_sync_and_per_repository_upload_limit(self) -> None:
        updated = self.registry.update_settings(
            self.record["id"],
            name="Alpha Prime",
            description="Local-first project",
            default_author="Rooke Poole",
            upload_limit_bytes=1024 * 1024,
        )
        self.assertEqual("Alpha Prime", updated["name"])
        self.assertEqual(1024 * 1024, updated["uploadLimitBytes"])
        state = self.registry.repository_service(self.record["id"]).load_state()
        self.assertEqual("Alpha Prime", state["repository"]["name"])
        with self.assertRaises(ForgeTraceError) as raised:
            self.registry.repository_service(self.record["id"]).write_file(
                "too-large.bin", b"x" * (1024 * 1024 + 1), "Rooke Poole", "", uploaded=True
            )
        self.assertEqual("repository_upload_limit_exceeded", raised.exception.code)

    def test_tags_collections_saved_filters_and_filtering(self) -> None:
        work = self.registry.create_collection(name="Work", description="Active work")
        research = self.registry.create_collection(name="Research")
        self.registry.set_repository_organization(
            self.record["id"], tags=["Python", "local first", "python"], collection_ids=[work["id"], research["id"]]
        )
        library = self.registry.list_library()
        self.assertEqual(2, len(library["collections"]))
        self.assertEqual(["local first", "Python"], self.registry.get_repository(self.record["id"])["tags"])
        self.assertEqual(1, len(self.registry.list_repositories(tag="python")["repositories"]))
        self.assertEqual(1, len(self.registry.list_repositories(collection_id=work["id"])["repositories"]))
        renamed = self.registry.update_collection(work["id"], name="Current Work", description="Renamed")
        self.assertEqual("Current Work", renamed["name"])
        deleted_collection = self.registry.delete_collection(research["id"])
        self.assertEqual(research["id"], deleted_collection["deleted"])
        self.assertEqual(1, len(self.registry.get_repository(self.record["id"])["collections"]))
        saved = self.registry.save_filter(name="Python work", query={"tag": "Python", "collectionId": work["id"]})
        self.assertEqual("Python work", saved["name"])
        self.assertEqual(1, len(self.registry.list_library()["savedFilters"]))
        deleted = self.registry.delete_filter(saved["id"])
        self.assertEqual(saved["id"], deleted["deleted"])

    def test_export_import_backup_and_doctor_discovery(self) -> None:
        collection = self.registry.create_collection(name="Portable")
        self.registry.set_repository_organization(
            self.record["id"], tags=["backup"], collection_ids=[collection["id"]]
        )
        payload = self.registry.export_registry()
        self.assertEqual("forgetrace-registry-export", payload["format"])
        backup = self.registry.create_backup("test")
        self.assertTrue(Path(backup["path"]).is_file())

        imported = RepositoryRegistry(ROOT, self.temp / "imported-data")
        report = imported.import_registry(payload)
        self.assertEqual(1, report["added"])
        imported_record = imported.get_repository(self.record["id"])
        self.assertEqual(["backup"], imported_record["tags"])
        self.assertEqual("Portable", imported_record["collections"][0]["name"])

        self.registry.unregister(self.record["id"])
        report = self.registry.doctor(repair=False, scan_roots=[self.temp / "repos"])
        self.assertIn("unregistered_repository_discovered", [issue["code"] for issue in report["issues"]])
        repaired = self.registry.doctor(repair=True, scan_roots=[self.temp / "repos"])
        self.assertIn("registered_discovered_repository", [action["action"] for action in repaired["actions"]])
        self.assertEqual(1, len(self.registry.list_repositories()["repositories"]))
        self.assertIsNotNone(repaired["backup"])

    def test_cli_doctor_export_import_and_backup(self) -> None:
        export_path = self.temp / "registry.json"
        command_base = [sys.executable, str(ROOT / "server.py")]
        doctor = subprocess.run(
            command_base + ["doctor", "--data-dir", str(self.temp / "data"), "--json"],
            cwd=ROOT, text=True, capture_output=True, check=True,
        )
        self.assertTrue(json.loads(doctor.stdout)["healthy"])
        subprocess.run(
            command_base + ["registry-export", str(export_path), "--data-dir", str(self.temp / "data")],
            cwd=ROOT, text=True, capture_output=True, check=True,
        )
        self.assertTrue(export_path.is_file())
        imported_dir = self.temp / "cli-import"
        imported = subprocess.run(
            command_base + ["registry-import", str(export_path), "--data-dir", str(imported_dir)],
            cwd=ROOT, text=True, capture_output=True, check=True,
        )
        self.assertEqual(1, json.loads(imported.stdout)["added"])
        backup = subprocess.run(
            command_base + ["backup", "--data-dir", str(imported_dir), "--label", "cli"],
            cwd=ROOT, text=True, capture_output=True, check=True,
        )
        self.assertTrue(Path(json.loads(backup.stdout)["path"]).is_file())


class RegistryReliabilityApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-reliability-api-"))
        app = build_application(ROOT, self.temp / "data")
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
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())

    def test_library_settings_backup_and_doctor_api(self) -> None:
        status, repository = self.request("POST", "/api/v1/repositories", {
            "path": str(self.temp / "repo"), "name": "API Repo", "author": "Rooke Poole",
            "createDirectory": True, "uploadLimitBytes": 2 * 1024 * 1024,
        })
        self.assertEqual(201, status)
        _, collection = self.request("POST", "/api/v1/collections", {"name": "API Collection"})
        _, organized = self.request("POST", f"/api/v1/repositories/{repository['id']}/organization", {
            "tags": ["api"], "collectionIds": [collection["id"]],
        })
        self.assertEqual(["api"], organized["tags"])
        _, settings = self.request("POST", f"/api/v1/repositories/{repository['id']}/settings", {
            "name": "Renamed API Repo", "description": "Settings work", "defaultAuthor": "Rooke Poole",
            "uploadLimitBytes": 1024 * 1024,
        })
        self.assertEqual("Renamed API Repo", settings["name"])
        _, library = self.request("GET", "/api/v1/library")
        self.assertEqual(1, len(library["collections"]))
        status, backup = self.request("POST", "/api/v1/registry/backup", {"label": "api"})
        self.assertEqual(201, status)
        self.assertTrue(Path(backup["path"]).is_file())
        _, export = self.request("GET", "/api/v1/registry/export")
        self.assertEqual("forgetrace-registry-export", export["format"])
        _, doctor = self.request("GET", "/api/v1/doctor")
        self.assertTrue(doctor["healthy"])
        legacy_request = urllib.request.Request(self.base + "/api/status", method="GET")
        with urllib.request.urlopen(legacy_request, timeout=10) as legacy_response:
            self.assertEqual("true", legacy_response.headers.get("Deprecation"))

        query = urllib.parse.urlencode({"path": "too-large.bin", "author": "Rooke Poole"})
        request = urllib.request.Request(
            self.base + f"/api/v1/repositories/{repository['id']}/upload?{query}",
            data=b"x" * (1024 * 1024 + 1), method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(413, raised.exception.code)
        payload = json.loads(raised.exception.read())
        self.assertEqual("request_too_large", payload["code"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
