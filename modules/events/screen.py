"""
A picture of a page of Journal cards, for the one thing the network cannot say: the
name of a tournament type the site has never heard of. The game sends the type as a
number, and the text exists only in what it draws.

Nothing here reads that text, and nothing is opened: the Journal is paged to the card
and the page goes to the site with that card outlined, for a person to name the type.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

import cv2

from utils.cancel import cancellation
from utils.logger import logger

from .layout import JournalLayout

STEP_WAIT = 1.2

OUTLINE_COLOUR = (0, 200, 255)   # blue, green, red
OUTLINE_WIDTH = 2


class CardPicture(NamedTuple):
    page_png: bytes
    icon_png: Optional[bytes]


def _png(image) -> Optional[bytes]:
    encoded, png = cv2.imencode(".png", image)
    return png.tobytes() if encoded else None


class JournalScreen:
    def __init__(self, context, layout: JournalLayout):
        self.ctx = context
        self.layout = layout

    def picture(self, page: int, card: int) -> Optional[CardPicture]:
        """A page of the Journal (0 is the first) with one of its cards (0 is the first) outlined; None if it could not look."""
        missing = self.layout.missing_steps()
        if missing:
            logger.warning(f"The Journal cannot be pictured without calibrating: {self.ctx.calibration.titles(missing)}.")
            return None

        browser, game_state = self.ctx.browser, self.ctx.game_state
        try:
            self.ctx.sync_viewport()
            game_state.prepare_board()   # the store comes in front of the game after a load
            for step in ("journal_button", "journal_events_tab"):
                point = self.layout.point(step)
                if point is not None:
                    browser.click(*point)
                    cancellation.sleep(STEP_WAIT)
            for _turn in range(page):
                browser.click(*self.layout.point("journal_next_page"))
                cancellation.sleep(STEP_WAIT)
            return self._capture(card)
        finally:
            game_state.close_modals()

    def _capture(self, card: int) -> Optional[CardPicture]:
        region = self.layout.page_region()
        pitch = self.layout.card_pitch()
        if region is None or pitch is None:
            logger.warning("The two card titles of the Journal are not calibrated one below the other.")
            return None
        image = self.ctx.browser.capture(region, scale=1.0)
        if image is None or image.size == 0:
            logger.warning(f"Empty capture of the Journal {region}.")
            return None

        top = pitch * card
        cv2.rectangle(image, (0, top), (image.shape[1] - 1, min(top + pitch, image.shape[0]) - 1),
                      OUTLINE_COLOUR, OUTLINE_WIDTH)
        page_png = _png(image)
        return CardPicture(page_png, self._icon(card)) if page_png else None

    def _icon(self, card: int) -> Optional[bytes]:
        region = self.layout.card_icon_region(card)
        if region is None:
            return None
        image = self.ctx.browser.capture(region, scale=1.0)
        return _png(image) if image is not None else None
