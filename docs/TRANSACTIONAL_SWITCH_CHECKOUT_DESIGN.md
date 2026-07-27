# ForgeTrace v0.5.3 — Transactional Switch/Checkout Design Contract

**Status:** Read-only preflight and sealed capture planner implemented; switch execution remains absent.  
**Runtime baseline:** ForgeTrace v0.5.3.0  
**Creator:** Rooke Poole  
**License:** MIT

## 0. Implemented checkpoint: preflight and sealed capture only

`forgetrace.git_switch.GitSwitchService` now implements the bounded read model, fail-closed eligibility analysis, exact source/local evidence capture, canonical sealing, and later plan verification required by this design. It performs no worktree or Git metadata mutation and has no execute method, route, or UI. Sections describing execution, transaction journals, rollback, security events, and owner controls remain mandatory future gates rather than claims of this package.

## 1. Decision

The first switch/checkout slice will expose one owner-only operation: **switch from the currently attached local branch to an already-existing local branch**. It will not expose Git's broader checkout surface.

The implementation must use a dedicated `GitSwitchService`. It may reuse the hardened Git executable boundary and must share the existing repository lock and repository-scoped Git mutation lock, but it must not silently broaden the accepted v0.5.2 `stage`, `commit`, `create_branch`, and `create_tag` operation set.

The proposed operation ID is `switch_branch`, its exact confirmation is `SWITCH BRANCH`, and its preview expires after five minutes.

## 2. Accepted first slice

A switch is eligible only when all of the following are true:

1. The repository is a registered, writable, repository-root worktree with a regular `.git` directory.
2. Exactly one worktree is present. Linked worktree administration, gitfiles, and bare repositories remain unsupported.
3. `HEAD` is born and symbolically attached to a direct `refs/heads/*` local branch.
4. The target is a different, already-existing, direct local branch ref whose commit and tree are resolved and preview-bound.
5. The index exactly matches the source `HEAD` tree: no staged, intent-to-add, unmerged, skip-worktree, or assume-unchanged entries.
6. Every tracked worktree path exactly matches the index: no unstaged tracked changes.
7. Bounded untracked and ignored regular files may remain only when they have no file/directory, case-fold, or ancestor collision with the target tree. Their bytes are captured and verified unchanged.
8. The source and target trees contain only supported regular-file entries. Symlinks, gitlinks, submodules, junctions, reparse points, devices, sockets, FIFOs, and other special entries fail closed.
9. Checkout-affecting filters, `working-tree-encoding`, sparse checkout/index, split index, external attributes, and other ambiguous materialization features fail closed.
10. The exact backup estimate is within limits and the application-data filesystem has sufficient free space before mutation begins.

The initial limits are 10,000 affected tracked paths, 5,000 untracked/ignored entries, 512 MiB of captured bytes, and a separate 64 MiB free-space reserve. Exceeding a limit produces an explicit unsupported result, not a partial switch.

## 3. Explicit exclusions

The following are not aliases or hidden modes of `switch_branch`:

- detached checkout;
- checkout by commit, tag, remote-tracking ref, or arbitrary revision expression;
- path checkout or `restore`;
- create-and-switch (`switch -c` or `-C`);
- orphan branches;
- force, discard-changes, or three-way merge checkout;
- merge, reset, rebase, cherry-pick, revert, or stash;
- branch or tag deletion;
- remote branch guessing, tracking setup, fetch, pull, push, clone, or remote contact;
- submodule recursion;
- hooks, signing, editors, credentials, helpers, shell execution, or repository code execution.

A target branch must be chosen from the bounded owner read model and revalidated by the backend. User input is never passed as an arbitrary revision expression.

## 4. Why a clean tracked state is mandatory

Git can carry non-conflicting staged or unstaged edits across a native switch, but doing so makes ownership of interrupted bytes ambiguous. The first ForgeTrace slice therefore requires the index and all tracked worktree bytes to match the source branch exactly.

This restriction provides a deterministic two-state model for every affected tracked path:

- the exact source pre-state; or
- the exact target post-state.

Recovery may automatically overwrite only bytes that match one of those known states. A path containing any third value is treated as possible external user activity, retained for manual inspection, and never silently overwritten.

## 5. Untracked and ignored files are part of the safety boundary

