"""
Reading and collecting the clan gifts of one profile.

The loop is: find the 'Open' button by its picture, read the chest text that is
showing, write it to the database, then click. Reading before clicking is the
whole trick - once the button is clicked the entry is gone and the next one has
slid into its place.

The same reading also dates the chest. The panel shows how long each one has
left to live, and a chest waits up to twenty hours to be opened, so the hour the
collector happened to pass by says nothing about when the chest was generated -
which is the hour the counting is done by, and the hour `collected_at` holds.
See CHEST_LIFETIME_MINUTES.

The loop is bounded by a count, and by nothing cleverer than that. Reading the
screen cannot tell whether a click worked: this game hands out long runs of
identical chests - 3 479 in a row at the record - so an unchanged panel is
perfectly normal. See the note on MAX_CHESTS_PER_TAB below.
"""

import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config.settings import ProfileConfig
from database.db_connection import DatabaseConnection
from database.repositories import ChestRepository
from database.vocabulary import Vocabulary, load_vocabulary
from utils.cancel import cancellation
from utils.logger import logger
from utils.text_utils import parse_key_value_line

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

from .base_module import BaseModule

# The only bound on the loop, and it has to be generous: measured on four months
# of production, a busy hour brought 3 408 chests and a busy day 16 712, while a
# single run collected at most 284. Five thousand is far above any run seen and
# still stops a loop that is not consuming chests.
#
# There used to be a second guard here that ended the tab after reading the same
# chests three times, on the theory that an unchanging screen meant the clicks
# were not working. Production says otherwise: the longest run of consecutive
# identical chests is 3 479 - `Jormungandr's Chest` from `VaLi` out of the same
# squad - and 585 runs reach sixteen or more. That guard would have cut the
# collection short 585 times over this history. To be safe it would need to
# tolerate more than 870 identical batches, which is where this ceiling already
# stops things anyway, so it protected nothing and cost real chests.
#
# Identical chests are simply what this game produces, and no reading of the
# screen can tell a click that consumed one from a click that did nothing when
# the chest behind it looks the same. The place to catch a click that does not
# work is the calibration wizard's "Testar este passo", which measures it
# directly, before a run.
MAX_CHESTS_PER_TAB = 5000

# An empty panel is checked again before the tab is closed: the game refills the
# list a moment after the last chests are opened. One second was enough in the
# case that was reported; raising this only costs that much per tab, once.
EMPTY_RETRIES = 1
EMPTY_RETRY_WAIT = 1.0

# How long a chest lives. It appears in the list showing '19 h : 59 m' and the
# countdown falls from there, so what the panel shows is not a property of the
# reading - it is the chest's age, measured backwards: 18 h : 59 m left means an
# hour has already passed since the chest was generated.
#
# That is what `collected_at` has to hold. Storing the moment the collector read
# the chest instead put a chest generated at 10:51 in the database at 18:06, and
# with the game's day rolling over at 14:00 that moves it into the next day's
# count entirely. A run is minutes long but the chests in it can be up to twenty
# hours old, so the distortion is not a rounding error - it is a whole day.
CHEST_LIFETIME_MINUTES = 19 * 60 + 59

# The countdown as the panel prints it: two units, largest first, with whatever
# the engine makes of the separator between them - '19 h : 53 m', '19h:53m',
# '19h 53m' have all been read off the same screen. Days appear in the game's
# other timers ('4 d 23 h') and are accepted here so that such a reading is
# understood rather than half-matched.
COUNTDOWN = re.compile(
    r"(?<![0-9A-Za-z])"
    r"(?:(\d{1,3})\s*d\s*[:.]?\s*(\d{1,2})\s*h"
    r"|(\d{1,2})\s*h\s*[:.]?\s*(\d{1,2})\s*m)"
    r"(?![0-9A-Za-z])",
    re.IGNORECASE,
)

# One half of it - '19h', '53m' - which is how the reading arrives when the
# engine boxes each number separately.
COUNTDOWN_PART = re.compile(r"(?<![0-9A-Za-z])\d{1,3}\s*[dhm](?![0-9A-Za-z])", re.IGNORECASE)

