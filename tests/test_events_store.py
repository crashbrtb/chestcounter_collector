"""What the events module remembers between runs: how far back the next search has to go."""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.events.store import SearchMemory  # noqa: E402

SITE = "https://clan-a.example.com"


class SearchMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def memory(self):
        return SearchMemory(SITE, self.tmp.name)

    def test_the_first_search_walks_every_day_configured(self):
        self.assertEqual((7, False), self.memory().days_to_walk(7, date(2026, 10, 11)))

    def test_later_ones_walk_the_days_since_the_last_complete_search(self):
        self.memory().record(datetime(2026, 10, 8, 3, 0), 7)

        self.assertEqual((1, False), self.memory().days_to_walk(7, date(2026, 10, 8)))
        self.assertEqual((2, False), self.memory().days_to_walk(7, date(2026, 10, 9)))
        self.assertEqual((4, False), self.memory().days_to_walk(7, date(2026, 10, 11)))
        # Never more than what is configured, however long it has been.
        self.assertEqual((7, False), self.memory().days_to_walk(7, date(2026, 12, 1)))
        self.assertEqual((3, False), self.memory().days_to_walk(3, date(2026, 10, 11)))

    def test_raising_the_days_walks_all_of_them_once(self):
        self.memory().record(datetime(2026, 10, 10, 3, 0), 3)
        self.assertEqual((14, True), self.memory().days_to_walk(14, date(2026, 10, 11)))

        self.memory().record(datetime(2026, 10, 11, 3, 0), 14)
        self.assertEqual((2, False), self.memory().days_to_walk(14, date(2026, 10, 12)))

    def test_each_site_has_its_own(self):
        self.memory().record(datetime(2026, 10, 10, 3, 0), 7)
        self.assertIsNone(SearchMemory("https://clan-b.example.com", self.tmp.name).last_search())


if __name__ == "__main__":
    unittest.main()
