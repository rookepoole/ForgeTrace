from __future__ import annotations

import contextlib
import http.client
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from forgetrace.app import build_application
from forgetrace.errors import ForgeTraceError
from forgetrace.security_events import SecurityEventError, SecurityEventLedger
from forgetrace.web import create_server


class SecurityEventLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "data"
        self.ledger = SecurityEventLedger(self.data_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_hash_chain_redaction_and_database_immutability(self) -> None:
        first = self.ledger.append(
            category="access",
            action="invite_checked",
            outcome="denied",
            severity="warning",
            surface="gateway",
            details={
                "token": "raw-invite-token-must-never-persist",
                "nested": {"password": "raw-password", "safe": "visible"},
            },
        )
        second = self.ledger.append(
            category="access",
            action="owner_route_blocked",
            outcome="denied",
            severity="warning",
            surface="gateway",
        )
        self.assertEqual("[REDACTED]", first["details"]["token"])
        self.assertEqual("[REDACTED]", first["details"]["nested"]["password"])
        self.assertEqual("visible", first["details"]["nested"]["safe"])
        self.assertEqual(first["eventHash"], second["previousHash"])
        self.assertNotIn(b"raw-invite-token-must-never-persist", self.ledger.db_path.read_bytes())
        self.assertNotIn(b"raw-password", self.ledger.db_path.read_bytes())

        with contextlib.closing(sqlite3.connect(self.ledger.db_path)) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE security_events SET outcome='success' WHERE sequence=1")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM security_events WHERE sequence=1")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE security_event_meta SET value='99' WHERE key='schema_version'")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM security_event_meta WHERE key='schema_version'")

        integrity = self.ledger.verify_integrity()
        self.assertTrue(integrity["healthy"])
        self.assertEqual(2, integrity["eventCount"])

    def test_tamper_detection_blocks_future_protected_appends(self) -> None:
        self.ledger.append(
            category="integrity",
            action="baseline",
            outcome="success",
            surface="system",
            details={"value": 1},
        )
        with contextlib.closing(sqlite3.connect(self.ledger.db_path)) as connection:
            connection.execute("DROP TRIGGER security_events_no_update")
            connection.execute("UPDATE security_events SET details_json='{}' WHERE sequence=1")
            connection.commit()

        result = self.ledger.verify_integrity()
        self.assertFalse(result["healthy"])
        self.assertTrue(any(item["code"] in {"immutability_trigger_missing", "event_hash_mismatch"} for item in result["issues"]))
        with self.assertRaises(SecurityEventError):
            self.ledger.append(
                category="integrity",
                action="blocked_append",
                outcome="failure",
                surface="system",
            )


    def test_missing_trigger_remains_a_restart_integrity_failure(self) -> None:
        self.ledger.append(
            category="integrity",
            action="baseline",
            outcome="success",
            surface="system",
        )
        with contextlib.closing(sqlite3.connect(self.ledger.db_path)) as connection:
            connection.execute("DROP TRIGGER security_events_no_delete")
            connection.commit()

        reopened = SecurityEventLedger(self.data_dir)
        self.assertFalse(reopened.startup_integrity["healthy"])
        self.assertTrue(any(
            issue.get("trigger") == "security_events_no_delete"
            for issue in reopened.startup_integrity["issues"]
        ))
        with self.assertRaises(SecurityEventError):
            reopened.append(
                category="integrity",
                action="blocked_after_restart",
                outcome="failure",
                surface="system",
            )

    def test_cross_process_appends_are_monotonic_and_restart_verifies(self) -> None:
        workers = 2
        count = 6
        worker_script = """
import sys
from pathlib import Path
from forgetrace.security_events import SecurityEventLedger

ledger = SecurityEventLedger(Path(sys.argv[1]))
prefix = sys.argv[2]
count = int(sys.argv[3])
for index in range(count):
    ledger.append(
        category='concurrency',
        action='worker_append',
        outcome='success',
        surface='system',
        subject_id=f'{prefix}-{index}',
        details={'worker': prefix, 'index': index},
    )
print(count)
"""
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", worker_script, str(self.data_dir), f"worker-{index}", str(count)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(workers)
        ]
        results: list[int] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(0, process.returncode, stderr)
            results.append(int(stdout.strip()))
        self.assertEqual([count] * workers, results)

        reopened = SecurityEventLedger(self.data_dir)
        result = reopened.query(limit=100)
        self.assertTrue(result["integrity"]["healthy"])
        self.assertEqual(workers * count, result["total"])
        sequences = sorted(item["sequence"] for item in result["events"])
        self.assertEqual(list(range(1, workers * count + 1)), sequences)


class SecurityEventApplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        project_root = Path(__file__).resolve().parents[1]
        self.app = build_application(project_root, self.root / "data")
        record = self.app.registry.register_repository(
            path=str(self.root / "repo"),
            name="Security Ledger",
            author="Rooke Poole",
            initialize=True,
            create_directory=True,
        )
        self.repository_id = record["id"]

    def tearDown(self) -> None:
        if self.app.gateway:
            self.app.gateway.stop()
        self.temp.cleanup()

    @staticmethod
    def _serve(app, surface: str):
        server = create_server(app, "127.0.0.1", 0, surface=surface)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    @staticmethod
    def _request(server, method: str, path: str):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        status = response.status
        connection.close()
        return status, headers, body

    def _tamper_security_ledger(self) -> None:
        with contextlib.closing(sqlite3.connect(self.app.security_events.db_path)) as connection:
            connection.execute("DROP TRIGGER security_events_no_update")
            connection.execute("UPDATE security_events SET details_json='{}' WHERE sequence=1")
            connection.commit()

    def test_owner_can_filter_and_export_but_gateway_cannot_read_ledger(self) -> None:
        self.app.audit(
            category="test",
            action="filter_target",
            outcome="success",
            surface="system",
            repository_id=self.repository_id,
            details={"marker": "browser-visible"},
        )
        owner, owner_thread = self._serve(self.app, "owner")
        gateway, gateway_thread = self._serve(self.app, "gateway")
        try:
            status, headers, body = self._request(
                owner, "GET", "/api/v1/security-events?category=test&search=browser-visible"
            )
            self.assertEqual(200, status)
            self.assertTrue(headers.get("X-ForgeTrace-Request-Id", "").startswith("req_"))
            payload = json.loads(body)
            self.assertEqual(1, payload["total"])
            self.assertEqual("filter_target", payload["events"][0]["action"])

            status, headers, body = self._request(
                owner, "GET", "/api/v1/security-events/export?category=test"
            )
            self.assertEqual(200, status)
            self.assertIn("attachment", headers.get("Content-Disposition", ""))
            exported = json.loads(body)
            self.assertTrue(exported["integrity"]["healthy"])
            self.assertGreaterEqual(exported["eventCount"], 1)

            private_search = "raw-private-search-value"
            status, _headers, _body = self._request(
                owner, "GET", f"/api/v1/security-events/export?search={private_search}"
            )
            self.assertEqual(200, status)
            exported_events = self.app.security_events.query(action="security_events_exported", limit=10)["events"]
            self.assertTrue(exported_events)
            self.assertNotIn(private_search, json.dumps(exported_events))
            for ledger_file in self.app.security_events.db_path.parent.glob("security-events.sqlite3*"):
                self.assertNotIn(private_search.encode("utf-8"), ledger_file.read_bytes(), ledger_file.name)

            status, _headers, body = self._request(gateway, "GET", "/api/v1/security-events")
            self.assertEqual(403, status)
            self.assertEqual("remote_owner_api_blocked", json.loads(body)["code"])
        finally:
            owner.shutdown()
            owner.server_close()
            owner_thread.join(timeout=2)
            gateway.shutdown()
            gateway.server_close()
            gateway_thread.join(timeout=2)

    def test_invite_token_never_enters_security_ledger(self) -> None:
        result = self.app.collaboration.create_invite(
            self.repository_id,
            label="Security review",
            allow_sensitive_source=True,
        )
        token = result["token"]
        ledger_files = list(self.app.security_events.db_path.parent.glob("security-events.sqlite3*"))
        self.assertTrue(ledger_files)
        for ledger_file in ledger_files:
            self.assertNotIn(token.encode("utf-8"), ledger_file.read_bytes(), ledger_file.name)
        events = self.app.security_events.query(action="invite_created", limit=10)["events"]
        self.assertEqual(1, len(events))
        self.assertNotEqual(token, events[0]["details"]["inviteFingerprint"])
        self.assertEqual(16, len(events[0]["details"]["inviteFingerprint"]))

    def test_tampered_ledger_fails_closed_before_invite_creation(self) -> None:
        self._tamper_security_ledger()
        with self.assertRaises(ForgeTraceError) as blocked:
            self.app.collaboration.create_invite(self.repository_id)
        self.assertEqual("security_event_ledger_unavailable", blocked.exception.code)
        with contextlib.closing(sqlite3.connect(self.app.collaboration.db_path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM collaboration_invites").fetchone()[0]
        self.assertEqual(0, count)

    def test_tampered_ledger_fails_closed_before_gateway_bind(self) -> None:
        self._tamper_security_ledger()
        with self.assertRaises(ForgeTraceError) as blocked:
            self.app.gateway.start(port=0, host="127.0.0.1")
        self.assertEqual("security_event_ledger_unavailable", blocked.exception.code)
        self.assertFalse(self.app.gateway.status()["enabled"])

    def test_tampered_ledger_fails_closed_before_sensitive_export(self) -> None:
        service = self.app.registry.repository_service(self.repository_id)
        service.write_file(".env", b"PRIVATE_KEY=must-not-export", "Rooke", "Add private fixture")
        transfer_dir = self.app.registry.data_dir / "transfers"
        existing = set(transfer_dir.glob("export-*.zip")) if transfer_dir.exists() else set()
        self._tamper_security_ledger()

        owner, owner_thread = self._serve(self.app, "owner")
        try:
            status, _headers, body = self._request(
                owner,
                "GET",
                f"/api/v1/repositories/{self.repository_id}/export?history=0&confirmSensitive=1",
            )
            self.assertEqual(503, status)
            self.assertEqual("security_event_ledger_unavailable", json.loads(body)["code"])
        finally:
            owner.shutdown()
            owner.server_close()
            owner_thread.join(timeout=2)

        current = set(transfer_dir.glob("export-*.zip")) if transfer_dir.exists() else set()
        self.assertEqual(existing, current)

    def test_tampered_ledger_fails_closed_before_pull_request_merge(self) -> None:
        invitation = self.app.collaboration.create_invite(self.repository_id)
        token = invitation["token"]
        pull_request = self.app.collaboration.create_pull_request(
            token,
            title="Protected merge",
            description="Verify audit failure prevents mutation.",
            author_name="Contributor",
        )
        self.app.collaboration.upload_pull_request_file(
            token,
            pull_request["id"],
            "protected.txt",
            b"must remain quarantined\n",
        )
        self.app.collaboration.submit_pull_request(token, pull_request["id"])
        approved = self.app.collaboration.review_pull_request(
            self.repository_id,
            pull_request["id"],
            reviewer="Rooke",
            verdict="approved",
            comment="Reviewed for the fail-closed test.",
        )
        workspace_file = self.app.registry.repository_service(self.repository_id).workspace / "protected.txt"
        self.assertFalse(workspace_file.exists())
        self._tamper_security_ledger()

        with self.assertRaises(ForgeTraceError) as blocked:
            self.app.collaboration.merge_pull_request(
                self.repository_id,
                pull_request["id"],
                merged_by="Rooke",
                confirmation=f"MERGE #{approved['number']}",
                expected_revision=approved["revision"],
            )
        self.assertEqual("security_event_ledger_unavailable", blocked.exception.code)
        self.assertFalse(workspace_file.exists())
        persisted = self.app.collaboration.get_pull_request(self.repository_id, pull_request["id"])
        self.assertEqual("approved", persisted["status"])

    def test_owner_ui_contains_security_event_viewer(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        for marker in (
            "securityEventsBtn",
            "securityEventsBackdrop",
            "securityEventIntegrity",
            "securityEventList",
            "/api/v1/security-events",
            "exportSecurityEvents",
        ):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()
