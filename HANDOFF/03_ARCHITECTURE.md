# v0.4.0 Architecture Handoff

## Process and trust boundaries

1. **Owner process:** loopback HTTP, registry, repository services, operation jobs, and optional contributor-gateway lifecycle.
2. **Application-data lock:** prevents two owner processes from mutating the same registry/job/quarantine roots.
3. **Repository lock:** OS-backed, path/UUID-keyed, cross-process mutation serialization.
4. **Filesystem transaction:** rollback journal for create/replace/delete operations.
5. **Repository metadata:** atomic revisioned `state.json` with backup and unique temporary writes.
6. **Import staging:** source enumeration, policy classification, capacity/conflict preview, staging, SHA verification, then transactional commit.
7. **Snapshot object store:** content-addressed objects verified before restore/export.
8. **Contributor gateway:** separate restricted listener; submitted bytes remain in quarantine.

## Core modules

- `forgetrace/app.py` — application composition, owner instance lock, gateway manager
- `forgetrace/web.py` — HTTP surfaces and scoped dispatchers
- `forgetrace/registry.py` — SQLite repository registry, migrations, Doctor, discovery/relink
- `forgetrace/repository.py` — workspace operations, snapshots, exports, hash index
- `forgetrace/importing.py` — staged import engine and conflict/capacity policy
- `forgetrace/transactions.py` — filesystem rollback journals
- `forgetrace/locks.py` — cross-platform file locks
- `forgetrace/jobs.py` — persistent operation lifecycle/progress/cancel
- `forgetrace/collaboration.py` — invitations, quarantine, reviews, merge, retention
- `forgetrace/policies.py` — protected/sensitive path classification
- `forgetrace/native_picker.py` — OS folder chooser adapters
- `index.html` — owner UI and virtualized depth-first tree
- `contribute.html` — remote contribution portal

## Invariants not to regress

- Restore must never delete/replace live content before object preflight succeeds.
- A repository mutation must hold the cross-process lock.
- A failed metadata write must roll back filesystem changes.
- Import must not modify live content before staging/verification and explicit conflict policy.
- Repository rows must be depth-first parent/child, not globally sorted by depth.
- Contributor gateway routes must never infer owner trust from loopback.
