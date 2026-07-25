# ForgeTrace Testing

Run from the repository root with Python 3:

```bash
python tests/smoke_test.py
python -m unittest discover -s tests -p "test_*.py" -v
```

Optional Chromium interface validation:

```bash
python tests/browser_smoke_test.py
python tests/browser_collaboration_test.py
```

The live collaboration browser test may report a managed-environment skip when Chromium policy blocks localhost navigation. The route-level collaboration flow is independently tested through real HTTP servers.

The application itself has no third-party runtime dependency.

## v0.3.3 acceptance coverage

- an empty registry can create a managed fork from a live restricted gateway;
- the owner HTTP API can fork before an active repository exists;
- the raw invite token is absent from repository and registry metadata;
- nested source files survive the streamed fork transfer;
- malformed, tokenless, cross-origin, unsafe-path, metadata-bearing, encrypted, symlink, and oversized archives are rejected by the implementation boundary;
- a recreated registry repopulates valid managed repositories by UUID;
- a moved managed repository auto-relinks only after UUID verification;
- upload, source, and pull-request transfer ceilings reflect the new limits;
- Chromium verifies collapsed folders, expand/collapse interaction, and fork-link onboarding;
- the full registry, recovery, security, collaboration, and sharing regression suite remains green.

## v0.3.2 onboarding coverage retained

- managed repositories are created under the configured application-data root;
- duplicate display names receive unique local directories;
- managed repositories have normal embedded identity and remain relinkable;
- the owner API creates a managed repository without an absolute path;
- individual-file and nested folder-path uploads reach the correct repository;
- the Add Repository dialog retains distinct file, folder, fork-link, and path choices;
- Chromium imports an individual file, imports a folder with root stripping, and reopens the unchanged path workflow.

## Existing repository and registry coverage

- repository-scoped API isolation and export boundaries;
- 100-repository registry fixture;
- restart persistence, offline detection, UUID relink, and non-destructive unregister;
- path traversal and `.forgetrace` protection;
- duplicate path rejection without identity mutation;
- atomic embedded metadata backup parsing;
- repository settings and per-repository upload limits;
- normalized tags, collections, saved filters, backups, import/export, Doctor, CLI, and v0.2.0 migration;
- Chromium file editing, organization, settings, Doctor, and repository switching.

## Secure collaboration coverage

- raw invite tokens are not stored in SQLite;
- collaboration schema migration and source-download scope;
- remote clients cannot access owner or repository APIs;
- general collaboration requests and source downloads are throttled;
- source-only ZIP includes normal source but excludes generated history, VCS metadata, and symlinks;
- source-download permission can be disabled;
- invite expiry, use, size, and deletion scopes are enforced;
- `.git`, `.forgetrace`, traversal, and oversized uploads are blocked;
- unchanged uploads are removed from the change set;
- contributor requests are recoverable through the same token;
- exact text diffs, binary evidence, risky-file flags, and conflicts are produced;
- open pull requests cannot merge without explicit approval;
- change-request revisions and resubmission work;
- risky files require separate owner confirmation;
- merge revalidates the baseline, creates recovery state, writes locally, and records attribution;
- a remote-simulated contributor can download source, submit through HTTP, and be merged only through a separate local owner HTTP surface.

## One-launch sharing coverage

- sharing status begins disabled;
- owner API can start a restricted listener on an available port;
- the listener serves the contributor page;
- the listener denies repository and owner APIs even from loopback;
- token-scoped contributor routes remain usable;
- changing ports requires an explicit stop first;
- Stop Sharing closes the listening socket;
- owner-process shutdown also stops the gateway;
- Chromium can open Collaborate, auto-start sharing, create an invite, and render the final token link;
- owner and contributor JavaScript parse successfully.

## Required mutation rule

Every new mutating endpoint must prove:

1. it cannot escape its selected repository, managed-import root, or quarantine root;
2. it cannot change another repository;
3. remote callers cannot invoke owner actions;
4. its data survives restart where persistence is promised;
5. failure returns a structured error;
6. recovery and portability are explicit;
7. rollback material exists before live files change;
8. no untrusted code is executed implicitly.

## v0.3.4 recursive-folder gate

Run:

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
python3 tests/browser_deep_folder_test.py
```

The API fixture verifies every descendant file and intermediate folder across six nesting levels. The Chromium test drives the recursive browser fallback, expands the resulting tree level by level, and repeats the workflow while creating a new managed repository.

## v0.3.5 verified native-folder gate

- Select a real on-disk directory through Chromium’s native `webkitdirectory` input.
- Confirm every descendant file carries its full `webkitRelativePath`.
- Keep the input disabled and uncleared until asynchronous upload and verification finish.
- Verify all expected paths against `/api/v1/repositories/<id>/state`.
- Simulate one interrupted nested upload and confirm the automatic retry succeeds.
- Confirm all imported parent folders are expanded and the deepest file is visible immediately.
- Confirm the launcher does not open a browser when port 8765 cannot be bound.

## v0.3.6 direct-disk complete-folder gate

Run:

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
python3 tests/browser_deep_folder_test.py
python3 tests/browser_folder_retry_test.py
python3 tests/browser_native_import_test.py
python3 tests/browser_smoke_test.py
```

Required evidence:

- the owner-only operating-system picker route returns an exact selected directory;
- the direct-disk importer preserves files and empty folders through at least six levels;
- existing-repository imports preserve the selected outer root;
- new managed repositories strip only the outer root;
- unreadable directories fail rather than disappearing silently;
- symbolic links and root `.forgetrace` metadata are skipped;
- every copied path is visible in the server-side repository tree;
- browser fallback still recursively uploads and retries missing descendants;
- the direct native-import UI expands the deepest file automatically.

## v0.4.0 stabilization matrix

The release requires 76 Python tests, five available Chromium workflows, 76% aggregate line coverage, 87% native-picker coverage, JavaScript syntax validation, Python compilation, source-manifest verification, and clean extracted startup. `tests/browser_blackbox_test.py` is the required real-server/real-disk UI flow. Physical Windows picker acceptance follows `tests/WINDOWS_NATIVE_PICKER_ACCEPTANCE.md`.

