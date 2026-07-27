from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

from forgetrace.app import build_application
from forgetrace.constants import (
    APP_SCHEMA_VERSION,
    REPOSITORY_ACCESS_READ_ONLY,
    REPOSITORY_ACCESS_READ_WRITE,
    REPOSITORY_SCHEMA_VERSION,
)
from forgetrace.errors import ForgeTraceError
from forgetrace.registry import MIGRATIONS, RepositoryRegistry
from forgetrace.web import create_server


ROOT = Path(__file__).resolve().parents[1]


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if ".forgetrace" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


class ReadOnlyServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="forgetrace-v043-")
        self.root = Path(self.temp.name)
        self.app = build_application(ROOT, self.root / "data")
        self.record = self.app.registry.register_repository(
            path=str(self.root / "repo"), name="Read only", author="Rooke Poole",
            initialize=True, create_directory=True,
        )
        self.repository_id = self.record["id"]
        service = self.app.registry.repository_service(self.repository_id)
        service.write_file("alpha.txt", b"alpha\n", "Rooke Poole", "baseline")
        self.commit = service.create_commit("Baseline", "Rooke Poole")

    def tearDown(self) -> None:
        if self.app.gateway:
            self.app.gateway.stop()
        self.temp.cleanup()

    def assert_read_only(self, call) -> ForgeTraceError:
        with self.assertRaises(ForgeTraceError) as raised:
            call()
        self.assertEqual(423, raised.exception.status)
        self.assertEqual("repository_read_only", raised.exception.code)
        return raised.exception

    def test_every_repository_write_authority_is_blocked_and_reads_remain_available(self) -> None:
        self.app.registry.set_access_mode(self.repository_id, REPOSITORY_ACCESS_READ_ONLY)
        service = self.app.registry.repository_service(self.repository_id)
        before_revision = service.load_state()["revision"]
        before_tree = tree_digest(service.workspace)
        hash_index = service.meta_dir / "hash-index.json"
        before_hash_index = hash_index.read_bytes() if hash_index.exists() else b""
        upload = self.root / "upload.bin"
        upload.write_bytes(b"upload")
        source = self.root / "source"
        source.mkdir()
        (source / "nested.txt").write_text("nested", encoding="utf-8")
        staged = self.root / "staged.txt"
        staged.write_text("staged", encoding="utf-8")

        operations = {
            "metadata": lambda: service.update_repository_metadata("Changed", "", "Owner"),
            "upstream": lambda: service.set_upstream({"baseUrl": "http://example.invalid"}),
            "text write": lambda: service.write_file("alpha.txt", b"changed", "Owner", "change"),
            "stream write": lambda: service.write_file_from_path("upload.bin", upload, "Owner", "upload"),
            "folder": lambda: service.create_folder("new-folder", "Owner"),
            "folder manifest": lambda: service.ensure_folders(["a", "a/b"], "Owner"),
            "folder import": lambda: service.import_local_folder(str(source), "Owner"),
            "rename": lambda: service.rename_path("alpha.txt", "renamed.txt", "Owner"),
            "delete": lambda: service.delete_path("alpha.txt", "Owner"),
            "snapshot": lambda: service.create_commit("Blocked", "Owner"),
            "restore": lambda: service.restore_commit(self.commit["id"], "Owner"),
            "object materialization": lambda: service.scan_index(store_objects=True),
            "direct metadata persistence": lambda: service.save_state(service.load_state()),
            "direct merge": lambda: service.merge_pull_request(
                pull_request_id="pr_blocked", pull_request_number=1, title="Blocked",
                contributor="Contributor", merged_by="Owner",
                staged_changes={"beta.txt": staged}, deletions=[], expected_base_hashes={"beta.txt": ""},
            ),
            "registry settings": lambda: self.app.registry.update_settings(
                self.repository_id, name="Changed", description="", default_author="Owner"
            ),
        }
        for name, operation in operations.items():
            with self.subTest(operation=name):
                self.assert_read_only(operation)

        state = service.load_state()
        self.assertEqual(before_revision, state["revision"])
        self.assertEqual(before_tree, tree_digest(service.workspace))
        self.assertEqual("alpha\n", service.read_file("alpha.txt")["content"])
        self.assertTrue(service.verify_snapshot_objects(state["commits"][0])["valid"])
        self.assertTrue(service.export_zip(include_history=True))
        summary = service.summary()
        after_hash_index = hash_index.read_bytes() if hash_index.exists() else b""
        self.assertEqual(before_hash_index, after_hash_index)
        self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, summary["accessPolicy"]["effectiveMode"])
        self.assertFalse(summary["accessPolicy"]["writable"])

    def test_mode_transition_is_two_copy_persistent_and_mismatch_fails_closed(self) -> None:
        changed = self.app.registry.set_access_mode(self.repository_id, REPOSITORY_ACCESS_READ_ONLY)
        self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, changed["accessMode"])
        self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, changed["accessPolicy"]["effectiveMode"])
        state_path = Path(changed["path"]) / ".forgetrace" / "state.json"
        self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, json.loads(state_path.read_text())["repository"]["accessMode"])

        reopened = build_application(ROOT, self.root / "data")
        try:
            policy = reopened.registry.repository_service(self.repository_id).access_policy()
            self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, policy["effectiveMode"])
            reopened.registry.set_access_mode(self.repository_id, REPOSITORY_ACCESS_READ_WRITE)
            self.assertTrue(reopened.registry.repository_service(self.repository_id).access_policy()["writable"])

            payload = json.loads(state_path.read_text())
            payload["repository"]["accessMode"] = REPOSITORY_ACCESS_READ_ONLY
            state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            mismatch = reopened.registry.repository_service(self.repository_id).access_policy()
            self.assertFalse(mismatch["consistent"])
            self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, mismatch["effectiveMode"])
            reopened.registry.set_access_mode(self.repository_id, REPOSITORY_ACCESS_READ_WRITE)
            self.assertTrue(reopened.registry.repository_service(self.repository_id).access_policy()["writable"])

            payload = json.loads(state_path.read_text())
            payload["repository"].pop("accessMode", None)
            payload["schemaVersion"] = REPOSITORY_SCHEMA_VERSION
            state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            missing = reopened.registry.repository_service(self.repository_id).access_policy()
            self.assertFalse(missing["consistent"])
            self.assertFalse(missing["embeddedValid"])
            self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, missing["embeddedMode"])
            self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, missing["effectiveMode"])
            reopened.registry.set_access_mode(self.repository_id, REPOSITORY_ACCESS_READ_WRITE)
            self.assertTrue(reopened.registry.repository_service(self.repository_id).access_policy()["writable"])

            payload = json.loads(state_path.read_text())
            payload["repository"]["accessMode"] = "invalid-mode"
            state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            invalid_embedded = reopened.registry.repository_service(self.repository_id).access_policy()
            self.assertFalse(invalid_embedded["embeddedValid"])
            self.assertFalse(invalid_embedded["consistent"])
            self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, invalid_embedded["effectiveMode"])
            reopened.registry.set_access_mode(self.repository_id, REPOSITORY_ACCESS_READ_WRITE)
            self.assertTrue(reopened.registry.repository_service(self.repository_id).access_policy()["writable"])

            with self.assertRaises(ForgeTraceError) as invalid_mode:
                reopened.registry.set_access_mode(self.repository_id, "")
            self.assertEqual("invalid_repository_access_mode", invalid_mode.exception.code)
        finally:
            if reopened.gateway:
                reopened.gateway.stop()

    def test_managed_repository_discard_is_blocked(self) -> None:
        managed = self.app.registry.create_managed_repository(name="Managed", author="Owner")
        self.app.registry.set_access_mode(managed["id"], REPOSITORY_ACCESS_READ_ONLY)
        self.assert_read_only(lambda: self.app.registry.discard_managed_repository(managed["id"]))
        self.assertTrue(Path(managed["path"]).is_dir())
        self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, self.app.registry.get_repository(managed["id"])["accessMode"])

    def test_quarantined_contribution_is_allowed_but_merge_is_blocked(self) -> None:
        # Leave the workspace dirty to exercise the non-mutating read-only baseline.
        service = self.app.registry.repository_service(self.repository_id)
        service.write_file("dirty.txt", b"owner dirty\n", "Owner", "dirty")
        self.app.registry.set_access_mode(self.repository_id, REPOSITORY_ACCESS_READ_ONLY)
        invite = self.app.collaboration.create_invite(self.repository_id, max_uses=2)
        token = invite["token"]
        pull_request = self.app.collaboration.create_pull_request(
            token, title="Read-only review", description="Quarantined", author_name="Contributor"
        )
        self.assertTrue(pull_request["baseCommitId"].startswith("readonly-"))
        pull_request = self.app.collaboration.upload_pull_request_file(
            token, pull_request["id"], "contribution.txt", b"quarantined\n"
        )
        pull_request = self.app.collaboration.submit_pull_request(token, pull_request["id"])
        pull_request = self.app.collaboration.review_pull_request(
            self.repository_id, pull_request["id"], reviewer="Owner", verdict="approved"
        )
        before = tree_digest(service.workspace)
        self.assert_read_only(lambda: self.app.collaboration.merge_pull_request(
            self.repository_id, pull_request["id"], merged_by="Owner",
            confirmation=f"MERGE #{pull_request['number']}", expected_revision=pull_request["revision"],
        ))
        self.assertEqual(before, tree_digest(service.workspace))
        self.assertFalse((service.workspace / "contribution.txt").exists())
        self.assertEqual("approved", self.app.collaboration.get_pull_request(self.repository_id, pull_request["id"])["status"])

    def test_registry_restore_merge_replace_rollback_and_restart_preserve_modes(self) -> None:
        # A read-write backup is merged into a live read-only repository: live authority wins.
        read_write_backup = self.app.registry.create_backup("read-write")
        self.app.registry.set_access_mode(self.repository_id, REPOSITORY_ACCESS_READ_ONLY)
        merge_preview = self.app.registry.preview_registry_restore(read_write_backup["name"], "merge")
        self.app.registry.restore_registry_backup(read_write_backup["name"], "merge", merge_preview["previewId"])
        self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, self.app.registry.get_repository(self.repository_id)["accessMode"])
        self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, self.app.registry.repository_service(self.repository_id).access_policy()["effectiveMode"])

        read_only_backup = self.app.registry.create_backup("read-only")
        self.app.registry.set_access_mode(self.repository_id, REPOSITORY_ACCESS_READ_WRITE)
        state_path = Path(self.record["path"]) / ".forgetrace" / "state.json"
        invalid_state = json.loads(state_path.read_text())
        invalid_state["repository"]["accessMode"] = "invalid-mode"
        state_path.write_text(json.dumps(invalid_state, indent=2), encoding="utf-8")
        self.assertFalse(self.app.registry.repository_service(self.repository_id).access_policy()["embeddedValid"])
        replace_preview = self.app.registry.preview_registry_restore(read_only_backup["name"], "replace")
        restored = self.app.registry.restore_registry_backup(read_only_backup["name"], "replace", replace_preview["previewId"])
        restored_policy = self.app.registry.repository_service(self.repository_id).access_policy()
        self.assertTrue(restored_policy["embeddedValid"])
        self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, restored_policy["effectiveMode"])
        self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, json.loads(state_path.read_text())["repository"]["accessMode"])
        rolled_back = self.app.registry.rollback_registry_restore(restored["restoreId"])
        self.assertEqual("rolled_back", rolled_back["state"])
        self.assertEqual(REPOSITORY_ACCESS_READ_WRITE, self.app.registry.get_repository(self.repository_id)["accessMode"])
        self.assertTrue(self.app.registry.repository_service(self.repository_id).access_policy()["writable"])

        reopened = build_application(ROOT, self.root / "data")
        try:
            self.assertTrue(reopened.registry.repository_service(self.repository_id).access_policy()["writable"])
        finally:
            if reopened.gateway:
                reopened.gateway.stop()

    def test_stale_service_in_another_process_observes_mode_change(self) -> None:
        ready = self.root / "ready"
        go = self.root / "go"
        script = r'''
import sys, time
from pathlib import Path
from forgetrace.registry import RepositoryRegistry
from forgetrace.errors import ForgeTraceError
root, data, repo_id, ready, go = map(Path, sys.argv[1:6])
registry = RepositoryRegistry(root, data)
service = registry.repository_service(str(repo_id))
ready.write_text('ready')
for _ in range(300):
    if go.exists(): break
    time.sleep(0.02)
try:
    service.write_file('cross-process.txt', b'blocked', 'Worker', 'blocked')
    print('UNEXPECTED_WRITE')
except ForgeTraceError as exc:
    print(f'{exc.status}:{exc.code}')
'''
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(ROOT), str(self.root / "data"), self.repository_id, str(ready), str(go)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(300):
            if ready.exists():
                break
            time.sleep(0.02)
        self.assertTrue(ready.exists())
        self.app.registry.set_access_mode(self.repository_id, REPOSITORY_ACCESS_READ_ONLY)
        go.write_text("go")
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(0, process.returncode, stderr)
        self.assertEqual("423:repository_read_only", stdout.strip())
        self.assertFalse((Path(self.record["path"]) / "cross-process.txt").exists())


