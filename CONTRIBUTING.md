# Contributing to ForgeTrace

Thank you for helping improve ForgeTrace. The project was created by **Rooke Poole** and is released under the MIT License.

## Development priorities

Follow `BUILD_PLAN.md`. Work should advance the active phase and satisfy its exit gate before broadening scope. The current priority is a hardened multi-repository foundation with safe support for multiple repository paths.

## Contribution workflow

1. Open or select an issue describing the problem or feature.
2. Keep changes focused enough to review and test.
3. Add or update tests for behavior and failure cases.
4. Preserve compatibility with normal files and standard Git repositories.
5. Update documentation and `CHANGELOG.md` when behavior changes.
6. Include a clear explanation of risks, migration effects, and recovery behavior.

## Required standards

- Never weaken path containment or metadata protection.
- Never introduce silent network uploads.
- Never store credentials in repository metadata or logs.
- Do not add a destructive action without confirmation and recovery design.
- Keep project data exportable through documented formats.
- Treat non-code contributions as first-class work.
- Avoid adding dependencies without a specific reliability or maintainability benefit.

## Testing

Run the existing smoke test:

```bash
python tests/smoke_test.py
```

New systems should add unit, integration, end-to-end, security, and performance tests as described in `BUILD_PLAN.md`.

## Commit guidance

Use clear imperative messages, for example:

```text
Add SQLite repository registry
Prevent duplicate normalized paths
Test repository isolation across drives
```

## Sign-off

By contributing, you certify that you have the right to submit the work under the project’s MIT License. A lightweight Developer Certificate of Origin sign-off may be adopted as the contributor base grows.
