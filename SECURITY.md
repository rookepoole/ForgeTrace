# ForgeTrace Security

## v0.5.3.0 switch-planner security boundary

ForgeTrace now has an internal, owner-authority preflight and capture planner for a future local-branch switch. The planner is **not an execution authority**: it has no `execute` method, no HTTP route, no UI control, and invokes neither `git switch` nor `git checkout`.

Planning acquires the repository OS lock, then the existing repository-scoped Git mutation lock, then an internal switch lock. It fails closed for read-only policy, deletion intent, retained Git-write recovery, native Git lock files, Git administrative state, non-root or multi-worktree layouts, dirty tracked/index state, checkout-affecting configuration, unsupported tree/index modes, protected paths, special filesystem objects, collisions, or capture limits.

The planner directly scans local files—including ignored files—because native Git can overwrite an ignored file that becomes tracked on the target. Accepted bytes are copied to application data, SHA-256 verified, revalidated against a second repository analysis, atomically installed, and bound by canonical plan and capture digests. Plans expire after five minutes and are invalidated by source, target, metadata, worktree, local-byte, or executable-state drift.

This checkpoint does not weaken or replace transactional Git-write journals, deletion intents, permanent-delete confinement, security-ledger authorization, read-only enforcement, immutable review/conflict evidence, or segmented Security history.

## v0.5.2.2 acceptance-runner boundary

v0.5.2.2 changes release-validation tooling only. The PowerShell runner no longer merges Python stderr into PowerShell's error pipeline. It captures native streams into temporary files, records them as inert evidence text, and decides success solely from the native exit code. No owner, contributor, repository, Git, deletion, Security-ledger, lock, journal, rollback, recovery, credential, shell, or network authority changed.


## v0.5.2.1 crash and Windows-cleanup boundary

The v0.5.2 write authority is unchanged. v0.5.2.1 adds recovery checkpoints and file-operation hardening without adding checkout, merge, remote, credential, or hosting authority.

A checkpoint is written into the canonical-digest transaction journal before the injected crash harness can stop execution. Production code has no route, environment variable, or user input that enables injection; tests pass a constructor-only callback and raise a `BaseException` signal so the normal in-process rollback handler is deliberately bypassed. A fresh service instance must then recover only from the durable journal and captures.

Critical evidence installation uses bounded retry for transient Windows `EACCES`/`EPERM`/`EBUSY` and WinError 5/32/33/145 conditions. Exhaustion still fails closed. Cleanup that occurs after a terminal journal and verified receipt—plus expired/consumed preview and retired-receipt housekeeping—is non-critical. Failure to remove those application-data files records a maintenance warning and retains evidence; it never changes the committed Git result or triggers rollback.

Recovery diagnostics are read-only evidence. ForgeTrace never removes native Git locks, never interrupts merge/rebase/cherry-pick/revert/bisect state, and never auto-repairs tampered journals or receipts. Incomplete valid journals roll back only after repository identity/path verification and lock acquisition. Verified terminal journals reconstruct missing receipts or clean terminal evidence without reverting committed refs.


## v0.5.2 transactional local Git-write boundary

ForgeTrace has two independent Git authorities. `GitIntelligenceService` remains read-only. `GitWriteService` is owner-loopback only and supports exactly selected-file stage, staged-tree commit, local branch creation, and lightweight local tag creation.

A write requires an expiring canonical-digest preview, exact typed confirmation, repository-state revalidation, the normal OS-backed repository lock, the repository-scoped application-data Git-write lock, effective read-write policy, no active deletion intent, and healthy security-ledger authorization. Native Git lock files and merge/rebase/cherry-pick/revert/bisect state are never removed or bypassed.

Every operation writes a hash-sealed application-data transaction journal before mutation. Stage/commit capture the exact index; commit also captures `HEAD`, the current branch ref, and reflog evidence; branch/tag capture only the target ref/reflog evidence required for exact rollback. Successful transactions emit canonical-digest receipts. Incomplete journals are recovered at startup, while tampered journals or receipts are retained and fail closed. Recovery is permitted after a later read-only transition because it restores the pre-transaction state; it still defers while native Git activity is present.

