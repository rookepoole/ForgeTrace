# Start Here

ForgeTrace is a working local repository application created by **Rooke Poole** and released as open-source software under the MIT License.

## Run on Windows

1. Extract the ZIP.
2. Double-click `run_local.bat`.
3. Open `http://127.0.0.1:8765`.
4. Create the repository in the first-run screen.
5. Upload files or an entire folder.

Actual repository files are saved in `workspace/`. ForgeTrace history and restorable snapshot objects are saved in `workspace/.forgetrace/`.

## Continue development

Read [`BUILD_PLAN.md`](BUILD_PLAN.md) before adding major features. The immediate target is a hardened multi-repository registry that can manage multiple real repository paths safely from one running ForgeTrace instance.
