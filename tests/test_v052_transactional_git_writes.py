from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from forgetrace.app import build_application
from forgetrace.constants import REPOSITORY_ACCESS_READ_ONLY
from forgetrace.errors import ForgeTraceError
from forgetrace.git_writes import GitWriteService, GitWriteTransaction
from forgetrace.web import create_server

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")


@unittest.skipUnless(GIT, "Git executable is required")
class TransactionalGitFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="forgetrace-v052-")
        self.root = Path(self.temp.name)
        self.app = build_application(ROOT, self.root / "data")
        self.repo_path = self.root / "repo"
        record = self.app.registry.register_repository(
            path=str(self.repo_path),
            name="Transactional Git Fixture",
            description="v0.5.2",
            author="Rooke Poole",
            initialize=True,
            create_directory=True,
        )
        self.repository_id = record["id"]
        self.run_git("init")
        self.run_git("config", "user.name", "External Fixture")
        self.run_git("config", "user.email", "external@example.invalid")
        self.run_git("add", "README.md")
        self.run_git("commit", "-m", "Initial commit")

    def tearDown(self) -> None:
        if self.app.gateway:
            self.app.gateway.stop()
        self.temp.cleanup()

    def run_git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [GIT, *args], cwd=self.repo_path, text=True, capture_output=True, check=check
        )

    def preview(self, operation: str, **values):
        return self.app.git_writes.preview(
            self.repository_id, {"operation": operation, **values}
        )

    def execute(self, preview: dict, confirmation: str):
        return self.app.git_writes.execute(
            self.repository_id,
            preview_id=preview["previewId"],
            confirmation=confirmation,
            actor="Rooke Poole",
            request_id="req-v052-test",
        )


