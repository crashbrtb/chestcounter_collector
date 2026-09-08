"""
Getting the game back to a known state before acting on it.

The game opens its store on its own after a while idle, and puts modals over the
board. With one of those in front, every calibrated click lands somewhere else -
and because the collector clicks blind, it would keep going as if nothing were
wrong. So each account and each profile starts from here.

Both store steps are optional in the calibration: without them there is simply
nothing to check, and the run proceeds as it did before.
"""

import time
from typing import Optional

from utils.cancel import cancellation
from utils.logger import logger


class GameState:
    """Detects and dismisses whatever is covering the game board."""

    def __init__(self, browser, vision, calibration, timing: dict):
        self.browser = browser
        self.vision = vision
        self.calibration = calibration
        self.store_close_wait = float(timing.get("store_close_wait", 15.0))
        self.action_delay = float(timing.get("action_delay", 0.35))

    def close_modals(self, presses: int = 2):
        """Escape twice: one modal may reveal another underneath."""
        for _ in range(presses):
            self.browser.press_key("Escape", delay=self.action_delay)

    def is_store_open(self) -> bool:
        if not self.calibration.is_calibrated("store_marker"):
            return False
        reference = self.calibration.ref_path("store_marker")
        found = self.vision.find(
            reference, region=None, base_scale=self.calibration.image_scale, attempts=1
        )
        return found is not None

    def close_store(self) -> bool:
        """Closes the store if it is in front. Returns True when something was closed."""
        if not self.is_store_open():
            return False

        logger.info(f"Store detected in front of the game (score {self.vision.last_score:.2f}); closing it.")
        close_point = self.calibration.point("store_close_button")
        if not close_point:
            logger.warning("Store is open but 'store_close_button' was never calibrated.")
            self.close_modals(1)
            return False

        self.browser.click(close_point[0], close_point[1], delay=1.0)
        return True

    def wait_and_close_store(self, max_wait: Optional[float] = None, poll_interval: float = 1.0) -> bool:
        """
        Watches for the store for a while and closes it.

        It is a wait, not a single check, because the store usually appears a
        second or two after the game finishes loading - exactly when the
        collector would otherwise be starting to click.
        """
        if not self.calibration.is_calibrated("store_marker"):
            return False

        limit = self.store_close_wait if max_wait is None else max_wait
        logger.info(f"Watching up to {limit:.0f}s for the store popup...")
        deadline = time.time() + limit
        while time.time() < deadline:
            cancellation.check()
            if self.close_store():
                cancellation.sleep(1.0)
                return True
            cancellation.sleep(poll_interval)
        return False

    def prepare_board(self):
        """The clean state every workflow starts from: no modals, no store."""
        self.close_modals()
        self.wait_and_close_store()
