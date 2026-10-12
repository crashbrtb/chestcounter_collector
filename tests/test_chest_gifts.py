"""The clan gifts read from the game's traffic, and a tab collected from them.

The packets are built by hand in the shapes found on the recording of
11/10/2026; the game itself is a small stand-in that opens a chest per click
and answers the way the real one does.
"""
import base64
import time
import unittest
from unittest import mock

from modules.chests import from_game
from modules.chests.from_game import GameCollection
from modules.chests.gifts import (CLAN_ROUTE, GIFT_EVENT, GIFT_LIFETIME_SECONDS, OPEN_ROUTE, Gift, GiftFeed,
                                  chests_of, parse_clan_gifts, parse_gift_row)
from modules.events import tbcodec

ANDRA, PIPO, KALEL = 901943467017, 489626671378, 4496830759598
BONE, SCORPION, BRIAREUS = 5512, 5782, 702062
SOON = int(time.time()) + 17 * 3600


def row(gift_id, player=ANDRA, chest_type=BONE, quantity=1, expires=SOON, kind=1):
    return [gift_id, [player], chest_type, expires, quantity, kind]


def clan_record(rows, members=True):
    """The clan record as route 15000 sends it: with members when the window was just opened."""
    roster = [[[ANDRA], 3, 1790833148], [[PIPO], 5, 1790827946]] if members else []
    node = [[936302870583], ["LPL", "LION PRIDE LEGION"], roster, [], {}, [], 100, 1, b"x", 2, b"y", 3, {}, rows, False]
    return [[node]]


def record(*frames):
    return {"kind": "RESP", "b64": base64.b64encode(b"".join(frames)).decode("ascii")}


def gift_event(*rows):
    return ([b"session-0001", 181738, 1791734216, GIFT_EVENT], list(rows))


class TailEvents(unittest.TestCase):
    def test_every_event_of_a_tail_is_read_with_what_it_carries(self):
        other = ([b"session-0001", 7, 1791734216, 100002], [[1, 2, 3]])
        frame = tbcodec.build_frame(318, 1, [0], tail_events=(other, gift_event(row(32689)), other))
        tail = next(tbcodec.iter_frames(frame)).tail

        events = tbcodec.tail_events(tail)

        self.assertEqual([100002, GIFT_EVENT, 100002], [event[0][3] for event in events])
        self.assertEqual([row(32689)], events[1][1])
        self.assertEqual([100002, GIFT_EVENT, 100002], [header[3] for header in tbcodec.decode_tail(tail)])

    def test_a_length_that_does_not_close_ends_the_reading(self):
        self.assertEqual([], tbcodec.tail_events(b"\x40\x00\x00\x00\x91\x01"))


class Parsing(unittest.TestCase):
    def test_a_gift_row(self):
        gift = parse_gift_row(row(32001, KALEL, BRIAREUS, quantity=3))
        self.assertEqual(Gift(32001, KALEL, BRIAREUS, SOON, 3, 1), gift)
        self.assertEqual(SOON - GIFT_LIFETIME_SECONDS, int(gift.generated_at.timestamp()))
        self.assertIsNone(parse_gift_row([32001, KALEL, BRIAREUS, SOON, 3, 1]))
        self.assertIsNone(parse_gift_row(row(32001)[:5]))

    def test_the_record_repeated_while_the_window_is_open_is_not_an_empty_list(self):
        self.assertIsNone(parse_clan_gifts(clan_record([], members=False)[0][0]))
        self.assertEqual([], parse_clan_gifts(clan_record([])[0][0]))
        self.assertEqual([31956, 31957], [g.gift_id for g in parse_clan_gifts(clan_record([row(31957), row(31956)])[0][0])])

    def test_a_gift_of_several_chests_is_that_many_lines(self):
        gifts = [parse_gift_row(row(1)), parse_gift_row(row(2, quantity=3))]
        self.assertEqual([1, 2, 2, 2], [gift.gift_id for gift in chests_of(gifts)])


