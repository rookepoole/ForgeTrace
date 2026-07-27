from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import ForgeTraceError
from .locks import InterProcessRLock
from .security_events import SecurityEventError, SecurityEventLedger
from .utils import utc_now

PROJECT_COORDINATION_SCHEMA_VERSION = 1
PROJECT_COORDINATION_FORMAT = "forgetrace-project-coordination"
MAX_TITLE_CHARS = 240
MAX_BODY_CHARS = 32_000
MAX_COMMENT_CHARS = 8_000
MAX_AUTHOR_CHARS = 120
MAX_ASSIGNEE_CHARS = 120
MAX_LABEL_NAME_CHARS = 64
MAX_LABEL_DESCRIPTION_CHARS = 500
MAX_MILESTONE_DESCRIPTION_CHARS = 4_000
MAX_REFERENCES = 20
MAX_REFERENCE_VALUE_CHARS = 512
MAX_LABELS_PER_REPOSITORY = 100
MAX_MILESTONES_PER_REPOSITORY = 100
MAX_ISSUES_PER_REPOSITORY = 5_000
MAX_DISCUSSIONS_PER_REPOSITORY = 2_000
MAX_COMMENTS_PER_TOPIC = 1_000
MAX_TOTAL_COMMENTS_PER_REPOSITORY = 50_000
MAX_PAGE_SIZE = 100
SOFT_DELETE_RETENTION_DAYS = 180
TOPIC_KINDS = {"issue", "discussion"}
TOPIC_STATES = {"open", "closed"}
REFERENCE_KINDS = {"pull_request", "revision", "commit", "path", "issue", "discussion"}
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
ID_PATTERN = re.compile(r"^[a-z]+_[0-9a-f]{16,32}$")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def render_inert_markdown(value: str) -> str:
    """Render a deliberately small, inert Markdown subset.

    The source is HTML-escaped before any formatting. Links, images, raw HTML,
    scripts, SVG, forms, and event attributes are never emitted.
    """

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    output: list[str] = []
    paragraph: list[str] = []
    list_open = False
    code_open = False

    def inline(line: str) -> str:
        escaped = html.escape(line, quote=True)
        escaped = re.sub(r"(?i)(javascript|data|vbscript):", r"\1&#58;", escaped)
        escaped = re.sub(r"`([^`]{1,500})`", r"<code>\1</code>", escaped)
        escaped = re.sub(r"\*\*([^*]{1,1000})\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"(?<!\*)\*([^*]{1,1000})\*(?!\*)", r"<em>\1</em>", escaped)
        return escaped

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            output.append("<p>" + "<br>".join(inline(item) for item in paragraph) + "</p>")
            paragraph = []

    for raw in lines:
        if raw.strip().startswith("```"):
            flush_paragraph()
            if list_open:
                output.append("</ul>")
                list_open = False
            if code_open:
                output.append("</code></pre>")
                code_open = False
            else:
                output.append("<pre><code>")
                code_open = True
            continue
        if code_open:
            output.append(html.escape(raw, quote=True) + "\n")
            continue
        if not raw.strip():
            flush_paragraph()
            if list_open:
                output.append("</ul>")
                list_open = False
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", raw)
        if heading:
            flush_paragraph()
            if list_open:
                output.append("</ul>")
                list_open = False
            level = len(heading.group(1))
            output.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+)$", raw)
        if bullet:
            flush_paragraph()
            if not list_open:
                output.append("<ul>")
                list_open = True
            output.append("<li>" + inline(bullet.group(1)) + "</li>")
            continue
        if list_open:
            output.append("</ul>")
            list_open = False
        paragraph.append(raw)
    flush_paragraph()
    if list_open:
        output.append("</ul>")
    if code_open:
        output.append("</code></pre>")
    return "".join(output)


