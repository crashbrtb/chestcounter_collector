"""Walks the Journal over the network, and finds the tournament types the site has no name for.

1. the list is asked for page after page, newest first, until the walk reaches what
   it was told not to look behind, or the end;
2. each card of the clan's results has its ranking asked for, and the players whose
   names are not known yet have their profiles asked for;
3. the game never sends a tournament's name, only its type. The site's catalogue
   names the types it knows; each of the others is left for the end, when the page of
   the Journal with its first card is pictured and sent to the catalogue.

A card's place in the list says where it is on screen: the Journal shows the same
entries in the same order, `cards_per_page` at a time. Nothing reads the picture: a
person names the type on the site, since a name read wrong would be given to every
tournament of that type. Until then its rankings are not sent, and the search is not
complete, so the next one looks again. While the site holds a picture of a type, no
other is taken.

Which cards are the clan's is in the list: the fourth field of a classic result is 1
for the clan's ranking and 0 for the player's own, a world ranking of up to 1.6 MB
that has no card of the clan's results.

The feed and the screen are handed in, so the whole walk can be run against a
simulated game (tests/test_events_mapper.py).
"""
from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set

from utils.cancel import cancellation

from . import journal, tbcodec
from .layout import DEFAULT_CARDS_PER_PAGE
from .traffic import GameLinkError

STATUS_MAPPED = "mapped"
STATUS_NO_ANSWER = "no answer from the game"
STATUS_UNKNOWN_FORMAT = "the game answered in an unknown format"
STATUS_NO_NAME = "its type has no name yet"

# The least time between one request and the next.
REQUEST_INTERVAL = 0.2
MAX_FAILURES = 3
PICTURE_ATTEMPTS = 2
# Kinds that look like a clan's ranking but no reader knows, to be pointed out.
LOOKS_LIKE_A_RANKING = re.compile(r"clan_.*statistic")


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
    picture_png: Optional[bytes] = None
    unnamed: int = 0
    ranking: str = ""
    for_catalogue: bool = False    # the card a new type is told to the site's catalogue with

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
    max_pages: int = 200   # requests of the list: a safety limit
    cards_per_page: int = DEFAULT_CARDS_PER_PAGE
    # Look back only to this moment (UTC seconds). None walks every page.
    stop_before: Optional[int] = None
    # Look back only this many days, counted as the calendar does. None sets no limit.
    max_age_days: Optional[int] = None
    interval: float = REQUEST_INTERVAL


# Why a walk ended (JournalMapper.finish_reason). Only END, CUTOFF and DAYS mean
# the whole period was looked at.
FINISH_END = "end"            # the last page of the Journal
FINISH_CUTOFF = "cutoff"      # entries older than stop_before
FINISH_DAYS = "days"          # entries older than max_age_days
FINISH_MAX_PAGES = "max_pages"
FINISH_NO_JOURNAL = "no_journal"  # the game stopped giving the list
COMPLETE_REASONS = (FINISH_END, FINISH_CUTOFF, FINISH_DAYS)


