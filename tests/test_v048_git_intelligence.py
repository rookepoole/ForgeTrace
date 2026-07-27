from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from forgetrace.app import build_application
from forgetrace.errors import ForgeTraceError
from forgetrace.git_intelligence import GitIntelligenceService
from forgetrace.web import create_server

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")


@unittest.skipUnless(GIT, "Git executable is required")
class GitIntelligenceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = build_application(ROOT, self.root / "data")
        self.repo_path = self.root / "repo"
        record = self.app.registry.register_repository(
            path=str(self.repo_path), name="Git Fixture", description="v0.4.8", author="Owner",
            initialize=True, create_directory=True,
        )
        self.repository_id = record["id"]
        self.run_git("init")
        self.run_git("config", "user.name", "ForgeTrace Test")
        self.run_git("config", "user.email", "test@example.invalid")
        self.run_git("add", "README.md")
        self.run_git("commit", "-m", "Initial commit")
        self.run_git("branch", "feature/read-only")
        self.run_git("tag", "v0.1.0")
        self.run_git("remote", "add", "origin", "https://user:secret@example.invalid/owner/repo.git?token=abc")

    def tearDown(self) -> None:
        if self.app.gateway:
            self.app.gateway.stop()
        self.temp.cleanup()

    def run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([GIT, *args], cwd=self.repo_path, text=True, capture_output=True, check=True)


class GitIntelligenceServiceTest(GitIntelligenceFixture):
    def test_overview_status_refs_commits_tags_and_sanitized_remote(self) -> None:
        (self.repo_path / "README.md").write_text("# changed\n", encoding="utf-8")
        (self.repo_path / "new.txt").write_text("new\n", encoding="utf-8")
        overview = self.app.git.overview(self.repository_id)
        self.assertTrue(overview["probe"]["supported"])
        self.assertTrue(overview["status"]["dirty"])
        self.assertEqual("master" if any(item["name"] == "master" for item in overview["branches"]) else "main", overview["status"]["branch"]["head"])
        self.assertTrue(any(item["name"] == "feature/read-only" for item in overview["branches"]))
        self.assertTrue(any(item["name"] == "v0.1.0" for item in overview["tags"]))
        self.assertEqual("Initial commit", overview["commits"][0]["subject"])
        remote = overview["remotes"][0]
        self.assertTrue(remote["redacted"])
        self.assertNotIn("secret", remote["url"])
        self.assertNotIn("token", remote["url"])

    def test_staged_unstaged_commit_detail_and_bounded_diff(self) -> None:
        (self.repo_path / "README.md").write_text("# staged\n", encoding="utf-8")
        self.run_git("add", "README.md")
        staged = self.app.git.diff(self.repository_id, scope="staged", path="README.md")
        self.assertIn("+# staged", staged["text"])
        (self.repo_path / "README.md").write_text("# unstaged\n", encoding="utf-8")
        working = self.app.git.diff(self.repository_id, scope="working", path="README.md")
        self.assertIn("+# unstaged", working["text"])
        oid = self.run_git("rev-parse", "HEAD").stdout.strip()
        detail = self.app.git.commit_detail(self.repository_id, oid)
        self.assertEqual(oid, detail["commit"]["oid"])
        self.assertTrue(any(item["path"] == "README.md" for item in detail["files"]))

    def test_inspection_does_not_change_git_index_or_repository_files(self) -> None:
        index = self.repo_path / ".git" / "index"
        before_hash = hashlib.sha256(index.read_bytes()).hexdigest()
        before_stat = index.stat().st_mtime_ns
        before_readme = (self.repo_path / "README.md").read_bytes()
        self.app.git.overview(self.repository_id)
        self.app.git.diff(self.repository_id, scope="working")
        after_hash = hashlib.sha256(index.read_bytes()).hexdigest()
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(before_stat, index.stat().st_mtime_ns)
        self.assertEqual(before_readme, (self.repo_path / "README.md").read_bytes())

    def test_absent_symlink_and_external_worktree_layouts_are_explicit(self) -> None:
        shutil.rmtree(self.repo_path / ".git")
        probe = self.app.git.probe(self.repository_id)
        self.assertFalse(probe["detected"])
        (self.repo_path / ".git").write_text("gitdir: ../outside-admin\n", encoding="utf-8")
        probe = self.app.git.probe(self.repository_id)
        self.assertTrue(probe["detected"])
        self.assertFalse(probe["supported"])
        self.assertEqual("external_worktree", probe["kind"])

    def test_root_commit_diff_binary_and_large_diff_are_bounded(self) -> None:
        oid = self.run_git("rev-parse", "HEAD").stdout.strip()
        root_diff = self.app.git.diff(self.repository_id, scope="commit", commit=oid, path="README.md")
        self.assertIn("README.md", root_diff["text"])

        binary = self.repo_path / "binary.dat"
        binary.write_bytes(b"\x00one\x00")
        self.run_git("add", "binary.dat")
        self.run_git("commit", "-m", "binary baseline")
        binary.write_bytes(b"\x00two\x00")
        binary_diff = self.app.git.diff(self.repository_id, scope="working", path="binary.dat")
        self.assertTrue(binary_diff["binary"])
        self.assertEqual("", binary_diff["text"])

        large = self.repo_path / "large.txt"
        large.write_text("A" * 700_000 + "\n", encoding="utf-8")
        self.run_git("add", "large.txt")
        self.run_git("commit", "-m", "large baseline")
        large.write_text("B" * 700_000 + "\n", encoding="utf-8")
        large_diff = self.app.git.diff(self.repository_id, scope="working", path="large.txt")
        self.assertTrue(large_diff["truncated"])
        self.assertLessEqual(large_diff["bytes"], 512 * 1024)

    def test_external_helpers_hooks_and_credentials_are_never_invoked(self) -> None:
        marker = self.root / "helper-executed"
        helper = self.root / "malicious-helper"
        helper.write_text(f"#!/bin/sh\necho executed > '{marker}'\n", encoding="utf-8")
        helper.chmod(0o755)
        self.run_git("config", "core.fsmonitor", str(helper))
        self.run_git("config", "diff.external", str(helper))
        self.run_git("config", "credential.helper", f"!{helper}")
        hook = self.repo_path / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\necho hook > '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        (self.repo_path / "README.md").write_text("helper test\n", encoding="utf-8")
        self.app.git.overview(self.repository_id)
        self.app.git.diff(self.repository_id, scope="working", path="README.md")
        self.assertFalse(marker.exists())

    def test_external_config_alternates_and_parent_discovery_are_rejected(self) -> None:
        config = self.repo_path / ".git" / "config"
        config.write_text(config.read_text(encoding="utf-8") + "\n[include]\npath = ../outside.cfg\n", encoding="utf-8")
        probe = self.app.git.probe(self.repository_id)
        self.assertFalse(probe["supported"])
        self.assertIn("external configuration", probe["reason"])

        # Restore a normal config, then prove alternate object stores are rejected.
        self.run_git("config", "--unset-all", "include.path") if False else None
        text = config.read_text(encoding="utf-8").split("[include]", 1)[0]
        config.write_text(text, encoding="utf-8")
        alternates = self.repo_path / ".git" / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(str(self.root / "outside-objects") + "\n", encoding="utf-8")
        probe = self.app.git.probe(self.repository_id)
        self.assertFalse(probe["supported"])
        self.assertIn("Alternate Git object stores", probe["reason"])

        # A repository nested inside a parent Git worktree is not discovered upward.
        nested = self.root / "parent" / "nested"
        nested.mkdir(parents=True)
        subprocess.run([GIT, "init"], cwd=nested.parent, check=True, capture_output=True)
        record = self.app.registry.register_repository(
            path=str(nested), name="Nested non-Git", author="Owner", initialize=True, create_directory=False
        )
        nested_probe = self.app.git.probe(record["id"])
        self.assertFalse(nested_probe["detected"])

    def test_diff_path_traversal_and_invalid_object_ids_are_rejected(self) -> None:
        with self.assertRaises(ForgeTraceError):
            self.app.git.diff(self.repository_id, scope="working", path="../outside.txt")
        with self.assertRaises(ForgeTraceError) as failed:
            self.app.git.commit_detail(self.repository_id, "HEAD")
        self.assertEqual("invalid_git_object_id", failed.exception.code)

    def test_timeout_and_output_limit_are_explicit(self) -> None:
        script = self.root / "fake-git"
        script.write_text(f"#!{os.sys.executable}\nimport sys,time\nif 'sleep' in sys.argv: time.sleep(2)\nelse: sys.stdout.write('x'*100000)\n", encoding="utf-8")
        script.chmod(0o755)
        service = GitIntelligenceService(self.app.registry, git_executable=str(script), timeout_seconds=0.1, output_limit=4096)
        with self.assertRaises(ForgeTraceError) as timeout:
            service._run(self.repo_path, ["sleep"])
        self.assertEqual("git_command_timeout", timeout.exception.code)
        overflow_service = GitIntelligenceService(self.app.registry, git_executable=str(script), timeout_seconds=2, output_limit=4096)
        with self.assertRaises(ForgeTraceError) as overflow:
            overflow_service._run(self.repo_path, ["overflow"])
        self.assertEqual("git_output_limit", overflow.exception.code)

    def test_health_reports_git_without_repair_authority(self) -> None:
        report = self.app.health.generate(request_id="req_git_health", repository_id=self.repository_id)
        self.assertIn("git", report["sections"])
        git_data = report["sections"]["git"]["data"]
        self.assertEqual(1, git_data["scannedCount"])
        self.assertTrue(git_data["repositories"][0]["detected"])
        self.assertNotIn("repair", git_data["repositories"][0])


