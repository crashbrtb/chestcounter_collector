"""Tournament names read from Journal card titles."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.events.titles import extract_tournament_name  # noqa: E402


class ExtractTournamentNameTest(unittest.TestCase):
    def test_results_in(self):
        self.assertEqual(
            extract_tournament_name("Your Clanmates' results in the Rise of the Ancients tournament 9 days ago, 14:00"),
            "Rise of the Ancients tournament",
        )

    def test_scores_in(self):
        self.assertEqual(extract_tournament_name("Your Clanmates' scores in Epic Hunt 12:30"), "Epic Hunt")

    def test_longer_titles_keep_the_whole_rest(self):
        self.assertEqual(
            extract_tournament_name("Your clanmates' score of Remnants of Dread in battles with Dark Omens. 9 days ago, 14:00"),
            "score of Remnants of Dread in battles with Dark Omens",
        )
        self.assertEqual(
            extract_tournament_name("Your clanmates' contributions of Omen Essence in the Dark Omens tournament. 9 days ago, 14:00"),
            "contributions of Omen Essence in the Dark Omens tournament",
        )

    def test_when_the_card_arrived_is_not_part_of_the_name(self):
        for title, name in [
            ("Your Clanmates' results in the Trials of Olympus tournament. today, 15:00", "Trials of Olympus tournament"),
            ("Your Clanmates' results in the Great Hunt tournament. yesterday, 14:00", "Great Hunt tournament"),
            ("Your Clanmates' results in the Great Hunt tournament 3 hours ago", "Great Hunt tournament"),
            ("Resultados dos membros do clã no Grande Caçada. ontem, 14:00", "Grande Caçada"),
        ]:
            self.assertEqual(name, extract_tournament_name(title), title)

    def test_a_name_that_begins_like_a_date_is_kept(self):
        self.assertEqual("Today's Trial tournament", extract_tournament_name("Your Clanmates' results in the Today's Trial tournament. today, 15:00"))

    def test_other_messages_are_not_tournaments(self):
        self.assertIsNone(extract_tournament_name("The Ruthless Slaughter is over. Here are the most furious warriors! 9 days ago, 14:00"))
        self.assertIsNone(extract_tournament_name("{You attacked: Epic Arcane Squad"))
        self.assertIsNone(extract_tournament_name("Raid canceled. None of your clanmates joined you (K:282 X:549 Y:527) yesterday, 10:52"))

    def test_stray_ocr_marks_before_the_title(self):
        self.assertEqual(extract_tournament_name("| Your Clanmates' scores in Epic Hunt"), "Epic Hunt")
        self.assertEqual(extract_tournament_name("a Your Clanmates' scores in Epic Hunt"), "Epic Hunt")


class CardAgeTest(unittest.TestCase):
    def test_reads_how_many_days_ago_a_card_was_received(self):
        from modules.events.titles import card_age_days

        for text, days in [
            ("Your Clanmates' results in the Great Hunt tournament. today, 15:00", 0),
            ("Your Clanmates' results in the Great Hunt tournament. yesterday, 14:00", 1),
            ("Your Clanmates' results in the Great Hunt tournament. 2 days ago, 14:00", 2),
            ("Raid canceled. None of your clanmates joined you (K:282 X:549 Y:527) 7 days ago, 10:52", 7),
            ("Your Clanmates' results in the Great Hunt tournament 12days ago, 14:00", 12),
            ("Your Clanmates' results in the Great Hunt tournament 3 hours ago", 0),
            ("Resultados dos membros do clã no torneio Grande Caçada ontem, 14:00", 1),
            ("Resultados dos membros do clã no torneio Grande Caçada 5 dias atrás, 14:00", 5),
            ("Resultados dos membros do clã no torneio Grande Caçada há 5 dias, 14:00", 5),
            ("Your Clanmates' results in the Great Hunt tournament 2 weeks ago", 14),
            # The card's own age is the last one: the name may hold another.
            ("Your Clanmates' results in the 3 days ago Memorial yesterday, 14:00", 1),
        ]:
            self.assertEqual(days, card_age_days(text), text)

    def test_a_title_that_does_not_say_is_not_guessed(self):
        from modules.events.titles import card_age_days

        for text in ("", "Your Clanmates' results in the Great Hunt tournament", "Great Hunt 14:00"):
            self.assertIsNone(card_age_days(text), text)


if __name__ == "__main__":
    unittest.main()