class JournalMapper:
    def __init__(
        self,
        feed,
        collector: journal.JournalCollector,
        options: Optional[MapperOptions] = None,
        known_name: Callable[[journal.Tournament], str] = lambda _t: "",
        take_picture: Callable[[int, int], Optional[object]] = lambda _page, _card: None,
        has_picture: Callable[[journal.Tournament], bool] = lambda _t: False,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self.feed = feed
        self.collector = collector
        self.options = options or MapperOptions()
        self.known_name = known_name
        self.take_picture = take_picture
        self.has_picture = has_picture
        self.on_log = on_log
        self.cards: List[MappedCard] = []
        self.finish_reason = FINISH_MAX_PAGES
        self.first_entry: Optional[str] = None
        self.entries = 0                     # how many of the list's entries were looked at: a card's place
        self.failures = 0
        self.pointed_out: Set[str] = set()

    def _log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    # ------------------------------------------------------------------- run
    def run(self) -> List[MappedCard]:
        try:
            self._walk()
        except GameLinkError as exc:
            self._log(f"The walk stopped: {exc}.")
            self.finish_reason = FINISH_NO_JOURNAL
        self._collect_names()
        self._picture_new_types()
        return self.cards

    def _walk(self) -> None:
        options = self.options
        today = datetime.now().date()
        cursor = None
        for _request in range(options.max_pages):
            cancellation.check()
            page = self.feed.journal_page(cursor)
            if not page:
                self.finish_reason = FINISH_END
                return
            self.collector.feed_tree({"stream": tbcodec.expand_blobs(page)}, time.time())
            self.first_entry = self.first_entry or page[0][0]
            for raw in page:
                if options.stop_before is not None and raw[1] < options.stop_before:
                    self.finish_reason = FINISH_CUTOFF
                    self._log("Reached entries older than the last complete search. End.")
                    return
                if (options.max_age_days is not None
                        and (today - datetime.fromtimestamp(raw[1]).date()).days > options.max_age_days):
                    self.finish_reason = FINISH_DAYS
                    self._log(f"Reached entries more than {options.max_age_days} day(s) old. End.")
                    return
                self._take_entry(raw)
            cursor = page[-1][2]
            cancellation.sleep(options.interval)

    def _take_entry(self, raw: list) -> None:
        position = self.entries
        self.entries += 1
        entry = journal.parse_journal_entry(tbcodec.expand_blobs(raw))
        is_card = entry is not None and entry.has_details
        if entry is None and LOOKS_LIKE_A_RANKING.search(str(raw[3])) and raw[3] not in self.pointed_out:
            self.pointed_out.add(raw[3])
            self._log(f"A kind of entry nothing reads yet: {raw[3]}")
        if not is_card:
            return

        per_page = self.options.cards_per_page
        card = MappedCard(name="", page=position // per_page + 1, card=position % per_page + 1,
                          result_uid=entry.result_uid, tournament_key=entry.tournament_key,
                          ended_at=entry.ended_at, ranking=entry.ranking)
        self.cards.append(card)
        cancellation.sleep(self.options.interval)
        try:
            answer = self.feed.ranking(raw[0], entry.result_uid)
        except GameLinkError as exc:
            self.failures += 1
            self._log(f"  The ranking of {entry.tournament_key} ({entry.ended_at_text}) did not come: {exc}.")
            if self.failures >= MAX_FAILURES:
                raise
            return
        self.failures = 0
        self.collector.feed_tree({"stream": tbcodec.expand_blobs(answer)}, time.time())

        found = self.collector.rankings.get(entry.result_uid)
        if found is not None:
            card.status = STATUS_MAPPED
            card.tournament_key = found[0] or entry.tournament_key
            card.ranking = found[3]
            card.name = self.known_name(journal.Tournament(entry.result_uid, card.tournament_key,
                                                           ranking=card.ranking)) or ""
        elif entry.result_uid in self.collector.unknown_details:
            kind, sample = self.collector.unknown_details[entry.result_uid]
            card.status = f"{STATUS_UNKNOWN_FORMAT}: {kind}"
            self._log(f"  The game answered a ranking in a format not read yet: {kind} · {sample}")

    @staticmethod
    def _key(card: MappedCard) -> str:
        return catalogue_key(card.game_type or 0, card.ranking)

    # ----------------------------------------------------------------- names
    def _collect_names(self) -> None:
        """Asks the game for every player of these rankings whose name is not known yet."""
        missing: List[int] = []
        for card in self.cards:
            if card.status == STATUS_MAPPED:
                missing += [p for p in self.collector.missing_profiles(card.result_uid) if p not in missing]
        if missing:
            self._log(f"{len(missing)} player(s) without a name: asking the game.")
            try:
                self.feed.fetch_profiles(missing, self.collector)
            except GameLinkError as exc:
                self._log(f"Could not ask for the players' names: {exc}.")
        for card in self.cards:
            if card.status == STATUS_MAPPED:
                card.unnamed = len(self.collector.missing_profiles(card.result_uid))

    def _picture_new_types(self) -> None:
        """One new type at a time: the page with its first card goes to the site, for a person to name the type."""
        waiting: Dict[str, List[MappedCard]] = {}
        for card in self.cards:
            if card.status == STATUS_MAPPED and not card.name:
                waiting.setdefault(self._key(card), []).append(card)
        for key, cards in waiting.items():
            cancellation.check()
            for card in cards:
                card.status = STATUS_NO_NAME
            first = cards[0]
            first.for_catalogue = True
            if self.has_picture(journal.Tournament(first.result_uid or "", first.tournament_key, ranking=first.ranking)):
                self._log(f"Tournament {key} is waiting for a name on the site: {len(cards)} ranking(s) wait with it.")
                continue
            self._log(f"Tournament {key} is new to the site: picturing page {first.page} of the Journal.")
            for _attempt in range(PICTURE_ATTEMPTS):
                picture = self._picture(first)
                if picture:
                    first.picture_png, first.icon_png = picture
                    self._log(f"  Name it on the site: {len(cards)} ranking(s) wait for it.")
                    break
            else:
                self._log(f"  Tournament {key} could not be pictured: {len(cards)} ranking(s) wait for the next search.")

    def _places_hold(self) -> bool:
        """Whether the list still starts where it did, so a card's place still says where it is."""
        try:
            if self.feed.journal_page(None, 1)[0][0] == self.first_entry:
                return True
        except (GameLinkError, IndexError):
            return False
        self._log("  A new entry arrived: the places in the list have moved.")
        return False

    def _picture(self, card: MappedCard):
        """(page, icon) with the card on them, when the list did not move meanwhile; None otherwise."""
        if not self._places_hold():
            return None
        picture = self.take_picture(card.page - 1, card.card - 1)
        # Turning hundreds of pages takes minutes: something may have arrived meanwhile.
        return picture if picture and self._places_hold() else None


def catalogue_key(game_type: int, ranking: str) -> str:
    """How a catalogue entry is told apart: a type can have several rankings."""
    return f"{game_type}:{ranking}" if ranking else str(game_type)


def catalogue_entries(cards: List[MappedCard]) -> List[dict]:
    """One entry per new type and ranking, nameless and with its picture, ready for POST /api/v1/tournament-catalog."""
    entries = []
    for card in cards:
        if not card.for_catalogue or card.status != STATUS_NO_NAME or card.game_type is None:
            continue
        entry = {"tournament_key": card.tournament_key, "ranking": card.ranking}
        if card.ended_at:
            entry["ended_at"] = journal.Tournament(card.result_uid or "", ended_at=card.ended_at).ended_at_iso
        if card.icon_png:
            entry["image"] = base64.b64encode(card.icon_png).decode("ascii")
        if card.picture_png:
            entry["picture"] = base64.b64encode(card.picture_png).decode("ascii")
        entries.append(entry)
    return entries
