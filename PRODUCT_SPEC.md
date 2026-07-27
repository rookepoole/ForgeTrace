# ForgeTrace Product Specification

## v0.5.3.0 implemented product slice — safe switch planning

ForgeTrace can now analyze whether a future switch between the current attached local branch and an existing direct local branch is eligible, and can create a sealed recovery capture without changing the repository. The planner reports source and target identity, affected tracked paths, preserved untracked/ignored files, collision decisions, capture estimates, and exact safety exclusions.

The user-facing switch action is intentionally not present. Execution remains gated on a separately accepted transaction/recovery engine, crash-injection proof, API/UI authorization, Health evidence, and physical Windows acceptance.

## v0.5.2.2 acceptance-runner requirements

1. The physical-Windows gate must not treat unittest's normal stderr progress as a PowerShell failure.
2. Native stdout and stderr must be preserved in the evidence log without entering PowerShell's terminating error pipeline.
3. Automated acceptance must be determined from the real Python process exit code.
4. `AUTOMATED_RESULT: OK` must be written only after the complete automated suite exits zero.
5. The v0.5.2/v0.5.2.1 Git-write authority and every inherited trust boundary must remain unchanged.


## v0.5.2.1 hardening requirements

1. Do not expand the accepted v0.5.2 Git operation set.
2. Persist a canonical-digest recovery checkpoint before every tested crash boundary that can follow capture, index, object, ref/reflog, rollback, journal, or receipt mutation.
3. Prove startup recovery using a fresh service instance, not an in-process exception path.
4. Restore exact captured index/`HEAD`/ref/reflog bytes for incomplete valid transactions; retain created object IDs as evidence rather than claiming object deletion.
5. Treat a verified terminal journal as the commit boundary: reconstruct a missing receipt or retry cleanup without rolling the operation back.
6. Retry transient Windows file-sharing failures only for a bounded period; fail closed when critical evidence cannot be installed.
7. Keep cleanup after terminal evidence, consumed-preview deletion, and retention deletion outside the mutation result. Blocked cleanup must be visible but non-destructive.
8. Defer rollback while native Git locks or active administrative state exist; never remove or bypass them.
9. Surface journal/receipt integrity, last checkpoint, blockers, recovery disposition, and an actionable next step without making read-only Git inspection depend on writer health.
10. Do not claim physical Windows acceptance until the exact archive passes the provided PowerShell runner and owner-browser checklist on Windows.


## v0.5.2 accepted product requirements

- Provide an owner-only transactional local Git write surface without changing the read-only Git intelligence authority.
- Support only selected-file staging, committing the already staged tree, creating a local branch, and creating a lightweight local tag.
- Require a digest-bound preview, state revalidation, exact typed confirmation, a writable repository, and security-ledger authorization.
- Serialize through the normal repository lock followed by a repository-scoped Git-write lock.
- Maintain durable hash-sealed transaction journals, exact operation-specific rollback captures, verified receipts, and startup recovery.
- Block active deletion intents, read-only repositories, native Git locks, active Git administrative state, unsupported layouts, protected paths, external clean filters, working-tree encoding, stale previews, and damaged evidence.
- Keep commits hookless, editorless, unsigned, non-interactive, credential-free, helper-free, shell-free, and network-free through Git plumbing.
- Keep switch/checkout, merge, reset, rebase, cherry-pick, revert, annotated/signed tags, signed commits, remotes, fetch, pull, push, clone, hosting, and public deployment outside this release.
- Keep read-only Git inspection available when write status is degraded.
- Preserve the Windows deletion transaction, Security primary-event resilience, inherited locks/journals/evidence, MIT license, and Rooke Poole credit.

## v0.5.1.2 maintenance requirements

### Permanent managed-repository deletion on Windows

1. ForgeTrace must wait for existing repository operations through the normal cross-process repository lock.
2. Before releasing any handle inside the repository, ForgeTrace must publish a durable application-data deletion intent and hold an external repository-ID deletion guard.
3. All new ForgeTrace reads and writes must fail closed while that intent exists.
4. The parent-directory move must occur only after every ForgeTrace-owned handle inside the repository is closed.
5. Registry removal, staging, rollback, tombstone commitment, and startup recovery must remain serialized by the existing registry operation lock and durable journal.
6. A third-party sharing conflict must be retried for a bounded interval and then return a recoverable, non-destructive HTTP 423 response.
7. When Windows Restart Manager identifies a blocker, the response may name the process and PID but ForgeTrace must never terminate or restart it.
8. A failed move must leave the repository registered and its original bytes untouched.

