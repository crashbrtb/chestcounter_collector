"""
Events module - the clan's tournament rankings, from the Journal to the clan site.

For each profile it listens to what the game receives, walks the Journal opening
every "Your Clanmates' results in ..." card, and sends the rankings the site
does not have yet (see autosend.py for the whole search). It was the standalone
EventUploader; here it runs on the profile the application has just switched to,
with the browser, the OCR and the calibration every module shares.

One clan, one site, one token
-----------------------------
A profile belongs to one clan and each clan has its own site, so the site's
address and its API token are kept with the profile - next to the database the
chests of that same profile are written to. A profile without them is simply
not searched.

Once a day
----------
A search costs a walk through the Journal, and the site only needs it after
tournaments end, so the module runs once a day by default (see
core/schedule.py): the first run of the day that completes the search is the
day's search, and one that does not is tried again on the next run.

How far back
------------
The Journal says how long ago each card was received ("yesterday", "7 days
ago"). A search walks back `days_back` days at most, and after a complete one
only the days since it (store.SearchMemory) - so a daily run reads a page or
two, not the whole Journal again.

No reload
---------
The search starts on the game as the previous module left it. The game does not
send again a ranking it already fetched since the page loaded, which only
matters when the Journal was read by hand in that same session; `reload_game`
is there for that, and costs a minute per profile.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config.schema import Field
from config.settings import ProfileConfig
from core.schedule import SCHEDULE_DAILY
from core.session import SessionError
from utils.cancel import cancellation
from utils.logger import logger

from ..base_module import BaseModule, ModuleAction
from . import autosend, journal, mapper
from .api import ApiError, SiteClient, normalize_site_url
from .layout import DEFAULT_CARDS_PER_PAGE, REQUIRED_STEPS, STEPS, JournalLayout
from .store import SearchMemory, SentHistory, profile_cache_path
from .titles import TitleReader
from .traffic import TrafficListener

# The version of the uploader this module carries, as the site records it.
CLIENT_VERSION = "1.1.0"


class EventsModule(BaseModule):
    key = "events"
    title = "Tournament rankings"
    description = ("Reads the clan's tournament results in the Journal and sends the rankings to the clan "
                   "site. Each profile uses its own site address and API token (Accounts and profiles > "
                   "Modules); a profile without them is skipped.")
    default_schedule = SCHEDULE_DAILY
    steps = STEPS
    required_steps = REQUIRED_STEPS
    settings = (
        Field("auto_publish", "bool", "Close complete rankings on the site", True,
              "When every player of a ranking has a name the site is asked to publish it right away. "
              "Otherwise, or with this off, it stays a draft for review on the site."),
        Field("days_back", "int", "Days to look back", 7,
              "How many days of the Journal a search walks, by the age each card shows (\"yesterday\", "
              "\"7 days ago\"). After a complete search only the days since it are walked again.",
              minimum=1, maximum=365),
        Field("reload_game", "bool", "Reload the game before searching", False,
              "Off: the search starts at once, on the game as it is. Turn on only if rankings come "
              "back without an answer: the game does not send again one it already fetched since "
              "the page loaded."),
        Field("reload_wait", "float", "Wait after reloading (s)", 60.0,
              "Only with the reload on: time the game takes to open again.", minimum=10.0, maximum=600.0),
        Field("cards_per_page", "int", "Cards per Journal page", DEFAULT_CARDS_PER_PAGE,
              "How many cards one page of the Journal shows. Only the first two are calibrated; "
              "the others are placed by the distance between them.", minimum=1, maximum=12),
        Field("max_pages", "int", "Journal pages limit", 200,
              "A safety limit: a search stops by itself when it reaches tournaments an earlier "
              "search has already covered.", minimum=1, maximum=1000),
        Field("title_language", "str", "Tesseract language for titles", "eng",
              "Language model used to read the card titles. Falls back to the OCR section's "
              "language when this one is not installed."),
    )
    profile_fields = (
        Field("site_url", "str", "Clan site address", "",
              "The chestcounter site of this profile's clan, e.g. https://myclan.example.com"),
        Field("api_token", "password", "Site API token", "",
              "The uploader token created on that site. Each clan site has its own."),
    )
    actions = (
        ModuleAction("Import from EventUploader", "import_legacy",
                     "Brings over what the standalone EventUploader saved on this computer: the Journal "
                     "calibration, the players' names and the list of tournaments already sent."),
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

    @classmethod
    def import_legacy(cls, config, calibration) -> str:
        from .legacy import import_eventuploader

        return import_eventuploader(config, calibration)

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

    def _reload_game(self, profile: ProfileConfig):
        """
        Reloads the page and makes sure the game came back on the same profile.

        The check is not a formality: the ranking read next goes to this
        profile's clan site, and a game that reopened on another city would
        send one clan's results to another clan's site.
        """
        wait = max(10.0, float(self.options.get("reload_wait", 60.0)))
        logger.info(f"Reloading the game and waiting {wait:.0f}s for it to open again...")
        self.browser.reload(wait=True)
        cancellation.sleep(wait)
        self.browser.refresh_dpr()
        self.game_state.prepare_board()

        siblings = self.config.profile_names(profile.account_name)
        if not self.ctx.profiles.is_active(profile.name, siblings):
            raise SessionError(
                f"after reloading, the game is not on profile '{profile.name}' "
                f"(reads '{self.ctx.profiles.active_profile_name()}')"
            )

    def run_for_profile(self, profile: ProfileConfig) -> Dict[str, Any]:
        site, token = self.site_of(profile)
        if not (site and token):
            return self._failure(profile, "no clan site address and token configured for this profile")

        layout = JournalLayout(self.calibration, int(self.options.get("cards_per_page", DEFAULT_CARDS_PER_PAGE)))
        missing = layout.missing_steps()
        if missing:
            return self._failure(profile, "Journal not calibrated: " + self.calibration.titles(missing))

        reader = TitleReader(self.ocr, str(self.options.get("title_language") or "eng"))
        if not reader.available():
            return self._failure(profile, reader.error)

        # Asked before anything is clicked: with a token the site refuses, the
        # whole walk through the Journal would end in rankings nobody accepts.
        client = SiteClient(site, token, CLIENT_VERSION)
        try:
            client.me()
        except ApiError as exc:
            return self._failure(profile, f"the site {site} refused the connection: {exc.describe()}")

        logger.info(f"[{profile.label}] Searching the Journal for tournaments to send to {site}")
        collector = journal.JournalCollector(journal.ProfileCache(profile_cache_path()))
        listener = TrafficListener(
            host=self.browser.host, port=self.browser.port, tab_id=self.browser.tab_id,
            url_filter=self.browser.url_filter, on_record=collector.feed_record,
            on_status=lambda text: logger.warning(f"Traffic: {text}"),
        )
        if not listener.start():
            return self._failure(profile, "could not listen to the game's traffic", retry=True)

        try:
            cancellation.sleep(1.5)   # the listener needs a moment to attach before anything is clicked
            reloaded = bool(self.options.get("reload_game", False))
            if reloaded:
                self._reload_game(profile)

            known: Dict[str, dict] = {}
            try:
                known = {f"{k['game_type']}|{k.get('ranking') or ''}": k for k in client.known_tournaments()}
            except ApiError as exc:
                logger.warning(f"The site's catalogue is unavailable: {exc.message}")

            def catalogue_name(t: journal.Tournament) -> str:
                entry = known.get(f"{(t.tournament_key or '').split(':')[0]}|{t.ranking or ''}")
                return entry["name"] if entry and entry.get("name") else ""

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
            options = mapper.MapperOptions(
                max_pages=max(1, int(self.options.get("max_pages", 200))),
                open_journal=True, reopen_known=True, max_age_days=days,
                # Right after a reload the Journal may not open at the first click.
                journal_retries=3 if reloaded else 1,
            )
            search = autosend.AutoSearch(
                self.browser, layout, reader, collector, client, options,
                local_info=history.info, mark_sent=history.mark, catalogue_name=catalogue_name,
                client_version=CLIENT_VERSION, auto_publish=bool(self.options.get("auto_publish", True)),
                on_log=logger.info, vision=self.vision, use_site_cutoff=not afresh,
            )
            summary = search.run()
            if summary.complete:
                memory.record(started, days_back)
            if not reloaded and summary.unanswered:
                logger.warning(f"{summary.unanswered} ranking(s) were not sent by the game, which keeps the ones "
                               f"it already fetched since the page loaded. If this repeats, turn on "
                               f"'Reload the game before searching' in the module's settings.")
        finally:
            listener.stop()
            collector.save_profiles()
            try:
                self.game_state.close_modals()   # leave the Journal closed for whatever runs next
            except Exception as exc:  # noqa: BLE001 - the browser may be what failed
                logger.debug(f"Closing the Journal: {exc}")

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
