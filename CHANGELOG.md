## v0.5.3 Design Contract — Transactional Switch/Checkout (non-runtime)

## 0.5.3.0 — Switch Preflight and Sealed Capture Planner

### Added

- Added a separate internal `GitSwitchService` for read-only local-branch eligibility analysis and exact capture planning.
- Added a bounded existing-local-branch read model and a five-minute, canonical-digest sealed `switch_branch` plan requiring future confirmation text `SWITCH BRANCH`.
- Added direct filesystem scanning and ignored-path classification so target collisions cannot hide behind `git status`.
- Added exact application-data captures for `HEAD`, index, `logs/HEAD`, affected source tracked bytes, and every accepted untracked or ignored regular file.
- Added immediate and later verification of plan JSON, seal marker, capture bytes, source/target refs and trees, worktree state, and preserved local bytes.

### Hardened boundaries

- Rejects dirty tracked/index state, native locks, active merge/rebase/cherry-pick/revert/bisect state, pending Git-write recovery, deletion intents, read-only policy, linked worktrees, sparse/split index, checkout-affecting configuration or attributes, symlinks/gitlinks/reparse points, protected paths, and bounded-resource overruns.
- Rejects all case-fold spelling ambiguities across source, target, and local preserved paths, including combinations only representable on case-sensitive hosts.
- Uses repository lock → shared Git mutation lock → switch-planner lock and performs no repository mutation.

### Deliberately absent

- No `git switch` or `git checkout` command, execution method, owner API route, owner UI button, contributor authority, merge behavior, remotes, credentials, hooks, helpers, or shell execution was added.
- The accepted v0.5.2 write operation set remains exactly `stage`, `commit`, `create_branch`, and `create_tag`.

### Validation

- 277 tests across 30 isolated modules: 275 passed on Linux and 2 expected physical-Windows skips.
- 14/15 focused planner tests; 82% branch-aware coverage of `forgetrace/git_switch.py`.
- 79 Python files compiled; owner and contributor inline JavaScript parsed; inherited transactional Git-write Chromium workflow passed.

### Designed

- Fixed the future first switch slice to one owner-only operation: attached local branch to an existing direct local branch.
- Required clean staged/tracked state, exact `HEAD`/index/`logs/HEAD` evidence, affected tracked-byte backups, and bounded backups of every untracked or ignored regular file.
- Required direct filesystem collision scanning because native Git may overwrite ignored files that become tracked on the target branch.
- Defined shared Git mutation lock ordering, read-only/deletion behavior, exact preview binding, conservative known-state recovery, and permanent-deletion blocking for pending switch journals.
- Kept branch switching separate from quarantine pull-request conflict resolution.
- Defined owner API/UI/Health and Windows/failure-injection acceptance gates.

### Not implemented

- At the design-contract checkpoint, runtime remained v0.5.2.2 and no switch authority was added. The later 0.5.3.0 entry above implements only preflight/capture planning; execution remains absent.
- The repaired automated Windows runner is operator-reported `OK`; the separate owner-browser checklist remains unrecorded.

# Changelog

## 0.5.2.2 — Windows Acceptance Runner Repair

### Fixed

- Repaired the physical-Windows acceptance runner for Windows PowerShell 5.1, where Python unittest's normal verbose progress on stderr was converted into a terminating `NativeCommandError` under `$ErrorActionPreference = "Stop"` even when the test itself passed.
- Native stdout and stderr are now redirected at the process boundary with `Start-Process`, captured as evidence text, and evaluated only through the real native exit code.
- Added a compatibility entry point at `tests/run_v0521_windows_git_write_acceptance.ps1` that delegates to the repaired v0.5.2.2 runner.
- No Git-write, deletion, Security, locking, journal, rollback, recovery, or repository-policy behavior changed.

### Validation boundary

- Added four static contract tests for the repaired PowerShell gate.
- Physical Windows Git-write acceptance remains outstanding until the exact v0.5.2.2 archive produces `AUTOMATED_RESULT: OK` and the owner-browser checklist passes.


