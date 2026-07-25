# ForgeTrace Migration Guide

## Application registry migrations

The global SQLite database owns `schema_migrations`. Migrations are ordered, immutable entries declared in `forgetrace/registry.py`.

Current migrations:

- `0001_repository_registry` — application state, repository paths, UUID uniqueness, embedded/external mode reservation, favorite state, and recency.
- `0002_registry_organization_and_limits` — per-repository upload limit and metadata-path fields, normalized tags, collections, repository-collection mappings, and saved filters.

After migration 0002, ForgeTrace backfills legacy `tags_json` and `collection_name` values into the normalized tables. The migration test creates a real v1 SQLite fixture and verifies tags, collection membership, and the default upload limit after upgrade.

On startup, ForgeTrace:

1. opens the registry with foreign keys enabled;
2. enables WAL journaling and full synchronous writes;
3. creates the migration table when absent;
4. applies each unapplied migration transactionally;
5. records its version and timestamp;
6. performs idempotent legacy-organization backfill.

Never edit an already released migration. Add a new numbered migration and test both a fresh database and an upgraded fixture.

## Repository metadata adoption

Repositories created before v0.2.0 may not contain a UUID. Registration assigns one and writes it into `.forgetrace/state.json`. Existing UUIDs are preserved. An identity already registered at another path must use relink rather than duplicate registration.

Repository settings in v0.2.1 update both the global registry and embedded repository metadata while the repository is online. Offline settings edits are rejected to prevent divergent sources of truth.

## Backup before mutation

Registry import and doctor repair create an online SQLite backup in the application-data `backups/` directory before making changes. Backups are produced through SQLite's backup API rather than copying a live WAL database directly.

## Rollback

The v0.2.0 release is tagged `v0.2.0`. To roll back code:

1. stop ForgeTrace;
2. back up the global application-data directory;
3. preserve the latest registry backup and JSON export;
4. check out the earlier tag;
5. launch with a fresh `--data-dir` if the older code cannot read schema version 2;
6. re-register repositories as needed.

Project files and embedded `.forgetrace` history remain ordinary files and are not rewritten by the registry migration.

## v0.4.0

Application schema is 3 and repository schema is 2. Existing repository state is migrated in place under the repository lock with atomic backup. Managed repositories remain identified by embedded UUID. Persistent job history and hash indexes are additive and can be regenerated.

