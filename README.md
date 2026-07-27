# v0.5.3 Transactional Switch/Checkout Design Contract

## Current development checkpoint: v0.5.3.0

ForgeTrace v0.5.3.0 adds the internal **Switch Preflight and Sealed Capture Planner**. It can prove eligibility and preserve exact recovery bytes for a future existing-local-branch switch, but it cannot execute a switch and exposes no switch route or UI control.

Accepted local Git writes remain selected-file stage, staged-tree commit, local branch creation, and lightweight tag creation. Merge, checkout execution, remotes, credentials, hooks, signing, and shell execution remain absent.

This package advances ForgeTrace design without changing the v0.5.2.2 runtime. It specifies one future owner-only operation—switching between existing local branches from a clean tracked state—with exact `HEAD`/index/reflog/worktree evidence, bounded untracked and ignored byte backups, collision rejection, shared Git mutation locking, conservative recovery ownership, and explicit separation from merge and quarantine conflict resolution.

No switch/checkout route, UI control, runtime operation, or Git command is implemented here. Read `docs/TRANSACTIONAL_SWITCH_CHECKOUT_DESIGN.md`.

The repaired v0.5.2.2 automated Windows runner is operator-reported `OK`; the separate owner-browser checklist remains unrecorded.

# ForgeTrace — Local-First Repository Workspace

## v0.5.2.2 Windows Acceptance Runner Repair

v0.5.2.2 repairs only the physical-Windows acceptance tooling. Windows PowerShell 5.1 converts Python unittest's normal verbose stderr stream into `NativeCommandError` records when native output is merged with `2>&1` under `$ErrorActionPreference = "Stop"`. The previous runner could therefore terminate after printing a passing test line.

The repaired runner redirects stdout and stderr at the native-process boundary, preserves both streams in the evidence log, and accepts or rejects the gate only from the native process exit code. The original v0.5.2.1 runner filename remains as a compatibility delegate.

No ForgeTrace runtime authority changed. The accepted Git-write surface remains selected-file staging, staged-tree commits, local branch creation, and lightweight local tags with the v0.5.2.1 crash/recovery hardening intact.

Physical Windows acceptance remains outstanding. Run `tests/run_v0522_windows_git_write_acceptance.ps1` and the checklist in `tests/WINDOWS_TRANSACTIONAL_GIT_WRITES_ACCEPTANCE.md` against the exact packaged archive before marking the Windows hardening accepted.

**Created by Rooke Poole. Open source under the MIT License.**

ForgeTrace v0.5.2.2 is a local-first repository workspace for managing multiple real folders, attributable activity, verified SHA-256 snapshots, staged imports, portable exports, owner-reviewed outside contributions, tamper-evident segmented security evidence, owner-controlled anchor receipts, validated registry recovery, service-enforced read-only repositories, revision-bound review evidence, quarantine-only conflict resolution, durable Health reporting, local project coordination and boards, verified local releases, read-only Git intelligence, and narrowly authorized transactional local Git writes without requiring a cloud account.

## v0.5.2 Transactional Local Git Writes

Owners can now stage explicitly selected changed files, commit the existing staged tree, create a local branch, and create a lightweight local tag. Every write is a two-step preview/execute transaction bound to the exact repository and Git state, an expiring canonical digest, and an operation-specific typed confirmation.

The writer is separate from the read-only Git intelligence service. It acquires the normal repository lock before a repository-scoped Git lock, requires a writable repository and a healthy security ledger, captures exact rollback evidence, writes a sealed transaction journal, verifies the post-state, emits a digest-verified receipt, and recovers incomplete work at startup. Native Git locks and merge/rebase/cherry-pick/revert/bisect state block ForgeTrace rather than being bypassed.

Commits use Git plumbing with explicit author and committer identity. ForgeTrace does not run repository hooks, an editor, signing, a shell, credential helpers, global/system Git configuration, network protocols, external clean filters, or submodule recursion. v0.5.2 does not switch or checkout branches, merge, reset, rebase, cherry-pick, revert, create annotated/signed tags, create signed commits, fetch, pull, push, clone, host repositories, or contact remotes.

