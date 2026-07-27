# ForgeTrace Build Plan

## v0.5.3.0 completed implementation checkpoint — Switch Preflight and Sealed Capture Planner

- [x] Add a dedicated `GitSwitchService` without extending the accepted `GitWriteService` operation set.
- [x] Expose an internal read model for the attached source branch and bounded existing direct local targets.
- [x] Require a writable registered root worktree, direct born local `HEAD`, one worktree, clean index, and clean tracked bytes.
- [x] Reject native Git locks, active Git administrative state, deletion intents, pending Git-write recovery, sparse/split index, checkout-affecting filters or attributes, symlinks/gitlinks/reparse points, and protected paths.
- [x] Scan the worktree directly so ignored files are included; reject exact, ancestor, file/directory, and conservative Unicode case-fold collisions across source, target, and preserved local paths.
- [x] Enforce 10,000 affected paths, 5,000 preserved files, 512 MiB capture, 8 MiB plan, and 64 MiB free-space-reserve limits.
- [x] Capture exact `HEAD`, index, `logs/HEAD`, affected source tracked bytes, and every bounded untracked/ignored regular file into application-data staging.
- [x] Revalidate all Git and worktree state after capture, install the plan atomically, and seal canonical plan and capture digests.
- [x] Verify plan expiry, repository identity, capture integrity, target/source state, and preserved local bytes without mutating the repository.
- [x] Keep `git switch`, `git checkout`, owner execute API/UI, contributor authority, and repository mutation absent.
- [x] Pass 277 tests across 30 isolated modules: 275 passed on Linux and 2 physical-Windows tests skipped.
- [x] Pass 15 focused planner tests with 82% branch-aware coverage of `forgetrace/git_switch.py`.
- [x] Preserve all accepted locks, journals, deletion intents, read-only enforcement, security history, MIT license, and Rooke Poole creator credit.
- [ ] Next gate: implement and prove the durable switch transaction/recovery journal and deterministic crash checkpoints internally; do not expose execution API/UI yet.

## v0.5.2.2 completed implementation checkpoint — Windows Acceptance Runner Repair

- [x] Reproduce the Windows PowerShell 5.1 `NativeCommandError` mechanism from the operator output without treating the passing unittest line as a ForgeTrace failure.
- [x] Remove direct `2>&1 | Tee-Object` execution from the physical-Windows gate while retaining `$ErrorActionPreference = "Stop"` for actual PowerShell failures.
- [x] Capture native stdout/stderr outside PowerShell's error pipeline and decide success only from the process exit code.
- [x] Preserve environment evidence and write `AUTOMATED_RESULT: OK` only after all automated suites complete.
- [x] Keep the prior runner filename as a compatibility delegate to the repaired gate.
- [x] Add regression tests and update handoff evidence without changing the accepted Git-write surface or inherited trust boundaries.
- [ ] Run `tests/run_v0522_windows_git_write_acceptance.ps1` and the owner-browser checklist on the exact v0.5.2.2 archive on physical Windows.


## v0.5.2.1 completed implementation checkpoint — Windows and Failure-Injection Hardening

- [x] Keep the accepted v0.5.2 operation set unchanged: selected-file stage, staged-tree commit, local branch creation, and lightweight local tag creation only.
- [x] Add sealed, constructor-injected crash checkpoints around capture finalization, index installation, object creation, ref/reflog installation, rollback finalization, terminal journal finalization, and receipt creation.
- [x] Prove fresh-process startup recovery restores exact index, `HEAD`, refs, and reflogs after injected interruption.
- [x] Reconstruct a missing receipt from a verified terminal journal without rolling back an already committed operation.
- [x] Defer recovery while native Git locks or administrative state are present and expose an actionable owner diagnostic.
- [x] Treat Windows sharing failures during non-critical preview, receipt-retention, and terminal-journal cleanup as maintenance evidence rather than a false write failure.
- [x] Add bounded retry for critical atomic replace/unlink operations and preserve primary exceptions from temporary-file cleanup.
- [x] Expose checkpoint, journal/receipt integrity, recovery disposition, exact next step, deferred cleanup, and manual-inspection evidence in owner status, Health, and the Git UI.
- [x] Scope maintenance warnings to the originating repository and de-duplicate unreadable global journals in Health findings.
- [x] Add an exact physical-Windows acceptance runner and checklist without claiming platform acceptance from Linux evidence.
- [x] The original v0.5.2.1 runner was attempted on physical Windows and exposed the PowerShell 5.1 false `NativeCommandError`; the gate is superseded by the v0.5.2.2 runner above.
- [x] Preserve all deletion intents, locks, journals, snapshot verification, staged imports, read-only enforcement, immutable review/conflict evidence, segmented security history, MIT license, and Rooke Poole creator credit.


## v0.5.2 completed checkpoint — Transactional Local Git Writes

- [x] Record the v0.5.1.2 physical Windows deletion transaction as operator-reported unskipped `OK` on 2026-07-26.
- [x] Keep the Windows deletion intent, external guard, transaction/recovery journal, tombstone, managed-path restriction, and Security-viewer resilience unchanged.
- [x] Add a separate owner-only `GitWriteService`; do not add mutation methods to the read-only Git intelligence authority.
- [x] Support only selected-file staging, staged-tree commits, local branch creation, and lightweight local tag creation.
- [x] Bind every write to an expiring, canonical-digest preview and an exact typed confirmation.
- [x] Acquire the normal repository lock before the repository-scoped application-data Git-write lock.
- [x] Fail closed for read-only repositories, active deletion intents, native Git locks, merge/rebase/cherry-pick/revert/bisect state, unsupported Git layouts, stale previews, and damaged evidence.
- [x] Capture exact index/ref/reflog/HEAD evidence as required by the operation and roll back from a durable hash-sealed transaction journal.
- [x] Recover incomplete transactions at startup without bypassing native Git operations; reconstruct missing terminal receipts before journal cleanup.
- [x] Use Git plumbing for commits with explicit author/committer identity and no shell, hooks, editor, signing, credential helpers, global/system configuration, network protocols, submodule recursion, or external filters.
- [x] Add owner API/UI controls, verified receipts, security-ledger evidence, and Health reporting while keeping read-only Git inspection independently available.
- [x] Pass 238 Python tests in fresh module processes: 236 passed on Linux and 2 physical Windows-only tests skipped.
- [x] Pass all 19 applicable Chromium workflows; retain the collaboration navigation script's documented managed-Chromium localhost-policy skip.
- [x] Pass 16 focused transactional Git-write tests with 78% branch-aware coverage of `forgetrace/git_writes.py`.
- [x] Preserve the MIT license and Rooke Poole creator credit.

## v0.5.1.2 accepted Windows gate

The exact physical test below was reported by the operator as an unskipped `OK` on Windows on 2026-07-26:

`tests.test_v0512_windows_delete_and_security_fetch.WindowsDeletionIntentRegressionTest.test_physical_windows_external_intent_delete_transaction`

This records the prerequisite acceptance supplied by the Windows operator. The Linux validation environment still skips both physical Windows-only tests and does not claim to have reproduced Win32 behavior itself.

# ForgeTrace Expansive Build Plan

**Project:** ForgeTrace
**Creator and project lead:** Rooke Poole
**License:** MIT
**Document status:** Authoritative development roadmap
**Baseline:** Transactional Local Git Writes v0.5.2
**Last implementation update:** 2026-07-26
**Target:** A polished, local-first alternative to cloud repository platforms

---

## 1. Product vision

ForgeTrace should become a dependable local repository environment that combines the parts people use most from GitHub, GitLab, desktop Git clients, file managers, project trackers, and code review tools—without requiring a cloud account or surrendering ownership of project data.

The product should remain useful in three progressively larger modes:

1. **Solo local mode** — one person manages many repositories on one machine.
2. **Trusted LAN mode** — a small team collaborates over a local network.
3. **Optional remote mode** — selected repositories replicate to user-controlled storage or standard Git remotes.

ForgeTrace is not intended to trap projects inside a proprietary format. A user must always be able to open the original files directly, use standard Git tools, export history, move repositories, and uninstall ForgeTrace without losing the project.

### North-star promise

> ForgeTrace gives creators a complete, explainable, portable history of how their projects evolved—across files, commits, reviews, issues, tests, documentation, and collaboration—while keeping the repository under their control.

### Differentiator

Standard contribution systems mostly count commits. ForgeTrace should preserve a broader evidence graph:

- who proposed a change;
- who implemented it;
- who reviewed it;
- which files and issues were affected;
- what tests verified it;
- which release included it;
- what later work depended on it;
- and how credit should be attributed.

This is the **Contribution Lineage** model. It must be useful, inspectable, and evidence-backed rather than a decorative score.

---

## 2. Product principles

Every architectural and product decision should be checked against these principles.

### 2.1 Local-first

- The primary copy of every repository remains on the user’s machine.
- Core functionality works without internet access.
- Cloud services are optional adapters, never hard dependencies.
- The user can inspect and back up all project data.

### 2.2 Open formats

- Repository files remain normal files and folders.
- Existing Git repositories remain valid Git repositories.
- Metadata uses documented SQLite/JSON formats.
- Exports contain human-readable manifests.
- No feature may make project recovery depend on ForgeTrace being installed.

### 2.3 Multiple repositories are foundational

- ForgeTrace manages a library of repositories, not one hard-coded workspace.
- Repositories may live on different drives and network paths.
- Each repository has isolated metadata, permissions, history, and settings.
- The UI always makes the active repository obvious.

### 2.4 Evidence before scoring

- Impact estimates must show their inputs.
- Contribution credit must link to concrete events.
- Automated classifications must be editable and auditable.
- The product must distinguish facts, calculations, and inferred signals.

### 2.5 Safe by default

- Destructive operations require clear confirmation and recovery paths.
- Network access is disabled unless explicitly enabled.
- Repository paths are sandboxed and validated.
- Secrets and private files are not indexed or exported accidentally.

### 2.6 Fast for real projects

- Large repositories must remain navigable.
- Background indexing must be incremental and cancellable.
- The UI must render progressively rather than waiting for full scans.
- Expensive operations must expose progress and diagnostics.

### 2.7 Incremental delivery

- Every phase ends with a working release.
- New systems replace old systems through explicit migrations.
- No phase should require a complete rewrite before users receive value.

---

## v0.3.4 implementation checkpoint — Comprehensive recursive folder import

**Status:** Complete and validated

This release closes the deep-folder import failure without changing repository identity, storage, collaboration, or upgrade behavior.

- [x] Add a File System Access API directory walker that recursively enumerates every file at arbitrary nesting depth.
- [x] Keep the `webkitdirectory` browser input as a compatibility fallback and preserve every supplied `webkitRelativePath`.
- [x] Use the same recursive import engine when adding a folder to an existing repository and when creating a new repository from a selected folder.
- [x] Preserve the selected root folder for uploads into an existing repository.
- [x] Strip only the selected outer root when the folder creates a new managed repository.
- [x] Preserve empty leaf folders when the modern directory picker exposes them.
- [x] Keep uploads disk-backed and repository-scoped, with per-file progress and partial-failure reporting.
- [x] Verify folder expansion through every nested level in the real interface.
- [x] Add a live API fixture with six files across up to six subfolder levels.
- [x] Add Chromium coverage for recursive existing-repository upload and recursive new-repository onboarding.
- [x] Run the complete regression suite: 39 unit/integration tests, JavaScript/Python validation, core Chromium smoke testing, and recursive-folder Chromium testing.

---

## v0.3.3 implementation checkpoint — Team onboarding and upgrade continuity

**Status:** Complete and validated

This release closes four usability failures discovered during real team onboarding without weakening the quarantined collaboration boundary.

- [x] Add **Fork shared link** to the empty state and Add Repository dialog; it works before any local repository exists.
- [x] Validate the fragment token through the restricted contributor gateway and stream a source-only ZIP into a managed local fork.
- [x] Reject cross-origin redirects, unsafe ZIP paths, symlinks, protected VCS/ForgeTrace metadata, encrypted entries, excessive file counts, and archive expansion beyond the safety ceiling.
- [x] Keep raw invitation tokens out of the registry and repository metadata; store only non-secret upstream provenance and a short token fingerprint.
- [x] Raise repository uploads to 1 GB, default invite files to 100 MB, default pull requests to 1 GB, maximum pull requests to 4 GB, and streamed source/fork archives to 2 GB.
- [x] Stream repository uploads, pull-request uploads, raw repository downloads, repository exports, source downloads, and fork downloads through temporary application-data files instead of loading complete transfers into memory.
- [x] Replace the always-expanded flat path display with an expandable/collapsible repository tree whose state persists per repository.
- [x] Scan the stable managed-repository root on startup and repopulate missing registry entries by embedded UUID.
- [x] Automatically relink an offline managed repository when startup discovery finds the same UUID at a moved path.
- [x] Inspect only bounded, known legacy ForgeTrace workspace locations rather than recursively scanning a user home or Downloads directory.
- [x] Add direct-service, live owner-API, continuity, security-surface, JavaScript, and Chromium coverage.
- [x] Run the complete regression suite: 37 unit/integration tests plus Chromium file-tree/onboarding flow.

---

## v0.3.2 implementation checkpoint — Repository onboarding usability

**Status:** Complete and validated

This patch is intentionally narrow and regression-sensitive. It improves the new-repository workflow without replacing the proven absolute-path registration flow.

- [x] Add a dedicated **Upload files** option to the Add Repository dialog.
- [x] Add a separate **Upload folder** option that preserves relative folder paths beneath the selected root.
- [x] Keep explicit absolute-path creation and existing-folder registration.
- [x] Create uploaded repositories as normal on-disk workspaces in a documented ForgeTrace-managed folder.
- [x] Make partial upload failures visible without hiding successfully imported files.
- [x] Add API, registry, static, and Chromium coverage for all three onboarding routes.
- [x] Run the complete existing repository and collaboration regression suite: 27 tests plus static and Chromium flows.

---

## 3. Current baseline

ForgeTrace v0.4.3 now provides:

