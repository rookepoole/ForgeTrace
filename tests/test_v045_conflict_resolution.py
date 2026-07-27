from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from forgetrace.app import build_application
from forgetrace.conflict_resolution import MAX_INLINE_RESOLUTION_BYTES
from forgetrace.errors import ForgeTraceError
from forgetrace.security_events import SecurityEventLedger
from forgetrace.web import make_handler

ROOT = Path(__file__).resolve().parents[1]


class ConflictResolutionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "data"
        self.workspace = self.root / "repo"
        self.app = build_application(ROOT, self.data_dir)
        record = self.app.registry.register_repository(
            path=str(self.workspace),
            name="Conflict Resolution",
            description="v0.4.5 fixture",
            author="Owner",
            initialize=True,
            create_directory=True,
        )
        self.repository_id = record["id"]
        self.repository = self.app.registry.repository_service(self.repository_id)
        self.repository.write_file("src/app.txt", b"base\nshared\n", "Owner", "seed")
        invite = self.app.collaboration.create_invite(
            self.repository_id,
            label="Contributor",
            max_uses=10,
            max_file_bytes=2 * 1024 * 1024,
            max_total_bytes=8 * 1024 * 1024,
        )
        self.token = invite["token"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def submitted_pr(
        self,
        *,
        changes: dict[str, bytes] | None = None,
        deletions: list[str] | None = None,
    ) -> dict:
        pr = self.app.collaboration.create_pull_request(
            self.token,
            title="Conflicting contribution",
            description="v0.4.5 fixture",
            author_name="Contributor",
        )
        for path, content in (changes or {"src/app.txt": b"incoming\nshared\n"}).items():
            self.app.collaboration.upload_pull_request_file(self.token, pr["id"], path, content)
        for path in deletions or []:
            self.app.collaboration.add_pull_request_deletion(self.token, pr["id"], path)
        return self.app.collaboration.submit_pull_request(self.token, pr["id"])

    def conflict(self, submitted: dict, *, path: str = "src/app.txt", content: bytes = b"current\nshared\n") -> dict:
        self.repository.write_file(path, content, "Owner", "concurrent owner edit")
        return self.app.collaboration.conflict_resolutions.prepare_owner(
            self.repository_id,
            submitted["id"],
            actor_name="Owner",
            expected_pull_request_revision=submitted["revision"],
            request_id="req_prepare",
        )

    def save_and_confirm(
        self,
        submitted: dict,
        draft: dict,
        *,
        decision: str,
        manual_text: str | None = None,
    ) -> dict:
        saved = self.app.collaboration.conflict_resolutions.save_decision_owner(
            self.repository_id,
            submitted["id"],
            draft["id"],
            actor_name="Owner",
            decision=decision,
            manual_text=manual_text,
            expected_version=draft["version"],
            request_id="req_save",
        )
        return self.app.collaboration.conflict_resolutions.confirm_owner(
            self.repository_id,
            submitted["id"],
            draft["id"],
            actor_name="Owner",
            expected_version=saved["version"],
            request_id="req_confirm",
        )

    def approve_and_merge(self, submitted: dict) -> dict:
        approved = self.app.collaboration.review_pull_request(
            self.repository_id,
            submitted["id"],
            reviewer="Owner",
            verdict="approved",
            expected_revision=submitted["revision"],
        )
        return self.app.collaboration.merge_pull_request(
            self.repository_id,
            submitted["id"],
            merged_by="Owner",
            confirmation=f"MERGE #{approved['number']}",
            expected_revision=submitted["revision"],
            request_id="req_merge",
        )


class ConflictResolutionServiceTest(ConflictResolutionFixture):
    def test_manual_text_resolution_preserves_three_way_evidence_and_merges(self) -> None:
        submitted = self.submitted_pr()
        model = self.conflict(submitted)
        self.assertEqual(1, model["conflictCount"])
        item = model["conflicts"][0]
        draft = item["draft"]
        self.assertEqual("base\nshared\n", draft["evidence"]["base"]["textContent"])
        self.assertEqual("current\nshared\n", draft["evidence"]["current"]["textContent"])
        self.assertEqual("incoming\nshared\n", draft["evidence"]["incoming"]["textContent"])
        self.assertTrue(draft["inlineEligible"])
        self.assertFalse(draft["activeContentRendered"])

        confirmed = self.save_and_confirm(
            submitted, draft, decision="manual", manual_text="resolved\nshared\n"
        )
        self.assertEqual("confirmed", confirmed["status"])
        self.assertEqual("resolved\nshared\n", confirmed["evidence"]["resolvedTextContent"])
        merged = self.approve_and_merge(submitted)
        self.assertEqual("merged", merged["status"])
        self.assertEqual("resolved\nshared\n", self.repository.read_file("src/app.txt")["content"])
        self.assertTrue(merged["conflictResolution"]["complete"])

    def test_current_incoming_and_delete_decisions(self) -> None:
        # Keeping current may create an intentionally empty merge but still records the PR merge.
        submitted = self.submitted_pr()
        model = self.conflict(submitted)
        self.save_and_confirm(submitted, model["conflicts"][0]["draft"], decision="current")
        merged = self.approve_and_merge(submitted)
        self.assertEqual("current\nshared\n", self.repository.read_file("src/app.txt")["content"])
        self.assertEqual("merged", merged["status"])

        # A deletion conflict may explicitly preserve the current file or delete it.
        self.repository.write_file("remove.txt", b"base removal\n", "Owner", "seed removal")
        deletion = self.submitted_pr(changes={}, deletions=["remove.txt"])
        self.repository.write_file("remove.txt", b"owner changed\n", "Owner", "change removal")
        model = self.app.collaboration.conflict_resolutions.prepare_owner(
            self.repository_id,
            deletion["id"],
            actor_name="Owner",
            expected_pull_request_revision=deletion["revision"],
        )
        draft = model["conflicts"][0]["draft"]
        self.assertEqual("deletion", draft["submittedKind"])
        self.assertEqual("absent", draft["evidence"]["incoming"]["kind"])
        self.save_and_confirm(deletion, draft, decision="delete")
        self.approve_and_merge(deletion)
        self.assertFalse((self.workspace / "remove.txt").exists())

    def test_binary_and_oversized_evidence_disables_manual_resolution(self) -> None:
        self.repository.write_file("blob.bin", b"\x00base", "Owner", "seed binary")
        submitted = self.submitted_pr(changes={"blob.bin": b"\x00incoming"})
        self.repository.write_file("blob.bin", b"\x00current", "Owner", "change binary")
        model = self.app.collaboration.conflict_resolutions.prepare_owner(
            self.repository_id, submitted["id"], actor_name="Owner",
            expected_pull_request_revision=submitted["revision"],
        )
        draft = model["conflicts"][0]["draft"]
        self.assertFalse(draft["inlineEligible"])
        with self.assertRaises(ForgeTraceError) as manual:
            self.app.collaboration.conflict_resolutions.save_decision_owner(
                self.repository_id, submitted["id"], draft["id"], actor_name="Owner",
                decision="manual", manual_text="unsafe", expected_version=draft["version"],
            )
        self.assertEqual("manual_conflict_resolution_unavailable", manual.exception.code)
        self.save_and_confirm(submitted, draft, decision="incoming")
        self.approve_and_merge(submitted)
        self.assertEqual(b"\x00incoming", (self.workspace / "blob.bin").read_bytes())

        self.repository.write_file("large.txt", b"a" * (MAX_INLINE_RESOLUTION_BYTES + 1), "Owner", "seed large")
        large = self.submitted_pr(changes={"large.txt": b"b" * (MAX_INLINE_RESOLUTION_BYTES + 1)})
        self.repository.write_file("large.txt", b"c" * (MAX_INLINE_RESOLUTION_BYTES + 1), "Owner", "change large")
        model = self.app.collaboration.conflict_resolutions.prepare_owner(
            self.repository_id, large["id"], actor_name="Owner",
            expected_pull_request_revision=large["revision"],
        )
        self.assertFalse(model["conflicts"][0]["draft"]["inlineEligible"])

    def test_new_path_conflict_and_rename_equivalent_metadata(self) -> None:
        self.repository.write_file("old.txt", b"rename payload\n", "Owner", "seed rename")
        submitted = self.submitted_pr(
            changes={"new.txt": b"rename payload\n"}, deletions=["old.txt"]
        )
        self.repository.write_file("new.txt", b"owner path\n", "Owner", "create destination")
        model = self.app.collaboration.conflict_resolutions.prepare_owner(
            self.repository_id, submitted["id"], actor_name="Owner",
            expected_pull_request_revision=submitted["revision"],
        )
        by_path = {item["path"]: item for item in model["conflicts"]}
        self.assertEqual("new_path_now_exists", by_path["new.txt"]["reason"])
        self.assertEqual("old.txt", by_path["new.txt"]["renameFrom"])
        # old.txt itself is not a conflict until the owner changes it; the rename hint is evidence only.
        self.assertNotIn("old.txt", by_path)

    def test_all_conflicts_require_confirmation_and_merge_uses_immutable_revision_bytes(self) -> None:
        self.repository.write_file("two.txt", b"base two\n", "Owner", "seed two")
        submitted = self.submitted_pr(changes={
            "src/app.txt": b"incoming one\n",
            "two.txt": b"incoming two\n",
            "clean.txt": b"immutable clean\n",
        })
        self.repository.write_file("src/app.txt", b"current one\n", "Owner", "change one")
        self.repository.write_file("two.txt", b"current two\n", "Owner", "change two")
        model = self.app.collaboration.conflict_resolutions.prepare_owner(
            self.repository_id, submitted["id"], actor_name="Owner",
            expected_pull_request_revision=submitted["revision"],
        )
        self.assertEqual(2, model["conflictCount"])
        first = model["conflicts"][0]["draft"]
        self.save_and_confirm(submitted, first, decision="incoming")
        with self.assertRaises(ForgeTraceError) as approval:
            self.app.collaboration.review_pull_request(
                self.repository_id, submitted["id"], reviewer="Owner", verdict="approved",
                expected_revision=submitted["revision"],
            )
        self.assertEqual("conflict_resolution_required", approval.exception.code)
        refreshed = self.app.collaboration.conflict_resolutions.list_owner(
            self.repository_id, submitted["id"]
        )
        second = next(item["draft"] for item in refreshed["conflicts"] if item["draft"]["status"] != "confirmed")
        self.save_and_confirm(submitted, second, decision="incoming")

        # Mutating the old working quarantine file must not affect the immutable submitted revision used by merge.
        staged_clean = self.app.collaboration._staged_path(self.repository_id, submitted["id"], "clean.txt")
        staged_clean.write_bytes(b"tampered mutable quarantine\n")
        self.approve_and_merge(submitted)
        self.assertEqual("immutable clean\n", self.repository.read_file("clean.txt")["content"])

    def test_stale_bindings_cover_repository_revision_threads_and_access_mode(self) -> None:
        submitted = self.submitted_pr()
        model = self.conflict(submitted)
        draft = model["conflicts"][0]["draft"]
        self.repository.write_file("unrelated.txt", b"drift\n", "Owner", "digest drift")
        stale = self.app.collaboration.conflict_resolutions.get_owner(
            self.repository_id, submitted["id"], draft["id"]
        )
        self.assertEqual("stale", stale["status"])
        self.assertIn("repository_digest_changed", stale["staleReasons"])

        # Fresh draft becomes stale when the current revision's review gate changes.
        model = self.app.collaboration.conflict_resolutions.prepare_owner(
            self.repository_id, submitted["id"], actor_name="Owner",
            expected_pull_request_revision=submitted["revision"],
        )
        fresh = model["conflicts"][0]["draft"]
        self.app.collaboration.review_conversations.create_for_owner(
            self.repository_id, submitted["id"], actor_name="Owner", body="Resolve this too",
            submitted_revision=submitted["revision"], expected_pull_request_revision=submitted["revision"],
            path="src/app.txt", request_id="req_thread",
        )
        stale = self.app.collaboration.conflict_resolutions.get_owner(
            self.repository_id, submitted["id"], fresh["id"]
        )
        self.assertIn("review_threads_changed", stale["staleReasons"])

        # A mode transition changes the access binding and requires a fresh draft.
        model = self.app.collaboration.conflict_resolutions.prepare_owner(
            self.repository_id, submitted["id"], actor_name="Owner",
            expected_pull_request_revision=submitted["revision"],
        )
        mode_draft = model["conflicts"][0]["draft"]
        self.app.registry.set_access_mode(self.repository_id, "read_only")
        stale = self.app.collaboration.conflict_resolutions.get_owner(
            self.repository_id, submitted["id"], mode_draft["id"]
        )
        self.assertIn("access_mode_changed", stale["staleReasons"])

    def test_read_only_allows_resolution_but_blocks_merge(self) -> None:
        submitted = self.submitted_pr()
        model = self.conflict(submitted)
        self.app.registry.set_access_mode(self.repository_id, "read_only")
        # The prior draft is stale, so prepare against the read-only binding.
        model = self.app.collaboration.conflict_resolutions.prepare_owner(
            self.repository_id, submitted["id"], actor_name="Owner",
            expected_pull_request_revision=submitted["revision"],
        )
        draft = model["conflicts"][0]["draft"]
        self.save_and_confirm(submitted, draft, decision="incoming")
        approved = self.app.collaboration.review_pull_request(
            self.repository_id, submitted["id"], reviewer="Owner", verdict="approved",
            expected_revision=submitted["revision"],
        )
        with self.assertRaises(ForgeTraceError) as blocked:
            self.app.collaboration.merge_pull_request(
                self.repository_id, submitted["id"], merged_by="Owner",
                confirmation=f"MERGE #{approved['number']}", expected_revision=submitted["revision"],
            )
        self.assertEqual("repository_read_only", blocked.exception.code)
        self.assertEqual("current\nshared\n", self.repository.read_file("src/app.txt")["content"])

    def test_security_ledger_failure_blocks_confirmation_before_state_change(self) -> None:
        submitted = self.submitted_pr()
        model = self.conflict(submitted)
        draft = model["conflicts"][0]["draft"]
        saved = self.app.collaboration.conflict_resolutions.save_decision_owner(
            self.repository_id, submitted["id"], draft["id"], actor_name="Owner",
            decision="incoming", expected_version=draft["version"],
        )
        ledger = SecurityEventLedger(self.data_dir)
        with closing(sqlite3.connect(ledger.db_path)) as connection:
            connection.execute("DROP TRIGGER security_events_no_update")
            connection.commit()
        with self.assertRaises(ForgeTraceError) as blocked:
            self.app.collaboration.conflict_resolutions.confirm_owner(
                self.repository_id, submitted["id"], draft["id"], actor_name="Owner",
                expected_version=saved["version"], request_id="req_blocked",
            )
        self.assertEqual("security_event_ledger_unavailable", blocked.exception.code)
        current = self.app.collaboration.conflict_resolutions.get_owner(
            self.repository_id, submitted["id"], draft["id"]
        )
        self.assertEqual("draft", current["status"])
        self.assertEqual(saved["version"], current["version"])

    def test_evidence_tampering_and_path_escape_fail_closed(self) -> None:
        submitted = self.submitted_pr()
        model = self.conflict(submitted)
        draft = model["conflicts"][0]["draft"]
        root = self.app.collaboration.conflict_resolutions._draft_root(
            self.repository_id, submitted["id"], draft["id"]
        )
        current_path = root / "current.bin"
        original = current_path.read_bytes()
        current_path.unlink()
        with self.assertRaises(ForgeTraceError) as missing:
            self.app.collaboration.conflict_resolutions.get_owner(
                self.repository_id, submitted["id"], draft["id"]
            )
        self.assertEqual("conflict_resolution_integrity_failed", missing.exception.code)
        current_path.write_bytes(original)
        (root / "current.bin").write_bytes(b"tampered")
        with self.assertRaises(ForgeTraceError) as integrity:
            self.app.collaboration.conflict_resolutions.get_owner(
                self.repository_id, submitted["id"], draft["id"]
            )
        self.assertEqual("conflict_resolution_integrity_failed", integrity.exception.code)
        with self.assertRaises(ForgeTraceError) as escape:
            self.app.collaboration.conflict_resolutions._draft_root("..", submitted["id"], draft["id"])
        self.assertEqual("conflict_resolution_path_escape", escape.exception.code)

    def test_manual_line_limit_and_evidence_storage_preflights(self) -> None:
        submitted = self.submitted_pr()
        model = self.conflict(submitted)
        draft = model["conflicts"][0]["draft"]
        with mock.patch("forgetrace.conflict_resolution.MAX_RESOLUTION_TEXT_LINES", 1):
            with self.assertRaises(ForgeTraceError) as too_many_lines:
                self.app.collaboration.conflict_resolutions.save_decision_owner(
                    self.repository_id, submitted["id"], draft["id"], actor_name="Owner",
                    decision="manual", manual_text="one\ntwo\n", expected_version=draft["version"],
                )
        self.assertEqual("manual_resolution_too_many_lines", too_many_lines.exception.code)

        limited = self.submitted_pr(changes={"quota.txt": b"incoming quota\n"})
        self.repository.write_file("quota.txt", b"current quota\n", "Owner", "create conflict")
        with mock.patch("forgetrace.conflict_resolution.MAX_RESOLUTION_EVIDENCE_BYTES_PER_PULL_REQUEST", 1):
            with self.assertRaises(ForgeTraceError) as quota:
                self.app.collaboration.conflict_resolutions.prepare_owner(
                    self.repository_id, limited["id"], actor_name="Owner",
                    expected_pull_request_revision=limited["revision"],
                )
        self.assertEqual("conflict_resolution_evidence_limit_reached", quota.exception.code)

        with mock.patch("forgetrace.conflict_resolution.shutil.disk_usage", return_value=mock.Mock(free=0)):
            with self.assertRaises(ForgeTraceError) as space:
                self.app.collaboration.conflict_resolutions.prepare_owner(
                    self.repository_id, limited["id"], actor_name="Owner",
                    expected_pull_request_revision=limited["revision"],
                )
        self.assertEqual("insufficient_conflict_resolution_space", space.exception.code)

    def test_restart_persistence_and_transactional_merge_failure(self) -> None:
        submitted = self.submitted_pr()
        model = self.conflict(submitted)
        draft = model["conflicts"][0]["draft"]
        confirmed = self.save_and_confirm(submitted, draft, decision="manual", manual_text="resolved\n")

        restarted = build_application(ROOT, self.data_dir)
        persisted = restarted.collaboration.conflict_resolutions.get_owner(
            self.repository_id, submitted["id"], draft["id"]
        )
        self.assertEqual("confirmed", persisted["status"])
        self.assertEqual(confirmed["resolvedHash"], persisted["resolvedHash"])
        approved = restarted.collaboration.review_pull_request(
            self.repository_id, submitted["id"], reviewer="Owner", verdict="approved",
            expected_revision=submitted["revision"],
        )
        repo = restarted.registry.repository_service(self.repository_id)
        before = repo.read_file("src/app.txt")["content"]
        with mock.patch("forgetrace.repository.ForgeTraceRepository.merge_pull_request", side_effect=RuntimeError("forced merge failure")):
            with self.assertRaises(RuntimeError):
                restarted.collaboration.merge_pull_request(
                    self.repository_id, submitted["id"], merged_by="Owner",
                    confirmation=f"MERGE #{approved['number']}", expected_revision=submitted["revision"],
                    request_id="req_fail",
                )
        self.assertEqual(before, repo.read_file("src/app.txt")["content"])
        still = restarted.collaboration.conflict_resolutions.get_owner(
            self.repository_id, submitted["id"], draft["id"]
        )
        self.assertEqual("confirmed", still["status"])


class ConflictResolutionMigrationTest(unittest.TestCase):
    def test_v4_database_migrates_through_schema_six(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            app = build_application(ROOT, root / "data")
            with closing(sqlite3.connect(app.collaboration.db_path)) as connection:
                connection.executescript(
                    """
                    DROP TABLE conflict_resolution_events;
                    DROP TABLE conflict_resolution_drafts;
                    UPDATE collaboration_meta SET value='4' WHERE key='schema_version';
                    """
                )
                connection.commit()
            migrated = build_application(ROOT, root / "data")
            with closing(sqlite3.connect(migrated.collaboration.db_path)) as connection:
                version = connection.execute(
                    "SELECT value FROM collaboration_meta WHERE key='schema_version'"
                ).fetchone()[0]
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            self.assertEqual("6", version)
            self.assertIn("conflict_resolution_drafts", tables)
            self.assertIn("conflict_resolution_events", tables)


class ConflictResolutionApiTest(ConflictResolutionFixture):
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
    def request_json(server, method: str, path: str, payload: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), json.loads(raw) if raw else {}
        connection.close()
        return result

    def test_owner_api_flow_and_gateway_denial(self) -> None:
        submitted = self.submitted_pr()
        self.repository.write_file("src/app.txt", b"owner api change\n", "Owner", "conflict")
        base = f"/api/v1/repositories/{self.repository_id}/pull-requests/{submitted['id']}/conflict-resolutions"
        status, headers, model = self.request_json(
            self.owner_server,
            "POST",
            base,
            {"actorName": "Owner", "expectedPullRequestRevision": submitted["revision"]},
        )
        self.assertEqual(201, status)
        self.assertTrue(headers.get("X-ForgeTrace-Request-Id"))
        draft = model["conflicts"][0]["draft"]
        status, _headers, saved = self.request_json(
            self.owner_server,
            "POST",
            f"{base}/{draft['id']}/decision",
            {
                "actorName": "Owner",
                "decision": "manual",
                "manualText": "api resolved\n",
                "expectedVersion": draft["version"],
            },
        )
        self.assertEqual(200, status)
        status, _headers, confirmed = self.request_json(
            self.owner_server,
            "POST",
            f"{base}/{draft['id']}/confirm",
            {"actorName": "Owner", "expectedVersion": saved["version"]},
        )
        self.assertEqual(200, status)
        self.assertEqual("confirmed", confirmed["status"])
        status, _headers, listed = self.request_json(self.owner_server, "GET", base)
        self.assertEqual(200, status)
        self.assertTrue(listed["complete"])

        status, _headers, denied = self.request_json(self.remote_server, "GET", base)
        self.assertEqual(403, status)
        self.assertEqual("remote_owner_api_blocked", denied["code"])
        status, _headers, denied = self.request_json(
            self.remote_server,
            "POST",
            f"{base}/{draft['id']}/decision",
            {"actorName": "Contributor", "decision": "incoming", "expectedVersion": confirmed["version"]},
        )
        self.assertEqual(403, status)
        self.assertEqual("remote_owner_api_blocked", denied["code"])


if __name__ == "__main__":
    unittest.main()