class Feed(unittest.TestCase):
    def test_list_arrivals_and_answers(self):
        feed = GiftFeed()
        feed.feed_record(record(tbcodec.build_frame(CLAN_ROUTE, 1, clan_record([row(10), row(11, quantity=2)]))))
        self.assertEqual((1, 0), feed.state())

        # The repeated record changes nothing; a gift arrives on the tail of an unrelated answer.
        feed.feed_record(record(tbcodec.build_frame(CLAN_ROUTE, 2, clan_record([], members=False))))
        feed.feed_record(record(tbcodec.build_frame(318, 3, [0], tail_events=(gift_event(row(12, PIPO, SCORPION)),))))
        feed.feed_record(record(tbcodec.build_frame(OPEN_ROUTE, 4, [[{"1117": 1}]]),
                                tbcodec.build_frame(OPEN_ROUTE, 5, [[{"515": 1}]])))

        self.assertEqual((1, 2), feed.state())
        self.assertEqual([10, 11, 12], [gift.gift_id for gift in feed.snapshot()])

        # A new list replaces what was known and starts the count again.
        feed.feed_record(record(tbcodec.build_frame(CLAN_ROUTE, 6, clan_record([row(12, PIPO, SCORPION)]))))
        self.assertEqual((2, 0), feed.state())
        self.assertEqual([12], [gift.gift_id for gift in feed.snapshot()])

    def test_what_is_not_an_answer_of_the_game_is_ignored(self):
        feed = GiftFeed()
        self.assertFalse(feed.feed_record({"kind": "WS_IN", "b64": "AAAA"}))
        self.assertFalse(feed.feed_record({"kind": "RESP", "b64": "not base64"}))
        self.assertEqual((0, 0), feed.state())


# ----------------------------------------------------------------- the stand-ins
class Repo:
    def __init__(self, types=None, players=None, existing=None):
        self.types = dict(types if types is not None else {BONE: ("Bone Chest", "Level 25 Crypt"),
                                                           SCORPION: ("Scorpion Chest", "Level 35 epic Crypt"),
                                                           BRIAREUS: ("Briareus Chest", "Epic Briareus squad")})
        self.players = dict(players if players is not None else {ANDRA: "AndraBianca", PIPO: "Pipolebon",
                                                                 KALEL: "KALEL"})
        self.rows = {}            # row id -> (name, player, source, generated_at, gift id)
        self.incomplete = []
        self.learned = {}
        self.existing = dict(existing or {})
        self.last_id = None
        self._next = 100

    def chest_types(self):
        return dict(self.types)

    def game_players(self):
        return dict(self.players)

    def chests_of_gifts(self, first, last):
        counts = {g: n for g, n in self.existing.items() if first <= g <= last}
        for _name, _player, _source, _when, gift_id in self.rows.values():
            if first <= gift_id <= last:
                counts[gift_id] = counts.get(gift_id, 0) + 1
        for entry in self.incomplete:
            counts[entry["gift"]] = counts.get(entry["gift"], 0) + 1
        return counts

    def insert_chest(self, name, player, source, collected_at=None, game=None):
        self._next += 1
        self.last_id = self._next
        self.rows[self._next] = (name, player, source, collected_at, game["game_gift_id"])
        return True

    def insert_incomplete_chest(self, name, player, source, screenshot=None, collected_at=None, game=None):
        self.incomplete.append({"name": name, "player": player, "source": source, "gift": game["game_gift_id"]})
        return True

    def learn_chest_type(self, type_id, name, source):
        self.learned[type_id] = (name, source)
        return True

    def delete_chest(self, row_id):
        return self.rows.pop(row_id, None) is not None

    def gifts_written(self):
        return sorted(gift_id for *_rest, gift_id in self.rows.values())


class Game:
    """The game: a list of gifts, a panel showing four of their chests, an answer per chest opened."""

    def __init__(self, rows, hidden=()):
        self.feed = GiftFeed()
        self.waiting = [parse_gift_row(r) for r in rows]
        self.hidden = set(hidden)          # gifts the panel does not show until the window is reopened
        self.answers = True
        self.sends_list = True
        self.reopened = 0
        self.reading = None                # rows the screen shows for every chest, when it is read
        self.minutes_left = None
        self._send_list()

    def _send_list(self):
        with self.feed.lock:
            self.feed.gifts = {gift.gift_id: gift for gift in self.waiting}
            self.feed.lists += 1
            self.feed.opened = 0

    def panel(self):
        return [gift for gift in chests_of(self.waiting) if gift.gift_id not in self.hidden]

    def arrive(self, gift_row, hidden=False):
        gift = parse_gift_row(gift_row)
        self.waiting.append(gift)
        if hidden:
            self.hidden.add(gift.gift_id)
        with self.feed.lock:
            self.feed.gifts[gift.gift_id] = gift

    def open_first(self):
        """A click on any 'Open' button of the panel: the chest under it is gone."""
        panel = self.panel()
        if not panel:
            return
        gift = panel[0]
        index = self.waiting.index(gift)
        if gift.quantity > 1:
            self.waiting[index] = Gift(gift.gift_id, gift.player_id, gift.chest_type, gift.expires_at,
                                       gift.quantity - 1, gift.kind)
        else:
            del self.waiting[index]
        if self.answers:
            with self.feed.lock:
                self.feed.opened += 1

    def reopen(self):
        self.reopened += 1
        self.hidden.clear()
        if self.sends_list:
            self._send_list()
        return True