- a Python local server with no third-party runtime dependencies;
- a dedicated application-data security-event ledger with immutable rows, monotonic sequence, canonical SHA-256 chaining, redaction, verification, owner filtering/export, and contributor denial;
- fail-closed audit gates for gateway start, invitation creation, sensitive exports, pull-request merge, registry restore/rollback, and repository access-mode transitions;
- validated registry backup preview with explicit additive Merge and exact Replace semantics;
- a registry-wide OS-backed operation lock covering normal connections, backup creation/retention, preview, restore, rollback, and startup recovery;
- staged older-schema migration, SQLite integrity/foreign-key/table verification, deterministic logical digests, and preview IDs bound to backup SHA-256 plus live/prepared state;
- exact pre-restore backups, durable recovery journals, automatic rollback after failed installation, conservative startup recovery, and explicit rollback that refuses to erase later registry work;
- owner-visible restore/rollback history and consequence preview; recovery never replaces repository content/history or the separate security ledger, and reconciles only online embedded access-mode metadata;
- service-enforced repository `read_write` / `read_only` authority stored in registry schema 4 and repository schema 3;
- fail-closed effective mode that permits writes only when both persisted copies explicitly agree on read-write;
- mode validation under the repository OS lock across file/folder changes, imports/jobs, snapshots/restores, embedded settings, object materialization, managed discard, and pull-request merge;
- safe read-only browsing, verification, export, contribution submission, and quarantine review without repository cache/object side effects;
- owner-visible read-only banner, lock labels, read-only editor, disabled mutation controls, and ledger-authorized owner mode switch;
- persistent operation jobs with progress, cancellation, and interrupted-job recovery;
- OS-backed cross-process repository locks, an owner-instance lock, and filesystem transaction journals;
- verified snapshot restore/export and Doctor recovery before live workspace mutation;
- a platform-specific global application-data directory;
- a persistent SQLite repository registry and migration framework;
- stable UUID identity for every repository;
- one running process managing many repository paths;
- repository creation and existing-folder registration;
- path-free repository creation from individual files or a recursively enumerated selected folder;
- empty-state and Add Repository onboarding from a secure ForgeTrace collaboration link;
- streamed, safety-validated source import into a normal managed local fork;
- expandable/collapsible nested repository folders with per-repository expansion persistence;
- startup recovery that repopulates managed repositories from embedded UUID metadata after registry loss or package replacement;
- streamed large-file upload, pull-request upload, export, source download, and fork-transfer paths;
- a documented managed-repository root containing ordinary movable local folders;
- recursive descendant discovery at arbitrary folder depth, with outer-folder stripping only during new-repository onboarding;
- repository switching, favorites, and recent-order tracking;
- offline-path detection and UUID-verified relinking;
- non-destructive unregister that leaves all project files and history intact;
- repository-scoped v1 APIs for file, snapshot, restore, export, and metadata operations;
- file and folder upload, browsing, editing, rename, and deletion;
- automatic contribution events;
- SHA-256 content-addressed snapshot objects;
- snapshot creation and restoration;
- ZIP export with portable history;
- atomic state writes with a parseable backup copy;
- shared in-process repository mutation locks;
- path traversal and protected-metadata defenses;
- a responsive multi-repository browser interface;
- automated two-repository isolation, 100-repository registry, recovery, security, API, and Chromium UI tests;
- normalized repository tags and many-to-many collections;
- searchable repository-library filters and persistent saved filters;
- repository settings synchronized into embedded metadata;
- repository-scoped upload limits enforced before request bodies are read;
- path capability reporting for availability, directory type, read/write access, free space, and UNC/network-path classification;
- online SQLite registry backups with retention;
- portable registry JSON export and non-destructive merge import;
- `server.py doctor` plus browser doctor controls for integrity, identity, path, metadata-drift, and unregistered-repository checks;
- safe doctor repair actions that create a pre-repair registry backup;
- migration `0002_registry_organization_and_limits` with tested v0.2.0 upgrade behavior;
- formal deprecation headers on temporary unscoped compatibility routes;
- a secure external contribution gateway with repository-scoped, expiring invitation tokens;
- source-only repository ZIP downloads that exclude ForgeTrace history, registry data, and machine paths;
- quarantined pull-request drafts stored outside the live repository workspace;
- explicit changed-file uploads and requested deletions with protected-path and size enforcement;
- owner-side exact diffs, binary hashes, risky-file warnings, approvals, change requests, and comments;
- conflict detection against a captured baseline plus revision checks at merge time;
- atomic local merges with a safety snapshot and rollback backup;
- localhost-only owner APIs and merge actions even when the contribution gateway is network-bound;
- remote blocking for registry, repository browsing, file editing, snapshots, exports, and settings APIs;
- token hashing, request throttling, security headers, active-content download protection, and no server-side code execution;
- a single normal launcher with UI-controlled start/stop of a second restricted contributor listener;
- listener-level contributor isolation that denies owner APIs even when the contributor port is accessed from loopback;
- automatic LAN-address detection and in-UI token-link generation, with an optional advanced private-VPN/tunnel URL override;
- deprecated `server.py share` compatibility retained without separate user-facing share launchers.

### Current limitations

- repository contribution/snapshot metadata remains JSON rather than per-repository SQLite;
- external metadata mode is reserved but not implemented;
- there is no filesystem watcher;
- there is no branch model beyond snapshots;
- no Git integration, diff engine, staging area, or remote support;
- no repository-wide or cross-repository search index;
- no issue tracker, release manager, or project boards;
- pull requests are ForgeTrace snapshot-native change sets rather than Git branches or hosted Git protocol;
- there are no persistent user accounts, roles, or authenticated full-workspace LAN sessions;
- no plugin architecture;
- no packaged desktop application or automatic updates;
- security-event retention/rotation, visual conflict resolution, and a unified health dashboard are not yet implemented.

The next phases should evolve this tested baseline instead of discarding it.

## 4. Target user groups

### 4.1 Independent creator

Needs:

- several projects in different folders;
- fast switching between them;
- snapshots before risky edits;
- clear activity history;
- local issues and milestones;
- simple backup and export;
- no account requirement.

### 4.2 Open-source maintainer

Needs:

- real Git compatibility;
- branch and diff workflows;
- contributor attribution;
- review queues;
- changelog and release creation;
- standard repository files;
- exportable project metrics.

### 4.3 Small studio or research team

Needs:

- repositories on a shared machine or LAN server;
- user identities and roles;
- file locking or conflict warnings;
- discussions, issues, and decisions;
- audit history;
- portable backups;
- private operation without a public cloud.

### 4.4 Educator or student

Needs:

- transparent project evolution;
- contribution evidence beyond commits;
- easy restoration to previous states;
- explainable feedback;
- assignment/project templates;
- simple installation.

### 4.5 Archive and preservation user

Needs:

- content-addressed integrity;
- immutable snapshots;
- checksums and verification reports;
- offline browsing;
- documented formats;
- migration and recovery tools.

---

## 5. Product boundaries

### Build directly

- repository library and path management;
- file operations and local editing;
- snapshots and restore;
- Git command orchestration where Git is available;
- search and indexing;
- local issues, reviews, releases, and project planning;
- contribution lineage and evidence;
- LAN collaboration;
- backup, export, and replication adapters;
- a local API and plugin system;
- desktop packaging.

### Reuse established tools

- use the installed `git` executable for Git compatibility rather than reimplementing the Git object model immediately;
- use SQLite for structured metadata and full-text search;
- use OS keychains for stored secrets;
- use platform-native file watchers where possible;
- use mature diff algorithms/libraries when dependencies are introduced;
- use standard SSH/HTTPS Git transports for remotes.

### Explicit non-goals for the near term

- replacing GitHub’s global public network;
- hosting arbitrary untrusted code execution by default;
- becoming a full IDE before repository workflows are reliable;
- inventing a proprietary version-control protocol;
- silently syncing private repositories to third parties;
- blockchain-based contribution credit;
- irreversible reputation scores.

---

## 6. Target architecture

ForgeTrace should evolve into a layered application.

```text
Desktop shell or browser
        │
        ▼
Local ForgeTrace API
        │
        ├── Repository Registry
        ├── Workspace Service
        ├── File Service
        ├── Snapshot Service
        ├── Git Adapter
        ├── Search/Index Service
        ├── Issue/Review Service
        ├── Contribution Lineage Service
        ├── Backup/Replication Service
        └── Authentication/Network Service
        │
        ▼
SQLite metadata + repository files + object stores
```

### 6.1 Process model

Recommended long-term process model:

- one ForgeTrace daemon/service per user account;
- one local API endpoint;
- one global application database;
- zero or more registered repositories;
- per-repository metadata directories;
- worker jobs for indexing, hashing, export, and replication;
- a desktop shell that starts/stops or connects to the daemon.

### 6.2 Technology progression

**Near term:** Keep Python and the browser UI to move quickly.
**Middle term:** Add a small dependency set, SQLite, typed models, and a framework with safer routing.
**Long term:** Package the UI and local API as a desktop application while preserving headless/server mode.

A practical path is:

1. Python 3.12+ backend.
2. SQLite database.
3. FastAPI or equivalent typed local API after the standard-library prototype is stabilized.
4. TypeScript front end with a small component system.
5. Tauri or another lightweight desktop shell.
6. Optional standalone server binary/package for LAN use.

The application should not move to a framework merely for appearance. Each migration must reduce a concrete reliability, security, maintainability, or packaging problem.

---

## 7. Multi-repository foundation

This is the highest-priority expansion.

### 7.1 Repository registry

Create a global registry stored outside managed repositories.

Suggested locations:

```text
Windows: %APPDATA%/ForgeTrace/forgetrace.db
macOS:   ~/Library/Application Support/ForgeTrace/forgetrace.db
Linux:   ~/.local/share/forgetrace/forgetrace.db
Portable mode: ./forgetrace-data/forgetrace.db
```

Each registered repository record should contain:

- stable repository UUID;
- display name;
- canonical absolute path;
- normalized path for duplicate detection;
- description;
- owner/default identity;
- repository type: snapshot-only, Git, or hybrid;
- metadata mode: embedded or external;
- favorite/pinned state;
- tags and collection memberships;
- creation date;
- last opened date;
- last successful scan date;
- missing/offline state;
- drive or network volume identity where available;
- default branch or active snapshot line;
- repository icon/accent metadata;
- security and indexing settings.

### 7.2 Repository path support

ForgeTrace must support:

- local folders;
- folders on other internal drives;
- removable drives;
- UNC paths on Windows;
- mounted SMB/NFS paths;
- read-only repository paths;
- symlinked paths with explicit policy;
- repositories whose path is temporarily unavailable;
- portable repositories moved between machines.

### 7.3 Add repository workflows

Provide six primary actions:

1. **Upload files** — implemented in v0.3.2; create a managed local repository from one or more selected files.
2. **Upload folder** — implemented in v0.3.2; create a managed local repository while preserving nested paths beneath the selected root.
3. **Create repository at path** — implemented; create a new folder and initialize ForgeTrace metadata.
4. **Add existing folder by path** — implemented; register a normal folder without moving it.
5. **Add existing Git repository** — detect `.git`, preserve it, and enable Git features.
6. **Import archive** — extract a ZIP/TAR into a selected destination after preview and safety checks.

Later add:

7. **Clone Git remote** — clone with standard Git and register automatically.
8. **Discover repositories** — scan selected roots for `.git` or `.forgetrace` markers.

### 7.4 Repository switcher

The UI must include:

- active repository name and full path;
- quick switcher with fuzzy search;
- favorites and recent repositories;
- collections/workspaces;
- missing-path warnings;
- open-in-file-manager action;
- copy-path action;
- close/unregister action that never deletes files;
- explicit delete-repository workflow separated from unregister.

### 7.5 Collections

Allow repositories to be grouped without moving them:

- studio projects;
- research projects;
- archived projects;
- client work;
- experiments;
- custom collections.

A repository can belong to multiple collections.

### 7.6 Embedded vs external metadata

Support two metadata modes.

**Embedded mode**

```text
project/
├── project files
└── .forgetrace/
```

Benefits: portable with the project.
Risks: metadata appears in backups or Git unless ignored.

**External mode**

```text
ForgeTrace data root/
└── repositories/<repo-uuid>/
```

Benefits: repository remains untouched.
Risks: metadata must be relinked if the project moves.

The registry must record the mode. Export should be able to create a portable package regardless of mode.

### 7.7 Multi-repository acceptance criteria

- [ ] Register at least 100 repositories without noticeable switcher lag.
- [ ] Repositories on two different drives work in the same session.
- [ ] A missing drive marks repositories offline without deleting registry entries.
- [ ] Reconnecting the drive restores access automatically or through relink.
- [ ] Duplicate path registration is prevented.
- [ ] Unregister never deletes repository contents.
- [ ] Delete requires an explicit destructive confirmation separate from unregister.
- [ ] The active repository is visible on every screen.
- [ ] Repository-specific activity never leaks into another repository.
- [ ] Export and restore operate against the explicitly selected repository.

---

## 8. Storage and metadata redesign

### 8.1 Move from monolithic JSON to SQLite

JSON can remain an export format, but operational metadata should move to SQLite for:

- atomic transactions;
- concurrent readers;
- migrations;
- indexed queries;
- full-text search;
- referential integrity;
- robust recovery after interruption.

### 8.2 Proposed global database tables

```text
repositories
repository_paths
collections
collection_members
identities
settings
jobs
notifications
schema_migrations
```

### 8.3 Proposed per-repository tables

```text
repo_state
file_entries
file_versions
snapshot_sets
snapshots
snapshot_entries
objects
contributions
contribution_edges
issues
issue_events
labels
issue_labels
milestones
reviews
review_comments
releases
release_assets
tests
decisions
work_sessions
repo_settings
repo_migrations
```

### 8.4 Stable identifiers

- Every repository receives a UUID.
- Every contribution receives a UUID.
- Every issue, review, snapshot, and release receives a stable internal ID.
- Human-facing numbers such as `#42` are repository-scoped counters.
- Paths are properties, not identifiers, because files can move.

### 8.5 Transaction rules

A mutating repository operation should:

1. validate repository and path;
2. write to a temporary location where applicable;
3. fsync or safely replace the destination;
4. update metadata in a transaction;
5. append a contribution event;
6. enqueue indexing/hash work;
7. commit the transaction;
8. emit a UI event.

If any step fails, the application must either roll back or present a recoverable partial-operation record.

### 8.6 Schema migration policy

- Use monotonically increasing migration IDs.
- Back up databases before destructive migrations.
- Never mutate repository files during an application metadata migration unless the migration explicitly declares it.
- Record migration start, completion, and failure.
- Provide a `forgetrace doctor` command to inspect and repair metadata.

---

## 9. File workspace and editor

### 9.1 File browser

The file browser should support:

- virtualized large-directory rendering;
- list and tree views;
- breadcrumbs;
- sortable columns;
- file type and status icons;
- hidden-file toggle;
- ignored-file toggle;
- quick preview;
- bulk selection;
- drag-and-drop move;
- copy, move, rename, delete, and duplicate;
- new file from template;
- new folder;
- reveal in OS file manager;
- copy relative and absolute path;
- file history and contribution lineage entry points.

### 9.2 Upload and import

- chunk large uploads;
- show per-file and aggregate progress;
- support cancellation;
- surface conflicts before overwrite;
- provide replace, skip, keep both, and apply-to-all choices;
- reject path traversal and unsafe archive paths;
- preserve timestamps only when the user requests it;
- optionally calculate checksums during upload;
- identify likely secrets before committing/exporting.

### 9.3 Text editor

The built-in editor should remain intentionally focused:

- syntax highlighting;
- line numbers;
- search and replace;
- undo/redo;
- tabs or a small working set;
- encoding and line-ending detection;
- save conflict detection;
- dirty-state protection;
- diff against last snapshot/commit;
- optional format command through plugins;
- large-file cutoff with external-editor fallback.

ForgeTrace should integrate with external editors through “Open in…” commands rather than trying to replace full IDEs immediately.

### 9.4 Binary preview

Support safe previews for:

- images;
- audio metadata/playback;
- video metadata/playback where browser codecs permit;
- PDFs;
- common archives;
- font metadata without redistributing files;
- structured data such as JSON and CSV;
- hexadecimal preview for unknown binaries.

### 9.5 File operation recovery

- move deleted files to a repository-local recovery area before permanent removal;
- maintain a trash manifest;
- allow restore to original or alternate path;
- expire trash by configurable age/size policy;
- never purge during an active snapshot/export operation;
- surface failures when OS locks or permissions prevent changes.

---

## 10. Snapshot and version-history engine

### 10.1 Preserve snapshot-only mode

ForgeTrace must remain useful without Git. Snapshot-only repositories should support:

- named snapshots;
- descriptions;
- tags;
- branching or lines of work;
- comparison between snapshots;
- restoration of all files or selected paths;
- immutable snapshot manifests;
- object verification;
- export and import.

### 10.2 Object store

- SHA-256 content-addressed objects;
- deduplication within a repository initially;
- optional global deduplication later;
- compression for eligible object types;
- reference counting or mark-and-sweep garbage collection;
- corruption verification;
- quarantine of invalid objects;
- configurable retention.

### 10.3 Snapshot manifests

Each snapshot should record:

- snapshot ID;
- repository ID;
- parent snapshot ID(s);
- author identity;
- message and optional extended description;
- UTC timestamp and local timezone offset;
- complete path manifest;
- file hashes, sizes, modes, and relevant timestamps;
- ignored/excluded path policy;
- application version and schema version;
- associated issue/review/release IDs;
- optional cryptographic signature later.

### 10.4 Restore safety

Before restore:

- detect unsaved editor changes;
- compare current workspace with target;
- show created, overwritten, and deleted paths;
- create an automatic safety snapshot unless disabled;
- check free disk space;
- block protected-path writes;
- write a restore journal.

After restore:

- verify file hashes;
- preserve the previous state as recoverable;
- log the operation as a contribution event;
- refresh search and Git status.

### 10.5 History visualization

Provide:

- chronological timeline;
- branch/line graph;
- snapshot detail view;
- per-file history;
- compare any two points;
- restore selected files;
- label/tag management;
- playback of repository evolution.

---

## 11. Git interoperability

