"""The events module end to end: the simulated feed, a fake site per clan, and the real run around them."""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import AccountConfig, ProfileConfig  # noqa: E402
from core.runner import ModuleRunner  # noqa: E402
from fakes import SITE_A, SITE_B, Clock, FakeContext, database, make_config, make_schedule  # noqa: E402
from modules.events import EventsModule, api, legacy  # noqa: E402
from modules.events import feed as events_feed  # noqa: E402
from modules.events import module as events_module  # noqa: E402
from modules.events.store import SearchMemory, SentHistory  # noqa: E402
from test_events_autosend import FakeSite  # noqa: E402
from test_events_mapper import FakeFeed, ranking_entry, uid  # noqa: E402
from utils.cancel import cancellation  # noqa: E402

UID_A, UID_B = uid(5001), uid(5002)


class EventsModuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = self.tmp.name
        self.config = make_config(folder, [AccountConfig(name="Main", login="a", password="b", profiles=[
            ProfileConfig(name="Alpha", database=database("clan_a"), account_name="Main",
                          modules={"events": dict(SITE_A)}),
            ProfileConfig(name="Beta", account_name="Main", modules={"events": dict(SITE_B)}),
            ProfileConfig(name="Gamma", database=database("clan_c"), account_name="Main"),
        ])])
        self.config.set("modules.chests", "enabled", False)
        self.sites = {SITE_A["site_url"]: FakeSite(), SITE_B["site_url"]: FakeSite()}
        for site in self.sites.values():
            site.known_tournaments = lambda: [{"game_type": 1024, "ranking": "", "name": "Rise of the Ancients"}]
        self.clients = []
        self.games = {}
        self.current = {}
        test = self

        class Feed:
            """Stands in for JournalFeed: the Journal of the profile the run has switched to."""

            def __init__(self, **kwargs):
                self.game = test.current["feed"]
                self.game.kwargs = kwargs
                self.game.running = False

            def start(self):
                self.game.running = True
                return True

            def stop(self):
                self.game.running = False

            def __getattr__(self, name):
                return getattr(self.game, name)

        def client(site, token, version=""):
            self.clients.append((site, token))
            return self.sites[site]

        patches = [
            mock.patch.object(events_feed, "JournalFeed", Feed),
            mock.patch.object(events_module, "SiteClient", client),
            mock.patch.object(events_module, "profile_cache_path", lambda: os.path.join(folder, "players.json")),
            mock.patch.object(events_module, "SentHistory", lambda site: SentHistory(site, folder)),
            mock.patch.object(events_module, "SearchMemory", lambda site: SearchMemory(site, folder)),
            # The walk's pauses are for a real game; here it answers at once.
            mock.patch.object(cancellation, "sleep", lambda _seconds: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def context(self, tournaments):
        """A context whose game is the feed: `tournaments` is profile -> the entry number of its ranking."""
        ctx = FakeContext(self.config)
        test = self

        class Switcher(type(ctx.profiles)):
            """Switching profile puts that clan's Journal in the feed, as the game would."""

            def switch_to(self, profile):
                test.current["feed"] = FakeFeed([ranking_entry(tournaments[profile.name], int(time.time()), (1024, 1))])
                test.games[profile.name] = test.current["feed"]
                return super().switch_to(profile)

        ctx.profiles = Switcher()
        self.games = {}
        return ctx

    def run_module(self, tournaments, **kwargs):
        ctx = self.context(tournaments)
        self.clock = getattr(self, "clock", Clock(datetime(2026, 10, 7, 3, 0)))
        self.runner = ModuleRunner(ctx, schedule=make_schedule(self.tmp.name, self.clock))
        return self.runner.run(**kwargs)

    def test_each_profile_sends_its_clan_ranking_to_its_own_site_with_its_own_token(self):
        summary = self.run_module({"Alpha": 1, "Beta": 2})

        self.assertEqual([], summary["failures"])
        # Gamma has no clan site: never switched to, never searched.
        self.assertEqual({"Alpha", "Beta"}, set(self.games))
        self.assertEqual([(SITE_A["site_url"], "cct_a"), (SITE_B["site_url"], "cct_b")], self.clients)

        [post_a] = self.sites[SITE_A["site_url"]].posts
        [post_b] = self.sites[SITE_B["site_url"]].posts
        self.assertEqual((UID_A, "Rise of the Ancients"), (post_a["result_uid"], post_a["name"]))
        self.assertEqual(UID_B, post_b["result_uid"])
        self.assertTrue(post_a["auto_publish"], "every player has a name")
        self.assertEqual(["Player 1001", "Player 1002"], [row["name"] for row in post_a["rows"]])
        self.assertTrue(self.sites[SITE_A["site_url"]].reports[0]["complete"])

        # The feed was attached to the tab the browser drives, and released.
        self.assertEqual("TAB", self.games["Alpha"].kwargs["tab_id"])
        self.assertFalse(any(game.running for game in self.games.values()))

        results = {r["profile"]: r for r in summary["results"]}
        self.assertEqual((1, 1, 1), (results["Alpha"]["tournaments"], results["Alpha"]["sent"],
                                     results["Alpha"]["published"]))

        # The complete search is remembered, per clan site.
        self.assertIsNotNone(SearchMemory(SITE_A["site_url"], self.tmp.name).last_search())

    def test_the_next_search_only_walks_the_days_since_the_last_one(self):
        self.config.set("modules.events", "days_back", 7)
        tournaments = {"Alpha": 1, "Beta": 2}
        seen = []
        original = events_module.autosend.AutoSearch

        def search(*args, **kwargs):
            seen.append((args[3].max_age_days, kwargs["use_site_cutoff"]))
            return original(*args, **kwargs)

        with mock.patch.object(events_module.autosend, "AutoSearch", search):
            self.run_module(tournaments, only_profile="Alpha")
            self.run_module(tournaments, only_profile="Alpha", force=True)
            self.config.set("modules.events", "days_back", 14)
            self.run_module(tournaments, only_profile="Alpha", force=True)

        # First: all 7 days. Same day again: today plus one of overlap. Raised to 14: all of them, once.
        self.assertEqual([(7, True), (1, True), (14, False)], seen)

    def test_it_runs_once_a_day_per_profile(self):
        tournaments = {"Alpha": 1, "Beta": 2}
        self.run_module(tournaments)
        self.clients.clear()

        summary = self.run_module(tournaments)
        self.assertEqual([], self.clients, "second start of the day: no site is even contacted")
        self.assertEqual({}, self.games)
        self.assertEqual([], summary["failures"])

    def test_a_token_the_site_refuses_stops_that_profile_before_the_game_is_asked(self):
        def refused():
            raise api.ApiError(401, "This API token is invalid, expired or revoked.")

        self.sites[SITE_A["site_url"]].me = refused
        summary = self.run_module({"Alpha": 1, "Beta": 2})

        [failure] = summary["failures"]
        self.assertEqual(("events", "Alpha"), (failure["module"], failure["profile"]))
        self.assertIn("invalid", failure["reason"])
        self.assertEqual([], self.games["Alpha"].asked_rankings)
        self.assertEqual(1, len(self.sites[SITE_B["site_url"]].posts), "the other clan is not affected")

    def test_half_filled_site_settings_are_pointed_out(self):
        profile = ProfileConfig(name="Half", account_name="Main", modules={"events": {"site_url": "x.example.com"}})
        self.assertFalse(EventsModule.configured_for(profile))
        self.assertIn("token missing", EventsModule.profile_summary(profile))
        module = EventsModule(FakeContext(self.config))
        self.assertEqual(1, len(module.preflight([profile])))
        self.assertEqual((False, "Fill in the clan site address."), EventsModule.test_profile({}))


class LegacyImportTest(unittest.TestCase):
    def test_brings_the_uploaders_names_and_history_without_replacing_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "EventUploader"
            old.mkdir(parents=True)
            (old / "perfis_jogadores.json").write_text(json.dumps(
                {"profiles": {"1001": {"name": "Lion", "power": 5, "clan_tag": "KOK"}}}), encoding="utf-8")
            (old / "config.json").write_text(json.dumps(
                {"site_url": "https://clan-a.example.com", "sent": {UID_A: {"event_number": 3}}}), encoding="utf-8")

            config = make_config(tmp)
            data = os.path.join(tmp, "data")
            with mock.patch.object(legacy, "legacy_dir", lambda: old), \
                    mock.patch.object(legacy, "profile_cache_path", lambda: os.path.join(data, "players.json")), \
                    mock.patch.object(legacy, "SentHistory", lambda site: SentHistory(site, data)):
                report = legacy.import_eventuploader(config)
                again = legacy.import_eventuploader(config)

            self.assertEqual(3, SentHistory("https://clan-a.example.com", data).info(UID_A)["event_number"])
            self.assertIn("1001", json.loads(Path(data, "players.json").read_text(encoding="utf-8"))["profiles"])
            self.assertIn("token is not imported", report)
            self.assertNotIn("imported.", again.split("The uploader")[0])


if __name__ == "__main__":
    unittest.main()
