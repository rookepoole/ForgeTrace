from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def platform_data_dir(override: Path | None = None) -> Path:
    """Return ForgeTrace's global application-data directory."""
    if override is not None:
        return override.expanduser().resolve()
    configured = os.environ.get("FORGETRACE_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "ForgeTrace"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ForgeTrace"
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "forgetrace"


def normalize_repository_path(raw: str | Path) -> tuple[str, str]:
    value = str(raw).strip()
    if not value:
        raise ValueError("A repository path is required.")
    path = Path(value).expanduser()
    absolute = Path(os.path.abspath(path))
    display = str(absolute)
    canonical = os.path.normcase(os.path.normpath(display))
    return display, canonical
