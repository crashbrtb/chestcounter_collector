"""
Reading text off the game screen.

Why the engine changed
----------------------
Tesseract was trained on scanned documents. The game draws small, stylised,
light text over a dark, textured background, so every read had to be rescued by
guesswork - upscale, binarise, invert, try again - and the guesswork is what
mangled foreign names: measured on game-style renderings, it turned
`Şükrü Öztürk` into `Sukriú Oztiirk`, `Muñoz` into `Mufioz` and `Björn` into
`Bjôm`. A wrong name is a chest credited to the wrong player.

RapidOCR runs the PaddleOCR models on ONNXRuntime. It was trained on text in the
wild and reads light-on-dark interface text directly, getting the letters right
where Tesseract guessed - but its dictionary carries no Latin diacritics, so it
drops every accent instead.

Neither engine is enough alone, so the default combines them (`HybridBackend`
below): the letters come from RapidOCR, and only the accents from Tesseract,
and only on letters the two already agree about. Either engine can still be
forced from the configuration.

The other half of the fix is upstream of the engine: regions are rendered by
Chrome at `ocr.capture_scale`, so the engine sees genuinely larger glyphs
instead of an interpolated blow-up of a small screenshot.
"""

import os
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

try:
    import pytesseract
    from pytesseract import Output
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

try:
    from rapidocr_onnxruntime import RapidOCR
    HAS_RAPIDOCR = True
except ImportError:
    HAS_RAPIDOCR = False

from utils.logger import logger
from utils.text_utils import (alphanumeric_key, best_match, clean_ocr_text, match_score,
                              normalize_name, strip_accents)

TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


class TextLine(NamedTuple):
    text: str
    center: Tuple[int, int]      # page coordinates (CSS pixels)
    confidence: float            # 0..100, whichever engine produced it
    top: int                     # page coordinate, for ordering lines
    height: int                  # page pixels, used to tell rows apart


class RapidOCRBackend:
    """
    RapidOCR, tuned for this workload.

    Two settings decide almost all of its speed, and both were measured on the
    real chest panel (4 chests, 694x614 px):

    * `intra_op_num_threads` - ONNXRuntime's default threading gave 1646 ms per
      read. Four threads bring it to 375 ms. More is worse, not better: eight
      threads measured 1097 ms, the cores spending their time contending rather
      than working.
    * the angle classifier - it exists to straighten rotated photographs. Game
      text is never rotated, and skipping the step saved another 490 ms.
    """

    name = "RapidOCR"

    def __init__(self, threads: int = 4):
        self.threads = max(1, int(threads or 4))
        self._engine = None

    @staticmethod
    def available() -> Tuple[bool, str]:
        if not HAS_RAPIDOCR:
            return False, "'rapidocr-onnxruntime' package not installed"
        return True, "RapidOCR (PaddleOCR/ONNX)"

    def _get_engine(self):
        if self._engine is None:
            logger.debug(f"Loading RapidOCR models ({self.threads} threads)...")
            try:
                self._engine = RapidOCR(intra_op_num_threads=self.threads)
            except TypeError:
                # Older builds do not accept the threading argument.
                self._engine = RapidOCR()
        return self._engine

    def read(self, image: np.ndarray, **_) -> List[Dict[str, Any]]:
        try:
            result, _elapsed = self._get_engine()(image, use_cls=False)
        except TypeError:
            try:
                result, _elapsed = self._get_engine()(image)
            except Exception as exc:
                logger.error(f"RapidOCR failed: {exc}")
                return []
        except Exception as exc:
            logger.error(f"RapidOCR failed: {exc}")
            return []
        if not result:
            return []

        lines = []
        for box, text, confidence in result:
            xs = [point[0] for point in box]
            ys = [point[1] for point in box]
            lines.append({
                "text": text,
                "center": (sum(xs) / len(xs), sum(ys) / len(ys)),
                "confidence": float(confidence) * 100.0,   # RapidOCR reports 0..1
                "top": min(ys),
                "height": max(ys) - min(ys),
            })
        return lines


