"""Stand-ins for the game and the services around it, shared by the tests.

Nothing here opens a browser, a database or the network: a run is exercised
against these, with a configuration and a calibration written to a temporary
folder.
"""
import os

from config.settings import AccountConfig, ConfigManager, DatabaseConfig, ProfileConfig
from core.calibration import Calibration
from core.schedule import ModuleSchedule
from modules.registry import calibration_scope, step_groups

SITE_A = {"site_url": "https://clan-a.example.com", "api_token": "cct_a"}
SITE_B = {"site_url": "https://clan-b.example.com", "api_token": "cct_b"}


def database(name="clan_a"):
    return DatabaseConfig(host="127.0.0.1", user="collector", password="x", database=name)


def make_config(folder, accounts=None):
    """A configuration on disk with one account: Alpha (database and site) and Beta (database only)."""
    config = ConfigManager(os.path.join(folder, "config.json"))
    if accounts is None:
        accounts = [AccountConfig(name="Main", login="me@example.com", password="pw", profiles=[
            ProfileConfig(name="Alpha", database=database("clan_a"), account_name="Main",
                          modules={"events": dict(SITE_A)}),
            ProfileConfig(name="Beta", database=database("clan_b"), account_name="Main"),
        ])]
    config.accounts = accounts
    config.set("timing", "between_profiles_wait", 0)
    config.save()
    return config


def make_calibration(folder, config, complete=True):
    """The shared steps calibrated; every module's too, unless `complete` is off."""
    calibration = Calibration(step_groups(), os.path.join(folder, "calibration.json"),
                              os.path.join(folder, "refs"))
    calibration.set_viewport(1000, 800)
    for step in calibration.steps:
        if step.optional or (not complete and step.name.startswith("journal_")):
            continue
        if step.type == "click":
            calibration.set_point(step.name, 100, 100)
        else:
            # An 'area' is enough for the tests: no reference picture is searched for.
            calibration.regions[step.name] = {"left": 10, "top": 10, "width": 50, "height": 20}
    calibration.use_viewport(1000, 800)
    calibration.required = calibration_scope(config)
    return calibration


class FakeBrowser:
    host, port, tab_id, url_filter = "127.0.0.1", 9222, "TAB", "totalbattle.com"

    def __init__(self):
        self.ws = None
        self.started = 0
        self.restarts = 0
        self.reloads = 0

    def switch_profile(self, _folder):
        return True

    def navigate(self, *_args, **_kwargs):
        pass

    def wait_for_load(self, *_args, **_kwargs):
        return True

    def reload(self, wait=True):
        self.reloads += 1

    def refresh_dpr(self):
        return 1.0

    def restart(self):
        self.restarts += 1

    def save_screenshot(self, _path):
        return False

    def viewport(self):
        return 1000, 800


class FakeLogin:
    enabled = False
    clear_between_accounts = False

    def end_session(self):
        pass


class FakeProfiles:
    """The profile switcher: remembers where it was sent, and can be told to fail."""

    def __init__(self):
        self.switched = []
        self.reachable = True
        self.active = None

    def switch_to(self, profile, _siblings=()):
        self.switched.append(profile.name)
        if self.reachable:
            self.active = profile.name
        return self.reachable

    def is_active(self, name, _siblings=()):
        return self.active == name

    def active_profile_name(self):
        return self.active or ""


class FakeGameState:
    def __init__(self):
        self.prepared = 0
        self.closed = 0

    def prepare_board(self):
        self.prepared += 1

    def close_modals(self, presses=2):
        self.closed += 1


class FakeOcrEngine:
    def status(self):
        return True, "fake engine"


class FakeContext:
    """What core.context.RunContext gives a run, without a browser behind it."""

    def __init__(self, config, calibration, browser=None, ocr=None):
        self.config = config
        self.calibration = calibration
        self.browser = browser or FakeBrowser()
        self.ocr = ocr or FakeOcrEngine()
        self.vision = None
        self.game_state = FakeGameState()
        self.login = FakeLogin()
        self.profiles = FakeProfiles()
        self.browser_starts = 0

    def start_browser(self):
        self.browser_starts += 1
        return self.browser

    def sync_viewport(self):
        pass

    def close(self):
        pass


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def make_schedule(folder, clock):
    return ModuleSchedule(os.path.join(folder, "module_state.json"), clock)