The Git subprocess uses an absolute executable, `shell=False`, bounded time/output, disabled prompts, credentials, helpers, global/system configuration, hooks, editors, pagers, signing, fsmonitor, external diff/text conversion, submodule recursion, and network protocols. Commit creation uses `write-tree`, `commit-tree`, and `update-ref`. Unsupported linked/bare/symlinked/external Git administration, config includes, alternates, protected metadata paths, external clean filters, and working-tree encoding fail closed.

This authority does not checkout/switch, merge, reset, rebase, cherry-pick, revert, create annotated/signed tags, create signed commits, fetch, pull, push, clone, host repositories, execute repository content, or provide credentials/network identity.

## v0.5.1.2 Windows deletion transaction

Permanent managed-repository deletion does not bypass repository serialization. ForgeTrace first acquires the normal repository lock, validates identity and policy, writes a durable deletion intent under application data, and holds an external repository-ID guard. It then closes every ForgeTrace handle inside the repository before attempting the Windows parent-directory move. Other ForgeTrace processes observe the intent and fail closed until deletion commits or rolls back.

The registry operation lock, deletion journal, staging directory, and tombstone remain authoritative for commit, rollback, and startup recovery. Transient WinError 5/32/33 sharing violations are retried. Persistent denial returns `repository_delete_path_busy` before registry removal or tombstone commitment. Windows Restart Manager is used only for read-only diagnostics; ForgeTrace never terminates, restarts, or closes another process.

## v0.5.1.2 Security viewer resilience

The owner Security viewer loads primary events before auxiliary segmented-history status. A failure in retention policy, sealed segments, anchors, rotation journals, or storage accounting is shown as degraded auxiliary evidence and does not suppress the primary event list. This does not weaken authorization: protected actions still call full logical-chain verification and fail closed on any integrity fault.

# ForgeTrace Security Policy — v0.5.1

ForgeTrace manages real local files, quarantined outside contributions, review evidence, and recovery metadata. Keep independent backups for irreplaceable data and report vulnerabilities privately.

## Trust surfaces

- **Owner workspace:** loopback-only administration, repository access, review moderation, security evidence, registry recovery, and access-mode authority.
- **Contributor gateway:** separately bound, disabled by default, invitation-token scoped, and permanently denied owner routes.
- **Quarantine:** application-data staging for untrusted submitted bytes.
- **Immutable review revisions:** application-data copies of submitted PR revisions and available base evidence, manifest-bound and never live repository authority.
- **Conflict-resolution evidence:** application-data Base/Current/Submitted captures and owner decisions; never a direct repository write surface.
- **Repository transaction service:** cross-process locked, verified mutation boundary with service-enforced access policy.
- **Registry recovery authority:** owner-only, cross-process locked, staged, journaled, verified, and rollback-capable.
- **Security event ledger:** separate owner-readable, contributor-inaccessible evidence store.

## Integrity protections

- OS-backed repository, registry-operation, security-ledger, and owner-instance locks
- Repository transaction journals and startup rollback recovery
- Snapshot SHA-256 verification before restore/export
- Staged imports with containment, capacity, conflict, sensitive-file, and hash checks
- Protected `.forgetrace` and `.git` path segments at every depth
- Symlink/junction avoidance where repository authority would escape containment
- Sensitive source exclusion by default and explicit export inclusion
- Token hashing, expiry, revocation, maximum use, bounded throttling, and quarantine cleanup
- HTTP timeouts, `HEAD`, security headers, and attachment treatment for active formats

## v0.4.6 health-dashboard security

