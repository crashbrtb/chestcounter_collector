"""
Paths, configuration loading and the account/profile model.

Everything the application needs to know is in config/config.json, written and
read here. There is no screen coordinate anywhere: every module talks to the
game over the network.

Account model
-------------
An account is a login (e-mail + password) on totalbattle.com; a profile is one
of the cities that account owns. Collection follows exactly this order - log in
to the account, then walk its profiles - which is why the profiles are nested
inside the account instead of being a flat list.

Each account lives in a Chrome window of its own, with its own debugging port and
its own folder. The folder holds the session the game verified by e-mail, so an
account has to open on the same one every time; both are written down the first
time they are worked out (`ConfigManager.pin_windows`), and renaming an account
does not move it.

Each profile has a database of its own: two profiles writing to one would mix
two clans' chests, so a run refuses that (`ConfigManager.database_problems`).

A profile without database credentials is skipped by the chest module: there
would be nowhere to record what was collected, and pretending otherwise would
silently throw the chests away.

What each module needs to know about a profile (the events module's site
address and token, for one) is kept with the profile too, under `modules`: each
profile belongs to one clan, so each one carries its own.
"""

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .schema import all_sections, bucket, default_config, field_for, module_section_key

# Project layout
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
CONFIG_FILE_PATH = os.path.join(CONFIG_DIR, "config.json")
# When each module last ran for each profile (see core/schedule.py).
MODULE_STATE_PATH = os.path.join(CONFIG_DIR, "module_state.json")
# What a module keeps between runs, one folder per module.
MODULE_DATA_DIR = os.path.join(CONFIG_DIR, "module_data")
# Held by the run in progress (utils/run_lock.py).
RUN_LOCK_PATH = os.path.join(CONFIG_DIR, "run.lock")
LEGACY_CONFIG_PATH = os.path.join(BASE_DIR, "position.cfg")

DEFAULT_PROFILES_DIR = os.path.join(os.path.expanduser("~"), ".total_battle_counter_profiles")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def same_folder(first: str, second: str) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def _folder_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text.strip()) or "account"


@dataclass
class DatabaseConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = ""
    password: str = ""
    database: str = ""

    @property
    def is_usable(self) -> bool:
        return bool(self.host and self.user and self.database)

    @property
    def identity(self) -> tuple:
        """What says two configurations are the same database, however they were typed."""
        host = self.host.strip().lower()
        return ("localhost" if host in LOCAL_HOSTS else host, self.port, self.database.strip().lower())

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["DatabaseConfig"]:
        if not data:
            return None
        try:
            port = int(data.get("port", 3306) or 3306)
        except (TypeError, ValueError):
            port = 3306
        return cls(
            host=str(data.get("host", "") or ""),
            port=port,
            user=str(data.get("user", "") or ""),
            password=str(data.get("password", "") or ""),
            database=str(data.get("database", "") or ""),
        )


@dataclass
class ProfileConfig:
    """One city inside an account."""

    name: str = ""
    enabled: bool = True
    database: Optional[DatabaseConfig] = None
    account_name: str = ""
    # What each module was told about this profile: {"events": {"site_url": ...}}.
    modules: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def has_database(self) -> bool:
        return self.database is not None and self.database.is_usable

    def module_settings(self, module_key: str) -> Dict[str, Any]:
        return dict(self.modules.get(module_key) or {})

    @property
    def label(self) -> str:
        return f"{self.account_name} / {self.name}" if self.account_name else self.name

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "database": self.database.to_dict() if self.database else None,
            "modules": {key: dict(values) for key, values in self.modules.items() if values},
        }

    @classmethod
    def from_dict(cls, data: dict, account_name: str = "") -> "ProfileConfig":
        modules = data.get("modules")
        return cls(
            name=str(data.get("name", "") or ""),
            enabled=bool(data.get("enabled", True)),
            database=DatabaseConfig.from_dict(data.get("database")),
            account_name=account_name,
            modules={str(key): dict(values) for key, values in modules.items() if isinstance(values, dict)}
            if isinstance(modules, dict) else {},
        )


