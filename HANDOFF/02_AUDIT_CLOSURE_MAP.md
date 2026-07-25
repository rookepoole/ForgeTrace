# ForgeTrace v0.4.0 Audit Closure Map

**Creator:** Rooke Poole  
**License:** MIT  
**Scope:** The 29 findings from the read-only v0.3.6 comprehensive audit.  
**Result:** 29/29 findings have an implemented remediation and validation evidence.

> This document closes the identified audit findings; it does not claim that any nontrivial software is permanently free of unknown defects.

## Validation summary

- 76 Python unit/integration tests passed.
- Five available Chromium workflows passed, including a real-server, real-disk black-box workflow.
- The live collaboration browser navigation test is environment-skipped because managed Chromium blocks localhost; equivalent HTTP collaboration paths pass.
- Python code coverage: 76% overall; native picker module: 87%.
- Physical Windows folder-dialog interaction is retained as an explicit manual release acceptance gate.

## Finding closures

### FT-AUD-001 — Snapshot restore deletes the live workspace before validating recovery objects

**Severity:** CRITICAL  
**Status:** CLOSED  
**Remediation:** Restore now validates every required object before workspace mutation, stages the target tree, and rolls back on failure.

**Evidence:**
- `forgetrace/repository.py: verify_snapshot_objects(), restore_commit()`
- `tests/test_v040_stabilization.py::test_restore_preflight_blocks_missing_or_corrupt_objects_without_touching_workspace`

### FT-AUD-002 — Corrupted content-addressed snapshot objects are restored without hash verification

**Severity:** CRITICAL  
**Status:** CLOSED  
**Remediation:** Restore, export, Doctor, and safety paths recompute SHA-256 and reject object/manifest mismatches.

**Evidence:**
- `forgetrace/repository.py: verify_snapshot_objects()`
- `tests/test_v040_stabilization.py::test_restore_preflight_blocks_missing_or_corrupt_objects_without_touching_workspace`

### FT-AUD-003 — Repository locks work only inside one Python process

**Severity:** CRITICAL  
**Status:** CLOSED  
**Remediation:** Repository UUID/path locks and an application-data single-instance lock serialize writers across processes; state writes use unique atomic temporary files.

**Evidence:**
- `forgetrace/locks.py`
- `forgetrace/app.py`
- `tests/test_v040_stabilization.py::test_cross_process_writes_are_serialized_without_loss`
- `tests/test_v040_audit_closure.py::test_application_single_instance_lock_is_cross_process_ready`

### FT-AUD-004 — Repository paths are sorted globally by depth instead of parent-child order

**Severity:** HIGH  
**Status:** CLOSED  
**Remediation:** Repository state is emitted in parent-child order with depth/parent metadata; the browser renders a true nested virtualized tree.

**Evidence:**
- `forgetrace/repository.py: tree construction`
- `index.html: virtualized depth-first tree`
- `tests/test_v040_stabilization.py::test_tree_is_depth_first_parent_child_order`
- `tests/browser_blackbox_test.py`

### FT-AUD-005 — Direct folder import silently overwrites existing files

**Severity:** HIGH  
**Status:** CLOSED  
**Remediation:** Imports preview conflicts and require abort, skip, overwrite, or rename behavior. Overwrite creates recovery state.

**Evidence:**
- `forgetrace/importing.py`
- `forgetrace/repository.py: import_local_folder()`
- `tests/test_v040_stabilization.py::test_folder_import_abort_is_non_destructive_and_overwrite_creates_safety_snapshot`

### FT-AUD-006 — Folder import is non-transactional and leaves partial untracked files on failure

**Severity:** HIGH  
**Status:** CLOSED  
**Remediation:** Folder imports stage outside the live workspace, verify bytes, commit transactionally, and leave no partial untracked tree on failure/cancel.

**Evidence:**
- `forgetrace/importing.py`
- `forgetrace/transactions.py`
- `tests/test_v040_audit_closure.py::test_import_preflight_space_and_cancellation_are_non_destructive`

### FT-AUD-007 — New-repository folder import is split across two requests and can leave an orphan repository

**Severity:** HIGH  
**Status:** CLOSED  
**Remediation:** New repository creation and folder population are one registry operation; failed imports remove staging and registration artifacts.

**Evidence:**
- `forgetrace/registry.py: atomic managed import`
- `tests/test_v040_stabilization.py::test_failed_atomic_managed_import_leaves_no_orphan`

### FT-AUD-008 — Filesystem mutations can succeed even when contribution metadata fails to save

**Severity:** HIGH  
**Status:** CLOSED  
**Remediation:** File changes and state updates share rollback journals; metadata-save failure restores the prior filesystem state.

**Evidence:**
- `forgetrace/transactions.py`
- `forgetrace/repository.py`
- `tests/test_v040_stabilization.py::test_metadata_save_failure_rolls_back_filesystem_change`