## 0.5.2.1 — Windows and Failure-Injection Hardening

### Fixed

- Prevented a Windows sharing violation during consumed-preview or terminal-journal cleanup from turning an already committed Git write into a false rollback attempt.
- Added bounded retries for critical atomic file replacement and required unlink operations used by previews, journals, captures, receipts, and rollback.
- Kept non-critical application-data cleanup failures out of the transactional commit boundary; blocked cleanup is retained as repository-scoped maintenance evidence.
- Prevented unreadable global transaction journals from producing duplicate Health findings for every registered Git repository.

### Recovery and evidence

- Added hash-sealed crash checkpoints around captures, index installation, tree/commit object creation, ref/reflog installation, exact rollback, terminal journals, and receipts.
- Added constructor-injected abrupt-stop testing that bypasses in-process rollback and proves fresh-process startup recovery from durable evidence.
- Added owner diagnostics for the last durable checkpoint, journal and receipt integrity, recovery disposition, native-lock blockers, recoverability, manual-inspection requirements, and the exact next step.
- Startup now reconstructs a missing receipt from a verified terminal committed/rolled-back journal before cleanup, while damaged or conflicting evidence remains retained and fail-closed.
- Read-only Git intelligence remains independently available when Git-write recovery evidence is degraded.

### Validation boundary

- Added `tests/test_v0521_git_write_failure_injection.py` and `tests/run_v0521_windows_git_write_acceptance.ps1`.
- Physical v0.5.2.1 Windows acceptance is not claimed until the exact packaged archive passes the automated PowerShell runner and owner-browser checklist on Windows.


## 0.5.2 — Transactional Local Git Writes

### Added

- Added a separate owner-only transactional Git-write authority for selected-file staging, staged-tree commits, local branch creation, and lightweight local tags.
- Added canonical-digest previews with a ten-minute expiry, exact typed confirmation, state revalidation, and stale-preview rejection.
- Added application-data Git-write locks, hash-sealed transaction journals, exact index/ref/reflog captures, verified terminal receipts, rollback, and startup recovery.
- Added owner Git UI controls and API endpoints for write status, preview, and execute; the contributor listener exposes no write routes.
- Added Git-write Health evidence for pending journals, verified receipts, native Git locks, supported restrictions, and startup recovery.
- Added 16 focused unit/integration tests and a real Chromium owner workflow covering stage, commit, branch, and tag.

### Security and integrity

- Repository lock order remains repository lock first, then the repository-scoped Git-write lock.
- Read-only policy, deletion intents, native Git lock files, active merge/rebase/cherry-pick/revert/bisect state, unsupported Git layouts, protected paths, external clean filters, and working-tree encoding fail closed.
- Commits use `write-tree`, `commit-tree`, and `update-ref` with explicit author/committer identity; ForgeTrace does not invoke hooks, an editor, signing, a shell, credential helpers, global/system Git configuration, network protocols, or submodule recursion.
- Branch/tag names use conservative validation and are created without changing `HEAD` or the working tree.
- Transaction journals and receipts are canonical-digest sealed; damaged evidence is retained and never silently trusted or overwritten.
- Security-ledger authorization is required before mutation and completion evidence is appended before a transaction is committed.

### Validation

- 238 Python tests discovered across 26 modules; 236 passed on Linux and 2 physical Windows-only tests skipped.
- The v0.5.1.2 Windows deletion prerequisite was operator-reported as an unskipped `OK` on Windows on 2026-07-26.
- 19 applicable Chromium workflows passed; the direct collaboration navigation script remains a documented managed-Chromium localhost-policy skip.
- `forgetrace/git_writes.py` reached 78% branch-aware focused coverage.
- Python compilation and owner/contributor JavaScript syntax validation passed.

## 0.5.1.2 — Windows Deletion Transaction and Security Viewer Repair

