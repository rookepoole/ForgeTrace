from __future__ import annotations

import http.client
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from forgetrace.app import build_application
from forgetrace.collaboration import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    MAX_SOURCE_ARCHIVE_BYTES,
)
from forgetrace.constants import MAX_REQUEST_BYTES
from forgetrace.forking import CollaborationForkClient
from forgetrace.registry import RepositoryRegistry
from forgetrace.web import create_server

ROOT = Path(__file__).resolve().parents[1]


class CollaborationForkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-v033-fork-"))
        self.owner = build_application(ROOT, self.temp / "owner-data")
        owner_record = self.owner.registry.register_repository(
            path=str(self.temp / "owner-repository"),
            name="Shared Project",
            description="Team onboarding source",
            author="Rooke Poole",
            create_directory=True,
        )
        self.owner_id = owner_record["id"]
        owner_repository = self.owner.registry.repository_service(self.owner_id)
        owner_repository.write_file("src/main.py", b"print('shared')\n", "Rooke Poole", "Add source", uploaded=True)
        owner_repository.write_file("docs/guide.md", b"# Guide\n", "Rooke Poole", "Add guide", uploaded=True)
        sharing = self.owner.gateway.start(port=0, host="127.0.0.1")
        self.gateway_port = int(sharing["port"])
        invite = self.owner.collaboration.create_invite(
            self.owner_id,
            label="New teammate",
            max_file_bytes=DEFAULT_MAX_FILE_BYTES,
            max_total_bytes=DEFAULT_MAX_TOTAL_BYTES,
        )
        self.share_url = f"http://127.0.0.1:{self.gateway_port}{invite['sharePath']}"

    def tearDown(self) -> None:
        self.owner.gateway.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_empty_installation_can_fork_from_collaboration_link(self) -> None:
        recipient = RepositoryRegistry(ROOT, self.temp / "recipient-data")
        self.assertEqual([], recipient.list_repositories()["repositories"])
        fork = recipient.fork_from_collaboration_link(
            share_url=self.share_url,
            author="New Teammate",
        )
        fork_path = Path(fork["path"])
        self.assertEqual(b"print('shared')\n", (fork_path / "src" / "main.py").read_bytes())
        self.assertEqual(b"# Guide\n", (fork_path / "docs" / "guide.md").read_bytes())
        self.assertGreaterEqual(fork["importedFiles"], 3)
        self.assertIn((self.temp / "recipient-data" / "managed-repositories").resolve(), fork_path.resolve().parents)

        state_text = (fork_path / ".forgetrace" / "state.json").read_text(encoding="utf-8")
        state = json.loads(state_text)
        upstream = state["repository"]["upstream"]
        self.assertEqual("Shared Project", upstream["repositoryName"])
        self.assertEqual(self.owner_id, upstream["repositoryId"])
        self.assertNotIn(self.share_url.split("#", 1)[1], state_text)
        self.assertEqual(1, len(state["commits"]))

    def test_owner_api_can_create_fork_without_an_active_repository(self) -> None:
        recipient_app = build_application(ROOT, self.temp / "recipient-api-data")
        server = create_server(recipient_app, "127.0.0.1", 0, surface="owner")
        import threading
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", int(server.server_address[1]), timeout=30)
            body = json.dumps({"shareUrl": self.share_url, "author": "API Teammate"}).encode("utf-8")
            connection.request(
                "POST", "/api/v1/repositories/fork", body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(201, response.status, payload)
            self.assertTrue((Path(payload["path"]) / "src" / "main.py").is_file())
            self.assertEqual(payload["id"], recipient_app.registry.active_repository_id())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_fork_rejects_missing_token(self) -> None:
        recipient = RepositoryRegistry(ROOT, self.temp / "invalid-data")
        with self.assertRaisesRegex(Exception, "invite token"):
            recipient.fork_from_collaboration_link(
                share_url=f"http://127.0.0.1:{self.gateway_port}/contribute.html",
                author="New Teammate",
            )


class ForkArchiveSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-v033-fork-security-"))
        self.client = CollaborationForkClient(self.temp / "transfers")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def _archive(self, entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> Path:
        archive_path = self.temp / f"archive-{len(list(self.temp.glob('archive-*.zip')))}.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for info, content in entries:
                archive.writestr(info, content)
        return archive_path

    def test_fork_archive_rejects_traversal_and_protected_metadata(self) -> None:
        for path in ("../escape.txt", "/absolute.txt", ".git/config", "src/.forgetrace/state.json"):
            with self.subTest(path=path):
                archive = self._archive([(path, b"blocked")])
                destination = self.temp / ("destination-" + str(abs(hash(path))))
                with self.assertRaises(Exception):
                    self.client.extract_source(archive, destination)
                self.assertFalse((self.temp / "escape.txt").exists())

    def test_fork_archive_rejects_symbolic_links(self) -> None:
        info = zipfile.ZipInfo("link-to-secret")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        archive = self._archive([(info, b"/etc/passwd")])
        with self.assertRaisesRegex(Exception, "Symbolic links"):
            self.client.extract_source(archive, self.temp / "destination")

    def test_fork_redirect_validation_is_same_origin_only(self) -> None:
        CollaborationForkClient._validate_final_origin(
            "http://192.168.1.25:8766",
            "http://192.168.1.25:8766/api/v1/collaboration/source",
        )
        with self.assertRaisesRegex(Exception, "different origin"):
            CollaborationForkClient._validate_final_origin(
                "http://192.168.1.25:8766",
                "http://192.168.1.26:8766/api/v1/collaboration/source",
            )


class LargeTransferRouteTest(unittest.TestCase):
    def test_repository_raw_download_returns_large_file_exactly(self) -> None:
        temp = Path(tempfile.mkdtemp(prefix="forgetrace-v033-download-"))
        server = None
        thread = None
        try:
            app = build_application(ROOT, temp / "data")
            record = app.registry.create_managed_repository(name="Large Download", author="Rooke Poole")
            content = (b"ForgeTrace-stream-check-" * 400_000)[:8 * 1024 * 1024]
            app.registry.repository_service(record["id"]).write_file(
                "assets/large.bin", content, "Rooke Poole", "Add large transfer fixture", uploaded=True
            )
            server = create_server(app, "127.0.0.1", 0, surface="owner")
            import threading
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", int(server.server_address[1]), timeout=30)
            connection.request(
                "GET",
                f"/api/v1/repositories/{record['id']}/raw?path=assets%2Flarge.bin&download=1",
            )
            response = connection.getresponse()
            received = bytearray()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                received.extend(chunk)
            connection.close()
            self.assertEqual(200, response.status)
            self.assertEqual(str(len(content)), response.getheader("Content-Length"))
            self.assertEqual(content, bytes(received))
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if thread is not None:
                thread.join(timeout=2)
            shutil.rmtree(temp, ignore_errors=True)


class UpgradeContinuityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="forgetrace-v033-upgrade-"))
        self.data_dir = self.temp / "stable-app-data"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_managed_repositories_repopulate_when_registry_is_recreated(self) -> None:
        original = RepositoryRegistry(ROOT, self.data_dir)
        record = original.create_managed_repository(name="Recovered Project", author="Rooke Poole")
        repository = original.repository_service(record["id"])
        repository.write_file("nested/file.txt", b"survives", "Rooke Poole", "Persist", uploaded=True)

        for suffix in ("", "-wal", "-shm"):
            Path(str(original.db_path) + suffix).unlink(missing_ok=True)

        upgraded = RepositoryRegistry(ROOT, self.data_dir)
        listing = upgraded.list_repositories()["repositories"]
        self.assertEqual(1, len(listing))
        self.assertEqual(record["id"], listing[0]["id"])
        self.assertEqual("online", listing[0]["status"])
        self.assertEqual(b"survives", (Path(listing[0]["path"]) / "nested" / "file.txt").read_bytes())
        self.assertEqual(1, upgraded.startup_recovery_report["registered"])

    def test_moved_managed_repository_is_auto_relinked_by_uuid(self) -> None:
        original = RepositoryRegistry(ROOT, self.data_dir)
        record = original.create_managed_repository(name="Moved Managed Project", author="Rooke Poole")
        old_path = Path(record["path"])
        moved_path = old_path.with_name(old_path.name + "-moved")
        old_path.rename(moved_path)

        upgraded = RepositoryRegistry(ROOT, self.data_dir)
        recovered = upgraded.get_repository(record["id"])
        self.assertEqual(moved_path.resolve(), Path(recovered["path"]).resolve())
        self.assertEqual("online", recovered["status"])
        self.assertEqual(1, upgraded.startup_recovery_report["relinked"])


class UsabilitySurfaceTest(unittest.TestCase):
    def test_limits_and_ui_surface_are_upgraded(self) -> None:
        self.assertEqual(1024 * 1024 * 1024, MAX_REQUEST_BYTES)
        self.assertEqual(100 * 1024 * 1024, DEFAULT_MAX_FILE_BYTES)
        self.assertEqual(1024 * 1024 * 1024, DEFAULT_MAX_TOTAL_BYTES)
        self.assertEqual(2 * 1024 * 1024 * 1024, MAX_SOURCE_ARCHIVE_BYTES)
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        for expected in (
            'id="welcomeForkBtn"',
            'id="repoForkChoice"',
            'id="repoShareLinkInput"',
            "expandedFolderKey()",
            "appState.expandedFolders.has(path)",
            "/api/v1/repositories/fork",
        ):
            self.assertIn(expected, html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