@dataclass
class AccountConfig:
    """A totalbattle.com login and the profiles reachable from it."""

    name: str = ""
    login: str = ""
    password: str = ""
    enabled: bool = True
    # Its Chrome window: the folder that keeps its verified session (a path, or a
    # name inside the folder of profiles) and the debugging port to reach it on.
    # Empty and 0 are filled in by ConfigManager.pin_windows.
    browser_profile: str = ""
    cdp_port: int = 0
    profiles: List[ProfileConfig] = field(default_factory=list)

    @property
    def active_profiles(self) -> List[ProfileConfig]:
        return [p for p in self.profiles if p.enabled and p.name]

    @property
    def collectable_profiles(self) -> List[ProfileConfig]:
        return [p for p in self.active_profiles if p.has_database]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "login": self.login,
            "password": self.password,
            "enabled": self.enabled,
            "browser_profile": self.browser_profile,
            "cdp_port": self.cdp_port,
            "profiles": [p.to_dict() for p in self.profiles],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AccountConfig":
        name = str(data.get("name", "") or "")
        try:
            port = int(data.get("cdp_port", 0) or 0)
        except (TypeError, ValueError):
            port = 0
        return cls(
            name=name,
            login=str(data.get("login", "") or ""),
            password=str(data.get("password", "") or ""),
            enabled=bool(data.get("enabled", True)),
            browser_profile=str(data.get("browser_profile", "") or ""),
            cdp_port=port,
            profiles=[ProfileConfig.from_dict(p, name) for p in data.get("profiles", []) or []],
        )


def coerce(value: Any, spec) -> Any:
    """Brings a value read from disk (or typed in the GUI) to its declared type."""
    if spec is None:
        return value
    try:
        if spec.type == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "sim")
            return bool(value)
        if spec.type == "int":
            value = int(float(value))
        elif spec.type == "float":
            value = float(value)
        elif spec.type == "choice":
            value = str(value)
            if spec.choices and value not in spec.choices:
                return spec.default
        else:
            value = "" if value is None else str(value)
    except (TypeError, ValueError):
        return spec.default

    if spec.type in ("int", "float"):
        if spec.minimum is not None:
            value = max(spec.minimum, value)
        if spec.maximum is not None:
            value = min(spec.maximum, value)
        value = int(value) if spec.type == "int" else float(value)
    return value


