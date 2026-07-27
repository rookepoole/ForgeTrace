from __future__ import annotations

import http.client
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from forgetrace.app import build_application
from forgetrace.errors import ForgeTraceError
from forgetrace.security_events import SecurityEventLedger
from forgetrace.web import make_handler

ROOT = Path(__file__).resolve().parents[1]


class ReviewConversationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "data"
        self.workspace = self.root / "repo"
        self.app = build_application(ROOT, self.data_dir)
        record = self.app.registry.register_repository(
            path=str(self.workspace),
            name="Review Conversations",
            description="v0.4.4 fixture",
            author="Owner",
            initialize=True,
            create_directory=True,
        )
        self.repository_id = record["id"]
        self.repository = self.app.registry.repository_service(self.repository_id)
        self.repository.write_file("src/app.txt", b"alpha\nbeta\n", "Owner", "seed")
        invite = self.app.collaboration.create_invite(
            self.repository_id,
            label="Reviewer",
            max_uses=5,
            max_file_bytes=1024 * 1024,
            max_total_bytes=4 * 1024 * 1024,
        )
        self.token = invite["token"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def submitted_pr(self, *, path: str = "src/app.txt", content: bytes = b"alpha\nbeta\ngamma\n") -> dict:
        pr = self.app.collaboration.create_pull_request(
            self.token,
            title="Improve application",
            description="Review conversation fixture",
            author_name="Contributor",
        )
        self.app.collaboration.upload_pull_request_file(self.token, pr["id"], path, content)
        return self.app.collaboration.submit_pull_request(self.token, pr["id"])

    def owner_thread(self, pr: dict, **overrides) -> dict:
        values = {
            "actor_name": "Owner",
            "body": "Please revise this line.",
            "submitted_revision": pr["revision"],
            "expected_pull_request_revision": pr["revision"],
            "path": "src/app.txt",
            "start_line": 3,
            "end_line": 3,
            "request_changes": False,
            "request_id": "req_owner_thread",
        }
        values.update(overrides)
        return self.app.collaboration.review_conversations.create_for_owner(
            self.repository_id, pr["id"], **values
        )


class ReviewConversationServiceTest(ReviewConversationFixture):
    def test_restart_persistence_revision_drift_and_immutable_context(self) -> None:
        submitted = self.submitted_pr()
        thread = self.owner_thread(submitted, request_changes=True)
        self.assertEqual("gamma", thread["context"]["excerpt"][0]["text"])
        self.assertFalse(thread["outdated"])
        revision_root = self.app.collaboration.review_conversations._revision_root(
            self.repository_id, submitted["id"], submitted["revision"]
        )
        self.assertTrue((revision_root / "manifest.json").is_file())

        restarted = build_application(ROOT, self.data_dir)
        persisted = restarted.collaboration.review_conversations.get_for_owner(
            self.repository_id, submitted["id"], thread["id"]
        )
        self.assertEqual(thread["createdAt"], persisted["createdAt"])
        self.assertEqual("gamma", persisted["context"]["excerpt"][0]["text"])

        revised = restarted.collaboration.upload_pull_request_file(
            self.token, submitted["id"], "src/app.txt", b"alpha\nbeta\ndelta\n"
        )
        self.assertEqual("draft", revised["status"])
        resubmitted = restarted.collaboration.submit_pull_request(self.token, submitted["id"])
        self.assertGreater(resubmitted["revision"], submitted["revision"])
        old_thread = restarted.collaboration.review_conversations.get_for_owner(
            self.repository_id, submitted["id"], thread["id"]
        )
        self.assertTrue(old_thread["outdated"])
        self.assertEqual("gamma", old_thread["context"]["excerpt"][0]["text"])
        self.assertEqual(2, len(resubmitted["submittedRevisions"]))

    def test_unresolved_current_thread_blocks_approval_and_merge(self) -> None:
        submitted = self.submitted_pr()
        thread = self.owner_thread(submitted)
        with self.assertRaises(ForgeTraceError) as approval:
            self.app.collaboration.review_pull_request(
                self.repository_id,
                submitted["id"],
                reviewer="Owner",
                verdict="approved",
                expected_revision=submitted["revision"],
            )
        self.assertEqual("unresolved_review_threads", approval.exception.code)

        resolved = self.app.collaboration.review_conversations.resolve_owner(
            self.repository_id,
            submitted["id"],
            thread["id"],
            actor_name="Owner",
            expected_version=thread["version"],
            request_id="req_resolve",
        )
        self.assertTrue(resolved["resolved"])
        approved = self.app.collaboration.review_pull_request(
            self.repository_id,
            submitted["id"],
            reviewer="Owner",
            verdict="approved",
            expected_revision=submitted["revision"],
        )
        self.assertEqual("approved", approved["status"])

        reopened = self.app.collaboration.review_conversations.reopen_owner(
            self.repository_id,
            submitted["id"],
            thread["id"],
            actor_name="Owner",
            expected_version=resolved["version"],
            request_id="req_reopen",
        )
        self.assertFalse(reopened["resolved"])
        current = self.app.collaboration.get_pull_request(self.repository_id, submitted["id"])
        self.assertEqual("open", current["status"])
        with self.assertRaises(ForgeTraceError) as merge:
            self.app.collaboration.merge_pull_request(
                self.repository_id,
                submitted["id"],
                merged_by="Owner",
                confirmation="MERGE #1",
                expected_revision=submitted["revision"],
            )
        self.assertIn(merge.exception.code, {"pull_request_not_approved", "unresolved_review_threads"})

    def test_roles_concurrency_and_request_ids(self) -> None:
        submitted = self.submitted_pr()
        thread = self.app.collaboration.review_conversations.create_for_token(
            self.token,
            submitted["id"],
            body="Can you clarify the expected behavior?",
            submitted_revision=submitted["revision"],
            expected_pull_request_revision=submitted["revision"],
            path="src/app.txt",
            start_line=2,
            end_line=2,
            request_id="req_contributor_create",
        )
        self.assertEqual("contributor", thread["createdByRole"])
        self.assertEqual("Contributor", thread["createdByName"])
        self.assertEqual("req_contributor_create", thread["createdRequestId"])

        owner_reply = self.app.collaboration.review_conversations.reply_owner(
            self.repository_id,
            submitted["id"],
            thread["id"],
            actor_name="Owner",
            body="Keep the existing behavior and add the new case.",
            expected_version=thread["version"],
            request_id="req_owner_reply",
        )
        self.assertEqual(2, owner_reply["version"])
        self.assertEqual("owner", owner_reply["comments"][-1]["authorRole"])
        self.assertEqual("req_owner_reply", owner_reply["comments"][-1]["requestId"])

        with self.assertRaises(ForgeTraceError) as stale:
            self.app.collaboration.review_conversations.reply_token(
                self.token,
                submitted["id"],
                thread["id"],
                body="This reply used a stale version.",
                expected_version=thread["version"],
                request_id="req_stale",
            )
        self.assertEqual("review_thread_version_changed", stale.exception.code)
        refreshed = self.app.collaboration.review_conversations.get_for_owner(
            self.repository_id, submitted["id"], thread["id"]
        )
        self.assertEqual(2, len(refreshed["comments"]))

    def test_path_range_active_content_and_integrity_validation(self) -> None:
        submitted = self.submitted_pr(
            path="review.html",
            content=b"<script>alert('x')</script>\n<div>plain evidence</div>\n",
        )
        thread = self.app.collaboration.review_conversations.create_for_owner(
            self.repository_id,
            submitted["id"],
            actor_name="Owner",
            body="Review as inert text only.",
            submitted_revision=submitted["revision"],
            expected_pull_request_revision=submitted["revision"],
            path="review.html",
            start_line=1,
            end_line=2,
            request_id="req_html",
        )
        self.assertFalse(thread["context"]["activeContentRendered"])
        self.assertIn("<script>", thread["context"]["excerpt"][0]["text"])

        with self.assertRaises(ForgeTraceError) as traversal:
            self.app.collaboration.review_conversations.create_for_owner(
                self.repository_id,
                submitted["id"],
                actor_name="Owner",
                body="bad",
                submitted_revision=submitted["revision"],
                expected_pull_request_revision=submitted["revision"],
                path="../review.html",
                request_id="req_bad",
            )
        self.assertIn(traversal.exception.code, {"path_traversal", "invalid_path", "invalid_request", "review_path_not_in_revision"})

        with self.assertRaises(ForgeTraceError) as range_error:
            self.app.collaboration.review_conversations.create_for_owner(
                self.repository_id,
                submitted["id"],
                actor_name="Owner",
                body="outside",
                submitted_revision=submitted["revision"],
                expected_pull_request_revision=submitted["revision"],
                path="review.html",
                start_line=3,
                end_line=3,
                request_id="req_range",
            )
        self.assertEqual("review_line_range_out_of_bounds", range_error.exception.code)

        revision_file = self.app.collaboration.review_conversations._revision_file(
            self.repository_id, submitted["id"], submitted["revision"], "review.html"
        )
        revision_file.write_text("tampered", encoding="utf-8")
        with self.assertRaises(ForgeTraceError) as tamper:
            self.app.collaboration.review_conversations.get_for_owner(
                self.repository_id, submitted["id"], thread["id"]
            )
        self.assertEqual("review_revision_integrity_failed", tamper.exception.code)

    def test_old_unresolved_threads_do_not_retarget_or_block_new_revision(self) -> None:
        submitted = self.submitted_pr()
        old_thread = self.owner_thread(submitted, request_changes=True)
        self.app.collaboration.upload_pull_request_file(
            self.token, submitted["id"], "src/app.txt", b"alpha\nbeta\naddressed\n"
        )
        current = self.app.collaboration.submit_pull_request(self.token, submitted["id"])
        detail = self.app.collaboration.get_pull_request(self.repository_id, submitted["id"])
        self.assertEqual(1, detail["reviewConversation"]["unresolvedThreadCount"])
        self.assertEqual(0, detail["reviewConversation"]["unresolvedCurrentRevisionCount"])
        old = self.app.collaboration.review_conversations.get_for_owner(
            self.repository_id, submitted["id"], old_thread["id"]
        )
        self.assertTrue(old["outdated"])
        approved = self.app.collaboration.review_pull_request(
            self.repository_id,
            submitted["id"],
            reviewer="Owner",
            verdict="approved",
            expected_revision=current["revision"],
        )
        self.assertEqual("approved", approved["status"])

    def test_read_only_repository_allows_review_but_blocks_merge(self) -> None:
        submitted = self.submitted_pr()
        self.app.registry.set_access_mode(self.repository_id, "read_only")
        thread = self.owner_thread(submitted, request_changes=True)
        replied = self.app.collaboration.review_conversations.reply_token(
            self.token,
            submitted["id"],
            thread["id"],
            body="Acknowledged.",
            expected_version=thread["version"],
            request_id="req_readonly_reply",
        )
        resolved = self.app.collaboration.review_conversations.resolve_owner(
            self.repository_id,
            submitted["id"],
            thread["id"],
            actor_name="Owner",
            expected_version=replied["version"],
            request_id="req_readonly_resolve",
        )
        self.assertTrue(resolved["resolved"])
        approved = self.app.collaboration.review_pull_request(
            self.repository_id,
            submitted["id"],
            reviewer="Owner",
            verdict="approved",
            expected_revision=submitted["revision"],
        )
        with self.assertRaises(ForgeTraceError) as blocked:
            self.app.collaboration.merge_pull_request(
                self.repository_id,
                submitted["id"],
                merged_by="Owner",
                confirmation="MERGE #1",
                expected_revision=approved["revision"],
            )
        self.assertEqual("repository_read_only", blocked.exception.code)

    def test_pagination_and_terminal_retention(self) -> None:
        submitted = self.submitted_pr()
        for number in range(3):
            self.owner_thread(
                submitted,
                body=f"Thread {number}",
                start_line=None,
                end_line=None,
                request_id=f"req_{number}",
            )
        first = self.app.collaboration.review_conversations.list_for_owner(
            self.repository_id, submitted["id"], limit=2, cursor=0, comment_limit=1
        )
        self.assertEqual(2, len(first["threads"]))
        self.assertIsNotNone(first["nextCursor"])
        second = self.app.collaboration.review_conversations.list_for_owner(
            self.repository_id, submitted["id"], limit=2, cursor=first["nextCursor"], comment_limit=1
        )
        self.assertEqual(1, len(second["threads"]))

        self.app.collaboration.close_pull_request(self.repository_id, submitted["id"])
        old = (datetime.now(timezone.utc) - timedelta(days=181)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with closing(sqlite3.connect(self.app.collaboration.db_path)) as connection:
            connection.execute("UPDATE pull_requests SET updated_at=? WHERE id=?", (old, submitted["id"]))
            connection.commit()
        result = self.app.collaboration.review_conversations.cleanup_retention()
        self.assertEqual(1, result["reviewRevisions"])
        with closing(sqlite3.connect(self.app.collaboration.db_path)) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM review_threads WHERE pull_request_id=?", (submitted["id"],)
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM pull_requests WHERE id=?", (submitted["id"],)
            ).fetchone()[0])

    def test_persistent_abuse_limits_block_before_insertion(self) -> None:
        submitted = self.submitted_pr()
        thread = self.owner_thread(submitted)
        with mock.patch("forgetrace.review_conversations.MAX_THREADS_PER_PULL_REQUEST", 1):
            with self.assertRaises(ForgeTraceError) as thread_limit:
                self.owner_thread(submitted, body="too many")
        self.assertEqual("review_thread_limit_reached", thread_limit.exception.code)

        with mock.patch("forgetrace.review_conversations.MAX_COMMENTS_PER_THREAD", 1):
            with self.assertRaises(ForgeTraceError) as comment_limit:
                self.app.collaboration.review_conversations.reply_token(
                    self.token, submitted["id"], thread["id"],
                    body="too many comments", expected_version=thread["version"],
                    request_id="req_limit",
                )
        self.assertEqual("review_thread_comment_limit_reached", comment_limit.exception.code)
        current = self.app.collaboration.review_conversations.get_for_owner(
            self.repository_id, submitted["id"], thread["id"]
        )
        self.assertEqual(1, len(current["comments"]))
        self.assertEqual(thread["version"], current["version"])

    def test_contributor_thread_invalidates_existing_approval(self) -> None:
        submitted = self.submitted_pr()
        approved = self.app.collaboration.review_pull_request(
            self.repository_id, submitted["id"], reviewer="Owner", verdict="approved",
            expected_revision=submitted["revision"],
        )
        self.assertEqual("approved", approved["status"])
        self.app.collaboration.review_conversations.create_for_token(
            self.token, submitted["id"], body="A new question after approval.",
            submitted_revision=submitted["revision"],
            expected_pull_request_revision=submitted["revision"],
            path="src/app.txt", request_id="req_after_approval",
        )
        current = self.app.collaboration.get_pull_request(self.repository_id, submitted["id"])
        self.assertEqual("open", current["status"])
        self.assertEqual(1, current["reviewConversation"]["unresolvedCurrentRevisionCount"])

    def test_tampered_security_ledger_blocks_resolution_before_state_change(self) -> None:
        submitted = self.submitted_pr()
        thread = self.owner_thread(submitted)
        ledger = SecurityEventLedger(self.data_dir)
        with closing(sqlite3.connect(ledger.db_path)) as connection:
            connection.execute("DROP TRIGGER security_events_no_update")
            connection.commit()
        with self.assertRaises(ForgeTraceError) as blocked:
            self.app.collaboration.review_conversations.resolve_owner(
                self.repository_id,
                submitted["id"],
                thread["id"],
                actor_name="Owner",
                expected_version=thread["version"],
                request_id="req_blocked",
            )
        self.assertEqual("security_event_ledger_unavailable", blocked.exception.code)
        unchanged = self.app.collaboration.review_conversations.get_for_owner(
            self.repository_id, submitted["id"], thread["id"]
        )
        self.assertFalse(unchanged["resolved"])
        self.assertEqual(thread["version"], unchanged["version"])


