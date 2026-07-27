# ForgeTrace API v1 Notes — v0.5.2.1

## v0.5.2.1 additive Git-write status evidence

The existing owner-only Git-write status route retains schema version `1` and adds read-only fields:

- `pendingTransactions[]`: journal/receipt integrity, status, last checkpoint/time/details, capture and created-object counts, native-lock/administrative blockers, path match, recovery disposition, recoverability, manual-inspection flag, and exact next step.
- `recoverySummary`: pending, recoverable, deferred, manual-inspection, terminal-cleanup, unassigned-journal, and maintenance-warning counts.
- `maintenanceWarnings[]`: repository-scoped, non-critical application-data cleanup failures.
- `unassignedTransactions[]`: unreadable journals that cannot be safely associated with a repository.
- `startupRecovery`: rollback, terminal cleanup, receipt reconstruction, deferred, retained, and manual-inspection actions.

These are additive diagnostics only. No recovery or repair endpoint was added, and the accepted write operations are unchanged.


## v0.4.10 repository-management API

`GET /api/v1/repositories` records include a backend-computed `managed` boolean.

`DELETE /api/v1/repositories/{repositoryId}/delete-managed?actor=...` is owner-only and requires a healthy security ledger. It permanently removes only a ForgeTrace-managed repository. External repositories remain eligible only for registry-only `DELETE /api/v1/repositories/{repositoryId}` unregister.

The permanent-delete result distinguishes deleted bytes, already-missing paths, cleanup-pending staging, and tombstone persistence. The contributor listener denies the route.

## v0.4.9 Project API

### Owner routes

```text
GET      /api/v1/repositories/{repo}/project
GET|POST /api/v1/repositories/{repo}/project/labels
PUT|DELETE /api/v1/repositories/{repo}/project/labels/{label}
GET|POST /api/v1/repositories/{repo}/project/milestones
PUT|DELETE /api/v1/repositories/{repo}/project/milestones/{milestone}
GET|POST /api/v1/repositories/{repo}/project/issues
GET|PUT|DELETE /api/v1/repositories/{repo}/project/issues/{issue}
POST     /api/v1/repositories/{repo}/project/issues/{issue}/comments
GET|POST /api/v1/repositories/{repo}/project/discussions
GET|PUT|DELETE /api/v1/repositories/{repo}/project/discussions/{discussion}
POST     /api/v1/repositories/{repo}/project/discussions/{discussion}/comments
POST     /api/v1/repositories/{repo}/project/comments/{comment}/moderate
```

Owner list routes support bounded `limit`, `offset`, `state`, `labelId`, `milestoneId`, `assignee`, and `query` filters where applicable. All mutable existing objects require `expectedVersion`; stale versions return HTTP 409. Delete operations soft-delete topics and detach/delete labels or milestones according to the service contract.

### Contributor routes

```text
GET      /api/v1/collaboration/project
GET|POST /api/v1/collaboration/project/issues
GET      /api/v1/collaboration/project/issues/{issue}
POST     /api/v1/collaboration/project/issues/{issue}/comments
GET|POST /api/v1/collaboration/project/discussions
GET      /api/v1/collaboration/project/discussions/{discussion}
POST     /api/v1/collaboration/project/discussions/{discussion}/comments
```

A valid invitation must carry `allowProjectParticipation: true`. Existing and ordinary invitations default to false. Contributors cannot manage labels/milestones, update owner fields, moderate, access repository/Git/Health/security/registry routes, approve, resolve conflicts, or merge.

`GET /api/v1/version` now reports application `0.4.10`, collaboration schema `6`, and `projectCoordinationSchemaVersion: 1`.

### Project response safety

Bodies and comments include escaped inert `bodyHtml`; raw active content is never returned as executable markup. References are bounded informational objects only. Limits and retention are documented in `HANDOFF/09_API_AND_SCHEMA_NOTES.md`.

## Unified health reports

Owner listener only:

- `POST /api/v1/health/reports` — generate a read-first report. Body accepts optional `repositoryId`, `scope` (`standard` or `complete`), and bounded `limits`.
- `GET /api/v1/health/reports?limit=<n>&offset=<n>` — list retained reports.
- `GET /api/v1/health/reports/{health_<32 hex>}` — read and reverify one report.
- `GET /api/v1/health/reports/{health_<32 hex>}/export` — download a hash-verified JSON envelope.

The application version response exposes `healthReportSchemaVersion: 1`. A report includes `reportId`, `requestId`, `generatedAt`, `scope`, optional repository scope, explicit limits, `complete`, overall `status`, ten section objects, summary counts, and `reportHash`.

These endpoints never repair. The existing `POST /api/v1/doctor` remains the only owner HTTP Doctor repair authority and now requires a healthy security-ledger authorization before repair begins. The contributor listener returns `remote_owner_api_blocked` for all health and Doctor routes.

## Versions

- Application `0.4.10`
- Registry schema `4`
- Repository schema `3`
- Collaboration schema `6`
- Project-coordination schema `1`
- Security-event schema `1`
- Registry-restore journal schema `1`

## Collaboration migration

