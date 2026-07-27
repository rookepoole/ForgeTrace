from __future__ import annotations

import contextlib
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from forgetrace.app import build_application
from forgetrace.errors import ForgeTraceError
from forgetrace.web import create_server

ROOT = Path(__file__).resolve().parents[1]


class HealthDashboardFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = build_application(ROOT, self.root / "data")
        self.app.owner_instance_lock_held = True
        record = self.app.registry.register_repository(
            path=str(self.root / "repo"),
            name="Health Dashboard",
            description="v0.4.6 fixture",
            author="Owner",
            initialize=True,
            create_directory=True,
        )
        self.repository_id = record["id"]
        self.repository = self.app.registry.repository_service(self.repository_id)
        self.repository.write_file("alpha.txt", b"alpha\n", "Owner", "seed")
        self.repository.write_file("beta.txt", b"beta\n", "Owner", "seed")
        self.repository.create_commit("baseline", "Owner")

    def tearDown(self) -> None:
        if self.app.gateway:
            self.app.gateway.stop()
        self.temp.cleanup()

    def generate(self, **kwargs):
        return self.app.health.generate(
            request_id=kwargs.pop("request_id", "req_health_test"),
            repository_id=kwargs.pop("repository_id", self.repository_id),
            **kwargs,
        )

    @staticmethod
    def codes(report: dict) -> set[str]:
        return {
            finding["code"]
            for section in report["sections"].values()
            for finding in section.get("findings", [])
        }


