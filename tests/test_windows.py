"""Each account in its own window: the port, the folder, who may use them, and accounts at once."""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import AccountConfig, ConfigManager, DatabaseConfig, ProfileConfig  # noqa: E402
from core.browser import Browser, WrongWindow, command_line_folder, on_port  # noqa: E402
from core.context import build_context  # noqa: E402
from core.runner import ModuleRunner, RunnerError  # noqa: E402
from core.session import SESSION_MARK, LoginManager, NeedsPerson  # noqa: E402
from fakes import FakeBrowser, FakeContext, make_calibration, make_config, make_schedule  # noqa: E402
from modules.base_module import BaseModule  # noqa: E402
from utils.cancel import Cancelled, cancellation  # noqa: E402


def account(name, *databases, port=0, folder=""):
    return AccountConfig(name=name, login=f"{name}@example.com", password="pw", cdp_port=port, browser_profile=folder,
                         profiles=[ProfileConfig(name=f"{name}{i}", account_name=name,
                                                 database=DatabaseConfig(host="127.0.0.1", user="u", database=base))
                                   for i, base in enumerate(databases)])


class PinningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = ConfigManager(os.path.join(self.tmp.name, "config.json"))
        self.config.set("browser", "profiles_dir", self.tmp.name)

    def test_each_account_gets_a_port_and_a_folder_that_stay_written(self):
        self.config.accounts = [account("Main", "a"), account("Second!", "b", port=9322), account("Second?", "c")]
        self.assertTrue(self.config.pin_windows())

        again = ConfigManager(self.config.path)
        self.assertEqual([9323, 9322, 9324], [a.cdp_port for a in again.accounts])
        folders = [a.browser_profile for a in again.accounts]
        self.assertEqual(os.path.join(self.tmp.name, "Main"), folders[0])
        self.assertEqual(os.path.join(self.tmp.name, "Second_"), folders[1])
        self.assertEqual(os.path.join(self.tmp.name, "Second__2"), folders[2], "the same name after cleaning")
        self.assertFalse(again.pin_windows(), "nothing left to write")

    def test_renaming_an_account_does_not_move_its_session(self):
        self.config.accounts = [account("Main", "a")]
        self.config.pin_windows()
        [main] = self.config.accounts
        before = self.config.chrome_folder_for(main)
        main.name = "Renamed"
        self.assertEqual(before, self.config.chrome_folder_for(main))

    def test_a_folder_can_be_a_path_or_a_name_inside_the_folder_of_profiles(self):
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        self.assertEqual(elsewhere, self.config.chrome_folder_for(account("A", "a", folder=elsewhere)))
        self.assertEqual(os.path.join(self.tmp.name, "other_name"),
                         self.config.chrome_folder_for(account("A", "a", folder="other name")))

    def test_two_active_accounts_cannot_share_a_port_or_a_folder(self):
        clash = [account("A", "a", port=9400, folder="same"), account("B", "b", port=9400, folder="SAME")]
        problems = self.config.window_problems(clash)
        self.assertEqual(2, len(problems))
        self.assertIn("same port", problems[0])
        self.assertIn("same Chrome folder", problems[1])

        clash[1].enabled = False
        self.assertEqual([], self.config.window_problems(clash))

    def test_two_active_profiles_cannot_share_a_database(self):
        self.assertEqual([], self.config.database_problems([account("A", "a", "b"), account("B", "c")]))

        [problem] = self.config.database_problems([account("A", "a"), account("B", "A")])
        self.assertIn("A / A0, B / B0", problem)
        self.assertEqual(1, len(self.config.database_problems([account("A", "a", "a")])), "in the same account too")

        # The same server however it is written; another server is another database.
        other = account("B", "a")
        other.profiles[0].database.host = "localhost"
        self.assertEqual(1, len(self.config.database_problems([account("A", "a"), other])))
        other.profiles[0].database.host = "db.example.com"
        self.assertEqual([], self.config.database_problems([account("A", "a"), other]))

        # What is switched off writes nowhere.
        same = account("B", "a")
        same.profiles[0].enabled = False
        self.assertEqual([], self.config.database_problems([account("A", "a"), same]))

    def test_the_services_of_each_account_are_on_its_own_window(self):
        self.config.accounts = [account("A", "a"), account("B", "b")]
        calibration_path = os.path.join(self.tmp.name, "refs")
        from core.calibration import Calibration
        from modules.registry import step_groups
        calibration = Calibration(step_groups(), os.path.join(self.tmp.name, "c.json"), calibration_path)
        context = build_context(self.config, calibration)
        first, second = self.config.accounts

        self.assertEqual(9322, context.browser.port, "the first account's window, for the wizard and the checks")
        self.assertIs(context, context.for_account(first))
        other = context.for_account(second)
        self.assertIsNot(context, other)
        self.assertIs(other, context.for_account(second))
        self.assertEqual((9323, self.config.chrome_folder_for(second)), (other.browser.port, other.browser.user_data_dir))
        self.assertIsNot(context.calibration, other.calibration)
        self.assertEqual(calibration.path, other.calibration.path)