- Replaced the Windows parent-rename strategy with a durable application-data deletion intent and external repository-deletion guard.
- The normal repository lock is still acquired first to wait for and linearize against existing operations, but every ForgeTrace handle inside the repository is closed before the directory move.
- New ForgeTrace reads and writes fail closed with `repository_delete_in_progress` while the deletion intent exists.
- Startup recovery clears valid orphan intents, rolls back staged directories when the registry row remains, and finalizes committed deletions when it does not.
- Persistent Windows sharing conflicts use Restart Manager diagnostics to name blocking processes and PIDs when available.
- Failure remains non-destructive and returns HTTP 423 `repository_delete_path_busy`.
- Security events and segmented history now load sequentially instead of issuing concurrent full-chain scans.
- Auxiliary retention, segment, anchor, journal, or size failures return a degraded operational status and do not disconnect the primary event viewer.
- Added five platform-independent regression tests, one Windows-only physical transaction test, and a real degraded-history Chromium workflow.

# ForgeTrace Changelog

## 0.5.1.1 — Windows Permanent Repository Deletion Hotfix

- Fixed Windows `WinError 5` when permanent deletion atomically moves a managed repository while its repository lock is held.
- Windows lock handles now use `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`, preserving the OS-backed lock while permitting the parent-directory rename.
- Added bounded retries for transient Explorer/editor/antivirus sharing handles.
- Persistent sharing denial now returns HTTP 423 `repository_delete_path_busy` instead of a generic unexpected server error.
- Failure remains non-destructive: the repository stays registered, source bytes remain in place, no tombstone is retained, and the preparation journal is removed.
- Added focused regression tests and a physical Windows acceptance test/checklist.

## 0.5.1 — Verified Releases and Artifacts

- Added immutable repository-scoped release records stored entirely in application data.
- Added draft and published states, inert release notes, informational tag/commit provenance, checksummed assets, owner export, and verified downloads.
- Published release records and assets are immutable; every asset is verified by size and SHA-256 at publish, download, export, and Health inspection.
- Added explicit contributor opt-in layered on project-participation invitations. Contributors receive download-only access to owner-enabled published releases.
- Added release asset quotas, path confinement, safe filenames, restart persistence, read-only compatibility, and registry-recovery independence.
- Added owner and contributor release workspaces, 7 focused tests, and a real two-sided Chromium workflow. Complete Python inventory: 211 tests.

## 0.5.0 — Project Boards and Roadmaps

- Added repository-scoped Kanban, table, and roadmap views stored only in application data.
- Added durable workflow columns, ranked cards, optimistic movement, custom fields, saved views, dependencies, and activity history.
- Added board-specific contributor visibility and card-movement permissions layered on explicit project-participation invitations.
- Added owner and contributor browser workspaces without repository or Git mutation authority.
- Added Project Health evidence for the board database.
- Added 8 focused board tests and a real two-sided Chromium workflow; complete Python inventory is 204 tests.

# Changelog

All notable changes to ForgeTrace are documented here.

## Unreleased

### Planned

- Project boards and roadmaps
- Windows physical acceptance and release automation
- Future transactional Git expansion only after separate acceptance: switch/merge and, much later, authenticated remote operations

## 0.4.10 — Repository File Workspace and Permanent Deletion

### Added

- Enlarged desktop repository file pane: 46% initial width, 720 px workspace minimum, and a 500–820 px responsive file-list height
- Responsive mobile file-list sizing without removing virtualization
- Owner-only permanent deletion for ForgeTrace-managed repositories
- Durable application-data deletion journals, staging, and tombstones
- Startup rollback/finalization for interrupted managed-repository deletion
- Automatic startup/Doctor suppression of tombstoned repository identities
- Explicit owner registration as the only supported tombstone-clearing restoration path
- `tests/test_v0491_repository_management.py` and `tests/browser_repository_management_test.py`

### Security and recovery

- Permanent deletion is denied for linked external repositories
- Read-only and fail-closed access-mode policy remains authoritative
- A healthy security-event ledger is required before the owner HTTP delete route begins
- Initialized repositories use the existing cross-process repository lock; empty/uninitialized managed directories use the same lock path as initialization
- The whole managed directory is atomically moved outside discovery before registry removal
- Crash journals determine rollback versus final cleanup from the committed registry state
- Missing managed paths can be tombstoned and removed from the registry without pretending files were deleted
- UUID-bearing legacy copies remain suppressed after restart and Doctor scans

