# v0.4.0 Implementation Change Map

## New infrastructure

- `forgetrace/locks.py` — cross-platform OS-backed file locking
- `forgetrace/transactions.py` — rollback journals and interrupted-operation recovery
- `forgetrace/importing.py` — staged import preview/verify/commit engine
- `forgetrace/jobs.py` — persistent progress, cancellation, and interrupted-job status
- `forgetrace/policies.py` — protected and sensitive path classification

## Major refactors

- `forgetrace/repository.py` — transactional mutations, verified restore/export, metadata-preserving snapshots, hash index, depth-first tree
- `forgetrace/registry.py` — atomic managed imports, Doctor backup recovery, startup cleanup, UUID-first relinking
- `forgetrace/web.py` — scoped dispatchers, job APIs, sensitive confirmation, HEAD/timeouts/rate-map limits
- `forgetrace/collaboration.py` — sensitive-source default exclusion, quarantine retention, storage metrics
- `forgetrace/app.py` — one-owner-instance lock and robust cleanup
- `forgetrace/native_picker.py` — tested PowerShell 7/Windows PowerShell STA and macOS/Linux adapters
- `index.html` — virtualized true tree, transactional import UX, folder actions, storage-failure fallback

## Added audit tests

- `tests/test_v040_stabilization.py`
- `tests/test_v040_audit_closure.py`
- `tests/browser_blackbox_test.py`
- `tests/WINDOWS_NATIVE_PICKER_ACCEPTANCE.md`
- `tests/windows_native_picker_fixture.ps1`

## Data compatibility

- Application schema: 3
- Repository schema: 2
- Existing managed repository UUIDs remain authoritative
- Hash indexes and job history are additive/rebuildable
- Repository files remain ordinary files and folders