Native Git may overwrite an ignored file when the target branch begins tracking the same path. Therefore `git status --untracked-files=all` is insufficient. ForgeTrace must scan the bounded worktree directly, excluding `.git` and `.forgetrace`, and classify every non-index path including ignored files.

Before mutation it must:

1. reject every target file/directory or case-fold collision;
2. reject symlinks, junctions, reparse points, or special filesystem objects;
3. record path, kind, size, mode, and SHA-256;
4. copy every accepted regular file into sealed application-data backup storage;
5. record required empty directory topology for affected paths; and
6. prove sufficient free space for the capture and atomic temporary files.

After a successful switch, every preserved untracked/ignored file must remain byte-identical. During recovery, changed bytes outside the known pre/target states force manual inspection.

## 6. Preview and exact state binding

The five-minute preview must be a canonical-digest sealed document binding at least:

- repository ID, canonical path, access mode, and deletion-intent state;
- absolute Git executable path, version, file size, and modification identity;
- object format and supported root `.git` layout;
- source `HEAD` ref/OID/tree and target ref/OID/tree;
- exact index size and SHA-256;
- clean tracked status digest;
- source and target direct-ref and branch-reflog verification state;
- `HEAD` and `logs/HEAD` state;
- relevant local Git configuration and checkout-affecting attribute result;
- affected tracked-path manifest with source/target raw-blob SHA-256 and modes;
- complete bounded untracked/ignored manifest and backup estimate;
- case-sensitivity/case-fold collision analysis;
- native Git locks and active administrative-state absence; and
- the required typed confirmation `SWITCH BRANCH`.

Execute must recompute the complete state under both locks. Any drift invalidates the preview before security authorization or mutation.

## 7. Locking and competing authorities

Lock order is fixed:

1. repository OS lock;
2. the existing repository-scoped Git mutation lock used by v0.5.2 writes;
3. switch-service internal transaction lock.

This prevents stage, commit, branch/tag creation, branch switching, managed deletion, and repository mutation from racing through ForgeTrace. Native Git lock files and administrative state are never removed or bypassed.

New preview and execute requests fail closed when:

- the repository is read-only;
- an external deletion intent exists;
- another ForgeTrace Git transaction is active;
- a native Git lock exists; or
- merge/rebase/cherry-pick/revert/bisect/sequencer state exists.

A later read-only transition does not block exact rollback of an already-started switch. A pending switch journal must block permanent deletion until it is recovered or terminally resolved.

## 8. Durable evidence captured before mutation

Evidence lives under application data `git-switches/`, never inside the repository. A sealed transaction must capture:

- the `HEAD` file;
- the Git index;
- `logs/HEAD`;
- source and target ref/OID and branch-reflog verification state;
- exact bytes and mode for every affected tracked source path;
- exact bytes and mode for every bounded untracked or ignored regular file;
- directory topology needed for idempotent restoration;
- the target raw-blob SHA-256/mode manifest;
- preview, state, manifest, and backup digests;
- Git/environment identity used for execution; and
- checkpoint and recovery classification evidence.

Every backup file is individually hashed. The aggregate manifest, journal, and eventual receipt are canonical SHA-256 sealed. Mutation cannot begin until all required backup bytes and the journal are fsynced.

## 9. Proposed native command boundary

The only switch command is conceptually:

```text
git switch --no-guess --no-recurse-submodules <validated-existing-local-branch>
```

It runs through the accepted absolute executable, no-shell subprocess boundary with prompts, credentials, helpers, global/system configuration, hooks, fsmonitor, submodule recursion, network protocols, pagers, and editors disabled. `--detach`, `-c`, `-C`, `--orphan`, `--discard-changes`, `--force`, `--merge`, and branch guessing are forbidden.

The design intentionally chooses native `git switch` rather than independently reimplementing Git's index/worktree transition. Safety comes from strict preflight, disabled extensibility, complete backups, exact post-verification, and conservative recovery ownership—not from trusting the command to be transactional across process termination.

## 10. Required post-state

A switch is committed only after all of these conditions pass:

