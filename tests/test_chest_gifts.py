"""The clan gifts as the game lists them, the requests that reach it, and a clan collected through them.

The packets are built by hand in the shapes measured on the real game; the game
itself is a small stand-in that answers the way the real one does.
"""
import base64
import time
import unittest
from unittest import mock

from modules.chests import from_game, game_link
from modules.chests.from_game import GameCollection
from modules.chests.game_link import GameLink, GameLinkError, build_request, parse_request, request_bytes
from modules.chests.gifts import (CLAN_ROUTE, GIFT_LIFETIME_SECONDS, OPEN_ROUTE, Gift, parse_clan_gifts, parse_gift_row)
from modules.events import tbcodec

ANDRA, PIPO, KALEL = 901943467017, 489626671378, 4496830759598
PLAYERS = {ANDRA: "AndraBianca", PIPO: "Pipolebon", KALEL: "KALEL"}
BONE, SCORPION, BRIAREUS = 5512, 5782, 702062
SOON = int(time.time()) + 17 * 3600
GIFT_EVENT = 300020
CLAN = 936302870583
ME = 485331363759
AUTH = [[ME], b"session-0001"]


def row(gift_id, player=ANDRA, chest_type=BONE, quantity=1, expires=SOON, kind=1):
    return [gift_id, [player], chest_type, expires, quantity, kind]


def clan_record(rows, members=True):
    """The clan record as route 15000 sends it: with members when asked for the gifts."""
    roster = [[[ANDRA], 3, 1790833148], [[PIPO], 5, 1790827946]] if members else []
    node = [[CLAN], ["LPL", "LION PRIDE LEGION"], roster, [], {}, [], 100, 1, b"x", 2, b"y", 3, {}, rows, False]
    return [[node]]


def gift_event(*rows):
    return ([b"session-0001", 181738, 1791734216, GIFT_EVENT], list(rows))


def linked(answer):
    """A GameLink that has seen the game's requests and gets `answer` to whatever it sends."""
    link = GameLink()
    link._learn("https://game/rubens-realm", build_request(318, 40, AUTH, [], b"cursors"))
    link.sent = []
    link._fetch = lambda body, timeout: link.sent.append(body) or answer
    return link


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

    def test_a_record_without_members_is_not_an_empty_list(self):
        self.assertIsNone(parse_clan_gifts(clan_record([], members=False)[0][0]))
        self.assertEqual([], parse_clan_gifts(clan_record([])[0][0]))
        self.assertEqual([31956, 31957], [g.gift_id for g in parse_clan_gifts(clan_record([row(31957), row(31956)])[0][0])])


