"""The Journal walk against a simulated game: a feed answering as the real one, and a screen for the pictures."""
import base64
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.events import journal, mapper, tbcodec  # noqa: E402
from modules.events.feed import ANSWER_KEY, build_call, parse_answer  # noqa: E402
from modules.events.screen import CardPicture  # noqa: E402
from modules.events.traffic import GameLinkError  # noqa: E402
from utils.cancel import cancellation  # noqa: E402

NOW = 1793500000
DAY = 86400


def uid(number):
    return f"11111111-0000-4000-8000-{number:012d}"


def ranking_entry(number, ended_at, key=(1024, 1), clan=1):
    """A line of the Journal for a classic result: `clan` is 1 for the clan's ranking, 0 for the player's own."""
    summary = tbcodec.pack(["global_tournament_user_result", None, list(key), clan])
    return [uid(number), ended_at, ended_at * 1000, "global_tournament_user_result", [summary], True, False, False,
            uid(number + 5000)]


def other_entry(number, ended_at, kind="battle"):
    return [uid(number), ended_at, ended_at * 1000, kind, [tbcodec.pack([kind, "x"])], True, False, False, None]


class FakeFeed:
    """journal_page, ranking and fetch_profiles as modules/events/feed.py gives them."""

    def __init__(self, entries, players=(1001, 1002), names=None, per_request=3):
        self.entries = entries
        self.players = list(players)
        self.names = {pid: f"Player {pid}" for pid in self.players} if names is None else names
        self.per_request = per_request
        self.asked_rankings = []
        self.asked_profiles = []
        self.failing = set()
        self.cut_at = None

    def journal_page(self, cursor=None, size=None):
        if self.cut_at is not None and cursor is not None and cursor <= self.cut_at:
            raise GameLinkError("the game did not answer")
        start = 0 if cursor is None else next(i for i, e in enumerate(self.entries) if e[2] == cursor) + 1
        return self.entries[start:start + (size or self.per_request)]

    def ranking(self, entry_uid, result_uid):
        self.asked_rankings.append(result_uid)
        if result_uid in self.failing:
            raise GameLinkError("the game did not answer")
        entry = next(e for e in self.entries if e[0] == entry_uid)
        key = tbcodec.unpack_stream(entry[4][0])[0][2]
        rows = [[[pid], 100 - i] for i, pid in enumerate(self.players)]
        detail = tbcodec.pack(["global_tournament_user_result", key, 1, [[1, 1, rows, [], []]]])
        return [[{result_uid: [detail, True]}]]

    def fetch_profiles(self, player_ids, collector):
        self.asked_profiles.append(list(player_ids))
        rows = [[[pid], 0, self.names[pid], "", 0, 0, 0, 0, 0, 0, 5] for pid in player_ids if pid in self.names]
        collector.feed_tree({"frames": [{"route": journal.PROFILES_ROUTE, "objects": [[rows]]}]})


def walk(feed, known=lambda _t: "", options=None, camera=lambda: CardPicture(b"page", b"icon"), has_picture=lambda _t: False):
    """(the mapper after its run, the places of the Journal pictured)."""
    pictured = []

    def take_picture(page, card):
        pictured.append((page, card))
        return camera()

    logs = []
    runner = mapper.JournalMapper(feed, journal.JournalCollector(), options or mapper.MapperOptions(interval=0),
                                  known_name=known, take_picture=take_picture, has_picture=has_picture, on_log=logs.append)
    runner.logs = logs
    with mock.patch.object(cancellation, "sleep", lambda _seconds: None):
        runner.run()
    return runner, pictured


class CallTest(unittest.TestCase):
    def test_a_call_is_built_byte_for_byte_as_the_game_builds_it(self):
        # A heartbeat recorded from the game on 01/11/2026.
        self.assertEqual("9181ce360b5577c40892cefaa920940c0090", build_call(2485168634, 12, []).hex())

    def test_an_answer_is_told_from_everything_else_on_the_socket(self):
        answer = tbcodec.pack([{int(ANSWER_KEY): tbcodec.pack([7, 0, ""])}]) + tbcodec.pack([["pong"]])
        self.assertEqual((7, 0, "", [[["pong"]]]), parse_answer(answer)[:4])
        self.assertIsNone(parse_answer(tbcodec.pack([{5: tbcodec.pack([1])}]) + tbcodec.pack([])))
        self.assertIsNone(parse_answer(b"PONG{}"))


