# ForgeTrace Recovery Guide

## Package update continuity

The registry and managed repositories live in platform application data rather than the extracted release folder. On every launch, v0.3.3 scans the stable managed root and bounded known legacy ForgeTrace workspace locations for `.forgetrace/state.json`.

Missing registrations are restored by UUID. An offline managed entry may be relinked automatically only when the discovered UUID matches. Offline repositories outside known managed roots remain in the list for manual relinking. ForgeTrace does not recursively scan an entire home or Downloads directory.

Normal update procedure:

1. stop the old ForgeTrace process;
2. extract the new package to a separate folder;
3. launch the new package normally;
4. confirm the repository list and active project;
5. keep the old package until the new release is verified.

Do not copy `registry.sqlite3` into the release folder. The launchers already use stable platform application data.

## First response

Keep repository files in place. Do not delete `.forgetrace`, the global registry, or registry backups while diagnosing a problem.

Run a read-only check:

```bash
python server.py doctor --json
python server.py doctor --scan-root /path/to/projects
```

Run safe repair only after reviewing the report:

```bash
python server.py doctor --scan-root /path/to/projects --repair
```

Repair creates a pre-repair SQLite backup and never deletes project files.

## Repository path is offline

ForgeTrace keeps the registry record. Reconnect the drive or mount and choose **Check again**. When an embedded repository moved, choose **Relink** and provide its new absolute path. ForgeTrace verifies the UUID in `.forgetrace/state.json`.

## Managed repository is missing from the list after an update

Restart ForgeTrace once so startup discovery can inspect the stable managed root. When the folder contains valid embedded identity, it is registered automatically. When it was moved outside known managed roots, use **Use a local path** or Doctor with an explicit scan root.

## Repository was unregistered accidentally

Unregister removes only the registry entry. Use Doctor with a scan root to discover and safely re-register embedded repositories:

```bash
python server.py doctor --scan-root /folder/containing/projects --repair
```

Adding the folder manually also reuses its stored UUID and history.

## Registry backup and transport

Create an online SQLite backup:

```bash
python server.py backup --label before-maintenance
```

Create a portable JSON registry export:

```bash
python server.py registry-export forgetrace-registry.json
```

Merge it into another installation or a fresh data directory:

```bash
python server.py registry-import forgetrace-registry.json --data-dir /new/app-data
```

JSON import transfers registrations and library metadata. It does not transfer repository files or snapshot objects.

## `state.json` is damaged

ForgeTrace retains `.forgetrace/state.json.bak` after subsequent saves. Doctor reports unreadable metadata and whether a backup exists, but v0.3.3 does not automatically replace metadata.

1. stop ForgeTrace;
2. copy the full repository folder;
3. validate both JSON files;
4. replace `state.json` only when the backup parses and the current file does not;
5. restart and run Doctor;
6. verify contributions and snapshots before continuing.

Automatic previewed backup restoration remains roadmap work.

## Snapshot object is missing

Working files remain usable. A restore requiring a missing object fails. Recover `.forgetrace/objects` from a full filesystem backup. v0.3.3 Doctor does not yet perform complete object verification.

## Global registry is damaged or lost

1. stop ForgeTrace;
2. move the damaged application-data directory aside;
3. start ForgeTrace with a fresh data directory;
4. allow startup discovery to repopulate the stable managed root;
5. import a JSON registry export when available;
6. otherwise scan external project roots with Doctor and repair;
7. relink any remaining moved paths.

Embedded UUIDs preserve repository identity.

## Export boundary

Repository ZIP exports contain current files and `FORGETRACE_HISTORY.json`. They intentionally exclude internal snapshot objects. Back up the complete repository folder when full restorable history is required.

## v0.4.0 recovery guarantees

Restore performs object preflight before any workspace mutation. Objects are checked for existence, expected size, and SHA-256. The target tree is staged and applied transactionally. Pending filesystem journals recover on repository open. Doctor can restore a valid `state.json.bak` after schema/UUID validation and can identify or reconstruct a missing object from a matching live file where safe.