class Module:
    """What GameCollection asks of the chest module."""

    profile_label = "Main / Alpha"
    vocabulary = None

    def __init__(self, game):
        self.game = game
        self.browser = self
        self.config = self
        self._crops = []
        self._minutes_left = []
        self.screen_reads = 0

    def section(self, _name):
        return {"chest_click_delay": 0}

    def click(self, _x, _y, delay=0.0):
        self.game.open_first()

    def _visible_chests(self):
        return [{"button": (0, 30 * slot, 10, 10)} for slot in range(min(4, len(self.game.panel())))]

    def _look_again(self, _label):
        return []

    def _read_resolved(self, chests, _scale):
        self.screen_reads += 1
        self._crops = [None] * len(chests)
        self._minutes_left = [self.game.minutes_left] * len(chests)
        return [list(self.game.reading or []) for _ in chests]

    @staticmethod
    def _encode(_crop):
        return None


class Collection(unittest.TestCase):
    def setUp(self):
        for name in ("CONFIRM_WAIT", "SCREEN_SETTLE_WAIT"):
            patcher = mock.patch.object(from_game, name, 0.05)
            patcher.start()
            self.addCleanup(patcher.stop)

    def collect(self, game, repo):
        module = Module(game)
        collection = GameCollection(module, repo, game.feed, "Gifts", game.reopen, answer_wait=0.2)
        return collection.run(), module

    def test_every_chest_is_written_from_the_list_without_reading_the_screen(self):
        game = Game([row(10), row(11, PIPO, SCORPION), row(12, KALEL, BRIAREUS, quantity=3), row(13), row(14)])
        repo = Repo()

        (collected, incomplete, finished), module = self.collect(game, repo)

        self.assertEqual((7, 0, True), (collected, incomplete, finished))
        self.assertEqual([10, 11, 12, 12, 12, 13, 14], repo.gifts_written())
        self.assertEqual(0, module.screen_reads)
        self.assertEqual([], game.waiting)
        briareus = [r for r in repo.rows.values() if r[4] == 12]
        self.assertEqual({("Briareus Chest", "KALEL", "Epic Briareus squad")}, {r[:3] for r in briareus})
        self.assertEqual(SOON - GIFT_LIFETIME_SECONDS, int(briareus[0][3].timestamp()))

    def test_a_gift_the_panel_did_not_show_is_taken_back_and_collected_once_it_shows(self):
        # Gift 11 is listed and not on the panel: it is written when it comes up in
        # the list, another chest is opened in its place, and settling puts both right.
        game = Game([row(10), row(11, PIPO, SCORPION), row(12), row(13), row(14), row(15)], hidden=[11])
        repo = Repo()

        (collected, incomplete, finished), _module = self.collect(game, repo)

        self.assertEqual((6, 0, True), (collected, incomplete, finished))
        self.assertEqual([10, 11, 12, 13, 14, 15], repo.gifts_written())
        self.assertEqual([], game.waiting)

    def test_a_gift_that_never_shows_is_left_for_the_next_run_and_not_written(self):
        game = Game([row(10), row(11, PIPO, SCORPION), row(12)], hidden=[11])
        game.reopen = lambda: (game._send_list(), True)[1]      # reopening does not bring it to the panel

        (collected, _incomplete, finished), _module = self.collect(game, repo := Repo())

        self.assertTrue(finished)
        self.assertEqual(2, collected)
        self.assertEqual([10, 12], repo.gifts_written())
        self.assertEqual([11], [gift.gift_id for gift in game.waiting])

    def test_a_gift_already_in_the_database_is_opened_and_not_written_again(self):
        game = Game([row(10), row(11, KALEL, BRIAREUS, quantity=2), row(12)])
        repo = Repo(existing={11: 2})

        (collected, _incomplete, finished), _module = self.collect(game, repo)

        self.assertEqual((2, True), (collected, finished))
        self.assertEqual([10, 12], repo.gifts_written())
        self.assertEqual([], game.waiting)

    def test_a_gift_arriving_during_the_run_is_collected(self):
        game = Game([row(10), row(11)])
        game.arrive(row(12, PIPO, SCORPION))

        (collected, _incomplete, _finished), _module = self.collect(game, repo := Repo())

        self.assertEqual(3, collected)
        self.assertEqual([10, 11, 12], repo.gifts_written())

    def test_a_new_chest_type_is_named_from_the_screen_once(self):
        game = Game([row(10, chest_type=9999), row(11, chest_type=9999)])
        game.reading = ["Mimic Chest", "From: AndraBianca", "Source: Mimic Chest"]
        game.minutes_left = (SOON - int(time.time())) // 60
        repo = Repo()

        (collected, incomplete, _finished), module = self.collect(game, repo)

        self.assertEqual((2, 0), (collected, incomplete))
        self.assertEqual({9999: ("Mimic Chest", "Mimic Chest")}, repo.learned)
        self.assertEqual({("Mimic Chest", "AndraBianca", "Mimic Chest")}, {r[:3] for r in repo.rows.values()})
        self.assertEqual(1, module.screen_reads)

    def test_a_reading_whose_countdown_is_not_the_gifts_is_not_believed(self):
        game = Game([row(10, chest_type=9999)])
        game.reading = ["Bone Chest", "From: Someone", "Source: Level 25 Crypt"]
        game.minutes_left = 5        # the gift has seventeen hours left
        repo = Repo()

        (collected, incomplete, _finished), _module = self.collect(game, repo)

        self.assertEqual((0, 1), (collected, incomplete))
        self.assertEqual({}, repo.learned)
        self.assertEqual([{"name": "", "player": "AndraBianca", "source": "", "gift": 10}], repo.incomplete)

    def test_without_any_countdown_the_players_vouch_for_the_reading(self):
        # A chest area calibrated around the text alone: no countdown is ever read.
        game = Game([row(10, chest_type=9999), row(11, chest_type=9999)])
        game.reading = ["Mimic Chest", "From: andrabianca", "Source: Mimic Chest"]
        repo = Repo()

        (collected, incomplete, _finished), _module = self.collect(game, repo)

        self.assertEqual((2, 0), (collected, incomplete))
        self.assertEqual({9999: ("Mimic Chest", "Mimic Chest")}, repo.learned)
        self.assertEqual({("Mimic Chest", "AndraBianca", "Mimic Chest")}, {r[:3] for r in repo.rows.values()})

    def test_without_any_countdown_another_players_line_is_not_believed(self):
        game = Game([row(10, chest_type=9999)])
        game.reading = ["Bone Chest", "From: Pipolebon", "Source: Level 25 Crypt"]
        repo = Repo()

        (collected, incomplete, _finished), _module = self.collect(game, repo)

        self.assertEqual((0, 1), (collected, incomplete))
        self.assertEqual({}, repo.learned)

    def test_without_any_countdown_an_unknown_player_is_not_read_from_the_screen(self):
        game = Game([row(10, player=777)])
        game.reading = ["Bone Chest", "From: Newcomer", "Source: Level 25 Crypt"]
        repo = Repo()

        (collected, incomplete, _finished), _module = self.collect(game, repo)

        self.assertEqual((0, 1), (collected, incomplete))
        self.assertEqual([{"name": "Bone Chest", "player": "", "source": "Level 25 Crypt", "gift": 10}],
                         repo.incomplete)

    def test_a_player_the_site_does_not_know_is_read_from_the_screen(self):
        game = Game([row(10, player=777)])
        game.reading = ["Bone Chest", "From: Newcomer", "Source: Level 25 Crypt"]
        game.minutes_left = (SOON - int(time.time())) // 60

        (collected, _incomplete, _finished), _module = self.collect(game, repo := Repo())

        self.assertEqual(1, collected)
        self.assertEqual([("Bone Chest", "Newcomer", "Level 25 Crypt")], [r[:3] for r in repo.rows.values()])

    def test_without_the_list_again_the_screen_takes_over(self):
        game = Game([row(10), row(11)])
        game.sends_list = False

        (collected, _incomplete, finished), _module = self.collect(game, repo := Repo())

        self.assertFalse(finished)
        self.assertEqual(2, collected)
        self.assertEqual([10, 11], repo.gifts_written())

    def test_clicks_the_game_does_not_answer_end_in_asking_for_the_list(self):
        game = Game([row(10), row(11)])
        game.answers = False

        (collected, _incomplete, finished), _module = self.collect(game, repo := Repo())

        # Both were opened all the same, and the list asked for again says so.
        self.assertTrue(finished)
        self.assertEqual(2, collected)
        self.assertEqual([10, 11], repo.gifts_written())


if __name__ == "__main__":
    unittest.main()
