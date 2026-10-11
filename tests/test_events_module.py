"""The events module end to end: the simulated game, a fake site per clan, and the real run around them."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import AccountConfig, ProfileConfig  # noqa: E402
from core.calibration import Calibration  # noqa: E402
from core.runner import ModuleRunner  # noqa: E402
from fakes import SITE_A, SITE_B, Clock, FakeBrowser, FakeContext, database, make_config, make_schedule  # noqa: E402
from modules.events import EventsModule, api, legacy  # noqa: E402
from modules.events import module as events_module  # noqa: E402
from modules.events.layout import JournalLayout  # noqa: E402
from modules.events.store import SearchMemory, SentHistory  # noqa: E402
from modules.registry import step_groups  # noqa: E402
from test_events_autosend import FakeSite, journal_entries  # noqa: E402
from test_events_journal import ENDED_AT  # noqa: E402
from test_events_mapper import FakeGame, make_calibration, title  # noqa: E402
from utils.cancel import cancellation  # noqa: E402

UID_A = "aaaaaaaa-0000-4000-8000-00000000000a"
UID_B = "bbbbbbbb-0000-4000-8000-00000000000b"


class GameBrowser(FakeGame, FakeBrowser):
    """The simulated game, with what the run and the module ask of the real browser besides."""

    reloads = restarts = 0

    def reload(self, wait=True):
        self.reloads += 1
        self.view, self.page = "list", 0

class GameOcr:
    """core.ocr.OCREngine as the module uses it: the text the fake game put on the capture."""

    def __init__(self):
        self.game = None

    def status(self):
        return True, "fake engine"

    def image_text(self, image, single_line=False, lang=""):
        if image is None or image.shape[0] != 4:
            return ""
        return self.game.texts[image.shape[1] - 1]


class FakeListener:
    """Stands in for the second CDP connection: the fake game feeds the collector itself."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.running = False
        FakeListener.instances.append(self)

    def start(self):
        self.running = True
        return True

    def stop(self):
        self.running = False


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
        self.config.set("modules.events", "cards_per_page", 3)

        self.layout = make_calibration(os.path.join(folder, "calibration.json"))
        calibration = self.layout.calibration
        for name in ("profile_menu_button",):
            calibration.set_point(name, 10, 10)
        for name in ("profiles_list_area", "profile_name_display_area"):
            calibration.regions[name] = {"left": 1, "top": 1, "width": 40, "height": 20}

        self.ocr = GameOcr()
        self.sites = {SITE_A["site_url"]: FakeSite(), SITE_B["site_url"]: FakeSite()}
        self.clients = []
        FakeListener.instances = []

        def client(site, token, version=""):
            self.clients.append((site, token))
            return self.sites[site]

        patches = [
            mock.patch.object(events_module, "TrafficListener", FakeListener),
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
        """A context whose browser is the game: `tournaments` is profile -> (title, uid)."""
        self.tournaments = tournaments
        ctx = FakeContext(self.config, self.layout.calibration, ocr=self.ocr)
        test = self

        class Switcher(type(ctx.profiles)):
            """Switching profile puts that clan's Journal on screen, as the game would."""

            def switch_to(self, profile, siblings=()):
                name, uid = test.tournaments[profile.name]
                game = GameBrowser(test.layout, [[title(name)]], {name: (uid, (1024, 1), [1001, 1002])}, None)
                test.games[profile.name] = game
                test.ocr.game = game
                ctx.browser = game
                # The collector only exists once the module starts: hand it over then.
                original = events_module.journal.JournalCollector

                def collector(cache=None):
                    made = original(cache)
                    game.collector = made
                    made.feed_record(journal_entries([(uid, ENDED_AT, (1024, 1))]))
                    return made

                test.collector_patch = mock.patch.object(events_module.journal, "JournalCollector", collector)
                test.collector_patch.start()
                test.addCleanup(test.collector_patch.stop)
                return super().switch_to(profile, siblings)

        ctx.profiles = Switcher()
        self.games = {}
        return ctx

    def run_module(self, tournaments, **kwargs):
        ctx = self.context(tournaments)
        self.clock = getattr(self, "clock", Clock(datetime(2026, 10, 7, 3, 0)))
        self.runner = ModuleRunner(ctx, schedule=make_schedule(self.tmp.name, self.clock))
        return self.runner.run(**kwargs)

    def test_each_profile_sends_its_clan_ranking_to_its_own_site_with_its_own_token(self):
        summary = self.run_module({"Alpha": ("Rise of the Ancients", UID_A), "Beta": ("Epic Hunt", UID_B)})

        self.assertEqual([], summary["failures"])
        # Gamma has no clan site: never switched to, never searched.
        self.assertEqual({"Alpha", "Beta"}, set(self.games))
        self.assertEqual([(SITE_A["site_url"], "cct_a"), (SITE_B["site_url"], "cct_b")], self.clients)

        [post_a] = self.sites[SITE_A["site_url"]].posts
        [post_b] = self.sites[SITE_B["site_url"]].posts
        self.assertEqual((UID_A, "Rise of the Ancients"), (post_a["result_uid"], post_a["name"]))
        self.assertEqual((UID_B, "Epic Hunt"), (post_b["result_uid"], post_b["name"]))
        self.assertTrue(post_a["auto_publish"], "every player has a name")
        self.assertEqual(["Player 1001", "Player 1002"], [row["name"] for row in post_a["rows"]])
        self.assertTrue(self.sites[SITE_A["site_url"]].reports[0]["complete"])

        # The listener was attached to the tab the browser drives, and released.
        self.assertEqual("TAB", FakeListener.instances[0].kwargs["tab_id"])
        self.assertFalse(any(listener.running for listener in FakeListener.instances))
        self.assertEqual("list", self.games["Alpha"].view, "the Journal was left on its list")

        results = {r["profile"]: r for r in summary["results"]}
        self.assertEqual((1, 1, 1), (results["Alpha"]["tournaments"], results["Alpha"]["sent"],
                                     results["Alpha"]["published"]))

        # Straight after whatever ran before: the game is not reloaded.
        self.assertEqual(0, self.games["Alpha"].reloads)
        # The complete search is remembered, per clan site.
        self.assertIsNotNone(SearchMemory(SITE_A["site_url"], self.tmp.name).last_search())

    def test_the_next_search_only_walks_the_days_since_the_last_one(self):
        self.config.set("modules.events", "days_back", 7)
        tournaments = {"Alpha": ("Rise of the Ancients", UID_A), "Beta": ("Epic Hunt", UID_B)}
        seen = []
        original = events_module.autosend.AutoSearch

        def search(*args, **kwargs):
            seen.append((args[5].max_age_days, kwargs["use_site_cutoff"]))
            return original(*args, **kwargs)

        with mock.patch.object(events_module.autosend, "AutoSearch", search):
            self.run_module(tournaments, only_profile="Alpha")
            self.run_module(tournaments, only_profile="Alpha", force=True)
            self.config.set("modules.events", "days_back", 14)
            self.run_module(tournaments, only_profile="Alpha", force=True)

        # First: all 7 days. Same day again: today plus one of overlap. Raised to 14: all of them, once.
        self.assertEqual([(7, True), (1, True), (14, False)], seen)
        self.assertEqual(1, len(self.sites[SITE_A["site_url"]].posts), "and nothing was sent twice")

    def test_it_runs_once_a_day_per_profile(self):
        tournaments = {"Alpha": ("Rise of the Ancients", UID_A), "Beta": ("Epic Hunt", UID_B)}
        self.run_module(tournaments)
        self.clients.clear()

        summary = self.run_module(tournaments)
        self.assertEqual([], self.clients, "second start of the day: no site is even contacted")
        self.assertEqual({}, self.games)
        self.assertEqual([], summary["failures"])

    def test_a_token_the_site_refuses_stops_that_profile_before_anything_is_clicked(self):
        def refused():
            raise api.ApiError(401, "This API token is invalid, expired or revoked.")

        self.sites[SITE_A["site_url"]].me = refused
        summary = self.run_module({"Alpha": ("Rise of the Ancients", UID_A), "Beta": ("Epic Hunt", UID_B)})

        [failure] = summary["failures"]
        self.assertEqual(("events", "Alpha"), (failure["module"], failure["profile"]))
        self.assertIn("invalid", failure["reason"])
        self.assertEqual([], self.games["Alpha"].clicks)
        self.assertEqual(1, len(self.sites[SITE_B["site_url"]].posts), "the other clan is not affected")

    def test_the_game_is_reloaded_first_and_must_come_back_on_the_same_profile(self):
        self.config.set("modules.events", "reload_game", True)
        self.run_module({"Alpha": ("Rise of the Ancients", UID_A), "Beta": ("Epic Hunt", UID_B)},
                        only_profile="Alpha")
        self.assertEqual(1, self.games["Alpha"].reloads)
        self.assertEqual(1, len(self.sites[SITE_A["site_url"]].posts))

        # Tomorrow the game reopens on another city: nothing may be sent.
        self.clock.now = datetime(2026, 10, 8, 3, 0)
        ctx = self.context({"Alpha": ("Other", UID_B), "Beta": ("Epic Hunt", UID_B)})
        ctx.profiles.is_active = lambda _name, _siblings=(): False
        self.runner = ModuleRunner(ctx, schedule=make_schedule(self.tmp.name, self.clock))
        summary = self.runner.run(only_profile="Alpha")

        self.assertEqual(["all attempts failed"], [f["reason"] for f in summary["failures"]])
        self.assertEqual(1, len(self.sites[SITE_A["site_url"]].posts), "nothing new was sent")

    def test_half_filled_site_settings_are_pointed_out(self):
        profile = ProfileConfig(name="Half", account_name="Main", modules={"events": {"site_url": "x.example.com"}})
        self.assertFalse(EventsModule.configured_for(profile))
        self.assertIn("token missing", EventsModule.profile_summary(profile))
        module = EventsModule(FakeContext(self.config, self.layout.calibration))
        self.assertEqual(1, len(module.preflight([profile])))
        self.assertEqual((False, "Fill in the clan site address."), EventsModule.test_profile({}))


class LegacyImportTest(unittest.TestCase):
    def test_brings_the_uploaders_calibration_names_and_history_without_replacing_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "EventUploader"
            (old / "calibracao_refs").mkdir(parents=True)
            (old / "calibracao_refs" / "show_details.png").write_bytes(b"png")
            (old / "calibracao_journal.json").write_text(json.dumps({
                "viewport": {"width": 1536, "height": 695}, "cards_per_page": 4,
                "points": {"journal_button": [40, 600], "next_page": [900, 650]},
                "regions": {"card1_title": {"left": 300, "top": 100, "width": 500, "height": 30},
                            "card2_title": {"left": 300, "top": 190, "width": 500, "height": 30},
                            "show_details": {"left": 600, "top": 500, "width": 160, "height": 40}},
                "step_viewports": {"journal_button": [1536, 695]},
            }), encoding="utf-8")
            (old / "perfis_jogadores.json").write_text(json.dumps(
                {"profiles": {"1001": {"name": "Lion", "power": 5, "clan_tag": "KOK"}}}), encoding="utf-8")
            (old / "config.json").write_text(json.dumps(
                {"site_url": "https://clan-a.example.com", "sent": {UID_A: {"event_number": 3}}}), encoding="utf-8")

            config = make_config(tmp)
            calibration = Calibration(step_groups(), os.path.join(tmp, "calibration.json"), os.path.join(tmp, "refs"))
            calibration.set_viewport(1536, 731)
            calibration.set_point("journal_next_page", 1, 2)      # already calibrated here: kept
            data = os.path.join(tmp, "data")
            with mock.patch.object(legacy, "legacy_dir", lambda: old), \
                    mock.patch.object(legacy, "profile_cache_path", lambda: os.path.join(data, "players.json")), \
                    mock.patch.object(legacy, "SentHistory", lambda site: SentHistory(site, data)):
                report = legacy.import_eventuploader(config, calibration)
                again = legacy.import_eventuploader(config, calibration)

            self.assertEqual((40, 600), calibration.points["journal_button"])
            self.assertEqual((1, 2), calibration.points["journal_next_page"])
            self.assertTrue(os.path.exists(calibration.ref_path("journal_show_details")))
            self.assertEqual((1536, 695), calibration.step_viewports["journal_button"])
            self.assertEqual(4, config.module("events")["cards_per_page"])
            self.assertEqual(190 - 100, JournalLayout(calibration).card_pitch())
            self.assertEqual(3, SentHistory("https://clan-a.example.com", data).info(UID_A)["event_number"])
            self.assertIn("1001", json.loads(Path(data, "players.json").read_text(encoding="utf-8"))["profiles"])
            self.assertIn("4 Journal step(s) imported", report)
            self.assertIn("1536x695", report, "a different page size is pointed out")
            self.assertIn("token is not imported", report)
            self.assertNotIn("imported.", again.split("The uploader")[0].replace("left as they are", ""))


if __name__ == "__main__":
    unittest.main()
