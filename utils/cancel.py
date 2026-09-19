"""
Stopping a run with the Escape key.

A collection drives the browser on its own for minutes at a time. Without a way
out, the only way to stop it going somewhere wrong was to kill the process -
which leaves the browser open, the log unfinished and a half-collected profile
behind.

Two details make this work in practice:

* Every long wait goes through `token.sleep()` instead of `time.sleep()`, so
  Escape is noticed immediately rather than after the twenty seconds a profile
  switch takes.
* The key is read with `GetAsyncKeyState`, which reports the physical keyboard
  regardless of which window has focus - the game will have it, not us. The
  Escape presses the collector itself sends to close dialogs go through CDP and
  never touch the physical key state, so they cannot cancel the run.
"""

import ctypes
import sys
import threading
import time
from typing import Optional

VK_ESCAPE = 0x1B
HOLD_SECONDS = 0.35     # deliberate press, not a brush against the key


class Cancelled(RuntimeError):
    """Raised at the first safe point after a cancellation is requested."""


class CancelToken:
    """A flag any part of a run can check, and interruptible sleeps."""

    def __init__(self):
        self._event = threading.Event()
        self.reason = ""

    def cancel(self, reason: str = "cancelled by user"):
        self.reason = reason
        self._event.set()

    def reset(self):
        self.reason = ""
        self._event.clear()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def check(self):
        """Raises if a cancellation is pending. Call between steps, not inside them."""
        if self._event.is_set():
            raise Cancelled(self.reason or "cancelled by user")

    def sleep(self, seconds: float):
        """A wait that ends early when cancelled - and then raises."""
        if seconds > 0 and self._event.wait(seconds):
            raise Cancelled(self.reason or "cancelled by user")
        self.check()


class EscapeWatcher:
    """Polls the physical Escape key and cancels the token when it is held."""

    def __init__(self, token: CancelToken, poll_interval: float = 0.1):
        self.token = token
        self.poll_interval = poll_interval
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def _pressed(self) -> bool:
        return bool(ctypes.windll.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)

    def _watch(self):
        while not self._stop.is_set():
            if self._pressed():
                held = time.time()
                while self._pressed() and not self._stop.is_set():
                    if time.time() - held >= HOLD_SECONDS:
                        self.token.cancel("ESC pressionado")
                        return
                    time.sleep(0.05)
            time.sleep(self.poll_interval)

    def start(self):
        if not self.supported or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()


# One run per process, so a single shared token spares every module a parameter
# it would only ever pass along - the same reason the logger is shared.
cancellation = CancelToken()
escape_watcher = EscapeWatcher(cancellation)
