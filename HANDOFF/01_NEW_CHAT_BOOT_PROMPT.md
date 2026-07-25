# New Chat Boot Prompt

Copy the text below into the new development chat and attach the complete handoff ZIP.

---

You are continuing development of **ForgeTrace** from the accepted **v0.4.0 Audit Stabilization and Transactional Recovery** baseline created by Rooke Poole.

Read these files first, in order:

1. `HANDOFF/00_READ_ME_FIRST.md`
2. `HANDOFF/02_AUDIT_CLOSURE_MAP.md`
3. `HANDOFF/03_ARCHITECTURE.md`
4. `HANDOFF/04_TESTING_AND_VALIDATION.md`
5. `HANDOFF/05_KNOWN_LIMITS_AND_NEXT_WORK.md`
6. `HANDOFF/06_FILE_AND_DATA_LAYOUT.md`
7. `BUILD_PLAN.md`
8. `AUDIT_CLOSURE.md`
9. `SECURITY.md`
10. `PRODUCT_SPEC.md`

Development rules:

- Work only from this exact source package; do not use an older ForgeTrace release.
- Preserve the MIT license and Rooke Poole creator credit.
- Do not call Google Drive or GitHub unless I explicitly ask.
- Never weaken cross-process locks, transaction journals, snapshot object verification, staged imports, repository isolation, or contributor-gateway isolation.
- Do not report a fix complete until unit/integration tests and the relevant real browser workflow pass.
- Update `BUILD_PLAN.md`, `CHANGELOG.md`, and handoff evidence as development proceeds.
- Keep application data outside the extracted package and preserve upgrade/recovery behavior.
- Be honest about environment-specific tests. The Windows native picker has an included physical acceptance checklist that still needs a Windows release-machine run.

First, inspect and summarize the accepted baseline and recommend the next best move from `HANDOFF/05_KNOWN_LIMITS_AND_NEXT_WORK.md`. Do not modify files until the baseline review is complete.

---
