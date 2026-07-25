# ForgeTrace v0.3.6 Comprehensive Bug Audit

**Audit date:** July 25, 2026  
**Audited package:** `ForgeTrace_v0.3.6_Direct_Disk_Complete_Folder_Import.zip`  
**Package SHA-256:** `98045cbe2cec76842f316c67f26f3b57bd0ba21a903eac0ba1bf0eebecc65b24`  
**Package modification status:** The original ZIP was not modified. All adversarial work used isolated temporary repositories and an extracted audit workspace.

## Executive verdict

ForgeTrace v0.3.6 contains a substantial amount of working functionality, and its packaged test suite is green. However, it is **not ready to be trusted with irreplaceable repositories** in its current form. The audit confirmed three critical data-integrity/concurrency defects, ten high-severity defects, thirteen medium-severity defects, and three low-severity engineering issues.

The user-reported “files in subfolders are not populating” issue has a concrete UI explanation: the backend returns paths sorted globally by depth rather than as a parent-child tree. After expanding a folder, its child can appear below unrelated root folders and files instead of directly beneath the parent. A separate UI failure can also report an import as failed after the server copied it successfully, and a failed new-repository import can leave a registered README-only repository.

### Severity summary

| Severity | Count | Release meaning |
|---|---:|---|
| Critical | 3 | Stop using restore/concurrent instances with valuable data; fix before next feature work |
| High | 10 | Can cause misleading state, partial writes, silent overwrite, or failed recovery |
| Medium | 13 | Significant correctness, usability, scalability, privacy, or test-confidence gaps |
| Low | 3 | Engineering hardening and maintainability |

## What passed

- ZIP integrity and SHA-256 verification: **PASS**.
- Packaged source manifest: **57/57 tracked files verified**.
- Python compilation: **PASS**.
- JavaScript syntax for `index.html` and `contribute.html`: **PASS**.
- Unit/integration suite: **48/48 PASS**.
- Chromium browser scripts: **4 PASS, 1 SKIP**. The live collaboration browser test was skipped because the managed browser blocks localhost navigation. The additional non-browser multi-repository smoke script also passed.
- Live packaged server startup and `/api/v1/version`: **PASS**, version `0.3.6`.
- Multi-repository isolation, registry migration, path traversal defenses, fork archive protections, invitation limits, remote owner-API denial, and atomic pull-request merge tests passed.
- No third-party Python runtime dependency is required.

## Why the green test suite did not catch the reported issue

The folder browser tests inject a mock `fetch` transport and load the HTML with `Page.setDocumentContent`. The native import test explicitly returns fabricated picker/import/state responses. This verifies portions of UI logic, but it does not prove the packaged server, real Windows picker, real repository disk, and actual UI tree work together. Unit coverage is 69% overall; `native_picker.py` is 23%, `app.py` is 39%, and the destructive `restore_commit` path was uncovered before this audit.

## Confirmed explanation for the subfolder symptom

The audit created this repository:

```text
Alpha/inside/deep.txt
Beta/b.txt
root.txt
```

The backend/API order was:

```text
Alpha
Beta
root.txt
Alpha/inside
Beta/b.txt
Alpha/inside/deep.txt
```

After expanding `Alpha`, Chromium displayed:

```text
Alpha
Beta
root.txt
Alpha/inside
```

The folder did expand, but the child was visually detached from its parent. This is a rendering/data-order defect, not proof that the underlying file is absent. Additional paths can genuinely leave files absent or hidden: partial import failure, an orphan README-only repository after onboarding failure, and a post-copy localStorage error that prevents refresh and reports a false failure.

## Detailed findings

### FT-AUD-001 — Snapshot restore deletes the live workspace before validating recovery objects

**Severity:** CRITICAL  
**Status:** reproduced  
**Category:** snapshot-recovery

**Impact:** A missing snapshot object causes restore to fail after current files have already been deleted. The working tree can be destroyed or left partially restored.

**Evidence:**
- `forgetrace/repository.py:959-978`
- `adversarial-results.json: restore_missing_object_destroys_working_tree_before_failure`

**Recommended correction:** Preflight every manifest entry, verify object hashes and free space, restore into a staging directory, then atomically swap or roll back.

### FT-AUD-002 — Corrupted content-addressed snapshot objects are restored without hash verification

**Severity:** CRITICAL  
**Status:** reproduced  
**Category:** snapshot-integrity

**Impact:** A damaged or tampered object is trusted based only on its filename. Restore can silently write corrupted content while reporting success.

