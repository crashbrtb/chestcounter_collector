"""
Shield module - keeps the Shield of Peace up on the profiles it is turned on for.

It asks the game, over the network, when the shield of the profile's city ends. With
less than the configured time left, it buys a shield with gold and activates it. A new
shield replaces the current one and always lasts 7 days, so what was left is lost:
that is why it waits for the shield to run low. Nothing is clicked or read off the screen.

What the game sends (measured on the live game, 02/11/2026; the end read here matched
the bonus panel, and the purchase and the activation were captured from the game's own):

    Read      route 14007   [[city_id]]                   -> [[start, end, active]]
    Buy       route 1002    [[city_id], 2113, 1, 0, 0]    -> [[[{"3003": 1}, ...]]]
    Activate  route 14001   [[[3003, 1]]]                 -> [[new_end]]

(the request is the one object GameLink.call packs; one more list around the purchase
or the activation, and the game refuses it with "Unexpected code 0x91")

The city is field 16 of the player's own profile (route 402).

A purchase is never repeated within a run: a shield that was bought and could not be
activated fails without a retry, and is reported. The next run buys another, because
what the player already holds is not read. A shield costs little; a city left without
one because of that would cost much more.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from config.schema import Field
from config.settings import ProfileConfig
from utils.logger import logger

from ..base_module import BaseModule
from ..events.traffic import GameLinkError
from ..members.roster import PROFILES_ROUTE

READ_ROUTE = 14007
BUY_ROUTE = 1002
ACTIVATE_ROUTE = 14001

# The shield of the game's store, and the item it puts in the player's hands.
SHIELD_PRODUCT = 2113
SHIELD_ITEM = 3003

PROFILE_CITY = 16


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_profile_city(row: Any) -> Optional[int]:
    """`[city_id]` at field 16 of a profile row -> city_id."""
    city = row[PROFILE_CITY] if isinstance(row, list) and len(row) > PROFILE_CITY else None
    return city[0] if isinstance(city, list) and city and _is_int(city[0]) else None


def parse_shield(objects: Any) -> Optional[Tuple[int, bool]]:
    """(end, active) of an answer of route 14007; None when it has neither."""
    try:
        _start, end, active = objects[0]
    except (IndexError, TypeError, ValueError):
        return None
    return (end, active) if _is_int(end) and isinstance(active, bool) else None


def bought_a_shield(objects: Any) -> bool:
    """Whether an answer of route 1002 says the item was handed over."""
    try:
        return objects[0][0][0].get(str(SHIELD_ITEM), 0) >= 1
    except (AttributeError, IndexError, TypeError):
        return False


def parse_activation(objects: Any) -> Optional[int]:
    """The new end, in an answer of route 14001."""
    try:
        end = objects[0][0]
    except (IndexError, TypeError):
        return None
    return end if _is_int(end) else None


def own_city(link) -> Optional[int]:
    """The city of the profile the game is on, from its own profile."""
    player = link.player_id
    for frame in link.call(PROFILES_ROUTE, [[[player]]]):
        rows = frame.objects[0][0] if frame.objects and frame.objects[0] else []
        for row in rows:
            if isinstance(row, list) and row and row[0] == [player]:
                return parse_profile_city(row)
    return None


def read_shield(link, city: int) -> Tuple[int, bool]:
    for frame in link.call(READ_ROUTE, [[city]]):
        shield = parse_shield(frame.objects)
        if shield:
            return shield
    raise GameLinkError("the game's answer did not carry the shield's end")


def describe(end: int, now: float) -> str:
    return f"{(end - now) / 3600:.1f}h left, until {datetime.fromtimestamp(end):%d/%m %H:%M}"


class ShieldModule(BaseModule):
    key = "shield"
    title = "Shield of Peace"
    description = ("Asks the game when the city's shield ends and, when it is about to, buys a new one with gold "
                   "and activates it. Runs on the profiles it is turned on for (Accounts and profiles > Modules). "
                   "A new shield replaces the current one and lasts 7 days. The time between runs must be shorter "
                   "than the time set here, or the shield can end before a run sees it.")
    settings = (
        Field("renew_below_hours", "int", "Renew with less than (hours)", 24,
              "A shield with less than this left is replaced by a new one, bought with gold.",
              minimum=1, maximum=167),
    )
    profile_fields = (
        Field("enabled", "bool", "Keep the shield up", False,
              "Buys and activates a shield for this profile's city when the current one runs low. Spends gold."),
    )

    # ---------------------------------------------------------- configuration
    @classmethod
    def configured_for(cls, profile: ProfileConfig) -> bool:
        return bool(profile.module_settings(cls.key).get("enabled"))

    @classmethod
    def profile_summary(cls, profile: ProfileConfig) -> str:
        return "shield" if cls.configured_for(profile) else ""

    # -------------------------------------------------------------------- run
    def _failure(self, profile: ProfileConfig, reason: str, retry: bool = False) -> Dict[str, Any]:
        logger.error(f"[{profile.label}] Shield: {reason}")
        return {"success": False, "retry": retry, "profile": profile.name,
                "account": profile.account_name, "reason": reason}

    def run_for_profile(self, profile: ProfileConfig) -> Dict[str, Any]:
        # Here and not at the top, as in the members module.
        from ..chests.game_link import GameLink

        link = GameLink(host=self.browser.host, port=self.browser.port, tab_id=self.browser.tab_id,
                        url_filter=self.browser.url_filter,
                        on_status=lambda text: logger.warning(f"Game link: {text}"))
        if not link.start():
            return self._failure(profile, "could not talk to the game", retry=True)
        try:
            return self._keep_up(link, profile)
        finally:
            link.stop()

    def _keep_up(self, link, profile: ProfileConfig) -> Dict[str, Any]:
        try:
            city = own_city(link)
            if city is None:
                return self._failure(profile, "the game's profile did not name the city", retry=True)
            end, active = read_shield(link, city)
        except GameLinkError as exc:
            return self._failure(profile, f"the game did not say when the shield ends: {exc}", retry=True)

        result: Dict[str, Any] = {"success": True, "profile": profile.name, "account": profile.account_name,
                                  "renewed": False, "shield_ends": end}
        now = time.time()
        limit = float(self.options.get("renew_below_hours", 24)) * 3600
        if active and end - now >= limit:
            logger.info(f"[{profile.label}] Shield up, {describe(end, now)}: nothing to do.")
            return result

        logger.info(f"[{profile.label}] " + (f"Shield running low, {describe(end, now)}"
                                             if active and end > now else "No shield up")
                    + ": buying and activating a new one.")
        try:
            answers = link.call(BUY_ROUTE, [[city], SHIELD_PRODUCT, 1, 0, 0])
        except GameLinkError as exc:
            return self._failure(profile, f"the shield could not be bought (is there enough gold?): {exc}")
        if not any(bought_a_shield(frame.objects) for frame in answers):
            return self._failure(profile, "the game's answer to the purchase did not hand over a shield")

        try:
            link.call(ACTIVATE_ROUTE, [[[SHIELD_ITEM, 1]]])
            new_end, new_active = read_shield(link, city)
        except GameLinkError as exc:
            # It may be the game refusing (a march on its way, a reinforcement in the
            # city), and asking again at once would not change that.
            return self._failure(profile, f"a shield was bought but not confirmed as activated: {exc}")
        if not new_active or new_end <= end:
            return self._failure(profile, "a shield was bought and activated, but the game still gives "
                                          f"the same end ({datetime.fromtimestamp(new_end):%d/%m %H:%M})")

        logger.info(f"[{profile.label}] New shield up, {describe(new_end, time.time())}.")
        result.update(renewed=True, shield_ends=new_end)
        return result
