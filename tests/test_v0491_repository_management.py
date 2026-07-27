from __future__ import annotations

import contextlib
import http.client
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from forgetrace.app import build_application
from forgetrace.errors import ForgeTraceError
from forgetrace.registry import RepositoryRegistry
from forgetrace.web import create_server

ROOT = Path(__file__).resolve().parents[1]


class RepositoryManagementFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="forgetrace-v0491-management-"))
        self.data_dir = self.root / "data"
        self.app = build_application(ROOT, self.data_dir)
        self.record = self.app.registry.create_managed_repository(
            name="Delete Me", description="maintenance fixture", author="Rooke Poole"
        )
        self.repository_id = self.record["id"]
        self.repository_path = Path(self.record["path"])
        self.app.registry.repository_service(self.repository_id).write_file(
            "nested/example.txt", b"managed repository bytes", "Rooke Poole", "fixture", uploaded=True
        )

    def tearDown(self) -> None:
        if self.app.gateway:
            self.app.gateway.stop()
        shutil.rmtree(self.root, ignore_errors=True)


class RepositoryManagementServiceTest(RepositoryManagementFixture):
    def test_managed_delete_removes_directory_and_blocks_automatic_rediscovery(self) -> None:
        ghost = self.data_dir / "repositories" / "old-copy"
        ghost.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.repository_path, ghost)

        result = self.app.registry.delete_managed_repository(self.repository_id)
        self.assertTrue(result["filesDeleted"])
        self.assertTrue(result["tombstoned"])
        self.assertFalse(self.repository_path.exists())
        self.assertNotIn(
            self.repository_id,
            {item["id"] for item in self.app.registry.list_repositories()["repositories"]},
        )

        reopened = RepositoryRegistry(ROOT, self.data_dir)
        self.assertNotIn(
            self.repository_id,
            {item["id"] for item in reopened.list_repositories()["repositories"]},
        )
        self.assertGreaterEqual(reopened.startup_recovery_report["tombstoned"], 1)
        doctor = reopened.doctor(repair=True, scan_roots=[ghost.parent])
        self.assertTrue(any(
            issue.get("code") == "permanently_deleted_repository_discovered"
            and issue.get("repositoryId") == self.repository_id
            for issue in doctor["issues"]
        ))
        self.assertNotIn(
            self.repository_id,
            {item["id"] for item in reopened.list_repositories()["repositories"]},
        )

    def test_explicit_owner_registration_clears_deletion_tombstone(self) -> None:
        preserved = self.root / "preserved-copy"
        shutil.copytree(self.repository_path, preserved)
        self.app.registry.delete_managed_repository(self.repository_id)
        self.assertIn(self.repository_id, self.app.registry.deleted_repository_ids())

        restored = self.app.registry.register_repository(
            path=str(preserved), name="Explicitly restored", initialize=False
        )
        self.assertEqual(self.repository_id, restored["id"])
        self.assertNotIn(self.repository_id, self.app.registry.deleted_repository_ids())

    def test_missing_managed_repository_can_be_tombstoned_and_removed(self) -> None:
        shutil.rmtree(self.repository_path)
        self.assertEqual("offline", self.app.registry.get_repository(self.repository_id)["status"])

        result = self.app.registry.delete_managed_repository(self.repository_id)
        self.assertTrue(result["pathWasMissing"])
        self.assertFalse(result["filesDeleted"])
        self.assertIn(self.repository_id, self.app.registry.deleted_repository_ids())
        self.assertNotIn(
            self.repository_id,
            {item["id"] for item in self.app.registry.list_repositories()["repositories"]},
        )

    def test_empty_uninitialized_managed_repository_can_be_permanently_deleted(self) -> None:
        # Match the user-visible failure mode: all visible files and embedded
        # metadata were removed manually, leaving an empty managed directory.
        shutil.rmtree(self.repository_path)
        self.repository_path.mkdir()
        self.assertEqual("uninitialized", self.app.registry.get_repository(self.repository_id)["status"])

        result = self.app.registry.delete_managed_repository(self.repository_id)
        self.assertTrue(result["filesDeleted"])
        self.assertFalse(self.repository_path.exists())
        self.assertIn(self.repository_id, self.app.registry.deleted_repository_ids())

    def test_external_repository_cannot_be_recursively_deleted(self) -> None:
        external_path = self.root / "external-repository"
        external = self.app.registry.register_repository(
            path=str(external_path), name="External", initialize=True, create_directory=True
        )
        marker = external_path / "keep.txt"
        marker.write_text("must remain", encoding="utf-8")
        with self.assertRaises(ForgeTraceError) as blocked:
            self.app.registry.delete_managed_repository(external["id"])
        self.assertEqual("repository_not_managed", blocked.exception.code)
        self.assertEqual("must remain", marker.read_text(encoding="utf-8"))
        self.assertIn(external["id"], {item["id"] for item in self.app.registry.list_repositories()["repositories"]})

    def test_read_only_managed_repository_delete_is_blocked_before_move(self) -> None:
        self.app.registry.set_access_mode(self.repository_id, "read_only")
        with self.assertRaises(ForgeTraceError) as blocked:
            self.app.registry.delete_managed_repository(self.repository_id)
        self.assertEqual("repository_read_only", blocked.exception.code)
        self.assertTrue(self.repository_path.is_dir())
        self.assertIn(self.repository_id, {item["id"] for item in self.app.registry.list_repositories()["repositories"]})

    def test_interrupted_deletion_with_live_registry_row_rolls_back_on_restart(self) -> None:
        deletion_id = "delete-" + "a" * 32
        staged = self.app.registry.repository_deletion_staging_dir / deletion_id
        journal = self.app.registry.repository_deletion_journals_dir / f"{deletion_id}.json"
        os.replace(self.repository_path, staged)
        self.app.registry._write_deletion_tombstone(
            self.repository_id, name=self.record["name"], original_path=str(self.repository_path)
        )
        self.app.registry._atomic_write_json(journal, {
            "schemaVersion": 1,
            "deletionId": deletion_id,
            "repositoryId": self.repository_id,
            "name": self.record["name"],
            "originalPath": str(self.repository_path),
            "stagedPath": str(staged),
            "createdAt": "2026-07-26T00:00:00Z",
            "status": "staged",
        })

        reopened = RepositoryRegistry(ROOT, self.data_dir)
        self.assertTrue(self.repository_path.is_dir())
        self.assertFalse(staged.exists())
        self.assertFalse(journal.exists())
        self.assertNotIn(self.repository_id, reopened.deleted_repository_ids())
        self.assertEqual(1, reopened.startup_repository_deletion_recovery_report["rolledBack"])

    def test_interrupted_committed_deletion_finishes_cleanup_on_restart(self) -> None:
        deletion_id = "delete-" + "b" * 32
        staged = self.app.registry.repository_deletion_staging_dir / deletion_id
        journal = self.app.registry.repository_deletion_journals_dir / f"{deletion_id}.json"
        os.replace(self.repository_path, staged)
        self.app.registry._write_deletion_tombstone(
            self.repository_id, name=self.record["name"], original_path=str(self.repository_path)
        )
        self.app.registry._atomic_write_json(journal, {
            "schemaVersion": 1,
            "deletionId": deletion_id,
            "repositoryId": self.repository_id,
            "name": self.record["name"],
            "originalPath": str(self.repository_path),
            "stagedPath": str(staged),
            "createdAt": "2026-07-26T00:00:00Z",
            "status": "registry_removed",
        })
        self.app.registry.unregister(self.repository_id)

        reopened = RepositoryRegistry(ROOT, self.data_dir)
        self.assertFalse(staged.exists())
        self.assertFalse(journal.exists())
        self.assertIn(self.repository_id, reopened.deleted_repository_ids())
        self.assertEqual(1, reopened.startup_repository_deletion_recovery_report["finalized"])