class OwnerTest(unittest.TestCase):
    FOLDER = r"C:\profiles\Main"

    def browser(self, *lines, ready=True):
        browser = Browser({"cdp_port": 9322}, self.FOLDER)
        browser._browser_processes = lambda: list(lines) if lines != (None,) else None
        browser.is_cdp_ready = lambda: ready
        return browser

    def line(self, port, folder):
        return rf'"C:\chrome.exe" --remote-debugging-port={port} "--user-data-dir={folder}" --no-first-run https://x'

    def test_the_folder_is_read_however_chrome_was_quoted(self):
        for text, expected in (
                (r'chrome.exe "--user-data-dir=C:\a b" --x', r"C:\a b"),
                (r'chrome.exe --user-data-dir="C:\a b" --x', r"C:\a b"),
                (r"chrome.exe --user-data-dir=C:\ab --x", r"C:\ab"),
                (r"chrome.exe --user-data-dir=C:\ab https://totalbattle.com", r"C:\ab"),
                (r"chrome.exe --user-data-dir=C:\ab", r"C:\ab"),
                ("chrome.exe --no-first-run", "")):
            self.assertEqual(expected, command_line_folder(text), text)

    def test_a_port_is_matched_whole(self):
        self.assertTrue(on_port("x --remote-debugging-port=9322 y", 9322))
        self.assertFalse(on_port("x --remote-debugging-port=93221 y", 9322))

    @unittest.skipUnless(os.name == "nt", "the command lines are read on Windows")
    def test_only_a_chrome_on_our_folder_is_ours(self):
        self.browser(self.line(9322, self.FOLDER.lower())).check_owner()

        for stranger in ((self.line(9322, r"C:\profiles\Main2"),),   # another account: tbtools, say
                         (self.line(9222, self.FOLDER),),            # nobody we can name on our port
                         (None,)):                                   # could not look: not ours
            with self.assertRaises(WrongWindow):
                self.browser(*stranger).check_owner()

    @unittest.skipUnless(os.name == "nt", "the command lines are read on Windows")
    def test_a_stranger_on_our_port_is_neither_used_nor_closed(self):
        browser = self.browser(self.line(9322, r"C:\someone\else"))
        browser.kill = lambda: self.fail("closed somebody else's window")
        browser.connect = lambda: self.fail("connected to somebody else's window")
        browser.launch = lambda: self.fail("a second Chrome could not take the port anyway")
        with self.assertRaises(WrongWindow):
            browser.start()
        with self.assertRaises(WrongWindow):
            browser.restart()

    @unittest.skipUnless(os.name == "nt", "the command lines are read on Windows")
    def test_our_folder_open_without_answering_on_the_port_is_said_instead_of_hanging(self):
        browser = self.browser(self.line(9555, self.FOLDER), ready=False)
        browser.launch = lambda: self.fail("Chrome would hand it to the open window and the port would never answer")
        with self.assertRaises(WrongWindow):
            browser.start()

        launched = []
        free = self.browser(self.line(9555, r"C:\profiles\Main2"), ready=False)
        free.launch = lambda: launched.append(True)
        free.open_for_person()
        self.assertEqual([True], launched)


