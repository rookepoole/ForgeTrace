# ForgeTrace Architecture — v0.3.3

ForgeTrace is a local-first Python application with a browser interface. Version 0.3.3 preserves the multi-repository registry and disk-backed workspace while making secure collaboration controllable from one normal launch.

## Process structure

```text
server.py
└── forgetrace.app
    ├── CollaborationGatewayManager  runtime start/stop/status for restricted listener
    ├── forgetrace.registry          global SQLite library, migrations, backup/import/doctor
    ├── forgetrace.repository        isolated file/history/snapshot/atomic-merge service
    ├── forgetrace.collaboration     invites, quarantine, pull requests, reviews, conflicts
    ├── forgetrace.web               listener surfaces, APIs, security boundary, static UI
    ├── forgetrace.utils             platform paths and time/path helpers
    ├── forgetrace.errors            structured user-facing errors
    └── forgetrace.constants         application/schema versions and limits
```

No third-party runtime packages are required.

## Listener model

One ForgeTrace process can own two HTTP servers:

| Listener | Default | Bind | Purpose |
|---|---|---|---|
| Owner | Always running | `127.0.0.1:8765` | Repository, registry, review, merge, and sharing controls |
| Contributor | Off until enabled | `0.0.0.0:8766` by default | `contribute.html` and token-scoped collaboration APIs only |

Each `ThreadingHTTPServer` carries a `forgetrace_surface` value: `owner`, `gateway`, or legacy `combined`. The handler applies route policy from this value before relying on client-address checks.

The gateway manager:

1. validates the requested port;
2. constructs a new server with `surface="gateway"`;
3. starts it on a daemon thread;
4. reports detected LAN addresses to the owner UI;
5. shuts it down and joins the thread when requested;
6. shuts it down automatically when the owner process exits.

Runtime sharing state is deliberately not persisted. A fresh launch is local-only until the owner enables sharing again.

## Global application data

| Platform | Default path |
|---|---|
| Windows | `%LOCALAPPDATA%\ForgeTrace` |
| macOS | `~/Library/Application Support/ForgeTrace` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/forgetrace` |

```text
application-data/
├── registry.sqlite3
├── backups/
├── managed-repositories/
│   └── <safe-repository-name>[-N]/
└── collaboration/
    ├── collaboration.sqlite3
    └── quarantine/
        └── <repository-uuid>/<pull-request-uuid>/files/
```


Browser file pickers do not expose an absolute host path. The owner-only managed-repository endpoint therefore creates a fresh ordinary workspace under `managed-repositories/`, initializes normal embedded metadata, and returns its repository UUID. The UI then uses the existing repository-scoped upload API. Folder imports strip one selected root segment and preserve every nested relative segment. Managed workspaces are not a proprietary container; they can be moved and relinked.

The registry stores repository paths and library metadata, not project contents. The collaboration database stores hashed invite capabilities, pull-request metadata, staged-path evidence, and reviews. Raw invitation tokens are not persisted.

## Per-repository data

```text
my-project/
├── normal project files
└── .forgetrace/
    ├── state.json
    ├── state.json.bak
    ├── merge-backups/   temporary rollback data during merges
    └── objects/         SHA-256 snapshot contents
```

`state.json` records contribution events and snapshot manifests. Embedded metadata is protected from normal repository operations and omitted from source-only collaboration archives.

## Owner and contributor trust boundaries

- Owner-listener requests must be loopback and use a local Host value.
- Gateway-listener requests may access only `/`, `/contribute.html`, and `/api/v1/collaboration/...`.
- Gateway restrictions apply even when accessed from `127.0.0.1`.
- Owner actions require matching request origin and local Host checks.
- Contributor changes are written to application-data quarantine, never directly to the repository.
- Merge requires explicit owner approval, matching revision, typed confirmation, conflict-free baseline hashes, and additional risky-file confirmation where applicable.

## Source-download boundary

A source invitation may produce a source-only ZIP. It excludes ForgeTrace metadata, generated history, Git/Mercurial/Subversion/Bazaar metadata, and symlinks. Normal project files remain in scope, so owners must disable source download when the repository contains secrets that should not be shared.

## Merge transaction

1. Ensure a snapshot represents current workspace state.
2. Acquire the repository mutation lock.
3. Recompute the current manifest and compare every affected path with the pull-request baseline.
4. Copy affected existing files into temporary merge-backup storage.
5. Copy staged bytes into sibling temporary files and atomically replace destinations.
6. Apply approved deletions.
7. Record external-contributor attribution.
8. Create a local merge snapshot attributed to the owner.
9. Remove temporary rollback data after success.
10. Restore prior files and metadata if any step fails.

## Isolation and limits

Every repository operation carries a repository UUID. Relative paths are resolved inside one canonical workspace. Collaboration adds repository-scoped token expiry, revocation, use count, per-file limits, total-size limits, maximum change count, general remote request throttling, and a stricter source-archive throttle.

Symlinks are omitted from repository tree/export traversal to prevent archive reads outside the selected workspace.

## Backup, import, and doctor

Registry backups use SQLite's online backup API. Import creates a pre-import backup and merges by UUID/canonical path without moving or deleting project files. Doctor validates SQLite, path state, embedded identity, metadata readability, drift, and discovered repositories under explicit scan roots.

Collaboration-database backup, persistent security-audit export, and quarantine garbage collection remain future work.

## Compatibility boundary

The bundled UI uses `/api/v1/...`. Older unscoped routes remain temporarily and return deprecation headers. The legacy `server.py share` command remains for compatibility, but the supported user flow is the normal owner launch plus UI-controlled gateway.

## v0.3.3 fork and continuity path

The owner application acts as a narrow HTTP client when a user pastes a collaboration link. It sends the fragment token only in `X-ForgeTrace-Invite`, validates the invite context, streams the source ZIP into application-data transfer storage, verifies every ZIP entry, extracts into a newly allocated managed repository, initializes embedded identity, records non-secret upstream provenance, and creates a baseline snapshot.

At startup the registry scans only stable managed roots and bounded known legacy ForgeTrace workspace locations. Embedded UUIDs repopulate missing registrations and may safely relink an offline managed entry when the same UUID is discovered at a new path.

## v0.3.6 complete-folder import boundary

The primary complete-folder workflow does not upload a browser-provided directory tree. A localhost owner action opens the operating-system folder chooser on the ForgeTrace machine. The resulting path is passed only to the owner listener, then `ForgeTraceRepository.import_local_folder` enumerates and copies the source directly.

The contributor gateway cannot call the picker or local-folder import routes. The importer follows no symbolic links, excludes root `.forgetrace` metadata, rejects source/destination containment loops, checks per-file limits, uses temporary files plus atomic replacement, and verifies the resulting repository tree.

## v0.4.0 transaction architecture

Repository mutation now crosses four explicit boundaries: an OS-backed repository lock, a filesystem rollback journal, an atomic repository-state write with revision, and post-commit index invalidation. Imports add a staging/verification layer before entering that boundary. The application-data directory has a separate owner-instance lock.

The browser consumes a depth-first tree with `depth` and `parentPath`, and virtualizes rendered rows. Persistent jobs are application-data records rather than browser-only progress state.

