# ForgeTrace Expansive Build Plan

**Project:** ForgeTrace  
**Creator and project lead:** Rooke Poole  
**License:** MIT  
**Document status:** Authoritative development roadmap  
**Baseline:** Working local repository v0.1.x  
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

## 3. Current baseline

The current working build already provides:

- a Python local server with no third-party runtime dependencies;
- one disk-backed workspace;
- repository initialization;
- file and folder upload;
- drag-and-drop upload;
- file browsing;
- text/source editing;
- binary storage and download;
- create, rename, and delete operations;
- automatic contribution events;
- SHA-256 content-addressed snapshot objects;
- snapshot creation and restoration;
- ZIP export with portable history;
- path traversal protection;
- a responsive browser interface.

### Current limitations

- only one workspace is active per server process;
- metadata is JSON rather than a queryable database;
- there is no global repository registry;
- there is no branch model beyond snapshots;
- no Git integration, diff engine, staging area, or remote support;
- no repository-wide search index;
- no issue tracker, review workflow, release manager, or project boards;
- no users, roles, sessions, or LAN collaboration;
- no plugin architecture;
- no packaged desktop application or automatic updates;
- limited automated test coverage;
- no crash recovery journal or structured migration framework.

The next phases should evolve this baseline instead of discarding it.

---

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

Provide four primary actions:

1. **Create repository** — create a new folder and initialize ForgeTrace metadata.
2. **Add existing folder** — register a normal folder without moving it.
3. **Add existing Git repository** — detect `.git`, preserve it, and enable Git features.
4. **Import archive** — extract a ZIP/TAR into a selected destination after preview and safety checks.

Later add:

5. **Clone Git remote** — clone with standard Git and register automatically.
6. **Discover repositories** — scan selected roots for `.git` or `.forgetrace` markers.

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
- **LAN trusted:** bind to selected interface; authentication required.
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

- [ ] Split `server.py` into repository, storage, API, export, and utility modules.
- [ ] Add typed data models and centralized validation.
- [ ] Add structured error codes.
- [ ] Replace ad hoc prints with structured logging.
- [ ] Add configuration loading and platform data directories.
- [ ] Add atomic state writes and backup copies.
- [ ] Add operation journals for upload, rename, delete, restore, and export.
- [ ] Add repository lock to prevent conflicting writers.
- [ ] Add test fixtures and unit test runner.
- [ ] Add adversarial path and archive tests.
- [ ] Add browser end-to-end smoke test.
- [ ] Add version endpoint and migration placeholder.
- [ ] Add `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, and `NOTICE.md`.

### Exit gate

- Existing single-repository workflow passes automated tests.
- Interrupted operations do not corrupt state.
- The codebase has clear module boundaries.
- A clean install/run procedure is documented for Windows, macOS, and Linux.

---

## Phase 1 — Multi-repository paths and registry

**Goal:** Manage many repositories from one ForgeTrace instance.

### Work

- [ ] Create global application data directory.
- [ ] Create SQLite registry and migration framework.
- [ ] Add repository UUIDs.
- [ ] Add create/add existing/unregister/relink workflows.
- [ ] Support repositories on different local drives.
- [ ] Support removable and network paths with offline states.
- [ ] Add repository switcher, favorites, recents, tags, and collections.
- [ ] Add duplicate path detection.
- [ ] Add embedded and external metadata modes.
- [ ] Make every API route repository-scoped.
- [ ] Make background jobs repository-scoped.
- [ ] Add repository settings and per-repository limits.
- [ ] Add multi-repository integration tests.

### Exit gate

- One process reliably manages at least 100 registered repositories.
- Switching repositories cannot mix files or history.
- Missing paths are recoverable through relink.
- Unregister and delete are clearly separate operations.

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
- [ ] Add change sets for Git ranges and snapshot comparisons.
- [ ] Add inline review comments and approval states.
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

- [ ] Add network bind configuration.
- [ ] Add users, password hashing, sessions, and recovery owner.
- [ ] Add repository roles and permissions.
- [ ] Add CSRF protection, security headers, rate limits, and audit logs.
- [ ] Add real-time events and job updates.
- [ ] Add stale-edit detection and advisory locks.
- [ ] Add invitations and account management.
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

## 30. Immediate next implementation sprint

The next sprint should be narrowly focused on **Phase 0 + the smallest vertical slice of Phase 1**.

### Sprint objective

Run one ForgeTrace server that can register, display, switch between, and operate on at least two repository paths without cross-repository state leakage.

### Ordered task list

1. [ ] Create a new development branch and tag the current working baseline.
2. [ ] Add `APP_VERSION` and schema version constants.
3. [ ] Introduce `forgetrace/` Python package structure.
4. [ ] Move current `ForgeTraceRepository` into a repository service module.
5. [ ] Add platform-specific application data directory helper.
6. [ ] Create SQLite global registry with migration `0001_repository_registry`.
7. [ ] Add repository UUID and path normalization.
8. [ ] Add `GET/POST /api/v1/repositories`.
9. [ ] Add repository-scoped state endpoint.
10. [ ] Add active-repository switcher in the UI.
11. [ ] Refactor all file/snapshot operations to accept repository context.
12. [ ] Add two-repository integration fixture.
13. [ ] Prove upload/edit/snapshot/export in repository A does not affect B.
14. [ ] Add unregister and relink; do not add destructive repository deletion yet.
15. [ ] Update migration, recovery, and test documentation.

### Sprint acceptance test

1. Start ForgeTrace once.
2. Add two existing folders on different paths.
3. Upload a file to repository A.
4. Switch to repository B and confirm it is absent.
5. Create a file and snapshot in repository B.
6. Switch to repository A and confirm its state is unchanged.
7. Stop and restart ForgeTrace.
8. Confirm both repositories remain registered and open correctly.
9. Temporarily rename/move repository B.
10. Confirm it becomes “offline,” then relink it and recover all history.

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

## 36. Next best move

Begin **Phase 0 / Sprint 1** by restructuring the backend and creating the SQLite repository registry. The first new user-visible release should do one thing exceptionally well: manage multiple real repository paths safely from a single ForgeTrace instance.
