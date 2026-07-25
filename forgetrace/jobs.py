from __future__ import annotations

import json
import os
import threading
import traceback
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable

from .utils import utc_now


@dataclass
class JobContext:
    job_id: str
    cancel_event: threading.Event
    update_callback: Callable[[dict[str, Any]], None]

    def update(self, **values: Any) -> None:
        self.update_callback(values)

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise JobCancelled("Operation cancelled by the user.")


class JobCancelled(RuntimeError):
    pass


@dataclass
class _Job:
    id: str
    kind: str
    status: str = "queued"
    phase: str = "queued"
    message: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    progress: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: dict[str, Any] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "progress": dict(self.progress),
            "result": self.result,
            "error": self.error,
            "cancelRequested": self.cancel_event.is_set(),
        }


class OperationManager:
    def __init__(self, *, max_retained: int = 200, history_path: Path | None = None) -> None:
        self.max_retained = max(20, int(max_retained))
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self.history_path = history_path.expanduser().resolve() if history_path else None
        self._load_history()

    def _load_history(self) -> None:
        if not self.history_path or not self.history_path.is_file():
            return
        try:
            payload = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in payload.get("jobs", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            status = str(item.get("status") or "failed")
            if status in {"queued", "running"}:
                status = "failed"
                item["error"] = {"type": "InterruptedOperation", "message": "ForgeTrace stopped before this operation completed."}
            job = _Job(
                id=str(item["id"]), kind=str(item.get("kind") or "operation"), status=status,
                phase=str(item.get("phase") or status), message=str(item.get("message") or ""),
                created_at=str(item.get("createdAt") or utc_now()), updated_at=str(item.get("updatedAt") or utc_now()),
                progress=dict(item.get("progress") or {}), result=item.get("result"), error=item.get("error"),
            )
            self._jobs[job.id] = job
        self._trim()

    def _persist(self) -> None:
        if not self.history_path:
            return
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schemaVersion": 1, "jobs": [job.public() for job in sorted(self._jobs.values(), key=lambda item: item.created_at)]}
        temp = self.history_path.with_name(f".{self.history_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, self.history_path)
        finally:
            temp.unlink(missing_ok=True)

    def _trim(self) -> None:
        completed = [job for job in self._jobs.values() if job.status in {"completed", "failed", "cancelled"}]
        completed.sort(key=lambda item: item.updated_at)
        while len(self._jobs) > self.max_retained and completed:
            job = completed.pop(0)
            self._jobs.pop(job.id, None)

    def start(self, kind: str, target: Callable[[JobContext], Any]) -> dict[str, Any]:
        job = _Job(id="job_" + uuid.uuid4().hex, kind=kind)
        with self._lock:
            self._jobs[job.id] = job
            self._trim()
            self._persist()

        def update(values: dict[str, Any]) -> None:
            with self._lock:
                current = self._jobs.get(job.id)
                if not current:
                    return
                if "phase" in values:
                    current.phase = str(values.pop("phase"))
                if "message" in values:
                    current.message = str(values.pop("message"))
                current.progress.update(values)
                current.updated_at = utc_now()
                self._persist()

        def runner() -> None:
            with self._lock:
                job.status = "running"
                job.phase = "starting"
                job.updated_at = utc_now()
                self._persist()
            context = JobContext(job.id, job.cancel_event, update)
            try:
                result = target(context)
                with self._lock:
                    job.result = result
                    job.status = "cancelled" if job.cancel_event.is_set() else "completed"
                    job.phase = job.status
                    job.updated_at = utc_now()
                    self._persist()
            except JobCancelled as exc:
                with self._lock:
                    job.status = "cancelled"
                    job.phase = "cancelled"
                    job.message = str(exc)
                    job.updated_at = utc_now()
                    self._persist()
            except Exception as exc:  # pragma: no cover - traceback is diagnostic
                with self._lock:
                    job.status = "failed"
                    job.phase = "failed"
                    job.error = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(limit=20),
                    }
                    job.updated_at = utc_now()
                    self._persist()

        thread = threading.Thread(target=runner, name=f"ForgeTraceJob-{job.id[-8:]}", daemon=True)
        thread.start()
        return job.public()

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            return job.public()

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if job.status in {"queued", "running"}:
                job.cancel_event.set()
                job.message = "Cancellation requested."
                job.updated_at = utc_now()
                self._persist()
            return job.public()