- Health endpoints are owner-loopback only and are rejected by the contributor gateway before dispatch.
- Report generation is assessment-only. It does not invoke transaction recovery, registry restore, snapshot restore, retention cleanup, hash-index refresh, or repository mutation.
- Report files use canonical JSON SHA-256 integrity and are regular-file checked before detail or export.
- Reports never include raw invitation tokens, credentials, file bodies from repository snapshots, or security-event secrets. Evidence details remain metadata, hashes, counts, statuses, and bounded diagnostics.
- A corrupt or non-writable security ledger appears as a critical finding. The report remains available even when its best-effort generation event cannot be appended.
- Doctor repair initiated from the owner HTTP/UI path now requires a healthy ledger authorization event before the existing repair authority begins.
- Advisory lock status is probed without creating or rewriting a lock file.
- Bounded scans disclose incompleteness; partial evidence is not promoted to a healthy complete result.
- Health history is local application data and locally tamper-evident, not externally anchored or immutable against an administrator with filesystem access.

## v0.4.5 conflict-resolution security

- Resolution evidence is stored only under `collaboration/conflict-resolutions/`; draft bytes never enter the live repository.
- Base, current, incoming, and resolved files are regular-file checked and verified by size and SHA-256. Missing, symlinked, unreadable, malformed, or changed evidence fails closed with an integrity error.
- Base evidence is accepted only from immutable revision snapshots, verified repository objects, or a locked live file whose hash still matches the recorded base.
- Submitted merge bytes come from immutable revision copies, not mutable working quarantine paths.
- Drafts bind to PR revision, full repository digest, conflict-set digest, effective access mode, unresolved current-thread digest, path, hashes, and optimistic version.
- Any binding change marks active drafts stale. Confirmation and merge require a healthy security ledger before state or repository mutation.
- Contributor listeners have no resolution routes. Only the owner listener may prepare, choose, confirm, approve, or merge.
- Base/Current/Submitted/manual content is escaped inert text. Binary or oversized evidence is never decoded or rendered as active content.
- Manual text is bounded to 512 KiB and 20,000 lines. Persistent evidence is capped at 1,000 drafts and 4 GiB per pull request, with a 16 MiB free-space reserve before capture.
- Final merge revalidates every binding and evidence file while holding the repository lock, then uses the existing filesystem transaction journal and rollback path.
- Applied drafts remain immutable historical evidence; current active drafts cannot be silently retargeted.

## v0.4.4 review-conversation security

- Review storage is under application data at `collaboration/collaboration.sqlite3` and `collaboration/review-revisions/`; it is never repository content.
- Every submitted revision records a manifest and SHA-256 for quarantined changed bytes. Review context is revalidated against that manifest before use.
- Thread paths must belong to the submitted revision and cannot traverse, become absolute, or target protected metadata.
- Context is bounded and returned as inert escaped text. `activeContentRendered` is always false; submitted HTML/SVG/JavaScript is never executed or actively embedded.
- Contributor APIs are invitation-token scoped and expose only the associated PR’s review data.
- Owner APIs require the owner listener and local-owner checks.
- Contributors cannot resolve/reopen threads or request changes. Those actions are owner-only.
- Current-revision unresolved threads block approval and merge. Historical threads never silently retarget.
- Optimistic thread versions prevent stale replies or moderation from overwriting later state.
- Persistent quotas stop unbounded thread/comment growth before insertion.
- Terminal review revisions and conversations are removed after 180 days by collaboration retention policy; terminal PR metadata remains.
- Required ledger authorization precedes owner changes-requested and resolve/reopen state changes. A damaged ledger blocks the protected action before collaboration state changes.
- Security details include only invitation fingerprints, never raw tokens.

## Repository access authority

- Registry schema 4 stores `repositories.access_mode`; repository schema 3 stores `repository.accessMode`.
- Effective mode is read-write only when both values are valid and agree on `read_write`.
- Missing, invalid, unavailable, or mismatched authority is read-only.
- Mutation checks occur while the repository OS lock is held.
- Read-only still permits safe review of quarantined changes, but merge remains blocked at the repository mutation boundary.

## Security event ledger

- `security-events.sqlite3` is separate from the package, registry, recovery journals, collaboration database, and repository metadata.
- Event/schema rows are insert-only under SQLite triggers and linked by monotonic sequence and canonical SHA-256 previous hashes.
- Startup and owner queries verify SQLite integrity, schema metadata, triggers, sequence continuity, JSON, previous hashes, and event hashes.
- Sensitive detail keys are recursively redacted and invitation evidence uses a short SHA-256 fingerprint.
- Contributor listeners cannot query, inspect, or export the ledger.

