"""The configuration window: built from the module registry, and saving what is typed in it."""
import os
import sys
import tempfile
import tkinter
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import ConfigManager  # noqa: E402
from core.calibration import Calibration  # noqa: E402
from core.schedule import SCHEDULE_DAILY, SCHEDULE_EVERY_RUN, ModuleSchedule  # noqa: E402
from fakes import make_config  # noqa: E402
from modules.registry import MODULES  # noqa: E402

try:
    import customtkinter as ctk

    from gui import app as gui_app
    from gui.calibration_wizard import CalibrationWizard
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
            # Nothing of this machine's real calibration or run history is touched.
            mock.patch.object(gui_app, "Calibration", lambda groups: Calibration(
                groups, os.path.join(folder, "calibration.json"), os.path.join(folder, "refs"))),
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

        # Alpha has a clan site, nothing is calibrated yet: the card says so.
        self.assertIn("not calibrated", self.app.module_status["events"].cget("text"))
        self.assertIn("1 profile(s) set up", self.app.module_status["events"].cget("text"))
        self.assertIn("2 profile(s) set up", self.app.module_status["chests"].cget("text"))

    def test_turning_a_module_off_is_saved_and_shown(self):
        self.app.widgets["modules.events"]["enabled"].deselect()
        self.app.widgets["modules.chests"]["schedule"].set(SCHEDULE_DAILY)
        self.app.widgets["modules.events"]["reload_wait"].delete(0, "end")
        self.app.widgets["modules.events"]["reload_wait"].insert(0, "45")
        self.app.save_parameters()

        saved = ConfigManager(self.config.path)
        self.assertFalse(saved.module("events")["enabled"])
        self.assertEqual(45.0, saved.module("events")["reload_wait"])
        self.assertEqual(SCHEDULE_DAILY, saved.module("chests")["schedule"])
        self.assertIn("Disabled", self.app.module_status["events"].cget("text"))
        self.assertNotIn("Tournament rankings", self.app.lbl_info.cget("text"))

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

    def test_the_wizard_lists_one_calibration_grouped_by_module(self):
        from fakes import FakeBrowser, FakeContext

        context = FakeContext(self.config, self.app.calibration, browser=FakeBrowser())
        context.browser.capture = lambda *a, **k: None
        wizard = CalibrationWizard(self.app, context)
        wizard.update()
        self.assertEqual(len(self.app.calibration.steps), len(wizard.rows))
        self.assertEqual(["shared", "chests", "members", "events"], [g.key for g in wizard.calibration.groups])
        wizard.go_to(len(wizard.steps) - 1)
        self.assertEqual("Journal: close the card", wizard.lbl_title.cget("text"))
        wizard.destroy()


if __name__ == "__main__":
    unittest.main()