class TesseractBackend:
    name = "Tesseract"

    # Measured on the real chest panel: dropping the second language saved 136 ms
    # (por+eng 446 ms, por alone 310 ms) with no loss - Portuguese covers the
    # Latin alphabet, English text included. psm 6 reads the panel as one block
    # and oem 1 uses the LSTM engine alone.
    CONFIG = "--psm 6 --oem 1"

    def __init__(self, executable_path: str = "", lang: str = "por"):
        self.executable_path = executable_path
        self.lang = lang
        self._configured = False

    def _configure(self):
        if self._configured or not HAS_TESSERACT:
            return
        self._configured = True
        candidates = ([self.executable_path] if self.executable_path else []) + TESSERACT_PATHS
        try:
            pytesseract.get_tesseract_version()
            return
        except Exception:
            pass
        for path in candidates:
            if path and os.path.exists(path):
                pytesseract.pytesseract.tesseract_cmd = path
                return

    def available(self) -> Tuple[bool, str]:
        if not HAS_TESSERACT:
            return False, "'pytesseract' package not installed"
        self._configure()
        try:
            return True, f"Tesseract {pytesseract.get_tesseract_version()}"
        except Exception as exc:
            return False, f"Tesseract not found in system ({exc})"

    @staticmethod
    def _prepare(image: np.ndarray) -> np.ndarray:
        """Tesseract wants dark text on a light background; the game gives the opposite."""
        if cv2 is None:
            return image
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        if gray.mean() < 128:
            gray = cv2.bitwise_not(gray)
        return gray

    def read(self, image: np.ndarray, **_) -> List[Dict[str, Any]]:
        self._configure()
        try:
            data = pytesseract.image_to_data(self._prepare(image), lang=self.lang,
                                             config=self.CONFIG, output_type=Output.DICT)
        except Exception as exc:
            logger.error(f"Tesseract failed: {exc}")
            return []

        grouped: Dict[tuple, list] = {}
        for index, word in enumerate(data["text"]):
            if not (word or "").strip():
                continue
            try:
                confidence = float(data["conf"][index])
            except (TypeError, ValueError):
                continue
            if confidence < 0:
                continue
            key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
            grouped.setdefault(key, []).append({
                "text": word,
                "left": data["left"][index], "top": data["top"][index],
                "width": data["width"][index], "height": data["height"][index],
                "conf": confidence,
            })

        lines = []
        for words in grouped.values():
            left = min(w["left"] for w in words)
            top = min(w["top"] for w in words)
            right = max(w["left"] + w["width"] for w in words)
            bottom = max(w["top"] + w["height"] for w in words)
            # The weakest real word decides whether this reading can be trusted;
            # stray punctuation ('—', '.') always scores low and says nothing.
            reais = [w for w in words if any(c.isalnum() for c in w["text"])]
            lines.append({
                "text": " ".join(w["text"] for w in words),
                "center": ((left + right) / 2, (top + bottom) / 2),
                "confidence": sum(w["conf"] for w in words) / len(words),
                "min_conf": min((w["conf"] for w in reais), default=0.0),
                "top": top,
                "height": bottom - top,
            })
        return lines


def _accented_window(base: str, text: str) -> Optional[str]:
    """
    The slice of `text` that is `base` with accents on it, if there is one.

    The two engines do not agree on where a line ends: RapidOCR often boxes the
    player's name on its own while Tesseract reads the whole row, label and all
    ('De: Aurélio Gonçalves'). Requiring the two strings to be equal threw away
    exactly the accents this is here to recover, so the shorter reading is
    looked for INSIDE the longer one.
    """
    if not base:
        return None
    target = strip_accents(base).lower()
    for start in range(len(text) - len(base) + 1):
        window = text[start:start + len(base)]
        if strip_accents(window).lower() == target:
            return window
    return None


def _merge_readings(base: str, other: str) -> str:
    """
    The best of two readings of the same text.

    When both engines agree on the letters and digits, they can still disagree
    about spaces and accents - and there Tesseract is the better witness:
    RapidOCR glues words together (`ShadowChest`, `Level35epicCrypt`) because
    its detection boxes whole phrases, while Tesseract segments words the way
    the old collector did. Since the chest name is stored, taking the spaced
    form keeps new rows comparable with the years of rows already in the
    database.

    When the letters do NOT agree, this is a foreign name Tesseract mangled, and
    only its accent marks are borrowed - never its letters.
    """
    if not base or base == other:
        return base

    key = alphanumeric_key(base)
    if key and key == alphanumeric_key(other):
        return other

    window = _accented_window(base, other)
    return _restore_diacritics(base, window) if window else base


