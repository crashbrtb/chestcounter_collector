"""Switching profile over the network: what counts as having arrived. The game and its link are simulated."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import ProfileConfig  # noqa: E402
from core import session  # noqa: E402
from fakes import FakeBrowser  # noqa: E402
from modules.chests import game_link  # noqa: E402
from utils.cancel import cancellation  # noqa: E402

TARGET = 485331438653
OTHER = 485331363759


class Link:
    """GameLink as the switcher uses it: who the game says is playing, one answer per look."""

    def __init__(self, players):
        self.players = players
        self.player_id = None

    def __call__(self, **_kwargs):
        return self

    def start(self):
        self.player_id = self.players.pop(0) if len(self.players) > 1 else self.players[0]
        return self.player_id is not None

    def stop(self):
        pass

    def player_names(self, ids):
        return {player: "Tesoureiro" if player == TARGET else "Crash BR" for player in ids}


class Browser(FakeBrowser):
    def __init__(self, status, states=("complete",)):
        super().__init__()
        self.status = status
        self.states = list(states)

    def send(self, method, params=None, timeout=30.0):
        return {"result": {"value": self.status}}

    def evaluate(self, _expression, default=None):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


class SwitchTest(unittest.TestCase):
    def switch(self, status, players, states=("complete",)):
        self.link = Link(players)
        self.browser = Browser(status, states)
        switcher = session.ProfileSwitcher(self.browser)
        clock = {"now": 0.0}
        patches = [
            mock.patch.object(game_link, "GameLink", self.link),
            mock.patch.object(session.time, "time", lambda: clock["now"]),
            mock.patch.object(cancellation, "sleep", lambda s: clock.update(now=clock["now"] + s)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return switcher.switch_to(ProfileConfig(name="Tesoureiro"))

    def test_it_arrives_when_the_loaded_game_carries_the_id_of_the_profile(self):
        # Asked first (Crash BR), then still the old one, then the right one.
        self.assertTrue(self.switch(f"going:{TARGET}", [OTHER, OTHER, TARGET]))

    def test_the_profile_already_on_needs_no_switch(self):
        self.assertTrue(self.switch("never sent", [TARGET]))

    def test_nothing_is_counted_while_the_page_is_loading(self):
        self.assertTrue(self.switch(f"going:{TARGET}", [OTHER, TARGET], ("loading", "loading", "complete")))
        self.assertEqual([TARGET], self.link.players)

    def test_a_game_that_comes_back_on_another_profile_is_a_failure(self):
        self.assertFalse(self.switch(f"going:{TARGET}", [OTHER]))

    def test_the_login_page_is_told_it_opens_in_the_tab_itself(self):
        # Without this the page calls a parent that does not exist and the tab stays blank.
        self.assertIn('"&state=switchAccount"', session._SWITCH_JS)

    def test_a_refusal_from_the_page_is_not_waited_on(self):
        for status in ("profile_not_found", "profile_ambiguous", "no_auth", "rpc_error: 1 expired", None):
            self.assertFalse(self.switch(status, [OTHER]), status)


if __name__ == "__main__":
    unittest.main()
