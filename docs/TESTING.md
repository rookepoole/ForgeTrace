# ForgeTrace Testing — v0.5.2.2

## v0.5.3.0 switch-planner validation

- 277 tests across 30 isolated Python modules; 275 passed on Linux and 2 physical-Windows tests skipped.
- 14 focused preflight/capture tests passed with 82% branch-aware coverage of `forgetrace/git_switch.py`.
- 79 Python files compiled.
- Owner and contributor inline JavaScript bundles passed Node syntax checking.
- The unchanged transactional Git-write owner Chromium workflow passed.

The focused suite proves no repository mutation during read-model, capture, or verification; exact capture integrity; stale source/target/local-byte rejection; ignored/untracked and file/directory collisions; conservative case-fold rejection; read-only/deletion/native-lock enforcement; configuration/index-feature rejection; resource limits; detached/unborn/invalid targets; special-file rejection; and cleanup after injected capture failure.

## v0.5.2.2 Windows runner repair

The physical-Windows runner uses `Start-Process` with separate redirected stdout/stderr files. This avoids Windows PowerShell 5.1 turning Python unittest's normal stderr progress into a terminating `NativeCommandError`. The gate checks the native exit code and appends `AUTOMATED_RESULT: OK` only after the complete suite succeeds. Run `tests/run_v0522_windows_git_write_acceptance.ps1` from the exact archive.


## v0.5.2.1 validation scope

`tests/test_v0521_git_write_failure_injection.py` injects abrupt stops after index/ref/reflog and terminal-evidence checkpoints, then constructs a fresh service to prove exact rollback, receipt reconstruction, native-lock deferral, tamper retention, and Windows-style cleanup behavior. The injected signal is test-only constructor state and is not reachable through HTTP or the UI.

The exact physical-Windows gate is `tests/run_v0522_windows_git_write_acceptance.ps1` plus `tests/WINDOWS_TRANSACTIONAL_GIT_WRITES_ACCEPTANCE.md`. Linux evidence validates deterministic transaction logic but does not claim Windows filesystem acceptance.


## v0.5.2 accepted evidence

- 249 Python tests discovered across 27 fresh processes; 247 passed on Linux and 2 physical Windows-only tests skipped.
- 27 focused transactional Git-write tests passed with 80% branch-aware coverage of `forgetrace/git_writes.py`.
- 19 applicable Chromium workflows passed; the direct collaboration navigation script remains an environment-policy skip with equivalent HTTP isolation coverage.
- Python compilation and both inline JavaScript bundles passed syntax validation.
- The v0.5.1.2 physical Windows deletion transaction prerequisite was operator-reported as an unskipped `OK` on 2026-07-26.

See `HANDOFF/EVIDENCE/v052-*` for current evidence. A monolithic suite process can stall in the inherited fsync-sensitive region on this host; only completed fresh-process modules are counted.

## v0.4.10 accepted evidence

- Baseline before maintenance changes: **185/185** Python unit/integration tests passed.
- Final inventory: **196/196** tests pass across isolated fresh Python processes.
- Focused repository-management suite: **11/11** passed warning-free.
- Real Chromium repository-management workflow: **PASS**.
- The full applicable browser matrix contains **15 workflows**; final results are recorded in `HANDOFF/EVIDENCE/browser-workflows.log`.
- Static and coverage results are recorded in `HANDOFF/EVIDENCE/`.

The focused suite covers initialized, manually emptied, and missing managed repository paths; external-path and read-only denial; ledger fail-closed behavior; contributor-gateway denial; security events; cross-process locking; pre-commit rollback; post-commit finalization; explicit restoration; tombstone suppression; and the enlarged Files workspace.

## Unit and integration suite

The authoritative host command runs every test module in a fresh process:

```bash
for file in tests/test_*.py; do
  module="tests.$(basename "$file" .py)"
  TMPDIR=/dev/shm PYTHONDONTWRITEBYTECODE=1 \
    python3 -m unittest -q "$module"
done
```

The mounted validation filesystem can intermittently stall one monolithic Python process after extensive inherited fsync, SQLite, subprocess, and Chromium activity. Every module exits cleanly in a fresh process. Evidence records that distinction rather than claiming a reliable monolithic exit on this host.

## Browser workflow

`tests/browser_repository_management_test.py` renders the real owner HTML in Chromium and verifies:

- at least 500 px rendered tree height;
- at least 500 px rendered file-pane width at 1680×1200;
- file pane at least 44% of the Files layout;
- the managed-repository danger zone and exact-name confirmation;
- the actual owner HTTP deletion route;
- managed-directory removal, active-repository fallback, and security events;
- restart-time tombstone suppression of a copied UUID-bearing repository.

Managed Chromium blocks direct localhost navigation. The workflow uses the established policy-compatible browser transport for initial UI data and forwards the destructive request through the real owner HTTP server. This is not represented as direct localhost browser navigation.

## Static validation

All Python sources must compile without writing bytecode into the source package, and both owner and contributor inline JavaScript bundles must pass Node syntax checking. Exact results are stored in `HANDOFF/EVIDENCE/static-validation.log`.

## Physical Windows gate

Automated Linux tests cover native-picker command/process/PowerShell/Unicode/cancellation/headless contracts. They do not replace the physical Windows checklist at `tests/WINDOWS_NATIVE_PICKER_ACCEPTANCE.md`.
