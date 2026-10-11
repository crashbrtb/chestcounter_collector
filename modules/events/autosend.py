"""Automatic search: walk the Journal, then send every new tournament to the site.

One search, for one profile and so for one clan site (module.py starts it):

1. asks the site how far back to look: the start of the last *complete*
   search, or, before the first one, the end of the newest tournament on the
   site; with a margin of overlap (CUTOFF_MARGIN), since looking at a
   tournament twice costs nothing but missing one loses it;
2. the mapper opens each "Your Clanmates' results in ..." card, newest first,
   presses "Show details" and scrolls the ranking until the players' names
   arrive, and stops once it reaches tournaments older than that point - or
   cards received more days ago than the search was told to look back
   (MapperOptions.max_age_days, read off the cards: "yesterday", "7 days ago");
3. the names read off the cards go to the site's catalogue;
4. asks the site which of the tournaments found it already has, and sends
   the rest. When every player has a name the site is asked to publish
   (close) it right away; otherwise it stays a draft for review;
5. tells the site how the search went. Only a complete one (the whole period
   looked at, every ranking read, every send accepted) moves the starting
   point of the next search forward; an interrupted one is simply redone.

Nothing is sent twice:
- the site keeps one event per game result id (a unique index) and answers a
  repeated upload with the same event; a ranking identical to the draft is not
  stored again, and one with different points is refused;
- before sending, the site is asked which results it has: a published one is
  never sent again;
- locally, a result whose ranking did not change since it was sent is skipped
  (signature()), so a draft is only sent again when it gained names.

Nothing here touches the window or the network directly, so the whole run can
be tested with the fake game of tests/test_events_mapper.py and a fake site.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from . import api, journal, mapper

CUTOFF_MARGIN = 2 * 3600  # seconds of overlap with the previous search

# What the site answers in `publish_blocked`, in words.
BLOCKED_REASONS = {
    "unnamed": "there are players without a name",
    "unlinked": "there are players not linked to members",
    "empty": "the ranking is empty",
    "not_draft": "the ranking is not a draft",
}

# The site knows a walk that stopped where it was told to as "cutoff", whatever
# told it: a moment in time or a number of days.
REPORTED_REASON = {mapper.FINISH_DAYS: mapper.FINISH_CUTOFF}

FINISH_TEXT = {
    mapper.FINISH_END: "end of the Journal",
    mapper.FINISH_CUTOFF: "reached the last complete search",
    mapper.FINISH_DAYS: "reached the limit of days to look back",
    mapper.FINISH_MAX_PAGES: "page limit",
    mapper.FINISH_NO_JOURNAL: "the Journal did not open",
}


@dataclass
class Outcome:
    result_uid: str
    name: str
    published: bool = False
    error: Optional[str] = None
    refused: bool = False   # the site said no for good (409): not an error to retry
    reply: Dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        label = f"#{self.reply.get('event_number')} {self.reply.get('event_name') or self.name}" if self.reply else self.name
        if self.error:
            return f"{label}: {self.error}"
        if self.published:
            return f"{label}: sent and closed on the site"
        reason = self.reply.get("publish_blocked")
        why = BLOCKED_REASONS.get(reason, reason) if reason else ""
        return f"{label}: sent for review" + (f" ({why})" if why else "")


@dataclass
class Summary:
    started_at: str = ""
    cutoff: Optional[str] = None
    reason: str = mapper.FINISH_MAX_PAGES
    complete: bool = False
    cards: List[mapper.MappedCard] = field(default_factory=list)
    catalogue: List[dict] = field(default_factory=list)
    outcomes: List[Outcome] = field(default_factory=list)
    skipped: int = 0
    conflicts: List[str] = field(default_factory=list)

    @property
    def opened(self) -> int:
        return sum(1 for c in self.cards if c.status == mapper.STATUS_MAPPED)

    @property
    def unanswered(self) -> int:
        return sum(1 for c in self.cards if c.status == mapper.STATUS_NO_ANSWER)

    @property
    def published(self) -> int:
        return sum(1 for o in self.outcomes if o.published)

    @property
    def for_review(self) -> int:
        return sum(1 for o in self.outcomes if not o.published and not o.error)

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.error and not o.refused)

    @property
    def text(self) -> str:
        out = (f"{self.opened} tournament(s) opened, {self.published} closed on the site, {self.for_review} for review, "
               f"{self.skipped} already on the site")
        if self.failed:
            out += f", {self.failed} with an error"
        out += f" · {FINISH_TEXT.get(self.reason, self.reason)}"
        out += " · complete search" if self.complete else " · incomplete search: the next one goes back to the same point"
        return out


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def cutoff_from(state: dict, margin: int = CUTOFF_MARGIN) -> Optional[int]:
    """How far back to look: the last complete search, else the newest tournament on the site."""
    moment = parse_iso(state.get("covered_until")) or parse_iso(state.get("latest_ended_at"))

    return int(moment) - margin if moment is not None else None


def signature(t: journal.Tournament) -> str:
    """What was sent: players, points and names. It changes only when names arrive."""
    rows = [[r.player_id, r.points, r.name or ""] for r in t.rows]

    return hashlib.sha1(json.dumps([t.result_uid, rows]).encode("utf-8")).hexdigest()


def should_send(t: journal.Tournament, local: Optional[dict], remote: Optional[dict]) -> Tuple[bool, str]:
    """(send it?, why not)."""
    if remote and remote.get("published"):
        return False, "already published on the site"
    if remote and remote.get("cancelled"):
        return False, "cancelled on the site"
    if local and local.get("conflict"):
        return False, f"refused before: {local['conflict']}"
    if local and local.get("signature") == signature(t):
        return False, "already sent, unchanged"
    return True, ""


def candidates(tournaments: List[journal.Tournament]) -> List[journal.Tournament]:
    """Tournaments with a ranking, oldest first, so the newest is the last one to
    set who is active in the clan."""
    found = [t for t in tournaments if t.has_ranking and t.tournament_key]

    return sorted(found, key=lambda t: t.ended_at or t.seen_at or 0)


def send_found(
    client: api.SiteClient,
    tournaments: List[journal.Tournament],
    name_for: Callable[[journal.Tournament], str],
    local_info: Callable[[str], Optional[dict]],
    mark_sent: Callable[[str, dict], None],
    client_version: str,
    auto_publish: bool = True,
    on_log: Optional[Callable[[str], None]] = None,
) -> Tuple[List[Outcome], int]:
    """Send every tournament the site does not have yet. (outcomes, how many were skipped)."""
    log = on_log or (lambda _text: None)
    found = candidates(tournaments)
    if not found:
        return [], 0

    try:
        remote = client.lookup([t.result_uid for t in found])
    except api.ApiError as exc:
        # An older site without the lookup: the local history and the site's
        # own refusal of duplicates still hold.
        log(f"Could not ask the site what it has ({exc.message}); going by the local history alone.")
        remote = {}

    outcomes: List[Outcome] = []
    skipped = 0
    for t in found:
        there = remote.get(t.result_uid)
        send, why = should_send(t, local_info(t.result_uid), there)
        if not send:
            skipped += 1
            if there and there.get("published") and not (local_info(t.result_uid) or {}).get("published"):
                mark_sent(t.result_uid, {"event_id": there.get("event_id"), "event_number": there.get("event_number"),
                                         "published": True, "signature": signature(t), "sent_at": _now()})
            continue

        name = name_for(t)
        # Without the end date the site would date it "now": that one is
        # better looked at before it is published.
        close = auto_publish and t.unnamed == 0 and t.ended_at is not None
        payload = journal.build_payload(t, name, t.ended_at_iso, client_version, auto_publish=close)
        outcome = Outcome(t.result_uid, name or t.tournament_key or t.result_uid[:8])
        try:
            reply = client.send_tournament(payload)
        except api.ApiError as exc:
            outcome.error = exc.message
            if exc.status == 409:
                # Published, cancelled, or points different from the game's:
                # sending it again would get the same answer.
                outcome.refused = True
                mark_sent(t.result_uid, {"conflict": exc.message, "signature": signature(t), "sent_at": _now()})
        else:
            outcome.reply = reply
            outcome.published = bool(reply.get("published"))
            mark_sent(t.result_uid, {
                "event_id": reply.get("event_id"),
                "event_number": reply.get("event_number"),
                "review_url": reply.get("review_url"),
                "published": outcome.published,
                "signature": signature(t),
                "sent_at": _now(),
            })
        log(outcome.text)
        outcomes.append(outcome)
    return outcomes, skipped


def trusted_cards(cards: List[mapper.MappedCard]) -> List[mapper.MappedCard]:
    """Mapped cards whose name is not in conflict (see mapper.conflicts())."""
    mapped = [c for c in cards if c.status == mapper.STATUS_MAPPED and c.game_type is not None]
    names_by_key: Dict[str, set] = {}
    keys_by_name: Dict[str, set] = {}
    for card in mapped:
        key = mapper.catalogue_key(card.game_type, card.ranking)
        names_by_key.setdefault(key, set()).add(mapper.normalize(card.name))
        keys_by_name.setdefault(mapper.normalize(card.name), set()).add(key)

    return [c for c in mapped
            if len(names_by_key[mapper.catalogue_key(c.game_type, c.ranking)]) == 1
            and len(keys_by_name[mapper.normalize(c.name)]) == 1]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AutoSearch:
    """One automatic search. `run()` blocks until the Journal is walked and the site told."""

    def __init__(
        self,
        browser,
        layout,
        ocr,
        collector: journal.JournalCollector,
        client: api.SiteClient,
        options: mapper.MapperOptions,
        local_info: Callable[[str], Optional[dict]],
        mark_sent: Callable[[str, dict], None],
        catalogue_name: Callable[[journal.Tournament], str],
        client_version: str,
        auto_publish: bool = True,
        on_log: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = lambda: datetime.now(timezone.utc).timestamp(),
        vision=None,
        use_site_cutoff: bool = True,
    ):
        self.browser = browser
        self.layout = layout
        self.ocr = ocr
        self.collector = collector
        self.client = client
        self.options = options
        self.local_info = local_info
        self.mark_sent = mark_sent
        self.catalogue_name = catalogue_name
        self.client_version = client_version
        self.auto_publish = auto_publish
        self.on_log = on_log
        self.clock = clock
        self.vision = vision
        # Off when the days to look back were raised: the site's last search
        # did not reach that far, so it is not where this one stops.
        self.use_site_cutoff = use_site_cutoff
        self.runner: Optional[mapper.JournalMapper] = None

    def _log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    def run(self) -> Summary:
        summary = Summary(started_at=iso(self.clock()))

        try:
            state = self.client.search_state()
        except api.ApiError as exc:
            self._log(f"Could not learn how far back to look ({exc.message}): the whole Journal will be walked.")
            state = {}
        cutoff = cutoff_from(state) if self.use_site_cutoff else None
        self.options.stop_before = cutoff
        summary.cutoff = iso(cutoff) if cutoff is not None else None
        days = self.options.max_age_days
        within = f" within the last {days} day(s)" if days is not None else ""
        if cutoff is not None:
            self._log(f"Looking for tournaments that ended since {summary.cutoff.replace('T', ' ')[:16]} UTC{within}.")
        elif days is not None:
            self._log(f"Walking the Journal back {days} day(s).")
        else:
            self._log("First search: walking the whole Journal.")

        # A run cancelled during the walk leaves through here as an exception:
        # nothing is sent and nothing is reported, so the next search goes back
        # to the same point.
        self.runner = mapper.JournalMapper(self.browser, self.layout, self.ocr, self.collector, self.options,
                                           on_log=self._log, vision=self.vision)
        summary.cards = self.runner.run()
        summary.reason = self.runner.finish_reason
        self._send(summary)

        summary.complete = (summary.reason in mapper.COMPLETE_REASONS and summary.unanswered == 0
                            and summary.failed == 0)
        if summary.unanswered:
            self._log(f"{summary.unanswered} tournament(s) without an answer from the game: the next search looks again.")
        self._report(summary)
        return summary

    def _send(self, summary: Summary) -> None:
        # A name read differently for one type is not trusted: the site keeps its own.
        summary.conflicts = mapper.conflicts(summary.cards)
        for text in summary.conflicts:
            self._log("CONFLICT: " + text)
        cards = trusted_cards(summary.cards)
        entries = mapper.catalogue_entries(cards)
        if entries:
            try:
                summary.catalogue = self.client.send_catalog(entries)
            except api.ApiError as exc:
                self._log(f"Catalogue not sent: {exc.message}")
        names = {c.result_uid: c.name for c in cards if c.result_uid}

        def name_for(t: journal.Tournament) -> str:
            return names.get(t.result_uid) or self.catalogue_name(t)

        summary.outcomes, summary.skipped = send_found(
            self.client, self.collector.tournaments(), name_for, self.local_info, self.mark_sent,
            self.client_version, self.auto_publish, self._log,
        )
        if not summary.outcomes:
            self._log("No new tournament to send.")

    def _report(self, summary: Summary) -> None:
        try:
            self.client.report_search({
                "started_at": summary.started_at,
                "complete": summary.complete,
                "reason": REPORTED_REASON.get(summary.reason, summary.reason),
                "cutoff": summary.cutoff,
                "cards": len(summary.cards),
                "sent": len(summary.outcomes) - summary.failed,
                "published": summary.published,
                "for_review": summary.for_review,
                "errors": summary.failed + summary.unanswered,
                "client_version": self.client_version,
            })
        except api.ApiError as exc:
            self._log(f"Could not record the search on the site ({exc.message}): the next one goes back to the same point.")