### Security viewer resilience

1. Primary event retrieval must not depend on segmented-retention, anchoring, journal, or storage-status retrieval.
2. The owner interface must load the primary event list before auxiliary history status to avoid competing full-chain scans.
3. Auxiliary status failures must be visible as degraded evidence without hiding already available primary events.
4. Read-only operational reporting may degrade component by component, but security-sensitive mutations must continue to require a healthy complete logical chain.

# ForgeTrace Product Specification — v0.5.1

## Product statement

ForgeTrace is a local-first repository workspace created by **Rooke Poole**. It manages real local project folders, attributable activity, verified snapshots, staged imports, owner-reviewed quarantined contributions, revision-bound review conversations, quarantine-only conflict resolution, segmented security evidence, owner-controlled anchor receipts, local issues and discussions, project boards, immutable local releases with checksummed artifacts, and recoverable application metadata without requiring a cloud account.

## Primary users

- A local owner managing one or more project folders
- A trusted outside contributor using a restricted invitation gateway
- A maintainer auditing project integrity, recovery state, access authority, and collaboration history

## Core product guarantees

1. Application data remains outside the extracted package.
2. Repository mutation occurs only through cross-process locked, journaled service boundaries.
3. Snapshot restore verifies every required object before workspace replacement.
4. Imports are staged, capacity-checked, conflict-explicit, cancellable, and byte-verified.
5. Repository identity and operations remain isolated by stable IDs and canonical paths.
6. Contributor bytes remain quarantined until owner-authorized merge.
7. Security-sensitive owner actions create durable tamper-evident evidence or fail closed.
8. Registry backup recovery is previewed, staged, journaled, verified, and rollback-aware.
9. Repository writes require consistent registry and embedded `read_write` authority.
10. Review discussions bind immutably to submitted revision bytes and never grant live-repository authority.
11. Conflict-resolution drafts preserve immutable three-way evidence in application data and cannot mutate the repository before final transactional merge.
12. Project coordination remains repository-ID scoped in application data and grants no repository, Git, registry, review, conflict, or merge mutation authority.

## v0.5.1 repository-management requirements

- The desktop Files workspace must expose a substantially larger virtualized tree without breaking mobile use.
- Permanent deletion must be distinct from unregister and available only for direct-child ForgeTrace-managed repositories.
- The backend must reject external, symlinked/special, stale-path, identity-mismatched, and read-only deletion attempts.
- Managed bytes must move outside discovery before the registry row is committed absent.
- Recovery journals must roll back when the row remains and finish deletion when the row is absent.
- Tombstones must prevent automatic startup and Doctor re-registration while allowing deliberate owner re-registration.
- Missing and manually emptied managed paths must be removable without false file-deletion claims.
- Security evidence must authorize the owner route before mutation.

## v0.4.9 project-coordination requirements

1. Issues, discussions, labels, milestones, comments, assignments, due dates, accepted answers, locking, pinning, and moderation are stored in a dedicated application-data database outside repositories and the extracted package.
2. Every record is scoped by stable repository ID. Cross-repository reads or writes fail closed.
3. The database uses transactional schema migration, one cross-process lock, optimistic row versions, bounded pages, quotas, restart persistence, soft deletion, and explicit retention cleanup.
4. Content is rendered only through bounded inert Markdown/code transformation. Active HTML, SVG, JavaScript, links, images, commands, hooks, and repository code are never executed.
5. Contributor participation requires an explicit invitation permission. Ordinary source-sharing invitations do not imply issue/discussion authority.
6. Permissioned contributors may create issues/discussions and comments only. They receive no labels, milestones, owner moderation, repository files, Git, Health, security-history, registry, access-mode, approval, conflict-resolution, or merge authority.
7. Owner moderation and destructive actions require durable security-history authorization or fail before state change.
8. References to PRs, immutable revisions, commits, paths, issues, or discussions are informational identifiers only and cannot mutate those authorities.
9. Read-only repositories permit coordination while repository and Git bytes remain unchanged.
10. Registry recovery does not replace the independent project database; same-ID project records remain preserved through Replace and rollback.
11. Unit/integration and real two-sided browser evidence must prove concurrency, isolation, permission denial, inert rendering, moderation, persistence, recovery independence, and no repository/Git mutation.

## v0.4.6 unified health-dashboard requirements