## Segmented security-history guarantees

- The logical chain spans an optional hashed retention checkpoint, verified canonical sealed segments, and active SQLite rows.
- Rotation never deletes rows in place. It seals a verified prefix, rebuilds the active suffix in staging, and installs only after an exact backup and hash-protected fsynced journal exist.
- Active immutable metadata binds the SQLite suffix to the final retained segment hash and retention-root hash.
- Missing, reordered, substituted, truncated, altered, or schema-invalid segments make full logical verification unhealthy.
- Protected operations continue to fail closed when complete retained history cannot verify or required authorization evidence cannot append.
- Retention deletes only verified whole segments after configured protected event and age minimums. The checkpoint does not reconstruct deleted event bodies.
- Rotation recovery artifact paths are confined to application-data rotation storage.
- Anchor export is offline and owner-controlled. Receipt hash and digest binding are verified locally, while independent external publication remains explicitly unverified.
- Contributor listeners cannot inspect policy, segments, journals, anchor evidence, or combined history.

## Registry recovery authority

- Only direct-child ForgeTrace backup names are eligible.
- Preview/restore use private staging, structural checks, foreign-key checks, schema controls, SHA-256 binding, and canonical logical digests.
- Restore holds `registry.lock`, creates an exact pre-restore backup, fsyncs a durable journal, and verifies after installation.
- Install failure triggers automatic rollback. Explicit rollback refuses to erase later registry work.
- Registry recovery never replaces repository content/history, quarantine, immutable review revisions, review conversations, or the security ledger.

## Deployment guidance

Use contributor sharing over a trusted LAN, Tailscale, WireGuard, or a correctly configured TLS reverse proxy. Do not directly port-forward ForgeTrace to the public internet. The owner workspace must remain loopback-only.

## Remaining limitations

- No persistent users, roles, sessions, MFA, or identity attestation
- No built-in TLS certificate lifecycle or public-internet adversarial certification
- No malware execution sandbox; ForgeTrace does not execute submissions
- No Git credentials, wire protocol, or remote hosting
- No independently verified external publication service; anchor receipts are owner-supplied local evidence
- Read-only is a ForgeTrace service policy, not an operating-system ACL

## Windows native-picker acceptance

Picker process and PowerShell contracts are automated. Physical Windows UI acceptance must follow `tests/WINDOWS_NATIVE_PICKER_ACCEPTANCE.md` on a release machine.

## Reporting

Include version, OS, minimal reproduction, expected/actual behavior, impact, and whether data loss, traversal, unauthorized access, credential exposure, quarantine escape, review-context or conflict-evidence tampering, recovery bypass, read-only bypass, or command execution is involved. Do not publish an active exploit before a mitigation is available.


## v0.5.1 permanent managed-repository deletion boundary

Permanent deletion is owner-only and applies only to a normal direct-child directory under ForgeTrace managed application data. Linked external paths, symlinks, special paths, stale registry paths, identity mismatches, and read-only repositories fail closed.

The HTTP route first requires a durable security event. Initialized repositories are serialized by the existing OS-backed repository mutation lock; the registry transaction remains protected by `registry.lock`. ForgeTrace writes a durable deletion journal, atomically moves the entire directory into application-data deletion staging, writes a tombstone, commits the registry removal, then erases staged bytes. Startup uses the committed registry row to decide rollback versus finalization.

A tombstone is not a backup and contains no repository content. It preserves only permanent-delete intent so automatic discovery and Doctor do not resurrect a UUID-bearing leftover. Explicit owner registration of a preserved copy clears the matching tombstone. Security-event history remains separate and is not deleted with repository bytes.

## v0.4.9 project-coordination trust boundary

Project coordination is a separate application-data authority at `project-coordination/project-coordination.sqlite3`, serialized by `project-coordination/project-coordination.lock`. It never writes repository files, `.forgetrace`, Git metadata, registry state, pull-request evidence, or security-history files.

