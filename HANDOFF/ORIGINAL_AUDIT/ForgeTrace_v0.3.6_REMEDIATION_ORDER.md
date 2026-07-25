# ForgeTrace v0.3.6 Remediation Order

This file converts the read-only audit into an implementation sequence. It does not modify the audited package.

## Release 0.3.7 — Data Integrity Recovery

- [ ] Preflight and hash-verify all snapshot objects before restore.
- [ ] Restore into staging and atomically swap with rollback.
- [ ] Add cross-process file locks and single-instance app-data lock.
- [ ] Replace fixed `state.json.tmp` with unique transaction files and revision checks.
- [ ] Make file/history mutations journaled and recoverable.
- [ ] Add validated `state.json.bak` restoration to Doctor.
- [ ] Add adversarial restore and multi-process CI tests.

**Exit gate:** No current file is lost under missing/corrupt snapshot objects, injected metadata-write failure, process crash, or two-process contention.

## Release 0.3.8 — Import and Tree Correctness

- [ ] Replace global depth sorting with a nested/depth-first tree API and renderer.
- [ ] Make managed repository creation plus import atomic.
- [ ] Stage imports and preflight every path/conflict.
- [ ] Default overwrite behavior to abort; provide preview and policy choices.
- [ ] Verify copied size/hash, total bytes, and free space.
- [ ] Exclude `.forgetrace` at every path depth.
- [ ] Decouple localStorage preference errors from import success.
- [ ] Enable folder rename/delete.
- [ ] Add real packaged-server and Windows picker end-to-end tests.

**Exit gate:** A selected directory containing deeply nested files, empty directories, Unicode names, binaries, conflicts, and failures produces an exact verified tree or a complete rollback—never a partial or misleading repository.

## Release 0.3.9 — Scale and Operations

- [ ] Incremental file index and hash cache.
- [ ] Virtualized/lazy nested tree.
- [ ] Background jobs with progress and cancellation.
- [ ] Export from immutable snapshot/manifest.
- [ ] Quarantine/transfer/backup retention and cleanup.
- [ ] Sensitive-file and secret preview policies.
- [ ] Request timeouts and operational diagnostics.

**Exit gate:** 100,000-file repositories remain navigable, and long operations expose truthful progress, cancellation, and recovery.
