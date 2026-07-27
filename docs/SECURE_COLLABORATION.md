# Secure Collaboration — ForgeTrace v0.5.2

## v0.5.2 Git isolation

The contributor listener exposes neither read-only Git intelligence nor transactional Git-write routes. Invitation permissions cannot grant stage, commit, branch, tag, status, diff, history, remote metadata, Health, recovery, or security-ledger access. The new writer is local-owner only and does not expand the trusted-LAN contributor model.

## v0.4.10 repository-deletion isolation

Permanent managed-repository deletion is owner-listener only. The contributor listener has no route to unregister, delete, tombstone, restore, discover, or re-register repositories. Deletion never derives authority from an invitation and never follows a linked external path. Existing collaboration quarantine and permissions remain separate application data.

## Explicit project-participation capability

Collaboration schema 6 adds `allow_project_participation`, default false. Source download, sensitive-source access, submission, and project participation are independent permissions. No pre-v0.4.9 or ordinary invitation acquires project access automatically.

A permissioned contributor may use only the contributor project overview/list/detail/create/comment routes. Topic locking is enforced by the service. Contributors cannot manage labels/milestones, change state/assignment/due date/pin/lock/accepted answer, moderate or delete content, inspect owner routes, access repository files through the project API, read Git/Health/security/registry data, approve, resolve conflicts, or merge.

Contributor-provided project text is escaped and rendered inert. Project participation writes only the dedicated application-data database and cannot mutate quarantine revision evidence or the live repository.

## Health boundary

The contributor listener cannot generate, list, inspect, or export health reports and cannot invoke Doctor repair. Collaboration health is computed only from the owner side using bounded metadata and immutable-evidence verification; reports do not expose raw invitation tokens or submitted file bodies.

## Boundary

ForgeTrace collaboration uses a separately identified contributor HTTP listener. Invitations are token-scoped, hashed at rest, time/use limited, revocable, rate-limited, and bound to one repository. The contributor listener cannot access owner repository administration, security events, registry recovery, access-mode changes, snapshot restore, review moderation, conflict-resolution decisions, approval, or merge.

## Submission, review, and conflict flow

1. Owner creates an invitation.
2. Contributor creates a draft PR in application-data quarantine.
3. Contributor uploads bounded changed files and optional deletions.
4. Submission freezes a revision identity and captures immutable submitted evidence plus available base evidence.
5. Owner and contributor discuss that revision using inline threads.
6. Contributor may submit a newer revision; earlier conversations remain historical.
7. When live repository bytes diverge from the submitted base, the owner prepares quarantine-only conflict drafts.
8. ForgeTrace preserves Base/Current/Submitted evidence and accepts an explicit current/incoming/manual/delete decision.
9. The owner resolves current threads, confirms every conflict decision, approves, and merges only after lock-time revalidation.

Submitted code is never executed.

## Immutable evidence

Submitted revision copies live under `collaboration/review-revisions/`. Conflict evidence and owner drafts live under `collaboration/conflict-resolutions/`.

Evidence is accepted only when path containment, regular-file status, expected size, and SHA-256 all verify. Base evidence is never fabricated. Active drafts bind to the exact PR revision, repository digest, access mode, conflict set, unresolved-thread gate, path, and optimistic version.

## Active-content safety

Review and conflict content is escaped inert text, not active HTML. HTML, SVG, JavaScript, and other executable/risky formats may be compared as hashes/metadata or bounded text, but ForgeTrace does not render or execute them. Binary, invalid UTF-8, and oversized files cannot use manual inline resolution.

## Authority and merge gates

- Owner and contributor can create review threads and replies.
- Only the owner can moderate threads, request changes, prepare conflict evidence, save/confirm decisions, approve, or merge.
- Current unresolved threads block approval and merge.
- Every live conflict requires a confirmed current draft.
- Contributor-created current discussion invalidates stale approval.
- Any repository/PR/access/review/conflict binding drift makes an active draft stale.
- Final merge revalidates all authority/evidence under the repository lock and uses the existing transaction journal.
- Non-conflicting changes come from immutable revision copies rather than mutable working quarantine bytes.

## Concurrency and abuse controls

Review threads and conflict drafts use optimistic versions. Persistent limits are enforced before insertion/capture:

- 500 threads/PR; 500 comments/thread; 5,000 comments/PR
- 8,000 characters/comment; 200 review-context lines
- 1,000 resolution drafts/PR; 4 GiB resolution evidence/PR
- 512 KiB and 20,000 lines/manual resolution
- 16 MiB free-space reserve for conflict capture
- page maximum 100; terminal retention 180 days

## Evidence and privacy

Security events record request IDs, actors, invitation fingerprints, decisions, hashes, sizes, and outcomes. They never store raw invitation tokens or submitted/resolved file bodies. Required owner confirmation and merge authorization fail closed if the ledger is unhealthy.

## Deployment posture

Use collaboration only over a trusted LAN or private VPN. Do not directly expose ForgeTrace to the public internet. ForgeTrace has no persistent network identity system, MFA, built-in TLS lifecycle, or malware sandbox.

## v0.4.7 security-history boundary

Contributors cannot list events or segments, inspect or change retention policy, preview or execute rotation, export chain-head digests, or record anchor receipts. Review and merge authorization continue to depend on full retained logical-chain health.


## v0.4.8 Git intelligence boundary

Git status, history, refs, diffs, and remote metadata are owner-only. Invitation-scoped contributor routes do not expose Git intelligence, Git configuration, remote names, branches, tags, commits, or repository working-tree status. v0.5.2 still adds no contributor Git read, mutation, or remote-hosting capability; the transactional writer is local-owner only.
