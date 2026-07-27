# ForgeTrace Migrations — v0.5.2

## v0.5.1.2 → v0.5.2

No application database or repository schema migration occurs. Git-write schema version 1 is application-data file evidence only: `git-writes/previews`, `locks`, `transactions`, and `receipts`. Existing repositories remain valid. Unsupported Git layouts are reported, not migrated. Startup recovery processes only digest-valid v0.5.2 journals and never rewrites damaged evidence.

## v0.4.9 → v0.4.10

No SQLite or repository-state schema migration is required. ForgeTrace creates application-data `repository-deletions/journals`, `staging`, and `tombstones` directories. Existing registry, repository, collaboration, project, security-history, Health, and Git data remain unchanged.

## v0.4.8 → v0.4.9

- Application registry schema remains `4`.
- Repository schema remains `3`.
- Collaboration schema migrates from `5` to `6` transactionally by adding `allow_project_participation INTEGER NOT NULL DEFAULT 0` to invitations.
- Existing invitations therefore remain denied project participation until an owner creates a new explicitly permissioned invitation.
- A separate `project-coordination/project-coordination.sqlite3` database is initialized at schema `1` with project metadata/counters, labels, milestones, topics, topic-label links, and comments.
- No repository files, `.forgetrace`, Git metadata, immutable collaboration evidence, registry backup, security segment, or Health report is migrated.
- Project schema versions newer than supported fail closed rather than being downgraded.

Project coordination is not part of registry backup/restore. An operator restoring the registry should preserve the entire application-data directory; same-ID project rows remain in place through registry Replace/rollback.

## Normal v0.4.5 → v0.4.6 upgrade

v0.4.6 does not change the application, repository, collaboration, security-event, or registry-restore schemas. On first owner startup it creates application-data `health-reports/` when needed. No report data is copied into the package and no repository metadata is changed merely by upgrading.

Health assessment opens repositories without startup recovery or workspace creation. Existing recovery remains owned by normal repository startup and explicit recovery authorities. Running an older package against newer application data remains unsupported.

## Current schemas

- Application version: `0.4.10`
- Registry schema: `4`
- Repository schema: `3`
- Collaboration schema: `6`
- Project-coordination schema: `1`
- Security-event schema: `1`
- Registry-restore journal schema: `1`

## Collaboration schema 5

Schema 5 adds:

- `conflict_resolution_drafts`
- `conflict_resolution_events`

Existing schema 4 review revisions, threads, comments, PR rows, invitations, quarantine metadata, and terminal history remain unchanged.

Submitted revisions created in v0.4.5 attempt to preserve immutable base copies for changed/deleted paths. A legacy v0.4.4 PR can still create a conflict draft only when the recorded base hash is available through a verified repository object or matching locked live bytes. ForgeTrace fails closed rather than inventing unavailable three-way evidence.

## Normal v0.4.4 → v0.4.5 upgrade

1. Keep the existing platform application-data directory.
2. Replace only the application package.
3. Start ForgeTrace once.
4. Collaboration schema migrates transactionally from 4 to 5.
5. Registry/repository schemas remain 4/3.
6. Existing PR review history remains intact.
7. New evidence directories are created only when an owner prepares a conflict resolution.
8. Verify owner review and conflict-resolution surfaces before enabling sharing.

## Failure behavior

- SQLite migration failure rolls back the collaboration transaction.
- Unsupported or corrupt collaboration data is not silently recreated as trusted evidence.
- Missing immutable base/current/submitted bytes block draft preparation or use.
- Existing repository locks, transaction journals, registry recovery, security ledger, read-only authority, and gateway isolation are unchanged.

## Downgrade warning

A v0.4.4 package does not understand collaboration schema 5 conflict-resolution rows or evidence semantics. Do not run an older package against upgraded application data. Preserve an independent application-data backup before any package rollback.

## v0.4.6 to v0.4.7

No registry, repository, collaboration, or active security-event database schema migration is required. Startup creates security segment/rotation/anchor directories and a hash-verified default retention policy. Existing active events remain in SQLite until the owner explicitly rotates them. Older packages should not be used to manage segmented v0.4.7 history.


## v0.4.7 to v0.4.8

No registry, repository, collaboration, security-event, or Health-report database migration is required. Git intelligence is a live owner-only read adapter and creates no repository metadata, cache, credential, or application-data database. Existing repositories without a supported root-level `.git` continue to work normally.
