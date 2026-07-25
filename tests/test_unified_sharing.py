from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from forgetrace.app import build_application
from forgetrace.web import create_server


class UnifiedSharingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="forgetrace-unified-sharing-")
        root = Path(self.temp.name)
        project_root = Path(__file__).resolve().parents[1]
        self.app = build_application(project_root, root / "data")
        record = self.app.registry.register_repository(
            path=str(root / "repository"),
            name="Unified Sharing",
            author="Owner",
            initialize=True,
            create_directory=True,
        )
        self.repository_id = record["id"]
        self.owner_server = create_server(self.app, "127.0.0.1", 0, surface="owner")
        self.owner_thread = threading.Thread(target=self.owner_server.serve_forever, daemon=True)
        self.owner_thread.start()

    def tearDown(self) -> None:
        if self.app.gateway:
            self.app.gateway.stop()
        self.owner_server.shutdown()
        self.owner_server.server_close()
        self.owner_thread.join(timeout=2)
        self.temp.cleanup()

    @staticmethod
    def request(port: int, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        body = b""
        merged_headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            merged_headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(method, path, body=body, headers=merged_headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), raw
        connection.close()
        return result

    def owner_request(self, method: str, path: str, payload: dict | None = None):
        return self.request(int(self.owner_server.server_address[1]), method, path, payload)

    def test_ui_controlled_gateway_lifecycle_and_boundary(self) -> None:
        status, _headers, raw = self.owner_request("GET", "/api/v1/sharing")
        self.assertEqual(200, status)
        self.assertFalse(json.loads(raw)["enabled"])

        status, _headers, raw = self.owner_request("POST", "/api/v1/sharing/start", {"port": 0})
        self.assertEqual(200, status)
        sharing = json.loads(raw)
        self.assertTrue(sharing["enabled"])
        gateway_port = int(sharing["port"])
        self.assertGreater(gateway_port, 0)

        # The restricted surface stays restricted even when accessed from loopback.
        status, headers, raw = self.request(gateway_port, "GET", "/")
        self.assertEqual(200, status)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"Contribute to ForgeTrace", raw)

        status, _headers, raw = self.request(gateway_port, "GET", "/api/v1/repositories")
        self.assertEqual(403, status)
        self.assertEqual("remote_owner_api_blocked", json.loads(raw)["code"])

        status, _headers, invite_raw = self.owner_request(
            "POST",
            f"/api/v1/repositories/{self.repository_id}/collaboration/invites",
            {"label": "UI generated", "maxUses": 1},
        )
        self.assertEqual(201, status)
        token = json.loads(invite_raw)["token"]
        status, _headers, raw = self.request(
            gateway_port,
            "GET",
            "/api/v1/collaboration/invite",
            headers={"X-ForgeTrace-Invite": token},
        )
        self.assertEqual(200, status)
        self.assertEqual("Unified Sharing", json.loads(raw)["repository"]["name"])

        status, _headers, raw = self.owner_request("POST", "/api/v1/sharing/stop", {})
        self.assertEqual(200, status)
        self.assertFalse(json.loads(raw)["enabled"])
        with self.assertRaises(OSError):
            with socket.create_connection(("127.0.0.1", gateway_port), timeout=0.5):
                pass

    def test_port_change_requires_explicit_stop(self) -> None:
        first = self.app.gateway.start(port=0, host="127.0.0.1")
        self.assertTrue(first["enabled"])
        current_port = int(first["port"])
        same = self.app.gateway.start(port=current_port, host="127.0.0.1")
        self.assertEqual(current_port, same["port"])
        status, _headers, raw = self.owner_request(
            "POST", "/api/v1/sharing/start", {"port": current_port + 1}
        )
        self.assertEqual(409, status)
        self.assertEqual("sharing_already_enabled", json.loads(raw)["code"])


if __name__ == "__main__":
    unittest.main()
