from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterator

from .errors import ForgeTraceError
from .locks import InterProcessRLock
from .security_events import SecurityEventError, SecurityEventLedger
from .utils import utc_now

PROJECT_BOARDS_SCHEMA_VERSION = 1
PROJECT_BOARDS_FORMAT = "forgetrace-project-boards"
MAX_BOARDS_PER_REPOSITORY = 50
MAX_COLUMNS_PER_BOARD = 30
MAX_CARDS_PER_BOARD = 5000
MAX_FIELDS_PER_BOARD = 50
MAX_SAVED_VIEWS_PER_BOARD = 100
MAX_DEPENDENCIES_PER_REPOSITORY = 10000
MAX_NAME_CHARS = 160
MAX_DESCRIPTION_CHARS = 4000
MAX_FIELD_VALUE_CHARS = 4000
MAX_FILTER_CHARS = 8000
BOARD_VIEWS = {"kanban", "table", "roadmap"}
FIELD_TYPES = {"text", "number", "date", "single_select", "boolean"}
DEPENDENCY_KINDS = {"blocks", "relates_to"}


class ProjectBoardService:
    """Application-data-only project boards over project coordination records.

    This service never mutates repository content or Git metadata. Cards point to
    existing issue/discussion IDs and carry board-local rank and custom fields.
    """

    def __init__(self, *, registry, project_coordination, collaboration, security_events: SecurityEventLedger | None = None) -> None:
        self.registry = registry
        self.project = project_coordination
        self.collaboration = collaboration
        self.security_events = security_events
        self.data_dir = registry.data_dir / "project-boards"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "project-boards.sqlite3"
        self.lock_path = self.data_dir / "project-boards.lock"
        self.lock = InterProcessRLock(self.lock_path, timeout=30.0)
        self._migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS board_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS boards(
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    default_view TEXT NOT NULL DEFAULT 'kanban' CHECK(default_view IN ('kanban','table','roadmap')),
                    contributor_view INTEGER NOT NULL DEFAULT 0 CHECK(contributor_view IN (0,1)),
                    contributor_move INTEGER NOT NULL DEFAULT 0 CHECK(contributor_move IN (0,1)),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_boards_repository ON boards(repository_id, deleted_at, updated_at DESC);
                CREATE TABLE IF NOT EXISTS board_columns(
                    id TEXT PRIMARY KEY,
                    board_id TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    color TEXT NOT NULL DEFAULT '#7a8799',
                    rank REAL NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(board_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_board_columns_rank ON board_columns(board_id, rank, id);
                CREATE TABLE IF NOT EXISTS board_cards(
                    id TEXT PRIMARY KEY,
                    board_id TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
                    repository_id TEXT NOT NULL,
                    topic_id TEXT NOT NULL,
                    column_id TEXT NOT NULL REFERENCES board_columns(id) ON DELETE RESTRICT,
                    rank REAL NOT NULL,
                    start_at TEXT NOT NULL DEFAULT '',
                    target_at TEXT NOT NULL DEFAULT '',
                    estimate REAL,
                    priority TEXT NOT NULL DEFAULT '',
                    owner TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(board_id, topic_id)
                );
                CREATE INDEX IF NOT EXISTS idx_board_cards_rank ON board_cards(board_id, column_id, rank, id);
                CREATE TABLE IF NOT EXISTS board_fields(
                    id TEXT PRIMARY KEY,
                    board_id TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    field_type TEXT NOT NULL CHECK(field_type IN ('text','number','date','single_select','boolean')),
                    options_json TEXT NOT NULL DEFAULT '[]',
                    rank REAL NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(board_id, name)
                );
                CREATE TABLE IF NOT EXISTS board_field_values(
                    card_id TEXT NOT NULL REFERENCES board_cards(id) ON DELETE CASCADE,
                    field_id TEXT NOT NULL REFERENCES board_fields(id) ON DELETE CASCADE,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(card_id, field_id)
                );
                CREATE TABLE IF NOT EXISTS board_saved_views(
                    id TEXT PRIMARY KEY,
                    board_id TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    view_type TEXT NOT NULL CHECK(view_type IN ('kanban','table','roadmap')),
                    filter_json TEXT NOT NULL DEFAULT '{}',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(board_id, name)
                );
                CREATE TABLE IF NOT EXISTS board_dependencies(
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    source_topic_id TEXT NOT NULL,
                    target_topic_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('blocks','relates_to')),
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    UNIQUE(repository_id, source_topic_id, target_topic_id, kind),
                    CHECK(source_topic_id != target_topic_id)
                );
                CREATE TABLE IF NOT EXISTS board_activity(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    board_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    actor_name TEXT NOT NULL,
                    subject_id TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_board_activity ON board_activity(board_id, id DESC);
                """
            )
            row = connection.execute("SELECT value FROM board_meta WHERE key='schema_version'").fetchone()
            current = int(row["value"]) if row else 0
            if current > PROJECT_BOARDS_SCHEMA_VERSION:
                raise ForgeTraceError("Project board data uses a newer schema.", HTTPStatus.CONFLICT, "board_schema_newer")
            now = utc_now()
            connection.execute("INSERT INTO board_meta(key,value,updated_at) VALUES('schema_version',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (str(PROJECT_BOARDS_SCHEMA_VERSION), now))
            connection.execute("INSERT INTO board_meta(key,value,updated_at) VALUES('format',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (PROJECT_BOARDS_FORMAT, now))

    @staticmethod
    def _clean(value: Any, label: str, maximum: int, required: bool = False) -> str:
        result = str(value or "").strip()
        if required and not result:
            raise ForgeTraceError(f"{label} is required.", code=f"{label.lower().replace(' ', '_')}_required")
        if len(result) > maximum:
            raise ForgeTraceError(f"{label} may not exceed {maximum} characters.", code="value_too_long")
        return result

    @staticmethod
    def _expected(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ForgeTraceError("expectedVersion is required.", code="expected_version_required") from exc

    def _repository(self, repository_id: str) -> dict[str, Any]:
        return self.registry.get_repository(repository_id)

    def _audit(self, *, action: str, repository_id: str, subject_id: str = "", actor: str = "", surface: str = "owner", details: dict[str, Any] | None = None, required: bool = False) -> None:
        if self.security_events is None:
            return
        try:
            if required:
                self.security_events.assert_writable()
            self.security_events.append(category="project_boards", action=action, outcome="success", surface=surface, repository_id=repository_id, subject_id=subject_id, actor=actor[:120], details=details or {})
        except SecurityEventError as exc:
            if required:
                raise ForgeTraceError("The security event ledger is unavailable. The board action was blocked.", HTTPStatus.SERVICE_UNAVAILABLE, "security_event_ledger_unavailable") from exc

    def _activity(self, connection: sqlite3.Connection, board_id: str, repository_id: str, action: str, actor_role: str, actor_name: str, subject_id: str = "", details: dict[str, Any] | None = None) -> None:
        connection.execute("INSERT INTO board_activity(board_id,repository_id,action,actor_role,actor_name,subject_id,details_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (board_id, repository_id, action, actor_role, actor_name[:120], subject_id, json.dumps(details or {}, sort_keys=True, separators=(",", ":")), utc_now()))

    def _board_row(self, connection: sqlite3.Connection, repository_id: str, board_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM boards WHERE id=? AND repository_id=? AND deleted_at=''", (board_id, repository_id)).fetchone()
        if not row:
            raise ForgeTraceError("Board not found.", HTTPStatus.NOT_FOUND, "board_not_found")
        return row

    def _column_row(self, connection: sqlite3.Connection, board_id: str, column_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM board_columns WHERE id=? AND board_id=?", (column_id, board_id)).fetchone()
        if not row:
            raise ForgeTraceError("Board column not found.", HTTPStatus.NOT_FOUND, "board_column_not_found")
        return row

    def _topic(self, repository_id: str, topic_id: str) -> dict[str, Any]:
        return self.project.get_topic(repository_id, topic_id, comment_limit=1, comment_offset=0)

    @staticmethod
    def _public_board(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "repositoryId": row["repository_id"], "name": row["name"], "description": row["description"], "defaultView": row["default_view"], "contributorView": bool(row["contributor_view"]), "contributorMove": bool(row["contributor_move"]), "version": int(row["version"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"]}

    @staticmethod
    def _public_column(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "color": row["color"], "rank": float(row["rank"]), "version": int(row["version"])}

    @staticmethod
    def _public_field(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "fieldType": row["field_type"], "options": json.loads(row["options_json"] or "[]"), "rank": float(row["rank"]), "version": int(row["version"])}

    def create_board(self, repository_id: str, *, name: Any, description: Any = "", default_view: Any = "kanban", contributor_view: Any = False, contributor_move: Any = False, actor: str = "Owner", request_id: str = "") -> dict[str, Any]:
        self._repository(repository_id)
        clean_name = self._clean(name, "Board name", MAX_NAME_CHARS, True)
        clean_description = self._clean(description, "Board description", MAX_DESCRIPTION_CHARS)
        view = str(default_view or "kanban").strip().lower()
        if view not in BOARD_VIEWS:
            raise ForgeTraceError("Board view is invalid.", code="invalid_board_view")
        self._audit(action="board_create_authorized", repository_id=repository_id, actor=actor, required=True, details={"requestId": request_id})
        now = utc_now(); board_id = "brd_" + uuid.uuid4().hex[:20]
        with self.lock, self.connect() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM boards WHERE repository_id=? AND deleted_at=''", (repository_id,)).fetchone()[0])
            if count >= MAX_BOARDS_PER_REPOSITORY:
                raise ForgeTraceError("This repository has reached the board limit.", HTTPStatus.CONFLICT, "board_limit")
            connection.execute("INSERT INTO boards(id,repository_id,name,description,default_view,contributor_view,contributor_move,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (board_id, repository_id, clean_name, clean_description, view, int(bool(contributor_view)), int(bool(contributor_move and contributor_view)), now, now))
            for index, title in enumerate(("Backlog", "In progress", "Done")):
                connection.execute("INSERT INTO board_columns(id,board_id,name,color,rank,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", ("col_" + uuid.uuid4().hex[:20], board_id, title, "#7a8799", float((index + 1) * 1000), now, now))
            self._activity(connection, board_id, repository_id, "board_created", "owner", actor, board_id)
            row = self._board_row(connection, repository_id, board_id)
        self._audit(action="board_created", repository_id=repository_id, subject_id=board_id, actor=actor)
        return self._public_board(row)

    def list_boards(self, repository_id: str, *, contributor: bool = False) -> dict[str, Any]:
        self._repository(repository_id)
        where = "repository_id=? AND deleted_at=''" + (" AND contributor_view=1" if contributor else "")
        with self.connect() as connection:
            rows = connection.execute(f"SELECT * FROM boards WHERE {where} ORDER BY updated_at DESC,name", (repository_id,)).fetchall()
        return {"boards": [self._public_board(row) for row in rows], "schemaVersion": PROJECT_BOARDS_SCHEMA_VERSION, "authority": {"repositoryMutation": False, "gitMutation": False}}

    def update_board(self, repository_id: str, board_id: str, *, expected_version: Any, actor: str = "Owner", request_id: str = "", **changes: Any) -> dict[str, Any]:
        expected = self._expected(expected_version)
        self._audit(action="board_update_authorized", repository_id=repository_id, subject_id=board_id, actor=actor, required=True, details={"requestId": request_id})
        with self.lock, self.connect() as connection:
            row = self._board_row(connection, repository_id, board_id)
            if int(row["version"]) != expected:
                raise ForgeTraceError("Board changed since it was loaded.", HTTPStatus.CONFLICT, "board_version_changed")
            name = self._clean(changes.get("name", row["name"]), "Board name", MAX_NAME_CHARS, True)
            description = self._clean(changes.get("description", row["description"]), "Board description", MAX_DESCRIPTION_CHARS)
            view = str(changes.get("default_view", row["default_view"]) or "kanban").lower()
            if view not in BOARD_VIEWS:
                raise ForgeTraceError("Board view is invalid.", code="invalid_board_view")
            cview = bool(changes.get("contributor_view", bool(row["contributor_view"])))
            cmove = bool(changes.get("contributor_move", bool(row["contributor_move"]))) and cview
            now = utc_now()
            connection.execute("UPDATE boards SET name=?,description=?,default_view=?,contributor_view=?,contributor_move=?,version=version+1,updated_at=? WHERE id=?", (name, description, view, int(cview), int(cmove), now, board_id))
            self._activity(connection, board_id, repository_id, "board_updated", "owner", actor, board_id, {"contributorView": cview, "contributorMove": cmove})
            updated = self._board_row(connection, repository_id, board_id)
        return self._public_board(updated)

    def delete_board(self, repository_id: str, board_id: str, *, expected_version: Any, actor: str = "Owner", request_id: str = "") -> dict[str, Any]:
        expected = self._expected(expected_version)
        self._audit(action="board_delete_authorized", repository_id=repository_id, subject_id=board_id, actor=actor, required=True, details={"requestId": request_id})
        with self.lock, self.connect() as connection:
            row = self._board_row(connection, repository_id, board_id)
            if int(row["version"]) != expected:
                raise ForgeTraceError("Board changed since it was loaded.", HTTPStatus.CONFLICT, "board_version_changed")
            now = utc_now(); connection.execute("UPDATE boards SET deleted_at=?,version=version+1,updated_at=? WHERE id=?", (now, now, board_id))
            self._activity(connection, board_id, repository_id, "board_deleted", "owner", actor, board_id)
        return {"deleted": True, "boardId": board_id}

    def create_column(self, repository_id: str, board_id: str, *, name: Any, color: Any = "#7a8799", actor: str = "Owner") -> dict[str, Any]:
        clean_name = self._clean(name, "Column name", MAX_NAME_CHARS, True); clean_color = str(color or "#7a8799")
        if len(clean_color) != 7 or not clean_color.startswith("#"):
            raise ForgeTraceError("Column color must be a six-digit hex color.", code="invalid_column_color")
        with self.lock, self.connect() as connection:
            self._board_row(connection, repository_id, board_id)
            count = int(connection.execute("SELECT COUNT(*) FROM board_columns WHERE board_id=?", (board_id,)).fetchone()[0])
            if count >= MAX_COLUMNS_PER_BOARD: raise ForgeTraceError("This board has reached the column limit.", HTTPStatus.CONFLICT, "board_column_limit")
            rank = float(connection.execute("SELECT COALESCE(MAX(rank),0)+1000 FROM board_columns WHERE board_id=?", (board_id,)).fetchone()[0])
            now=utc_now(); column_id="col_"+uuid.uuid4().hex[:20]
            connection.execute("INSERT INTO board_columns(id,board_id,name,color,rank,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (column_id,board_id,clean_name,clean_color,rank,now,now))
            connection.execute("UPDATE boards SET version=version+1,updated_at=? WHERE id=?", (now,board_id)); self._activity(connection,board_id,repository_id,"column_created","owner",actor,column_id)
            row=self._column_row(connection,board_id,column_id)
        return self._public_column(row)

    def add_card(self, repository_id: str, board_id: str, *, topic_id: Any, column_id: Any = "", actor: str = "Owner") -> dict[str, Any]:
        topic = self._topic(repository_id, str(topic_id or ""))
        with self.lock, self.connect() as connection:
            self._board_row(connection, repository_id, board_id)
            count=int(connection.execute("SELECT COUNT(*) FROM board_cards WHERE board_id=?",(board_id,)).fetchone()[0])
            if count>=MAX_CARDS_PER_BOARD: raise ForgeTraceError("This board has reached the card limit.",HTTPStatus.CONFLICT,"board_card_limit")
            cid=str(column_id or "").strip()
            if cid: self._column_row(connection,board_id,cid)
            else:
                first=connection.execute("SELECT * FROM board_columns WHERE board_id=? ORDER BY rank,id LIMIT 1",(board_id,)).fetchone()
                if not first: raise ForgeTraceError("The board has no columns.",HTTPStatus.CONFLICT,"board_has_no_columns")
                cid=first["id"]
            rank=float(connection.execute("SELECT COALESCE(MAX(rank),0)+1000 FROM board_cards WHERE board_id=? AND column_id=?",(board_id,cid)).fetchone()[0])
            now=utc_now(); card_id="crd_"+uuid.uuid4().hex[:20]
            try: connection.execute("INSERT INTO board_cards(id,board_id,repository_id,topic_id,column_id,rank,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(card_id,board_id,repository_id,topic["id"],cid,rank,now,now))
            except sqlite3.IntegrityError as exc: raise ForgeTraceError("That project item is already on this board.",HTTPStatus.CONFLICT,"board_card_exists") from exc
            connection.execute("UPDATE boards SET version=version+1,updated_at=? WHERE id=?",(now,board_id));self._activity(connection,board_id,repository_id,"card_added","owner",actor,card_id,{"topicId":topic["id"]})
        return self.get_board(repository_id,board_id)

    def _move_card(self, repository_id: str, board_id: str, card_id: str, *, column_id: Any, before_card_id: Any = "", expected_version: Any, actor_role: str, actor_name: str) -> dict[str, Any]:
        expected=self._expected(expected_version)
        with self.lock,self.connect() as connection:
            board=self._board_row(connection,repository_id,board_id)
            if actor_role=="contributor" and not bool(board["contributor_move"]): raise ForgeTraceError("This board does not allow contributor card movement.",HTTPStatus.FORBIDDEN,"board_contributor_move_denied")
            card=connection.execute("SELECT * FROM board_cards WHERE id=? AND board_id=?",(card_id,board_id)).fetchone()
            if not card: raise ForgeTraceError("Board card not found.",HTTPStatus.NOT_FOUND,"board_card_not_found")
            if int(card["version"])!=expected: raise ForgeTraceError("Board card changed since it was loaded.",HTTPStatus.CONFLICT,"board_card_version_changed")
            target=str(column_id or "");self._column_row(connection,board_id,target)
            before=str(before_card_id or "").strip()
            if before:
                before_row=connection.execute("SELECT * FROM board_cards WHERE id=? AND board_id=? AND column_id=?",(before,board_id,target)).fetchone()
                if not before_row: raise ForgeTraceError("The reference card is not in the target column.",code="board_before_card_invalid")
                previous=connection.execute("SELECT rank FROM board_cards WHERE board_id=? AND column_id=? AND rank<? AND id!=? ORDER BY rank DESC LIMIT 1",(board_id,target,float(before_row["rank"]),card_id)).fetchone()
                rank=(float(previous["rank"])+float(before_row["rank"]))/2 if previous else float(before_row["rank"])-1000
            else: rank=float(connection.execute("SELECT COALESCE(MAX(rank),0)+1000 FROM board_cards WHERE board_id=? AND column_id=? AND id!=?",(board_id,target,card_id)).fetchone()[0])
            now=utc_now();connection.execute("UPDATE board_cards SET column_id=?,rank=?,version=version+1,updated_at=? WHERE id=?",(target,rank,now,card_id));connection.execute("UPDATE boards SET version=version+1,updated_at=? WHERE id=?",(now,board_id));self._activity(connection,board_id,repository_id,"card_moved",actor_role,actor_name,card_id,{"columnId":target})
        return self.get_board(repository_id,board_id,contributor=actor_role=="contributor")

    def move_card(self, repository_id: str, board_id: str, card_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._move_card(repository_id,board_id,card_id,column_id=kwargs.get("column_id"),before_card_id=kwargs.get("before_card_id"),expected_version=kwargs.get("expected_version"),actor_role="owner",actor_name=str(kwargs.get("actor") or "Owner"))

    def contributor_context(self, token: str) -> dict[str,str]:
        return self.collaboration.project_participant(token)

    def list_boards_for_token(self, token: str) -> dict[str, Any]:
        context=self.contributor_context(token);return self.list_boards(context["repositoryId"],contributor=True)

    def get_board_for_token(self, token: str, board_id: str) -> dict[str, Any]:
        context=self.contributor_context(token);return self.get_board(context["repositoryId"],board_id,contributor=True)

    def move_card_for_token(self, token: str, board_id: str, card_id: str, *, column_id: Any, before_card_id: Any, expected_version: Any, actor_name: Any) -> dict[str, Any]:
        context=self.contributor_context(token);name=self._clean(actor_name,"Contributor name",120,True)
        return self._move_card(context["repositoryId"],board_id,card_id,column_id=column_id,before_card_id=before_card_id,expected_version=expected_version,actor_role="contributor",actor_name=name)

    def create_field(self, repository_id: str, board_id: str, *, name: Any, field_type: Any, options: Any = None, actor: str = "Owner") -> dict[str, Any]:
        clean_name=self._clean(name,"Field name",MAX_NAME_CHARS,True);kind=str(field_type or "").lower()
        if kind not in FIELD_TYPES: raise ForgeTraceError("Custom field type is invalid.",code="invalid_board_field_type")
        clean_options=[]
        if kind=="single_select":
            if not isinstance(options,list) or not 1<=len(options)<=100: raise ForgeTraceError("Single-select fields require 1 to 100 options.",code="invalid_board_field_options")
            clean_options=[self._clean(item,"Field option",120,True) for item in options]
        with self.lock,self.connect() as connection:
            self._board_row(connection,repository_id,board_id);count=int(connection.execute("SELECT COUNT(*) FROM board_fields WHERE board_id=?",(board_id,)).fetchone()[0])
            if count>=MAX_FIELDS_PER_BOARD: raise ForgeTraceError("This board has reached the custom-field limit.",HTTPStatus.CONFLICT,"board_field_limit")
            rank=float(connection.execute("SELECT COALESCE(MAX(rank),0)+1000 FROM board_fields WHERE board_id=?",(board_id,)).fetchone()[0]);now=utc_now();field_id="fld_"+uuid.uuid4().hex[:20]
            connection.execute("INSERT INTO board_fields(id,board_id,name,field_type,options_json,rank,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(field_id,board_id,clean_name,kind,json.dumps(clean_options),rank,now,now));self._activity(connection,board_id,repository_id,"field_created","owner",actor,field_id)
            row=connection.execute("SELECT * FROM board_fields WHERE id=?",(field_id,)).fetchone()
        return self._public_field(row)

    def set_card_fields(self, repository_id: str, board_id: str, card_id: str, *, values: Any, expected_version: Any, actor: str = "Owner") -> dict[str, Any]:
        expected=self._expected(expected_version)
        if not isinstance(values,dict): raise ForgeTraceError("values must be an object.",code="invalid_board_field_values")
        with self.lock,self.connect() as connection:
            self._board_row(connection,repository_id,board_id);card=connection.execute("SELECT * FROM board_cards WHERE id=? AND board_id=?",(card_id,board_id)).fetchone()
            if not card: raise ForgeTraceError("Board card not found.",HTTPStatus.NOT_FOUND,"board_card_not_found")
            if int(card["version"])!=expected: raise ForgeTraceError("Board card changed since it was loaded.",HTTPStatus.CONFLICT,"board_card_version_changed")
            fields={row["id"]:row for row in connection.execute("SELECT * FROM board_fields WHERE board_id=?",(board_id,))};now=utc_now()
            for field_id,value in values.items():
                field=fields.get(str(field_id));
                if not field: raise ForgeTraceError("A custom field does not belong to this board.",code="board_field_scope_mismatch")
                serialized=json.dumps(value,sort_keys=True,separators=(",",":"))
                if len(serialized)>MAX_FIELD_VALUE_CHARS: raise ForgeTraceError("A custom field value is too large.",code="board_field_value_too_large")
                connection.execute("INSERT INTO board_field_values(card_id,field_id,value_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(card_id,field_id) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",(card_id,field_id,serialized,now))
            connection.execute("UPDATE board_cards SET version=version+1,updated_at=? WHERE id=?",(now,card_id));self._activity(connection,board_id,repository_id,"card_fields_updated","owner",actor,card_id)
        return self.get_board(repository_id,board_id)

    def create_saved_view(self, repository_id: str, board_id: str, *, name: Any, view_type: Any, filters: Any, actor: str="Owner") -> dict[str,Any]:
        clean_name=self._clean(name,"Saved view name",MAX_NAME_CHARS,True);kind=str(view_type or "").lower()
        if kind not in BOARD_VIEWS: raise ForgeTraceError("Saved view type is invalid.",code="invalid_board_view")
        if not isinstance(filters,dict): raise ForgeTraceError("Saved view filters must be an object.",code="invalid_saved_view_filter")
        encoded=json.dumps(filters,sort_keys=True,separators=(",",":"))
        if len(encoded)>MAX_FILTER_CHARS: raise ForgeTraceError("Saved view filter is too large.",code="saved_view_filter_too_large")
        with self.lock,self.connect() as connection:
            self._board_row(connection,repository_id,board_id);count=int(connection.execute("SELECT COUNT(*) FROM board_saved_views WHERE board_id=?",(board_id,)).fetchone()[0])
            if count>=MAX_SAVED_VIEWS_PER_BOARD: raise ForgeTraceError("This board has reached the saved-view limit.",HTTPStatus.CONFLICT,"saved_view_limit")
            now=utc_now();view_id="viw_"+uuid.uuid4().hex[:20];connection.execute("INSERT INTO board_saved_views(id,board_id,name,view_type,filter_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(view_id,board_id,clean_name,kind,encoded,now,now));self._activity(connection,board_id,repository_id,"saved_view_created","owner",actor,view_id)
        return self.get_board(repository_id,board_id)

    def add_dependency(self, repository_id: str, *, source_topic_id: Any, target_topic_id: Any, kind: Any="blocks", actor: str="Owner") -> dict[str,Any]:
        source=self._topic(repository_id,str(source_topic_id or ""));target=self._topic(repository_id,str(target_topic_id or ""));clean_kind=str(kind or "blocks")
        if clean_kind not in DEPENDENCY_KINDS: raise ForgeTraceError("Dependency kind is invalid.",code="invalid_dependency_kind")
        if source["id"]==target["id"]: raise ForgeTraceError("An item cannot depend on itself.",code="dependency_self_reference")
        with self.lock,self.connect() as connection:
            count=int(connection.execute("SELECT COUNT(*) FROM board_dependencies WHERE repository_id=?",(repository_id,)).fetchone()[0])
            if count>=MAX_DEPENDENCIES_PER_REPOSITORY: raise ForgeTraceError("This repository has reached the dependency limit.",HTTPStatus.CONFLICT,"dependency_limit")
            dep_id="dep_"+uuid.uuid4().hex[:20]
            try: connection.execute("INSERT INTO board_dependencies(id,repository_id,source_topic_id,target_topic_id,kind,created_at,created_by) VALUES(?,?,?,?,?,?,?)",(dep_id,repository_id,source["id"],target["id"],clean_kind,utc_now(),actor))
            except sqlite3.IntegrityError as exc: raise ForgeTraceError("That dependency already exists.",HTTPStatus.CONFLICT,"dependency_exists") from exc
        return {"id":dep_id,"sourceTopicId":source["id"],"targetTopicId":target["id"],"kind":clean_kind}

    def get_board(self, repository_id: str, board_id: str, *, contributor: bool=False) -> dict[str,Any]:
        self._repository(repository_id)
        with self.connect() as connection:
            board=self._board_row(connection,repository_id,board_id)
            if contributor and not bool(board["contributor_view"]): raise ForgeTraceError("This board is not shared with contributors.",HTTPStatus.FORBIDDEN,"board_contributor_view_denied")
            columns=[self._public_column(row) for row in connection.execute("SELECT * FROM board_columns WHERE board_id=? ORDER BY rank,id",(board_id,))]
            fields=[self._public_field(row) for row in connection.execute("SELECT * FROM board_fields WHERE board_id=? ORDER BY rank,id",(board_id,))]
            views=[{"id":row["id"],"name":row["name"],"viewType":row["view_type"],"filters":json.loads(row["filter_json"]),"version":int(row["version"])} for row in connection.execute("SELECT * FROM board_saved_views WHERE board_id=? ORDER BY name",(board_id,))]
            cards=[]
            for row in connection.execute("SELECT * FROM board_cards WHERE board_id=? ORDER BY column_id,rank,id",(board_id,)):
                try: topic=self._topic(repository_id,row["topic_id"])
                except ForgeTraceError: topic={"id":row["topic_id"],"title":"Unavailable project item","kind":"unknown","number":0,"state":"unknown","milestone":None,"dueAt":"","assignee":""}
                values={value["field_id"]:json.loads(value["value_json"]) for value in connection.execute("SELECT * FROM board_field_values WHERE card_id=?",(row["id"],))}
                cards.append({"id":row["id"],"topicId":row["topic_id"],"columnId":row["column_id"],"rank":float(row["rank"]),"startAt":row["start_at"],"targetAt":row["target_at"],"estimate":row["estimate"],"priority":row["priority"],"owner":row["owner"],"version":int(row["version"]),"topic":topic,"fieldValues":values})
            dependencies=[{"id":row["id"],"sourceTopicId":row["source_topic_id"],"targetTopicId":row["target_topic_id"],"kind":row["kind"]} for row in connection.execute("SELECT * FROM board_dependencies WHERE repository_id=? ORDER BY created_at",(repository_id,))]
            activity=[{"id":int(row["id"]),"action":row["action"],"actorRole":row["actor_role"],"actorName":row["actor_name"],"subjectId":row["subject_id"],"details":json.loads(row["details_json"]),"createdAt":row["created_at"]} for row in connection.execute("SELECT * FROM board_activity WHERE board_id=? ORDER BY id DESC LIMIT 100",(board_id,))]
        return {"board":self._public_board(board),"columns":columns,"cards":cards,"fields":fields,"savedViews":views,"dependencies":dependencies,"activity":activity,"authority":{"repositoryMutation":False,"gitMutation":False,"contributorCanView":bool(board["contributor_view"]),"contributorCanMove":bool(board["contributor_move"]),"actor":"contributor" if contributor else "owner"}}

    def health_status(self, repository_id: str="") -> dict[str,Any]:
        result={"schemaVersion":PROJECT_BOARDS_SCHEMA_VERSION,"databasePath":str(self.db_path),"integrity":"unknown","storageBytes":0,"repositories":[]}
        try:
            result["storageBytes"]=self.db_path.stat().st_size if self.db_path.exists() else 0
            with self.connect() as connection:
                result["integrity"]=str(connection.execute("PRAGMA integrity_check").fetchone()[0]);where=" WHERE repository_id=?" if repository_id else "";params=(repository_id,) if repository_id else ()
                for row in connection.execute(f"SELECT repository_id,COUNT(*) AS boards FROM boards{where} GROUP BY repository_id",params):
                    cards=int(connection.execute("SELECT COUNT(*) FROM board_cards WHERE repository_id=?",(row["repository_id"],)).fetchone()[0]);result["repositories"].append({"repositoryId":row["repository_id"],"boardCount":int(row["boards"]),"cardCount":cards})
        except (OSError,sqlite3.Error) as exc: result["integrity"]="error";result["error"]=str(exc)
        return result
