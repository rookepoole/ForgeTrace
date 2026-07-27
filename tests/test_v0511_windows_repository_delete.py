from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from forgetrace.app import build_application
from forgetrace.errors import ForgeTraceError
from forgetrace.locks import _windows_lock_open_parameters
from forgetrace.web import create_server

ROOT = Path(__file__).resolve().parents[1]


def access_denied() -> PermissionError:
    error = PermissionError(13, "Access is denied")
    error.winerror = 5
    return error


class WindowsLockContractTest(unittest.TestCase):
    def test_windows_lock_handle_allows_parent_directory_rename(self) -> None:
        parameters = _windows_lock_open_parameters(create=True)
        self.assertEqual(4, parameters["creation_disposition"])
        self.assertEqual(0x00000004, parameters["share_mode"] & 0x00000004)
        self.assertEqual(
            0x00000001 | 0x00000002 | 0x00000004,
            parameters["share_mode"],
        )


class WindowsRepositoryDeletionRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="forgetrace-v0511-windows-delete-"))
        self.data_dir = self.root / "data"
        self.app = build_application(ROOT, self.data_dir)
        self.record = self.app.registry.create_managed_repository(
            name="Windows Delete Regression",
            description="WinError 5 regression fixture",
            author="Rooke Poole",
        )
        self.repository_id = self.record["id"]
        self.repository_path = Path(self.record["path"])
        self.app.registry.repository_service(self.repository_id).write_file(
            "example.txt",
            b"preserve until atomic staging succeeds",
            "Rooke Poole",
            "WinError 5 fixture",
            uploaded=True,
        )

    def tearDown(self) -> None:
        if self.app.gateway:
            self.app.gateway.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def _is_repository_stage_move(self, source: object, destination: object) -> bool:
        return (
            Path(source) == self.repository_path
            and Path(destination).parent == self.app.registry.repository_deletion_staging_dir
        )

    def test_transient_windows_access_denied_is_retried(self) -> None:
        real_replace = os.replace
        attempts = 0

        def flaky_replace(source, destination):
            nonlocal attempts
            if self._is_repository_stage_move(source, destination):
                attempts += 1
                if attempts < 3:
                    raise access_denied()
            return real_replace(source, destination)

        with mock.patch("forgetrace.registry.os.replace", side_effect=flaky_replace), mock.patch(
            "forgetrace.registry.time.sleep", return_value=None
        ):
            result = self.app.registry.delete_managed_repository(self.repository_id)

        self.assertEqual(3, attempts)
        self.assertTrue(result["filesDeleted"])
        self.assertFalse(self.repository_path.exists())
        self.assertNotIn(
            self.repository_id,
            {item["id"] for item in self.app.registry.list_repositories()["repositories"]},
        )

    def test_persistent_windows_access_denied_is_specific_and_non_destructive(self) -> None:
        real_replace = os.replace

        def denied_replace(source, destination):
            if self._is_repository_stage_move(source, destination):
                raise access_denied()
            return real_replace(source, destination)

        with mock.patch("forgetrace.registry.os.replace", side_effect=denied_replace), mock.patch(
            "forgetrace.registry.time.sleep", return_value=None
        ):
            with self.assertRaises(ForgeTraceError) as blocked:
                self.app.registry.delete_managed_repository(self.repository_id)

        self.assertEqual("repository_delete_path_busy", blocked.exception.code)
        self.assertEqual(423, int(blocked.exception.status))
        self.assertEqual(5, blocked.exception.details["winError"])
        self.assertTrue(self.repository_path.is_dir())
        self.assertEqual(
            b"preserve until atomic staging succeeds",
            (self.repository_path / "example.txt").read_bytes(),
        )
        self.assertIn(
            self.repository_id,
            {item["id"] for item in self.app.registry.list_repositories()["repositories"]},
        )
        self.assertNotIn(self.repository_id, self.app.registry.deleted_repository_ids())
        self.assertEqual([], list(self.app.registry.repository_deletion_journals_dir.glob("delete-*.json")))

    def test_owner_api_returns_recoverable_423_instead_of_unexpected_server_error(self) -> None:
        owner = create_server(self.app, "127.0.0.1", 0, surface="owner")
        thread = threading.Thread(target=owner.serve_forever, daemon=True)
        thread.start()
        real_replace = os.replace

        def denied_replace(source, destination):
            if self._is_repository_stage_move(source, destination):
                raise access_denied()
            return real_replace(source, destination)

        try:
            with mock.patch("forgetrace.registry.os.replace", side_effect=denied_replace), mock.patch(
                "forgetrace.registry.time.sleep", return_value=None
            ):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", owner.server_address[1], timeout=30
                )
                connection.request(
                    "DELETE",
                    f"/api/v1/repositories/{self.repository_id}/delete-managed?actor=Rooke%20Poole",
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
            self.assertEqual(423, response.status)
            self.assertEqual("repository_delete_path_busy", payload["code"])
            self.assertNotIn("Unexpected server error", payload.get("error", ""))
            self.assertTrue(self.repository_path.is_dir())
        finally:
            owner.shutdown()
            owner.server_close()
            thread.join(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Physical Windows rename acceptance")
    def test_physical_windows_parent_rename_succeeds_while_repository_lock_is_held(self) -> None:
        service = self.app.registry.repository_service(self.repository_id)
        destination = self.app.registry.repository_deletion_staging_dir / "physical-windows-lock-test"
        with service.lock:
            os.replace(self.repository_path, destination)
            self.assertTrue(destination.is_dir())
            os.replace(destination, self.repository_path)
        self.assertTrue(self.repository_path.is_dir())


if __name__ == "__main__":
    unittest.main()
