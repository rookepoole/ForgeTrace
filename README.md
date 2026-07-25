# ForgeTrace — Working Local Repository

**Created by Rooke Poole. Open-source under the MIT License.**

ForgeTrace is a **real local-first repository application**, not a static demo. It stores uploaded and edited files on disk, records contribution history, creates restorable content-addressed snapshots, and exports the complete repository as a ZIP.


## Development roadmap

The authoritative expansion plan is [`BUILD_PLAN.md`](BUILD_PLAN.md). Its first priority is safe multi-repository support: one ForgeTrace instance managing many repository paths across local drives, removable storage, and trusted network locations.

Project governance and safety documents:

- [`CONTRIBUTING.md`](CONTRIBUTING.md)
- [`SECURITY.md`](SECURITY.md)
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
- [`NOTICE.md`](NOTICE.md)
- [`CHANGELOG.md`](CHANGELOG.md)

## Run

### Windows

Double-click `run_local.bat`, then open:

`http://127.0.0.1:8765`

### macOS / Linux

```bash
./run_local.sh
```

No third-party packages are required; only Python 3.

## Working features

- Initialize a repository with a name, description, and owner
- Upload one or many real files
- Upload complete folders while preserving relative paths
- Drag and drop files
- Browse files and folders
- Create files and folders
- Edit and save UTF-8 text/source files
- Download binary or text files
- Rename and delete paths
- Persist repository contents on disk in `workspace/`
- Record every operation in contribution history
- Create deduplicated, restorable repository snapshots
- Restore any snapshot from the UI
- Export current files plus `FORGETRACE_HISTORY.json` as a ZIP
- Block path traversal and protect internal metadata
- Accept files up to 250 MB per request

## Storage model

```text
workspace/
├── your actual repository files
└── .forgetrace/
    ├── state.json       contribution and snapshot metadata
    └── objects/         content-addressed snapshot objects
```

The application never needs GitHub credentials. It is an independent repository workspace. Snapshot objects are SHA-256 addressed and reused across commits when file contents do not change.

## Optional custom workspace

```bash
python server.py --workspace /path/to/project --port 8765
```

This lets ForgeTrace manage an existing folder. ForgeTrace metadata is stored in that folder’s `.forgetrace/` directory.

## Important

Do not open `index.html` directly. The UI depends on the local Python API. Start `server.py`, `run_local.bat`, or `run_local.sh` first.
