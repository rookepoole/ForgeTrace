# ForgeTrace v0.4.0 Stabilization Delivery Report

## Purpose

v0.4.0 is a data-integrity and recovery release built from the read-only v0.3.6 comprehensive audit. It closes the 29 recorded findings across snapshot safety, concurrency, imports, repository-tree presentation, recovery, collaboration storage, HTTP behavior, and test quality.

## Audit result

- Findings remediated: **29/29**
- Critical: **3/3 closed**
- High: **10/10 closed**
- Medium: **13/13 closed**
- Low: **3/3 closed**

See `AUDIT_CLOSURE.md` and `AUDIT_CLOSURE.json` for one-to-one evidence.

## Principal architectural changes

1. **Cross-process transaction boundary** — OS file locks serialize repository writers and the owner application instance.
2. **Verified recovery** — restore/export/Doctor validate object hashes before use; recovery is staged and transactional.
3. **Staged import engine** — import preview, conflict policy, sensitive-path classification, free-space preflight, progress, cancellation, verification, and rollback are one service.
4. **Persistent operation jobs** — long operations survive UI refreshes and interrupted running jobs are marked explicitly after restart.
5. **Real nested file index** — depth-first parent-child output, incremental hashes, and virtualized browser rendering replace the misleading global-depth list.
6. **Bounded collaboration storage** — terminal quarantine cleanup, storage metrics, and sensitive source defaults.
7. **HTTP hardening** — request timeouts, HEAD support, bounded client maps, and split route handlers.

## Validation

- Python unit/integration tests: **76 passed**
- Python line coverage: **76% overall**
- Native picker module coverage: **87%**
- Chromium workflows passed: owner smoke, deep folder, retry, native import, and real-server/real-disk black box
- Live collaboration Chromium navigation: environment-skipped because managed Chromium blocks localhost; equivalent collaboration HTTP integration tests pass
- Python compilation: PASS
- JavaScript syntax: PASS
- Source manifest verification: PASS
- Clean extracted release startup and `/api/v1/version`: PASS

## Platform acceptance note

The implementation includes Windows PowerShell 7/Windows PowerShell STA picker contracts, Unicode/cancel tests, and a manual acceptance fixture. This Linux build environment cannot physically click the Windows folder dialog. Run `tests/WINDOWS_NATIVE_PICKER_ACCEPTANCE.md` on the release Windows machine before broad distribution.

## Creator and license

Original project and concept by **Rooke Poole**. Copyright © 2026 Rooke Poole. Open source under the MIT License.