class ReadOnlySchemaMigrationTest(unittest.TestCase):
    def test_fresh_and_v042_schemas_upgrade_to_two_copy_read_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgetrace-v043-schema-") as raw:
            root = Path(raw)
            fresh = RepositoryRegistry(ROOT, root / "fresh-data")
            record = fresh.register_repository(
                path=str(root / "fresh-repo"), name="Fresh", initialize=True, create_directory=True
            )
            with fresh.connect() as connection:
                row = connection.execute("SELECT access_mode FROM repositories WHERE id = ?", (record["id"],)).fetchone()
                self.assertEqual(REPOSITORY_ACCESS_READ_WRITE, row["access_mode"])
            fresh_state = json.loads((Path(record["path"]) / ".forgetrace" / "state.json").read_text())
            self.assertEqual(REPOSITORY_SCHEMA_VERSION, fresh_state["schemaVersion"])
            self.assertEqual(REPOSITORY_ACCESS_READ_WRITE, fresh_state["repository"]["accessMode"])

            data = root / "upgrade-data"
            data.mkdir()
            db_path = data / "registry.sqlite3"
            repo_path = root / "upgrade-repo"
            meta = repo_path / ".forgetrace"
            meta.mkdir(parents=True)
            repository_id = "11111111-1111-4111-8111-111111111111"
            (meta / "state.json").write_text(json.dumps({
                "schemaVersion": 2, "revision": 1,
                "repository": {"id": repository_id, "name": "Upgrade", "description": "", "defaultAuthor": "Owner", "createdAt": "2026-01-01T00:00:00Z"},
                "contributions": [], "commits": [],
            }), encoding="utf-8")
            with contextlib.closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL)")
                for version, name, sql in MIGRATIONS[:2]:
                    connection.executescript(sql)
                    connection.execute("INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)", (version, name, "2026-01-01T00:00:00Z"))
                connection.execute("INSERT INTO application_state(key,value,updated_at) VALUES('schema_version','3','2026-01-01T00:00:00Z')")
                connection.execute("INSERT INTO application_state(key,value,updated_at) VALUES('active_repository_id',?,'2026-01-01T00:00:00Z')", (repository_id,))
                connection.execute(
                    """INSERT INTO repositories(id,name,description,path,canonical_path,metadata_mode,default_author,favorite,tags_json,collection_name,created_at,updated_at,last_opened_at,upload_limit_bytes,metadata_path)
                    VALUES(?,?,?,?,?,'embedded','Owner',0,'[]','',?,?,?,1073741824,'')""",
                    (repository_id, "Upgrade", "", str(repo_path.resolve()), str(repo_path.resolve()), "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                )
                connection.commit()
            upgraded = RepositoryRegistry(ROOT, data)
            with upgraded.connect() as connection:
                schema_row = connection.execute("SELECT value FROM application_state WHERE key = 'schema_version'").fetchone()
                self.assertEqual(APP_SCHEMA_VERSION, int(schema_row["value"]))
            self.assertEqual(REPOSITORY_ACCESS_READ_WRITE, upgraded.get_repository(repository_id)["accessMode"])
            upgraded.repository_service(repository_id)
            state = json.loads((meta / "state.json").read_text())
            self.assertEqual(REPOSITORY_SCHEMA_VERSION, state["schemaVersion"])
            self.assertEqual(REPOSITORY_ACCESS_READ_WRITE, state["repository"]["accessMode"])


class ReadOnlyApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="forgetrace-v043-api-")
        self.root = Path(self.temp.name)
        self.app = build_application(ROOT, self.root / "data")
        self.record = self.app.registry.create_managed_repository(name="API read-only", author="Owner")
        self.repository_id = self.record["id"]
        service = self.app.registry.repository_service(self.repository_id)
        service.write_file("alpha.txt", b"alpha", "Owner", "baseline")
        self.commit = service.create_commit("Baseline", "Owner")
        self.app.registry.set_active(self.repository_id)
        self.app.registry.set_access_mode(self.repository_id, REPOSITORY_ACCESS_READ_ONLY)
        self.owner = create_server(self.app, "127.0.0.1", 0, surface="owner")
        self.owner_thread = threading.Thread(target=self.owner.serve_forever, daemon=True)
        self.owner_thread.start()
        self.gateway = create_server(self.app, "127.0.0.1", 0, surface="gateway")
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()

    def tearDown(self) -> None:
        self.owner.shutdown(); self.owner.server_close(); self.owner_thread.join(timeout=5)
        self.gateway.shutdown(); self.gateway.server_close(); self.gateway_thread.join(timeout=5)
        if self.app.gateway:
            self.app.gateway.stop()
        self.temp.cleanup()

    @staticmethod
    def request(server, method: str, path: str, payload=None, content_type="application/json"):
        body = b""
        headers = {}
        if payload is not None:
            if content_type == "application/json":
                body = json.dumps(payload).encode()
            elif isinstance(payload, bytes):
                body = payload
            else:
                body = str(payload).encode()
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        response_headers = dict(response.getheaders())
        connection.close()
        try:
            decoded = json.loads(raw)
        except Exception:
            decoded = raw
        return status, response_headers, decoded

    def assert_route_read_only(self, method, path, payload=None, content_type="application/json"):
        status, _headers, body = self.request(self.owner, method, path, payload, content_type)
        self.assertEqual(423, status, (method, path, body))
        self.assertEqual("repository_read_only", body.get("code"), (method, path, body))

    def test_owner_and_legacy_mutation_routes_return_423_before_change(self) -> None:
        base = f"/api/v1/repositories/{self.repository_id}"
        source = self.root / "import-source"
        source.mkdir()
        (source / "x.txt").write_text("x", encoding="utf-8")
        routes = [
            ("PUT", base + "/file", {"path":"alpha.txt","content":"changed","author":"Owner"}, "application/json"),
            ("POST", base + "/upload?path=upload.bin&author=Owner&message=Upload", b"upload", "application/octet-stream"),
            ("POST", base + "/folder", {"path":"folder","author":"Owner"}, "application/json"),
            ("POST", base + "/folders", {"paths":["a","a/b"],"author":"Owner"}, "application/json"),
            ("POST", base + "/import-jobs", {"path":str(source),"author":"Owner"}, "application/json"),
            ("POST", base + "/import-local-folder", {"path":str(source),"author":"Owner"}, "application/json"),
            ("POST", base + "/rename", {"path":"alpha.txt","newPath":"beta.txt","author":"Owner"}, "application/json"),
            ("POST", base + "/commit", {"message":"Blocked","author":"Owner"}, "application/json"),
            ("POST", base + "/checkout", {"commitId":self.commit["id"],"author":"Owner"}, "application/json"),
            ("POST", base + "/settings", {"name":"Changed","description":"","defaultAuthor":"Owner","uploadLimitBytes":1024*1024}, "application/json"),
            ("DELETE", base + "/path?path=alpha.txt&author=Owner", None, "application/json"),
            ("DELETE", base + "/discard", None, "application/json"),
            ("PUT", "/api/file", {"path":"alpha.txt","content":"changed","author":"Owner"}, "application/json"),
            ("POST", "/api/upload?path=legacy.bin&author=Owner&message=Upload", b"legacy", "application/octet-stream"),
            ("POST", "/api/folder", {"path":"legacy-folder","author":"Owner"}, "application/json"),
            ("POST", "/api/rename", {"path":"alpha.txt","newPath":"legacy.txt","author":"Owner"}, "application/json"),
            ("POST", "/api/commit", {"message":"Blocked","author":"Owner"}, "application/json"),
            ("POST", "/api/checkout", {"commitId":self.commit["id"],"author":"Owner"}, "application/json"),
            ("DELETE", "/api/path?path=alpha.txt&author=Owner", None, "application/json"),
        ]
        for method, path, payload, content_type in routes:
            with self.subTest(method=method, path=path):
                self.assert_route_read_only(method, path, payload, content_type)
        service = self.app.registry.repository_service(self.repository_id)
        self.assertEqual("alpha", service.read_file("alpha.txt")["content"])

    def test_mode_route_is_owner_only_audited_and_security_fail_closed(self) -> None:
        path = f"/api/v1/repositories/{self.repository_id}/access-mode"
        status, _headers, body = self.request(self.gateway, "POST", path, {"mode":"read_write","actor":"Owner"})
        self.assertIn(status, {403, 404})
        self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, self.app.registry.get_access_mode(self.repository_id))

        status, _headers, body = self.request(self.owner, "POST", path, {"mode":"read_write","actor":"Owner"})
        self.assertEqual(200, status, body)
        self.assertEqual(REPOSITORY_ACCESS_READ_WRITE, body["accessMode"])
        events = self.app.security_events.query(action="repository_access_mode_change", limit=20)["events"]
        self.assertTrue(any(item["outcome"] == "success" and item["repositoryId"] == self.repository_id for item in events))

        self.request(self.owner, "POST", path, {"mode":"read_only","actor":"Owner"})
        with contextlib.closing(sqlite3.connect(self.app.security_events.db_path)) as connection:
            connection.execute("DROP TRIGGER security_events_no_update")
            connection.execute("UPDATE security_events SET details_json='{}' WHERE sequence=1")
            connection.commit()
        status, _headers, body = self.request(self.owner, "POST", path, {"mode":"read_write","actor":"Owner"})
        self.assertEqual(503, status, body)
        self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, self.app.registry.get_access_mode(self.repository_id))
        embedded = json.loads((Path(self.record["path"]) / ".forgetrace" / "state.json").read_text())["repository"]["accessMode"]
        self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, embedded)

    def test_ui_exposes_immutable_mode_and_disables_mutations(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        for marker in (
            'id="readOnlyBanner"', 'id="settingsAccessMode"', 'id="applyAccessModeBtn"',
            "function isReadOnly()", "Service-enforced read-only repository",
            "/access-mode", "data-pr-merge", "settingsSaveBtn",
        ):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()