The v0.5.1.2 physical Windows deletion transaction prerequisite was reported by the Windows operator as an unskipped `OK` on 2026-07-26. Linux validation still records the physical Windows tests as platform skips rather than claiming independent Win32 execution.

## v0.5.1.2 Windows deletion transaction and Security viewer repair

This maintenance release supersedes v0.5.1.1 after physical Windows testing showed that delete-sharing on the repository lock file was not sufficient on every machine. ForgeTrace now installs a durable application-data deletion intent while holding the normal repository lock, releases every ForgeTrace handle inside the repository, and performs the parent-directory move under an external repository-deletion guard plus the registry operation lock. New ForgeTrace access fails closed while the intent exists. Persistent third-party blockers are queried through Windows Restart Manager and are named by process and PID when Windows provides that evidence.

The Security viewer also loads the primary event list before segmented retention/anchor history, preventing two full-chain requests from contending on the same lock. Auxiliary history failure is shown as a degraded panel and no longer prevents primary event inspection.

Windows lock handles still request delete sharing, but v0.5.1.2 no longer relies on an in-repository handle during the parent move. The normal repository lock is acquired first for ordering, the durable external intent/guard takes over, and every ForgeTrace handle inside the repository is closed before rename. Transient third-party handles are retried; a persistent conflict returns `repository_delete_path_busy` with HTTP 423 and leaves the repository registered and untouched.

The included Windows-only transaction test was operator-reported as an unskipped `OK` on Windows on 2026-07-26. Linux validation cannot independently prove Win32 rename semantics and continues to report that test as a platform skip.

## v0.4.10 Repository File Workspace and Permanent Deletion

The repository file workspace now gives the file tree substantially more usable width and vertical space on desktop while preserving a practical mobile layout and the existing virtualized tree behavior.

Owners can now permanently delete a **ForgeTrace-managed** repository from Settings or its offline card. Permanent deletion removes the managed repository directory, embedded `.forgetrace` history, and registry entry. ForgeTrace first moves the directory outside every discovery root, writes a durable recovery journal and deletion tombstone, commits the registry change under the existing repository/registry locks, and then removes staged bytes. Interrupted operations roll back or finish on startup according to the committed registry state.

Deletion remains deliberately unavailable for linked external repositories, which can only be unregistered. Read-only repositories cannot be deleted until deliberately returned to read-write mode. Missing or manually emptied managed directories can still be permanently removed, and the tombstone prevents UUID-bearing leftovers from being silently recovered by startup discovery or Doctor. Explicit owner registration of a preserved copy is required to restore that repository identity.

## v0.5.1 Verified Releases and Artifacts

ForgeTrace stores repository-scoped draft and published release records entirely in application data. Published metadata and asset rows are immutable, downloads and ZIP exports reverify size and SHA-256, contributor downloads require explicit opt-in, and ForgeTrace never executes artifacts or claims external publication.

## v0.4.9 Local Issues, Labels, Milestones, and Discussions

ForgeTrace now includes a repository-scoped **Project** workspace stored entirely in application data. Owners can create and manage issues, discussions, labels, milestones, assignments, due dates, comments, accepted answers, locking, pinning, and moderation. Contributors can participate only when an invitation explicitly grants `projectParticipation`; ordinary source-sharing invitations do not imply project access.

Project content uses bounded inert Markdown rendering, optimistic versions, repository-ID isolation, a cross-process application-data lock, quotas, pagination, restart persistence, and 180-day cleanup for soft-deleted records. Destructive and moderation actions require tamper-evident security-ledger authorization. Project activity does not stage Git changes, modify repository files, alter `.forgetrace`, approve pull requests, resolve conflicts, or merge code.

The Project database is independent of registry backups. Validated registry Replace/rollback leaves project data untouched; temporarily unregistered repository records remain preserved and become available again after relinking or rollback. Read-only repositories continue to permit coordination while repository bytes remain frozen.

## Start

### Windows

Double-click `START_FORGETRACE.bat`.

### macOS / Linux

```bash
chmod +x START_FORGETRACE.sh
./START_FORGETRACE.sh
```

The owner workspace opens at `http://127.0.0.1:8765`. Sharing remains off until enabled from the selected repository’s **Collaborate** panel.


