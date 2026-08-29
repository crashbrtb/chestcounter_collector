"""
Chest Collector module for detecting, reading, and collecting Clan Gifts and Triumphal Gifts.
"""

import os
import time
from typing import Dict, Any, Tuple, Optional
from config.settings import IMAGES_DIR
from config.config_loader import AccountConfig
from database.db_connection import DatabaseConnection
from database.repositories import ChestRepository
from utils.logger import logger
from utils.text_utils import parse_key_value_line
from .base_module import BaseModule


class ChestCollector(BaseModule):
    """Automates opening the Gifts menu, parsing chest OCR text, and saving to database for all gift tabs."""

    @property
    def name(self) -> str:
        return "ChestCollector"

    def open_gift_menu(self) -> bool:
        """Navigates from main game screen to Clan -> Gifts menu."""
        coords = self.config.coordinates
        if not coords.coord_clan_button or not coords.coord_gift_button:
            logger.error("Clan or Gift button coordinates missing in config.")
            return False

        logger.info("Opening Clan Gifts menu...")
        self.controller.click(coords.coord_clan_button[0], coords.coord_clan_button[1], delay=1.2)
        self.controller.click(coords.coord_gift_button[0], coords.coord_gift_button[1], delay=1.2)
        logger.info("Clan Gifts menu opened.")
        return True

    def switch_to_triumphal_gifts_tab(self) -> bool:
        """Switches to the 'Triumphal Gifts' horizontal tab."""
        coords = self.config.coordinates
        tab_coord = coords.coord_triumphal_gifts_tab or (1055, 285)
        logger.info(f"Switching to 'Triumphal Gifts' tab at ({tab_coord[0]}, {tab_coord[1]})...")
        self.controller.click(tab_coord[0], tab_coord[1], delay=1.0)
        return True

    def switch_to_gifts_tab(self) -> bool:
        """Switches back to the default 'Gifts' horizontal tab."""
        coords = self.config.coordinates
        tab_coord = coords.coord_gifts_tab or (805, 285)
        logger.info(f"Switching back to standard 'Gifts' tab at ({tab_coord[0]}, {tab_coord[1]})...")
        self.controller.click(tab_coord[0], tab_coord[1], delay=1.0)
        return True

    def parse_chest_ocr(self, chest_area: Tuple[int, int, int, int]) -> Tuple[str, str, str, list]:
        """
        Captures the chest info box and parses chest name, player name, and source.
        """
        lines = self.ocr.extract_lines_from_area(chest_area, upscale=200, apply_threshold=True)

        chest_name = ""
        player_name = ""
        source = ""

        if len(lines) >= 1:
            chest_name = lines[0].strip()

        if len(lines) >= 2:
            _, player_name = parse_key_value_line(lines[1])

        if len(lines) >= 3:
            _, source = parse_key_value_line(lines[2])

        return chest_name, player_name, source, lines

    def process_single_chest(
        self, chest_area: Tuple[int, int, int, int], repo: ChestRepository
    ) -> int:
        """
        Processes one chest on screen.
        Returns:
            1: Successfully parsed and inserted
            2: Incomplete data (inserted into incomplete_chests)
            0: Critical OCR or DB failure
        """
        chest, player, source, raw_lines = self.parse_chest_ocr(chest_area)

        if not raw_lines:
            logger.warning(f"OCR error: No text detected in chest area {chest_area}.")
            repo.insert_error("OCR_NO_DATA_DETECTED")
            return 2

        if chest and player and source:
            if repo.insert_chest(chest, player, source):
                logger.info(f"Chest collected -> Name: '{chest}', Player: '{player}', Source: '{source}'")
                return 1
            else:
                logger.error(f"Failed to insert chest into database: '{chest}', '{player}'")
                return 0
        else:
            logger.warning(
                f"Incomplete chest data -> Name: '{chest}', Player: '{player}', Source: '{source}'. Raw: {raw_lines}"
            )
            repo.insert_incomplete_chest(chest, player, source)
            return 2

    def _collect_current_tab_chests(
        self, tab_label: str, repo: ChestRepository
    ) -> Tuple[int, int]:
        """Loops through open chests in the currently active tab at maximum speed."""
        coords = self.config.coordinates
        open_btn_img = os.path.join(IMAGES_DIR, "open.png")
        collected_count = 0
        incomplete_count = 0

        logger.info(f"Beginning chest collection loop in tab: '{tab_label}'...")
        while True:
            match = self.vision.find_template(
                open_btn_img,
                coords.cord_menu_button_open_chest,
                confidence=0.6,
                max_attempts=1,
                wait_interval=0.0,
            )

            if match is None:
                logger.info(f"No more 'Open' buttons detected in tab '{tab_label}'.")
                break

            result_code = self.process_single_chest(coords.chest_area, repo)

            if result_code == 1:
                click_x = match[0] + match[2] // 2
                click_y = match[1] + match[3] // 2
                self.controller.click(click_x, click_y, delay=0.25)
                collected_count += 1
            elif result_code == 2:
                click_x = match[0] + match[2] // 2
                click_y = match[1] + match[3] // 2
                self.controller.click(click_x, click_y, delay=0.25)
                incomplete_count += 1
            else:
                incomplete_count += 1
                break

        logger.info(
            f"Tab '{tab_label}' finished: {collected_count} collected, {incomplete_count} incomplete."
        )
        return collected_count, incomplete_count

    def collect_for_account(self, account_config: AccountConfig) -> Dict[str, Any]:
        """Collects all available clan chests (Gifts and Triumphal Gifts) for a specific account."""
        coords = self.config.coordinates

        if not coords.cord_menu_button_open_chest or not coords.chest_area:
            logger.error("Open button search area or chest_area not configured.")
            return {"success": False, "collected": 0, "errors": 0}

        logger.info(f"Connecting to database for account '{account_config.account}'...")
        with DatabaseConnection(account_config) as connection:
            if not connection:
                logger.error(f"Cannot connect to database for account '{account_config.account}'. Aborting collection.")
                return {"success": False, "collected": 0, "errors": 0}

            repo = ChestRepository(connection)

            # 1. Open Gifts menu (default tab: Gifts)
            self.open_gift_menu()

            # 2. Collect regular Gifts
            col_gifts, inc_gifts = self._collect_current_tab_chests("Gifts", repo)

            # 3. Switch to Triumphal Gifts tab and collect
            self.switch_to_triumphal_gifts_tab()
            col_triumphal, inc_triumphal = self._collect_current_tab_chests("Triumphal Gifts", repo)

            # 4. Return to default Gifts tab
            self.switch_to_gifts_tab()

            total_collected = col_gifts + col_triumphal
            total_incomplete = inc_gifts + inc_triumphal

        logger.info(
            f"Summary for '{account_config.account}': {total_collected} total chests collected "
            f"({col_gifts} Gifts + {col_triumphal} Triumphal Gifts), {total_incomplete} incomplete/errors."
        )

        return {
            "success": True,
            "account": account_config.account,
            "collected": total_collected,
            "collected_gifts": col_gifts,
            "collected_triumphal": col_triumphal,
            "incomplete": total_incomplete,
        }

    def run(self, account_config: Optional[AccountConfig] = None, **kwargs) -> Dict[str, Any]:
        """Runs chest collection for the given account or all accounts in config."""
        if account_config:
            return self.collect_for_account(account_config)

        results = []
        for acc in self.config.accounts:
            res = self.collect_for_account(acc)
            results.append(res)

        return {"results": results}
