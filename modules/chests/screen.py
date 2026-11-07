"""
A picture of a chest on the gifts panel, for the one thing the network cannot say:
the name of a chest type met for the first time. The game sends the type as a
number, and the text exists only in what it draws.

Nothing here reads that text. The picture goes with the chest to the site's review
queue, where a person names the type once, for all its chests.

The panel is opened afresh for each picture, so it shows the list as it is now,
and scrolled to the chest asked for: the list is in the order of the gifts, so a
chest's place in it is known. The picture is of that chest alone, as whoever names
the type should see no other, and nothing is clicked but the way into the panel.
"""

from typing import List, Optional

import cv2

from utils.cancel import cancellation
from utils.logger import logger

from .gifts import PLAIN_GIFT

# What one wheel event moves the panel, against the height of one of its chests:
# measured on the real panel, 40 px and 80 px.
WHEELS_PER_CHEST = 2


class ChestScreen:
    """The gifts panel of the game, as ChestCollector's context drives it."""

    STEPS = ("clan_button", "gift_button", "gifts_tab", "chest_area", "open_button_area", "open_button")

    def __init__(self, context):
        self.ctx = context

    def picture(self, position: int, kind: int = PLAIN_GIFT) -> Optional[bytes]:
        """
        A PNG of the chest at `position` of its tab's list (0 is the oldest); None if it could not look.

        `kind` is the gift's tab: a triumphal gift is pictured on the Triumphal Gifts tab.
        """
        calibration, browser, game_state = self.ctx.calibration, self.ctx.browser, self.ctx.game_state
        steps = self.STEPS if kind == PLAIN_GIFT else self.STEPS + ("triumphal_gifts_tab",)
        missing = calibration.require(*steps)
        if missing:
            logger.warning(f"The gifts panel cannot be pictured without calibrating: {calibration.titles(missing)}.")
            return None

        try:
            self.ctx.sync_viewport()
            game_state.prepare_board()   # the store comes in front of the game after a load
            clicks = [("clan_button", 2.5), ("gift_button", 2.0), ("gifts_tab", 2.0)]
            if kind != PLAIN_GIFT:
                clicks.append(("triumphal_gifts_tab", 2.0))
            for step, wait in clicks:
                point = calibration.point(step)
                browser.click(point[0], point[1], delay=wait)
            area = calibration.region("chest_area")
            if position:
                browser.scroll(area[0] + area[2] // 2, area[1] + area[3] // 2, delta_y=400,
                               repetitions=position * WHEELS_PER_CHEST, delay=0.01)
                cancellation.sleep(1.5)
            return self._capture_panel()
        finally:
            game_state.close_modals()

    def _chest_tops(self) -> List[int]:
        """Where the text of each chest on screen starts, a fixed distance from its 'Open' button; first one first."""
        calibration, browser = self.ctx.calibration, self.ctx.browser
        chest_area, button_area = calibration.region("chest_area"), calibration.region("open_button_area")
        _view_w, view_h = browser.viewport()
        top = max(0, button_area[1] - 10)
        column = (button_area[0], top, button_area[2], max(button_area[3], view_h - top))
        buttons = self.ctx.vision.find_all(calibration.ref_path("open_button"), column,
                                           base_scale=calibration.image_scale)
        offset = chest_area[1] - button_area[1]
        return sorted(button[1] + offset for button in buttons)

    def _capture_panel(self) -> Optional[bytes]:
        tops = self._chest_tops()
        if not tops:
            logger.warning("No 'Open' button found on the gifts panel.")
            return None

        left, _top, width, height = self.ctx.calibration.region("chest_area")
        chest = (left, tops[0], width, height)
        image = self.ctx.browser.capture(chest, scale=1.0)
        if image is None or image.size == 0:
            logger.warning(f"Empty capture of the gifts panel {chest}.")
            return None

        encoded, png = cv2.imencode(".png", image)
        return png.tobytes() if encoded else None
