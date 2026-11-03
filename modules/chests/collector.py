"""
The clan chests of one profile: every gift waiting is written to the profile's
database and opened, over the network (see from_game.py). The screen is only read
for the name of a chest type met for the first time, so a collection does not
depend on the calibration until that happens.
"""

from typing import Any, Dict, List, Optional

from config.schema import Field
from config.settings import ProfileConfig
from database.db_connection import DatabaseConnection
from database.job_runs import JobRunReporter
from database.repositories import ChestRepository
from utils.logger import logger

from ..base_module import BaseModule
from .from_game import GameCollection
from .game_link import GameLink
from .screen import ChestScreen
from .steps import STEPS


class ChestCollector(BaseModule):
    """Collects the clan's gifts for a profile."""

    key = "chests"
    title = "Clan chests"
    description = ("Opens every clan gift of each profile and records it in that profile's database. "
                   "A profile without a database is skipped.")
    steps = STEPS
    settings = (
        # Measured on a recorded run of 856 chests opened by clicking: 1.3 per
        # second on average, 4 at the most in any one second. One request at a
        # time, each waiting for its answer, is a further limit under this one.
        Field("open_interval", "float", "Time between chests (s)", 0.2,
              "The least time between asking the game to open one gift and the next. Each one also waits "
              "for the game's answer, so 0.2 means five per second at the very most.",
              minimum=0.2, maximum=5.0),
    )

    def __init__(self, context):
        super().__init__(context)
        self._reporter: Optional[JobRunReporter] = None

    @classmethod
    def configured_for(cls, profile: ProfileConfig) -> bool:
        return profile.has_database

    # -------------------------------------------------------------- heartbeat
    def begin_run(self, profiles: List[ProfileConfig]):
        """Opens the run's row in the site's `job_runs`, in every database it will write to."""
        self._reporter = JobRunReporter(profiles)
        self._reporter.start()

    def end_run(self, results: List[Dict[str, Any]], duration: str = "",
                error: Optional[str] = None, cancelled: bool = False):
        if self._reporter is not None:
            self._reporter.finish(results, duration=duration, error=error, cancelled=cancelled)
            self._reporter = None

    # -------------------------------------------------------------------- run
    def run_for_profile(self, profile: ProfileConfig) -> Dict[str, Any]:
        """Collects the gifts of one profile and writes them to its database."""
        result = {"success": False, "collected": 0, "incomplete": 0, "profile": profile.name,
                  "account": profile.account_name}

        if not profile.has_database:
            logger.error(f"Profile '{profile.label}' has no database configured; skipping it.")
            return dict(result, reason="no database configured")

        with DatabaseConnection(profile.database, profile.label) as connection:
            if not connection:
                return dict(result, reason="database connection failure")

            repo = ChestRepository(connection)
            if not repo.can_read_from_game():
                logger.error(f"[{profile.label}] This database has no game_chest_types table or no game columns "
                             f"in collected_chests: update the site and run its migrations.")
                return dict(result, reason="the site's database is too old")

            link = GameLink(host=self.browser.host, port=self.browser.port, tab_id=self.browser.tab_id,
                            url_filter=self.browser.url_filter,
                            on_status=lambda text: logger.warning(f"Game link: {text}"))
            if not link.start():
                return dict(result, reason="could not talk to the game")
            try:
                clan_id = link.clan_id()
                if clan_id is None:
                    return dict(result, reason="the game says this profile has no clan")
                interval = float(self.options.get("open_interval", 0.2))
                collection = GameCollection(profile.label, repo, link, clan_id, interval, ChestScreen(self.ctx).picture)
                collected, incomplete, finished = collection.run()
            finally:
                link.stop()

        logger.info(f"Profile '{profile.label}': {collected} chests collected, {incomplete} incomplete.")
        result.update(success=finished, collected=collected, incomplete=incomplete)
        if not finished:
            result["reason"] = collection.problem or "the collection did not finish"
        return result
