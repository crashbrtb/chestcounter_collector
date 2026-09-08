"""
Paths, configuration loading and the account/profile model.

Everything the application needs to know is in config/config.json, written and
read here. There is no coordinate in this file and none in the code: screen
positions live in config/calibration.json, produced by the calibration wizard.

Account model
-------------
An account is a login (e-mail + password) on totalbattle.com; a profile is one
of the cities that account owns. Collection follows exactly this order - log in
to the account, then walk its profiles - which is why the profiles are nested
inside the account instead of being a flat list.

A profile without database credentials is skipped: there would be nowhere to
record what was collected, and pretending otherwise would silently throw the
chests away.
"""

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .schema import SECTIONS, default_config, field_for

# Project layout
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
CONFIG_FILE_PATH = os.path.join(CONFIG_DIR, "config.json")
CALIBRATION_FILE_PATH = os.path.join(CONFIG_DIR, "calibration.json")
CALIBRATION_REFS_DIR = os.path.join(CONFIG_DIR, "calib_refs")
LEGACY_CONFIG_PATH = os.path.join(BASE_DIR, "position.cfg")


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

    @property
    def has_database(self) -> bool:
        return self.database is not None and self.database.is_usable

    @property
    def label(self) -> str:
        return f"{self.account_name} / {self.name}" if self.account_name else self.name

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "database": self.database.to_dict() if self.database else None,
        }

    @classmethod
    def from_dict(cls, data: dict, account_name: str = "") -> "ProfileConfig":
        return cls(
            name=str(data.get("name", "") or ""),
            enabled=bool(data.get("enabled", True)),
            database=DatabaseConfig.from_dict(data.get("database")),
            account_name=account_name,
        )


@dataclass
class AccountConfig:
    """A totalbattle.com login and the profiles reachable from it."""

    name: str = ""
    login: str = ""
    password: str = ""
    enabled: bool = True
    # Own browser profile folder, so this account keeps its own verified session.
    # Empty = the shared profile, which is all a single account ever needs.
    browser_profile: str = ""
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
            "profiles": [p.to_dict() for p in self.profiles],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AccountConfig":
        name = str(data.get("name", "") or "")
        return cls(
            name=name,
            login=str(data.get("login", "") or ""),
            password=str(data.get("password", "") or ""),
            enabled=bool(data.get("enabled", True)),
            browser_profile=str(data.get("browser_profile", "") or ""),
            profiles=[ProfileConfig.from_dict(p, name) for p in data.get("profiles", []) or []],
        )


def _coerce(value: Any, spec) -> Any:
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
                raise ValueError(f"config.json inválido ({exc}). Corrija o arquivo ou apague-o para recomeçar.")

        merged = default_config()
        for section in SECTIONS:
            stored = raw.get(section.key) or {}
            for spec in section.fields:
                if spec.key in stored:
                    merged[section.key][spec.key] = _coerce(stored[spec.key], spec)
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
        return self.data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value: Any):
        self.data.setdefault(section, {})[key] = _coerce(value, field_for(section, key))

    def section(self, key: str) -> Dict[str, Any]:
        return copy.deepcopy(self.data.get(key, {}))

    # -- accounts
    @property
    def accounts(self) -> List[AccountConfig]:
        return [AccountConfig.from_dict(a) for a in self.data.get("accounts", [])]

    @accounts.setter
    def accounts(self, accounts: List[AccountConfig]):
        self.data["accounts"] = [a.to_dict() for a in accounts]

    def enabled_accounts(self) -> List[AccountConfig]:
        return [a for a in self.accounts if a.enabled and a.active_profiles]

    def resolved_log_dir(self) -> str:
        raw = self.get("execution", "log_dir") or "execution_logs"
        return raw if os.path.isabs(raw) else os.path.join(BASE_DIR, raw)

    def user_data_dir(self) -> str:
        raw = self.get("browser", "user_data_dir") or ""
        if raw:
            return os.path.expandvars(os.path.expanduser(raw))
        return os.path.join(os.path.expanduser("~"), ".total_battle_chest_profile")

    def browser_profile_for(self, account: AccountConfig) -> str:
        """
        Which browser profile folder an account should use.

        Empty on the account means the shared folder - the case of a single
        account, and the one that must not change, because that folder already
        holds a session the game has verified. A name becomes a sibling folder,
        so each account keeps a verified session of its own.
        """
        custom = (getattr(account, "browser_profile", "") or "").strip()
        default = self.user_data_dir()
        if not custom:
            return default
        if os.path.isabs(custom):
            return os.path.expandvars(os.path.expanduser(custom))
        slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in custom)
        return os.path.join(os.path.dirname(default), f"{os.path.basename(default)}_{slug}")


def migrate_legacy_config(legacy_path: str = LEGACY_CONFIG_PATH) -> List[AccountConfig]:
    """
    Rebuilds the account list from the old position.cfg.

    Only the database credentials survive the move. The old [COORDINATES] were
    screen positions of the desktop client; in the browser they point nowhere,
    so they are dropped and the calibration wizard takes over.

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
