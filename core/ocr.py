"""
Reading text off the game screen.

Why the engine changed
----------------------
Tesseract was trained on scanned documents. The game draws small, stylised text
over a textured background, so every read had to be rescued by guesswork -
upscale, binarise, invert, try again - and the guesswork is what mangles foreign
names. A wrong name is a chest credited to the wrong player.

RapidOCR runs the PaddleOCR models on ONNXRuntime. It was trained on text in the
wild and reads interface text directly, getting the letters right where
Tesseract guesses.

This file used to say that RapidOCR's dictionary carried no Latin diacritics and
dropped every accent, and the engines were arranged around that: Tesseract read,
and RapidOCR was called in only when Tesseract's confidence sagged. Measured
again on 314 real chest panels - 3750 lines, one engine per process, the same
saved images fed to each - that claim is no longer true of the PP-OCRv6 models
the package now ships. RapidOCR returned 42 accented lines against Tesseract's
13, and on every contested string it was RapidOCR that had the accent right:

    on screen          Tesseract / old arrangement     RapidOCR
    Kenan BAŞKAN       Kenan BASKAN                    Kenan BAŞKAN
    aklın şaşar        aklin gagar                     aklın şaşar
    Annihilatör        Annihilatôr, Annihilatór        Annihilatör
    VaLi               Vali, VaLi                      VaLi
    EMiRBeY            EMIRBeY                         EMiRBeY
    Dark²              Dark?                           Dark²

Over 257 lines whose correct spelling was read off the panel by eye, Tesseract
placed 53 and RapidOCR 244. The old arrangement scored exactly what Tesseract
scored: its confidence gate fired on 14 of 315 reads, because Tesseract is not
hesitant about these names - it is confidently wrong, and `Vali` for `VaLi`
raises no low score to trip the gate. Worse, it spelt three players two ways
each, which splits one person's chests across two rows in the database.

So the roles are now reversed. RapidOCR reads, and Tesseract is asked about one
thing only: digits. That is RapidOCR's single measured weakness - it reads the
zeros in `T6000SK` as the letter o - and Tesseract got that string right 10 times
out of 10. `HybridBackend` below consults it only for tokens that mix digits with
digit-shaped letters, which is 27 of the 314 panels, and takes back nothing but
the digits. Either engine can still be forced from the configuration.

The cost is real but smaller than it first measured: RapidOCR takes 551 ms per
panel against Tesseract's 258 ms, so a 1200-chest run spends about a minute and a
half more inside the engine. What that buys is 191 lines: 204 of the 257 went out
wrong before, 13 do now.

It measured at 1231 ms until `RapidOCRBackend._THREAD_KEY` was found to be
pointing at a path the package had moved. Anyone re-measuring this should check
that setting arrived before believing a timing.

One limit on all of these numbers. They come from the 257 lines the engines
disagreed about, checked one by one against the panel; the other 3493 lines all
four engines read the same way, and a mistake they share would not show up in
any of this. The error counts are a floor, not a ceiling.

The other half of the fix is upstream of the engine: regions are rendered by
Chrome at `ocr.capture_scale`, so the engine sees genuinely larger glyphs
instead of an interpolated blow-up of a small screenshot.
"""

import os
import re
import statistics
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

# RapidOCR changed package name. 'rapidocr-onnxruntime' was capped at
# Requires-Python <3.13 and stopped at 1.4.4, so from 3.13 on the engine only
# exists under the plain 'rapidocr' name. The two speak different dialects - the
# old call returns (triples, elapsed), the new one a RapidOCROutput - so which
# one answered is remembered here and the difference is absorbed below.
RAPIDOCR_API = ""
RAPIDOCR_IMPORT_ERROR = ""
try:
    from rapidocr import RapidOCR
    RAPIDOCR_API = "v3"
except Exception as _exc_v3:
    try:
        from rapidocr_onnxruntime import RapidOCR
        RAPIDOCR_API = "legacy"
    except Exception as _exc_legacy:
        RAPIDOCR_IMPORT_ERROR = f"rapidocr (v3): {_exc_v3}; rapidocr-onnxruntime: {_exc_legacy}"
HAS_RAPIDOCR = bool(RAPIDOCR_API)

