"""Where the Journal's controls are: the module's calibration steps, and what is derived from them.

Only needed to picture a tournament type met for the first time: the
rankings themselves come over the network (feed.py). Only the first two cards are
marked: the distance between them places every other card of the page.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from core.calibration import Step

Point = Tuple[int, int]
Region = Tuple[int, int, int, int]

DEFAULT_CARDS_PER_PAGE = 5

STEPS = (
    Step("journal_button", "click", "Journal button",
         "On the game's main screen, click the button that opens the JOURNAL.\n"
         "Close the gifts panel first if it is still open from the previous steps.",
         acts=True),
    Step("journal_events_tab", "click", "Journal: events tab",
         "In the Journal, click the tab where the tournament results appear\n"
         "(\"Your Clanmates' results in ...\"). Skip if the Journal already opens on it.",
         optional=True, acts=True),
    Step("journal_card1_title", "area", "Journal: title of the 1st card",
         "Mark the area of the TITLE of the FIRST card of the list (where\n"
         "\"Your Clanmates' results in <event>\" appears). Take the whole line, with some margin.\n"
         "Click two opposite corners."),
    Step("journal_card2_title", "area", "Journal: title of the 2nd card",
         "Mark the same area, now on the SECOND card. The distance between the two\n"
         "is used to find the other cards of the page."),
    Step("journal_card_icon", "area", "Journal: icon of the 1st card",
         "Mark the ICON of the first card, tightly. It becomes the tournament's picture\n"
         "in the site's catalogue when the tournament has none yet.",
         optional=True),
    # Not pressed by the wizard: the rest of the calibration goes on from page 1.
    Step("journal_next_page", "click", "Journal: next page",
         "Click the arrow that goes to the NEXT page of the Journal.\n"
         "It is not pressed now: the rest of the calibration goes on from page 1."),
)

REQUIRED_STEPS = tuple(step.name for step in STEPS if not step.optional)


class JournalLayout:
    """The calibrated Journal, with the cards the calibration did not mark derived from the two it did."""

    def __init__(self, calibration, cards_per_page: int = DEFAULT_CARDS_PER_PAGE):
        self.calibration = calibration
        self.cards_per_page = max(1, int(cards_per_page))

    def missing_steps(self) -> List[str]:
        return self.calibration.require(*REQUIRED_STEPS)

    # --------------------------------------------------------- the calibration
    def point(self, name: str) -> Optional[Point]:
        return self.calibration.point(name)

    def region(self, name: str) -> Optional[Region]:
        return self.calibration.region(name)

    # --------------------------------------------------------------- the cards
    def card_pitch(self) -> Optional[int]:
        first, second = self.region("journal_card1_title"), self.region("journal_card2_title")
        if not first or not second:
            return None
        pitch = second[1] - first[1]
        return pitch if pitch > 5 else None

    def page_region(self) -> Optional[Region]:
        """The cards of a page, each `card_pitch` tall, with their icons when those are calibrated."""
        first, pitch = self.region("journal_card1_title"), self.card_pitch()
        if not first or pitch is None:
            return None
        icon = self.region("journal_card_icon")
        left = min(first[0], icon[0]) if icon else first[0]
        return left, first[1], first[0] + first[2] - left, pitch * self.cards_per_page

    def card_icon_region(self, index: int) -> Optional[Region]:
        icon, pitch = self.region("journal_card_icon"), self.card_pitch()
        if not icon or pitch is None:
            return None
        return icon[0], icon[1] + pitch * index, icon[2], icon[3]
