"""
The modules the application has, in the order a run does them on each profile.

This list is the only place a module is named. The configuration file, the
Modules tab, the per-profile fields and the run are all built from it - so a
new module is its folder under modules/ plus one line here.
"""

from typing import Dict, List, Type

from config.settings import ConfigManager

from .base_module import BaseModule
from .chests import ChestCollector
from .events import EventsModule
from .members import MembersModule
from .shield import ShieldModule

MODULES: List[Type[BaseModule]] = [ShieldModule, ChestCollector, MembersModule, EventsModule]

MODULES_BY_KEY: Dict[str, Type[BaseModule]] = {module.key: module for module in MODULES}


def enabled_modules(config: ConfigManager) -> List[Type[BaseModule]]:
    return [module for module in MODULES if config.module(module.key).get("enabled", module.default_enabled)]


def modules_in_use(config: ConfigManager) -> List[Type[BaseModule]]:
    """Enabled modules that have at least one profile set up for them."""
    profiles = [profile for account in config.enabled_accounts() for profile in account.active_profiles]
    return [module for module in enabled_modules(config)
            if any(module.configured_for(profile) for profile in profiles)]