### Validation

- 196 Python tests across the complete test inventory
- 11 focused repository-management tests
- Real Chromium repository-management workflow passed
- Complete coverage, static, browser-matrix, and clean-room results are recorded in the v0.4.10 handoff

## 0.4.9 — Local Issues, Labels, Milestones, and Discussions

### Added

- Dedicated repository-scoped application-data project database and cross-process lock
- Issues and discussions with comments, close/reopen, pinning, locking, assignees, due dates, and safe informational references
- Labels and milestones with optimistic versions and repository isolation
- Discussion accepted answers and owner comment moderation
- Explicit invitation-scoped project-participation permission in collaboration schema 6
- Owner Project workspace and contributor Project workspace
- Project integrity/storage section in Health
- `tests/test_v049_project_coordination.py` and `tests/browser_project_coordination_test.py`

### Security and recovery

- Bounded inert Markdown/code rendering with no active HTML, script, link, image, hook, or submitted-code execution
- Owner destructive and moderation actions require healthy tamper-evident security-history authorization
- Ordinary source-sharing invitations receive no project access
- Contributor project routes expose no labels, milestones, moderation, registry, Health, security history, Git, review approval, conflict resolution, or merge authority
- Project coordination remains independent of repository bytes, `.forgetrace`, Git metadata, and registry backup replacement
- Read-only repositories permit coordination while all repository mutation remains blocked
- Cross-process serialization, optimistic versions, quotas, pagination, soft deletion, and 180-day cleanup

### Validation

- 185/185 Python tests and 14/14 focused project-coordination tests passed
- 81% application coverage and 87% project-coordination coverage
- Fourteen applicable Chromium workflows passed; the project workflow passed on the final source in a fresh Chromium process
- 60 Python source files compiled and both JavaScript bundles passed syntax validation

## 0.4.8 — Git Intelligence and Branch Explorer

### Added

- Dedicated owner-only `GitIntelligenceService` for bounded local Git inspection
- Root-level Git detection without upward parent-repository discovery
- Branch, detached-HEAD, upstream, ahead/behind, staged, unstaged, and untracked status
- Bounded inert working-tree, staged, and commit diffs with binary suppression and explicit truncation
- Commit history, full-object commit detail, local branches, tags, and credential-sanitized remotes
- Owner Git tab with changed-path diff inspection, commit detail, refresh, and explicit read-only/no-network status
- Read-only Git findings in Health with a bounded repository scan and no repair authority
- Owner-only Git API routes and contributor-gateway denial
- `tests/test_v048_git_intelligence.py` and `tests/browser_git_intelligence_test.py`

### Security

- Git runs through an absolute executable with no shell and a sanitized subprocess environment
- Terminal prompts, credential helpers, askpass, hooks, fsmonitor, pagers, external diff drivers, text conversion, submodule recursion, lazy partial-clone fetching, global/system configuration, and network-triggering helpers are disabled
- External worktree administration, symlinked/special `.git` layouts, config includes, alternate object stores, and path-escaping metadata fail closed as unsupported
- Git inspection never stages, commits, checks out, switches branches, creates tags, changes configuration, contacts remotes, or mutates `.git`
- Repository paths and object IDs are validated; output, commit/ref counts, execution time, and diff bytes are bounded
- Remote userinfo, query strings, fragments, and token-like material are never returned to the UI
- Git content is rendered only as escaped inert text

### Validation

- 171 Python unit/integration tests passed with 81% application line coverage
- 11 focused v0.4.8 Git-intelligence tests passed
- `forgetrace/git_intelligence.py` reached 83% line coverage
- Thirteen applicable Chromium workflows passed
- The Git owner workflow passed three additional consecutive runs
- 57 Python source files compiled and both HTML JavaScript bundles passed syntax validation

## 0.4.7 — Segmented Security Event Retention and Owner-Controlled Anchoring

