from __future__ import annotations

import contextlib
import http.client
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from forgetrace.app import build_application
from forgetrace.security_events import SecurityEventError, SecurityEventLedger
from forgetrace.web import create_server


class SecurityRetentionServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "data"
        self.ledger = SecurityEventLedger(self.data_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _append(self, count: int, *, prefix: str = "event", occurred_at: str = "") -> None:
        for index in range(count):
            self.ledger.append(
                category="retention",
                action="fixture_append",
                outcome="success",
                surface="system",
                subject_id=f"{prefix}-{index}",
                occurred_at=occurred_at,
                details={"index": index, "prefix": prefix},
            )

    def _rotate(self, count: int) -> dict:
        preview = self.ledger.preview_rotation(rotate_count=count)
        self.assertTrue(preview["canRotate"])
        return self.ledger.execute_rotation(
            preview_id=preview["previewId"],
            rotate_count=count,
            request_id="req_rotation_test",
        )

    def test_rotation_preserves_one_logical_chain_query_export_and_restart(self) -> None:
        self._append(12)
        before = self.ledger.verify_integrity()
        result = self._rotate(6)
        self.assertEqual((1, 6), (result["segment"]["firstSequence"], result["segment"]["lastSequence"]))
        after = self.ledger.verify_integrity()
        self.assertTrue(after["healthy"])
        self.assertEqual(1, after["segmentCount"])
        self.assertEqual(6, after["segmentEventCount"])
        self.assertGreater(after["activeEventCount"], 6)
        self.assertGreater(after["lastSequence"], before["lastSequence"])
        queried = self.ledger.query(limit=1000)
        self.assertEqual(after["eventCount"], queried["total"])
        self.assertEqual(after["lastSequence"], queried["events"][0]["sequence"])
        exported = self.ledger.export(category="retention")
        self.assertTrue(exported["integrity"]["healthy"])
        self.assertEqual(list(range(1, 13)), [item["sequence"] for item in exported["events"] if item["action"] == "fixture_append"])

        reopened = SecurityEventLedger(self.data_dir)
        self.assertTrue(reopened.startup_integrity["healthy"])
        self.assertEqual(after["lastHash"], reopened.startup_integrity["lastHash"])
        appended = reopened.append(category="retention", action="after_restart", outcome="success")
        self.assertEqual(after["lastSequence"] + 1, appended["sequence"])

    def test_stale_preview_is_rejected_before_segment_or_database_replacement(self) -> None:
        self._append(8)
        preview = self.ledger.preview_rotation(rotate_count=4)
        self.ledger.append(category="retention", action="preview_invalidated", outcome="success")
        before_digest = self.ledger.verify_integrity()["activeDigest"]
        with self.assertRaisesRegex(SecurityEventError, "stale"):
            self.ledger.execute_rotation(preview_id=preview["previewId"], rotate_count=4)
        self.assertFalse(list(self.ledger.segments_dir.glob("segment_*.json")))
        self.assertEqual(before_digest, self.ledger.verify_integrity()["activeDigest"])

    def test_missing_truncated_and_substituted_segments_fail_closed(self) -> None:
        for mode in ("missing", "truncated", "substituted"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                ledger = SecurityEventLedger(Path(td))
                for index in range(16):
                    ledger.append(category="segment", action="fixture", outcome="success", subject_id=str(index))
                first = ledger.preview_rotation(rotate_count=5)
                ledger.execute_rotation(preview_id=first["previewId"], rotate_count=5)
                second = ledger.preview_rotation(rotate_count=5)
                ledger.execute_rotation(preview_id=second["previewId"], rotate_count=5)
                segments = sorted(ledger.segments_dir.glob("segment_*.json"))
                self.assertEqual(2, len(segments))
                if mode == "missing":
                    segments[-1].unlink()
                elif mode == "truncated":
                    segments[0].write_bytes(segments[0].read_bytes()[:80])
                else:
                    segments[0].write_bytes(segments[1].read_bytes())
                integrity = ledger.verify_integrity()
                self.assertFalse(integrity["healthy"])
                with self.assertRaises(SecurityEventError):
                    ledger.append(category="segment", action="blocked", outcome="failure")

    def test_failed_install_rolls_back_active_database_and_new_segment(self) -> None:
        self._append(9)
        preview = self.ledger.preview_rotation(rotate_count=4)
        original_replace = __import__("forgetrace.security_events", fromlist=["os"]).os.replace

        def selective_replace(source, destination):
            if Path(source).name == "active-after.sqlite3" and Path(destination) == self.ledger.db_path:
                raise OSError("injected active install failure")
            return original_replace(source, destination)

        with mock.patch("forgetrace.security_events.os.replace", side_effect=selective_replace):
            with self.assertRaisesRegex(SecurityEventError, "rolled back"):
                self.ledger.execute_rotation(preview_id=preview["previewId"], rotate_count=4)
        integrity = self.ledger.verify_integrity()
        self.assertTrue(integrity["healthy"], integrity)
        self.assertEqual(0, integrity["segmentCount"])
        journals = self.ledger.list_rotation_journals()
        self.assertEqual("rolled_back", journals[0]["state"])

    def test_startup_recovers_an_incomplete_rotation_journal(self) -> None:
        self._append(3)
        operation = self.ledger.rotations_dir / "rotation_manual"
        operation.mkdir()
        backup = operation / "active-before.sqlite3"
        with self.ledger.lock:
            self.ledger._backup_active_locked(backup)
        self.ledger.append(category="recovery", action="should_disappear", outcome="success")
        segment_final = self.ledger.segments_dir / "segment_00000000000000000001_00000000000000000001_seg_fake.json"
        segment_final.write_text("fake", encoding="utf-8")
        journal = {
            "format": "forgetrace-security-rotation-journal",
            "schemaVersion": 1,
            "rotationId": "rotation_manual",
            "createdAt": "2026-01-01T00:00:00Z",
            "state": "installing",
            "activeBackup": str(backup),
            "segmentFinal": str(segment_final),
            "prunedBackupDir": str(operation / "pruned-segments"),
            "oldRootExisted": False,
            "rootBefore": str(operation / "retention-root-before.json"),
        }
        with self.ledger.lock:
            self.ledger._journal_write_locked(self.ledger.rotations_dir / "rotation_manual.json", journal)

        reopened = SecurityEventLedger(self.data_dir)
        self.assertTrue(reopened.startup_integrity["healthy"])
        self.assertEqual(1, reopened.startup_rotation_recovery["rolledBack"])
        self.assertFalse(segment_final.exists())
        self.assertEqual(3, reopened.query(action="fixture_append", limit=100)["total"])
        self.assertEqual(0, reopened.query(action="should_disappear", limit=100)["total"])

    def test_retention_prunes_only_verified_whole_segments_and_preserves_checkpoint(self) -> None:
        self.ledger.update_retention_policy({
            "maxActiveEvents": 3,
            "segmentEventTarget": 2,
            "maxRetainedEvents": 6,
            "maxRetentionAgeDays": 1,
            "maxStorageBytes": 1024 * 1024 * 1024,
            "minimumProtectedEvents": 2,
            "minimumProtectedAgeDays": 0,
        })
        self._append(10, occurred_at="2020-01-01T00:00:00Z")
        first = self._rotate(4)
        second_preview = self.ledger.preview_rotation(rotate_count=4)
        self.assertIn(first["segment"]["segmentId"], second_preview["retention"]["pruneSegmentIds"])
        second = self.ledger.execute_rotation(
            preview_id=second_preview["previewId"], rotate_count=4, request_id="req_prune"
        )
        self.assertGreaterEqual(second["prunedSegmentCount"], 1)
        integrity = self.ledger.verify_integrity()
        self.assertTrue(integrity["healthy"], integrity)
        self.assertGreater(integrity["deletedEventCount"], 0)
        self.assertEqual(integrity["deletedEventCount"] + 1, integrity["retainedStartSequence"])
        root = json.loads(self.ledger.retention_root_path.read_text(encoding="utf-8"))
        self.assertEqual(integrity["deletedEventCount"], root["lastDeletedSequence"])

    def test_rotation_lock_serializes_another_process(self) -> None:
        self._append(4)
        script = """
import sys
from pathlib import Path
from forgetrace.security_events import SecurityEventLedger
ledger=SecurityEventLedger(Path(sys.argv[1]))
print(ledger.preview_rotation(rotate_count=1)['rotateCount'], flush=True)
"""
        self.ledger.lock.acquire()
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(self.data_dir)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            time.sleep(0.3)
            self.assertIsNone(process.poll())
        finally:
            self.ledger.lock.release()
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(0, process.returncode, stderr)
        self.assertEqual("1", stdout.strip())

    def test_preview_scans_all_journals_and_terminal_history_is_bounded(self) -> None:
        self._append(4)
        with self.ledger.lock:
            for index in range(105):
                self.ledger._journal_write_locked(
                    self.ledger.rotations_dir / f"rotation_{index + 1000:04d}.json",
                    {
                        "format": "forgetrace-security-rotation-journal",
                        "schemaVersion": 1,
                        "rotationId": f"rotation_{index + 1000:04d}",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "state": "completed",
                    },
                )
            self.ledger._journal_write_locked(
                self.ledger.rotations_dir / "rotation_0000.json",
                {
                    "format": "forgetrace-security-rotation-journal",
                    "schemaVersion": 1,
                    "rotationId": "rotation_0000",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "state": "installing",
                },
            )
        self.assertEqual(100, len(self.ledger.list_rotation_journals()))
        with self.assertRaisesRegex(SecurityEventError, "incomplete"):
            self.ledger.preview_rotation(rotate_count=1)
        with self.ledger.lock:
            incomplete_path = self.ledger.rotations_dir / "rotation_0000.json"
            incomplete = self.ledger._load_rotation_journal_locked(incomplete_path)
            incomplete["state"] = "rolled_back"
            self.ledger._journal_write_locked(incomplete_path, incomplete)
            removed = self.ledger._prune_completed_rotation_journals_locked(maximum=100)
        self.assertEqual(6, removed)
        self.assertEqual(100, len(list(self.ledger.rotations_dir.glob("rotation_*.json"))))
        self.assertTrue(self.ledger.preview_rotation(rotate_count=1)["canRotate"])

    def test_tampered_or_path_escaping_journal_blocks_rotation_without_touching_external_file(self) -> None:
        self._append(4)
        external = Path(self.temp.name) / "outside.sqlite3"
        external.write_bytes(b"do-not-touch")
        journal_path = self.ledger.rotations_dir / "rotation_escape.json"
        with self.ledger.lock:
            self.ledger._journal_write_locked(
                journal_path,
                {
                    "format": "forgetrace-security-rotation-journal",
                    "schemaVersion": 1,
                    "rotationId": "rotation_escape",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "state": "installing",
                    "activeBackup": str(external),
                    "segmentFinal": str(self.ledger.segments_dir / "segment_fake.json"),
                    "prunedBackupDir": str(self.ledger.rotations_dir / "rotation_escape" / "pruned"),
                    "oldRootExisted": False,
                    "rootBefore": str(self.ledger.rotations_dir / "rotation_escape" / "root.json"),
                },
            )
        reopened = SecurityEventLedger(self.data_dir)
        self.assertEqual(b"do-not-touch", external.read_bytes())
        self.assertEqual(1, reopened.startup_rotation_recovery["failed"])
        with self.assertRaisesRegex(SecurityEventError, "incomplete"):
            reopened.preview_rotation(rotate_count=1)

        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        payload["state"] = "completed"
        journal_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(SecurityEventError, "incomplete"):
            reopened.preview_rotation(rotate_count=1)

    def test_anchor_request_and_owner_receipt_are_bound_without_external_claim(self) -> None:
        self._append(8)
        self._rotate(4)
        request = self.ledger.create_anchor_request(request_id="req_anchor")
        self.assertFalse(request["externalPublicationVerified"])
        self.assertEqual(request, self.ledger.get_anchor_request(request["anchorId"]))
        with self.assertRaisesRegex(SecurityEventError, "does not match"):
            self.ledger.record_anchor_receipt(
                request["anchorId"], anchored_digest="0" * 64,
                mechanism="manual", evidence="wrong digest",
            )
        receipt = self.ledger.record_anchor_receipt(
            request["anchorId"], anchored_digest=request["anchorDigest"],
            mechanism="signed-file", external_reference="receipt-42",
            evidence="owner retained signed receipt",
        )
        self.assertTrue(receipt["bindingVerified"])
        self.assertFalse(receipt["externalPublicationVerified"])
        anchors = self.ledger.list_anchors()
        self.assertEqual(0, anchors["unanchoredSegmentCount"])
        self.assertTrue(anchors["anchors"][0]["receiptRecorded"])

    def test_anchor_receipt_tamper_and_policy_tamper_are_health_visible(self) -> None:
        self._append(7)
        self._rotate(3)
        request = self.ledger.create_anchor_request()
        self.ledger.record_anchor_receipt(
            request["anchorId"], anchored_digest=request["anchorDigest"],
            mechanism="manual", evidence="receipt",
        )
        receipt_path = self.ledger._anchor_receipt_path(request["anchorId"])
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        payload["evidence"] = "tampered"
        receipt_path.write_text(json.dumps(payload), encoding="utf-8")
        anchors = self.ledger.list_anchors()
        self.assertTrue(anchors["invalid"])

        policy = json.loads(self.ledger.policy_path.read_text(encoding="utf-8"))
        policy["maxActiveEvents"] = 2
        self.ledger.policy_path.write_text(json.dumps(policy), encoding="utf-8")
        status = self.ledger.operational_status()
        self.assertIn("hash", status["policyError"].lower())
        with self.assertRaises(SecurityEventError):
            self.ledger.preview_rotation(rotate_count=1)
        # Policy corruption does not rewrite the already verified event chain.
        self.assertTrue(self.ledger.verify_integrity()["healthy"])


class SecurityRetentionApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        project_root = Path(__file__).resolve().parents[1]
        self.app = build_application(project_root, self.root / "data")
        for index in range(9):
            self.app.security_events.append(
                category="retention", action="api_fixture", outcome="success", subject_id=str(index)
            )
        self.owner, self.owner_thread = self._serve("owner")
        self.gateway, self.gateway_thread = self._serve("gateway")

    def tearDown(self) -> None:
        for server, thread in ((self.owner, self.owner_thread), (self.gateway, self.gateway_thread)):
            server.shutdown(); server.server_close(); thread.join(timeout=5)
        self.temp.cleanup()

    def _serve(self, surface: str):
        server = create_server(self.app, "127.0.0.1", 0, surface=surface)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    @staticmethod
    def _request(server, method: str, path: str, payload: dict | None = None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def test_owner_rotation_anchor_receipt_flow_and_gateway_denial(self) -> None:
        status, _headers, body = self._request(
            self.owner, "POST", "/api/v1/security-events/rotation-preview", {"rotateCount": 4}
        )
        self.assertEqual(200, status, body)
        preview = json.loads(body)
        status, _headers, body = self._request(
            self.owner, "POST", "/api/v1/security-events/rotate",
            {"previewId": preview["previewId"], "rotateCount": preview["rotateCount"]},
        )
        self.assertEqual(200, status, body)
        rotation = json.loads(body)
        self.assertEqual("completed", rotation["state"])

        status, _headers, body = self._request(self.owner, "GET", "/api/v1/security-events/segments")
        self.assertEqual(200, status)
        self.assertEqual(1, len(json.loads(body)["segments"]))

        status, _headers, body = self._request(self.owner, "POST", "/api/v1/security-events/anchors", {})
        self.assertEqual(201, status, body)
        anchor = json.loads(body)
        status, headers, exported = self._request(
            self.owner, "GET", f"/api/v1/security-events/anchors/{anchor['anchorId']}/export"
        )
        self.assertEqual(200, status)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertEqual(anchor["anchorDigest"], json.loads(exported)["anchorDigest"])
        status, _headers, body = self._request(
            self.owner, "POST", f"/api/v1/security-events/anchors/{anchor['anchorId']}/receipt",
            {"anchoredDigest": anchor["anchorDigest"], "mechanism": "test-receipt", "evidence": "receipt bytes"},
        )
        self.assertEqual(200, status, body)
        self.assertTrue(json.loads(body)["bindingVerified"])

        for method, path, payload in (
            ("GET", "/api/v1/security-events/segments", None),
            ("GET", "/api/v1/security-events/anchors", None),
            ("POST", "/api/v1/security-events/rotation-preview", {"rotateCount": 1}),
            ("POST", "/api/v1/security-events/anchors", {}),
        ):
            status, _headers, _body = self._request(self.gateway, method, path, payload)
            self.assertEqual(403, status, (method, path, status))

    def test_owner_can_update_retention_policy_and_gateway_cannot(self) -> None:
        payload = {
            "maxActiveEvents": 20,
            "segmentEventTarget": 5,
            "maxRetainedEvents": 200,
            "maxRetentionAgeDays": 365,
            "maxStorageBytes": 64 * 1024 * 1024,
            "minimumProtectedEvents": 10,
            "minimumProtectedAgeDays": 7,
        }
        status, _headers, body = self._request(
            self.owner, "POST", "/api/v1/security-events/retention-policy", payload
        )
        self.assertEqual(200, status, body)
        saved = json.loads(body)
        self.assertEqual(20, saved["maxActiveEvents"])
        self.assertRegex(saved["policyHash"], r"^[0-9a-f]{64}$")
        status, _headers, body = self._request(
            self.owner, "GET", "/api/v1/security-events/retention-policy"
        )
        self.assertEqual(200, status, body)
        self.assertEqual(saved["policyHash"], json.loads(body)["policyHash"])
        status, _headers, _body = self._request(
            self.gateway, "POST", "/api/v1/security-events/retention-policy", payload
        )
        self.assertEqual(403, status)
        actions = self.app.security_events.query(action="security_retention_policy_changed", limit=10)
        self.assertEqual(1, actions["total"])

    def test_health_reports_unanchored_segments_and_invalid_receipts(self) -> None:
        preview = self.app.security_events.preview_rotation(rotate_count=4)
        self.app.security_events.execute_rotation(preview_id=preview["previewId"], rotate_count=4)
        report = self.app.health.generate(request_id="req_health_retention", scope="standard")
        codes = {
            finding["code"]
            for section in report["sections"].values()
            for finding in section.get("findings", [])
        }
        self.assertIn("security_segments_unanchored", codes)
        self.assertIn("segmentedHistory", report["sections"]["security"]["data"])

    def test_owner_ui_exposes_rotation_segments_and_receipt_warning(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        for marker in (
            "securityHistorySummary", "securityRotationPreviewBtn", "securityRotationExecuteBtn",
            "securityAnchorExportBtn", "securityAnchorReceiptBtn", "securityRetentionPolicyDetails",
            "securityPolicySaveBtn", "saveSecurityRetentionPolicy", "external publication remains owner-attested",
        ):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()