class ReviewConversationMigrationTest(unittest.TestCase):
    def test_v3_open_pull_request_migrates_through_schema_six_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            data_dir = root / "data"
            app = build_application(ROOT, data_dir)
            record = app.registry.register_repository(
                path=str(root / "repo"), name="Migration", author="Owner",
                initialize=True, create_directory=True,
            )
            repo = app.registry.repository_service(record["id"])
            repo.write_file("file.txt", b"old\n", "Owner", "seed")
            invite = app.collaboration.create_invite(record["id"], max_uses=2)
            token = invite["token"]
            pr = app.collaboration.create_pull_request(token, title="Migrate", description="", author_name="Contributor")
            app.collaboration.upload_pull_request_file(token, pr["id"], "file.txt", b"new\n")
            submitted = app.collaboration.submit_pull_request(token, pr["id"])

            shutil.rmtree(app.collaboration.review_conversations.revisions_dir, ignore_errors=True)
            with closing(sqlite3.connect(app.collaboration.db_path)) as connection:
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.executescript(
                    """
                    DROP TABLE review_thread_events;
                    DROP TABLE review_comments;
                    DROP TABLE review_threads;
                    DROP TABLE pull_request_revisions;
                    UPDATE collaboration_meta SET value='3' WHERE key='schema_version';
                    """
                )
                connection.commit()

            migrated = build_application(ROOT, data_dir)
            with closing(sqlite3.connect(migrated.collaboration.db_path)) as connection:
                version = connection.execute(
                    "SELECT value FROM collaboration_meta WHERE key='schema_version'"
                ).fetchone()[0]
                count = connection.execute(
                    "SELECT COUNT(*) FROM pull_request_revisions WHERE pull_request_id=? AND revision=?",
                    (pr["id"], submitted["revision"]),
                ).fetchone()[0]
            self.assertEqual("6", version)
            self.assertEqual(1, count)
            detail = migrated.collaboration.get_pull_request(record["id"], pr["id"])
            self.assertEqual("complete", detail["submittedRevisions"][0]["snapshotState"])


