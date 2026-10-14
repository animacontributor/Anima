"""Process leases for a live call's models. OS locks disappear after a crash."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import threading

import config


class Busy(RuntimeError):
    pass


_mutex = threading.RLock()
_held: dict[Path, list] = {}


class Lease:
    def __init__(self, name: str):
        self.path = (config.ROOT / ".runtime" / f"{name}.lock").resolve()
        self.taken = False

    def acquire(self):
        with _mutex:
            if self.taken:
                return self
            if self.path in _held:
                _held[self.path][1] += 1
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                f = self.path.open("a+b")
                try:
                    if f.seek(0, os.SEEK_END) == 0:
                        f.write(b"\0"); f.flush()
                    f.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as e:
                    f.close()
                    raise Busy("Another process is using the models. Finish its visit, wake or call first.") from e
                _held[self.path] = [f, 1]
            self.taken = True
        return self

    def release(self):
        with _mutex:
            if not self.taken:
                return
            self.taken = False
            rec = _held[self.path]
            rec[1] -= 1
            if rec[1] == 0:
                f = rec[0]
                try:
                    f.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                finally:
                    f.close()
                    del _held[self.path]


@contextlib.contextmanager
def hold(name="brain"):
    lease = Lease(name).acquire()
    try:
        yield lease
    finally:
        lease.release()
