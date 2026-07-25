# Secure Collaboration Architecture

ForgeTrace v0.3.3 accepts outside contributions through a quarantined capability gateway. It does **not** expose the owner workspace as a normal multi-user web application.

## One process, two listeners

```text
START_FORGETRACE.bat / START_FORGETRACE.sh
                     |
                     v
              ForgeTrace process
                 /          \
                /            \
127.0.0.1:8765               0.0.0.0:8766
owner surface                 contributor surface
(always local)                (off until UI enables it)
```

The owner launches ForgeTrace once. The owner listener is loopback-only. Selecting **Collaborate** and enabling sharing creates a second `ThreadingHTTPServer` whose server object is labeled `gateway`.

The request handler checks that listener label before client-address logic. Therefore the contributor listener remains restricted even when accessed through `127.0.0.1`. It cannot become an owner surface merely because the caller is local.

Sharing state is runtime-only and defaults to off after every restart. The UI can:

- inspect gateway status;
- choose a contributor port;
- start the restricted listener;
- display detected LAN addresses;
- use an optional private VPN/tunnel base-URL override for copied links;
- stop the listener.

The override changes only the generated link. It does not create a tunnel or configure TLS.

## Trust zones

```text
Remote contributor browser
        |
        | expiring repository-scoped token
        v
Restricted contribution listener
        |
        v
Application-data quarantine
        |
        | local owner reviews exact evidence
        v
Repository merge service under workspace lock
        |
        v
Live repository files + safety snapshot
```

### Zone 1 — remote contributor

Allowed:

- load `contribute.html`;
- validate one invitation;
- optionally download one source-only repository ZIP;
- create a pull-request draft;
- upload explicit changed/new file bytes;
- request explicit repository-relative deletions when permitted;
- submit, refresh, and revise that pull request.

Denied:

- registry and repository library APIs;
- arbitrary file browsing or raw-file reads;
- owner settings and organization;
- snapshots, full history exports, doctor, backup, or recovery APIs;
- sharing start/stop controls;
- invitation administration;
- reviews, approvals, closures, or merges;
- command execution, build hooks, archive extraction, or shell access.

### Zone 2 — quarantine

Quarantine lives under ForgeTrace application data, not the repository path. Each staged file is nested under repository UUID and pull-request UUID. Paths pass both repository-relative normalization and quarantine-root containment checks.

ForgeTrace blocks `.git` and `.forgetrace` path segments, traversal, oversized files, oversized pull requests, and excessive change counts. Script/executable suffixes are retained as untrusted bytes and marked risky; they are never run.

### Zone 3 — local owner

Owner routes call `require_local_owner()`. The client address must be loopback and the Host must be `localhost`, `127.0.0.1`, or `::1`. Cross-site owner requests and mismatched Origin/Host requests are rejected.

The owner reviews:

- unified text diffs;
- binary size and SHA-256 values;
- requested deletions;
- risky-file flags;
- baseline conflicts;
- review history and pull-request revision.

### Zone 4 — merge service

A merge requires:

1. owner-local API access;
2. approved pull-request status;
3. matching expected revision;
4. exact typed phrase `MERGE #N`;
5. explicit risky-file approval when relevant;
6. no baseline conflicts.

The repository service creates a safety snapshot, acquires the workspace mutation lock, recomputes the current manifest, and revalidates every affected path. It copies existing affected content to rollback storage, applies staged files through temporary files and `os.replace`, processes deletions, records attribution, and creates a merge snapshot. A failure restores prior files and metadata.

## Invitation design

Invitation tokens use `secrets.token_urlsafe(32)`. ForgeTrace stores only `SHA-256(token)`. The raw token is returned once and included after `#` in the contributor URL. Browsers do not send URL fragments in the initial HTTP request, reducing accidental token exposure in ordinary access logs and referrers.

Invitations include:

- repository UUID;
- expiry;
- revocation state;
- maximum pull-request count;
- maximum file bytes;
- maximum total bytes;
- deletion permission;
- source-download permission.

The token is a bearer capability. Anyone possessing it can use its remaining scope until it expires or is revoked, so send it only through a trusted channel.

## Source archive

The optional source archive is generated from the selected repository’s normal file tree with `include_history=False`. It excludes `.forgetrace` data, Git/Mercurial/Subversion/Bazaar metadata, snapshot objects, registry data, absolute paths, invite records, symlinks, and collaboration database contents. Source archives are built on disk and streamed in chunks. A 2 GB archive gate remains, and fork imports additionally reject unsafe paths, protected metadata, symlinks, encrypted entries, excessive file counts, cross-origin redirects, and expansion beyond 8 GB.

## Network operation

Normal startup binds the owner workspace to loopback. The UI-controlled gateway binds a separate restricted listener to external interfaces only after explicit owner action.

Recommended:

- trusted home/studio LAN;
- Tailscale or WireGuard private network;
- authenticated TLS reverse proxy operated by the owner.

Unsupported:

- direct router port forwarding;
- unauthenticated public cloud deployment;
- exposing the owner workspace through a generic reverse proxy;
- treating invite tokens as permanent accounts.

An operating system firewall may prompt the first time the contributor listener opens. Permit only private-network access when the platform offers that distinction.

## Shutdown behavior

- **Stop sharing** closes the contributor listener and leaves the owner workspace running.
- **Ctrl+C / closing ForgeTrace** closes both listeners.
- Restarting ForgeTrace does not automatically reopen sharing.
- Existing invitations remain stored but unreachable until sharing is enabled again.

## Threats addressed

- arbitrary path traversal;
- direct workspace writes;
- remote owner API access;
- accidental owner-route access through the contributor port from localhost;
- accidental `.git`/`.forgetrace` mutation;
- oversized request bodies;
- invite reuse beyond configured scope;
- merge after owner-side path changes;
- script execution by the server;
- active HTML/SVG/JavaScript rendering through raw file routes;
- accidental raw-token persistence.

## Remaining threats and planned controls

- persistent audit-log review;
- brute-force and distributed denial-of-service beyond basic throttling;
- contributor identity proof;
- malware and content scanning;
- TLS lifecycle management;
- line-level review abuse controls;
- signed contribution bundles;
- Git remote credential and protocol security;
- formal public-internet penetration testing.

## v0.4.0 retention and sensitive-source policy

Source bundles exclude sensitive paths unless an invitation explicitly enables them. Merged and closed pull-request quarantine is purged immediately; stale terminal data is cleaned on startup. Storage metrics are available to the owner. The gateway remains separate from the owner listener.

