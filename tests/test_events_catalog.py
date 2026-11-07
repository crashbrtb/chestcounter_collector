import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.events import autosend, catalog, journal, mapper  # noqa: E402
from test_events_journal import ENDED_AT  # noqa: E402
from test_events_mapper import FakeFeed, ranking_entry  # noqa: E402
from test_events_autosend import FakeSite  # noqa: E402
from utils.cancel import cancellation  # noqa: E402


class TournamentCatalogTest(unittest.TestCase):
    def test_clean_clanmates_title_extracts_exact_names(self):
        self.assertEqual(
            "Great Hunt tournament",
            catalog.clean_clanmates_title("Your Clanmates' results in the Great Hunt tournament."),
        )
        self.assertEqual(
            "Union of Triumph tournament",
            catalog.clean_clanmates_title("Your Clanmates' scores in the Union of Triumph tournament."),
        )
        self.assertEqual(
            "contributions of Omen Essence in the Dark Omens tournament",
            catalog.clean_clanmates_title(
                "Your Clanmates' contributions of Omen Essence in the Dark Omens tournament."
            ),
        )
        self.assertEqual(
            "score of Remnants of Dread in battles with Dark Omens",
            catalog.clean_clanmates_title(
                "Your clanmates' score of Remnants of Dread in battles with Dark Omens."
            ),
        )

    def test_local_cache_resolves_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "tournament_types.json")
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "version": {"no_duplicates": "v1", "strings": "s1", "en_US": "m1"},
                        "rankings": {
                            "1024|": "Rise of the Ancients tournament",
                            "1032|multistage_tournament_clan_statistics_entry": "Trials of Olympus tournament",
                        },
                        "types": {
                            "1007": "Doomsday tournament",
                            "1024": "Rise of the Ancients tournament",
                            "1032": "Trials of Olympus tournament",
                        },
                    },
                    fh,
                )

            cat = catalog.TournamentCatalog(cache_path=cache_path)
            with mock.patch.object(catalog, "fetch_cdn_version") as mock_cdn:
                self.assertEqual("Rise of the Ancients tournament", cat.lookup(1024, ""))
                self.assertEqual(
                    "Trials of Olympus tournament",
                    cat.lookup(1032, "multistage_tournament_clan_statistics_entry"),
                )
                self.assertEqual("Doomsday tournament", cat.lookup(1007, ""))
                mock_cdn.assert_not_called()

    def test_cdn_is_queried_only_on_unknown_tournament_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "tournament_types.json")
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "version": {"no_duplicates": "v1", "strings": "s1", "en_US": "m1"},
                        "rankings": {"1024|": "Rise of the Ancients tournament"},
                        "types": {"1024": "Rise of the Ancients tournament"},
                    },
                    fh,
                )

            cat = catalog.TournamentCatalog(cache_path=cache_path)
            new_version = {"no_duplicates": "v2", "strings": "s2", "en_US": "m2"}
            with mock.patch.object(
                catalog, "fetch_cdn_version", return_value=(new_version, "m2")
            ) as mock_ver, mock.patch.object(
                catalog,
                "fetch_cdn_catalog",
                return_value=(
                    {
                        "1024|": "Rise of the Ancients tournament",
                        "1099|new_clan_ranking": "Brand New tournament",
                    },
                    {1024: "Rise of the Ancients tournament", 1099: "Brand New tournament"},
                ),
            ) as mock_dl:
                self.assertEqual("Brand New tournament", cat.lookup(1099, "new_clan_ranking"))
                mock_ver.assert_called_once()
                mock_dl.assert_called_once_with(new_version)

    def test_make_catalogue_name_names_pending_and_new_tournaments_automatically(self):
        site = FakeSite()
        known = {
            "1032|multistage_tournament_clan_statistics_entry": {
                "game_type": 1032,
                "ranking": "multistage_tournament_clan_statistics_entry",
                "tournament_key": "1032:1",
                "name": None,
            }
        }
        fake_lookup = lambda gtype, ranking="": {
            (1032, "multistage_tournament_clan_statistics_entry"): "Trials of Olympus tournament",
            (1024, ""): "Rise of the Ancients tournament",
        }.get((gtype, ranking), "")

        resolver = catalog.make_catalogue_name(site, known, lookup_fn=fake_lookup)

        # 1. Pending nameless entry on the site was immediately named via send_catalog
        self.assertEqual(1, len(site.catalogue))
        self.assertEqual("Trials of Olympus tournament", site.catalogue[0]["name"])
        self.assertEqual(
            "Trials of Olympus tournament",
            known["1032|multistage_tournament_clan_statistics_entry"]["name"],
        )

        # 2. A new tournament encountered in the Journal is named and sent without waiting
        feed = FakeFeed([ranking_entry(1, ENDED_AT, (1024, 1))])
        collector = journal.JournalCollector()
        sent = {}
        with mock.patch.object(cancellation, "sleep", lambda _s: None):
            search = autosend.AutoSearch(
                feed,
                collector,
                site,
                mapper.MapperOptions(interval=0),
                local_info=sent.get,
                mark_sent=lambda u, i: sent.__setitem__(u, i),
                catalogue_name=resolver,
                client_version="test",
            )
            summary = search.run()

        self.assertTrue(summary.complete)
        self.assertEqual(0, summary.nameless)
        self.assertEqual(1, len(site.posts))
        self.assertEqual("Rise of the Ancients tournament", site.posts[0]["name"])


if __name__ == "__main__":
    unittest.main()