### Added

- Canonical sealed security-event segments with one monotonic logical chain across retained segments and the active SQLite suffix
- Hash-verified retention policy with owner UI controls for active count, segment target, retained count, age, storage, and protected minimums
- Exact preview-bound, cross-process serialized, staged, fsynced, journaled rotation with rollback and startup recovery
- Hashed retention-root checkpoints for authorized whole-segment pruning
- Owner-only segment inventory, rotation history, chain-head digest export, and receipt recording
- Health findings for segment/policy/journal/retention/anchor state
- `tests/test_v047_security_retention.py` and `tests/browser_security_retention_test.py`

### Security

- Missing, reordered, substituted, truncated, or altered segments fail full logical-chain verification
- Incomplete and unreadable journals block further rotation even when older than the 100-record display/history cap
- Journal recovery paths are confined to rotation storage
- Terminal rotation-journal evidence is bounded while incomplete evidence is never silently pruned
- Contributor listener has no security-history, policy, rotation, export, or anchor authority
- Anchor receipts remain explicitly owner-attested local evidence; ForgeTrace does not claim independent external publication

### Validation

- 160 Python unit/integration tests passed with 81% application line coverage
- 15 focused v0.4.7 security-retention tests passed
- `forgetrace/security_events.py` reached 87% coverage
- Twelve applicable Chromium workflows passed
- 54 Python files compiled and both HTML JavaScript bundles passed syntax validation

## 0.4.6 — Unified Health Dashboard

### Added

- Added an owner-only health dashboard with System, Registry, Repositories, Recovery, Security, Access, Collaboration, and Storage sections.
- Added durable request-linked health reports under application-data `health-reports/`, canonical SHA-256 report verification, bounded history, detail retrieval, and JSON export.
- Added bounded read-only inspection for repository transaction journals, incremental hash indexes, snapshot objects, immutable review revisions, conflict-resolution evidence, collaboration storage, orphan directories, registry recovery journals, access-mode authority, and advisory lock state.
- Added standard and complete scan scopes plus repository-scoped assessment without silently treating a bounded scan as complete.
- Added owner UI for report generation, section drill-down, finding evidence, history, export, and explicit Doctor repair through the existing authority.
- Added 10 focused health-dashboard tests and a real owner Chromium health/report/export/repair workflow.

### Integrity and security behavior

- Health assessment never performs repository transaction recovery, snapshot restore, registry restore, collaboration cleanup, hash-index refresh, or other repair work.
- Report generation persists only its own immutable evidence file and a best-effort security event; a damaged security ledger remains visible in a report even when that event cannot be appended.
- Report files are regular-file checked and verified against their canonical SHA-256 before list detail or export use.
- Lock probing opens only an existing advisory lock file and never creates or rewrites one.
- Standard scans are explicitly bounded; incomplete repository, object, review, conflict, orphan, and storage scans are labeled partial.
- Contributor listeners expose no health report, report export, or repair route.
- Doctor repair remains a separate existing authority, requires explicit owner confirmation in the UI, and now requires a healthy security ledger before repair begins.
- Existing cross-process locks, journals, verified objects/imports, registry recovery, read-only enforcement, review evidence, conflict evidence, and gateway isolation remain unchanged.

### Validation

- 145 Python unit/integration tests passed with 80% application line coverage.
- Ten focused v0.4.6 health-dashboard tests passed; `forgetrace/health.py` reached 80% coverage.
- Eleven applicable Chromium workflows passed; the health workflow generated, drilled into, exported, repaired, regenerated, and verified durable evidence through the real owner UI.
- The health browser workflow passed three additional consecutive runs.
- The separate live collaboration navigation script remains environment-skipped because managed Chromium blocks localhost; equivalent HTTP and contributor-isolation tests pass.
- Physical Windows native-picker acceptance remains an explicit release-machine gate.

## 0.4.5 — Quarantine-Only Visual Conflict Resolution

### Added

