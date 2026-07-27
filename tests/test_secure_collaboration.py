from __future__ import annotations

import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
import zipfile
from pathlib import Path

from forgetrace.app import build_application
from forgetrace.errors import ForgeTraceError
from forgetrace.web import make_handler
from http.server import ThreadingHTTPServer


class SecureCollaborationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_root = Path(__file__).resolve().parents[1]
        self.data_dir = self.root / "data"
        self.workspace = self.root / "repo"
        self.app = build_application(self.project_root, self.data_dir)
        record = self.app.registry.register_repository(
            path=str(self.workspace),
            name="Shared Project",
            description="Secure collaboration fixture",
            author="Local Owner",
            initialize=True,
            create_directory=True,
        )
        self.repository_id = record["id"]
        self.repository = self.app.registry.repository_service(self.repository_id)
        self.repository.write_file("src/app.txt", b"alpha\n", "Local Owner", "seed")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _invite(self, **kwargs):
        result = self.app.collaboration.create_invite(
            self.repository_id,
            label="Outside contributor",
            expires_in_hours=24,
            max_uses=3,
            **kwargs,
        )
        return result["token"], result

    def test_quarantined_pull_request_review_and_merge(self) -> None:
        token, invite_result = self._invite()
        self.assertIn("/contribute.html#", invite_result["sharePath"])
        with closing(sqlite3.connect(self.app.collaboration.db_path)) as connection:
            token_hash = connection.execute(
                "SELECT token_hash FROM collaboration_invites WHERE id = ?",
                (invite_result["invite"]["id"],),
            ).fetchone()[0]
        self.assertNotEqual(token, token_hash)
        self.assertNotIn(token.encode(), self.app.collaboration.db_path.read_bytes())

        pr = self.app.collaboration.create_pull_request(
            token,
            title="Improve application text",
            description="Updates the core file and adds a helper script.",
            author_name="Outside Contributor",
        )
        pr_id = pr["id"]
        self.app.collaboration.upload_pull_request_file(token, pr_id, "src/app.txt", b"alpha\nbeta\n")
        self.app.collaboration.upload_pull_request_file(token, pr_id, "tools/check.sh", b"#!/bin/sh\necho checked\n")
        submitted = self.app.collaboration.submit_pull_request(token, pr_id)
        self.assertEqual("open", submitted["status"])

        detail = self.app.collaboration.get_pull_request(self.repository_id, pr_id)
        self.assertIn("+beta", detail["files"][0]["diff"])
        self.assertEqual(1, detail["riskyFileCount"])
        self.assertEqual([], detail["conflicts"])

        with self.assertRaises(ForgeTraceError) as unapproved:
            self.app.collaboration.merge_pull_request(
                self.repository_id,
                pr_id,
                merged_by="Local Owner",
                confirmation="MERGE #1",
                expected_revision=detail["revision"],
                allow_risky_files=True,
            )
        self.assertEqual("pull_request_not_approved", unapproved.exception.code)

        approved = self.app.collaboration.review_pull_request(
            self.repository_id,
            pr_id,
            reviewer="Local Owner",
            verdict="approved",
            comment="Diff reviewed locally.",
        )
        self.assertEqual("approved", approved["status"])

        with self.assertRaises(ForgeTraceError) as risky:
            self.app.collaboration.merge_pull_request(
                self.repository_id,
                pr_id,
                merged_by="Local Owner",
                confirmation="MERGE #1",
                expected_revision=approved["revision"],
                allow_risky_files=False,
            )
        self.assertEqual("risky_files_require_confirmation", risky.exception.code)

        merged = self.app.collaboration.merge_pull_request(
            self.repository_id,
            pr_id,
            merged_by="Local Owner",
            confirmation="MERGE #1",
            expected_revision=approved["revision"],
            allow_risky_files=True,
        )
        self.assertEqual("merged", merged["status"])
        self.assertEqual(b"alpha\nbeta\n", (self.workspace / "src" / "app.txt").read_bytes())
        self.assertEqual(b"#!/bin/sh\necho checked\n", (self.workspace / "tools" / "check.sh").read_bytes())
        state = self.repository.load_state()
        merge_events = [item for item in state["contributions"] if item["action"] == "pull_request_merged"]
        self.assertEqual(1, len(merge_events))
        self.assertEqual("Outside Contributor", merge_events[0]["author"])
        self.assertTrue(merged["mergeCommitId"])

    def test_conflict_detection_and_changes_requested_revision(self) -> None:
        token, _ = self._invite(max_file_bytes=1024 * 1024, max_total_bytes=2 * 1024 * 1024)
        pr = self.app.collaboration.create_pull_request(
            token, title="Change README", description="", author_name="Contributor"
        )
        pr_id = pr["id"]
        self.app.collaboration.upload_pull_request_file(token, pr_id, "README.md", b"# Proposed\n")
        submitted = self.app.collaboration.submit_pull_request(token, pr_id)
        requested = self.app.collaboration.review_pull_request(
            self.repository_id, pr_id, reviewer="Owner", verdict="changes_requested", comment="Add context."
        )
        self.assertEqual("changes_requested", requested["status"])
        revised = self.app.collaboration.upload_pull_request_file(
            token, pr_id, "README.md", b"# Proposed\n\nMore context.\n"
        )
        self.assertEqual("draft", revised["status"])
        self.assertGreater(revised["revision"], submitted["revision"])
        resubmitted = self.app.collaboration.submit_pull_request(token, pr_id)
        self.app.collaboration.review_pull_request(
            self.repository_id, pr_id, reviewer="Owner", verdict="approved", comment="Approved before merge."
        )
        self.repository.write_file("README.md", b"# Owner changed this\n", "Owner", "parallel edit")
        detail = self.app.collaboration.get_pull_request(self.repository_id, pr_id)
        self.assertEqual("conflict", detail["effectiveStatus"])
        with self.assertRaises(ForgeTraceError) as conflict:
            self.app.collaboration.merge_pull_request(
                self.repository_id, pr_id, merged_by="Owner",
                confirmation="MERGE #1", expected_revision=resubmitted["revision"]
            )
        self.assertEqual("pull_request_conflict", conflict.exception.code)

    def test_protected_paths_and_limits(self) -> None:
        token, _ = self._invite(max_file_bytes=1024, max_total_bytes=2048)
        pr = self.app.collaboration.create_pull_request(
            token, title="Limit checks", description="", author_name="Contributor"
        )
        with self.assertRaises(ForgeTraceError) as protected:
            self.app.collaboration.upload_pull_request_file(token, pr["id"], ".git/config", b"bad")
        self.assertEqual("protected_collaboration_path", protected.exception.code)
        with self.assertRaises(ForgeTraceError) as oversized:
            self.app.collaboration.upload_pull_request_file(token, pr["id"], "large.bin", b"x" * 1025)
        self.assertEqual("pull_request_file_too_large", oversized.exception.code)

    def test_unchanged_file_is_removed_from_the_change_set(self) -> None:
        token, _ = self._invite()
        pr = self.app.collaboration.create_pull_request(
            token, title="No-op guard", description="", author_name="Contributor"
        )
        result = self.app.collaboration.upload_pull_request_file(
            token, pr["id"], "src/app.txt", b"alpha\n"
        )
        self.assertEqual(0, result["changeCount"])
        with self.assertRaises(ForgeTraceError) as empty:
            self.app.collaboration.submit_pull_request(token, pr["id"])
        self.assertEqual("empty_pull_request", empty.exception.code)


class RemoteBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        project_root = Path(__file__).resolve().parents[1]
        self.app = build_application(project_root, root / "data")
        record = self.app.registry.register_repository(
            path=str(root / "repo"), name="Remote Boundary", author="Owner",
            initialize=True, create_directory=True,
        )
        self.repository_id = record["id"]
        invite = self.app.collaboration.create_invite(self.repository_id)
        self.token = invite["token"]

        base = make_handler(self.app)

        class SimulatedRemoteHandler(base):
            _remote_rate_windows = {}
            _remote_rate_limit = 2
            _source_rate_windows = {}
            _source_rate_limit = 1

            def is_loopback_client(self) -> bool:
                return False

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SimulatedRemoteHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, response.getheader("Content-Type", ""), data

    def test_remote_clients_only_receive_contribution_surface(self) -> None:
        status, _content_type, data = self.request("GET", "/api/v1/repositories")
        self.assertEqual(403, status)
        self.assertEqual("remote_owner_api_blocked", json.loads(data)["code"])

        status, _content_type, data = self.request(
            "POST", "/api/v1/repositories/managed",
            body=json.dumps({"name": "Blocked remote import"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(403, status)
        self.assertEqual("remote_owner_api_blocked", json.loads(data)["code"])

        status, content_type, data = self.request("GET", "/")
        self.assertEqual(200, status)
        self.assertIn("text/html", content_type)
        self.assertIn(b"Contribute to ForgeTrace", data)

        status, _content_type, data = self.request(
            "GET", "/api/v1/collaboration/invite", headers={"X-ForgeTrace-Invite": self.token}
        )
        self.assertEqual(200, status)
        payload = json.loads(data)
        self.assertFalse(payload["rules"]["directWorkspaceAccess"])
        self.assertEqual("Remote Boundary", payload["repository"]["name"])

    def test_remote_collaboration_requests_are_throttled(self) -> None:
        headers = {"X-ForgeTrace-Invite": self.token}
        for _ in range(2):
            status, _content_type, _data = self.request(
                "GET", "/api/v1/collaboration/invite", headers=headers
            )
            self.assertEqual(200, status)
        status, _content_type, data = self.request(
            "GET", "/api/v1/collaboration/invite", headers=headers
        )
        self.assertEqual(429, status)
        self.assertEqual("collaboration_rate_limited", json.loads(data)["code"])

    def test_source_archive_has_a_stricter_throttle(self) -> None:
        headers = {"X-ForgeTrace-Invite": self.token}
        status, content_type, _data = self.request(
            "GET", "/api/v1/collaboration/source", headers=headers
        )
        self.assertEqual(200, status)
        self.assertEqual("application/zip", content_type)
        status, _content_type, data = self.request(
            "GET", "/api/v1/collaboration/source", headers=headers
        )
        self.assertEqual(429, status)
        self.assertEqual("source_download_rate_limited", json.loads(data)["code"])


class SecureCollaborationApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        project_root = Path(__file__).resolve().parents[1]
        self.workspace = root / "repo"
        self.app = build_application(project_root, root / "data")
        record = self.app.registry.register_repository(
            path=str(self.workspace), name="API Collaboration", author="Owner",
            initialize=True, create_directory=True,
        )
        self.repository_id = record["id"]
        self.app.registry.repository_service(self.repository_id).write_file(
            "src/app.txt", b"baseline\n", "Owner", "seed"
        )
        (self.workspace / ".git").mkdir(exist_ok=True)
        (self.workspace / ".git" / "config").write_text(
            "[remote \"origin\"]\nurl = https://secret@example.invalid/repo.git\n",
            encoding="utf-8",
        )
        self.external_secret = root / "outside-secret.txt"
        self.external_secret.write_text("must not leak\n", encoding="utf-8")
        try:
            (self.workspace / "outside-link.txt").symlink_to(self.external_secret)
            self.symlink_supported = True
        except (OSError, NotImplementedError):
            self.symlink_supported = False

        owner_handler = make_handler(self.app)
        self.owner_server = ThreadingHTTPServer(("127.0.0.1", 0), owner_handler)
        self.owner_thread = threading.Thread(target=self.owner_server.serve_forever, daemon=True)
        self.owner_thread.start()

        remote_base = make_handler(self.app)

        class RemoteHandler(remote_base):
            def is_loopback_client(self) -> bool:
                return False

        self.remote_server = ThreadingHTTPServer(("127.0.0.1", 0), RemoteHandler)
        self.remote_thread = threading.Thread(target=self.remote_server.serve_forever, daemon=True)
        self.remote_thread.start()

    def tearDown(self) -> None:
        for server, thread in (
            (self.owner_server, self.owner_thread),
            (self.remote_server, self.remote_thread),
        ):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.temp.cleanup()

    @staticmethod
    def request(server, method: str, path: str, *, body: bytes = b"", headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        connection.close()
        return result

    @classmethod
    def request_json(cls, server, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        body = json.dumps(payload or {}).encode("utf-8") if payload is not None else b""
        merged_headers = {"Content-Type": "application/json", **(headers or {})}
        status, response_headers, raw = cls.request(server, method, path, body=body, headers=merged_headers)
        return status, response_headers, json.loads(raw) if raw else {}

    def test_source_download_remote_submission_and_local_merge(self) -> None:
        status, _headers, invite = self.request_json(
            self.owner_server,
            "POST",
            f"/api/v1/repositories/{self.repository_id}/collaboration/invites",
            {
                "label": "API contributor",
                "maxUses": 1,
                "allowSourceDownload": True,
                "allowDeletes": False,
            },
        )
        self.assertEqual(201, status)
        token = invite["token"]
        token_headers = {"X-ForgeTrace-Invite": token}

        status, headers, source_zip = self.request(
            self.remote_server, "GET", "/api/v1/collaboration/source", headers=token_headers
        )
        self.assertEqual(200, status)
        self.assertEqual("application/zip", headers["Content-Type"])
        with zipfile.ZipFile(io.BytesIO(source_zip)) as archive:
            self.assertIn("src/app.txt", archive.namelist())
            self.assertNotIn("FORGETRACE_HISTORY.json", archive.namelist())
            self.assertNotIn(".git/config", archive.namelist())
            self.assertNotIn("outside-link.txt", archive.namelist())
            self.assertEqual(b"baseline\n", archive.read("src/app.txt"))

        status, _headers, pr = self.request_json(
            self.remote_server,
            "POST",
            "/api/v1/collaboration/pull-requests",
            {"authorName": "API Contributor", "title": "Improve app", "description": "E2E API test"},
            token_headers,
        )
        self.assertEqual(201, status)
        pr_id = pr["id"]

        status, _headers, own_requests = self.request_json(
            self.remote_server,
            "GET",
            "/api/v1/collaboration/pull-requests",
            None,
            token_headers,
        )
        self.assertEqual(200, status)
        self.assertEqual([pr_id], [item["id"] for item in own_requests["pullRequests"]])

        status, _headers, staged = self.request(
            self.remote_server,
            "POST",
            f"/api/v1/collaboration/pull-requests/{pr_id}/files?path=src%2Fapp.txt",
            body=b"baseline\ncontributor change\n",
            headers=token_headers,
        )
        self.assertEqual(201, status)
        self.assertEqual(1, json.loads(staged)["changeCount"])

        status, _headers, submitted = self.request_json(
            self.remote_server,
            "POST",
            f"/api/v1/collaboration/pull-requests/{pr_id}/submit",
            {},
            token_headers,
        )
        self.assertEqual(200, status)
        self.assertEqual("open", submitted["status"])

        status, _headers, approved = self.request_json(
            self.owner_server,
            "POST",
            f"/api/v1/repositories/{self.repository_id}/pull-requests/{pr_id}/review",
            {"reviewer": "Owner", "verdict": "approved", "comment": "Reviewed exact diff."},
        )
        self.assertEqual(200, status)
        self.assertEqual("approved", approved["status"])

        status, _headers, merged = self.request_json(
            self.owner_server,
            "POST",
            f"/api/v1/repositories/{self.repository_id}/pull-requests/{pr_id}/merge",
            {
                "mergedBy": "Owner",
                "confirmation": "MERGE #1",
                "expectedRevision": approved["revision"],
                "allowRiskyFiles": False,
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("merged", merged["status"])
        self.assertEqual(b"baseline\ncontributor change\n", (self.workspace / "src" / "app.txt").read_bytes())

    def test_source_download_scope_can_be_disabled(self) -> None:
        invite = self.app.collaboration.create_invite(
            self.repository_id, allow_source_download=False
        )
        status, _headers, raw = self.request(
            self.remote_server,
            "GET",
            "/api/v1/collaboration/source",
            headers={"X-ForgeTrace-Invite": invite["token"]},
        )
        self.assertEqual(403, status)
        self.assertEqual("source_download_not_allowed", json.loads(raw)["code"])


class SecureCollaborationMigrationTest(unittest.TestCase):
    def test_v1_invite_schema_adds_source_download_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            data_dir = root / "data"
            collaboration_dir = data_dir / "collaboration"
            collaboration_dir.mkdir(parents=True)
            database = collaboration_dir / "collaboration.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE collaboration_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE collaboration_invites (
                        id TEXT PRIMARY KEY,
                        repository_id TEXT NOT NULL,
                        token_hash TEXT NOT NULL UNIQUE,
                        label TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        max_uses INTEGER NOT NULL DEFAULT 1,
                        uses INTEGER NOT NULL DEFAULT 0,
                        revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
                        max_file_bytes INTEGER NOT NULL,
                        max_total_bytes INTEGER NOT NULL,
                        allow_deletes INTEGER NOT NULL DEFAULT 1 CHECK(allow_deletes IN (0,1)),
                        last_used_at TEXT NOT NULL DEFAULT ''
                    );
                    """
                )
            app = build_application(Path(__file__).resolve().parents[1], data_dir)
            with closing(sqlite3.connect(app.collaboration.db_path)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(collaboration_invites)")}
                version = connection.execute(
                    "SELECT value FROM collaboration_meta WHERE key='schema_version'"
                ).fetchone()[0]
            self.assertIn("allow_source_download", columns)
            self.assertEqual("6", version)


if __name__ == "__main__":
    unittest.main()
