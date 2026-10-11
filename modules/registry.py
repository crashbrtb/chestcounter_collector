"""
The modules the application has, in the order a run does them on each profile.

This list is the only place a module is named. The configuration file, the
Modules tab, the per-profile fields, the calibration wizard and the run are all
built from it - so a new module is its folder under modules/ plus one line here.
"""

from typing import Dict, List, Type

from config.settings import ConfigManager
from core.calibration import SHARED_GROUP, StepGroup

from .base_module import BaseModule
from .chests import ChestCollector
from .events import EventsModule
from .members import MembersModule

# Chests first: it is the job that runs every time. The members module comes
# right after because its screens are in the same clan window, which is where
# the wizard is when the chest steps end. The events module then reads the
# Journal on the game as the others left it.
MODULES: List[Type[BaseModule]] = [ChestCollector, MembersModule, EventsModule]

MODULES_BY_KEY: Dict[str, Type[BaseModule]] = {module.key: module for module in MODULES}


def enabled_modules(config: ConfigManager) -> List[Type[BaseModule]]:
    return [module for module in MODULES if config.module(module.key).get("enabled", module.default_enabled)]


def step_groups() -> List[StepGroup]:
    """
    Every calibration step: the shared ones, then each module's.

    A step is known by its name, so one that two modules declare is calibrated
    once - it stays in the first group that has it and is left out of the others.
    """
    groups = [SHARED_GROUP]
    seen = {step.name for step in SHARED_GROUP.steps}
    for module in MODULES:
        steps = [step for step in module.steps if step.name not in seen]
        seen.update(step.name for step in steps)
        if steps:
            groups.append(StepGroup(module.key, module.title, steps))
    return groups


def modules_in_use(config: ConfigManager) -> List[Type[BaseModule]]:
    """Enabled modules that have at least one profile set up for them."""
    profiles = [profile for account in config.enabled_accounts() for profile in account.active_profiles]
    return [module for module in enabled_modules(config)
            if any(module.configured_for(profile) for profile in profiles)]


def calibration_scope(config: ConfigManager) -> List[str]:
    """
    The steps the wizard has to see calibrated: the shared ones and those of the modules in use.

    A module nobody has set up a profile for asks for nothing: its steps are in
    the wizard all the same, but leaving them for later does not make the
    calibration incomplete.
    """
    names = [step.name for step in SHARED_GROUP.steps if not step.optional]
    for module in modules_in_use(config):
        names += [step.name for step in module.steps if not step.optional and step.name not in names]
    return names
