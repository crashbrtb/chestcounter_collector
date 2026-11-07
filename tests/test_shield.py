"""The shield module: the answers it reads, and a run against a simulated game."""
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import AccountConfig, ProfileConfig  # noqa: E402
from fakes import FakeContext, database, make_config  # noqa: E402
from modules.chests import game_link  # noqa: E402
from modules.events.traffic import GameLinkError  # noqa: E402
from modules.registry import MODULES  # noqa: E402
from modules.shield import ShieldModule  # noqa: E402
from modules.shield.module import (ACTIVATE_ROUTE, BUY_ROUTE, READ_ROUTE, bought_a_shield,  # noqa: E402
                                   parse_activation, parse_profile_city, parse_shield)
from modules.members.roster import PROFILES_ROUTE  # noqa: E402

PLAYER = 485331363759
CITY = 485390815746
HOUR = 3600
WEEK = 7 * 24 * HOUR


def frame(objects):
    return types.SimpleNamespace(objects=objects)


class ShieldGame:
    """GameLink as the module uses it: one shield, replaced when one is activated."""

    def __init__(self, left, active=True, refuse=(), hands_over=True, starts=True):
        self.end = int(time.time() + left)
        self.active = active
        self.refuse = set(refuse)
        self.hands_over = hands_over
        self.starts = starts
        self.player_id = PLAYER
        self.calls = []
        self.running = False

    def start(self):
        self.running = self.starts
        return self.starts

    def stop(self):
        self.running = False

    def call(self, route, objects):
        self.calls.append((route, objects))
        if route in self.refuse:
            raise GameLinkError(f"the game refused route {route}")
        if route == PROFILES_ROUTE:
            row = [[PLAYER], "tb:1", "Crash BR"] + [0] * 13 + [[CITY]]
            return [frame([[[row]]])]
        if route == READ_ROUTE:
            return [frame([[self.end - WEEK, self.end, self.active]])]
        if route == BUY_ROUTE:
            return [frame([[[{"3003": 1} if self.hands_over else {}, {}, []]]])]
        if route == ACTIVATE_ROUTE:
            self.end, self.active = int(time.time() + WEEK), True
            return [frame([[self.end]])]
        raise AssertionError(f"unexpected route {route}")

    def routes(self):
        return [route for route, _objects in self.calls]


class AnswersTest(unittest.TestCase):
    def test_a_measured_read(self):
        self.assertEqual((1793803172, True), parse_shield([[1793198372, 1793803172, True]]))

    def test_an_answer_without_the_shield_is_not_a_shield(self):
        for answer in ([], [[]], [[1, "x", True]], [[1, 2, 1]], None):
            self.assertIsNone(parse_shield(answer), answer)

    def test_a_measured_purchase_and_activation(self):
        self.assertTrue(bought_a_shield([[[{"3003": 1}, {}, [], [], [], [], 0, 0, 0, {}, [], {}, 0, [], 0, {}]]]))
        self.assertEqual(1794249566, parse_activation([[1794249566]]))

    def test_a_purchase_of_something_else_is_not_a_shield(self):
        for answer in ([[[{"9": 1}]]], [[[{}]]], [], None):
            self.assertFalse(bought_a_shield(answer), answer)

    def test_the_city_comes_from_the_profile(self):
        self.assertEqual(CITY, parse_profile_city([0] * 16 + [[CITY]]))
        self.assertIsNone(parse_profile_city([0] * 3))


class ShieldModuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(self.tmp.name, [AccountConfig(name="Main", login="a", password="b", profiles=[
            ProfileConfig(name="Alpha", database=database("clan_a"), account_name="Main",
                          modules={"shield": {"enabled": True}}),
            ProfileConfig(name="Beta", database=database("clan_b"), account_name="Main"),
        ])])
        self.alpha, self.beta = self.config.accounts[0].profiles
        patch = mock.patch.object(game_link, "GameLink", lambda **_kwargs: self.game)
        patch.start()
        self.addCleanup(patch.stop)

    def run_on(self, game):
        self.game = game
        return ShieldModule(FakeContext(self.config)).run_for_profile(self.alpha)

    def test_it_is_registered_and_runs_only_where_it_was_turned_on(self):
        self.assertIn(ShieldModule, MODULES)
        self.assertTrue(ShieldModule.configured_for(self.alpha))
        self.assertFalse(ShieldModule.configured_for(self.beta))

    def test_a_shield_with_time_left_is_left_alone(self):
        result = self.run_on(ShieldGame(25 * HOUR))
        self.assertTrue(result["success"], result)
        self.assertFalse(result["renewed"])
        self.assertEqual([PROFILES_ROUTE, READ_ROUTE], self.game.routes())
        self.assertEqual((READ_ROUTE, [[CITY]]), self.game.calls[1])
        self.assertFalse(self.game.running)

    def test_a_shield_running_low_is_bought_activated_and_confirmed(self):
        result = self.run_on(ShieldGame(23 * HOUR))
        self.assertTrue(result["success"], result)
        self.assertTrue(result["renewed"])
        self.assertEqual([PROFILES_ROUTE, READ_ROUTE, BUY_ROUTE, ACTIVATE_ROUTE, READ_ROUTE], self.game.routes())
        self.assertEqual([[CITY], 2113, 1, 0, 0], self.game.calls[2][1])
        self.assertEqual([[[3003, 1]]], self.game.calls[3][1])
        self.assertEqual(self.game.end, result["shield_ends"])

    def test_the_limit_comes_from_the_settings(self):
        self.config.set("modules.shield", "renew_below_hours", 48)
        self.assertTrue(self.run_on(ShieldGame(47 * HOUR))["renewed"])

    def test_a_city_without_a_shield_gets_one(self):
        self.assertTrue(self.run_on(ShieldGame(-HOUR, active=False))["renewed"])

    def test_nothing_is_bought_when_the_shield_could_not_be_read(self):
        result = self.run_on(ShieldGame(HOUR, refuse=[READ_ROUTE]))
        self.assertFalse(result["success"])
        self.assertTrue(result["retry"])
        self.assertNotIn(BUY_ROUTE, self.game.routes())

    def test_a_refused_purchase_is_not_retried_nor_activated(self):
        result = self.run_on(ShieldGame(HOUR, refuse=[BUY_ROUTE]))
        self.assertFalse(result["success"])
        self.assertFalse(result["retry"])
        self.assertNotIn(ACTIVATE_ROUTE, self.game.routes())

    def test_a_purchase_that_handed_nothing_over_is_not_activated(self):
        result = self.run_on(ShieldGame(HOUR, hands_over=False))
        self.assertFalse(result["success"])
        self.assertNotIn(ACTIVATE_ROUTE, self.game.routes())

    def test_a_refused_activation_does_not_buy_again(self):
        result = self.run_on(ShieldGame(HOUR, refuse=[ACTIVATE_ROUTE]))
        self.assertFalse(result["success"])
        self.assertFalse(result["retry"])
        self.assertEqual(1, self.game.routes().count(BUY_ROUTE))
        self.assertIn("bought", result["reason"])

    def test_no_link_to_the_game_is_tried_again(self):
        result = self.run_on(ShieldGame(HOUR, starts=False))
        self.assertFalse(result["success"])
        self.assertTrue(result["retry"])
        self.assertEqual([], self.game.routes())


if __name__ == "__main__":
    unittest.main()
