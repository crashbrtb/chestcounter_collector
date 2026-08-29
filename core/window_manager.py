"""
Window manager for detecting, focusing, launching, and closing Total Battle.
"""

import time
import subprocess
from typing import Optional, Tuple, List
import pyautogui
from screeninfo import get_monitors
from utils.logger import logger
from config.settings import DEFAULT_LAUNCHER_WAIT, DEFAULT_GAME_START_WAIT


class WindowManager:
    """Manages the lifecycle of Total Battle game and launcher windows."""

    def __init__(self, window_title: str = "Total Battle", launcher_title: str = "MainWindow", launcher_path: str = ""):
        self.window_title = window_title
        self.launcher_title = launcher_title
        self.launcher_path = launcher_path

    @staticmethod
    def get_monitor_resolution() -> Tuple[int, int, int, int]:
        """Returns the primary monitor bounding box (0, 0, width, height)."""
        monitors = get_monitors()
        if monitors:
            return (0, 0, monitors[0].width, monitors[0].height)
        # Fallback to pyautogui resolution
        size = pyautogui.size()
        return (0, 0, size.width, size.height)

    def is_window_open(self, title: str) -> bool:
        """Checks if any window containing the given title is open and active."""
        windows = pyautogui.getWindowsWithTitle(title)
        return len(windows) > 0

    def activate_window(self, title: str) -> bool:
        """Finds, activates, and maximizes the window with the given title."""
        try:
            windows = pyautogui.getWindowsWithTitle(title)
            if windows:
                win = windows[0]
                if not win.isMaximized:
                    win.maximize()
                win.activate()
                time.sleep(0.5)
                logger.info(f"Window '{title}' activated and maximized.")
                return True
            else:
                logger.debug(f"Window '{title}' not found.")
                return False
        except Exception as e:
            logger.warning(f"Failed to activate window '{title}': {e}")
            return False

    def close_window(self, title: str) -> bool:
        """Closes the window with the given title."""
        try:
            windows = pyautogui.getWindowsWithTitle(title)
            if windows:
                for win in windows:
                    win.close()
                logger.info(f"Window '{title}' closed.")
                return True
            return False
        except Exception as e:
            logger.warning(f"Failed to close window '{title}': {e}")
            return False

    def launch_game_via_launcher(self, play_button_coord: Optional[Tuple[int, int]] = None) -> bool:
        """Launches Total Battle using the launcher executable path."""
        logger.info("Starting Total Battle Launcher...")
        if not self.launcher_path:
            logger.error("No launcher path configured.")
            return False

        try:
            pyautogui.hotkey("win", "r")
            time.sleep(0.5)
            pyautogui.write(self.launcher_path)
            pyautogui.press("enter")
            logger.info(f"Waiting {DEFAULT_LAUNCHER_WAIT}s for launcher to start...")
            time.sleep(DEFAULT_LAUNCHER_WAIT)

            if self.activate_window(self.launcher_title):
                if play_button_coord:
                    logger.info(f"Clicking Play button at {play_button_coord}...")
                    pyautogui.click(play_button_coord[0], play_button_coord[1])
                    time.sleep(DEFAULT_GAME_START_WAIT)
                return True
            else:
                logger.error(f"Launcher window '{self.launcher_title}' not detected.")
                return False
        except Exception as e:
            logger.error(f"Error launching Total Battle: {e}")
            return False

    def ensure_game_open(self, play_button_coord: Optional[Tuple[int, int]] = None) -> bool:
        """Ensures the Total Battle game is running and focused."""
        logger.info("Checking if Total Battle is already running...")
        if self.activate_window(self.window_title):
            return True

        logger.info("Total Battle is not open. Attempting to launch...")
        if self.launch_game_via_launcher(play_button_coord):
            if self.activate_window(self.window_title):
                return True

        logger.error("Could not ensure Total Battle game is open.")
        return False
