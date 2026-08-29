"""
Abstract base class for all bot feature modules.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict
from config.config_loader import AppConfig
from core.window_manager import WindowManager
from core.vision import Vision
from core.ocr_engine import OCREngine
from core.bot_controller import BotController
from core.game_state import GameStateHelper


class BaseModule(ABC):
    """Base interface for all automation modules."""

    def __init__(
        self,
        config: AppConfig,
        window_mgr: WindowManager,
        vision: Vision,
        ocr: OCREngine,
        controller: BotController,
        game_state: GameStateHelper,
    ):
        self.config = config
        self.window_mgr = window_mgr
        self.vision = vision
        self.ocr = ocr
        self.controller = controller
        self.game_state = game_state

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the module."""
        pass

    @abstractmethod
    def run(self, **kwargs) -> Dict[str, Any]:
        """
        Executes the module task.
        Returns a summary dictionary with metrics/results.
        """
        pass