from utils.logger import logger
from utils.text_utils import best_match, clean_ocr_text, match_score, normalize_name

TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]

# How tall the text is when the capture is worth trusting.
#
# Measured over two clean runs, 1222 lines between them: the median line is 15
# page pixels tall, the tenth percentile 14, the shortest 11. The spread is that
# narrow because the game draws this panel at one size.
#
# The floor exists because of a run that read for thirty minutes off a picture
# that was not worth trusting. It glued words together - 'EpicMonster Chest',
# 'Level 35epicCrypt' - on more chests than it collected, dropped every accent,
# and split one player across six spellings in the database. Nothing in the log
# said anything was wrong, and the only reason it was ever found was somebody
# reading the rows afterwards. The next such run should say so in its first
# panel instead.
#
# These are page pixels, not capture pixels: `lines_from_image` divides by the
# capture factor before this is measured, so the numbers describe the game's own
# rendering and do not move when `ocr.capture_scale` does.
HEALTHY_LINE_HEIGHT = 15
SUSPECT_LINE_HEIGHT = 12

# Below this, a median means nothing: the profile banner is one line, and small
# regions are read at their own size on purpose.
ENOUGH_LINES_TO_JUDGE = 6


class TextLine(NamedTuple):
    text: str
    center: Tuple[int, int]      # page coordinates (CSS pixels)
    confidence: float            # 0..100, whichever engine produced it
    top: int                     # page coordinate, for ordering lines
    height: int                  # page pixels, used to tell rows apart


