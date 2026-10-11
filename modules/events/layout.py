"""Where the Journal's controls are: the module's calibration steps, and what is derived from them.

The steps are in the order of one pass through the Journal, so calibrating
means walking through it once. Only the first two cards are marked: the
distance between them places every other card of the page.
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
    Step("journal_page_area", "area", "Journal: page number",
         "Mark where the PAGE NUMBER appears, between the arrows.\n"
         "It tells when the last page has been reached.",
         optional=True),
    # Not pressed by the wizard: the rest of the calibration goes on from page 1.
    Step("journal_next_page", "click", "Journal: next page",
         "Click the arrow that goes to the NEXT page of the Journal.\n"
         "It is not pressed now: the rest of the calibration goes on from page 1."),
    Step("journal_card_open", "click", "Journal: open the 1st card",
         "Click the FIRST card to open it (preferably a tournament result).\n"
         "The wizard opens the card before the next step.",
         acts=True),
    Step("journal_show_details", "region", "Journal: Show details button",
         "With the card open, mark only the \"SHOW DETAILS\" button, tightly.\n"
         "Its picture is searched for on screen, because its position changes from one tournament to another.",
         acts=True),
    Step("journal_ranking_list", "area", "Journal: ranking list",
         "With the ranking open, mark the area of the LIST OF PLAYERS (the rows with name and points).\n"
         "The list is scrolled to its end: the game only sends the names of the players that are on screen.",
         optional=True),
    Step("journal_details_close", "click", "Journal: close the ranking",
         "With the ranking open, click the X (or back) that CLOSES the ranking.",
         acts=True),
    Step("journal_card_close", "click", "Journal: close the card",
         "Click the X that closes the card and goes back to the Journal list.\n"
         "Skip if the Esc key already does that in the game.",
         optional=True, acts=True),
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

    def region_center(self, name: str) -> Optional[Point]:
        return self.calibration.region_center(name)

    def ref_path(self, name: str) -> str:
        return self.calibration.ref_path(name)

    @property
    def image_scale(self) -> float:
        return self.calibration.image_scale

    # --------------------------------------------------------------- the cards
    def card_pitch(self) -> Optional[int]:
        first, second = self.region("journal_card1_title"), self.region("journal_card2_title")
        if not first or not second:
            return None
        pitch = second[1] - first[1]
        return pitch if pitch > 5 else None

    def card_title_region(self, index: int) -> Optional[Region]:
        first, pitch = self.region("journal_card1_title"), self.card_pitch()
        if not first or pitch is None:
            return None
        return first[0], first[1] + pitch * index, first[2], first[3]

    def card_icon_region(self, index: int) -> Optional[Region]:
        icon, pitch = self.region("journal_card_icon"), self.card_pitch()
        if not icon or pitch is None:
            return None
        return icon[0], icon[1] + pitch * index, icon[2], icon[3]

    def card_click_point(self, index: int) -> Optional[Point]:
        """Where to click card `index`: the calibrated click, moved down by the pitch."""
        base, pitch = self.point("journal_card_open"), self.card_pitch()
        if base is None:
            base = self.region_center("journal_card1_title")
        if base is None or pitch is None:
            return None
        return base[0], base[1] + pitch * index
