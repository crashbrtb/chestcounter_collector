"""
Where the game's controls are - learned, never hard-coded.

The old position.cfg carried screen coordinates typed in by hand. Every layout
change, every different monitor, meant editing an INI file and guessing pixel
values. Here the positions are recorded once by clicking on a screenshot of the
game, and stored in config/calibration.json.

Three kinds of step
-------------------
click   a single point to click.
area    a rectangle only - where to look for text or for a button. Nothing is
        remembered about what it looked like.
region  a rectangle AND a crop of what was inside it. The crop becomes a
        reference image: at run time we search for that picture instead of
        trusting a fixed position, which is what lets the 'Open' button be found
        as the list shifts.

Viewport independence
---------------------
The viewport size at calibration time is stored alongside the coordinates. If
the browser window is a different size on the day of the run, points and
regions are rescaled by the ratio - and reference images are searched for at a
matching ladder of scales (see vision.py). A window resize therefore degrades
accuracy instead of breaking the run outright.
"""

import json
import os
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    cv2 = None
    np = None

from config.settings import CALIBRATION_FILE_PATH, CALIBRATION_REFS_DIR
from utils.logger import logger

Point = Tuple[int, int]
Region = Tuple[int, int, int, int]      # (left, top, width, height)

# Below this much variation, a reference image has nothing to recognise it by.
# Template matching scores a featureless crop ~1.0 against ANY flat background,
# so such a reference would be "found" everywhere, forever. Measured: a crop of
# flat panel scores 4 here, a crop of a real button with its label scores 32.
MIN_REFERENCE_CONTRAST = 10.0


class Step(NamedTuple):
    name: str
    type: str                # click | area | region
    title: str
    instruction: str
    optional: bool = False

    @property
    def is_rectangle(self) -> bool:
        return self.type in ("area", "region")

    @property
    def keeps_reference(self) -> bool:
        return self.type == "region"


# The order is the order of a real collection run: each step leaves the game in
# the state the next one expects, so calibrating means playing through it once.
STEPS: List[Step] = [
    Step("profile_menu_button", "click", "Profile menu",
         "In the logged-in game, click the button that opens the PROFILES (cities) list."),
    Step("profiles_list_area", "area", "Profiles list",
         "Mark the AREA where profile names appear.\n"
         "This is where each profile's configured name will be searched.\n"
         "Click two opposite corners."),
    Step("profile_switch_confirm", "region", "Confirm switch button",
         "Mark the BUTTON that confirms switching profile.\n"
         "Its image is saved so it can be found even if its position shifts."),
    Step("profile_name_display_area", "area", "Active profile name",
         "Once inside a profile, mark the area where the game displays the ACTIVE profile name.\n"
         "This verifies that switching succeeded."),
    Step("clan_button", "click", "Clan button",
         "Click the button that opens the CLAN menu."),
    Step("gift_button", "click", "Gifts button",
         "Inside the clan menu, click GIFTS."),
    Step("gifts_tab", "click", "Gifts tab",
         "Click the standard gifts tab."),
    Step("triumphal_gifts_tab", "click", "Triumphal gifts tab",
         "Click the triumphal gifts tab."),
    Step("chest_area", "area", "Chest text",
         "Mark the area where the CHEST NAME, player, and source appear.\n"
         "This text is saved to the database — leave some margin."),
    Step("open_button_area", "area", "Search area for 'Open' button",
         "Mark the area where the OPEN chest button appears.\n"
         "Can be generous: the button is located by image matching inside it."),
    Step("open_button", "region", "Open button",
         "Mark only the button to open the chest, tightly.\n"
         "This image is the reference searched for each chest."),
    Step("store_marker", "region", "Store open (optional)",
         "Leave the in-game STORE open and mark a fixed part of it (such as the title).\n"
         "This is how the collector knows the store is blocking the screen.\n"
         "Skip if you do not need this protection.",
         optional=True),
    Step("store_close_button", "click", "Close store (optional)",
         "Click the X that CLOSES the store.",
         optional=True),
]

STEPS_BY_NAME = {step.name: step for step in STEPS}


