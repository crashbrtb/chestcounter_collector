"""
Shared base for the feature modules (chests, journal, chat).

A module receives the whole RunContext rather than a handful of services: what a
module needs tends to grow (the chest collector ended up needing the profile
switcher too), and threading one more argument through every constructor each
time was how the old signatures drifted apart.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseModule(ABC):
    def __init__(self, context):
        self.ctx = context

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

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the module, used in logs."""

    @abstractmethod
    def run(self, **kwargs) -> Dict[str, Any]:
        """Executes the module and returns a summary of what it did."""
