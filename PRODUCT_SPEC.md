# ForgeTrace Product Specification

**Creator:** Rooke Poole  
**Current version:** 0.4.0  
**License:** MIT

## Product boundary

ForgeTrace manages multiple real repository folders from a local owner process. It provides registry organization, file operations, content-addressed snapshots, staged imports, portable exports, local forks, and quarantined pull-request collaboration. It is not yet Git wire-protocol hosting and does not provide persistent network user accounts.

## Non-negotiable integrity guarantees

- Repository mutations are serialized across processes.
- Filesystem and metadata updates commit together or roll back.
- Restore validates object existence, size, and SHA-256 before workspace mutation.
- Imports stage outside the live tree and expose explicit conflict behavior.
- New managed-repository import either registers a complete repository or leaves no orphan.
- Exports hold the repository lock and use verified immutable content.
- Startup recovers pending transaction journals and preserves unavailable registry entries.
- Repository A cannot read or mutate repository B.

## Repository tree requirements

- Parent rows precede their descendants in depth-first order.
- Every row exposes depth and parent information.
- Folder expansion is independent per repository and survives storage failure in memory.
- Folder rename/delete operate through the same transaction boundary as file actions.
- Large trees render through virtualization rather than one DOM row per visible path.
- File hashes are reused for unchanged size/mtime identities.

## Import requirements

- Support file selection, direct local-folder import, browser-folder fallback, shared-link fork, and exact local paths.
- Preview conflicts, sensitive paths, entry count, total bytes, and destination capacity.
- Support abort, skip, overwrite, and rename conflict policies.
- Preserve nested files, empty folders, supported modes, and timestamps where applicable.
- Reject protected metadata and path escapes at every depth.
- Verify committed files by size and SHA-256.
- Expose persistent progress and safe cancellation.

## Snapshot and recovery requirements

- Content objects are addressed and verified by SHA-256.
- Snapshot manifests record files, directories, mode, and timestamps.
- Missing/corrupt objects fail before current data is removed.
- Doctor can validate and restore a valid metadata backup.
- Recovery actions create backup evidence and are auditable.

## Collaboration requirements

- The contributor gateway cannot reach owner APIs, even from loopback.
- Submitted changes remain in quarantine until local approval.
- Source downloads exclude sensitive paths by default.
- Merge revalidates conflicts under the repository lock and retains rollback state.
- Closed/merged/stale quarantine is cleaned by retention policy.
- No submitted code is executed.

## Next product gate

v0.4.1 should focus on a persistent security-event audit viewer, inline review conversations, visual conflict resolution, validated registry backup restore UI, explicit read-only repositories, and then a narrowly scoped Git status/diff adapter. See `HANDOFF/05_KNOWN_LIMITS_AND_NEXT_WORK.md`.
