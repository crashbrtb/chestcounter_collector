"""The configuration window: built from the module registry, and saving what is typed in it."""
import os
import sys
import tempfile
import tkinter
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import ConfigManager  # noqa: E402
from core.schedule import SCHEDULE_DAILY, SCHEDULE_EVERY_RUN, ModuleSchedule  # noqa: E402
from fakes import make_config  # noqa: E402
from modules.registry import MODULES  # noqa: E402

try:
    import customtkinter as ctk

    from gui import app as gui_app
except Exception as exc:  # pragma: no cover - no display, or customtkinter missing
    ctk = None
    IMPORT_ERROR = str(exc)


@unittest.skipIf(ctk is None, "the interface cannot be imported here")
class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = self.tmp.name
        self.config = make_config(folder)
        patches = [
            mock.patch.object(gui_app, "ModuleSchedule",
                              lambda: ModuleSchedule(os.path.join(folder, "module_state.json"))),
            mock.patch.object(gui_app.messagebox, "showinfo", lambda *a, **k: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        try:
            self.app = gui_app.App(self.config)
        except tkinter.TclError as exc:  # pragma: no cover
            self.skipTest(f"no display: {exc}")
        self.app.update()

    def tearDown(self):
        self.app.destroy()
        self.tmp.cleanup()

    def test_every_module_has_a_card_with_its_switch_schedule_and_parameters(self):
        for module in MODULES:
            widgets = self.app.widgets[f"modules.{module.key}"]
            self.assertEqual([f.key for f in module.section().fields], list(widgets))
            self.assertIn(module.key, self.app.module_status)
        self.assertEqual(SCHEDULE_DAILY, self.app.widgets["modules.events"]["schedule"].get())
        self.assertEqual(SCHEDULE_EVERY_RUN, self.app.widgets["modules.chests"]["schedule"].get())

        # Alpha has a clan site, Beta only a database.
        self.assertIn("1 profile(s) set up", self.app.module_status["events"].cget("text"))
        self.assertIn("2 profile(s) set up", self.app.module_status["chests"].cget("text"))

    def test_turning_a_module_off_is_saved_and_shown(self):
        self.app.widgets["modules.events"]["enabled"].deselect()
        self.app.widgets["modules.chests"]["schedule"].set(SCHEDULE_DAILY)
        self.app.widgets["modules.events"]["days_back"].delete(0, "end")
        self.app.widgets["modules.events"]["days_back"].insert(0, "45")
        self.app.save_parameters()

        saved = ConfigManager(self.config.path)
        self.assertFalse(saved.module("events")["enabled"])
        self.assertEqual(45, saved.module("events")["days_back"])
        self.assertEqual(SCHEDULE_DAILY, saved.module("chests")["schedule"])
        self.assertIn("Disabled", self.app.module_status["events"].cget("text"))
        self.assertNotIn("Tournament rankings", self.app.lbl_info.cget("text"))

    def test_each_card_runs_its_module_alone_ignoring_the_schedule(self):
        from fakes import FakeBrowser, FakeContext

        self.assertEqual([m.key for m in MODULES], list(self.app.module_run_buttons))
        runs = []

        class Runner:
            def __init__(self, context, modules=None):
                self.modules = modules

            def run(self, force=False):
                runs.append(([m.key for m in self.modules] if self.modules else None, force))
                return {"collected": 0, "failures": [], "duration": "0"}

        self.app._run_in_background = lambda target, **options: target()
        for answer, expected in ((False, []), (True, [(["members"], True)])):
            self.app.context = FakeContext(self.config, browser=FakeBrowser())
            with mock.patch.object(gui_app.messagebox, "askyesno", lambda *a, **k: answer), \
                    mock.patch("core.runner.ModuleRunner", Runner):
                self.app.module_run_buttons["members"].invoke()
            self.assertEqual(expected, runs)

        # Run now is still every enabled module, on its schedule.
        self.app.context = FakeContext(self.config, browser=FakeBrowser())
        with mock.patch.object(gui_app.messagebox, "askyesno", lambda *a, **k: True), \
                mock.patch("core.runner.ModuleRunner", Runner):
            self.app.run_collection()
        self.assertEqual((None, False), runs[-1])

    def test_each_profile_is_asked_for_its_own_site_and_token(self):
        card = self.app.cards[0]
        alpha, beta = card.rows
        self.assertIn("clan-a.example.com", alpha.lbl_database.cget("text"))
        self.assertNotIn("events", beta.lbl_database.cget("text"))

        saved = {}
        dialog = gui_app.ProfileModulesDialog(self.app, beta.profile, saved.update)
        dialog.update()
        dialog.entries["events"]["site_url"].insert(0, "clan-b.example.com")
        dialog.entries["events"]["api_token"].insert(0, " cct_b ")
        self.assertEqual("*", dialog.entries["events"]["api_token"].cget("show"))
        dialog._save()
        self.assertEqual({"events": {"site_url": "clan-b.example.com", "api_token": "cct_b"}}, saved)

        # Through the row, into the file, and the other profile is untouched.
        beta.profile.modules = saved
        self.app.save_accounts()
        alpha_saved, beta_saved = ConfigManager(self.config.path).accounts[0].profiles
        self.assertEqual("cct_a", alpha_saved.module_settings("events")["api_token"])
        self.assertEqual("cct_b", beta_saved.module_settings("events")["api_token"])
        self.assertEqual("clan_b", beta_saved.database.database)

    def test_each_account_shows_its_port_and_folder_and_they_can_be_changed(self):
        card = self.app.cards[0]
        self.assertEqual("9322", card.cdp_port.get())
        self.assertTrue(card.browser_profile.get().endswith("Main"))

        card.show_window(type(card.account)(browser_profile=r"C:\elsewhere\Main", cdp_port=9500))
        self.app.save_accounts()
        [saved] = ConfigManager(self.config.path).accounts
        self.assertEqual((9500, r"C:\elsewhere\Main"), (saved.cdp_port, saved.browser_profile))

    def test_a_port_that_cannot_be_one_is_not_saved(self):
        before = ConfigManager(self.config.path).accounts[0].cdp_port
        card = self.app.cards[0]
        for typed in ("abc", "80"):
            card.cdp_port.delete(0, "end")
            card.cdp_port.insert(0, typed)
            errors = []
            with mock.patch.object(gui_app.messagebox, "showerror", lambda *a, **k: errors.append(a)):
                self.app.save_accounts()
            self.assertEqual(1, len(errors), typed)
        self.assertEqual(before, ConfigManager(self.config.path).accounts[0].cdp_port)

    def test_a_port_left_empty_gets_a_free_one_when_saved(self):
        card = self.app.cards[0]
        card.cdp_port.delete(0, "end")
        self.app.save_accounts()
        self.assertEqual("9322", card.cdp_port.get())

    def test_two_profiles_on_one_database_are_not_saved(self):
        alpha, beta = self.app.cards[0].rows
        beta.profile.database = alpha.profile.database
        errors = []
        with mock.patch.object(gui_app.messagebox, "showerror", lambda *a, **k: errors.append(a[1])):
            self.app.save_accounts()
        self.assertIn("same database", errors[0])
        self.assertEqual("clan_b", ConfigManager(self.config.path).accounts[0].profiles[1].database.database)

    def test_open_window_opens_the_saved_account_for_the_person(self):
        opened = []

        class Window:
            def open_for_person(self):
                opened.append(True)

        self.app._run_in_background = lambda target, **options: target()
        with mock.patch("core.context.browser_for", lambda config, account: Window()):
            self.app.open_account_window(self.app.cards[0])
        self.assertEqual([True], opened)


if __name__ == "__main__":
    unittest.main()