Schema 5 preserves schema 4 review revisions/threads/comments and adds `conflict_resolution_drafts` plus `conflict_resolution_events`. Schema 6 adds only the explicit project-participation invitation permission. Migrations are transactional and never modify repository content.

## Owner review routes

```text
GET|POST /api/v1/repositories/{repo}/pull-requests/{pr}/review-threads
GET       /api/v1/repositories/{repo}/pull-requests/{pr}/review-threads/{thread}
POST      /api/v1/repositories/{repo}/pull-requests/{pr}/review-threads/{thread}/comments
POST      /api/v1/repositories/{repo}/pull-requests/{pr}/review-threads/{thread}/resolve
POST      /api/v1/repositories/{repo}/pull-requests/{pr}/review-threads/{thread}/reopen
```

## Owner conflict-resolution routes

```text
GET|POST /api/v1/repositories/{repo}/pull-requests/{pr}/conflict-resolutions
GET       /api/v1/repositories/{repo}/pull-requests/{pr}/conflict-resolutions/{draft}
POST      /api/v1/repositories/{repo}/pull-requests/{pr}/conflict-resolutions/{draft}/decision
POST      /api/v1/repositories/{repo}/pull-requests/{pr}/conflict-resolutions/{draft}/confirm
```

`POST` collection prepares missing current drafts and requires `expectedPullRequestRevision`. Decision/confirm require `expectedVersion`. Stale PR/draft/repository bindings return HTTP 409. Missing or damaged evidence returns a fail-closed integrity error.

Decision payloads support `current`, `incoming`, `manual`, and `delete`. Manual content is accepted only when all required evidence is bounded UTF-8 text.

## Contributor review routes

```text
GET|POST /api/v1/collaboration/pull-requests/{pr}/review-threads
GET       /api/v1/collaboration/pull-requests/{pr}/review-threads/{thread}
POST      /api/v1/collaboration/pull-requests/{pr}/review-threads/{thread}/comments
```

There are no contributor conflict-resolution, resolve/reopen, request-changes, approval, or merge routes. Contributor authority remains invitation-token derived.

## Merge/review state

Public PR data includes `reviewConversation`, `submittedRevisions`, conflicts, and `conflictResolution` summary data. Approval and merge fail when current threads remain unresolved or any current conflict lacks a confirmed current draft. Merge revalidates all bindings under the repository lock.

## Context safety

Review and conflict content is returned only as escaped inert text. Active content is never executed or embedded. Every evidence file is regular-file checked and size/SHA-256 verified before use.

## Limits

Review limits remain 500 threads/PR, 500 comments/thread, 5,000 comments/PR, 8,000 characters/body, 100 rows/page, and 180-day terminal retention.

Conflict-resolution limits are 1,000 drafts/PR, 4 GiB evidence/PR, 512 KiB and 20,000 lines for manual text, a 16 MiB free-space reserve, and 180-day terminal retention.

## v0.4.7 segmented security-history API

Owner-only routes now expose segment inventory/status, hash-verified retention policy, previewed rotation, bounded rotation history, chain-head digest export, and owner receipt recording under `/api/v1/security-events/*`. The contributor listener rejects all of them. Query/export span retained sealed segments plus active rows. See `HANDOFF/09_API_AND_SCHEMA_NOTES.md`.


## v0.4.8 owner Git intelligence API

Owner listener only; all routes are GET/read-only:

```text
GET /api/v1/repositories/{repositoryId}/git?commitLimit=<1..200>
GET /api/v1/repositories/{repositoryId}/git/diff?scope=working|staged&path=<optional relative path>
GET /api/v1/repositories/{repositoryId}/git/diff?scope=commit&commit=<full 40-64 hex object id>&path=<optional relative path>
GET /api/v1/repositories/{repositoryId}/git/commits/{full 40-64 hex object id}
```

The overview returns probe/layout state, branch or detached HEAD, upstream and ahead/behind counts, staged/unstaged/untracked paths, bounded commit history, local branches, tags, and credential-sanitized remotes. Diff responses include scope, path/commit binding, byte count, truncation, binary suppression, and inert text. Commit detail includes bounded metadata, parents, changed files, and a bounded commit diff.

`GET /api/v1/version` exposes `gitIntelligenceSchemaVersion: 1` and `gitWriteSchemaVersion: 1`. The contributor listener rejects every owner Git route.

## v0.5.2 owner transactional Git-write API

```text
GET  /api/v1/repositories/{repositoryId}/git/writes?receiptLimit=<1..200>
POST /api/v1/repositories/{repositoryId}/git/writes/preview
POST /api/v1/repositories/{repositoryId}/git/writes/execute
```

The accepted preview operations are `stage`, `commit`, `create_branch`, and `create_tag`. Execute requires the exact `previewId`, server-provided typed confirmation, and actor label. All writes are local owner-only, state-bound, security-ledger authorized, repository/Git locked, journaled, rollback-capable, and startup recoverable. There are no contributor writes, branch switching, merge, remotes, credentials, fetch/pull/push/clone, hooks, signing, editor, or shell paths. See `HANDOFF/09_API_AND_SCHEMA_NOTES.md`.
