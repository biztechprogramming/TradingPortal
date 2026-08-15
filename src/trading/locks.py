"""Execution lock.

Two commands must never run at once — a LONG racing a SHORT could sell QQQ
and buy PSQ from one process while the other does the reverse, leaving the
account somewhere neither command intended.

The lock is both in-process (threading) and cross-process (an advisory
``flock``), so a second copy of the CLI, or a CLI running alongside the web
server, is blocked too.
"""

from __future__ import annotations

import errno
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import ExecutionLocked

try:
    import fcntl

    HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    HAVE_FCNTL = False

_process_lock = threading.Lock()


class ExecutionLock:
    """Exclusive, non-blocking, and it tells you who is holding it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._held_by_thread = False

    @contextmanager
    def acquire(self, owner: str) -> Iterator[None]:
        if not _process_lock.acquire(blocking=False):
            raise ExecutionLocked(
                "Another command is already executing in this process. "
                "Wait for it to finish."
            )
        self._held_by_thread = True
        try:
            self._acquire_file_lock(owner)
            try:
                yield
            finally:
                self._release_file_lock()
        finally:
            self._held_by_thread = False
            _process_lock.release()

    def _acquire_file_lock(self, owner: str) -> None:
        if not HAVE_FCNTL:
            return
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = self._read_holder()
            self._fh.close()
            self._fh = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise ExecutionLocked(
                    f"Another process is executing a command{holder}. "
                    "Commands are serialised so LONG and SHORT can never "
                    "overlap."
                ) from exc
            raise
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(
            json.dumps({"owner": owner, "pid": os.getpid(), "acquired_at": time.time()})
        )
        self._fh.flush()

    def _read_holder(self) -> str:
        try:
            data = json.loads(self.path.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            return ""
        owner = data.get("owner")
        pid = data.get("pid")
        if owner:
            return f" (owner={owner}, pid={pid})"
        return ""

    def _release_file_lock(self) -> None:
        if self._fh is None:
            return
        try:
            if HAVE_FCNTL:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    @property
    def is_held_here(self) -> bool:
        return self._held_by_thread
