from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from .utils import utc_now


class FilesystemTransaction:
    """Rollback journal for repository filesystem mutations.

    A transaction captures every path before it is changed. The journal is written
    under .forgetrace/transactions so a later process can decide whether a crashed
    operation committed metadata or needs filesystem rollback.
    """

    def __init__(
        self,
        workspace: Path,
        meta_dir: Path,
        *,
        operation: str,
        state_revision_before: int,
    ) -> None:
        self.workspace = workspace.resolve()
        self.root = meta_dir / "transactions" / f"txn-{uuid.uuid4().hex}"
        self.backups = self.root / "backups"
        self.journal_path = self.root / "journal.json"
        self.operation = operation
        self.state_revision_before = int(state_revision_before)
        self.records: list[dict[str, Any]] = []
        self._captured: set[str] = set()
        self.root.mkdir(parents=True, exist_ok=False)
        self.backups.mkdir(parents=True, exist_ok=True)
        self._write_journal("pending")

    def _write_journal(self, status: str, **extra: Any) -> None:
        payload = {
            "schemaVersion": 1,
            "id": self.root.name,
            "operation": self.operation,
            "status": status,
            "createdAt": utc_now(),
            "stateRevisionBefore": self.state_revision_before,
            "records": self.records,
            **extra,
        }
        temp = self.journal_path.with_name(f"journal.{uuid.uuid4().hex}.tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.journal_path)

    def capture(self, rel: str, path: Path) -> None:
        normalized = str(rel).replace("\\", "/").strip("/")
        if normalized in self._captured:
            return
        # Capturing a parent directory already protects all descendants.
        for existing in self._captured:
            if normalized.startswith(existing + "/"):
                return
        self._captured.add(normalized)
        backup = self.backups / normalized
        record: dict[str, Any] = {
            "path": normalized,
            "existed": path.exists(),
            "kind": "missing",
        }
        if path.exists():
            if path.is_dir():
                record["kind"] = "directory"
                shutil.copytree(path, backup, symlinks=True)
            else:
                record["kind"] = "file"
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup, follow_symlinks=False)
        self.records.append(record)
        self._write_journal("pending")

    @staticmethod
    def _remove(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def rollback(self) -> None:
        for record in reversed(self.records):
            rel = record["path"]
            destination = self.workspace / rel
            self._remove(destination)
            if not record.get("existed"):
                continue
            backup = self.backups / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            if record.get("kind") == "directory":
                shutil.copytree(backup, destination, symlinks=True)
            elif record.get("kind") == "file":
                shutil.copy2(backup, destination, follow_symlinks=False)
        self._write_journal("rolled_back", rolledBackAt=utc_now())
        shutil.rmtree(self.root, ignore_errors=True)

    def commit(self, state_revision_after: int) -> None:
        self._write_journal(
            "committed", stateRevisionAfter=int(state_revision_after), committedAt=utc_now()
        )
        shutil.rmtree(self.root, ignore_errors=True)


def recover_transactions(
    workspace: Path,
    meta_dir: Path,
    *,
    current_revision: Callable[[], int],
) -> list[dict[str, Any]]:
    transaction_root = meta_dir / "transactions"
    if not transaction_root.is_dir():
        return []
    actions: list[dict[str, Any]] = []
    for root in sorted(transaction_root.glob("txn-*")):
        journal_path = root / "journal.json"
        try:
            payload = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Unknown leftovers are retained for Doctor rather than guessed at.
            actions.append({"transaction": root.name, "action": "retained_unreadable"})
            continue
        status = str(payload.get("status") or "pending")
        before = int(payload.get("stateRevisionBefore") or 0)
        revision = current_revision()
        if status == "committed" or revision > before:
            shutil.rmtree(root, ignore_errors=True)
            actions.append({"transaction": root.name, "action": "cleaned_committed"})
            continue
        records = payload.get("records") if isinstance(payload.get("records"), list) else []
        backups = root / "backups"
        for record in reversed(records):
            rel = str(record.get("path") or "").replace("\\", "/").strip("/")
            if not rel or rel == ".forgetrace" or rel.startswith(".forgetrace/"):
                continue
            destination = workspace / rel
            FilesystemTransaction._remove(destination)
            if not record.get("existed"):
                continue
            backup = backups / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            if record.get("kind") == "directory" and backup.is_dir():
                shutil.copytree(backup, destination, symlinks=True)
            elif record.get("kind") == "file" and backup.is_file():
                shutil.copy2(backup, destination, follow_symlinks=False)
        shutil.rmtree(root, ignore_errors=True)
        actions.append({"transaction": root.name, "action": "rolled_back_pending"})
    return actions