class ReviewConversationApiTest(ReviewConversationFixture):
    def setUp(self) -> None:
        super().setUp()
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
        for server, thread in ((self.owner_server, self.owner_thread), (self.remote_server, self.remote_thread)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        super().tearDown()

    @staticmethod
    def request_json(server, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), json.loads(raw) if raw else {}
        connection.close()
        return result

    def test_owner_contributor_exchange_and_gateway_isolation(self) -> None:
        submitted = self.submitted_pr()
        token_headers = {"X-ForgeTrace-Invite": self.token}
        contributor_path = f"/api/v1/collaboration/pull-requests/{submitted['id']}/review-threads"
        status, headers, thread = self.request_json(
            self.remote_server,
            "POST",
            contributor_path,
            {
                "body": "Contributor question",
                "submittedRevision": submitted["revision"],
                "expectedPullRequestRevision": submitted["revision"],
                "path": "src/app.txt",
                "startLine": 2,
                "endLine": 2,
            },
            token_headers,
        )
        self.assertEqual(201, status)
        self.assertTrue(headers.get("X-ForgeTrace-Request-Id"))
        self.assertEqual(headers["X-ForgeTrace-Request-Id"], thread["createdRequestId"])

        owner_path = (
            f"/api/v1/repositories/{self.repository_id}/pull-requests/{submitted['id']}"
            f"/review-threads/{thread['id']}/comments"
        )
        status, _headers, replied = self.request_json(
            self.owner_server,
            "POST",
            owner_path,
            {"actorName": "Owner", "body": "Owner answer", "expectedVersion": thread["version"]},
        )
        self.assertEqual(201, status)
        self.assertEqual("owner", replied["comments"][-1]["authorRole"])

        status, _headers, listed = self.request_json(
            self.remote_server, "GET", contributor_path, None, token_headers
        )
        self.assertEqual(200, status)
        self.assertEqual([thread["id"]], [item["id"] for item in listed["threads"]])

        resolve_path = (
            f"/api/v1/repositories/{self.repository_id}/pull-requests/{submitted['id']}"
            f"/review-threads/{thread['id']}/resolve"
        )
        status, _headers, denied = self.request_json(
            self.remote_server,
            "POST",
            resolve_path,
            {"actorName": "Contributor", "expectedVersion": replied["version"]},
            token_headers,
        )
        self.assertEqual(403, status)
        self.assertIn(denied["code"], {"local_owner_required", "remote_owner_route_denied", "remote_owner_api_blocked"})

        status, _headers, denied = self.request_json(
            self.remote_server,
            "GET",
            f"/api/v1/repositories/{self.repository_id}/pull-requests/{submitted['id']}/review-threads",
            None,
            token_headers,
        )
        self.assertEqual(403, status)
        self.assertIn(denied["code"], {"local_owner_required", "remote_owner_route_denied", "remote_owner_api_blocked"})

        other = self.app.collaboration.create_invite(self.repository_id, max_uses=1)
        status, _headers, denied = self.request_json(
            self.remote_server,
            "GET",
            contributor_path,
            None,
            {"X-ForgeTrace-Invite": other["token"]},
        )
        self.assertEqual(404, status)
        self.assertEqual("pull_request_not_found", denied["code"])


if __name__ == "__main__":
    unittest.main()
