"""
Declarative description of every application parameter.

The GUI, the default configuration file and the validation of config.json are
all generated from this single list, so a new parameter only has to be declared
here once to become visible, editable and persisted.

The sections below are the ones every module shares (browser, login...).
Each feature module declares its own parameters next to its code, and they are
stored under "modules" in config.json: see `module_sections()`.

Field types
    str / password / path : free text (password is masked in the GUI)
    int / float           : numeric, optionally bounded by minimum/maximum
    bool                  : checkbox
    choice                : dropdown restricted to `choices`
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


@dataclass(frozen=True)
class Field:
    key: str
    type: str
    label: str
    default: Any
    help: str = ""
    choices: Tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    # False keeps the parameter in config.json but out of the Parameters tab,
    # for one that is really edited somewhere else (each account's own card).
    in_parameters_tab: bool = True

    @property
    def is_secret(self) -> bool:
        return self.type == "password"


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    description: str
    fields: List[Field] = field(default_factory=list)


SECTIONS: List[Section] = [
    Section(
        "execution",
        "Execution",
        "How a run behaves when Windows Task Scheduler triggers run.bat. What a run does is "
        "decided in the Modules tab.",
        [
            Field("close_browser_on_finish", "bool", "Close browser on finish", True,
                  "Disable to inspect the game screen after collection finishes."),
            Field("stop_on_error", "bool", "Stop on first error", False,
                  "When enabled, a failing profile stops the entire run. When disabled, the collector "
                  "logs the error and continues to the next profile."),
            Field("parallel_accounts", "int", "Accounts at the same time", 4,
                  "How many accounts work at once, each in its own Chrome window. The profiles of an "
                  "account are always one after the other. 1 = one account at a time.",
                  minimum=1, maximum=4),
            Field("retries_per_profile", "int", "Retries per profile", 2,
                  "How many times a profile is retried before being considered failed.",
                  minimum=1, maximum=5),
            Field("restart_browser_on_failure", "bool", "Restart browser when an account fails", True,
                  "When no profile of an account can be collected (usually the game froze in Chrome "
                  "and needs F5), closes the browser, opens a fresh one and tries the account once more."),
            Field("log_level", "choice", "Log level", "INFO",
                  "DEBUG logs every click.",
                  choices=("DEBUG", "INFO", "WARNING", "ERROR")),
            Field("log_dir", "path", "Log folder", "execution_logs",
                  "Relative to the project folder, or an absolute path."),
            Field("log_retention_days", "int", "Log retention (days)", 7,
                  "Logs older than this are deleted at the start of each run. 0 = never delete.",
                  minimum=0, maximum=3650),
            Field("screenshot_on_error", "bool", "Save screenshot on error", True,
                  "Saves a screenshot of the game to execution_logs/screenshots for diagnostics."),
        ],
    ),
    Section(
        "browser",
        "Browser",
        "Chrome controlled via CDP (Chrome DevTools Protocol). Replaces the desktop application.",
        [
            Field("cdp_host", "str", "CDP Host", "127.0.0.1",
                  "Almost always 127.0.0.1."),
            Field("first_cdp_port", "int", "First debugging port", 9322,
                  "Accounts without a port get the next free one from here. Each account's port is "
                  "shown, and can be changed, in Accounts and profiles.",
                  minimum=1024, maximum=65535, in_parameters_tab=False),
            Field("game_url", "str", "Game URL", "https://totalbattle.com/en/",
                  "URL opened at the beginning of each account session."),
            Field("game_url_filter", "str", "Game tab URL filter", "totalbattle.com",
                  "URL substring used to find the game tab among open tabs."),
            Field("executable_path", "path", "Chrome executable path", "",
                  "Empty = search for Chrome, Edge, and Brave in standard Windows paths."),
            Field("profiles_dir", "path", "Folder of Chrome profiles", "",
                  "Where new accounts get their Chrome folder. Empty = "
                  "%USERPROFILE%/.total_battle_counter_profiles. An account that already has a folder keeps it.",
                  in_parameters_tab=False),
            Field("reuse_existing", "bool", "Reuse open browser", True,
                  "If the account's window is already open, connects to it instead of restarting it. "
                  "A window of another folder on that port is never used."),
            Field("start_maximized", "bool", "Start maximized", True, ""),
            Field("window_width", "int", "Window width", 1920,
                  "Used only when 'Start maximized' is disabled.", minimum=800, maximum=7680),
            Field("window_height", "int", "Window height", 1080,
                  "Used only when 'Start maximized' is disabled.", minimum=600, maximum=4320),
            Field("page_load_timeout", "float", "Page load timeout (s)", 90.0,
                  "Maximum wait time for the game to load after opening the URL.",
                  minimum=10.0, maximum=600.0),
        ],
    ),
    Section(
        "login",
        "Login",
        "Authentication on the website form. Empty selectors are detected automatically.",
        [
            Field("enabled", "bool", "Automatic login", True,
                  "Disable if you prefer leaving the session already logged into the browser profile."),
            Field("open_login_selector", "str", "CSS selector for login opener button", "",
                  "The totalbattle.com login form is in the HTML on page load but hidden: "
                  "you must click 'Log in' before typing. Empty = automatically find a visible "
                  "button with login text (social login buttons like Google/Facebook are discarded)."),
            Field("email_selector", "str", "CSS selector for email field", "",
                  "Empty = detect automatically (input[type=email], name/id containing email or login)."),
            Field("password_selector", "str", "CSS selector for password field", "",
                  "Empty = detect automatically (input[type=password])."),
            Field("submit_selector", "str", "CSS selector for submit button", "",
                  "Empty = detect automatically (button[type=submit] or button with login text)."),
            Field("logged_in_selector", "str", "Selector confirming logged-in state", "",
                  "Element that only exists after login, e.g. 'canvas'. "
                  "Empty = consider logged in when the password field disappears."),
            Field("wait_after_submit", "float", "Wait after submit (s)", 30.0,
                  "Maximum wait time for the game to load after submitting login.",
                  minimum=5.0, maximum=300.0),
            Field("max_attempts", "int", "Login retry attempts", 2,
                  "Attempts before giving up on the account.", minimum=1, maximum=5),
        ],
    ),
    Section(
        "timing",
        "Timing",
        "Delays between actions. Increase if the game is sluggish; decrease to collect faster.",
        [
            Field("action_delay", "float", "Delay between actions (s)", 0.35, "", minimum=0.0, maximum=10.0),
            Field("between_profiles_wait", "float", "Delay between profiles (s)", 3.0, "",
                  minimum=0.0, maximum=60.0),
            Field("store_close_wait", "float", "Store close wait (s)", 15.0,
                  "Time window in which the store is searched and closed before starting.",
                  minimum=0.0, maximum=120.0),
        ],
    ),
    Section(
        "vision",
        "Image Recognition",
        "Button search using reference images saved during calibration.",
        [
            Field("match_threshold", "float", "Similarity threshold", 0.80,
                  "Minimum correlation score to consider the image found.",
                  minimum=0.3, maximum=1.0),
            Field("scaled_threshold_relief", "float", "Scaled threshold relief", 0.06,
                  "Threshold discount applied when the image is resized, since "
                  "rescaling always slightly lowers correlation.",
                  minimum=0.0, maximum=0.3),
            Field("max_attempts", "int", "Search attempts", 3, "", minimum=1, maximum=20),
            Field("retry_interval", "float", "Retry interval (s)", 0.4, "",
                  minimum=0.0, maximum=10.0),
        ],
    ),
]

SECTIONS_BY_KEY = {s.key: s for s in SECTIONS}

# Where the modules' own sections live in config.json: {"modules": {"chests": {...}}}.
# A section key with a dot is a path into the file, so "modules.chests" is read
# and written with the same get/set as "browser".
MODULES_PREFIX = "modules"


def module_section_key(module_key: str) -> str:
    return f"{MODULES_PREFIX}.{module_key}"


def module_sections() -> List[Section]:
    """One section per registered module: its switch, its schedule, its parameters."""
    # Imported here because the modules themselves import this file for `Field`.
    from modules.registry import MODULES

    return [module.section() for module in MODULES]


def all_sections() -> List[Section]:
    return SECTIONS + module_sections()


def bucket(data: dict, section_key: str, create: bool = False) -> Optional[dict]:
    """The dictionary a section is stored in, following the dots of its key."""
    node = data
    for part in section_key.split("."):
        child = node.get(part)
        if not isinstance(child, dict):
            if not create:
                return None
            child = node[part] = {}
        node = child
    return node


def default_config() -> dict:
    """The full configuration with every parameter at its declared default."""
    data: dict = {}
    for section in all_sections():
        bucket(data, section.key, create=True).update({f.key: f.default for f in section.fields})
    data["accounts"] = []
    return data


def field_for(section_key: str, field_key: str) -> Optional[Field]:
    section = SECTIONS_BY_KEY.get(section_key)
    if section is None:
        section = next((s for s in module_sections() if s.key == section_key), None)
    if not section:
        return None
    for f in section.fields:
        if f.key == field_key:
            return f
    return None
