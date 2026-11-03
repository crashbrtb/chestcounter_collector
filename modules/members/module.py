"""
Members module - who is in the clan, with each one's player id and power.

For each profile it asks the game, over the network, for the clan's record (id and
rank of every member) and for the members' profiles (name, power, level, place on
the map). Nothing is clicked or read off the screen - see roster.py for the answers.

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

from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from config.schema import Field
from config.settings import ProfileConfig
from core.schedule import SCHEDULE_DAILY
from utils.logger import logger

from ..base_module import BaseModule
from ..events.api import ApiError, SiteClient, normalize_site_url
from ..events.traffic import GameLinkError
from .roster import CLAN_RECORD_WHOLE, CLAN_ROUTE, PROFILES_PER_REQUEST, PROFILES_ROUTE, Roster, RosterCollector
from .store import save_roster

# The version of this module, as the site records it.
CLIENT_VERSION = "1.0.0"

# The clan is asked for this many times before giving up on a list that came short.
ATTEMPTS = 2

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
    description = ("Asks the game for the clan's members and records every one with their player id, power, "
                   "rank and level, as the game sends them. Runs on the profiles it is turned on for "
                   "(Accounts and profiles > Modules); the list is saved to config/module_data/members "
                   "and sent to the profile's clan site, where it decides who is an active member.")
    default_schedule = SCHEDULE_DAILY
    settings = (
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
    def _read(link, collector: RosterCollector) -> Roster:
        """The clan's record, then the profiles of its members."""
        clan_id = link.clan_id()
        if clan_id is None:
            return Roster()
        roster = Roster()
        for _attempt in range(ATTEMPTS):
            collector.clear()
            for frame in link.call(CLAN_ROUTE, [[[[clan_id], CLAN_RECORD_WHOLE]]]):
                collector.feed_tree({"frames": [{"route": CLAN_ROUTE, "objects": frame.objects}]})
            roster = collector.roster()
            if roster.clan is None:
                continue
            for start in range(0, len(roster.missing), PROFILES_PER_REQUEST):
                ids = roster.missing[start:start + PROFILES_PER_REQUEST]
                for frame in link.call(PROFILES_ROUTE, [[[player] for player in ids]]):
                    collector.feed_tree({"frames": [{"route": PROFILES_ROUTE, "objects": frame.objects}]})
            roster = collector.roster()
            if roster.complete:
                break
        return roster

    def run_for_profile(self, profile: ProfileConfig) -> Dict[str, Any]:
        logger.info(f"[{profile.label}] Reading the clan's members")
        collector = RosterCollector()
        # Here and not at the top: the link reads profiles with this package's roster.py.
        from ..chests.game_link import GameLink

        link = GameLink(host=self.browser.host, port=self.browser.port, tab_id=self.browser.tab_id,
                        url_filter=self.browser.url_filter,
                        on_status=lambda text: logger.warning(f"Game link: {text}"))
        if not link.start():
            return self._failure(profile, "could not talk to the game", retry=True)

        try:
            roster = self._read(link, collector)
        except GameLinkError as exc:
            return self._failure(profile, f"the game did not give the clan's members: {exc}", retry=True)
        finally:
            link.stop()

        if roster.clan is None:
            return self._failure(profile, "the game gave no clan's membership for this profile", retry=True)
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
