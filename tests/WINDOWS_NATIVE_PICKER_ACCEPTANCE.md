# Windows Native Folder Picker Acceptance

This checklist is the required physical-Windows acceptance run for ForgeTrace v0.4.0. Automated Linux CI validates the PowerShell command contract and the complete import API, but cannot display a real Windows folder dialog.

## Matrix

Run once on each available configuration:

- Windows 10 + Windows PowerShell 5.1
- Windows 11 + Windows PowerShell 5.1
- Windows 11 + PowerShell 7 (`pwsh`)
- Local NTFS path
- Path containing spaces and Unicode
- Long nested path
- UNC or mapped network path when available

## Procedure

1. Extract the release to a new folder.
2. Run `START_FORGETRACE.bat`.
3. Select **Add repository → Import local folder**.
4. In the native Windows dialog, select the prepared fixture folder.
5. Confirm the preview count, byte total, sensitive-file warning, and conflict policy.
6. Complete the import.
7. Confirm the deepest file is visible in the parent-child tree.
8. Open the deepest file and compare its SHA-256 with the fixture.
9. Rename a parent folder, then delete it and confirm both actions affect disk.
10. Restart ForgeTrace and confirm the repository repopulates automatically.

## Required evidence

Record Windows version, PowerShell version, path type, result, screenshot, repository path, and `python server.py doctor --json` output. Do not mark the release Windows-accepted without this evidence.
