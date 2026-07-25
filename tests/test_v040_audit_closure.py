from __future__ import annotations

import ast
import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from forgetrace.app import build_application
from forgetrace.collaboration import CollaborationService
from forgetrace.errors import RepositoryError
from forgetrace.importing import build_folder_import_plan
from forgetrace.jobs import JobCancelled, OperationManager
from forgetrace.locks import FileLock, LockUnavailable
from forgetrace.registry import RepositoryRegistry
from forgetrace.repository import ForgeTraceRepository
from forgetrace.transactions import FilesystemTransaction
from forgetrace.web import create_server

ROOT = Path(__file__).resolve().parents[1]


class AuditClosureInfrastructureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-closure-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_operation_history_persists_and_running_jobs_become_interrupted(self) -> None:
        history = self.temp / "jobs.json"
        manager = OperationManager(history_path=history)
        job = manager.start("complete", lambda context: {"value": 7})
        deadline = time.time() + 5
        while time.time() < deadline and manager.get(job["id"])["status"] not in {"completed", "failed"}:
            time.sleep(.02)
        self.assertEqual("completed", manager.get(job["id"])["status"])
        reloaded = OperationManager(history_path=history)
        self.assertEqual({"value": 7}, reloaded.get(job["id"])["result"])

        payload = json.loads(history.read_text(encoding="utf-8"))
        payload["jobs"].append({
            "id": "job_interrupted", "kind": "import", "status": "running", "phase": "applying",
            "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:01Z",
            "progress": {"filesApplied": 3}, "result": None, "error": None,
        })
        history.write_text(json.dumps(payload), encoding="utf-8")
        interrupted = OperationManager(history_path=history).get("job_interrupted")
        self.assertEqual("failed", interrupted["status"])
        self.assertEqual("InterruptedOperation", interrupted["error"]["type"])

    def test_pending_filesystem_transaction_recovers_on_repository_open(self) -> None:
        workspace = self.temp / "repository"
        service = ForgeTraceRepository(ROOT, workspace, "repo")
        service.initialize("Txn", "", "Rooke")
        service.write_file("value.txt", b"before", "Rooke", "before", uploaded=True)
        state = service.load_state()
        transaction = FilesystemTransaction(
            workspace, service.meta_dir, operation="crash-test",
            state_revision_before=service.state_revision(state),
        )
        transaction.capture("value.txt", workspace / "value.txt")
        (workspace / "value.txt").write_bytes(b"after-crash")
        reopened = ForgeTraceRepository(ROOT, workspace, "repo")
        self.assertEqual(b"before", (workspace / "value.txt").read_bytes())
        self.assertTrue(any(item["action"] == "rolled_back_pending" for item in reopened._recovery_actions))

    def test_import_preflight_space_and_cancellation_are_non_destructive(self) -> None:
        workspace = self.temp / "repository"
        source = self.temp / "source"
        source.mkdir(); (source / "payload.bin").write_bytes(b"x" * 4096)
        service = ForgeTraceRepository(ROOT, workspace, "repo")
        service.initialize("Space", "", "Rooke")
        usage = shutil._ntuple_diskusage(total=10_000, used=9_999, free=1)
        with mock.patch("forgetrace.importing.shutil.disk_usage", return_value=usage):
            with self.assertRaises(RepositoryError) as caught:
                build_folder_import_plan(service, str(source), include_root=True)
        self.assertEqual("insufficient_import_space", caught.exception.code)
        self.assertFalse((workspace / source.name).exists())
        with self.assertRaises(JobCancelled):
            build_folder_import_plan(service, str(source), include_root=True, cancelled=lambda: True)
        self.assertFalse((workspace / source.name).exists())

    def test_application_single_instance_lock_is_cross_process_ready(self) -> None:
        lock_path = self.temp / "owner.instance.lock"
        first = FileLock(lock_path, timeout=.1); first.acquire()
        try:
            second = FileLock(lock_path, timeout=.1)
            with self.assertRaises(LockUnavailable):
                second.acquire()
        finally:
            first.release()
        with FileLock(lock_path, timeout=.1):
            pass


    def test_hash_index_reuses_unchanged_file_digest(self) -> None:
        workspace = self.temp / "repository-cache"
        service = ForgeTraceRepository(ROOT, workspace, "repo-cache")
        service.initialize("Cache", "", "Rooke")
        target = workspace / "large.bin"
        target.write_bytes(b"cache-me" * 1024)
        original_open = Path.open
        reads = []
        def tracked(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if path == target and "b" in str(mode) and "r" in str(mode):
                reads.append(path)
            return original_open(path, *args, **kwargs)
        with mock.patch.object(Path, "open", tracked):
            service.scan_index(store_objects=False)
            first = len(reads)
            service.scan_index(store_objects=False)
            second = len(reads) - first
        self.assertGreaterEqual(first, 2)
        # The unchanged file is only sniffed for text/binary display on refresh; its
        # expensive full SHA-256 pass is reused from file-index.json.
        self.assertEqual(1, second)

    def test_export_waits_for_repository_mutation_lock(self) -> None:
        import threading
        workspace = self.temp / "repository-export-lock"
        service = ForgeTraceRepository(ROOT, workspace, "repo-export")
        service.initialize("Export lock", "", "Rooke")
        service.write_file("value.txt", b"value", "Rooke", "value", uploaded=True)
        completed = threading.Event(); errors = []
        def export_worker():
            try:
                service.export_zip_to_path(self.temp / "locked.zip")
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()
        service.lock.acquire()
        try:
            thread = threading.Thread(target=export_worker); thread.start()
            time.sleep(.15)
            self.assertFalse(completed.is_set())
        finally:
            service.lock.release()
        thread.join(timeout=10)
        self.assertTrue(completed.is_set())
        self.assertEqual([], errors)

    def test_windows_picker_prefers_pwsh_and_runs_sta(self) -> None:
        from forgetrace import native_picker
        with mock.patch.object(native_picker.os, "name", "nt"), \
             mock.patch.object(native_picker.shutil, "which", side_effect=lambda name: "C:/pwsh.exe" if name == "pwsh.exe" else None), \
             mock.patch.object(native_picker, "_run", return_value="C:/Projects/Unicode Ω") as run:
            selected = native_picker.pick_local_folder()
        self.assertEqual("C:/Projects/Unicode Ω", selected)
        command = run.call_args.args[0]
        self.assertEqual("C:/pwsh.exe", command[0])
        self.assertIn("-STA", command)

    def test_native_picker_run_and_platform_contracts(self) -> None:
        from types import SimpleNamespace
        from forgetrace import native_picker
        completed = SimpleNamespace(returncode=0, stdout=str(self.temp) + "\n", stderr="")
        with mock.patch.object(native_picker.subprocess, "run", return_value=completed):
            self.assertEqual(str(self.temp.resolve()), native_picker._run(["picker"]))
        cancelled = SimpleNamespace(returncode=1, stdout="", stderr="")
        with mock.patch.object(native_picker.subprocess, "run", return_value=cancelled):
            self.assertIsNone(native_picker._run(["picker"]))
        failed = SimpleNamespace(returncode=2, stdout="", stderr="boom")
        with mock.patch.object(native_picker.subprocess, "run", return_value=failed):
            with self.assertRaises(native_picker.NativeFolderPickerUnavailable):
                native_picker._run(["picker"])
        with mock.patch.object(native_picker.subprocess, "run", side_effect=native_picker.subprocess.TimeoutExpired("picker", 1)):
            with self.assertRaises(native_picker.NativeFolderPickerUnavailable):
                native_picker._run(["picker"], timeout=1)

        with mock.patch.object(native_picker.os, "name", "posix"), \
             mock.patch.object(native_picker.sys, "platform", "darwin"), \
             mock.patch.object(native_picker.shutil, "which", return_value="/usr/bin/osascript"), \
             mock.patch.object(native_picker, "_run", return_value="/tmp/mac-folder") as run:
            self.assertEqual("/tmp/mac-folder", native_picker.pick_local_folder())
            self.assertIn("osascript", run.call_args.args[0][0])
        with mock.patch.object(native_picker.os, "name", "posix"), \
             mock.patch.object(native_picker.sys, "platform", "linux"), \
             mock.patch.object(native_picker.shutil, "which", side_effect=lambda name: "/usr/bin/zenity" if name == "zenity" else None), \
             mock.patch.object(native_picker, "_run", return_value="/tmp/linux-folder") as run:
            self.assertEqual("/tmp/linux-folder", native_picker.pick_local_folder())
            self.assertIn("--directory", run.call_args.args[0])
        with mock.patch.object(native_picker.os, "name", "posix"), \
             mock.patch.object(native_picker.sys, "platform", "linux"), \
             mock.patch.object(native_picker.shutil, "which", return_value=None):
            with self.assertRaises(native_picker.NativeFolderPickerUnavailable):
                native_picker.pick_local_folder()

    def test_route_and_import_functions_are_split_into_bounded_units(self) -> None:
        web_tree = ast.parse((ROOT / "forgetrace" / "web.py").read_text(encoding="utf-8"))
        import_tree = ast.parse((ROOT / "forgetrace" / "importing.py").read_text(encoding="utf-8"))
        functions = {}
        for tree in (web_tree, import_tree):
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[node.name] = (node.end_lineno or node.lineno) - node.lineno + 1
        self.assertLessEqual(functions["do_POST"], 30)
        for name in ("_post_global", "_post_owner", "_post_repository", "build_folder_import_plan", "apply_folder_import"):
            self.assertLessEqual(functions[name], 220, (name, functions[name]))


class AuditClosureCollaborationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-collab-closure-"))
        self.registry = RepositoryRegistry(ROOT, self.temp / "data")
        self.record = self.registry.create_managed_repository(name="Secure", author="Rooke")
        self.service = self.registry.repository_service(self.record["id"])
        self.service.write_file("normal.txt", b"normal", "Rooke", "normal", uploaded=True)
        self.service.write_file(".env", b"TOKEN=secret", "Rooke", "secret", uploaded=True)
        self.collaboration = CollaborationService(self.registry)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_source_archive_excludes_sensitive_files_unless_explicitly_allowed(self) -> None:
        safe = self.collaboration.create_invite(self.record["id"], allow_source_download=True)
        archive, _ = self.collaboration.source_archive(safe["token"])
        with zipfile.ZipFile(Path(self.temp / "safe.zip"), "w") as _:
            pass
        with zipfile.ZipFile(__import__("io").BytesIO(archive)) as bundle:
            self.assertIn("normal.txt", bundle.namelist())
            self.assertNotIn(".env", bundle.namelist())
        privileged = self.collaboration.create_invite(
            self.record["id"], allow_source_download=True, allow_sensitive_source=True
        )
        archive, _ = self.collaboration.source_archive(privileged["token"])
        with zipfile.ZipFile(__import__("io").BytesIO(archive)) as bundle:
            self.assertIn(".env", bundle.namelist())

    def test_storage_metrics_and_retention_cleanup_remove_terminal_quarantine(self) -> None:
        invitation = self.collaboration.create_invite(self.record["id"])
        pr = self.collaboration.create_pull_request(
            invitation["token"], title="Update", description="", author_name="Contributor"
        )
        staged = self.temp / "staged.txt"; staged.write_text("changed", encoding="utf-8")
        self.collaboration.upload_pull_request_file_from_path(
            invitation["token"], pr["id"], "normal.txt", staged
        )
        self.collaboration.submit_pull_request(invitation["token"], pr["id"])
        self.collaboration.review_pull_request(
            self.record["id"], pr["id"], reviewer="Rooke", verdict="approved", comment="ok"
        )
        reviewed = self.collaboration.get_pull_request(self.record["id"], pr["id"])
        self.collaboration.merge_pull_request(
            self.record["id"], pr["id"], merged_by="Rooke",
            confirmation=f"MERGE #{reviewed['number']}", expected_revision=reviewed["revision"],
        )
        self.assertFalse((self.collaboration.quarantine_dir / self.record["id"] / pr["id"]).exists())
        metrics = self.collaboration.storage_metrics()
        self.assertEqual(0, metrics["quarantineBytes"])
        self.assertEqual(0, metrics["quarantineFiles"])
        self.assertEqual("purged immediately", metrics["retention"]["closedAndMergedQuarantine"])


class AuditClosureHttpSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        import threading
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-http-closure-"))
        self.app = build_application(ROOT, self.temp / "data")
        self.record = self.app.registry.create_managed_repository(name="HTTP", author="Rooke")
        self.service = self.app.registry.repository_service(self.record["id"])
        self.service.write_file(".env", b"SECRET=x", "Rooke", "secret", uploaded=True)
        self.server = create_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_sensitive_export_requires_confirmation_and_head_has_no_body(self) -> None:
        url = f"{self.base}/api/v1/repositories/{self.record['id']}/export?history=0"
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(url, timeout=10)
        self.assertEqual(409, denied.exception.code)
        body = json.loads(denied.exception.read())
        self.assertEqual("sensitive_export_confirmation_required", body["code"])
        with urllib.request.urlopen(url + "&confirmSensitive=1", timeout=10) as response:
            self.assertEqual("application/zip", response.headers.get_content_type())
            self.assertGreater(len(response.read()), 0)
        request = urllib.request.Request(self.base + "/api/version", method="HEAD")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual("0.4.0", response.headers["X-ForgeTrace-Version"])
            self.assertEqual(b"", response.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
