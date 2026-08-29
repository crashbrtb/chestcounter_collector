"""
Game state detector and popup dismissal helper.
"""

import os
import time
from typing import Optional, Tuple
from config.settings import IMAGES_DIR
from utils.logger import logger
from .vision import Vision
from .bot_controller import BotController


class GameStateHelper:
    """Detects in-game screen states and dismisses blocking dialogs/stores."""

    def __init__(self, vision: Vision, controller: BotController):
        self.vision = vision
        self.controller = controller

    def close_store_if_open(self, screen_area: Tuple[int, int, int, int]) -> bool:
        """
        Checks if the in-game bonus store/sale popup is displayed and clicks the 'X' button to close it.
        """
        bonus_sale_img = os.path.join(IMAGES_DIR, "bonussale.png")
        x_button_img = os.path.join(IMAGES_DIR, "x.png")

        match_store = self.vision.find_template(bonus_sale_img, screen_area, confidence=0.6, max_attempts=1)
        if match_store is None:
            # Check if standalone X is visible in upper right quadrant of screen
            return False

        logger.info("Store screen detected. Looking for 'X' button to close it...")
        match_x = self.vision.find_template(x_button_img, screen_area, confidence=0.6, max_attempts=3)
        if match_x is not None:
            click_x = match_x[0] + match_x[2] // 2
            click_y = match_x[1] + match_x[3] // 2
            self.controller.click(click_x, click_y, delay=2.0)
            logger.info(f"Store popup closed by clicking 'X' at ({click_x}, {click_y}).")
            return True

        logger.warning("Store was detected but 'X' button could not be located.")
        return False

    def wait_and_close_store(
        self,
        screen_area: Tuple[int, int, int, int],
        max_wait_seconds: float = 15.0,
        poll_interval: float = 1.0,
    ) -> bool:
        """
        Waits up to max_wait_seconds for the store popup to appear and closes it.
        Returns True if a store was closed, False if no store appeared within the window.
        """
        logger.info(f"Waiting up to {max_wait_seconds}s to detect and close any store popups...")
        start = time.time()

        while time.time() - start < max_wait_seconds:
            if self.close_store_if_open(screen_area):
                time.sleep(1.0)
                return True
            time.sleep(poll_interval)

        logger.debug("No store popup detected during wait interval.")
        return False

    def is_confirm_dialog_open(self, confirm_button_area: Tuple[int, int, int, int]) -> bool:
        """Checks if the account switch confirmation button is visible."""
        confirm_img = os.path.join(IMAGES_DIR, "progress_confirm.png")
        match = self.vision.find_template(confirm_img, confirm_button_area, confidence=0.7, max_attempts=3)
        return match is not None

    def click_confirm_button(self, confirm_button_area: Tuple[int, int, int, int]) -> bool:
        """Clicks the confirmation button inside the given area."""
        confirm_img = os.path.join(IMAGES_DIR, "progress_confirm.png")
        match = self.vision.find_template(confirm_img, confirm_button_area, confidence=0.7, max_attempts=3)
        if match is not None:
            center_x = match[0] + match[2] // 2
            center_y = match[1] + match[3] // 2
            self.controller.click(center_x, center_y, delay=2.0)
            logger.info(f"Clicked progress confirmation button at ({center_x}, {center_y}).")
            return True
        return False