### FT-AUD-009 — state.json.bak is created and detected but cannot be restored by Doctor

**Severity:** HIGH  
**Status:** CLOSED  
**Remediation:** Doctor validates state.json.bak, restores it atomically, preserves a repair backup, and verifies repository UUID/schema.

**Evidence:**
- `forgetrace/registry.py: _restore_repository_state_backup()`
- `tests/test_v040_stabilization.py::test_doctor_restores_valid_state_backup`

### FT-AUD-010 — Nested .forgetrace metadata is imported into repositories

**Severity:** HIGH  
**Status:** CLOSED  
**Remediation:** Import policy rejects .forgetrace at any path depth instead of excluding only the selected root.

**Evidence:**
- `forgetrace/policies.py`
- `forgetrace/importing.py`
- `tests/test_v040_stabilization.py::test_import_rejects_nested_forgetrace_and_verifies_bytes`

### FT-AUD-011 — A stale directory at the old path prevents UUID-based startup relinking

**Severity:** HIGH  
**Status:** CLOSED  
**Remediation:** Startup discovery compares embedded identities even when the old path still exists with a stale/wrong identity.

**Evidence:**
- `forgetrace/registry.py`
- `tests/test_v040_stabilization.py::test_stale_old_path_relinks_by_uuid`

### FT-AUD-012 — A localStorage failure after successful copy is reported as an import failure

**Severity:** HIGH  
**Status:** CLOSED  
**Remediation:** UI persistence failures fall back to in-memory expansion and cannot convert a successful disk import into a failed result.

**Evidence:**
- `index.html: expandedMemory and import completion flow`
- `tests/browser_blackbox_test.py`

### FT-AUD-013 — Every state refresh repeatedly walks and hashes the entire repository

**Severity:** HIGH  
**Status:** CLOSED  
**Remediation:** A repository-local cache reuses size/mtime/hash metadata for unchanged files and avoids rehashing the full tree on every refresh.

**Evidence:**
- `forgetrace/repository.py: file-index cache`
- `tests/test_v040_audit_closure.py::test_hash_index_reuses_unchanged_file_digest`

### FT-AUD-014 — Folder rename and delete controls are disabled

**Severity:** MEDIUM  
**Status:** CLOSED  
**Remediation:** Folder actions are available in the UI and execute through transactional repository APIs.

**Evidence:**
- `index.html`
- `tests/browser_blackbox_test.py`

### FT-AUD-015 — Import verification checks path existence, not byte size or content hash

**Severity:** MEDIUM  
**Status:** CLOSED  
**Remediation:** Import manifests verify path, type, size, and SHA-256, not existence alone.

**Evidence:**
- `forgetrace/importing.py`
- `tests/test_v040_stabilization.py::test_import_rejects_nested_forgetrace_and_verifies_bytes`

### FT-AUD-016 — No total import-size or destination free-space preflight

**Severity:** MEDIUM  
**Status:** CLOSED  
**Remediation:** Imports calculate total bytes and compare required staging/commit space with destination free space before mutation.

**Evidence:**
- `forgetrace/importing.py`
- `tests/test_v040_audit_closure.py::test_import_preflight_space_and_cancellation_are_non_destructive`

### FT-AUD-017 — Direct import enumerates the complete tree in memory with no progress or cancellation

**Severity:** MEDIUM  
**Status:** CLOSED  
**Remediation:** Long imports run as persistent jobs with bounded history, progress counters, cancellation, and interrupted-job recovery.

**Evidence:**
- `forgetrace/jobs.py`
- `forgetrace/web.py: job APIs`
- `tests/test_v040_audit_closure.py::test_operation_history_persists_and_running_jobs_become_interrupted`

### FT-AUD-018 — Snapshots do not preserve empty directories

**Severity:** MEDIUM  
**Status:** CLOSED  
**Remediation:** Snapshot manifests record directories and restore empty directory structure.

**Evidence:**
- `forgetrace/repository.py`
- `tests/test_v040_stabilization.py::test_snapshot_restores_empty_directories_mode_and_timestamp`

### FT-AUD-019 — Snapshots do not preserve permissions, executable bits, timestamps, or other metadata

**Severity:** MEDIUM  
**Status:** CLOSED  
**Remediation:** Snapshot/restore preserves supported mode bits and nanosecond modification timestamps.

**Evidence:**
- `forgetrace/repository.py`
- `tests/test_v040_stabilization.py::test_snapshot_restores_empty_directories_mode_and_timestamp`

### FT-AUD-020 — Repository export is not protected by the repository mutation lock

**Severity:** MEDIUM  
**Status:** CLOSED  
**Remediation:** Export acquires the repository mutation lock and streams from verified snapshot/object content.

