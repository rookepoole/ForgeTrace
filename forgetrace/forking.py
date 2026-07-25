from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ForgeTraceError

MAX_FORK_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_FORK_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
MAX_FORK_FILES = 50_000
PROTECTED_SEGMENTS = {".forgetrace", ".git", ".hg", ".svn", ".bzr"}


class CollaborationForkClient:
    """Download a token-scoped ForgeTrace source bundle into a local managed fork.

    The raw bearer token is used only for the current request. It is never written to
    repository metadata, logs, or the registry.
    """

    def __init__(self, transfer_dir: Path) -> None:
        self.transfer_dir = transfer_dir.expanduser().resolve()
        self.transfer_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def parse_share_link(raw_url: str) -> tuple[str, str]:
        value = str(raw_url or "").strip()
        if not value:
            raise ForgeTraceError("A collaboration link is required.", code="collaboration_link_required")
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"}:
            raise ForgeTraceError(
                "Collaboration links must use http or https.", code="invalid_collaboration_link_scheme"
            )
        if not parsed.hostname or not parsed.netloc:
            raise ForgeTraceError("Collaboration link host is missing.", code="invalid_collaboration_link")
        if parsed.username or parsed.password:
            raise ForgeTraceError(
                "Collaboration links may not contain embedded usernames or passwords.",
                code="invalid_collaboration_link_credentials",
            )
        token = urllib.parse.unquote(parsed.fragment or "").strip()
        if len(token) < 32:
            raise ForgeTraceError(
                "The collaboration link does not contain a valid invite token.",
                code="collaboration_token_missing",
            )
        base_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
        return base_url, token

    @staticmethod
    def _request(base_url: str, route: str, token: str) -> urllib.request.Request:
        return urllib.request.Request(
            base_url + route,
            headers={
                "X-ForgeTrace-Invite": token,
                "User-Agent": "ForgeTrace-Fork/0.3.6",
                "Accept": "application/json, application/zip;q=0.9",
            },
            method="GET",
        )

    @staticmethod
    def _validate_final_origin(base_url: str, final_url: str) -> None:
        expected = urllib.parse.urlsplit(base_url)
        actual = urllib.parse.urlsplit(final_url)
        if actual.scheme not in {"http", "https"} or actual.netloc.casefold() != expected.netloc.casefold():
            raise ForgeTraceError(
                "The collaboration gateway redirected to a different origin.",
                code="collaboration_redirect_blocked",
            )

    def fetch_context(self, base_url: str, token: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(self._request(base_url, "/api/v1/collaboration/invite", token), timeout=30) as response:
                self._validate_final_origin(base_url, response.geturl())
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ForgeTraceError("Invite response is unexpectedly large.", code="invalid_invite_response")
                payload = json.loads(raw.decode("utf-8"))
        except ForgeTraceError:
            raise
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", str(exc))
            except Exception:
                detail = str(exc)
            raise ForgeTraceError(
                f"The collaboration link could not be validated: {detail}",
                status=exc.code,
                code="collaboration_link_rejected",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ForgeTraceError(
                f"Could not reach the collaboration gateway: {exc}",
                code="collaboration_gateway_unreachable",
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("repository"), dict):
            raise ForgeTraceError("The collaboration gateway returned an invalid invite response.", code="invalid_invite_response")
        if not payload.get("rules", {}).get("sourceDownload", False):
            raise ForgeTraceError(
                "This invitation does not permit creating a local fork.",
                code="source_download_not_allowed",
            )
        return payload

    def download_source(self, base_url: str, token: str) -> Path:
        fd, raw_path = tempfile.mkstemp(prefix="fork-source-", suffix=".zip", dir=self.transfer_dir)
        os.close(fd)
        destination = Path(raw_path)
        try:
            with urllib.request.urlopen(self._request(base_url, "/api/v1/collaboration/source", token), timeout=120) as response:
                self._validate_final_origin(base_url, response.geturl())
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        announced = int(content_length)
                    except ValueError as exc:
                        raise ForgeTraceError("Source download has an invalid size header.", code="invalid_source_length") from exc
                    if announced > MAX_FORK_ARCHIVE_BYTES:
                        raise ForgeTraceError(
                            "Shared repository archive exceeds the 2 GB fork limit.",
                            code="fork_archive_too_large",
                            details={"limitBytes": MAX_FORK_ARCHIVE_BYTES, "archiveBytes": announced},
                        )
                total = 0
                with destination.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_FORK_ARCHIVE_BYTES:
                            raise ForgeTraceError(
                                "Shared repository archive exceeds the 2 GB fork limit.",
                                code="fork_archive_too_large",
                                details={"limitBytes": MAX_FORK_ARCHIVE_BYTES, "archiveBytes": total},
                            )
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
        except ForgeTraceError:
            destination.unlink(missing_ok=True)
            raise
        except urllib.error.HTTPError as exc:
            destination.unlink(missing_ok=True)
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", str(exc))
            except Exception:
                detail = str(exc)
            raise ForgeTraceError(
                f"The shared source could not be downloaded: {detail}",
                status=exc.code,
                code="source_download_failed",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise ForgeTraceError(
                f"The shared source download failed: {exc}", code="source_download_failed"
            ) from exc
        return destination

    @staticmethod
    def _safe_member_path(info: zipfile.ZipInfo) -> str:
        raw = info.filename.replace("\\", "/")
        path = PurePosixPath(raw)
        if not raw or path.is_absolute() or ".." in path.parts:
            raise ForgeTraceError("The shared archive contains an unsafe path.", code="unsafe_fork_archive")
        parts = [part for part in path.parts if part not in {"", "."}]
        if not parts:
            return ""
        if {part.casefold() for part in parts} & PROTECTED_SEGMENTS:
            raise ForgeTraceError(
                "The shared archive contains protected repository metadata.", code="protected_fork_archive_path"
            )
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise ForgeTraceError("Symbolic links are not accepted in shared forks.", code="fork_symlink_blocked")
        return PurePosixPath(*parts).as_posix()

    def extract_source(self, archive_path: Path, destination: Path) -> dict[str, Any]:
        destination = destination.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        file_count = 0
        total_uncompressed = 0
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_FORK_FILES:
                    raise ForgeTraceError(
                        "Shared repository contains too many archive entries.",
                        code="fork_file_count_exceeded",
                        details={"limit": MAX_FORK_FILES, "entries": len(infos)},
                    )
                prepared: list[tuple[zipfile.ZipInfo, str]] = []
                for info in infos:
                    if info.flag_bits & 0x1:
                        raise ForgeTraceError("Encrypted ZIP entries are not supported.", code="encrypted_fork_archive")
                    rel = self._safe_member_path(info)
                    if not rel:
                        continue
                    total_uncompressed += int(info.file_size)
                    if total_uncompressed > MAX_FORK_UNCOMPRESSED_BYTES:
                        raise ForgeTraceError(
                            "Shared repository expands beyond the 8 GB safety limit.",
                            code="fork_uncompressed_size_exceeded",
                            details={"limitBytes": MAX_FORK_UNCOMPRESSED_BYTES},
                        )
                    prepared.append((info, rel))
                for info, rel in prepared:
                    target = (destination / rel).resolve()
                    if target != destination and destination not in target.parents:
                        raise ForgeTraceError("Archive path escapes the managed repository.", code="unsafe_fork_archive")
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temp_target = target.with_name(f".{target.name}.fork.tmp")
                    with archive.open(info, "r") as source, temp_target.open("wb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temp_target, target)
                    file_count += 1
        except ForgeTraceError:
            raise
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise ForgeTraceError(f"Shared source archive is invalid: {exc}", code="invalid_fork_archive") from exc
        return {
            "files": file_count,
            "uncompressedBytes": total_uncompressed,
            "archiveSha256": hashlib.sha256(archive_path.read_bytes()).hexdigest()
            if archive_path.stat().st_size <= 64 * 1024 * 1024
            else self._hash_file(archive_path),
        }

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
