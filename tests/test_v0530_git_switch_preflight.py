from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from forgetrace.app import build_application
from forgetrace.errors import ForgeTraceError
from forgetrace.git_switch import GitSwitchService
from forgetrace.git_writes import OPERATIONS

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")


@unittest.skipUnless(GIT, "Git executable is required")
class GitSwitchPlannerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="forgetrace-v0530-")
        self.root = Path(self.temp.name)
        self.app = build_application(ROOT, self.root / "data")
        self.repo_path = self.root / "repo"
        record = self.app.registry.register_repository(
            path=str(self.repo_path),
            name="Switch Planner Fixture",
            description="v0.5.3.0",
            author="Rooke Poole",
            initialize=True,
            create_directory=True,
        )
        self.repository_id = record["id"]
        self.run_git("init")
        self.run_git("config", "user.name", "External Fixture")
        self.run_git("config", "user.email", "external@example.invalid")
        (self.repo_path / "delete-me.txt").write_text("delete from target\n", encoding="utf-8")
        (self.repo_path / "unchanged.txt").write_text("unchanged\n", encoding="utf-8")
        self.run_git("add", "README.md", "delete-me.txt", "unchanged.txt")
        self.run_git("commit", "-m", "Initial switch fixture")
        self.source_branch = self.run_git("branch", "--show-current").stdout.strip()

    def tearDown(self) -> None:
        if self.app.gateway:
            self.app.gateway.stop()
        self.temp.cleanup()

    def run_git(
        self,
        *args: str,
        check: bool = True,
        input_data: str | bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        text = not isinstance(input_data, bytes)
        return subprocess.run(
            [GIT, *args],
            cwd=self.repo_path,
            input=input_data,
            text=text,
            capture_output=True,
            check=check,
            env=env,
        )

    def create_target(
        self,
        *,
        name: str = "feature/switch-target",
        additions: dict[str, bytes | str] | None = None,
        include_default_changes: bool = True,
    ) -> str:
        self.run_git("switch", "-c", name)
        staged: list[str] = []
        if include_default_changes:
            (self.repo_path / "README.md").write_text("# Target branch\n\nChanged safely.\n", encoding="utf-8")
            (self.repo_path / "delete-me.txt").unlink()
            target = self.repo_path / "target" / "new.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("target bytes\n", encoding="utf-8")
            staged.extend(["README.md", "delete-me.txt", "target/new.txt"])
        for rel, value in (additions or {}).items():
            path = self.repo_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, bytes):
                path.write_bytes(value)
            else:
                path.write_text(value, encoding="utf-8")
            staged.append(rel)
        if staged:
            self.run_git("add", "-A", "--", *staged)
        self.run_git("commit", "-m", f"Target branch {name}")
        self.run_git("switch", self.source_branch)
        return name

    def add_local_preserved_files(self) -> None:
        info_exclude = self.repo_path / ".git" / "info" / "exclude"
        info_exclude.write_text(info_exclude.read_text(encoding="utf-8") + "ignored.bin\n", encoding="utf-8")
        (self.repo_path / "keep-local.txt").write_text("untracked local bytes\n", encoding="utf-8")
        (self.repo_path / "ignored.bin").write_bytes(b"ignored local bytes\x00\x01")

    def planner(self, **kwargs) -> GitSwitchService:
        return GitSwitchService(
            registry=self.app.registry,
            git_intelligence=self.app.git,
            git_writes=self.app.git_writes,
            **kwargs,
        )


