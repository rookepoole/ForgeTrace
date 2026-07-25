# ForgeTrace Security Policy — v0.4.0

ForgeTrace manages real local files. Keep independent backups for irreplaceable data and report vulnerabilities privately.

## Trust surfaces

- **Owner workspace:** loopback-only administration and repository access.
- **Contributor gateway:** separately bound, disabled by default, token-scoped, and permanently denied owner routes.
- **Quarantine:** application-data staging for untrusted submitted bytes.
- **Repository transaction service:** local, locked, verified mutation boundary.

## v0.4.0 protections

- OS-backed repository and application instance locks
- Transaction journals and startup rollback recovery
- Snapshot SHA-256 verification before restore/export
- Staged imports with containment, free-space, conflict, sensitive-file, and hash checks
- Protected `.forgetrace` paths at every depth
- Symlink/junction avoidance in import and source archive paths
- Sensitive source exclusion by default and explicit export inclusion
- Request/file/count/total limits and bounded remote rate maps
- Contributor route isolation, token hashing, expiry, revocation, and maximum use
- Quarantine cleanup after terminal pull requests
- HTTP timeouts, HEAD handling, security headers, and active-content attachment behavior

## Deployment guidance

Use contributor sharing over a trusted LAN, Tailscale, WireGuard, or a properly configured TLS reverse proxy. Do not directly port-forward the gateway to the public internet. The owner workspace must remain loopback-only.

## Remaining limitations

- No persistent users, roles, sessions, MFA, or identity attestation
- No built-in TLS certificate lifecycle
- No integrated malware execution sandbox
- No public-internet adversarial certification
- No Git credential storage or Git remote server
- No persistent owner-visible security-event audit viewer yet

## Windows native-picker acceptance

Picker process and PowerShell contracts are automated. Physical Windows UI acceptance must follow `tests/WINDOWS_NATIVE_PICKER_ACCEPTANCE.md` on a release machine.

## Reporting

Include version, OS, minimal reproduction, expected/actual behavior, impact, and whether data loss, traversal, unauthorized access, credential exposure, or command execution is involved. Do not publish an active exploit before a mitigation is available.
