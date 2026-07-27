from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .locks import InterProcessRLock
from .utils import utc_now

SECURITY_EVENT_SCHEMA_VERSION = 1
SECURITY_SEGMENT_SCHEMA_VERSION = 1
SECURITY_ROTATION_JOURNAL_SCHEMA_VERSION = 1
SECURITY_RETENTION_POLICY_SCHEMA_VERSION = 1
SECURITY_ANCHOR_SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
MAX_DETAIL_DEPTH = 6
MAX_DETAIL_ITEMS = 100
MAX_DETAIL_STRING = 2048
MAX_EXPORT_EVENTS = 100_000
MAX_ROTATION_EVENTS = 100_000
MIN_ROTATION_FREE_BYTES = 16 * 1024 * 1024
MAX_ANCHOR_EVIDENCE_BYTES = 64 * 1024

DEFAULT_RETENTION_POLICY = {
    "maxActiveEvents": 50_000,
    "segmentEventTarget": 10_000,
    "maxRetainedEvents": 1_000_000,
    "maxRetentionAgeDays": 3650,
    "maxStorageBytes": 2 * 1024 * 1024 * 1024,
    "minimumProtectedEvents": 10_000,
    "minimumProtectedAgeDays": 90,
}

_ALLOWED_SEVERITIES = {"info", "warning", "error", "critical"}
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$")
_SENSITIVE_KEY_PARTS = {
    "authorization",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "session",
    "token",
}


class SecurityEventError(RuntimeError):
    """Raised when required security evidence cannot be verified or recorded."""


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_bytes(path, _canonical_bytes(payload))


