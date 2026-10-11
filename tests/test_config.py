"""The configuration file: module sections, per-profile module values, and old files."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.schema import all_sections, field_for  # noqa: E402
from config.settings import ConfigManager  # noqa: E402
from core.calibration import SHARED_GROUP, Calibration, Step, StepGroup  # noqa: E402
from core.schedule import SCHEDULE_DAILY, SCHEDULE_EVERY_RUN  # noqa: E402
from fakes import Clock, make_calibration, make_config, make_schedule  # noqa: E402
from modules.registry import MODULES, calibration_scope, step_groups  # noqa: E402


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_module_has_its_switch_and_schedule_with_defaults(self):
        config = ConfigManager(self.path)
        self.assertEqual({"chests", "members", "events"}, set(config.data["modules"]))
        self.assertTrue(config.module("chests")["enabled"])
        self.assertEqual(SCHEDULE_EVERY_RUN, config.module("chests")["schedule"])
        self.assertEqual(SCHEDULE_DAILY, config.module("events")["schedule"])
        for module in MODULES:
            keys = [f.key for f in module.section().fields]
            self.assertEqual(["enabled", "schedule", "daily_attempts"], keys[:3])

    def test_module_values_are_validated_and_survive_a_save(self):
        config = ConfigManager(self.path)
        config.set("modules.events", "enabled", "false")
        config.set("modules.events", "schedule", "whenever")       # not a choice: back to the default
        config.set("modules.events", "reload_wait", 5)             # below the minimum
        config.set("modules.chests", "schedule", SCHEDULE_DAILY)
        config.save()

        again = ConfigManager(self.path)
        self.assertFalse(again.module("events")["enabled"])
        self.assertEqual(SCHEDULE_DAILY, again.module("events")["schedule"])
        self.assertEqual(10.0, again.module("events")["reload_wait"])
        self.assertEqual(SCHEDULE_DAILY, again.get("modules.chests", "schedule"))
        self.assertEqual("bool", field_for("modules.events", "enabled").type)

    def test_a_file_from_before_the_modules_loads_and_keeps_its_values(self):
        old = {
            "execution": {"module": "chests", "retries_per_profile": 4},
            "browser": {"cdp_port": 9333},
            "accounts": [{"name": "Main", "login": "a", "password": "b", "enabled": True, "browser_profile": "",
                          "profiles": [{"name": "Alpha", "enabled": True,
                                        "database": {"host": "h", "port": 3306, "user": "u",
                                                     "password": "p", "database": "d"}}]}],
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(old, fh)

        config = ConfigManager(self.path)
        self.assertEqual(4, config.get("execution", "retries_per_profile"))
        self.assertEqual(9333, config.get("browser", "cdp_port"))
        self.assertNotIn("module", config.section("execution"))
        self.assertTrue(config.module("chests")["enabled"])
        [account] = config.accounts
        self.assertTrue(account.profiles[0].has_database)
        self.assertEqual({}, account.profiles[0].modules)

    def test_each_profile_keeps_its_own_module_values(self):
        with tempfile.TemporaryDirectory() as folder:
            config = make_config(folder)
            alpha, beta = ConfigManager(config.path).accounts[0].profiles
            self.assertEqual("cct_a", alpha.module_settings("events")["api_token"])
            self.assertEqual({}, beta.module_settings("events"))
            self.assertEqual("Main", alpha.account_name)
            self.assertEqual(["Alpha", "Beta"], config.profile_names("Main"))

            events = next(m for m in MODULES if m.key == "events")
            chests = next(m for m in MODULES if m.key == "chests")
            self.assertTrue(events.configured_for(alpha))
            self.assertFalse(events.configured_for(beta))
            self.assertTrue(chests.configured_for(beta))
            self.assertIn("clan-a.example.com", events.profile_summary(alpha))

    def test_all_sections_have_distinct_keys(self):
        keys = [section.key for section in all_sections()]
        self.assertEqual(len(keys), len(set(keys)))


class CalibrationGroupsTest(unittest.TestCase):
    def test_shared_steps_come_first_and_each_module_brings_its_own(self):
        groups = step_groups()
        self.assertEqual(["shared", "chests", "members", "events"], [g.key for g in groups])
        names = [step.name for group in groups for step in group.steps]
        self.assertEqual(len(names), len(set(names)), "a step is calibrated once")
        self.assertIn("profile_menu_button", [s.name for s in groups[0].steps])
        self.assertIn("open_button", [s.name for s in groups[1].steps])
        # The clan button is also the members module's, and stays with the chests.
        self.assertEqual(["clan_members_button", "clan_members_list"], [s.name for s in groups[2].steps])
        self.assertIn("journal_show_details", [s.name for s in groups[3].steps])

    def test_a_step_two_modules_declare_is_calibrated_once(self):
        first = StepGroup("a", "A", [Step("clan_button", "click", "Clan", "")])
        with tempfile.TemporaryDirectory() as folder:
            cal = Calibration([SHARED_GROUP, first], os.path.join(folder, "c.json"), folder)
            cal.set_viewport(1000, 800)
            cal.set_point("clan_button", 5, 6)
            cal.save()
            # Another module reading the same name gets the same point.
            again = Calibration([SHARED_GROUP, first], cal.path, folder)
            self.assertEqual((5, 6), again.point("clan_button"))

    def test_only_the_modules_in_use_make_the_calibration_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            config = make_config(folder)
            calibration = make_calibration(folder, config, complete=False)
            # Alpha has a clan site, so the Journal steps are asked for.
            self.assertIn("journal_button", calibration.missing_steps())

            config.set("modules.events", "enabled", False)
            calibration.required = calibration_scope(config)
            self.assertEqual([], calibration.missing_steps())
            self.assertTrue(calibration.complete)

    def test_a_calibration_file_from_before_the_modules_still_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "calibration.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"viewport": {"width": 1536, "height": 731},
                           "points": {"profile_menu_button": [10, 20], "clan_button": [30, 40]},
                           "regions": {"chest_area": {"left": 1, "top": 2, "width": 30, "height": 40}},
                           "step_viewports": {"clan_button": [1536, 731]}}, fh)
            calibration = Calibration(step_groups(), path, folder)
            self.assertTrue(calibration.is_calibrated("clan_button"))
            self.assertTrue(calibration.is_calibrated("chest_area"))
            self.assertFalse(calibration.is_calibrated("journal_button"))
            self.assertEqual("Clan button, Journal button", calibration.titles(["clan_button", "journal_button"]))


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(datetime(2026, 10, 7, 3, 0))
        self.schedule = make_schedule(self.tmp.name, self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_run_is_always_due(self):
        self.schedule.record("chests", "Main", "Alpha", True)
        self.assertEqual((True, ""), self.schedule.is_due("chests", SCHEDULE_EVERY_RUN, "Main", "Alpha"))

    def test_once_a_day_is_due_until_it_succeeds_and_again_tomorrow(self):
        due = lambda: self.schedule.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 3)[0]  # noqa: E731
        self.assertTrue(due())
        self.schedule.record("events", "Main", "Alpha", True)
        self.assertFalse(due())
        self.assertTrue(self.schedule.is_due("events", SCHEDULE_DAILY, "Main", "Beta", 3)[0], "per profile")

        # Another process, later the same day: the file is what remembers.
        self.clock.now += timedelta(hours=6)
        again = make_schedule(self.tmp.name, self.clock)
        due_again, why = again.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 3)
        self.assertFalse(due_again)
        self.assertIn("03:00", why)

        self.clock.now += timedelta(days=1)
        self.assertTrue(again.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 3)[0])

    def test_a_failed_day_is_retried_a_limited_number_of_times(self):
        for _ in range(2):
            self.assertTrue(self.schedule.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 2)[0])
            self.schedule.record("events", "Main", "Alpha", False)
        due, why = self.schedule.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 2)
        self.assertFalse(due)
        self.assertIn("tomorrow", why)

        self.clock.now += timedelta(days=1)
        self.assertTrue(self.schedule.is_due("events", SCHEDULE_DAILY, "Main", "Alpha", 2)[0])


if __name__ == "__main__":
    unittest.main()
