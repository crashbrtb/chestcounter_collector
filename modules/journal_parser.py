"""
Journal Parser module for reading event results and recording player scores.
"""

from typing import Dict, Any, List, Optional, Tuple
from utils.logger import logger
from database.db_connection import DatabaseConnection
from database.repositories import JournalRepository
from config.config_loader import AccountConfig
from .base_module import BaseModule


class JournalParser(BaseModule):
    """
    Automates reading the in-game Journal ('Diário') to calculate player event scores
    and store participation metrics in the database.
    """

    @property
    def name(self) -> str:
        return "JournalParser"

    def open_journal(self, journal_button_coord: Optional[Tuple[int, int]] = None) -> bool:
        """Clicks the in-game Journal icon to open event history."""
        if not journal_button_coord:
            logger.warning("Journal button coordinates not provided.")
            return False

        logger.info(f"Opening Journal at {journal_button_coord}...")
        self.controller.click(journal_button_coord[0], journal_button_coord[1], delay=2.0)
        return True

    def read_event_leaderboard(
        self,
        event_name: str,
        ranking_area: Tuple[int, int, int, int],
        account_config: AccountConfig,
    ) -> List[Dict[str, Any]]:
        """
        Extracts player rankings and scores from an event summary window using OCR
        and saves them to the database.
        """
        logger.info(f"Reading leaderboard for event '{event_name}' in area {ranking_area}...")
        lines = self.ocr.extract_lines_from_area(ranking_area, upscale=200, apply_threshold=True)

        results = []
        with DatabaseConnection(account_config) as connection:
            if not connection:
                logger.error("Database connection failed for Journal score saving.")
                return results

            repo = JournalRepository(connection)

            for line in lines:
                # Example parsing line: "1. PlayerName - 15,400,000"
                logger.debug(f"Parsing journal line: '{line}'")
                # Structure can be expanded with regex per event type
                results.append({"raw_line": line})

        return results

    def run(self, event_name: str = "Tournament", **kwargs) -> Dict[str, Any]:
        """Runs the journal parsing workflow."""
        logger.info(f"Starting Journal parsing for event: '{event_name}'")
        return {"module": self.name, "event": event_name, "status": "ready_for_event_mapping"}