- Added collaboration schema 5 with durable `conflict_resolution_drafts` and `conflict_resolution_events`.
- Added immutable Base/Current/Submitted evidence under application-data `collaboration/conflict-resolutions/`.
- Added owner-only prepare, inspect, current/incoming/manual/delete decision, confirm, refresh, and stale-draft replacement APIs and UI.
- Added conservative immutable base snapshots for newly submitted revisions and verified legacy base fallbacks.
- Added per-PR evidence count/byte quotas, free-space preflight, bounded manual UTF-8 resolution, retention, metrics, and orphan cleanup.
- Added 13 focused conflict-resolution tests and a real owner Chromium resolver workflow.

### Integrity and security behavior

- Conflict drafts never write the live repository; all draft and resolved bytes remain in application data until final merge.
- Evidence manifests and every Base/Current/Submitted/resolved file are regular-file checked and verified by size and SHA-256.
- Drafts bind to repository/PR/revision/path, full repository digest, conflict-set digest, access mode, unresolved-thread gate, request IDs, and optimistic version.
- Missing base evidence, damaged files, stale bindings, stale versions, insufficient space, or quota exhaustion fail closed.
- Manual resolution is limited to valid inline text, 512 KiB, and 20,000 lines. Binary or oversized evidence uses hash/size-only choices.
- Contributors have no conflict-resolution route or authority. Read-only permits review/draft preparation but still blocks merge.
- Approval and merge require confirmed current drafts for every conflict and no unresolved current review threads.
- Final merge revalidates all authority and evidence under the repository lock, then uses the existing transaction journal and rollback path.
- Non-conflicting merge bytes come from immutable submitted-revision copies, closing mutable working-quarantine tampering as a merge input.
- Security events record metadata, hashes, sizes, decisions, and request IDs without raw tokens or file bodies.

### Validation

- 135 Python unit/integration tests passed with 80% application line coverage.
- `forgetrace/conflict_resolution.py` reached 86%, collaboration 85%, and repository 83% coverage.
- Thirteen focused v0.4.5 conflict-resolution tests passed.
- Ten applicable Chromium workflows passed; the resolver workflow passed three additional consecutive runs.
- The resolver browser test proved a confirmed draft becomes stale after unrelated repository change and merge is rejected before target mutation, then proved regenerated evidence merges successfully.
- The separate live collaboration navigation script remains environment-skipped because managed Chromium blocks localhost; equivalent HTTP and contributor-isolation tests pass.
- Physical Windows native-picker acceptance remains an explicit release-machine gate.

## 0.4.4 — Inline Review Conversations

### Added

- Added collaboration schema 4 with `pull_request_revisions`, `review_threads`, `review_comments`, and `review_thread_events`.
- Added immutable application-data review copies and canonical manifests for every newly submitted pull-request revision.
- Added owner and invitation-scoped contributor APIs for revision-bound thread creation, bounded listing, thread inspection, and replies.
- Added owner-only resolve/reopen controls and request-changes thread creation.
- Added owner and contributor inline review interfaces with escaped immutable line context and outdated-revision indicators.
- Added review storage metrics, 180-day terminal retention, orphan revision cleanup, persistent quotas, request IDs, and optimistic thread versions.
- Added 12 focused review-conversation tests and a two-sided owner/contributor real Chromium workflow.

### Integrity and security behavior

- Threads bind permanently to repository ID, pull request, submitted revision, quarantined manifest path, optional line range, author role/name, request ID, and creation metadata.
- Submitted review bytes are stored outside live repositories and verified by size and SHA-256 before context is returned.
- Historical threads never silently retarget when a contributor submits newer bytes; they remain visible as outdated evidence.
- Current-revision unresolved threads block approval and merge. A contributor opening a current-revision thread after approval invalidates that approval.
- Only owners can resolve/reopen or request changes; contributor routes remain invitation-token scoped and cannot access owner authority.
- Review context is inert escaped text with `activeContentRendered: false`; submitted HTML, SVG, JavaScript, and other code are never executed or actively rendered.
- Optimistic `expectedVersion` and `expectedPullRequestRevision` checks reject stale writes rather than overwriting newer state.
- Limits are enforced before insertion: 500 threads per PR, 500 comments per thread, 5,000 comments per PR, 8,000-character bodies, and 200-line context spans.
- Required security-ledger authorization precedes changes-requested and resolve/reopen state changes; raw invitation tokens never enter evidence.
- Read-only repositories may receive and discuss quarantined submissions, but merge remains subject to central repository access authority.