Every project record is repository-ID scoped and uses optimistic versions. Bodies, comments, titles, labels, references, pages, topic counts, and repository totals are bounded. Rendering escapes input before applying a deliberately small inert Markdown subset; raw HTML/SVG/JavaScript, executable links/images, commands, hooks, and submitted code are never run.

Contributor access is denied unless an invitation explicitly carries `allow_project_participation`. This permission is independent of source download, sensitive-source access, submission, and owner authority. Permissioned contributors can create issues/discussions and comments; they cannot manage labels/milestones, moderate, inspect owner APIs, read repository files through project routes, access Git/Health/security/registry data, approve, resolve conflicts, or merge.

Owner destructive and moderation actions require a healthy tamper-evident security history. Read-only repository mode does not disable coordination because project records are outside the repository, but it continues to block every repository and Git mutation. Registry recovery replaces only registry state and leaves project coordination untouched.

Known limit: v0.4.9 does not provide a dedicated project-database backup/restore UI. Operators must protect the application-data directory using normal host backups. Health verifies project-database integrity and storage but does not repair it.

## v0.4.8 Git intelligence boundary

Git intelligence is owner-only and read-only. ForgeTrace uses an absolute Git executable without a shell and supplies a restricted environment that disables terminal prompts, credential helpers, askpass, hooks, fsmonitor, pagers, external diff/text conversion, global/system configuration, submodule recursion, and lazy partial-clone fetching. Commands are local inspection commands only and have strict time/output/count limits.

ForgeTrace detects only a root-level `.git` marker. Symlinked or special metadata, external linked-worktree administration, config includes, object alternates, path escapes, and unsupported/bare layouts fail closed with an explicit unavailable result. Remote URLs are parsed from local configuration with userinfo, query strings, fragments, and token-like material removed. Git and diff text is rendered only as escaped inert content.

The read-only Git intelligence routes remain non-mutating. v0.5.2 adds separate owner-only preview/execute routes for selected-file stage, staged-tree commit, local branch creation, and lightweight local tags. There are no contributor Git routes. ForgeTrace still does not checkout/switch, merge, reset, rebase, cherry-pick, revert, change Git configuration, fetch, pull, push, clone, contact a remote, run hooks, invoke credentials/helpers, sign commits/tags, or execute repository content.


## v0.5.1 board authority

Boards are stored in a separate SQLite database under application data. Contributor access requires an explicit project-participation invitation and board-level view permission; card movement additionally requires board-level move permission. No board route has repository or Git mutation authority.


## Verified release and artifact boundary

- Release metadata and assets live under application data, not repository or Git content.
- Draft records are mutable; published records and their assets are immutable at both service and SQLite-trigger layers.
- Every asset is verified by exact size and SHA-256 before publish, download, export, and Health inspection.
- Release notes use the inert Markdown renderer; assets are never executed, installed, or actively rendered.
- Contributor access requires both explicit project-participation permission and per-release opt-in and remains download-only.
- ForgeTrace does not claim remote publication, malware safety, provenance authenticity, or trust merely because a hash matches.
- Abandoned drafts may be removed after 180 days; published evidence is not automatically pruned.

## v0.5.3 transactional switch/checkout design boundary

The design package adds no runtime authority. A future first implementation is restricted to switching between existing direct local branches from a clean index and clean tracked worktree. It must directly inventory and seal all bounded untracked and ignored regular-file bytes because native Git may overwrite ignored collisions.

Automatic rollback may overwrite only exact known pre-state, target-state, or expected-missing values. Unknown bytes, refs, reflogs, index values, or `HEAD` state require retained evidence and manual inspection. Pending switch journals block permanent deletion; later read-only mode does not block exact recovery. The switch service must share the existing repository/Git mutation locks and remain separate from pull-request quarantine conflict resolution.

Detached/path checkout, force/discard, three-way checkout, merge, submodules, sparse/split index, filters/encodings, linked worktrees, remotes, credentials, hooks, shell execution, and contributor access remain prohibited.