class RapidOCRBackend:
    """
    RapidOCR, tuned for this workload.

    Two settings decide almost all of its speed, measured on 40 real chest
    panels (4 chests, 694x616 px):

    * `intra_op_num_threads` - one thread reads a panel in 1271 ms, two in
      689 ms, four in 524 ms. More is worse, not better: eight measured 1269 ms,
      the cores spending their time contending rather than working.
    * the angle classifier - it exists to straighten rotated photographs. Game
      text is never rotated, and skipping the step is worth about a third of the
      remaining time.
    """

    name = "RapidOCR"

    def __init__(self, threads: int = 4):
        self.threads = max(1, int(threads or 4))
        self._engine = None

    @staticmethod
    def available() -> Tuple[bool, str]:
        if not HAS_RAPIDOCR:
            detail = f": {RAPIDOCR_IMPORT_ERROR}" if RAPIDOCR_IMPORT_ERROR else ""
            return False, f"'rapidocr' package not installed or failed to load{detail}"
        return True, "RapidOCR (PaddleOCR/ONNX)"

    # Where this build keeps the ONNXRuntime thread count. It has moved twice:
    # `rapidocr_onnxruntime` took it as a constructor argument, an earlier
    # `rapidocr` 3.x nested one copy per pipeline stage under
    # `Det.engine_cfg.onnxruntime...`, and 3.9 keeps a single setting for all
    # three stages here.
    #
    # The path matters more than a path usually does, because `params` accepts
    # keys it does not recognise WITHOUT raising: a stale path is not an error,
    # it is a silent return to the default. That is not a small loss. This file
    # carried the middle spelling until it was measured again - every read had
    # been running on the default, which is 1271 ms against 524 ms at four
    # threads. So the value is read back below rather than assumed.
    _THREAD_KEY = "EngineConfig.onnxruntime.intra_op_num_threads"

    def _get_engine(self):
        if self._engine is None:
            logger.debug(f"Loading RapidOCR models ({self.threads} threads)...")
            self._engine = self._build_engine()
        return self._engine

    def _build_engine(self):
        """
        The engine, tuned where the build allows it and plain where it does not.

        The thread count is a speed setting, not a correctness one, so a build
        that rejects the key is worth falling back on rather than failing the
        run: a slower read still credits the chest to the right player. It is
        worth a warning, though, and not a debug line - silence here is what let
        the setting go unapplied for a whole package migration.
        """
        if RAPIDOCR_API == "v3":
            try:
                engine = RapidOCR(params={self._THREAD_KEY: self.threads})
            except Exception as exc:
                logger.warning(f"RapidOCR did not accept the thread setting ({exc}); "
                               f"using its own defaults, which measure about twice as slow.")
                return RapidOCR()
            applied = self._applied_threads(engine)
            if applied != self.threads:
                logger.warning(
                    f"RapidOCR ignored '{self._THREAD_KEY}' (it reads back as {applied}, "
                    f"not {self.threads}). The setting has moved again in this build; reads "
                    f"will run at the package default, roughly twice as slow.")
            return engine
        try:
            return RapidOCR(intra_op_num_threads=self.threads)
        except TypeError:
            # Older builds do not accept the threading argument.
            return RapidOCR()

    @classmethod
    def _applied_threads(cls, engine) -> Optional[int]:
        """What the built engine actually believes, or None if it cannot be read."""
        node = getattr(engine, "cfg", None)
        for part in cls._THREAD_KEY.split("."):
            if node is None:
                return None
            try:
                node = node.get(part)
            except Exception:
                return None
        return node

    @staticmethod
    def _triples(raw) -> List[Tuple[Any, str, float]]:
        """
        (box, text, score) triples, whichever dialect produced them.

        The old package returned the triples themselves alongside an elapsed
        time; the current one returns an object carrying three parallel
        sequences. Both also have more than one way of saying 'nothing found' -
        None, an empty list, or an output whose boxes are None - and every one of
        those has to come back out of here as no lines at all.
        """
        if raw is None:
            return []
        if not hasattr(raw, "txts"):
            # Legacy: (triples, elapsed).
            if isinstance(raw, tuple) and len(raw) == 2:
                raw = raw[0]
            return list(raw) if raw else []
        boxes = getattr(raw, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return []
        return list(zip(boxes, raw.txts or (), raw.scores or ()))

    def read(self, image: np.ndarray, **_) -> List[Dict[str, Any]]:
        try:
            raw = self._get_engine()(image, use_cls=False)
        except TypeError:
            try:
                raw = self._get_engine()(image)
            except Exception as exc:
                logger.error(f"RapidOCR failed: {exc}")
                return []
        except Exception as exc:
            logger.error(f"RapidOCR failed: {exc}")
            return []

        result = self._triples(raw)
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
            words.sort(key=lambda w: w["left"])
            segments = []
            current_seg = [words[0]]
            for w in words[1:]:
                prev = current_seg[-1]
                gap = w["left"] - (prev["left"] + prev["width"])
                threshold_gap = max(30, int(prev["height"] * 1.8))
                if gap > threshold_gap:
                    segments.append(current_seg)
                    current_seg = [w]
                else:
                    current_seg.append(w)
            segments.append(current_seg)

            for seg in segments:
                left = min(w["left"] for w in seg)
                top = min(w["top"] for w in seg)
                right = max(w["left"] + w["width"] for w in seg)
                bottom = max(w["top"] + w["height"] for w in seg)
                lines.append({
                    "text": " ".join(w["text"] for w in seg),
                    "center": ((left + right) / 2, (top + bottom) / 2),
                    "confidence": sum(w["conf"] for w in seg) / len(seg),
                    "top": top,
                    "height": bottom - top,
                })
        return lines


#: A digit, and the letters the reading engine has been seen to put in its place.
#: Only two pairs, because only two have evidence behind them: `T6o00SK` for
#: `T6000SK` is the confusion this corpus actually produced, and l/1/I is the
#: same mistake in the other alphabet. Every pair added here widens
#: `_mixed_token` and buys more Tesseract calls for the same panels - these two
#: fire on 60 of 3750 lines, and adding S/5 and B/8 took that to 91 without
#: fixing anything that was wrong.
DIGIT_LOOKALIKES = {"0": "oO", "1": "lIi"}
_LOOKALIKE_LETTERS = frozenset("".join(DIGIT_LOOKALIKES.values()))


def _mixed_token(text: str) -> bool:
    """
    Is any single word here a digit and a digit-shaped letter side by side?

    This is the entire trigger for calling the second engine, so it has to be
    narrow. Asking merely whether the line contains a digit fires on 287 of the
    314 measured panels - `Source: Level 35 epic Crypt` sits on most rows - and
    would pay for Tesseract on nearly every read, which is slower than either
    engine alone. Asking for one word that mixes the two fires on 27 panels, and
    those are the words genuinely at risk: `T6o00SK`, never `Level 35`.

    It also keeps `Dark²` out, which matters: Tesseract reads that name as
    `Dark?`, so a wider trigger would hand 26 correct lines to the worse witness.
    """
    return any(
        any(character.isdigit() for character in token)
        and any(character in _LOOKALIKE_LETTERS for character in token)
        for token in text.split()
    )


def _digit_swap(base: str, other: str) -> Optional[str]:
    """
    `base` carrying the digits `other` saw in it, or None if that is not safe.

    The substitution runs one way only - a letter of `base` becomes a digit of
    `other` - so the engine that reads letters well keeps the letters, and the
    length never changes.

    Two guards keep the other engine's own mistakes out. The digit it offers has
    to sit NEXT TO another digit in its own reading, which is what tells a serial
    number from a word: the `o` in `T6000SK` has digits on either side, the `o`
    in `Tolga46` has `T` and `l`, so an engine that misread `Tolga46` as
    `T0lga46` is refused here rather than believed. And every other difference
    between the two readings must be nil - one disagreement about a letter and
    the whole word is left alone, because a disagreement about letters is not
    something this function is competent to settle.
    """
    if len(base) != len(other) or base == other:
        return None

    out = []
    for index, (mine, theirs) in enumerate(zip(base, other)):
        if mine == theirs:
            out.append(mine)
            continue
        if mine not in DIGIT_LOOKALIKES.get(theirs, ""):
            return None
        neighbours = other[max(0, index - 1):index] + other[index + 1:index + 2]
        if not any(character.isdigit() for character in neighbours):
            return None
        out.append(theirs)
    return "".join(out)


def _restore_digits(base: str, others: Sequence[str]) -> str:
    """
    Every doubtful word of `base`, settled against the words `others` read.

    Word by word rather than line by line, because the two engines do not agree
    on where a line ends: RapidOCR often boxes the player's name on its own while
    Tesseract reads the whole row, label and all. Requiring the two lines to be
    the same length threw the correction away exactly where it was needed.
    """
    parts = re.split(r"(\s+)", base)
    candidates = [token for text in others for token in text.split()]
    for position, token in enumerate(parts):
        if not token.strip() or not _mixed_token(token):
            continue
        for candidate in candidates:
            fixed = _digit_swap(token, candidate)
            if fixed:
                parts[position] = fixed
                break
    return "".join(parts)


class HybridBackend:
    """
    RapidOCR reads; Tesseract is asked about digits and nothing else.

    Measured over 314 real chest panels - 3750 lines, one engine per process,
    the same saved images fed to each:

        engine                        per panel    lines wrong (of 257 checked)
        RapidOCR TINY models             131 ms                             34
        Tesseract (por, psm 6, oem 1)    258 ms                            204
        RapidOCR (4 threads, no cls)     551 ms                             13
        this class                       557 ms                              8

    'Lines wrong' counts only lines whose correct spelling was read off the panel
    by eye, which is where the engines disagree; the 3493 lines all of them agree
    about are evidence for nobody and are left out.

    The TINY row is the interesting one and is left on the table deliberately: at
    a quarter of the time it gets 223 of those lines right, and 21 of the 34 it
    misses are a single player whose Ş it drops. If a run ever has to fit in a
    smaller window that is the trade to make, and `Det.model_type` /
    `Rec.model_type` is where it is made. It is not the default because 1200
    chests cost about three minutes here in total, and three minutes is not worth
    26 more wrong names.

    Of RapidOCR's 13 misreadings, 5 are one player - `Gugenot T6000SK`, whose
    zeros it reads as the letter o - and Tesseract spelt that string right 10
    times out of 10. That is the whole reason the second engine is still here. So
    it is consulted only about words mixing digits with digit-shaped letters
    (`_mixed_token`: 27 of the 314 panels), and nothing but digits is taken back
    from it.

    The arrangement this replaced ran the other way about - Tesseract first,
    RapidOCR only when Tesseract's confidence sagged - on the belief that a low
    score marked its mistakes. It does not. The gate fired on 14 of 315 reads
    while 204 lines went out wrong, because Tesseract is not hesitant about
    `Vali` for `VaLi`; it is confident and wrong. A confidence gate cannot catch
    that, so there is no longer one.
    """

    name = "RapidOCR + Tesseract for digits"

    def __init__(self, rapid: RapidOCRBackend, tesseract: TesseractBackend):
        self.rapid = rapid
        self.tesseract = tesseract
        self.verifications = 0
        self.reads = 0
        # Words already put to Tesseract and confirmed unchanged. A clan of
        # thirty players sends the same names over and over - `Tolga46` alone is
        # 55 of the 60 lines that trip `_mixed_token` - and a word the two
        # engines already spelt identically does not need asking about twice.
        # Only confirmations are remembered: a word that WAS corrected stays
        # doubtful, so the next panel showing it gets corrected too.
        self._confirmed: set = set()

    def available(self) -> Tuple[bool, str]:
        rapid_ok, rapid_reason = self.rapid.available()
        tess_ok, tess_reason = self.tesseract.available()
        if rapid_ok and tess_ok:
            return True, (f"{rapid_reason} + {tess_reason} — second engine consulted only about "
                          f"words mixing digits and digit-shaped letters")
        if rapid_ok:
            return True, f"{rapid_reason} (without Tesseract: {tess_reason})"
        if tess_ok:
            return True, f"{tess_reason} (without RapidOCR: {rapid_reason})"
        return False, f"{rapid_reason}; {tess_reason}"

    def read(self, image: np.ndarray, careful: bool = False, **kwargs) -> List[Dict[str, Any]]:
        """
        `careful=True` re-checks words this run had already confirmed.

        The collector sets it when the database did not recognise a field, and it
        is worth less than it was: the second engine can only settle digits now,
        so an unrecognised foreign name has no better witness to appeal to. What
        it still does is lift the cache, in case a word that matched once was a
        different word this time. It does not force a call that has nothing to
        find - with no doubtful word on the panel there is nothing for Tesseract
        to say, and the 258 ms is not spent.
        """
        self.reads += 1
        lines = self.rapid.read(image, **kwargs) if self.rapid.available()[0] else []

        if not lines:
            # No reading at all: the worse engine still beats an empty panel.
            return self.tesseract.read(image, **kwargs) if self.tesseract.available()[0] else []

        doubtful = [line for line in lines if _mixed_token(line["text"])]
        if not careful:
            doubtful = [line for line in doubtful
                        if normalize_name(line["text"]) not in self._confirmed]
        if not doubtful or not self.tesseract.available()[0]:
            return lines

        self.verifications += 1
        other = self.tesseract.read(image, **kwargs)
        if not other:
            return lines

        for line in doubtful:
            nearby = sorted(other, key=lambda o: abs(o["center"][1] - line["center"][1]))
            fixed = _restore_digits(line["text"], [item["text"] for item in nearby])
            if fixed != line["text"]:
                logger.debug(f"Digits corrected: '{line['text']}' -> '{fixed}'")
                line["text"] = fixed
            else:
                # The two engines spell it the same way; stop paying for it.
                self._confirmed.add(normalize_name(line["text"]))
        return lines


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
        self._hybrid = HybridBackend(self._rapid, self._tesseract)
        self._warned_small_glyphs = False

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
        self._check_glyph_size(lines)
        return lines

    def _check_glyph_size(self, lines: List[TextLine]):
        """
        Says so, once, when the text is coming back too small to read well.

        Small glyphs are not a separate fault from bad readings - they are the
        same fault seen earlier. The engine stops resolving the gap between two
        words before it stops resolving the words, so gluing appears first and
        accents go next, which is exactly the order the bad run showed.

        Once per engine, not once per panel: the condition lasts a whole run, and
        314 copies of the same warning would bury the log it is meant to improve.
        """
        if self._warned_small_glyphs or len(lines) < ENOUGH_LINES_TO_JUDGE:
            return
        median = statistics.median(line.height for line in lines)
        if median >= SUSPECT_LINE_HEIGHT:
            return
        self._warned_small_glyphs = True
        logger.warning(
            f"Text is coming back {median:.0f}px tall where a good capture reads "
            f"{HEALTHY_LINE_HEIGHT}px. The game is probably drawing this screen smaller than it "
            f"did at calibration. Expect glued words ('EpicMonster Chest') and dropped accents "
            f"in this run, and names split across spellings in the database. Reloading the game "
            f"at the calibrated window size is what fixes it."
        )

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
