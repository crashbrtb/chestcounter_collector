"""
Chat module - announcements sent to the clan chat.

In the browser the text goes in through `Input.insertText`, which delivers the
whole string as one event: accents and emoji arrive intact and no keyboard
layout is involved. That is the part worth keeping from the old clipboard trick,
without the clipboard.

Like the journal module, it waits on calibration steps of its own (where the
chat opens and where its input box is) before it can do anything unattended.
"""

from typing import Any, Dict

from utils.logger import logger

from .base_module import BaseModule

REQUIRED_STEPS = ("chat_button", "chat_input")


class ChatAutomator(BaseModule):
    @property
    def name(self) -> str:
        return "ChatAutomator"

    def send_message(self, message: str) -> bool:
        chat_button = self.calibration.point("chat_button")
        chat_input = self.calibration.point("chat_input")
        if not (chat_button and chat_input):
            return False

        self.browser.click(chat_button[0], chat_button[1], delay=1.0)
        self.browser.click(chat_input[0], chat_input[1], delay=0.4)
        self.browser.insert_text(message)
        self.browser.press_key("Enter", delay=0.3)
        logger.info(f"Chat message sent: '{message}'")
        return True

    def run(self, message: str = "", **kwargs) -> Dict[str, Any]:
        missing = self.calibration.require(*REQUIRED_STEPS)
        if missing:
            logger.warning(
                f"The Chat module has no calibration steps configured yet ({', '.join(missing)}). "
                f"Nothing was sent."
            )
            return {"module": self.name, "success": False, "reason": "no calibration"}
        if not message:
            return {"module": self.name, "success": False, "reason": "empty message"}
        return {"module": self.name, "success": self.send_message(message), "message": message}
