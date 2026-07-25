# File and Data Layout

## Package source

- `server.py` — entry point
- `START_FORGETRACE.bat` / `.sh` — one-launch wrappers
- `forgetrace/` — Python application
- `index.html` — owner UI
- `contribute.html` — contributor UI
- `tests/` — regression and acceptance harnesses
- `docs/` — architecture/API/recovery/security documentation

## Platform application data

Resolved by `forgetrace.utils.app_data_dir()` and contains:

- SQLite registry and backups
- `managed-repositories/`
- persistent operation-job history
- collaboration invitations/quarantine/storage metadata
- transfer/staging roots
- application owner-instance lock

## Repository-local metadata

`<repository>/.forgetrace/` contains:

- `state.json` and `state.json.bak`
- content-addressed `objects/`
- transaction/recovery artifacts while active
- file-hash index
- repository lock

Never expose `.forgetrace` through normal repository file APIs or import it from nested source folders.