# What separates two blocks printed on the same row from two pieces of the same
# block. Measured on the real panel: inside the countdown - its label, its
# hourglass, its numbers - the boxes sit 1 to 13 px apart, while the gap between
# the player's name and the countdown that follows it on the same line is 260 px.
# Anything in between is a comfortable place to draw the line.
MIN_BLOCK_GAP = 20
BLOCK_GAP_PER_HEIGHT = 2.5


def countdown_minutes(text: str) -> Optional[int]:
    """The countdown in a piece of text, as minutes, or None if there is none."""
    match = COUNTDOWN.search(text or "")
    if not match:
        return None
    days, day_hours, hours, minutes = match.groups()
    if days is not None:
        return int(days) * 24 * 60 + int(day_hours) * 60
    return int(hours) * 60 + int(minutes)


def generation_time(minutes_left: Optional[int], now: Optional[datetime] = None) -> Optional[datetime]:
    """
    When a chest showing `minutes_left` on its countdown was generated.

    None when the countdown could not be read - the caller then lets the
    database stamp the row itself, which is the old behaviour and the only
    honest answer when the age is unknown.

    The age is clamped to the chest's lifetime at both ends: a misread that
    claims more time than a chest can have would otherwise date the row in the
    future, and one that claims a negative remainder would date it before any
    chest could exist.
    """
    if minutes_left is None:
        return None
    age = min(max(CHEST_LIFETIME_MINUTES - minutes_left, 0), CHEST_LIFETIME_MINUTES)
    return (now or datetime.now()).replace(microsecond=0) - timedelta(minutes=age)


def _same_block(left, right) -> bool:
    """Whether two boxes on a row are close enough to be one block of text."""
    gap = right.left - (left.left + left.width)
    tolerance = max(MIN_BLOCK_GAP, int(BLOCK_GAP_PER_HEIGHT * max(left.height, right.height)))
    return gap <= tolerance


def _before_countdown(text: str) -> str:
    """
    What a row says before its countdown, read off the words alone.

    The fallback for when the boxes cannot be separated by position - one box
    holding both the player and the countdown, or a row printed so tightly that
    the two are one block. The countdown's label is recognised by its shape
    rather than its wording: the trailing tokens that end in a delimiter, plus
    the lone glyph of the hourglass.

    What is left is kept only if it still carries its own label. A row that was
    nothing but the countdown ('Time left: 19 h : 52 m') ends up without one and
    is dropped, which is the point - kept, it would become an extra row, and the
    chest's three fields are read by position.

    A label of two words leaves its first word behind ('Night King Time'): where
    the box is one, nothing says which words were the label and which the name.
    That is a suffix on a name the vocabulary still matches, and it is the
    fallback of a fallback - both engines in use box the two separately.
    """
    match = COUNTDOWN.search(text or "")
    if not match:
        return (text or "").strip()
    head = text[:match.start()].split()
    while head and (len(head[-1]) <= 1 or head[-1].endswith((":", "."))):
        head.pop()
    kept = " ".join(head)
    return kept if ":" in kept else ""


def split_row(row: Sequence, column_left: Optional[int] = None) -> Tuple[str, Optional[int]]:
    """
    One row of the panel, separated into the chest's own text and its countdown.

    The panel puts three things on the same line: the chest's text on the left,
    then its countdown, then its 'Open' button. The calibrated chest area spans
    all of them - it has to, since the countdown is what dates the chest - so
    grouping the line by eye, which is what `group_rows` does, hands back
    'From: Night King Time left: 12h:44m' and 'Source: Epic Chimera squad Open'
    as single strings. Stored like that, the button's caption becomes part of a
    source's name and the countdown part of a player's.

    So the right-hand column is cut off here, and what it held is returned
    separately. Two things mark where it begins: a box that carries part of a
    countdown, and `column_left` - where the 'Open' button was actually found for
    this chest, which is the one landmark on the row that is measured rather than
    guessed. From there the cut walks left over whatever is printed tight against
    it (the countdown's label, its hourglass) and keeps everything before.

    Nothing here reads the game's words. The label is in whatever language the
    account is set to, and the only signals used are the shape of a countdown and
    the blank space between one block of text and the next.
    """
    text = " ".join(line.text for line in row).strip()
    minutes = countdown_minutes(text)

    def in_right_column(index: int) -> bool:
        line = row[index]
        if COUNTDOWN_PART.search(line.text):
            return True
        return column_left is not None and line.left >= column_left

    first = next((i for i in range(len(row)) if in_right_column(i)), None)
    if first is None:
        return text, minutes

    while first > 0 and _same_block(row[first - 1], row[first]):
        first -= 1

    kept = [line.text for line in row[:first]]
    if not kept:
        # The whole row was the right-hand column - or it came back as a single
        # box with the text and the countdown inside it, and there was nothing to
        # cut by position. The words decide it instead.
        return _before_countdown(text), minutes
    return " ".join(kept).strip(), minutes


