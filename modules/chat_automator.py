"""
Chat module - announcements sent to the clan chat.

In the browser the text goes in through `Input.insertText`, which delivers the
whole string as one event: accents and emoji arrive intact and no keyboard
layout is involved. That is the part worth keeping from the old clipboard trick,
without the clipboard.

Not in modules/registry.py yet: it has nothing to announce by itself. It is kept
as the smallest example of what a module is - its own calibration steps, its
own parameter, one job per profile - and listing it in the registry is all it
takes for it to show up in the Modules tab, the wizard and the run.
"""

from typing import Any, Dict

from config.schema import Field
from config.settings import ProfileConfig
from core.calibration import Step
from utils.logger import logger

from .base_module import BaseModule


class ChatAutomator(BaseModule):
    key = "chat"
    title = "Clan chat message"
    description = "Sends a message to the clan chat of each profile."
    default_enabled = False
    settings = (
        Field("message", "str", "Message", "", "What is sent to the clan chat. Empty sends nothing."),
    )
    steps = (
        Step("chat_button", "click", "Chat button",
             "Click the button that opens the CLAN CHAT.", acts=True),
        Step("chat_input", "click", "Chat input box",
             "With the chat open, click the box where a message is typed."),
    )
    required_steps = ("chat_button", "chat_input")

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

    def run_for_profile(self, profile: ProfileConfig) -> Dict[str, Any]:
        message = str(self.options.get("message") or "").strip()
        if not message:
            return {"success": False, "retry": False, "profile": profile.name, "reason": "empty message"}
        return {"success": self.send_message(message), "profile": profile.name, "message": message}
