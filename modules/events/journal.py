"""Reads tournament results out of the game's traffic.

What the game sends (confirmed on a real recording, 12/09/2026):

- Journal list (WebSocket): one entry per message, among them
  `[uuid, ended_at, n, "global_tournament_user_result", [{blob: [[kind, null, [type, variant], has_details]]}], ..., result_uid]`.
  `ended_at` is when the tournament closed (UTC seconds), `[type, variant]` is
  the tournament type and `result_uid` points at the detail message.
- Detail (WebSocket, when "Show details" is opened):
  `{result_uid: [{blob: [["global_tournament_user_result", [type, variant], n, [[1, 1, [[[player_id], points], ...], [], []]]]]}]}`.
  The ranking is already in order and includes players with 0 points.
- Other rankings (the Dark Omens, type 1033, and the Trials of Olympus): one
  tournament can have several, each with its own goal and points, all with
  the same `[type, variant]`. The Journal entry's kind names the ranking:
  `clan_members_item_gain_tracking_final_tracker_statistic_entry` carries a
  statistic (`[kind, [type, variant], "omens_damage"]`) and its detail a list
  of `[[player_id], points]`; `clan_points_mining_tournament_clan_statistics_entry`
  and `multistage_tournament_clan_statistics_entry` have a detail
  `{"1": [[player_id], points], "2": ...}` keyed by position. The rows are
  looked for in whatever shape they come, so a ranking of a kind listed here
  is read even if the game moves them around. The site keeps one catalogue
  entry per type and ranking.
  A detail of a kind nobody has taught this yet is remembered in
  `unknown_details`, with a sample of its shape, so the mapper can say what
  arrived instead of just "no answer".
- Player profiles (HTTP, route 402): rows `[[player_id], account, name, country, ..., power at [10], ..., clan tag at [13], ...]`.
  The ranking carries no names: they come from here, joined by player id.
  The game asks for them a few at a time, as the ranking list is scrolled,
  and only for players it has not fetched since the page was loaded. So every
  profile seen is kept on disk (ProfileCache) and a ranking reopened later
  still gets its names.

The game never sends the tournament's name. The site remembers it per type.

Nothing here knows about the network or the window, so it can be tested with
hand-built packets and fed from a live capture or a saved recording alike.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import tbcodec

USER_RESULT = "global_tournament_user_result"
ITEM_GAIN_TRACKER = "clan_members_item_gain_tracking_final_tracker_statistic_entry"
POINTS_MINING = "clan_points_mining_tournament_clan_statistics_entry"
MULTISTAGE = "multistage_tournament_clan_statistics_entry"
RANKING_KINDS = (USER_RESULT, ITEM_GAIN_TRACKER, POINTS_MINING, MULTISTAGE)
SAMPLE_CHARS = 400
PROFILES_ROUTE = 402
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
UNKNOWN_NAME_PREFIX = "id:"


@dataclass
class Profile:
    player_id: int
    name: str
    power: Optional[int] = None
    clan_tag: Optional[str] = None


@dataclass
class RankingRow:
    position: int
    player_id: int
    points: int
    name: Optional[str] = None
    power: Optional[int] = None

    @property
    def display_name(self) -> str:
        return self.name if self.name else f"{UNKNOWN_NAME_PREFIX}{self.player_id}"


@dataclass
class Tournament:
    result_uid: str
    tournament_key: Optional[str] = None
    ended_at: Optional[int] = None
    has_details: bool = True
    rows: List[RankingRow] = field(default_factory=list)
    seen_at: float = 0.0
    ranking: str = ""   # which ranking of the tournament; "" for the classic result

    @property
    def has_ranking(self) -> bool:
        return bool(self.rows)

    @property
    def ended_at_iso(self) -> Optional[str]:
        if self.ended_at is None:
            return None
        return datetime.fromtimestamp(self.ended_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def ended_at_text(self) -> str:
        if self.ended_at is None:
            return ""
        return datetime.fromtimestamp(self.ended_at, timezone.utc).strftime("%d/%m/%Y %H:%M")

    @property
    def unnamed(self) -> int:
        return sum(1 for r in self.rows if not r.name)


def _iter_nodes(node: Any, depth: int = 0) -> Iterator[Any]:
    """Every list and dict in a tree, parents before children."""
    stack: List[Tuple[Any, int]] = [(node, depth)]
    while stack:
        current, level = stack.pop()
        if level > 60:
            continue
        if isinstance(current, (list, dict)):
            yield current
            children = current.values() if isinstance(current, dict) else current
            for child in children:
                if isinstance(child, (list, dict)):
                    stack.append((child, level + 1))


def _blob_messages(node: Any) -> List[Any]:
    """The messages inside a `{"blob": [...]}` wrapper, or [] when it is not one."""
    if isinstance(node, dict) and set(node.keys()) == {"blob"} and isinstance(node["blob"], list):
        return node["blob"]
    return []


def _type_key(value: Any) -> Optional[str]:
    if isinstance(value, list) and value and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        return ":".join(str(v) for v in value[:3])
    return None


def _first_blob(node: Any) -> Optional[list]:
    """The first message inside the first blob of a list, if any."""
    if not isinstance(node, list):
        return None
    for item in node:
        for message in _blob_messages(item):
            if isinstance(message, list):
                return message
    return None


def ranking_name(message: Any) -> Optional[str]:
    """Which ranking a Journal message is: "" for the classic result, None when it is not a ranking."""
    if not (isinstance(message, list) and message and message[0] in RANKING_KINDS):
        return None
    if message[0] == USER_RESULT:
        return ""
    # A kind can cover several rankings, told apart by a statistic of its own
    # ("omens_damage"), which the game puts right after the type.
    statistic = message[2] if len(message) > 2 and isinstance(message[2], str) and message[2].strip() else ""
    return f"{message[0]}:{statistic}" if statistic else message[0]


def ranking_label(ranking: str) -> str:
    """Short name of a ranking for the screen: "omens_damage", "multistage_tournament_clan_statistics"."""
    if not ranking:
        return ""
    statistic = ranking.split(":", 1)[1] if ":" in ranking else ""

    return statistic or re.sub(r"_entry$", "", ranking)


def sample_of(message: Any) -> str:
    """A short, readable sketch of a message, to report a shape nobody reads yet."""
    text = json.dumps(message, default=str, ensure_ascii=False)

    return text if len(text) <= SAMPLE_CHARS else text[:SAMPLE_CHARS] + "..."


def parse_journal_entry(entry: Any) -> Optional[Tournament]:
    """A Journal list entry for a tournament ranking."""
    if not (isinstance(entry, list) and len(entry) >= 6):
        return None
    if entry[3] not in RANKING_KINDS or not isinstance(entry[1], int) or not isinstance(entry[0], str):
        return None
    result_uid = entry[-1]
    if not (isinstance(result_uid, str) and UUID_RE.match(result_uid)):
        return None
    message = _first_blob(entry[4]) if len(entry) > 4 else None
    key = None
    has_details = True
    ranking = ranking_name(message)
    if ranking is None:
        ranking = "" if entry[3] == USER_RESULT else entry[3]
    elif message[0] == USER_RESULT:
        key = next((k for k in (_type_key(v) for v in message[1:3]) if k), None)
        if len(message) > 3 and isinstance(message[3], int):
            has_details = message[3] != 0
    else:
        key = _type_key(message[1]) if len(message) > 1 else None
    return Tournament(result_uid=result_uid, tournament_key=key, ended_at=entry[1], has_details=has_details, ranking=ranking)


def _pair(row: Any) -> Optional[Tuple[int, int]]:
    """`[[player_id], points]` -> (player_id, points)."""
    if (
        isinstance(row, list) and len(row) == 2
        and isinstance(row[0], list) and row[0] and isinstance(row[0][0], int)
        and isinstance(row[1], int) and not isinstance(row[1], bool)
    ):
        return row[0][0], row[1]
    return None


def _pairs(node: Any) -> List[Tuple[int, int]]:
    """A ranking as a list of rows, or as rows keyed by position ("1", "2", ...); [] otherwise."""
    if isinstance(node, list) and node:
        pairs = [_pair(row) for row in node]
        return [] if None in pairs else pairs
    if isinstance(node, dict) and node and all(isinstance(k, str) and k.isdigit() for k in node):
        by_position = sorted(node.items(), key=lambda item: int(item[0]))
        pairs = [_pair(row) for _, row in by_position]
        return [] if None in pairs else pairs
    return []


def parse_ranking_message(message: Any) -> Optional[Tuple[Optional[str], List[Tuple[int, int]], str]]:
    """A ranking detail -> (key, [(player_id, points)], ranking).

    `[user_result, [type, variant], n, [[1, 1, rows, ...]]]`,
    `[item_gain_tracker, [type, variant], statistic, [rows]]` or
    `[points_mining, [type, variant], {position: row}]`.
    """
    ranking = ranking_name(message)
    if ranking is None or len(message) < 3:
        return None
    key = _type_key(message[1])
    for node in _iter_nodes(message[2:]):
        pairs = _pairs(node)
        if pairs:
            return key, pairs, ranking
    return None


def parse_profile_row(row: Any) -> Optional[Profile]:
    if not (isinstance(row, list) and len(row) > 10):
        return None
    if not (isinstance(row[0], list) and row[0] and isinstance(row[0][0], int)):
        return None
    if not isinstance(row[2], str) or not row[2].strip():
        return None
    power = row[10] if isinstance(row[10], int) and not isinstance(row[10], bool) else None
    tag = row[13] if len(row) > 13 and isinstance(row[13], str) else None
    return Profile(player_id=row[0][0], name=row[2], power=power, clan_tag=tag)


class ProfileCache:
    """Every player profile ever seen, in a JSON file: player id -> name, power, clan tag."""

    def __init__(self, path: str):
        self.path = path

    def load(self) -> Dict[int, Profile]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        out: Dict[int, Profile] = {}
        for key, value in (data.get("profiles") or {}).items() if isinstance(data, dict) else []:
            try:
                player_id = int(key)
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("name"), str) and value["name"].strip():
                power = value.get("power")
                out[player_id] = Profile(player_id, value["name"], power if isinstance(power, int) else None, value.get("clan_tag"))
        return out

    def save(self, profiles: Dict[int, Profile]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        data = {"profiles": {str(p.player_id): {"name": p.name, "power": p.power, "clan_tag": p.clan_tag}
                             for p in profiles.values()}}
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(temporary, self.path)


class JournalCollector:
    """Accumulates what the traffic reveals and assembles whole tournaments."""

    def __init__(self, cache: Optional[ProfileCache] = None) -> None:
        self.lock = threading.Lock()
        self.entries: Dict[str, Tournament] = {}
        # result uid -> (key, [(player_id, points)], seen_at, ranking)
        self.rankings: Dict[str, Tuple[Optional[str], List[Tuple[int, int]], float, str]] = {}
        # result uid -> (kind, sample), for details the game sent in a shape not read here yet
        self.unknown_details: Dict[str, Tuple[str, str]] = {}
        self.profiles: Dict[int, Profile] = cache.load() if cache is not None else {}
        self.cache = cache
        self._saved_profiles = dict(self.profiles)
        self.packets = 0

    # ----------------------------------------------------------------- input
    def feed_record(self, record: dict) -> bool:
        """Take one record from traffic.TrafficListener. True when something was learned."""
        kind = record.get("kind")
        routes = record.get("routes") or []
        # Map chunks and the like are large and never hold what is needed here.
        if kind == "RESP" and PROFILES_ROUTE not in routes:
            return False
        try:
            tree = tbcodec.decode_payload(base64.b64decode(record.get("b64", "")))
        except Exception:
            return False
        return self.feed_tree(tree, record.get("ts", 0.0))

    def feed_tree(self, tree: Any, seen_at: float = 0.0) -> bool:
        if not isinstance(tree, dict):
            return False
        learned = False
        with self.lock:
            self.packets += 1
            if "frames" in tree:
                for frame in tree["frames"]:
                    if frame.get("route") == PROFILES_ROUTE:
                        learned |= self._take_profiles(frame.get("objects"))
            if "stream" in tree:
                learned |= self._take_stream(tree["stream"], seen_at)
        return learned

    def _take_profiles(self, objects: Any) -> bool:
        learned = False
        for node in _iter_nodes(objects):
            if not isinstance(node, list):
                continue
            for row in node:
                profile = parse_profile_row(row)
                if profile is not None:
                    self.profiles[profile.player_id] = profile
                    learned = True
        return learned

    def _take_stream(self, stream: Any, seen_at: float) -> bool:
        learned = False
        for node in _iter_nodes(stream):
            if isinstance(node, list):
                entry = parse_journal_entry(node)
                if entry is not None:
                    entry.seen_at = seen_at
                    self.entries[entry.result_uid] = entry
                    learned = True
            elif isinstance(node, dict):
                for key, value in node.items():
                    if not (isinstance(key, str) and UUID_RE.match(key)):
                        continue
                    message = _first_blob(value)
                    parsed = parse_ranking_message(message)
                    if parsed is not None:
                        self.rankings[key] = (parsed[0], parsed[1], seen_at, parsed[2])
                        learned = True
                    elif isinstance(message, list) and message and isinstance(message[0], str):
                        # An answer in a shape nobody has taught this yet: worth saying so,
                        # with a sketch of it, which is enough to learn the shape from.
                        self.unknown_details[key] = (message[0], sample_of(message))
                        learned = True
        return learned

    # ---------------------------------------------------------------- output
    def tournaments(self) -> List[Tournament]:
        """Every tournament with a ranking, plus Journal entries still to be opened."""
        with self.lock:
            out: Dict[str, Tournament] = {}
            for uid, entry in self.entries.items():
                if entry.has_details:
                    out[uid] = Tournament(uid, entry.tournament_key, entry.ended_at, True, [], entry.seen_at, entry.ranking)
            for uid, (key, pairs, seen_at, ranking) in self.rankings.items():
                tournament = out.get(uid) or Tournament(uid, key, None, True, [], seen_at, ranking)
                tournament.tournament_key = tournament.tournament_key or key
                tournament.ranking = ranking
                tournament.seen_at = max(tournament.seen_at, seen_at)
                tournament.rows = [
                    RankingRow(
                        position=index + 1,
                        player_id=player_id,
                        points=points,
                        name=self.profiles[player_id].name if player_id in self.profiles else None,
                        power=self.profiles[player_id].power if player_id in self.profiles else None,
                    )
                    for index, (player_id, points) in enumerate(pairs)
                ]
                out[uid] = tournament
        return sorted(out.values(), key=lambda t: (not t.has_ranking, -(t.ended_at or t.seen_at or 0)))

    def missing_profiles(self, result_uid: str) -> List[int]:
        """Players of a ranking whose name is still unknown, in ranking order."""
        with self.lock:
            ranking = self.rankings.get(result_uid)
            if ranking is None:
                return []
            return [player_id for player_id, _ in ranking[1] if player_id not in self.profiles]

    def save_profiles(self) -> bool:
        """Write the profiles to the cache when any changed. True when written."""
        if self.cache is None:
            return False
        with self.lock:
            if self.profiles == self._saved_profiles:
                return False
            snapshot = dict(self.profiles)
        self.cache.save(snapshot)
        self._saved_profiles = snapshot
        return True

    def clear(self) -> None:
        """Forget the tournaments. Profiles stay: a player's name does not depend on the tournament."""
        with self.lock:
            self.entries.clear()
            self.rankings.clear()
            self.unknown_details.clear()
            self.packets = 0