1. Health assessment is owner-only and read-first; it creates report evidence but cannot repair repository, registry, collaboration, or ledger state.
2. Reports contain System, Registry, Repositories, Recovery, Security, Access, Collaboration, and Storage sections with status, completion, timestamp, findings, identifiers, and next steps.
3. Existing Doctor, snapshot verification, transaction inspection, registry recovery, ledger integrity, access-mode, review, and conflict-evidence authorities are reused rather than replaced by weaker checks.
4. Every expensive scan is bounded or explicitly complete; partial results can never be labeled fully healthy.
5. Reports are stored outside the package under application data, are capped to a bounded history, and are verified by canonical SHA-256 before retrieval or export.
6. A damaged ledger must remain reportable even though a health-generation security event cannot be appended.
7. Repair remains a separate explicit action through its original authority. Browser Doctor repair requires confirmation and a healthy ledger before mutation.
8. Contributor listeners expose no health report, export, or repair authority.
9. Health generation must not recover pending repository transactions, rebuild indexes, clean retention storage, restore snapshots/registries, or change repository access mode.
10. Unit/integration and real browser evidence must prove report durability, tamper detection, bounded scans, corrupted object/evidence/ledger visibility, journal preservation, gateway isolation, export, and original-authority repair.

## v0.4.5 conflict-resolution requirements

### Evidence and storage

- Collaboration schema 5 stores versioned conflict-resolution draft/event metadata.
- Preserve verified Base, Current, and Submitted bytes under `collaboration/conflict-resolutions/`.
- Base evidence must come from immutable submitted-revision base snapshots, verified content-addressed objects, or matching locked live bytes; unavailable base evidence fails closed.
- Current evidence is captured under the repository lock and bound to a full repository digest.
- Submitted evidence comes from immutable submitted-revision copies, never mutable working quarantine files.
- Evidence manifests and every stored file are verified by size and SHA-256 before display, confirmation, or merge.

### Draft authority

Each draft binds to repository ID, pull-request ID, submitted revision, path, conflict reason, submitted kind, repository digest, access mode, conflict-set digest, unresolved-thread digest/count, author, request IDs, optimistic version, evidence manifest hash, decision, result kind, and resolved hash/size.

Only the owner listener may prepare, edit, confirm, or inspect resolution drafts. Contributor routes expose no resolution authority. Stale versions fail with an optimistic-concurrency conflict.

### Decisions and rendering

- Supported decisions: keep current, accept incoming, manual UTF-8 text, or delete.
- Manual resolution is limited to 512 KiB and 20,000 lines.
- Binary, invalid UTF-8, or oversized evidence cannot use inline manual resolution; show only hashes, sizes, and explicit current/incoming/delete choices.
- Never execute or actively render HTML, SVG, JavaScript, or any submitted/resolved content.
- Preserve original Base/Current/Submitted evidence after a decision is saved.

### Merge gates

- Every current conflict requires a confirmed current draft before approval or merge.
- Any change to repository digest, PR revision, conflict set, access mode, or unresolved current-review state makes active drafts stale.
- Immediately before merge, revalidate all bindings, paths, evidence hashes, immutable revision bytes, read-only authority, current review gate, and security-ledger authorization while holding the repository lock.
- Merge non-conflicting files from immutable revision copies and conflicting files only from confirmed verified resolution results.
- Use the existing repository transaction journal and rollback behavior; never write draft bytes directly into the live repository.
- Applied drafts become immutable historical merge evidence.

### Limits and retention

- 1,000 conflict drafts per pull request
- 4 GiB conflict-evidence storage per pull request
- 16 MiB minimum free-space reserve beyond the capture estimate
- 180-day terminal retention with orphan cleanup
- Security events contain metadata, hashes, decisions, sizes, and request IDs—not file bodies

## v0.4.4 review-conversation requirements

### Storage and revision identity

- Persist conversations in application-data collaboration storage, not repository content.
- Collaboration schema 4 stores submitted revision metadata, threads, comments, and resolution events.
- Copy each submitted revision’s quarantined changed files into immutable application-data review storage.
- Record a canonical manifest with path, size, and SHA-256 and verify bytes when review context is read.
- Backfill open pull requests from collaboration schema 3 without modifying repository content.

### Thread model

Each thread must include:

- repository ID and pull-request ID
- submitted PR revision
- quarantined path
- optional start/end line
- creator role and display name
- creation timestamp and request ID
- monotonic thread version
- resolved state and owner resolution metadata

A thread may span at most 200 lines. Paths must be present in the submitted revision manifest and must not contain `.forgetrace`, `.git`, traversal, absolute paths, or unsupported context.