class ConfigManager:
    """Reads, validates and writes config/config.json."""

    def __init__(self, path: str = CONFIG_FILE_PATH):
        self.path = path
        self.data: Dict[str, Any] = default_config()
        self.load()

    # -- persistence
    def load(self) -> Dict[str, Any]:
        raw: Dict[str, Any] = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh) or {}
            except (OSError, ValueError) as exc:
                raise ValueError(f"Invalid config.json ({exc}). Fix the file or delete it to start over.")

        merged = default_config()
        for section in all_sections():
            stored = bucket(raw, section.key) or {}
            values = bucket(merged, section.key)
            for spec in section.fields:
                if spec.key in stored:
                    values[spec.key] = coerce(stored[spec.key], spec)
        merged["accounts"] = raw.get("accounts") or []
        self.data = merged
        return self.data

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)

    @property
    def exists(self) -> bool:
        return os.path.exists(self.path)

    # -- values
    def get(self, section: str, key: str, default: Any = None) -> Any:
        return (bucket(self.data, section) or {}).get(key, default)

    def set(self, section: str, key: str, value: Any):
        bucket(self.data, section, create=True)[key] = coerce(value, field_for(section, key))

    def section(self, key: str) -> Dict[str, Any]:
        return copy.deepcopy(bucket(self.data, key) or {})

    def module(self, module_key: str) -> Dict[str, Any]:
        """A module's own parameters, with its switch and its schedule."""
        return self.section(module_section_key(module_key))

    # -- accounts
    @property
    def accounts(self) -> List[AccountConfig]:
        return [AccountConfig.from_dict(a) for a in self.data.get("accounts", [])]

    @accounts.setter
    def accounts(self, accounts: List[AccountConfig]):
        self.data["accounts"] = [a.to_dict() for a in accounts]

    def enabled_accounts(self) -> List[AccountConfig]:
        return [a for a in self.accounts if a.enabled and a.active_profiles]

    def profile_names(self, account_name: str) -> List[str]:
        """Every profile name of an account, to tell near-twin names apart on screen."""
        for account in self.accounts:
            if account.name == account_name:
                return [p.name for p in account.profiles if p.name]
        return []

    def resolved_log_dir(self) -> str:
        raw = self.get("execution", "log_dir") or "execution_logs"
        return raw if os.path.isabs(raw) else os.path.join(BASE_DIR, raw)

    # -- windows
    def profiles_dir(self) -> str:
        raw = self.get("browser", "profiles_dir") or ""
        return os.path.expandvars(os.path.expanduser(raw)) if raw else DEFAULT_PROFILES_DIR

    def chrome_folder_for(self, account: AccountConfig) -> str:
        """The Chrome folder of an account: its own path, or a name inside the folder of profiles."""
        custom = os.path.expandvars(os.path.expanduser((account.browser_profile or "").strip()))
        if os.path.isabs(custom):
            return custom
        return os.path.join(self.profiles_dir(), _folder_name(custom or account.name or account.login))

    def pin_windows(self) -> bool:
        """
        Writes down the port and the folder of every account that has none yet.

        Written, not worked out on each run: a folder derived from the name would
        change with the name, and a new folder means another code by e-mail.
        """
        accounts = self.accounts
        ports = {account.cdp_port for account in accounts if account.cdp_port}
        folders = [self.chrome_folder_for(account) for account in accounts if account.browser_profile.strip()]
        port = int(self.get("browser", "first_cdp_port", 9322))
        changed = False
        for account in accounts:
            if not account.cdp_port:
                while port in ports:
                    port += 1
                account.cdp_port = port
                ports.add(port)
                changed = True
            if not account.browser_profile.strip():
                base = self.chrome_folder_for(account)
                folder, turn = base, 2
                while any(same_folder(folder, other) for other in folders):
                    folder, turn = f"{base}_{turn}", turn + 1
                account.browser_profile = folder
                folders.append(folder)
                changed = True
        if changed:
            self.accounts = accounts
            self.save()
        return changed

    def window_problems(self, accounts: Optional[List[AccountConfig]] = None) -> List[str]:
        """Active accounts that would share a Chrome window."""
        active = [a for a in (self.accounts if accounts is None else accounts) if a.enabled]
        problems = []
        for index, account in enumerate(active):
            for other in active[index + 1:]:
                if account.cdp_port and account.cdp_port == other.cdp_port:
                    problems.append(f"Accounts '{account.name}' and '{other.name}' use the same port "
                                    f"({account.cdp_port}). Each window needs its own.")
                if same_folder(self.chrome_folder_for(account), self.chrome_folder_for(other)):
                    problems.append(f"Accounts '{account.name}' and '{other.name}' use the same Chrome folder "
                                    f"({self.chrome_folder_for(account)}). Each keeps its session in its own.")
        return problems

    def database_problems(self, accounts: Optional[List[AccountConfig]] = None) -> List[str]:
        """Active profiles that would write to the same database."""
        users: Dict[tuple, List[str]] = {}
        for account in (self.accounts if accounts is None else accounts):
            if account.enabled:
                for profile in account.active_profiles:
                    if profile.has_database:
                        users.setdefault(profile.database.identity, []).append(f"{account.name} / {profile.name}")
        return [f"Profiles {', '.join(names)} use the same database ('{identity[2]}' on {identity[0]}). "
                f"Each profile needs its own."
                for identity, names in users.items() if len(names) > 1]


def migrate_legacy_config(legacy_path: str = LEGACY_CONFIG_PATH) -> List[AccountConfig]:
    """
    Rebuilds the account list from the old position.cfg.

    Only the database credentials survive the move. The old [COORDINATES] were
    screen positions of the desktop client; nothing uses screen positions any
    more, so they are dropped.

    Each old [account_*] section was really a profile of a single logged-in
    account, which is exactly how they are imported: one account with an empty
    login, holding every profile found.
    """
    import configparser

    if not os.path.exists(legacy_path):
        return []

    parser = configparser.ConfigParser()
    parser.read(legacy_path, encoding="utf-8")

    profiles: List[ProfileConfig] = []
    for section in parser.sections():
        if not section.lower().startswith("account"):
            continue
        name = parser.get(section, "account", fallback="").strip()
        if not name:
            continue
        database = DatabaseConfig(
            host=parser.get(section, "host", fallback="127.0.0.1"),
            port=parser.getint(section, "port", fallback=3306),
            user=parser.get(section, "user", fallback=""),
            password=parser.get(section, "password", fallback=""),
            database=parser.get(section, "database", fallback=""),
        )
        profiles.append(ProfileConfig(name=name, enabled=True, database=database))

    if not profiles:
        return []

    account = AccountConfig(name="Conta importada", login="", password="", enabled=True, profiles=profiles)
    for profile in account.profiles:
        profile.account_name = account.name
    return [account]