1. `HEAD` is a symbolic ref to the exact target `refs/heads/*` ref.
2. resolved `HEAD` equals the preview-bound target OID.
3. `git write-tree` for the installed index equals the target tree OID.
4. every affected tracked path matches its target raw-blob SHA-256 and expected mode;
5. every accepted untracked/ignored path remains byte-identical;
6. source and target branch refs are unchanged;
7. source and target branch reflogs are unchanged;
8. `logs/HEAD` differs from its pre-state only by one expected source-to-target switch entry;
9. no native lock or active administrative state remains; and
10. read-only Git intelligence independently reports the expected target branch and clean tracked state.

If a condition fails, the service enters recovery classification rather than declaring success.

## 11. Recovery ownership and exact rollback

Startup and in-process recovery first verify the journal and every backup digest. They then classify each captured item as:

- exact pre-state;
- exact target-state;
- expected missing state; or
- unknown.

Automatic rollback is allowed only when every item is in a known state. A mixed set of source and target states is a recoverable interrupted switch. Any unknown byte, ref, reflog, index, or `HEAD` value retains the transaction and requires manual inspection.

Recovery defers while any native lock or Git administrative state remains, including `index.lock`, `HEAD.lock`, `logs/HEAD.lock`, packed-ref/ref/reflog locks, merge/rebase/cherry-pick/revert/bisect state, or a sequencer directory.

Idempotent rollback order is:

1. restore or remove affected tracked paths and restore preserved untracked/ignored bytes;
2. restore the index;
3. restore `HEAD`;
4. restore `logs/HEAD`;
5. verify the exact source worktree/index/HEAD state and unchanged branch refs/reflogs;
6. write a sealed rollback receipt; and
7. remove the terminal journal only after receipt durability.

Checkpoints must allow a fresh process to restart rollback after any restore step without losing evidence.

## 12. Separation from quarantine conflict resolution

ForgeTrace's quarantine conflict-resolution authority applies immutable submitted pull-request evidence to the live repository after explicit owner decisions. A branch switch is unrelated local Git worktree movement.

The switch service must never:

- consume or modify pull requests, submitted revisions, review threads, approvals, or conflict drafts;
- reuse the conflict-resolution UI as a branch-merge UI;
- invoke `git switch --merge` or any three-way checkout;
- synthesize conflict markers; or
- represent a branch switch as a pull-request merge.

A switch that cannot complete as a clean two-tree transition is rejected.

## 13. Owner API, UI, Health, and evidence

Planned owner-only routes:

- `POST /api/repositories/{repositoryId}/git-switch/preview`
- `POST /api/repositories/{repositoryId}/git-switch/execute`
- `GET /api/repositories/{repositoryId}/git-switch/status`

The contributor listener exposes none of them.

The owner Git tab should show source and target refs/OIDs, affected path and backup counts, untracked/ignored preservation, unsupported reasons, exact confirmation, pending recovery, receipts, and maintenance warnings. Read-only Git status/history/diffs must remain available when the switch authority is degraded.

Planned security events are authorization, completion, rollback, and startup recovery. Events contain IDs, refs, OIDs, counts, digests, outcome, and request IDs—not file bodies, commit messages, credentials, or raw backup content.

Health is evidence-only. It may report pending, deferred, terminal-cleanup, tampered, or manual-inspection switch transactions but has no direct repair route.

## 14. Required implementation tests

The implementation release cannot be accepted without tests covering:

- every preflight restriction and limit;
- source/target drift after preview;
- ignored and untracked collisions;
- case-only and directory/file transitions;
- hook suppression and absence of prompts/network/helpers;
- source and target refs/reflogs remaining unchanged;
- exact successful post-state;
- command failure before and after partial filesystem change;
- deterministic crash injection at every durable boundary;
- fresh-process rollback from every known mixed pre/target state;
- native-lock deferral;
- unknown-byte/manual-inspection behavior;
- read-only and deletion-intent races;
- pending-journal deletion blocking;
- tampered journal, manifest, backup, and receipt evidence;
- contributor-gateway denial and read-only Git independence;
- bounded Windows sharing violations, read-only file attributes, case behavior, long paths, and endpoint-protection interference; and
- a real owner-browser workflow from the exact packaged archive.

## 15. Design-package invariant

This design package must not add `switch_branch` to the runtime operation enum, add a switch/checkout API route, add a UI control, or execute `git switch`/`git checkout`. Its purpose is to make the implementation boundary reviewable before repository worktree mutation is introduced.

The machine-readable companion is `docs/TRANSACTIONAL_SWITCH_CHECKOUT_CONTRACT.json`.