class Calibration:
    """The calibrated positions, rescaled to whatever viewport the run happens to have."""

    def __init__(self, path: str = CALIBRATION_FILE_PATH, refs_dir: str = CALIBRATION_REFS_DIR):
        self.path = path
        self.refs_dir = refs_dir
        self.points: Dict[str, Point] = {}
        self.regions: Dict[str, Dict[str, int]] = {}
        self.viewport: Tuple[int, int] = (0, 0)
        # The page size each individual step was recorded at. A calibration
        # session can span several captures, and the window may change size
        # between them; keeping only one viewport for the whole file silently
        # mixed coordinate systems - points recorded at 1920 wide were stored as
        # if they had been taken at 1536, and landed outside the page.
        self.step_viewports: Dict[str, Tuple[int, int]] = {}
        self.created_at: str = ""
        self._current_viewport: Tuple[int, int] = (0, 0)
        self.load()

    # ------------------------------------------------------------ persistence
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        self.points = {k: (int(v[0]), int(v[1])) for k, v in (data.get("points") or {}).items()}
        self.regions = data.get("regions") or {}
        self.step_viewports = {k: (int(v[0]), int(v[1]))
                               for k, v in (data.get("step_viewports") or {}).items()}
        stored = data.get("viewport") or {}
        self.viewport = (int(stored.get("width", 0)), int(stored.get("height", 0)))
        self.created_at = data.get("created_at", "")

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        payload = {
            "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
            "points": {k: list(v) for k, v in self.points.items()},
            "regions": self.regions,
            "step_viewports": {k: list(v) for k, v in self.step_viewports.items()},
            "created_at": self.created_at or time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------ state
    @property
    def exists(self) -> bool:
        return os.path.exists(self.path)

    def is_calibrated(self, name: str) -> bool:
        step = STEPS_BY_NAME.get(name)
        if step is None:
            return False
        return name in (self.regions if step.is_rectangle else self.points)

    def missing_steps(self, include_optional: bool = False) -> List[str]:
        return [
            step.name for step in STEPS
            if (include_optional or not step.optional) and not self.is_calibrated(step.name)
        ]

    @property
    def complete(self) -> bool:
        return not self.missing_steps()

    # ----------------------------------------------------------------- scaling
    def use_viewport(self, width: int, height: int):
        """Tells the calibration which viewport the current run is using."""
        self._current_viewport = (int(width), int(height))
        if self.viewport[0] and self.viewport[1] and (width, height) != self.viewport:
            logger.warning(
                f"Viewport is {width}x{height} but calibration was made at "
                f"{self.viewport[0]}x{self.viewport[1]}; coordinates will be rescaled. "
                f"Recalibrate if clicks land in the wrong place."
            )

    def _factors(self, name: Optional[str] = None) -> Tuple[float, float]:
        """How much to stretch a step, from the page size IT was recorded at."""
        calibrated_w, calibrated_h = self.step_viewports.get(name, self.viewport) if name             else self.viewport
        current_w, current_h = self._current_viewport
        if not (calibrated_w and calibrated_h and current_w and current_h):
            return 1.0, 1.0
        return current_w / calibrated_w, current_h / calibrated_h

    @property
    def image_scale(self) -> float:
        """How much a reference image has to grow or shrink for the current viewport."""
        scale_x, scale_y = self._factors()
        return min(scale_x, scale_y)

    # ------------------------------------------------------------------ values
    def point(self, name: str) -> Optional[Point]:
        raw = self.points.get(name)
        if not raw:
            return None
        scale_x, scale_y = self._factors(name)
        return (int(round(raw[0] * scale_x)), int(round(raw[1] * scale_y)))

    def region(self, name: str) -> Optional[Region]:
        raw = self.regions.get(name)
        if not raw:
            return None
        scale_x, scale_y = self._factors(name)
        return (
            int(round(raw["left"] * scale_x)),
            int(round(raw["top"] * scale_y)),
            max(1, int(round(raw["width"] * scale_x))),
            max(1, int(round(raw["height"] * scale_y))),
        )

    def region_center(self, name: str) -> Optional[Point]:
        region = self.region(name)
        if not region:
            return None
        return (region[0] + region[2] // 2, region[1] + region[3] // 2)

    def require(self, *names: str) -> List[str]:
        """Names among these that are not calibrated - for a clear message before starting."""
        return [name for name in names if not self.is_calibrated(name)]

    # -------------------------------------------------------------- recording
    def ref_path(self, name: str) -> str:
        return os.path.join(self.refs_dir, f"{name}.png")

    def reference(self, name: str):
        path = self.ref_path(name)
        if cv2 is None or not os.path.exists(path):
            return None
        return cv2.imread(path, cv2.IMREAD_COLOR)

    def set_viewport(self, width: int, height: int):
        self.viewport = (int(width), int(height))

    def set_point(self, name: str, x: int, y: int):
        self.points[name] = (int(x), int(y))
        if self.viewport[0] and self.viewport[1]:
            self.step_viewports[name] = self.viewport

    def set_region(self, name: str, corner_a: Point, corner_b: Point, image=None) -> Optional[str]:
        """
        Stores a rectangle, and for 'region' steps the crop inside it.

        Corners may be given in any order - the user drags whichever way feels
        natural, and a negative width would silently produce an empty capture.

        Returns a warning when the crop has too little contrast to be a usable
        reference, rather than refusing it: the user can see the area and may
        know better, but a featureless reference is found everywhere and would
        turn into a store that is always "open" or an Open button that never
        runs out.
        """
        left, right = sorted((int(corner_a[0]), int(corner_b[0])))
        top, bottom = sorted((int(corner_a[1]), int(corner_b[1])))
        width, height = right - left, bottom - top
        if width < 6 or height < 6:
            raise ValueError("the marked area is too small — mark two opposite corners")

        warning = None
        step = STEPS_BY_NAME.get(name)
        if step is not None and step.keeps_reference:
            if image is None or cv2 is None:
                raise ValueError("could not crop reference image from this area")
            os.makedirs(self.refs_dir, exist_ok=True)
            crop = image[top:bottom, left:right]
            if crop.size == 0:
                raise ValueError("the marked area is outside the capture")
            cv2.imwrite(self.ref_path(name), crop)

            contrast = float(np.std(crop)) if np is not None else 100.0
            if contrast < MIN_REFERENCE_CONTRAST:
                warning = (f"⚠ this area has almost no detail (contrast {contrast:.0f}). A featureless "
                           f"image is 'found' anywhere on flat backgrounds. Mark something with "
                           f"graphics or text — the button border, its label.")

        self.regions[name] = {"left": left, "top": top, "width": width, "height": height}
        if self.viewport[0] and self.viewport[1]:
            self.step_viewports[name] = self.viewport
        return warning

    def out_of_bounds(self, width: int = 0, height: int = 0) -> List[str]:
        """
        Steps whose coordinates fall outside the page.

        A click beyond the page edge reaches nothing at all, and looks exactly
        like a button that did not respond - which is how a menu that never
        opened was mistaken for a game that ignores automated clicks.
        """
        page_w = width or self._current_viewport[0] or self.viewport[0]
        page_h = height or self._current_viewport[1] or self.viewport[1]
        if not (page_w and page_h):
            return []

        outside = []
        for name in self.points:
            point = self.point(name)
            if point and not (0 <= point[0] < page_w and 0 <= point[1] < page_h):
                outside.append(name)
        for name in self.regions:
            region = self.region(name)
            if region and (region[0] >= page_w or region[1] >= page_h
                           or region[0] + region[2] <= 0 or region[1] + region[3] <= 0):
                outside.append(name)
        return outside

    def rescale(self, factor: float) -> int:
        """
        Divides every recorded coordinate - and every reference image - by `factor`.

        Exists for one specific accident: captures used to come back at the
        display scaling (1.25x on a 125% Windows desktop) while clicks take CSS
        pixels, so everything marked on those images was recorded that much too
        large. The mistake is uniform, so undoing it is arithmetic rather than
        recalibration - which would otherwise mean redoing all thirteen steps.
        """
        if factor <= 0 or abs(factor - 1.0) < 0.001:
            return 0

        changed = 0
        for name, (x, y) in list(self.points.items()):
            self.points[name] = (int(round(x / factor)), int(round(y / factor)))
            changed += 1

        for name, region in list(self.regions.items()):
            self.regions[name] = {
                "left": int(round(region["left"] / factor)),
                "top": int(round(region["top"] / factor)),
                "width": max(1, int(round(region["width"] / factor))),
                "height": max(1, int(round(region["height"] / factor))),
            }
            changed += 1

            # The reference image was cropped from the same oversized capture,
            # so it has to shrink by the same amount or it will never match.
            path = self.ref_path(name)
            if cv2 is not None and os.path.exists(path):
                image = cv2.imread(path, cv2.IMREAD_COLOR)
                if image is not None:
                    size = (max(1, int(round(image.shape[1] / factor))),
                            max(1, int(round(image.shape[0] / factor))))
                    cv2.imwrite(path, cv2.resize(image, size, interpolation=cv2.INTER_AREA))

        return changed

    def forget(self, name: str):
        """Drops one step, so it can be recalibrated without redoing the whole sequence."""
        self.points.pop(name, None)
        self.regions.pop(name, None)
        self.step_viewports.pop(name, None)
        path = self.ref_path(name)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        self.save()

    def clear(self):
        self.points.clear()
        self.regions.clear()
        self.step_viewports.clear()
        self.viewport = (0, 0)
        self.created_at = ""
        self.save()

    def summary(self) -> Dict[str, Any]:
        return {
            "created_at": self.created_at,
            "viewport": self.viewport,
            "points": len(self.points),
            "regions": len(self.regions),
            "missing": self.missing_steps(),
        }
