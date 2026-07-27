# ForgeTrace Recovery — v0.5.2.1

## v0.5.2.1 checkpoint-driven recovery

Every testable mutation boundary now records a sealed `lastCheckpoint`, timestamp, and bounded details in the transaction journal. Incomplete valid journals are classified as `rollback_on_restart`; native Git locks or active administrative state produce `deferred_external_git_state`; damaged/unreadable evidence produces `manual_inspection_required`; a terminal journal with a missing receipt produces `reconstruct_receipt_then_cleanup`; and a terminal journal with a verified receipt produces `cleanup_terminal_journal`.

Recovery never infers success from repository appearance alone. It verifies journal identity/digest, registered repository identity and canonical paths, capture integrity, and native Git state. Exact captured files are restored under repository lock followed by the repository-scoped Git-write lock. A later read-only transition does not prevent rollback because recovery restores pre-transaction state, but new writes remain denied.

Windows sharing failures during already-terminal directory cleanup are retained as maintenance evidence and retried on startup. They do not roll back an already committed ref. A missing terminal receipt is reconstructed from the verified journal before cleanup; a damaged or conflicting receipt blocks cleanup and requires manual inspection.


## v0.5.2 Git-write recovery

Transactional Git writes store a hash-sealed journal and operation-specific captures under application data `git-writes/transactions/`. Stage/commit capture the exact index; commit also captures `HEAD`, current ref, and reflog evidence; branch/tag capture only target ref/reflog evidence. Failure restores exact captured state and writes a verified receipt. Startup recovers incomplete journals, but defers while native Git locks or merge/rebase/cherry-pick/revert/bisect state exists.

Tampered journals are retained without restoration. Missing terminal receipts are reconstructed before journal cleanup, while a damaged/conflicting receipt prevents cleanup. Recovery may restore pre-transaction state after a later read-only transition. Unreachable Git objects created before a failed ref update can remain for normal Git garbage collection; receipts record created object IDs.

## Permanent managed-repository deletion recovery

v0.4.10 adds a separate application-data deletion transaction for ForgeTrace-managed repositories. The transaction writes a confined, fsynced journal, stages the complete managed directory outside every discovery root, writes a repository-ID tombstone, removes the registry row under the registry operation lock, and then erases staged bytes. It never recursively deletes linked external repositories.

Startup recovery interprets the registry row as the commit boundary:

- if the registry row still exists, staged bytes are restored to the original managed path and the tombstone is cleared;
- if the registry row is absent, the tombstone is preserved and staged bytes are finalized for deletion;
- malformed or path-escaping journals are retained for operator inspection and are never followed outside deletion storage.

A tombstone suppresses startup discovery and Doctor re-registration of UUID-identical leftovers. Explicit owner registration of that repository ID clears the tombstone only after registration succeeds. Missing or manually emptied managed directories can still be removed from the registry and tombstoned.

## Project-coordination recovery boundary

The project database is independent application data. Registry backups and validated registry Replace/merge/rollback do not include, overwrite, prune, or migrate it. A repository temporarily removed from the registry may have preserved project rows that are intentionally inaccessible through the service until the same repository ID is restored or relinked.

v0.4.9 project-coordination tests hash-verified the project database across registry Replace and then reopened preserved rows after rollback. It also proved project operations on a read-only repository leave all repository bytes unchanged.

ForgeTrace v0.4.10 does not provide a dedicated project-database backup/restore transaction. Protect the full application-data directory using host backup tooling. Health reports database integrity and storage pressure but has no project repair authority. Manual SQLite replacement while ForgeTrace is running is unsupported.

## Health assessment is not recovery

The Health dashboard may inspect transaction journals, startup recovery summaries, registry restore journals, rollback authority, snapshot objects, hash-index metadata, and access-mode consistency. It does not execute recovery, delete completed journals, restore objects, install backups, reconcile modes, or repair metadata.

A finding may expose the existing Doctor repair action. The owner must confirm it separately, and the HTTP/UI Doctor path requires a healthy security-ledger authorization event before repair begins. Registry restore and rollback remain available only through their existing preview/journal authorities.

## Repository recovery

Repository mutations use transaction journals under `.forgetrace/transactions/`. Snapshot restore verifies every required object before workspace replacement and assembles the target in staging. Pending journals recover when the repository opens.

Conflict-resolution drafts never write the workspace. Only the existing transactional PR merge applies verified immutable revision/resolution bytes, so merge failure retains repository rollback behavior and confirmed quarantine evidence.

## Registry recovery

Validated registry backup recovery remains the v0.4.2 authority: non-mutating Merge/Replace preview, SQLite/schema checks, SHA-256 staging, canonical logical digests, cross-process `registry.lock`, exact pre-restore backup, durable journal, post-install verification, automatic rollback, guarded explicit rollback, and startup recovery.

Registry recovery never replaces repository content/history, collaboration quarantine, immutable review revisions, conflict-resolution evidence, review conversations, jobs, or the separate security-event ledger.

## Review and conflict evidence recovery

Collaboration evidence is durable application data in `collaboration/collaboration.sqlite3`, `collaboration/review-revisions/`, and `collaboration/conflict-resolutions/`.

- SQLite transactions make thread, draft, decision, confirmation, and applied-state changes atomic.
- Revision/evidence manifests and file SHA-256 values detect damage.
- Missing, unreadable, symlinked, malformed, or changed evidence fails closed.
- Applied resolution drafts remain immutable historical evidence.
- Startup retention cleanup removes eligible terminal review/resolution evidence and orphan directories.
- There is no standalone point-in-time collaboration-history restore UI; independently back up application data when history matters.

## Access-mode and security interaction

Review and draft preparation remain available in read-only mode, but final merge is rejected by the repository mutation boundary. Protected confirmation and merge require a healthy security ledger before state or repository mutation.

## Manual Windows gate

Physical native-folder chooser acceptance is not implied by Linux recovery testing. Use `tests/WINDOWS_NATIVE_PICKER_ACCEPTANCE.md` on the Windows release machine.

## Security-history rotation recovery

Rotation has a separate hash-protected journal and exact active-database backup. Startup rolls an incomplete installation back, restores pruned segments and the prior retention root, and removes the newly installed segment. Journal artifact paths are confined to application-data rotation storage. An unreadable or incomplete journal blocks further rotation.


## v0.4.8 Git intelligence recovery boundary

Git intelligence has no recovery or repair authority because it writes no Git or repository state. Timeouts, unavailable Git, unsupported metadata, corrupt objects, or bounded-output failures return explicit read errors. Existing ForgeTrace transaction, registry, snapshot, security-history, and collaboration recovery paths are unchanged.
