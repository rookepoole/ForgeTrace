# ForgeTrace Local API v1

The owner API serves the bundled local UI and future CLI/desktop clients. It is unauthenticated and remains bound to `127.0.0.1`. ForgeTrace may create a separate restricted contributor listener, but that listener cannot access the owner API.

## Application and library endpoints

```text
GET  /api/v1/version
GET  /api/v1/repositories?query=&tag=&collectionId=&status=&favorite=
POST /api/v1/repositories
POST /api/v1/repositories/managed
POST /api/v1/repositories/fork
GET  /api/v1/active-repository
POST /api/v1/active-repository
GET  /api/v1/library
POST /api/v1/collections
PUT  /api/v1/collections/{collectionId}
DELETE /api/v1/collections/{collectionId}
POST /api/v1/saved-filters
DELETE /api/v1/saved-filters/{filterId}
```

`POST /api/v1/repositories` accepts:

```json
{
  "path": "/absolute/path",
  "name": "Optional display name",
  "description": "Optional description",
  "author": "Rooke Poole",
  "initialize": true,
  "createDirectory": false,
  "metadataMode": "embedded",
  "uploadLimitBytes": 1073741824
}
```

External metadata mode currently returns `501 metadata_mode_not_implemented`.

`POST /api/v1/repositories/managed` creates a normal initialized repository under ForgeTrace application data for browser imports that cannot supply an absolute host path:

```json
{
  "name": "Imported Project",
  "description": "Optional description",
  "author": "Rooke Poole",
  "uploadLimitBytes": 1073741824
}
```

The UI then sends each selected file through the normal repository-scoped upload endpoint. Folder imports strip the selected outer folder and retain nested relative paths. The resulting repository may be moved and relinked like any path-created repository.

`POST /api/v1/repositories/fork` works before any active repository exists and accepts:

```json
{
  "shareUrl": "http://192.168.1.25:8766/contribute.html#invite-token",
  "name": "Optional local fork name",
  "description": "Optional local description",
  "author": "New Teammate",
  "uploadLimitBytes": 1073741824
}
```

The owner process validates the invite against the restricted gateway, streams the source-only archive, performs safe ZIP extraction into a newly allocated managed directory, registers the fork, and returns it as the active repository candidate. The raw token is not stored.

## Repository-scoped endpoints

```text
GET    /api/v1/repositories/{id}
DELETE /api/v1/repositories/{id}                 unregister only
GET    /api/v1/repositories/{id}/state
GET    /api/v1/repositories/{id}/file?path=...
PUT    /api/v1/repositories/{id}/file
GET    /api/v1/repositories/{id}/raw?path=...
POST   /api/v1/repositories/{id}/upload?path=...
POST   /api/v1/repositories/{id}/folder
POST   /api/v1/repositories/{id}/rename
DELETE /api/v1/repositories/{id}/path?path=...
POST   /api/v1/repositories/{id}/commit
POST   /api/v1/repositories/{id}/checkout
GET    /api/v1/repositories/{id}/export
POST   /api/v1/repositories/{id}/favorite
POST   /api/v1/repositories/{id}/initialize
POST   /api/v1/repositories/{id}/relink
POST   /api/v1/repositories/{id}/settings
POST   /api/v1/repositories/{id}/organization
```

Settings payload:

```json
{
  "name": "Repository name",
  "description": "Description",
  "defaultAuthor": "Rooke Poole",
  "uploadLimitBytes": 10485760
}
```

Organization payload:

```json
{
  "tags": ["python", "active"],
  "collectionIds": ["collection-uuid"]
}
```

## Registry reliability endpoints

```text
GET  /api/v1/registry/export
GET  /api/v1/registry/backups
POST /api/v1/registry/backup
POST /api/v1/registry/import
GET  /api/v1/doctor?scanRoot=/path
POST /api/v1/doctor
```

Registry import is a non-destructive merge. It does not copy repository files, delete files, or replace repository paths for matching UUIDs unless `updatePaths` is explicitly true.

Doctor POST payload:

```json
{
  "repair": false,
  "scanRoots": ["/path/to/projects"]
}
```

Safe repair creates a registry backup first. Current repair actions clear an invalid active selection, synchronize embedded metadata into the registry, and register discovered embedded repositories.

## Errors and limits

Errors use:

```json
{
  "error": "Human-readable explanation",
  "code": "stable_machine_readable_code",
  "details": {}
}
```

Raw uploads use `application/octet-stream`. JSON endpoints require an object body. The application maximum is 1 GB; each repository may set a lower limit. Binary uploads are streamed to application-data temporary files before atomic placement. Upload routes reject oversized bodies before reading them.

## Compatibility routes

The old unscoped `/api/...` routes are deprecated and return `Deprecation: true` plus a successor-version link. New clients must use `/api/v1/...`.

## Sharing lifecycle endpoints — v0.3.1

