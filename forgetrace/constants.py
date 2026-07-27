from __future__ import annotations

APP_VERSION = "0.5.3.0"
APP_SCHEMA_VERSION = 4
REPOSITORY_SCHEMA_VERSION = 3

REPOSITORY_ACCESS_READ_WRITE = "read_write"
REPOSITORY_ACCESS_READ_ONLY = "read_only"
REPOSITORY_ACCESS_MODES = {REPOSITORY_ACCESS_READ_WRITE, REPOSITORY_ACCESS_READ_ONLY}


def normalize_repository_access_mode(value: object, *, fail_closed: bool = False) -> str:
    mode = str(value or "").strip().lower()
    if mode in REPOSITORY_ACCESS_MODES:
        return mode
    if fail_closed:
        return REPOSITORY_ACCESS_READ_ONLY
    return REPOSITORY_ACCESS_READ_WRITE


MAX_REQUEST_BYTES = 1024 * 1024 * 1024
MAX_IMPORT_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MAX_EDITABLE_TEXT_BYTES = 5 * 1024 * 1024

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".html", ".htm", ".css", ".js", ".mjs", ".cjs",
    ".json", ".jsonc", ".xml", ".svg", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".py", ".pyw", ".rb", ".php", ".java", ".kt", ".kts", ".swift",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs", ".go", ".rs", ".sh",
    ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd", ".sql", ".graphql",
    ".gql", ".vue", ".svelte", ".jsx", ".tsx", ".ts", ".env", ".gitignore",
    ".dockerignore", ".editorconfig", ".lock", ".csv", ".tsv", ".log",
}
