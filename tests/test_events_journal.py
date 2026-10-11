"""Journal reader, site client and what the events module remembers between runs.

Packets are built in the game's own shape, as seen on the recording of
12/09/2026 (see modules/events/journal.py).
"""
import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.events import api, journal, tbcodec  # noqa: E402
from modules.events.store import SentHistory, site_slug  # noqa: E402

RESULT_UID = "eede1d1c-3f13-481a-819d-51fdcebb1e54"
ENDED_AT = 1789232426  # 2026-09-12 17:00:26 UTC
PLAYERS = [(300647954111, "Naughtius Maximus", 1703103642), (438086712193, "Lion", 873578580),
           (4307852203038, "Lion", 503945198), (622770520040, "BENAR", 0)]


def ws_record(stream_objects, idx=1):
    data = b"".join(tbcodec.pack(o) for o in stream_objects)
    return {"idx": idx, "kind": "WS_IN", "ts": 100.0 + idx, "url": "wss://x", "routes": [],
            "b64": base64.b64encode(data).decode("ascii")}


def http_record(frame_bytes, idx=1):
    return {"idx": idx, "kind": "RESP", "ts": 100.0 + idx, "url": "https://game-us37.totalbattle.com/rubens-realm237",
            "routes": [f.route for f in tbcodec.iter_frames(frame_bytes) if f.route is not None],
            "b64": base64.b64encode(frame_bytes).decode("ascii")}


def journal_list(has_details=1, key=(1024, 1)):
    summary = tbcodec.pack(["global_tournament_user_result", None, list(key), has_details])
    entry = ["3cd16678-5f7b-44f8-847e-0bee495480d9", ENDED_AT, 1876146181246441, "global_tournament_user_result",
             [summary], True, False, False, RESULT_UID]
    noise = ["4285e683-78b8-4f1d-a6b9-8ab18d5f9af3", ENDED_AT - 50, 1, "treasury_buy", [b"\x94\x01\x02\x03\x04"], True, False, False, None]
    return ws_record([[{"733765024": b"\x93\xcd\x5e\x02\x00\xa0"}], [[noise, entry]]], idx=1)


def journal_detail(players=PLAYERS):
    ranking = [[[pid], points] for pid, _, points in players]
    inner = tbcodec.pack(["global_tournament_user_result", [1024, 1], 1, [[1, 1, ranking, [], []]]])
    return ws_record([[{"733765024": b"\x93\x06\x00\xa0"}], [{RESULT_UID: [inner, True]}]], idx=2)


def profiles(players=PLAYERS):
    rows = [[[pid], f"tb:{pid}", name, "BR", 3, 1, 2, 444, 45, 25, 1_000_000 + i, [4410931413044], 528144, "KOK"]
            for i, (pid, name, _) in enumerate(players)]
    return http_record(tbcodec.build_frame(402, 7, [rows]), idx=3)


