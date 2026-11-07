"""
Assembling the pieces a run needs.

Every service is built from one ConfigManager, in one place, so a module never
reaches for a global and the GUI can build the very same context to run a test
collection without duplicating the wiring.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, Optional

from config.settings import AccountConfig, ConfigManager, same_folder
from utils.logger import logger

from .browser import Browser
from .session import LoginManager, ProfileSwitcher


@dataclass
class RunContext:
    config: ConfigManager
    browser: Browser
    login: LoginManager
    profiles: ProfileSwitcher
    # The other accounts' windows, each with services of its own.
    windows: Dict[str, "RunContext"] = field(default_factory=dict)

    def for_account(self, account: AccountConfig) -> "RunContext":
        """The services working on this account's window. Asked from one thread: the runner's."""
        if self.browser.port == account.cdp_port and same_folder(self.browser.user_data_dir,
                                                                 self.config.chrome_folder_for(account)):
            return self
        if account.name not in self.windows:
            self.windows[account.name] = build_context(self.config, account)
        return self.windows[account.name]

    def start_browser(self):
        """
        Opens the browser for this account's window.

        Calling this again on a live connection is a no-op, so a run that
        executes more than one module does not reconnect between them.
        """
        if self.browser.ws is not None:
            return self.browser

        self.browser.start()
        return self.browser

    def close(self):
        for window in self.windows.values():
            window.close()
        if self.config.get("execution", "close_browser_on_finish", True):
            self.browser.close()
        else:
            logger.info("Leaving the browser open (see Execution > Close browser on finish).")
            self.browser.disconnect()


def browser_for(config: ConfigManager, account: AccountConfig) -> Browser:
    """The Chrome window of an account: its port, its folder."""
    return Browser(dict(config.section("browser"), cdp_port=account.cdp_port), config.chrome_folder_for(account))


def build_context(config: Optional[ConfigManager] = None, account: Optional[AccountConfig] = None) -> RunContext:
    """The services of one window: the account's, or the first account's for the checks."""
    config = config or ConfigManager()
    config.pin_windows()

    if account is None:
        account = next(iter(config.enabled_accounts() or config.accounts), None)
    if account is None:
        browser = Browser(dict(config.section("browser"), cdp_port=config.get("browser", "first_cdp_port")),
                          os.path.join(config.profiles_dir(), "default"))
    else:
        browser = browser_for(config, account)

    return RunContext(
        config=config,
        browser=browser,
        login=LoginManager(browser, config.section("login")),
        profiles=ProfileSwitcher(browser),
    )