These owner-only routes control the optional contributor listener from the normal UI:

```text
GET  /api/v1/sharing
POST /api/v1/sharing/start
POST /api/v1/sharing/stop
```

Start payload:

```json
{
  "port": 8766
}
```

The response reports `enabled`, `port`, detected `addresses`, `baseUrls`, `publicBaseUrl`, and `startedAt`. The gateway binds externally but is permanently labeled as a contributor-only surface. It denies owner APIs even for loopback clients.

Stopping sharing closes the contributor listener without stopping the owner workspace. Sharing is not automatically restored after a process restart.

## Secure collaboration endpoints — v0.3.1

### Owner-only routes

These routes require a loopback client and local Host header.

```text
GET    /api/v1/repositories/{id}/collaboration/invites
POST   /api/v1/repositories/{id}/collaboration/invites
DELETE /api/v1/repositories/{id}/collaboration/invites/{inviteId}
GET    /api/v1/repositories/{id}/pull-requests
GET    /api/v1/repositories/{id}/pull-requests/{pullRequestId}
POST   /api/v1/repositories/{id}/pull-requests/{pullRequestId}/review
POST   /api/v1/repositories/{id}/pull-requests/{pullRequestId}/merge
POST   /api/v1/repositories/{id}/pull-requests/{pullRequestId}/close
```

Invite creation payload:

```json
{
  "label": "Alex documentation",
  "expiresInHours": 72,
  "maxUses": 1,
  "maxFileBytes": 104857600,
  "maxTotalBytes": 1073741824,
  "allowDeletes": true,
  "allowSourceDownload": true
}
```

The response includes the raw token once and a fragment-based `sharePath`. Store or transmit the link securely; the server stores only the token hash.

Review payload:

```json
{
  "reviewer": "Rooke Poole",
  "verdict": "approved",
  "comment": "Reviewed the exact diff."
}
```

Valid verdicts are `approved`, `changes_requested`, and `comment`.

Merge payload:

```json
{
  "mergedBy": "Rooke Poole",
  "confirmation": "MERGE #4",
  "expectedRevision": 2,
  "allowRiskyFiles": false
}
```

### Contributor routes

Every route requires `X-ForgeTrace-Invite: <raw-token>`.

```text
GET  /api/v1/collaboration/invite
GET  /api/v1/collaboration/source
GET  /api/v1/collaboration/pull-requests
POST /api/v1/collaboration/pull-requests
GET  /api/v1/collaboration/pull-requests/{pullRequestId}
POST /api/v1/collaboration/pull-requests/{pullRequestId}/files?path=...
POST /api/v1/collaboration/pull-requests/{pullRequestId}/deletions
POST /api/v1/collaboration/pull-requests/{pullRequestId}/submit
```

`GET /source` returns `application/zip` only when the invitation permits source download. The archive contains repository source files without `FORGETRACE_HISTORY.json` or internal metadata.

File upload bodies are raw bytes. The `path` query value must be repository-relative and may not contain `.git` or `.forgetrace`. ForgeTrace does not accept or extract change archives.

Pull-request creation payload:

```json
{
  "authorName": "External Contributor",
  "title": "Improve parser diagnostics",
  "description": "Adds context and updates tests."
}
```

Deletion payload:

```json
{
  "path": "obsolete/file.txt"
}
```

Clients on the contributor listener are denied every API route outside `/api/v1/collaboration/...`. This listener-level restriction applies even when the client address is loopback.

### Create a managed fork

`POST /api/v1/repositories/fork` accepts `shareUrl`, optional `name`, `description`, `author`, and `uploadLimitBytes`. It is owner-local only. The server validates the fragment token against the remote restricted gateway, streams and validates the source archive, and returns the newly registered repository. The raw token is not persisted.

## v0.3.6 local complete-folder import

### `POST /api/v1/system/pick-folder`

Local-owner only. Opens the operating-system folder chooser on the ForgeTrace machine.

Response:

```json
{
  "available": true,
  "cancelled": false,
  "path": "C:\\Projects\\Example",
  "name": "Example"
}
```

### `POST /api/v1/repositories/{repositoryId}/import-local-folder`

Local-owner only. Recursively copies a selected local folder directly into the repository.

```json
{
  "path": "C:\\Projects\\Example",
  "includeRoot": true,
  "author": "Rooke Poole"
}
```

`includeRoot=true` preserves `Example/` in an existing repository. `includeRoot=false` imports its contents at the repository root for new managed-repository onboarding.

### `POST /api/v1/repositories/{repositoryId}/folders`

Creates or verifies a normalized folder manifest in one repository mutation. Used by browser recursive fallback uploads before file transfer.

## v0.4.0 additions

The API includes persistent operation-job status/cancel routes, import preview/job routes, sensitive-export confirmation, collaboration storage metrics, `HEAD`, and the compatibility `/api/version` alias. Owner-only native picker/import routes remain loopback-gated.

