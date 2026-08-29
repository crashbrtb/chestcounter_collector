"""
Controller for mouse clicks, scrolling, key presses, and clipboard text typing.
"""

import time
from typing import Optional, Tuple
import pyautogui
import pyperclip
from utils.logger import logger
from config.settings import DEFAULT_CLICK_DELAY, DEFAULT_ACTION_DELAY


class BotController:
    """Provides human-like, safe automation actions for input devices."""

    def __init__(self, default_click_delay: float = DEFAULT_CLICK_DELAY):
        self.default_click_delay = default_click_delay

    def click(
        self,
        x: int,
        y: int,
        delay: Optional[float] = None,
        button: str = "left",
        clicks: int = 1,
    ):
        """Clicks at the given screen coordinates and waits."""
        pyautogui.click(x=x, y=y, clicks=clicks, button=button)
        wait_time = delay if delay is not None else self.default_click_delay
        if wait_time > 0:
            time.sleep(wait_time)

    def move_to(self, x: int, y: int):
        """Moves mouse cursor to coordinate."""
        pyautogui.moveTo(x, y)

    def scroll(
        self,
        clicks: int = -100,
        x: Optional[int] = None,
        y: Optional[int] = None,
        repetitions: int = 5,
        delay_after: float = 1.0,
    ):
        """
        Scrolls the mouse wheel at (x, y). Negative clicks scroll down, positive scroll up.
        """
        if x is not None and y is not None:
            self.move_to(x, y)

        for _ in range(repetitions):
            pyautogui.scroll(clicks)
            time.sleep(0.1)

        if delay_after > 0:
            time.sleep(delay_after)

    def paste_text(self, text: str, press_enter: bool = False, delay: float = DEFAULT_ACTION_DELAY):
        """
        Copies text to clipboard and pastes using Ctrl+V.
        Supports UTF-8 characters, emojis, and language accents without typing bugs.
        """
        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
        if press_enter:
            time.sleep(0.2)
            pyautogui.press("enter")
        if delay > 0:
            time.sleep(delay)
        logger.info(f"Pasted text into active input: '{text}' (Enter={press_enter})")

    def press_key(self, key: str, delay: float = DEFAULT_ACTION_DELAY):
        """Presses a single keyboard key."""
        pyautogui.press(key)
        if delay > 0:
            time.sleep(delay)

    def hotkey(self, *keys, delay: float = DEFAULT_ACTION_DELAY):
        """Executes a hotkey combination (e.g. ('ctrl', 'c'))."""
        pyautogui.hotkey(*keys)
        if delay > 0:
            time.sleep(delay)