class GitSwitchPlannerTest(GitSwitchPlannerFixture):
    def test_read_model_and_sealed_plan_capture_without_repository_mutation(self) -> None:
        target = self.create_target()
        self.add_local_preserved_files()
        planner = self.app.git_switches

        git_dir = self.repo_path / ".git"
        before = {
            "head": (git_dir / "HEAD").read_bytes(),
            "index": (git_dir / "index").read_bytes(),
            "logsHead": (git_dir / "logs" / "HEAD").read_bytes(),
            "sourceRef": self.run_git("rev-parse", f"refs/heads/{self.source_branch}").stdout.strip(),
            "targetRef": self.run_git("rev-parse", f"refs/heads/{target}").stdout.strip(),
            "branch": self.run_git("branch", "--show-current").stdout.strip(),
            "readme": (self.repo_path / "README.md").read_bytes(),
            "untracked": (self.repo_path / "keep-local.txt").read_bytes(),
            "ignored": (self.repo_path / "ignored.bin").read_bytes(),
        }

        model = planner.read_model(self.repository_id)
        self.assertEqual(self.source_branch, model["source"]["name"])
        self.assertIn(target, {item["name"] for item in model["targets"]})
        self.assertTrue(model["plannerOnly"])
        self.assertFalse(model["executionImplemented"])
        self.assertFalse(model["network"])

        plan = planner.plan_capture(self.repository_id, target)
        self.assertEqual("sealed_capture_plan", plan["status"])
        self.assertEqual("SWITCH BRANCH", plan["requiredConfirmation"])
        self.assertFalse(plan["authority"]["repositoryMutation"])
        self.assertEqual(2, len(plan["analysis"]["preservedLocalFiles"]))
        classifications = {item["path"]: item["classification"] for item in plan["analysis"]["preservedLocalFiles"]}
        self.assertEqual("untracked", classifications["keep-local.txt"])
        self.assertEqual("ignored", classifications["ignored.bin"])
        self.assertGreaterEqual(plan["analysis"]["captureEstimate"]["affectedTrackedPathCount"], 3)
        self.assertTrue(plan["planDigest"])
        self.assertTrue(plan["capturesDigest"])
        self.assertTrue((planner.plans_dir / plan["planId"] / "SEALED").is_file())
        self.assertTrue(planner.verify_plan(plan["planId"])["valid"])

        after = {
            "head": (git_dir / "HEAD").read_bytes(),
            "index": (git_dir / "index").read_bytes(),
            "logsHead": (git_dir / "logs" / "HEAD").read_bytes(),
            "sourceRef": self.run_git("rev-parse", f"refs/heads/{self.source_branch}").stdout.strip(),
            "targetRef": self.run_git("rev-parse", f"refs/heads/{target}").stdout.strip(),
            "branch": self.run_git("branch", "--show-current").stdout.strip(),
            "readme": (self.repo_path / "README.md").read_bytes(),
            "untracked": (self.repo_path / "keep-local.txt").read_bytes(),
            "ignored": (self.repo_path / "ignored.bin").read_bytes(),
        }
        self.assertEqual(before, after)
        captured = {item["path"]: item for item in plan["captures"]}
        for rel in ("keep-local.txt", "ignored.bin", "README.md", "delete-me.txt"):
            self.assertIn(rel, captured)
            backup = planner.plans_dir / plan["planId"] / captured[rel]["backupPath"]
            self.assertEqual(captured[rel]["sha256"], planner._hash_file(backup)[1])

    def test_plan_revalidation_detects_target_and_local_byte_drift(self) -> None:
        target = self.create_target()
        self.add_local_preserved_files()
        planner = self.app.git_switches
        plan = planner.plan_capture(self.repository_id, target)

        (self.repo_path / "keep-local.txt").write_text("changed after plan\n", encoding="utf-8")
        with self.assertRaises(ForgeTraceError) as local_drift:
            planner.verify_plan(plan["planId"])
        self.assertEqual("git_switch_plan_stale", local_drift.exception.code)

        (self.repo_path / "keep-local.txt").write_text("untracked local bytes\n", encoding="utf-8")
        self.run_git("switch", target)
        (self.repo_path / "target" / "new.txt").write_text("new target revision\n", encoding="utf-8")
        self.run_git("add", "target/new.txt")
        self.run_git("commit", "-m", "Target drift")
        self.run_git("switch", self.source_branch)
        with self.assertRaises(ForgeTraceError) as target_drift:
            planner.verify_plan(plan["planId"])
        self.assertEqual("git_switch_plan_stale", target_drift.exception.code)

    def test_sealed_plan_expires_after_the_bound_window(self) -> None:
        target = self.create_target()
        plan = self.app.git_switches.plan_capture(self.repository_id, target)
        with mock.patch("forgetrace.git_switch.time.time", return_value=plan["expiresAtEpoch"] + 1):
            with self.assertRaises(ForgeTraceError) as expired:
                self.app.git_switches.verify_plan(plan["planId"])
        self.assertEqual("git_switch_plan_expired", expired.exception.code)

    def test_tampered_plan_or_backup_fails_integrity_verification(self) -> None:
        target = self.create_target()
        self.add_local_preserved_files()
        planner = self.app.git_switches
        first = planner.plan_capture(self.repository_id, target)
        first_root = planner.plans_dir / first["planId"]
        backup = first_root / first["captures"][0]["backupPath"]
        backup.write_bytes(backup.read_bytes() + b"tamper")
        with self.assertRaises(ForgeTraceError) as backup_error:
            planner.load_plan(first["planId"])
        self.assertEqual("git_switch_plan_integrity_failed", backup_error.exception.code)

        second = planner.plan_capture(self.repository_id, target)
        second_path = planner.plans_dir / second["planId"] / "plan.json"
        payload = json.loads(second_path.read_text(encoding="utf-8"))
        payload["targetBranch"] = "tampered"
        second_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ForgeTraceError) as plan_error:
            planner.load_plan(second["planId"])
        self.assertEqual("git_switch_plan_integrity_failed", plan_error.exception.code)

    def test_staged_and_unstaged_tracked_changes_fail_closed(self) -> None:
        target = self.create_target()
        planner = self.app.git_switches

        (self.repo_path / "staged.txt").write_text("staged\n", encoding="utf-8")
        self.run_git("add", "staged.txt")
        with self.assertRaises(ForgeTraceError) as staged:
            planner.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_index_not_clean", staged.exception.code)
        self.run_git("reset", "--hard", "HEAD")

        (self.repo_path / "README.md").write_text("unstaged\n", encoding="utf-8")
        with self.assertRaises(ForgeTraceError) as unstaged:
            planner.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_tracked_worktree_not_clean", unstaged.exception.code)

    def test_ignored_and_untracked_target_collisions_fail_closed(self) -> None:
        target = self.create_target(additions={"ignored.bin": b"target ignored path"})
        info_exclude = self.repo_path / ".git" / "info" / "exclude"
        info_exclude.write_text(info_exclude.read_text(encoding="utf-8") + "ignored.bin\n", encoding="utf-8")
        (self.repo_path / "ignored.bin").write_bytes(b"local ignored bytes")
        with self.assertRaises(ForgeTraceError) as collision:
            self.app.git_switches.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_target_collision", collision.exception.code)
        self.assertIn("target_file_overwrites_local_file", {item["reason"] for item in collision.exception.details["collisions"]})

    def test_directory_file_and_casefold_collisions_fail_closed(self) -> None:
        target = self.create_target(additions={"folder": "target file\n", "Case.txt": "case target\n"})
        folder = self.repo_path / "folder"
        folder.mkdir()
        (folder / "local.txt").write_text("local\n", encoding="utf-8")
        with self.assertRaises(ForgeTraceError) as directory_collision:
            self.app.git_switches.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_target_collision", directory_collision.exception.code)

        shutil.rmtree(folder)
        (self.repo_path / "case.txt").write_text("local case\n", encoding="utf-8")
        with self.assertRaises(ForgeTraceError) as case_collision:
            self.app.git_switches.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_casefold_collision", case_collision.exception.code)

    def test_source_and_local_casefold_ambiguity_fails_closed(self) -> None:
        target = self.create_target()
        tracked = self.repo_path / "TrackedCase.txt"
        tracked.write_text("tracked source bytes\n", encoding="utf-8")
        self.run_git("add", "TrackedCase.txt")
        self.run_git("commit", "-m", "Add case-sensitive source path")
        (self.repo_path / "trackedcase.txt").write_text("untracked local bytes\n", encoding="utf-8")

        with self.assertRaises(ForgeTraceError) as collision:
            self.app.git_switches.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_casefold_collision", collision.exception.code)

    def test_read_only_deletion_intent_and_native_git_lock_block_planning(self) -> None:
        target = self.create_target()
        planner = self.app.git_switches

        self.app.registry.set_access_mode(self.repository_id, "read_only")
        with self.assertRaises(ForgeTraceError) as read_only:
            planner.plan_capture(self.repository_id, target)
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
                planner.plan_capture(self.repository_id, target)
            self.assertEqual("repository_delete_in_progress", deleting.exception.code)
        finally:
            self.app.registry._clear_deletion_intent(self.repository_id)

        native = self.repo_path / ".git" / "index.lock"
        native.write_text("external git", encoding="utf-8")
        try:
            with self.assertRaises(ForgeTraceError) as locked:
                planner.plan_capture(self.repository_id, target)
            self.assertEqual("git_switch_native_lock_present", locked.exception.code)
        finally:
            native.unlink()

    def test_checkout_affecting_configuration_attributes_and_index_flags_are_rejected(self) -> None:
        target = self.create_target()
        self.run_git("config", "core.autocrlf", "true")
        with self.assertRaises(ForgeTraceError) as config:
            self.app.git_switches.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_checkout_configuration_unsupported", config.exception.code)
        self.run_git("config", "--unset", "core.autocrlf")

        self.run_git("update-index", "--assume-unchanged", "README.md")
        with self.assertRaises(ForgeTraceError) as flags:
            self.app.git_switches.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_index_flags_unsupported", flags.exception.code)
        self.run_git("update-index", "--no-assume-unchanged", "README.md")

        self.run_git("switch", target)
        (self.repo_path / ".gitattributes").write_text("*.txt text\n", encoding="utf-8")
        self.run_git("add", ".gitattributes")
        self.run_git("commit", "-m", "Add attributes")
        self.run_git("switch", self.source_branch)
        with self.assertRaises(ForgeTraceError) as attributes:
            self.app.git_switches.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_checkout_attributes_unsupported", attributes.exception.code)

    def test_limits_and_free_space_are_enforced_before_capture_installation(self) -> None:
        target = self.create_target()
        (self.repo_path / "one.tmp").write_text("1", encoding="utf-8")
        (self.repo_path / "two.tmp").write_text("2", encoding="utf-8")
        limited_entries = self.planner(max_untracked_entries=1)
        with self.assertRaises(ForgeTraceError) as entries:
            limited_entries.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_untracked_entry_limit", entries.exception.code)

        (self.repo_path / "one.tmp").unlink()
        (self.repo_path / "two.tmp").unlink()
        limited_bytes = self.planner(max_capture_bytes=8)
        with self.assertRaises(ForgeTraceError) as bytes_limit:
            limited_bytes.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_capture_size_limit", bytes_limit.exception.code)

        usage = namedtuple("usage", "total used free")(1024, 1024, 0)
        with mock.patch("forgetrace.git_switch.shutil.disk_usage", return_value=usage):
            with self.assertRaises(ForgeTraceError) as free_space:
                self.app.git_switches.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_free_space_insufficient", free_space.exception.code)

    def test_detached_current_missing_target_and_same_target_are_rejected(self) -> None:
        target = self.create_target()
        with self.assertRaises(ForgeTraceError) as same:
            self.app.git_switches.plan_capture(self.repository_id, self.source_branch)
        self.assertEqual("git_switch_target_is_current", same.exception.code)

        with self.assertRaises(ForgeTraceError) as missing:
            self.app.git_switches.plan_capture(self.repository_id, "missing/branch")
        self.assertEqual("git_switch_target_not_found", missing.exception.code)

        self.run_git("checkout", "--detach", "HEAD")
        with self.assertRaises(ForgeTraceError) as detached:
            self.app.git_switches.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_detached_head", detached.exception.code)

    @unittest.skipIf(os.name == "nt", "Creating a symlink may require Windows developer mode")
    def test_untracked_symlink_is_rejected_without_following_it(self) -> None:
        target = self.create_target()
        outside = self.root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        os.symlink(outside, self.repo_path / "local-link")
        with self.assertRaises(ForgeTraceError) as symlink:
            self.app.git_switches.plan_capture(self.repository_id, target)
        self.assertEqual("git_switch_worktree_entry_unsupported", symlink.exception.code)
        self.assertEqual("outside\n", outside.read_text(encoding="utf-8"))

    def test_capture_failure_leaves_no_installed_plan_and_no_repository_change(self) -> None:
        target = self.create_target()
        self.add_local_preserved_files()
        planner = self.app.git_switches
        git_dir = self.repo_path / ".git"
        before = {
            "head": (git_dir / "HEAD").read_bytes(),
            "index": (git_dir / "index").read_bytes(),
            "logs": (git_dir / "logs" / "HEAD").read_bytes(),
            "status": self.run_git("status", "--porcelain=v2", "-z").stdout,
        }
        original = planner._capture_file
        calls = {"count": 0}

        def fail_after_first(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated application-data capture failure")
            return original(*args, **kwargs)

        with mock.patch.object(planner, "_capture_file", side_effect=fail_after_first):
            with self.assertRaises(OSError):
                planner.plan_capture(self.repository_id, target)
        self.assertEqual([], list(planner.plans_dir.iterdir()))
        self.assertEqual([], list(planner.staging_dir.iterdir()))
        after = {
            "head": (git_dir / "HEAD").read_bytes(),
            "index": (git_dir / "index").read_bytes(),
            "logs": (git_dir / "logs" / "HEAD").read_bytes(),
            "status": self.run_git("status", "--porcelain=v2", "-z").stdout,
        }
        self.assertEqual(before, after)

    def test_runtime_exposes_no_switch_execute_route_ui_or_git_write_operation(self) -> None:
        self.assertFalse(hasattr(self.app.git_switches, "execute"))
        self.assertEqual({"stage", "commit", "create_branch", "create_tag"}, OPERATIONS)
        web_source = (ROOT / "forgetrace" / "web.py").read_text(encoding="utf-8")
        owner_html = (ROOT / "index.html").read_text(encoding="utf-8")
        contributor_html = (ROOT / "contribute.html").read_text(encoding="utf-8")
        service_source = (ROOT / "forgetrace" / "git_switch.py").read_text(encoding="utf-8")
        self.assertNotIn("/git-switch/", web_source)
        self.assertNotIn("SWITCH BRANCH", owner_html)
        self.assertNotIn("SWITCH BRANCH", contributor_html)
        self.assertNotIn('["switch"', service_source)
        self.assertNotIn('["checkout"', service_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
