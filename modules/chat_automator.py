"""
Chat Automator module for broadcasting automated clan/kingdom messages and announcements.
"""

from typing import Dict, Any, Optional, Tuple
from utils.logger import logger
from .base_module import BaseModule


class ChatAutomator(BaseModule):
    """
    Automates sending structured clan announcements, reminders, and alerts
    using UTF-8 clipboard pasting (supporting emojis and accents).
    """

    @property
    def name(self) -> str:
        return "ChatAutomator"

    def open_chat(self, chat_icon_coord: Tuple[int, int]) -> bool:
        """Opens the in-game chat interface."""
        logger.info(f"Opening chat interface at {chat_icon_coord}...")
        self.controller.click(chat_icon_coord[0], chat_icon_coord[1], delay=1.0)
        return True

    def send_message(self, message: str, chat_input_coord: Tuple[int, int]) -> bool:
        """
        Clicks the chat input field, pastes the text, and presses Enter.
        """
        logger.info(f"Sending chat message: '{message}'...")
        # 1. Click input box
        self.controller.click(chat_input_coord[0], chat_input_coord[1], delay=0.5)

        # 2. Paste text and send
        self.controller.paste_text(message, press_enter=True, delay=0.5)
        logger.info("Message sent successfully.")
        return True

    def run(self, message: str = "", chat_input_coord: Optional[Tuple[int, int]] = None, **kwargs) -> Dict[str, Any]:
        """Executes chat message sending."""
        if not message or not chat_input_coord:
            logger.warning("Message or input coordinate missing.")
            return {"module": self.name, "success": False}

        success = self.send_message(message, chat_input_coord)
        return {"module": self.name, "success": success, "message": message}
