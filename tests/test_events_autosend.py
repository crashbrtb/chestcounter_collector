"""The automatic search: the fake feed of tests/test_events_mapper.py plus a fake site."""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.events import api, autosend, journal, mapper  # noqa: E402
from modules.events.screen import CardPicture  # noqa: E402
from test_events_journal import ENDED_AT  # noqa: E402
from test_events_mapper import FakeFeed, ranking_entry, uid  # noqa: E402
from utils.cancel import cancellation  # noqa: E402

HOUR = 3600
NEWEST, HALF = uid(5001), uid(5002)
OLD = [uid(5003), uid(5004), uid(5005), uid(5006)]


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
        return [{"game_type": e["tournament_key"].split(":")[0], "name": None, "created": True} for e in entries]

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
        self.collector = journal.JournalCollector()
        self.sent = {}
        patch = mock.patch.object(cancellation, "sleep", lambda _seconds: None)
        patch.start()
        self.addCleanup(patch.stop)

    def mark(self, uid, info):
        self.sent[uid] = info

    def search(self, feed, site, known=lambda _t: "Epic Hunt", **options):
        options = mapper.MapperOptions(interval=0, cards_per_page=3, **options)
        return autosend.AutoSearch(
            feed, lambda _page, _card: CardPicture(b"page", None), self.collector, site, options,
            local_info=self.sent.get, mark_sent=self.mark, catalogue_name=known, has_picture=lambda _t: False,
            client_version="test",
            clock=lambda: ENDED_AT + 10 * HOUR,
        )

    def journal(self):
        """Newest first, an hour apart: six rankings. Player 1002 is unknown to the game only in the second."""
        keys = [(1024, 1), (1007, 3), (1001, 1), (1002, 1), (1003, 1), (1004, 1)]
        return FakeFeed([ranking_entry(i + 1, ENDED_AT - i * HOUR, key) for i, key in enumerate(keys)])

    def test_looks_back_to_the_last_complete_search_and_closes_what_has_every_name(self):
        # The last complete search covered up to the third's end, with the overlap margin.
        site = FakeSite(covered_until=autosend.iso(ENDED_AT - 2 * HOUR + autosend.CUTOFF_MARGIN))
        feed = self.journal()

        summary = self.search(feed, site).run()

        self.assertEqual([NEWEST, HALF, OLD[0]], [c.result_uid for c in summary.cards])
        self.assertEqual(mapper.FINISH_CUTOFF, summary.reason)

        # Oldest first, and closed, since every player has a name.
        self.assertEqual([OLD[0], HALF, NEWEST], [p["result_uid"] for p in site.posts])
        self.assertTrue(all(p["auto_publish"] for p in site.posts))
        self.assertEqual("Epic Hunt", site.posts[0]["name"])
        self.assertEqual([], site.catalogue, "the catalogue already knows this name")

        # Complete: the next search starts here.
        self.assertTrue(summary.complete)
        [report] = site.reports
        self.assertTrue(report["complete"])
        self.assertEqual(autosend.iso(ENDED_AT + 10 * HOUR), site.state["covered_until"])

    def test_someone_without_a_name_goes_to_review(self):
        site = FakeSite()
        feed = FakeFeed([ranking_entry(1, ENDED_AT)], names={1001: "Lion"})

        self.search(feed, site).run()

        [post] = site.posts
        self.assertFalse(post["auto_publish"])
        self.assertIn("id:1002", [r["name"] for r in post["rows"]])

    def test_a_new_type_goes_to_the_catalogue_with_a_picture_and_its_rankings_wait(self):
        known = lambda t: "" if t.tournament_key.split(":")[0] in ("1024", "1007") else "Known"
        site = FakeSite(latest_ended_at=None)
        feed = FakeFeed([ranking_entry(1, ENDED_AT, (1024, 1)), ranking_entry(2, ENDED_AT - HOUR, (1007, 3)),
                         ranking_entry(3, ENDED_AT - 2 * HOUR, (1001, 1)), ranking_entry(4, ENDED_AT - 3 * HOUR, (1007, 4))])

        summary = self.search(feed, site, known).run()

        self.assertEqual([uid(5003)], [p["result_uid"] for p in site.posts], "only the named one is sent")
        self.assertEqual(3, summary.nameless)
        self.assertEqual(2, len(site.catalogue))
        self.assertEqual({"cGFnZQ=="}, {e["picture"] for e in site.catalogue})
        self.assertFalse(summary.complete, "the next search looks again")
        self.assertEqual(3, site.reports[0]["errors"])

    def test_running_again_sends_nothing_twice(self):
        site = FakeSite(covered_until=autosend.iso(ENDED_AT - 2 * HOUR + autosend.CUTOFF_MARGIN))
        self.search(self.journal(), site).run()
        first = len(site.posts)

        # Same Journal, and the local history lost (another computer, or a new install).
        self.sent.clear()
        self.collector = journal.JournalCollector()
        site.state["covered_until"] = autosend.iso(ENDED_AT - 2 * HOUR + autosend.CUTOFF_MARGIN)
        summary = self.search(self.journal(), site).run()

        # The published ones are skipped by asking the site.
        self.assertEqual([], site.posts[first:])
        self.assertGreaterEqual(summary.skipped, 1)

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
        summary = self.search(self.journal(), FakeSite()).run()

        self.assertIsNone(summary.cutoff)
        self.assertEqual(6, len(summary.cards))
        self.assertEqual(mapper.FINISH_END, summary.reason)
        self.assertTrue(summary.complete)

    def test_a_walk_stopped_by_the_days_limit_is_a_complete_search(self):
        site = FakeSite()
        now = int(datetime.now().timestamp())
        feed = FakeFeed([ranking_entry(1, now), ranking_entry(2, now - 9 * 86400)])

        summary = self.search(feed, site, max_age_days=2).run()

        self.assertEqual([NEWEST], [c.result_uid for c in summary.cards])
        self.assertEqual(mapper.FINISH_DAYS, summary.reason)
        self.assertTrue(summary.complete)
        self.assertEqual(mapper.FINISH_CUTOFF, site.reports[0]["reason"], "the site only knows 'cutoff'")

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

    def test_an_unanswered_ranking_does_not_move_the_starting_point(self):
        site = FakeSite(covered_until=autosend.iso(ENDED_AT - 2 * HOUR + autosend.CUTOFF_MARGIN))
        feed = self.journal()
        feed.failing = {HALF}

        summary = self.search(feed, site).run()

        self.assertFalse(summary.complete)
        self.assertFalse(site.reports[0]["complete"])
        self.assertEqual(autosend.iso(ENDED_AT - 2 * HOUR + autosend.CUTOFF_MARGIN), site.state["covered_until"])

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
