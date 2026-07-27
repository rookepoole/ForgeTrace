# Start Here — ForgeTrace v0.5.3 Design Contract

## v0.5.3.0 source status

This source includes the read-only switch preflight and sealed capture planner. Start the application normally; no branch-switch button or execute route exists in this checkpoint. Continue development from `HANDOFF/00_READ_ME_FIRST.md` and `HANDOFF/29_SWITCH_PREFLIGHT_AND_SEALED_CAPTURE_PLANNER.md`.

**Runtime baseline:** v0.5.2.2  
**Design:** Transactional Switch/Checkout  
**Creator:** Rooke Poole  
**License:** MIT

## Purpose

This package contains the unchanged accepted v0.5.2.2 runtime plus the complete design contract for the future v0.5.3 branch-switch transaction. It deliberately implements no switch or checkout authority.

Read, in order:

1. `HANDOFF/00_READ_ME_FIRST.md`
2. `HANDOFF/28_TRANSACTIONAL_SWITCH_CHECKOUT_DESIGN.md`
3. `docs/TRANSACTIONAL_SWITCH_CHECKOUT_DESIGN.md`
4. `docs/TRANSACTIONAL_SWITCH_CHECKOUT_CONTRACT.json`
5. `BUILD_PLAN.md`

## Runtime launch

- Windows: double-click `START_FORGETRACE.bat`.
- macOS/Linux: `chmod +x START_FORGETRACE.sh && ./START_FORGETRACE.sh`.
- Owner workspace: `http://127.0.0.1:8765`.

The runtime still supports only selected-file staging, staged-tree commits, local branch creation, and lightweight local tags.

## Validation state

The design package adds nine focused contract/probe tests while preserving the two physical-Windows-only platform skips. The exact final counts and evidence are recorded in `HANDOFF/04_TESTING_AND_VALIDATION.md`, `PACKAGE_METADATA.json`, and `HANDOFF/EVIDENCE/v053-*`.
