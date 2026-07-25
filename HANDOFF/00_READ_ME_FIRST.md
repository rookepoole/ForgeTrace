# ForgeTrace v0.4.0 Development Handoff — Read First

This package is the authoritative continuation point after the v0.3.6 comprehensive audit and v0.4.0 stabilization pass.

## Source of truth

- Exact source: the package root
- Current version: `0.4.0`
- Creator/project lead: Rooke Poole
- License: MIT
- Audit closure: `AUDIT_CLOSURE.md` and `AUDIT_CLOSURE.json`
- Roadmap: `BUILD_PLAN.md`
- Test evidence: `HANDOFF/EVIDENCE/`

Do not restart from an older v0.3.x ZIP. Do not regress the transaction, locking, snapshot-verification, staged-import, depth-first tree, or contributor-gateway boundaries.

## First actions in a new chat

1. Read this file.
2. Read `01_NEW_CHAT_BOOT_PROMPT.md`.
3. Read `02_AUDIT_CLOSURE_MAP.md`.
4. Read `03_ARCHITECTURE.md` and `04_TESTING_AND_VALIDATION.md`.
5. Run the test commands before editing.
6. Make changes in small gated releases and update `BUILD_PLAN.md` during development.

## Release honesty

All 29 identified v0.3.6 audit findings have an implemented remediation and automated evidence. This does not claim that unknown bugs cannot exist. The only environment-specific release gate not physically executed here is clicking the Windows native folder dialog; the implementation contracts are automated and the acceptance fixture is included.
