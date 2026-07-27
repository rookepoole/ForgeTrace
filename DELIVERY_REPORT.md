# ForgeTrace v0.5.3 Design Contract Delivery

## v0.5.3.0 delivery

Delivered an internal read-only switch preflight and sealed capture planner, 14 focused tests, machine/human contract updates, and complete validation evidence. This is a real runtime capability for analysis and evidence capture, but deliberately not a branch-switch execution release.

This continuation delivers a reviewable transactional switch/checkout contract without changing runtime authority. The contract fixes the future scope to existing local branch switching from a clean tracked state, requires exact worktree and ignored/untracked byte protection, defines lock and deletion interactions, specifies conservative crash recovery, and prohibits merge/remotes.

The operator-reported v0.5.2.2 automated Windows gate `OK` is recorded separately from the still-unrecorded owner-browser checklist.

# ForgeTrace v0.5.2.2 Delivery Report

## v0.5.2.2 runner repair

The Windows gate failure reported after a passing unittest line was traced to Windows PowerShell 5.1 converting ordinary native stderr into a terminating `NativeCommandError`. The corrected runner uses process-boundary stdout/stderr redirection and real exit-code evaluation. Runtime ForgeTrace code is unchanged except for the application version string; no Git-write authority was expanded.


**Release:** Windows and Failure-Injection Hardening  
**Creator:** Rooke Poole  
**License:** MIT  
**Date:** 2026-07-26

## Delivered

v0.5.2.1 preserves the v0.5.2 owner-only operations—selected-file stage, commit of the already staged tree, local branch creation without switching `HEAD`, and lightweight local tags—and hardens their Windows and crash-recovery boundaries.

The transaction journal now records sealed checkpoints around capture finalization, index installation, Git object creation, ref/reflog installation, rollback, terminal journal state, and receipt creation. The test harness can abruptly stop at those points without invoking in-process rollback, allowing a fresh service instance to prove exact startup recovery from durable evidence.

Critical atomic replacements and required removals use bounded retry for transient Windows sharing conditions. Non-critical cleanup after terminal evidence, consumed/expired previews, and receipt retention is outside the transaction result. A locked cleanup file creates repository-scoped maintenance evidence and leaves receipts/journals for retry; it cannot convert a successful write into a false rollback.

Owner status, the Git panel, and Health now expose journal/receipt integrity, last durable checkpoint, native blockers, recovery disposition, automatic/manual classification, and the exact next step. Read-only Git intelligence remains independent.

## Validation

- Fresh-process Python inventory: 249 discovered across 27 modules; 247 passed on Linux; 2 physical Windows-only tests skipped.
- Focused transactional suites: 27/27 passed.
- Focused branch-aware coverage: `forgetrace/git_writes.py` 80%.
- Chromium: 19 applicable workflows reached PASS; direct collaboration navigation remains an environment-policy skip.
- Python compilation: 75 files passed.
- Owner and contributor JavaScript syntax checks passed.
- Source-manifest and clean-room archive verification are recorded in the final package evidence.

## Honest platform boundary

The v0.5.1.2 Windows deletion prerequisite is accepted from the operator-reported unskipped `OK`. Physical v0.5.2.2 Git-write acceptance has not been completed from this Linux environment. Run `tests/run_v0522_windows_git_write_acceptance.ps1` and complete `tests/WINDOWS_TRANSACTIONAL_GIT_WRITES_ACCEPTANCE.md` against the exact packaged archive before marking Windows acceptance complete.

## Release artifacts

Runtime application data, bytecode caches, coverage databases, temporary browser profiles, validation scratch files, and VCS metadata are excluded. The source manifest and archive SHA-256 values are generated after final clean-room validation.