**Evidence:**
- `forgetrace/repository.py:972-978`
- `adversarial-results.json: restore_accepts_corrupted_snapshot_object`

**Recommended correction:** Recompute SHA-256 for every object during Doctor, restore, export, and merge safety checks; reject mismatches before touching the workspace.

### FT-AUD-003 — Repository locks work only inside one Python process

**Severity:** CRITICAL  
**Status:** reproduced  
**Category:** concurrency

**Impact:** Two ForgeTrace instances or processes editing the same repository race on the fixed state.json.tmp file. Three runs lost 35-37 operations; one run also lost a repository file.

**Evidence:**
- `forgetrace/repository.py:42-64`
- `multiprocess-results.json`

**Recommended correction:** Add an operating-system file lock keyed by repository UUID/path, unique temporary filenames, optimistic revision checks, and a single-instance lock for shared app data.

### FT-AUD-004 — Repository paths are sorted globally by depth instead of parent-child order

**Severity:** HIGH  
**Status:** reproduced  
**Category:** file-tree-ui

**Impact:** Expanding a folder does not place its children beneath it. The reproduced UI order was Alpha, Beta, root.txt, Alpha/inside. This is the strongest confirmed explanation for users believing subfolder files did not populate.

**Evidence:**
- `forgetrace/repository.py:247-268`
- `index.html:669-682`
- `ui-tree-order-result.json`

**Recommended correction:** Return a nested tree or stable depth-first order. Render children recursively inside their parent node rather than filtering a flat globally sorted list.

### FT-AUD-005 — Direct folder import silently overwrites existing files

**Severity:** HIGH  
**Status:** reproduced  
**Category:** folder-import

**Impact:** Existing repository content can be replaced with no conflict preview, confirmation, safety snapshot, or rollback point.

**Evidence:**
- `forgetrace/repository.py:599-614`
- `adversarial-results.json: local_import_silently_overwrites_existing_file`

**Recommended correction:** Add explicit conflict policies (abort, skip, overwrite, rename), default to abort, show a preview, and create a safety snapshot before any overwrite.

### FT-AUD-006 — Folder import is non-transactional and leaves partial untracked files on failure

**Severity:** HIGH  
**Status:** reproduced  
**Category:** folder-import

**Impact:** Earlier files remain copied when a later conflict/error aborts the import, but no contribution is recorded. Repository contents and ForgeTrace history diverge.

**Evidence:**
- `forgetrace/repository.py:583-650`
- `adversarial-results.json: failed_import_leaves_untracked_partial_files`

**Recommended correction:** Stage the full import outside the repository, validate all conflicts first, then apply atomically with rollback and one metadata transaction.

### FT-AUD-007 — New-repository folder import is split across two requests and can leave an orphan repository

**Severity:** HIGH  
**Status:** reproduced  
**Category:** repository-onboarding

**Impact:** ForgeTrace registers and activates a managed repository before import. If import fails, a README-only repository remains in the list and can look like the selected folder was empty.

**Evidence:**
- `index.html:1046-1055`
- `adversarial-results.json: new_repository_survives_failed_folder_import`

**Recommended correction:** Create-and-import in one server transaction or automatically unregister/delete the new managed repository after a failed import, with a clear recovery prompt.

### FT-AUD-008 — Filesystem mutations can succeed even when contribution metadata fails to save

**Severity:** HIGH  
**Status:** reproduced  
**Category:** metadata-consistency

**Impact:** A file write can survive while the API returns an error and contribution history remains unchanged. Similar ordering exists in rename, delete, import, and other operations.

**Evidence:**
- `forgetrace/repository.py:97-109`
- `transaction-results.json: file_write_survives_metadata_failure`

**Recommended correction:** Use a write-ahead operation journal or transaction directory, save an intent/revision first, then commit filesystem and metadata together with rollback.

### FT-AUD-009 — state.json.bak is created and detected but cannot be restored by Doctor

**Severity:** HIGH  
**Status:** reproduced  
**Category:** metadata-recovery

**Impact:** A repository can become unusable after state.json corruption even though a valid backup is present and Doctor reports backupAvailable=true.

**Evidence:**
- `forgetrace/repository.py:97-109`
- `forgetrace/registry.py:1367-1376`
- `additional-results.json: state_backup_exists_but_doctor_does_not_restore_it`

**Recommended correction:** Add previewed backup validation, one-click restore, automatic pre-repair backup, schema/UUID checks, and rollback if recovery fails.

### FT-AUD-010 — Nested .forgetrace metadata is imported into repositories

