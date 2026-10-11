"""The automatic search: the Journal walk of tests/test_events_mapper.py plus a fake site."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.events import api, autosend, journal, mapper, tbcodec  # noqa: E402
from test_events_journal import ENDED_AT, ws_record  # noqa: E402
from test_events_mapper import FakeGame, FakeOcr, make_calibration, title  # noqa: E402

HOUR = 3600
NEWEST = "11111111-0000-4000-8000-000000000001"
HALF = "11111111-0000-4000-8000-000000000002"
OLD = ["11111111-0000-4000-8000-00000000000a", "11111111-0000-4000-8000-00000000000b",
       "11111111-0000-4000-8000-00000000000c", "11111111-0000-4000-8000-00000000000d"]


def journal_entries(entries):
    """The Journal list as the game sends it: (result uid, ended at, type) per tournament."""
    items = []
    for i, (uid, ended_at, key) in enumerate(entries):
        summary = tbcodec.pack(["global_tournament_user_result", None, list(key), 1])
        items.append([f"3cd16678-5f7b-44f8-847e-{i:012d}", ended_at, 1, "global_tournament_user_result",
                      [summary], True, False, False, uid])
    return ws_record([[{"733765024": b"\x93\xcd\x5e\x02\x00\xa0"}], [items]], idx=1)


class FakeSite:
    """The site's side: one event per result id, published ones refused, searches recorded."""

    def __init__(self, covered_until=None, latest_ended_at=None):
        self.events = {}        # result uid -> {"event_number", "published", "points"}
        self.posts = []
        self.catalogue = []
        self.reports = []
        self.state = {"covered_until": covered_until, "latest_ended_at": latest_ended_at}

    def me(self):
        return {"user": {"name": "Admin"}}

    def known_tournaments(self):
        return []

    def search_state(self):
        return dict(self.state)

    def report_search(self, report):
        self.reports.append(report)
        if report["complete"]:
            self.state["covered_until"] = report["started_at"]
        return {"recorded": True}

    def lookup(self, uids):
        return {uid: {"event_id": e["event_number"], "event_number": e["event_number"], "published": e["published"]}
                for uid, e in self.events.items() if uid in uids}

    def send_catalog(self, entries):
        self.catalogue.extend(entries)
        return [{"game_type": e["tournament_key"].split(":")[0], "name": e["name"], "created": True} for e in entries]

    def send_tournament(self, payload):
        self.posts.append(payload)
        uid = payload["result_uid"]
        points = [(r["player_id"], r["points"]) for r in payload["rows"]]
        event = self.events.get(uid)
        if event is not None and (event["published"] or event["points"] != points):
            raise api.ApiError(409, "already published" if event["published"] else "points cannot change")
        if event is None:
            event = self.events[uid] = {"event_number": len(self.events) + 1, "published": False, "points": points}
        event["published"] = bool(payload["auto_publish"])
        return {"event_id": event["event_number"], "event_number": event["event_number"], "event_name": payload["name"],
                "published": event["published"], "publish_blocked": None if event["published"] else "unnamed",
                "review_url": f"https://site/review/{event['event_number']}"}


class AutoSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cal = make_calibration(os.path.join(self.tmp.name, "cal.json"))
        self.collector = journal.JournalCollector()
        self.sent = {}

    def tearDown(self):
        self.tmp.cleanup()

    def mark(self, uid, info):
        self.sent[uid] = info

    def search(self, game, site, **options):
        options = mapper.MapperOptions(max_pages=10, step_delay=0.0, answer_timeout=0.3, names_delay=0.0,
                                       reopen_known=True, **options)
        return autosend.AutoSearch(
            game, self.cal, FakeOcr(game), self.collector, site, options,
            local_info=self.sent.get, mark_sent=self.mark, catalogue_name=lambda t: "", client_version="test",
            clock=lambda: ENDED_AT + 10 * HOUR,
        )

    def journal(self):
        """Newest first, an hour apart: Newest, Half named, Old A..D."""
        pages = [
            [title("Newest"), title("Half named"), title("Old A")],
            [title("Old B"), title("Old C"), title("Old D")],
        ]
        answers = {
            "Newest": (NEWEST, (1024, 1), [1001, 1002]),
            # Four players, two on screen and no list to scroll: two never get a name.
            "Half named": (HALF, (1007, 3), [2001, 2002, 2003, 2004]),
            "Old A": (OLD[0], (1001, 1)), "Old B": (OLD[1], (1002, 1)),
            "Old C": (OLD[2], (1003, 1)), "Old D": (OLD[3], (1004, 1)),
        }
        uids = [NEWEST, HALF] + OLD
        keys = [(1024, 1), (1007, 3), (1001, 1), (1002, 1), (1003, 1), (1004, 1)]
        self.collector.feed_record(journal_entries([(uid, ENDED_AT - i * HOUR, key) for i, (uid, key) in enumerate(zip(uids, keys))]))
        return FakeGame(self.cal, pages, answers, self.collector)

    def test_looks_back_to_the_last_complete_search_and_closes_what_has_every_name(self):
        # The last complete search covered up to Old A's end (plus the overlap margin).
        site = FakeSite(covered_until=autosend.iso(ENDED_AT - 2 * HOUR + autosend.CUTOFF_MARGIN))
        game = self.journal()

        summary = self.search(game, site).run()

        # Old B and Old C ended before that point: two in a row, so Old D is never opened.
        self.assertEqual(["Newest", "Half named", "Old A", "Old B", "Old C"], [c.name for c in summary.cards])
        self.assertEqual(mapper.FINISH_CUTOFF, summary.reason)
        self.assertEqual("list", game.view)

        # Oldest first; the one with every name is closed, the other goes to review.
        sent = [p["result_uid"] for p in site.posts]
        self.assertEqual([OLD[2], OLD[1], OLD[0], HALF, NEWEST], sent)
        by_uid = {p["result_uid"]: p for p in site.posts}
        self.assertTrue(by_uid[NEWEST]["auto_publish"])
        self.assertFalse(by_uid[HALF]["auto_publish"])
        self.assertIn("id:2004", [r["name"] for r in by_uid[HALF]["rows"]])

        # Complete: the next search starts here.
        self.assertTrue(summary.complete)
        [report] = site.reports
        self.assertTrue(report["complete"])
        self.assertEqual(autosend.iso(ENDED_AT + 10 * HOUR), site.state["covered_until"])

    def test_running_again_sends_nothing_twice(self):
        site = FakeSite(covered_until=autosend.iso(ENDED_AT - 2 * HOUR + autosend.CUTOFF_MARGIN))
        self.search(self.journal(), site).run()
        first = len(site.posts)

        # Same Journal, and the local history lost (another computer, or a new install).
        self.sent.clear()
        self.collector = journal.JournalCollector()
        site.state["covered_until"] = autosend.iso(ENDED_AT - 2 * HOUR + autosend.CUTOFF_MARGIN)
        summary = self.search(self.journal(), site).run()

        # The published ones are skipped by asking the site. The draft is sent again
        # with the same ranking: the site keeps one event for it and stores nothing new.
        resent = [p["result_uid"] for p in site.posts[first:]]
        self.assertNotIn(NEWEST, resent)
        self.assertEqual(len(set(p["result_uid"] for p in site.posts)), len(site.events))
        self.assertGreaterEqual(summary.skipped, 1)

        # With the local history kept, not even the draft is sent again.
        posts = len(site.posts)
        self.collector = journal.JournalCollector()
        self.search(self.journal(), site).run()
        self.assertEqual(posts, len(site.posts))

    def test_a_draft_is_sent_again_once_its_names_arrived(self):
        rows = [journal.RankingRow(1, 1001, 10, "A"), journal.RankingRow(2, 1002, 5, None)]
        t = journal.Tournament(HALF, "1024:1", ENDED_AT, rows=rows)
        site = FakeSite()
        autosend.send_found(site, [t], lambda _t: "", self.sent.get, self.mark, "test")
        autosend.send_found(site, [t], lambda _t: "", self.sent.get, self.mark, "test")
        self.assertEqual(1, len(site.posts), "nothing changed: not sent again")

        rows[1].name = "B"
        [outcome], _ = autosend.send_found(site, [t], lambda _t: "", self.sent.get, self.mark, "test")
        self.assertEqual(2, len(site.posts))
        self.assertTrue(outcome.published)

    def test_the_first_search_without_any_history_walks_the_whole_journal(self):
        site = FakeSite()
        summary = self.search(self.journal(), site).run()

        self.assertIsNone(summary.cutoff)
        self.assertEqual(6, len(summary.cards))
        self.assertEqual(mapper.FINISH_END, summary.reason)
        self.assertTrue(summary.complete)

    def test_a_walk_stopped_by_the_days_limit_is_a_complete_search(self):
        site = FakeSite()
        game = self.journal()
        game.pages = [[text + f" {i} days ago, 14:00" for i, text in enumerate(page, start=3 * n)]
                      for n, page in enumerate(game.pages)]

        summary = self.search(game, site, max_age_days=2).run()

        # 0, 1 and 2 days ago are opened; 3 and 4 end the walk.
        self.assertEqual(["Newest", "Half named", "Old A"], [c.name for c in summary.cards])
        self.assertEqual(mapper.FINISH_DAYS, summary.reason)
        self.assertTrue(summary.complete)
        self.assertEqual(mapper.FINISH_CUTOFF, site.reports[0]["reason"], "the site only knows 'cutoff'")
        self.assertEqual({NEWEST, HALF, OLD[0]}, {p["result_uid"] for p in site.posts})

    def test_the_sites_last_search_can_be_set_aside(self):
        site = FakeSite(covered_until=autosend.iso(ENDED_AT + 5 * HOUR))
        search = self.search(self.journal(), site)
        search.use_site_cutoff = False
        summary = search.run()
        self.assertIsNone(summary.cutoff)
        self.assertEqual(6, len(summary.cards))

    def test_the_newest_tournament_on_the_site_is_the_fallback(self):
        site = FakeSite(latest_ended_at=autosend.iso(ENDED_AT))
        summary = self.search(self.journal(), site).run()
        self.assertEqual(autosend.iso(ENDED_AT - autosend.CUTOFF_MARGIN), summary.cutoff)
        self.assertEqual(mapper.FINISH_CUTOFF, summary.reason)

    def test_an_unanswered_ranking_or_a_failed_send_does_not_move_the_starting_point(self):
        site = FakeSite(covered_until=autosend.iso(ENDED_AT - 2 * HOUR + autosend.CUTOFF_MARGIN))
        game = self.journal()
        del game.answers["Half named"]   # the game had it cached and sent nothing

        summary = self.search(game, site).run()

        self.assertFalse(summary.complete)
        self.assertFalse(site.reports[0]["complete"])
        self.assertEqual(autosend.iso(ENDED_AT - 2 * HOUR + autosend.CUTOFF_MARGIN), site.state["covered_until"])

    def test_a_journal_that_did_not_open_is_not_a_complete_search(self):
        site = FakeSite()
        game = FakeGame(self.cal, [[]], {}, self.collector)

        summary = self.search(game, site).run()

        self.assertEqual(mapper.FINISH_NO_JOURNAL, summary.reason)
        self.assertFalse(summary.complete)

    def test_a_refused_tournament_is_not_tried_on_every_run(self):
        t = journal.Tournament(NEWEST, "1024:1", ENDED_AT, rows=[journal.RankingRow(1, 1001, 10, "A")])
        site = FakeSite()
        site.events[NEWEST] = {"event_number": 1, "published": False, "points": [(1001, 99)]}

        [outcome], _ = autosend.send_found(site, [t], lambda _t: "A", self.sent.get, self.mark, "test")

        self.assertEqual("points cannot change", outcome.error)
        self.assertTrue(outcome.refused)
        self.assertEqual("points cannot change", self.sent[NEWEST]["conflict"])
        self.assertEqual(([], 1), autosend.send_found(site, [t], lambda _t: "A", self.sent.get, self.mark, "test"))

    def test_without_the_end_date_or_with_closing_off_it_goes_to_review(self):
        rows = [journal.RankingRow(1, 1001, 10, "A")]
        site = FakeSite()
        autosend.send_found(site, [journal.Tournament(NEWEST, "1024:1", None, rows=rows)],
                            lambda _t: "", self.sent.get, self.mark, "test")
        autosend.send_found(site, [journal.Tournament(HALF, "1024:1", ENDED_AT, rows=rows)],
                            lambda _t: "", self.sent.get, self.mark, "test", auto_publish=False)

        self.assertEqual([False, False], [p["auto_publish"] for p in site.posts])


if __name__ == "__main__":
    unittest.main()
