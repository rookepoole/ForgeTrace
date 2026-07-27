from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from forgetrace.constants import APP_VERSION
from forgetrace.git_writes import OPERATIONS


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "TRANSACTIONAL_SWITCH_CHECKOUT_CONTRACT.json"
DESIGN_PATH = ROOT / "docs" / "TRANSACTIONAL_SWITCH_CHECKOUT_DESIGN.md"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TransactionalSwitchCheckoutDesignContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_preflight_planner_claims_only_the_bounded_runtime_slice(self) -> None:
        metadata = json.loads((ROOT / "PACKAGE_METADATA.json").read_text(encoding="utf-8"))
        self.assertEqual("0.5.3.0", APP_VERSION)
        self.assertEqual(APP_VERSION, metadata["version"])
        self.assertEqual("0.5.3", metadata["design_version"])
        self.assertEqual("preflight_capture_planner_implemented_execution_absent", metadata["design_status"])
        self.assertTrue(metadata["transactional_switch_preflight_planner_implemented"])
        self.assertFalse(metadata["transactional_switch_checkout_implemented"])

    def test_machine_contract_accepts_one_narrow_operation(self) -> None:
        self.assertEqual(1, self.contract["schemaVersion"])
        self.assertEqual("0.5.3", self.contract["designVersion"])
        self.assertEqual("preflight_capture_planner_implemented_execution_absent", self.contract["status"])
        self.assertEqual("switch_branch", self.contract["operation"]["id"])
        self.assertEqual("SWITCH BRANCH", self.contract["operation"]["requiredConfirmation"])
        self.assertTrue(self.contract["acceptedScope"]["existingLocalBranchOnly"])
        self.assertTrue(self.contract["acceptedScope"]["cleanIndexRequired"])
        self.assertTrue(self.contract["acceptedScope"]["cleanTrackedWorktreeRequired"])
        self.assertIn("merge, reset, rebase, cherry-pick, revert, stash", self.contract["explicitlyExcluded"])
        self.assertIn("fetch, pull, push, clone, remote contact", self.contract["explicitlyExcluded"])

    def test_design_requires_exact_worktree_and_untracked_evidence(self) -> None:
        captures = self.contract["transactionEvidence"]["captures"]
        self.assertIn("exact pre-state bytes and mode for every affected tracked path", captures)
        self.assertIn(
            "exact bytes and mode for every bounded untracked or ignored regular file",
            captures,
        )
        self.assertIn("target collisions with any untracked or ignored path", self.contract["preflight"]["rejects"])
        self.assertIn("unknown bytes require retained evidence and manual inspection", self.contract["recovery"]["ownershipRule"])
        self.assertTrue(self.contract["recovery"]["pendingSwitchJournalBlocksPermanentDeletion"])

    def test_human_design_contains_required_trust_sections(self) -> None:
        source = DESIGN_PATH.read_text(encoding="utf-8")
        for heading in (
            "## 5. Untracked and ignored files are part of the safety boundary",
            "## 7. Locking and competing authorities",
            "## 8. Durable evidence captured before mutation",
            "## 11. Recovery ownership and exact rollback",
            "## 12. Separation from quarantine conflict resolution",
            "## 14. Required implementation tests",
            "## 15. Design-package invariant",
        ):
            self.assertIn(heading, source)

    def test_v052_runtime_operation_set_remains_unchanged(self) -> None:
        self.assertEqual({"stage", "commit", "create_branch", "create_tag"}, OPERATIONS)
        self.assertNotIn("switch_branch", OPERATIONS)
        self.assertNotIn("checkout", OPERATIONS)

    def test_no_execute_route_ui_or_native_switch_command_was_added(self) -> None:
        route_and_ui_paths = [
            ROOT / "forgetrace" / "web.py",
            ROOT / "index.html",
            ROOT / "contribute.html",
            ROOT / "assets" / "owner.js",
            ROOT / "assets" / "contributor.js",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in route_and_ui_paths if path.exists())
        self.assertNotIn("/git-switch/", combined)
        self.assertNotIn("SWITCH BRANCH", combined)
        service = (ROOT / "forgetrace" / "git_switch.py").read_text(encoding="utf-8")
        self.assertNotIn('["switch"', service)
        self.assertNotIn('["checkout"', service)

    def test_windows_gate_record_separates_automated_ok_from_browser_acceptance(self) -> None:
        metadata = json.loads((ROOT / "PACKAGE_METADATA.json").read_text(encoding="utf-8"))
        self.assertTrue(metadata["windows_v0522_git_write_automated_acceptance"])
        self.assertIn("operator-reported", metadata["windows_v0522_git_write_automated_acceptance_basis"])
        self.assertFalse(metadata["windows_v0522_owner_browser_acceptance"])
        self.assertFalse(metadata["windows_v0522_git_write_physical_acceptance"])

    @unittest.skipUnless(shutil.which("git"), "Git is required for the disposable design probe")
    def test_native_switch_changes_head_index_and_head_reflog_not_branch_refs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgetrace-v053-probe-") as raw:
            root = Path(raw)
            self._git(root, "init", "-q", "-b", "main")
            self._git(root, "config", "user.name", "ForgeTrace Design Probe")
            self._git(root, "config", "user.email", "probe@example.invalid")
            self._git(root, "config", "core.autocrlf", "false")
            (root / "a.txt").write_bytes(b"main\n")
            self._git(root, "add", "a.txt")
            self._git(root, "commit", "-qm", "main")
            self._git(root, "branch", "topic")
            self._git(root, "switch", "-q", "topic")
            (root / "a.txt").write_bytes(b"topic\n")
            (root / "b.txt").write_bytes(b"new\n")
            self._git(root, "add", "a.txt", "b.txt")
            self._git(root, "commit", "-qm", "topic")
            self._git(root, "switch", "-q", "main")
            (root / "untracked.bin").write_bytes(b"preserve-me\x00\xff")

            git_dir = root / ".git"
            tracked = {
                name: _sha256(git_dir / name)
                for name in (
                    "HEAD",
                    "index",
                    "logs/HEAD",
                    "refs/heads/main",
                    "refs/heads/topic",
                    "logs/refs/heads/main",
                    "logs/refs/heads/topic",
                )
            }
            self._git(
                root,
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "protocol.allow=never",
                "switch",
                "--no-guess",
                "--no-recurse-submodules",
                "topic",
            )
            after = {name: _sha256(git_dir / name) for name in tracked}
            self.assertNotEqual(tracked["HEAD"], after["HEAD"])
            self.assertNotEqual(tracked["index"], after["index"])
            self.assertNotEqual(tracked["logs/HEAD"], after["logs/HEAD"])
            for name in (
                "refs/heads/main",
                "refs/heads/topic",
                "logs/refs/heads/main",
                "logs/refs/heads/topic",
            ):
                self.assertEqual(tracked[name], after[name])
            self.assertEqual(b"preserve-me\x00\xff", (root / "untracked.bin").read_bytes())

    @unittest.skipUnless(shutil.which("git"), "Git is required for the disposable design probe")
    def test_native_switch_can_overwrite_an_ignored_collision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forgetrace-v053-collision-") as raw:
            root = Path(raw)
            self._git(root, "init", "-q", "-b", "main")
            self._git(root, "config", "user.name", "ForgeTrace Design Probe")
            self._git(root, "config", "user.email", "probe@example.invalid")
            self._git(root, "config", "core.autocrlf", "false")
            (root / "base.txt").write_bytes(b"base\n")
            self._git(root, "add", "base.txt")
            self._git(root, "commit", "-qm", "base")
            self._git(root, "branch", "topic")
            self._git(root, "switch", "-q", "topic")
            (root / "ignored.txt").write_bytes(b"target tracked bytes\n")
            self._git(root, "add", "ignored.txt")
            self._git(root, "commit", "-qm", "target")
            self._git(root, "switch", "-q", "main")
            (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            self._git(root, "add", ".gitignore")
            self._git(root, "commit", "-qm", "ignore local collision")
            (root / "ignored.txt").write_bytes(b"owner local ignored bytes\n")

            self._git(
                root,
                "-c",
                f"core.hooksPath={os.devnull}",
                "switch",
                "--no-guess",
                "--no-recurse-submodules",
                "topic",
            )
            self.assertEqual(b"target tracked bytes\n", (root / "ignored.txt").read_bytes())

    def _git(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [shutil.which("git") or "git", *args],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            },
        )
        if result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr}")
        return result


if __name__ == "__main__":
    unittest.main()
