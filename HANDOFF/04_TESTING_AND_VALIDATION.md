# Testing and Validation Handoff

## Required unit/integration command

```bash
python -m unittest discover -s tests -v
```

Accepted baseline: **76 tests pass**.

## Coverage

```bash
python -m coverage run --source=forgetrace -m unittest discover -s tests
python -m coverage report -m
```

Accepted baseline: **76% total**, **87% native picker**.

## Browser workflows

```bash
python tests/browser_smoke_test.py
python tests/browser_deep_folder_test.py
python tests/browser_folder_retry_test.py
python tests/browser_native_import_test.py
PYTHONPATH=. python tests/browser_blackbox_test.py
PYTHONPATH=. python tests/browser_collaboration_test.py
```

The first five pass in the build environment. The collaboration browser script may skip because managed Chromium blocks localhost; its HTTP collaboration suite must still pass.

## Mandatory targeted gates

- Snapshot/recovery changes: `test_v040_stabilization.StabilizedRepositoryTest`
- Lock/transaction changes: cross-process and metadata rollback tests
- Import changes: v0.3.4–v0.3.6 import tests plus v0.4.0 import tests and browser black box
- Registry/recovery changes: registry reliability, security/recovery, and stabilized registry tests
- Collaboration changes: secure collaboration, unified sharing, collaboration closure tests
- UI tree changes: JavaScript syntax plus smoke and black-box browser tests

## Windows release gate

Follow `tests/WINDOWS_NATIVE_PICKER_ACCEPTANCE.md` and run `tests/windows_native_picker_fixture.ps1` on Windows. Record OS, PowerShell selection, chosen Unicode/deep path, cancellation behavior, and resulting repository tree.
