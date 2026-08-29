"""
Account Manager module for switching between accounts and verifying active profile.
"""

import time
from typing import Optional, Dict, Any
from utils.logger import logger
from utils.text_utils import normalize_name
from config.settings import DEFAULT_ACCOUNT_SWITCH_WAIT
from .base_module import BaseModule


class AccountManager(BaseModule):
    """Handles switching in-game accounts and verifying active player identity."""

    @property
    def name(self) -> str:
        return "AccountManager"

    def is_account_active(self, target_account: str) -> bool:
        """Verifies if the specified account name is currently active on screen."""
        coords = self.config.coordinates
        display_area = coords.account_name_display_area
        if not display_area:
            logger.warning("No account_name_display_area configured.")
            return False

        # If store popup is open, dismiss it first
        if coords.screen_area:
            self.game_state.close_store_if_open(coords.screen_area)

        is_active = self.ocr.is_account_active_in_area(display_area, target_account)
        logger.info(f"Account '{target_account}' active check: {is_active}")
        return is_active

    def select_account(self, target_account: str) -> bool:
        """Switches to the specified account if not already active."""
        coords = self.config.coordinates

        # 1. Close any open dialogs (e.g. Clan/Gifts menu from previous account)
        logger.info("Ensuring clean main screen state (closing any open modals)...")
        self.controller.press_key("escape", delay=0.5)
        self.controller.press_key("escape", delay=0.5)

        if coords.screen_area:
            self.game_state.wait_and_close_store(coords.screen_area, max_wait_seconds=6.0)

        # 2. Check if already active
        if self.is_account_active(target_account):
            logger.info(f"Account '{target_account}' is already active. Skipping selection.")
            return True

        if not coords.account_menu_button or not coords.accounts_area:
            logger.error("Account menu coordinates not configured in position.cfg.")
            return False

        # 3. Open Account menu ONCE
        logger.info(f"Opening Account Menu at {coords.account_menu_button}...")
        self.controller.click(coords.account_menu_button[0], coords.account_menu_button[1], delay=1.5)

        center_x = coords.accounts_area[0] + (coords.accounts_area[2] - coords.accounts_area[0]) // 2
        center_y = coords.accounts_area[1] + (coords.accounts_area[3] - coords.accounts_area[1]) // 2

        # 4. Guarantee scroll is at the very TOP first
        logger.info("Ensuring accounts list is scrolled to the top...")
        self.controller.scroll(clicks=150, x=center_x, y=center_y, repetitions=6, delay_after=0.8)

        # 5. Search inside the same open window (View 1 = Top, View 2 = Scrolled down)
        for attempt in range(2):
            logger.info(f"Searching account list (View #{attempt + 1}) for '{target_account}'...")

            coords_account = self.ocr.find_account_coordinates_in_area(coords.accounts_area, target_account)

            if coords_account:
                logger.info(f"Clicking account entry at ({coords_account[0]}, {coords_account[1]})...")
                self.controller.click(coords_account[0], coords_account[1], delay=1.5)

                # Check and click confirmation dialog if appears
                if coords.switch_progress_confirm_button_area:
                    if self.game_state.click_confirm_button(coords.switch_progress_confirm_button_area):
                        logger.info(f"Waiting {DEFAULT_ACCOUNT_SWITCH_WAIT}s for account switch to complete...")
                        time.sleep(DEFAULT_ACCOUNT_SWITCH_WAIT)

                        self.window_mgr.activate_window(coords.window_title)
                        if coords.screen_area:
                            self.game_state.wait_and_close_store(coords.screen_area, max_wait_seconds=15.0)
                        return True
                    else:
                        logger.warning("Progress confirmation button not found (account might already be active).")
                        self.controller.press_key("escape", delay=1.0)
                        if self.is_account_active(target_account):
                            return True
                return True

            logger.info(f"Account '{target_account}' not found in View #{attempt + 1}.")

            # If not found on top view, scroll down within the same open window
            if attempt == 0:
                logger.info("Scrolling down to reveal lower accounts in the list...")
                self.controller.scroll(clicks=-150, x=center_x, y=center_y, repetitions=6, delay_after=1.0)

        logger.error(f"Account '{target_account}' not found in account list.")
        self.controller.press_key("escape", delay=0.5)
        return False

    def run(self, target_account: str, **kwargs) -> Dict[str, Any]:
        """Executes account selection."""
        success = self.select_account(target_account)
        return {"account": target_account, "success": success}
