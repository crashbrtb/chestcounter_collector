"""Reads the clan's gifts out of the game's traffic.

What the game sends (confirmed on a real recording, 11/10/2026, 859 chests):

- The list (HTTP, route 15000), when the clan window is opened. The clan record
  is `[[clan_id], [tag, name, ...], [members], ..., gifts at [13], ...]` and each
  gift `[gift_id, [player_id], chest_type, expires_at, quantity, 1]`. The game
  asks for the same record again every few seconds while the window is open, and
  those answers come with the members and the gifts empty: only one that has
  members counts, and such a one with no gifts says there are none.
- A gift generated while the game is open (any HTTP answer): an event on the
  tail of whatever is sent next, `[session, seq, time, 300020]` followed by
  `[[gift row]]`.
- Opening a chest (HTTP, route 15013): one answer per chest opened, carrying what
  was inside it. It does not say which gift it was.

A gift is one kill, one crypt, one purchase; `quantity` is how many chests it
put in the panel - an epic monster drops several at once - and the panel lists
one line for each. Gift ids are consecutive within the clan, and the panel
lists the gifts oldest first, which is the order of the ids.

A gift expires twenty hours after it was generated, to the second, so
`expires_at` dates it better than the countdown the panel prints in minutes.

The game sends neither the chest's name nor the player's: the type is a number
(`game_chest_types` in the database names it) and so is the player
(`members.game_player_id`).

Nothing here knows about the network or the window, so it can be tested with
hand-built packets and fed from a live capture or a saved recording alike.
"""
from __future__ import annotations

import base64
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..events import tbcodec

CLAN_ROUTE = 15000
OPEN_ROUTE = 15013
GIFT_EVENT = 300020
GIFTS_FIELD = 13
GIFT_LIFETIME_SECONDS = 20 * 60 * 60

# The row's last field. Every gift seen so far has 1 there; one that has
# something else is of a kind nobody has looked at yet (the Triumphal Gifts tab
# was empty on the recordings) and is left to the reading of the screen.
PLAIN_GIFT = 1


@dataclass(frozen=True)
class Gift:
    gift_id: int
    player_id: int
    chest_type: int
    expires_at: int
    quantity: int = 1
    kind: int = PLAIN_GIFT

    @property
    def generated_at(self) -> datetime:
        """When the gift was generated, in this computer's time - what `collected_at` holds."""
        return datetime.fromtimestamp(self.expires_at - GIFT_LIFETIME_SECONDS)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_gift_row(row: Any) -> Optional[Gift]:
    if not (isinstance(row, list) and len(row) == 6):
        return None
    player = row[1]
    if not (isinstance(player, list) and len(player) == 1 and _is_int(player[0])):
        return None
    if not all(_is_int(value) for value in (row[0], *row[2:])):
        return None
    return Gift(gift_id=row[0], player_id=player[0], chest_type=row[2], expires_at=row[3],
                quantity=max(row[4], 1), kind=row[5])


def parse_clan_gifts(node: Any) -> Optional[List[Gift]]:
    """
    The gifts of a clan record that carries them; None for anything else.

    The record the game repeats while the window is open has no members and no
    gifts, and must not be taken for 'there are no gifts': a record counts only
    when its membership has rows. One that does, with an empty list, is the game
    saying that nothing is waiting.
    """
    if not (isinstance(node, list) and len(node) > GIFTS_FIELD):
        return None
    clan, info, members, rows = node[0], node[1], node[2], node[GIFTS_FIELD]
    if not (isinstance(clan, list) and len(clan) == 1 and _is_int(clan[0])):
        return None
    if not (isinstance(info, list) and len(info) >= 2 and isinstance(info[0], str)):
        return None
    if not (isinstance(members, list) and members and isinstance(rows, list)):
        return None
    gifts = [parse_gift_row(row) for row in rows]
    if None in gifts:
        return None
    return sorted(gifts, key=lambda gift: gift.gift_id)


def _clan_gifts(objects: Any) -> Optional[List[Gift]]:
    stack = [(objects, 0)]
    while stack:
        node, level = stack.pop()
        if not isinstance(node, list) or level > 6:
            continue
        gifts = parse_clan_gifts(node)
        if gifts is not None:
            return gifts
        stack.extend((child, level + 1) for child in node)
    return None


def _event_gifts(tail: bytes) -> List[Gift]:
    gifts: List[Gift] = []
    for event in tbcodec.tail_events(tail):
        header = event[0]
        if not (isinstance(header, list) and len(header) == 4 and header[3] == GIFT_EVENT):
            continue
        for payload in event[1:]:
            for row in payload if isinstance(payload, list) else []:
                gift = parse_gift_row(row)
                if gift is not None:
                    gifts.append(gift)
    return gifts


class GiftFeed:
    """
    What the traffic says about the gifts: which are waiting, and how many chests were opened.

    `lists` counts the lists received, so whoever opened the clan window can wait
    for the one that answers it. A list replaces everything known before it and
    sets `opened` back to zero: from then on the gifts are that list plus the
    ones announced after it, and `opened` the chests opened since.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.gifts: Dict[int, Gift] = {}
        self.lists = 0
        self.opened = 0

    # ----------------------------------------------------------------- input
    def feed_record(self, record: dict) -> bool:
        """Take one record from traffic.TrafficListener. True when something was learned."""
        if record.get("kind") != "RESP":
            return False
        try:
            data = tbcodec._decompress(base64.b64decode(record.get("b64", "")))
            frames = list(tbcodec.iter_frames(data))
        except Exception:
            return False
        return self.feed_frames(frames)

    def feed_frames(self, frames) -> bool:
        learned = False
        with self.lock:
            for frame in frames:
                if frame.route == CLAN_ROUTE:
                    gifts = _clan_gifts(frame.objects)
                    if gifts is not None:
                        self.gifts = {gift.gift_id: gift for gift in gifts}
                        self.lists += 1
                        self.opened = 0
                        learned = True
                elif frame.route == OPEN_ROUTE and not frame.error:
                    self.opened += 1
                    learned = True
                # After the list of the same answer, so a gift announced with it is kept.
                for gift in _event_gifts(frame.tail) if frame.tail else []:
                    if gift.gift_id not in self.gifts:
                        self.gifts[gift.gift_id] = gift
                        learned = True
        return learned

    # ---------------------------------------------------------------- output
    def snapshot(self) -> List[Gift]:
        """Every gift known since the last list, oldest first - the order of the panel."""
        with self.lock:
            return sorted(self.gifts.values(), key=lambda gift: gift.gift_id)

    def state(self) -> tuple:
        with self.lock:
            return self.lists, self.opened


def chests_of(gifts: List[Gift]) -> List[Gift]:
    """One entry per chest, in the order of the panel: a gift of three chests is three lines."""
    return [gift for gift in gifts for _ in range(gift.quantity)]
