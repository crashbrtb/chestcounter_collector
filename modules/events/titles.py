"""Reading the Journal: the tournament name inside a card title.

The titles are short English (or Portuguese) sentences in a large font. They are
read as whole sentences (core.ocr.OCREngine.image_text), from a capture Chrome
renders at twice the size instead of one enlarged afterwards.
"""
from __future__ import annotations

import re
from typing import Optional

# A title starting with "Your Clanmates'" is a tournament, whatever word comes
# next ("results", "scores", ...): "Your Clanmates' results in the Rise of the
# Ancients tournament". Tolerates the misreadings OCR produces around the
# apostrophe and the doubled letters.
#
# Only at the start of the title ("Raid canceled. None of your clanmates joined
# you" is not a tournament), allowing a stray mark or one or two letters OCR
# picks up from the card border before it. Or anywhere, when it starts with a
# capital as a title does: a title region that also takes the divider above the
# card reads it as "SSS + == SSS Your Clanmates' ...".
_START = r"(?:^[^\w]*(?:\w{1,2}[^\w]+)?|(?<!\w)(?=(?-i:[A-Z])))"
_CLANMATES = re.compile(
    _START + r"your\s*cla[nm]{1,2}\s*-?\s*mates?\s*['’`´]?\s*s?\s*\S*?\s*(?:in\s*the|in|inthe)\s+(.+)",
    re.IGNORECASE,
)
# Any other "Your clanmates' ..." title, such as "Your clanmates' score of
# Remnants of Dread in battles with Dark Omens.": the whole rest is the name,
# since cutting it at "in" would give two tournaments the same name.
_CLANMATES_ANY = re.compile(
    _START + r"your\s*cla[nm]{1,2}\s*-?\s*mates?\s*['’`´]?\s*s?\s+(.+)",
    re.IGNORECASE,
)
_CLANMATES_PT = re.compile(
    _START + r"resultados?\s*d[oae]s?\s*(?:membros\s*do\s*cl[aã]|companheiros(?:\s*de\s*cl[aã])?)\s*(?:n[oae]s?|em)\s+(.+)",
    re.IGNORECASE,
)
# When the card was received, glued to the end of the title by the OCR:
# "today, 15:00", "yesterday, 14:00", "9 days ago, 14:00", "14:00". The
# punctuation before it goes too, so "... tournament. today" is not a name.
_TRAILING = re.compile(
    r"[\s.,;:|-]*(?:"
    r"\d+\s*(?:days?|hours?|minutes?|dias?|horas?|minutos?)\s*(?:ago|atr[áa]s)"
    r"|today|yesterday|hoje|ontem"
    r"|\d{1,2}[:.]\d{2}"
    r")",
    re.IGNORECASE,
)
# The same, when the OCR garbles the day ("today, 14:01" read "emaAars 14.71"):
# whatever follows the title's closing period and ends in a time.
_DATE_LINE = re.compile(r"\.\s+[^.]*?\d{1,2}[:.]\d{2}\W*$")


# How long ago the card was received, in whole days. The last one in the title
# is the card's own: a tournament could be called "3 Days of Glory".
_AGE = re.compile(
    r"(?P<days>\d+)\s*(?:days?\s*ago|dias?\s*atr[áa]s)"
    r"|h[áa]\s*(?P<days_pt>\d+)\s*dias?"
    r"|(?P<weeks>\d+)\s*(?:weeks?\s*ago|semanas?\s*atr[áa]s)"
    r"|(?P<yesterday>yesterday|ontem)"
    r"|(?P<today>today|hoje|\d+\s*(?:hours?|minutes?|horas?|minutos?)\s*(?:ago|atr[áa]s))",
    re.IGNORECASE,
)


def card_age_days(text: str) -> Optional[int]:
    """
    How many days ago a Journal card was received, as its title says it:
    "today" 0, "yesterday" 1, "7 days ago" 7. None when the title does not say
    (a bare "14:00", or the OCR missed it), which is never taken as old.
    """
    if not text:
        return None
    found = list(_AGE.finditer(" ".join(text.replace("\n", " ").split())))
    if not found:
        return None
    match = found[-1]
    if match.group("days") or match.group("days_pt"):
        return int(match.group("days") or match.group("days_pt"))
    if match.group("weeks"):
        return 7 * int(match.group("weeks"))
    return 1 if match.group("yesterday") else 0


def extract_tournament_name(text: str) -> Optional[str]:
    """The tournament name in a clan results title, or None if it is not one."""
    if not text:
        return None
    clean = " ".join(text.replace("\n", " ").split())
    for pattern in (_CLANMATES, _CLANMATES_PT, _CLANMATES_ANY):
        match = pattern.search(clean)
        if match:
            name = _DATE_LINE.sub("", match.group(1))
            # Only after the name: a tournament could be called "Today's Trial".
            when = next((m for m in _TRAILING.finditer(name) if m.start() > 0), None)
            if when is not None:
                name = name[:when.start()]
            name = name.strip(" .,:;-|")
            # OCR sometimes glues a stray character from the card border.
            name = re.sub(r"^[^\w]+|[^\w)]+$", "", name)
            return name or None
    return None


def parse_page_number(text: str) -> Optional[int]:
    digits = re.findall(r"\d+", text or "")
    return int(digits[0]) if digits else None


class TitleReader:
    """Reads a card title off a capture, through the application's OCR engine."""

    def __init__(self, engine, lang: str = "eng"):
        self.engine = engine
        self.lang = lang
        self.error = ""

    def available(self) -> bool:
        ok, reason = self.engine.status()
        self.error = "" if ok else f"OCR unavailable: {reason}"
        return ok

    def read(self, image, single_line: bool = False) -> str:
        return self.engine.image_text(image, single_line=single_line, lang=self.lang)