class TransactionalGitWriteServiceTest(TransactionalGitFixture):
    def test_stage_preview_execute_receipt_and_security_evidence(self) -> None:
        target = self.repo_path / "alpha.txt"
        target.write_text("alpha\n", encoding="utf-8")
        before_bytes = target.read_bytes()

        preview = self.preview("stage", paths=["alpha.txt"])
        self.assertEqual("STAGE", preview["requiredConfirmation"])
        self.assertEqual("alpha.txt", preview["state"]["selected"][0]["path"])
        self.assertFalse(preview["authority"]["network"])
        self.assertFalse(preview["authority"]["hooks"])

        result = self.execute(preview, "STAGE")
        self.assertFalse(result["rolledBack"])
        self.assertEqual(["alpha.txt"], result["result"]["paths"])
        self.assertEqual(before_bytes, target.read_bytes())
        staged = self.run_git("diff", "--cached", "--name-only").stdout.splitlines()
        self.assertEqual(["alpha.txt"], staged)
        self.assertTrue(result["receipt"]["receiptDigest"])
        self.assertEqual("committed", result["receipt"]["outcome"])
        self.assertTrue(self.app.git_writes.list_receipts(self.repository_id)[0]["verified"])

        events = self.app.security_events.query(repository_id=self.repository_id, limit=100)["events"]
        actions = {item["action"] for item in events}
        self.assertIn("git_stage_authorized", actions)
        self.assertIn("git_stage_completed", actions)

    def test_preview_is_stale_when_selected_bytes_change(self) -> None:
        target = self.repo_path / "stale.txt"
        target.write_text("one\n", encoding="utf-8")
        preview = self.preview("stage", paths=["stale.txt"])
        target.write_text("two\n", encoding="utf-8")
        with self.assertRaises(ForgeTraceError) as stale:
            self.execute(preview, "STAGE")
        self.assertEqual("git_write_preview_stale", stale.exception.code)
        self.assertEqual("?? stale.txt", self.run_git("status", "--short", "--", "stale.txt").stdout.strip())

    def test_commit_uses_plumbing_without_hooks_signing_editor_or_config_identity(self) -> None:
        target = self.repo_path / "commit.txt"
        target.write_text("committed through ForgeTrace\n", encoding="utf-8")
        self.execute(self.preview("stage", paths=["commit.txt"]), "STAGE")
        marker = self.root / "hook-ran"
        for name in ("pre-commit", "commit-msg", "post-commit", "reference-transaction"):
            hook = self.repo_path / ".git" / "hooks" / name
            hook.write_text(f"#!/bin/sh\necho hook > '{marker}'\nexit 1\n", encoding="utf-8")
            hook.chmod(0o755)
        self.run_git("config", "commit.gpgSign", "true")
        self.run_git("config", "user.name", "Wrong Config Identity")
        self.run_git("config", "user.email", "wrong@example.invalid")

        preview = self.preview(
            "commit",
            message="Transactional commit\n\nNo hook execution.",
            authorName="Rooke Poole",
            authorEmail="rooke@example.invalid",
        )
        result = self.execute(preview, "COMMIT")
        oid = result["result"]["commitOid"]
        self.assertEqual(oid, self.run_git("rev-parse", "HEAD").stdout.strip())
        self.assertFalse(marker.exists())
        self.assertEqual("", self.run_git("diff", "--cached", "--name-only").stdout)
        metadata = self.run_git("show", "-s", "--format=%an%x00%ae%x00%s", oid).stdout.strip().split("\x00")
        self.assertEqual(["Rooke Poole", "rooke@example.invalid", "Transactional commit"], metadata)
        self.assertEqual("commit", result["result"]["operation"])
        self.assertIn(oid, result["receipt"]["createdObjects"])

    def test_branch_and_lightweight_tag_are_created_without_switching_head(self) -> None:
        original_branch = self.run_git("branch", "--show-current").stdout.strip()
        original_head = self.run_git("rev-parse", "HEAD").stdout.strip()

        branch = self.execute(
            self.preview("create_branch", name="feature/transactional", targetOid=original_head),
            "CREATE BRANCH",
        )
        tag = self.execute(
            self.preview("create_tag", name="v0.5.2-test", targetOid=original_head),
            "CREATE TAG",
        )

        self.assertEqual(original_branch, self.run_git("branch", "--show-current").stdout.strip())
        self.assertEqual(original_head, self.run_git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(original_head, self.run_git("rev-parse", "refs/heads/feature/transactional").stdout.strip())
        self.assertEqual(original_head, self.run_git("rev-parse", "refs/tags/v0.5.2-test").stdout.strip())
        self.assertEqual("create_branch", branch["result"]["operation"])
        self.assertEqual("create_tag", tag["result"]["operation"])
        self.assertTrue((self.repo_path / ".git" / "logs" / "refs" / "heads" / "feature" / "transactional").is_file())
        self.assertTrue((self.repo_path / ".git" / "logs" / "refs" / "tags" / "v0.5.2-test").is_file())

    def test_read_only_and_deletion_intent_fail_closed(self) -> None:
        (self.repo_path / "blocked.txt").write_text("blocked\n", encoding="utf-8")
        self.app.registry.set_access_mode(self.repository_id, "read_only")
        with self.assertRaises(ForgeTraceError) as read_only:
            self.preview("stage", paths=["blocked.txt"])
        self.assertEqual("repository_read_only", read_only.exception.code)

        self.app.registry.set_access_mode(self.repository_id, "read_write")
        record = self.app.registry.get_repository(self.repository_id)
        self.app.registry._write_deletion_intent(
            self.repository_id,
            deletion_id="delete-" + "a" * 32,
            name=record["name"],
            original_path=record["path"],
        )
        try:
            with self.assertRaises(ForgeTraceError) as deleting:
                self.preview("stage", paths=["blocked.txt"])
            self.assertEqual("repository_delete_in_progress", deleting.exception.code)
        finally:
            self.app.registry._clear_deletion_intent(self.repository_id)

    def test_protected_paths_external_filters_and_unsafe_refs_are_blocked(self) -> None:
        with self.assertRaises(ForgeTraceError) as protected:
            self.preview("stage", paths=[".forgetrace/state.json"])
        self.assertEqual("git_stage_protected_path", protected.exception.code)

        marker = self.root / "filter-ran"
        helper = self.root / "filter-helper"
        helper.write_text(f"#!/bin/sh\necho filter > '{marker}'\ncat\n", encoding="utf-8")
        helper.chmod(0o755)
        self.run_git("config", "filter.evil.clean", str(helper))
        self.run_git("config", "filter.evil.required", "true")
        (self.repo_path / ".gitattributes").write_text("*.bin filter=evil\n", encoding="utf-8")
        (self.repo_path / "payload.bin").write_bytes(b"payload")
        with self.assertRaises(ForgeTraceError) as filtered:
            self.preview("stage", paths=["payload.bin"])
        self.assertEqual("git_stage_external_filter_blocked", filtered.exception.code)
        self.assertFalse(marker.exists())

        with self.assertRaises(ForgeTraceError):
            self.preview("create_branch", name="bad;touch-owned")
        self.assertFalse((self.root / "owned").exists())

    def test_security_ledger_failure_blocks_before_git_mutation(self) -> None:
        target = self.repo_path / "ledger.txt"
        target.write_text("ledger\n", encoding="utf-8")
        preview = self.preview("stage", paths=["ledger.txt"])
        before_index = (self.repo_path / ".git" / "index").read_bytes()
        with mock.patch.object(self.app.security_events, "assert_writable", side_effect=RuntimeError("ledger unavailable")):
            with self.assertRaises(RuntimeError):
                self.execute(preview, "STAGE")
        self.assertEqual(before_index, (self.repo_path / ".git" / "index").read_bytes())
        self.assertEqual("?? ledger.txt", self.run_git("status", "--short", "--", "ledger.txt").stdout.strip())

    def test_failed_write_restores_exact_index_and_records_rollback(self) -> None:
        target = self.repo_path / "rollback.txt"
        target.write_text("rollback\n", encoding="utf-8")
        preview = self.preview("stage", paths=["rollback.txt"])
        index = self.repo_path / ".git" / "index"
        before_index = index.read_bytes()
        real_stage = self.app.git_writes._execute_stage

        def fail_after_stage(root: Path, input_data: dict):
            real_stage(root, input_data)
            raise ForgeTraceError("forced post-stage failure", code="forced_failure")

        with mock.patch.object(self.app.git_writes, "_execute_stage", side_effect=fail_after_stage):
            with self.assertRaises(ForgeTraceError) as failed:
                self.execute(preview, "STAGE")
        self.assertEqual("forced_failure", failed.exception.code)
        self.assertEqual(before_index, index.read_bytes())
        self.assertEqual("?? rollback.txt", self.run_git("status", "--short", "--", "rollback.txt").stdout.strip())
        receipts = self.app.git_writes.list_receipts(self.repository_id)
        self.assertEqual("rolled_back", receipts[0]["outcome"])
        self.assertTrue(receipts[0]["verified"])

    def test_incomplete_journal_is_rolled_back_on_service_startup(self) -> None:
        target = self.repo_path / "recovery.txt"
        target.write_text("recover\n", encoding="utf-8")
        repository, root, git_dir = self.app.git_writes._context(self.repository_id)
        index = git_dir / "index"
        before_index = index.read_bytes()
        transaction = GitWriteTransaction(
            self.app.git_writes,
            repository_id=self.repository_id,
            repository_path=root,
            git_dir=git_dir,
            operation="stage",
            preview_id="git_preview_" + "b" * 32,
            preview_digest="c" * 64,
        )
        transaction.capture("index")
        transaction.applying()
        self.run_git("add", "--", "recovery.txt")
        self.assertNotEqual(before_index, index.read_bytes())

        recovered = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, recovered.startup_recovery_report["rolledBack"])
        self.assertEqual(before_index, index.read_bytes())
        self.assertEqual("?? recovery.txt", self.run_git("status", "--short", "--", "recovery.txt").stdout.strip())
        self.assertEqual([], list(recovered.transactions_dir.glob("git_txn_*")))

    def test_startup_recovery_restores_incomplete_write_after_read_only_change(self) -> None:
        target = self.repo_path / "read-only-recovery.txt"
        target.write_text("recover while read-only\n", encoding="utf-8")
        repository, root, git_dir = self.app.git_writes._context(self.repository_id)
        index = git_dir / "index"
        before_index = index.read_bytes()
        transaction = GitWriteTransaction(
            self.app.git_writes,
            repository_id=self.repository_id,
            repository_path=root,
            git_dir=git_dir,
            operation="stage",
            preview_id="git_preview_" + "d" * 32,
            preview_digest="e" * 64,
        )
        transaction.capture("index")
        transaction.applying()
        self.run_git("add", "--", "read-only-recovery.txt")
        self.assertNotEqual(before_index, index.read_bytes())
        self.app.registry.set_access_mode(self.repository_id, REPOSITORY_ACCESS_READ_ONLY)

        recovered = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, recovered.startup_recovery_report["rolledBack"])
        self.assertEqual(before_index, index.read_bytes())
        self.assertEqual(
            "?? read-only-recovery.txt",
            self.run_git("status", "--short", "--", "read-only-recovery.txt").stdout.strip(),
        )
        self.assertFalse(recovered.status(self.repository_id)["writable"])

    def test_failed_ref_write_restores_ref_and_reflog_without_touching_index(self) -> None:
        preview = self.preview("create_branch", name="feature/rollback-cleanup")
        index = self.repo_path / ".git" / "index"
        before_index = index.read_bytes()
        real_create = self.app.git_writes._execute_ref_create

        def create_then_fail(*args, **kwargs):
            real_create(*args, **kwargs)
            raise RuntimeError("forced failure after ref creation")

        with mock.patch.object(self.app.git_writes, "_execute_ref_create", side_effect=create_then_fail):
            with self.assertRaises(RuntimeError):
                self.execute(preview, "CREATE BRANCH")

        self.assertEqual(before_index, index.read_bytes())
        self.assertNotEqual(0, self.run_git("show-ref", "--verify", "--quiet", "refs/heads/feature/rollback-cleanup", check=False).returncode)
        self.assertFalse((self.repo_path / ".git" / "refs" / "heads" / "feature").exists())
        self.assertFalse((self.repo_path / ".git" / "logs" / "refs" / "heads" / "feature").exists())
        receipt = self.app.git_writes.list_receipts(self.repository_id)[0]
        self.assertEqual("rolled_back", receipt["outcome"])
        self.assertNotIn("index", {item["path"] for item in receipt["captureManifest"]})

    def test_terminal_journal_reconstructs_missing_receipt_before_cleanup(self) -> None:
        repository, root, git_dir = self.app.git_writes._context(self.repository_id)
        transaction = GitWriteTransaction(
            self.app.git_writes,
            repository_id=self.repository_id,
            repository_path=root,
            git_dir=git_dir,
            operation="create_tag",
            preview_id="git_preview_" + "f" * 32,
            preview_digest="1" * 64,
        )
        transaction.capture("refs/tags/recovered-receipt")
        transaction._write(
            "committed",
            beforeStateDigest="2" * 64,
            afterStateDigest="3" * 64,
            result={
                "operation": "create_tag",
                "ref": "refs/tags/recovered-receipt",
                "targetOid": self.run_git("rev-parse", "HEAD").stdout.strip(),
            },
        )
        self.assertFalse(self.app.git_writes._receipt_path(transaction.id).exists())

        recovered = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, recovered.startup_recovery_report["cleanedCommitted"])
        self.assertEqual(1, recovered.startup_recovery_report["recoveredReceipts"])
        receipt = recovered.list_receipts(self.repository_id)[0]
        self.assertTrue(receipt["verified"])
        self.assertTrue(receipt["details"]["receiptRecoveredAtStartup"])
        self.assertFalse(transaction.root.exists())

    def test_tampered_transaction_journal_is_retained_without_restoring(self) -> None:
        target = self.repo_path / "tampered-journal.txt"
        target.write_text("tampered journal\n", encoding="utf-8")
        repository, root, git_dir = self.app.git_writes._context(self.repository_id)
        index = git_dir / "index"
        before_index = index.read_bytes()
        transaction = GitWriteTransaction(
            self.app.git_writes,
            repository_id=self.repository_id,
            repository_path=root,
            git_dir=git_dir,
            operation="stage",
            preview_id="git_preview_" + "4" * 32,
            preview_digest="5" * 64,
        )
        transaction.capture("index")
        transaction.applying()
        self.run_git("add", "--", "tampered-journal.txt")
        mutated_index = index.read_bytes()
        self.assertNotEqual(before_index, mutated_index)
        journal = json.loads(transaction.journal_path.read_text(encoding="utf-8"))
        journal["operation"] = "create_branch"
        transaction.journal_path.write_text(json.dumps(journal), encoding="utf-8")

        recovered = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, recovered.startup_recovery_report["retained"])
        self.assertEqual(mutated_index, index.read_bytes())
        self.assertTrue(transaction.root.exists())
        self.assertIn("integrity", recovered.startup_recovery_report["actions"][0]["message"].lower())

    def test_native_git_lock_defers_startup_rollback_until_later_start(self) -> None:
        target = self.repo_path / "deferred-recovery.txt"
        target.write_text("deferred recovery\n", encoding="utf-8")
        repository, root, git_dir = self.app.git_writes._context(self.repository_id)
        index = git_dir / "index"
        before_index = index.read_bytes()
        transaction = GitWriteTransaction(
            self.app.git_writes,
            repository_id=self.repository_id,
            repository_path=root,
            git_dir=git_dir,
            operation="stage",
            preview_id="git_preview_" + "6" * 32,
            preview_digest="7" * 64,
        )
        transaction.capture("index")
        transaction.applying()
        self.run_git("add", "--", "deferred-recovery.txt")
        mutated_index = index.read_bytes()
        native_lock = git_dir / "index.lock"
        native_lock.write_text("external operation", encoding="utf-8")
        deferred = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, deferred.startup_recovery_report["retained"])
        self.assertEqual(mutated_index, index.read_bytes())
        self.assertTrue(transaction.root.exists())

        native_lock.unlink()
        recovered = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, recovered.startup_recovery_report["rolledBack"])
        self.assertEqual(before_index, index.read_bytes())
        self.assertFalse(transaction.root.exists())

    def test_native_git_lock_and_active_merge_state_are_not_bypassed(self) -> None:
        target = self.repo_path / "locked.txt"
        target.write_text("locked\n", encoding="utf-8")
        native_lock = self.repo_path / ".git" / "index.lock"
        native_lock.write_text("external", encoding="utf-8")
        try:
            with self.assertRaises(ForgeTraceError) as locked:
                self.preview("stage", paths=["locked.txt"])
            self.assertEqual("git_native_lock_present", locked.exception.code)
        finally:
            native_lock.unlink()
        (self.repo_path / ".git" / "MERGE_HEAD").write_text(self.run_git("rev-parse", "HEAD").stdout, encoding="utf-8")
        try:
            with self.assertRaises(ForgeTraceError) as active:
                self.preview("stage", paths=["locked.txt"])
            self.assertEqual("git_operation_in_progress", active.exception.code)
        finally:
            (self.repo_path / ".git" / "MERGE_HEAD").unlink()


