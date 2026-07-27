# Windows Transactional Local Git Writes Acceptance — v0.5.2.2

This is the physical Windows acceptance gate for the v0.5.2.2 package carrying the v0.5.2.1 durability hardening. Linux failure-injection results do not substitute for this platform run.

> v0.5.2.2 repairs the Windows PowerShell 5.1 native-stderr handling defect. A passing unittest progress line must no longer terminate the gate as `NativeCommandError`.

## Automated evidence

1. Extract the exact v0.5.2.2 release to a new directory.
2. Close older ForgeTrace, Git GUI, editor, terminal, and Explorer preview processes using the disposable repository or ForgeTrace application-data folders.
3. From PowerShell in the release root, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\tests\run_v0522_windows_git_write_acceptance.ps1
```

The runner records Windows, PowerShell, Python, Git, and filesystem versions, then runs the inherited v0.5.2 suite, the v0.5.2.1 crash/failure-injection suite, and the v0.5.2.2 runner-contract suite. Preserve the generated `windows-v0522-git-write-acceptance.log`.

## Owner-browser smoke

1. Launch ForgeTrace and register or create a disposable root-level Git worktree.
2. Modify two files. Select only one file in Git, preview `STAGE`, confirm, and verify only that path is staged.
3. Preview and execute `COMMIT` with explicit name/email. Verify the author and confirm that no hook, editor, signing, credential, or helper prompt appears.
4. Create a local branch and lightweight tag. Verify the current branch and worktree do not change.
5. Confirm four verified receipts and Security authorization/completion evidence.
6. Set the repository read-only and confirm all four previews fail closed.
7. Create a known test `.git/index.lock`; confirm ForgeTrace refuses the write, reports `index.lock`, and does not remove it.
8. Remove only that known test lock. Restart ForgeTrace and confirm Health reports no incomplete Git transaction.
9. While ForgeTrace is closed, temporarily hold an application-data preview file open with a tool that denies deletion. Restart, complete a disposable write, and verify a cleanup warning is shown without reversing the committed Git state. Close the handle and restart; cleanup should retry.

## Acceptance record

Record:

- release archive SHA-256;
- Windows edition/build;
- Python and Git versions;
- filesystem and antivirus/endpoint protection;
- automated log result;
- browser result;
- native-lock diagnostic text;
- cleanup-warning result;
- any retained transaction ID or unexpected prompt.

Do not mark physical Windows v0.5.2.2 acceptance complete until the automated run is unskipped `OK` and the owner-browser smoke passes on the exact packaged archive.