Git compatibility is essential for becoming a credible local alternative.

### 11.1 Git detection

For each repository:

- detect whether Git is installed;
- detect `.git` directory or worktree metadata;
- identify repository root;
- identify active branch and detached HEAD;
- identify configured remotes;
- identify submodules and worktrees;
- show Git version and capability warnings.

### 11.2 Initial Git feature set

- status summary;
- staged and unstaged changes;
- text diff;
- stage/unstage selected files or hunks;
- commit with author and message;
- branch list/create/switch/delete;
- tag list/create;
- log and commit details;
- restore/discard with safety confirmation;
- remote list;
- fetch, pull, and push through standard Git;
- credential delegation to Git credential helpers.

### 11.3 Hybrid history

ForgeTrace should not duplicate Git blobs unnecessarily by default. In hybrid mode:

- Git remains authoritative for Git commits;
- ForgeTrace records contribution, issue, review, test, and decision metadata around commits;
- optional safety snapshots capture uncommitted work;
- a contribution can link to one or more Git commit hashes;
- ForgeTrace imports existing Git author history without pretending it created those events.

### 11.4 Git safety rules

- never force-push by default;
- never rewrite public history without an expert warning;
- preview merge/rebase consequences;
- preserve reflog-based recovery guidance;
- do not store raw credentials in ForgeTrace databases;
- redact credentials from command output;
- support dry-run where Git provides it;
- serialize conflicting Git operations per repository.

### 11.5 Merge conflict workflow

- identify conflicted files;
- provide base/ours/theirs views;
- allow external merge tool launch;
- track resolution decisions;
- run configured tests before finalizing;
- link resolution work to contribution lineage.

### 11.6 Git acceptance criteria

- [ ] Opening an existing Git repository does not modify it until the user performs an action.
- [ ] Status matches `git status --porcelain=v2` for the tested fixtures.
- [ ] Commits created through ForgeTrace are readable by standard Git clients.
- [ ] Commits created externally appear after refresh/file-watch events.
- [ ] Branch switching handles dirty worktrees safely.
- [ ] Credential material never appears in logs or exports.
- [ ] Submodules and worktrees are detected and never silently flattened.

---

## 12. Contribution Lineage system

### 12.1 Contribution event model

Every event should have:

- ID;
- repository ID;
- actor identity;
- event type;
- timestamp;
- source: ForgeTrace, Git import, API, plugin, or manual entry;
- evidence references;
- affected files;
- associated issues, reviews, tests, snapshots, commits, and releases;
- parent and child relationships;
- confidence level for inferred links;
- editable credit allocations;
- visibility and redaction state.

### 12.2 Event types

At minimum:

- idea/proposal;
- issue report;
- reproduction;
- design/decision;
- file creation;
- implementation;
- refactor;
- documentation;
- test creation;
- test execution;
- review;
- review response;
- merge/conflict resolution;
- release work;
- support/mentoring;
- repository administration;
- snapshot/restore;
- imported Git commit.

### 12.3 Evidence model

Evidence may include:

- file diff;
- commit hash;
- snapshot ID;
- issue event;
- review comment;
- test result;
- decision record;
- linked artifact;
- external URL stored as a reference;
- manual note clearly marked as manual.

### 12.4 Lineage relationships

Support explicit edge types:

- caused;
- implements;
- verifies;
- reviews;
- documents;
- blocks;
- unblocks;
- supersedes;
- depends on;
- included in;
- reverts;
- inspired by;
- duplicates.

### 12.5 Impact estimates

Impact must be an explainable estimate, not a universal score. Suggested dimensions:

- direct project effect;
- downstream work unlocked;
- defect/risk reduction;
- verification strength;
- documentation/reuse value;
- collaboration breadth;
- release criticality.

Rules:

- show the calculation inputs;
- let users disable scores;
- avoid ranking people by one number;
- distinguish recorded evidence from inferred relationships;
- allow correction and annotation;
- retain an audit trail of credit edits;
- avoid punitive “low impact” labels.

### 12.6 Lineage UI

- interactive graph;
- timeline mode;
- file-centered mode;
- issue-centered mode;
- release-centered mode;
- filters by contributor, type, date, confidence, label, and workstream;
- upstream/downstream highlighting;
- chronological playback;
- printable/shareable contribution receipts;
- export to JSON, CSV, and SVG/PNG where appropriate.

---

## 13. Repository-wide search and indexing

### 13.1 Search scope

Search across:

- file names and paths;
- text file contents;
- symbols where parsers are available;
- commits and snapshots;
- contribution events;
- issues and comments;
- reviews;
- releases;
- decisions;
- tags and labels.

### 13.2 Index architecture

Use SQLite FTS5 initially for metadata and text content. Store:

- repository ID;
- path;
- content hash;
- modification fingerprint;
- indexed excerpt/content;
- language/type;
- indexing status;
- exclusion reason.

### 13.3 Index policy

- respect `.gitignore`, `.ignore`, and ForgeTrace-specific ignore rules;
- default-exclude `.git`, `.forgetrace`, dependency caches, build outputs, and obvious secret stores;
- set configurable file-size and binary limits;
- incremental reindex through file watchers;
- support manual rebuild;
- expose index health and last updated time;
- pause on battery or high system load if configured.

### 13.4 Search experience

- fuzzy repository switcher;
- global search across registered repositories;
- current-repository search;
- exact phrase and regex modes;
- filters for path, extension, contributor, date, event type, and status;
- keyboard navigation;
- result previews with highlighted matches;
- open result at line/event;
- saved searches.

---

## 14. Issues, planning, and decisions

### 14.1 Local issue tracker

Each repository should have:

- numbered issues;
- title and Markdown description;
- status;
- author and assignees;
- labels;
- milestones;
- priority and severity;
- due date;
- comments/events;
- linked files, commits, snapshots, reviews, and releases;
- dependencies and duplicates;
- templates.

### 14.2 Project boards

Provide local boards with:

- table, kanban, and roadmap views;
- custom fields;
- saved filters;
- drag-and-drop status changes;
- milestones and target dates;
- repository or cross-repository boards;
- export to CSV/JSON.

### 14.3 Decision records

Add lightweight Architecture/Project Decision Records:

- context;
- decision;
- alternatives;
- consequences;
- status: proposed, accepted, superseded, rejected;
- linked evidence;
- author and reviewers;
- optional generation of Markdown ADR files in the repository.

### 14.4 Discussions and notes

Provide repository-local discussions for:

- ideas;
- questions;
- announcements;
- retrospectives;
- release planning.

These must be exportable and optionally stored as Markdown so they are not trapped in the database.

---

## 15. Review workflow

### 15.1 Change sets

A change set is a reviewable unit linked to:

- a Git branch/commit range;
- a snapshot comparison;
- or an explicitly selected file set.

### 15.2 Review features

- summary and intent;
- changed file list;
- unified and side-by-side diffs;
- inline comments;
- threaded replies;
- approve, request changes, or comment;
- checklist templates;
- test evidence;
- required reviewers in team mode;
- resolution state;
- final merge/apply record.

### 15.3 Local solo review

Solo users should be able to create a self-review checkpoint before merging or releasing. The product should frame this as a quality tool, not fake collaboration.

### 15.4 Review lineage

Review comments and resolutions become contribution events. Credit should distinguish:

- defect discovery;
- design suggestion;
- verification;
- implementation response;
- final approval.

---

## 16. Releases and artifacts

### 16.1 Release model

A release should contain:

- version/tag;
- title;
- notes;
- source snapshot or Git commit/tag;
- included issues and contributions;
- build/test evidence;
- attached assets;
- checksums;
- publication state;
- signing information later.

### 16.2 Release automation

- generate draft notes from lineage and issues;
- allow curation before publication;
- generate SHA-256 manifests;
- produce source archives;
- invoke user-defined build scripts in an explicitly trusted mode;
- retain logs;
- export a release bundle.

### 16.3 Artifact library

Repository artifacts may include:

- binaries;
- installers;
- reports;
- model weights;
- datasets;
- documentation packages;
- screenshots.

Large artifacts should support external storage pointers to avoid bloating repository metadata or Git history.

---

## 17. Local collaboration and LAN mode

### 17.1 Network modes

- **Local-only:** bind to loopback; no login required by default.
- **Scoped contribution gateway:** bind a deliberately restricted surface for expiring pull-request invitations while owner APIs remain loopback-only.
- **LAN trusted:** bind the complete workspace to a selected interface only after authentication and authorization are implemented.
- **Reverse-proxy/server:** advanced deployment with TLS and explicit configuration.

### 17.2 Identity and authentication

LAN mode requires:

- local accounts;
- strong password hashing;
- session expiration;
- CSRF protection;
- role-based authorization;
- optional OS/LDAP/OIDC integration later;
- audit log;
- recovery owner account;
- optional invitation tokens.

### 17.3 Roles

Suggested roles:

- owner;
- administrator;
- maintainer;
- contributor;
- reviewer;
- reporter;
- read-only.

Permissions should be repository-scoped and explicit.

### 17.4 Concurrent editing

Near-term approach:

- detect file modifications since open;
- warn on stale saves;
- offer compare, overwrite, or save copy;
- optional advisory locks;
- presence indicators.

Do not implement opaque real-time collaborative text editing before repository consistency and conflict handling are reliable.

### 17.5 Event delivery

Use server-sent events or WebSockets for:

- file changes;
- active repository updates;
- job progress;
- issue/review activity;
- presence;
- notifications.

### 17.6 LAN security gate

LAN mode must not ship until:

- threat modeling is complete;
- authentication and authorization tests pass;
- TLS guidance exists;
- path access is repository-scoped;
- request size/rate limits exist;
- security headers are set;
- audit logs are protected;
- a security review is completed.

### 17.7 Secure external contribution gateway — v0.3.0 implemented slice

This is intentionally narrower than full trusted-LAN mode. Remote invitees receive no general repository browser, shell, registry access, owner session, or direct write capability.

- [x] Keep the normal owner workspace bound to loopback by default.
- [x] Add the initial dedicated share launcher for the restricted contribution surface (superseded by the v0.3.1 one-launch UI controller).
- [x] Block every remote route except the contributor page and token-scoped collaboration API.
- [x] Use repository-scoped, expiring, revocable, maximum-use invitation tokens.
- [x] Store only SHA-256 token hashes and place the raw token in the URL fragment.
- [x] Allow optional source-only ZIP download without ForgeTrace metadata or machine paths.
- [x] Allow source download to be disabled per invitation.
- [x] Allow a new teammate to paste the invitation link into an empty ForgeTrace installation and create a managed local fork.
- [x] Stream source archives and fork downloads with ZIP path, symlink, metadata, expansion-size, and redirect validation.
- [x] Stage all submitted files under application-data quarantine, never in the live workspace.
- [x] Reject `.git`, `.forgetrace`, traversal paths, oversized files, oversized pull requests, and excessive file counts.
- [x] Never extract contributor archives and never execute contributor code.
- [x] Generate owner-visible text diffs, binary hashes, deletion notices, and risky-file warnings.
- [x] Support approval, change requests, comments, revisions, token-scoped draft recovery, closure, and merge status.
- [x] Detect baseline conflicts and revalidate affected hashes under the repository merge lock.
- [x] Require a typed merge phrase and separate confirmation for executable/script-like files.
- [x] Create a safety snapshot and rollback backup before applying an atomic local merge.
- [x] Attribute the submitted change to the external contributor and the merge action to the local owner.
- [x] Add security headers, active-content attachment enforcement, origin checks, and remote request throttling.
- [x] Add service, route-boundary, API end-to-end, conflict, limit, source-download, and merge tests.
- [x] Add persistent append-only security-event storage and an owner-visible filter/integrity/export viewer.
- [ ] Add retention/rotation and externally anchored ledger signing/verification.
- [ ] Add line-level review comments and contributor responses.
- [ ] Add a visual merge-conflict resolver.
- [ ] Add optional antivirus/content-scanner hooks without making them mandatory.
- [ ] Add TLS/Tailscale/WireGuard deployment guides and safe tunnel presets.
- [ ] Add signed invitation metadata and optional contributor identity verification.
- [ ] Add Git-native fork/branch/patch interoperability while preserving the quarantine model.

**Security position:** the UI-enabled restricted contribution listener is appropriate for a trusted LAN or private VPN. Direct router port-forwarding to the public internet remains unsupported and should be blocked in documentation and UI warnings until TLS, persistent authentication, audit review, and adversarial testing are complete.

### 17.8 One-launch gateway control — v0.3.1 implemented slice

The v0.3.0 security model was correct, but requiring a separate launcher made collaboration easy to misunderstand and difficult for nontechnical users. v0.3.1 consolidates operation without weakening the trust boundary.

- [x] Keep the owner listener bound to loopback throughout the process lifetime.
- [x] Start ForgeTrace through one normal Windows launcher and one platform-equivalent shell launcher.
- [x] Remove the separate local/share batch and shell launchers from the package.
- [x] Add a runtime gateway manager that starts and stops a second HTTP listener from the owner process.
- [x] Label each HTTP listener as `owner`, `gateway`, or legacy `combined` and enforce policy from that listener identity.
- [x] Deny owner routes on the gateway listener even when the client address is loopback.
- [x] Default sharing to disabled after every process start.
- [x] Add owner-only status, start, and stop APIs for the contributor listener.
- [x] Add in-UI sharing status, port selection, LAN-address detection, and Stop Sharing controls.
- [x] Automatically enable sharing when the owner explicitly generates a token link while sharing is off.
- [x] Generate the final fragment-token URL in the UI without requiring users to type their LAN address.
- [x] Keep an optional advanced private-VPN/tunnel base-URL override without claiming to configure a tunnel or TLS.
- [x] Close the gateway when the owner clicks Stop Sharing or when the ForgeTrace process exits.
- [x] Add lifecycle, port-conflict, socket-closure, gateway-boundary, token-route, JavaScript, and Chromium UI tests.

**Usability position:** enabling sharing remains an explicit owner action, but it is now part of the same visible workflow as invitation creation. A user should never need to understand process binding or launch a second terminal to accept a pull request.

---

## 18. Backup, synchronization, and portability

### 18.1 Backup types

- repository files only;
- ForgeTrace metadata only;
- complete portable repository bundle;
- complete application registry backup;
- incremental snapshot/object backup.

### 18.2 Backup destinations

- local folder;
- external drive;
- network share;
- user-controlled S3-compatible storage;
- standard Git remote for Git data;
- optional plugins for other providers.

### 18.3 Backup policy

- scheduled or manual;
- versioned retention;
- checksum verification;
- encryption option;
- dry-run/estimate;
- bandwidth limits;
- resumable transfers;
- clear source and destination directions;
- restore testing.

### 18.4 Repository relocation

When a repository moves:

- detect missing path;
- offer search by marker/UUID;
- allow manual relink;
- verify repository identity;
- update registry without rewriting history;
- protect against accidentally linking the wrong folder.

### 18.5 Portable bundle format

A portable bundle should contain:

```text
repository files/
FORGETRACE_MANIFEST.json
FORGETRACE_HISTORY.json
FORGETRACE_ISSUES.json
FORGETRACE_LINEAGE.json
objects/              # optional, for full restoration
checksums.sha256
README_RESTORE.md
```

The format must be documented and versioned.

---

## 19. Security model

### 19.1 Threats to address

- path traversal;
- symlink escape;
- malicious archives;
- oversized uploads;
- decompression bombs;
- cross-site request forgery;
- cross-site scripting through filenames/Markdown;
- command injection in Git/build integration;
- credential leakage;
- unauthorized LAN access;
- plugin abuse;
- repository deletion;
- object-store corruption;
- secret indexing/export;
- race conditions and time-of-check/time-of-use attacks.

### 19.2 Path safety

- canonicalize every requested path;
- reject paths outside the active repository;
- define a strict symlink policy;
- protect `.forgetrace` and global data paths;
- use OS-safe atomic replace operations;
- avoid following directory junctions unexpectedly on Windows;
- add adversarial path tests.

### 19.3 Command safety

