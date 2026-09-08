"""
Journal module - event scores read from the in-game journal.

Still a placeholder, as it was before the browser migration, but now honest
about it: it needs its own calibration steps (the journal button and the
ranking area), and those do not exist in core/calibration.py yet. Rather than
click at coordinates nobody recorded, it says what is missing and stops.

Adding it later means: two entries in STEPS, and filling in the parsing of a
ranking line below.
"""

from typing import Any, Dict, List

from database.db_connection import DatabaseConnection
from database.repositories import JournalRepository
from utils.logger import logger

from .base_module import BaseModule

REQUIRED_STEPS = ("journal_button", "journal_ranking_area")


class JournalParser(BaseModule):
    @property
    def name(self) -> str:
        return "JournalParser"

    def read_leaderboard(self, event_name: str, profile) -> List[Dict[str, Any]]:
        """Reads the ranking area and records one row per player."""
        area = self.calibration.region("journal_ranking_area")
        if not area:
            return []

        rows = self.ocr.read_rows(area)
        recorded = []
        with DatabaseConnection(profile.database, profile.label) as connection:
            if not connection:
                return []
            repo = JournalRepository(connection)
            for row in rows:
                logger.debug(f"Journal row: '{row}'")
                recorded.append({"raw": row, "event": event_name})
            _ = repo  # rows are not parsed into scores yet
        return recorded

    def run(self, event_name: str = "Tournament", **kwargs) -> Dict[str, Any]:
        missing = self.calibration.require(*REQUIRED_STEPS)
        if missing:
            logger.warning(
                f"O módulo Diário ainda não tem passos de calibração ({', '.join(missing)}). "
                f"Nada foi lido."
            )
            return {"module": self.name, "success": False, "reason": "sem calibração"}
        return {"module": self.name, "success": False, "reason": "leitura de ranking ainda não implementada"}
