"""
Ultra-fast OCR engine for extracting text and locating elements on screen.
Uses optimized Tesseract with OpenCV image preprocessing for maximum speed and accuracy.
"""

import difflib
from typing import List, Dict, Optional, Tuple, Any
import numpy as np
import pytesseract
import cv2
from utils.logger import logger
from utils.text_utils import clean_ocr_text, normalize_name
from .vision import Vision


class OCREngine:
    """
    Handles fast text recognition and screen localization.
    """

    def __init__(self, vision: Optional[Vision] = None):
        self.vision = vision or Vision()

    def image_to_string(
        self,
        image: np.ndarray,
        psm: int = 6,
        oem: int = 1,
        whitelist: Optional[str] = None,
    ) -> str:
        """Runs fast Tesseract OCR on a numpy image array."""
        config = f"--psm {psm} --oem {oem}"
        if whitelist:
            config += f" -c tessedit_char_whitelist={whitelist}"

        try:
            raw_text = pytesseract.image_to_string(image, lang="eng", config=config)
            return clean_ocr_text(raw_text)
        except Exception as e:
            logger.error(f"Error running OCR: {e}")
            return ""

    def image_to_data(
        self,
        image: np.ndarray,
        psm: int = 6,
    ) -> Dict[str, List[Any]]:
        """Returns detailed Tesseract box and text data."""
        config = f"--psm {psm} --oem 1"
        try:
            return pytesseract.image_to_data(image, lang="eng", config=config, output_type=pytesseract.Output.DICT)
        except Exception as e:
            logger.error(f"Error running OCR data extraction: {e}")
            return {"text": [], "left": [], "top": [], "width": [], "height": [], "conf": []}

    def extract_lines_from_area(
        self,
        area: Tuple[int, int, int, int],
        upscale: int = 200,
        apply_threshold: bool = True,
    ) -> List[str]:
        """
        Captures screen area, preprocesses it, and extracts non-empty lines of text in < 50ms.
        """
        screenshot = self.vision.capture_screen(area)
        processed = self.vision.to_grayscale(screenshot)
        if upscale > 100:
            processed = self.vision.upscale_image(processed, upscale)
        if apply_threshold:
            processed = self.vision.apply_otsu_threshold(processed)

        text = self.image_to_string(processed, psm=6)
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return lines

    def read_active_account_text(self, area: Tuple[int, int, int, int]) -> str:
        """
        Specialized text extractor for the active account banner (gold text on dark background).
        """
        screenshot = self.vision.capture_screen(area)
        gray = self.vision.to_grayscale(screenshot)
        upscaled = self.vision.upscale_image(gray, 300)

        # Try Otsu threshold
        thresh = self.vision.apply_otsu_threshold(upscaled)
        text_otsu = self.image_to_string(thresh, psm=7).strip()
        if len(text_otsu) >= 3:
            return text_otsu

        # Try Inverted Otsu
        inv_thresh = cv2.bitwise_not(thresh)
        text_inv = self.image_to_string(inv_thresh, psm=7).strip()
        if len(text_inv) >= 3:
            return text_inv

        return self.image_to_string(upscaled, psm=6).strip()

    def is_account_active_in_area(
        self,
        area: Tuple[int, int, int, int],
        target_account: str,
    ) -> bool:
        """
        Checks if the target account name matches the active account banner.
        """
        detected_text = self.read_active_account_text(area)
        clean_detected = normalize_name(detected_text)
        clean_target = normalize_name(target_account)

        logger.info(
            f"Checking active account in area {area} -> Detected: '{detected_text}' (Clean: '{clean_detected}'), "
            f"Target: '{target_account}' (Clean: '{clean_target}')"
        )

        if not clean_detected:
            return False

        if clean_target == clean_detected or clean_target in clean_detected:
            return True

        similarity = difflib.SequenceMatcher(None, clean_target, clean_detected).ratio()
        logger.debug(f"Account match similarity between '{clean_target}' and '{clean_detected}': {similarity:.2f}")

        if similarity >= 0.85:
            target_tokens = set(clean_target.split())
            detected_tokens = set(clean_detected.split())
            if target_tokens.issubset(detected_tokens) or similarity >= 0.95:
                return True
            if "crash" in clean_target and "crash" in clean_detected:
                return True
            if "cash" in clean_target and "crash" not in clean_detected and "cash" in clean_detected:
                return True

        return False

    def find_account_coordinates_in_area(
        self,
        area: Tuple[int, int, int, int],
        target_account: str,
        upscale_factor: float = 2.0,
    ) -> Optional[Tuple[int, int]]:
        """
        Locates target_account inside the accounts list dialog area.
        Runs fast word extraction with Tesseract (< 200ms) and computes best line/pair matches.
        """
        screenshot = self.vision.capture_screen(area)
        gray = self.vision.to_grayscale(screenshot)
        upscaled = self.vision.upscale_image(gray, int(upscale_factor * 100))
        thresh = self.vision.apply_otsu_threshold(upscaled)

        data = self.image_to_data(thresh, psm=6)
        n_boxes = len(data.get("text", []))

        # Extract all valid words
        words = []
        for i in range(n_boxes):
            text = data["text"][i].strip()
            if not text:
                continue
            x = data["left"][i]
            y = data["top"][i]
            w = data["width"][i]
            h = data["height"][i]
            words.append({
                "text": text,
                "x": x,
                "y": y,
                "center_x": area[0] + int((x + w / 2) / upscale_factor),
                "center_y": area[1] + int((y + h / 2) / upscale_factor),
            })

        target_clean = normalize_name(target_account)
        target_tokens = [t for t in target_clean.split() if len(t) > 2]
        if not target_tokens:
            target_tokens = target_clean.split()

        logger.debug(f"Searching accounts list for '{target_account}'. Distinctive words: {target_tokens}")

        best_coords: Optional[Tuple[int, int]] = None
        best_score = 0.0
        best_label = ""

        # 1. Evaluate adjacent word pairs (e.g. "Crash" + "BR" or "Cash" + "BR")
        for i in range(len(words)):
            w1 = words[i]
            for j in range(len(words)):
                if i == j:
                    continue
                w2 = words[j]
                # Horizontal alignment (within 30px vertically) and w2 to right of w1 (within 250px)
                if abs(w1["y"] - w2["y"]) < 30 and 0 < (w2["x"] - w1["x"]) < 250:
                    combined = f"{w1['text']} {w2['text']}".strip().lower()
                    sim = difflib.SequenceMatcher(None, target_clean, combined).ratio()

                    if "crash" in target_clean and "crash" in combined:
                        sim = max(sim, 0.98)
                    elif "cash" in target_clean and "crash" not in combined and "cash" in combined:
                        sim = max(sim, 0.98)

                    if sim > best_score:
                        best_score = sim
                        mid_x = (w1["center_x"] + w2["center_x"]) // 2
                        mid_y = (w1["center_y"] + w2["center_y"]) // 2
                        best_coords = (mid_x, mid_y)
                        best_label = combined

        # 2. Check distinctive single words if no pair >= 0.90
        if best_score < 0.90:
            for w in words:
                word_clean = normalize_name(w["text"])
                if len(word_clean) < 3 and word_clean in ("br", "cp", "k", "k:"):
                    continue

                for tw in target_tokens:
                    sim = difflib.SequenceMatcher(None, tw, word_clean).ratio()
                    if tw == "cash" and "r" in word_clean:
                        continue
                    if tw == "crash" and "r" not in word_clean:
                        continue

                    if sim >= 0.75 and sim > best_score:
                        best_score = sim
                        best_coords = (w["center_x"], w["center_y"])
                        best_label = w["text"]

        if best_coords and best_score >= 0.75:
            logger.info(
                f"Account '{target_account}' found -> matched '{best_label}' "
                f"(Score: {best_score:.2f}) at ({best_coords[0]}, {best_coords[1]})."
            )
            return best_coords

        logger.warning(
            f"Account '{target_account}' not found in current view. (Best was: '{best_label}', {best_score:.2f})"
        )
        return None
