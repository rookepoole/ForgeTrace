from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from forgetrace.app import build_application
from forgetrace.errors import ForgeTraceError
from forgetrace.repository import ForgeTraceRepository
from forgetrace.security_events import SecurityEventError

ROOT = Path(__file__).resolve().parents[1]


def access_denied() -> PermissionError:
    error = PermissionError(13, "Access is denied")
    error.winerror = 5
    return error


class WindowsDeletionIntentRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="forgetrace-v0512-delete-intent-"))
        self.app = build_application(ROOT, self.root / "data")
        self.record = self.app.registry.create_managed_repository(
            name="Intent Delete", description="v0.5.1.2 fixture", author="Rooke Poole"
        )
        self.repository_id = self.record["id"]
        self.repository_path = Path(self.record["path"])
        self.app.registry.repository_service(self.repository_id).write_file(
            "example.txt", b"delete intent bytes", "Rooke Poole", "fixture", uploaded=True
        )

    def tearDown(self) -> None:
        if self.app.gateway:
            self.app.gateway.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_parent_move_releases_repository_local_handle_after_external_intent(self) -> None:
        registry = self.app.registry
        real_move = registry._replace_directory_with_retry
        observed = {"move": False}

        def inspect_move(source: Path, destination: Path) -> None:
            if Path(source) == self.repository_path:
                observed["move"] = True
                lock_key = os.path.normcase(str(self.repository_path.resolve()))
                repository_lock = ForgeTraceRepository._workspace_locks[lock_key]
                self.assertIsNone(repository_lock._file_lock)
                self.assertTrue(registry.repository_deletion_pending(self.repository_id))
                with self.assertRaises(ForgeTraceError) as blocked:
                    registry.repository_service(self.repository_id)
                self.assertEqual("repository_delete_in_progress", blocked.exception.code)
            real_move(Path(source), Path(destination))

        with mock.patch.object(registry, "_replace_directory_with_retry", side_effect=inspect_move):
            result = registry.delete_managed_repository(self.repository_id)

        self.assertTrue(observed["move"])
        self.assertTrue(result["filesDeleted"])
        self.assertFalse(registry.repository_deletion_pending(self.repository_id))

    def test_persistent_external_blocker_is_named_and_intent_rolls_back(self) -> None:
        registry = self.app.registry

        with mock.patch.object(registry, "_replace_directory_with_retry", side_effect=access_denied()), mock.patch(
            "forgetrace.registry.windows_locking_processes",
            return_value=[{"pid": 8124, "name": "Code.exe", "service": "", "restartable": True}],
        ):
            with self.assertRaises(ForgeTraceError) as blocked:
                registry.delete_managed_repository(self.repository_id)

        self.assertEqual("repository_delete_path_busy", blocked.exception.code)
        self.assertIn("Code.exe (PID 8124)", str(blocked.exception))
        self.assertEqual("Code.exe", blocked.exception.details["blockingProcesses"][0]["name"])
        self.assertFalse(registry.repository_deletion_pending(self.repository_id))
        self.assertTrue(self.repository_path.is_dir())
        self.assertIn(
            self.repository_id,
            {item["id"] for item in registry.list_repositories()["repositories"]},
        )
        self.assertEqual([], list(registry.repository_deletion_journals_dir.glob("delete-*.json")))

    @unittest.skipUnless(os.name == "nt", "Physical Windows v0.5.1.2 deletion acceptance")
    def test_physical_windows_external_intent_delete_transaction(self) -> None:
        result = self.app.registry.delete_managed_repository(self.repository_id)
        self.assertTrue(result["filesDeleted"])
        self.assertFalse(self.repository_path.exists())
        self.assertFalse(self.app.registry.repository_deletion_pending(self.repository_id))
        self.assertNotIn(
            self.repository_id,
            {item["id"] for item in self.app.registry.list_repositories()["repositories"]},
        )

    def test_orphaned_external_intent_is_cleared_during_startup_recovery(self) -> None:
        registry = self.app.registry
        registry._write_deletion_intent(
            self.repository_id,
            deletion_id="delete-" + "c" * 32,
            name=self.record["name"],
            original_path=str(self.repository_path),
        )
        self.assertTrue(registry.repository_deletion_pending(self.repository_id))

        reopened = type(registry)(ROOT, self.root / "data")
        self.assertFalse(reopened.repository_deletion_pending(self.repository_id))
        self.assertEqual(1, reopened.startup_repository_deletion_recovery_report["clearedIntents"])
        self.assertEqual(self.repository_id, reopened.repository_service(self.repository_id).repository_id)


class SecurityViewerResilienceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="forgetrace-v0512-security-"))
        self.app = build_application(ROOT, self.root / "data")
        self.app.security_events.append(
            category="security",
            action="viewer_resilience_fixture",
            outcome="success",
            surface="owner",
            details={"fixture": True},
        )

    def tearDown(self) -> None:
        if self.app.gateway:
            self.app.gateway.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_auxiliary_history_failure_returns_degraded_status_and_events_remain_queryable(self) -> None:
        ledger = self.app.security_events
        with mock.patch.object(ledger, "list_segments", side_effect=PermissionError("history denied")):
            status = ledger.operational_status()
        self.assertTrue(status["degraded"])
        self.assertTrue(any(item["component"] == "segment_inventory" for item in status["errors"]))

        result = ledger.query(limit=100)
        self.assertTrue(result["integrity"]["healthy"])
        self.assertTrue(any(item["action"] == "viewer_resilience_fixture" for item in result["events"]))

    def test_security_ui_loads_primary_events_before_auxiliary_history(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("await loadSecurityEvents();\n      await loadSecurityHistory();", html)
        self.assertNotIn("Promise.all([loadSecurityEvents(),loadSecurityHistory()])", html)
        self.assertIn("The primary event list remains available", html)


if __name__ == "__main__":
    unittest.main()