- never construct shell commands through string concatenation;
- invoke Git/processes with argument arrays;
- enforce working directory;
- redact environment variables and remote URLs where needed;
- time out hung commands;
- cap output;
- require explicit trust for repository-provided scripts.

### 19.4 Secret protection

- integrate ignore rules;
- warn on likely secrets before export, commit, or LAN sharing;
- never transmit detected secret content to external services by default;
- store credentials in OS keychain/credential manager;
- allow repository-level private path rules.

### 19.5 Plugin sandbox

Plugins should declare capabilities such as:

- read repository metadata;
- read files;
- write files;
- execute commands;
- access network;
- access secrets;
- contribute UI panels.

High-risk permissions require explicit approval.

### 19.6 Security documentation

Maintain:

- `SECURITY.md`;
- supported-version policy;
- private vulnerability reporting instructions;
- threat model;
- security changelog entries;
- release integrity checksums.

---

## 20. Performance and scale targets

### 20.1 Target repository sizes

Initial supported targets:

- 100,000 files;
- 20 GB workspace;
- 10,000 contribution events;
- 10,000 issues/comments combined;
- 2,000 snapshots or Git commits rendered through pagination;
- individual files up to configurable preview/edit limits;
- larger files retained and downloadable without loading fully into memory.

### 20.2 Performance targets

On a typical modern desktop with a warm cache:

- repository switcher result under 100 ms;
- dashboard usable under 500 ms;
- first file-tree page under 500 ms;
- common metadata search under 200 ms;
- text search shows first results under 1 second;
- no full-repository hash scan on every page load;
- memory remains bounded during large upload/export;
- long jobs are cancellable and survive UI navigation.

### 20.3 File watching

- incremental file events;
- debounce bursts;
- detect watcher overflow;
- schedule reconciliation scan after overflow;
- distinguish ForgeTrace writes from external writes;
- record external changes without falsely attributing an actor;
- allow pause/resume.

### 20.4 Job system

Long-running work becomes jobs:

- indexing;
- hashing;
- snapshot creation;
- export;
- import;
- backup;
- Git fetch/clone;
- object verification;
- garbage collection.

Jobs need:

- ID and repository scope;
- state;
- progress;
- cancellation;
- logs;
- retry policy;
- error summary;
- persistence across server restart where feasible.

---

## 21. API design

### 21.1 API principles

- versioned under `/api/v1`;
- repository-scoped routes use stable repository IDs;
- typed request/response schemas;
- pagination for lists;
- consistent error objects;
- idempotency for retryable writes where useful;
- CSRF/session protection in networked mode;
- OpenAPI documentation after framework migration;
- event stream for live updates.

### 21.2 Proposed repository routes

```text
GET    /api/v1/repositories
POST   /api/v1/repositories
GET    /api/v1/repositories/{repo_id}
PATCH  /api/v1/repositories/{repo_id}
DELETE /api/v1/repositories/{repo_id}          # unregister by default
POST   /api/v1/repositories/{repo_id}/relink
POST   /api/v1/repositories/discover
```

### 21.3 Proposed file routes

```text
GET    /api/v1/repositories/{repo_id}/tree
GET    /api/v1/repositories/{repo_id}/files/content
PUT    /api/v1/repositories/{repo_id}/files/content
POST   /api/v1/repositories/{repo_id}/files/upload
POST   /api/v1/repositories/{repo_id}/files/move
POST   /api/v1/repositories/{repo_id}/folders
DELETE /api/v1/repositories/{repo_id}/paths
POST   /api/v1/repositories/{repo_id}/trash/restore
```

### 21.4 Proposed snapshot routes

```text
GET    /api/v1/repositories/{repo_id}/snapshots
POST   /api/v1/repositories/{repo_id}/snapshots
GET    /api/v1/repositories/{repo_id}/snapshots/{snapshot_id}
POST   /api/v1/repositories/{repo_id}/snapshots/{snapshot_id}/restore
GET    /api/v1/repositories/{repo_id}/compare
POST   /api/v1/repositories/{repo_id}/verify
```

### 21.5 Proposed Git routes

```text
GET    /api/v1/repositories/{repo_id}/git/status
GET    /api/v1/repositories/{repo_id}/git/log
GET    /api/v1/repositories/{repo_id}/git/diff
POST   /api/v1/repositories/{repo_id}/git/stage
POST   /api/v1/repositories/{repo_id}/git/commit
GET    /api/v1/repositories/{repo_id}/git/branches
POST   /api/v1/repositories/{repo_id}/git/branches
POST   /api/v1/repositories/{repo_id}/git/checkout
POST   /api/v1/repositories/{repo_id}/git/fetch
POST   /api/v1/repositories/{repo_id}/git/pull
POST   /api/v1/repositories/{repo_id}/git/push
```

### 21.6 Proposed project routes

```text
/api/v1/repositories/{repo_id}/issues
/api/v1/repositories/{repo_id}/reviews
/api/v1/repositories/{repo_id}/contributions
/api/v1/repositories/{repo_id}/lineage
/api/v1/repositories/{repo_id}/releases
/api/v1/repositories/{repo_id}/decisions
/api/v1/repositories/{repo_id}/search
/api/v1/repositories/{repo_id}/exports
```

### 21.7 Error format

```json
{
  "error": {
    "code": "PATH_CONFLICT",
    "message": "A file already exists at the destination.",
    "details": {
      "path": "src/app.py"
    },
    "requestId": "..."
  }
}
```

---

## 22. Front-end information architecture

### 22.1 Global shell

- repository switcher;
- global search;
- job/progress center;
- notifications;
- settings;
- connection/server status;
- current user identity in collaborative mode.

### 22.2 Repository navigation

Recommended primary sections:

1. Overview
2. Files
3. Changes
4. History
5. Lineage
6. Issues
7. Reviews
8. Releases
9. People
10. Settings

Do not expose every section when a repository mode does not support it.

### 22.3 Overview dashboard

- repository identity and path;
- current branch/snapshot line;
- dirty/changed status;
- recent activity;
- open issues and blockers;
- latest snapshots/commits;
- contribution summary;
- test/release health;
- storage/index health;
- quick actions.

### 22.4 Accessibility

- full keyboard navigation;
- visible focus states;
- semantic landmarks;
- screen-reader labels;
- scalable typography;
- reduced-motion support;
- high-contrast mode;
- color never carries status alone;
- accessible graph alternatives as lists/tables.

### 22.5 Mobile and tablet

Mobile should support monitoring, issue management, review, and small file operations. Large code editing and complex graph manipulation can be optimized for desktop while remaining readable on mobile.

---

## 23. Desktop packaging and operating-system integration

### 23.1 Packaging goals

- Windows installer and portable ZIP;
- macOS application bundle;
- Linux AppImage/deb/rpm as feasible;
- headless Python/server package retained;
- signed releases when infrastructure permits;
- reproducible build documentation.

### 23.2 Desktop integration

- file/folder picker;
- system tray option;
- start on login option;
- open repository from OS context menu;
- protocol handler such as `forgetrace://repo/<id>`;
- recent repositories in OS jump lists where supported;
- native notifications;
- OS keychain;
- automatic update channel with opt-out;
- crash report generation that excludes repository contents by default.

### 23.3 CLI

Proposed commands:

```text
forgetrace serve
forgetrace repo add <path>
forgetrace repo list
forgetrace repo open <id-or-path>
forgetrace snapshot create
forgetrace snapshot restore <id>
forgetrace verify
forgetrace export
forgetrace doctor
forgetrace backup
```

The CLI should call the same service layer as the UI.

---

## 24. Plugin and automation system

### 24.1 Plugin goals

Allow extensions for:

- language parsers;
- linters and formatters;
- test runners;
- build systems;
- artifact previews;
- backup providers;
- remote hosts;
- issue import/export;
- custom contribution classifiers;
- dashboard panels.

### 24.2 Plugin manifest

Each plugin declares:

- ID and version;
- compatible ForgeTrace API versions;
- permissions;
- entry points;
- settings schema;
- repository types/languages supported;
- network domains if applicable;
- commands and UI contributions.

### 24.3 Automation recipes

Users should be able to define safe recipes such as:

- run tests before snapshot;
- create a snapshot before branch switch;
- export release notes after tagging;
- back up after a successful release;
- warn when generated files change;
- create an issue from a failed test.

Automation must be transparent, logged, and disableable.

---

## 25. Observability and diagnostics

### 25.1 Structured logs

- JSON-compatible structured events;
- timestamp, severity, component, request/job ID, repository ID;
- no file contents by default;
- secret redaction;
- rolling log retention;
- user-exportable diagnostic bundle.

### 25.2 Health page

Show:

- application and schema version;
- active data paths;
- database integrity;
- registered repository count;
- missing repositories;
- index status;
- object-store verification status;
- watcher status;
- Git version;
- disk space;
- recent failed jobs;
- network bind mode.

### 25.3 Doctor command

`forgetrace doctor` should:

- verify databases;
- verify repository path mappings;
- inspect object references;
- identify orphaned objects;
- test write permissions;
- check Git availability;
- check port conflicts;
- generate a repair plan;
- avoid destructive repair without confirmation.

---

## 26. Testing strategy

### 26.1 Unit tests

Cover:

- path normalization;
- path containment;
- repository registry;
- database migrations;
- hash/object storage;
- snapshot manifests;
- diff calculations;
- contribution graph validation;
- Git output parsing;
- permission checks;
- export/import validation.

### 26.2 Integration tests

- create/register/switch multiple repositories;
- repositories on separate paths;
- upload/edit/rename/delete/restore;
- crash during mutation and recover;
- snapshot and selective restore;
- Git commit/branch/merge fixtures;
- search index updates after external file changes;
- export then import round trip;
- LAN authorization boundaries;
- backup/restore.

### 26.3 End-to-end browser tests

Automate:

- first-run setup;
- add existing repository;
- switch repositories;
- upload folder;
- edit/save conflict;
- create snapshot;
- browse history;
- create issue;
- create change set/review;
- generate release;
- contribution lineage filtering;
- keyboard navigation;
- responsive layouts.

### 26.4 Adversarial tests

- `../` and encoded traversal;
- absolute paths;
- symlink/junction escape;
- archive traversal;
- case-insensitive collisions;
- Unicode normalization collisions;
- Windows reserved names;
- locked files;
- permission changes mid-operation;
- disk-full behavior;
- interrupted export;
- malformed SQLite/JSON;
- malicious Markdown/filename XSS;
- oversized requests;
- command injection strings;
- corrupted object blobs.

### 26.5 Performance tests

Maintain generated fixtures for:

- 100,000 small files;
- deeply nested trees;
- large binary files;
- long Git history;
- large contribution graph;
- many repositories in registry;
- slow/removable/network storage.

### 26.6 Release gate

A release is not complete until:

- tests pass on supported operating systems;
- migration from the prior version is tested;
- export/import round trip passes;
- security regression suite passes;
- package starts from a clean machine/user profile;
- checksums are generated;
- changelog and known issues are updated;
- recovery procedure is documented.

---

## 27. Open-source project structure

Recommended repository structure:

```text
forgetrace/
├── README.md
├── START_HERE.md
├── BUILD_PLAN.md
├── ARCHITECTURE.md
├── CONTRIBUTING.md
├── SECURITY.md
├── CODE_OF_CONDUCT.md
├── NOTICE.md
├── CHANGELOG.md
├── LICENSE
├── pyproject.toml
├── frontend/
│   ├── package.json
│   └── src/
├── backend/
│   └── forgetrace/
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   ├── security/
│   └── fixtures/
├── docs/
│   ├── architecture/
│   ├── api/
│   ├── formats/
│   ├── threat-model/
│   └── screenshots/
├── scripts/
├── packaging/
└── examples/
```

### Open-source governance

- MIT license remains authoritative.
- Rooke Poole is credited as creator and original project lead.
- Contributions should use a Developer Certificate of Origin or simple sign-off rather than a heavy contributor license agreement unless legal needs change.
- Public decisions should be recorded through issues/ADRs.
- Releases should have signed tags when practical.
- Security reports should have a private route.
- The contribution model must recognize documentation, design, testing, review, and support—not only code.

---

## 28. Versioning and compatibility

Use semantic versioning after the internal API and data format stabilize.

Suggested development sequence:

- `0.1.x` — single-repository working baseline;
- `0.2.x` — repository registry and multi-path support;
- `0.3.x` — SQLite migration, search, and robust history;
- `0.4.x` — Git workbench;
- `0.5.x` — issues, reviews, and contribution lineage;
- `0.6.x` — releases, backup, and portability;
- `0.7.x` — LAN collaboration preview;
- `0.8.x` — plugins and automation;
- `0.9.x` — desktop packaging and release hardening;
- `1.0.0` — stable local-first repository platform.

Compatibility commitments for 1.0:

- documented database and export migrations;
- no silent repository-content rewrites;
- stable portable bundle format versioning;
- support importing all prior public ForgeTrace export formats;
- clear supported-version security policy.

---

## 29. Phased implementation roadmap

## Phase 0 — Baseline hardening

**Goal:** Turn the hackathon implementation into a maintainable foundation before adding broad features.

### Work

