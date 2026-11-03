"""The audit of chest types, on the database as it stood before 5784 and 7013 were put right."""
import unittest
from datetime import datetime

from tools.audit_chest_types import audit

NOW = datetime(2026, 10, 19, 9, 0)
EVENT = (datetime(2026, 10, 12, 14, 0), datetime(2026, 10, 14, 19, 42))
WEEK = (datetime(2026, 10, 12, 15, 0), datetime(2026, 10, 19, 8, 18))

CRYPT35, FENRIR, JORMUN = "Level 35 epic Crypt", "Epic Fenrir squad", "Epic Jormungandr squad"


def chests(name, source, rows, gifts, when=WEEK):
    return [(name, source, rows, gifts, *when)]


WRITTEN = {
    2084: chests("Fenrir's Chest", FENRIR, 5475, 1800, EVENT),
    2085: chests("Jormungandr's Chest", JORMUN, 1955, 650, EVENT),
    5781: chests("Abandoned Chest", CRYPT35, 280, 280),
    5782: chests("Scorpion Chest", CRYPT35, 294, 294),
    5783: chests("Turtle Chest", CRYPT35, 276, 276),
    5784: chests("Fenrir's Chest", FENRIR, 352, 352),
    5785: chests("White Wood Chest", CRYPT35, 301, 301),
    5786: chests("Jormungandr's Chest", JORMUN, 329, 329),
    5787: chests("Barbarian Chest", CRYPT35, 261, 261),
    5514: chests("Mayan Chest", "Level 25 Crypt", 633, 633),
    7010: chests("Elven Citadel Chest", "Level 10 Citadel", 15, 15),
    7011: chests("Elven Citadel Chest", "Level 15 Citadel", 3, 3),
    7012: chests("Elven Citadel Chest", "Level 20 Citadel", 6, 6),
    7013: chests("Mayan Chest", "Level 25 Crypt", 188, 188),
    7014: chests("Elven Citadel Chest", "Level 30 Citadel", 846, 846),
    # Real twins, numbered side by side, are left alone.
    6511: chests("Rare Dragon Chest", "Level 25 rare Crypt", 1035, 1035),
    6512: chests("Rare Dragon Chest", "Level 25 rare Crypt", 898, 898),
}
SCREEN = {CRYPT35: {"Chest of the Cursed": 39, "Cobalt Chest": 38, "Turtle Chest": 43},
          "Level 25 Citadel": {"Elven Citadel Chest": 938}}


class Audit(unittest.TestCase):
    def found(self, table=None):
        return {f["type"]: f for f in audit(WRITTEN, table or {}, SCREEN, now=NOW)}

    def test_a_crypt_chest_counted_as_an_event_squads_is_found(self):
        found = self.found()
        for type_id in (5784, 5786):
            signs = " | ".join(found[type_id]["signs"])
            for sign in ("block:", "twin:", "quantity:", "outlives:"):
                self.assertIn(sign, signs, type_id)
        self.assertIn("Chest of the Cursed, Cobalt Chest", found[5784]["hints"][0])

    def test_a_citadel_chest_counted_as_a_crypts_is_found(self):
        found = self.found()
        signs = " | ".join(found[7013]["signs"])
        self.assertIn("'Elven Citadel Chest'", signs)
        self.assertIn("twin: 'Mayan Chest' (Level 25 Crypt) is also type 5514", signs)
        self.assertIn("Level 25 Citadel", found[7013]["hints"][0])

    def test_the_table_disagreeing_with_the_rows_is_a_sign(self):
        found = self.found({5784: ("Chest of the Cursed", CRYPT35)})
        self.assertTrue(any(s.startswith("table: 352 row(s)") for s in found[5784]["signs"]))

    def test_chests_named_right_are_left_alone(self):
        found = self.found()
        for type_id in (2084, 2085, 5781, 5782, 5783, 5785, 5787, 7010, 7014, 6511, 6512):
            self.assertNotIn(type_id, found)


if __name__ == "__main__":
    unittest.main()
