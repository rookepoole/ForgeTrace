from __future__ import annotations

import contextlib
import http.client
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from forgetrace.app import build_application
from forgetrace.constants import APP_SCHEMA_VERSION
from forgetrace.errors import ForgeTraceError
from forgetrace.registry import MIGRATIONS, RepositoryRegistry
from forgetrace.web import create_server


ROOT = Path(__file__).resolve().parents[1]


class RegistryRestoreServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="forgetrace-v042-")
        self.root = Path(self.temp.name)
        self.registry = RepositoryRegistry(ROOT, self.root / "data")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_repository(self, name: str, folder: str | None = None) -> dict:
        return self.registry.register_repository(
            path=str(self.root / (folder or name.lower().replace(" ", "-"))),
            name=name,
            author="Rooke Poole",
            initialize=True,
            create_directory=True,
        )

    def test_replace_preview_restore_and_explicit_rollback(self) -> None:
        first = self.add_repository("First")
        backup = self.registry.create_backup("replace-source")
        second = self.add_repository("Second")

        preview = self.registry.preview_registry_restore(backup["name"], "replace")
        self.assertEqual(2, preview["current"]["repositoryCount"])
        self.assertEqual(1, preview["restored"]["repositoryCount"])
        self.assertEqual(1, preview["impact"]["repositoriesRemoved"])
        self.assertEqual("replace", preview["mode"])

        restored = self.registry.restore_registry_backup(
            backup["name"], "replace", preview["previewId"]
        )
        self.assertEqual("completed", restored["state"])
        self.assertTrue(restored["rollbackAvailable"])
        self.assertEqual([first["id"]], [item["id"] for item in self.registry.list_repositories()["repositories"]])
        self.assertTrue((self.registry.data_dir / "registry-restores" / "journals" / f"{restored['restoreId']}.json").is_file())

        rolled_back = self.registry.rollback_registry_restore(restored["restoreId"])
        self.assertEqual("rolled_back", rolled_back["state"])
        self.assertEqual(
            {first["id"], second["id"]},
            {item["id"] for item in self.registry.list_repositories()["repositories"]},
        )
        self.assertFalse(rolled_back["rollbackAvailable"])

    def test_merge_is_additive_and_preserves_live_authority(self) -> None:
        first = self.add_repository("Backup First")
        restored_only = self.add_repository("Restored Only")
        collection = self.registry.create_collection(name="Backup Collection")
        self.registry.set_repository_organization(
            first["id"], tags=["backup"], collection_ids=[collection["id"]]
        )
        self.registry.save_filter(name="Backup Filter", query={"tag": "backup"})
        backup = self.registry.create_backup("merge-source")

        self.registry.unregister(restored_only["id"])
        self.registry.update_settings(
            first["id"],
            name="Live First",
            description="Live description",
            default_author="Live Owner",
            upload_limit_bytes=2 * 1024 * 1024,
        )
        self.registry.set_repository_organization(first["id"], tags=["live"], collection_ids=[])
        live_only = self.add_repository("Live Only")

        preview = self.registry.preview_registry_restore(backup["name"], "merge")
        self.assertEqual(1, preview["impact"]["repositoriesAdded"])
        self.assertEqual(1, preview["impact"]["repositoriesPreserved"])
        restored = self.registry.restore_registry_backup(
            backup["name"], "merge", preview["previewId"]
        )
        self.assertEqual("completed", restored["state"])
        self.assertEqual(1, restored["report"]["repositoriesAdded"])
        self.assertEqual(1, restored["report"]["repositoriesPreserved"])

        live_first = self.registry.get_repository(first["id"])
        self.assertEqual("Live First", live_first["name"])
        self.assertEqual("Live description", live_first["description"])
        self.assertEqual({"backup", "live"}, set(live_first["tags"]))
        self.assertEqual(["Backup Collection"], [item["name"] for item in live_first["collections"]])
        repository_ids = {item["id"] for item in self.registry.list_repositories()["repositories"]}
        self.assertEqual({first["id"], restored_only["id"], live_only["id"]}, repository_ids)

    def test_preview_rejects_stale_newer_corrupt_and_unsafe_backups(self) -> None:
        self.add_repository("One")
        backup = self.registry.create_backup("valid")
        preview = self.registry.preview_registry_restore(backup["name"], "replace")
        self.add_repository("Two")
        with self.assertRaises(ForgeTraceError) as stale:
            self.registry.restore_registry_backup(backup["name"], "replace", preview["previewId"])
        self.assertEqual("registry_restore_preview_stale", stale.exception.code)
        self.assertEqual(2, len(self.registry.list_repositories()["repositories"]))

        with self.assertRaises(ForgeTraceError) as unsafe:
            self.registry.preview_registry_restore("../registry.sqlite3", "replace")
        self.assertEqual("invalid_registry_backup_name", unsafe.exception.code)

        newer = self.registry.backups_dir / "registry-newer.sqlite3"
        shutil.copy2(backup["path"], newer)
        with contextlib.closing(sqlite3.connect(newer)) as connection:
            connection.execute(
                "UPDATE application_state SET value='999' WHERE key='schema_version'"
            )
            connection.commit()
        with self.assertRaises(ForgeTraceError) as unsupported:
            self.registry.preview_registry_restore(newer.name, "replace")
        self.assertEqual("registry_backup_schema_newer", unsupported.exception.code)

        corrupt = self.registry.backups_dir / "registry-corrupt.sqlite3"
        corrupt.write_bytes(b"not a sqlite database")
        with self.assertRaises(ForgeTraceError) as invalid:
            self.registry.preview_registry_restore(corrupt.name, "replace")
        self.assertIn(
            invalid.exception.code,
            {"registry_backup_unreadable", "registry_backup_integrity_failed"},
        )

    def test_legacy_v1_backup_preview_is_deterministic_and_restores_after_migration(self) -> None:
        legacy_path = self.registry.backups_dir / "registry-legacy-v1.sqlite3"
        repository_id = "11111111-1111-4111-8111-111111111111"
        repository_path = self.root / "legacy-repository"
        repository_path.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(legacy_path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    applied_at TEXT NOT NULL
                )
                """
            )
            connection.executescript(MIGRATIONS[0][2])
            connection.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (1, MIGRATIONS[0][1], "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                "INSERT INTO application_state(key, value, updated_at) VALUES (?, ?, ?)",
                ("schema_version", "1", "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                "INSERT INTO application_state(key, value, updated_at) VALUES (?, ?, ?)",
                ("active_repository_id", repository_id, "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                """
                INSERT INTO repositories(
                    id, name, description, path, canonical_path, metadata_mode,
                    default_author, favorite, tags_json, collection_name, created_at,
                    updated_at, last_opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repository_id,
                    "Legacy",
                    "Version one backup",
                    str(repository_path),
                    str(repository_path.resolve()),
                    "embedded",
                    "Rooke Poole",
                    1,
                    '["legacy", "migrated"]',
                    "Recovered",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
            connection.commit()

        first_preview = self.registry.preview_registry_restore(legacy_path.name, "replace")
        second_preview = self.registry.preview_registry_restore(legacy_path.name, "replace")
        self.assertEqual(first_preview["previewId"], second_preview["previewId"])
        self.assertEqual(1, first_preview["preparation"]["sourceSchemaVersion"])
        self.assertEqual(APP_SCHEMA_VERSION, first_preview["preparation"]["preparedSchemaVersion"])
        self.assertIn(MIGRATIONS[1][1], first_preview["preparation"]["migrationsApplied"])

        restored = self.registry.restore_registry_backup(
            legacy_path.name, "replace", first_preview["previewId"]
        )
        self.assertEqual("completed", restored["state"])
        recovered = self.registry.get_repository(repository_id)
        self.assertEqual({"legacy", "migrated"}, set(recovered["tags"]))
        self.assertEqual(["Recovered"], [item["name"] for item in recovered["collections"]])

    def test_pre_restore_backup_is_pinned_until_rollback_authority_is_consumed(self) -> None:
        self.add_repository("First")
        source = self.registry.create_backup("pin-source")
        self.add_repository("Second")
        preview = self.registry.preview_registry_restore(source["name"], "replace")
        restored = self.registry.restore_registry_backup(
            source["name"], "replace", preview["previewId"]
        )
        journal_path = (
            self.registry.data_dir
            / "registry-restores"
            / "journals"
            / f"{restored['restoreId']}.json"
        )
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        protected_name = journal["preRestoreBackup"]["name"]
        protected_path = self.registry.backups_dir / protected_name
        self.assertTrue(protected_path.is_file())

        for index in range(25):
            self.registry.create_backup(f"prune-{index}")

        self.assertTrue(protected_path.is_file())
        self.assertIn(protected_name, self.registry.restore_service.protected_backup_names())

        self.registry.rollback_registry_restore(restored["restoreId"])
        self.assertNotIn(protected_name, self.registry.restore_service.protected_backup_names())

    def test_registry_operation_lock_serializes_another_owner_process(self) -> None:
        script = """
import sys
from pathlib import Path
from forgetrace.registry import RepositoryRegistry
registry = RepositoryRegistry(Path(sys.argv[1]), Path(sys.argv[2]))
print(len(registry.list_repositories()["repositories"]))
"""
        self.registry.operation_lock.acquire()
        process = None
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(ROOT),
                    str(self.registry.data_dir),
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.35)
            self.assertIsNone(process.poll(), "Second registry process bypassed registry.lock")
        finally:
            self.registry.operation_lock.release()
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(0, process.returncode, stderr)
        self.assertEqual("0", stdout.strip())

    def test_explicit_rollback_remains_available_after_application_restart(self) -> None:
        self.add_repository("First")
        source = self.registry.create_backup("restart-rollback-source")
        second = self.add_repository("Second")
        preview = self.registry.preview_registry_restore(source["name"], "replace")
        restored = self.registry.restore_registry_backup(
            source["name"], "replace", preview["previewId"]
        )

        reopened = RepositoryRegistry(ROOT, self.registry.data_dir)
        rolled_back = reopened.rollback_registry_restore(restored["restoreId"])
        self.assertEqual("rolled_back", rolled_back["state"])
        self.assertIn(
            second["id"],
            {item["id"] for item in reopened.list_repositories()["repositories"]},
        )

    def test_staged_backup_hash_must_match_the_previewed_bytes(self) -> None:
        first = self.add_repository("First")
        source = self.registry.create_backup("staging-hash-source")
        second = self.add_repository("Second")
        preview = self.registry.preview_registry_restore(source["name"], "replace")
        original_copy = shutil.copy2
        calls = {"count": 0}

        def corrupt_second_stage(source_path, destination_path, *args, **kwargs):
            result = original_copy(source_path, destination_path, *args, **kwargs)
            calls["count"] += 1
            if calls["count"] == 2:
                with Path(destination_path).open("ab") as handle:
                    handle.write(b"staging-corruption")
            return result

        with mock.patch(
            "forgetrace.registry_restore.shutil.copy2", side_effect=corrupt_second_stage
        ):
            with self.assertRaises(ForgeTraceError) as raised:
                self.registry.restore_registry_backup(
                    source["name"], "replace", preview["previewId"]
                )
        self.assertEqual("registry_restore_preview_stale", raised.exception.code)
        self.assertEqual(
            {first["id"], second["id"]},
            {item["id"] for item in self.registry.list_repositories()["repositories"]},
        )

    def test_failed_install_automatically_restores_pre_restore_registry(self) -> None:
        self.add_repository("First")
        backup = self.registry.create_backup("failure-source")
        second = self.add_repository("Second")
        preview = self.registry.preview_registry_restore(backup["name"], "replace")
        original_replace = self.registry.restore_service._replace_live

        calls = {"count": 0}

        def install_then_fail(path: Path) -> None:
            calls["count"] += 1
            original_replace(path)
            if calls["count"] == 1:
                raise ForgeTraceError("Injected post-install failure.", 500, "injected_failure")

        with mock.patch.object(self.registry.restore_service, "_replace_live", side_effect=install_then_fail):
            with self.assertRaises(ForgeTraceError) as raised:
                self.registry.restore_registry_backup(
                    backup["name"], "replace", preview["previewId"]
                )
        self.assertEqual("injected_failure", raised.exception.code)
        self.assertEqual(2, len(self.registry.list_repositories()["repositories"]))
        self.assertIn(second["id"], {item["id"] for item in self.registry.list_repositories()["repositories"]})
        journals = self.registry.list_registry_restores()
        self.assertEqual("failed_rolled_back", journals[0]["state"])

    def test_rollback_blocks_later_registry_changes(self) -> None:
        self.add_repository("First")
        backup = self.registry.create_backup("rollback-source")
        self.add_repository("Second")
        preview = self.registry.preview_registry_restore(backup["name"], "replace")
        restored = self.registry.restore_registry_backup(
            backup["name"], "replace", preview["previewId"]
        )
        self.registry.create_collection(name="Later Work")
        with self.assertRaises(ForgeTraceError) as raised:
            self.registry.rollback_registry_restore(restored["restoreId"])
        self.assertEqual("registry_restore_rollback_stale", raised.exception.code)
        self.assertEqual(1, len(self.registry.list_repositories()["repositories"]))
        self.assertEqual(1, len(self.registry.list_library()["collections"]))

    def test_startup_recovers_interrupted_install_by_rolling_back(self) -> None:
        self.add_repository("First")
        backup = self.registry.create_backup("crash-source")
        second = self.add_repository("Second")
        preview = self.registry.preview_registry_restore(backup["name"], "replace")
        restored = self.registry.restore_registry_backup(
            backup["name"], "replace", preview["previewId"]
        )
        journal_path = (
            self.registry.data_dir
            / "registry-restores"
            / "journals"
            / f"{restored['restoreId']}.json"
        )
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["state"] = "installing"
        journal["rollbackAvailable"] = False
        journal.pop("after", None)
        journal_path.write_text(json.dumps(journal, indent=2), encoding="utf-8")

        reopened = RepositoryRegistry(ROOT, self.registry.data_dir)
        self.assertEqual(1, reopened.startup_restore_recovery_report["rolledBack"])
        self.assertEqual(2, len(reopened.list_repositories()["repositories"]))
        self.assertIn(second["id"], {item["id"] for item in reopened.list_repositories()["repositories"]})


class RegistryRestoreApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="forgetrace-v042-api-")
        self.root = Path(self.temp.name)
        self.app = build_application(ROOT, self.root / "data")
        first = self.app.registry.register_repository(
            path=str(self.root / "first"),
            name="First",
            author="Rooke Poole",
            initialize=True,
            create_directory=True,
        )
        self.first_id = first["id"]
        self.backup = self.app.registry.create_backup("api-source")
        second = self.app.registry.register_repository(
            path=str(self.root / "second"),
            name="Second",
            author="Rooke Poole",
            initialize=True,
            create_directory=True,
        )
        self.second_id = second["id"]
        self.owner, self.owner_thread = self.serve("owner")

    def tearDown(self) -> None:
        self.owner.shutdown()
        self.owner.server_close()
        self.owner_thread.join(timeout=5)
        if self.app.gateway:
            self.app.gateway.stop()
        self.temp.cleanup()

    def serve(self, surface: str):
        server = create_server(self.app, "127.0.0.1", 0, surface=surface)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    @staticmethod
    def request(server, method: str, path: str, payload: dict | None = None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        response_headers = dict(response.getheaders())
        connection.close()
        return status, response_headers, json.loads(raw) if raw else {}

    def test_owner_api_restores_rolls_back_audits_and_gateway_is_denied(self) -> None:
        status, _headers, preview = self.request(
            self.owner,
            "POST",
            "/api/v1/registry/restore/preview",
            {"backupName": self.backup["name"], "mode": "replace"},
        )
        self.assertEqual(200, status)
        status, _headers, restored = self.request(
            self.owner,
            "POST",
            "/api/v1/registry/restore",
            {
                "backupName": self.backup["name"],
                "mode": "replace",
                "previewId": preview["previewId"],
            },
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(self.app.registry.list_repositories()["repositories"]))
        status, _headers, history = self.request(
            self.owner, "GET", "/api/v1/registry/restores"
        )
        self.assertEqual(200, status)
        self.assertEqual(restored["restoreId"], history["restores"][0]["restoreId"])

        gateway, gateway_thread = self.serve("gateway")
        try:
            status, _headers, denied = self.request(
                gateway, "GET", "/api/v1/registry/restores"
            )
            self.assertEqual(403, status)
            self.assertEqual("remote_owner_api_blocked", denied["code"])
        finally:
            gateway.shutdown()
            gateway.server_close()
            gateway_thread.join(timeout=5)

        status, _headers, rolled_back = self.request(
            self.owner,
            "POST",
            f"/api/v1/registry/restores/{restored['restoreId']}/rollback",
            {},
        )
        self.assertEqual(200, status)
        self.assertEqual("rolled_back", rolled_back["state"])
        self.assertEqual(
            {self.first_id, self.second_id},
            {item["id"] for item in self.app.registry.list_repositories()["repositories"]},
        )
        events = self.app.security_events.query(category="recovery", limit=100)["events"]
        actions = {item["action"] for item in events}
        self.assertIn("registry_restore_previewed", actions)
        self.assertIn("registry_restore_authorized", actions)
        self.assertIn("registry_restore", actions)
        self.assertIn("registry_restore_rollback", actions)

    def test_tampered_ledger_blocks_restore_before_registry_mutation(self) -> None:
        preview = self.app.registry.preview_registry_restore(self.backup["name"], "replace")
        with contextlib.closing(sqlite3.connect(self.app.security_events.db_path)) as connection:
            connection.execute("DROP TRIGGER security_events_no_update")
            connection.execute("UPDATE security_events SET details_json='{}' WHERE sequence=1")
            connection.commit()
        status, _headers, denied = self.request(
            self.owner,
            "POST",
            "/api/v1/registry/restore",
            {
                "backupName": self.backup["name"],
                "mode": "replace",
                "previewId": preview["previewId"],
            },
        )
        self.assertEqual(503, status)
        self.assertEqual("security_event_ledger_unavailable", denied["code"])
        self.assertEqual(2, len(self.app.registry.list_repositories()["repositories"]))
        self.assertEqual([], self.app.registry.list_registry_restores())


if __name__ == "__main__":
    unittest.main(verbosity=2)