class HealthDashboardServiceTest(HealthDashboardFixture):
    def test_report_is_durable_hash_verified_and_read_only(self) -> None:
        state_path = self.repository.state_path
        before_state = state_path.read_bytes()
        before_revision = self.repository.load_state()["revision"]
        report = self.generate()
        self.assertEqual(
            {"system", "registry", "repositories", "git", "recovery", "security", "access", "collaboration", "project", "storage"},
            set(report["sections"]),
        )
        self.assertEqual("req_health_test", report["requestId"])
        self.assertEqual(64, len(report["reportHash"]))
        self.assertEqual(before_state, state_path.read_bytes())
        self.assertEqual(before_revision, self.repository.load_state()["revision"])
        loaded = self.app.health.get_report(report["reportId"])
        self.assertEqual(report["reportHash"], loaded["reportHash"])
        listed = self.app.health.list_reports()
        self.assertEqual(1, listed["total"])
        self.assertEqual(report["reportId"], listed["reports"][0]["reportId"])
        exported = self.app.health.export_report(report["reportId"], request_id="req_export")
        self.assertEqual("forgetrace-health-report-export", exported["format"])
        self.assertEqual(report["reportHash"], exported["report"]["reportHash"])
        self.assertTrue((self.app.registry.data_dir / "health-reports" / f"{report['reportId']}.json").is_file())

    def test_pending_transaction_and_corrupt_object_are_reported_without_recovery(self) -> None:
        state = self.repository.load_state()
        digest = next(iter(state["commits"][-1]["manifest"].values()))["hash"]
        self.repository.object_path(digest).write_bytes(b"corrupt")
        transaction = self.repository.meta_dir / "transactions" / "txn-health-test"
        (transaction / "backups").mkdir(parents=True)
        journal = {
            "schemaVersion": 1,
            "id": "txn-health-test",
            "operation": "file_saved",
            "status": "pending",
            "createdAt": "2026-07-25T00:00:00Z",
            "stateRevisionBefore": state["revision"],
            "records": [],
        }
        (transaction / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
        report = self.generate()
        codes = self.codes(report)
        self.assertIn("pending_repository_transaction", codes)
        self.assertIn("snapshot_object_integrity", codes)
        self.assertTrue(transaction.is_dir(), "health assessment must not recover or delete journals")
        self.assertEqual(b"corrupt", self.repository.object_path(digest).read_bytes())

    def test_bounded_scan_is_explicitly_partial(self) -> None:
        report = self.generate(
            limits={
                "objects": 1,
                "commitsPerRepository": 1,
                "hashIndexEntriesPerRepository": 1,
            }
        )
        self.assertFalse(report["complete"])
        self.assertIn("snapshot_verification_partial", self.codes(report))
        self.assertFalse(report["sections"]["repositories"]["complete"])

    def test_access_mode_mismatch_fails_closed_and_is_visible(self) -> None:
        payload = json.loads(self.repository.state_path.read_text(encoding="utf-8"))
        payload["repository"]["accessMode"] = "invalid-mode"
        self.repository.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        report = self.generate()
        self.assertIn("repository_access_mode_mismatch", self.codes(report))
        policy = report["sections"]["repositories"]["data"]["repositories"][0]["accessPolicy"]
        self.assertEqual("read_only", policy["effectiveMode"])
        self.assertFalse(policy["embeddedValid"])

    def test_corrupt_ledger_is_reported_but_report_still_persists(self) -> None:
        with contextlib.closing(sqlite3.connect(self.app.security_events.db_path)) as connection:
            connection.execute("DROP TRIGGER security_events_no_update")
            connection.execute("UPDATE security_events SET details_json='{}' WHERE sequence=1")
            connection.commit()
        report = self.generate()
        self.assertTrue({"immutability_trigger_missing", "event_hash_mismatch"} & self.codes(report))
        self.assertEqual("critical", report["sections"]["security"]["status"])
        self.assertEqual(report["reportHash"], self.app.health.get_report(report["reportId"])["reportHash"])

    def test_review_evidence_tamper_is_detected(self) -> None:
        invite = self.app.collaboration.create_invite(self.repository_id, max_uses=2)
        pr = self.app.collaboration.create_pull_request(
            invite["token"], title="Health evidence", description="tamper test", author_name="Contributor"
        )
        self.app.collaboration.upload_pull_request_file(invite["token"], pr["id"], "review.txt", b"submitted\n")
        submitted = self.app.collaboration.submit_pull_request(invite["token"], pr["id"])
        evidence = self.app.collaboration.review_conversations._revision_file(
            self.repository_id, submitted["id"], submitted["revision"], "review.txt"
        )
        evidence.write_bytes(b"tampered\n")
        report = self.generate()
        self.assertIn("review_revision_integrity_failed", self.codes(report))
        self.assertEqual("critical", report["sections"]["collaboration"]["status"])


    def test_missing_lock_probe_is_read_only(self) -> None:
        from forgetrace.locks import inspect_file_lock

        path = self.root / "never-created.lock"
        result = inspect_file_lock(path)
        self.assertEqual("not_created", result["state"])
        self.assertTrue(result["available"])
        self.assertFalse(path.exists())

    def test_report_tampering_fails_integrity_verification(self) -> None:
        report = self.generate()
        path = self.app.registry.data_dir / "health-reports" / f"{report['reportId']}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ForgeTraceError) as failed:
            self.app.health.get_report(report["reportId"])
        self.assertEqual("health_report_integrity_failed", failed.exception.code)


class HealthDashboardApiTest(HealthDashboardFixture):
    def setUp(self) -> None:
        super().setUp()
        self.owner_server = create_server(self.app, "127.0.0.1", 0, surface="owner")
        self.owner_thread = threading.Thread(target=self.owner_server.serve_forever, daemon=True)
        self.owner_thread.start()
        self.gateway_server = create_server(self.app, "127.0.0.1", 0, surface="gateway")
        self.gateway_thread = threading.Thread(target=self.gateway_server.serve_forever, daemon=True)
        self.gateway_thread.start()

    def tearDown(self) -> None:
        for server, thread in ((self.owner_server, self.owner_thread), (self.gateway_server, self.gateway_thread)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        super().tearDown()

    @staticmethod
    def request(server, method: str, path: str, payload: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), json.loads(raw) if raw else {}
        connection.close()
        return result

    def test_owner_report_flow_and_gateway_denial(self) -> None:
        status, headers, report = self.request(
            self.owner_server,
            "POST",
            "/api/v1/health/reports",
            {"repositoryId": self.repository_id, "scope": "standard"},
        )
        self.assertEqual(201, status)
        self.assertEqual(headers["X-ForgeTrace-Request-Id"], report["requestId"])
        report_id = report["reportId"]
        status, _headers, listed = self.request(self.owner_server, "GET", "/api/v1/health/reports")
        self.assertEqual(200, status)
        self.assertEqual(report_id, listed["reports"][0]["reportId"])
        status, _headers, detail = self.request(self.owner_server, "GET", f"/api/v1/health/reports/{report_id}")
        self.assertEqual(200, status)
        self.assertEqual(report["reportHash"], detail["reportHash"])
        status, export_headers, exported = self.request(
            self.owner_server, "GET", f"/api/v1/health/reports/{report_id}/export"
        )
        self.assertEqual(200, status)
        self.assertIn("attachment", export_headers["Content-Disposition"])
        self.assertEqual(report_id, exported["report"]["reportId"])

        for method, path, payload in (
            ("GET", "/api/v1/health/reports", None),
            ("POST", "/api/v1/health/reports", {}),
            ("GET", f"/api/v1/health/reports/{report_id}", None),
        ):
            status, _headers, denied = self.request(self.gateway_server, method, path, payload)
            self.assertEqual(403, status)
            self.assertEqual("remote_owner_api_blocked", denied["code"])

    def test_doctor_repair_uses_existing_authority_and_fails_closed_on_bad_ledger(self) -> None:
        state = json.loads(self.repository.state_path.read_text(encoding="utf-8"))
        state["repository"]["name"] = "Embedded Drift"
        self.repository.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        status, _headers, checked = self.request(self.owner_server, "GET", "/api/v1/doctor")
        self.assertEqual(200, status)
        self.assertIn("registry_metadata_drift", {item["code"] for item in checked["issues"]})
        status, _headers, repaired = self.request(
            self.owner_server, "POST", "/api/v1/doctor", {"repair": True, "scanRoots": []}
        )
        self.assertEqual(200, status)
        self.assertTrue(repaired["actions"])
        actions = self.app.security_events.query(action="doctor_repair_authorized", limit=20)["events"]
        self.assertTrue(actions)

        state["repository"]["name"] = "Second Drift"
        self.repository.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        with contextlib.closing(sqlite3.connect(self.app.security_events.db_path)) as connection:
            connection.execute("DROP TRIGGER security_events_no_update")
            connection.execute("UPDATE security_events SET details_json='{}' WHERE sequence=1")
            connection.commit()
        status, _headers, denied = self.request(
            self.owner_server, "POST", "/api/v1/doctor", {"repair": True, "scanRoots": []}
        )
        self.assertEqual(503, status)
        self.assertEqual("security_event_ledger_unavailable", denied["code"])
        self.assertEqual("Second Drift", json.loads(self.repository.state_path.read_text())["repository"]["name"])


if __name__ == "__main__":
    unittest.main()