class ProjectCoordinationService:
    """Repository-scoped issues and discussions stored outside repository content.

    The service has no filesystem or Git mutation authority. Contributor access is
    resolved through an explicitly permissioned collaboration invitation.
    """

    def __init__(
        self,
        *,
        registry,
        collaboration,
        security_events: SecurityEventLedger | None = None,
    ) -> None:
        self.registry = registry
        self.collaboration = collaboration
        self.security_events = security_events
        self.data_dir = registry.data_dir / "project-coordination"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "project-coordination.sqlite3"
        self.lock_path = self.data_dir / "project-coordination.lock"
        self.lock = InterProcessRLock(self.lock_path, timeout=30.0)
        self._migrate()
        self.cleanup_retention()

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
                CREATE TABLE IF NOT EXISTS project_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_counters (
                    repository_id TEXT PRIMARY KEY,
                    next_issue_number INTEGER NOT NULL DEFAULT 1,
                    next_discussion_number INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS project_labels (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    color TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(repository_id, normalized_name)
                );
                CREATE INDEX IF NOT EXISTS idx_project_labels_repository
                    ON project_labels(repository_id, deleted_at, normalized_name);

                CREATE TABLE IF NOT EXISTS project_milestones (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    due_at TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed')),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL DEFAULT '',
                    deleted_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(repository_id, normalized_title)
                );
                CREATE INDEX IF NOT EXISTS idx_project_milestones_repository
                    ON project_milestones(repository_id, deleted_at, state, due_at);

                CREATE TABLE IF NOT EXISTS project_topics (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('issue','discussion')),
                    number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    references_json TEXT NOT NULL DEFAULT '[]',
                    author_role TEXT NOT NULL CHECK(author_role IN ('owner','contributor')),
                    author_name TEXT NOT NULL,
                    invite_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed')),
                    milestone_id TEXT REFERENCES project_milestones(id) ON DELETE SET NULL,
                    assignee TEXT NOT NULL DEFAULT '',
                    due_at TEXT NOT NULL DEFAULT '',
                    locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)),
                    pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)),
                    accepted_comment_id TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL DEFAULT '',
                    deleted_at TEXT NOT NULL DEFAULT '',
                    deleted_by TEXT NOT NULL DEFAULT '',
                    deletion_reason TEXT NOT NULL DEFAULT '',
                    UNIQUE(repository_id, kind, number)
                );
                CREATE INDEX IF NOT EXISTS idx_project_topics_repository
                    ON project_topics(repository_id, kind, deleted_at, state, pinned DESC, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_project_topics_milestone
                    ON project_topics(repository_id, milestone_id, deleted_at);

                CREATE TABLE IF NOT EXISTS project_topic_labels (
                    topic_id TEXT NOT NULL REFERENCES project_topics(id) ON DELETE CASCADE,
                    label_id TEXT NOT NULL REFERENCES project_labels(id) ON DELETE CASCADE,
                    PRIMARY KEY(topic_id, label_id)
                );

                CREATE TABLE IF NOT EXISTS project_comments (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    topic_id TEXT NOT NULL REFERENCES project_topics(id) ON DELETE CASCADE,
                    author_role TEXT NOT NULL CHECK(author_role IN ('owner','contributor')),
                    author_name TEXT NOT NULL,
                    invite_id TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL,
                    references_json TEXT NOT NULL DEFAULT '[]',
                    hidden INTEGER NOT NULL DEFAULT 0 CHECK(hidden IN (0,1)),
                    moderation_reason TEXT NOT NULL DEFAULT '',
                    moderated_by TEXT NOT NULL DEFAULT '',
                    moderated_at TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_project_comments_topic
                    ON project_comments(topic_id, deleted_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_project_comments_repository
                    ON project_comments(repository_id, deleted_at, created_at);
                """
            )
            row = connection.execute(
                "SELECT value FROM project_meta WHERE key = 'schema_version'"
            ).fetchone()
            current = int(row["value"]) if row else 0
            if current > PROJECT_COORDINATION_SCHEMA_VERSION:
                raise ForgeTraceError(
                    "Project coordination data uses a newer schema.",
                    HTTPStatus.CONFLICT,
                    "project_schema_newer",
                    {"current": current, "supported": PROJECT_COORDINATION_SCHEMA_VERSION},
                )
            now = utc_now()
            connection.execute(
                "INSERT INTO project_meta(key,value,updated_at) VALUES('schema_version',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (str(PROJECT_COORDINATION_SCHEMA_VERSION), now),
            )
            connection.execute(
                "INSERT INTO project_meta(key,value,updated_at) VALUES('format',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (PROJECT_COORDINATION_FORMAT, now),
            )

    def _audit(
        self,
        *,
        action: str,
        outcome: str,
        repository_id: str,
        subject_id: str = "",
        actor: str = "",
        surface: str = "owner",
        severity: str = "info",
        request_id: str = "",
        details: dict[str, Any] | None = None,
        required: bool = False,
    ) -> None:
        if self.security_events is None:
            return
        try:
            if required:
                self.security_events.assert_writable()
            self.security_events.append(
                category="project_coordination",
                action=action,
                outcome=outcome,
                severity=severity,
                surface=surface,
                repository_id=repository_id,
                request_id=str(request_id or "")[:120],
                actor=str(actor or "")[:MAX_AUTHOR_CHARS],
                subject_id=subject_id,
                details=details or {},
            )
        except SecurityEventError as exc:
            if required:
                raise ForgeTraceError(
                    "The security event ledger is unavailable or failed integrity verification. The protected project action was blocked.",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "security_event_ledger_unavailable",
                    {"reason": str(exc)},
                ) from exc

    @staticmethod
    def _clean_text(value: Any, *, label: str, maximum: int, required: bool = False) -> str:
        text = str(value or "").replace("\x00", "").strip()
        if required and not text:
            raise ForgeTraceError(f"{label} is required.", code=f"{label.lower().replace(' ', '_')}_required")
        if len(text) > maximum:
            raise ForgeTraceError(
                f"{label} must be {maximum:,} characters or fewer.",
                code=f"{label.lower().replace(' ', '_')}_too_long",
                details={"maximum": maximum},
            )
        return text

    @staticmethod
    def _page(limit: Any, offset: Any) -> tuple[int, int]:
        try:
            bounded_limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        except (TypeError, ValueError):
            bounded_limit = 50
        try:
            bounded_offset = max(0, int(offset))
        except (TypeError, ValueError):
            bounded_offset = 0
        return bounded_limit, bounded_offset

    @staticmethod
    def _normalize_due_at(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = _parse_utc(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError) as exc:
            raise ForgeTraceError("Due date must be an ISO-8601 timestamp.", code="invalid_due_at") from exc
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _repository(self, repository_id: str):
        return self.registry.repository_service(repository_id)

    def contributor_actor(self, token: str, actor_name: Any) -> dict[str, str]:
        context = self.collaboration.project_participant(token)
        context["actorName"] = self._clean_text(
            actor_name, label="Contributor name", maximum=MAX_AUTHOR_CHARS, required=True
        )
        return context

    def _clean_references(self, repository_id: str, supplied: Any) -> list[dict[str, Any]]:
        if supplied in (None, ""):
            return []
        if not isinstance(supplied, list):
            raise ForgeTraceError("references must be a list.", code="invalid_project_references")
        if len(supplied) > MAX_REFERENCES:
            raise ForgeTraceError(
                f"At most {MAX_REFERENCES} references are allowed.", code="project_reference_limit"
            )
        repository = self._repository(repository_id)
        normalized: list[dict[str, Any]] = []
        for item in supplied:
            if not isinstance(item, dict):
                raise ForgeTraceError("Each reference must be an object.", code="invalid_project_reference")
            kind = str(item.get("kind") or "").strip().lower()
            if kind not in REFERENCE_KINDS:
                raise ForgeTraceError("Project reference kind is invalid.", code="invalid_project_reference_kind")
            value = str(item.get("value") or "").strip()
            if not value or len(value) > MAX_REFERENCE_VALUE_CHARS:
                raise ForgeTraceError("Project reference value is invalid.", code="invalid_project_reference_value")
            entry: dict[str, Any] = {"kind": kind, "value": value, "verified": False, "authority": "informational"}
            if kind == "commit":
                if not COMMIT_PATTERN.fullmatch(value):
                    raise ForgeTraceError("Commit references must be hexadecimal object IDs.", code="invalid_commit_reference")
            elif kind == "path":
                rel = repository.normalize_rel(value)
                parts = {part.casefold() for part in Path(rel).parts}
                if ".git" in parts or ".forgetrace" in parts:
                    raise ForgeTraceError("Protected metadata paths cannot be referenced.", HTTPStatus.FORBIDDEN, "protected_reference_path")
                entry["value"] = rel
            elif kind == "revision":
                pull_request_id = str(item.get("pullRequestId") or value).strip()
                try:
                    revision = int(item.get("revision"))
                except (TypeError, ValueError) as exc:
                    raise ForgeTraceError("Revision references require a positive revision number.", code="invalid_revision_reference") from exc
                if not pull_request_id or revision < 1:
                    raise ForgeTraceError("Revision references are invalid.", code="invalid_revision_reference")
                entry["value"] = pull_request_id
                entry["revision"] = revision
            normalized.append(entry)
        return normalized

    @staticmethod
    def _references_json(references: list[dict[str, Any]]) -> str:
        return json.dumps(references, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _ensure_counter(self, connection: sqlite3.Connection, repository_id: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO project_counters(repository_id,next_issue_number,next_discussion_number) VALUES(?,1,1)",
            (repository_id,),
        )

    def _next_number(self, connection: sqlite3.Connection, repository_id: str, kind: str) -> int:
        self._ensure_counter(connection, repository_id)
        column = "next_issue_number" if kind == "issue" else "next_discussion_number"
        row = connection.execute(
            f"SELECT {column} AS number FROM project_counters WHERE repository_id = ?",
            (repository_id,),
        ).fetchone()
        number = int(row["number"])
        connection.execute(
            f"UPDATE project_counters SET {column} = ? WHERE repository_id = ?",
            (number + 1, repository_id),
        )
        return number

    def _check_quota(self, connection: sqlite3.Connection, repository_id: str, kind: str) -> None:
        maximum = MAX_ISSUES_PER_REPOSITORY if kind == "issue" else MAX_DISCUSSIONS_PER_REPOSITORY
        count = int(connection.execute(
            "SELECT COUNT(*) FROM project_topics WHERE repository_id = ? AND kind = ? AND deleted_at = ''",
            (repository_id, kind),
        ).fetchone()[0])
        if count >= maximum:
            raise ForgeTraceError(
                f"This repository has reached the {kind} limit.",
                HTTPStatus.CONFLICT,
                "project_topic_limit",
                {"kind": kind, "maximum": maximum},
            )

    def _labels_for_topic(self, connection: sqlite3.Connection, topic_id: str) -> list[dict[str, Any]]:
        return [
            self._public_label(row)
            for row in connection.execute(
                "SELECT l.* FROM project_labels l JOIN project_topic_labels tl ON tl.label_id=l.id "
                "WHERE tl.topic_id=? AND l.deleted_at='' ORDER BY l.normalized_name",
                (topic_id,),
            )
        ]

    def _validate_label_ids(
        self, connection: sqlite3.Connection, repository_id: str, supplied: Any
    ) -> list[str]:
        if supplied in (None, ""):
            return []
        if not isinstance(supplied, list):
            raise ForgeTraceError("labelIds must be a list.", code="invalid_label_ids")
        ids = list(dict.fromkeys(str(item or "").strip() for item in supplied if str(item or "").strip()))
        if len(ids) > MAX_LABELS_PER_REPOSITORY:
            raise ForgeTraceError("Too many labels were selected.", code="project_label_selection_limit")
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT id FROM project_labels WHERE repository_id=? AND deleted_at='' AND id IN ({placeholders})",
            (repository_id, *ids),
        ).fetchall()
        found = {row["id"] for row in rows}
        if found != set(ids):
            raise ForgeTraceError("One or more labels do not belong to this repository.", code="project_label_scope_mismatch")
        return ids

    def _validate_milestone_id(
        self, connection: sqlite3.Connection, repository_id: str, milestone_id: Any
    ) -> str:
        value = str(milestone_id or "").strip()
        if not value:
            return ""
        row = connection.execute(
            "SELECT id FROM project_milestones WHERE id=? AND repository_id=? AND deleted_at=''",
            (value, repository_id),
        ).fetchone()
        if not row:
            raise ForgeTraceError("Milestone not found for this repository.", HTTPStatus.NOT_FOUND, "project_milestone_not_found")
        return value

    def _topic_row(
        self, connection: sqlite3.Connection, repository_id: str, topic_id: str, *, kind: str = ""
    ) -> sqlite3.Row:
        query = "SELECT * FROM project_topics WHERE id=? AND repository_id=? AND deleted_at=''"
        params: list[Any] = [topic_id, repository_id]
        if kind:
            query += " AND kind=?"
            params.append(kind)
        row = connection.execute(query, params).fetchone()
        if not row:
            raise ForgeTraceError("Project item not found.", HTTPStatus.NOT_FOUND, "project_topic_not_found")
        return row

    @staticmethod
    def _public_label(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "repositoryId": row["repository_id"], "name": row["name"],
            "color": row["color"], "description": row["description"], "version": int(row["version"]),
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _public_milestone(row: sqlite3.Row, *, progress: dict[str, int] | None = None) -> dict[str, Any]:
        result = {
            "id": row["id"], "repositoryId": row["repository_id"], "title": row["title"],
            "description": row["description"], "dueAt": row["due_at"], "state": row["state"],
            "version": int(row["version"]), "createdAt": row["created_at"],
            "updatedAt": row["updated_at"], "closedAt": row["closed_at"],
        }
        if progress is not None:
            result["progress"] = progress
        return result

    def _public_topic(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_body: bool = True,
        include_comments: bool = False,
        comment_limit: int = 50,
        comment_offset: int = 0,
    ) -> dict[str, Any]:
        references = json.loads(row["references_json"] or "[]")
        milestone = None
        if row["milestone_id"]:
            milestone_row = connection.execute(
                "SELECT * FROM project_milestones WHERE id=? AND deleted_at=''", (row["milestone_id"],)
            ).fetchone()
            if milestone_row:
                milestone = self._public_milestone(milestone_row)
        comment_count = int(connection.execute(
            "SELECT COUNT(*) FROM project_comments WHERE topic_id=? AND deleted_at=''", (row["id"],)
        ).fetchone()[0])
        result: dict[str, Any] = {
            "id": row["id"], "repositoryId": row["repository_id"], "kind": row["kind"],
            "number": int(row["number"]), "title": row["title"], "authorRole": row["author_role"],
            "authorName": row["author_name"], "state": row["state"], "milestone": milestone,
            "assignee": row["assignee"], "dueAt": row["due_at"], "locked": bool(row["locked"]),
            "pinned": bool(row["pinned"]), "acceptedCommentId": row["accepted_comment_id"],
            "version": int(row["version"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            "closedAt": row["closed_at"], "labels": self._labels_for_topic(connection, row["id"]),
            "references": references, "commentCount": comment_count,
            "rendering": {"activeContentRendered": False, "linksActivated": False, "rawHtmlAllowed": False},
        }
        if include_body:
            result["body"] = row["body"]
            result["bodyHtml"] = render_inert_markdown(row["body"])
        if include_comments:
            comments = connection.execute(
                "SELECT * FROM project_comments WHERE topic_id=? AND deleted_at='' ORDER BY created_at LIMIT ? OFFSET ?",
                (row["id"], comment_limit, comment_offset),
            ).fetchall()
            result["comments"] = [self._public_comment(comment) for comment in comments]
            result["commentLimit"] = comment_limit
            result["commentOffset"] = comment_offset
        return result

    @staticmethod
    def _public_comment(row: sqlite3.Row) -> dict[str, Any]:
        hidden = bool(row["hidden"])
        body = "[removed by repository owner]" if hidden else row["body"]
        return {
            "id": row["id"], "repositoryId": row["repository_id"], "topicId": row["topic_id"],
            "authorRole": row["author_role"], "authorName": row["author_name"],
            "body": body, "bodyHtml": render_inert_markdown(body),
            "references": json.loads(row["references_json"] or "[]"),
            "hidden": hidden, "moderationReason": row["moderation_reason"] if hidden else "",
            "version": int(row["version"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            "rendering": {"activeContentRendered": False, "linksActivated": False, "rawHtmlAllowed": False},
        }

    def create_label(
        self, repository_id: str, *, name: Any, color: Any = "#6e7681", description: Any = "",
        actor: str = "Repository Owner", request_id: str = ""
    ) -> dict[str, Any]:
        self._repository(repository_id)
        clean_name = self._clean_text(name, label="Label name", maximum=MAX_LABEL_NAME_CHARS, required=True)
        normalized = clean_name.casefold()
        clean_color = str(color or "").strip()
        if not COLOR_PATTERN.fullmatch(clean_color):
            raise ForgeTraceError("Label color must be a six-digit hex color.", code="invalid_label_color")
        clean_description = self._clean_text(description, label="Label description", maximum=MAX_LABEL_DESCRIPTION_CHARS)
        now = utc_now(); label_id = "label_" + uuid.uuid4().hex[:20]
        with self.lock, self.connect() as connection:
            count = int(connection.execute(
                "SELECT COUNT(*) FROM project_labels WHERE repository_id=? AND deleted_at=''", (repository_id,)
            ).fetchone()[0])
            if count >= MAX_LABELS_PER_REPOSITORY:
                raise ForgeTraceError("This repository has reached the label limit.", HTTPStatus.CONFLICT, "project_label_limit")
            try:
                connection.execute(
                    "INSERT INTO project_labels(id,repository_id,name,normalized_name,color,description,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (label_id, repository_id, clean_name, normalized, clean_color.lower(), clean_description, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ForgeTraceError("A label with this name already exists.", HTTPStatus.CONFLICT, "project_label_exists") from exc
            row = connection.execute("SELECT * FROM project_labels WHERE id=?", (label_id,)).fetchone()
        self._audit(action="project_label_created", outcome="success", repository_id=repository_id, subject_id=label_id, actor=actor, request_id=request_id, details={"name": clean_name})
        return self._public_label(row)

    def update_label(
        self, repository_id: str, label_id: str, *, expected_version: Any, name: Any = None,
        color: Any = None, description: Any = None, actor: str = "Repository Owner", request_id: str = ""
    ) -> dict[str, Any]:
        self._repository(repository_id)
        try: expected = int(expected_version)
        except (TypeError, ValueError) as exc: raise ForgeTraceError("expectedVersion is required.", code="expected_version_required") from exc
        with self.lock, self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_labels WHERE id=? AND repository_id=? AND deleted_at=''", (label_id, repository_id)
            ).fetchone()
            if not row: raise ForgeTraceError("Label not found.", HTTPStatus.NOT_FOUND, "project_label_not_found")
            if int(row["version"]) != expected: raise ForgeTraceError("Label changed since it was loaded.", HTTPStatus.CONFLICT, "project_label_version_changed")
            clean_name = row["name"] if name is None else self._clean_text(name,label="Label name",maximum=MAX_LABEL_NAME_CHARS,required=True)
            clean_color = row["color"] if color is None else str(color or "").strip()
            if not COLOR_PATTERN.fullmatch(clean_color): raise ForgeTraceError("Label color must be a six-digit hex color.", code="invalid_label_color")
            clean_description = row["description"] if description is None else self._clean_text(description,label="Label description",maximum=MAX_LABEL_DESCRIPTION_CHARS)
            now=utc_now()
            try:
                connection.execute(
                    "UPDATE project_labels SET name=?,normalized_name=?,color=?,description=?,version=version+1,updated_at=? WHERE id=?",
                    (clean_name, clean_name.casefold(), clean_color.lower(), clean_description, now, label_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ForgeTraceError("A label with this name already exists.", HTTPStatus.CONFLICT, "project_label_exists") from exc
            updated=connection.execute("SELECT * FROM project_labels WHERE id=?",(label_id,)).fetchone()
        self._audit(action="project_label_updated",outcome="success",repository_id=repository_id,subject_id=label_id,actor=actor,request_id=request_id)
        return self._public_label(updated)

    def delete_label(self, repository_id: str, label_id: str, *, actor: str, request_id: str = "") -> dict[str, Any]:
        self._repository(repository_id)
        self._audit(action="project_label_delete_authorized",outcome="authorized",repository_id=repository_id,subject_id=label_id,actor=actor,request_id=request_id,severity="warning",required=True)
        with self.lock, self.connect() as connection:
            row=connection.execute("SELECT * FROM project_labels WHERE id=? AND repository_id=? AND deleted_at=''",(label_id,repository_id)).fetchone()
            if not row: raise ForgeTraceError("Label not found.",HTTPStatus.NOT_FOUND,"project_label_not_found")
            now=utc_now()
            connection.execute("DELETE FROM project_topic_labels WHERE label_id=?",(label_id,))
            connection.execute("UPDATE project_labels SET deleted_at=?,updated_at=?,version=version+1 WHERE id=?",(now,now,label_id))
        self._audit(action="project_label_deleted",outcome="success",repository_id=repository_id,subject_id=label_id,actor=actor,request_id=request_id,severity="warning")
        return {"deleted": True, "id": label_id}

    def list_labels(self, repository_id: str) -> list[dict[str, Any]]:
        self._repository(repository_id)
        with self.connect() as connection:
            return [self._public_label(row) for row in connection.execute(
                "SELECT * FROM project_labels WHERE repository_id=? AND deleted_at='' ORDER BY normalized_name",(repository_id,)
            )]

    def create_milestone(self, repository_id: str, *, title: Any, description: Any = "", due_at: Any = "", actor: str="Repository Owner", request_id: str="") -> dict[str, Any]:
        self._repository(repository_id)
        clean_title=self._clean_text(title,label="Milestone title",maximum=MAX_TITLE_CHARS,required=True)
        clean_description=self._clean_text(description,label="Milestone description",maximum=MAX_MILESTONE_DESCRIPTION_CHARS)
        clean_due=self._normalize_due_at(due_at); now=utc_now(); milestone_id="mile_"+uuid.uuid4().hex[:20]
        with self.lock,self.connect() as connection:
            count=int(connection.execute("SELECT COUNT(*) FROM project_milestones WHERE repository_id=? AND deleted_at=''",(repository_id,)).fetchone()[0])
            if count>=MAX_MILESTONES_PER_REPOSITORY: raise ForgeTraceError("This repository has reached the milestone limit.",HTTPStatus.CONFLICT,"project_milestone_limit")
            try:
                connection.execute("INSERT INTO project_milestones(id,repository_id,title,normalized_title,description,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(milestone_id,repository_id,clean_title,clean_title.casefold(),clean_description,clean_due,now,now))
            except sqlite3.IntegrityError as exc: raise ForgeTraceError("A milestone with this title already exists.",HTTPStatus.CONFLICT,"project_milestone_exists") from exc
            row=connection.execute("SELECT * FROM project_milestones WHERE id=?",(milestone_id,)).fetchone()
        self._audit(action="project_milestone_created",outcome="success",repository_id=repository_id,subject_id=milestone_id,actor=actor,request_id=request_id)
        return self._public_milestone(row,progress={"open":0,"closed":0,"total":0})

    def update_milestone(self, repository_id: str, milestone_id: str, *, expected_version: Any, title: Any=None, description: Any=None, due_at: Any=None, state: Any=None, actor: str="Repository Owner", request_id: str="") -> dict[str, Any]:
        self._repository(repository_id)
        try: expected=int(expected_version)
        except (TypeError,ValueError) as exc: raise ForgeTraceError("expectedVersion is required.",code="expected_version_required") from exc
        with self.lock,self.connect() as connection:
            row=connection.execute("SELECT * FROM project_milestones WHERE id=? AND repository_id=? AND deleted_at=''",(milestone_id,repository_id)).fetchone()
            if not row: raise ForgeTraceError("Milestone not found.",HTTPStatus.NOT_FOUND,"project_milestone_not_found")
            if int(row["version"])!=expected: raise ForgeTraceError("Milestone changed since it was loaded.",HTTPStatus.CONFLICT,"project_milestone_version_changed")
            clean_title=row["title"] if title is None else self._clean_text(title,label="Milestone title",maximum=MAX_TITLE_CHARS,required=True)
            clean_description=row["description"] if description is None else self._clean_text(description,label="Milestone description",maximum=MAX_MILESTONE_DESCRIPTION_CHARS)
            clean_due=row["due_at"] if due_at is None else self._normalize_due_at(due_at)
            clean_state=row["state"] if state is None else str(state).strip().lower()
            if clean_state not in TOPIC_STATES: raise ForgeTraceError("Milestone state must be open or closed.",code="invalid_milestone_state")
            now=utc_now(); closed_at=now if clean_state=="closed" else ""
            try:
                connection.execute("UPDATE project_milestones SET title=?,normalized_title=?,description=?,due_at=?,state=?,closed_at=?,version=version+1,updated_at=? WHERE id=?",(clean_title,clean_title.casefold(),clean_description,clean_due,clean_state,closed_at,now,milestone_id))
            except sqlite3.IntegrityError as exc: raise ForgeTraceError("A milestone with this title already exists.",HTTPStatus.CONFLICT,"project_milestone_exists") from exc
            updated=connection.execute("SELECT * FROM project_milestones WHERE id=?",(milestone_id,)).fetchone()
            progress=self._milestone_progress(connection,milestone_id)
        self._audit(action="project_milestone_updated",outcome="success",repository_id=repository_id,subject_id=milestone_id,actor=actor,request_id=request_id,details={"state":clean_state})
        return self._public_milestone(updated,progress=progress)

    def _milestone_progress(self, connection: sqlite3.Connection, milestone_id: str) -> dict[str,int]:
        rows=connection.execute("SELECT state,COUNT(*) AS count FROM project_topics WHERE milestone_id=? AND deleted_at='' GROUP BY state",(milestone_id,)).fetchall()
        values={row["state"]:int(row["count"]) for row in rows}; total=sum(values.values())
        return {"open":values.get("open",0),"closed":values.get("closed",0),"total":total}

    def list_milestones(self, repository_id: str) -> list[dict[str,Any]]:
        self._repository(repository_id)
        with self.connect() as connection:
            rows=connection.execute("SELECT * FROM project_milestones WHERE repository_id=? AND deleted_at='' ORDER BY state,due_at='',due_at,normalized_title",(repository_id,)).fetchall()
            return [self._public_milestone(row,progress=self._milestone_progress(connection,row["id"])) for row in rows]

    def delete_milestone(self, repository_id: str, milestone_id: str, *, actor: str, request_id: str="") -> dict[str,Any]:
        self._repository(repository_id)
        self._audit(action="project_milestone_delete_authorized",outcome="authorized",repository_id=repository_id,subject_id=milestone_id,actor=actor,request_id=request_id,severity="warning",required=True)
        with self.lock,self.connect() as connection:
            row=connection.execute("SELECT id FROM project_milestones WHERE id=? AND repository_id=? AND deleted_at=''",(milestone_id,repository_id)).fetchone()
            if not row: raise ForgeTraceError("Milestone not found.",HTTPStatus.NOT_FOUND,"project_milestone_not_found")
            now=utc_now(); connection.execute("UPDATE project_topics SET milestone_id=NULL,version=version+1,updated_at=? WHERE milestone_id=? AND deleted_at=''",(now,milestone_id)); connection.execute("UPDATE project_milestones SET deleted_at=?,updated_at=?,version=version+1 WHERE id=?",(now,now,milestone_id))
        self._audit(action="project_milestone_deleted",outcome="success",repository_id=repository_id,subject_id=milestone_id,actor=actor,request_id=request_id,severity="warning")
        return {"deleted":True,"id":milestone_id}

    def create_topic(self, repository_id: str, *, kind: str, title: Any, body: Any="", references: Any=None, actor_role: str="owner", actor_name: Any="Repository Owner", invite_id: str="", request_id: str="") -> dict[str,Any]:
        self._repository(repository_id)
        clean_kind=str(kind or "").strip().lower()
        if clean_kind not in TOPIC_KINDS: raise ForgeTraceError("Project item kind must be issue or discussion.",code="invalid_project_topic_kind")
        clean_title=self._clean_text(title,label="Title",maximum=MAX_TITLE_CHARS,required=True)
        clean_body=self._clean_text(body,label="Body",maximum=MAX_BODY_CHARS)
        clean_actor=self._clean_text(actor_name,label="Author name",maximum=MAX_AUTHOR_CHARS,required=True)
        clean_references=self._clean_references(repository_id,references)
        now=utc_now(); topic_id=("issue_" if clean_kind=="issue" else "disc_")+uuid.uuid4().hex[:20]
        with self.lock,self.connect() as connection:
            self._check_quota(connection,repository_id,clean_kind)
            number=self._next_number(connection,repository_id,clean_kind)
            connection.execute("INSERT INTO project_topics(id,repository_id,kind,number,title,body,references_json,author_role,author_name,invite_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(topic_id,repository_id,clean_kind,number,clean_title,clean_body,self._references_json(clean_references),actor_role,clean_actor,str(invite_id or "")[:64],now,now))
            row=self._topic_row(connection,repository_id,topic_id)
            result=self._public_topic(connection,row,include_comments=True)
        self._audit(action=f"project_{clean_kind}_created",outcome="success",repository_id=repository_id,subject_id=topic_id,actor=clean_actor,surface="owner" if actor_role=="owner" else "gateway",request_id=request_id,details={"number":number,"inviteId":str(invite_id or "")[:64]})
        return result

    def create_topic_for_token(self, token: str, *, kind: str, title: Any, body: Any="", references: Any=None, actor_name: Any, request_id: str="") -> dict[str,Any]:
        actor=self.contributor_actor(token,actor_name)
        return self.create_topic(actor["repositoryId"],kind=kind,title=title,body=body,references=references,actor_role="contributor",actor_name=actor["actorName"],invite_id=actor["inviteId"],request_id=request_id)

    def _apply_topic_labels(self, connection: sqlite3.Connection, topic_id: str, label_ids: list[str]) -> None:
        connection.execute("DELETE FROM project_topic_labels WHERE topic_id=?",(topic_id,))
        connection.executemany("INSERT INTO project_topic_labels(topic_id,label_id) VALUES(?,?)",[(topic_id,label_id) for label_id in label_ids])

    def update_topic(self, repository_id: str, topic_id: str, *, expected_version: Any, title: Any=None, body: Any=None, references: Any=None, state: Any=None, milestone_id: Any=None, assignee: Any=None, due_at: Any=None, locked: Any=None, pinned: Any=None, label_ids: Any=None, accepted_comment_id: Any=None, actor: str="Repository Owner", request_id: str="") -> dict[str,Any]:
        self._repository(repository_id)
        try: expected=int(expected_version)
        except (TypeError,ValueError) as exc: raise ForgeTraceError("expectedVersion is required.",code="expected_version_required") from exc
        with self.lock,self.connect() as connection:
            row=self._topic_row(connection,repository_id,topic_id)
            if int(row["version"])!=expected: raise ForgeTraceError("Project item changed since it was loaded.",HTTPStatus.CONFLICT,"project_topic_version_changed")
            clean_title=row["title"] if title is None else self._clean_text(title,label="Title",maximum=MAX_TITLE_CHARS,required=True)
            clean_body=row["body"] if body is None else self._clean_text(body,label="Body",maximum=MAX_BODY_CHARS)
            refs=json.loads(row["references_json"] or "[]") if references is None else self._clean_references(repository_id,references)
            clean_state=row["state"] if state is None else str(state).strip().lower()
            if clean_state not in TOPIC_STATES: raise ForgeTraceError("State must be open or closed.",code="invalid_project_topic_state")
            clean_milestone=row["milestone_id"] or "" if milestone_id is None else self._validate_milestone_id(connection,repository_id,milestone_id)
            clean_assignee=row["assignee"] if assignee is None else self._clean_text(assignee,label="Assignee",maximum=MAX_ASSIGNEE_CHARS)
            clean_due=row["due_at"] if due_at is None else self._normalize_due_at(due_at)
            clean_locked=bool(row["locked"]) if locked is None else bool(locked)
            clean_pinned=bool(row["pinned"]) if pinned is None else bool(pinned)
            selected_labels=self._validate_label_ids(connection,repository_id,label_ids) if label_ids is not None else [item["id"] for item in self._labels_for_topic(connection,topic_id)]
            accepted=row["accepted_comment_id"] if accepted_comment_id is None else str(accepted_comment_id or "").strip()
            if accepted:
                if row["kind"]!="discussion": raise ForgeTraceError("Only discussions can accept an answer.",code="accepted_answer_issue_not_allowed")
                comment=connection.execute("SELECT id FROM project_comments WHERE id=? AND topic_id=? AND deleted_at='' AND hidden=0",(accepted,topic_id)).fetchone()
                if not comment: raise ForgeTraceError("Accepted answer comment not found.",HTTPStatus.NOT_FOUND,"project_comment_not_found")
            sensitive_changed=clean_locked!=bool(row["locked"]) or clean_pinned!=bool(row["pinned"]) or accepted!=row["accepted_comment_id"]
            if sensitive_changed:
                self._audit(action="project_moderation_authorized",outcome="authorized",repository_id=repository_id,subject_id=topic_id,actor=actor,request_id=request_id,severity="warning",required=True,details={"locked":clean_locked,"pinned":clean_pinned,"acceptedCommentId":accepted})
            now=utc_now(); closed_at=now if clean_state=="closed" else ""
            connection.execute("UPDATE project_topics SET title=?,body=?,references_json=?,state=?,milestone_id=?,assignee=?,due_at=?,locked=?,pinned=?,accepted_comment_id=?,version=version+1,updated_at=?,closed_at=? WHERE id=?",(clean_title,clean_body,self._references_json(refs),clean_state,clean_milestone or None,clean_assignee,clean_due,int(clean_locked),int(clean_pinned),accepted,now,closed_at,topic_id))
            self._apply_topic_labels(connection,topic_id,selected_labels)
            updated=self._topic_row(connection,repository_id,topic_id)
            result=self._public_topic(connection,updated,include_comments=True)
        self._audit(action=f"project_{row['kind']}_updated",outcome="success",repository_id=repository_id,subject_id=topic_id,actor=actor,request_id=request_id,details={"state":clean_state,"locked":clean_locked,"pinned":clean_pinned})
        return result

    def delete_topic(self, repository_id: str, topic_id: str, *, actor: str, reason: Any="", request_id: str="") -> dict[str,Any]:
        self._repository(repository_id); clean_reason=self._clean_text(reason,label="Deletion reason",maximum=500)
        self._audit(action="project_topic_delete_authorized",outcome="authorized",repository_id=repository_id,subject_id=topic_id,actor=actor,request_id=request_id,severity="warning",required=True)
        with self.lock,self.connect() as connection:
            row=self._topic_row(connection,repository_id,topic_id); now=utc_now()
            connection.execute("UPDATE project_topics SET deleted_at=?,deleted_by=?,deletion_reason=?,version=version+1,updated_at=? WHERE id=?",(now,actor,clean_reason,now,topic_id))
        self._audit(action=f"project_{row['kind']}_deleted",outcome="success",repository_id=repository_id,subject_id=topic_id,actor=actor,request_id=request_id,severity="warning",details={"reason":clean_reason})
        return {"deleted":True,"id":topic_id}

    def add_comment(self, repository_id: str, topic_id: str, *, body: Any, references: Any=None, expected_version: Any, actor_role: str="owner", actor_name: Any="Repository Owner", invite_id: str="", request_id: str="") -> dict[str,Any]:
        self._repository(repository_id)
        try: expected=int(expected_version)
        except (TypeError,ValueError) as exc: raise ForgeTraceError("expectedVersion is required.",code="expected_version_required") from exc
        clean_body=self._clean_text(body,label="Comment",maximum=MAX_COMMENT_CHARS,required=True)
        clean_actor=self._clean_text(actor_name,label="Author name",maximum=MAX_AUTHOR_CHARS,required=True)
        refs=self._clean_references(repository_id,references); now=utc_now(); comment_id="comment_"+uuid.uuid4().hex[:20]
        with self.lock,self.connect() as connection:
            topic=self._topic_row(connection,repository_id,topic_id)
            if int(topic["version"])!=expected: raise ForgeTraceError("Project item changed since it was loaded.",HTTPStatus.CONFLICT,"project_topic_version_changed")
            if bool(topic["locked"]) and actor_role!="owner": raise ForgeTraceError("This project item is locked.",HTTPStatus.LOCKED,"project_topic_locked")
            topic_count=int(connection.execute("SELECT COUNT(*) FROM project_comments WHERE topic_id=? AND deleted_at=''",(topic_id,)).fetchone()[0])
            total_count=int(connection.execute("SELECT COUNT(*) FROM project_comments WHERE repository_id=? AND deleted_at=''",(repository_id,)).fetchone()[0])
            if topic_count>=MAX_COMMENTS_PER_TOPIC: raise ForgeTraceError("This project item has reached the comment limit.",HTTPStatus.CONFLICT,"project_comment_limit")
            if total_count>=MAX_TOTAL_COMMENTS_PER_REPOSITORY: raise ForgeTraceError("This repository has reached the project comment limit.",HTTPStatus.CONFLICT,"project_repository_comment_limit")
            connection.execute("INSERT INTO project_comments(id,repository_id,topic_id,author_role,author_name,invite_id,body,references_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(comment_id,repository_id,topic_id,actor_role,clean_actor,str(invite_id or "")[:64],clean_body,self._references_json(refs),now,now))
            connection.execute("UPDATE project_topics SET version=version+1,updated_at=? WHERE id=?",(now,topic_id))
            updated=self._topic_row(connection,repository_id,topic_id)
            result=self._public_topic(connection,updated,include_comments=True,comment_limit=MAX_PAGE_SIZE)
        self._audit(action="project_comment_created",outcome="success",repository_id=repository_id,subject_id=comment_id,actor=clean_actor,surface="owner" if actor_role=="owner" else "gateway",request_id=request_id,details={"topicId":topic_id,"kind":topic["kind"],"inviteId":str(invite_id or "")[:64]})
        return result

    def add_comment_for_token(self, token: str, topic_id: str, *, body: Any, references: Any=None, expected_version: Any, actor_name: Any, request_id: str="") -> dict[str,Any]:
        actor=self.contributor_actor(token,actor_name)
        return self.add_comment(actor["repositoryId"],topic_id,body=body,references=references,expected_version=expected_version,actor_role="contributor",actor_name=actor["actorName"],invite_id=actor["inviteId"],request_id=request_id)

    def moderate_comment(self, repository_id: str, comment_id: str, *, expected_version: Any, actor: str, reason: Any="", request_id: str="") -> dict[str,Any]:
        self._repository(repository_id)
        try: expected=int(expected_version)
        except (TypeError,ValueError) as exc: raise ForgeTraceError("expectedVersion is required.",code="expected_version_required") from exc
        clean_reason=self._clean_text(reason,label="Moderation reason",maximum=500,required=True)
        self._audit(action="project_comment_moderation_authorized",outcome="authorized",repository_id=repository_id,subject_id=comment_id,actor=actor,request_id=request_id,severity="warning",required=True)
        with self.lock,self.connect() as connection:
            row=connection.execute("SELECT * FROM project_comments WHERE id=? AND repository_id=? AND deleted_at=''",(comment_id,repository_id)).fetchone()
            if not row: raise ForgeTraceError("Comment not found.",HTTPStatus.NOT_FOUND,"project_comment_not_found")
            if int(row["version"])!=expected: raise ForgeTraceError("Comment changed since it was loaded.",HTTPStatus.CONFLICT,"project_comment_version_changed")
            now=utc_now(); connection.execute("UPDATE project_comments SET hidden=1,moderation_reason=?,moderated_by=?,moderated_at=?,version=version+1,updated_at=? WHERE id=?",(clean_reason,actor,now,now,comment_id)); connection.execute("UPDATE project_topics SET version=version+1,updated_at=? WHERE id=?",(now,row["topic_id"]))
            updated=connection.execute("SELECT * FROM project_comments WHERE id=?",(comment_id,)).fetchone()
        self._audit(action="project_comment_moderated",outcome="success",repository_id=repository_id,subject_id=comment_id,actor=actor,request_id=request_id,severity="warning",details={"topicId":row["topic_id"],"reason":clean_reason})
        return self._public_comment(updated)

    def list_topics(self, repository_id: str, *, kind: str, state: str="", query: str="", label_id: str="", milestone_id: str="", limit: Any=50, offset: Any=0) -> dict[str,Any]:
        self._repository(repository_id); clean_kind=str(kind or "").strip().lower()
        if clean_kind not in TOPIC_KINDS: raise ForgeTraceError("Project item kind must be issue or discussion.",code="invalid_project_topic_kind")
        clean_state=str(state or "").strip().lower()
        if clean_state and clean_state not in TOPIC_STATES: raise ForgeTraceError("State filter is invalid.",code="invalid_project_topic_state")
        bounded_limit,bounded_offset=self._page(limit,offset); where=["t.repository_id=?","t.kind=?","t.deleted_at=''"]; params:[Any]=[repository_id,clean_kind]
        if clean_state: where.append("t.state=?");params.append(clean_state)
        clean_query=str(query or "").strip()
        if clean_query: where.append("(t.title LIKE ? OR t.body LIKE ? OR t.assignee LIKE ?)");pattern=f"%{clean_query[:200]}%";params.extend([pattern,pattern,pattern])
        if milestone_id: where.append("t.milestone_id=?");params.append(str(milestone_id))
        join=""
        if label_id: join=" JOIN project_topic_labels filter_labels ON filter_labels.topic_id=t.id ";where.append("filter_labels.label_id=?");params.append(str(label_id))
        clause=" AND ".join(where)
        with self.connect() as connection:
            total=int(connection.execute(f"SELECT COUNT(DISTINCT t.id) FROM project_topics t {join} WHERE {clause}",params).fetchone()[0])
            rows=connection.execute(f"SELECT DISTINCT t.* FROM project_topics t {join} WHERE {clause} ORDER BY t.pinned DESC,t.updated_at DESC,t.number DESC LIMIT ? OFFSET ?",(*params,bounded_limit,bounded_offset)).fetchall()
            items=[self._public_topic(connection,row,include_body=False) for row in rows]
        return {"items":items,"total":total,"limit":bounded_limit,"offset":bounded_offset,"kind":clean_kind}

    def get_topic(self, repository_id: str, topic_id: str, *, comment_limit: Any=50, comment_offset: Any=0) -> dict[str,Any]:
        self._repository(repository_id); bounded_limit,bounded_offset=self._page(comment_limit,comment_offset)
        with self.connect() as connection:
            row=self._topic_row(connection,repository_id,topic_id)
            return self._public_topic(connection,row,include_comments=True,comment_limit=bounded_limit,comment_offset=bounded_offset)

    def get_topic_for_token(self, token: str, topic_id: str, *, comment_limit: Any=50, comment_offset: Any=0) -> dict[str,Any]:
        actor=self.contributor_actor(token,"Contributor")
        return self.get_topic(actor["repositoryId"],topic_id,comment_limit=comment_limit,comment_offset=comment_offset)

    def list_topics_for_token(self, token: str, *, kind: str, state: str="", query: str="", label_id: str="", milestone_id: str="", limit: Any=50, offset: Any=0) -> dict[str,Any]:
        actor=self.contributor_actor(token,"Contributor")
        return self.list_topics(actor["repositoryId"],kind=kind,state=state,query=query,label_id=label_id,milestone_id=milestone_id,limit=limit,offset=offset)

    def overview(self, repository_id: str, *, recent_limit: Any=20) -> dict[str,Any]:
        record=self.registry.get_repository(repository_id); bounded_limit,_=self._page(recent_limit,0)
        with self.connect() as connection:
            counts={}
            for kind in TOPIC_KINDS:
                for state in TOPIC_STATES:
                    counts[f"{kind}{state.title()}"]=int(connection.execute("SELECT COUNT(*) FROM project_topics WHERE repository_id=? AND kind=? AND state=? AND deleted_at=''",(repository_id,kind,state)).fetchone()[0])
            labels=[self._public_label(row) for row in connection.execute("SELECT * FROM project_labels WHERE repository_id=? AND deleted_at='' ORDER BY normalized_name",(repository_id,))]
            milestones=[self._public_milestone(row,progress=self._milestone_progress(connection,row["id"])) for row in connection.execute("SELECT * FROM project_milestones WHERE repository_id=? AND deleted_at='' ORDER BY state,due_at='',due_at,normalized_title",(repository_id,))]
            recent_rows=connection.execute("SELECT * FROM project_topics WHERE repository_id=? AND deleted_at='' ORDER BY pinned DESC,updated_at DESC LIMIT ?",(repository_id,bounded_limit)).fetchall()
            recent=[self._public_topic(connection,row,include_body=False) for row in recent_rows]
        return {"schemaVersion":PROJECT_COORDINATION_SCHEMA_VERSION,"repository":{"id":record["id"],"name":record["name"],"status":record["status"]},"counts":counts,"labels":labels,"milestones":milestones,"recent":recent,"limits":{"titleChars":MAX_TITLE_CHARS,"bodyChars":MAX_BODY_CHARS,"commentChars":MAX_COMMENT_CHARS,"pageSize":MAX_PAGE_SIZE,"references":MAX_REFERENCES},"authority":{"repositoryMutation":False,"gitMutation":False,"activeContentRendered":False,"contributorPermission":"projectParticipation"}}

    def overview_for_token(self, token: str) -> dict[str,Any]:
        actor=self.contributor_actor(token,"Contributor")
        result=self.overview(actor["repositoryId"])
        result["contributor"]={"inviteId":actor["inviteId"],"inviteFingerprint":actor["inviteFingerprint"],"canParticipate":True}
        return result

    def cleanup_retention(self) -> dict[str,int]:
        cutoff=(datetime.now(timezone.utc)-timedelta(days=SOFT_DELETE_RETENTION_DAYS)).isoformat(timespec="seconds").replace("+00:00","Z")
        removed_topics=removed_comments=removed_labels=removed_milestones=0
        with self.lock,self.connect() as connection:
            rows=connection.execute("SELECT id FROM project_topics WHERE deleted_at!='' AND deleted_at<?",(cutoff,)).fetchall(); removed_topics=len(rows)
            if rows: connection.executemany("DELETE FROM project_topics WHERE id=?",[(row["id"],) for row in rows])
            removed_comments=connection.execute("DELETE FROM project_comments WHERE deleted_at!='' AND deleted_at<?",(cutoff,)).rowcount
            removed_labels=connection.execute("DELETE FROM project_labels WHERE deleted_at!='' AND deleted_at<?",(cutoff,)).rowcount
            removed_milestones=connection.execute("DELETE FROM project_milestones WHERE deleted_at!='' AND deleted_at<?",(cutoff,)).rowcount
        return {"topics":removed_topics,"comments":max(0,removed_comments),"labels":max(0,removed_labels),"milestones":max(0,removed_milestones)}

    def health_status(self, repository_id: str="") -> dict[str,Any]:
        result={"schemaVersion":PROJECT_COORDINATION_SCHEMA_VERSION,"databasePath":str(self.db_path),"lockPath":str(self.lock_path),"integrity":"unknown","repositories":[],"storageBytes":0}
        try:
            result["storageBytes"]=self.db_path.stat().st_size if self.db_path.exists() else 0
            with self.connect() as connection:
                integrity=str(connection.execute("PRAGMA integrity_check").fetchone()[0]); result["integrity"]=integrity
                params:tuple[Any,...]=()
                where=""
                if repository_id: where=" WHERE repository_id=?";params=(repository_id,)
                repo_rows=connection.execute(f"SELECT repository_id,COUNT(*) AS topics FROM project_topics{where} GROUP BY repository_id",params).fetchall()
                for row in repo_rows:
                    comments=int(connection.execute("SELECT COUNT(*) FROM project_comments WHERE repository_id=? AND deleted_at=''",(row["repository_id"],)).fetchone()[0])
                    result["repositories"].append({"repositoryId":row["repository_id"],"topicCount":int(row["topics"]),"commentCount":comments})
        except (OSError,sqlite3.Error) as exc:
            result["integrity"]="error";result["error"]=str(exc)
        return result