### Validation

- 122 Python unit/integration tests passed with 79% application line coverage.
- `forgetrace/review_conversations.py` reached 85% and `forgetrace/collaboration.py` reached 81% line coverage.
- Twelve focused v0.4.4 review tests passed.
- Nine applicable Chromium workflows passed, including a real owner/contributor exchange, active-content safety proof, resolution, changes requested, resubmission, outdated context, approval, merge, disk verification, and security-event verification.
- The separate live collaboration navigation script remains environment-skipped because managed Chromium blocks localhost; equivalent HTTP collaboration and contributor-isolation tests pass.
- Physical Windows native-picker acceptance remains an explicit release-machine gate.

## 0.4.3 — Service-Enforced Read-Only Repositories

### Added

- Added application registry schema 4 with `repositories.access_mode` and repository schema 3 with `repository.accessMode`.
- Added a two-copy `accessPolicy` exposing registry mode, embedded mode, embedded validity, effective mode, consistency, and writability state.
- Added an owner-only `POST /api/v1/repositories/{repositoryId}/access-mode` endpoint and visible Settings control.
- Added persistent owner UI read-only banners, lock labels, read-only editor state, and disabled mutation/merge/restore controls.
- Added focused service/API/migration/recovery/process tests and a real owner Chromium read-only workflow.

### Integrity and security behavior

- Write authority exists only when both registry and embedded mode explicitly agree on `read_write`; invalid, missing, unavailable, or mismatched authority fails closed to read-only.
- Mutation authorization is checked while the repository cross-process lock is held, so stale services cannot write after another process tightens authority.
- Tightening transitions registry-first and relaxing transitions embedded-first, ensuring an interrupted transition never creates accidental write authority.
- Central enforcement blocks file/folder writes, rename/delete, uploads, local-folder imports, import jobs, snapshot creation/restore, embedded settings, snapshot-object materialization, managed-repository discard, and pull-request merge.
- Safe browsing, raw reads, verification, source/export preview, verified export, contribution submission, quarantine review/closure, and owner mode recovery remain available.
- Read-only export streams and re-hashes live files under the repository lock without writing content objects or hash-index metadata; it aborts if source bytes change.
- Registry Merge preserves current access authority for existing repositories. Replace, rollback, and startup recovery restore backed-up authority and reconcile online embedded copies; offline failures remain read-only.
- Mode transitions require fail-closed security-ledger authorization and remain inaccessible from the contributor gateway.

### Validation

- 110 Python unit/integration tests passed with 79% application line coverage.
- `forgetrace/repository.py` reached 82%; registry recovery and security events remained at 84%; native picker remained at 87%.
- Ten focused v0.4.3 read-only tests passed, including a real second-process stale-service check.
- Eight applicable Chromium workflows passed. The new owner workflow toggles read-only, verifies safe reads/export preview and HTTP 423 write rejection, then returns to read-write and saves through the actual editor.
- The collaboration browser script remains environment-skipped because managed Chromium blocks localhost; equivalent HTTP collaboration and contributor-isolation tests pass.
- Physical Windows native-picker acceptance remains an explicit release-machine gate.

## 0.4.2 — Validated Registry Recovery

### Added

- Added a dedicated `RegistryRestoreService` for validated registry backup preview, staged preparation, merge/replace installation, post-install verification, explicit rollback, and interrupted-restore startup recovery.
- Added a registry-wide OS-backed `registry.lock` shared by normal registry connections, backup creation/retention, preview, restore, rollback, and startup recovery.
- Added durable atomic restore journals under application data with pre-restore backup identity, before/target/after logical digests, state transitions, verification evidence, and rollback authority.
- Added owner-only backup/restore APIs and a visible recovery workflow that shows repository additions/removals/changes, path conflicts, path availability, schema migration, warnings, and rollback history before mutation.
- Added additive merge semantics that preserve live repository settings, paths, filters, collections, and active selection while adding missing registrations and unioning organization memberships.
- Added exact replace semantics that install only a fully staged, migrated, integrity-checked registry database and never modify repository folders or the separate security-event ledger.

