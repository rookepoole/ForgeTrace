from __future__ import annotations

import os
import threading
import time
from pathlib import Path


class LockUnavailable(RuntimeError):
    pass


class FileLock:
    """Small cross-platform advisory file lock.

    ForgeTrace uses advisory locks only for its own processes. The lock file remains
    on disk after release; ownership is represented by the OS lock, not existence.
    """

    def __init__(self, path: Path, *, timeout: float = 30.0, poll_interval: float = 0.05) -> None:
        self.path = path.expanduser().resolve()
        self.timeout = max(0.0, float(timeout))
        self.poll_interval = max(0.01, float(poll_interval))
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    handle.close()
                    raise LockUnavailable(f"Timed out waiting for ForgeTrace lock: {self.path}")
                time.sleep(self.poll_interval)

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class InterProcessRLock:
    """Thread-reentrant lock backed by one advisory lock across processes."""

    def __init__(self, path: Path, *, timeout: float = 30.0) -> None:
        self.path = path.expanduser().resolve()
        self.timeout = timeout
        self._thread_lock = threading.RLock()
        self._local = threading.local()
        self._file_lock: FileLock | None = None

    def acquire(self) -> bool:
        self._thread_lock.acquire()
        depth = int(getattr(self._local, "depth", 0))
        try:
            if depth == 0:
                lock = FileLock(self.path, timeout=self.timeout)
                lock.acquire()
                self._file_lock = lock
            self._local.depth = depth + 1
            return True
        except Exception:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        depth = int(getattr(self._local, "depth", 0))
        if depth <= 0:
            raise RuntimeError("ForgeTrace lock released without being acquired")
        depth -= 1
        self._local.depth = depth
        try:
            if depth == 0 and self._file_lock is not None:
                self._file_lock.release()
                self._file_lock = None
        finally:
            self._thread_lock.release()

    def __enter__(self) -> "InterProcessRLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
