# Changelog

All notable changes to ForgeTrace are documented here.

## 0.4.0 — Audit stabilization and transactional recovery

### Fixed

- Closed all 29 findings from the v0.3.6 comprehensive bug audit.
- Prevented restore from touching the workspace before every object passes existence, size, and SHA-256 checks.
- Added cross-process repository/application locks and transactional filesystem/metadata rollback.
- Replaced global-depth file ordering with a true depth-first parent-child tree and virtualized rendering.
- Replaced non-transactional folder copying with staged, verified, cancellable imports and explicit conflict policies.
- Made new managed-repository imports atomic and recoverable.
- Added Doctor restoration of valid `state.json.bak`, transaction recovery, and UUID-first moved-path relinking.
- Enabled folder rename/delete and corrected successful-import behavior when browser storage is unavailable.
- Added incremental file hashing, locked exports, sensitive-file previews, quarantine cleanup, HTTP timeouts/HEAD, bounded rate maps, and split route handlers.

### Validation

- 76 Python tests passed with 76% total line coverage.
- Five available Chromium workflows passed, including a real-server/real-disk black-box test.
- Windows picker automation coverage increased to 87%; a physical Windows acceptance harness is included.

## Unreleased

### Planned

- Persistent security-event audit viewer
- Inline review conversations and visual conflict resolution
- Read-only repositories and validated registry restore UI
- Narrow Git status/diff interoperability after recovery gates

## 0.3.5 — Verified native folder import

- Made native `webkitdirectory` selection the primary recursive folder workflow in Chrome and Edge.
- Prevented folder inputs from being cleared until asynchronous uploads complete.
- Added server-state verification for every expected imported file and empty folder.
- Added one automatic retry for descendant paths missing after the first transfer pass.
- Added a persistent import report showing discovered, verified, and missing paths.
- Automatically expands all imported parent folders and added Expand all / Collapse all controls.
- Added real Chromium on-disk directory-input and interrupted-upload retry tests.
- Changed launchers to open the browser only after the current package binds successfully, avoiding stale older-server confusion.

## 0.3.4 — Comprehensive recursive folder import

### Fixed

- Folder selection now recursively enumerates every descendant file instead of relying solely on a flat browser `FileList`.
- Deep files remain at their full repository-relative paths rather than disappearing or being flattened.
- Folder rows expand through every nested level after import.
- New-repository folder onboarding strips only the selected outer root; existing-repository uploads preserve it.

### Added

- Modern `showDirectoryPicker()` integration with an asynchronous directory-handle walker.
- `webkitdirectory` compatibility fallback for browsers without the modern picker.
- Empty leaf-folder preservation when directory handles expose empty directories.
- Dedicated deep-folder API fixture containing six files across six folder levels.
- Chromium validation for recursive folder upload into an existing repository and recursive creation of a new repository.

### Compatibility

- No repository schema, registry schema, snapshot format, collaboration route, or security-boundary change.
- Existing file upload, path onboarding, local forks, pull requests, upgrade recovery, and exports remain intact.

## 0.3.3 — Team onboarding and upgrade continuity

### Added

- **Fork shared link** onboarding from both the empty state and Add Repository dialog.
- Owner-only `POST /api/v1/repositories/fork` that validates a remote invite and creates a normal managed local repository.
- Streamed source downloads with same-origin redirect validation and token headers rather than token-bearing request URLs.
- Safe ZIP import that rejects traversal, absolute paths, symlinks, encrypted entries, protected metadata, excessive file counts, and oversized expansion.
- Non-secret upstream provenance in fork metadata without storing the raw invite token.
- Expandable/collapsible nested folder rows with expansion state persisted per repository.
- Automatic startup rediscovery of managed repositories from embedded UUIDs.
- Safe automatic relinking when a moved managed repository is rediscovered with the same UUID.
- Tests covering empty-install fork onboarding, live fork API, upgrade recovery, moved-path recovery, transfer ceilings, and folder-tree behavior.