class TransactionalGitWriteApiTest(TransactionalGitFixture):
    def setUp(self) -> None:
        super().setUp()
        self.owner = create_server(self.app, "127.0.0.1", 0, surface="owner")
        self.gateway = create_server(self.app, "127.0.0.1", 0, surface="gateway")
        self.owner_thread = threading.Thread(target=self.owner.serve_forever, daemon=True)
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.owner_thread.start()
        self.gateway_thread.start()

    def tearDown(self) -> None:
        for server, thread in ((self.owner, self.owner_thread), (self.gateway, self.gateway_thread)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        super().tearDown()

    @staticmethod
    def request(server, method: str, path: str, body: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if raw is not None else {}
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data) if data else {}

    def test_owner_preview_execute_status_and_gateway_denial(self) -> None:
        (self.repo_path / "api.txt").write_text("api\n", encoding="utf-8")
        base = f"/api/v1/repositories/{self.repository_id}/git/writes"
        status, payload = self.request(self.owner, "GET", base)
        self.assertEqual(200, status)
        self.assertTrue(payload["supported"])
        self.assertFalse(payload["restrictions"]["network"])

        status, preview = self.request(
            self.owner,
            "POST",
            base + "/preview",
            {"operation": "stage", "paths": ["api.txt"]},
        )
        self.assertEqual(200, status)
        status, result = self.request(
            self.owner,
            "POST",
            base + "/execute",
            {
                "previewId": preview["previewId"],
                "confirmation": "STAGE",
                "actor": "Rooke Poole",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("stage", result["result"]["operation"])

        denied, body = self.request(self.gateway, "GET", base)
        self.assertEqual(403, denied)
        self.assertEqual("remote_owner_api_blocked", body["code"])
        denied, body = self.request(
            self.gateway,
            "POST",
            base + "/preview",
            {"operation": "create_tag", "name": "gateway-denied"},
        )
        self.assertEqual(403, denied)
        self.assertEqual("remote_owner_api_blocked", body["code"])


if __name__ == "__main__":
    unittest.main()