class WalkTest(unittest.TestCase):
    def named(self, _t):
        return "Epic Hunt"

    def test_every_page_is_walked_and_only_the_clans_rankings_are_asked_for(self):
        entries = [ranking_entry(1, NOW), other_entry(2, NOW - 1), ranking_entry(3, NOW - 2, clan=0),
                   ranking_entry(4, NOW - 3), other_entry(5, NOW - 4)]
        feed = FakeFeed(entries)
        runner, read = walk(feed, known=self.named)

        self.assertEqual([uid(5001), uid(5004)], feed.asked_rankings, "not the player's own world ranking")
        self.assertEqual(mapper.FINISH_END, runner.finish_reason)
        self.assertEqual([], read, "every type is known: the screen is not touched")
        self.assertEqual({mapper.STATUS_MAPPED}, {c.status for c in runner.cards})
        self.assertEqual(["Player 1001", "Player 1002"],
                         [r.name for r in runner.collector.tournaments()[0].rows])

    def test_the_names_are_asked_for_once_and_only_those_missing(self):
        feed = FakeFeed([ranking_entry(1, NOW), ranking_entry(2, NOW - 1)], names={1001: "Lion"})
        runner, _read = walk(feed, known=self.named)

        self.assertEqual([[1001, 1002]], feed.asked_profiles)
        self.assertEqual([1, 1], [c.unnamed for c in runner.cards], "the game does not know one of them")

    def test_it_stops_at_the_last_search(self):
        entries = [ranking_entry(1, NOW), other_entry(2, NOW - 100), ranking_entry(3, NOW - 200)]
        feed = FakeFeed(entries)
        runner, _ = walk(feed, known=self.named, options=mapper.MapperOptions(interval=0, stop_before=NOW - 50))

        self.assertEqual(mapper.FINISH_CUTOFF, runner.finish_reason)
        self.assertEqual([uid(5001)], feed.asked_rankings)

    def test_it_stops_at_the_days_to_look_back(self):
        now = int(datetime.now().timestamp())
        entries = [ranking_entry(1, now), ranking_entry(2, now - 5 * DAY), ranking_entry(3, now - 6 * DAY)]
        feed = FakeFeed(entries)
        runner, _ = walk(feed, known=self.named, options=mapper.MapperOptions(interval=0, max_age_days=2))

        self.assertEqual(mapper.FINISH_DAYS, runner.finish_reason)
        self.assertEqual([uid(5001)], feed.asked_rankings)

    def test_a_ranking_that_does_not_come_is_counted_and_the_walk_goes_on(self):
        feed = FakeFeed([ranking_entry(1, NOW), ranking_entry(2, NOW - 1)])
        feed.failing = {uid(5001)}
        runner, _ = walk(feed, known=self.named)

        self.assertEqual([mapper.STATUS_NO_ANSWER, mapper.STATUS_MAPPED], [c.status for c in runner.cards])
        self.assertEqual(mapper.FINISH_END, runner.finish_reason)

    def test_a_game_that_keeps_failing_ends_the_walk_as_not_complete(self):
        feed = FakeFeed([ranking_entry(n, NOW - n) for n in range(1, 8)], per_request=10)
        feed.failing = {uid(5000 + n) for n in range(1, 8)}
        runner, _ = walk(feed, known=self.named)

        self.assertEqual(mapper.MAX_FAILURES, len(feed.asked_rankings))
        self.assertNotIn(runner.finish_reason, mapper.COMPLETE_REASONS)

    def test_a_list_that_stops_coming_is_not_a_complete_walk(self):
        entries = [ranking_entry(n, NOW - n) for n in range(1, 6)]
        feed = FakeFeed(entries)
        feed.cut_at = entries[2][2]
        runner, _ = walk(feed, known=self.named)

        self.assertEqual(mapper.FINISH_NO_JOURNAL, runner.finish_reason)
        self.assertEqual(3, len(runner.cards), "what was read before stays")

    def test_a_kind_nobody_reads_is_pointed_out_once(self):
        feed = FakeFeed([other_entry(1, NOW, "new_clan_war_statistics"), other_entry(2, NOW - 1, "new_clan_war_statistics")])
        runner, _ = walk(feed)
        self.assertEqual(1, sum("new_clan_war_statistics" in text for text in runner.logs))

    def test_an_answer_in_an_unknown_shape_says_so(self):
        feed = FakeFeed([ranking_entry(1, NOW)])
        detail = tbcodec.pack(["multistage_tournament_results_entry", [1040, 1], [[1, 2, 3]]])
        feed.ranking = lambda _entry, result: [[{result: [detail, True]}]]
        runner, _ = walk(feed, known=self.named)
        self.assertIn(mapper.STATUS_UNKNOWN_FORMAT, runner.cards[0].status)


