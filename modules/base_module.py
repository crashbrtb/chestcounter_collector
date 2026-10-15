"""
What a feature module is.

A module is one job done on a profile of the game: collecting the clan chests,
sending the tournament rankings to the clan site, and whatever comes next. The
rest of the application - the configuration file, the settings window, the
calibration wizard, the run - is built from what each module declares here, so
adding one means writing its folder under modules/ and listing it in
modules/registry.py. Nothing else has to learn its name.

A module declares
-----------------
key, title, description   how it is called in config.json, in the window, in the log
settings                  its own parameters (config.schema.Field), shown in the Modules tab
profile_fields            what it needs to know about each profile (a site token, say),
                          edited per profile in Accounts and profiles
steps                     the calibration steps of its own screens, in the order of a pass
required_steps            the steps a run cannot start without
default_schedule          every run, or once a day

Every module gets two parameters for free: whether it is enabled, and when it
runs. The run itself asks the module one profile at a time, with the game
already logged in and on that profile.

A module receives the whole RunContext rather than a handful of services: what a
module needs tends to grow (the chest collector ended up needing the profile
switcher too), and threading one more argument through every constructor each
time was how the old signatures drifted apart.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from config.schema import Field, Section, module_section_key
from config.settings import ProfileConfig
from core.calibration import Step
from core.schedule import DEFAULT_DAY_START, SCHEDULE_DAILY, SCHEDULE_EVERY_RUN, SCHEDULES


class ModuleAction(NamedTuple):
    """A button on the module's card in the Modules tab."""

    label: str
    method: str          # classmethod(config, calibration) -> message to show
    help: str = ""


class BaseModule(ABC):
    key: str = ""
    title: str = ""
    description: str = ""
    default_enabled: bool = True
    default_schedule: str = SCHEDULE_EVERY_RUN
    default_day_start: str = DEFAULT_DAY_START
    settings: Sequence[Field] = ()
    profile_fields: Sequence[Field] = ()
    steps: Sequence[Step] = ()
    required_steps: Sequence[str] = ()
    actions: Sequence[ModuleAction] = ()

    def __init__(self, context):
        self.ctx = context
        self.options: Dict[str, Any] = context.config.module(self.key)

    # Convenience shortcuts - modules read these constantly.
    @property
    def browser(self):
        return self.ctx.browser

    @property
    def ocr(self):
        return self.ctx.ocr

    @property
    def vision(self):
        return self.ctx.vision

    @property
    def calibration(self):
        return self.ctx.calibration

    @property
    def game_state(self):
        return self.ctx.game_state

    @property
    def config(self):
        return self.ctx.config

    # ---------------------------------------------------------- configuration
    @classmethod
    def section(cls) -> Section:
        """The module's block of config.json: the two common parameters, then its own."""
        common = [
            Field("enabled", "bool", "Enabled", cls.default_enabled,
                  "Turns the whole module on or off. A disabled module is not run, and its "
                  "calibration steps are not required."),
            Field("schedule", "choice", "Runs", cls.default_schedule,
                  f"'{SCHEDULE_EVERY_RUN}': each time the application runs. '{SCHEDULE_DAILY}': on the "
                  f"first run of the day for each profile, and not again until tomorrow.",
                  choices=SCHEDULES),
            Field("daily_attempts", "int", "Attempts per day", 3,
                  f"Only for '{SCHEDULE_DAILY}': a run that fails is tried again on the next runs "
                  f"of the same day, up to this many times.", minimum=1, maximum=24),
            Field("day_start", "str", "Game day starts at (HH:MM)", cls.default_day_start,
                  f"Only for '{SCHEDULE_DAILY}': the time, on this computer's clock, the game's day "
                  f"turns. A run before it still counts as the previous day."),
        ]
        return Section(module_section_key(cls.key), cls.title, cls.description, common + list(cls.settings))

    @classmethod
    def configured_for(cls, profile: ProfileConfig) -> bool:
        """Whether the profile has what this module needs to run on it."""
        return True

    @classmethod
    def profile_summary(cls, profile: ProfileConfig) -> str:
        """One short line about the profile, for its row in the accounts tab. '' says nothing."""
        return ""

    @classmethod
    def test_profile(cls, values: Dict[str, Any]) -> Optional[Tuple[bool, str]]:
        """Tries the values typed for a profile. None when there is nothing to try."""
        return None

    # -------------------------------------------------------------------- run
    def preflight(self, profiles: List[ProfileConfig]) -> List[str]:
        """What would make running on these profiles pointless, said before the run starts."""
        return []

    def begin_run(self, profiles: List[ProfileConfig]):
        """Called once, before the browser opens, with the profiles this run will do."""

    def end_run(self, results: List[Dict[str, Any]], duration: str = "",
                error: Optional[str] = None, cancelled: bool = False):
        """Called once when the run ends, however it ends, with each profile's last result."""

    @abstractmethod
    def run_for_profile(self, profile: ProfileConfig) -> Dict[str, Any]:
        """
        Does the module's job on the profile that is active in the game.

        Returns at least {"success": bool}; "reason" says why not. A failure is
        tried again while the profile has attempts left, unless the result says
        {"retry": False} - for the failures that trying again cannot change.
        """