### Recovery and security behavior

- Restore requires a current preview ID bound to backup SHA-256, mode, live logical digest, prepared logical digest, and application schema.
- Staged bytes must still match the previewed SHA-256 before migration or installation; changed, corrupt, unsafe-path, or newer-schema backups are rejected before live mutation.
- Every restore creates and pins an exact pre-restore SQLite backup; normal retention cannot prune it while rollback authority remains active.
- Install failures automatically restore the pre-restore registry. Interrupted prepared/installing/installed journals are conservatively abandoned, finalized, or rolled back at startup.
- Explicit rollback refuses to erase later registry work by requiring the current logical digest to match the recorded post-restore digest.
- Required security-ledger authorization is recorded before restore or rollback; an unhealthy ledger blocks the protected action before registry mutation.
- Contributor listeners cannot preview, execute, inspect, or roll back registry recovery operations.
- Recovery route growth was split into a bounded dispatcher rather than weakening the v0.4.0 route-complexity gate.

### Validation

- 100 Python unit/integration tests passed with 77% application line coverage.
- `forgetrace/registry_restore.py` reached 84% line coverage.
- Seven applicable Chromium workflows passed, including a real owner workflow that previewed a 2→1 replacement, restored it, exposed rollback authority, and rolled back to the two-repository state.
- Cross-process tests prove repository writes, security-ledger appends, and registry operations remain serialized by real OS processes.
- The live collaboration navigation script remains environment-skipped because managed Chromium blocks localhost; equivalent HTTP collaboration integration and contributor-isolation tests pass.
- Physical Windows native-picker acceptance remains an explicit release-machine gate.

## 0.4.1 — Security Event Ledger

### Added

- Added a dedicated application-data `security-events.sqlite3` ledger, separate from the repository registry and repository-local metadata.
- Added monotonic event sequences, canonical JSON serialization, previous-event SHA-256 chaining, immutable event/schema rows, startup integrity verification, and cross-process append serialization.
- Added recursive detail sanitization and sensitive-key redaction; raw invitation tokens, credentials, cookies, sessions, passwords, and secrets are never intentionally stored.
- Added owner-only query, integrity, and JSON export routes with filters, pagination, request IDs, and a 100,000-event export ceiling.
- Added an owner Security viewer that verifies the chain, filters events, displays request/repository context, and exports filtered evidence.
- Added audit coverage for gateway lifecycle, access denials, rate limits, invitation lifecycle, sensitive exports, pull-request review/merge/closure, Doctor actions, startup recovery, transaction recovery, and integrity failures.

### Security behavior

- Gateway start, invitation creation, sensitive source/export inclusion, and pull-request merge fail closed when the ledger is unwritable or fails integrity verification.
- Invitation evidence stores only a 16-character SHA-256 fingerprint; tests scan the SQLite database and WAL artifacts to prove the raw token is absent.
- Missing immutability triggers remain a visible integrity failure after restart rather than being silently recreated.
- Export-search text is not copied into ledger evidence, preventing an arbitrary owner-entered search value from becoming persistent audit data.
- The contributor listener cannot query, inspect, or export security events, even when reached through loopback.

### Validation

- 87 Python unit/integration tests passed with 76% application line coverage.
- `forgetrace/security_events.py` reached 84% line coverage.
- Six applicable Chromium workflows passed, including a real owner workflow that generated, verified, filtered, inspected, and exported ledger evidence.
- The live collaboration navigation script remains environment-skipped because managed Chromium blocks localhost; equivalent HTTP collaboration integration and contributor-isolation tests pass.
- Physical Windows native-picker acceptance remains an explicit release-machine gate.

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