### Changed

- Repository upload ceiling increased from 250 MB to 1 GB.
- Default invitation file limit increased from 25 MB to 100 MB.
- Default pull-request total increased from 100 MB to 1 GB; configurable maximum is 4 GB.
- Source and fork archive ceiling increased from 250 MB to 2 GB.
- Repository uploads, pull-request uploads, raw repository downloads, exports, source downloads, and fork imports now stream through temporary files rather than requiring a complete in-memory payload.
- Security/review hardening remains planned and is now targeted for v0.3.5 after the recursive folder-import gate.

### Compatibility and security

- Existing file upload, folder upload, absolute-path, registry, collaboration, and merge workflows remain intact.
- Raw collaboration tokens are never persisted by the fork workflow.
- ForgeTrace-native forks do not yet provide Git clone/push/fetch or automatic upstream submission.

## 0.3.2 — Repository onboarding usability

### Added

- Three explicit Add Repository choices: **Upload files**, **Upload folder**, and **Use a local path**.
- Owner-only `POST /api/v1/repositories/managed` for browser imports that cannot expose an absolute host path.
- ForgeTrace-managed repository root under platform application data.
- Cross-platform-safe managed directory naming with collision suffixes.
- File-selection summary with count, total size, representative paths, and storage explanation.
- Automatic repository-name inference from a selected folder or single file.
- Chromium coverage for individual-file import, folder import, root-folder stripping, and the retained path workflow.
- Registry and live-API tests for managed repository creation, uniqueness, identity, nested uploads, and normal disk persistence.

### Changed

- The Add Repository dialog no longer assumes every user knows an absolute filesystem path.
- Folder onboarding strips only the selected outer directory while preserving all nested repository-relative paths.
- Existing in-repository folder upload behavior is unchanged and still preserves the selected folder name.
- Partial import failures are reported after successful files remain available in the created repository.
- The security/review hardening roadmap target moves to v0.3.3; no collaboration security boundary was weakened or bypassed.

### Compatibility

- Absolute-path create and existing-folder registration remain available with the same API and UI capabilities.
- Managed repositories are ordinary local folders with embedded `.forgetrace` metadata and can be moved, unregistered, exported, and relinked.

## 0.3.1 — One-launch secure sharing

### Added

- Runtime `CollaborationGatewayManager` owned by the normal ForgeTrace process.
- Owner-only sharing status, start, and stop API endpoints.
- UI sharing status, recommended port control, detected LAN address, optional VPN/tunnel link override, and Stop Sharing control.
- Automatic gateway startup when the owner generates a token link from the Collaborate panel.
- A single Windows launcher, `START_FORGETRACE.bat`, that starts ForgeTrace and opens the owner workspace.
- A single macOS/Linux launcher, `START_FORGETRACE.sh`.
- Automated lifecycle tests for start, stop, port-change protection, token use, socket closure, and gateway route denial.
- Chromium coverage for the complete in-UI sharing/link-generation workflow.

### Changed

- Sharing now defaults to off and is controlled from the normal owner UI; a second terminal is no longer required.
- The owner listener remains loopback-only while sharing creates a separate contributor-only listener, normally on port 8766.
- Contributor route restrictions are now listener-level and apply even to loopback clients using the contributor port.
- The raw contributor base-URL field was replaced by automatic LAN detection with an optional advanced override.
- Documentation now presents one supported launch path.

### Removed

- `run_local.bat`, `run_local.sh`, `run_share.bat`, and `run_share.sh` from the user-facing package.

### Compatibility

- `python server.py share` remains as a deprecated advanced compatibility command, but is no longer required or documented as the normal workflow.

## 0.3.0 — Secure quarantined collaboration

### Added