def checks(tournament: Tournament) -> List[Tuple[bool, str]]:
    """What to look at before sending."""
    rows = tournament.rows
    out: List[Tuple[bool, str]] = []
    out.append((0 < len(rows) <= 100, f"{len(rows)} players in the ranking (expected: 1 to 100)"))
    decreasing = all(a.points >= b.points for a, b in zip(rows, rows[1:]))
    out.append((decreasing, "points in decreasing order"))
    ids = [r.player_id for r in rows]
    out.append((len(set(ids)) == len(ids), "no repeated players"))
    missing = tournament.unnamed
    out.append((missing == 0, "every player has a name" if missing == 0 else
                f"{missing} player(s) without a name: the game did not send their profiles"))
    out.append((tournament.ended_at is not None, "end date known" if tournament.ended_at is not None else
                "end date unknown: the Journal list was not seen in this session"))
    out.append((tournament.tournament_key is not None, f"tournament type: {tournament.tournament_key or 'unknown'}"))
    return out


def build_payload(tournament: Tournament, name: str, ended_at_iso: Optional[str], client_version: str,
                  auto_publish: bool = False) -> dict:
    """The body of POST /api/v1/tournaments.

    `auto_publish` asks the site to publish (close) the tournament right away.
    It is only asked for when every player has a name; the site checks again
    and leaves the ranking as a draft for review otherwise.
    """
    return {
        "result_uid": tournament.result_uid,
        "tournament_key": tournament.tournament_key or "0",
        "ranking": tournament.ranking,
        "name": name.strip() or None,
        "ended_at": ended_at_iso,
        "capture_method": "packet",
        "client_version": client_version,
        "auto_publish": bool(auto_publish) and tournament.unnamed == 0,
        "rows": [
            {
                "position": row.position,
                "name": row.display_name[:60],
                "points": row.points,
                "player_id": row.player_id,
                "power": row.power,
            }
            for row in tournament.rows
        ],
    }
