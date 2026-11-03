"""What the game says about the clan's gifts.

What it sends (confirmed on real recordings, 11/10/2026 and 01/11/2026):

- The list (route 15000). The clan record is
  `[[clan_id], [tag, name, ...], [members], ..., gifts at [13], ...]` and each
  gift `[gift_id, [player_id], chest_type, expires_at, quantity, 1]`. Only a
  record that has members counts, and such a one with no gifts says there are none.
- Opening a gift (route 15013): the answer carries what was inside it, added up,
  and does not say which gift it was.

A gift is one kill, one crypt, one purchase; `quantity` is how many chests it
put in the panel - an epic monster drops several at once - and the panel lists
one line for each. Gift ids are consecutive within the clan, and the panel
lists the gifts oldest first, which is the order of the ids.

A gift expires twenty hours after it was generated, to the second, so
`expires_at` dates it better than the countdown the panel prints in minutes.

The game sends neither the chest's name nor the player's: the type is a number
(`game_chest_types` in the database names it) and so is the player
(`members.game_player_id`).

Nothing here knows about the network, so it can be tested with hand-built packets.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional

CLAN_ROUTE = 15000
OPEN_ROUTE = 15013
GIFTS_FIELD = 13
GIFT_LIFETIME_SECONDS = 20 * 60 * 60

# The row's last field. Every gift seen so far has 1 there; one that has
# something else is of a kind nobody has looked at yet, and is left in the game
# (the Triumphal Gifts tab was empty on every recording).
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