class Requests(unittest.TestCase):
    def test_a_request_is_built_the_way_the_game_builds_it(self):
        data = build_request(OPEN_ROUTE, 1336, AUTH, [{123596: 1}], b"cursors")

        self.assertEqual((OPEN_ROUTE, 1336, AUTH, b"cursors"), parse_request(data))
        unpacker = tbcodec.LEUnpacker(data, 8)
        unpacker.read()
        self.assertEqual({"123596": 1}, unpacker.read()[0])

    def test_chrome_reports_the_body_in_either_form(self):
        data = build_request(318, 7, AUTH, [], b"cursors")
        self.assertEqual(7, request_bytes(data)[1])
        self.assertEqual(7, request_bytes(base64.b64encode(data))[1])
        self.assertIsNone(request_bytes(b"not a request"))

    def test_what_is_learned_from_the_game_is_what_goes_out(self):
        link = linked(tbcodec.build_frame(OPEN_ROUTE, 45, [[{"1071": 1}]]))
        # A batch moves the count on and has no tail of its own to take.
        link._learn("https://game/rubens-realm", build_request(2, 44, AUTH, [], b""))

        self.assertTrue(link.learned.is_set())
        self.assertEqual(ME, link.player_id)
        link.open_gift(Gift(123593, 1, 2, 3, quantity=4))

        self.assertEqual((OPEN_ROUTE, 45, AUTH, b"cursors"), parse_request(link.sent[0]))
        self.assertIn(tbcodec.pack({123593: 4}), link.sent[0])

    def test_a_refusal_is_an_error_and_does_not_show_the_session(self):
        link = linked(tbcodec.build_frame(game_link.REFUSED_ROUTE, 0, [-2, "no"]))
        with self.assertRaises(GameLinkError) as caught:
            link.call(OPEN_ROUTE, [{1: 1}])
        self.assertNotIn("session", str(caught.exception))

    def test_an_answer_to_something_else_is_not_an_answer(self):
        with self.assertRaises(GameLinkError):
            linked(tbcodec.build_frame(318, 2, [0])).call(OPEN_ROUTE, [{1: 1}])

    def test_the_clan_comes_from_the_players_own_profile(self):
        mine = [[ME], "tb:1", "Crash", "BR", 3, 0, 0, 25, 45, 14, 100, [CLAN]]
        link = linked(tbcodec.build_frame(402, 2, [[mine]]))
        self.assertEqual(CLAN, link.clan_id())
        self.assertIn(tbcodec.pack([[[ME]]]), link.sent[0])

    def test_names_are_asked_a_clan_at_a_time(self):
        profile = [[ANDRA], "tb:1", "AndraBianca", "BR", 3, 0, 0, 25, 45, 14, 100, [CLAN]]
        link = linked(tbcodec.build_frame(402, 2, [[profile]]))

        self.assertEqual({ANDRA: "AndraBianca"}, link.player_names(list(range(250))))

        self.assertEqual(3, len(link.sent))

    def test_the_list_comes_from_the_clan_record(self):
        link = linked(tbcodec.build_frame(CLAN_ROUTE, 2, clan_record([row(11), row(10)])))
        self.assertEqual([10, 11], [gift.gift_id for gift in link.gifts(CLAN)])
        self.assertIn(tbcodec.pack([[[[CLAN], 58]]]), link.sent[0])

    def test_a_list_that_did_not_come_is_an_error(self):
        link = linked(tbcodec.build_frame(CLAN_ROUTE, 2, clan_record([], members=False)))
        with self.assertRaises(GameLinkError):
            link.gifts(CLAN)


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
        self.in_review = set()
        self.learned = {}
        self.existing = dict(existing or {})
        self.last_id = None
        self.refuse = False
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
        if self.refuse:
            return False
        self._next += 1
        self.last_id = self._next
        self.rows[self._next] = (name, player, source, collected_at, game["game_gift_id"])
        return True

    def insert_incomplete_chest(self, name, player, source, screenshot=None, collected_at=None, game=None):
        self.incomplete.append({"name": name, "player": player, "source": source, "gift": game["game_gift_id"],
                                "type": game["game_type_id"], "picture": screenshot})
        return True

    def types_in_review(self):
        return set(self.in_review)

    def delete_chest(self, row_id):
        return self.rows.pop(row_id, None) is not None

    def gifts_written(self):
        return sorted(gift_id for *_rest, gift_id in self.rows.values())


class Game:
    """The game as GameLink shows it: a list of gifts, and gifts opened on request."""

    def __init__(self, rows):
        self.waiting = {gift.gift_id: gift for gift in map(parse_gift_row, rows)}
        self.opened = []
        self.refused = set()         # gifts refused and left in place
        self.lost = set()            # gifts opened whose answer never came
        self.arrivals = []           # rows that appear after the next gift is opened
        self.lists_left = None       # how many more lists it answers
        self.names = {}              # the players it knows beyond the site's member list
        self.pictures = []           # the places of the list the panel was pictured at
        self.camera = lambda position: b"png"
        self.log = []

    def player_names(self, player_ids):
        return {player: self.names[player] for player in player_ids if player in self.names}

    def take_picture(self, position):
        self.pictures.append(position)
        return self.camera(position)

    def gifts(self, _clan_id):
        if self.lists_left is not None:
            if self.lists_left == 0:
                raise GameLinkError("the game's server did not answer in time")
            self.lists_left -= 1
        return sorted(self.waiting.values(), key=lambda gift: gift.gift_id)

    def open_gift(self, gift):
        if gift.gift_id in self.refused:
            raise GameLinkError("refused")
        del self.waiting[gift.gift_id]
        self.opened.append(gift.gift_id)
        for new in self.arrivals:
            parsed = parse_gift_row(new)
            self.waiting[parsed.gift_id] = parsed
        self.arrivals = []
        if gift.gift_id in self.lost:
            raise GameLinkError("the game's server did not answer in time")


