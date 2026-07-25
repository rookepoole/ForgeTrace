from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from forgetrace.errors import ForgeTraceError
from forgetrace.registry import RepositoryRegistry

ROOT = Path(__file__).resolve().parents[1]


class SecurityAndRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-security-"))
        self.registry = RepositoryRegistry(ROOT, self.temp / "data")
        self.record = self.registry.register_repository(
            path=str(self.temp / "repository"),
            name="Security Fixture",
            author="Rooke Poole",
            create_directory=True,
        )
        self.service = self.registry.repository_service(self.record["id"])

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def assert_error_code(self, expected: str, callback) -> None:
        with self.assertRaises(ForgeTraceError) as raised:
            callback()
        self.assertEqual(expected, raised.exception.code)

    def test_path_traversal_and_metadata_are_blocked(self) -> None:
        with self.assertRaises(ForgeTraceError):
            self.service.write_file("../escape.txt", b"no", "Rooke Poole", "bad", uploaded=True)
        with self.assertRaises(ForgeTraceError):
            self.service.write_file(".forgetrace/state.json", b"no", "Rooke Poole", "bad", uploaded=True)
        self.assertFalse((self.temp / "escape.txt").exists())

    def test_duplicate_paths_are_rejected_without_rewriting_identity(self) -> None:
        state_before = json.loads(self.service.state_path.read_text(encoding="utf-8"))
        with self.assertRaises(ForgeTraceError) as raised:
            self.registry.register_repository(path=str(self.service.workspace), name="Duplicate")
        self.assertEqual("duplicate_repository_path", raised.exception.code)
        state_after = json.loads(self.service.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state_before["repository"]["id"], state_after["repository"]["id"])

    def test_atomic_state_backup_and_export_boundary(self) -> None:
        self.service.write_file("notes.txt", b"one", "Rooke Poole", "first", uploaded=True)
        self.service.write_file("notes.txt", b"two", "Rooke Poole", "second", uploaded=False)
        backup = self.service.state_path.with_suffix(".json.bak")
        self.assertTrue(backup.is_file())
        json.loads(backup.read_text(encoding="utf-8"))

        archive_data = self.service.export_zip(include_history=True)
        with zipfile.ZipFile(BytesIO(archive_data)) as archive:
            names = archive.namelist()
            self.assertIn("notes.txt", names)
            self.assertIn("FORGETRACE_HISTORY.json", names)
            self.assertFalse(any(name.startswith(".forgetrace/") for name in names))


if __name__ == "__main__":
    unittest.main(verbosity=2)
