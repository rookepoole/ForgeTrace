# ForgeTrace — Local-First Repository Workspace

**Created by Rooke Poole. Open source under the MIT License.**

ForgeTrace v0.4.0 is a stabilized local-first repository workspace for managing multiple real folders, attributable project activity, restorable SHA-256 snapshots, portable exports, and owner-reviewed outside contributions without requiring a cloud account.

## Start

### Windows

Double-click `START_FORGETRACE.bat`.

### macOS / Linux

```bash
chmod +x START_FORGETRACE.sh
./START_FORGETRACE.sh
```

The owner workspace opens at `http://127.0.0.1:8765`. Sharing is off until enabled from the selected repository’s **Collaborate** panel.

## v0.4.0 stabilization highlights

- Cross-process repository locking and one owner process per application-data directory
- Transactional file, folder, import, merge, and restore operations with rollback journals
- Snapshot preflight and SHA-256 verification before workspace mutation
- Empty-directory, file-mode, and timestamp preservation in snapshots
- Staged imports with conflict preview, free-space preflight, progress, cancellation, and byte-level verification
- Atomic new-repository imports that cannot leave an orphan README-only repository
- True depth-first, parent-child, virtualized file tree
- Working folder rename and delete controls
- Incremental file-hash cache rather than full rehashing on every refresh
- Doctor recovery from valid `state.json.bak` and pending transaction journals
- UUID-first managed-repository rediscovery and moved-path relinking
- Sensitive-file preview and explicit inclusion controls for import/share/export
- Locked immutable exports
- Contributor quarantine cleanup and storage metrics
- HTTP timeouts, HEAD support, bounded rate-limit maps, and split route handlers

The complete closure of the v0.3.6 audit is documented in [`AUDIT_CLOSURE.md`](AUDIT_CLOSURE.md).

## Repository onboarding

Choose **+ Repository** and use one of four paths:

1. **Upload files** — create a managed repository from selected files.
2. **Import local folder** — use the operating-system picker and transactional server-side directory import.
3. **Fork shared link** — create a managed local fork from a ForgeTrace collaboration link before any repository exists.
4. **Use a local path** — create or register a repository at an exact absolute path.

Imports preview conflicts and sensitive paths before mutation. Long imports expose persistent progress and can be cancelled safely.

## Collaboration boundary

ForgeTrace uses a separate restricted contributor listener. Remote invitees can download an allowed source bundle and submit quarantined changes, but they cannot browse the owner filesystem, access the registry, edit the live workspace, restore snapshots, or merge pull requests. The local owner reviews exact evidence and approves the merge.

Use contributor sharing only over a trusted LAN or private VPN. Do not directly port-forward ForgeTrace to the public internet.

## Storage

Application data is stored outside the extracted package:

- Registry and settings: platform application-data directory
- Managed repositories: `managed-repositories/`
- Import/fork transfers and persistent job history: application-data storage
- Repository history: `<repository>/.forgetrace/`

Replacing the application package does not replace these data locations. Startup discovery repopulates valid managed repositories by embedded UUID.

## Validate the source

```bash
python -m unittest discover -s tests -v
python tests/browser_smoke_test.py
PYTHONPATH=. python tests/browser_blackbox_test.py
```

See [`HANDOFF/04_TESTING_AND_VALIDATION.md`](HANDOFF/04_TESTING_AND_VALIDATION.md) for the complete matrix.

## Important limitations

ForgeTrace is not yet a Git wire-protocol host. It does not provide `git clone`, `git push`, persistent user accounts, MFA, built-in TLS certificate management, or public-internet hardening. Physical Windows native-picker acceptance remains a release-machine test described in `tests/WINDOWS_NATIVE_PICKER_ACCEPTANCE.md`.

## Development

Start with [`HANDOFF/00_READ_ME_FIRST.md`](HANDOFF/00_READ_ME_FIRST.md), then paste [`HANDOFF/01_NEW_CHAT_BOOT_PROMPT.md`](HANDOFF/01_NEW_CHAT_BOOT_PROMPT.md) into a new development chat.
