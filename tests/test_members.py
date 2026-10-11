"""The members module: the packets it reads, the files it writes, and a run against a simulated game."""
import base64
import csv
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import AccountConfig, ProfileConfig  # noqa: E402
from core.calibration import Calibration  # noqa: E402
from core.runner import ModuleRunner  # noqa: E402
from fakes import SITE_A, Clock, FakeBrowser, FakeContext, database, make_config, make_schedule  # noqa: E402
from modules.events import tbcodec  # noqa: E402
from modules.events.api import ApiError  # noqa: E402
from modules.members import MembersModule  # noqa: E402
from modules.members import module as members_module  # noqa: E402
from modules.members.roster import (CLAN_ROUTE, PROFILES_ROUTE, RosterCollector, parse_clan_record,  # noqa: E402
                                    parse_profile_row)
from modules.members.store import roster_paths, save_roster  # noqa: E402
from modules.registry import MODULES, step_groups  # noqa: E402
from utils.cancel import cancellation  # noqa: E402

CLAN_ID = 936302870583
CLAN_POINT, MEMBERS_POINT = (10, 10), (20, 20)
LIST_AREA = {"left": 100, "top": 100, "width": 200, "height": 100}

# player id -> (name, rank, power, level), as the game has them.
PLAYERS = {
    644245120340: ("Alok BR", 1, 1660747555, 450),
    463856535987: ("Ventura", 2, 1874523405, 454),
    545461058706: ("Vida BR", 2, 1601800905, 431),
    335007482510: ("Vithor", 3, 864258005, 331),
    597000781537: ("Daniela", 5, 242447015, 227),
}


def profile_row(player_id, clan_id=CLAN_ID):
    """A row of route 402, in the shape the game sends it."""
    name, _rank, power, level = PLAYERS.get(player_id, ("Stranger", 0, 5, 1))
    return [[player_id], "tb:1", name, "BR", 3, 10000054, 10000104, level, 45, 14, power, [clan_id], 1966850,
            "LPL", 0, [282, 640, 554], [335093746196], 26434, 43729967, [[375, 1791576827]], [[2, 6]], "", [0]]


def clan_record(player_ids=tuple(PLAYERS)):
    """A clan of route 15000: the membership is its third item."""
    membership = [[[pid], PLAYERS[pid][1], 1790833148] for pid in player_ids]
    return [[CLAN_ID], ["LPL", "LION PRIDE LEGION", 1966850, "Bem vindo"], membership, [], {}, [], 100]


def record(route, *objects):
    """What traffic.TrafficListener hands over for one answer of the game."""
    data = tbcodec.build_frame(route, 1, *objects)
    return {"kind": "RESP", "routes": [route], "b64": base64.b64encode(data).decode("ascii")}


def clan_answer(player_ids=tuple(PLAYERS)):
    return record(CLAN_ROUTE, [[clan_record(player_ids)]])


def profiles_answer(player_ids):
    return record(PROFILES_ROUTE, [[profile_row(pid) for pid in player_ids]])


