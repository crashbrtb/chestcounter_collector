"""
Text manipulation, string normalization, and OCR parsing utilities.
"""

import re
from typing import Optional, Tuple


def find_splitter(text: str) -> Optional[str]:
    """
    Finds the first occurrence of a delimiter separator in OCR lines (':', ',', '.').
    Returns the character found or None.
    """
    if not text:
        return None
    for char in (":", ",", "."):
        if char in text:
            return char
    return None


def clean_ocr_text(text: str) -> str:
    """
    Cleans raw OCR output by removing dashes, extra whitespace and unwanted characters.
    """
    if not text:
        return ""
    cleaned = text.replace("——", "").replace("—", "").replace("_", " ")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


def normalize_name(name: str) -> str:
    """
    Normalizes account or player names for comparison (lower case, stripped whitespace).
    """
    if not name:
        return ""
    return re.sub(r"\s+", " ", name.strip().lower())


def parse_key_value_line(line: str) -> Tuple[str, str]:
    """
    Parses a line like 'From: player_name' or 'Source: Crypt' into (key, value).
    If no separator is present, returns ('', line).
    """
    splitter = find_splitter(line)
    if splitter:
        parts = line.split(splitter, 1)
        if len(parts) > 1:
            return parts[0].strip(), parts[1].strip()
    return "", line.strip()
