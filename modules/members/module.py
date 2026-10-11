"""
Members module - who is in the clan, with each one's player id and power.

For each profile it listens to what the game receives, opens the clan window and
its Members menu, and keeps what the game sent: the clan's membership (id and
rank of every member) and the members' profiles (name, power, level, place on
the map). Nothing is read off the screen - see roster.py for the packets.

No scrolling, most of the time
------------------------------
The game sends the whole clan in one answer when Members is clicked. The list is
only scrolled when the membership names someone no profile came for, and then
only until that stops being true or the list stops moving.

One profile per clan
--------------------
The roster is the clan's, so one profile of each clan is enough: the module runs
on the profiles it was turned on for (Accounts and profiles > Modules), and
nowhere else.

To the clan site
----------------
A profile that has a clan site (the address and token the tournament module
uses, kept with the profile) also sends the list there: every player in it
becomes an active member on the site and every member not in it inactive. That
is why only a complete list is ever sent - one that came short would turn real
members off. The files are written first, so a site that is down costs nothing
but the sending, which the next run of the day tries again.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Tuple

from config.schema import Field
from config.settings import ProfileConfig
from core.schedule import SCHEDULE_DAILY
from core.vision import changed_fraction
from utils.cancel import cancellation
from utils.logger import logger

from ..base_module import BaseModule
from ..events.api import ApiError, SiteClient, normalize_site_url
from ..events.traffic import TrafficListener
from .roster import Roster, RosterCollector
from .steps import REQUIRED_STEPS, STEPS
from .store import save_roster

# The version of this module, as the site records it.
CLIENT_VERSION = "1.0.0"

# The clan window is opened this many times before giving up: the game sends the
# membership when the window opens, so opening it again is the way to ask again.
OPEN_ATTEMPTS = 2

# Where the profile's clan site is kept: with the tournament module, which was
# the first to need it. A profile has one clan and a clan one site.
SITE_MODULE_KEY = "events"


def build_payload(roster: Roster, read_at: datetime, client_version: str = CLIENT_VERSION) -> dict:
    """The body of POST /api/v1/members. `read_at` is an aware time; it travels in UTC."""
    clan = roster.clan
    return {
        "clan": {"id": clan.clan_id, "tag": clan.tag, "name": clan.name},
        "read_at": read_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "client_version": client_version,
        "members": [
            {"player_id": member.player_id, "name": member.name, "power": member.power,
             "rank": member.rank, "level": member.level}
            for member in roster.members
        ],
    }


class MembersModule(BaseModule):
    key = "members"
    title = "Clan members"
    description = ("Opens the clan's Members list and records every member with their player id, power, "
                   "rank and level, as the game sends them. Runs on the profiles it is turned on for "
                   "(Accounts and profiles > Modules); the list is saved to config/module_data/members "
                   "and sent to the profile's clan site, where it decides who is an active member.")
    default_schedule = SCHEDULE_DAILY
    steps = STEPS
    required_steps = REQUIRED_STEPS
    settings = (
        Field("answer_wait", "float", "Wait for the game's answer (s)", 10.0,
              "How long to wait, after each click, for the game to send the list of members.",
              minimum=2.0, maximum=120.0),
        Field("max_scrolls", "int", "Scrolls of the list at most", 40,
              "Only used when the game did not send every member at once: the list is scrolled "
              "this many times at most to get the rest.", minimum=0, maximum=500),
        Field("send_to_site", "bool", "Send the list to the clan site", True,
              "On a profile that has a clan site address and token (Accounts and profiles > Modules), "
              "the list is sent there: its players become active members on the site and every other "
              "member inactive. Off, the list is only saved to the files."),
    )
    profile_fields = (
        Field("enabled", "bool", "Read clan members", False,
              "Reads the members of this profile's clan. One profile per clan is enough."),
    )

    # ---------------------------------------------------------- configuration
    @classmethod
    def configured_for(cls, profile: ProfileConfig) -> bool:
        return bool(profile.module_settings(cls.key).get("enabled"))

    @classmethod
    def profile_summary(cls, profile: ProfileConfig) -> str:
        return "members" if cls.configured_for(profile) else ""

    @staticmethod
    def site_of(profile: ProfileConfig) -> Tuple[str, str]:
        """The profile's clan site and its token; ('', '') unless both are filled in."""
        values = profile.module_settings(SITE_MODULE_KEY)
        site = normalize_site_url(str(values.get("site_url") or ""))
        token = str(values.get("api_token") or "").strip()
        return (site, token) if site and token else ("", "")

    # -------------------------------------------------------------------- run
    def _failure(self, profile: ProfileConfig, reason: str, retry: bool = False) -> Dict[str, Any]:
        logger.error(f"[{profile.label}] Clan members not read: {reason}")
        return {"success": False, "retry": retry, "profile": profile.name,
                "account": profile.account_name, "reason": reason}

    @staticmethod
    def _wait(condition: Callable[[], bool], timeout: float) -> bool:
        deadline = time.time() + timeout
        while not condition():
            if time.time() >= deadline:
                return False
            cancellation.sleep(0.3)
        return True

    def _scroll_for_the_rest(self, collector: RosterCollector) -> Roster:
        """Scrolls the list until every member has a profile, or there is no more list."""
        roster = collector.roster()
        area = self.calibration.region("clan_members_list")
        if not area:
            logger.warning(f"The game sent {len(roster.members)} of {roster.expected} members, and the list "
                           f"cannot be scrolled for the rest: 'Clan: members list' is not calibrated.")
            return roster

        logger.info(f"The game sent {len(roster.members)} of {roster.expected} members; scrolling for the rest.")
        x, y = area[0] + area[2] // 2, area[1] + area[3] // 2
        for _ in range(max(0, int(self.options.get("max_scrolls", 40)))):
            if collector.roster().complete:
                break
            before = self.browser.capture(area, scale=1.0)
            self.browser.scroll_down(x, y)
            cancellation.sleep(0.8)
            if changed_fraction(before, self.browser.capture(area, scale=1.0)) < 0.01:
                # The end of the list: what was asked for last may still be on its way.
                self._wait(lambda: collector.roster().complete, 2.0)
                break
        return collector.roster()

    def _read(self, collector: RosterCollector) -> Roster:
        wait = float(self.options.get("answer_wait", 10.0))
        clan_point = self.calibration.point("clan_button")
        members_point = self.calibration.point("clan_members_button")

        roster = Roster()
        for attempt in range(1, OPEN_ATTEMPTS + 1):
            if attempt > 1:
                logger.info("The game did not send the whole list; closing the clan window and opening it again.")
                self.game_state.close_modals()
            collector.clear()

            self.browser.click(clan_point[0], clan_point[1], delay=0.5)
            # Also what tells the window is open before its menu is clicked.
            self._wait(lambda: collector.roster().clan is not None, wait)
            self.browser.click(members_point[0], members_point[1], delay=0.5)
            self._wait(lambda: collector.roster().complete, wait)

            roster = collector.roster()
            if roster.clan is not None and not roster.complete:
                roster = self._scroll_for_the_rest(collector)
            if roster.complete:
                break
        return roster

    def run_for_profile(self, profile: ProfileConfig) -> Dict[str, Any]:
        missing = self.calibration.require(*REQUIRED_STEPS)
        if missing:
            return self._failure(profile, "clan menu not calibrated: " + self.calibration.titles(missing))

        logger.info(f"[{profile.label}] Reading the clan's members")
        collector = RosterCollector()
        listener = TrafficListener(
            host=self.browser.host, port=self.browser.port, tab_id=self.browser.tab_id,
            url_filter=self.browser.url_filter, on_record=collector.feed_record,
            on_status=lambda text: logger.warning(f"Traffic: {text}"),
        )
        if not listener.start():
            return self._failure(profile, "could not listen to the game's traffic", retry=True)

        try:
            cancellation.sleep(1.5)   # the listener needs a moment to attach before anything is clicked
            roster = self._read(collector)
        finally:
            listener.stop()
            try:
                self.game_state.close_modals()   # leave the clan window closed for whatever runs next
            except Exception as exc:  # noqa: BLE001 - the browser may be what failed
                logger.debug(f"Closing the clan window: {exc}")

        if roster.clan is None:
            return self._failure(profile, "the game did not send the clan's membership when the clan "
                                          "window was opened", retry=True)
        if not roster.complete:
            return self._failure(profile, f"the game sent {len(roster.members)} of {roster.expected} members",
                                 retry=True)

        clan = roster.clan
        read_at = datetime.now().astimezone()
        json_path, csv_path = save_roster(roster, read_at.replace(tzinfo=None), profile.label)
        power = sum(member.power or 0 for member in roster.members)
        logger.info(f"[{profile.label}] [{clan.tag}] {clan.name}: {len(roster.members)} members, "
                    f"total power {power:,}. Saved to {csv_path}")
        result: Dict[str, Any] = {
            "success": True,
            "profile": profile.name,
            "account": profile.account_name,
            "clan_id": clan.clan_id,
            "clan_tag": clan.tag,
            "members": len(roster.members),
            "file": json_path,
            "sent": False,
        }

        site, token = self.site_of(profile)
        if not self.options.get("send_to_site", True):
            return result
        if not site:
            logger.info(f"[{profile.label}] No clan site address and token on this profile: "
                        f"the list was saved to the files only.")
            return result

        try:
            answer = SiteClient(site, token, CLIENT_VERSION).send_members(build_payload(roster, read_at))
        except ApiError as exc:
            if exc.status == 404:
                # A site from before this API: nothing is wrong with the reading.
                logger.warning(f"[{profile.label}] The site {site} does not receive member lists yet "
                               f"(update the site): the list was saved to the files only.")
                return result
            # Not tried again at once: reading the game again would not change the
            # site's answer. The next run of the day sends a fresh list.
            return self._failure(profile, f"the list was read and saved, but the site {site} "
                                          f"did not take it: {exc.describe()}")

        changes = answer.get("members") or {}
        logger.info(f"[{profile.label}] Sent to {site}: {answer.get('active_members', '?')} active members there now "
                    f"({changes.get('created', 0)} new, {changes.get('activated', 0)} back to active, "
                    f"{changes.get('deactivated', 0)} made inactive, {changes.get('renamed', 0)} renamed, "
                    f"{changes.get('chests_moved', 0)} chests moved to a new name).")
        if changes.get("ambiguous"):
            logger.warning(f"[{profile.label}] Not matched on the site, because more than one member without a "
                           f"game id has the same name: {', '.join(map(str, changes['ambiguous']))}. "
                           f"Set the game id of the right one on the site.")
        result.update(sent=True, site=site, active_on_site=answer.get("active_members"),
                      deactivated=changes.get("deactivated", 0))
        return result