class Collection(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(from_game, "FAILURE_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def collect(self, game, repo):
        return GameCollection("Main / Alpha", repo, game, CLAN, 0, game.take_picture).run()

    def test_every_gift_is_written_and_opened_oldest_first(self):
        game = Game([row(10), row(11, PIPO, SCORPION), row(12, KALEL, BRIAREUS, quantity=3), row(13)])
        repo = Repo()

        self.assertEqual((6, 0, True), self.collect(game, repo))

        self.assertEqual([10, 11, 12, 12, 12, 13], repo.gifts_written())
        self.assertEqual([10, 11, 12, 13], game.opened)
        briareus = [r for r in repo.rows.values() if r[4] == 12]
        self.assertEqual({("Briareus Chest", "KALEL", "Epic Briareus squad")}, {r[:3] for r in briareus})
        self.assertEqual(SOON - GIFT_LIFETIME_SECONDS, int(briareus[0][3].timestamp()))

    def test_a_gift_is_written_before_it_is_opened(self):
        game = Game([row(10)])
        repo = Repo()
        seen = []
        opening = game.open_gift
        game.open_gift = lambda gift: (seen.append(repo.gifts_written()), opening(gift))

        self.collect(game, repo)

        self.assertEqual([[10]], seen)

    def test_no_gifts_is_a_finished_run(self):
        self.assertEqual((0, 0, True), self.collect(Game([]), Repo()))

    def test_requests_start_at_least_the_interval_apart(self):
        game = Game([row(10), row(11), row(12)])
        started = time.time()
        GameCollection("Main / Alpha", Repo(), game, CLAN, 0.05, game.take_picture).run()
        self.assertGreaterEqual(time.time() - started, 0.14)

    def test_a_gift_already_in_the_database_is_opened_and_not_written_again(self):
        game = Game([row(10), row(11, KALEL, BRIAREUS, quantity=2), row(12)])
        repo = Repo(existing={11: 2})

        self.assertEqual((2, 0, True), self.collect(game, repo))

        self.assertEqual([10, 12], repo.gifts_written())
        self.assertEqual([10, 11, 12], game.opened)

    def test_a_gift_arriving_during_the_run_is_collected(self):
        game = Game([row(10), row(11)])
        game.arrivals = [row(12, PIPO, SCORPION)]
        repo = Repo()

        self.assertEqual((3, 0, True), self.collect(game, repo))

        self.assertEqual([10, 11, 12], repo.gifts_written())

    def test_an_expired_gift_is_neither_written_nor_opened(self):
        game = Game([row(10, expires=int(time.time()) - 60), row(11)])
        repo = Repo()

        self.assertEqual((1, 0, True), self.collect(game, repo))

        self.assertEqual([11], game.opened)
        self.assertEqual([11], repo.gifts_written())

    def test_a_gift_of_an_unknown_kind_is_left_in_the_game(self):
        game = Game([row(10, kind=2), row(11)])

        self.assertEqual((1, 0, True), self.collect(game, Repo()))

        self.assertEqual([11], game.opened)
        self.assertEqual([10], list(game.waiting))

    def test_a_player_the_site_does_not_know_is_named_by_the_game(self):
        game = Game([row(10, player=777)])
        game.names = {777: "Newcomer"}
        repo = Repo()

        self.assertEqual((1, 0, True), self.collect(game, repo))

        self.assertEqual([("Bone Chest", "Newcomer", "Level 25 Crypt")], [r[:3] for r in repo.rows.values()])

    def test_a_new_type_is_pictured_after_the_known_ones_and_goes_to_the_queue(self):
        game = Game([row(10, chest_type=9999), row(11), row(12, PIPO, chest_type=9999, quantity=2), row(13)])
        repo = Repo()

        self.assertEqual((2, 3, True), self.collect(game, repo))

        self.assertEqual([11, 13, 10, 12], game.opened)
        self.assertEqual([0], game.pictures)
        self.assertEqual([(10, b"png"), (12, None), (12, None)], [(e["gift"], e["picture"]) for e in repo.incomplete])
        self.assertEqual({9999}, {e["type"] for e in repo.incomplete})
        self.assertEqual(["AndraBianca", "Pipolebon", "Pipolebon"], [e["player"] for e in repo.incomplete])
        self.assertEqual({""}, {e["name"] for e in repo.incomplete})

    def test_the_picture_is_of_the_chest_at_its_place_in_the_list(self):
        game = Game([row(10, quantity=3, chest_type=9998), row(11, chest_type=9999)])
        repo = Repo()
        repo.in_review = {9998}

        self.collect(game, repo)

        self.assertEqual([0], game.pictures)   # the first type was opened, so the second is first

    def test_each_new_type_is_pictured_in_its_turn(self):
        game = Game([row(10, chest_type=9998), row(11, chest_type=9999)])
        repo = Repo()

        self.assertEqual((0, 2, True), self.collect(game, repo))

        self.assertEqual([b"png", b"png"], [e["picture"] for e in repo.incomplete])
        self.assertEqual([0, 0], game.pictures)

    def test_a_type_pictured_when_the_passes_ran_out_is_still_opened_with_its_picture(self):
        game = Game([row(10), row(11, chest_type=9999)])
        repo = Repo()

        with mock.patch.object(from_game, "MAX_PASSES", 1):
            self.assertEqual((1, 1, True), self.collect(game, repo))

        self.assertEqual([10, 11], game.opened)
        self.assertEqual([(11, b"png")], [(e["gift"], e["picture"]) for e in repo.incomplete])

    def test_a_type_with_a_picture_waiting_on_the_site_needs_no_other(self):
        game = Game([row(10, chest_type=9999)])
        repo = Repo()
        repo.in_review = {9999}

        self.assertEqual((0, 1, True), self.collect(game, repo))

        self.assertEqual([], game.pictures)
        self.assertEqual([None], [e["picture"] for e in repo.incomplete])

    def test_a_panel_that_fails_leaves_the_rest_collected(self):
        game = Game([row(10, chest_type=9999), row(11)])

        def broken(_position):
            raise RuntimeError("no browser")
        game.camera = broken
        repo = Repo()

        self.assertEqual((1, 0, False), self.collect(game, repo))
        self.assertEqual([11], game.opened)
        self.assertEqual([10], list(game.waiting))

    def test_a_gift_about_to_expire_goes_to_the_review_queue_rather_than_be_lost(self):
        game = Game([row(10, chest_type=9999, expires=int(time.time()) + 600), row(11, chest_type=9999)])
        game.camera = lambda position: None
        repo = Repo()

        collected, incomplete, _finished = self.collect(game, repo)

        self.assertEqual((0, 1), (collected, incomplete))
        self.assertEqual([10], game.opened)
        self.assertEqual([11], list(game.waiting))
        self.assertEqual("AndraBianca", repo.incomplete[0]["player"])
        self.assertIsNone(repo.incomplete[0]["picture"])

    def test_a_player_nobody_knows_goes_to_the_review_queue_and_is_opened(self):
        game = Game([row(11, player=777)])
        repo = Repo()

        self.assertEqual((0, 1, True), self.collect(game, repo))

        self.assertEqual([11], game.opened)
        self.assertEqual(("Bone Chest", "", "Level 25 Crypt"), tuple(repo.incomplete[0][k] for k in ("name", "player", "source")))

    def test_rows_the_database_refuses_go_to_the_review_queue(self):
        game = Game([row(10)])
        repo = Repo()
        repo.refuse = True

        self.assertEqual((0, 1, True), self.collect(game, repo))

        self.assertEqual(1, len(repo.incomplete))
        self.assertEqual([10], game.opened)

    def test_a_refused_gift_is_taken_back_and_the_others_go_on(self):
        game = Game([row(10), row(11), row(12)])
        game.refused = {11}
        repo = Repo()

        collected, _incomplete, _finished = self.collect(game, repo)

        self.assertEqual(2, collected)
        self.assertEqual([10, 12], repo.gifts_written())
        self.assertEqual([11], list(game.waiting))

    def test_a_gift_whose_answer_was_lost_stays_written(self):
        game = Game([row(10), row(11)])
        game.lost = {10}
        repo = Repo()

        self.assertEqual((2, 0, True), self.collect(game, repo))

        self.assertEqual([10, 11], repo.gifts_written())

    def test_refusals_in_a_row_end_the_run_and_what_was_written_is_taken_back(self):
        game = Game([row(gift_id) for gift_id in range(10, 20)])
        game.refused = set(game.waiting)
        repo = Repo()

        self.assertEqual((0, 0, False), self.collect(game, repo))

        self.assertEqual([], repo.gifts_written())

    def test_losing_the_game_mid_run_keeps_what_was_written_for_the_next_run(self):
        game = Game([row(10), row(11)])
        game.lists_left = 1
        repo = Repo()

        collected, _incomplete, finished = self.collect(game, repo)

        self.assertFalse(finished)
        self.assertEqual(2, collected)
        self.assertEqual([10, 11], repo.gifts_written())


if __name__ == "__main__":
    unittest.main()