def visible_chests(calibration, vision, browser) -> List[dict]:
    """
    Every chest on screen: where its 'Open' button is and where its text is.

    The panel shows four at a time. The text of chest N sits at a fixed offset
    from its own button - the same offset the calibration recorded between
    `chest_area` and `open_button_area` - so one calibrated row describes all of
    them and nothing extra has to be marked by hand.

    It lives outside the module so the calibration wizard can ask exactly the
    question the run asks - 'which chests does this calibration see right now?'
    - instead of a lookalike that could pass while the run finds nothing.
    """
    chest_area = calibration.region("chest_area")
    button_area = calibration.region("open_button_area")
    if not (chest_area and button_area):
        return []

    # Look down the whole column: the calibrated box covers only the first row.
    _view_w, view_h = browser.viewport()
    top = max(0, button_area[1] - 10)
    column = (button_area[0], top, button_area[2], max(button_area[3], view_h - top))

    buttons = vision.find_all(calibration.ref_path("open_button"), column,
                              base_scale=calibration.image_scale)
    offset = chest_area[1] - button_area[1]
    return [{
        "button": button,
        "text_area": (chest_area[0], button[1] + offset, chest_area[2], chest_area[3]),
    } for button in buttons]


class ChestCollector(BaseModule):
    """Collects the 'Gifts' and 'Triumphal Gifts' tabs for a profile."""

    #: names the database already knows, loaded per profile
    vocabulary: Optional[Vocabulary] = None
    #: which profile is being collected, for the audit trail in `errors`
    profile_label: str = "?"
    #: the picture of each chest on screen, from the last batch read
    _crops: List[Any] = []
    #: how many minutes each chest of the last batch has left on its countdown
    _minutes_left: List[Optional[int]] = []

    @property
    def name(self) -> str:
        return "ChestCollector"

    # ------------------------------------------------------------- navigation
    def open_gift_menu(self) -> bool:
        missing = self.calibration.require("clan_button", "gift_button")
        if missing:
            logger.error(f"Calibration missing for the gifts menu: {', '.join(missing)}")
            return False

        clan = self.calibration.point("clan_button")
        gift = self.calibration.point("gift_button")
        timing = self.config.section("timing")
        delay = float(timing.get("click_delay", 0.5)) + 0.7

        logger.info("Opening the clan gifts menu...")
        self.browser.click(clan[0], clan[1], delay=delay)
        self.browser.click(gift[0], gift[1], delay=delay)
        return True

    def switch_tab(self, step_name: str, label: str) -> bool:
        point = self.calibration.point(step_name)
        if not point:
            logger.warning(f"Tab '{label}' is not calibrated ({step_name}); skipping it.")
            return False
        logger.info(f"Switching to the '{label}' tab...")
        self.browser.click(point[0], point[1], delay=1.0)
        return True

    # ------------------------------------------------------------------- OCR
    def parse_chest(self) -> Tuple[str, str, str, List[str]]:
        """
        Reads the chest box: its name, the player who sent it, and the source.

        The layout is three rows - a title, then two labelled values ('From: X',
        'Source: Y'). Only the value half is kept, and the label is whatever the
        game's language calls it, so the split is on the delimiter rather than on
        a known word.
        """
        area = self.calibration.region("chest_area")
        if not area:
            return "", "", "", []

        rows = self.ocr.read_rows(area)
        chest = rows[0].strip() if len(rows) >= 1 else ""
        player = parse_key_value_line(rows[1])[1] if len(rows) >= 2 else ""
        source = parse_key_value_line(rows[2])[1] if len(rows) >= 3 else ""
        return chest, player, source, rows

    # ------------------------------------------------------------- collection
    def _record(self, rows: List[str], repo: ChestRepository, crop=None,
                minutes_left: Optional[int] = None) -> int:
        """
        Writes one chest, from the rows already read for it.

        `minutes_left` is the chest's own countdown, which is what dates the row:
        see CHEST_LIFETIME_MINUTES. Where it could not be read the row is left
        for the database to stamp, as it always was.

        Returns 1 recorded, 2 incomplete but still worth opening, 0 nothing
        readable - the only case that stops the tab, since carrying on would
        open chests without recording them.
        """
        generated_at = generation_time(minutes_left)

        if not rows:
            # Unreadable, but not lost: the picture goes to the review queue and
            # the chest is opened like any other. Leaving it in the game only
            # meant reading the same unreadable thing again next run.
            logger.warning(
                f"[{self.profile_label}] Nothing readable in chest area; screenshot sent to "
                f"review queue."
            )
            repo.insert_incomplete_chest("", "", "", self._encode(crop), generated_at)
            return 2

        chest = rows[0].strip()
        player = parse_key_value_line(rows[1])[1] if len(rows) >= 2 else ""
        source = parse_key_value_line(rows[2])[1] if len(rows) >= 3 else ""

        if chest and player and source:
            if repo.insert_chest(chest, player, source, generated_at):
                logger.info(f"Chest recorded -> '{chest}' from '{player}' ({source})"
                            f"{self._age_note(minutes_left, generated_at)}")
                if self.vocabulary:
                    self.vocabulary.learn(chest, player, source)
                return 1
            logger.error(
                f"[{self.profile_label}] Database rejected chest '{chest}' from '{player}' "
                f"({source})."
            )
            return 0

        logger.warning(
            f"[{self.profile_label}] Incomplete chest -> name='{chest}' player='{player}' "
            f"source='{source}' | read={rows}. Screenshot sent to review queue."
        )
        repo.insert_incomplete_chest(chest, player, source, self._encode(crop), generated_at)
        return 2

    @staticmethod
    def _age_note(minutes_left: Optional[int], generated_at: Optional[datetime]) -> str:
        """The 'generated at' half of the log line, when the countdown was read."""
        if generated_at is None:
            return " — countdown unreadable, dated on arrival"
        age = min(max(CHEST_LIFETIME_MINUTES - (minutes_left or 0), 0), CHEST_LIFETIME_MINUTES)
        return f", generated {age // 60}h{age % 60:02d} ago at {generated_at:%H:%M}"

    @staticmethod
    def _encode(crop) -> Optional[bytes]:
        """The chest's picture as PNG bytes - encoded only when a chest fails."""
        if crop is None or cv2 is None or getattr(crop, "size", 0) == 0:
            return None
        ok, buffer = cv2.imencode(".png", crop)
        return buffer.tobytes() if ok else None

    def _visible_chests(self) -> List[dict]:
        return visible_chests(self.calibration, self.vision, self.browser)

    def _look_again(self, label: str) -> List[dict]:
        """
        Waits a moment and looks once more before believing the list is empty.

        An empty screen is not proof that the chests are gone: the game refills
        the list a beat after the last ones are opened, and a capture taken in
        that gap shows nothing. It is worst at the end, where only one or two
        chests were clicked and the pauses between clicks did not cover the
        refresh - which is how a run stopped with chests still waiting.

        Only when it is still empty after the wait does the tab end.
        """
        for attempt in range(1, EMPTY_RETRIES + 1):
            logger.info(
                f"No 'Open' buttons in '{label}'; waiting {EMPTY_RETRY_WAIT:g}s in case the game "
                f"is still refreshing the list ({attempt}/{EMPTY_RETRIES})..."
            )
            cancellation.sleep(EMPTY_RETRY_WAIT)
            chests = self._visible_chests()
            if chests:
                logger.info(f"{len(chests)} more chest(s) appeared after the wait; continuing.")
                return chests

        logger.info(f"No more chests in '{label}'.")
        return []

    def _read_batch(self, chests: List[dict], scale: float, careful: bool = False) -> List[List[str]]:
        """
        Reads every visible chest with a single capture and a single OCR pass.

        This is where the run got its speed back. Measured on the real panel,
        the engine takes about as long for four chests as for one - 1.6s either
        way - because the cost is model overhead, not image area. Reading them
        one at a time paid that overhead four times.
        """
        tops = [chest["text_area"][1] for chest in chests]
        height = chests[0]["text_area"][3]
        panel = (chests[0]["text_area"][0], min(tops), chests[0]["text_area"][2],
                 max(tops) + height - min(tops))

        image = self.browser.capture(panel, scale=scale)
        if image is None or image.size == 0:
            logger.warning(f"Empty capture for the chest panel {panel}.")
            self._crops = [None] * len(chests)
            self._minutes_left = [None] * len(chests)
            return [[] for _ in chests]

        # Each chest's slice of the panel, kept for the ones that fail: it is
        # what a person will read in the web interface to finish the record.
        self._crops = [
            image[int((top - panel[1]) * scale):int((top - panel[1] + height) * scale), :]
            for top in tops
        ]

        rows = self.ocr.group_row_lines(
            self.ocr.lines_from_image(image, (panel[0], panel[1]), scale, careful=careful))

        # Each row belongs to the chest whose slot it falls in; anything between
        # slots goes to the nearest one rather than being thrown away.
        # Where the panel's right-hand column starts, measured: the leftmost
        # 'Open' button found on screen. Everything the chest itself says is
        # printed to the left of it.
        column_left = min((chest["button"][0] for chest in chests), default=None)

        grouped: List[List[str]] = [[] for _ in chests]
        minutes_left: List[Optional[int]] = [None] * len(chests)
        for row in rows:
            text, minutes = split_row(row, column_left)
            centre_y = row[0].center[1]
            index = next((i for i, top in enumerate(tops) if top <= centre_y < top + height), None)
            if index is None:
                index = min(range(len(tops)), key=lambda i: abs(centre_y - (tops[i] + height / 2)))
            # The first countdown the chest shows is its own; a second reading in
            # the same slot would be a stray box from the row above or below.
            if minutes is not None and minutes_left[index] is None:
                minutes_left[index] = minutes
            # A row that was nothing but the countdown disappears with it, and
            # must not be kept as an empty row: the fields are read by position.
            if text:
                grouped[index].append(text)

        self._minutes_left = minutes_left
        return grouped

    def _resolve(self, rows: List[str]) -> Tuple[List[str], bool]:
        """
        Replaces each field with the name the database already knows.

        Returns the corrected rows and whether everything was recognised. An
        unrecognised field is the signal to read again with the slower engine:
        it means either a genuinely new name - a member who just joined - or a
        reading too mangled to place, and only the second case improves by
        looking harder.
        """
        if not self.vocabulary or not self.vocabulary.is_useful or len(rows) < 3:
            return rows, bool(rows)

        chest_name, chest_ok = self._resolved(self.vocabulary.chests, rows[0], "chest name")
        player_label, player_read = parse_key_value_line(rows[1])
        source_label, source_read = parse_key_value_line(rows[2])

        player, how = self.vocabulary.resolve_player(player_read)
        source, source_ok = self._resolved(self.vocabulary.sources, source_read, "source")

        if player and player != player_read:
            logger.info(f"Player '{player_read}' recorded as '{player}' ({how}).")

        rows = list(rows)
        rows[0] = chest_name
        rows[1] = f"{player_label}: {player or player_read}" if player_label else (player or player_read)
        rows[2] = f"{source_label}: {source}" if source_label else source
        return rows, bool(chest_ok and player and source_ok)

    @staticmethod
    def _resolved(vocabulary, reading: str, field: str) -> Tuple[str, bool]:
        """
        The known spelling of a reading, and whether it was recognised at all.

        Anything the collector writes differently from what it read is logged.
        These are only formatting differences, but they are still a change to
        stored data, and a change nobody can see is a change nobody can question.
        """
        found = vocabulary.resolve(reading)
        if found and found != reading:
            logger.info(f"Formatting — {field}: '{reading}' recorded as '{found}'.")
        return (found, True) if found else (reading, False)

    def collect_tab(self, label: str, repo: ChestRepository) -> Tuple[int, int]:
        """Opens every chest in the tab currently showing."""
        chest_delay = float(self.config.section("timing").get("chest_click_delay", 0.25))
        scale = float(self.config.section("ocr").get("capture_scale", 2.0))

        collected = 0
        incomplete = 0
        processed = 0

        logger.info(f"Collecting the '{label}' tab...")
        while processed < MAX_CHESTS_PER_TAB:
            cancellation.check()
            chests = self._visible_chests()
            if not chests:
                chests = self._look_again(label)
            if not chests:
                break

            batch = self._read_batch(chests, scale)
            resolved = [self._resolve(rows) for rows in batch]

            # One unrecognised field is enough to pay for the careful engine -
            # but only once, and for the whole panel, since it costs the same
            # for four chests as for one.
            if any(not ok for _rows, ok in resolved) and self.vocabulary and self.vocabulary.is_useful:
                logger.info("A field did not match known database entries; re-reading with secondary engine.")
                batch = self._read_batch(chests, scale, careful=True)
                resolved = [self._resolve(rows) for rows in batch]

            batch = [rows for rows, _ok in resolved]
            logger.info(f"{len(chests)} chest(s) on screen, read in one pass.")

            # Bottom-up: opening a chest shifts everything BELOW it up, so taking
            # the last one first leaves the positions already read untouched. No
            # waiting for the list to settle between clicks.
            failed = False
            crops = self._crops if len(self._crops) == len(chests) else [None] * len(chests)
            times = (self._minutes_left if len(self._minutes_left) == len(chests)
                     else [None] * len(chests))
            for chest, rows, crop, minutes in reversed(list(zip(chests, batch, crops, times))):
                cancellation.check()
                code = self._record(rows, repo, crop, minutes)
                if code == 0:
                    incomplete += 1
                    failed = True
                    break

                button = chest["button"]
                self.browser.click(button[0] + button[2] // 2, button[1] + button[3] // 2,
                                   delay=chest_delay)
                processed += 1
                if code == 1:
                    collected += 1
                else:
                    incomplete += 1

            if failed:
                logger.error(f"Stopping the '{label}' tab: a chest could not be recorded.")
                break
        else:
            logger.warning(f"Reached the {MAX_CHESTS_PER_TAB} chest ceiling in '{label}'.")

        logger.info(f"Tab '{label}': {collected} collected, {incomplete} incomplete.")
        return collected, incomplete

    def collect_for_profile(self, profile: ProfileConfig) -> Dict[str, Any]:
        """Collects both gift tabs for one profile and writes them to its database."""
        missing = self.calibration.require("chest_area", "open_button_area", "open_button")
        if missing:
            logger.error(f"Calibration missing for chest collection: {', '.join(missing)}")
            return {"success": False, "collected": 0, "incomplete": 0, "profile": profile.name}

        if not profile.has_database:
            logger.error(f"Profile '{profile.label}' has no database configured; skipping it.")
            return {"success": False, "collected": 0, "incomplete": 0, "profile": profile.name,
                    "reason": "no database configured"}

        with DatabaseConnection(profile.database, profile.label) as connection:
            if not connection:
                return {"success": False, "collected": 0, "incomplete": 0, "profile": profile.name,
                        "reason": "database connection failure"}

            repo = ChestRepository(connection)
            self.profile_label = profile.label
            self.vocabulary = (load_vocabulary(connection)
                               if self.config.section("ocr").get("use_known_names", True) else None)

            if not self.open_gift_menu():
                logger.error(f"[{profile.label}] Clan gift menu did not open.")
                return {"success": False, "collected": 0, "incomplete": 0, "profile": profile.name,
                        "reason": "gift menu did not open"}

            self.switch_tab("gifts_tab", "Gifts")
            gifts, gifts_incomplete = self.collect_tab("Gifts", repo)

            triumphal = triumphal_incomplete = 0
            if self.switch_tab("triumphal_gifts_tab", "Triumphal Gifts"):
                triumphal, triumphal_incomplete = self.collect_tab("Triumphal Gifts", repo)
                self.switch_tab("gifts_tab", "Gifts")

        total = gifts + triumphal
        total_incomplete = gifts_incomplete + triumphal_incomplete
        logger.info(
            f"Profile '{profile.label}': {total} chests collected "
            f"({gifts} gifts + {triumphal} triumphal), {total_incomplete} incomplete."
        )

        self.browser.press_key("Escape", delay=0.4)
        return {
            "success": True,
            "profile": profile.name,
            "account": profile.account_name,
            "collected": total,
            "collected_gifts": gifts,
            "collected_triumphal": triumphal,
            "incomplete": total_incomplete,
        }

    def run(self, profile: Optional[ProfileConfig] = None, **kwargs) -> Dict[str, Any]:
        if profile is None:
            raise ValueError("ChestCollector.run requires a profile.")
        return self.collect_for_profile(profile)
