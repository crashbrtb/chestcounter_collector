"""The Journal mapper against a simulated game.

The fake game is a small state machine: the list of cards, an open card, the
ranking. Clicks at the calibrated points move it between those screens, and
opening a ranking makes it "send" the ranking message the way the real game
does, through the same JournalCollector the mapper listens to.
"""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.calibration import SHARED_GROUP, Calibration, StepGroup  # noqa: E402
from modules.events import journal, mapper, tbcodec, titles  # noqa: E402
from modules.events.layout import STEPS, JournalLayout  # noqa: E402
from test_events_journal import http_record, ws_record  # noqa: E402

PITCH = 90


def ranking_record(uid, key, player_ids=(1001, 1002)):
    rows = [[[pid], 100 - i] for i, pid in enumerate(player_ids)]
    inner = tbcodec.pack(["global_tournament_user_result", list(key), 1, [[1, 1, rows, [], []]]])
    return ws_record([[{"1": b"\x93\x06\x00\xa0"}], [{uid: [inner, True]}]], idx=7)


def unknown_record(uid, kind="multistage_tournament_results_entry"):
    """A detail the game answers in a shape the reader does not know."""
    inner = tbcodec.pack([kind, [1040, 1], [[1, 2, 3]]])
    return ws_record([[{"1": b"\x93\x06\x00\xa0"}], [{uid: [inner, True]}]], idx=9)


def make_calibration(path):
    """The Journal as the wizard would leave it, wrapped in the layout the mapper reads."""
    groups = [SHARED_GROUP, StepGroup("events", "Tournament rankings", list(STEPS))]
    cal = Calibration(groups, path, os.path.join(os.path.dirname(path), "refs"))
    cal.set_viewport(1000, 800)
    cal.set_point("journal_button", 50, 750)
    cal.set_region("journal_card1_title", (300, 100), (800, 130))
    cal.set_region("journal_card2_title", (300, 100 + PITCH), (800, 130 + PITCH))
    cal.set_region("journal_card_icon", (220, 95), (280, 150))
    cal.set_point("journal_next_page", 700, 700)
    cal.set_point("journal_card_open", 500, 115)
    screen = np.random.default_rng(1).integers(0, 255, (800, 1000, 3), dtype=np.uint8)
    cal.set_region("journal_show_details", (600, 600), (760, 640), screen)
    cal.set_point("journal_details_close", 900, 60)
    cal.set_point("journal_card_close", 950, 40)
    cal.use_viewport(1000, 800)
    return JournalLayout(cal, cards_per_page=3)


