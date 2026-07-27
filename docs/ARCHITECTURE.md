# ForgeTrace Architecture — v0.5.2

## v0.5.2 transactional local Git-write architecture

ForgeTrace now separates Git reads and writes. `GitIntelligenceService` remains read-only. `GitWriteService` is an owner-only authority for selected-file staging, staged-tree commits, local branch creation, and lightweight local tags. It uses digest-bound previews, repository-then-Git lock ordering, security-ledger authorization, operation-specific sealed captures, exact rollback, startup recovery, and verified receipts under application data `git-writes/`.

Commit creation uses Git plumbing with a sanitized non-interactive environment. There is no checkout/switch, merge, reset, rebase, cherry-pick, revert, remote, credential, hook, signing, editor, shell, or public-hosting path. Read-only Git inspection remains available if writer status is degraded.

## v0.4.10 repository-management architecture

Permanent managed-repository deletion is a distinct owner authority in `RepositoryRegistry`. It classifies managed paths as normal direct children of application-data `managed-repositories/`, serializes with repository/registry locks, moves bytes atomically into `repository-deletions/staging/`, journals every crash boundary, writes an identity tombstone, commits registry removal, and then erases staged bytes. Startup rolls back or finalizes from the committed row state before normal repository discovery.

The Files UI sizing change does not alter repository service, tree, file, editor, or virtualization authority.

## v0.4.9 Project architecture

`ProjectCoordinationService` is a dedicated application-data coordination authority. It owns one SQLite database and one OS-backed lock under `project-coordination/`; it does not write repository files, `.forgetrace`, Git metadata, registry backups, collaboration evidence, or security-history storage.

Its schema stores repository counters, labels, milestones, issues/discussions, topic-label links, and comments. All objects carry a stable repository ID and optimistic version. The service enforces quotas, bounded pages, inert rendering, safe informational references, soft deletion, and 180-day cleanup.

Collaboration schema 6 adds an explicit invitation capability for project participation. The contributor listener maps only create/read/comment project methods and never exposes owner label, milestone, update, moderation, repository, Git, Health, registry, security, approval, conflict, or merge routes.

Read-only repositories remain coordinate-able because project data is outside the repository; the central repository mutation boundary is unchanged. Registry recovery replaces only registry state, so project records survive repository unregister/Replace/rollback and become accessible again with the same repository ID.

## Trust boundaries

1. **Owner listener** — loopback-only repository administration, security evidence, registry recovery, access-mode authority, review moderation, conflict-resolution decisions, approval, and merge.
2. **Contributor listener** — separately identified, disabled by default, invitation-token scoped, and denied all owner routes.
3. **Application data** — registry, locks, recovery journals, managed repositories, jobs, collaboration database, quarantine, immutable submitted revisions, conflict evidence, security ledger, and durable health reports outside the extracted package.
4. **Repository service** — stable repository ID/path isolation, cross-process lock, two-copy access authority, object verification, filesystem transactions, and startup journal recovery.


## Unified health architecture

`HealthDashboardService` is an owner-side read-first aggregator, not a repair authority. It calls bounded inspection paths on the existing registry, repository, transaction, recovery, ledger, collaboration, review, and conflict services and writes a canonical hash-verified report under application data `health-reports/`.

The report has ten sections: System, Registry, Repositories, Git, Recovery, Security, Access, Collaboration, Project, and Storage. Each section carries a completion flag. When any explicit repository/object/journal/hash-index/review/conflict/orphan/storage limit truncates work, the report remains visibly partial.

Health report generation does not recover pending transactions, restore a registry, repair embedded metadata, refresh a hash cache, remove quarantine data, change access mode, or mutate repository content. The owner UI may separately invoke the existing Doctor repair route after confirmation. That protected HTTP repair requires a healthy security-ledger authorization event before Doctor begins.

Report list, detail, and export revalidate regular-file status, report format, and canonical SHA-256. The contributor listener rejects every health/report/export/repair route before dispatch.

## Collaboration evidence layers

- `collaboration/quarantine/` holds mutable working PR uploads.
- `collaboration/review-revisions/` holds immutable submitted-revision files, manifests, and available base snapshots.
- `collaboration/conflict-resolutions/` holds immutable Base/Current/Submitted captures, evidence manifests, and optional resolved bytes.
- `collaboration.sqlite3` stores PR, invitation, revision, review-thread, and conflict-draft/event metadata.

None of these directories is live repository authority.

## Conflict-resolution flow

1. Conflict detection compares current repository manifest against recorded PR base hashes.
2. Owner preparation holds the collaboration and repository locks, computes a full current repository digest, verifies revision evidence, preflights quota/free space, and copies Base/Current/Submitted files into a private draft directory.
3. The draft manifest binds repository, PR, revision, path, conflict set, repository digest, access mode, unresolved-thread gate, hashes, and request metadata.
4. Owner decision writes only `resolved.bin` in the draft directory and updates SQLite using optimistic version checks.
5. Confirmation revalidates all bindings and evidence and requires a healthy security ledger.
6. Approval requires no unresolved current threads and confirmed current drafts for every conflict.
7. Final merge reacquires the repository lock, recomputes all bindings, verifies immutable revision and resolution files, checks writable authority and ledger authorization, and invokes the existing transactional repository merge.
8. Applied drafts become immutable historical evidence; failure before/inside merge leaves the live repository unchanged or transactionally rolled back.

## Lock hierarchy

- owner-instance lock for one owner process per application-data directory
- registry operation lock for registry backup/restore and coordinated registry actions
- collaboration lock for PR/review/draft metadata
- repository lock for any live repository read binding or mutation
- security-ledger lock for append/integrity authority

Conflict operations acquire collaboration state before the repository lock, matching existing merge ordering. No contributor route can acquire owner mutation authority.

## Access and recovery interaction

Effective repository mode is writable only when registry and embedded copies both validly agree on `read_write`. Review and conflict preparation may read a read-only repository, but merge is rejected at the central mutation boundary. Registry recovery never restores collaboration evidence or the security ledger.

## Deployment posture

ForgeTrace v0.5.2 includes local project coordination, read-only Git intelligence, and a narrowly bounded transactional local Git writer, but it is not a general Git client, Git remote, execution sandbox, identity provider, public-internet collaboration service, or independently externally verified audit publication system. Use contributor sharing only over a trusted LAN or private VPN, and keep the owner listener loopback-only.

## v0.4.7 segmented security-history architecture

Security history is one logical event chain across an optional retention checkpoint, canonical sealed segments, and the active SQLite suffix. Rotation is staged under the cross-process security lock with exact backup, hash-protected journal, full post-install verification, rollback, and startup recovery. Anchor exports are offline and owner-controlled.


## v0.4.8 Git intelligence architecture

`GitIntelligenceService` is an isolated owner read authority. Routes call the service; they never construct subprocess arguments directly. The service resolves only the registered repository root, accepts only a root-level supported `.git` directory, verifies that Git's top-level path matches the registered root, and rejects bare/external/symlinked/config-extended/object-alternate layouts.

Every invocation uses an absolute Git executable, no shell, a restricted environment, disabled hooks/helpers/prompts/pagers/fsmonitor/submodule recursion/lazy fetch, strict timeout and output limits, and local read commands only. Results are parsed into bounded structured data. Diff and metadata content is rendered inert in the owner UI. No Git result is written into repository state, the Git index, security history, or collaboration evidence.
