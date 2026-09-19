"""
Finding a button by the picture of it taken during calibration.

Template matching has no scale invariance: a reference cropped from a 1920-wide
viewport scores near zero against the same button in a 1536-wide one, and the
collector would sit there clicking nothing. So every search sweeps a short
ladder of scales around the ratio between the calibrated viewport and the
current one.

Rescaling a template always costs some correlation - interpolation never
reproduces the original pixels - so a resized template competes against a
slightly lower bar (`vision.scaled_threshold_relief`). Without that discount a
correctly located button scored 0.79 against a 0.80 threshold and was thrown
away.
"""

import os
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

from utils.logger import logger

Region = Tuple[int, int, int, int]      # (left, top, width, height) in page CSS pixels
Match = Tuple[int, int, int, int]       # (left, top, width, height) of what was found


def changed_fraction(before, after, tolerance: int = 12) -> float:
    """
    How much of the screen changed between two captures, from 0 to 1.

    'The click did nothing' is the hardest thing to diagnose from a log, because
    a click that misses and a click that lands on a dead area look identical:
    both are silent. Comparing the screen before and after turns that into a
    number - a menu opening moves a good part of the screen, while a click into
    the void moves almost nothing.

    The tolerance ignores the flicker of animations and anti-aliasing, which are
    always a pixel or two different even on a still screen.
    """
    if before is None or after is None or cv2 is None:
        return 0.0
    if before.shape != after.shape or before.size == 0:
        return 1.0
    difference = cv2.absdiff(before, after)
    if difference.ndim == 3:
        difference = difference.max(axis=2)
    return float((difference > tolerance).mean())


