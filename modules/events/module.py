"""
Events module - the clan's tournament rankings, from the Journal to the clan site.

For each profile it asks the game for the Journal over the network and sends the
rankings the site does not have yet (see autosend.py for the whole search). It was
the standalone EventUploader; here it runs on the profile the application has just
switched to. The screen is only pictured for a tournament type the site has never
heard of, so a search does not depend on the calibration until that happens.

One clan, one site, one token
-----------------------------
A profile belongs to one clan and each clan has its own site, so the site's
address and its API token are kept with the profile - next to the database the
chests of that same profile are written to. A profile without them is simply
not searched.

Once a day
----------
The site only needs a search after tournaments end, so the module runs once a day by default (see
core/schedule.py): the first run of the day that completes the search is the
day's search, and one that does not is tried again on the next run. The day is
the game's, starting at `day_start` (14:00 by default), when the tournaments of
the day before have ended.

How far back
------------
Every entry of the Journal says when it was received. A search walks back `days_back` days at most, and after a complete one
only the days since it (store.SearchMemory) - so a daily run asks for a page or
two, not the whole Journal again.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config.schema import Field
from config.settings import ProfileConfig
from core.schedule import SCHEDULE_DAILY
from utils.logger import logger

from ..base_module import BaseModule
from . import autosend, journal, mapper
from .api import ApiError, SiteClient, normalize_site_url
from .layout import DEFAULT_CARDS_PER_PAGE, STEPS, JournalLayout
from .screen import JournalScreen
from .store import SearchMemory, SentHistory, profile_cache_path

# The version of the uploader this module carries, as the site records it.
CLIENT_VERSION = "1.1.0"


class EventsModule(BaseModule):
    key = "events"
    title = "Tournament rankings"
    description = ("Reads the clan's tournament results in the Journal and sends the rankings to the clan "
                   "site. Each profile uses its own site address and API token (Accounts and profiles > "
                   "Modules); a profile without them is skipped.")
    default_schedule = SCHEDULE_DAILY
    default_day_start = "14:00"   # the game's day turns in the afternoon, not at midnight
    steps = STEPS
    settings = (
        Field("auto_publish", "bool", "Close complete rankings on the site", True,
              "When every player of a ranking has a name the site is asked to publish it right away. "
              "Otherwise, or with this off, it stays a draft for review on the site."),
        Field("days_back", "int", "Days to look back", 7,
              "How many days of the Journal a search walks. After a complete search only the days since "
              "it are walked again.",
              minimum=1, maximum=365),
        Field("cards_per_page", "int", "Cards per Journal page", DEFAULT_CARDS_PER_PAGE,
              "How many cards one page of the Journal shows. It says on which page a card is when the "
              "new tournament has to be pictured.", minimum=1, maximum=12),
        Field("max_pages", "int", "Journal requests limit", 200,
              "A safety limit, of 50 entries per request: a search stops by itself when it reaches "
              "tournaments an earlier search has already covered.", minimum=1, maximum=1000),
    )
    profile_fields = (
        Field("site_url", "str", "Clan site address", "",
              "The chestcounter site of this profile's clan, e.g. https://myclan.example.com"),
        Field("api_token", "password", "Site API token", "",
              "The uploader token created on that site. Each clan site has its own."),
    )
    # ---------------------------------------------------------- configuration
    @classmethod
    def site_of(cls, profile: ProfileConfig) -> Tuple[str, str]:
        values = profile.module_settings(cls.key)
        return (normalize_site_url(str(values.get("site_url") or "")),
                str(values.get("api_token") or "").strip())

    @classmethod
    def configured_for(cls, profile: ProfileConfig) -> bool:
        return all(cls.site_of(profile))

    @classmethod
    def profile_summary(cls, profile: ProfileConfig) -> str:
        site, token = cls.site_of(profile)
        if site and token:
            return "events → " + site.split("://", 1)[-1]
        if site or token:
            return "events: " + ("token missing" if site else "site address missing")
        return ""

    @classmethod
    def test_profile(cls, values: Dict[str, Any]) -> Optional[Tuple[bool, str]]:
        site = normalize_site_url(str(values.get("site_url") or ""))
        if not site:
            return False, "Fill in the clan site address."
        try:
            me = SiteClient(site, str(values.get("api_token") or ""), CLIENT_VERSION).me()
        except ApiError as exc:
            return False, exc.describe()
        name = (me.get("user") or {}).get("name") or "?"
        return True, f"Connected to {site} as {name}."

    def preflight(self, profiles: List[ProfileConfig]) -> List[str]:
        return [
            f"Profile '{profile.label}': the events module needs both the clan site address and its "
            f"API token (Accounts and profiles > Modules). Fill in both, or clear both to skip it."
            for profile in profiles
            if any(self.site_of(profile)) and not self.configured_for(profile)
        ]

    # -------------------------------------------------------------------- run
    def _failure(self, profile: ProfileConfig, reason: str, retry: bool = False) -> Dict[str, Any]:
        logger.error(f"[{profile.label}] Tournament search not done: {reason}")
        return {"success": False, "retry": retry, "profile": profile.name,
                "account": profile.account_name, "reason": reason}

    def run_for_profile(self, profile: ProfileConfig) -> Dict[str, Any]:
        site, token = self.site_of(profile)
        if not (site and token):
            return self._failure(profile, "no clan site address and token configured for this profile")

        # Asked first: with a token the site refuses, the whole walk through the
        # Journal would end in rankings nobody accepts.
        client = SiteClient(site, token, CLIENT_VERSION)
        try:
            client.me()
        except ApiError as exc:
            return self._failure(profile, f"the site {site} refused the connection: {exc.describe()}")

        logger.info(f"[{profile.label}] Searching the Journal for tournaments to send to {site}")
        collector = journal.JournalCollector(journal.ProfileCache(profile_cache_path()))
        # Here and not at the top: the feed builds on the chests' link, which imports this package.
        from .feed import JournalFeed

        feed = JournalFeed(host=self.browser.host, port=self.browser.port, tab_id=self.browser.tab_id,
                           url_filter=self.browser.url_filter,
                           on_status=lambda text: logger.warning(f"Game link: {text}"))
        if not feed.start():
            return self._failure(profile, "could not talk to the game", retry=True)

        try:
            known: Dict[str, dict] = {}
            try:
                known = {f"{k['game_type']}|{k.get('ranking') or ''}": k for k in client.known_tournaments()}
            except ApiError as exc:
                logger.warning(f"The site's catalogue is unavailable: {exc.message}")

            def catalogue_name(t: journal.Tournament) -> str:
                entry = known.get(f"{(t.tournament_key or '').split(':')[0]}|{t.ranking or ''}")
                return entry["name"] if entry and entry.get("name") else ""

            def has_picture(t: journal.Tournament) -> bool:
                entry = known.get(f"{(t.tournament_key or '').split(':')[0]}|{t.ranking or ''}")
                return bool(entry and entry.get("has_picture"))

            history = SentHistory(site)
            memory = SearchMemory(site)
            started = datetime.now()
            days_back = max(1, int(self.options.get("days_back", 7)))
            days, afresh = memory.days_to_walk(days_back, started.date())
            last = memory.last_search()
            if afresh:
                logger.info(f"The days to look back were raised to {days_back}: walking all of them once.")
            elif last is not None:
                logger.info(f"Last complete search on {last:%d/%m/%Y %H:%M}: walking back {days} day(s) "
                            f"of the {days_back} configured.")
            cards_per_page = int(self.options.get("cards_per_page", DEFAULT_CARDS_PER_PAGE))
            options = mapper.MapperOptions(max_pages=max(1, int(self.options.get("max_pages", 200))),
                                           cards_per_page=cards_per_page, max_age_days=days)
            screen = JournalScreen(self.ctx, JournalLayout(self.calibration, cards_per_page))
            search = autosend.AutoSearch(
                feed, screen.picture, collector, client, options,
                local_info=history.info, mark_sent=history.mark, catalogue_name=catalogue_name,
                has_picture=has_picture,
                client_version=CLIENT_VERSION, auto_publish=bool(self.options.get("auto_publish", True)),
                on_log=logger.info, use_site_cutoff=not afresh,
            )
            summary = search.run()
            if summary.complete:
                memory.record(started, days_back)
        finally:
            feed.stop()
            collector.save_profiles()

        logger.info(f"[{profile.label}] Tournament search: {summary.text}")
        result: Dict[str, Any] = {
            "success": summary.complete,
            # An incomplete search is not repeated at once: the site keeps the
            # point it stopped at, and the next run of the day goes back to it.
            "retry": False,
            "profile": profile.name,
            "account": profile.account_name,
            "tournaments": summary.opened,
            "sent": len(summary.outcomes) - summary.failed,
            "published": summary.published,
            "for_review": summary.for_review,
            "skipped": summary.skipped,
        }
        if not summary.complete:
            result["reason"] = summary.text
        return result
