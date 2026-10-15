"""Walks the Journal and learns which name belongs to which tournament type.

The game never sends a tournament's name, only its type. The name is on screen,
in the card title "Your Clanmates' results in <name>". Pairing the two by the
order of the cards would be fragile (the Journal mixes other messages in), so
the pairing is causal instead:

1. read the card's title (OCR);
2. open the card and press "Show details";
3. the game answers with the ranking message, which carries the result id and
   the tournament type - that answer is what the name belongs to;
4. scroll the ranking until the players' names arrive: the game only asks for
   the profiles of the rows it shows;
5. close back to the list and go on.

A name already mapped is not opened again, so after the first pass a run only
opens tournaments it has never seen. The game may keep a ranking it already
fetched and not ask again; reloading the game page (F5) before mapping avoids it.

The browser, the OCR and the image search are handed in, and only a handful of
their methods are used, so the whole walk can be run against a simulated game
(tests/test_events_mapper.py).
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

from core.vision import changed_fraction
from utils.cancel import cancellation

from . import journal
from .layout import JournalLayout
from .titles import card_age_days, extract_tournament_name, parse_page_number

STATUS_MAPPED = "mapped"
STATUS_NO_ANSWER = "no answer from the game"
STATUS_UNKNOWN_FORMAT = "the game answered in an unknown format"
STATUS_SKIPPED = "already known"


@dataclass
class MappedCard:
    name: str
    page: int
    card: int
    status: str = STATUS_NO_ANSWER
    result_uid: Optional[str] = None
    tournament_key: Optional[str] = None
    ended_at: Optional[int] = None
    icon_png: Optional[bytes] = None
    unnamed: int = 0
    ranking: str = ""

    @property
    def game_type(self) -> Optional[int]:
        if not self.tournament_key:
            return None
        try:
            return int(self.tournament_key.split(":")[0])
        except ValueError:
            return None


@dataclass
class MapperOptions:
    max_pages: int = 30
    open_journal: bool = True
    reopen_known: bool = False
    answer_timeout: float = 8.0
    step_delay: float = 1.2
    names_timeout: float = 40.0
    names_delay: float = 0.8
    names_stalls: int = 4   # scrolls in a row that neither move the list nor bring a name before giving up
    names_max_time: float = 300.0   # the whole walk down one ranking, at most
    # The game moves the ranking a small fixed step per wheel event, whatever
    # its delta: each scroll is a burst of this many events, repeated until the
    # list stops moving (turning past the end does nothing).
    names_wheel_turns: int = 30
    # Look back only to this moment (UTC seconds): the Journal lists the newest
    # first, so once `stop_after_older` tournaments in a row ended before it,
    # the rest is older still and was covered by an earlier search. None walks
    # every page.
    stop_before: Optional[int] = None
    stop_after_older: int = 2
    # Look back only this many days, by the age each card shows in its title
    # ("yesterday", "7 days ago"): a card older than that is not opened, and
    # `stop_after_older` of them in a row end the walk. None sets no limit.
    max_age_days: Optional[int] = None
    # Right after the game page reloads the Journal may not open at the first
    # click: try again this many times, this far apart.
    journal_retries: int = 0
    journal_retry_delay: float = 15.0


def normalize(name: str) -> str:
    return " ".join(name.lower().split())


class ReachedCutoff(Exception):
    """The walk reached tournaments older than where it was told to stop."""


class ReachedAgeLimit(Exception):
    """The walk reached cards received more days ago than it was told to look back."""


# Why a walk ended (JournalMapper.finish_reason). Only END, CUTOFF and DAYS mean
# the whole period was looked at.
FINISH_END = "end"            # the last page of the Journal
FINISH_CUTOFF = "cutoff"      # tournaments older than stop_before
FINISH_DAYS = "days"          # cards older than max_age_days
FINISH_MAX_PAGES = "max_pages"
FINISH_NO_JOURNAL = "no_journal"  # the first page showed no title: the Journal did not open
COMPLETE_REASONS = (FINISH_END, FINISH_CUTOFF, FINISH_DAYS)


class JournalMapper:
    def __init__(
        self,
        browser,
        layout: JournalLayout,
        ocr,
        collector: journal.JournalCollector,
        options: Optional[MapperOptions] = None,
        known_names: Optional[Set[str]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_card: Optional[Callable[[MappedCard], None]] = None,
        vision=None,
    ):
        self.browser = browser
        self.layout = layout
        self.ocr = ocr
        self.collector = collector
        self.options = options or MapperOptions()
        self.known = {normalize(n) for n in (known_names or set())}
        self.on_log = on_log
        self.on_card = on_card
        # core.vision.Vision, to find "Show details" by its picture. Without it
        # the calibrated position is pressed.
        self.vision = vision
        self.cards: List[MappedCard] = []
        self.finish_reason = FINISH_MAX_PAGES
        self._older_streak = 0
        self._old_cards = 0
        self._ages_read = 0
        # Whether the last turn of page was confirmed by the page number on screen.
        self._page_number_moved = False

    # --------------------------------------------------------------- control
    def _log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    # Every wait goes through the run's cancellation token, so holding ESC stops
    # the walk at once and not after the page it is on.
    @staticmethod
    def _check() -> None:
        cancellation.check()

    @staticmethod
    def _sleep(seconds: float) -> None:
        cancellation.sleep(seconds)

    # ------------------------------------------------------------------- run
    def run(self) -> List[MappedCard]:
        missing = self.layout.missing_steps()
        if missing:
            raise RuntimeError("Incomplete Journal calibration: " + ", ".join(missing))
        if not self.ocr.available():
            raise RuntimeError(self.ocr.error or "OCR unavailable")

        try:
            self._open_journal()
            previous_signature = None
            for page in range(1, self.options.max_pages + 1):
                self._check()
                titles = self._read_titles()
                for _attempt in range(self.options.journal_retries if page == 1 else 0):
                    if any(titles):
                        break
                    self._log(f"The Journal did not open; trying again in {self.options.journal_retry_delay:.0f}s.")
                    self._sleep(self.options.journal_retry_delay)
                    self._open_journal()
                    titles = self._read_titles()
                signature = "|".join(titles)
                if not any(titles):
                    if page == 1:
                        self.finish_reason = FINISH_NO_JOURNAL
                        self._log("No title read on the first page: the Journal did not open.")
                    else:
                        self.finish_reason = FINISH_END
                        self._log(f"Page {page}: no title read. End.")
                    break
                # A run of the same message can fill whole pages with the same
                # titles: only when the page number cannot be read does that mean
                # the arrow stopped moving.
                if signature == previous_signature and not self._page_number_moved:
                    self.finish_reason = FINISH_END
                    self._log(f"Page {page} is the same as the previous one: last page reached.")
                    break
                previous_signature = signature
                self._log(f"Page {page}: " + " / ".join(t for t in titles if t)[:300])

                for index, text in enumerate(titles):
                    self._check()
                    if self._too_old(text):
                        continue
                    name = extract_tournament_name(text)
                    if not name:
                        continue
                    self._handle_card(page, index, name)

                if page == 1 and self.options.max_age_days is not None and not self._ages_read:
                    self._log("No card on the first page shows how long ago it was received (\"yesterday\", "
                              "\"2 days ago\"): the limit of days cannot be applied. Calibrate the card title "
                              "regions wide enough to include it.")

                if not self._next_page():
                    self.finish_reason = FINISH_END
                    self._log("The page did not change when advancing: end of the Journal.")
                    break
        except ReachedCutoff:
            self.finish_reason = FINISH_CUTOFF
            self._log(f"{self.options.stop_after_older} tournament(s) in a row older than the last complete search: "
                      "the rest of the Journal has been seen already. End.")
        except ReachedAgeLimit:
            self.finish_reason = FINISH_DAYS
            self._log(f"Reached cards received more than {self.options.max_age_days} day(s) ago: "
                      "the rest of the Journal is older still. End.")
        return self.cards

    def _too_old(self, text: str) -> bool:
        """
        Whether a card is beyond the days to look back, by the age in its title.

        Every card counts, tournament or not: the Journal lists the newest
        first, so any card says how far back the walk has got. One without a
        readable age is never too old, and does not break a run of old ones.
        """
        limit = self.options.max_age_days
        age = card_age_days(text) if limit is not None else None
        if age is None:
            return False
        self._ages_read += 1
        if age <= limit:
            self._old_cards = 0
            return False
        self._old_cards += 1
        if self._old_cards >= max(1, self.options.stop_after_older):
            raise ReachedAgeLimit()
        return True

    # ------------------------------------------------------------- journal
    def _open_journal(self) -> None:
        if not self.options.open_journal:
            return
        for name in ("journal_button", "journal_events_tab"):
            point = self.layout.point(name)
            if point is not None:
                self._log(f"Clicking {name} {point}")
                self.browser.click(*point)
                self._sleep(self.options.step_delay)

    def _read_titles(self) -> List[str]:
        titles = []
        for index in range(self.layout.cards_per_page):
            region = self.layout.card_title_region(index)
            image = self.browser.capture(region, scale=2.0) if region else None
            titles.append(self.ocr.read(image))
        return titles

    def _next_page(self) -> bool:
        point = self.layout.point("journal_next_page")
        if point is None:
            return False
        page_region = self.layout.region("journal_page_area")
        before_page = parse_page_number(self.ocr.read(self.browser.capture(page_region, scale=2.0), True)) if page_region else None
        before = self.browser.capture(self.layout.card_title_region(0), scale=1.0)

        self.browser.click(*point)
        self._sleep(self.options.step_delay)

        self._page_number_moved = False
        if page_region:
            after_page = parse_page_number(self.ocr.read(self.browser.capture(page_region, scale=2.0), True))
            if before_page is not None and after_page is not None:
                self._page_number_moved = after_page != before_page
                return self._page_number_moved
        after = self.browser.capture(self.layout.card_title_region(0), scale=1.0)
        if before is None or after is None:
            return True
        return not np.array_equal(before, after)

    # ---------------------------------------------------------------- cards
    def _handle_card(self, page: int, index: int, name: str) -> None:
        card = MappedCard(name=name, page=page, card=index + 1)
        key = normalize(name)
        if key in self.known and not self.options.reopen_known:
            card.status = STATUS_SKIPPED
            self._emit(card)
            return

        card.icon_png = self._icon(index)
        # By when each ranking was last received: one opened earlier in this
        # session arrives again under the same id, and still counts as the answer.
        before = {uid: ranking[2] for uid, ranking in self.collector.rankings.items()}
        before_unknown = set(self.collector.unknown_details.keys())

        point = self.layout.card_click_point(index)
        self._log(f"Opening \"{name}\" (page {page}, card {index + 1})")
        self.browser.click(*point)
        self._sleep(self.options.step_delay)

        self._press_show_details()
        uid, unknown = self._wait_for_answer(before, before_unknown)
        if uid is not None:
            key_type, _, _, ranking = self.collector.rankings[uid]
            entry = self.collector.entries.get(uid)
            card.result_uid = uid
            card.tournament_key = key_type or (entry.tournament_key if entry else None)
            card.ranking = ranking
            card.ended_at = entry.ended_at if entry else None
            card.status = STATUS_MAPPED
            self.known.add(key)
            self._log(f"  \"{name}\" = type {card.tournament_key}")
            card.unnamed = self._collect_names(uid)
        elif unknown is not None:
            card.status = f"{STATUS_UNKNOWN_FORMAT}: {unknown.split(' · ')[0]}"
            self._log(f"  The game answered the ranking of \"{name}\" in a format not read yet: {unknown}")
        else:
            self._log(f"  The game did not send the ranking of \"{name}\" (it may have been cached: reload the game).")

        self._back_to_list(index, name)
        self._emit(card)
        self._count_older(card)

    def _count_older(self, card: MappedCard) -> None:
        """Stop once enough tournaments in a row ended before `stop_before`.
        A card whose date is unknown neither counts nor breaks the run."""
        if self.options.stop_before is None or card.ended_at is None:
            return
        self._older_streak = self._older_streak + 1 if card.ended_at < self.options.stop_before else 0
        if self._older_streak >= max(1, self.options.stop_after_older):
            raise ReachedCutoff()

    def _emit(self, card: MappedCard) -> None:
        self.cards.append(card)
        if self.on_card:
            self.on_card(card)

    def _icon(self, index: int) -> Optional[bytes]:
        region = self.layout.card_icon_region(index)
        if region is None or cv2 is None:
            return None
        image = self.browser.capture(region, scale=1.0)
        if image is None:
            return None
        ok, png = cv2.imencode(".png", image)
        return png.tobytes() if ok else None

    def _press_show_details(self) -> None:
        found = None
        if self.vision is not None:
            found = self.vision.find(self.layout.ref_path("journal_show_details"), None,
                                     base_scale=self.layout.image_scale)
        if found is not None:
            x, y = found[0] + found[2] // 2, found[1] + found[3] // 2
        else:
            center = self.layout.region_center("journal_show_details")
            if center is None:
                return
            x, y = center
            if self.vision is not None:
                self._log(f"  Show details button not recognised (similarity {self.vision.last_score:.2f}); "
                          "pressing the calibrated position.")
        self.browser.click(x, y)

    def _wait_for_answer(self, before: Dict[str, float], before_unknown: Set[str]) -> Tuple[Optional[str], Optional[str]]:
        """(result uid, None) when the ranking arrived, (None, "kind · sample") when the answer was not one."""
        deadline = time.time() + self.options.answer_timeout
        while time.time() < deadline:
            self._check()
            new = {uid for uid, ranking in list(self.collector.rankings.items()) if before.get(uid) != ranking[2]}
            if new:
                # Normally one; if several arrived, the newest is the one just asked for.
                return max(new, key=lambda uid: self.collector.rankings[uid][2]), None
            unknown = set(self.collector.unknown_details.keys()) - before_unknown
            if unknown:
                kind, sample = self.collector.unknown_details[unknown.pop()]
                return None, f"{kind} · {sample}"
            self._sleep(0.25)
        return None, None

    def _collect_names(self, uid: str) -> int:
        """Scroll the ranking until every player has a profile. The players still without one."""
        missing = self.collector.missing_profiles(uid)
        if not missing:
            self._log(f"  {len(self.collector.rankings[uid][1])} player(s) and every name already known: no scrolling.")
            return 0
        region = self.layout.region("journal_ranking_list")
        self._log(f"  {len(missing)} player(s) without a name: " + ("scrolling the ranking list..." if region else
                  "waiting for the profiles (calibrate 'Journal: ranking list' so the list can be scrolled)"))
        # The timeout counts only the time without progress: a long ranking takes
        # many wheel turns to walk, and while the list still moves or names still
        # come it is not stuck. `names_max_time` caps the whole walk regardless.
        started = time.time()
        deadline = started + self.options.names_timeout
        stalls = 0
        while (missing and stalls < self.options.names_stalls and time.time() < deadline
               and time.time() - started < self.options.names_max_time):
            before = len(missing)
            moved = False
            if region is not None:
                view_before = self.browser.capture(region, scale=1.0)
                self.browser.scroll(region[0] + region[2] // 2, region[1] + region[3] // 2, delta_y=600,
                                    repetitions=self.options.names_wheel_turns, delay=0.05)
            self._sleep(self.options.names_delay)
            if region is not None:
                moved = changed_fraction(view_before, self.browser.capture(region, scale=1.0)) >= 0.01
            missing = self.collector.missing_profiles(uid)
            if moved or len(missing) < before:
                stalls = 0
                deadline = time.time() + self.options.names_timeout
            else:
                stalls += 1
        if missing:
            self._log(f"  {len(missing)} name(s) still missing; this ranking goes to the site for review.")
        else:
            self._log("  Every name received.")
        return len(missing)

    def _back_to_list(self, index: int, name: str) -> None:
        for step in ("journal_details_close", "journal_card_close"):
            point = self.layout.point(step)
            if point is not None:
                self.browser.click(*point)
            else:
                self.browser.press_key("Escape")
            self._sleep(self.options.step_delay * 0.7)

        # Back on the list, the same card shows the same title again.
        for _attempt in range(2):
            text = self.ocr.read(self.browser.capture(self.layout.card_title_region(index), scale=2.0))
            if normalize(extract_tournament_name(text) or "") == normalize(name):
                return
            self.browser.press_key("Escape")
            self._sleep(self.options.step_delay)
        raise RuntimeError(
            f"After closing \"{name}\" the Journal did not return to the list. "
            "Check the steps 'Journal: close the ranking' and 'Journal: close the card' in the calibration."
        )


def catalogue_key(game_type: int, ranking: str) -> str:
    """How a catalogue entry is told apart: a type can have several rankings."""
    return f"{game_type}:{ranking}" if ranking else str(game_type)


def catalogue_entries(cards: List[MappedCard]) -> List[dict]:
    """One entry per tournament type and ranking, ready for POST /api/v1/tournament-catalog."""
    by_entry: Dict[str, MappedCard] = {}
    for card in cards:
        if card.status != STATUS_MAPPED or card.game_type is None:
            continue
        key = catalogue_key(card.game_type, card.ranking)
        current = by_entry.get(key)
        if current is None or (current.icon_png is None and card.icon_png is not None):
            by_entry[key] = card

    entries = []
    for card in by_entry.values():
        entry = {"tournament_key": card.tournament_key, "ranking": card.ranking, "name": card.name}
        if card.ended_at:
            entry["ended_at"] = journal.Tournament(card.result_uid or "", ended_at=card.ended_at).ended_at_iso
        if card.icon_png:
            entry["image"] = base64.b64encode(card.icon_png).decode("ascii")
        entries.append(entry)
    return entries


def conflicts(cards: List[MappedCard]) -> List[str]:
    """Different names read for one type and ranking, or one name for different ones."""
    names_by_entry: Dict[str, Set[str]] = {}
    entries_by_name: Dict[str, Set[str]] = {}
    for card in cards:
        if card.status != STATUS_MAPPED or card.game_type is None:
            continue
        key = catalogue_key(card.game_type, card.ranking)
        names_by_entry.setdefault(key, set()).add(card.name)
        entries_by_name.setdefault(normalize(card.name), set()).add(key)
    out = [f"Type {t}: different names read ({', '.join(sorted(n))})" for t, n in names_by_entry.items() if len(n) > 1]
    out += [f"\"{n}\" appeared in different types ({', '.join(sorted(ts))})" for n, ts in entries_by_name.items() if len(ts) > 1]
    return out
