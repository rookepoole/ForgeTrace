# ForgeTrace Product Specification

ForgeTrace is a local-first alternative repository workspace focused on traceable contribution history.

## Current product boundary

The application stores real files and real restorable snapshots locally. It is not yet a multi-user internet host and does not implement Git wire protocols, OAuth, pull requests, or remote replication.

## Core model

- **Workspace:** actual files and folders on disk
- **Contribution:** an attributable repository mutation such as upload, edit, rename, delete, or restore
- **Snapshot:** a manifest of paths, sizes, and SHA-256 object hashes
- **Object store:** deduplicated file contents retained for restoration
- **Export:** current workspace plus portable public history JSON

## Differentiator

Contributions are recorded automatically at the repository operation level, so file history includes creation, upload, editing, structural changes, commits, and restoration—not only commit totals.
