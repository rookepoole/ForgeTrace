from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import BinaryIO


def _windows_lock_open_parameters(*, create: bool) -> dict[str, int]:
    """Return the Win32 flags used for ForgeTrace advisory lock handles.

    FILE_SHARE_DELETE is essential: managed-repository deletion atomically moves the
    repository directory while its repository lock remains held. Without delete
    sharing, Windows rejects the parent-directory rename with WinError 5.
    """

    return {
        "desired_access": 0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
        "share_mode": 0x00000001 | 0x00000002 | 0x00000004,  # READ | WRITE | DELETE
        "creation_disposition": 4 if create else 3,  # OPEN_ALWAYS | OPEN_EXISTING
        "flags_and_attributes": 0x00000080,  # FILE_ATTRIBUTE_NORMAL
    }


def _open_windows_shared_lock_file(path: Path, *, create: bool) -> BinaryIO:
    """Open a lock file through CreateFileW with delete sharing enabled."""

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    parameters = _windows_lock_open_parameters(create=create)
    raw_handle = create_file(
        str(path),
        parameters["desired_access"],
        parameters["share_mode"],
        None,
        parameters["creation_disposition"],
        parameters["flags_and_attributes"],
        None,
    )
    handle_value = raw_handle if isinstance(raw_handle, int) else raw_handle.value
    invalid_handle = ctypes.c_void_p(-1).value
    if handle_value is None or handle_value == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle_value), os.O_RDWR | getattr(os, "O_BINARY", 0)
        )
    except Exception:
        close_handle(raw_handle)
        raise
    try:
        return os.fdopen(descriptor, "r+b", buffering=0)
    except Exception:
        os.close(descriptor)
        raise


def _open_lock_file(path: Path, *, create: bool) -> BinaryIO:
    if os.name == "nt":
        return _open_windows_shared_lock_file(path, create=create)
    mode = "a+b" if create else "r+b"
    return path.open(mode)


def windows_locking_processes(paths: list[Path]) -> list[dict[str, object]]:
    """Return Windows Restart Manager processes holding handles to ``paths``.

    The query is diagnostic only: ForgeTrace never closes, restarts, or terminates the
    returned processes. Non-Windows platforms return an empty list.
    """

    if os.name != "nt":
        return []
    candidates = [str(Path(path).expanduser().resolve()) for path in paths if Path(path).exists()]
    if not candidates:
        return []
    try:
        import ctypes
        from ctypes import wintypes

        cch_app = 255
        cch_service = 63
        error_more_data = 234

        class RM_UNIQUE_PROCESS(ctypes.Structure):
            _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]

        class RM_PROCESS_INFO(ctypes.Structure):
            _fields_ = [
                ("Process", RM_UNIQUE_PROCESS),
                ("strAppName", wintypes.WCHAR * (cch_app + 1)),
                ("strServiceShortName", wintypes.WCHAR * (cch_service + 1)),
                ("ApplicationType", wintypes.DWORD),
                ("AppStatus", wintypes.ULONG),
                ("TSSessionId", wintypes.DWORD),
                ("bRestartable", wintypes.BOOL),
            ]

        restart_manager = ctypes.WinDLL("rstrtmgr", use_last_error=True)
        start_session = restart_manager.RmStartSession
        start_session.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
        start_session.restype = wintypes.DWORD
        register_resources = restart_manager.RmRegisterResources
        register_resources.argtypes = [
            wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
            wintypes.UINT, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
        ]
        register_resources.restype = wintypes.DWORD
        get_list = restart_manager.RmGetList
        get_list.argtypes = [
            wintypes.DWORD, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(RM_PROCESS_INFO), ctypes.POINTER(wintypes.DWORD),
        ]
        get_list.restype = wintypes.DWORD
        end_session = restart_manager.RmEndSession
        end_session.argtypes = [wintypes.DWORD]
        end_session.restype = wintypes.DWORD

        session = wintypes.DWORD()
        key = ctypes.create_unicode_buffer(33)
        if start_session(ctypes.byref(session), 0, key) != 0:
            return []
        try:
            resources = (wintypes.LPCWSTR * len(candidates))(*candidates)
            if register_resources(session.value, len(candidates), resources, 0, None, 0, None) != 0:
                return []
            needed = wintypes.UINT(0)
            count = wintypes.UINT(0)
            reasons = wintypes.DWORD(0)
            result = get_list(session.value, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
            if result not in {0, error_more_data} or needed.value == 0:
                return []
            entries = (RM_PROCESS_INFO * needed.value)()
            count = wintypes.UINT(needed.value)
            result = get_list(
                session.value, ctypes.byref(needed), ctypes.byref(count), entries, ctypes.byref(reasons)
            )
            if result != 0:
                return []
            records: list[dict[str, object]] = []
            for entry in entries[: count.value]:
                records.append({
                    "pid": int(entry.Process.dwProcessId),
                    "name": str(entry.strAppName or "").strip(),
                    "service": str(entry.strServiceShortName or "").strip(),
                    "applicationType": int(entry.ApplicationType),
                    "restartable": bool(entry.bRestartable),
                })
            return records
        finally:
            end_session(session.value)
    except Exception:
        return []


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
        handle = _open_lock_file(self.path, create=True)
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        handle.seek(0)
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


def inspect_file_lock(path: Path) -> dict[str, object]:
    """Probe an existing advisory lock without creating or rewriting its file."""

    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {"path": str(resolved), "available": True, "state": "not_created"}
    try:
        handle = _open_lock_file(resolved, create=False)
    except OSError as exc:
        return {"path": str(resolved), "available": False, "state": "unavailable", "message": str(exc)}
    try:
        try:
            if resolved.stat().st_size < 1:
                return {"path": str(resolved), "available": True, "state": "uninitialized"}
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return {"path": str(resolved), "available": False, "state": "busy"}
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        return {"path": str(resolved), "available": True, "state": "available"}
    finally:
        handle.close()


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