class RosterTest(unittest.TestCase):
    def test_a_profile_row_gives_id_name_power_level_and_place(self):
        member = parse_profile_row(profile_row(644245120340))
        self.assertEqual((644245120340, "Alok BR", 1660747555, 450), (member.player_id, member.name,
                                                                      member.power, member.level))
        self.assertEqual((282, 640, 554), member.coordinates)
        self.assertEqual(CLAN_ID, member.clan_id)
        self.assertIsNone(parse_profile_row([[1], "tb:1", ""]))
        self.assertIsNone(parse_profile_row("not a row"))

    def test_a_clan_record_counts_only_with_its_membership(self):
        clan = parse_clan_record(clan_record())
        self.assertEqual((CLAN_ID, "LPL", "LION PRIDE LEGION"), (clan.clan_id, clan.tag, clan.name))
        self.assertEqual({pid: values[1] for pid, values in PLAYERS.items()}, clan.ranks)
        # The answers to the window's periodic refresh come with no members.
        self.assertIsNone(parse_clan_record(clan_record(())))

    def test_the_roster_is_complete_when_every_member_has_a_profile(self):
        collector = RosterCollector()
        self.assertFalse(collector.roster().complete, "nothing is known before the clan arrives")

        self.assertTrue(collector.feed_record(clan_answer()))
        self.assertTrue(collector.feed_record(profiles_answer(list(PLAYERS)[:3])))
        roster = collector.roster()
        self.assertEqual((5, 3, False), (roster.expected, len(roster.members), roster.complete))
        self.assertEqual([335007482510, 597000781537], roster.missing)

        collector.feed_record(profiles_answer(list(PLAYERS)[3:]))
        # A refresh without members does not undo the membership already seen.
        self.assertFalse(collector.feed_record(clan_answer(())))
        roster = collector.roster()
        self.assertTrue(roster.complete)
        # By rank, the strongest first - the order of the game's own list.
        self.assertEqual(["Alok BR", "Ventura", "Vida BR", "Vithor", "Daniela"], [m.name for m in roster.members])
        self.assertEqual(["Leader", "Superior", "Superior", "Officer", "Soldier"],
                         [m.rank_name for m in roster.members])

    def test_players_of_other_clans_and_other_answers_are_left_out(self):
        collector = RosterCollector()
        collector.feed_record(clan_answer())
        collector.feed_record(profiles_answer(list(PLAYERS)))
        collector.feed_record(record(PROFILES_ROUTE, [[profile_row(999, clan_id=5)]]))
        self.assertFalse(collector.feed_record(record(318, [[profile_row(998)]])))
        self.assertFalse(collector.feed_record({"kind": "RESP", "routes": [PROFILES_ROUTE], "b64": "!!"}))
        self.assertEqual(set(PLAYERS), {m.player_id for m in collector.roster().members})

    def test_the_roster_is_saved_as_json_and_as_a_spreadsheet(self):
        collector = RosterCollector()
        collector.feed_record(clan_answer())
        collector.feed_record(profiles_answer(list(PLAYERS)))
        with tempfile.TemporaryDirectory() as folder:
            json_path, csv_path = save_roster(collector.roster(), datetime(2026, 10, 11, 10, 0), "Main / Alpha", folder)
            self.assertEqual(roster_paths(collector.roster().clan, folder), (json_path, csv_path))
            self.assertTrue(json_path.endswith(f"LPL_{CLAN_ID}.json"))
            with open(json_path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual({"id": CLAN_ID, "tag": "LPL", "name": "LION PRIDE LEGION"}, data["clan"])
            self.assertEqual("2026-10-11T10:00:00", data["read_at"])
            self.assertEqual({"player_id": 644245120340, "name": "Alok BR", "power": 1660747555, "rank": 1,
                              "rank_name": "Leader", "level": 450, "kingdom": 282, "x": 640, "y": 554},
                             data["members"][0])
            with open(csv_path, encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(5, len(rows))
            self.assertEqual(("463856535987", "Ventura", "1874523405"),
                             (rows[1]["player_id"], rows[1]["name"], rows[1]["power"]))


class ClanGame(FakeBrowser):
    """
    The game as the module drives it: the clan button answers with the clan, the
    Members menu with the first batch of profiles, and each scroll of the list
    with the next one.
    """

    def __init__(self, batches, sends_membership=True, list_views=3):
        super().__init__()
        self.batches = [list(batch) for batch in batches]
        self.sends_membership = sends_membership
        self.list_views = list_views
        self.feed = None            # the listener's on_record, once it is listening
        self.clicks = []
        self.scrolls = 0
        self.pending = []

    def click(self, x, y, delay=0.0, clicks=1):
        self.clicks.append((x, y))
        if (x, y) == CLAN_POINT:
            self.pending = [list(batch) for batch in self.batches]
            self.scrolls = 0
            if self.sends_membership:
                self.feed(clan_answer())
        elif (x, y) == MEMBERS_POINT:
            self._send_batch()

    def scroll_down(self, x, y, repetitions=5):
        self.scrolls += 1
        self._send_batch()

    def _send_batch(self):
        if self.pending:
            self.feed(profiles_answer(self.pending.pop(0)))

    def capture(self, region=None, scale=1.0):
        # The list shows another picture at each scroll, until its end.
        return np.full((4, 4, 3), 60 * min(self.scrolls, self.list_views), dtype=np.uint8)


class MembersModuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        self.config = make_config(self.folder, [AccountConfig(name="Main", login="a", password="b", profiles=[
            ProfileConfig(name="Alpha", database=database("clan_a"), account_name="Main",
                          modules={"members": {"enabled": True}}),
            ProfileConfig(name="Beta", database=database("clan_b"), account_name="Main"),
        ])])
        self.config.set("modules.chests", "enabled", False)
        self.config.set("modules.events", "enabled", False)
        self.alpha, self.beta = self.config.accounts[0].profiles

        self.calibration = Calibration(step_groups(), os.path.join(self.folder, "calibration.json"),
                                       os.path.join(self.folder, "refs"))
        self.calibration.set_viewport(1000, 800)
        for name in ("profile_menu_button",):
            self.calibration.set_point(name, 5, 5)
        for name in ("profiles_list_area", "profile_name_display_area"):
            self.calibration.regions[name] = {"left": 1, "top": 1, "width": 40, "height": 20}
        self.calibration.set_point("clan_button", *CLAN_POINT)
        self.calibration.set_point("clan_members_button", *MEMBERS_POINT)
        self.calibration.regions["clan_members_list"] = dict(LIST_AREA)
        self.calibration.use_viewport(1000, 800)

        self.listeners = []
        test = self

        class Listener:
            """Stands in for the second CDP connection: the fake game feeds the collector itself."""

            def __init__(self, **kwargs):
                self.running = False
                self.on_record = kwargs["on_record"]
                test.listeners.append(self)

            def start(self):
                self.running = True
                test.game.feed = self.on_record
                return test.listening

        Listener.stop = lambda listener: setattr(listener, "running", False)
        self.listening = True

        # The waits are for a real game; here the clock moves as fast as it is slept.
        clock = types.SimpleNamespace(now=0.0)
        patches = [
            mock.patch.object(members_module, "TrafficListener", Listener),
            mock.patch.object(members_module, "save_roster",
                              lambda roster, when, by="": save_roster(roster, when, by, self.folder)),
            mock.patch.object(members_module, "time", types.SimpleNamespace(time=lambda: clock.now)),
            mock.patch.object(cancellation, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def module(self, game):
        self.game = game
        self.ctx = FakeContext(self.config, self.calibration, browser=game)
        return MembersModule(self.ctx)

    def saved(self):
        path = os.path.join(self.folder, f"LPL_{CLAN_ID}.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_it_is_a_registered_module_run_only_where_it_was_turned_on(self):
        self.assertIn(MembersModule, MODULES)
        self.assertTrue(MembersModule.configured_for(self.alpha))
        self.assertFalse(MembersModule.configured_for(self.beta))
        self.assertEqual("members", MembersModule.profile_summary(self.alpha))
        self.assertEqual("", MembersModule.profile_summary(self.beta))

    def test_the_whole_clan_in_one_answer_is_saved_without_scrolling(self):
        result = self.module(ClanGame([list(PLAYERS)])).run_for_profile(self.alpha)

        self.assertTrue(result["success"], result)
        self.assertEqual((5, "LPL", CLAN_ID), (result["members"], result["clan_tag"], result["clan_id"]))
        self.assertEqual([CLAN_POINT, MEMBERS_POINT], self.game.clicks)
        self.assertEqual(0, self.game.scrolls)
        saved = self.saved()
        self.assertEqual("Main / Alpha", saved["read_by"])
        self.assertEqual([("Alok BR", 644245120340, 1660747555), ("Ventura", 463856535987, 1874523405)],
                         [(m["name"], m["player_id"], m["power"]) for m in saved["members"][:2]])
        self.assertFalse(self.listeners[0].running, "the listener is stopped")
        self.assertEqual(1, self.ctx.game_state.closed, "the clan window is closed for whatever runs next")

    def test_the_list_is_scrolled_only_for_the_members_that_did_not_come(self):
        ids = list(PLAYERS)
        result = self.module(ClanGame([ids[:2], ids[2:4], ids[4:]])).run_for_profile(self.alpha)

        self.assertTrue(result["success"], result)
        self.assertEqual(2, self.game.scrolls, "one scroll per batch still owed, and no more")
        self.assertEqual(5, len(self.saved()["members"]))

    def test_a_list_that_ends_short_is_asked_for_again_and_then_given_up(self):
        ids = list(PLAYERS)
        result = self.module(ClanGame([ids[:2], ids[2:4]])).run_for_profile(self.alpha)

        self.assertEqual((False, True), (result["success"], result["retry"]))
        self.assertIn("4 of 5", result["reason"])
        self.assertEqual([CLAN_POINT, MEMBERS_POINT] * 2, self.game.clicks, "the clan window was opened again")
        self.assertIsNone(self.saved(), "a roster with members missing is not saved")

    def test_without_the_membership_nothing_is_saved(self):
        result = self.module(ClanGame([list(PLAYERS)], sends_membership=False)).run_for_profile(self.alpha)

        self.assertEqual((False, True), (result["success"], result["retry"]))
        self.assertIn("membership", result["reason"])
        self.assertIsNone(self.saved())

    def test_nothing_is_clicked_without_the_calibration_or_the_listener(self):
        self.listening = False
        result = self.module(ClanGame([list(PLAYERS)])).run_for_profile(self.alpha)
        self.assertEqual((False, True, []), (result["success"], result["retry"], self.game.clicks))

        self.listening = True
        del self.calibration.points["clan_members_button"]
        result = self.module(ClanGame([list(PLAYERS)])).run_for_profile(self.alpha)
        self.assertEqual((False, False, []), (result["success"], result["retry"], self.game.clicks))
        self.assertIn("Clan: Members", result["reason"])

    def with_site(self, answer=None, error=None):
        """Gives Alpha a clan site and stands in for it: returns what was sent there."""
        self.alpha.modules["events"] = dict(SITE_A)
        sent = []

        class Site:
            def __init__(self, site, token, version=""):
                sent.append({"site": site, "token": token, "version": version})

            def send_members(self, payload):
                sent[-1]["payload"] = payload
                if error is not None:
                    raise error
                return answer or {"members": {"applied": True, "created": 1, "activated": 0, "deactivated": 2,
                                              "renamed": 0, "ambiguous": []},
                                  "received": len(payload["members"]), "active_members": len(payload["members"])}

        patch = mock.patch.object(members_module, "SiteClient", Site)
        patch.start()
        self.addCleanup(patch.stop)
        return sent

    def test_a_profile_with_a_clan_site_sends_it_the_whole_list(self):
        sent = self.with_site()
        result = self.module(ClanGame([list(PLAYERS)])).run_for_profile(self.alpha)

        self.assertTrue(result["success"], result)
        self.assertEqual((True, 5, 2), (result["sent"], result["active_on_site"], result["deactivated"]))
        [call] = sent
        self.assertEqual((SITE_A["site_url"], SITE_A["api_token"]), (call["site"], call["token"]))
        payload = call["payload"]
        self.assertEqual({"id": CLAN_ID, "tag": "LPL", "name": "LION PRIDE LEGION"}, payload["clan"])
        self.assertRegex(payload["read_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual({"player_id": 644245120340, "name": "Alok BR", "power": 1660747555, "rank": 1, "level": 450},
                         payload["members"][0])
        self.assertEqual(set(PLAYERS), {m["player_id"] for m in payload["members"]})

    def test_the_time_of_the_reading_travels_in_utc(self):
        collector = RosterCollector()
        collector.feed_record(clan_answer())
        collector.feed_record(profiles_answer(list(PLAYERS)))
        brasilia = timezone(timedelta(hours=-3))
        payload = members_module.build_payload(collector.roster(), datetime(2026, 10, 11, 10, 9, 5, tzinfo=brasilia))
        self.assertEqual("2026-10-11T13:09:05Z", payload["read_at"])

    def test_a_short_list_is_never_sent_to_the_site(self):
        sent = self.with_site()
        ids = list(PLAYERS)
        result = self.module(ClanGame([ids[:2], ids[2:4]])).run_for_profile(self.alpha)

        self.assertFalse(result["success"])
        self.assertEqual([], sent, "members missing from the list would be made inactive on the site")

    def test_a_site_that_refuses_the_list_fails_the_module_but_keeps_the_files(self):
        self.with_site(error=ApiError(409, "The site already has a newer list of the clan."))
        result = self.module(ClanGame([list(PLAYERS)])).run_for_profile(self.alpha)

        self.assertEqual((False, False), (result["success"], result["retry"]))
        self.assertIn("newer list", result["reason"])
        self.assertEqual(5, len(self.saved()["members"]))

    def test_a_site_from_before_this_api_or_the_switch_off_leaves_only_the_files(self):
        sent = self.with_site(error=ApiError(404, "Not Found"))
        result = self.module(ClanGame([list(PLAYERS)])).run_for_profile(self.alpha)
        self.assertEqual((True, False), (result["success"], result["sent"]))
        self.assertEqual(1, len(sent))

        self.config.set("modules.members", "send_to_site", False)
        result = self.module(ClanGame([list(PLAYERS)])).run_for_profile(self.alpha)
        self.assertEqual((True, False), (result["success"], result["sent"]))
        self.assertEqual(1, len(sent), "nothing more was sent")

        # A site address without its token is no site at all.
        self.config.set("modules.members", "send_to_site", True)
        self.alpha.modules["events"] = {"site_url": SITE_A["site_url"]}
        self.assertEqual(("", ""), MembersModule.site_of(self.alpha))

    def test_a_run_reads_the_members_on_the_profile_that_asked_for_them(self):
        module = self.module(ClanGame([list(PLAYERS)]))
        schedule = make_schedule(self.folder, Clock(datetime(2026, 10, 11, 3, 0)))
        summary = ModuleRunner(self.ctx, [module], schedule).run()

        self.assertEqual([], summary["failures"])
        self.assertEqual(["Alpha"], self.ctx.profiles.switched, "Beta did not ask for it and is not visited")
        self.assertEqual(5, len(self.saved()["members"]))
        # Once a day: the next start of the same day leaves the clan window alone.
        self.game.clicks.clear()
        ModuleRunner(self.ctx, [module], schedule).run()
        self.assertEqual([], self.game.clicks)


if __name__ == "__main__":
    unittest.main()