class JournalTest(unittest.TestCase):
    def test_assembles_a_tournament_from_list_detail_and_profiles(self):
        collector = journal.JournalCollector()
        self.assertTrue(collector.feed_record(journal_list()))
        self.assertTrue(collector.feed_record(journal_detail()))
        self.assertTrue(collector.feed_record(profiles()))

        [tournament] = collector.tournaments()
        self.assertEqual(RESULT_UID, tournament.result_uid)
        self.assertEqual("1024:1", tournament.tournament_key)
        self.assertEqual("2026-09-12T17:00:26Z", tournament.ended_at_iso)
        self.assertEqual(["Naughtius Maximus", "Lion", "Lion", "BENAR"], [r.name for r in tournament.rows])
        self.assertEqual([1, 2, 3, 4], [r.position for r in tournament.rows])
        self.assertEqual(0, tournament.rows[3].points)
        self.assertEqual(0, tournament.unnamed)
        self.assertTrue(all(ok for ok, _ in journal.checks(tournament)))

        payload = journal.build_payload(tournament, " Rise of the Ancients ", tournament.ended_at_iso, "1.0.0")
        self.assertEqual("Rise of the Ancients", payload["name"])
        self.assertEqual("1024:1", payload["tournament_key"])
        self.assertEqual({"position": 3, "name": "Lion", "points": 503945198, "player_id": 4307852203038, "power": 1000002},
                         payload["rows"][2])

    def test_order_of_arrival_does_not_matter(self):
        collector = journal.JournalCollector()
        for record in (profiles(), journal_detail(), journal_list()):
            collector.feed_record(record)
        [tournament] = collector.tournaments()
        self.assertEqual("2026-09-12T17:00:26Z", tournament.ended_at_iso)
        self.assertEqual(4, len(tournament.rows))

    def test_detail_without_list_or_profiles_is_still_usable_but_flagged(self):
        collector = journal.JournalCollector()
        collector.feed_record(journal_detail())

        [tournament] = collector.tournaments()
        self.assertEqual("1024:1", tournament.tournament_key, "the detail carries the type too")
        self.assertIsNone(tournament.ended_at)
        self.assertEqual(4, tournament.unnamed)
        self.assertEqual("id:622770520040", tournament.rows[3].display_name)
        failing = [text for ok, text in journal.checks(tournament) if not ok]
        self.assertEqual(2, len(failing))

    def test_list_entry_without_details_is_ignored_and_one_with_details_waits(self):
        collector = journal.JournalCollector()
        collector.feed_record(journal_list(has_details=0))
        self.assertEqual([], collector.tournaments())

        collector.feed_record(journal_list(has_details=1))
        [pending] = collector.tournaments()
        self.assertFalse(pending.has_ranking)

    def test_map_chunks_are_not_even_decoded(self):
        collector = journal.JournalCollector()
        chunk = http_record(tbcodec.build_frame(312, 1, [[1, 2, 3]]))
        self.assertFalse(collector.feed_record(chunk))
        self.assertEqual(0, collector.packets)

    def test_profiles_seen_once_name_a_ranking_reopened_in_a_later_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = journal.ProfileCache(os.path.join(tmp, "perfis.json"))
            first = journal.JournalCollector(cache)
            first.feed_record(journal_detail())
            self.assertEqual([p[0] for p in PLAYERS], first.missing_profiles(RESULT_UID))
            first.feed_record(profiles(PLAYERS[:3]))
            self.assertEqual([PLAYERS[3][0]], first.missing_profiles(RESULT_UID))
            self.assertTrue(first.save_profiles())
            self.assertFalse(first.save_profiles(), "nothing new to write")

            # Clearing forgets the tournaments, not who the players are.
            first.clear()
            self.assertEqual(3, len(first.profiles))

            # The game does not send profiles it already fetched; the cache has them.
            later = journal.JournalCollector(journal.ProfileCache(os.path.join(tmp, "perfis.json")))
            later.feed_record(journal_detail())
            [tournament] = later.tournaments()
            self.assertEqual(["Naughtius Maximus", "Lion", "Lion", None], [r.name for r in tournament.rows])
            self.assertEqual(1_000_000, tournament.rows[0].power)
            self.assertEqual(1, tournament.unnamed)

    def test_the_dark_omens_rankings_are_read_and_told_apart(self):
        damage_uid, essence_uid = "efa2484f-e61d-41e2-9c7f-25fac121b1df", "f553b1cf-3cde-4f2d-9ea3-2e9d0e96c8d1"
        tracker, mining = journal.ITEM_GAIN_TRACKER, journal.POINTS_MINING

        def entry(uid, message, result_uid):
            return [uid, ENDED_AT, 1, message[0], [tbcodec.pack(message)], True, False, False, result_uid]

        listing = ws_record([[{"1": b"\x93\x06\x00\xa0"}], [[
            entry("d4f94d3a-d11d-41e2-9c7f-25fac121b1df", [tracker, [1033, 1], "omens_damage"], damage_uid),
            entry("3980ca1e-e66d-4aa1-8a6e-c9f5ae6d2ca3", [mining, [1033, 1]], essence_uid),
        ]]], idx=1)
        damage = tbcodec.pack([tracker, [1033, 1], "omens_damage", [[[[PLAYERS[0][0]], 16331921], [[PLAYERS[1][0]], 12880697]]]])
        # Keyed by position, and not necessarily in order.
        essence = tbcodec.pack([mining, [1033, 1], {"2": [[PLAYERS[0][0]], 3122000], "1": [[PLAYERS[2][0]], 3122001]}])
        details = ws_record([[{"1": b"\x93\x06\x00\xa0"}], [{damage_uid: [damage, True], essence_uid: [essence, True]}]], idx=2)

        collector = journal.JournalCollector()
        for record in (listing, details, profiles()):
            collector.feed_record(record)
        by_uid = {t.result_uid: t for t in collector.tournaments()}

        omens = by_uid[damage_uid]
        self.assertEqual(("1033:1", f"{tracker}:omens_damage"), (omens.tournament_key, omens.ranking))
        self.assertEqual([("Naughtius Maximus", 16331921), ("Lion", 12880697)], [(r.name, r.points) for r in omens.rows])
        self.assertEqual(ENDED_AT, omens.ended_at)

        essence_t = by_uid[essence_uid]
        self.assertEqual(("1033:1", mining), (essence_t.tournament_key, essence_t.ranking))
        self.assertEqual([PLAYERS[2][0], PLAYERS[0][0]], [r.player_id for r in essence_t.rows])

        payload = journal.build_payload(essence_t, "Omen Essence", essence_t.ended_at_iso, "test")
        self.assertEqual(mining, payload["ranking"])
        self.assertEqual("", journal.build_payload(journal.Tournament("x"), "", None, "t")["ranking"])

    def test_the_trials_of_olympus_ranking_is_read(self):
        uid = "38509dc8-d8fa-4614-a4c0-fdc03336f6cf"
        rows = {str(i + 1): [[pid], 900 - i] for i, (pid, _, _) in enumerate(PLAYERS)}
        detail = tbcodec.pack([journal.MULTISTAGE, [1040, 1], rows])
        record = ws_record([[{"1": b"\x93\x06\x00\xa0"}], [{uid: [detail, True]}]], idx=4)

        collector = journal.JournalCollector()
        collector.feed_record(record)
        collector.feed_record(profiles())

        [tournament] = collector.tournaments()
        self.assertEqual(("1040:1", journal.MULTISTAGE), (tournament.tournament_key, tournament.ranking))
        self.assertEqual([900, 899, 898, 897], [r.points for r in tournament.rows])
        self.assertEqual("Naughtius Maximus", tournament.rows[0].name)
        self.assertEqual({}, collector.unknown_details)

    def test_an_unknown_detail_is_remembered_with_a_sketch_of_its_shape(self):
        uid = "11111111-2222-4333-8444-555555555555"
        detail = tbcodec.pack(["some_new_tournament_entry", [1050, 2], [[[[42], 7]]]])
        collector = journal.JournalCollector()
        collector.feed_record(ws_record([[{"1": b"\x93\x06\x00\xa0"}], [{uid: [detail, True]}]], idx=5))

        kind, sample = collector.unknown_details[uid]
        self.assertEqual("some_new_tournament_entry", kind)
        self.assertIn("[1050, 2]", sample)
        self.assertLessEqual(len(sample), journal.SAMPLE_CHARS + 3)
        self.assertEqual([], collector.tournaments())

    def test_a_broken_cache_file_is_an_empty_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "perfis.json")
            Path(path).write_text("{not json", encoding="utf-8")
            self.assertEqual({}, journal.ProfileCache(path).load())


