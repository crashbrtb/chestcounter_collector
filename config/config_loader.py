"""
Configuration loader for position.cfg and application settings.
"""

import os
import configparser
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
from .settings import CONFIG_FILE_PATH


@dataclass
class AccountConfig:
    section: str
    account: str
    host: str
    user: str
    password: str
    database: str


@dataclass
class CoordinatesConfig:
    window_title: str = "Total Battle"
    launcher_title: str = "MainWindow"
    path_total_battle: str = ""
    coords_play_button: Optional[Tuple[int, int]] = None
    coord_clan_button: Optional[Tuple[int, int]] = None
    coord_gift_button: Optional[Tuple[int, int]] = None
    coord_gifts_tab: Optional[Tuple[int, int]] = (805, 285)
    coord_triumphal_gifts_tab: Optional[Tuple[int, int]] = (1055, 285)
    triumphal_gifts_tab_area: Optional[Tuple[int, int, int, int]] = (900, 265, 1200, 315)
    chest_area: Optional[Tuple[int, int, int, int]] = None
    cord_menu_button_open_chest: Optional[Tuple[int, int, int, int]] = None
    screen_area: Optional[Tuple[int, int, int, int]] = None
    account_menu_button: Optional[Tuple[int, int]] = None
    accounts_area: Optional[Tuple[int, int, int, int]] = None
    account_name_display_area: Optional[Tuple[int, int, int, int]] = None
    switch_progress_confirm_button_area: Optional[Tuple[int, int, int, int]] = None


@dataclass
class AppConfig:
    coordinates: CoordinatesConfig = field(default_factory=CoordinatesConfig)
    accounts: List[AccountConfig] = field(default_factory=list)


def parse_value(val: Optional[str]) -> Any:
    """Parses a string value from INI into bool, int, tuple, or string."""
    if val is None:
        return None
    val = val.strip()
    if not val:
        return None
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    if val.isdigit():
        return int(val)
    if val.startswith("(") and val.endswith(")"):
        inner = val[1:-1].strip()
        if inner:
            return tuple(int(x.strip()) for x in inner.split(",") if x.strip())
    return val


class ConfigLoader:
    """Reads and parses position.cfg into structured AppConfig objects."""

    def __init__(self, config_path: str = CONFIG_FILE_PATH):
        self.config_path = config_path

    def load(self) -> AppConfig:
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")

        parser = configparser.ConfigParser()
        parser.read(self.config_path, encoding="utf-8")

        app_config = AppConfig()

        # Parse COORDINATES
        if "COORDINATES" in parser:
            coords_sec = parser["COORDINATES"]
            app_config.coordinates = CoordinatesConfig(
                window_title=coords_sec.get("window_title", "Total Battle"),
                launcher_title=coords_sec.get("laucher_title", "MainWindow"),
                path_total_battle=coords_sec.get("path_total_battle", ""),
                coords_play_button=parse_value(coords_sec.get("coords_play_button")),
                coord_clan_button=parse_value(coords_sec.get("coord_clan_button")),
                coord_gift_button=parse_value(coords_sec.get("coord_gift_button")),
                coord_gifts_tab=parse_value(coords_sec.get("coord_gifts_tab")) or (805, 285),
                coord_triumphal_gifts_tab=parse_value(coords_sec.get("coord_triumphal_gifts_tab")) or (1055, 285),
                triumphal_gifts_tab_area=parse_value(coords_sec.get("triumphal_gifts_tab_area")) or (900, 265, 1200, 315),
                chest_area=parse_value(coords_sec.get("chest_area")),
                cord_menu_button_open_chest=parse_value(coords_sec.get("cord_menu_button_open_chest")),
                screen_area=parse_value(coords_sec.get("screen_area")),
                account_menu_button=parse_value(coords_sec.get("account_menu_button")),
                accounts_area=parse_value(coords_sec.get("accounts_area")),
                account_name_display_area=parse_value(coords_sec.get("account_name_display_area")),
                switch_progress_confirm_button_area=parse_value(coords_sec.get("switch_progress_confirm_button_area")),
            )

        # Parse Account sections
        for section in parser.sections():
            if section.lower().startswith("account"):
                acc = AccountConfig(
                    section=section,
                    account=parser.get(section, "account", fallback=""),
                    host=parser.get(section, "host", fallback="127.0.0.1"),
                    user=parser.get(section, "user", fallback="root"),
                    password=parser.get(section, "password", fallback=""),
                    database=parser.get(section, "database", fallback=""),
                )
                app_config.accounts.append(acc)

        return app_config