### Conversation authority

- Owners and invitation-scoped contributors may create threads and append replies.
- Only owners may resolve, reopen, or request changes.
- Contributors may never resolve/reopen, approve, merge, inspect security events, recover the registry, or change repository access mode.
- Every reply/resolution uses optimistic version checks. Stale writes fail with `review_thread_version_changed`.
- Resolved threads must be reopened before replying.

### Revision behavior

- A new submission creates a new immutable submitted revision.
- Existing threads remain attached to their original revision and become visibly outdated.
- Historical unresolved threads do not block approval or merge of a newer revision.
- Current-revision unresolved threads block approval and merge.
- A contributor opening a new current-revision thread after approval invalidates that approval.

### Safety and rendering

- Never execute submitted code.
- Never render submitted HTML, SVG, JavaScript, or other active content inline.
- Return review context as bounded escaped text with explicit `activeContentRendered: false` evidence.
- Verify stored review bytes against the immutable manifest before presenting context.

### Limits and retention

- 500 threads per pull request
- 500 comments per thread
- 5,000 comments per pull request
- body limit: 8,000 characters
- thread/comment page maximum: 100
- terminal review artifacts retained 180 days, then revision snapshots and conversation rows are removed while terminal PR metadata remains

### Evidence

- Thread creation and replies create normal collaboration security events.
- Owner changes-requested authorization and thread resolution/reopen require a healthy security ledger before state change.
- Events include request ID, repository, PR, thread, revision, path, actor/surface, and invite fingerprint where relevant; raw invitation tokens are never stored.

## Existing repository integrity requirements

- Use OS-backed owner-instance, registry, repository, and security-ledger locks.
- Use filesystem transaction journals and startup recovery.
- Verify content-addressed objects by existence, expected size, and SHA-256.
- Preserve supported empty directories, modes, and timestamps.
- Keep protected metadata outside import, contribution, and review path authority.
- Keep read-only authorization central and under the repository lock.

## Registry recovery requirements

- Preview must be non-mutating and digest-bound.
- Supported older backups migrate only in staging; newer schemas fail closed.
- Restore must create an exact pre-restore backup and durable journal before live replacement.
- Automatic and explicit rollback must protect later registry work.
- Recovery never replaces repository content/history, quarantine, review revisions, or the security ledger.

## Deployment posture

- Owner listener: loopback only
- Contributor listener: disabled by default; trusted LAN/private VPN only
- No direct public-internet support
- No persistent network accounts, MFA, built-in TLS lifecycle, or identity attestation
- No execution sandbox because submitted code is never executed

## Next product gate

v0.4.8 delivers **Git Intelligence and Branch Explorer** as an owner-only, strictly read-only adapter for supported repository-root Git worktrees. It provides bounded status, staged/unstaged/untracked paths, inert diffs, commit history/detail, branches, tags, upstream divergence, and credential-sanitized remotes. It runs with a hardened subprocess environment that disables hooks, prompts, credentials, external helpers, global/system configuration, submodule recursion, and lazy fetching. It performs no Git or network mutation and adds no contributor authority.

The next product increment is **v0.5.1 Verified Releases and Artifacts**. Boards should add Kanban/table views, configurable workflow states, optimistic drag ordering, custom fields, saved views, dependencies, and milestone timelines while remaining application-data coordination records with no repository or Git mutation authority. Verified releases, transactional local Git writes, identity/TLS, and remote hosting remain ordered behind that foundation. See `HANDOFF/05_KNOWN_LIMITS_AND_NEXT_WORK.md`.


## v0.5.1 project boards requirements

Repository-scoped boards support Kanban, table, and roadmap presentation, configurable columns, optimistic ranked movement, custom fields, saved views, dependencies, and activity history. Coordination remains outside repository and Git data.

## Planned v0.5.3 branch switch — design only

The planned owner Git workspace will eventually permit one additional action: switching from the current attached local branch to a different existing local branch. The first slice requires a clean staged/tracked state and preserves bounded non-conflicting untracked and ignored regular files through exact application-data backups.

The preview will show source/target branch and commit, affected-path count, backup bytes, untracked/ignored preservation, unsupported reasons, expiry, and the exact `SWITCH BRANCH` confirmation. Read-only Git inspection remains available even when switch recovery is degraded. Contributors receive no switch routes.

This package does not implement the feature. Merge, detached/path checkout, force/discard, branch creation during switch, remotes, credentials, hooks, and public hosting remain absent.
