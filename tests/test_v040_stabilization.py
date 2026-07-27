from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from forgetrace.app import build_application
from forgetrace.constants import APP_VERSION
from forgetrace.errors import RepositoryError
from forgetrace.registry import RepositoryRegistry
from forgetrace.repository import ForgeTraceRepository
from forgetrace.web import create_server

ROOT = Path(__file__).resolve().parents[1]


def _writer_worker(root: str, workspace: str, repo_id: str, start: int, count: int, queue) -> None:
    try:
        service = ForgeTraceRepository(Path(root), Path(workspace), repo_id)
        for index in range(start, start + count):
            service.write_file(f"workers/{index:03d}.txt", str(index).encode(), "worker", "parallel", uploaded=True)
        queue.put(None)
    except Exception as exc:  # pragma: no cover - child diagnostic
        queue.put(repr(exc))


class StabilizedRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-v040-"))
        self.workspace = self.temp / "repository"
        self.service = ForgeTraceRepository(ROOT, self.workspace, "repo-v040")
        self.service.initialize("Stabilized", "", "Rooke Poole")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_restore_preflight_blocks_missing_or_corrupt_objects_without_touching_workspace(self) -> None:
        self.service.write_file("proof.txt", b"baseline", "Rooke", "baseline", uploaded=True)
        commit = self.service.create_commit("baseline", "Rooke")
        self.service.write_file("proof.txt", b"current", "Rooke", "current", uploaded=False)
        state = self.service.load_state()
        digest = state["commits"][0]["manifest"]["proof.txt"]["hash"]
        object_path = self.service.object_path(digest)
        original = object_path.read_bytes()

        object_path.unlink()
        with self.assertRaises(RepositoryError) as missing:
            self.service.restore_commit(commit["id"], "Rooke")
        self.assertEqual("snapshot_integrity_failed", missing.exception.code)
        self.assertEqual(b"current", (self.workspace / "proof.txt").read_bytes())

        object_path.parent.mkdir(parents=True, exist_ok=True)
        object_path.write_bytes(b"CORRUPT")
        with self.assertRaises(RepositoryError) as corrupt:
            self.service.restore_commit(commit["id"], "Rooke")
        self.assertEqual("snapshot_integrity_failed", corrupt.exception.code)
        self.assertEqual(b"current", (self.workspace / "proof.txt").read_bytes())
        object_path.write_bytes(original)

    def test_snapshot_restores_empty_directories_mode_and_timestamp(self) -> None:
        folder = self.workspace / "empty" / "nested"
        folder.mkdir(parents=True)
        script = self.workspace / "bin" / "run.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        os.chmod(script, 0o755)
        stamp = 1_700_000_000_000_000_000
        os.utime(script, ns=(stamp, stamp))
        commit = self.service.create_commit("portable metadata", "Rooke")
        shutil.rmtree(self.workspace / "empty")
        script.unlink()
        self.service.restore_commit(commit["id"], "Rooke")
        self.assertTrue(folder.is_dir())
        self.assertTrue(script.is_file())
        if os.name != "nt":
            self.assertEqual(0o755, stat.S_IMODE(script.stat().st_mode))
        self.assertLess(abs(script.stat().st_mtime_ns - stamp), 2_000_000_000)

    def test_metadata_save_failure_rolls_back_filesystem_change(self) -> None:
        original = self.service.save_state
        with mock.patch.object(self.service, "save_state", side_effect=RepositoryError("forced metadata failure")):
            with self.assertRaises(RepositoryError):
                self.service.write_file("rollback.txt", b"must disappear", "Rooke", "test", uploaded=True)
        self.assertFalse((self.workspace / "rollback.txt").exists())
        self.service.save_state = original

    def test_cross_process_writes_are_serialized_without_loss(self) -> None:
        ctx = mp.get_context("spawn")
        queue = ctx.Queue()
        processes = [
            ctx.Process(target=_writer_worker, args=(str(ROOT), str(self.workspace), "repo-v040", offset, 15, queue))
            for offset in (0, 15, 30, 45)
        ]
        for process in processes:
            process.start()
        errors = [queue.get(timeout=60) for _ in processes]
        for process in processes:
            process.join(timeout=60)
            self.assertEqual(0, process.exitcode)
        self.assertEqual([None] * 4, errors)
        state = self.service.load_state()
        worker_events = [item for item in state["contributions"] if item.get("path", "").startswith("workers/")]
        self.assertEqual(60, len(worker_events))
        self.assertEqual(60, len(list((self.workspace / "workers").glob("*.txt"))))

    def test_tree_is_depth_first_parent_child_order(self) -> None:
        for path in ("Alpha/inside/deep.txt", "Alpha/a.txt", "Beta/b.txt", "root.txt"):
            target = self.workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path, encoding="utf-8")
        paths = [entry["path"] for entry in self.service.api_state()["tree"]]
        relevant = [path for path in paths if path != "README.md"]
        self.assertEqual(
            ["Alpha", "Alpha/inside", "Alpha/inside/deep.txt", "Alpha/a.txt", "Beta", "Beta/b.txt", "root.txt"],
            relevant,
        )

    def test_folder_import_abort_is_non_destructive_and_overwrite_creates_safety_snapshot(self) -> None:
        source = self.temp / "source"
        (source / "nested").mkdir(parents=True)
        (source / "nested" / "value.txt").write_text("new", encoding="utf-8")
        existing = self.workspace / "source" / "nested" / "value.txt"
        existing.parent.mkdir(parents=True)
        existing.write_text("old", encoding="utf-8")
        with self.assertRaises(RepositoryError):
            self.service.import_local_folder(str(source), "Rooke", include_root=True, conflict_policy="abort")
        self.assertEqual("old", existing.read_text(encoding="utf-8"))
        result = self.service.import_local_folder(str(source), "Rooke", include_root=True, conflict_policy="overwrite")
        self.assertEqual("new", existing.read_text(encoding="utf-8"))
        self.assertIsNotNone(result["safetySnapshot"])

    def test_import_rejects_nested_forgetrace_and_verifies_bytes(self) -> None:
        source = self.temp / "source"
        (source / "nested" / ".forgetrace").mkdir(parents=True)
        (source / "nested" / ".forgetrace" / "state.json").write_text("secret", encoding="utf-8")
        payload = b"\x00\x01nested-data"
        (source / "nested" / "binary.bin").write_bytes(payload)
        result = self.service.import_local_folder(str(source), "Rooke", include_root=True)
        self.assertFalse((self.workspace / "source" / "nested" / ".forgetrace").exists())
        target = self.workspace / "source" / "nested" / "binary.bin"
        self.assertEqual(payload, target.read_bytes())
        self.assertIn("source/nested/.forgetrace", result["skippedMetadata"])
        expected = hashlib.sha256(payload).hexdigest()
        self.assertEqual(expected, hashlib.sha256(target.read_bytes()).hexdigest())

    def test_export_is_immutable_verified_and_requires_explicit_sensitive_inclusion(self) -> None:
        (self.workspace / ".env").write_text("TOKEN=secret", encoding="utf-8")
        (self.workspace / "normal.txt").write_text("normal", encoding="utf-8")
        preview = self.service.sensitive_file_preview()
        self.assertEqual(1, preview["sensitiveCount"])
        out = self.temp / "safe.zip"
        self.service.export_zip_to_path(out, include_sensitive=False)
        with zipfile.ZipFile(out) as archive:
            self.assertNotIn(".env", archive.namelist())
            self.assertIn("normal.txt", archive.namelist())


class StabilizedRegistryAndRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-v040-registry-"))
        self.data = self.temp / "data"
        self.registry = RepositoryRegistry(ROOT, self.data)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_failed_atomic_managed_import_leaves_no_orphan(self) -> None:
        source = self.temp / "source"
        source.mkdir()
        (source / "too-large.bin").write_bytes(b"x" * (2 * 1024 * 1024))
        with self.assertRaises(Exception):
            self.registry.create_managed_repository_from_local_folder(
                source_path=str(source), name="Failed", author="Rooke", upload_limit_bytes=1024 * 1024
            )
        self.assertEqual([], self.registry.list_repositories()["repositories"])
        self.assertFalse(any(self.registry.managed_repositories_dir.glob(".importing-*")))

    def test_doctor_restores_valid_state_backup(self) -> None:
        record = self.registry.create_managed_repository(name="Recover", author="Rooke")
        service = self.registry.repository_service(record["id"])
        service.write_file("a.txt", b"a", "Rooke", "a", uploaded=True)
        backup = service.state_path.with_suffix(".json.bak")
        self.assertTrue(backup.is_file())
        service.state_path.write_text("{broken", encoding="utf-8")
        report = self.registry.doctor(repair=True)
        self.assertTrue(any(action["action"] == "restored_repository_metadata_backup" for action in report["actions"]))
        json.loads(service.state_path.read_text(encoding="utf-8"))

    def test_stale_old_path_relinks_by_uuid(self) -> None:
        record = self.registry.create_managed_repository(name="Move", author="Rooke")
        old = Path(record["path"])
        moved = old.with_name(old.name + "-moved")
        old.rename(moved)
        old.mkdir()
        restarted = RepositoryRegistry(ROOT, self.data)
        recovered = restarted.get_repository(record["id"])
        self.assertEqual(str(moved.resolve()), str(Path(recovered["path"]).resolve()))

    def test_startup_cleanup_removes_stale_transfer_and_unregistered_staging(self) -> None:
        transfer = self.data / "transfers" / "old.tmp"
        transfer.parent.mkdir(parents=True, exist_ok=True)
        transfer.write_text("old", encoding="utf-8")
        staging = self.data / "managed-repositories" / ".importing-old"
        staging.mkdir(parents=True)
        old = time.time() - 3 * 24 * 3600
        os.utime(transfer, (old, old))
        restarted = RepositoryRegistry(ROOT, self.data)
        self.assertFalse(transfer.exists())
        self.assertFalse(staging.exists())
        self.assertGreaterEqual(restarted.startup_cleanup_report["removedCount"], 2)


class StabilizedHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-v040-http-"))
        self.app = build_application(ROOT, self.temp / "data")
        self.server = create_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        shutil.rmtree(self.temp, ignore_errors=True)

    def request(self, method: str, path: str, payload=None):
        data = None; headers = {}
        if payload is not None:
            data = json.dumps(payload).encode(); headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read()
            return response.status, response.headers, json.loads(raw) if raw else None

    def test_head_legacy_version_job_cancel_and_rate_map_pruning(self) -> None:
        status, headers, payload = self.request("HEAD", "/api/v1/version")
        self.assertEqual(200, status); self.assertIsNone(payload); self.assertEqual(APP_VERSION, headers["X-ForgeTrace-Version"])
        status, _, payload = self.request("GET", "/api/version")
        self.assertEqual(APP_VERSION, payload["version"])
        job = self.app.jobs.start("wait", lambda context: (time.sleep(1), {"ok": True})[1])
        status, _, cancelled = self.request("DELETE", f"/api/v1/jobs/{job['id']}")
        self.assertTrue(cancelled["cancelRequested"])
        handler = type(self.server.RequestHandlerClass)
        # Exercise the bounded eviction helper directly with expired entries.
        windows = {f"client-{i}": [0.0] for i in range(5000)}
        self.server.RequestHandlerClass._prune_rate_windows(windows, 1.0, cap=100)
        self.assertLessEqual(len(windows), 100)

    def test_black_box_atomic_import_and_real_disk_tree(self) -> None:
        source = self.temp / "real-source"
        (source / "a" / "b" / "c").mkdir(parents=True)
        (source / "a" / "b" / "c" / "deep.txt").write_text("deep", encoding="utf-8")
        _, _, job = self.request("POST", "/api/v1/repositories/import-local", {
            "path": str(source), "name": "Black Box", "author": "Rooke", "conflictPolicy": "abort"
        })
        deadline = time.time() + 30
        while time.time() < deadline:
            _, _, state = self.request("GET", f"/api/v1/jobs/{job['id']}")
            if state["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(.05)
        self.assertEqual("completed", state["status"], state)
        record = state["result"]
        _, _, repo_state = self.request("GET", f"/api/v1/repositories/{record['id']}/state")
        paths = [item["path"] for item in repo_state["tree"]]
        self.assertIn("a/b/c/deep.txt", paths)
        self.assertEqual("deep", (Path(record["path"]) / "a" / "b" / "c" / "deep.txt").read_text())


class StabilizedSurfaceTest(unittest.TestCase):
    def test_ui_has_virtualized_tree_folder_actions_atomic_import_and_sensitive_preview(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        for expected in (
            "Import complete folder", "Browser folder fallback", "tree-spacer", "requestAnimationFrame",
            "selectedEntry", "import-jobs", "repositories/import-local", "export-preview",
            "confirmSensitive=1", "Cancel operation", "allowSensitiveSource",
        ):
            self.assertIn(expected, html)
        self.assertIn("Folder selected. Rename or delete it", html)

    def test_native_picker_override_unicode_and_cancel_contract(self) -> None:
        from forgetrace import native_picker
        with tempfile.TemporaryDirectory(prefix="unicodé-folder-") as folder:
            with mock.patch.dict(os.environ, {"FORGETRACE_TEST_PICK_FOLDER": folder}):
                self.assertEqual(str(Path(folder).resolve()), native_picker.pick_local_folder())
        with mock.patch.object(native_picker, "_run", return_value=None), mock.patch.object(native_picker.os, "name", "nt"), mock.patch.object(native_picker.shutil, "which", return_value="powershell"):
            self.assertIsNone(native_picker.pick_local_folder())


if __name__ == "__main__":
    unittest.main(verbosity=2)