class NewTypeTest(unittest.TestCase):
    """A type the catalogue has no name for is pictured where its card is, and its rankings wait for a name."""

    def setUp(self):
        self.options = mapper.MapperOptions(interval=0, cards_per_page=2)
        self.entries = [other_entry(1, NOW), other_entry(2, NOW - 1), other_entry(3, NOW - 2),
                        ranking_entry(4, NOW - 3, (1040, 1)), ranking_entry(5, NOW - 4, (1040, 2)),
                        ranking_entry(6, NOW - 5, (1050, 1))]

    def known_but(self, *unknown):
        return lambda t: "" if t.tournament_key.split(":")[0] in unknown else "Known"

    def test_the_card_of_a_new_type_is_pictured_once_and_all_of_it_waits(self):
        runner, pictured = walk(FakeFeed(self.entries), self.known_but("1040"), self.options)

        self.assertEqual([(1, 1)], pictured, "the 4th entry is the 2nd card of the 2nd page")
        self.assertEqual([mapper.STATUS_NO_NAME, mapper.STATUS_NO_NAME, mapper.STATUS_MAPPED],
                         [c.status for c in runner.cards])
        [entry] = mapper.catalogue_entries(runner.cards)
        self.assertEqual("1040:1", entry["tournament_key"])
        self.assertNotIn("name", entry)
        self.assertEqual((base64.b64encode(b"page").decode(), base64.b64encode(b"icon").decode()),
                         (entry["picture"], entry["image"]))

    def test_each_new_type_is_pictured_on_its_own(self):
        runner, pictured = walk(FakeFeed(self.entries), self.known_but("1040", "1050"), self.options)

        self.assertEqual([(1, 1), (2, 1)], pictured)
        self.assertEqual(["1040:1", "1050:1"], [e["tournament_key"] for e in mapper.catalogue_entries(runner.cards)])

    def test_a_type_whose_picture_is_on_the_site_needs_no_other(self):
        runner, pictured = walk(FakeFeed(self.entries), self.known_but("1040"), self.options, has_picture=lambda _t: True)

        self.assertEqual([], pictured)
        self.assertEqual(mapper.STATUS_NO_NAME, runner.cards[0].status)
        self.assertNotIn("picture", mapper.catalogue_entries(runner.cards)[0])

    def test_a_screen_that_could_not_look_still_tells_the_site_and_is_tried_twice(self):
        runner, pictured = walk(FakeFeed(self.entries), self.known_but("1040"), self.options, camera=lambda: None)

        self.assertEqual([(1, 1), (1, 1)], pictured)
        self.assertEqual(mapper.STATUS_NO_NAME, runner.cards[0].status)
        [entry] = mapper.catalogue_entries(runner.cards)
        self.assertNotIn("picture", entry)

    def test_a_new_entry_moves_the_places_so_nothing_is_pictured(self):
        feed = FakeFeed(self.entries)
        real = feed.journal_page

        def page(cursor=None, size=None):
            return [other_entry(99, NOW + 1)] if size == 1 else real(cursor, size)

        feed.journal_page = page
        runner, pictured = walk(feed, self.known_but("1040"), self.options)

        self.assertEqual([], pictured)
        self.assertEqual(mapper.STATUS_NO_NAME, runner.cards[0].status)


if __name__ == "__main__":
    unittest.main()
