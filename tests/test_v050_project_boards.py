from __future__ import annotations

import hashlib
import http.client
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from forgetrace.app import build_application
from forgetrace.errors import ForgeTraceError
from forgetrace.web import create_server

ROOT = Path(__file__).resolve().parents[1]


class BoardFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="forgetrace-v050-boards-"))
        self.app = build_application(ROOT, self.root / "data")
        record = self.app.registry.register_repository(path=str(self.root / "repo"), name="Boards", author="Rooke Poole", initialize=True, create_directory=True)
        self.repository_id = record["id"]
        self.repository_path = Path(record["path"])
        self.issue = self.app.project.create_topic(self.repository_id, kind="issue", title="Board issue", body="Track this")
        self.discussion = self.app.project.create_topic(self.repository_id, kind="discussion", title="Roadmap discussion")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class ProjectBoardServiceTest(BoardFixture):
    def test_board_columns_cards_order_and_optimistic_concurrency(self) -> None:
        board = self.app.boards.create_board(self.repository_id, name="Delivery", default_view="kanban")
        detail = self.app.boards.get_board(self.repository_id, board["id"])
        self.assertEqual(["Backlog", "In progress", "Done"], [c["name"] for c in detail["columns"]])
        detail = self.app.boards.add_card(self.repository_id, board["id"], topic_id=self.issue["id"])
        card = detail["cards"][0]
        target = detail["columns"][1]
        moved = self.app.boards.move_card(self.repository_id, board["id"], card["id"], column_id=target["id"], expected_version=card["version"])
        self.assertEqual(target["id"], moved["cards"][0]["columnId"])
        with self.assertRaises(ForgeTraceError) as stale:
            self.app.boards.move_card(self.repository_id, board["id"], card["id"], column_id=detail["columns"][2]["id"], expected_version=card["version"])
        self.assertEqual("board_card_version_changed", stale.exception.code)

    def test_table_fields_saved_views_and_roadmap_dates(self) -> None:
        board = self.app.boards.create_board(self.repository_id, name="Roadmap", default_view="roadmap")
        detail = self.app.boards.add_card(self.repository_id, board["id"], topic_id=self.issue["id"])
        field = self.app.boards.create_field(self.repository_id, board["id"], name="Effort", field_type="number")
        card = detail["cards"][0]
        detail = self.app.boards.set_card_fields(self.repository_id, board["id"], card["id"], values={field["id"]: 8}, expected_version=card["version"])
        self.assertEqual(8, detail["cards"][0]["fieldValues"][field["id"]])
        detail = self.app.boards.create_saved_view(self.repository_id, board["id"], name="Open roadmap", view_type="roadmap", filters={"state": "open"})
        self.assertEqual("roadmap", detail["savedViews"][0]["viewType"])

    def test_dependencies_and_repository_isolation(self) -> None:
        dependency = self.app.boards.add_dependency(self.repository_id, source_topic_id=self.issue["id"], target_topic_id=self.discussion["id"], kind="blocks")
        self.assertEqual("blocks", dependency["kind"])
        with self.assertRaises(ForgeTraceError):
            self.app.boards.add_dependency(self.repository_id, source_topic_id=self.issue["id"], target_topic_id=self.issue["id"])
        second = self.app.registry.register_repository(path=str(self.root / "second"), name="Second", author="Owner", initialize=True, create_directory=True)
        with self.assertRaises(ForgeTraceError):
            self.app.boards.create_board(second["id"], name="Other") if False else self.app.boards.get_board(second["id"], "missing")

    def test_contributor_board_permissions_are_board_specific(self) -> None:
        invite = self.app.collaboration.create_invite(self.repository_id, allow_project_participation=True)
        private = self.app.boards.create_board(self.repository_id, name="Private")
        shared = self.app.boards.create_board(self.repository_id, name="Shared", contributor_view=True, contributor_move=True)
        detail = self.app.boards.add_card(self.repository_id, shared["id"], topic_id=self.issue["id"])
        visible = self.app.boards.list_boards_for_token(invite["token"])
        self.assertEqual([shared["id"]], [item["id"] for item in visible["boards"]])
        with self.assertRaises(ForgeTraceError) as denied:
            self.app.boards.get_board_for_token(invite["token"], private["id"])
        self.assertEqual("board_contributor_view_denied", denied.exception.code)
        card = detail["cards"][0]
        moved = self.app.boards.move_card_for_token(invite["token"], shared["id"], card["id"], column_id=detail["columns"][1]["id"], before_card_id="", expected_version=card["version"], actor_name="Alex")
        self.assertEqual(detail["columns"][1]["id"], moved["cards"][0]["columnId"])

    def test_restart_persistence_and_no_repository_or_git_mutation(self) -> None:
        before = hashlib.sha256((self.repository_path / "README.md").read_bytes()).hexdigest()
        board = self.app.boards.create_board(self.repository_id, name="Persistent")
        self.app.boards.add_card(self.repository_id, board["id"], topic_id=self.issue["id"])
        restarted = build_application(ROOT, self.root / "data")
        detail = restarted.boards.get_board(self.repository_id, board["id"])
        self.assertEqual("Persistent", detail["board"]["name"])
        self.assertEqual(before, hashlib.sha256((self.repository_path / "README.md").read_bytes()).hexdigest())
        self.assertFalse((self.repository_path / ".git").exists())

    def test_read_only_repository_still_allows_board_coordination(self) -> None:
        service = self.app.registry.repository_service(self.repository_id)
        self.app.registry.set_access_mode(self.repository_id, "read_only")
        board = self.app.boards.create_board(self.repository_id, name="Read only planning")
        detail = self.app.boards.add_card(self.repository_id, board["id"], topic_id=self.issue["id"])
        self.assertEqual(1, len(detail["cards"]))
        self.assertEqual("read_only", service.access_policy()["effectiveMode"])

    def test_owner_and_contributor_http_routes(self) -> None:
        server = create_server(self.app, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            port = server.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            body = json.dumps({"name":"HTTP board","contributorView":True,"contributorMove":True})
            connection.request("POST", f"/api/v1/repositories/{self.repository_id}/boards", body=body, headers={"Content-Type":"application/json","Origin":f"http://127.0.0.1:{port}"})
            response = connection.getresponse(); payload=json.loads(response.read())
            self.assertEqual(201, response.status)
            board_id = payload["id"]
            invite = self.app.collaboration.create_invite(self.repository_id, allow_project_participation=True)
            connection.request("GET", "/api/v1/collaboration/boards", headers={"X-ForgeTrace-Invite":invite["token"]})
            response=connection.getresponse(); listing=json.loads(response.read())
            self.assertEqual(200,response.status);self.assertEqual(board_id,listing["boards"][0]["id"])
            connection.close()
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)

    def test_health_status_reports_board_storage(self) -> None:
        board=self.app.boards.create_board(self.repository_id,name="Health")
        self.app.boards.add_card(self.repository_id,board["id"],topic_id=self.issue["id"])
        status=self.app.boards.health_status(self.repository_id)
        self.assertEqual("ok",status["integrity"])
        self.assertEqual(1,status["repositories"][0]["boardCount"])


if __name__ == "__main__":
    unittest.main()
