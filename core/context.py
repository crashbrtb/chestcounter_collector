"""
Assembling the pieces a run needs.

Every service is built from one ConfigManager, in one place, so a module never
reaches for a global and the GUI can build the very same context to run a test
collection without duplicating the wiring.
"""

from dataclasses import dataclass
from typing import Optional

from config.settings import ConfigManager
from utils.logger import logger

from .browser import Browser
from .calibration import Calibration
from .game_state import GameState
from .ocr import OCREngine
from .session import LoginManager, ProfileSwitcher
from .vision import Vision


@dataclass
class RunContext:
    config: ConfigManager
    calibration: Calibration
    browser: Browser
    ocr: OCREngine
    vision: Vision
    game_state: GameState
    login: LoginManager
    profiles: ProfileSwitcher

    def start_browser(self):
        """
        Opens the browser and tells the calibration which viewport it got.

        The viewport has to be read after the game tab exists: it is the number
        every calibrated coordinate is rescaled against.

        Calling this again on a live connection is a no-op, so a run that
        executes more than one module does not reconnect between them.
        """
        if self.browser.ws is not None:
            return self.browser

        self.browser.start()
        self.sync_viewport()
        return self.browser

    def sync_viewport(self):
        """
        Puts the page back to the size it was calibrated at, and tells the
        calibration what it actually got.

        Resizing is tried first because it removes the problem; rescaling the
        coordinates only compensates for it, and a game interface does not
        scale linearly - a rescaled point drifts off a small button and the
        click lands on nothing.
        """
        wanted_w, wanted_h = self.calibration.viewport
        if wanted_w and wanted_h:
            width, height = self.browser.viewport()
            if (width, height) != (wanted_w, wanted_h):
                logger.info(f"Page is {width}x{height}; resizing to the calibrated {wanted_w}x{wanted_h}...")
                if self.browser.set_viewport(wanted_w, wanted_h):
                    logger.info("Window resized: calibrated coordinates are used as they were recorded.")
                else:
                    logger.warning(
                        "Could not restore the calibrated page size; coordinates will be rescaled, "
                        "which can miss small buttons. Recalibrate at the current size if clicks fail."
                    )

        width, height = self.browser.viewport()
        self.calibration.use_viewport(width, height)
        logger.info(
            f"Game viewport: {width}x{height} "
            f"(calibrated at {self.calibration.viewport[0]}x{self.calibration.viewport[1]})"
        )

    def close(self):
        if self.config.get("execution", "close_browser_on_finish", True):
            self.browser.close()
        else:
            logger.info("Leaving the browser open (see Execução > Fechar o navegador ao terminar).")
            self.browser.disconnect()


def build_context(config: Optional[ConfigManager] = None, calibration: Optional[Calibration] = None) -> RunContext:
    config = config or ConfigManager()
    calibration = calibration or Calibration()

    browser = Browser(config.section("browser"), config.user_data_dir())
    ocr = OCREngine(browser, config.section("ocr"))
    vision = Vision(browser, config.section("vision"))
    timing = config.section("timing")
    game_state = GameState(browser, vision, calibration, timing)

    return RunContext(
        config=config,
        calibration=calibration,
        browser=browser,
        ocr=ocr,
        vision=vision,
        game_state=game_state,
        login=LoginManager(browser, config.section("login")),
        profiles=ProfileSwitcher(browser, ocr, vision, calibration, game_state, timing),
    )