def _restore_diacritics(base: str, accented: str) -> str:
    """
    Puts `accented`'s accents back onto `base`, letter by letter.

    Only marks are transferred, never letters and never case: the reading that
    got the letters right stays in charge of them. A character is only replaced
    when the other engine saw the same letter with a mark on it, which is why a
    disagreement about the letter itself cannot sneak in through here.
    """
    if len(base) != len(accented):
        return base

    out = []
    for plain, marked in zip(base, accented):
        if plain == marked or strip_accents(marked) == marked:
            out.append(plain)
            continue
        if strip_accents(marked).lower() == plain.lower():
            out.append(marked.upper() if plain.isupper() else marked.lower())
        else:
            out.append(plain)
    return "".join(out)


class HybridBackend:
    """
    The fast engine first; the careful one only when the fast one hesitates.

    Both engines were measured on the real chest panel (4 chests, 694x616 px):

        Tesseract (por, psm 6, oem 1)     310 ms
        RapidOCR (4 threads, no cls)      462 ms
        onnxtr fast_tiny                 1040 ms
        both, always                     1350 ms

    Running both on every batch bought accuracy that was almost never needed:
    on ordinary names the two agree. What decides is Tesseract's own confidence,
    and it turned out to be a clean signal - correct readings scored 71 to 96,
    while the two it mangled scored 16 (`Sükrü Öztürk`) and 35 (`Muñoz`). So a
    low score, and only a low score, is worth 462 ms of second opinion.

    When that second opinion is fetched, the roles are the ones the earlier
    measurements justified: RapidOCR supplies the letters (7/8 correct against
    Tesseract's 6/8 on foreign names) and Tesseract the spacing and accents,
    which its dictionary has and RapidOCR's has not.
    """

    name = "Tesseract + RapidOCR on demand"

    def __init__(self, rapid: RapidOCRBackend, tesseract: TesseractBackend,
                 confidence_gate: float = 60.0):
        self.rapid = rapid
        self.tesseract = tesseract
        self.confidence_gate = float(confidence_gate)
        self.verifications = 0
        self.reads = 0
        # Text already cross-checked once. A clan of thirty players sending
        # thousands of chests repeats the same names and the same sources over
        # and over ('Level 35 epic Crypt' is on most of them), and confidence is
        # a property of the glyphs, not of the run: if the two engines agreed on
        # a string once, the same string does not need paying for again.
        self._trusted: set = set()

    def available(self) -> Tuple[bool, str]:
        rapid_ok, rapid_reason = self.rapid.available()
        tess_ok, tess_reason = self.tesseract.available()
        if rapid_ok and tess_ok:
            return True, (f"{tess_reason} + {rapid_reason} — secondary engine used only when "
                          f"confidence drops below {self.confidence_gate:g}")
        if rapid_ok:
            return True, f"{rapid_reason} (without Tesseract: {tess_reason})"
        if tess_ok:
            return True, f"{tess_reason} (without RapidOCR: {rapid_reason})"
        return False, f"{rapid_reason}; {tess_reason}"

    def read(self, image: np.ndarray, careful: bool = False, **kwargs) -> List[Dict[str, Any]]:
        """
        `careful=True` forces the second engine.

        The caller often knows better than any confidence score whether a
        reading can be trusted: the collector checks every field against the
        names already in the database, and an unrecognised one is a far better
        reason to spend 460 ms than a low number from Tesseract.
        """
        self.reads += 1
        tess_ok = self.tesseract.available()[0]
        fast = self.tesseract.read(image, **kwargs) if tess_ok else []

        if fast and not careful and not self._doubtful(fast):
            return fast

        if not self.rapid.available()[0]:
            return fast

        self.verifications += 1
        careful = self.rapid.read(image, **kwargs)
        if not careful:
            return fast
        if not fast:
            self._trust(careful)
            return careful

        # RapidOCR keeps the letters; Tesseract lends spacing and accents.
        for item in careful:
            if not item["text"].strip():
                continue
            for other in sorted(fast, key=lambda o: abs(o["center"][1] - item["center"][1])):
                merged = _merge_readings(item["text"], other["text"])
                if merged != item["text"]:
                    logger.debug(f"Reading improved: '{item['text']}' -> '{merged}'")
                    item["text"] = merged
                    break

        # Both engines have now spoken about these strings; they are settled.
        self._trust(careful)
        self._trust(fast)
        return careful

    def _doubtful(self, lines: List[Dict[str, Any]]) -> bool:
        """Is any line shaky, and not already vouched for?"""
        for line in lines:
            confidence = line.get("min_conf", line["confidence"])
            if confidence >= self.confidence_gate:
                continue
            if normalize_name(line["text"]) in self._trusted:
                continue
            logger.debug(f"'{line['text']}' read at confidence {confidence:.0f}; "
                         f"checking with RapidOCR.")
            return True
        return False

    def _trust(self, lines: List[Dict[str, Any]]):
        for line in lines:
            key = normalize_name(line["text"])
            if key:
                self._trusted.add(key)