class Page:
    """The browser as the login sees it: a page state and a folder."""

    def __init__(self, folder, **state):
        self.user_data_dir = folder
        self.state = state

    def wait_for_load(self, *_args, **_kwargs):
        return True

    def evaluate(self, _script):
        return self.state


class FirstLoginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.login = LoginManager(Page(self.tmp.name), {})
        self.typed = []
        self.login.login = lambda who: self.typed.append(who.name) or True
        self.who = account("Main", "a")

    def mark(self):
        open(os.path.join(self.tmp.name, SESSION_MARK), "w").close()

    def test_a_folder_that_never_logged_in_waits_for_a_person_without_typing_the_password(self):
        self.login.browser.state = {"form_in_dom": True, "opener": "Log in"}
        with self.assertRaises(NeedsPerson) as stopped:
            self.login.enter_account(self.who)
        self.assertEqual([], self.typed)
        self.assertIn("Open window", str(stopped.exception))

    def test_a_dropped_session_in_a_verified_folder_is_logged_back_into(self):
        self.mark()
        self.login.browser.state = {"form_in_dom": True, "opener": "Log in"}
        self.assertTrue(self.login.enter_account(self.who))
        self.assertEqual(["Main"], self.typed)

    def test_the_code_screen_is_not_taken_for_being_logged_in_even_in_a_verified_folder(self):
        self.mark()
        self.login.browser.state = {"form_in_dom": False, "code_asked": True}
        self.assertFalse(self.login.is_logged_in())
        with self.assertRaises(NeedsPerson):
            self.login.enter_account(self.who)
        self.assertEqual([], self.typed)

    def test_a_folder_counts_as_verified_only_once_the_game_has_come_up_in_it(self):
        path = os.path.join(self.tmp.name, SESSION_MARK)
        self.login.browser.state = {"opener": ""}
        self.login.remember_session()
        self.assertFalse(os.path.exists(path))

        self.login.browser.state = {"canvas": True}
        self.assertTrue(self.login.enter_account(self.who), "a person logged in: the game opens by itself")
        self.login.remember_session()
        self.assertTrue(os.path.exists(path))


class Chests(BaseModule):
    key, title = "chests", "Clan chests"
    required_steps = ()
    configured_for = classmethod(lambda cls, profile: profile.has_database)

    def __init__(self, context, on_profile):
        super().__init__(context)
        self.on_profile = on_profile

    def run_for_profile(self, profile):
        self.on_profile(self.ctx, profile)
        return {"success": True, "collected": 1}


class Windows(FakeContext):
    """A context with one window per account."""

    def __init__(self, config, calibration):
        super().__init__(config, calibration)
        self.windows = {}

    def for_account(self, who):
        if who.name not in self.windows:
            self.windows[who.name] = FakeContext(self.config, self.calibration, FakeBrowser())
            self.windows[who.name].browser.port = who.cdp_port
        return self.windows[who.name]


class AtOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        accounts = [account("A", "a1", "a2", port=9401), account("B", "b1", port=9402), account("C", "c1", port=9403)]
        for each in accounts:
            each.browser_profile = each.name
        self.config = make_config(self.tmp.name, accounts)
        self.addCleanup(cancellation.reset)

    def runner(self, on_profile):
        self.ctx = Windows(self.config, make_calibration(self.tmp.name, self.config))
        self.module = Chests(self.ctx, on_profile)
        from datetime import datetime
        return ModuleRunner(self.ctx, [self.module], make_schedule(self.tmp.name, datetime.now),
                            lock_path=os.path.join(self.tmp.name, "run.lock"))

    def test_accounts_overlap_each_on_its_window_and_the_profiles_of_one_do_not(self):
        barrier = threading.Barrier(3, timeout=20)
        lock = threading.Lock()
        seen, busy = [], {}

        def on_profile(context, profile):
            with lock:
                self.assertFalse(busy.get(profile.account_name), "two profiles of one account at the same time")
                busy[profile.account_name] = True
                seen.append((context.browser.port, profile.name))
            if profile.name.endswith("0"):
                barrier.wait()      # only passes with all three accounts inside at once
            with lock:
                busy[profile.account_name] = False

        summary = self.runner(on_profile).run()

        self.assertEqual([], summary["failures"])
        self.assertEqual(4, summary["collected"])
        self.assertEqual({(9401, "A0"), (9401, "A1"), (9402, "B0"), (9403, "C0")}, set(seen))
        self.assertLess(seen.index((9401, "A0")), seen.index((9401, "A1")), "the configured order inside an account")
        self.assertEqual([1, 1, 1], [w.browser_starts for w in self.ctx.windows.values()])
        self.assertEqual(0, self.ctx.browser_starts)

    def test_one_at_a_time_when_set_to_one(self):
        self.config.set("execution", "parallel_accounts", 1)
        seen = []
        self.runner(lambda context, profile: seen.append(profile.name)).run()
        self.assertEqual(["A0", "A1", "B0", "C0"], seen)

    def test_never_more_accounts_than_the_setting(self):
        self.config.set("execution", "parallel_accounts", 2)
        lock = threading.Lock()
        now, most = [0], [0]

        def on_profile(_context, _profile):
            with lock:
                now[0] += 1
                most[0] = max(most[0], now[0])
            threading.Event().wait(0.05)
            with lock:
                now[0] -= 1

        self.runner(on_profile).run()
        self.assertEqual(2, most[0])

    def test_an_account_that_needs_a_person_stops_alone_without_a_restart(self):
        seen = []
        runner = self.runner(lambda context, profile: seen.append(profile.name))
        window = self.ctx.for_account(self.config.accounts[1])
        window.login.enabled = True
        window.login.enter_account = lambda who: (_ for _ in ()).throw(NeedsPerson("log in by hand"))
        summary = runner.run()

        self.assertEqual({"A0", "A1", "C0"}, set(seen))
        [failure] = summary["failures"]
        self.assertEqual(("B", "log in by hand"), (failure["account"], failure["reason"]))
        self.assertEqual(0, window.browser.restarts)

    def test_a_window_that_is_not_ours_stops_its_account_without_a_restart(self):
        seen = []
        runner = self.runner(lambda context, profile: seen.append(profile.name))
        window = self.ctx.for_account(self.config.accounts[0])
        window.start_browser = lambda: (_ for _ in ()).throw(WrongWindow("port taken"))
        summary = runner.run()

        self.assertEqual({"B0", "C0"}, set(seen))
        self.assertEqual(["port taken"], [f["reason"] for f in summary["failures"]])
        self.assertEqual(0, window.browser.restarts)

    def test_a_cancellation_ends_the_run_after_every_account_has_stopped(self):
        def on_profile(_context, _profile):
            cancellation.cancel("test")
            cancellation.check()

        with self.assertRaises(Cancelled):
            self.runner(on_profile).run()
        self.assertTrue(self.module.ctx is self.ctx)

    def test_a_shared_database_keeps_the_whole_run_from_starting(self):
        self.config.accounts = [account("A", "same", port=9401, folder="A"), account("B", "same", port=9402, folder="B")]
        seen = []
        with self.assertRaises(RunnerError) as refused:
            self.runner(lambda context, profile: seen.append(profile.name)).run(only_account="A")
        self.assertIn("same database", str(refused.exception))
        self.assertEqual([], seen)


if __name__ == "__main__":
    unittest.main()