## v0.4.8 Git intelligence and branch explorer

The owner Git tab inspects a repository-root local Git worktree without changing it. It shows branch or detached-HEAD state, upstream divergence, staged/unstaged/untracked paths, bounded inert diffs, recent commits, commit details, local branches, tags, and credential-sanitized remotes. Repositories without a supported root-level `.git` remain normal ForgeTrace repositories and display an explicit unavailable/unsupported state.

ForgeTrace invokes Git with no shell, no prompts, no credential helpers, no hooks, no fsmonitor, no external diff or text conversion, no submodule recursion, no global/system configuration, and no network-intended command. This inspection authority still performs no writes. v0.5.2 adds a separate owner-only transactional authority for selected-file staging, staged-tree commits, local branch creation, and lightweight local tags; switch/checkout, merge, reset, rebase, fetch, pull, push, and remote contact remain absent.

## v0.4.7 segmented security-event retention and owner-controlled anchoring

- One monotonic logical event chain spans retained sealed segments and the active SQLite ledger.
- Canonical segment files carry sequence range, prior segment hash, event hashes, counts, timestamps, schema, and a full-file SHA-256.
- Rotation is owner-only, exact-preview bound, cross-process serialized, staged, fsynced, journaled, fully verified, rollback-safe, and startup recoverable.
- Retention removes only verified whole segments after protected event and age minimums are satisfied. A hashed retention checkpoint records the deleted-prefix boundary.
- The owner can configure active count, segment target, retained count, maximum age, storage budget, protected events, and protected age. Policy files are hash-verified and changes are ledger-authorized.
- Chain-head digest exports are local only. ForgeTrace never silently contacts a cloud or third-party service.
- Owner-supplied receipts are hash-verified and bound to the exported digest, but `externalPublicationVerified` remains false because ForgeTrace cannot independently prove publication.
- Health reports segment-chain faults, retention pressure, policy damage, incomplete journals, unanchored segments, and invalid/missing receipts.
- Contributor routes cannot list, inspect, rotate, export, anchor, or change security-history policy.

## v0.4.6 unified health dashboard

- The owner-only **Health** surface composes existing Doctor, object, transaction, registry recovery, security-ledger, access-mode, collaboration, review, conflict-evidence, storage, and runtime checks.
- Reports have ten sections: System, Registry, Repositories, Git, Recovery, Security, Access, Collaboration, Project, and Storage.
- Standard scans are bounded and visibly partial when a repository, object, journal, hash-index, review, conflict, orphan, or storage limit is reached. Complete mode raises those explicit limits but never hides residual bounds.
- Every report carries a request ID, evidence time, affected identifiers, severity, next step, explicit limits, completion state, and canonical SHA-256.
- Up to 100 verified report files are retained under application data `health-reports/`; list, detail, and JSON export revalidate the report hash.
- Assessment does not recover transactions, repair metadata, restore registries, clean quarantine, refresh hash caches, or mutate repositories.
- Repair remains separate. The UI can invoke the existing Doctor safe-repair authority only after owner confirmation and a healthy security-ledger authorization event.
- The contributor gateway has no health, report, export, or repair route.

## v0.4.5 quarantine-only visual conflict resolution

- Conflict views are built from preserved immutable base bytes, a locked current-repository read view, and immutable submitted-revision bytes.
- Every draft is stored only under application-data `collaboration/conflict-resolutions/`; draft bytes never enter the live repository.
- Drafts bind to repository/PR/revision/path, current repository digest, conflict-set digest, access mode, unresolved-thread gate, request IDs, evidence hashes, and an optimistic version.
- The owner may keep current, accept incoming, delete, or enter bounded manual UTF-8 text. Binary or oversized evidence is represented only by hashes, sizes, and explicit non-inline choices.
- Base/current/submitted content is escaped inert text. ForgeTrace never executes or actively renders submitted HTML, SVG, JavaScript, or other active content.
- Confirmed decisions are required for every current conflict before approval or merge.
- Merge revalidates repository digest, PR revision, access mode, unresolved threads, paths, immutable submitted bytes, draft evidence, and the security ledger while the repository lock is held.
- Non-conflicting files are merged from immutable submitted-revision copies rather than mutable working quarantine files.
- Resolution evidence is bounded to 1,000 drafts and 4 GiB per pull request, with a 16 MiB free-space reserve, 512 KiB/20,000-line manual text limits, and 180-day terminal retention.
- Contributor routes have no resolution authority. Read-only repositories permit review and draft preparation but still block the final repository mutation.

