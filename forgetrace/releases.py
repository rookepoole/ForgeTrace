from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterator

from .errors import ForgeTraceError
from .locks import InterProcessRLock
from .project_coordination import render_inert_markdown
from .security_events import SecurityEventLedger
from .utils import utc_now

RELEASES_SCHEMA_VERSION = 1
MAX_RELEASES_PER_REPOSITORY = 500
MAX_ASSETS_PER_RELEASE = 100
MAX_ASSET_BYTES = 256 * 1024 * 1024
MAX_RELEASE_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOTAL_STORAGE_BYTES = 50 * 1024 * 1024 * 1024
DRAFT_RETENTION_DAYS = 180
MAX_NOTES_CHARS = 64_000
MAX_NAME_CHARS = 160
MAX_VERSION_CHARS = 120
MAX_PROVENANCE_CHARS = 512
SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._ -]+")


class ReleaseService:
    """Immutable published release records and verified assets outside repositories."""

    def __init__(self, *, registry, collaboration, security_events: SecurityEventLedger | None = None) -> None:
        self.registry = registry
        self.collaboration = collaboration
        self.security_events = security_events
        self.root = registry.data_dir / "releases"
        self.assets_root = self.root / "assets"
        self.exports_root = self.root / "exports"
        self.db_path = self.root / "releases.sqlite3"
        self.lock = InterProcessRLock(self.root / "releases.lock")
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets_root.mkdir(parents=True, exist_ok=True)
        self.exports_root.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        with self.lock, self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS releases(
              id TEXT PRIMARY KEY,repository_id TEXT NOT NULL,name TEXT NOT NULL,version TEXT NOT NULL,
              notes TEXT NOT NULL,notes_html TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('draft','published')),
              tag_ref TEXT NOT NULL,commit_ref TEXT NOT NULL,contributor_access INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,updated_at TEXT NOT NULL,published_at TEXT NOT NULL DEFAULT '',
              version_number INTEGER NOT NULL DEFAULT 1,
              UNIQUE(repository_id,version)
            );
            CREATE TABLE IF NOT EXISTS assets(
              id TEXT PRIMARY KEY,release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
              filename TEXT NOT NULL,content_type TEXT NOT NULL,size_bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,
              storage_name TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(release_id,filename)
            );
            CREATE INDEX IF NOT EXISTS idx_releases_repo ON releases(repository_id,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_assets_release ON assets(release_id,created_at);
            CREATE TRIGGER IF NOT EXISTS published_release_update_block BEFORE UPDATE ON releases
              WHEN OLD.state='published' BEGIN SELECT RAISE(ABORT,'published_release_immutable'); END;
            CREATE TRIGGER IF NOT EXISTS published_release_delete_block BEFORE DELETE ON releases
              WHEN OLD.state='published' BEGIN SELECT RAISE(ABORT,'published_release_immutable'); END;
            CREATE TRIGGER IF NOT EXISTS published_asset_update_block BEFORE UPDATE ON assets
              WHEN EXISTS(SELECT 1 FROM releases WHERE id=OLD.release_id AND state='published') BEGIN SELECT RAISE(ABORT,'published_release_immutable'); END;
            CREATE TRIGGER IF NOT EXISTS published_asset_delete_block BEFORE DELETE ON assets
              WHEN EXISTS(SELECT 1 FROM releases WHERE id=OLD.release_id AND state='published') BEGIN SELECT RAISE(ABORT,'published_release_immutable'); END;
            """)
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)", (str(RELEASES_SCHEMA_VERSION),))
            db.commit()

    def _repo(self, repository_id: str) -> dict[str, Any]:
        return self.registry.get_repository(repository_id)

    @staticmethod
    def _clean(value: Any, label: str, limit: int, required: bool = False) -> str:
        text = str(value or "").strip()
        if required and not text:
            raise ForgeTraceError(f"{label} is required.", code="release_field_required")
        if len(text) > limit:
            raise ForgeTraceError(f"{label} is too long.", code="release_field_too_long")
        return text

    def _audit(self, action: str, repository_id: str, release_id: str, details: dict[str, Any], *, required: bool = True) -> None:
        if not self.security_events:
            return
        if required:
            self.security_events.assert_writable()
        self.security_events.append(category="release", action=action, outcome="success", surface="owner", repository_id=repository_id, subject_id=release_id, details=details)

    def _row(self, db: sqlite3.Connection, release_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if not row:
            raise ForgeTraceError("Release not found.", HTTPStatus.NOT_FOUND, "release_not_found")
        return row

    def _serialize(self, db: sqlite3.Connection, row: sqlite3.Row, include_assets: bool = True) -> dict[str, Any]:
        assets = []
        if include_assets:
            assets = [dict(item) for item in db.execute("SELECT id,filename,content_type AS contentType,size_bytes AS sizeBytes,sha256,created_at AS createdAt FROM assets WHERE release_id=? ORDER BY created_at", (row["id"],))]
        return {
            "id": row["id"], "repositoryId": row["repository_id"], "name": row["name"], "version": row["version"],
            "notes": row["notes"], "notesHtml": row["notes_html"], "state": row["state"], "tagRef": row["tag_ref"],
            "commitRef": row["commit_ref"], "contributorAccess": bool(row["contributor_access"]), "createdAt": row["created_at"],
            "updatedAt": row["updated_at"], "publishedAt": row["published_at"], "recordVersion": int(row["version_number"]), "assets": assets,
            "authority": {"artifactExecution": False, "gitMutation": False, "remotePublicationVerified": False},
        }

    def create(self, repository_id: str, *, name: Any, version: Any, notes: Any = "", tag_ref: Any = "", commit_ref: Any = "", contributor_access: bool = False, actor: str = "Repository Owner") -> dict[str, Any]:
        self._repo(repository_id)
        name = self._clean(name, "Release name", MAX_NAME_CHARS, True)
        version = self._clean(version, "Release version", MAX_VERSION_CHARS, True)
        notes = self._clean(notes, "Release notes", MAX_NOTES_CHARS)
        tag_ref = self._clean(tag_ref, "Tag reference", MAX_PROVENANCE_CHARS)
        commit_ref = self._clean(commit_ref, "Commit reference", MAX_PROVENANCE_CHARS)
        release_id = "rel_" + uuid.uuid4().hex[:24]
        now = utc_now()
        with self.lock, self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM releases WHERE repository_id=?", (repository_id,)).fetchone()[0]
            if count >= MAX_RELEASES_PER_REPOSITORY:
                raise ForgeTraceError("Release quota reached.", HTTPStatus.CONFLICT, "release_quota_reached")
            try:
                db.execute("INSERT INTO releases(id,repository_id,name,version,notes,notes_html,state,tag_ref,commit_ref,contributor_access,created_at,updated_at) VALUES(?,?,?,?,?,?,'draft',?,?,?,?,?)", (release_id, repository_id, name, version, notes, render_inert_markdown(notes), tag_ref, commit_ref, int(bool(contributor_access)), now, now))
                db.commit()
            except sqlite3.IntegrityError as exc:
                raise ForgeTraceError("That release version already exists.", HTTPStatus.CONFLICT, "release_version_exists") from exc
            row = self._row(db, release_id)
        self._audit("release_created", repository_id, release_id, {"version": version, "actor": actor})
        return self.get(repository_id, release_id)

    def update(self, repository_id: str, release_id: str, *, expected_version: Any, **changes: Any) -> dict[str, Any]:
        with self.lock, self._connect() as db:
            row = self._row(db, release_id)
            if row["repository_id"] != repository_id:
                raise ForgeTraceError("Release not found.", HTTPStatus.NOT_FOUND, "release_not_found")
            if row["state"] != "draft":
                raise ForgeTraceError("Published releases are immutable.", HTTPStatus.CONFLICT, "release_published_immutable")
            if int(expected_version or 0) != int(row["version_number"]):
                raise ForgeTraceError("Release changed since it was loaded.", HTTPStatus.CONFLICT, "release_version_changed")
            values = {
                "name": self._clean(changes.get("name", row["name"]), "Release name", MAX_NAME_CHARS, True),
                "notes": self._clean(changes.get("notes", row["notes"]), "Release notes", MAX_NOTES_CHARS),
                "tag_ref": self._clean(changes.get("tag_ref", row["tag_ref"]), "Tag reference", MAX_PROVENANCE_CHARS),
                "commit_ref": self._clean(changes.get("commit_ref", row["commit_ref"]), "Commit reference", MAX_PROVENANCE_CHARS),
                "contributor_access": int(bool(changes.get("contributor_access", row["contributor_access"]))),
            }
            db.execute("UPDATE releases SET name=?,notes=?,notes_html=?,tag_ref=?,commit_ref=?,contributor_access=?,updated_at=?,version_number=version_number+1 WHERE id=?", (values["name"], values["notes"], render_inert_markdown(values["notes"]), values["tag_ref"], values["commit_ref"], values["contributor_access"], utc_now(), release_id))
            db.commit()
        self._audit("release_updated", repository_id, release_id, {"fields": sorted(changes)})
        return self.get(repository_id, release_id)

    @staticmethod
    def _safe_filename(filename: Any) -> str:
        name = Path(str(filename or "")).name.strip()
        name = SAFE_FILE_RE.sub("_", name).strip(" .")
        if not name or name in {".", ".."}:
            raise ForgeTraceError("Asset filename is invalid.", code="invalid_release_asset_filename")
        return name[:180]

    def add_asset_base64(self, repository_id: str, release_id: str, *, filename: Any, content_base64: Any, content_type: Any = "application/octet-stream") -> dict[str, Any]:
        filename = self._safe_filename(filename)
        try:
            raw = base64.b64decode(str(content_base64 or ""), validate=True)
        except Exception as exc:
            raise ForgeTraceError("Asset content must be valid base64.", code="invalid_release_asset_content") from exc
        if len(raw) > MAX_ASSET_BYTES:
            raise ForgeTraceError("Release asset exceeds the size limit.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "release_asset_too_large")
        asset_id = "asset_" + uuid.uuid4().hex[:24]
        storage_name = asset_id + ".bin"
        target = self.assets_root / storage_name
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock, self._connect() as db:
            row = self._row(db, release_id)
            if row["repository_id"] != repository_id:
                raise ForgeTraceError("Release not found.", HTTPStatus.NOT_FOUND, "release_not_found")
            if row["state"] != "draft":
                raise ForgeTraceError("Published releases are immutable.", HTTPStatus.CONFLICT, "release_published_immutable")
            stats = db.execute("SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM assets WHERE release_id=?", (release_id,)).fetchone()
            total_storage = int(db.execute("SELECT COALESCE(SUM(size_bytes),0) FROM assets").fetchone()[0])
            if int(stats[0]) >= MAX_ASSETS_PER_RELEASE or int(stats[1]) + len(raw) > MAX_RELEASE_BYTES or total_storage + len(raw) > MAX_TOTAL_STORAGE_BYTES:
                raise ForgeTraceError("Release asset quota reached.", HTTPStatus.CONFLICT, "release_asset_quota_reached")
            fd, temp_name = tempfile.mkstemp(prefix="release-asset-", dir=self.assets_root)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw); handle.flush(); os.fsync(handle.fileno())
                os.replace(temp_name, target)
                db.execute("INSERT INTO assets(id,release_id,filename,content_type,size_bytes,sha256,storage_name,created_at) VALUES(?,?,?,?,?,?,?,?)", (asset_id, release_id, filename, self._clean(content_type, "Content type", 200) or "application/octet-stream", len(raw), digest, storage_name, utc_now()))
                db.execute("UPDATE releases SET updated_at=?,version_number=version_number+1 WHERE id=?", (utc_now(), release_id))
                db.commit()
            except Exception:
                Path(temp_name).unlink(missing_ok=True); target.unlink(missing_ok=True); raise
        self._audit("release_asset_added", repository_id, release_id, {"assetId": asset_id, "filename": filename, "sizeBytes": len(raw), "sha256": digest})
        return self.get(repository_id, release_id)

    def publish(self, repository_id: str, release_id: str, *, expected_version: Any) -> dict[str, Any]:
        with self.lock, self._connect() as db:
            row = self._row(db, release_id)
            if row["repository_id"] != repository_id:
                raise ForgeTraceError("Release not found.", HTTPStatus.NOT_FOUND, "release_not_found")
            if row["state"] == "published":
                return self._serialize(db, row)
            if int(expected_version or 0) != int(row["version_number"]):
                raise ForgeTraceError("Release changed since it was loaded.", HTTPStatus.CONFLICT, "release_version_changed")
            assets = db.execute("SELECT * FROM assets WHERE release_id=?", (release_id,)).fetchall()
            for asset in assets:
                self._verify_asset_row(asset)
            now = utc_now()
            db.execute("UPDATE releases SET state='published',published_at=?,updated_at=?,version_number=version_number+1 WHERE id=?", (now, now, release_id))
            db.commit()
        self._audit("release_published", repository_id, release_id, {"assetCount": len(assets), "version": row["version"]})
        return self.get(repository_id, release_id)

    def _verify_asset_row(self, asset: sqlite3.Row | dict[str, Any]) -> Path:
        path = (self.assets_root / str(asset["storage_name"])).resolve()
        if path.parent != self.assets_root.resolve() or not path.is_file():
            raise ForgeTraceError("Release asset is missing.", HTTPStatus.CONFLICT, "release_asset_missing")
        size = path.stat().st_size
        if size != int(asset["size_bytes"]):
            raise ForgeTraceError("Release asset size verification failed.", HTTPStatus.CONFLICT, "release_asset_size_mismatch")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != str(asset["sha256"]):
            raise ForgeTraceError("Release asset hash verification failed.", HTTPStatus.CONFLICT, "release_asset_hash_mismatch")
        return path

    def list(self, repository_id: str, *, published_only: bool = False) -> dict[str, Any]:
        self._repo(repository_id)
        with self._connect() as db:
            where = "repository_id=?" + (" AND state='published'" if published_only else "")
            rows = db.execute(f"SELECT * FROM releases WHERE {where} ORDER BY created_at DESC LIMIT 500", (repository_id,)).fetchall()
            return {"items": [self._serialize(db, row) for row in rows], "schemaVersion": RELEASES_SCHEMA_VERSION}

    def get(self, repository_id: str, release_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = self._row(db, release_id)
            if row["repository_id"] != repository_id:
                raise ForgeTraceError("Release not found.", HTTPStatus.NOT_FOUND, "release_not_found")
            return self._serialize(db, row)

    def _token_context(self, token: str) -> dict[str, Any]:
        context = self.collaboration.invite_context(token)
        if not context.get("rules", {}).get("projectParticipation"):
            raise ForgeTraceError("This invitation does not permit release access.", HTTPStatus.FORBIDDEN, "release_access_not_permitted")
        return context

    def list_for_token(self, token: str) -> dict[str, Any]:
        context = self._token_context(token)
        repository_id = context["repository"]["id"]
        with self._connect() as db:
            rows = db.execute("SELECT * FROM releases WHERE repository_id=? AND state='published' AND contributor_access=1 ORDER BY published_at DESC", (repository_id,)).fetchall()
            return {"items": [self._serialize(db, row) for row in rows], "repository": context["repository"], "authority": {"downloadOnly": True}}

    def asset_path(self, repository_id: str, release_id: str, asset_id: str, *, token: str = "") -> tuple[Path, str, str]:
        if token:
            context = self._token_context(token)
            if context["repository"]["id"] != repository_id:
                raise ForgeTraceError("Release not found.", HTTPStatus.NOT_FOUND, "release_not_found")
        with self._connect() as db:
            release = self._row(db, release_id)
            if release["repository_id"] != repository_id or (token and (release["state"] != "published" or not bool(release["contributor_access"]))):
                raise ForgeTraceError("Release not found.", HTTPStatus.NOT_FOUND, "release_not_found")
            asset = db.execute("SELECT * FROM assets WHERE id=? AND release_id=?", (asset_id, release_id)).fetchone()
            if not asset:
                raise ForgeTraceError("Release asset not found.", HTTPStatus.NOT_FOUND, "release_asset_not_found")
            path = self._verify_asset_row(asset)
            return path, str(asset["filename"]), str(asset["content_type"])

    def export_release(self, repository_id: str, release_id: str) -> tuple[Path, str]:
        release = self.get(repository_id, release_id)
        with self._connect() as db:
            rows = db.execute("SELECT * FROM assets WHERE release_id=? ORDER BY created_at", (release_id,)).fetchall()
            verified = [(row, self._verify_asset_row(row)) for row in rows]
        fd, temp_name = tempfile.mkstemp(prefix="release-export-", suffix=".zip", dir=self.exports_root); os.close(fd)
        path = Path(temp_name)
        manifest = {"format": "forgetrace-verified-release", "schemaVersion": RELEASES_SCHEMA_VERSION, "release": release, "assets": [{"id": row["id"], "filename": row["filename"], "sizeBytes": row["size_bytes"], "sha256": row["sha256"]} for row, _ in verified], "remotePublicationVerified": False}
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("release-manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            archive.writestr("release-notes.md", release["notes"])
            for row, asset_path in verified:
                archive.write(asset_path, f"assets/{row['filename']}")
        return path, f"{release['version']}-verified-release.zip"

    def cleanup_retention(self, *, days: int = DRAFT_RETENTION_DAYS) -> dict[str, Any]:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat().replace("+00:00", "Z")
        removed: list[str] = []
        with self.lock, self._connect() as db:
            rows = db.execute("SELECT id FROM releases WHERE state='draft' AND updated_at<?", (cutoff,)).fetchall()
            for row in rows:
                assets = db.execute("SELECT storage_name FROM assets WHERE release_id=?", (row["id"],)).fetchall()
                db.execute("DELETE FROM releases WHERE id=?", (row["id"],))
                for asset in assets:
                    (self.assets_root / str(asset["storage_name"])).unlink(missing_ok=True)
                removed.append(str(row["id"]))
            db.commit()
        return {"removedDrafts": len(removed), "retentionDays": max(1, int(days))}

    def health_status(self, repository_id: str = "") -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        checked = 0
        total_bytes = 0
        with self._connect() as db:
            query = "SELECT a.*,r.repository_id,r.state FROM assets a JOIN releases r ON r.id=a.release_id"
            args: tuple[Any, ...] = ()
            if repository_id:
                query += " WHERE r.repository_id=?"; args = (repository_id,)
            rows = db.execute(query, args).fetchall()
            for row in rows:
                checked += 1; total_bytes += int(row["size_bytes"])
                try:
                    self._verify_asset_row(row)
                except ForgeTraceError as exc:
                    findings.append({"code": exc.code, "severity": "critical" if row["state"] == "published" else "warning", "assetId": row["id"], "releaseId": row["release_id"], "message": str(exc)})
        return {"available": True, "schemaVersion": RELEASES_SCHEMA_VERSION, "status": "critical" if any(f["severity"] == "critical" for f in findings) else "warning" if findings else "healthy", "assetCount": checked, "storageBytes": total_bytes, "findings": findings}
