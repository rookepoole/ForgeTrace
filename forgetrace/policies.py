from __future__ import annotations

from pathlib import Path

SENSITIVE_BASENAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "credentials", "credentials.json", "secrets.json", "secret.json",
    "id_rsa", "id_ed25519", "known_hosts", ".npmrc", ".pypirc",
    "service-account.json", "firebase-adminsdk.json", "aws_credentials",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".jks", ".keystore"}
CACHE_SEGMENTS = {"node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".cache", "dist", "build"}
VCS_SEGMENTS = {".git", ".hg", ".svn", ".bzr"}


def path_policy_warnings(path: str) -> list[str]:
    parts = [part.casefold() for part in Path(path).parts]
    name = parts[-1] if parts else ""
    suffix = Path(name).suffix.casefold()
    warnings: list[str] = []
    if name in SENSITIVE_BASENAMES or suffix in SENSITIVE_SUFFIXES:
        warnings.append("possible_secret_or_credential")
    if any(part in CACHE_SEGMENTS for part in parts):
        warnings.append("generated_or_cache_content")
    if any(part in VCS_SEGMENTS for part in parts):
        warnings.append("version_control_metadata")
    if name.startswith("."):
        warnings.append("hidden_file")
    return warnings


def is_protected_metadata_path(path: str) -> bool:
    return any(part.casefold() == ".forgetrace" for part in Path(path).parts)