**Evidence:**
- `forgetrace/repository.py: export`
- `tests/test_v040_audit_closure.py::test_export_waits_for_repository_mutation_lock`

### FT-AUD-021 — Closed/merged pull-request quarantine cleanup exists but is never invoked

**Severity:** MEDIUM  
**Status:** CLOSED  
**Remediation:** Merged/closed request quarantine is purged immediately and terminal/stale data is cleaned on collaboration startup.

**Evidence:**
- `forgetrace/collaboration.py: storage_metrics() and cleanup`
- `tests/test_v040_audit_closure.py::test_storage_metrics_and_retention_cleanup_remove_terminal_quarantine`

### FT-AUD-022 — Crash leftovers in transfers and merge-backups have no startup cleanup/recovery policy

**Severity:** MEDIUM  
**Status:** CLOSED  
**Remediation:** Pending filesystem transactions recover on repository open; stale transfer, merge-backup, and unregistered staging artifacts are bounded/cleaned.

**Evidence:**
- `forgetrace/transactions.py`
- `forgetrace/registry.py`
- `tests/test_v040_audit_closure.py::test_pending_filesystem_transaction_recovers_on_repository_open`
- `tests/test_v040_stabilization.py::test_startup_cleanup_removes_stale_transfer_and_unregistered_staging`

### FT-AUD-023 — Import/share/export workflows lack a sensitive-file preview

**Severity:** MEDIUM  
**Status:** CLOSED  
**Remediation:** Import/share/export surfaces classify sensitive paths, exclude them by default where appropriate, and require explicit confirmation.

**Evidence:**
- `forgetrace/policies.py`
- `index.html`
- `tests/test_v040_audit_closure.py::test_source_archive_excludes_sensitive_files_unless_explicitly_allowed`
- `tests/test_v040_audit_closure.py::test_sensitive_export_requires_confirmation_and_head_has_no_body`

### FT-AUD-024 — Browser tests inject mocked transport instead of exercising the packaged server

**Severity:** MEDIUM  
**Status:** CLOSED  
**Remediation:** A black-box browser flow uses the actual packaged UI, HTTP handler, repository service, and filesystem; mocked UI tests remain supplemental.

**Evidence:**
- `tests/browser_blackbox_test.py`
- `HANDOFF/EVIDENCE/browser-validation.log`

### FT-AUD-025 — Native folder picker has only 23% unit coverage and no real Windows chooser test

**Severity:** MEDIUM  
**Status:** CLOSED  
**Remediation:** Platform adapters, PowerShell 7/Windows PowerShell STA invocation, Unicode, cancel, and headless contracts are automated. A dedicated Windows physical-dialog acceptance script/checklist is included.

**Evidence:**
- `forgetrace/native_picker.py`
- `tests/test_v040_audit_closure.py::test_windows_picker_prefers_pwsh_and_runs_sta`
- `tests/test_v040_audit_closure.py::test_native_picker_run_and_platform_contracts`
- `tests/WINDOWS_NATIVE_PICKER_ACCEPTANCE.md`
- `tests/windows_native_picker_fixture.ps1`

### FT-AUD-026 — The file tree renders all visible rows with no virtualization

**Severity:** MEDIUM  
**Status:** CLOSED  
**Remediation:** The browser renders a bounded visible window with spacer rows instead of inserting all expanded paths into the DOM.

**Evidence:**
- `index.html: virtualized tree renderer`
- `tests/test_v040_stabilization.py::test_ui_has_virtualized_tree_folder_actions_atomic_import_and_sensitive_preview`

### FT-AUD-027 — HTTP server lacks explicit request/socket timeouts and does not implement HEAD

**Severity:** LOW  
**Status:** CLOSED  
**Remediation:** Threaded servers have socket timeouts/backlog limits and implement HEAD for health/static/API paths.

**Evidence:**
- `forgetrace/web.py`
- `tests/test_v040_stabilization.py::test_head_legacy_version_job_cancel_and_rate_map_pruning`

### FT-AUD-028 — Remote rate-limit client maps can retain inactive keys

**Severity:** LOW  
**Status:** CLOSED  
**Remediation:** Inactive rate buckets are pruned and the map is capped to prevent unbounded key retention.

**Evidence:**
- `forgetrace/web.py`
- `tests/test_v040_stabilization.py::test_head_legacy_version_job_cancel_and_rate_map_pruning`

### FT-AUD-029 — Core routing and import functions are highly monolithic

**Severity:** LOW  
**Status:** CLOSED  
**Remediation:** Monolithic request/import logic was split into scoped dispatchers and services with AST complexity/length guards.

**Evidence:**
- `forgetrace/web.py: _post_global/_post_contributor/_post_owner/_post_repository/_post_legacy`
- `forgetrace/importing.py`
- `tests/test_v040_audit_closure.py::test_route_and_import_functions_are_split_into_bounded_units`

