# Windows Managed Repository Permanent-Deletion Acceptance — v0.5.1.2

## Accepted prerequisite record

On **2026-07-26**, the operator reported that the following exact test completed on Windows with an unskipped `OK`:

```powershell
py -3 -m unittest -v tests.test_v0512_windows_delete_and_security_fetch.WindowsDeletionIntentRegressionTest.test_physical_windows_external_intent_delete_transaction
```

This satisfies the v0.5.2 prerequisite gate. The report was supplied by the Windows operator; the Linux packaging environment did not independently execute or capture Win32 output. Linux therefore continues to skip this platform-only test.

## Remaining manual observations

For future maintenance, preserve the original manual checks:

1. Create a disposable managed repository, add a file, and permanently delete it from Settings.
2. Confirm the registry entry and managed directory remain absent after restart.
3. Confirm the primary Security event list loads even if auxiliary segmented-history status is degraded.
4. Repeat deletion while a known editor holds a file. A persistent blocker must return `repository_delete_path_busy`, preserve registration/bytes, and identify process/PID when Restart Manager can provide evidence.
5. Close the blocker and confirm retry completes.

Record Windows, Python, filesystem, Git, antivirus, and blocker details with future release evidence.