**Severity:** HIGH  
**Status:** reproduced  
**Category:** folder-import-security

**Impact:** Only a root-level .forgetrace directory is excluded. Nested project histories and object stores can be copied, exposed in the UI/export, and greatly inflate imports.

**Evidence:**
- `forgetrace/repository.py:539-568`
- `additional-results.json: nested_forgetrace_metadata_is_imported`

**Recommended correction:** Reject any path segment named .forgetrace at every depth and report each excluded path.

### FT-AUD-011 — A stale directory at the old path prevents UUID-based startup relinking

**Severity:** HIGH  
**Status:** reproduced  
**Category:** upgrade-continuity

**Impact:** After moving a repository, an empty/stale old directory causes ForgeTrace to retain an uninitialized record even though the real UUID-bearing repository is discovered elsewhere.

**Evidence:**
- `additional-results.json: startup_recovery_does_not_relink_if_old_path_still_exists`

**Recommended correction:** Treat path identity, not path existence, as authoritative. Relink when the old path lacks the expected UUID and exactly one discovered candidate matches.

### FT-AUD-012 — A localStorage failure after successful copy is reported as an import failure

**Severity:** HIGH  
**Status:** reproduced  
**Category:** folder-import-ui

**Impact:** The server can copy every file successfully, then UI expansion-state persistence throws. ForgeTrace displays “Native folder import failed,” skips refresh, and makes successful files appear absent.

**Evidence:**
- `index.html:866-878`
- `index.html:910-916`
- `localstorage-result.txt`

**Recommended correction:** Make expansion-state persistence best-effort and isolated from import success. Always refresh server state after copy, then warn separately if UI preferences could not be saved.

### FT-AUD-013 — Every state refresh repeatedly walks and hashes the entire repository

**Severity:** HIGH  
**Status:** measured  
**Category:** performance-usability

**Impact:** api_state calls tree multiple times and hashes every file to compute dirty state. Measured API state time was ~0.87 s at 10,000 tiny files before network/DOM cost; real binaries and slower drives can make imports look stuck or incomplete.

**Evidence:**
- `forgetrace/repository.py:993-1039`
- `performance-results.jsonl`

**Recommended correction:** Add an indexed manifest with incremental filesystem changes, cache hashes by size/mtime/file ID, and expose scan progress/cancellation.

### FT-AUD-014 — Folder rename and delete controls are disabled

**Severity:** MEDIUM  
**Status:** reproduced  
**Category:** file-tree-ui

**Impact:** Selecting a folder clears selectedFile, which disables rename/delete along with file-only actions, despite backend support for folder rename/delete.

**Evidence:**
- `index.html:685-700`
- `ui-folder-action-result.txt`

**Recommended correction:** Track a selected tree entry separately from selected file content; enable rename/delete for both files and folders.

### FT-AUD-015 — Import verification checks path existence, not byte size or content hash

**Severity:** MEDIUM  
**Status:** source-confirmed  
**Category:** folder-import-integrity

**Impact:** Truncated, changed, or wrong file contents can pass verification as long as the expected path exists.

**Evidence:**
- `forgetrace/repository.py:616-627`

**Recommended correction:** Capture source size/hash during enumeration and verify destination content before reporting success.

### FT-AUD-016 — No total import-size or destination free-space preflight

**Severity:** MEDIUM  
**Status:** source-confirmed  
**Category:** folder-import-capacity

**Impact:** Only individual file limits are checked. A large tree can exhaust disk space midway and trigger the partial-import defect.

**Evidence:**
- `forgetrace/repository.py:526-578`

**Recommended correction:** Sum bytes before copying, compare with free space plus safety margin, enforce configurable total limits, and fail before creating destination files.

### FT-AUD-017 — Direct import enumerates the complete tree in memory with no progress or cancellation

**Severity:** MEDIUM  
**Status:** source-confirmed  
**Category:** folder-import-usability

**Impact:** Large or network folders can block one request for minutes. The UI cannot show discovery/copy progress or cancel safely.

**Evidence:**
- `forgetrace/repository.py:526-583`
- `forgetrace/native_picker.py:14-38`

**Recommended correction:** Use a job model with phases, counters, cancellation tokens, resumable staging, and a persistent operation log.

### FT-AUD-018 — Snapshots do not preserve empty directories

**Severity:** MEDIUM  
**Status:** reproduced  
**Category:** snapshot-fidelity

**Impact:** Restoring a snapshot can produce a structurally different project even when no file content is missing.

**Evidence:**
- `forgetrace/repository.py:719-738`
- `transaction-results.json: empty_directories_not_restored`

