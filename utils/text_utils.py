"""Comparing names: the one a profile is configured with, and the one the game gives."""

import re
import unicodedata

_DASHES = dict.fromkeys(map(ord, "—–‒−―"), "-")


def normalize_name(name: str) -> str:
    """Comparison form: no accents, no case, single spaces."""
    if not name:
        return ""
    without_accents = "".join(char for char in unicodedata.normalize("NFD", name) if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", without_accents.translate(_DASHES)).strip().lower()
