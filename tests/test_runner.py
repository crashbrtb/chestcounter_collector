"""The run: which module is done on which profile, when, and what a failure costs."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import AccountConfig, ProfileConfig  # noqa: E402
from core.runner import ModuleRunner, RunAlreadyActive, RunnerError  # noqa: E402
from core.schedule import SCHEDULE_DAILY  # noqa: E402
from fakes import Clock, FakeContext, make_calibration, make_config, make_schedule  # noqa: E402
from modules.base_module import BaseModule  # noqa: E402
from modules.chests import ChestCollector  # noqa: E402
from modules.events import EventsModule  # noqa: E402
from utils.run_lock import RunLock  # noqa: E402


class Scripted(BaseModule):
    """A module that records what it was asked and answers what the test scripted."""

    def __init__(self, context, log):
        super().__init__(context)
        self.log = log
        self.answers = {}       # profile name -> list of results, or exceptions, one per call
        self.began = None
        self.ended = None

    def begin_run(self, profiles):
        self.began = [p.name for p in profiles]

    def end_run(self, results, duration="", error=None, cancelled=False):
        self.ended = {"results": results, "error": error, "cancelled": cancelled}

    def run_for_profile(self, profile):
        self.log.append((self.key, profile.name))
        queue = self.answers.get(profile.name) or [{"success": True}]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


class Chests(Scripted):
    key, title = "chests", "Clan chests"
    required_steps = ChestCollector.required_steps
    configured_for = classmethod(lambda cls, profile: profile.has_database)


class Events(Scripted):
    key, title = "events", "Tournament rankings"
    required_steps = EventsModule.required_steps
    fresh_browser = EventsModule.fresh_browser
    configured_for = classmethod(lambda cls, profile: EventsModule.configured_for(profile))


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self.tmp.name)
        self.config.set("execution", "screenshot_on_error", False)
        self.clock = Clock(datetime(2026, 10, 7, 3, 0))
        self.log = []

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, complete=True):
        self.ctx = FakeContext(self.config, make_calibration(self.tmp.name, self.config, complete))
        self.chests, self.events = Chests(self.ctx, self.log), Events(self.ctx, self.log)
        return ModuleRunner(self.ctx, [self.chests, self.events], make_schedule(self.tmp.name, self.clock),
                            lock_path=self.lock_path)

    @property
    def lock_path(self):
        return os.path.join(self.tmp.name, "run.lock")

    def run_again(self, **kwargs):
        """A later start of the application: new process, same files."""
        self.log.clear()
        return self.runner().run(**kwargs)

    # -------------------------------------------------------- one at a time
    def test_a_run_does_not_start_while_another_holds_the_lock(self):
        other = RunLock(self.lock_path)
        self.assertTrue(other.acquire())
        try:
            with self.assertRaises(RunAlreadyActive):
                self.runner().run()
            self.assertEqual([], self.log)
            self.assertEqual([], self.ctx.profiles.switched)
        finally:
            other.release()

        self.runner().run()
        self.assertIn(("chests", "Alpha"), self.log)

    # ------------------------------------------------------- fresh browser
    def test_a_run_with_tournament_rankings_due_starts_on_a_fresh_browser(self):
        runner = self.runner()
        runner.run()
        self.assertEqual(1, self.ctx.browser.restarts)

    def test_a_chest_only_run_keeps_the_open_browser(self):
        runner = self.runner()
        runner.run(only_modules=["chests"])
        self.assertEqual(0, self.ctx.browser.restarts)

    def test_the_fresh_browser_can_be_turned_off(self):
        self.config.set("execution", "fresh_browser_for_rankings", False)
        runner = self.runner()
        runner.run()
        self.assertEqual(0, self.ctx.browser.restarts)
        self.assertIn(("events", "Alpha"), self.log)

    def test_the_lock_is_released_when_the_run_fails(self):
        runner = self.runner()
        runner._locked_run = lambda *args: (_ for _ in ()).throw(RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            runner.run()

        other = RunLock(self.lock_path)
        self.assertTrue(other.acquire())
        other.release()

    # ------------------------------------------------------------- the order
    def test_each_profile_gets_the_modules_set_up_for_it_chests_first(self):
        summary = self.runner().run()

        # Alpha has a database and a clan site; Beta only a database.
        self.assertEqual([("chests", "Alpha"), ("events", "Alpha"), ("chests", "Beta")], self.log)
        self.assertEqual(["Alpha", "Beta"], self.ctx.profiles.switched)
        self.assertEqual([], summary["failures"])
        self.assertEqual(2, summary["profiles_done"])
        self.assertEqual(["Alpha", "Beta"], self.chests.began)
        self.assertEqual(["Alpha"], self.events.began)
        self.assertEqual({"chests"}, {r["module"] for r in self.chests.ended["results"]})

    def test_the_profile_the_game_opened_on_is_done_first(self):
        runner = self.runner()
        self.ctx.profiles.active = "Beta"
        runner.run()

        self.assertEqual([("chests", "Beta"), ("chests", "Alpha"), ("events", "Alpha")], self.log)
        self.assertEqual(["Beta", "Alpha"], self.ctx.profiles.switched)

    def test_a_profile_the_game_opened_on_with_nothing_due_does_not_change_the_order(self):
        runner = self.runner()
        self.ctx.profiles.active = "Beta"
        runner.run(only_modules=["events"])
        self.assertEqual([("events", "Alpha")], self.log)

        runner = self.runner()
        self.ctx.profiles.active = "Someone else"
        self.log.clear()
        runner.run(force=True)
        self.assertEqual([("chests", "Alpha"), ("events", "Alpha"), ("chests", "Beta")], self.log)

    # ---------------------------------------------------------- the schedule
    def test_the_daily_module_runs_once_a_day_and_the_other_every_time(self):
        self.runner().run()

        self.clock.now += timedelta(hours=2)
        self.run_again()
        self.assertEqual([("chests", "Alpha"), ("chests", "Beta")], self.log)
        self.assertIsNone(self.events.began, "a module with nothing to do is not told a run started")

        self.run_again(force=True)
        self.assertIn(("events", "Alpha"), self.log)

        self.clock.now += timedelta(days=1)
        self.run_again()
        self.assertIn(("events", "Alpha"), self.log)

    def test_an_incomplete_daily_run_is_tried_again_on_the_next_start_up_to_the_limit(self):
        self.config.set("modules.events", "daily_attempts", 2)
        runner = self.runner()
        self.events.answers["Alpha"] = [{"success": False, "retry": False, "reason": "incomplete search"}]
        summary = runner.run()

        self.assertEqual(1, self.log.count(("events", "Alpha")), "retry: False is not repeated in the same run")
        self.assertEqual(["incomplete search"], [f["reason"] for f in summary["failures"]])

        runner = self.runner()
        self.events.answers["Alpha"] = [{"success": False, "retry": False, "reason": "incomplete search"}]
        self.log.clear()
        runner.run()
        self.assertIn(("events", "Alpha"), self.log, "second start of the day: tried again")

        self.run_again()
        self.assertNotIn(("events", "Alpha"), self.log, "two failures today: next try tomorrow")

    def test_when_nothing_is_due_the_browser_is_not_even_opened(self):
        self.config.set("modules.chests", "schedule", SCHEDULE_DAILY)
        self.runner().run()
        self.assertEqual(1, self.ctx.browser_starts)

        summary = self.run_again()
        self.assertEqual([], self.log)
        self.assertEqual(0, self.ctx.browser_starts)
        self.assertEqual([], summary["failures"])

    def test_one_module_only_on_request(self):
        self.runner().run(only_modules=["events"])
        self.assertEqual([("events", "Alpha")], self.log)
        self.assertEqual(["Alpha"], self.ctx.profiles.switched, "Beta has nothing to do: not switched to")

        with self.assertRaises(RunnerError):
            self.runner().run(only_modules=["nope"])

    # -------------------------------------------------------------- failures
    def test_a_module_that_fails_is_retried_without_repeating_the_one_that_worked(self):
        runner = self.runner()
        self.chests.answers["Alpha"] = [{"success": False, "reason": "gift menu did not open"},
                                        {"success": True, "collected": 7}]
        summary = runner.run()

        self.assertEqual([("chests", "Alpha"), ("events", "Alpha"), ("chests", "Alpha"), ("chests", "Beta")],
                         self.log)
        self.assertEqual([], summary["failures"], "the last attempt is what counts")
        self.assertEqual(7, summary["collected"])

    def test_a_module_that_crashes_does_not_take_the_next_one_with_it(self):
        runner = self.runner()
        self.chests.answers["Alpha"] = [RuntimeError("database exploded")]
        summary = runner.run()

        self.assertIn(("events", "Alpha"), self.log)
        self.assertEqual(2, self.log.count(("chests", "Alpha")), "retries_per_profile")
        [failure] = summary["failures"]
        self.assertEqual(("chests", "Alpha", "all attempts failed"),
                         (failure["module"], failure["profile"], failure["reason"]))
        # The events module succeeded and is told only about its own results.
        self.assertEqual([True], [r["success"] for r in self.events.ended["results"]])
        self.assertEqual([False, True], sorted(r["success"] for r in self.chests.ended["results"]))

    def test_a_profile_that_cannot_be_reached_fails_every_module_due_on_it(self):
        runner = self.runner()
        self.ctx.profiles.reachable = False
        summary = runner.run()

        self.assertEqual([], self.log)
        self.assertEqual({("chests", "Alpha"), ("events", "Alpha"), ("chests", "Beta")},
                         {(f["module"], f["profile"]) for f in summary["failures"]})
        # One because the tournament rankings are due, one because nothing was done on the account.
        self.assertEqual(2, self.ctx.browser.restarts, "a fresh browser to start, then one for the retry")

    def test_a_module_whose_screens_were_never_calibrated_is_left_out_and_the_rest_runs(self):
        summary = self.runner(complete=False).run()

        self.assertEqual([("chests", "Alpha"), ("chests", "Beta")], self.log)
        [failure] = summary["failures"]
        self.assertEqual("events", failure["module"])
        self.assertIn("Incomplete calibration", failure["reason"])
        self.assertIn("Journal button", failure["reason"])

    def test_without_the_shared_steps_nothing_starts(self):
        runner = self.runner()
        self.ctx.calibration.forget("profile_menu_button")
        with self.assertRaises(RunnerError) as refused:
            runner.run()
        self.assertIn("Profile menu", str(refused.exception))
        self.assertEqual("Execution cannot start: " + str(refused.exception).split(": ", 1)[1],
                         self.chests.ended["error"], "the modules hear how the run ended")
        self.assertEqual(0, self.ctx.browser_starts)

    def test_a_profile_with_nothing_set_up_is_reported(self):
        self.config.accounts = [AccountConfig(name="Main", login="a", password="b", profiles=[
            ProfileConfig(name="Alpha", database=self.config.accounts[0].profiles[0].database, account_name="Main"),
            ProfileConfig(name="Empty", account_name="Main"),
        ])]
        summary = self.runner().run()
        self.assertEqual([("chests", "Alpha")], self.log)
        self.assertEqual([("", "Empty")], [(f["module"], f["profile"]) for f in summary["failures"]])

    def test_check_lists_what_is_wrong_before_running(self):
        runner = self.runner(complete=False)
        problems = runner.preflight()
        self.assertEqual(1, len(problems))
        self.assertIn("Tournament rankings", problems[0])

        self.config.set("modules.events", "enabled", False)
        self.assertEqual([], ModuleRunner(FakeContext(self.config, self.ctx.calibration),
                                          [self.chests]).preflight())


if __name__ == "__main__":
    unittest.main()
