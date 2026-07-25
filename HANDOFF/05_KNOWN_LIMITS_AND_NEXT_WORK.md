# Known Limits and Next Work

## Known product limits

- No Git clone/push/fetch server or credential management
- No persistent network users, roles, sessions, MFA, or identity verification
- No built-in TLS lifecycle or public-internet certification
- No integrated malware execution sandbox
- No persistent owner-visible security-event audit viewer
- No inline review threads or visual conflict editor
- No explicit read-only repository mode
- Registry backup restore exists as lower-level recovery work but lacks a complete preview/rollback UI
- Physical Windows picker acceptance must be run on a Windows release machine

## Recommended v0.4.1 order

1. Persistent append-only security event log and UI
2. Validated registry backup restore preview/rollback
3. Read-only repository enforcement
4. Inline review conversations
5. Visual quarantine-only conflict resolution
6. Doctor/integrity dashboard
7. Windows release-machine acceptance evidence
8. Read-only Git status/diff adapter

Do not begin Git remote hosting or full LAN owner access before identity, TLS, audit, and permission gates are designed and adversarially tested.
