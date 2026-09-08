"""
Declarative description of every application parameter.

The GUI, the default configuration file and the validation of config.json are
all generated from this single list, so a new parameter only has to be declared
here once to become visible, editable and persisted.

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
        "How the collector behaves when Windows Task Scheduler triggers run.bat.",
        [
            Field("module", "choice", "Execution module", "chests",
                  "Routine executed by run.bat. 'chests' collects clan chests.",
                  choices=("chests", "journal", "chat", "all")),
            Field("close_browser_on_finish", "bool", "Close browser on finish", True,
                  "Disable to inspect the game screen after collection finishes."),
            Field("stop_on_error", "bool", "Stop on first error", False,
                  "When enabled, a failing profile stops the entire run. When disabled, the collector "
                  "logs the error and continues to the next profile."),
            Field("retries_per_profile", "int", "Retries per profile", 2,
                  "How many times a profile is retried before being considered failed.",
                  minimum=1, maximum=5),
            Field("log_level", "choice", "Log level", "INFO",
                  "DEBUG logs every OCR reading and every click.",
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
            Field("cdp_port", "int", "CDP Port", 9222,
                  "Remote debugging port. Change if another Chrome instance is using 9222.",
                  minimum=1024, maximum=65535),
            Field("game_url", "str", "Game URL", "https://totalbattle.com/en/",
                  "URL opened at the beginning of each account session."),
            Field("game_url_filter", "str", "Game tab URL filter", "totalbattle.com",
                  "URL substring used to find the game tab among open tabs."),
            Field("executable_path", "path", "Chrome executable path", "",
                  "Empty = search for Chrome, Edge, and Brave in standard Windows paths."),
            Field("user_data_dir", "path", "Browser profile directory", "",
                  "Chrome data folder used by the collector. Empty = "
                  "%USERPROFILE%/.total_battle_chest_profile. A dedicated profile avoids "
                  "interfering with your daily Chrome browser."),
            Field("reuse_existing", "bool", "Reuse open browser", True,
                  "If the CDP port is already responding, connects to it instead of launching a new browser."),
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
            Field("clear_session_between_accounts", "bool", "Clear session between accounts", False,
                  "KEEP DISABLED. Clearing cookies causes the game to treat the browser as a new device "
                  "and email a verification code — which an unattended run cannot answer. "
                  "For multiple accounts, assign each its own 'Browser profile' directory in Accounts and profiles."),
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
            Field("click_delay", "float", "Delay after click (s)", 0.5, "", minimum=0.0, maximum=10.0),
            Field("action_delay", "float", "Delay between actions (s)", 0.35, "", minimum=0.0, maximum=10.0),
            Field("chest_click_delay", "float", "Chest click delay (s)", 0.25,
                  "Interval between opening a chest and reading the next.", minimum=0.0, maximum=5.0),
            Field("profile_switch_wait", "float", "Profile switch wait (s)", 20.0,
                  "Time the game takes to reload after switching cities.",
                  minimum=1.0, maximum=180.0),
            Field("between_profiles_wait", "float", "Delay between profiles (s)", 3.0, "",
                  minimum=0.0, maximum=60.0),
            Field("store_close_wait", "float", "Store close wait (s)", 15.0,
                  "Time window in which the store is searched and closed before starting.",
                  minimum=0.0, maximum=120.0),
        ],
    ),
    Section(
        "ocr",
        "OCR",
        "Reading names on screen. RapidOCR (PaddleOCR/ONNX) reads accents and foreign names "
        "that Tesseract struggles with.",
        [
            Field("engine", "choice", "OCR Engine", "auto",
                  "auto = both engines combined when installed: letters come from RapidOCR "
                  "(substantially better with foreign names) and accents come from Tesseract. "
                  "Use 'rapidocr' or 'tesseract' to force a single engine.",
                  choices=("auto", "hybrid", "rapidocr", "tesseract")),
            Field("threads", "int", "RapidOCR threads", 4,
                  "CPU cores used by the neural engine. Benchmark on this machine: default "
                  "ONNXRuntime took 1646 ms per read, 4 threads took 375 ms, and 8 threads worsened "
                  "to 1097 ms due to thread contention.",
                  minimum=1, maximum=32),
            Field("capture_scale", "float", "Capture upscale factor", 2.0,
                  "The region is rendered directly by Chrome at this scale, without interpolation. "
                  "Benchmarked: 2.0 reads identically to 3.0 and halves capture time; 1.0 makes errors.",
                  minimum=1.0, maximum=6.0),
            Field("min_confidence", "float", "Minimum confidence (%)", 45.0,
                  "Readings below this threshold are discarded.", minimum=0.0, maximum=100.0),
            Field("name_match_threshold", "float", "Name match threshold", 0.75,
                  "How closely a read name must match the configured name to count as the same profile.",
                  minimum=0.4, maximum=1.0),
            Field("use_known_names", "bool", "Match with database names", True,
                  "Compares each read name with known database records. Only corrects formatting differences "
                  "(spacing, punctuation, capitalization) and entries in player_name_mappings — similar names "
                  "are NEVER merged, as an erroneous merge leaves no trace to undo. Also skips the slower "
                  "engine when all three fields are already recognized."),
            Field("confidence_gate", "float", "Tesseract confidence gate", 60.0,
                  "Below this score, readings are cross-checked with RapidOCR. "
                  "Benchmarked: correct readings score 71 to 96; erroneous readings score 16 and 35. "
                  "Increase to cross-check more often (slower, safer).",
                  minimum=0.0, maximum=100.0),
            Field("tesseract_lang", "str", "Tesseract language", "por",
                  "Single language model. Benchmarked: 'por+eng' costs 446 ms and 'por' alone costs 310 ms "
                  "with identical results — Portuguese covers the Latin alphabet."),
            Field("tesseract_path", "path", "tesseract.exe path", "",
                  "Empty = search in PATH and standard Windows paths."),
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


def default_config() -> dict:
    """The full configuration with every parameter at its declared default."""
    data = {s.key: {f.key: f.default for f in s.fields} for s in SECTIONS}
    data["accounts"] = []
    return data


def field_for(section_key: str, field_key: str) -> Optional[Field]:
    section = SECTIONS_BY_KEY.get(section_key)
    if not section:
        return None
    for f in section.fields:
        if f.key == field_key:
            return f
    return None