- Dedicated `server.py share`, `run_share.bat`, and `run_share.sh` collaboration launchers.
- Repository-scoped, expiring, revocable, maximum-use invitation tokens.
- SHA-256-only token storage and fragment-based contributor links.
- Optional source-only repository ZIP download with no ForgeTrace history, registry data, or machine paths.
- Application-data quarantine for all outsider-submitted files.
- Snapshot-native pull-request drafts, changed-file uploads, requested deletions, and token-scoped draft recovery.
- Owner-side pull-request list and exact diff review interface.
- Approval, change-request, comment, revision, close, conflict, and merge states.
- Risky executable/script-file detection and separate merge confirmation.
- Baseline conflict detection plus under-lock merge-time hash revalidation.
- Safety snapshots, atomic replacement, and rollback material for local merges.
- External-contributor attribution and local-merger attribution in repository history.
- Remote route isolation, request throttling, security headers, origin checks, and active-content attachment protection.
- Source-download, service, API, route-boundary, conflict, limits, and end-to-end merge tests.

### Security

- Remote clients are blocked from registry, repository browser, file-editing, snapshot, export, settings, and owner merge APIs.
- Submitted archives are never extracted and contributor code is never executed.
- `.git`, `.forgetrace`, path traversal, excessive file counts, oversized files, and oversized pull requests are rejected.
- Direct public router port-forwarding remains unsupported; use a trusted LAN or private VPN.

### Not included

- Git branches, Git wire protocol, forks, or GitHub-compatible hosting.
- Persistent accounts, roles, sessions, MFA, or identity verification.
- Built-in TLS management, persistent security-event viewer, malware scanner, inline review threads, or visual conflict resolution.

## 0.2.1 — Registry reliability and organization

### Added

- SQLite migration `0002_registry_organization_and_limits`.
- Editable repository name, description, default contributor, and upload limit.
- Per-repository upload enforcement from 1 MB through 250 MB.
- Normalized repository tags and many-to-many collections.
- Repository-library search, tag/collection/status/pin filters, and saved filters.
- Repository capability reporting for availability, directory type, access, free space, and UNC/network classification.
- Online registry backups with bounded retention.
- Portable registry JSON export and non-destructive merge import.
- Browser library-tools interface for backup, import/export, and doctor.
- `server.py doctor`, `backup`, `registry-export`, and `registry-import` commands.
- Doctor checks for SQLite integrity, path status, embedded UUID identity, metadata readability, and registry drift.
- Doctor scan-root discovery and safe registration of unregistered embedded repositories.
- Automatic pre-import and pre-repair registry backups.
- v0.2.0 migration fixture and expanded API/CLI/Chromium tests.

### Changed

- Repository settings now synchronize with embedded `.forgetrace/state.json` metadata.
- Offline repositories reject settings edits instead of creating metadata drift.
- Upload request bodies are rejected before reading when they exceed the selected repository limit.
- Legacy unscoped API responses now carry formal deprecation headers.
- Roadmap completion state and next release target were updated.

### Not included

- External metadata mode remains reserved until identity-safe relinking is implemented.
- Doctor reports unreadable metadata backups but does not automatically replace `state.json`.
- Snapshot-object integrity and backup restoration remain v0.2.2 work.

## 0.2.0 — Multi-repository registry

### Added

- Platform-specific global application-data directory.
- SQLite repository registry with migration `0001_repository_registry`.
- Stable repository UUIDs and canonical path normalization.
- Create, add-existing, switch, favorite, offline, relink, and non-destructive unregister workflows.
- Repository-scoped `/api/v1/repositories/{id}/...` API.
- Atomic repository metadata writes and shared per-workspace locks.
- Multi-repository browser interface.
- Isolation, 100-repository, security, recovery, export-boundary, and Chromium tests.

## 0.1.0 — Working local repository baseline

### Added

- Disk-backed repository workspace and file operations.
- Contribution history and content-addressed snapshots.
- Snapshot restoration and ZIP export.
- Local Python server and responsive browser UI.