class Vision:
    """Template search inside a region of the game page."""

    def __init__(self, browser, config: Dict[str, Any]):
        self.browser = browser
        self.threshold = float(config.get("match_threshold", 0.80))
        self.relief = float(config.get("scaled_threshold_relief", 0.06))
        self.max_attempts = int(config.get("max_attempts", 3))
        self.retry_interval = float(config.get("retry_interval", 0.4))
        self._cache: Dict[Tuple[str, float], Optional[np.ndarray]] = {}
        self.last_score: float = 0.0

    # -- templates
    def load_template(self, path: str, scale: float = 1.0) -> Optional[np.ndarray]:
        key = (os.path.abspath(path), round(float(scale), 3))
        if key in self._cache:
            return self._cache[key]

        image = cv2.imread(path, cv2.IMREAD_GRAYSCALE) if cv2 is not None else None
        if image is None:
            logger.error(f"Reference image could not be read: {path}")
            self._cache[key] = None
            return None

        if abs(scale - 1.0) > 0.01:
            width = max(1, int(round(image.shape[1] * scale)))
            height = max(1, int(round(image.shape[0] * scale)))
            interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            image = cv2.resize(image, (width, height), interpolation=interpolation)

        self._cache[key] = image
        return image

    @staticmethod
    def scales_for(base: float) -> Tuple[float, ...]:
        """
        Scales tried, most likely first.

        The ladder is fine near the nominal ratio because game interfaces do not
        scale perfectly linearly with the viewport: a 2% error already costs a
        60px icon enough correlation to miss.
        """
        factors = (1.0, 0.98, 1.02, 0.95, 1.05) if abs(base - 1.0) < 0.02 else \
                  (1.0, 0.98, 1.02, 0.95, 1.05, 0.92, 1.08, 0.9, 1.1)
        scales = []
        for factor in factors:
            value = round(base * factor, 3)
            if value > 0 and value not in scales:
                scales.append(value)
        if 1.0 not in scales:
            scales.append(1.0)
        return tuple(scales)

    def _fit_region(self, region: Optional[Region], template_w: int, template_h: int) -> Optional[Region]:
        """
        Grows the search region around its own centre until the template fits.

        A region calibrated snugly around a button becomes smaller than that
        button once it is rescaled down for a smaller window. Template matching
        then has nothing to slide the template over and simply finds nothing -
        which the collector reads as 'no chests left' and stops. Growing the
        region costs a slightly larger capture and removes that silent stop.
        """
        if region is None:
            return None

        left, top, width, height = region
        needed_w = int(template_w * 1.2)
        needed_h = int(template_h * 1.2)
        if width >= needed_w and height >= needed_h:
            return region

        if width < needed_w:
            left -= (needed_w - width) // 2
            width = needed_w
        if height < needed_h:
            top -= (needed_h - height) // 2
            height = needed_h

        view_w, view_h = self.browser.viewport()
        left = max(0, left)
        top = max(0, top)
        if view_w and view_h:
            width = min(width, view_w - left)
            height = min(height, view_h - top)
        logger.debug(f"Search region enlarged to fit the reference image: {(left, top, width, height)}")
        return (left, top, max(1, width), max(1, height))

    # -- search
    def find(
        self,
        template_path: str,
        region: Optional[Region] = None,
        base_scale: float = 1.0,
        threshold: Optional[float] = None,
        attempts: Optional[int] = None,
    ) -> Optional[Match]:
        """Looks for the reference image inside a page region; returns its box in page pixels."""
        if cv2 is None:
            logger.error("opencv-python not installed; template matching is disabled.")
            return None
        if not os.path.exists(template_path):
            logger.error(f"Reference image is missing: {template_path}")
            return None

        limit = self.threshold if threshold is None else threshold
        tries = self.max_attempts if attempts is None else attempts
        scales = self.scales_for(base_scale)

        largest = self.load_template(template_path, max(scales))
        if largest is not None:
            region = self._fit_region(region, largest.shape[1], largest.shape[0])

        for attempt in range(max(1, tries)):
            screenshot = self.browser.capture(region, scale=1.0)
            if screenshot is None or screenshot.size == 0:
                return None
            haystack = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)

            best_margin = None
            best: Optional[Match] = None
            for scale in scales:
                template = self.load_template(template_path, scale)
                if template is None:
                    return None
                height, width = template.shape[:2]
                if height > haystack.shape[0] or width > haystack.shape[1]:
                    continue

                result = cv2.matchTemplate(haystack, template, cv2.TM_CCOEFF_NORMED)
                # Flat areas (the black beyond the page edge) produce NaN here.
                result = np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0)
                _, score, _, location = cv2.minMaxLoc(result)

                bar = limit if abs(scale - 1.0) <= 0.01 else max(0.0, limit - self.relief)
                margin = score - bar
                if best_margin is None or margin > best_margin:
                    best_margin = margin
                    self.last_score = float(score)
                    offset_x = region[0] if region else 0
                    offset_y = region[1] if region else 0
                    best = (offset_x + location[0], offset_y + location[1], width, height)
                if margin >= 0:
                    break

            if best_margin is not None and best_margin >= 0:
                return best

            if attempt < tries - 1 and self.retry_interval > 0:
                time.sleep(self.retry_interval)

        logger.debug(
            f"'{os.path.basename(template_path)}' not found in {region} "
            f"(best score {self.last_score:.2f}, threshold {limit:.2f})."
        )
        return None

    def find_all(
        self,
        template_path: str,
        region: Optional[Region] = None,
        base_scale: float = 1.0,
        threshold: Optional[float] = None,
        min_distance: int = 15,
    ) -> List[Match]:
        """
        Every place the reference appears, top to bottom.

        One capture answers for the whole list, which is the point: the chest
        panel shows four 'Open' buttons at once, and finding them together lets
        four chests be read in a single OCR pass instead of four.

        Matches closer together than `min_distance` vertically are the same
        button seen twice - template matching peaks over several neighbouring
        pixels - so only the strongest of each cluster is kept.
        """
        if cv2 is None or not os.path.exists(template_path):
            return []

        limit = self.threshold if threshold is None else threshold
        screenshot = self.browser.capture(region, scale=1.0)
        if screenshot is None or screenshot.size == 0:
            return []
        haystack = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)

        best: List[Tuple[float, Match]] = []
        for scale in self.scales_for(base_scale):
            template = self.load_template(template_path, scale)
            if template is None:
                continue
            height, width = template.shape[:2]
            if height > haystack.shape[0] or width > haystack.shape[1]:
                continue

            result = np.nan_to_num(cv2.matchTemplate(haystack, template, cv2.TM_CCOEFF_NORMED),
                                   nan=-1.0, posinf=-1.0, neginf=-1.0)
            bar = limit if abs(scale - 1.0) <= 0.01 else max(0.0, limit - self.relief)
            ys, xs = np.where(result >= bar)
            if len(ys) == 0:
                continue

            offset_x = region[0] if region else 0
            offset_y = region[1] if region else 0
            candidates = sorted(((float(result[y, x]), int(x), int(y)) for y, x in zip(ys, xs)),
                                reverse=True)
            best = []
            for score, x, y in candidates:
                if all(abs(y - kept[1][1] + offset_y) >= min_distance for kept in best):
                    best.append((score, (offset_x + x, offset_y + y, width, height)))
            if best:
                # The first scale that finds anything is the right one; the
                # ladder exists for a resized window, not to merge scales.
                break

        matches = [match for _score, match in best]
        matches.sort(key=lambda m: m[1])
        return matches

    def find_center(self, template_path: str, region: Optional[Region] = None, **kwargs) -> Optional[Tuple[int, int]]:
        match = self.find(template_path, region, **kwargs)
        if not match:
            return None
        return (match[0] + match[2] // 2, match[1] + match[3] // 2)

    def is_present(self, template_path: str, region: Optional[Region] = None, **kwargs) -> bool:
        return self.find(template_path, region, **kwargs) is not None
