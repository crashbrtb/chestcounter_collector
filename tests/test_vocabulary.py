"""Whose chest it is: the clan's member list placing a reading on one member, or declining to."""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.vocabulary import ROSTER_MAX_AGE_HOURS, Roster, load_vocabulary  # noqa: E402
from utils.text_utils import ocr_key  # noqa: E402

NOW = datetime(2026, 10, 12, 3, 0, 0)

# The clan as the game lists it. Alok BR and ALOK BR are two people.
ACTIVE = ["Alok BR", "ALOK BR", "HeStIa BR", "DRIKA BRف", "么l2M flayir 2 么", "Tony Stanna", "Ventura", "LINDA BRف"]


class FakeConnection:
    """A profile's database, answering the queries the vocabulary asks by what they mention."""

    def __init__(self, list_read_hours_ago=1.0, active=ACTIVE, job_runs=True):
        self.list_read_hours_ago = list_read_hours_ago
        self.active = active
        self.job_runs = job_runs
        self.sql = ""

    def cursor(self):
        return self

    def close(self):
        pass

    def execute(self, sql):
        self.sql = sql
        if "job_runs" in sql and not self.job_runs:
            raise RuntimeError("Table 'job_runs' doesn't exist")

    def fetchall(self):
        sql = self.sql
        if "job_runs" in sql:
            if self.list_read_hours_ago is None:
                return []
            read_at = NOW - timedelta(hours=self.list_read_hours_ago)
            return [(json.dumps({"read_at": read_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "members": len(self.active)}), NOW)]
        if "FROM members WHERE active = 1" in sql:
            return [(name,) for name in self.active]
        if "SELECT player FROM members" in sql:
            # Every member, the ones who left included.
            return [(name,) for name in self.active + ["Gone Long Ago"]]
        if "DISTINCT correct_name" in sql:
            return [("Éomer",)]
        if "SELECT player, COUNT(*)" in sql:
            return [("ALOK BR", 3307), ("Ventura III", 599), ("LINDA", 510), ("Gone Long Ago", 40)]
        if "ocr_text, correct_name" in sql:
            return [("Eomer", "Éomer")]
        return []


class RosterTest(unittest.TestCase):
    def test_the_key_keeps_what_a_screen_reading_cannot_change(self):
        self.assertEqual(ocr_key("HeStla BR"), ocr_key("HeStIa BR"))
        self.assertEqual(ocr_key("DRIKA BR"), ocr_key("DRIKA BRف"))
        self.assertEqual(ocr_key("ZIC0 1O"), ocr_key("zico lo"))
        self.assertNotEqual(ocr_key("Ventura"), ocr_key("Ventura III"))
        self.assertNotEqual(ocr_key("UČUR 1907"), ocr_key("UĞUR 1907"), "a different letter is a different name")

    def test_a_reading_is_placed_on_the_one_member_it_can_be(self):
        roster = Roster(ACTIVE)
        self.assertEqual(("HeStIa BR", "member"), roster.resolve("HeStla BR"))
        self.assertEqual(("DRIKA BRف", "member"), roster.resolve("DRIKA BR"))
        self.assertEqual(("么l2M flayir 2 么", "member"), roster.resolve("么l2M flayir 2么"))
        self.assertEqual(("Tony Stanna", "member"), roster.resolve(" TonyStanna "))
        self.assertEqual(("Ventura", "member"), roster.resolve("Ventura"))
        # Nobody in the clan is like these: the list has no say.
        self.assertEqual((None, ""), roster.resolve("Ventura III"))
        self.assertEqual((None, ""), roster.resolve("LINDA"))
        self.assertEqual((None, ""), roster.resolve(""))

    def test_members_a_reading_cannot_tell_apart_are_never_guessed(self):
        roster = Roster(ACTIVE)
        self.assertEqual([["Alok BR", "ALOK BR"]], roster.twins)
        # Read exactly as one of them, it is that one...
        self.assertEqual(("Alok BR", "member"), roster.resolve("Alok BR"))
        self.assertEqual(("ALOK BR", "member"), roster.resolve("ALOK BR"))
        # ...and anything else with their key is for a person to decide.
        self.assertEqual((None, "ambiguous"), roster.resolve("Alok Br"))
        self.assertEqual((None, "ambiguous"), roster.resolve("AlokBR"))

    def test_a_name_with_nothing_left_to_compare_matches_nobody(self):
        roster = Roster(["么么", "Ventura"])
        self.assertEqual((None, ""), roster.resolve("!!"))
        self.assertEqual(("么么", "member"), roster.resolve("么么"), "read exactly, it is still that member")


class VocabularyRosterTest(unittest.TestCase):
    def test_a_recent_member_list_decides_before_the_history_does(self):
        vocabulary = load_vocabulary(FakeConnection())
        self.assertEqual(len(ACTIVE), len(vocabulary.roster.names))

        # A person's correction still wins outright.
        self.assertEqual(("Éomer", "mapping"), vocabulary.resolve_player("Eomer"))
        self.assertEqual(("HeStIa BR", "member"), vocabulary.resolve_player("HeStla BR"))
        # The history has 3 307 chests under 'ALOK BR'; the leader is not folded into it any more.
        self.assertEqual(("Alok BR", "member"), vocabulary.resolve_player("Alok BR"))
        self.assertEqual((None, "ambiguous"), vocabulary.resolve_player("Alok Br"))
        # Who the list does not know is looked for the old way: by what was collected before.
        self.assertEqual(("Ventura III", "known"), vocabulary.resolve_player("Ventura III"))
        self.assertEqual(("Gone Long Ago", "known"), vocabulary.resolve_player("GoneLongAgo"))
        self.assertEqual((None, "unknown"), vocabulary.resolve_player("Never Seen"))

    def test_a_list_older_than_the_limit_is_not_trusted(self):
        fresh = load_vocabulary(FakeConnection(list_read_hours_ago=ROSTER_MAX_AGE_HOURS - 0.5))
        self.assertIsNotNone(fresh.roster)

        stale = load_vocabulary(FakeConnection(list_read_hours_ago=ROSTER_MAX_AGE_HOURS + 0.5))
        self.assertIsNone(stale.roster)
        # As before the list existed: one spelling per key, the history's.
        self.assertEqual(("ALOK BR", "known"), stale.resolve_player("Alok Br"))
        self.assertEqual((None, "unknown"), stale.resolve_player("HeStla BR"))

    def test_a_database_without_a_member_list_works_as_before(self):
        for connection in (FakeConnection(list_read_hours_ago=None), FakeConnection(job_runs=False),
                           FakeConnection(active=[])):
            vocabulary = load_vocabulary(connection)
            self.assertIsNone(vocabulary.roster)
            self.assertEqual(("Ventura III", "known"), vocabulary.resolve_player("ventura iii"))


if __name__ == "__main__":
    unittest.main()