**Recommended correction:** Include directory manifests and restore empty directories explicitly.

### FT-AUD-019 — Snapshots do not preserve permissions, executable bits, timestamps, or other metadata

**Severity:** MEDIUM  
**Status:** source-confirmed  
**Category:** snapshot-fidelity

**Impact:** Restored scripts/build assets may lose executable or metadata semantics, especially on macOS/Linux.

**Evidence:**
- `forgetrace/repository.py:719-738`
- `forgetrace/repository.py:972-978`

**Recommended correction:** Store documented portable metadata and restore it where supported, with clear cross-platform fallbacks.

### FT-AUD-020 — Repository export is not protected by the repository mutation lock

**Severity:** MEDIUM  
**Status:** source-confirmed  
**Category:** export-consistency

**Impact:** Files can be renamed/deleted while an archive is being built, causing inconsistent archives or failures.

**Evidence:**
- `forgetrace/repository.py:1041-1085`

**Recommended correction:** Create a stable manifest/snapshot under lock and export from immutable objects or a staging view.

### FT-AUD-021 — Closed/merged pull-request quarantine cleanup exists but is never invoked

**Severity:** MEDIUM  
**Status:** source-confirmed  
**Category:** collaboration-storage

**Impact:** Submitted files can remain indefinitely in application data, consuming disk and retaining content users may assume was removed.

**Evidence:**
- `forgetrace/collaboration.py:882-885; no call sites`

**Recommended correction:** Add retention policy, owner-visible storage metrics, purge after merge/close, and safe scheduled cleanup.

### FT-AUD-022 — Crash leftovers in transfers and merge-backups have no startup cleanup/recovery policy

**Severity:** MEDIUM  
**Status:** source-confirmed  
**Category:** temporary-storage

**Impact:** Interrupted forks, downloads, or merges can leave large temporary data or ambiguous rollback directories.

**Evidence:**
- `forgetrace/collaboration.py:403,647`
- `forgetrace/repository.py:824-906`

**Recommended correction:** Record transfer/merge leases, clean expired safe artifacts at startup, and surface recoverable interrupted operations.

### FT-AUD-023 — Import/share/export workflows lack a sensitive-file preview

**Severity:** MEDIUM  
**Status:** source-confirmed  
**Category:** privacy

**Impact:** Hidden files, .env files, credentials, build caches, and VCS data can be imported/exported unless a specific source-only path excludes some metadata.

**Evidence:**
- `forgetrace/repository.py:539-578`
- `forgetrace/repository.py:1041-1061`

**Recommended correction:** Add ignore rules, secret-name warnings, size previews, and a share/export manifest requiring explicit approval.

### FT-AUD-024 — Browser tests inject mocked transport instead of exercising the packaged server

**Severity:** MEDIUM  
**Status:** confirmed  
**Category:** test-validity

**Impact:** Tests can pass while real routing, filesystem behavior, Windows picker behavior, serialization, or state refresh is broken. This produced false confidence around the recurring folder issue.

**Evidence:**
- `tests/browser_deep_folder_test.py:93-100`
- `tests/browser_native_import_test.py:40-84`

**Recommended correction:** Add true black-box tests that launch the packaged server, operate through real HTTP, inspect real disk output, and run on Windows CI.

### FT-AUD-025 — Native folder picker has only 23% unit coverage and no real Windows chooser test

**Severity:** MEDIUM  
**Status:** measured  
**Category:** platform-coverage

**Impact:** The primary Windows workflow depends on PowerShell/WinForms but is tested through an environment override or mocked API, not the actual user interaction.

**Evidence:**
- `coverage.txt`
- `forgetrace/native_picker.py:41-83`

**Recommended correction:** Run Windows 10/11 CI or a manual acceptance harness for PowerShell 5.1 and PowerShell 7, Unicode paths, network drives, cancellation, hidden windows, and long paths.

### FT-AUD-026 — The file tree renders all visible rows with no virtualization

**Severity:** MEDIUM  
**Status:** measured  
**Category:** ui-scalability

**Impact:** 100,000 rows generated ~23.3 MB of HTML and took ~1.34 s just to assign innerHTML in headless Chromium. Auto-expanding imported trees magnifies memory and responsiveness problems.

**Evidence:**
- `index.html:669-682`
- `ui-scale-results.jsonl`
- `ui-scale-big-results.jsonl`

**Recommended correction:** Use a real nested virtualized tree, lazy child loading, row recycling, and bounded expansion.

### FT-AUD-027 — HTTP server lacks explicit request/socket timeouts and does not implement HEAD

