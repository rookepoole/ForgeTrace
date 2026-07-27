from __future__ import annotations

import hashlib
import http.client
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from forgetrace.app import build_application
from forgetrace.constants import REPOSITORY_ACCESS_READ_ONLY
from forgetrace.errors import ForgeTraceError
from forgetrace.project_coordination import render_inert_markdown
from forgetrace.web import create_server

ROOT = Path(__file__).resolve().parents[1]


def directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*"), key=lambda candidate: candidate.as_posix()):
        relative = item.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        if item.is_symlink():
            digest.update(b"SYMLINK")
            digest.update(item.readlink().as_posix().encode("utf-8"))
        elif item.is_file():
            digest.update(b"FILE")
            digest.update(item.read_bytes())
        elif item.is_dir():
            digest.update(b"DIR")
    return digest.hexdigest()


class ProjectFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="forgetrace-v049-project-"))
        self.app = build_application(ROOT, self.root / "data")
        record = self.app.registry.register_repository(
            path=str(self.root / "repository"), name="Project Coordination", description="v0.4.9 fixture",
            author="Rooke Poole", initialize=True, create_directory=True,
        )
        self.repository_id = record["id"]
        self.repository_path = Path(record["path"])

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class ProjectCoordinationServiceTest(ProjectFixture):
    def test_owner_issue_label_milestone_comment_and_concurrency(self) -> None:
        label = self.app.project.create_label(self.repository_id, name="bug", color="#ff3344")
        milestone = self.app.project.create_milestone(
            self.repository_id, title="v1.0", due_at="2026-08-30T00:00:00Z"
        )
        issue = self.app.project.create_topic(
            self.repository_id, kind="issue", title="Fix import race", body="## Evidence\nReproduce twice.",
            actor_name="Rooke Poole",
        )
        updated = self.app.project.update_topic(
            self.repository_id, issue["id"], expected_version=issue["version"],
            label_ids=[label["id"]], milestone_id=milestone["id"], assignee="Rooke Poole",
            due_at="2026-08-15T00:00:00Z", actor="Rooke Poole",
        )
        self.assertEqual("bug", updated["labels"][0]["name"])
        self.assertEqual("v1.0", updated["milestone"]["title"])
        self.assertEqual("Rooke Poole", updated["assignee"])
        commented = self.app.project.add_comment(
            self.repository_id, issue["id"], body="Working on a transactional fix.",
            expected_version=updated["version"], actor_name="Rooke Poole",
        )
        self.assertEqual(1, commented["commentCount"])
        with self.assertRaises(ForgeTraceError) as stale:
            self.app.project.update_topic(
                self.repository_id, issue["id"], expected_version=updated["version"], title="Stale update"
            )
        self.assertEqual("project_topic_version_changed", stale.exception.code)

    def test_label_milestone_crud_filters_and_detachment(self) -> None:
        label = self.app.project.create_label(
            self.repository_id, name="triage", color="#445566", description="Initial"
        )
        label = self.app.project.update_label(
            self.repository_id, label["id"], expected_version=label["version"],
            name="priority-high", color="#aa2233", description="Escalated", actor="Owner",
        )
        milestone = self.app.project.create_milestone(
            self.repository_id, title="v0.5", description="Project layer"
        )
        milestone = self.app.project.update_milestone(
            self.repository_id, milestone["id"], expected_version=milestone["version"],
            description="Project layer complete", due_at="2026-09-01T00:00:00Z", actor="Owner",
        )
        issue = self.app.project.create_topic(
            self.repository_id, kind="issue", title="Filtered issue", body="Searchable coordination"
        )
        issue = self.app.project.update_topic(
            self.repository_id, issue["id"], expected_version=issue["version"],
            label_ids=[label["id"]], milestone_id=milestone["id"], actor="Owner",
        )
        by_label = self.app.project.list_topics(
            self.repository_id, kind="issue", label_id=label["id"], query="coordination"
        )
        by_milestone = self.app.project.list_topics(
            self.repository_id, kind="issue", milestone_id=milestone["id"]
        )
        self.assertEqual([issue["id"]], [item["id"] for item in by_label["items"]])
        self.assertEqual([issue["id"]], [item["id"] for item in by_milestone["items"]])
        self.app.project.delete_label(self.repository_id, label["id"], actor="Owner")
        detached = self.app.project.get_topic(self.repository_id, issue["id"])
        self.assertEqual([], detached["labels"])
        self.app.project.delete_milestone(self.repository_id, milestone["id"], actor="Owner")
        detached = self.app.project.get_topic(self.repository_id, issue["id"])
        self.assertIsNone(detached["milestone"])
        closed = self.app.project.update_topic(
            self.repository_id, issue["id"], expected_version=detached["version"],
            state="closed", actor="Owner",
        )
        self.assertEqual("closed", closed["state"])
        self.assertEqual(1, self.app.project.list_topics(
            self.repository_id, kind="issue", state="closed"
        )["total"])

    def test_inert_markdown_and_safe_informational_references(self) -> None:
        issue = self.app.project.create_topic(
            self.repository_id, kind="issue", title="Rendering",
            body="# Heading\n<script>window.pwned=true</script>\n```html\n<img src=x onerror=alert(1)>\n```",
            references=[
                {"kind": "commit", "value": "a" * 40},
                {"kind": "path", "value": "src/main.py"},
                {"kind": "pull_request", "value": "pr_unknown"},
            ],
        )
        self.assertNotIn("<script>", issue["bodyHtml"])
        self.assertIn("&lt;script&gt;", issue["bodyHtml"])
        self.assertFalse(issue["rendering"]["activeContentRendered"])
        self.assertTrue(all(not item["verified"] for item in issue["references"]))
        with self.assertRaises(ForgeTraceError) as protected:
            self.app.project.create_topic(
                self.repository_id, kind="issue", title="Bad ref",
                references=[{"kind": "path", "value": ".git/config"}],
            )
        self.assertEqual("protected_reference_path", protected.exception.code)
        self.assertNotIn("javascript:", render_inert_markdown("[x](javascript:alert(1))"))

    def test_explicit_invite_permission_contributor_participation_and_locking(self) -> None:
        denied = self.app.collaboration.create_invite(self.repository_id, allow_project_participation=False)
        with self.assertRaises(ForgeTraceError) as blocked:
            self.app.project.overview_for_token(denied["token"])
        self.assertEqual("project_participation_not_allowed", blocked.exception.code)

        allowed = self.app.collaboration.create_invite(self.repository_id, allow_project_participation=True)
        discussion = self.app.project.create_topic_for_token(
            allowed["token"], kind="discussion", title="API shape", body="Should this return a cursor?",
            actor_name="Alex",
        )
        self.assertEqual("contributor", discussion["authorRole"])
        discussion = self.app.project.add_comment_for_token(
            allowed["token"], discussion["id"], body="Pagination should stay bounded.",
            expected_version=discussion["version"], actor_name="Alex",
        )
        locked = self.app.project.update_topic(
            self.repository_id, discussion["id"], expected_version=discussion["version"],
            locked=True, actor="Rooke Poole",
        )
        with self.assertRaises(ForgeTraceError) as denied_reply:
            self.app.project.add_comment_for_token(
                allowed["token"], discussion["id"], body="Another reply",
                expected_version=locked["version"], actor_name="Alex",
            )
        self.assertEqual("project_topic_locked", denied_reply.exception.code)

    def test_discussion_accepted_answer_pin_and_moderation(self) -> None:
        discussion = self.app.project.create_topic(
            self.repository_id, kind="discussion", title="Best storage layout", actor_name="Owner"
        )
        discussion = self.app.project.add_comment(
            self.repository_id, discussion["id"], body="Use isolated application data.",
            expected_version=discussion["version"], actor_name="Contributor"
        )
        answer = discussion["comments"][0]
        accepted = self.app.project.update_topic(
            self.repository_id, discussion["id"], expected_version=discussion["version"],
            accepted_comment_id=answer["id"], pinned=True, actor="Owner",
        )
        self.assertEqual(answer["id"], accepted["acceptedCommentId"])
        moderated = self.app.project.moderate_comment(
            self.repository_id, answer["id"], expected_version=answer["version"],
            actor="Owner", reason="Contains private data",
        )
        self.assertTrue(moderated["hidden"])
        self.assertEqual("[removed by repository owner]", moderated["body"])

    def test_tampered_security_ledger_blocks_moderation_before_change(self) -> None:
        issue = self.app.project.create_topic(self.repository_id, kind="issue", title="Audit gate")
        issue = self.app.project.add_comment(
            self.repository_id, issue["id"], body="Comment", expected_version=issue["version"]
        )
        comment = issue["comments"][0]
        connection = sqlite3.connect(self.app.security_events.db_path)
        try:
            connection.execute("DROP TRIGGER security_events_no_update")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ForgeTraceError) as blocked:
            self.app.project.moderate_comment(
                self.repository_id, comment["id"], expected_version=comment["version"],
                actor="Owner", reason="Moderate",
            )
        self.assertEqual("security_event_ledger_unavailable", blocked.exception.code)
        fresh = self.app.project.get_topic(self.repository_id, issue["id"])
        self.assertFalse(fresh["comments"][0]["hidden"])

    def test_restart_persistence_repository_isolation_and_no_workspace_mutation(self) -> None:
        before = hashlib.sha256((self.repository_path / "README.md").read_bytes()).hexdigest()
        issue = self.app.project.create_topic(self.repository_id, kind="issue", title="Persistent issue")
        second = self.app.registry.register_repository(
            path=str(self.root / "second"), name="Second", author="Owner", initialize=True, create_directory=True
        )
        with self.assertRaises(ForgeTraceError):
            self.app.project.get_topic(second["id"], issue["id"])
        restarted = build_application(ROOT, self.root / "data")
        loaded = restarted.project.get_topic(self.repository_id, issue["id"])
        self.assertEqual("Persistent issue", loaded["title"])
        after = hashlib.sha256((self.repository_path / "README.md").read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertFalse((self.repository_path / ".git").exists())

    def test_registry_recovery_does_not_replace_project_coordination_data(self) -> None:
        issue = self.app.project.create_topic(
            self.repository_id, kind="issue", title="Survives registry recovery", actor_name="Owner"
        )
        backup = self.app.registry.create_backup("before-registry-change")
        second = self.app.registry.register_repository(
            path=str(self.root / "restore-only-second"), name="Second", author="Owner",
            initialize=True, create_directory=True,
        )
        self.app.project.create_topic(
            second["id"], kind="discussion", title="Second repository topic", actor_name="Owner"
        )
        project_files = [
            candidate for candidate in (
                self.app.project.db_path,
                Path(str(self.app.project.db_path) + "-wal"),
                Path(str(self.app.project.db_path) + "-shm"),
            ) if candidate.exists()
        ]
        before = {candidate.name: hashlib.sha256(candidate.read_bytes()).hexdigest() for candidate in project_files}

        preview = self.app.registry.preview_registry_restore(backup["name"], "replace")
        restored = self.app.registry.restore_registry_backup(
            backup["name"], "replace", preview["previewId"]
        )
        self.assertEqual("completed", restored["state"])
        after = {candidate.name: hashlib.sha256(candidate.read_bytes()).hexdigest() for candidate in project_files}
        self.assertEqual(before, after)
        loaded = self.app.project.get_topic(self.repository_id, issue["id"])
        self.assertEqual("Survives registry recovery", loaded["title"])
        with self.app.project.connect() as connection:
            orphaned_count = connection.execute(
                "SELECT COUNT(*) FROM project_topics WHERE repository_id=? AND kind='discussion'",
                (second["id"],),
            ).fetchone()[0]
        self.assertEqual(1, orphaned_count)
        rolled_back = self.app.registry.rollback_registry_restore(restored["restoreId"])
        self.assertEqual("rolled_back", rolled_back["state"])
        second_topics = self.app.project.list_topics(second["id"], kind="discussion")
        self.assertEqual(1, second_topics["total"])

    def test_read_only_repository_allows_coordination_without_repository_mutation(self) -> None:
        self.app.registry.set_access_mode(self.repository_id, REPOSITORY_ACCESS_READ_ONLY)
        before = directory_digest(self.repository_path)
        issue = self.app.project.create_topic(
            self.repository_id, kind="issue", title="Coordinate while locked", actor_name="Owner"
        )
        issue = self.app.project.add_comment(
            self.repository_id, issue["id"], body="Repository bytes remain frozen.",
            expected_version=issue["version"], actor_name="Owner",
        )
        invitation = self.app.collaboration.create_invite(
            self.repository_id, allow_project_participation=True
        )
        discussion = self.app.project.create_topic_for_token(
            invitation["token"], kind="discussion", title="Read-only discussion", actor_name="Contributor"
        )
        self.app.project.add_comment_for_token(
            invitation["token"], discussion["id"], body="Coordination only.",
            expected_version=discussion["version"], actor_name="Contributor",
        )
        self.assertEqual(before, directory_digest(self.repository_path))
        policy = self.app.registry.repository_service(self.repository_id).access_policy()
        self.assertEqual(REPOSITORY_ACCESS_READ_ONLY, policy["effectiveMode"])
        self.assertFalse(policy["writable"])
        self.assertEqual(1, self.app.project.list_topics(self.repository_id, kind="issue")["total"])
        self.assertEqual(1, self.app.project.list_topics(self.repository_id, kind="discussion")["total"])

    def test_quotas_and_soft_delete_retention(self) -> None:
        issue = self.app.project.create_topic(self.repository_id, kind="issue", title="Limited")
        with patch("forgetrace.project_coordination.MAX_COMMENTS_PER_TOPIC", 1):
            issue = self.app.project.add_comment(
                self.repository_id, issue["id"], body="One", expected_version=issue["version"]
            )
            with self.assertRaises(ForgeTraceError) as limited:
                self.app.project.add_comment(
                    self.repository_id, issue["id"], body="Two", expected_version=issue["version"]
                )
            self.assertEqual("project_comment_limit", limited.exception.code)
        self.app.project.delete_topic(self.repository_id, issue["id"], actor="Owner", reason="obsolete")
        with self.app.project.lock, self.app.project.connect() as connection:
            connection.execute(
                "UPDATE project_topics SET deleted_at='2020-01-01T00:00:00Z' WHERE id=?", (issue["id"],)
            )
        cleanup = self.app.project.cleanup_retention()
        self.assertEqual(1, cleanup["topics"])

    def test_health_dashboard_reports_project_coordination_integrity(self) -> None:
        self.app.project.create_topic(
            self.repository_id, kind="issue", title="Health-visible issue", actor_name="Owner"
        )
        report = self.app.health.generate(
            request_id="project-health-test", repository_id=self.repository_id, scope="standard"
        )
        section = report["sections"]["project"]
        self.assertEqual("ok", section["data"]["integrity"])
        self.assertTrue(section["complete"])
        self.assertEqual(self.repository_id, section["data"]["repositories"][0]["repositoryId"])
        self.assertEqual(1, section["data"]["repositories"][0]["topicCount"])

    def test_cross_process_project_lock_serializes_writers(self) -> None:
        script = self.root / "writer.py"
        script.write_text(
            "from pathlib import Path\nfrom forgetrace.app import build_application\n"
            f"app=build_application(Path({str(ROOT)!r}),Path({str(self.root/'data')!r}))\n"
            f"app.project.create_topic({self.repository_id!r},kind='issue',title=__import__('sys').argv[1])\n",
            encoding="utf-8",
        )
        processes = [
            subprocess.Popen(["python3", str(script), f"Issue {index}"], cwd=ROOT, env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
            for index in range(4)
        ]
        self.assertTrue(all(process.wait(timeout=30) == 0 for process in processes))
        listed = self.app.project.list_topics(self.repository_id, kind="issue", limit=100)
        self.assertEqual(4, listed["total"])
        self.assertEqual([1, 2, 3, 4], sorted(item["number"] for item in listed["items"]))


class ProjectCoordinationMigrationTest(unittest.TestCase):
    def test_collaboration_schema_six_adds_explicit_project_permission(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="forgetrace-v049-migration-"))
        try:
            app = build_application(ROOT, root / "data")
            connection = sqlite3.connect(app.collaboration.db_path)
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(collaboration_invites)")}
                version = connection.execute(
                    "SELECT value FROM collaboration_meta WHERE key='schema_version'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertIn("allow_project_participation", columns)
            self.assertEqual("6", version)
            self.assertEqual(1, app.project.overview(app.registry.list_repositories()["repositories"][0]["id"])["schemaVersion"] if app.registry.list_repositories()["repositories"] else 1)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class ProjectCoordinationApiTest(ProjectFixture):
    def setUp(self) -> None:
        super().setUp()
        self.owner = create_server(self.app, "127.0.0.1", 0, surface="owner")
        self.gateway = create_server(self.app, "127.0.0.1", 0, surface="gateway")
        self.owner_thread = threading.Thread(target=self.owner.serve_forever, daemon=True); self.owner_thread.start()
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True); self.gateway_thread.start()

    def tearDown(self) -> None:
        for server, thread in ((self.owner, self.owner_thread), (self.gateway, self.gateway_thread)):
            server.shutdown(); server.server_close(); thread.join(timeout=3)
        super().tearDown()

    @staticmethod
    def request(server, method: str, path: str, body=None, token: str = ""):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
        headers = {}
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8"); headers["Content-Type"] = "application/json"
        if token:
            headers["X-ForgeTrace-Invite"] = token
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse(); payload_raw = response.read(); connection.close()
        payload = json.loads(payload_raw) if payload_raw else {}
        return response.status, payload

    def test_owner_and_contributor_api_flow_and_gateway_isolation(self) -> None:
        rid = self.repository_id
        status, label = self.request(self.owner, "POST", f"/api/v1/repositories/{rid}/project/labels", {
            "name": "feature", "color": "#22aa88", "actor": "Owner"
        })
        self.assertEqual(201, status)
        status, issue = self.request(self.owner, "POST", f"/api/v1/repositories/{rid}/project/issues", {
            "title": "Owner issue", "body": "Track this", "authorName": "Owner"
        })
        self.assertEqual(201, status)
        status, overview = self.request(self.owner, "GET", f"/api/v1/repositories/{rid}/project")
        self.assertEqual(200, status); self.assertEqual(1, overview["counts"]["issueOpen"])
        status, issue = self.request(self.owner, "PUT", f"/api/v1/repositories/{rid}/project/issues/{issue['id']}", {
            "expectedVersion": issue["version"], "labelIds": [label["id"]], "pinned": True,
            "assignee": "Owner", "actor": "Owner",
        })
        self.assertEqual(200, status); self.assertTrue(issue["pinned"]); self.assertEqual("Owner", issue["assignee"])
        disposable_status, disposable = self.request(self.owner, "POST", f"/api/v1/repositories/{rid}/project/issues", {
            "title": "Disposable issue", "authorName": "Owner"
        })
        self.assertEqual(201, disposable_status)
        status, deleted = self.request(
            self.owner, "DELETE",
            f"/api/v1/repositories/{rid}/project/issues/{disposable['id']}?actor=Owner&reason=browserless-api-test",
        )
        self.assertEqual(200, status); self.assertTrue(deleted["deleted"])

        invite = self.app.collaboration.create_invite(rid, allow_project_participation=True)
        status, contributor_overview = self.request(
            self.gateway, "GET", "/api/v1/collaboration/project", token=invite["token"]
        )
        self.assertEqual(200, status); self.assertTrue(contributor_overview["contributor"]["canParticipate"])
        status, discussion = self.request(self.gateway, "POST", "/api/v1/collaboration/project/discussions", {
            "title": "Contributor discussion", "body": "Question", "authorName": "Alex"
        }, token=invite["token"])
        self.assertEqual(201, status)
        status, discussion = self.request(self.gateway, "POST", f"/api/v1/collaboration/project/discussions/{discussion['id']}/comments", {
            "body": "Follow-up", "authorName": "Alex", "expectedVersion": discussion["version"]
        }, token=invite["token"])
        self.assertEqual(201, status); self.assertEqual(1, discussion["commentCount"])

        status, _ = self.request(self.gateway, "GET", f"/api/v1/repositories/{rid}/project")
        self.assertEqual(403, status)
        status, _ = self.request(self.gateway, "POST", f"/api/v1/repositories/{rid}/project/labels", {"name": "forbidden"})
        self.assertEqual(403, status)
        status, _ = self.request(self.gateway, "GET", "/api/v1/security-events")
        self.assertEqual(403, status)


if __name__ == "__main__":
    unittest.main()
