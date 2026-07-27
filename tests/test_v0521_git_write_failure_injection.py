from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from forgetrace.git_writes import (
    GitWriteInjectedCrash,
    GitWriteService,
)
from tests.test_v052_transactional_git_writes import GIT, TransactionalGitFixture


class TransactionalGitWriteFailureInjectionTest(TransactionalGitFixture):
    def crash_writer(self, checkpoint: str) -> GitWriteService:
        def injector(current: str, _context: dict) -> None:
            if current == checkpoint:
                raise GitWriteInjectedCrash(current)

        return GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
            failure_injector=injector,
        )

    def execute_with(self, writer: GitWriteService, preview: dict, confirmation: str):
        return writer.execute(
            self.repository_id,
            preview_id=preview["previewId"],
            confirmation=confirmation,
            actor="Rooke Poole",
            request_id="req-v0521-failure-injection",
        )

    @staticmethod
    def optional_bytes(path: Path) -> bytes | None:
        return path.read_bytes() if path.is_file() else None

    def test_crash_after_stage_index_install_is_exactly_recovered(self) -> None:
        target = self.repo_path / "crash-stage.txt"
        target.write_text("stage crash recovery\n", encoding="utf-8")
        index = self.repo_path / ".git" / "index"
        before_index = index.read_bytes()
        writer = self.crash_writer("stage_index_installed")
        preview = writer.preview(self.repository_id, {"operation": "stage", "paths": [target.name]})

        with self.assertRaises(GitWriteInjectedCrash):
            self.execute_with(writer, preview, "STAGE")

        self.assertNotEqual(before_index, index.read_bytes())
        pending = writer.status(self.repository_id)["pendingTransactions"]
        self.assertEqual(1, len(pending))
        self.assertEqual("stage_index_installed", pending[0]["lastCheckpoint"])
        self.assertEqual("rollback_on_restart", pending[0]["recoveryDisposition"])
        self.assertTrue(pending[0]["recoverable"])

        recovered = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, recovered.startup_recovery_report["rolledBack"])
        self.assertEqual(before_index, index.read_bytes())
        receipt = recovered.list_receipts(self.repository_id)[0]
        self.assertEqual("recovered_rollback", receipt["outcome"])
        self.assertEqual("stage_index_installed", receipt["details"]["recoveryOriginCheckpoint"])
        self.assertTrue(receipt["verified"])

    def test_native_git_lock_defers_injected_stage_recovery_with_actionable_diagnostics(self) -> None:
        target = self.repo_path / "deferred-injected-stage.txt"
        target.write_text("deferred injected stage\n", encoding="utf-8")
        index = self.repo_path / ".git" / "index"
        before_index = index.read_bytes()
        writer = self.crash_writer("stage_index_installed")
        preview = writer.preview(self.repository_id, {"operation": "stage", "paths": [target.name]})
        with self.assertRaises(GitWriteInjectedCrash):
            self.execute_with(writer, preview, "STAGE")
        mutated_index = index.read_bytes()
        lock_path = self.repo_path / ".git" / "index.lock"
        lock_path.write_text("external git", encoding="utf-8")

        diagnostic = writer.status(self.repository_id)["pendingTransactions"][0]
        self.assertEqual("deferred_external_git_state", diagnostic["recoveryDisposition"])
        self.assertEqual(["index.lock"], diagnostic["blockingNativeLocks"])
        self.assertIn("confirmed-stale", diagnostic["nextStep"])

        deferred = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, deferred.startup_recovery_report["retained"])
        self.assertEqual(1, deferred.startup_recovery_report["deferred"])
        self.assertEqual(mutated_index, index.read_bytes())

        lock_path.unlink()
        recovered = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, recovered.startup_recovery_report["rolledBack"])
        self.assertEqual(before_index, index.read_bytes())

    def test_crash_after_commit_ref_and_reflog_install_restores_exact_precommit_state(self) -> None:
        target = self.repo_path / "crash-commit.txt"
        target.write_text("commit crash recovery\n", encoding="utf-8")
        self.execute(self.preview("stage", paths=[target.name]), "STAGE")
        git_dir = self.repo_path / ".git"
        branch = self.run_git("symbolic-ref", "HEAD").stdout.strip()
        branch_path = git_dir / branch
        branch_log = git_dir / "logs" / branch
        before = {
            "headOid": self.run_git("rev-parse", "HEAD").stdout.strip(),
            "index": (git_dir / "index").read_bytes(),
            "head": (git_dir / "HEAD").read_bytes(),
            "headLog": self.optional_bytes(git_dir / "logs" / "HEAD"),
            "branch": self.optional_bytes(branch_path),
            "branchLog": self.optional_bytes(branch_log),
        }
        writer = self.crash_writer("commit_ref_installed")
        preview = writer.preview(
            self.repository_id,
            {
                "operation": "commit",
                "message": "Crash after reference install",
                "authorName": "Rooke Poole",
                "authorEmail": "rooke@example.invalid",
            },
        )

        with self.assertRaises(GitWriteInjectedCrash):
            self.execute_with(writer, preview, "COMMIT")

        installed_oid = self.run_git("rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(before["headOid"], installed_oid)
        diagnostic = writer.status(self.repository_id)["pendingTransactions"][0]
        self.assertEqual("commit_ref_installed", diagnostic["lastCheckpoint"])
        self.assertGreaterEqual(diagnostic["createdObjectCount"], 2)

        recovered = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, recovered.startup_recovery_report["rolledBack"])
        self.assertEqual(before["headOid"], self.run_git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(before["index"], (git_dir / "index").read_bytes())
        self.assertEqual(before["head"], (git_dir / "HEAD").read_bytes())
        self.assertEqual(before["headLog"], self.optional_bytes(git_dir / "logs" / "HEAD"))
        self.assertEqual(before["branch"], self.optional_bytes(branch_path))
        self.assertEqual(before["branchLog"], self.optional_bytes(branch_log))
        receipt = recovered.list_receipts(self.repository_id)[0]
        self.assertEqual("recovered_rollback", receipt["outcome"])
        self.assertIn(installed_oid, receipt["createdObjects"])

    def test_crash_after_branch_ref_install_removes_new_ref_and_reflog(self) -> None:
        git_dir = self.repo_path / ".git"
        index_before = (git_dir / "index").read_bytes()
        target_oid = self.run_git("rev-parse", "HEAD").stdout.strip()
        ref = git_dir / "refs" / "heads" / "crash" / "branch"
        reflog = git_dir / "logs" / "refs" / "heads" / "crash" / "branch"
        writer = self.crash_writer("branch_ref_installed")
        preview = writer.preview(
            self.repository_id,
            {"operation": "create_branch", "name": "crash/branch", "targetOid": target_oid},
        )

        with self.assertRaises(GitWriteInjectedCrash):
            self.execute_with(writer, preview, "CREATE BRANCH")

        self.assertTrue(ref.is_file())
        self.assertTrue(reflog.is_file())
        recovered = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, recovered.startup_recovery_report["rolledBack"])
        self.assertFalse(ref.exists())
        self.assertFalse(reflog.exists())
        self.assertEqual(index_before, (git_dir / "index").read_bytes())

    def test_crash_after_terminal_commit_journal_reconstructs_receipt_without_rollback(self) -> None:
        target_oid = self.run_git("rev-parse", "HEAD").stdout.strip()
        writer = self.crash_writer("terminal_journal_committed")
        preview = writer.preview(
            self.repository_id,
            {"operation": "create_tag", "name": "v0.5.2.1-terminal", "targetOid": target_oid},
        )
        with self.assertRaises(GitWriteInjectedCrash):
            self.execute_with(writer, preview, "CREATE TAG")

        self.assertEqual(target_oid, self.run_git("rev-parse", "refs/tags/v0.5.2.1-terminal").stdout.strip())
        pending = writer.status(self.repository_id)["pendingTransactions"][0]
        self.assertEqual("committed", pending["status"])
        self.assertEqual("missing", pending["receiptIntegrity"])
        self.assertEqual("reconstruct_receipt_then_cleanup", pending["recoveryDisposition"])

        recovered = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, recovered.startup_recovery_report["cleanedCommitted"])
        self.assertEqual(1, recovered.startup_recovery_report["recoveredReceipts"])
        self.assertEqual(target_oid, self.run_git("rev-parse", "refs/tags/v0.5.2.1-terminal").stdout.strip())
        receipt = recovered.list_receipts(self.repository_id)[0]
        self.assertEqual("committed", receipt["outcome"])
        self.assertTrue(receipt["details"]["receiptRecoveredAtStartup"])

    def test_crash_after_terminal_receipt_keeps_verified_receipt_and_cleans_journal(self) -> None:
        target_oid = self.run_git("rev-parse", "HEAD").stdout.strip()
        writer = self.crash_writer("terminal_receipt_written")
        preview = writer.preview(
            self.repository_id,
            {"operation": "create_branch", "name": "receipt-written", "targetOid": target_oid},
        )
        with self.assertRaises(GitWriteInjectedCrash):
            self.execute_with(writer, preview, "CREATE BRANCH")

        pending = writer.status(self.repository_id)["pendingTransactions"][0]
        self.assertEqual("verified", pending["receiptIntegrity"])
        self.assertEqual("cleanup_terminal_journal", pending["recoveryDisposition"])
        recovered = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, recovered.startup_recovery_report["cleanedCommitted"])
        self.assertEqual(0, recovered.startup_recovery_report["recoveredReceipts"])
        self.assertEqual(target_oid, self.run_git("rev-parse", "refs/heads/receipt-written").stdout.strip())
        self.assertEqual([], recovered.status(self.repository_id)["pendingTransactions"])

    def test_locked_consumed_preview_cleanup_is_noncritical_after_success(self) -> None:
        target = self.repo_path / "locked-preview.txt"
        target.write_text("locked preview cleanup\n", encoding="utf-8")
        writer = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        preview = writer.preview(self.repository_id, {"operation": "stage", "paths": [target.name]})
        preview_path = writer._preview_path(preview["previewId"])
        original_unlink = writer._unlink_required

        def blocked_preview_only(path: Path) -> None:
            if path == preview_path:
                raise PermissionError(13, "simulated Windows sharing violation", str(path))
            original_unlink(path)

        with mock.patch.object(writer, "_unlink_required", side_effect=blocked_preview_only):
            result = self.execute_with(writer, preview, "STAGE")

        self.assertEqual("committed", result["receipt"]["outcome"])
        self.assertEqual([target.name], self.run_git("diff", "--cached", "--name-only").stdout.splitlines())
        self.assertTrue(preview_path.exists())
        status = writer.status(self.repository_id)
        self.assertEqual(1, status["recoverySummary"]["maintenanceWarningCount"])
        self.assertEqual("cleanup_consumed_git_write_preview", status["maintenanceWarnings"][0]["action"])

    def test_locked_terminal_directory_cleanup_keeps_committed_ref_and_verified_receipt(self) -> None:
        target_oid = self.run_git("rev-parse", "HEAD").stdout.strip()
        writer = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        preview = writer.preview(
            self.repository_id,
            {"operation": "create_tag", "name": "cleanup-blocked", "targetOid": target_oid},
        )
        error = PermissionError(13, "simulated Windows directory sharing violation")
        error.winerror = 32
        with mock.patch("forgetrace.git_writes.shutil.rmtree", side_effect=error), mock.patch(
            "forgetrace.git_writes.time.sleep", return_value=None
        ):
            result = self.execute_with(writer, preview, "CREATE TAG")

        self.assertEqual("committed", result["receipt"]["outcome"])
        self.assertEqual(target_oid, self.run_git("rev-parse", "refs/tags/cleanup-blocked").stdout.strip())
        status = writer.status(self.repository_id)
        self.assertEqual(1, len(status["pendingTransactions"]))
        self.assertEqual("cleanup_terminal_journal", status["pendingTransactions"][0]["recoveryDisposition"])
        self.assertEqual("verified", status["pendingTransactions"][0]["receiptIntegrity"])
        self.assertEqual(1, status["recoverySummary"]["maintenanceWarningCount"])

        recovered = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        self.assertEqual(1, recovered.startup_recovery_report["cleanedCommitted"])
        self.assertEqual(target_oid, self.run_git("rev-parse", "refs/tags/cleanup-blocked").stdout.strip())
        self.assertEqual([], recovered.status(self.repository_id)["pendingTransactions"])

    def test_tampered_injected_journal_is_owner_visible_as_manual_inspection(self) -> None:
        target = self.repo_path / "tampered-diagnostic.txt"
        target.write_text("tampered diagnostic\n", encoding="utf-8")
        writer = self.crash_writer("stage_index_installed")
        preview = writer.preview(self.repository_id, {"operation": "stage", "paths": [target.name]})
        with self.assertRaises(GitWriteInjectedCrash):
            self.execute_with(writer, preview, "STAGE")
        transaction_root = next(writer.transactions_dir.glob("git_txn_*"))
        journal_path = transaction_root / "journal.json"
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        payload["operation"] = "create_branch"
        journal_path.write_text(json.dumps(payload), encoding="utf-8")

        status = writer.status(self.repository_id)
        diagnostic = status["pendingTransactions"][0]
        self.assertEqual("invalid", diagnostic["integrity"])
        self.assertEqual("manual_inspection_required", diagnostic["recoveryDisposition"])
        self.assertTrue(diagnostic["requiresManualInspection"])
        self.assertFalse(diagnostic["recoverable"])
        self.assertEqual(1, status["recoverySummary"]["manualInspectionCount"])

    def test_maintenance_warnings_are_scoped_to_the_originating_repository(self) -> None:
        writer = GitWriteService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            security_events=self.app.security_events,
        )
        error = PermissionError(13, "simulated repository-scoped cleanup warning")
        error.winerror = 32
        writer._record_maintenance_warning(
            action="cleanup_consumed_git_write_preview",
            path=writer.previews_dir / "git_preview_scoped.json",
            exc=error,
            repository_id=self.repository_id,
        )

        second_path = self.root / "second-repo"
        second = self.app.registry.register_repository(
            path=str(second_path),
            name="Second Transactional Git Fixture",
            description="maintenance warning isolation",
            author="Rooke Poole",
            initialize=True,
            create_directory=True,
        )
        subprocess.run([GIT, "init"], cwd=second_path, text=True, capture_output=True, check=True)

        self.assertEqual(1, writer.status(self.repository_id)["recoverySummary"]["maintenanceWarningCount"])
        self.assertEqual(0, writer.status(second["id"])["recoverySummary"]["maintenanceWarningCount"])

    def test_atomic_json_replace_retries_transient_windows_style_sharing_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgetrace-v0521-retry-") as directory:
            destination = Path(directory) / "payload.json"
            real_replace = os.replace
            attempts = {"count": 0}

            def flaky_replace(source, target):
                attempts["count"] += 1
                if attempts["count"] < 3:
                    error = PermissionError(13, "simulated sharing violation", str(target))
                    error.winerror = 32
                    raise error
                return real_replace(source, target)

            with mock.patch("forgetrace.git_writes.os.replace", side_effect=flaky_replace), mock.patch(
                "forgetrace.git_writes.time.sleep", return_value=None
            ):
                GitWriteService._atomic_write_json(destination, {"version": "0.5.2.1"})

            self.assertEqual(3, attempts["count"])
            self.assertEqual({"version": "0.5.2.1"}, json.loads(destination.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