class OCREngine:
    """Reads regions of the game page, in page coordinates in and page coordinates out."""

    def __init__(self, browser, config: Dict[str, Any]):
        self.browser = browser
        self.capture_scale = float(config.get("capture_scale", 3.0))
        self.min_confidence = float(config.get("min_confidence", 45.0))
        self.match_threshold = float(config.get("name_match_threshold", 0.75))
        self._preference = str(config.get("engine", "auto")).lower()
        self._rapid = RapidOCRBackend(threads=int(config.get("threads", 4) or 4))
        self._tesseract = TesseractBackend(
            config.get("tesseract_path", ""), config.get("tesseract_lang", "por")
        )
        self._hybrid = HybridBackend(self._rapid, self._tesseract,
                                     float(config.get("confidence_gate", 60.0)))

    # -- engine choice
    def backend(self):
        """
        The engine in use.

        'auto' means the hybrid whenever both engines are installed, and
        whichever one is present otherwise - so a machine without the RapidOCR
        models still collects, just with the old accuracy.
        """
        if self._preference == "tesseract":
            return self._tesseract
        if self._preference == "rapidocr":
            return self._rapid
        if self._preference == "hybrid":
            return self._hybrid
        if RapidOCRBackend.available()[0] and self._tesseract.available()[0]:
            return self._hybrid
        return self._rapid if RapidOCRBackend.available()[0] else self._tesseract

    def status(self) -> Tuple[bool, str]:
        """(usable, description) - so the GUI can warn before a run, not during it."""
        return self.backend().available()

    # -- reading
    def read_lines(self, region: Tuple[int, int, int, int], scale: Optional[float] = None) -> List[TextLine]:
        """
        Every line found in a page region, ordered top to bottom, in page coordinates.

        The region is captured by the browser at `scale`, so the coordinates that
        come back from the engine are divided by it to become page coordinates
        again.
        """
        if not region or region[2] <= 0 or region[3] <= 0:
            return []

        factor = float(scale or self.capture_scale)
        image = self.browser.capture(region, scale=factor)
        if image is None or image.size == 0:
            logger.warning(f"Empty capture for region {region}.")
            return []
        return self.lines_from_image(image, (region[0], region[1]), factor)

    def lines_from_image(self, image, origin: Tuple[int, int], factor: float,
                         careful: bool = False) -> List[TextLine]:
        """
        Same reading, on a picture already in hand.

        Capturing and reading are separate so that one capture of the whole
        chest panel can feed one OCR pass for every chest on it - the engine
        costs about the same for four chests as for one, so splitting the work
        per chest was paying that price four times over.
        """
        region = (origin[0], origin[1])
        backend = self.backend()
        try:
            items = backend.read(image, careful=careful)
        except TypeError:      # a single engine takes no such argument
            items = backend.read(image)

        lines = []
        for item in items:
            cleaned = clean_ocr_text(item["text"])
            if not cleaned or item["confidence"] < self.min_confidence:
                continue
            # A box with no letter or digit in it is panel decoration - a border
            # tick read as '.', a divider read as '_'. Kept, it became a row of
            # its own, and being the topmost row it was stored as the chest's
            # NAME, pushing the player into the name field and the source into
            # the player field. Silent, and wrong in the database.
            if not any(character.isalnum() for character in cleaned):
                continue
            center = item["center"]
            lines.append(TextLine(
                text=cleaned,
                center=(int(region[0] + center[0] / factor), int(region[1] + center[1] / factor)),
                confidence=item["confidence"],
                top=int(region[1] + item["top"] / factor),
                height=max(1, int(item["height"] / factor)),
            ))
        lines.sort(key=lambda line: (line.top, line.center[0]))
        logger.debug(f"OCR {self.backend().name} in {region}: {[line.text for line in lines]}")
        return lines

    def read_rows(self, region: Tuple[int, int, int, int], scale: Optional[float] = None) -> List[str]:
        """
        The region's text as visual rows, left to right.

        A detection engine returns boxes, not rows: a label and its value sitting
        side by side ('De:' and the player's name) arrive as two separate boxes,
        and reading them as two lines lost the association between them. Boxes
        whose vertical centres are within half a line height belong to the same
        row and are joined here.
        """
        return [text for text, _y in self.read_rows_positioned(region, scale)]

    def group_rows(self, lines: List[TextLine]) -> List[Tuple[str, int]]:
        """Joins boxes that sit on the same visual row; returns (text, centre y)."""
        rows: List[List[TextLine]] = []
        for line in lines:
            placed = False
            for row in rows:
                reference = row[0]
                tolerance = max(6, reference.height // 2)
                if abs(line.center[1] - reference.center[1]) <= tolerance:
                    row.append(line)
                    placed = True
                    break
            if not placed:
                rows.append([line])

        joined = []
        for row in rows:
            row.sort(key=lambda line: line.center[0])
            text = " ".join(line.text for line in row).strip()
            if text:
                joined.append((text, row[0].center[1]))
        joined.sort(key=lambda item: item[1])
        return joined

    def read_rows_positioned(self, region: Tuple[int, int, int, int],
                             scale: Optional[float] = None) -> List[Tuple[str, int]]:
        return self.group_rows(self.read_lines(region, scale))

    def read_text(self, region: Tuple[int, int, int, int], scale: Optional[float] = None) -> str:
        return "\n".join(self.read_rows(region, scale))

    def find_text(
        self,
        region: Tuple[int, int, int, int],
        wanted: str,
        competitors: Sequence[str] = (),
        scale: Optional[float] = None,
    ) -> Optional[TextLine]:
        """
        Where to click to hit `wanted` inside the region.

        `competitors` are the other names that could plausibly be on the same
        list - the account's other profiles. A line is only accepted when
        `wanted` beats all of them on that line, which is what keeps two similar
        profile names apart without either one being written into the code.
        """
        names = [wanted, *[name for name in competitors if name and name != wanted]]

        scored = []
        for line in self.read_lines(region, scale):
            if best_match(line.text, names, self.match_threshold) != wanted:
                continue
            scored.append((match_score(wanted, line.text), -len(line.text), line))

        if not scored:
            return None

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        best = scored[0][2]
        logger.info(
            f"Text '{wanted}' matched '{best.text}' at {best.center} "
            f"(score {scored[0][0]:.2f}, confidence {best.confidence:.0f}%)."
        )
        return best

    def identify(
        self,
        region: Tuple[int, int, int, int],
        candidates: Sequence[str],
        scale: Optional[float] = None,
    ) -> Tuple[Optional[str], str]:
        """
        Which of `candidates` the region is showing, plus the raw text it read.

        Used for the active-profile banner: the question there is not 'is this
        name present' but 'which of my profiles is this', and asking it that way
        is what stops a near-twin name from answering yes to both.
        """
        text = " ".join(line.text for line in self.read_lines(region, scale)).strip()
        winner = best_match(text, candidates, self.match_threshold)
        logger.info(f"Region {region} reads '{text}' -> {winner or 'undefined'}")
        return winner, text