class GitIntelligenceApiTest(GitIntelligenceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.owner = create_server(self.app, "127.0.0.1", 0, surface="owner")
        self.gateway = create_server(self.app, "127.0.0.1", 0, surface="gateway")
        self.owner_thread = threading.Thread(target=self.owner.serve_forever, daemon=True); self.owner_thread.start()
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True); self.gateway_thread.start()

    def tearDown(self) -> None:
        for server, thread in ((self.owner, self.owner_thread), (self.gateway, self.gateway_thread)):
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        super().tearDown()

    @staticmethod
    def request(server, path: str):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
        connection.request("GET", path)
        response = connection.getresponse(); raw = response.read(); connection.close()
        return response.status, json.loads(raw) if raw else {}

    def test_owner_routes_and_gateway_denial(self) -> None:
        base = f"/api/v1/repositories/{self.repository_id}/git"
        status, payload = self.request(self.owner, base)
        self.assertEqual(200, status)
        self.assertTrue(payload["probe"]["supported"])
        oid = payload["commits"][0]["oid"]
        self.assertEqual(200, self.request(self.owner, f"{base}/commits/{oid}")[0])
        self.assertEqual(200, self.request(self.owner, f"{base}/diff?scope=working")[0])
        denied, body = self.request(self.gateway, base)
        self.assertEqual(403, denied)
        self.assertEqual("remote_owner_api_blocked", body["code"])