def _parse_utc(value: str) -> datetime:
    cleaned = str(value or "").strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class SecurityEventLedger:
    """Append-only, segmented, tamper-evident application security history.

    The active ledger remains SQLite with immutable rows and FULL-synchronous commits.
    Rotation seals only a verified prefix into immutable application-data JSON segments,
    then installs a rebuilt active database whose immutable metadata binds it to the
    final sealed segment and any retention checkpoint. Every operation is serialized by
    the same OS-backed lock used for protected appends.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "security-events.sqlite3"
        self.lock = InterProcessRLock(self.data_dir / "security-events.lock", timeout=30.0)
        self.segments_dir = self.data_dir / "security-event-segments"
        self.rotations_dir = self.data_dir / "security-event-rotations"
        self.anchors_dir = self.data_dir / "security-event-anchors"
        self.policy_path = self.data_dir / "security-event-retention.json"
        self.retention_root_path = self.segments_dir / "retention-root.json"
        for directory in (self.segments_dir, self.rotations_dir, self.anchors_dir):
            directory.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.startup_rotation_recovery = self._recover_rotation_journals_locked()
            self._migrate_locked()
            self._ensure_policy_locked()
        self.startup_integrity = self.verify_integrity()
        self._compromised = not bool(self.startup_integrity["healthy"])

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        if not write:
            connection.execute("PRAGMA query_only = ON")
        try:
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _schema_sql() -> str:
        return """
        CREATE TABLE IF NOT EXISTS security_event_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS security_events (
            sequence INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL UNIQUE,
            occurred_at TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT NOT NULL CHECK(severity IN ('info','warning','error','critical')),
            action TEXT NOT NULL,
            outcome TEXT NOT NULL,
            surface TEXT NOT NULL,
            repository_id TEXT NOT NULL DEFAULT '',
            request_id TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT '',
            subject_id TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL UNIQUE
        );

        CREATE INDEX IF NOT EXISTS idx_security_events_occurred_at
            ON security_events(occurred_at DESC, sequence DESC);
        CREATE INDEX IF NOT EXISTS idx_security_events_category
            ON security_events(category, sequence DESC);
        CREATE INDEX IF NOT EXISTS idx_security_events_repository
            ON security_events(repository_id, sequence DESC);
        """

    @staticmethod
    def _trigger_sql() -> str:
        return """
        CREATE TRIGGER security_events_no_update
        BEFORE UPDATE ON security_events
        BEGIN
            SELECT RAISE(ABORT, 'ForgeTrace security events are append-only');
        END;

        CREATE TRIGGER security_events_no_delete
        BEFORE DELETE ON security_events
        BEGIN
            SELECT RAISE(ABORT, 'ForgeTrace security events are append-only');
        END;

        CREATE TRIGGER security_event_meta_no_update
        BEFORE UPDATE ON security_event_meta
        BEGIN
            SELECT RAISE(ABORT, 'ForgeTrace security event metadata is immutable');
        END;

        CREATE TRIGGER security_event_meta_no_delete
        BEFORE DELETE ON security_event_meta
        BEGIN
            SELECT RAISE(ABORT, 'ForgeTrace security event metadata is immutable');
        END;
        """

    def _migrate_locked(self) -> None:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            existing_events_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='security_events'"
            ).fetchone() is not None
            connection.executescript(self._schema_sql())
            now = utc_now()
            ledger_id = "ledger_" + uuid.uuid4().hex
            meta_defaults = {
                "schema_version": str(SECURITY_EVENT_SCHEMA_VERSION),
                "ledger_id": ledger_id,
                "active_start_sequence": "1",
                "active_previous_event_hash": GENESIS_HASH,
                "previous_segment_hash": GENESIS_HASH,
                "retention_root_sha256": "",
            }
            for key, value in meta_defaults.items():
                connection.execute(
                    "INSERT OR IGNORE INTO security_event_meta(key,value,updated_at) VALUES(?,?,?)",
                    (key, value, now),
                )
            if not existing_events_table:
                connection.executescript(self._trigger_sql())
            connection.commit()
        finally:
            connection.close()

    def _create_active_database(
        self,
        path: Path,
        *,
        rows: list[dict[str, Any]],
        ledger_id: str,
        active_start_sequence: int,
        active_previous_event_hash: str,
        previous_segment_hash: str,
        retention_root_sha256: str,
    ) -> None:
        path.unlink(missing_ok=True)
        connection = sqlite3.connect(path, timeout=30.0)
        try:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(self._schema_sql())
            now = utc_now()
            for key, value in {
                "schema_version": str(SECURITY_EVENT_SCHEMA_VERSION),
                "ledger_id": ledger_id,
                "active_start_sequence": str(active_start_sequence),
                "active_previous_event_hash": active_previous_event_hash,
                "previous_segment_hash": previous_segment_hash,
                "retention_root_sha256": retention_root_sha256,
            }.items():
                connection.execute(
                    "INSERT INTO security_event_meta(key,value,updated_at) VALUES(?,?,?)",
                    (key, value, now),
                )
            for event in rows:
                connection.execute(
                    """
                    INSERT INTO security_events(
                        sequence,event_id,occurred_at,category,severity,action,outcome,
                        surface,repository_id,request_id,actor,subject_id,details_json,
                        previous_hash,event_hash
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        int(event["sequence"]), str(event["eventId"]), str(event["occurredAt"]),
                        str(event["category"]), str(event["severity"]), str(event["action"]),
                        str(event["outcome"]), str(event["surface"]), str(event.get("repositoryId") or ""),
                        str(event.get("requestId") or ""), str(event.get("actor") or ""),
                        str(event.get("subjectId") or ""), self._details_json(event.get("details", {})),
                        str(event["previousHash"]), str(event["eventHash"]),
                    ),
                )
            connection.executescript(self._trigger_sql())
            connection.commit()
        finally:
            connection.close()
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

    @staticmethod
    def _clean_identifier(value: Any, *, label: str, allow_empty: bool = False) -> str:
        cleaned = str(value or "").strip().lower()
        if not cleaned and allow_empty:
            return ""
        if not _IDENTIFIER.fullmatch(cleaned):
            raise SecurityEventError(f"Invalid {label} for security event ledger.")
        return cleaned

    @staticmethod
    def _clean_text(value: Any, *, maximum: int = 240) -> str:
        return " ".join(str(value or "").strip().split())[:maximum]

    @classmethod
    def sanitize_details(cls, value: Any, *, _depth: int = 0) -> Any:
        if _depth >= MAX_DETAIL_DEPTH:
            return "[TRUNCATED]"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            cleaned = value.replace("\x00", "")
            return cleaned[:MAX_DETAIL_STRING] + ("…" if len(cleaned) > MAX_DETAIL_STRING else "")
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for index, (raw_key, raw_value) in enumerate(value.items()):
                if index >= MAX_DETAIL_ITEMS:
                    result["_truncated"] = True
                    break
                key = cls._clean_text(raw_key, maximum=120) or "field"
                normalized = key.casefold().replace("-", "_").replace(" ", "_")
                if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                    result[key] = "[REDACTED]"
                else:
                    result[key] = cls.sanitize_details(raw_value, _depth=_depth + 1)
            return result
        if isinstance(value, (list, tuple, set)):
            items = list(value)
            sanitized = [cls.sanitize_details(item, _depth=_depth + 1) for item in items[:MAX_DETAIL_ITEMS]]
            if len(items) > MAX_DETAIL_ITEMS:
                sanitized.append("[TRUNCATED]")
            return sanitized
        return cls.sanitize_details(str(value), _depth=_depth + 1)

    @staticmethod
    def _details_json(details: Any) -> str:
        return json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _event_hash(
        cls,
        *,
        sequence: int,
        event_id: str,
        occurred_at: str,
        category: str,
        severity: str,
        action: str,
        outcome: str,
        surface: str,
        repository_id: str,
        request_id: str,
        actor: str,
        subject_id: str,
        details_json: str,
        previous_hash: str,
    ) -> str:
        payload = {
            "schemaVersion": SECURITY_EVENT_SCHEMA_VERSION,
            "sequence": sequence,
            "eventId": event_id,
            "occurredAt": occurred_at,
            "category": category,
            "severity": severity,
            "action": action,
            "outcome": outcome,
            "surface": surface,
            "repositoryId": repository_id,
            "requestId": request_id,
            "actor": actor,
            "subjectId": subject_id,
            "details": json.loads(details_json),
            "previousHash": previous_hash,
        }
        return _sha256_bytes(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    @staticmethod
    def _public_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            details = {"_invalid": True}
        return {
            "sequence": int(row["sequence"]),
            "eventId": row["event_id"],
            "occurredAt": row["occurred_at"],
            "category": row["category"],
            "severity": row["severity"],
            "action": row["action"],
            "outcome": row["outcome"],
            "surface": row["surface"],
            "repositoryId": row["repository_id"],
            "requestId": row["request_id"],
            "actor": row["actor"],
            "subjectId": row["subject_id"],
            "details": details,
            "previousHash": row["previous_hash"],
            "eventHash": row["event_hash"],
        }

    def _meta_locked(self) -> dict[str, str]:
        with self.connect() as connection:
            return {str(row["key"]): str(row["value"]) for row in connection.execute("SELECT key,value FROM security_event_meta")}

    def _active_rows_locked(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [self._public_row(row) for row in connection.execute("SELECT * FROM security_events ORDER BY sequence")]

    def _active_digest_locked(self, meta: dict[str, str] | None = None, rows: list[dict[str, Any]] | None = None) -> str:
        selected_meta = meta if meta is not None else self._meta_locked()
        selected_rows = rows if rows is not None else self._active_rows_locked()
        payload = {
            "ledgerId": selected_meta.get("ledger_id", ""),
            "activeStartSequence": selected_meta.get("active_start_sequence", ""),
            "activePreviousEventHash": selected_meta.get("active_previous_event_hash", ""),
            "previousSegmentHash": selected_meta.get("previous_segment_hash", ""),
            "retentionRootSha256": selected_meta.get("retention_root_sha256", ""),
            "events": selected_rows,
        }
        return _sha256_bytes(_canonical_bytes(payload))

    def _load_retention_root_locked(self) -> dict[str, Any] | None:
        if not self.retention_root_path.exists():
            return None
        raw = self.retention_root_path.read_bytes()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecurityEventError("Security retention checkpoint is unreadable.") from exc
        if payload.get("format") != "forgetrace-security-retention-root" or int(payload.get("schemaVersion", 0)) != 1:
            raise SecurityEventError("Security retention checkpoint format is invalid.")
        payload = dict(payload)
        payload["fileSha256"] = _sha256_bytes(raw)
        return payload

    def _segment_paths_locked(self) -> list[Path]:
        return sorted(self.segments_dir.glob("segment_*.json"))

    def _load_segment_locked(self, path: Path, *, verify_events: bool = True) -> dict[str, Any]:
        raw = path.read_bytes()
        file_hash = _sha256_bytes(raw)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecurityEventError(f"Security segment is unreadable: {path.name}") from exc
        if payload.get("format") != "forgetrace-security-event-segment":
            raise SecurityEventError(f"Security segment format is invalid: {path.name}")
        if int(payload.get("segmentSchemaVersion", 0)) != SECURITY_SEGMENT_SCHEMA_VERSION:
            raise SecurityEventError(f"Security segment schema is unsupported: {path.name}")
        events = payload.get("events")
        if not isinstance(events, list) or not events:
            raise SecurityEventError(f"Security segment contains no valid event list: {path.name}")
        if int(payload.get("eventCount", -1)) != len(events):
            raise SecurityEventError(f"Security segment event count does not match: {path.name}")
        if int(payload.get("firstSequence", -1)) != int(events[0].get("sequence", -2)):
            raise SecurityEventError(f"Security segment first sequence does not match: {path.name}")
        if int(payload.get("lastSequence", -1)) != int(events[-1].get("sequence", -2)):
            raise SecurityEventError(f"Security segment last sequence does not match: {path.name}")
        if str(payload.get("startingPreviousEventHash") or "") != str(events[0].get("previousHash") or ""):
            raise SecurityEventError(f"Security segment starting hash does not match: {path.name}")
        if str(payload.get("finalEventHash") or "") != str(events[-1].get("eventHash") or ""):
            raise SecurityEventError(f"Security segment final hash does not match: {path.name}")
        if verify_events:
            expected_sequence = int(payload["firstSequence"])
            previous_hash = str(payload["startingPreviousEventHash"])
            for event in events:
                self._verify_public_event(event, expected_sequence=expected_sequence, previous_hash=previous_hash)
                previous_hash = str(event["eventHash"])
                expected_sequence += 1
        result = dict(payload)
        result["fileSha256"] = file_hash
        result["bytes"] = len(raw)
        result["path"] = str(path)
        return result

    def _segment_summaries_locked(self, *, verify_events: bool = True) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for path in self._segment_paths_locked():
            segment = self._load_segment_locked(path, verify_events=verify_events)
            summaries.append({key: segment.get(key) for key in (
                "segmentId", "createdAt", "ledgerId", "firstSequence", "lastSequence",
                "firstOccurredAt", "lastOccurredAt", "eventCount", "eventBytes",
                "startingPreviousEventHash", "finalEventHash", "previousSegmentHash",
                "fileSha256", "bytes", "path",
            )})
        summaries.sort(key=lambda item: int(item["firstSequence"]))
        return summaries

    @classmethod
    def _verify_public_event(cls, event: dict[str, Any], *, expected_sequence: int, previous_hash: str) -> None:
        sequence = int(event.get("sequence", -1))
        if sequence != expected_sequence:
            raise SecurityEventError(f"Security event sequence gap at {sequence}; expected {expected_sequence}.")
        if str(event.get("previousHash") or "") != previous_hash:
            raise SecurityEventError(f"Security event previous hash mismatch at sequence {sequence}.")
        details_json = cls._details_json(event.get("details", {}))
        expected_hash = cls._event_hash(
            sequence=sequence,
            event_id=str(event.get("eventId") or ""),
            occurred_at=str(event.get("occurredAt") or ""),
            category=str(event.get("category") or ""),
            severity=str(event.get("severity") or ""),
            action=str(event.get("action") or ""),
            outcome=str(event.get("outcome") or ""),
            surface=str(event.get("surface") or ""),
            repository_id=str(event.get("repositoryId") or ""),
            request_id=str(event.get("requestId") or ""),
            actor=str(event.get("actor") or ""),
            subject_id=str(event.get("subjectId") or ""),
            details_json=details_json,
            previous_hash=previous_hash,
        )
        if str(event.get("eventHash") or "") != expected_hash:
            raise SecurityEventError(f"Security event hash mismatch at sequence {sequence}.")

    def append(
        self,
        *,
        category: str,
        action: str,
        outcome: str,
        severity: str = "info",
        surface: str = "system",
        repository_id: str = "",
        request_id: str = "",
        actor: str = "",
        subject_id: str = "",
        details: Any = None,
        occurred_at: str = "",
    ) -> dict[str, Any]:
        category = self._clean_identifier(category, label="category")
        action = self._clean_identifier(action, label="action")
        outcome = self._clean_identifier(outcome, label="outcome")
        surface = self._clean_identifier(surface, label="surface")
        severity = self._clean_identifier(severity, label="severity")
        if severity not in _ALLOWED_SEVERITIES:
            raise SecurityEventError("Invalid severity for security event ledger.")
        repository_id = self._clean_text(repository_id, maximum=128)
        request_id = self._clean_text(request_id, maximum=128)
        actor = self._clean_text(actor, maximum=160)
        subject_id = self._clean_text(subject_id, maximum=160)
        occurred_at = self._clean_text(occurred_at, maximum=64) or utc_now()
        sanitized = self.sanitize_details(details if details is not None else {})
        details_json = self._details_json(sanitized)
        event_id = "sev_" + uuid.uuid4().hex

        with self.lock:
            if self._compromised:
                raise SecurityEventError("Security event ledger integrity verification failed; append is blocked.")
            connection = sqlite3.connect(self.db_path, timeout=30.0)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    "SELECT sequence,event_hash FROM security_events ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                if previous:
                    sequence = int(previous["sequence"]) + 1
                    previous_hash = str(previous["event_hash"])
                else:
                    meta = {str(row[0]): str(row[1]) for row in connection.execute("SELECT key,value FROM security_event_meta")}
                    sequence = int(meta.get("active_start_sequence", "1"))
                    previous_hash = meta.get("active_previous_event_hash", GENESIS_HASH)
                event_hash = self._event_hash(
                    sequence=sequence, event_id=event_id, occurred_at=occurred_at,
                    category=category, severity=severity, action=action, outcome=outcome,
                    surface=surface, repository_id=repository_id, request_id=request_id,
                    actor=actor, subject_id=subject_id, details_json=details_json,
                    previous_hash=previous_hash,
                )
                connection.execute(
                    """
                    INSERT INTO security_events(
                        sequence,event_id,occurred_at,category,severity,action,outcome,
                        surface,repository_id,request_id,actor,subject_id,details_json,
                        previous_hash,event_hash
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (sequence,event_id,occurred_at,category,severity,action,outcome,surface,
                     repository_id,request_id,actor,subject_id,details_json,previous_hash,event_hash),
                )
                connection.commit()
                row = connection.execute("SELECT * FROM security_events WHERE sequence=?", (sequence,)).fetchone()
                return self._public_row(row)
            except (sqlite3.Error, OSError) as exc:
                connection.rollback()
                raise SecurityEventError(f"Could not durably append security event: {exc}") from exc
            finally:
                connection.close()

    def assert_writable(self) -> None:
        result = self.verify_integrity()
        if not result["healthy"]:
            self._compromised = True
            raise SecurityEventError("Security event ledger integrity verification failed; protected action is blocked.")
        with self.lock:
            connection = sqlite3.connect(self.db_path, timeout=30.0)
            try:
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            except (sqlite3.Error, OSError) as exc:
                raise SecurityEventError(f"Security event ledger is not writable: {exc}") from exc
            finally:
                connection.close()

    def verify_integrity(self) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        retained_checked = 0
        segment_checked = 0
        active_checked = 0
        expected_sequence = 1
        last_event_hash = GENESIS_HASH
        previous_segment_hash = GENESIS_HASH
        segment_summaries: list[dict[str, Any]] = []
        meta: dict[str, str] = {}
        retention_root: dict[str, Any] | None = None
        with self.lock:
            try:
                meta = self._meta_locked()
                ledger_id = meta.get("ledger_id", "")
                if not ledger_id:
                    issues.append({"code": "ledger_id_missing"})
                try:
                    retention_root = self._load_retention_root_locked()
                    expected_root_hash = meta.get("retention_root_sha256", "")
                    actual_root_hash = retention_root.get("fileSha256", "") if retention_root else ""
                    if expected_root_hash != actual_root_hash:
                        issues.append({"code": "retention_root_hash_mismatch"})
                    if retention_root:
                        if retention_root.get("ledgerId") != ledger_id:
                            issues.append({"code": "retention_root_ledger_mismatch"})
                        expected_sequence = int(retention_root.get("lastDeletedSequence", 0)) + 1
                        last_event_hash = str(retention_root.get("lastDeletedEventHash") or GENESIS_HASH)
                        previous_segment_hash = str(retention_root.get("lastDeletedSegmentHash") or GENESIS_HASH)
                except (SecurityEventError, OSError, ValueError, TypeError) as exc:
                    issues.append({"code": "retention_root_invalid", "message": str(exc)})

                try:
                    segment_summaries = self._segment_summaries_locked(verify_events=True)
                    for segment in segment_summaries:
                        if str(segment.get("ledgerId") or "") != ledger_id:
                            issues.append({"code": "segment_ledger_mismatch", "segmentId": segment.get("segmentId")})
                            break
                        if int(segment["firstSequence"]) != expected_sequence:
                            issues.append({"code": "segment_sequence_gap", "segmentId": segment.get("segmentId"), "expectedSequence": expected_sequence})
                            break
                        if str(segment.get("previousSegmentHash") or "") != previous_segment_hash:
                            issues.append({"code": "segment_chain_hash_mismatch", "segmentId": segment.get("segmentId")})
                            break
                        if str(segment.get("startingPreviousEventHash") or "") != last_event_hash:
                            issues.append({"code": "segment_event_chain_mismatch", "segmentId": segment.get("segmentId")})
                            break
                        expected_sequence = int(segment["lastSequence"]) + 1
                        last_event_hash = str(segment["finalEventHash"])
                        previous_segment_hash = str(segment["fileSha256"])
                        segment_checked += int(segment["eventCount"])
                except (SecurityEventError, OSError, ValueError, TypeError) as exc:
                    issues.append({"code": "segment_integrity_failed", "message": str(exc)})

                try:
                    with self.connect() as connection:
                        sqlite_integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                        if sqlite_integrity.lower() != "ok":
                            issues.append({"code": "sqlite_integrity_failed", "message": sqlite_integrity})
                        schema_row = connection.execute("SELECT value FROM security_event_meta WHERE key='schema_version'").fetchone()
                        if schema_row is None or str(schema_row["value"]) != str(SECURITY_EVENT_SCHEMA_VERSION):
                            issues.append({"code": "schema_version_mismatch", "expected": SECURITY_EVENT_SCHEMA_VERSION, "actual": str(schema_row["value"]) if schema_row else "missing"})
                        triggers = {str(row["name"]) for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('security_events','security_event_meta')"
                        )}
                        required = {"security_events_no_update","security_events_no_delete","security_event_meta_no_update","security_event_meta_no_delete"}
                        for trigger in required - triggers:
                            issues.append({"code": "immutability_trigger_missing", "trigger": trigger})
                        if int(meta.get("active_start_sequence", "-1")) != expected_sequence:
                            issues.append({"code": "active_start_sequence_mismatch", "expectedSequence": expected_sequence, "actualSequence": meta.get("active_start_sequence")})
                        if meta.get("active_previous_event_hash", "") != last_event_hash:
                            issues.append({"code": "active_previous_hash_mismatch"})
                        if meta.get("previous_segment_hash", "") != previous_segment_hash:
                            issues.append({"code": "active_segment_hash_mismatch"})
                        rows = connection.execute("SELECT * FROM security_events ORDER BY sequence")
                        for row in rows:
                            event = self._public_row(row)
                            try:
                                self._verify_public_event(event, expected_sequence=expected_sequence, previous_hash=last_event_hash)
                            except SecurityEventError as exc:
                                issues.append({"code": "active_event_integrity_failed", "sequence": event.get("sequence"), "message": str(exc)})
                                break
                            active_checked += 1
                            expected_sequence += 1
                            last_event_hash = str(event["eventHash"])
                except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
                    issues.append({"code": "ledger_unreadable", "message": str(exc)})
            except Exception as exc:
                issues.append({"code": "ledger_verification_failed", "message": str(exc)})
        retained_checked = segment_checked + active_checked
        healthy = not issues
        self._compromised = not healthy
        deleted_count = int(retention_root.get("lastDeletedSequence", 0)) if retention_root else 0
        return {
            "healthy": healthy,
            "schemaVersion": SECURITY_EVENT_SCHEMA_VERSION,
            "segmentSchemaVersion": SECURITY_SEGMENT_SCHEMA_VERSION,
            "ledgerId": meta.get("ledger_id", ""),
            "eventCount": retained_checked,
            "segmentEventCount": segment_checked,
            "activeEventCount": active_checked,
            "segmentCount": len(segment_summaries),
            "deletedEventCount": deleted_count,
            "retainedStartSequence": deleted_count + 1,
            "lastSequence": expected_sequence - 1,
            "lastHash": last_event_hash,
            "previousSegmentHash": previous_segment_hash,
            "activeDigest": self._active_digest_locked(meta) if meta and self.db_path.exists() else "",
            "retentionRootSha256": retention_root.get("fileSha256", "") if retention_root else "",
            "verifiedAt": utc_now(),
            "issues": issues,
        }

    @staticmethod
    def _bounded_limit(value: Any, *, default: int = 100, maximum: int = 1000) -> int:
        try:
            return max(1, min(int(value), maximum))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _bounded_offset(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    def _retained_events_locked(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for path in self._segment_paths_locked():
            try:
                events.extend(self._load_segment_locked(path, verify_events=False)["events"])
            except (SecurityEventError, OSError):
                continue
        events.extend(self._active_rows_locked())
        events.sort(key=lambda item: int(item.get("sequence", 0)))
        return events

    def query(
        self,
        *,
        category: str = "",
        severity: str = "",
        action: str = "",
        outcome: str = "",
        surface: str = "",
        repository_id: str = "",
        search: str = "",
        since: str = "",
        until: str = "",
        limit: Any = 100,
        offset: Any = 0,
    ) -> dict[str, Any]:
        filters = {
            "category": str(category or "").strip().lower(),
            "severity": str(severity or "").strip().lower(),
            "action": str(action or "").strip().lower(),
            "outcome": str(outcome or "").strip().lower(),
            "surface": str(surface or "").strip().lower(),
            "repositoryId": str(repository_id or "").strip(),
            "search": str(search or "").strip(),
            "since": str(since or "").strip(),
            "until": str(until or "").strip(),
        }
        with self.lock:
            events = self._retained_events_locked()
        def matches(event: dict[str, Any]) -> bool:
            for key in ("category","severity","action","outcome","surface"):
                if filters[key] and str(event.get(key, "")).lower() != filters[key]:
                    return False
            if filters["repositoryId"] and str(event.get("repositoryId", "")) != filters["repositoryId"]:
                return False
            occurred = str(event.get("occurredAt", ""))
            if filters["since"] and occurred < filters["since"]:
                return False
            if filters["until"] and occurred > filters["until"]:
                return False
            if filters["search"]:
                haystack = " ".join([
                    str(event.get("eventId", "")), str(event.get("action", "")),
                    str(event.get("actor", "")), str(event.get("subjectId", "")),
                    str(event.get("requestId", "")), json.dumps(event.get("details", {}), ensure_ascii=False),
                ]).casefold()
                if filters["search"].casefold() not in haystack:
                    return False
            return True
        selected = [event for event in events if matches(event)]
        selected.sort(key=lambda item: int(item["sequence"]), reverse=True)
        limit_value = self._bounded_limit(limit)
        offset_value = self._bounded_offset(offset)
        page = selected[offset_value:offset_value + limit_value]
        facets = {
            "categories": sorted({str(item.get("category", "")) for item in events if item.get("category")}),
            "severities": sorted({str(item.get("severity", "")) for item in events if item.get("severity")}),
            "outcomes": sorted({str(item.get("outcome", "")) for item in events if item.get("outcome")}),
            "surfaces": sorted({str(item.get("surface", "")) for item in events if item.get("surface")}),
        }
        return {
            "events": page,
            "total": len(selected),
            "limit": limit_value,
            "offset": offset_value,
            "hasMore": offset_value + len(page) < len(selected),
            "filters": filters,
            "facets": facets,
            "integrity": self.verify_integrity(),
        }

    def export(self, **filters: Any) -> dict[str, Any]:
        requested = dict(filters)
        requested["limit"] = MAX_EXPORT_EVENTS
        requested["offset"] = 0
        result = self.query(**requested)
        events = list(reversed(result["events"]))
        return {
            "format": "ForgeTrace Security Event Export",
            "schemaVersion": SECURITY_EVENT_SCHEMA_VERSION,
            "generatedAt": utc_now(),
            "integrity": result["integrity"],
            "filters": result["filters"],
            "eventCount": len(events),
            "truncated": result["total"] > len(events),
            "events": events,
        }

    # ---- Retention policy -------------------------------------------------

    @staticmethod
    def _validate_policy(values: dict[str, Any]) -> dict[str, int]:
        merged = dict(DEFAULT_RETENTION_POLICY)
        merged.update({key: values[key] for key in DEFAULT_RETENTION_POLICY if key in values})
        bounds = {
            "maxActiveEvents": (2, 5_000_000),
            "segmentEventTarget": (1, 1_000_000),
            "maxRetainedEvents": (2, 100_000_000),
            "maxRetentionAgeDays": (1, 36_500),
            "maxStorageBytes": (1024, 1024 * 1024 * 1024 * 1024),
            "minimumProtectedEvents": (1, 10_000_000),
            "minimumProtectedAgeDays": (0, 36_500),
        }
        normalized: dict[str, int] = {}
        for key, (minimum, maximum) in bounds.items():
            try:
                number = int(merged[key])
            except (TypeError, ValueError) as exc:
                raise SecurityEventError(f"Security retention policy field {key} must be an integer.") from exc
            if number < minimum or number > maximum:
                raise SecurityEventError(f"Security retention policy field {key} is outside its supported range.")
            normalized[key] = number
        if normalized["segmentEventTarget"] >= normalized["maxActiveEvents"]:
            normalized["segmentEventTarget"] = max(1, normalized["maxActiveEvents"] - 1)
        if normalized["minimumProtectedEvents"] > normalized["maxRetainedEvents"]:
            raise SecurityEventError("The protected event minimum cannot exceed the retained-event budget.")
        return normalized

    @staticmethod
    def _policy_hash(payload: dict[str, Any]) -> str:
        material = {key: payload[key] for key in DEFAULT_RETENTION_POLICY}
        material["schemaVersion"] = SECURITY_RETENTION_POLICY_SCHEMA_VERSION
        return _sha256_bytes(_canonical_bytes(material))

    def _ensure_policy_locked(self) -> None:
        if self.policy_path.exists():
            return
        payload: dict[str, Any] = {
            "format": "forgetrace-security-retention-policy",
            "schemaVersion": SECURITY_RETENTION_POLICY_SCHEMA_VERSION,
            "updatedAt": utc_now(),
            **DEFAULT_RETENTION_POLICY,
        }
        payload["policyHash"] = self._policy_hash(payload)
        _atomic_write_json(self.policy_path, payload)

    def _load_policy_locked(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SecurityEventError("Security retention policy is unreadable.") from exc
        if payload.get("format") != "forgetrace-security-retention-policy":
            raise SecurityEventError("Security retention policy format is invalid.")
        if int(payload.get("schemaVersion", 0)) != SECURITY_RETENTION_POLICY_SCHEMA_VERSION:
            raise SecurityEventError("Security retention policy schema is unsupported.")
        normalized = self._validate_policy(payload)
        expected_hash = self._policy_hash({**payload, **normalized})
        if str(payload.get("policyHash") or "") != expected_hash:
            raise SecurityEventError("Security retention policy hash verification failed.")
        return {**payload, **normalized, "policyHash": expected_hash}

    def get_retention_policy(self) -> dict[str, Any]:
        with self.lock:
            return self._load_policy_locked()

    def update_retention_policy(
        self,
        values: dict[str, Any],
        *,
        request_id: str = "",
        actor: str = "owner",
    ) -> dict[str, Any]:
        with self.lock:
            self.assert_writable()
            current = self._load_policy_locked()
            normalized = self._validate_policy({**current, **dict(values or {})})
            self.append(
                category="security", action="security_retention_policy_change_authorized",
                outcome="authorized", severity="warning", surface="owner",
                request_id=request_id, actor=actor,
                details={"changedFields": sorted(key for key in normalized if normalized[key] != current.get(key))},
            )
            payload: dict[str, Any] = {
                "format": "forgetrace-security-retention-policy",
                "schemaVersion": SECURITY_RETENTION_POLICY_SCHEMA_VERSION,
                "updatedAt": utc_now(),
                **normalized,
            }
            payload["policyHash"] = self._policy_hash(payload)
            _atomic_write_json(self.policy_path, payload)
            self.append(
                category="security", action="security_retention_policy_changed",
                outcome="success", severity="warning", surface="owner",
                request_id=request_id, actor=actor,
                details={key: payload[key] for key in DEFAULT_RETENTION_POLICY},
            )
            return payload

    # ---- Rotation and retention -----------------------------------------

    def _retention_candidates_locked(
        self,
        segments: list[dict[str, Any]],
        *,
        total_events: int,
        total_bytes: int,
        policy: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        remaining = list(segments)
        removed: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        minimum_cutoff = now - timedelta(days=int(policy["minimumProtectedAgeDays"]))
        maximum_cutoff = now - timedelta(days=int(policy["maxRetentionAgeDays"]))
        current_events = total_events
        current_bytes = total_bytes

        def pressure() -> dict[str, bool]:
            oldest = remaining[0] if remaining else None
            too_old = False
            if oldest:
                try:
                    too_old = _parse_utc(str(oldest.get("lastOccurredAt") or "")) < maximum_cutoff
                except (ValueError, TypeError):
                    too_old = True
            return {
                "eventBudgetExceeded": current_events > int(policy["maxRetainedEvents"]),
                "storageBudgetExceeded": current_bytes > int(policy["maxStorageBytes"]),
                "ageBudgetExceeded": too_old,
            }

        while remaining and any(pressure().values()):
            candidate = remaining[0]
            after_events = current_events - int(candidate.get("eventCount") or 0)
            try:
                old_enough = _parse_utc(str(candidate.get("lastOccurredAt") or "")) < minimum_cutoff
            except (ValueError, TypeError):
                old_enough = False
            if after_events < int(policy["minimumProtectedEvents"]) or not old_enough:
                break
            remaining.pop(0)
            removed.append(candidate)
            current_events = after_events
            current_bytes -= int(candidate.get("bytes") or 0)
        remaining_pressure = pressure()
        return removed, {
            **remaining_pressure,
            "pressure": any(remaining_pressure.values()),
            "retainedEventCount": current_events,
            "retainedSegmentBytes": current_bytes,
        }

    def preview_rotation(self, *, rotate_count: Any = None) -> dict[str, Any]:
        with self.lock:
            integrity = self.verify_integrity()
            if not integrity["healthy"]:
                raise SecurityEventError("Security history must verify before rotation can be previewed.")
            policy = self._load_policy_locked()
            incomplete = self._incomplete_rotation_journals_locked()
            if incomplete:
                raise SecurityEventError("An incomplete security rotation journal must be recovered before another preview.")
            if not os.access(self.rotations_dir, os.W_OK) or not os.access(self.segments_dir, os.W_OK):
                raise SecurityEventError("Security rotation storage is not writable.")
            rows = self._active_rows_locked()
            active_count = len(rows)
            if rotate_count in (None, ""):
                count = max(0, active_count - int(policy["maxActiveEvents"]))
                count = min(count, int(policy["segmentEventTarget"]))
            else:
                try:
                    count = int(rotate_count)
                except (TypeError, ValueError) as exc:
                    raise SecurityEventError("Rotation event count must be an integer.") from exc
            count = max(0, min(count, MAX_ROTATION_EVENTS, max(0, active_count - 1)))
            selected = rows[:count]
            segments = self._segment_summaries_locked(verify_events=True)
            selected_bytes = sum(len(_canonical_bytes(item)) for item in selected)
            estimated_segment_bytes = selected_bytes + 2048
            active_db_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
            total_retained_events = int(integrity["eventCount"])
            total_segment_bytes = sum(int(item.get("bytes") or 0) for item in segments) + estimated_segment_bytes
            prune, retention = self._retention_candidates_locked(
                segments,
                total_events=total_retained_events,
                total_bytes=total_segment_bytes,
                policy=policy,
            )
            required_free = max(MIN_ROTATION_FREE_BYTES, active_db_bytes * 2 + estimated_segment_bytes)
            try:
                available_free = shutil.disk_usage(self.data_dir).free
            except OSError as exc:
                raise SecurityEventError(f"Security rotation destination space cannot be verified: {exc}") from exc
            state = {
                "ledgerId": integrity.get("ledgerId", ""),
                "lastSequence": integrity.get("lastSequence", 0),
                "lastHash": integrity.get("lastHash", ""),
                "activeDigest": integrity.get("activeDigest", ""),
                "previousSegmentHash": integrity.get("previousSegmentHash", ""),
                "retentionRootSha256": integrity.get("retentionRootSha256", ""),
                "policyHash": policy["policyHash"],
                "rotateCount": count,
                "eventIds": [item["eventId"] for item in selected],
                "pruneSegmentIds": [item["segmentId"] for item in prune],
            }
            preview_id = "rotprev_" + _sha256_bytes(_canonical_bytes(state))
            return {
                "format": "forgetrace-security-rotation-preview",
                "previewId": preview_id,
                "generatedAt": utc_now(),
                "canRotate": count > 0 and available_free >= required_free,
                "rotateCount": count,
                "firstSequence": int(selected[0]["sequence"]) if selected else None,
                "lastSequence": int(selected[-1]["sequence"]) if selected else None,
                "eventIds": [item["eventId"] for item in selected],
                "activeEventCountBefore": active_count,
                "activeEventCountAfter": active_count - count,
                "estimatedSegmentBytes": estimated_segment_bytes,
                "requiredFreeBytes": required_free,
                "availableFreeBytes": available_free,
                "spaceAvailable": available_free >= required_free,
                "retention": {
                    **retention,
                    "pruneSegmentIds": [item["segmentId"] for item in prune],
                    "pruneSequenceRanges": [[item["firstSequence"], item["lastSequence"]] for item in prune],
                    "protectedMinimumEvents": policy["minimumProtectedEvents"],
                    "protectedMinimumAgeDays": policy["minimumProtectedAgeDays"],
                },
                "policy": policy,
                "binding": state,
                "integrity": integrity,
            }

    def _segment_payload_locked(self, rows: list[dict[str, Any]], *, previous_segment_hash: str, ledger_id: str) -> dict[str, Any]:
        if not rows:
            raise SecurityEventError("A security segment cannot be empty.")
        return {
            "format": "forgetrace-security-event-segment",
            "segmentSchemaVersion": SECURITY_SEGMENT_SCHEMA_VERSION,
            "segmentId": "seg_" + uuid.uuid4().hex,
            "ledgerId": ledger_id,
            "createdAt": utc_now(),
            "previousSegmentHash": previous_segment_hash,
            "startingPreviousEventHash": rows[0]["previousHash"],
            "firstSequence": rows[0]["sequence"],
            "lastSequence": rows[-1]["sequence"],
            "firstOccurredAt": rows[0]["occurredAt"],
            "lastOccurredAt": rows[-1]["occurredAt"],
            "eventCount": len(rows),
            "eventBytes": sum(len(_canonical_bytes(item)) for item in rows),
            "finalEventHash": rows[-1]["eventHash"],
            "events": rows,
        }

    def _checkpoint_payload_locked(self, removed: list[dict[str, Any]], old_root: dict[str, Any] | None, ledger_id: str) -> dict[str, Any] | None:
        if not removed:
            return None
        last = removed[-1]
        return {
            "format": "forgetrace-security-retention-root",
            "schemaVersion": 1,
            "ledgerId": ledger_id,
            "updatedAt": utc_now(),
            "lastDeletedSequence": int(last["lastSequence"]),
            "lastDeletedEventHash": str(last["finalEventHash"]),
            "lastDeletedSegmentHash": str(last["fileSha256"]),
            "deletedSegmentCount": int((old_root or {}).get("deletedSegmentCount", 0)) + len(removed),
            "priorRootSha256": str((old_root or {}).get("fileSha256", "")),
        }

    def _backup_active_locked(self, destination: Path) -> None:
        source = sqlite3.connect(self.db_path, timeout=30.0)
        backup = sqlite3.connect(destination, timeout=30.0)
        try:
            source.execute("PRAGMA wal_checkpoint(FULL)")
            source.backup(backup)
            backup.commit()
        finally:
            backup.close()
            source.close()
        with destination.open("rb") as handle:
            os.fsync(handle.fileno())

    def _checkpoint_active_locked(self) -> None:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

    def _confined_rotation_path(self, value: Any, *, allow_missing: bool = True) -> Path:
        path = Path(str(value or "")).expanduser().resolve()
        root = self.rotations_dir.resolve()
        if path != root and root not in path.parents:
            raise SecurityEventError("Security rotation journal references a path outside rotation storage.")
        if not allow_missing and not path.exists():
            raise SecurityEventError("Security rotation journal recovery artifact is missing.")
        return path

    @staticmethod
    def _journal_hash(payload: dict[str, Any]) -> str:
        material = dict(payload)
        material.pop("journalHash", None)
        return _sha256_bytes(_canonical_bytes(material))

    def _journal_write_locked(self, path: Path, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        payload["updatedAt"] = utc_now()
        payload["journalHash"] = self._journal_hash(payload)
        _atomic_write_json(path, payload)

    def _load_rotation_journal_locked(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SecurityEventError("Security rotation journal is unreadable.") from exc
        supplied = str(payload.get("journalHash") or "")
        if not supplied or supplied != self._journal_hash(payload):
            raise SecurityEventError("Security rotation journal hash verification failed.")
        if payload.get("format") != "forgetrace-security-rotation-journal":
            raise SecurityEventError("Security rotation journal format is invalid.")
        if int(payload.get("schemaVersion", 0)) != SECURITY_ROTATION_JOURNAL_SCHEMA_VERSION:
            raise SecurityEventError("Security rotation journal schema is unsupported.")
        return payload

    def execute_rotation(
        self,
        *,
        preview_id: str,
        rotate_count: Any,
        request_id: str = "",
        actor: str = "owner",
    ) -> dict[str, Any]:
        with self.lock:
            preview = self.preview_rotation(rotate_count=rotate_count)
            if str(preview_id or "") != preview["previewId"]:
                raise SecurityEventError("Security rotation preview is stale; generate a new preview.")
            if not preview["canRotate"]:
                if not preview["spaceAvailable"]:
                    raise SecurityEventError("Security rotation destination does not have verified free space.")
                raise SecurityEventError("Security rotation preview contains no rotatable events.")
            self.assert_writable()
            self.append(
                category="security", action="security_rotation_authorized", outcome="authorized",
                severity="warning", surface="owner", request_id=request_id, actor=actor,
                subject_id=preview["previewId"],
                details={"rotateCount": preview["rotateCount"], "firstSequence": preview["firstSequence"], "lastSequence": preview["lastSequence"], "pruneSegmentIds": preview["retention"]["pruneSegmentIds"]},
            )
            integrity = self.verify_integrity()
            if not integrity["healthy"]:
                raise SecurityEventError("Security history changed unexpectedly after rotation authorization.")
            meta = self._meta_locked()
            rows = self._active_rows_locked()
            count = int(preview["rotateCount"])
            rotated_rows = rows[:count]
            remaining_rows = rows[count:]
            if [item["eventId"] for item in rotated_rows] != preview["eventIds"]:
                raise SecurityEventError("Security rotation rows changed after preview authorization.")
            existing_segments = self._segment_summaries_locked(verify_events=True)
            previous_segment_hash = existing_segments[-1]["fileSha256"] if existing_segments else (
                self._load_retention_root_locked() or {}
            ).get("lastDeletedSegmentHash", GENESIS_HASH)
            segment_payload = self._segment_payload_locked(rotated_rows, previous_segment_hash=str(previous_segment_hash), ledger_id=meta["ledger_id"])
            segment_bytes = _canonical_bytes(segment_payload)
            segment_sha = _sha256_bytes(segment_bytes)
            segment_name = f"segment_{int(segment_payload['firstSequence']):020d}_{int(segment_payload['lastSequence']):020d}_{segment_payload['segmentId']}.json"
            segment_final = self.segments_dir / segment_name

            prune_ids = set(preview["retention"]["pruneSegmentIds"])
            pruned = [item for item in existing_segments if item["segmentId"] in prune_ids]
            old_root = self._load_retention_root_locked()
            new_root_payload = self._checkpoint_payload_locked(pruned, old_root, meta["ledger_id"])
            new_root_bytes = _canonical_bytes(new_root_payload) if new_root_payload else None
            new_root_sha = _sha256_bytes(new_root_bytes) if new_root_bytes else str((old_root or {}).get("fileSha256", ""))

            rotation_id = "rotation_" + uuid.uuid4().hex
            operation_dir = self.rotations_dir / rotation_id
            operation_dir.mkdir(parents=True, exist_ok=False)
            journal_path = self.rotations_dir / f"{rotation_id}.json"
            backup_db = operation_dir / "active-before.sqlite3"
            staged_db = operation_dir / "active-after.sqlite3"
            segment_staged = operation_dir / segment_name
            root_staged = operation_dir / "retention-root-after.json"
            root_before = operation_dir / "retention-root-before.json"
            pruned_dir = operation_dir / "pruned-segments"
            pruned_dir.mkdir(parents=True, exist_ok=True)
            self._backup_active_locked(backup_db)
            _atomic_write_bytes(segment_staged, segment_bytes)
            if new_root_bytes is not None:
                _atomic_write_bytes(root_staged, new_root_bytes)
            if self.retention_root_path.exists():
                shutil.copy2(self.retention_root_path, root_before)
            for item in pruned:
                source = Path(str(item["path"]))
                shutil.copy2(source, pruned_dir / source.name)
            self._create_active_database(
                staged_db,
                rows=remaining_rows,
                ledger_id=meta["ledger_id"],
                active_start_sequence=int(segment_payload["lastSequence"]) + 1,
                active_previous_event_hash=str(segment_payload["finalEventHash"]),
                previous_segment_hash=segment_sha,
                retention_root_sha256=new_root_sha,
            )
            journal = {
                "format": "forgetrace-security-rotation-journal",
                "schemaVersion": SECURITY_ROTATION_JOURNAL_SCHEMA_VERSION,
                "rotationId": rotation_id,
                "createdAt": utc_now(),
                "state": "prepared",
                "previewId": preview["previewId"],
                "requestId": str(request_id or "")[:120],
                "activeBackup": str(backup_db),
                "stagedActive": str(staged_db),
                "segmentStaged": str(segment_staged),
                "segmentFinal": str(segment_final),
                "segmentSha256": segment_sha,
                "prunedSegments": [Path(str(item["path"])).name for item in pruned],
                "prunedBackupDir": str(pruned_dir),
                "oldRootExisted": self.retention_root_path.exists(),
                "rootBefore": str(root_before),
                "rootAfter": str(root_staged) if new_root_bytes is not None else "",
                "oldActiveDigest": preview["integrity"]["activeDigest"],
                "rotatedFirstSequence": segment_payload["firstSequence"],
                "rotatedLastSequence": segment_payload["lastSequence"],
            }
            self._journal_write_locked(journal_path, journal)
            try:
                journal["state"] = "installing"
                self._journal_write_locked(journal_path, journal)
                os.replace(segment_staged, segment_final)
                _fsync_directory(self.segments_dir)
                for item in pruned:
                    Path(str(item["path"])).unlink()
                if new_root_bytes is not None:
                    os.replace(root_staged, self.retention_root_path)
                    _fsync_directory(self.segments_dir)
                self._checkpoint_active_locked()
                for suffix in ("-wal", "-shm"):
                    Path(str(self.db_path) + suffix).unlink(missing_ok=True)
                os.replace(staged_db, self.db_path)
                _fsync_directory(self.data_dir)
                journal["state"] = "installed"
                self._journal_write_locked(journal_path, journal)
                verified = self.verify_integrity()
                if not verified["healthy"]:
                    raise SecurityEventError("Installed security rotation failed full chain verification.")
                journal["state"] = "completed"
                journal["completedAt"] = utc_now()
                journal["segmentId"] = segment_payload["segmentId"]
                journal["segmentSha256"] = segment_sha
                journal["prunedCount"] = len(pruned)
                self._journal_write_locked(journal_path, journal)
                shutil.rmtree(operation_dir, ignore_errors=True)
                self._compromised = False
                self.append(
                    category="security", action="security_rotation_completed", outcome="success",
                    severity="warning", surface="owner", request_id=request_id, actor=actor,
                    subject_id=segment_payload["segmentId"],
                    details={"rotationId": rotation_id, "segmentSha256": segment_sha, "eventCount": count, "firstSequence": segment_payload["firstSequence"], "lastSequence": segment_payload["lastSequence"], "prunedSegmentCount": len(pruned)},
                )
                self._prune_completed_rotation_journals_locked()
                return {
                    "rotationId": rotation_id,
                    "state": "completed",
                    "segment": {"segmentId": segment_payload["segmentId"], "firstSequence": segment_payload["firstSequence"], "lastSequence": segment_payload["lastSequence"], "eventCount": count, "sha256": segment_sha, "bytes": len(segment_bytes)},
                    "prunedSegmentCount": len(pruned),
                    "integrity": self.verify_integrity(),
                }
            except Exception as exc:
                rollback_error = ""
                try:
                    self._rollback_rotation_locked(journal)
                    journal["state"] = "rolled_back"
                    journal["rolledBackAt"] = utc_now()
                except Exception as rollback_exc:
                    rollback_error = str(rollback_exc)
                    journal["state"] = "recovery_failed"
                journal["error"] = str(exc)
                journal["rollbackError"] = rollback_error
                self._journal_write_locked(journal_path, journal)
                self._compromised = not self.verify_integrity()["healthy"]
                raise SecurityEventError(f"Security rotation failed and was {'not ' if rollback_error else ''}rolled back: {exc}") from exc

    def _rollback_rotation_locked(self, journal: dict[str, Any]) -> None:
        backup_db = self._confined_rotation_path(journal.get("activeBackup"), allow_missing=False)
        if not backup_db.is_file():
            raise SecurityEventError("Security rotation active-ledger backup is missing.")
        restore_tmp = self.db_path.with_name(f".{self.db_path.name}.rotation-restore.tmp")
        shutil.copy2(backup_db, restore_tmp)
        for suffix in ("-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)
        os.replace(restore_tmp, self.db_path)
        segment_final = Path(str(journal.get("segmentFinal") or ""))
        if segment_final.parent == self.segments_dir:
            segment_final.unlink(missing_ok=True)
        pruned_dir = self._confined_rotation_path(journal.get("prunedBackupDir"))
        if pruned_dir.is_dir():
            for source in pruned_dir.glob("segment_*.json"):
                shutil.copy2(source, self.segments_dir / source.name)
        if journal.get("oldRootExisted"):
            root_before = self._confined_rotation_path(journal.get("rootBefore"), allow_missing=False)
            if not root_before.is_file():
                raise SecurityEventError("Security rotation retention-root backup is missing.")
            shutil.copy2(root_before, self.retention_root_path)
        else:
            self.retention_root_path.unlink(missing_ok=True)
        _fsync_directory(self.data_dir)
        _fsync_directory(self.segments_dir)

    def _recover_rotation_journals_locked(self) -> dict[str, Any]:
        report = {"checked": 0, "rolledBack": 0, "completed": 0, "failed": 0, "actions": []}
        for path in sorted(self.rotations_dir.glob("rotation_*.json")):
            report["checked"] += 1
            try:
                journal = self._load_rotation_journal_locked(path)
            except SecurityEventError as exc:
                report["failed"] += 1
                report["actions"].append({"journal": path.name, "action": "journal_unreadable", "error": str(exc)})
                continue
            state = str(journal.get("state") or "")
            if state == "completed":
                report["completed"] += 1
                continue
            if state == "rolled_back":
                continue
            try:
                self._rollback_rotation_locked(journal)
                journal["state"] = "rolled_back"
                journal["rolledBackAt"] = utc_now()
                journal["recoveryReason"] = "startup_incomplete_rotation"
                self._journal_write_locked(path, journal)
                report["rolledBack"] += 1
                report["actions"].append({"journal": path.name, "action": "rolled_back"})
            except Exception as exc:
                report["failed"] += 1
                report["actions"].append({"journal": path.name, "action": "recovery_failed", "error": str(exc)})
        report["prunedTerminalJournals"] = self._prune_completed_rotation_journals_locked()
        return report

    def _rotation_journal_records_locked(self, *, limit: int | None = 100) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.rotations_dir.glob("rotation_*.json"), reverse=True):
            try:
                payload = self._load_rotation_journal_locked(path)
                records.append({key: payload.get(key) for key in (
                    "rotationId","createdAt","updatedAt","completedAt","rolledBackAt","state","previewId",
                    "rotatedFirstSequence","rotatedLastSequence","segmentId","segmentSha256",
                    "prunedCount","error","rollbackError",
                )})
            except SecurityEventError as exc:
                records.append({"rotationId": path.stem, "state": "journal_unreadable", "error": str(exc)})
            if limit is not None and len(records) >= max(0, int(limit)):
                break
        return records

    def _incomplete_rotation_journals_locked(self) -> list[dict[str, Any]]:
        return [
            item for item in self._rotation_journal_records_locked(limit=None)
            if item.get("state") not in {"completed", "rolled_back"}
        ]

    def _prune_completed_rotation_journals_locked(self, *, maximum: int = 100) -> int:
        terminal: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(self.rotations_dir.glob("rotation_*.json"), reverse=True):
            try:
                payload = self._load_rotation_journal_locked(path)
            except SecurityEventError:
                continue
            if payload.get("state") in {"completed", "rolled_back"}:
                terminal.append((path, payload))
        removed = 0
        for path, payload in terminal[max(0, int(maximum)):]:
            rotation_id = str(payload.get("rotationId") or "")
            operation_dir = self.rotations_dir / rotation_id
            try:
                operation_dir.relative_to(self.rotations_dir)
            except ValueError:
                operation_dir = self.rotations_dir / "__invalid__"
            if operation_dir.parent == self.rotations_dir:
                shutil.rmtree(operation_dir, ignore_errors=True)
            path.unlink(missing_ok=True)
            removed += 1
        if removed:
            _fsync_directory(self.rotations_dir)
        return removed

    def list_rotation_journals(self) -> list[dict[str, Any]]:
        with self.lock:
            return self._rotation_journal_records_locked(limit=100)

    # ---- Owner-controlled anchoring -------------------------------------

    def _anchor_request_path(self, anchor_id: str) -> Path:
        if not re.fullmatch(r"anchor_[0-9a-f]{32}", str(anchor_id or "")):
            raise SecurityEventError("Invalid security anchor identifier.")
        return self.anchors_dir / f"{anchor_id}.request.json"

    def _anchor_receipt_path(self, anchor_id: str) -> Path:
        return self.anchors_dir / f"{anchor_id}.receipt.json"

    @staticmethod
    def _anchor_digest(payload: dict[str, Any]) -> str:
        target = {
            "ledgerId": payload["ledgerId"],
            "lastSequence": payload["lastSequence"],
            "lastEventHash": payload["lastEventHash"],
            "previousSegmentHash": payload["previousSegmentHash"],
            "activeDigest": payload["activeDigest"],
            "retentionRootSha256": payload["retentionRootSha256"],
            "segmentHashes": payload["segmentHashes"],
        }
        return _sha256_bytes(_canonical_bytes(target))

    def create_anchor_request(self, *, request_id: str = "", actor: str = "owner") -> dict[str, Any]:
        with self.lock:
            self.assert_writable()
            self.append(
                category="security", action="security_anchor_export_authorized", outcome="authorized",
                severity="warning", surface="owner", request_id=request_id, actor=actor,
            )
            integrity = self.verify_integrity()
            if not integrity["healthy"]:
                raise SecurityEventError("Security history must verify before an anchor request can be created.")
            segments = self._segment_summaries_locked(verify_events=True)
            anchor_id = "anchor_" + uuid.uuid4().hex
            payload: dict[str, Any] = {
                "format": "forgetrace-security-anchor-request",
                "schemaVersion": SECURITY_ANCHOR_SCHEMA_VERSION,
                "anchorId": anchor_id,
                "generatedAt": utc_now(),
                "ledgerId": integrity["ledgerId"],
                "lastSequence": integrity["lastSequence"],
                "lastEventHash": integrity["lastHash"],
                "previousSegmentHash": integrity["previousSegmentHash"],
                "activeDigest": integrity["activeDigest"],
                "retentionRootSha256": integrity["retentionRootSha256"],
                "segmentHashes": [item["fileSha256"] for item in segments],
                "externalPublicationVerified": False,
                "notice": "This local digest request is not proof of external publication. Record owner-supplied external evidence separately.",
            }
            payload["anchorDigest"] = self._anchor_digest(payload)
            material = dict(payload)
            payload["requestHash"] = _sha256_bytes(_canonical_bytes(material))
            _atomic_write_json(self._anchor_request_path(anchor_id), payload)
            self.append(
                category="security", action="security_anchor_request_created", outcome="success",
                surface="owner", request_id=request_id, actor=actor, subject_id=anchor_id,
                details={"anchorDigest": payload["anchorDigest"], "lastSequence": payload["lastSequence"], "segmentCount": len(payload["segmentHashes"]), "externalPublicationVerified": False},
            )
            return payload

    def get_anchor_request(self, anchor_id: str) -> dict[str, Any]:
        path = self._anchor_request_path(anchor_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SecurityEventError("Security anchor request is missing or unreadable.") from exc
        request_hash = str(payload.pop("requestHash", ""))
        if request_hash != _sha256_bytes(_canonical_bytes(payload)):
            raise SecurityEventError("Security anchor request hash verification failed.")
        payload["requestHash"] = request_hash
        if payload.get("anchorDigest") != self._anchor_digest(payload):
            raise SecurityEventError("Security anchor digest verification failed.")
        return payload

    def record_anchor_receipt(
        self,
        anchor_id: str,
        *,
        anchored_digest: str,
        mechanism: str,
        external_reference: str = "",
        evidence: str = "",
        published_at: str = "",
        request_id: str = "",
        actor: str = "owner",
    ) -> dict[str, Any]:
        with self.lock:
            request = self.get_anchor_request(anchor_id)
            if str(anchored_digest or "").strip().lower() != str(request["anchorDigest"]).lower():
                raise SecurityEventError("External receipt digest does not match the exported anchor request.")
            evidence_bytes = str(evidence or "").encode("utf-8")
            if len(evidence_bytes) > MAX_ANCHOR_EVIDENCE_BYTES:
                raise SecurityEventError("External anchor evidence exceeds the 64 KiB limit.")
            self.assert_writable()
            self.append(
                category="security", action="security_anchor_receipt_authorized", outcome="authorized",
                severity="warning", surface="owner", request_id=request_id, actor=actor,
                subject_id=anchor_id, details={"mechanism": self._clean_text(mechanism, maximum=120)},
            )
            payload: dict[str, Any] = {
                "format": "forgetrace-security-anchor-receipt",
                "schemaVersion": SECURITY_ANCHOR_SCHEMA_VERSION,
                "anchorId": anchor_id,
                "recordedAt": utc_now(),
                "publishedAt": self._clean_text(published_at, maximum=64),
                "mechanism": self._clean_text(mechanism, maximum=120) or "owner-supplied",
                "externalReference": self._clean_text(external_reference, maximum=1000),
                "anchoredDigest": request["anchorDigest"],
                "requestHash": request["requestHash"],
                "evidence": str(evidence or ""),
                "evidenceSha256": _sha256_bytes(evidence_bytes),
                "bindingVerified": True,
                "externalPublicationVerified": False,
                "notice": "ForgeTrace verified the local digest binding only. It did not contact or independently verify the external publication mechanism.",
            }
            material = dict(payload)
            payload["receiptHash"] = _sha256_bytes(_canonical_bytes(material))
            _atomic_write_json(self._anchor_receipt_path(anchor_id), payload)
            self.append(
                category="security", action="security_anchor_receipt_recorded", outcome="success",
                severity="warning", surface="owner", request_id=request_id, actor=actor,
                subject_id=anchor_id,
                details={"mechanism": payload["mechanism"], "anchoredDigest": payload["anchoredDigest"], "bindingVerified": True, "externalPublicationVerified": False},
            )
            return payload

    def _load_anchor_receipt_locked(self, anchor_id: str) -> dict[str, Any] | None:
        path = self._anchor_receipt_path(anchor_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SecurityEventError("Security anchor receipt is unreadable.") from exc
        receipt_hash = str(payload.pop("receiptHash", ""))
        if receipt_hash != _sha256_bytes(_canonical_bytes(payload)):
            raise SecurityEventError("Security anchor receipt hash verification failed.")
        payload["receiptHash"] = receipt_hash
        request = self.get_anchor_request(anchor_id)
        if payload.get("requestHash") != request.get("requestHash") or payload.get("anchoredDigest") != request.get("anchorDigest"):
            raise SecurityEventError("Security anchor receipt is not bound to its request.")
        if payload.get("evidenceSha256") != _sha256_bytes(str(payload.get("evidence") or "").encode("utf-8")):
            raise SecurityEventError("Security anchor evidence hash verification failed.")
        return payload

    def list_anchors(self) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        covered_segments: set[str] = set()
        with self.lock:
            for path in sorted(self.anchors_dir.glob("anchor_*.request.json"), reverse=True):
                anchor_id = path.name.removesuffix(".request.json")
                try:
                    request = self.get_anchor_request(anchor_id)
                    receipt = self._load_anchor_receipt_locked(anchor_id)
                    if receipt:
                        covered_segments.update(str(item) for item in request.get("segmentHashes", []))
                    records.append({
                        "anchorId": anchor_id,
                        "generatedAt": request.get("generatedAt", ""),
                        "anchorDigest": request.get("anchorDigest", ""),
                        "lastSequence": request.get("lastSequence", 0),
                        "segmentCount": len(request.get("segmentHashes", [])),
                        "receiptRecorded": receipt is not None,
                        "bindingVerified": bool(receipt and receipt.get("bindingVerified")),
                        "externalPublicationVerified": False,
                        "mechanism": receipt.get("mechanism", "") if receipt else "",
                        "externalReference": receipt.get("externalReference", "") if receipt else "",
                    })
                except SecurityEventError as exc:
                    invalid.append({"anchorId": anchor_id, "code": "anchor_integrity_failed", "message": str(exc)})
            segments = self._segment_summaries_locked(verify_events=True)
        unanchored = [item for item in segments if item["fileSha256"] not in covered_segments]
        return {
            "anchors": records,
            "invalid": invalid,
            "coveredSegmentCount": len(segments) - len(unanchored),
            "unanchoredSegmentCount": len(unanchored),
            "unanchoredSegments": [{"segmentId": item["segmentId"], "sha256": item["fileSha256"], "lastSequence": item["lastSequence"]} for item in unanchored],
        }

    # ---- Operational/health model ---------------------------------------

    def list_segments(self) -> dict[str, Any]:
        with self.lock:
            segments = self._segment_summaries_locked(verify_events=True)
            root = self._load_retention_root_locked()
        anchors = self.list_anchors()
        covered = {
            item["fileSha256"]
            for item in segments
            if not any(unanchored["sha256"] == item["fileSha256"] for unanchored in anchors["unanchoredSegments"])
        }
        return {
            "segments": [{**item, "anchoredReceiptRecorded": item["fileSha256"] in covered} for item in segments],
            "retentionRoot": root,
            "anchors": anchors,
        }

    def operational_status(self) -> dict[str, Any]:
        """Return a read-only operational view even when auxiliary evidence is damaged.

        The Security viewer must remain able to show the primary event chain when a
        retention policy, segment inventory, anchor receipt, rotation journal, or file
        statistic is unreadable. Protected actions still use ``assert_writable`` and fail
        closed; this method only prevents an auxiliary status failure from disconnecting
        the owner UI.
        """

        errors: list[dict[str, str]] = []

        def capture(component: str, default: Any, operation: Any) -> Any:
            try:
                return operation()
            except Exception as exc:
                errors.append({
                    "component": component,
                    "errorType": type(exc).__name__,
                    "message": str(exc),
                })
                return default

        integrity = capture(
            "integrity",
            {
                "healthy": False,
                "eventCount": 0,
                "activeEventCount": 0,
                "segmentCount": 0,
                "retainedStartSequence": 1,
                "lastSequence": 0,
                "lastHash": "",
                "issues": [{"code": "security_history_status_unavailable"}],
            },
            self.verify_integrity,
        )
        policy = capture("retention_policy", {}, self.get_retention_policy)
        inventory = capture(
            "segment_inventory",
            {
                "segments": [],
                "retentionRoot": None,
                "anchors": {
                    "anchors": [],
                    "invalid": [],
                    "coveredSegmentCount": 0,
                    "unanchoredSegmentCount": 0,
                    "unanchoredSegments": [],
                },
            },
            self.list_segments,
        )
        journals = capture("rotation_journals", [], self.list_rotation_journals)
        incomplete = capture(
            "incomplete_rotation_journals",
            [],
            lambda: self._incomplete_rotation_journals_status(),
        )
        segment_bytes = sum(int(item.get("bytes") or 0) for item in inventory.get("segments", []))
        active_bytes = capture(
            "active_ledger_size",
            0,
            lambda: self.db_path.stat().st_size if self.db_path.exists() else 0,
        )
        total_bytes = segment_bytes + int(active_bytes or 0)
        pressure: dict[str, bool] = {}
        if policy:
            try:
                pressure = {
                    "activeEventBudgetExceeded": int(integrity.get("activeEventCount", 0)) > int(policy["maxActiveEvents"]),
                    "retainedEventBudgetExceeded": int(integrity.get("eventCount", 0)) > int(policy["maxRetainedEvents"]),
                    "storageBudgetExceeded": total_bytes > int(policy["maxStorageBytes"]),
                }
            except (KeyError, TypeError, ValueError) as exc:
                errors.append({
                    "component": "retention_pressure",
                    "errorType": type(exc).__name__,
                    "message": str(exc),
                })
                pressure = {}
        return {
            "integrity": integrity,
            "policy": policy,
            "policyError": next((item["message"] for item in errors if item["component"] == "retention_policy"), ""),
            "segmentError": next((item["message"] for item in errors if item["component"] == "segment_inventory"), ""),
            "segments": inventory.get("segments", []),
            "retentionRoot": inventory.get("retentionRoot"),
            "anchors": inventory.get("anchors", {}),
            "rotationJournals": journals,
            "incompleteRotationJournals": incomplete,
            "pressure": {**pressure, "pressure": any(pressure.values()) if pressure else False},
            "storage": {
                "activeLedgerBytes": int(active_bytes or 0),
                "segmentBytes": segment_bytes,
                "totalSecurityHistoryBytes": total_bytes,
            },
            "startupRecovery": self.startup_rotation_recovery,
            "degraded": bool(errors),
            "errors": errors,
        }

    def _incomplete_rotation_journals_status(self) -> list[dict[str, Any]]:
        with self.lock:
            return self._incomplete_rotation_journals_locked()
