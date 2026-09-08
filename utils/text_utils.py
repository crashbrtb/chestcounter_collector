"""
Text cleaning and name comparison for OCR output.

Two different jobs live here, and they must not be confused:

* what gets STORED - the player name is written to the database exactly as it
  was read, accents included. Stripping them here was part of why foreign names
  came out wrong.
* what gets COMPARED - when deciding whether the name on screen is the profile
  we are looking for, accents, case and spacing are ignored, because every OCR
  engine drops or merges them in its own way.
"""

import difflib
import re
import unicodedata
from typing import Optional, Sequence, Tuple

# Dashes an OCR engine happily swaps for one another.
_DASHES = dict.fromkeys(map(ord, "—–‒−―"), "-")

# Confusions that survive normalization: same glyph, different code point.
_LOOKALIKES = str.maketrans({"0": "o", "1": "l", "5": "s", "8": "b", "|": "l"})


def clean_ocr_text(text: str) -> str:
    """Collapses whitespace and normalizes dashes, keeping every real character."""
    if not text:
        return ""
    cleaned = text.translate(_DASHES)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip(" \t-")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text or "") if unicodedata.category(c) != "Mn")


def normalize_name(name: str) -> str:
    """Comparison form: no accents, no case, single spaces."""
    if not name:
        return ""
    return re.sub(r"\s+", " ", strip_accents(name).translate(_DASHES)).strip().lower()


def _no_spaces(text: str) -> str:
    return normalize_name(text).replace(" ", "")


def _alphanumeric(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", normalize_name(text))


def alphanumeric_key(text: str) -> str:
    """Letters and digits only, no accents, no case - what two readings of the
    same text always share, whatever they disagree about in spacing."""
    return _alphanumeric(text)


def _lookalike(text: str) -> str:
    return _alphanumeric(text).translate(_LOOKALIKES)


def similarity(a: str, b: str) -> float:
    """How alike two names are (0..1), at the most tolerant level that still matches."""
    scores = [
        difflib.SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio(),
        difflib.SequenceMatcher(None, _no_spaces(a), _no_spaces(b)).ratio(),
        difflib.SequenceMatcher(None, _lookalike(a), _lookalike(b)).ratio(),
    ]
    return max(scores)


def names_match(wanted: str, read: str, threshold: float = 0.75) -> bool:
    """
    Is `read` the name `wanted`?

    Containment is checked first, at three levels of tolerance, because the
    banner often carries extra text around the name ('Bem-vindo, Crash BR'), and
    a ratio would be dragged down by that surplus. Only then does the fuzzy score
    decide - it is what covers a couple of mangled letters inside the name.
    """
    if not (wanted or "").strip() or not (read or "").strip():
        return False
    if normalize_name(wanted) in normalize_name(read):
        return True
    if _no_spaces(wanted) and _no_spaces(wanted) in _no_spaces(read):
        return True
    target = _lookalike(wanted)
    if target and len(target) >= 4 and target in _lookalike(read):
        return True
    return similarity(wanted, read) >= threshold


def match_score(wanted: str, read: str, allow_containment: bool = True) -> float:
    """
    How strongly `read` claims to be `wanted`, on the same 0..1 scale as similarity.

    Containment scores near the top because the name is often surrounded by
    other text ('Bem-vindo, Crash BR'), where a plain ratio would be dragged
    down by the surplus and lose to a shorter, wronger candidate.

    `allow_containment=False` for the opposite situation, where the reading IS
    the name and nothing else. There, containment is a trap: any short name that
    happens to occur inside the text scores a perfect 1.0, and 'Nome Que Nao
    Existe' was confidently resolved to the player 'Ste' - which lives inside
    'exiSTE'.
    """
    if not (wanted or "").strip() or not (read or "").strip():
        return 0.0
    if allow_containment:
        if normalize_name(wanted) in normalize_name(read):
            return 1.0
        if _no_spaces(wanted) and _no_spaces(wanted) in _no_spaces(read):
            return 0.97
        target = _lookalike(wanted)
        if target and len(target) >= 4 and target in _lookalike(read):
            return 0.94
    return similarity(wanted, read)


def best_match(read: str, candidates: Sequence[str], threshold: float = 0.75,
               margin: float = 0.02, allow_containment: bool = True) -> Optional[str]:
    """
    Which of `candidates` the read text actually is - or None if it cannot tell.

    Judging a name on its own is not enough when two profiles of the same account
    are called 'Crash BR' and 'Cash BR': either one scores 0.93 against the
    other, comfortably over any usable threshold. The previous version solved
    that by naming those two players inside the OCR engine.

    Here the candidates compete instead. A name wins only if it beats every other
    configured name by `margin`; a tie means the reading is ambiguous and the
    caller is told nothing matched, which is recoverable, rather than being told
    the wrong profile, which is not.
    """
    scored = sorted(
        ((match_score(candidate, read, allow_containment), candidate)
         for candidate in candidates if candidate),
        reverse=True,
    )
    if not scored or scored[0][0] < threshold:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < margin:
        return None
    return scored[0][1]


def find_splitter(text: str) -> Optional[str]:
    """First delimiter present in an OCR line, in order of preference."""
    if not text:
        return None
    for char in (":", ",", "."):
        if char in text:
            return char
    return None


def parse_key_value_line(line: str) -> Tuple[str, str]:
    """
    Splits 'From: player_name' into ('From', 'player_name').

    Splits on the FIRST delimiter only: player names contain dots and commas of
    their own, and splitting on the last one truncated them.
    """
    splitter = find_splitter(line)
    if splitter:
        parts = line.split(splitter, 1)
        if len(parts) > 1:
            return parts[0].strip(), parts[1].strip()
    return "", line.strip()