class _Handler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def _reply(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        _Handler.requests.append(("GET", self.path, dict(self.headers), None))
        if self.headers.get("X-Api-Token") != "cct_good":
            return self._reply(401, {"error": "This API token is invalid, expired or revoked."})
        if self.path == "/site/api/v1/me":
            return self._reply(200, {"user": {"name": "Admin"}})
        return self._reply(200, {"tournaments": [{"tournament_key": "1024:1", "name": "Rise", "rewards": []}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        _Handler.requests.append(("POST", self.path, dict(self.headers), body))
        if not body.get("rows"):
            return self._reply(422, {"error": "The ranking was not accepted.", "errors": {"rows": "At least one player."}})
        return self._reply(201, {"event_id": 9, "event_number": 12, "event_created": True, "created": True})


class ApiClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/site/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_sends_the_token_in_both_headers(self):
        client = api.SiteClient(self.url, "cct_good", "1.0.0")
        self.assertEqual("Admin", client.me()["user"]["name"])
        self.assertEqual("Rise", client.known_tournaments()[0]["name"])
        headers = _Handler.requests[-1][2]
        self.assertEqual("Bearer cct_good", headers["Authorization"])
        self.assertEqual("cct_good", headers["X-Api-Token"])

    def test_errors_carry_status_and_reasons(self):
        with self.assertRaises(api.ApiError) as bad_token:
            api.SiteClient(self.url, "cct_bad").me()
        self.assertEqual(401, bad_token.exception.status)
        self.assertIn("invalid", bad_token.exception.describe())

        client = api.SiteClient(self.url, "cct_good")
        with self.assertRaises(api.ApiError) as invalid:
            client.send_tournament({"rows": []})
        self.assertEqual(422, invalid.exception.status)
        self.assertIn("At least one player.", invalid.exception.describe())

        self.assertEqual(12, client.send_tournament({"rows": [{"position": 1}]})["event_number"])
        self.assertEqual("/site/api/v1/tournaments", _Handler.requests[-1][1])

    def test_unreachable_site_and_missing_token(self):
        with self.assertRaises(api.ApiError) as missing:
            api.SiteClient(self.url, "").me()
        self.assertEqual(0, missing.exception.status)
        with self.assertRaises(api.ApiError):
            api.SiteClient("http://127.0.0.1:1", "cct_good").me()

    def test_site_url_is_normalised(self):
        self.assertEqual("https://kokmain.counter.li", api.normalize_site_url(" kokmain.counter.li/ "))
        self.assertEqual("http://localhost/chestcounter", api.normalize_site_url("http://localhost/chestcounter/api/v1"))


class SentHistoryTest(unittest.TestCase):
    def test_each_site_remembers_its_own_sent_tournaments(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = SentHistory("https://kokmain.counter.li", tmp)
            history.mark(RESULT_UID, {"event_number": 12})

            again = SentHistory("kokmain.counter.li/api/v1", tmp)
            self.assertEqual(12, again.info(RESULT_UID)["event_number"])
            self.assertIsNone(SentHistory("https://other.counter.li", tmp).info(RESULT_UID))

    def test_a_site_address_becomes_a_file_name(self):
        self.assertEqual("kokmain.counter.li", site_slug("https://KokMain.counter.li/"))
        self.assertEqual("localhost_chestcounter", site_slug("http://localhost/chestcounter/api/v1"))


if __name__ == "__main__":
    unittest.main()
