"""The members module: the packets it reads, the files it writes, and a run against a simulated game."""
import base64
import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import AccountConfig, ProfileConfig  # noqa: E402
from core.runner import ModuleRunner  # noqa: E402
from fakes import SITE_A, Clock, FakeContext, database, make_config, make_schedule  # noqa: E402
from modules.chests import game_link  # noqa: E402
from modules.events import tbcodec  # noqa: E402
from modules.events.api import ApiError  # noqa: E402
from modules.events.traffic import GameLinkError  # noqa: E402
from modules.members import MembersModule  # noqa: E402
from modules.members import module as members_module  # noqa: E402
from modules.members.roster import (CLAN_ROUTE, PROFILES_ROUTE, RosterCollector, parse_clan_record,  # noqa: E402
                                    parse_profile_row)
from modules.members.store import roster_paths, save_roster  # noqa: E402
from modules.registry import MODULES  # noqa: E402

CLAN_ID = 936302870583

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


def types_frame(*objects):
    return tbcodec.Frame(0, 0, list(objects), 0, 0, None)


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


class ClanGame:
    """GameLink as the module uses it: the clan's record, and each request of profiles answered in turn."""

    def __init__(self, batches, sends_membership=True, starts=True, fails=False):
        self.batches = [list(batch) for batch in batches]
        self.pending = []
        self.sends_membership = sends_membership
        self.starts = starts
        self.fails = fails
        self.clan_requests = 0
        self.profile_requests = []
        self.running = False

    def start(self):
        self.running = self.starts
        return self.starts

    def stop(self):
        self.running = False

    def clan_id(self):
        return CLAN_ID

    def call(self, route, objects):
        if self.fails:
            raise GameLinkError("the game did not answer route 15000")
        if route == CLAN_ROUTE:
            self.clan_requests += 1
            self.pending = [list(batch) for batch in self.batches]
            members = tuple(PLAYERS) if self.sends_membership else ()
            return [types_frame([[clan_record(members)]])]
        self.profile_requests.append([player[0] for player in objects[0]])
        batch = self.pending.pop(0) if self.pending else []
        return [types_frame([[profile_row(pid) for pid in batch]])]


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

        test = self
        patches = [
            mock.patch.object(game_link, "GameLink", lambda **_kwargs: test.game),
            mock.patch.object(members_module, "save_roster",
                              lambda roster, when, by="": save_roster(roster, when, by, self.folder)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def module(self, game):
        self.game = game
        self.ctx = FakeContext(self.config)
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

    def test_the_whole_clan_is_saved_from_the_games_answers(self):
        result = self.module(ClanGame([list(PLAYERS)])).run_for_profile(self.alpha)

        self.assertTrue(result["success"], result)
        self.assertEqual((5, "LPL", CLAN_ID), (result["members"], result["clan_tag"], result["clan_id"]))
        self.assertEqual([list(PLAYERS)], self.game.profile_requests)
        saved = self.saved()
        self.assertEqual("Main / Alpha", saved["read_by"])
        self.assertEqual([("Alok BR", 644245120340, 1660747555), ("Ventura", 463856535987, 1874523405)],
                         [(m["name"], m["player_id"], m["power"]) for m in saved["members"][:2]])
        self.assertFalse(self.game.running, "the link is stopped")

    def test_a_list_that_comes_short_is_asked_for_again_and_then_given_up(self):
        ids = list(PLAYERS)
        result = self.module(ClanGame([ids[:4]])).run_for_profile(self.alpha)

        self.assertEqual((False, True), (result["success"], result["retry"]))
        self.assertIn("4 of 5", result["reason"])
        self.assertEqual(2, self.game.clan_requests)
        self.assertIsNone(self.saved(), "a roster with members missing is not saved")

    def test_without_the_membership_nothing_is_saved(self):
        result = self.module(ClanGame([list(PLAYERS)], sends_membership=False)).run_for_profile(self.alpha)

        self.assertEqual((False, True), (result["success"], result["retry"]))
        self.assertIn("membership", result["reason"])
        self.assertIsNone(self.saved())

    def test_a_game_that_cannot_be_asked_is_tried_again_later(self):
        for game in (ClanGame([], starts=False), ClanGame([], fails=True)):
            result = self.module(game).run_for_profile(self.alpha)
            self.assertEqual((False, True), (result["success"], result["retry"]))
            self.assertFalse(game.running)

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
        result = self.module(ClanGame([ids[:4]])).run_for_profile(self.alpha)

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
        # Once a day: the next start of the same day does not ask again.
        ModuleRunner(self.ctx, [module], schedule).run()
        self.assertEqual(1, self.game.clan_requests)


if __name__ == "__main__":
    unittest.main()