class RepositoryManagementApiTest(RepositoryManagementFixture):
    def setUp(self) -> None:
        super().setUp()
        self.owner = create_server(self.app, "127.0.0.1", 0, surface="owner")
        self.gateway = create_server(self.app, "127.0.0.1", 0, surface="gateway")
        self.owner_thread = threading.Thread(target=self.owner.serve_forever, daemon=True)
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.owner_thread.start(); self.gateway_thread.start()

    def tearDown(self) -> None:
        for server, thread in ((self.owner, self.owner_thread), (self.gateway, self.gateway_thread)):
            server.shutdown(); server.server_close(); thread.join(timeout=3)
        super().tearDown()

    @staticmethod
    def request(server, method: str, path: str):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
        connection.request(method, path)
        response = connection.getresponse(); raw = response.read(); connection.close()
        return response.status, json.loads(raw) if raw else {}

    def _tamper_ledger(self) -> None:
        with contextlib.closing(sqlite3.connect(self.app.security_events.db_path)) as connection:
            connection.execute("DROP TRIGGER security_events_no_update")
            connection.execute("UPDATE security_events SET details_json='{}' WHERE sequence=1")
            connection.commit()

    def test_owner_delete_is_audited_and_gateway_is_denied(self) -> None:
        status, _ = self.request(
            self.gateway, "DELETE", f"/api/v1/repositories/{self.repository_id}/delete-managed?actor=Outside"
        )
        self.assertEqual(403, status)
        self.assertTrue(self.repository_path.exists())

        status, payload = self.request(
            self.owner, "DELETE", f"/api/v1/repositories/{self.repository_id}/delete-managed?actor=Rooke%20Poole"
        )
        self.assertEqual(200, status)
        self.assertEqual(self.repository_id, payload["deleted"])
        self.assertFalse(self.repository_path.exists())
        actions = {
            event["action"]
            for event in self.app.security_events.query(repository_id=self.repository_id, limit=100)["events"]
        }
        self.assertIn("managed_repository_delete_authorized", actions)
        self.assertIn("managed_repository_deleted", actions)

    def test_bad_ledger_blocks_delete_before_filesystem_or_registry_change(self) -> None:
        self._tamper_ledger()
        status, payload = self.request(
            self.owner, "DELETE", f"/api/v1/repositories/{self.repository_id}/delete-managed?actor=Rooke"
        )
        self.assertEqual(503, status)
        self.assertEqual("security_event_ledger_unavailable", payload["code"])
        self.assertTrue(self.repository_path.exists())
        self.assertIn(self.repository_id, {item["id"] for item in self.app.registry.list_repositories()["repositories"]})


class RepositoryManagementSurfaceTest(unittest.TestCase):
    def test_file_tree_is_larger_and_permanent_delete_is_explicit(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns:minmax(380px, 46%)", html)
        self.assertIn("height:clamp(500px,68vh,820px)", html)
        self.assertIn('id="deleteManagedRepositoryBtn"', html)
        self.assertIn("Type the repository name exactly", html)
        self.assertIn("delete-managed?actor=", html)
        self.assertIn("A deletion tombstone prevents automatic startup or Doctor recovery", html)


if __name__ == "__main__":
    unittest.main()
