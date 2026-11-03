"""
One run at a time on this machine.

The scheduled run and a run started from the interface drive the same Chrome.
On 19/10/2026 the scheduler started at 18:51 while a module run from the
interface was still in the Journal: the newcomer reset the game under it, the
Journal closed, and both runs failed every profile after that.

The lock is an OS lock on a file, not the file's existence: the system drops it
when the process ends, however it ends, so a crash never leaves a stale lock
that would keep every later run out.
"""
import os
from typing import Optional

try:
    import msvcrt
except ImportError:  # not Windows
    msvcrt = None
    import fcntl


class RunLock:
    def __init__(self, path: str):
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        """True when this process now holds the lock; False when another one does."""
        if self._handle is not None:
            return True
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        # Who holds it, for whoever opens the file wondering.
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    def holder(self) -> Optional[str]:
        """The process id written by whoever holds the lock, if it can be read."""
        try:
            with open(self.path, "r") as handle:
                return handle.read().strip() or None
        except OSError:
            return None
