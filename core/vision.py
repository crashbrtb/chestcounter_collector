"""
Computer vision module for screen capture, image preprocessing, and template matching.
"""

import os
import time
from typing import Optional, Tuple, Dict
import cv2
import numpy as np
from PIL import ImageGrab
from utils.logger import logger
from config.settings import DEFAULT_MATCH_CONFIDENCE, DEFAULT_MAX_ATTEMPTS


class Vision:
    """Provides high-performance image capture and template matching methods."""

    def __init__(self):
        self._template_cache: Dict[str, np.ndarray] = {}

    def capture_screen(self, area: Optional[Tuple[int, int, int, int]] = None) -> np.ndarray:
        """
        Captures a screenshot of the specified bounding box (left, top, right, bottom)
        or full screen if area is None. Returns RGB numpy array.
        """
        if area:
            # bbox = (left, top, right, bottom)
            img = ImageGrab.grab(bbox=(area[0], area[1], area[2], area[3]), include_layered_windows=True)
        else:
            img = ImageGrab.grab(include_layered_windows=True)

        return np.array(img)

    @staticmethod
    def to_grayscale(image: np.ndarray) -> np.ndarray:
        """Converts RGB image to Grayscale."""
        if len(image.shape) == 2:
            return image
        return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

    @staticmethod
    def remove_noise(image: np.ndarray, ksize: int = 5) -> np.ndarray:
        """Applies median blur to reduce noise."""
        return cv2.medianBlur(image, ksize)

    @staticmethod
    def apply_otsu_threshold(image: np.ndarray) -> np.ndarray:
        """Applies Otsu's binarization thresholding."""
        if len(image.shape) == 3:
            image = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        return cv2.threshold(image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

    @staticmethod
    def upscale_image(image: np.ndarray, scale_percent: int = 200) -> np.ndarray:
        """Upscales image for improved OCR accuracy."""
        width = int(image.shape[1] * scale_percent / 100)
        height = int(image.shape[0] * scale_percent / 100)
        return cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)

    def load_template(self, template_path: str) -> Optional[np.ndarray]:
        """Loads and caches template image from disk in grayscale."""
        if template_path in self._template_cache:
            return self._template_cache[template_path]

        if not os.path.exists(template_path):
            logger.error(f"Template image not found: {template_path}")
            return None

        # Read template image in grayscale
        template = cv2.imread(template_path, cv2.IMREAD_GRAYSCALE)
        if template is not None:
            self._template_cache[template_path] = template
        else:
            logger.error(f"Failed to decode template image: {template_path}")
        return template

    def find_template(
        self,
        template_path: str,
        search_area: Optional[Tuple[int, int, int, int]] = None,
        confidence: float = DEFAULT_MATCH_CONFIDENCE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        wait_interval: float = 0.2,
    ) -> Optional[Tuple[int, int, int, int]]:
        """
        Searches for a template image inside a screen region.
        Returns (screen_x, screen_y, width, height) relative to screen coordinates, or None.
        """
        template = self.load_template(template_path)
        if template is None:
            return None

        th, tw = template.shape[:2]

        for _ in range(max_attempts):
            screenshot = self.capture_screen(search_area)
            gray_screen = self.to_grayscale(screenshot)

            # Match template
            result = cv2.matchTemplate(gray_screen, template, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

            if max_val >= confidence:
                offset_x = search_area[0] if search_area else 0
                offset_y = search_area[1] if search_area else 0
                match_x = offset_x + max_loc[0]
                match_y = offset_y + max_loc[1]
                return (match_x, match_y, tw, th)

            if max_attempts > 1 and wait_interval > 0:
                time.sleep(wait_interval)

        return None
