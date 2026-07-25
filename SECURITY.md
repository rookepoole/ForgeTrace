# Security Policy

ForgeTrace manages real local files and repository history. Security reports should be handled carefully and should not include private repository contents unless they are essential to reproduce the issue.

## Current support status

The current build is an early local-first development release. It should bind only to `127.0.0.1` and is not approved for exposure to the public internet.

## Report a vulnerability

Until a private security channel is established, contact the project maintainer through the repository’s designated private contact method. Do not publish a working exploit before a fix or mitigation is available.

Include:

- affected version;
- operating system;
- minimal reproduction steps;
- expected and actual behavior;
- security impact;
- whether path traversal, command execution, credential exposure, unauthorized access, or data loss is involved.

Avoid attaching unrelated private files or credentials.

## Security priorities

- path containment and symlink/junction safety;
- protected `.forgetrace` metadata;
- archive extraction safety;
- upload and resource limits;
- atomic writes and recovery;
- safe subprocess invocation;
- secret redaction;
- local-only network binding by default;
- authenticated and authorized LAN mode before network collaboration ships.

See the security and testing sections of `BUILD_PLAN.md` for the required threat model and release gates.