**Severity:** LOW  
**Status:** source-confirmed  
**Category:** http-robustness

**Impact:** Slow clients can occupy threads longer than necessary; standard health/proxy HEAD checks receive 501.

**Evidence:**
- `forgetrace/web.py:1063-1068`
- `live-server.log`

**Recommended correction:** Set connection/read timeouts, body deadlines, bounded thread behavior, and implement HEAD for static/version health resources.

### FT-AUD-028 — Remote rate-limit client maps can retain inactive keys

**Severity:** LOW  
**Status:** source-confirmed  
**Category:** resource-management

**Impact:** Long-running gateways can accumulate small amounts of per-client bookkeeping.

**Evidence:**
- `forgetrace/web.py rate-limit structures`

**Recommended correction:** Evict expired/empty keys periodically and cap map size.

### FT-AUD-029 — Core routing and import functions are highly monolithic

**Severity:** LOW  
**Status:** measured  
**Category:** maintainability

**Impact:** Large branch-heavy functions increase regression risk and make route-level security/transaction review difficult.

**Evidence:**
- `complexity.json: web.do_POST 376 lines/approx. complexity 69; registry.import_registry 151/54; repository.import_local_folder 163/41`

**Recommended correction:** Split handlers into typed route controllers/services, isolate transaction stages, and enforce complexity thresholds in CI.

## Performance observations

| Fixture | `api_state()` | JSON response |
|---:|---:|---:|
| 1,000 files | 0.092 s | 194 KB |
| 5,000 files | 0.434 s | 930 KB |
| 10,000 files | 0.874 s | 1.85 MB |

These fixtures contained tiny files on fast local storage. Repositories with large binaries, antivirus scanning, removable drives, or network paths will be slower. UI rendering reached ~1.34 seconds and ~23.3 MB of generated HTML for 100,000 visible rows, excluding server hashing and transfer time.

## Recommended remediation sequence

### Gate 0 — protect user data before any new features
1. Replace restore with preflighted, hash-verified, staged, rollback-capable restore.
2. Add cross-process repository and app-data locking; reject a second owner instance unless explicitly read-only.
3. Make filesystem/history operations transactional or journaled.
4. Add `state.json.bak` validation and recovery through Doctor.

### Gate 1 — resolve the repeated import failure
1. Replace the flat globally sorted path list with a nested/depth-first tree.
2. Make create-new-repository plus folder import one server transaction.
3. Stage imports, preflight conflicts/free space, default to no overwrite, and roll back failures.
4. Verify imported bytes by size/hash and exclude `.forgetrace` at every depth.
5. Refresh state regardless of localStorage preference failures; report UI preference errors separately.
6. Enable folder rename/delete and add a visible import result manifest with “open containing folder.”

### Gate 2 — make the evidence credible
1. Add black-box Windows tests using the packaged launcher, real server, actual filesystem, and actual PowerShell folder picker.
2. Add destructive restore corruption/missing-object tests.
3. Add multi-process/two-server race tests.
4. Add interrupted import, disk-full, permission-denied, network-disconnect, and overwrite-conflict tests.
5. Raise coverage on `native_picker.py`, `app.py`, restore, Doctor repair, and all owner web routes.

### Gate 3 — scale and operational hardening
1. Incremental indexed manifests instead of rehashing all files on every refresh.
2. Virtualized nested tree with lazy loading.
3. Background job/progress/cancellation model for import, export, fork, snapshot, restore, and Doctor.
4. Retention/cleanup for quarantine, transfers, merge backups, and stale locks.
5. Sensitive-file preview and ignore policies for imports, exports, and source sharing.

## Audit limitations

- The audit environment is Linux, so it could not interact with a real Windows WinForms folder dialog. The Windows picker implementation was reviewed, and its tests were evaluated, but a genuine Windows acceptance run is still required.
- Managed Chromium blocked the live collaboration navigation test; collaboration service/API tests passed independently.
- No third-party penetration-testing scanner was installed. Security review combined source inspection with existing and custom adversarial tests.
- Network-volume behavior was not tested against a real SMB/NFS share or removable drive.

## Final release recommendation

Freeze feature development and treat the next release as a **reliability recovery release**, not another incremental folder-import patch. Do not advertise snapshot restore as safe until FT-AUD-001 and FT-AUD-002 are fixed. Do not run multiple ForgeTrace owner instances against the same app data or repository until FT-AUD-003 is fixed. Keep backups outside ForgeTrace before testing restore, import-overwrite, or concurrent workflows.
