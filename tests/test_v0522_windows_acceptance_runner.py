from __future__ import annotations

import json
import unittest
from pathlib import Path

from forgetrace.constants import APP_VERSION


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests" / "run_v0522_windows_git_write_acceptance.ps1"
COMPAT_RUNNER = ROOT / "tests" / "run_v0521_windows_git_write_acceptance.ps1"


class WindowsAcceptanceRunnerRepairTest(unittest.TestCase):
    def test_release_metadata_preserves_runner_repair_after_v0522(self) -> None:
        metadata = json.loads((ROOT / "PACKAGE_METADATA.json").read_text(encoding="utf-8"))
        self.assertEqual(APP_VERSION, metadata["version"])
        version = tuple(int(part) for part in APP_VERSION.split("."))
        self.assertGreaterEqual(version, (0, 5, 2, 2))
        self.assertEqual(
            "tests/run_v0522_windows_git_write_acceptance.ps1",
            metadata["windows_git_write_acceptance_runner"],
        )
        self.assertTrue(metadata["windows_v0522_git_write_automated_acceptance"])

    def test_runner_never_merges_native_stderr_into_the_error_pipeline(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn('$ErrorActionPreference = "Stop"', source)
        self.assertNotIn("2>&1", source)
        self.assertNotIn("Tee-Object", source)
        self.assertIn("Start-Process", source)
        self.assertIn("-RedirectStandardOutput", source)
        self.assertIn("-RedirectStandardError", source)

    def test_runner_checks_real_process_exit_code_and_records_terminal_ok(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("$testResult.ExitCode -ne 0", source)
        self.assertIn("AUTOMATED_RESULT: OK", source)
        self.assertIn("tests.test_v052_transactional_git_writes", source)
        self.assertIn("tests.test_v0521_git_write_failure_injection", source)
        self.assertIn("tests.test_v0522_windows_acceptance_runner", source)

    def test_legacy_runner_delegates_to_the_repaired_gate(self) -> None:
        source = COMPAT_RUNNER.read_text(encoding="utf-8")
        self.assertIn("run_v0522_windows_git_write_acceptance.ps1", source)
        self.assertNotIn("2>&1", source)
        self.assertNotIn("Tee-Object", source)


if __name__ == "__main__":
    unittest.main()
