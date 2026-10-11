"""Reads the clan's members out of the game's traffic.

What the game sends (confirmed on a real recording, 11/10/2026):

- The clan (HTTP, route 15000), when the clan window is opened:
  `[[clan_id], [tag, name, ...], [[[player_id], rank, timestamp], ...], ...]`.
  The third item is the membership: one row per member, with the rank as a
  number (1 Leader, 2 Superior, 3 Officer, 4 Veteran, 5 Soldier). The game asks
  for the same record again every few seconds while the window is open, and
  those answers come with the membership empty: only one that has rows counts.
- The members' profiles (HTTP, route 402), when the Members menu is clicked:
  rows `[[player_id], account, name, country, ..., level at [7], ..., power at [10],
  [clan_id] at [11], ..., clan tag at [13], ..., [kingdom, x, y] at [15], ...]`.
  The whole clan came in one answer on the recording (95 members), before the
  list was scrolled.

The membership says who the members are and the profiles say what is known of
each, so a roster is complete when every id of the first has a row in the
second - which is how a list that arrived short is told from a whole one.

Nothing here knows about the network or the window, so it can be tested with
hand-built packets and fed from a live capture or a saved recording alike.
"""
from __future__ import annotations

import base64
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..events import tbcodec

CLAN_ROUTE = 15000
PROFILES_ROUTE = 402
RANKS = {1: "Leader", 2: "Superior", 3: "Officer", 4: "Veteran", 5: "Soldier"}


@dataclass(frozen=True)
class Member:
    player_id: int
    name: str
    power: Optional[int] = None
    rank: Optional[int] = None
    level: Optional[int] = None
    coordinates: Optional[Tuple[int, int, int]] = None   # kingdom, x, y
    clan_id: Optional[int] = None

    @property
    def rank_name(self) -> str:
        return RANKS.get(self.rank, "") if self.rank is not None else ""


@dataclass
class Clan:
    clan_id: int
    tag: str
    name: str
    ranks: Dict[int, int] = field(default_factory=dict)   # player id -> rank, in the game's order


@dataclass
class Roster:
    clan: Optional[Clan] = None
    members: List[Member] = field(default_factory=list)
    missing: List[int] = field(default_factory=list)      # members the game sent no profile for

    @property
    def expected(self) -> int:
        return len(self.clan.ranks) if self.clan else 0

    @property
    def complete(self) -> bool:
        return self.clan is not None and not self.missing


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _id(value: Any) -> Optional[int]:
    """`[id]` -> id."""
    if isinstance(value, list) and value and _is_int(value[0]):
        return value[0]
    return None


def _iter_lists(node: Any) -> Iterator[list]:
    """Every list in a tree, parents before children."""
    stack: List[Tuple[Any, int]] = [(node, 0)]
    while stack:
        current, level = stack.pop()
        if level > 60 or not isinstance(current, (list, dict)):
            continue
        if isinstance(current, list):
            yield current
        children = current.values() if isinstance(current, dict) else current
        stack.extend((child, level + 1) for child in children)


def parse_profile_row(row: Any) -> Optional[Member]:
    if not (isinstance(row, list) and len(row) > 11):
        return None
    player_id = _id(row[0])
    if player_id is None or not isinstance(row[2], str) or not row[2].strip():
        return None
    place = row[15] if len(row) > 15 else None
    coordinates = tuple(place) if isinstance(place, list) and len(place) == 3 and all(map(_is_int, place)) else None
    return Member(
        player_id=player_id,
        name=row[2],
        power=row[10] if _is_int(row[10]) else None,
        level=row[7] if _is_int(row[7]) else None,
        coordinates=coordinates,
        clan_id=_id(row[11]),
    )


def parse_clan_record(node: Any) -> Optional[Clan]:
    """A clan record that carries its membership; None for anything else, an empty membership included."""
    if not (isinstance(node, list) and len(node) >= 3):
        return None
    clan_id, info, rows = _id(node[0]), node[1], node[2]
    if clan_id is None or not (isinstance(info, list) and len(info) >= 2
                               and isinstance(info[0], str) and isinstance(info[1], str)):
        return None
    if not (isinstance(rows, list) and rows):
        return None
    ranks: Dict[int, int] = {}
    for row in rows:
        player_id = _id(row[0]) if isinstance(row, list) and len(row) >= 2 else None
        if player_id is None or not _is_int(row[1]):
            return None
        ranks[player_id] = row[1]
    return Clan(clan_id=clan_id, tag=info[0], name=info[1], ranks=ranks)


class RosterCollector:
    """Accumulates what the traffic reveals and assembles the clan's roster."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.clan: Optional[Clan] = None
        self.profiles: Dict[int, Member] = {}

    # ----------------------------------------------------------------- input
    def feed_record(self, record: dict) -> bool:
        """Take one record from traffic.TrafficListener. True when something was learned."""
        routes = record.get("routes") or []
        if record.get("kind") != "RESP" or not (CLAN_ROUTE in routes or PROFILES_ROUTE in routes):
            return False
        try:
            tree = tbcodec.decode_payload(base64.b64decode(record.get("b64", "")))
        except Exception:
            return False
        return self.feed_tree(tree)

    def feed_tree(self, tree: Any) -> bool:
        if not isinstance(tree, dict):
            return False
        learned = False
        with self.lock:
            for frame in tree.get("frames") or []:
                if frame.get("route") == PROFILES_ROUTE:
                    learned |= self._take_profiles(frame.get("objects"))
                elif frame.get("route") == CLAN_ROUTE:
                    learned |= self._take_clan(frame.get("objects"))
        return learned

    def _take_profiles(self, objects: Any) -> bool:
        learned = False
        for node in _iter_lists(objects):
            for row in node:
                member = parse_profile_row(row)
                if member is not None:
                    self.profiles[member.player_id] = member
                    learned = True
        return learned

    def _take_clan(self, objects: Any) -> bool:
        for node in _iter_lists(objects):
            clan = parse_clan_record(node)
            if clan is not None:
                self.clan = clan
                return True
        return False

    # ---------------------------------------------------------------- output
    def roster(self) -> Roster:
        """The members as the game lists them: by rank, the strongest first."""
        with self.lock:
            if self.clan is None:
                return Roster()
            members = [replace(self.profiles[player_id], rank=rank)
                       for player_id, rank in self.clan.ranks.items() if player_id in self.profiles]
            missing = [player_id for player_id in self.clan.ranks if player_id not in self.profiles]
            members.sort(key=lambda m: (m.rank, -(m.power or 0), m.name.lower()))
            return Roster(self.clan, members, missing)

    def clear(self) -> None:
        with self.lock:
            self.clan = None
            self.profiles.clear()