- [x] Split `server.py` into repository, registry, API, application, error, constant, and utility modules.
- [ ] Add typed data models and centralized validation.
- [x] Add structured error codes.
- [x] Replace ad hoc request prints with centralized logging.
- [x] Add environment/CLI configuration and platform data directories.
- [x] Add atomic repository state writes and backup copies.
- [x] Add operation journals for file/folder mutation, restore, import, and merge; hold export under the repository lock.
- [x] Add shared per-workspace in-process repository locks to prevent conflicting request writers.
- [x] Add multi-repository, API, security, recovery, and 100-repository fixtures with `unittest`.
- [x] Add traversal, protected metadata, duplicate identity/path, backup, and export-boundary tests.
- [x] Add Chromium UI smoke testing for repository switching and file editing.
- [x] Add application version endpoint and numbered SQLite migration framework.
- [x] Add `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, and `NOTICE.md`.

### Exit gate

- Existing single-repository workflow passes automated tests.
- Interrupted operations do not corrupt state.
- The codebase has clear module boundaries.
- A clean install/run procedure is documented for Windows, macOS, and Linux.

---

## Phase 1 — Multi-repository paths and registry

**Goal:** Manage many repositories from one ForgeTrace instance.

### Work

- [x] Create global platform-specific application data directory.
- [x] Create SQLite registry and migration `0001_repository_registry`.
- [x] Add stable repository UUIDs embedded in repository metadata.
- [x] Add create, add existing, managed file/folder import, initialize, unregister, and relink workflows.
- [x] Support arbitrary absolute repository paths, including different local drives.
- [x] Support browser-created managed repositories without requiring users to know an absolute path.
- [x] Fork a shared repository from a collaboration link before any local repository exists.
- [x] Automatically repopulate valid managed repositories by embedded UUID during startup recovery.
- [x] Render nested repository paths as an expandable and persistent folder tree.
- [x] Preserve unavailable removable/mounted paths as offline registry entries with recovery actions.
- [x] Add repository switcher, favorites, and recency ordering.
- [x] Add complete tags and collections workflows.
- [x] Add canonical duplicate-path and duplicate-identity detection.
- [ ] Complete metadata placement modes. Embedded mode is working; external mode remains pending.
- [x] Add repository-scoped `/api/v1/repositories/{id}/...` routes for every file and snapshot operation.
- [ ] Make future background jobs repository-scoped; no background job system exists yet.
- [x] Add repository settings and per-repository limits.
- [x] Add live API and direct-service multi-repository integration tests.

### Exit gate

- [x] One process reliably manages at least 100 registered repositories in the automated fixture.
- [x] Switching repositories cannot mix files or snapshot history in live API tests.
- [x] Missing paths are recoverable through UUID-verified relink.
- [x] Unregister and file deletion are separate operations; unregister deletes no files.

**Phase status:** The repository-library product layer is green. Phase 1 remains open only for external metadata mode, deeper removable/network capability classification, and future background-job scoping.

---

## Phase 2 — SQLite history, indexing, and file reliability

**Goal:** Replace fragile metadata storage and make repositories searchable.

### Work

- [ ] Define per-repository database schema.
- [ ] Migrate JSON history into SQLite.
- [ ] Retain JSON as export format.
- [ ] Add transaction boundaries to all mutations.
- [ ] Add repository trash/recovery area.
- [ ] Add file watcher and reconciliation scans.
- [ ] Add FTS5 index for paths, text content, and metadata.
- [ ] Add ignore-rule engine.
- [ ] Add global and repository search.
- [ ] Add incremental hashing and object verification.
- [ ] Add selective file restore.
- [ ] Add snapshot comparison and per-file history.
- [ ] Add job manager with progress and cancellation.
- [ ] Add health/doctor interface.

### Exit gate

- Search remains responsive on the target large fixture.
- Database and object verification detects corruption.
- External file changes are reconciled safely.
- Export/import round trip preserves files and history.

---

## Phase 3 — Git workbench

**Goal:** Make ForgeTrace a useful local Git client without breaking snapshot-only repositories.

### Work

- [ ] Detect Git repositories and installed Git capabilities.
- [ ] Read status, branches, log, tags, and remotes.
- [ ] Add text diff and changed-file views.
- [ ] Add stage/unstage and commit.
- [ ] Add branch create/switch/delete.
- [ ] Add fetch/pull/push with credential-helper delegation.
- [ ] Add conflict detection and external merge-tool launch.
- [ ] Link Git commits to ForgeTrace contributions.
- [ ] Add automatic safety snapshots before risky Git operations.
- [ ] Detect externally created commits and branches.
- [ ] Handle submodules and worktrees conservatively.
- [ ] Add Git fixture and cross-client compatibility tests.

### Exit gate

- A repository can move between ForgeTrace and command-line Git without incompatibility.
- ForgeTrace never stores raw Git credentials.
- Dirty-worktree and conflict cases have recovery paths.

---

## Phase 4 — Contribution Lineage and project intelligence

**Goal:** Deliver the project’s unique value beyond file hosting.

### Work

- [ ] Implement contribution event and edge schema.
- [ ] Import existing file/snapshot/Git evidence.
- [ ] Add manual proposal, decision, support, and review events.
- [ ] Add explicit and inferred lineage links with confidence labels.
- [ ] Build lineage graph and accessible table view.
- [ ] Add filters, playback, and upstream/downstream tracing.
- [ ] Add contribution receipts.
- [ ] Add explainable impact dimensions.
- [ ] Add credit allocation and correction audit trail.
- [ ] Add data export for lineage.
- [ ] Add privacy/redaction controls.
- [ ] Validate scoring language with users to avoid misleading rankings.

### Exit gate

- Every score or relationship can be traced to evidence or marked as inference.
- Users can correct credit without rewriting source history.
- Non-code contributions are first-class and searchable.

---

## Phase 5 — Issues, decisions, reviews, and boards

**Goal:** Replace the most-used local project-management parts of hosted repository platforms.

### Work

- [ ] Add issues, labels, milestones, assignees, comments, and templates.
- [ ] Add issue dependencies and duplicate relationships.
- [ ] Add project table, kanban, and roadmap views.
- [ ] Add decision records and optional Markdown ADR generation.
- [x] Add snapshot-native quarantined pull-request change sets with exact text diffs and binary hashes.
- [ ] Add Git-range and branch-backed change sets.
- [ ] Add inline review comments.
- [x] Add pull-request approval, change-request, comment, revision, conflict, close, and merge states.
- [ ] Link issues, decisions, reviews, commits, snapshots, tests, and releases.
- [ ] Add Markdown import/export.
- [ ] Add cross-repository boards.
- [ ] Add notifications and saved filters.

### Exit gate

- A solo project can plan, implement, review, and close work without an external tracker.
- All project-management data exports to documented formats.

---

## Phase 6 — Releases, artifacts, backup, and replication

**Goal:** Make projects distributable and recoverable.

### Work

- [ ] Add release records and source references.
- [ ] Generate curated release notes from issues and lineage.
- [ ] Attach assets and generate checksums.
- [ ] Add portable repository bundles.
- [ ] Add scheduled local/external-drive backups.
- [ ] Add S3-compatible backup adapter.
- [ ] Add standard Git remote operations to backup workflows.
- [ ] Add encryption option for backup bundles.
- [ ] Add restore preview and verification.
- [ ] Add retention and garbage-collection policy.

### Exit gate

- A complete repository can be restored on another machine from a documented bundle.
- Backups are checksum-verified and testable.

---

## Phase 7 — Trusted LAN collaboration

**Goal:** Support small teams on user-controlled networks.

### Work

- [x] Add an explicit restricted contribution-gateway network bind while preserving loopback owner access.
- [x] Add one-launch owner UI controls for gateway status, start, stop, port, and detected share address.
- [x] Enforce contributor-only routing by listener identity, including for loopback requests to the gateway port.
- [ ] Add configurable trusted-LAN full-workspace bind after authentication exists.
- [ ] Add users, password hashing, sessions, and recovery owner.
- [ ] Add repository roles and permissions.
- [x] Add origin protection, security headers, and request throttling to the contribution gateway.
- [ ] Add session CSRF tokens and protected persistent audit logs for full trusted-LAN mode.
- [ ] Add real-time events and job updates.
- [ ] Add stale-edit detection and advisory locks.
- [x] Add expiring, revocable, repository-scoped contribution invitations.
- [ ] Add persistent account management and role-bearing invitations.
- [ ] Add TLS/reverse-proxy documentation.
- [ ] Complete threat model and security review.
- [ ] Add LAN penetration/adversarial test suite.

### Exit gate

- Unauthorized users cannot read repository metadata or files.
- Every write is attributable and auditable.
- Local-only mode remains the default.

---

## Phase 8 — Plugins, automation, and integrations

**Goal:** Let ForgeTrace adapt without bloating the core.

### Work

- [ ] Define versioned plugin manifest and permission model.
- [ ] Add plugin installation and disable/remove flows.
- [ ] Add command and UI extension points.
- [ ] Add test runner/build adapter interface.
- [ ] Add repository hooks and automation recipes.
- [ ] Add backup-provider adapter interface.
- [ ] Add issue import/export adapters.
- [ ] Add plugin diagnostics and safe mode.
- [ ] Document plugin SDK with example plugins.

### Exit gate

- Plugins cannot access files, network, commands, or secrets without declared permission.
- A broken plugin cannot prevent safe-mode startup.

---

## Phase 9 — Desktop packaging and 1.0 hardening

**Goal:** Deliver a trustworthy, installable product.

### Work

- [ ] Create desktop shell and native file pickers.
- [ ] Produce Windows, macOS, and Linux packages.
- [ ] Add OS keychain and notifications.
- [ ] Add update channels and rollback.
- [ ] Add crash-safe startup and safe mode.
- [ ] Complete accessibility review.
- [ ] Complete performance targets.
- [ ] Complete format/API documentation.
- [ ] Complete migration and uninstall tests.
- [ ] Sign release artifacts when feasible.
- [ ] Publish checksums and software bill of materials.
- [ ] Run release candidate period on real repositories.

### 1.0 exit gate

- Core local workflows are reliable on supported operating systems.
- Existing Git repositories remain standard and portable.
- Multi-repository management, search, history, issues, reviews, releases, lineage, and backup are stable.
- No internet account is required.
- Data formats and recovery procedures are documented.

---

## 30. Completed implementation sprint — v0.2.0

This sprint delivered **Phase 0 hardening plus the smallest vertical slice of Phase 1**.

### Sprint objective

Run one ForgeTrace server that can register, display, switch between, and operate on at least two repository paths without cross-repository state leakage.

### Ordered task list

1. [x] Create a new development branch and tag the current working baseline.
2. [x] Add `APP_VERSION` and schema version constants.
3. [x] Introduce `forgetrace/` Python package structure.
4. [x] Move current `ForgeTraceRepository` into a repository service module.
5. [x] Add platform-specific application data directory helper.
6. [x] Create SQLite global registry with migration `0001_repository_registry`.
7. [x] Add repository UUID and path normalization.
8. [x] Add `GET/POST /api/v1/repositories`.
9. [x] Add repository-scoped state endpoint.
10. [x] Add active-repository switcher in the UI.
11. [x] Refactor all file/snapshot operations to accept repository context.
12. [x] Add two-repository integration fixture.
13. [x] Prove upload/edit/snapshot/export in repository A does not affect B.
14. [x] Add unregister and relink; do not add destructive repository deletion yet.
15. [x] Update migration, recovery, and test documentation.

### Sprint completion record — v0.2.0

**Status:** Complete on 2026-07-24.

Implemented evidence:

- branch `feature/multi-repository-registry`;
- rollback tag `v0.1.0-roadmap-baseline`;
- `APP_VERSION = 0.2.0`;
- `forgetrace/` package boundaries;
- platform application-data helper;
- SQLite registry migration `0001_repository_registry`;
- UUID and canonical-path enforcement;
- repository-scoped API v1;
- real repository switcher and path management UI;
- live two-repository API isolation test;
- 100-repository registry fixture;
- restart, offline detection, relink, and unregister tests;
- path/archive/recovery tests;
- Chromium UI switching and editing test;
- architecture, API, migration, recovery, and testing documentation.

**Known carryover:** This sprint completes the narrow vertical slice, not all of Phase 0 or Phase 1. Typed models, operation journals, external metadata, tags/collections, repository settings, and background jobs remain open.

### Sprint acceptance test

1. [x] Start ForgeTrace once.
2. [x] Add two folders on different paths.
3. [x] Upload a file to repository A.
4. [x] Switch to repository B and confirm it is absent.
5. [x] Create a file and snapshot in repository B.
6. [x] Switch to repository A and confirm its state is unchanged.
7. [x] Recreate the registry service to simulate restart.
8. [x] Confirm both repositories remain registered and open correctly.
9. [x] Temporarily rename/move repository B.
10. [x] Confirm it becomes `offline`, then relink it and recover all history.

Do not begin Git, issues, or LAN networking until this test is consistently green.

---

## 31. Definition of done for every feature

A feature is done only when:

- behavior is implemented, not mocked;
- errors are handled and visible;
- data survives restart;
- repository boundaries are enforced;
- destructive effects are recoverable or clearly irreversible;
- unit/integration tests cover normal and failure paths;
- security implications are reviewed;
- keyboard and accessible behavior is considered;
- documentation is updated;
- migration impact is addressed;
- export/portability is not broken;
- contribution events are recorded where appropriate;
- no credentials or private file content appear in logs;
- release notes identify the change and known limitations.

---

## 32. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---:|---:|---|
| Feature scope expands faster than core reliability | High | High | Phase gates; reject work that bypasses current gate |
| Reimplementing Git creates incompatibilities | Medium | High | Use standard Git executable and formats |
| Multi-repository operations target wrong path | Medium | Critical | Stable repo IDs, explicit context, path containment tests |
| Metadata corruption after interruption | Medium | High | SQLite transactions, journals, backups, doctor tool |
| Object store consumes excessive disk | Medium | High | Retention, compression, GC, usage dashboard |
| Secret files enter index/export | Medium | High | Ignore rules, secret warnings, explicit export preview |
| LAN mode exposes private repositories | Medium | Critical | Local-only default, auth gate, threat model, security tests |
| Contribution scores misrepresent people | Medium | High | Explainable dimensions, corrections, opt-out, no single ranking |
| Large repos make UI unusable | High | High | Pagination, virtualization, incremental indexing, scale fixtures |
| Plugin ecosystem creates attack surface | Medium | High | Capability declarations, permissions, safe mode, signing later |
| Desktop packaging diverges from server mode | Medium | Medium | Shared service layer and API contract |
| Open-source contributions become hard to manage | Medium | Medium | Clear contribution guide, issue templates, DCO/sign-off |

---

## 33. Success metrics

Avoid vanity metrics such as repository count alone. Track product outcomes.

### Reliability

- successful repository operations;
- operation rollback/recovery success;
- database/object integrity verification rate;
- crash-free sessions;
- successful export/import round trips.

### Usability

- time to add first repository;
- time to switch repositories;
- time to find a file/change/issue;
- restore task completion rate;
- percentage of workflows completed without external documentation.

### Performance

- first useful render;
- search latency;
- indexing throughput;
- memory use by repository size;
- export/restore throughput.

### Portability

- successful open in standard Git tools;
- successful repository move/relink;
- successful restore on a second machine;
- percentage of data represented in documented export formats.

### Contribution visibility

- percentage of issue/review/test/documentation events linked to evidence;
- percentage of inferred links corrected by users;
- contribution types represented beyond commits;
- user-reported trust in lineage explanations.

---

## 34. Release checklist template

```text
[ ] Scope matches the current phase
[ ] Unit tests pass
[ ] Integration tests pass
[ ] End-to-end tests pass
[ ] Security tests pass
[ ] Migration from previous release passes
[ ] Export/import round trip passes
[ ] Object/database verification passes
[ ] Windows package/run tested
[ ] macOS package/run tested
[ ] Linux package/run tested
[ ] Documentation updated
[ ] CHANGELOG updated
[ ] Known limitations documented
[ ] LICENSE and NOTICE included
[ ] Rooke Poole creator credit retained
[ ] Checksums generated
[ ] Release archive inspected
[ ] Recovery/rollback procedure tested
```

---

## 35. Final product test scenario

ForgeTrace is ready to call a great local alternative when this complete scenario works:

1. A user installs ForgeTrace without creating an online account.
2. They register five projects located on multiple drives.
3. ForgeTrace identifies two as Git repositories and three as snapshot-only.
4. They search across every repository and open a result at the correct file and line.
5. They create an issue, link a decision, edit files, create tests, and record a review.
6. ForgeTrace shows an evidence-backed lineage from issue to release.
7. They create a branch or snapshot line, compare changes, and safely restore one file.
8. They create a release bundle with checksums and notes.
9. They back up the repository to an external drive.
10. They move one repository to another path and relink it without losing history.
11. A second trusted user reviews the project over LAN with repository-scoped permissions.
12. The project still opens normally with standard file tools and Git outside ForgeTrace.
13. ForgeTrace is uninstalled, and the project files remain usable.
14. ForgeTrace is reinstalled, and the repository can be re-registered or restored from its portable bundle.

That scenario—not visual resemblance to GitHub—is the standard for success.

---

## 36. Completed implementation checkpoint — v0.2.1

The Registry Reliability and Organization release completed and validated:

- [x] Editable name, description, default author, and upload limit.
- [x] Repository metadata synchronization with offline-state protection.
- [x] Normalized tags and many-to-many collections.
- [x] Pinned repositories, client-side library search, and persistent saved filters.
- [x] Registry backup creation with bounded retention.
- [x] Portable registry JSON export and non-destructive merge import.
- [x] CLI and browser doctor checks.
- [x] UUID-based discovery and safe re-registration of embedded repositories.
- [x] Availability, access, free-space, and UNC/network-path capability probes.
- [x] Migration from the v0.2.0 registry schema with legacy organization backfill.
- [x] Formal deprecation headers on unscoped compatibility API responses.
- [x] Direct-service, live API, CLI, migration, security, isolation, and Chromium UI tests.

External metadata mode was deliberately not marked complete. Its relink and identity model must be as trustworthy as embedded UUID verification before release.

---

## 37. Completed implementation checkpoint — v0.3.0

The Secure Quarantined Collaboration release completed and validated:

- [x] Restricted network share launcher with remote owner-route denial.
- [x] Repository-scoped expiring, revocable, maximum-use invites.
- [x] Hash-only token persistence and fragment-based share links.
- [x] Optional source-only repository download with metadata exclusion.
- [x] Application-data quarantine and protected-path containment.
- [x] Snapshot-native pull requests with changed files and requested deletions.
- [x] Exact text diffs, binary hashes, risky-file warnings, and conflict evidence.
- [x] Approval, change request, comment, revision, resubmission, close, and merge states.
- [x] Typed merge confirmation, risky-file approval, safety snapshot, under-lock revalidation, atomic application, and rollback path.
- [x] External contributor and local merger attribution.
- [x] Request/body/file/count limits, remote throttling, security headers, origin protection, and active-content attachment enforcement.
- [x] Owner and contributor browser interfaces.
- [x] Direct-service, remote-boundary, source-download, API end-to-end, conflict, merge, regression, and Chromium interface tests.

The release does not claim GitHub protocol compatibility or full authenticated LAN hosting. Those remain separate roadmap gates.

---

## 38. Completed implementation checkpoint — v0.3.1

The One-Launch Secure Sharing release completed and validated:

- [x] One obvious Windows launcher and one platform-equivalent shell launcher.
- [x] Automatic opening of the localhost owner workspace from the packaged launchers.
- [x] Runtime contributor-gateway manager inside the normal ForgeTrace process.
- [x] Sharing disabled by default after every fresh start.
- [x] Owner-only sharing status, start, and stop endpoints.
- [x] Separate owner and contributor `ThreadingHTTPServer` surfaces.
- [x] Gateway denial of owner APIs even when reached through loopback.
- [x] In-UI sharing toggle, contributor port, detected LAN address, and optional advanced URL override.
- [x] Automatic sharing startup plus token-link generation from one explicit form action.
- [x] In-UI Stop Sharing that closes the contributor socket without stopping the owner workspace.
- [x] Process shutdown cleanup for both listeners.
- [x] Removal of separate local/share batch and shell launcher pairs.
- [x] Updated security, architecture, API, startup, testing, product, and roadmap documentation.
- [x] Full 24-test regression suite plus Chromium one-launch invite-generation coverage.

The release preserves the v0.3.0 quarantine, review, conflict, and atomic-merge model. It changes how the gateway is operated, not what remote contributors are trusted to do.

---

## 39. Completed implementation checkpoint — v0.3.2

The Repository Onboarding Usability release completed and validated:

- [x] Three distinct Add Repository choices: **Upload files**, **Upload folder**, and **Use a local path**.
- [x] Owner-only managed-repository creation API for browsers that cannot provide absolute host paths.
- [x] Cross-platform-safe managed directory naming with collision suffixes.
- [x] Ordinary on-disk repository workspaces under platform application data.
- [x] Automatic repository-name inference and selected-file count/size/path preview.
- [x] Folder import that strips one selected root and preserves nested relative paths.
- [x] Existing absolute-path create and existing-folder registration preserved.
- [x] Partial failure reporting that retains successful imports and the created repository.
- [x] Direct registry, live API, static, and Chromium tests for all onboarding routes.
- [x] Full 27-test registry, recovery, collaboration, security, and unified-sharing regression suite.

This release adds an easier path to a normal local repository; it does not introduce virtual files, cloud storage, archive extraction, or remote owner access.

---

## 40. Completed implementation checkpoint — v0.3.3

The Team Onboarding and Upgrade Continuity release completed and validated:

- [x] Local fork creation from a pasted secure collaboration link with no active repository requirement.
- [x] Source-only gateway validation and streamed managed-repository import.
- [x] Non-secret upstream provenance retained inside the fork; raw bearer tokens are never persisted.
- [x] Streamed transfer path and increased configurable limits for practical team repository sizes.
- [x] Expandable/collapsible nested repository tree with per-repository local persistence.
- [x] Stable application-data registry reuse plus startup UUID rediscovery and safe managed-repository relinking.
- [x] 37-test regression suite and Chromium onboarding/tree validation.

This release implements a ForgeTrace-native local fork, not Git wire-protocol cloning or a GitHub-compatible fork network.

---

## 41. Completed implementation checkpoint — v0.3.4

The Comprehensive Recursive Folder Import release completed and validated:

- [x] Recursive directory-handle enumeration for every descendant file.
- [x] Compatibility fallback for browsers that expose only `webkitdirectory` file lists.
- [x] Existing-repository upload retains the selected target-folder root.
- [x] New-repository onboarding removes only that outer root and retains every descendant path.
- [x] Empty leaf-folder preservation where the browser exposes directory handles.
- [x] Deep repository tree expansion verified through six levels.
- [x] 39-test regression suite plus dedicated Chromium recursive-folder validation.

This patch changes folder discovery and presentation only. It does not alter the collaboration trust boundary, registry location, repository UUID model, or snapshot format.

---

## 42. Completed implementation checkpoint — v0.3.5

The Verified Native Folder Import release completed and validated:

- [x] Real Chromium `webkitdirectory` selection used as the primary recursive folder path.
- [x] File objects are snapshotted and retained until asynchronous uploads finish; inputs are cleared only afterward.
- [x] Every selected file path is verified against the server-side repository tree after upload.
- [x] Missing descendant files receive one automatic upload retry before the import is declared incomplete.
- [x] Import results show discovered, verified, and missing path counts directly in the workspace.
- [x] Every imported parent folder is automatically expanded so nested files appear immediately.
- [x] Manual **Expand all** and **Collapse all** controls are available for large trees.
- [x] New-repository folder onboarding and existing-repository folder upload share the same verification guarantees.
- [x] One-launch startup opens the browser only after the new package successfully binds its server port, preventing an older running package from being mistaken for the update.
- [x] Native on-disk directory input, automatic retry, JavaScript syntax, Python compilation, and complete regression coverage.

This checkpoint fixes both failure modes reported by users: nested files that were not retained through an asynchronous fallback upload, and nested files that uploaded successfully but remained hidden inside collapsed folders.

---

## 43. Completed implementation checkpoint — v0.3.6

The Direct-Disk Complete Folder Import release completed and validated:

- [x] The primary complete-folder action opens a local operating-system folder chooser rather than relying on a browser-generated `FileList`.
- [x] Windows uses a PowerShell STA `FolderBrowserDialog`; macOS uses AppleScript; Linux uses Zenity or KDialog when available.
- [x] Owner-only picker and import APIs remain inaccessible from the contributor gateway and non-loopback clients.
- [x] The server enumerates the selected source with `os.walk`, follows no symbolic links, and raises visible errors for unreadable directories.
- [x] Every readable descendant file is copied through a temporary file and atomic replacement.
- [x] Empty directories and arbitrary nesting depth are preserved.
- [x] Existing-repository import retains the selected outer folder; new managed-repository onboarding imports only its contents.
- [x] Root `.forgetrace` metadata is excluded to prevent repository-identity collisions while ordinary hidden files and Git metadata remain importable.
- [x] The resulting repository tree is verified before success is returned.
- [x] Browser upload remains available as a clearly labeled fallback.
- [x] Browser fallback prefers explicit `showDirectoryPicker()` recursion over `webkitdirectory` and sends a complete bulk folder manifest to the server.
- [x] 48 Python unit/integration tests plus deep-folder, interrupted-upload, owner-workspace, and direct native-import Chromium tests pass.

This checkpoint addresses the repeated real-world report that nested files were still absent even when synthetic browser tests passed. The primary workflow no longer depends on browser directory enumeration.

---

---

## 44. Completed implementation checkpoint — v0.4.0

The Audit Stabilization and Transactional Recovery release closes the full 29-item v0.3.6 bug audit.

### Critical integrity

- [x] Validate every snapshot object before workspace mutation.
- [x] Recompute SHA-256 for restore, export, Doctor, and safety paths.
- [x] Serialize repository writers across processes and enforce one owner instance per application-data directory.
- [x] Couple filesystem and metadata changes through rollback journals.

### Imports and repository presentation

- [x] Stage folder imports outside the live repository.
- [x] Preview conflicts and require abort, skip, overwrite, or rename behavior.
- [x] Preflight total bytes and free space; expose persistent progress and cancellation.
- [x] Verify committed file size and SHA-256.
- [x] Make new managed-repository imports atomic with no orphan repository on failure.
- [x] Reject nested `.forgetrace` metadata.
- [x] Render a true depth-first parent-child tree with virtualization.
- [x] Enable transactional folder rename and delete.
- [x] Keep successful imports successful when browser storage is unavailable.

### Recovery, performance, and metadata

- [x] Restore valid `state.json.bak` through Doctor.
- [x] Recover pending filesystem journals at startup.
- [x] Relink moved managed repositories by UUID despite stale old paths.
- [x] Cache unchanged file digests.
- [x] Preserve empty directories, modes, and timestamps in snapshots.
- [x] Hold the repository mutation lock throughout export.

### Collaboration and HTTP

- [x] Clean terminal/stale quarantine and expose storage metrics.
- [x] Add sensitive-file previews and explicit inclusion controls.
- [x] Add request timeouts, HEAD support, and bounded rate maps.
- [x] Split monolithic route/import functions into bounded units.

### Test quality and release evidence

- [x] Add a real-server, real-disk Chromium workflow.
- [x] Add Windows picker process/PowerShell contract tests and a physical Windows acceptance harness.
- [x] Pass 76 Python tests with 76% total coverage and 87% native-picker coverage.
- [x] Produce one-to-one audit closure evidence and a complete new-chat handoff package.

See `AUDIT_CLOSURE.md` for the evidence attached to every finding.

---

## 45. Completed implementation checkpoint — v0.4.1 Security Event Ledger

### Storage and integrity

- [x] Store security evidence in a dedicated application-data SQLite database separate from registry and repository metadata.
- [x] Serialize appends with an OS-backed cross-process lock and SQLite `WAL`/`synchronous=FULL`.
- [x] Enforce immutable event/schema rows with SQLite update/delete rejection triggers.
- [x] Assign monotonic sequences and canonical SHA-256 previous-event chains.
- [x] Verify SQLite integrity, schema version, triggers, sequence continuity, JSON, previous hashes, and event hashes at startup and on demand.
- [x] Treat missing immutability controls as integrity failure after restart rather than silently recreating them.

### Privacy and trust boundaries

- [x] Recursively redact sensitive detail keys and bound detail depth/item/string size.
- [x] Never pass raw invitation tokens to the ledger; persist only a short SHA-256 fingerprint.
- [x] Exclude arbitrary export-search text from audit details.
- [x] Deny ledger query/integrity/export routes on the contributor listener, including loopback access.
- [x] Fail closed before gateway start, invitation creation, sensitive export/source inclusion, and pull-request merge when required evidence cannot be recorded.

### Owner workflow and instrumentation

- [x] Add paginated owner query, integrity, and bounded JSON export APIs.
- [x] Add request IDs to ForgeTrace responses and event context.
- [x] Add a Security viewer with integrity status, filters, repository context, event details, and export.
- [x] Instrument gateway lifecycle, denials, throttling, invitations, exports, pull-request review/merge/closure, Doctor, startup recovery, transaction recovery, and integrity failures.

### Validation

- [x] Pass 87 Python unit/integration tests with 76% application coverage.
- [x] Reach 84% line coverage for `forgetrace/security_events.py`.
- [x] Pass the five accepted v0.4.0 Chromium workflows plus the new real security-ledger owner workflow.
- [x] Confirm the live collaboration browser environment skip and passing equivalent HTTP integration coverage.
- [x] Preserve the unexecuted physical Windows native-picker acceptance gate.

---

## 46. Completed implementation checkpoint — v0.4.2 Validated Registry Recovery

### Recovery authority and locking

- [x] Add a dedicated registry recovery service rather than overloading registry JSON import.
- [x] Serialize registry connections, backups, retention pruning, preview, restore, rollback, and startup recovery with one OS-backed `registry.lock`.
- [x] Keep registry recovery state in platform application data outside the extracted package.
- [x] Keep repository workspaces, repository-local `.forgetrace` history, quarantine, and `security-events.sqlite3` outside the registry replacement boundary.

### Preview and staged preparation

- [x] Restrict restore sources to direct-child ForgeTrace backup names and reject traversal, symlinks, missing/unreadable files, corrupt SQLite, foreign-key errors, and unsupported newer schemas.
- [x] Open the source read-only, copy it to private staging, verify copied SHA-256, migrate supported older schemas only in staging, and verify all required tables afterward.
- [x] Produce deterministic logical registry digests that exclude migration bookkeeping timestamps but include all user-authoritative registry state.
- [x] Bind preview IDs to backup name/SHA-256, selected mode, live digest, prepared digest, and application schema so stale previews fail closed.
- [x] Show repository additions/removals/changes, path conflicts, path availability, schema migration, warnings, and exact Merge/Replace semantics in the owner UI.

### Installation, journals, and rollback

- [x] Create and verify an exact pre-restore SQLite backup before mutation.
- [x] Write fsynced atomic journals with prepared/installing/installed/completed/failure/rollback transitions.
- [x] Implement exact Replace installation with post-install logical digest verification.
- [x] Implement additive Merge that preserves live settings, paths, filters, collections, and active selection while adding missing registrations and unioning organization memberships.
- [x] Automatically restore the pre-restore registry after an installation failure.
- [x] Recover interrupted journals at startup by abandoning pre-install work, finalizing a fully installed target, or rolling back ambiguous mutation.
- [x] Permit explicit rollback only while the current digest still equals the recorded post-restore digest.
- [x] Pin pre-restore backups against normal retention pruning while rollback authority is available.
- [x] Serialize pruning with restore so an eligible backup cannot disappear during validation/staging.

### Security and owner workflow

- [x] Require a healthy durable security-event authorization before registry restore or rollback.
- [x] Deny all backup recovery APIs to the contributor listener, including loopback access.
- [x] Split recovery POST routing into a bounded handler and preserve the v0.4.0 route-complexity gate.
- [x] Add owner backup selection, preview, confirmation, completion, history, and rollback controls.

### Validation

- [x] Pass 100 Python unit/integration tests with 77% application coverage.
- [x] Reach 84% line coverage for `forgetrace/registry_restore.py`.
- [x] Prove real OS-process serialization for repository writes, security-event appends, and registry operations.
- [x] Pass seven applicable Chromium workflows, including a real 2→1 replace and verified rollback to two repositories.
- [x] Preserve the managed-Chromium collaboration navigation skip with passing equivalent HTTP coverage.
- [x] Preserve the unexecuted physical Windows native-picker acceptance gate.

---

## 47. Next best move — v0.4.3 service-enforced read-only repositories

1. Add an explicit registry-level read-only setting with a migration and clear separation from detected filesystem writability.
2. Centralize authoritative `require_writable_repository()` enforcement in the repository service before every mutation, not only in UI controls.
3. Enforce read-only mode across file/folder writes, rename/delete, snapshots/restores, imports/jobs, settings that write repository metadata, pull-request merge, and any future adapter.
4. Keep safe browsing, file reads, diffs, exports, source downloads, Doctor checks, and security/registry evidence available where no repository mutation occurs.
5. Add owner UI state, warnings, enable/disable confirmation, capability explanation, and disabled controls without relying on disabled controls as the security boundary.
6. Audit read-only mode changes and blocked mutation attempts without logging file contents or secrets.
7. Add service/API/job/collaboration/restart/browser tests proving no mutation path bypasses the setting and that a failed metadata update does not leave the mode ambiguous.
8. Then proceed to inline review conversations, quarantine-only visual conflict resolution, unified health dashboard, security-event retention/anchoring, Windows physical acceptance, and finally a read-only Git status/diff adapter.

Do not begin Git credential management, remote hosting, or direct public-internet owner access before identity, TLS, permission, audit-retention, and adversarial networking gates are designed and tested.

## 48. Completed implementation checkpoint — v0.4.3 Service-Enforced Read-Only Repositories

### Accepted implementation

- [x] Migrate the application registry to schema 4 with a validated `read_write` / `read_only` access-mode field.
- [x] Migrate initialized repository metadata to schema 3 with an embedded access-mode copy.
- [x] Compute effective authority from both copies and fail closed unless both explicitly agree on read-write.
- [x] Check effective mode under the repository OS lock immediately before every mutation.
- [x] Preserve fail-closed interruption semantics by ordering read-only registry-first and read-write embedded-first.
- [x] Centrally guard direct metadata persistence and snapshot-object materialization, not only public routes.
- [x] Reject file/folder writes, rename/delete, upload/import/job apply, snapshot creation/restore, embedded settings, managed discard, and pull-request merge.
- [x] Keep safe reads, verification, export preview/export, contribution submission, quarantine review/closure, and owner mode recovery available.
- [x] Implement read-only export without repository metadata/object/cache writes while verifying streamed source bytes under lock.
- [x] Preserve live access authority during registry Merge and restore backed-up authority during Replace/rollback/startup recovery.
- [x] Reconcile online embedded access-mode metadata after registry installation; leave unavailable repositories fail-closed.
- [x] Add owner-only fail-closed security-ledger authorization for mode changes and deny the contributor listener.
- [x] Add a persistent owner UI banner, lock labels, read-only editor, disabled mutation controls, and explicit mode switch.

### Completion evidence

- [x] 110/110 Python unit/integration tests passed.
- [x] 10/10 focused v0.4.3 read-only tests passed.
- [x] 79% application line coverage; repository 82%; registry recovery/security ledger 84%; native picker 87%.
- [x] Eight applicable real Chromium workflows passed.
- [x] Real owner workflow verified mode transition, safe reads, HTTP 423 mutation rejection, return to read-write, and actual editor save.
- [x] Real second-process test proved a stale service observes the mode change.
- [x] Contributor collaboration navigation remains an environment-policy skip only; equivalent HTTP integration tests pass.
- [x] MIT license and Rooke Poole creator credit preserved.

## 49. Next best move — v0.4.4 inline review conversations

1. Add persistent pull-request review threads and comments under application-data collaboration storage, never repository content.
2. Bind each thread/comment to repository ID, pull-request ID, revision, file path, optional line/range context, author role, and immutable creation metadata.
3. Keep contributor operations token-scoped and deny all owner-only repository, ledger, registry-recovery, and access-mode authority.
4. Allow owner resolution/reopening and changes-requested workflows without mutating the live repository.
5. Preserve quarantine-only review: submitted code is never executed and comments cannot introduce filesystem paths outside validated PR context.
6. Add bounded pagination, retention behavior, request IDs, security events for sensitive moderation actions, and restart persistence.
7. Add unit/integration tests for authorization, revision drift, path validation, concurrency, persistence, and gateway isolation.
8. Add a real owner/contributor browser workflow that exchanges comments, resolves a thread, requests changes, submits a revision, and proves merge remains subject to conflicts and repository access mode.
9. Then proceed to quarantine-only visual conflict resolution, a unified health dashboard, security-event retention/anchoring, Windows physical acceptance, and finally a narrow read-only Git status/diff adapter.

---

## 50. Completed implementation checkpoint — v0.4.4 Inline Review Conversations

### Revision-bound storage

- [x] Migrate collaboration storage to schema 4 with submitted revisions, review threads, comments, and thread-state events.
- [x] Preserve immutable submitted-revision manifests and changed bytes under application data, outside the live repository.
- [x] Verify review context by manifest path, size, and SHA-256 before presenting bounded line text.
- [x] Backfill legacy open pull requests without inventing unavailable bytes or modifying repository content.
- [x] Keep old threads permanently bound to their original revision and mark them outdated after resubmission.

### Authorization and workflow

- [x] Allow owner and invitation-scoped contributors to create threads and append replies.
- [x] Reserve request-changes, resolve, and reopen authority for the owner listener.
- [x] Require expected PR revision for thread creation and expected thread version for replies/moderation.
- [x] Block approval and merge while current-revision threads remain unresolved.
- [x] Invalidate stale approval when a contributor opens a new current-revision thread.
- [x] Keep historical unresolved threads visible without blocking a newer revision.
- [x] Preserve review availability for read-only repositories while leaving merge centrally blocked.

### Security, safety, and retention

- [x] Reject traversal, absolute paths, `.forgetrace`, `.git`, paths outside the submitted manifest, invalid ranges, and spans over 200 lines.
- [x] Render all submitted context as escaped inert text and explicitly report that active content was not rendered.
- [x] Never execute submitted code and never use live repository bytes as a silent review-context fallback.
- [x] Add persistent pre-insertion limits: 500 threads per PR, 500 comments per thread, and 5,000 comments per PR.
- [x] Add bounded pagination, request IDs, storage metrics, 180-day terminal retention, and orphan revision cleanup.
- [x] Record thread/reply evidence and require fail-closed ledger authorization before changes-requested and resolve/reopen state changes.
- [x] Keep raw invitation tokens out of collaboration evidence.

### Owner and contributor interfaces

- [x] Add path/line thread creation, replies, role labels, immutable context, resolution state, current/outdated revision badges, and refresh behavior to both review surfaces.
- [x] Add owner resolution/reopen and request-changes controls.
- [x] Disable approval/merge presentation while current unresolved threads exist without relying on UI state as the backend boundary.

### Completion evidence

- [x] 122/122 Python unit/integration tests passed.
- [x] 12/12 focused v0.4.4 review-conversation tests passed.
- [x] 79% application coverage; review conversations 85%; collaboration 81%; repository 82%; registry 77%; recovery/security ledger 84%; native picker 87%.
- [x] Nine applicable Chromium workflows passed.
- [x] Real owner/contributor workflow exchanged comments, proved active-content safety, resolved a thread, requested changes, submitted a newer revision, verified outdated context, approved, merged, checked disk bytes, and checked security events.
- [x] The separate collaboration navigation test remains only an environment-policy skip; equivalent real HTTP isolation tests pass.
- [x] MIT license and Rooke Poole creator credit remain unchanged.

## 51. Next best move — v0.4.5 quarantine-only visual conflict resolution

1. Build conflict views exclusively from immutable base evidence, the current repository read view, and quarantined submitted revision bytes.
2. Never execute submitted code or write temporary resolution bytes into the live repository.
3. Preserve all original conflict inputs and record explicit per-file owner decisions: current, incoming, manual resolved text, or defer.
4. Treat binary and oversized files as non-inline choices with hashes and metadata rather than unsafe rendering.
5. Bind a resolution draft to repository ID, PR ID, submitted revision, current repository digest, path, and request ID; stale repository or PR state invalidates the draft.
6. Re-run conflict detection, access-mode authorization, snapshot/object verification, and unresolved-thread gates under the repository lock immediately before merge.
7. Store manual resolution drafts in quarantine-side application data with bounded size, optimistic concurrency, retention, and owner-only authority.
8. Add fail-closed security evidence for resolution authorization and final merge, without storing file bodies in the ledger.
9. Add service/API/browser tests for text conflicts, binary conflicts, stale drafts, read-only mode, revision drift, path containment, rollback, and gateway isolation.
10. After that, proceed to a unified health dashboard, security-event retention/anchoring, Windows physical acceptance/release automation, and a narrow read-only Git status/diff adapter.

Do not add direct public-internet owner access, Git credentials, remote hosting, or submission execution before identity, TLS, permission, evidence-retention, and adversarial networking gates are designed and validated.

## 52. Completed implementation checkpoint — v0.4.5 Quarantine-Only Visual Conflict Resolution

### Immutable evidence and storage

- [x] Migrate collaboration storage to schema 5 with conflict-resolution drafts and lifecycle events.
- [x] Preserve immutable submitted-revision bytes and available base snapshots outside the live repository.
- [x] Capture verified Base/Current/Submitted evidence under application data while holding the repository lock.
- [x] Verify manifest/file containment, regular-file status, size, and SHA-256 before display, confirmation, and merge.
- [x] Fail closed when base/current/incoming/resolved evidence is missing, unreadable, symlinked, malformed, or changed.
- [x] Enforce 1,000 drafts and 4 GiB evidence per PR plus a 16 MiB free-space reserve and 180-day terminal retention.

### Owner authority and workflow

- [x] Add owner-only prepare/list/get/save/confirm conflict-resolution APIs.
- [x] Add explicit current, incoming, manual text, and delete decisions.
- [x] Bound manual text to valid inline UTF-8, 512 KiB, and 20,000 lines; keep binary/oversized files as hash/metadata choices.
- [x] Add optimistic draft versions, request IDs, stale lifecycle, applied historical evidence, and security events without bodies.
- [x] Add a three-column escaped inert Base/Current/Submitted owner UI with save, confirm, refresh, and stale visibility.
- [x] Deny all conflict-resolution authority to the contributor listener.

### Merge integrity

- [x] Bind drafts to repository ID, PR ID, revision, path, repository digest, conflict-set digest, access mode, and unresolved-thread digest/count.
- [x] Require confirmed current drafts for every conflict before approval or merge.
- [x] Recompute every binding and verify all evidence under the repository lock immediately before merge.
- [x] Merge non-conflicting files from immutable submitted-revision copies rather than mutable working quarantine.
- [x] Apply conflicting files only from verified confirmed resolution results.
- [x] Preserve service-enforced read-only rejection, security-ledger fail-closed authorization, transaction journaling, and rollback.

### Completion evidence

- [x] 135/135 Python unit/integration tests passed.
- [x] 13/13 focused v0.4.5 conflict-resolution tests passed.
- [x] 80% application coverage; conflict resolution 86%; collaboration 85%; repository 83%; registry/recovery/security/native-picker coverage recorded.
- [x] Ten applicable Chromium workflows passed.
- [x] Conflict-resolution browser workflow passed three additional consecutive runs.
- [x] Real owner workflow proved stale-draft HTTP 409 before repository mutation and successful regenerated transactional merge.
- [x] The separate collaboration navigation test remains only an environment-policy skip; equivalent real HTTP isolation tests pass.
- [x] MIT license and Rooke Poole creator credit remain unchanged.

## 53. Next best move — v0.4.6 unified health dashboard

1. Compose existing Doctor, object verification, pending transaction, registry recovery, security-ledger integrity, access-mode, collaboration storage, review, and conflict-evidence status into one owner-only read-first model.
2. Reuse existing service checks and preserve their locks/fail-closed behavior; do not invent weaker duplicate checks.
3. Separate assessment from repair. Any repair must call an existing authority with explicit confirmation and appropriate security evidence.
4. Expose severity, evidence timestamp, request ID, affected identifiers, and a precise next action for every finding.
5. Bound or job-manage expensive scans and distinguish cached/sampled/complete verification honestly.
6. Deny all health and repair routes to the contributor gateway.
7. Add API, corruption, offline, partial-result, bounded-scan, repair-isolation, export, and real owner-browser tests.
8. After that, proceed to security-event retention/anchoring, Windows physical acceptance/release automation, and a narrow read-only Git status/diff adapter.

Do not add direct public-internet owner access, Git credentials, remote hosting, or submission execution before identity, TLS, permission, evidence-retention, and adversarial networking gates are designed and validated.



## 54. Completed implementation checkpoint — v0.4.6 Unified Health Dashboard

### Read-first health model

- [x] Add one owner-only health service that composes existing registry, repository, recovery, security-ledger, access-mode, collaboration, review, conflict-evidence, storage, and runtime checks.
- [x] Keep assessment separate from repair; generating a report does not recover journals, restore a registry, clean collaboration data, refresh caches, or mutate repository content.
- [x] Add System, Registry, Repositories, Recovery, Security, Access, Collaboration, and Storage sections with severity, evidence time, request ID, identifiers, next step, explicit limits, and completion state.
- [x] Add repository-scoped, bounded standard, and maximum-scope reports; disclose every truncation as partial rather than healthy-complete.
- [x] Add non-mutating transaction-journal, hash-index, snapshot-object, lock, review-revision, conflict-evidence, orphan, and storage inspection paths.
- [x] Probe advisory locks without creating, touching, or rewriting lock files.

### Durable evidence and owner workflow

- [x] Store canonical SHA-256 health reports under application-data `health-reports/`, outside the extracted package and repository trees.
- [x] Cap retained report history at 100 and revalidate regular-file status, format, and report hash on list/detail/export.
- [x] Add owner-only report generation, history, detail, and JSON export APIs and a real Health interface with drill-down and precise next actions.
- [x] Deny all health, report, export, and repair routes to the contributor listener.
- [x] Keep a damaged security ledger visible as a critical finding while allowing read-only report generation to remain available.
- [x] Require a healthy ledger authorization event before the existing HTTP/UI Doctor repair authority begins; do not add a new repair authority.

### Completion evidence

- [x] 145/145 Python unit/integration tests passed.
- [x] 10/10 focused v0.4.6 health-dashboard tests passed.
- [x] 80% application coverage; health dashboard 80%; collaboration 84%; conflict resolution 84%; repository 82%; registry 78%; registry recovery/security ledger 84%; native picker 87%.
- [x] Eleven applicable Chromium workflows passed.
- [x] Health-dashboard browser workflow passed three additional consecutive runs.
- [x] Real owner workflow generated and exported durable evidence, surfaced real metadata drift, invoked only the existing confirmed Doctor repair, regenerated clean evidence, and verified security events.
- [x] The separate direct-localhost collaboration navigation script remains an environment-policy skip; equivalent HTTP and contributor-isolation coverage passes.
- [x] MIT license and Rooke Poole creator credit remain unchanged.

## 55. Next best move — v0.4.7 security-event retention, segmented rotation, and optional external anchoring

1. Preserve the current append-only event semantics while introducing explicit bounded retention segments rather than deleting rows from the active ledger.
2. Seal each completed segment with canonical metadata, first/last sequence, previous-segment hash, final event hash, byte count, event count, and SHA-256 of the exported segment artifact.
3. Keep one monotonic logical chain across active and sealed segments; startup and owner verification must detect missing, reordered, truncated, altered, or substituted segment files.
4. Make rotation an owner-only, separately confirmed protected action that fails closed before changing active-ledger state when required evidence cannot be recorded.
5. Provide configurable local retention by age, event count, and storage budget with conservative defaults, dry-run preview, protected minimum history, and rollback-safe rotation journals.
6. Support optional owner-selected external hash anchoring as exported digest receipts only; do not require or silently contact any cloud service.
7. Extend the unified Health dashboard with active/segment integrity, retention pressure, unanchored-segment, missing-receipt, and rotation-journal findings.
8. Keep contributors unable to list, inspect, rotate, export, or anchor security evidence.
9. Add corruption, crash-recovery, cross-process, quota, rollback, export/import, health integration, and real owner-browser tests.
10. After that, execute the physical Windows native-picker release checklist and release automation, then consider a narrow read-only Git status/diff adapter.

Do not add direct public-internet owner access, Git credentials, remote hosting, submission execution, or automatic third-party uploads before identity, TLS, permissions, and adversarial networking gates are separately designed and validated.

## 56. Completed implementation checkpoint — v0.4.7 Segmented Security Event Retention and Owner-Controlled Anchoring

### Logical history and verification

- [x] Preserve global monotonic event sequence and event hashes while moving verified active prefixes into canonical sealed segment files.
- [x] Bind every segment to the previous full-file segment hash and every active database to the final retained segment and retention-root hash.
- [x] Verify retention checkpoint, all retained segments, active immutable metadata, SQLite integrity/triggers, and every active event before protected authorization.
- [x] Detect missing, reordered, substituted, truncated, altered, schema-invalid, or path-invalid evidence.

### Rotation, retention, and recovery

- [x] Add owner-only exact rotation previews bound to active digest, chain head, policy hash, event IDs, and pruning set.
- [x] Serialize preview/rotation/policy/anchor operations with the OS-backed security-history lock.
- [x] Stage canonical segment, retention root, rebuilt active database, exact backup, and pruned-segment backups before installation.
- [x] Add fsynced hash-protected journals, post-install full-chain verification, automatic rollback, and startup recovery.
- [x] Confine recovery paths to rotation storage and block another preview for any incomplete or unreadable journal.
- [x] Bound terminal completed/rolled-back journal history at 100 without hiding incomplete evidence.
- [x] Add whole-segment retention by age, event count, and storage budget while preserving minimum event and age windows.
- [x] Add canonical retention-root checkpoints for locally deleted prefixes without claiming deleted event bodies remain recoverable.

### Owner workflow, anchoring, and Health

- [x] Add owner UI/API for policy, segment inventory, preview, execute, history, chain-head digest export, and receipt recording.
- [x] Make anchoring offline and owner-controlled with no automatic third-party network call.
- [x] Verify request/receipt hashes and digest binding while keeping `externalPublicationVerified` false.
- [x] Add Health findings for policy faults, active/segment corruption, incomplete journals, retention pressure, unanchored segments, and invalid/missing receipts.
- [x] Deny all security-history routes to the contributor listener.

### Completion evidence

- [x] 160/160 Python unit/integration tests passed.
- [x] 15/15 focused v0.4.7 tests passed.
- [x] 81% application coverage; segmented security history 87%; repository 82%; registry 78%; registry recovery/conflict/collaboration/security-adjacent modules recorded.
- [x] Twelve applicable Chromium workflows passed.
- [x] Real owner workflow previewed/sealed a prefix, queried full history, exported a digest, recorded a matching receipt, and preserved the explicit no-independent-publication claim.
- [x] The separate direct-localhost collaboration navigation script remains an environment-policy skip; equivalent HTTP isolation coverage passes.
- [x] MIT license and Rooke Poole creator credit remain unchanged.

## 57. Next best move — v0.4.8 Git Intelligence and Branch Explorer

This is the first deliberate step toward more GitHub-like functionality. It must improve repository understanding without introducing credential, network, hook, or `.git` mutation risk.

1. Detect local Git worktrees, linked worktrees, bare repositories, submodules, and non-Git folders without walking outside the registered repository boundary.
2. Build one bounded subprocess authority with explicit Git executable discovery, sanitized environment, timeouts, output limits, `--no-pager`, disabled terminal prompting, and no credential-helper invocation.
3. Add owner-only read views for:
   - working-tree status
   - staged and unstaged changes
   - bounded inert text diffs and binary metadata
   - commit graph/log and commit detail
   - current branch or detached HEAD
   - local branches and upstream names
   - tags
   - sanitized remote names and URLs
4. Do not stage, commit, reset, checkout, switch, branch, tag, fetch, pull, push, clone, invoke hooks, or write `.git`.
5. Deny every Git intelligence route to the contributor gateway.
6. Integrate Git health/status findings read-only without adding a Git repair path.
7. Add tests for absent Git, corrupt metadata, linked worktrees, large/binary diffs, unusual paths/encodings, timeout/output caps, URL secret redaction, contributor denial, and a real temporary-Git browser workflow.
8. Keep physical Windows native-picker acceptance as a parallel release gate.

## 58. Ordered GitHub-like feature track after v0.4.8

### v0.4.9 — Local Issues, labels, milestones, and discussions

- Repository-scoped application-data issues with title/body/status/assignees only after identity semantics are defined for local owners and invite contributors.
- Labels, milestones, linked PRs/commits/snapshots, comments, mentions, search, filters, activity, optimistic concurrency, retention, and security evidence.
- No live repository mutation required.

### v0.5.0 — Project boards and roadmaps

- Kanban/table/roadmap views over issues, PRs, milestones, releases, and repository work.
- Durable ordering, custom fields, filters, saved views, dependency links, and export.

### v0.5.1 — Releases and verified artifacts

- Release notes, checksummed local artifacts, snapshot/commit references, downloadable bundles, retention, and provenance.
- Git tags only after the transactional Git-write authority below is accepted.

### v0.5.2 — Transactional local Git writes

- First accepted slice: selected-file stage, staged-tree commit, local branch creation, and lightweight tag creation through a separate authority with preflight, exact index/ref/reflog evidence, hook suppression, read-only enforcement, lock ordering, rollback/recovery, and adversarial tests. Switch and merge require later explicit design and acceptance.
- Never shell through user-controlled strings.

### Remote collaboration gate

Persistent identities, roles, sessions, MFA, TLS lifecycle, credential isolation, protocol limits, rate limits, and adversarial networking must be accepted before remote clone/fetch/pull/push, hosted repositories, public discovery, or direct public-internet exposure.


## 58. Completed implementation checkpoint — v0.4.8 Git Intelligence and Branch Explorer

### Read-only Git authority

- [x] Add a dedicated Git intelligence service rather than invoking Git from route handlers.
- [x] Detect only a repository-root `.git` marker and never discover a parent repository implicitly.
- [x] Report branch/detached-HEAD, upstream, ahead/behind, staged, unstaged, and untracked state.
- [x] Provide bounded working-tree, staged, and full-object commit diffs with binary suppression and explicit truncation.
- [x] Provide bounded commit history/detail, local branches, tags, and credential-sanitized remotes.
- [x] Keep Git intelligence live/read-only; do not create durable repository snapshots or mutate security history for ordinary reads.

### Git subprocess and layout hardening

- [x] Use an absolute Git executable with no shell and bounded timeout/stdout/stderr.
- [x] Disable prompts, credential helpers, askpass, hooks, fsmonitor, external diff/textconv, pagers, submodule recursion, global/system configuration, and lazy partial-clone fetching.
- [x] Reject external worktree administrative paths, symlinked/special Git metadata, config includes, object alternates, and path escapes.
- [x] Validate repository-relative paths and require full object IDs for commit-specific inspection.
- [x] Sanitize control characters and remote credentials while rendering all content as inert escaped text.

### Owner UI, API, Health, and isolation

- [x] Add owner-only overview, diff, and commit-detail GET routes.
- [x] Add an owner Git tab with status, changed paths, diffs, branches, tags, history, and sanitized remotes.
- [x] Add a bounded read-only Git section to Health without any repair authority.
- [x] Deny all Git routes on the contributor listener.
- [x] Preserve repository/read-only/registry/recovery/security/collaboration boundaries unchanged.

### Completion evidence

- [x] 171/171 Python unit/integration tests passed.
- [x] 11/11 focused v0.4.8 Git-intelligence tests passed.
- [x] 81% application coverage and 83% Git-intelligence coverage.
- [x] Thirteen applicable Chromium workflows passed.
- [x] The Git browser workflow passed three additional consecutive runs.
- [x] 57 Python source files compiled and both JavaScript bundles passed syntax validation.

## 59. Completed implementation checkpoint — v0.4.9 Local Issues, Labels, Milestones, and Discussions

ForgeTrace now has a durable repository-scoped project-coordination layer without new repository or Git mutation authority.

### Required trust model

- [x] Store project coordination in a dedicated application-data SQLite database outside repositories and the package.
- [x] Scope every label, milestone, issue, discussion, and comment by stable repository ID.
- [x] Use optimistic versions, an OS-backed cross-process lock, bounded bodies/pages/counts, transactional schema migration, quotas, soft deletion, and 180-day cleanup.
- [x] Escape input before a bounded inert Markdown transformation; never execute submitted HTML, SVG, JavaScript, links, images, hooks, commands, or repository code.
- [x] Keep project activity independent of `.git`, `.forgetrace`, snapshots, repository bytes, PR evidence, and merge authority.
- [x] Require healthy segmented security-history authorization before owner moderation or destructive actions.
- [x] Preserve project data through restart and registry Replace/rollback; keep it outside registry backup replacement.
- [x] Permit coordination on read-only repositories while proving repository bytes remain unchanged.

### Product scope

- [x] Owner issue creation, assignment, close/reopen, pin, lock, labels, milestones, due dates, references, comments, and soft deletion.
- [x] Repository discussions, replies, accepted answers, pinning, locking, and owner moderation.
- [x] Search/filter by state, label, milestone, assignee, text query, and repository.
- [x] Safe informational references to PRs, revisions, commits, paths, issues, and discussions without changing those authorities.
- [x] Invitation-scoped contributor issue/discussion creation and comments only when project participation is explicitly granted.
- [x] Ordinary invitations remain denied and contributors receive no owner filesystem, Git, Health, security-history, registry, moderation, approval, conflict-resolution, or merge access.
- [x] Owner and contributor browser workspaces with inert rendering and service-backed concurrency/error handling.

### Completion evidence

- [x] 185/185 Python unit/integration tests passed.
- [x] 14/14 focused v0.4.9 project-coordination tests passed.
- [x] 81% application coverage and 87% project-coordination coverage.
- [x] Fourteen applicable Chromium workflows passed.
- [x] The Project browser workflow passed on the final source; cumulative repeat-launch attempts were recorded as a host scheduling limitation rather than an application failure.
- [x] 60 Python source files compiled and both JavaScript bundles passed syntax validation.
- [x] Registry recovery independence and read-only repository byte immutability have direct tests.
- [x] MIT license and Rooke Poole creator credit remain unchanged.

## 60. Completed maintenance checkpoint — v0.4.10 Repository File Workspace and Permanent Deletion

### File workspace

- [x] Increase the desktop Files tree from 38%/300 px to 46%/380 px.
- [x] Increase desktop workspace minimum height to 720 px.
- [x] Give the virtualized file list a 500–820 px responsive desktop height.
- [x] Preserve a bounded 320–520 px mobile file-list height.
- [x] Preserve search, virtualization, editor behavior, and read-only presentation.

### Permanent managed-repository deletion

- [x] Keep unregister as a non-destructive registry-only action.
- [x] Add a separately named owner-only delete action for ForgeTrace-managed repositories.
- [x] Require exact-name confirmation and a healthy security-event ledger before HTTP mutation.
- [x] Reject external, symlinked/special, stale-path, identity-mismatched, and read-only repositories.
- [x] Support initialized, empty/uninitialized, and missing managed repository paths.
- [x] Serialize initialized/initializing repositories with the existing OS-backed repository lock and registry lock order.
- [x] Atomically move the whole directory outside discovery before committing registry removal.
- [x] Add fsynced deletion journals, staging, startup rollback/finalization, and cleanup-pending recovery.
- [x] Add durable deletion tombstones so startup discovery and Doctor cannot silently resurrect UUID-bearing leftovers.
- [x] Clear a tombstone only through explicit owner registration of the preserved repository identity.
- [x] Preserve separate security history and application data outside the extracted package.

### Completion evidence

- [x] 196 tests across the complete test inventory.
- [x] 11/11 focused repository-management tests.
- [x] Real Chromium enlarged-tree and permanent-delete workflow.
- [x] Relevant registry, recovery, read-only, collaboration, project, Git, and security regressions remain green.
- [x] MIT license and Rooke Poole creator credit preserved.

## 61. Next best move — v0.5.0 Project Boards and Roadmaps

ForgeTrace now has stable project work items but lacks a visual planning layer. The next increment should compose accepted issues, discussions, milestones, and pull requests into local boards without introducing automation, repository writes, or remote hosting.

### Required trust model

- [ ] Store boards, views, columns, cards, ordering, custom fields, dependencies, and saved filters in application data.
- [ ] Scope all board objects by repository ID; cross-repository portfolio boards remain a later explicit design decision.
- [ ] Use one cross-process board authority, optimistic versions, transactional migration, deterministic ordering/rank repair, quotas, pagination, restart persistence, and deletion/retention rules.
- [ ] Treat references to issues, discussions, milestones, PRs, releases, commits, or paths as informational; board moves must not mutate those source authorities unless a separate explicit action is designed.
- [ ] Keep all fields and exports inert; execute no webhooks, automation, commands, hooks, or repository content.
- [ ] Preserve read-only repository behavior and registry-recovery independence.
- [ ] Keep contributor board administration denied by default; do not silently treat project participation as board-edit permission.
- [ ] Ledger-authorize destructive owner actions if the threat review determines they are security-sensitive.

### Product scope

- [ ] Kanban columns with durable card ordering and drag/drop optimistic conflicts.
- [ ] Table and roadmap/timeline views over issues and milestones.
- [ ] Custom fields with bounded types and values.
- [ ] Saved filters and views.
- [ ] Dependency and blocker links with cycle detection.
- [ ] Board/card archive and export.
- [ ] Owner browser workflow plus migration, concurrency, isolation, recovery, read-only, and quota tests.

### Ordered GitHub-like track after v0.5.0

1. Verified releases and downloadable artifact manifests.
2. Separately authorized transactional local Git staging, commits, branches, and tags.
3. Persistent identities, roles, sessions, MFA, and TLS.
4. Only then remote clone/fetch/pull/push and repository hosting.


## 61. Completed checkpoint — v0.5.0 Project Boards and Roadmaps

- [x] Application-data-only board database and cross-process lock
- [x] Kanban, table, and roadmap views
- [x] Workflow columns and optimistic ranked card movement
- [x] Custom fields and saved views
- [x] Issue/discussion dependencies and activity history
- [x] Board-specific contributor view and move permissions
- [x] Owner and contributor browser workflows
- [x] No repository or Git mutation authority

### Next move

Proceed to v0.5.1 Verified Releases and Artifacts: immutable release records, checksummed assets, release notes, provenance, retention, and owner-controlled publication/export without executing artifacts.


## v0.5.1 verified releases and artifacts requirements — Complete

- [x] Dedicated repository-scoped release database and cross-process lock in application data.
- [x] Draft and published release records with inert notes and informational tag/commit provenance.
- [x] Asset bytes stored outside repositories and Git metadata.
- [x] Size and SHA-256 verification at upload, publish, download, export, and Health inspection.
- [x] Published records and assets are immutable.
- [x] Owner-exported verified ZIP containing manifest, notes, assets, hashes, and explicit no-external-publication claim.
- [x] Explicit contributor access layered on project-participation invitations; download-only and published-only.
- [x] Asset and release quotas, safe filenames, path confinement, restart persistence, read-only compatibility, and registry-recovery independence.
- [x] Owner and contributor browser surfaces plus unit/integration and Chromium evidence.

## v0.5.2 completed checkpoint — Transactional Local Git Writes

The accepted first write slice is complete: selected-file staging, staged-tree commits, local branch creation, and lightweight local tag creation. It uses a separate owner-only authority with expiring digest-bound previews, exact typed confirmation, repository/Git lock ordering, security-ledger authorization, sealed journals and receipts, exact rollback, startup recovery, read-only/deletion-intent enforcement, native Git-operation exclusion, and hardened no-shell Git plumbing.

Switch/checkout, merge, reset, rebase, cherry-pick, revert, annotated or signed tags, signed commits, remotes, credentials, fetch, pull, push, clone, hosting, and public exposure remain outside v0.5.2. Any future expansion must be designed as a new accepted transactional slice rather than inferred from this authority.

## 62. Completed design checkpoint — v0.5.3 Transactional Switch/Checkout Contract

### Scope and authority

- [x] Restrict the first slice to switching from an attached born local branch to a different existing direct local branch.
- [x] Require a dedicated future `GitSwitchService` sharing the accepted repository/Git mutation lock order.
- [x] Preserve the v0.5.2 runtime operation set and add no execute route, UI, or command in the design package.
- [x] Exclude detached/path checkout, create-and-switch, force/discard, three-way checkout, merge, remotes, credentials, hooks, submodules, linked worktrees, sparse/split index, and ambiguous filters.

### Byte safety and recovery

- [x] Require a clean index and clean tracked worktree.
- [x] Define exact source/target manifests and exact backup of every affected tracked source path.
- [x] Define bounded direct scanning and exact backup of all untracked and ignored regular files.
- [x] Reject target collisions, case-fold collisions, protected paths, special files, unsupported tree modes, and insufficient backup space.
- [x] Capture `HEAD`, index, `logs/HEAD`, ref/reflog verification state, manifests, and backups before mutation.
- [x] Permit automatic rollback only for known pre/target/missing states; retain unknown bytes for manual inspection.
- [x] Define native-lock deferral, later-read-only recovery, deletion-intent blocking, and pending-journal deletion blocking.

### Validation and evidence

- [x] Add a machine-readable schema-1 contract and human design specification.
- [x] Add static isolation tests proving no runtime switch surface exists.
- [x] Add disposable Git probes for ref/reflog boundaries and ignored-file overwrite behavior.
- [x] Record the operator-reported v0.5.2.2 automated Windows `OK` separately from the unrecorded owner-browser checklist.
- [x] Preserve MIT license and Rooke Poole creator credit.

## 63. Next best move — v0.5.3 preflight and sealed capture planner

1. Create a dedicated `GitSwitchService` skeleton with no execute command.
2. Share the existing repository and Git mutation locks; add no weaker or parallel lock order.
3. Resolve only attached source and existing direct local target refs.
4. Prove clean index/tracked worktree and reject all unsupported Git/index/worktree features.
5. Build exact affected tracked manifests and direct untracked/ignored filesystem manifests.
6. Reject collisions and enforce 10,000 affected paths, 5,000 untracked entries, 512 MiB capture, and free-space reserve limits.
7. Persist digest-bound preview and sealed capture-plan evidence under application data.
8. Add service, corruption, drift, race, read-only, deletion-intent, contributor-denial, and Health tests.
9. Do not add `git switch`, execute API/UI, rollback mutation, merge, or remotes until the planner boundary is accepted.