## v0.4.4 inline review conversations

- Owner and contributor review threads are stored in application-data collaboration storage, never in the live repository.
- Every thread binds permanently to a repository, pull request, submitted revision, quarantined path, optional line range, immutable author role/name, request ID, and creation time.
- Submitted revisions receive immutable manifests and SHA-256-verified review copies under `collaboration/review-revisions/`.
- Old threads remain attached to their original bytes and become visibly outdated after resubmission; ForgeTrace never silently retargets them.
- Owner and contributor replies use optimistic thread versions. Stale writes receive a conflict instead of overwriting newer discussion state.
- Only the owner can resolve or reopen threads and request changes. Contributor routes remain invite-token scoped.
- Unresolved threads on the current submitted revision block approval and merge. A contributor opening a current-revision thread after approval invalidates that approval.
- Review context is returned as inert escaped text. ForgeTrace never executes or actively renders submitted HTML, SVG, JavaScript, or other code.
- Bounded storage allows at most 500 threads per pull request, 500 comments per thread, and 5,000 comments per pull request. Terminal review artifacts are retained for 180 days and then removed by policy.
- Sensitive moderation actions are request-linked in the security ledger, and required owner resolution/reopen/changes-requested actions fail closed if ledger integrity is unavailable.

## v0.4.3 service-enforced read-only repositories

- Owner-controlled `read_write` / `read_only` authority is stored in both the registry and repository-local `.forgetrace/state.json`.
- Write authority exists only when both copies are valid and explicitly agree on `read_write`; missing, invalid, unavailable, or mismatched authority fails closed to read-only.
- Mode checks happen under the repository cross-process lock immediately before mutation.
- File/folder writes, imports, snapshots/restores, embedded settings, managed-repository discard, object materialization, and pull-request merge are centrally blocked.
- Safe browsing, raw reads, verification, export, contribution submission, and quarantine-side review conversations remain available.

## v0.4.2 validated registry recovery

- Owner-visible Merge or Replace previews
- Read-only SQLite integrity, foreign-key, required-table, and schema preflight
- Isolated staging and deterministic migration of supported older backups
- Preview IDs bound to backup SHA-256 and current/prepared logical state
- Cross-process `registry.lock`, exact pre-restore backups, durable journals, post-install verification, automatic rollback, explicit rollback, and startup recovery
- Recovery never replaces repository content/history, quarantine, review revisions, or the separate security-event ledger

## v0.4.1 security event ledger

- Dedicated application-data SQLite evidence store
- Cross-process serialized, monotonic, canonical SHA-256 hash chaining
- Immutable event/schema rows and startup/on-demand integrity verification
- Recursive secret-key redaction and invitation fingerprints rather than raw tokens
- Owner-only filtering and JSON export; contributor-gateway denial
- Fail-closed gates for protected exposure, sensitive export, merge, registry recovery, repository access-mode, and sensitive review moderation actions

## v0.4.0 stabilization foundation

- Cross-process repository locking and one owner process per application-data directory
- Transactional file, folder, import, merge, and snapshot-restore operations with rollback journals
- Snapshot object existence, size, and SHA-256 verification before workspace mutation
- Staged imports with conflict preview, capacity preflight, progress, cancellation, and byte verification
- Atomic managed-repository imports, UUID-first recovery, sensitive-file controls, locked exports, HTTP hardening, and bounded route handlers

The complete closure of the v0.3.6 audit is documented in [`AUDIT_CLOSURE.md`](AUDIT_CLOSURE.md).

## Collaboration and review workflow