class FakeGame:
    """Plays the browser: captures return which text is on screen, clicks move screens."""

    def __init__(self, calibration, pages, answers, collector):
        self.cal = calibration
        self.pages = pages          # list of pages, each a list of card titles
        self.answers = answers      # tournament name -> (result uid, (type, variant)), None when cached
        self.collector = collector
        self.page = 0
        self.view = "list"
        self.open_card = None
        self.texts = []
        self.clicks = []
        self.stuck_on_close = False
        self.visible_rows = 2        # players on screen when the ranking opens
        self.scrolls = 0
        self.list_end = 100          # scrolls before the ranking list stops moving
        self.profiles_on_scroll = True
        self.unknown_kind = None    # the game answers in a shape the reader does not know

    # browser API used by the mapper
    connected = True

    def viewport(self):
        return 1000, 800

    def focus_tab(self):
        pass

    def _image(self, text):
        self.texts.append(text)
        return np.zeros((4, len(self.texts), 3), dtype=np.uint8)

    def capture(self, region=None, scale=1.0):
        for i in range(self.cal.cards_per_page):
            if region == self.cal.card_title_region(i):
                visible = self.view == "list" and i < len(self.pages[self.page])
                return self._image(self.pages[self.page][i] if visible else "")
        if region == self.cal.card_icon_region(0) or region in [self.cal.card_icon_region(i) for i in range(3)]:
            return np.full((20, 20, 3), 90, dtype=np.uint8)
        if region == self.cal.region("journal_ranking_list"):
            # The same picture until a scroll moves the list, and none past its end.
            return np.full((5, 5, 3), 30 * min(self.scrolls, self.list_end) % 256, dtype=np.uint8)
        return self._image(f"screen {self.page} {self.view}")

    def click(self, x, y, delay=0.0):
        self.clicks.append((x, y))
        for i in range(self.cal.cards_per_page):
            if (x, y) == self.cal.card_click_point(i) and self.view == "list":
                self.view, self.open_card = "card", i
                return
        if (x, y) == self.cal.region_center("journal_show_details") and self.view == "card":
            self.view = "details"
            name = titles.extract_tournament_name(self.pages[self.page][self.open_card])
            answer = self.answers.get(name)
            if answer is not None and self.unknown_kind:
                self.collector.feed_record(unknown_record(answer[0], self.unknown_kind))
                return
            if answer is not None:
                self.collector.feed_record(ranking_record(*answer))
                self.shown = 0
                self._show_rows(self.visible_rows)
            return
        if (x, y) == self.cal.point("journal_details_close") and self.view == "details":
            self.view = "card"
            return
        if (x, y) == self.cal.point("journal_card_close") and self.view == "card" and not self.stuck_on_close:
            self.view = "list"
            return
        if (x, y) == self.cal.point("journal_next_page") and self.page < len(self.pages) - 1:
            self.page += 1

    def _players(self):
        answer = self.answers[titles.extract_tournament_name(self.pages[self.page][self.open_card])]
        return list(answer[2]) if len(answer) > 2 else [1001, 1002]

    def _show_rows(self, count):
        """Like the game: the profiles of the rows that come into view."""
        players = self._players()
        new = players[self.shown:self.shown + count]
        self.shown += len(new)
        if new:
            rows = [[[pid], f"tb:{pid}", f"Player {pid}", "BR", 3, 1, 2, 444, 45, 25, 5000 + pid, [1], 1, "KOK"] for pid in new]
            self.collector.feed_record(http_record(tbcodec.build_frame(402, 1, [rows]), idx=8))

    def scroll(self, x, y, delta_y=400, repetitions=1, delay=0.15):
        self.scrolls += 1
        if self.view == "details" and self.profiles_on_scroll:
            self._show_rows(2)

    def press_key(self, name, delay=0.0):
        if self.view == "details":
            self.view = "card"
        elif self.view == "card" and not self.stuck_on_close:
            self.view = "list"


class FakeOcr:
    def __init__(self, game):
        self.game = game
        self.error = ""

    def available(self):
        return True

    def read(self, image, single_line=False):
        if image is None or image.shape[0] != 4:
            return ""
        return self.game.texts[image.shape[1] - 1]


def title(name):
    return f"Your Clanmates' results in {name}"


class MapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cal = make_calibration(os.path.join(self.tmp.name, "cal.json"))
        self.collector = journal.JournalCollector()

    def tearDown(self):
        self.tmp.cleanup()

    def run_mapper(self, game, **kwargs):
        options = mapper.MapperOptions(max_pages=10, step_delay=0.0, answer_timeout=0.3, names_delay=0.0)
        runner = mapper.JournalMapper(game, self.cal, FakeOcr(game), self.collector, options, **kwargs)
        return runner.run()

    def test_maps_every_tournament_across_pages_and_skips_the_rest(self):
        pages = [
            [title("Rise of the Ancients"), "Clan treasury purchase", title("Epic Hunt")],
            [title("Rise of the Ancients"), title("Known One")],
        ]
        answers = {
            "Rise of the Ancients": ("aaaaaaaa-0000-4000-8000-000000000001", (1024, 1)),
            "Epic Hunt": ("aaaaaaaa-0000-4000-8000-000000000002", (1007, 14)),
            "Known One": ("aaaaaaaa-0000-4000-8000-000000000003", (1001, 1)),
        }
        game = FakeGame(self.cal, pages, answers, self.collector)

        cards = self.run_mapper(game, known_names={"known one"})

        by_name = {(c.name, c.page): c for c in cards}
        rise = by_name[("Rise of the Ancients", 1)]
        self.assertEqual(mapper.STATUS_MAPPED, rise.status)
        self.assertEqual("1024:1", rise.tournament_key)
        self.assertEqual(1024, rise.game_type)
        self.assertIsNotNone(rise.icon_png)
        self.assertEqual("1007:14", by_name[("Epic Hunt", 1)].tournament_key)
        # Mapped on page 1, so not opened again on page 2; known on the site, not opened at all.
        self.assertEqual(mapper.STATUS_SKIPPED, by_name[("Rise of the Ancients", 2)].status)
        self.assertEqual(mapper.STATUS_SKIPPED, by_name[("Known One", 2)].status)
        self.assertEqual("list", game.view)
        self.assertEqual(1, game.page)

        entries = mapper.catalogue_entries(cards)
        self.assertEqual({"Rise of the Ancients", "Epic Hunt"}, {e["name"] for e in entries})
        self.assertTrue(all("image" in e for e in entries))
        self.assertEqual([], mapper.conflicts(cards))

    def test_pages_full_of_the_same_message_are_walked_by_the_page_number(self):
        self.cal.calibration.set_region("journal_page_area", (480, 690), (520, 710))
        same = ["Epic Fenrir squad attacked", "Epic Fenrir squad attacked"]
        pages = [same, same, same, [title("Ragnarok tournament")]]
        answers = {"Ragnarok tournament": ("cccccccc-0000-4000-8000-000000000001", (1026, 1))}
        game = FakeGame(self.cal, pages, answers, self.collector)

        cards = self.run_mapper(game)

        self.assertEqual([("Ragnarok tournament", 4, mapper.STATUS_MAPPED)], [(c.name, c.page, c.status) for c in cards])

    def test_without_the_page_number_the_same_titles_mean_the_last_page(self):
        same = ["Epic Fenrir squad attacked"]
        game = FakeGame(self.cal, [same, same, [title("Never reached")]], {}, self.collector)

        self.assertEqual([], self.run_mapper(game))
        self.assertEqual(1, game.page)

    def test_a_cached_ranking_is_reported_and_the_walk_goes_on(self):
        pages = [[title("Cached"), title("Fresh")]]
        answers = {"Fresh": ("bbbbbbbb-0000-4000-8000-000000000001", (1013, 4))}
        game = FakeGame(self.cal, pages, answers, self.collector)

        cards = self.run_mapper(game)

        self.assertEqual([mapper.STATUS_NO_ANSWER, mapper.STATUS_MAPPED], [c.status for c in cards])
        self.assertEqual(["Fresh"], [e["name"] for e in mapper.catalogue_entries(cards)])

    def test_the_ranking_is_scrolled_until_every_player_has_a_name(self):
        self.cal.calibration.set_region("journal_ranking_list", (300, 200), (700, 600))
        uid = "dddddddd-0000-4000-8000-000000000001"
        players = list(range(2001, 2010))
        game = FakeGame(self.cal, [[title("Long")]], {"Long": (uid, (1024, 1), players)}, self.collector)

        [card] = self.run_mapper(game)

        self.assertEqual(mapper.STATUS_MAPPED, card.status)
        self.assertEqual(0, card.unnamed)
        self.assertEqual([], self.collector.missing_profiles(uid))
        self.assertEqual(4, game.scrolls, "2 rows on screen, then 2 more per scroll: 9 players")
        self.assertEqual("list", game.view)

    def test_no_scrolling_when_every_name_is_already_known(self):
        self.cal.calibration.set_region("journal_ranking_list", (300, 200), (700, 600))
        players = list(range(4001, 4010))
        self.collector.profiles.update({pid: journal.Profile(pid, f"Known {pid}") for pid in players})
        uid = "ffffffff-0000-4000-8000-000000000001"
        game = FakeGame(self.cal, [[title("Cached names")]], {"Cached names": (uid, (1024, 1), players)}, self.collector)
        game.visible_rows = 0   # the game sends no profile: it already fetched them

        [card] = self.run_mapper(game)

        self.assertEqual(0, card.unnamed)
        self.assertEqual(0, game.scrolls)

    def test_each_ranking_of_a_tournament_is_its_own_catalogue_entry(self):
        tracker = journal.ITEM_GAIN_TRACKER + ":omens_damage"
        cards = [
            mapper.MappedCard("score of Remnants of Dread", 1, 1, mapper.STATUS_MAPPED, "u1", "1033:1", ranking=tracker),
            mapper.MappedCard("contributions of Omen Essence", 1, 2, mapper.STATUS_MAPPED, "u2", "1033:1", ranking=journal.POINTS_MINING),
        ]
        entries = mapper.catalogue_entries(cards)
        self.assertEqual({tracker, journal.POINTS_MINING}, {e["ranking"] for e in entries})
        self.assertEqual([], mapper.conflicts(cards))

    def test_names_that_never_arrive_are_counted_without_hanging(self):
        self.cal.calibration.set_region("journal_ranking_list", (300, 200), (700, 600))
        game = FakeGame(self.cal, [[title("Cached")]],
                        {"Cached": ("eeeeeeee-0000-4000-8000-000000000001", (1024, 1), [3001, 3002, 3003, 3004])},
                        self.collector)
        game.profiles_on_scroll = False
        game.list_end = 0

        [card] = self.run_mapper(game)

        self.assertEqual(mapper.STATUS_MAPPED, card.status)
        self.assertEqual(2, card.unnamed)
        self.assertEqual(mapper.MapperOptions().names_stalls, game.scrolls)

    def test_a_list_that_still_moves_is_scrolled_on_without_new_names(self):
        self.cal.calibration.set_region("journal_ranking_list", (300, 200), (700, 600))
        game = FakeGame(self.cal, [[title("Long walk")]],
                        {"Long walk": ("eeeeeeee-0000-4000-8000-000000000002", (1024, 1), [3101, 3102, 3103, 3104])},
                        self.collector)
        game.profiles_on_scroll = False
        game.list_end = 6

        [card] = self.run_mapper(game)

        self.assertEqual(2, card.unnamed)
        self.assertEqual(6 + mapper.MapperOptions().names_stalls, game.scrolls,
                         "scrolled to the end of the list, then the usual stalls")

    def test_an_answer_in_an_unknown_shape_says_so_instead_of_no_answer(self):
        game = FakeGame(self.cal, [[title("Trials of Olympus tournament"), title("Fresh")]], {
            "Trials of Olympus tournament": ("aaaaaaaa-0000-4000-8000-00000000000a", (1040, 1)),
            "Fresh": ("aaaaaaaa-0000-4000-8000-00000000000b", (1024, 1)),
        }, self.collector)
        game.unknown_kind = "multistage_tournament_results_entry"

        cards = self.run_mapper(game)

        self.assertIn(mapper.STATUS_UNKNOWN_FORMAT, cards[0].status)
        self.assertIn("multistage_tournament_results_entry", cards[0].status)
        self.assertNotIn("[1040, 1]", cards[0].status, "the sketch belongs in the log, not in the table")
        self.assertEqual([], mapper.catalogue_entries(cards), "nothing to send to the site")
        self.assertEqual("list", game.view, "the walk goes on")

    def test_losing_the_list_stops_with_a_clear_message(self):
        game = FakeGame(self.cal, [[title("Stuck")]], {"Stuck": ("cccccccc-0000-4000-8000-000000000001", (1, 1))}, self.collector)
        game.stuck_on_close = True

        with self.assertRaises(RuntimeError) as failure:
            self.run_mapper(game)
        self.assertIn("did not return to the list", str(failure.exception))

    def test_the_walk_goes_back_only_the_days_it_was_told_to(self):
        pages = [
            [title("Today") + " today, 15:00", "Clan treasury purchase yesterday, 10:52",
             title("Two") + ". 2 days ago, 14:00"],
            [title("Three") + " 3 days ago, 14:00", "Raid canceled 4 days ago, 09:00", title("Five") + " 5 days ago, 14:00"],
            [title("Nine") + " 9 days ago, 14:00"],
        ]
        answers = {name: (f"aaaaaaaa-0000-4000-8000-00000000000{i}", (1000 + i, 1))
                   for i, name in enumerate(["Today", "Two", "Three", "Five", "Nine"])}
        game = FakeGame(self.cal, pages, answers, self.collector)
        options = mapper.MapperOptions(max_pages=10, step_delay=0.0, answer_timeout=0.3, names_delay=0.0,
                                       max_age_days=2)
        runner = mapper.JournalMapper(game, self.cal, FakeOcr(game), self.collector, options)

        cards = runner.run()

        # "3 days ago" is not opened; the second card past the limit, tournament or not, ends the walk.
        self.assertEqual(["Today", "Two"], [c.name for c in cards])
        self.assertEqual(mapper.FINISH_DAYS, runner.finish_reason)
        self.assertEqual(1, game.page, "the third page is never reached")
        self.assertEqual("list", game.view)

    def test_a_card_that_does_not_say_its_age_is_never_too_old(self):
        pages = [[title("Old") + " 9 days ago, 14:00", title("Undated"), title("Fresh") + " yesterday, 14:00"]]
        answers = {name: (f"bbbbbbbb-0000-4000-8000-00000000000{i}", (1000 + i, 1))
                   for i, name in enumerate(["Old", "Undated", "Fresh"])}
        game = FakeGame(self.cal, pages, answers, self.collector)
        options = mapper.MapperOptions(max_pages=10, step_delay=0.0, answer_timeout=0.3, names_delay=0.0,
                                       max_age_days=2)
        runner = mapper.JournalMapper(game, self.cal, FakeOcr(game), self.collector, options)

        # One misread age alone does not end the walk either.
        self.assertEqual(["Undated", "Fresh"], [c.name for c in runner.run()])
        self.assertEqual(mapper.FINISH_END, runner.finish_reason)

    def test_incomplete_calibration_is_refused(self):
        self.cal.calibration.forget("journal_next_page")
        game = FakeGame(self.cal, [[title("A")]], {}, self.collector)
        with self.assertRaises(RuntimeError):
            self.run_mapper(game)

    def test_conflicting_readings_are_reported(self):
        cards = [
            mapper.MappedCard("Rise", 1, 1, mapper.STATUS_MAPPED, "u1", "1024:1"),
            mapper.MappedCard("Rlse", 2, 1, mapper.STATUS_MAPPED, "u2", "1024:3"),
            mapper.MappedCard("Hunt", 2, 2, mapper.STATUS_MAPPED, "u3", "1007:1"),
            mapper.MappedCard("hunt", 3, 1, mapper.STATUS_MAPPED, "u4", "1010:1"),
        ]
        problems = mapper.conflicts(cards)
        self.assertEqual(2, len(problems))
        self.assertIn("1024", problems[0])


class LayoutTest(unittest.TestCase):
    def test_cards_are_derived_from_the_first_two_and_rescale_with_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = make_calibration(os.path.join(tmp, "cal.json")).calibration
            first.save()
            cal = Calibration(first.groups, first.path, first.refs_dir)
            cal.use_viewport(1000, 800)
            again = JournalLayout(cal, cards_per_page=3)
            self.assertEqual((300, 100 + 2 * PITCH, 500, 30), again.card_title_region(2))
            self.assertEqual((500, 115 + PITCH), again.card_click_point(1))
            self.assertEqual([], again.missing_steps())

            cal.use_viewport(2000, 1600)
            self.assertEqual((1000, 230 + 2 * 180), again.card_click_point(2))


if __name__ == "__main__":
    unittest.main()
