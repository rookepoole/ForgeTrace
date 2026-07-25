# Start Here — ForgeTrace v0.4.0

ForgeTrace is a local-first repository workspace created by **Rooke Poole** and released under the MIT License.

## Launch once

- Windows: double-click `START_FORGETRACE.bat`
- macOS/Linux: run `./START_FORGETRACE.sh`

The owner UI opens at `http://127.0.0.1:8765`. If that port is occupied, close the older ForgeTrace process instead of assuming the new package started.

## Safest first workflow

1. Choose **+ Repository**.
2. Add an existing path, create a managed repository, import a local folder, or fork a trusted collaboration link.
3. Review the import preview, including conflicts, sensitive paths, size, and available space.
4. Start the job and watch progress. Cancellation is non-destructive.
5. Create a snapshot before risky work.
6. Keep an independent backup for irreplaceable projects.

## Existing repositories after an update

Repositories and registry data live in platform application data or in the repository’s own `.forgetrace` directory—not inside the extracted release folder. On startup, ForgeTrace:

- opens the existing registry;
- recovers interrupted transaction journals;
- scans the managed-repository root for embedded UUIDs;
- relinks a moved managed repository when the UUID matches;
- leaves unavailable external repositories visible for manual relinking.

Do not copy old runtime data into the new package folder.

## Folder import

The primary local-folder workflow uses an operating-system folder chooser and a server-side staged importer. It preserves nested files and empty folders, rejects nested `.forgetrace` metadata, previews conflicts, verifies size/SHA-256, and commits transactionally. The browser directory uploader is a compatibility fallback.

## Restore safety

v0.4.0 verifies every snapshot object before touching the current workspace. Missing or corrupt objects cause a preflight failure. Restore stages the target tree and uses rollback recovery rather than deleting the live tree first.

## Outside collaboration

1. Select a repository and open **Collaborate**.
2. Enable sharing and generate an expiring repository-scoped link.
3. Share it only over a trusted LAN/private VPN.
4. Review quarantined changes locally.
5. Approve and type the required merge confirmation.

Sensitive source files are excluded unless the owner explicitly permits them. Terminal pull-request quarantine is cleaned by policy.

## Continue development

Open `HANDOFF/00_READ_ME_FIRST.md`. The exact new-chat boot prompt is in `HANDOFF/01_NEW_CHAT_BOOT_PROMPT.md`.