1. The owner creates a restricted invitation from **Collaborate**.
2. The contributor downloads the allowed source bundle, creates a quarantined pull request, uploads changes, and submits a revision.
3. Either side can open a thread against a path and optional line range in that submitted revision.
4. The owner can reply, resolve/reopen, or create a thread that requests changes.
5. A contributor can reply and submit a new revision. Earlier discussions remain visible as outdated historical context.
6. Current-revision unresolved threads must be resolved before approval or merge.
7. When conflicts exist, the owner opens the quarantine-only resolver, inspects immutable Base/Current/Submitted evidence, saves an explicit decision for each path, and confirms each draft.
8. Approval is allowed only when current threads are resolved and current conflict drafts are confirmed.
9. Merge revalidates every binding under the repository lock and applies only immutable submitted bytes or verified resolved draft bytes through the existing transactional merge.

A read-only repository can receive and discuss quarantined submissions, but merge remains blocked until the owner deliberately returns it to read-write.

## Registry recovery workflow

Open **Library tools → Validated backup restore**. Select a generated registry backup, choose additive Merge or exact Replace, generate a preview, and review repository effects, conflicts, path status, schema migration, warnings, and access-mode consequences before authorization. A completed restore exposes rollback only while the live registry still matches the verified post-restore state.

## Trust boundary

The contributor gateway is a separately identified restricted listener. Remote invitees cannot browse the owner filesystem, inspect security events, use registry recovery, change repository access mode, edit the live workspace, restore snapshots, resolve owner threads, or merge pull requests. Use sharing only over a trusted LAN or private VPN. Do not directly port-forward ForgeTrace to the public internet.

## Storage

Application data stays outside the extracted package:

- `registry.sqlite3`, `registry.lock`, generated backups, and `registry-restores/`
- `security-events.sqlite3`, `security-events.lock`, `security-event-retention.json`, sealed `security-event-segments/`, journaled `security-event-rotations/`, and owner-controlled `security-event-anchors/`
- `health-reports/` for canonical request-linked health evidence
- `managed-repositories/`, transfer staging, and persistent jobs
- `collaboration/collaboration.sqlite3`
- `collaboration/quarantine/` for current submitted bytes
- `collaboration/review-revisions/` for immutable submitted-revision and base evidence
- `collaboration/conflict-resolutions/` for immutable Base/Current/Submitted evidence and owner resolution drafts
- Repository history and embedded access authority in `<repository>/.forgetrace/`

Replacing the application package does not replace these data locations.

## Validate the source

```bash
python -m unittest discover -s tests -v
PYTHONPATH=. python tests/browser_blackbox_test.py
PYTHONPATH=. python tests/browser_security_events_test.py
PYTHONPATH=. python tests/browser_registry_restore_test.py
PYTHONPATH=. python tests/browser_read_only_test.py
PYTHONPATH=. python tests/browser_review_conversations_test.py
PYTHONPATH=. python tests/browser_conflict_resolution_test.py
PYTHONPATH=. python tests/browser_health_dashboard_test.py
PYTHONPATH=. python tests/browser_security_retention_test.py
PYTHONPATH=. python tests/browser_git_intelligence_test.py
```

See [`HANDOFF/04_TESTING_AND_VALIDATION.md`](HANDOFF/04_TESTING_AND_VALIDATION.md) for the complete matrix and environment notes.

## Important limitations

ForgeTrace is not a Git wire-protocol host and does not provide `git clone`, `git push`, persistent network users, MFA, built-in TLS certificate management, public-internet hardening, or an execution sandbox. Anchor receipts are owner-supplied local evidence; ForgeTrace verifies their binding but does not independently prove external publication. Submitted code is never executed. Physical Windows native-picker acceptance remains a release-machine test described in `tests/WINDOWS_NATIVE_PICKER_ACCEPTANCE.md`.

## Development

Start with [`HANDOFF/00_READ_ME_FIRST.md`](HANDOFF/00_READ_ME_FIRST.md), then use [`HANDOFF/01_NEW_CHAT_BOOT_PROMPT.md`](HANDOFF/01_NEW_CHAT_BOOT_PROMPT.md).


## Verified Releases and Artifacts

ForgeTrace provides application-data-only Kanban, table, and roadmap views over repository issues and discussions. Boards include workflow columns, ranked cards, custom fields, saved views, dependencies, activity history, and board-specific contributor permissions. Board operations never mutate repository files or Git metadata.
