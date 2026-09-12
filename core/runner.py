"""
The run itself: accounts in order, and inside each one, its profiles.

This is the order the whole redesign was for. An account is opened by logging
in, its profiles are collected one after another, and only then is the session
ended and the next account opened. Nothing here assumes a session left behind by
a previous run.

Failures are contained at the smallest level that still makes sense: a profile
that fails is retried, then abandoned with a reason recorded, and the run moves
on. An account that cannot be logged into loses its profiles but not the rest of
the run. `execution.stop_on_error` turns that off for anyone who would rather
have the run halt.
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.settings import AccountConfig, ProfileConfig
from modules.chest_collector import ChestCollector
from utils.cancel import Cancelled, cancellation
from utils.logger import logger, screenshot_dir

from .calibration import STEPS_BY_NAME
from .session import SessionError

REQUIRED_STEPS = (
    "profile_menu_button", "profiles_list_area", "profile_name_display_area",
    "clan_button", "gift_button", "chest_area", "open_button_area", "open_button",
)


class RunnerError(RuntimeError):
    pass


class CollectorRunner:
    """Drives a full collection across every enabled account and profile."""

    def __init__(self, context):
        self.ctx = context
        self.execution = context.config.section("execution")
        self.timing = context.config.section("timing")
        self.collector = ChestCollector(context)
        self.results: List[Dict[str, Any]] = []

    # ------------------------------------------------------------- validation
    def preflight(self) -> List[str]:
        """Everything that would make the run pointless, reported before it starts."""
        problems = []

        accounts = self.ctx.config.enabled_accounts()
        if not accounts:
            problems.append("No enabled accounts with profiles found. Configure in Accounts and profiles.")

        for account in accounts:
            if self.ctx.login.enabled and not (account.login and account.password):
                problems.append(f"Account '{account.name}' has no username or password.")
            if not account.collectable_profiles:
                problems.append(
                    f"Account '{account.name}' has no profiles with a configured database."
                )

        # Sessions are no longer wiped between accounts (that costs an e-mail
        # verification code), so two accounts sharing one browser profile would
        # both run inside whichever session happens to be open - and the second
        # account's chests would land in the first account's database.
        if len(accounts) > 1 and not self.ctx.login.clear_between_accounts:
            folders = [self.ctx.config.browser_profile_for(a) for a in accounts]
            if len(set(folders)) < len(accounts):
                problems.append(
                    "Multiple accounts share the same browser profile. Since sessions are "
                    "no longer cleared between accounts, the second account would use the first's session. "
                    "Assign each account its own 'Browser profile' in Accounts and profiles."
                )

        missing = [name for name in REQUIRED_STEPS if not self.ctx.calibration.is_calibrated(name)]
        if missing:
            titles = ", ".join(STEPS_BY_NAME[name].title for name in missing)
            problems.append(f"Incomplete calibration. Missing steps: {titles}.")

        # Works with or without the browser open: with it, against the live page
        # size; without it, against the size the calibration was recorded at.
        outside = self.ctx.calibration.out_of_bounds()
        if outside:
            titles = ", ".join(STEPS_BY_NAME[n].title for n in outside if n in STEPS_BY_NAME)
            problems.append(
                f"These calibration steps fall OUTSIDE the page and clicks will hit nothing: "
                f"{titles}. Redo them in the wizard, with the window at your normal running size."
            )

        ocr_ok, ocr_reason = self.ctx.ocr.status()
        if not ocr_ok:
            problems.append(f"OCR unavailable: {ocr_reason}.")
        else:
            logger.info(f"OCR engine: {ocr_reason}")
            from core.ocr import RapidOCRBackend
            rapid_ok, _ = RapidOCRBackend.available()
            if not rapid_ok:
                logger.warning(
                    "ALERTA CRÍTICO: RapidOCR não está disponível neste computador! "
                    "O Tesseract está sendo usado como fallback. O Tesseract frequentemente falha "
                    "ao ler a lista de perfis e nomes complexos. Execute install.bat para instalar RapidOCR."
                )

        return problems

    # -------------------------------------------------------------- execution
    def run(self, only_account: Optional[str] = None, only_profile: Optional[str] = None) -> Dict[str, Any]:
        started = datetime.now()
        problems = self.preflight()
        if problems:
            for problem in problems:
                logger.error(problem)
            raise RunnerError("Execution cannot start: " + " | ".join(problems))

        accounts = self.ctx.config.enabled_accounts()
        if only_account:
            accounts = [a for a in accounts if only_account.lower() in a.name.lower()]
            if not accounts:
                raise RunnerError(f"Account '{only_account}' not found among enabled accounts.")

        self.ctx.start_browser()

        for index, account in enumerate(accounts, start=1):
            cancellation.check()
            logger.info("=" * 60)
            logger.info(f"ACCOUNT {index}/{len(accounts)}: '{account.name}'")
            logger.info("=" * 60)
            try:
                self._run_account(account, only_profile)
            except Cancelled:
                raise
            except SessionError as exc:
                self._record_failure(account, None, str(exc))
                if self.execution.get("stop_on_error"):
                    raise
            except Exception as exc:  # noqa: BLE001 - one account must not sink the run
                logger.exception(f"Unexpected failure on account '{account.name}': {exc}")
                self._record_failure(account, None, f"unexpected error: {exc}")
                self._save_error_screenshot(f"account_{account.name}")
                if self.execution.get("stop_on_error"):
                    raise

        return self._summary(started)

    def _run_account(self, account: AccountConfig, only_profile: Optional[str]):
        # An account with a browser profile of its own gets the browser restarted
        # on that folder, so it arrives at a session the game has already verified.
        wanted_profile = self.ctx.config.browser_profile_for(account)
        if not self.ctx.browser.switch_profile(wanted_profile):
            raise SessionError(
                f"Account '{account.name}' uses browser profile '{wanted_profile}', "
                f"which could not be opened."
            )
        self.ctx.sync_viewport()

        if self.ctx.login.enabled:
            if not self.ctx.login.enter_account(account):
                raise SessionError(f"Could not log into account '{account.name}'.")
        else:
            self.ctx.browser.navigate()

        self.ctx.browser.wait_for_load()
        self.ctx.game_state.prepare_board()

        profiles = account.active_profiles
        if only_profile:
            profiles = [p for p in profiles if only_profile.lower() in p.name.lower()]

        # Every profile name of the account, so the switcher can tell near-twin
        # names apart by competition instead of by threshold.
        siblings = [p.name for p in account.profiles if p.name]

        for profile in profiles:
            if not profile.has_database:
                logger.warning(
                    f"Profile '{profile.label}' has no database configured and was skipped "
                    f"(a profile without a database has nowhere to record its chests)."
                )
                self._record_failure(account, profile, "profile has no database")
                continue
            cancellation.check()
            self._run_profile(account, profile, siblings)
            cancellation.sleep(float(self.timing.get("between_profiles_wait", 3.0)))

        if self.ctx.login.enabled:
            self.ctx.login.end_session()

    def _run_profile(self, account: AccountConfig, profile: ProfileConfig, siblings: List[str]):
        attempts = int(self.execution.get("retries_per_profile", 2))
        for attempt in range(1, attempts + 1):
            logger.info(f"--- Profile '{profile.label}' (attempt {attempt}/{attempts}) ---")
            try:
                if not self.ctx.profiles.switch_to(profile, siblings):
                    raise SessionError(f"could not activate profile '{profile.name}'")

                result = self.collector.collect_for_profile(profile)
                result.setdefault("account", account.name)
                self.results.append(result)
                if result.get("success"):
                    return
                logger.warning(f"Collection for '{profile.label}' reported: {result.get('reason', 'failure')}")
            except Cancelled:
                raise
            except SessionError as exc:
                logger.error(f"Profile '{profile.label}': {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Unexpected failure on profile '{profile.label}': {exc}")
                self._save_error_screenshot(f"profile_{profile.name}")

            if attempt < attempts:
                logger.info("Retrying the profile after resetting the board...")
                self.ctx.game_state.prepare_board()

        self._record_failure(account, profile, "all attempts failed")

    # ---------------------------------------------------------------- results
    def _record_failure(self, account: AccountConfig, profile: Optional[ProfileConfig], reason: str):
        logger.error(f"FAILED: {account.name}{' / ' + profile.name if profile else ''} - {reason}")

        self.results.append({
            "success": False,
            "account": account.name,
            "profile": profile.name if profile else "",
            "collected": 0,
            "incomplete": 0,
            "reason": reason,
        })

    def _save_error_screenshot(self, label: str) -> Optional[str]:
        if not self.execution.get("screenshot_on_error", True):
            return None
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:60]
        path = f"{screenshot_dir()}/{datetime.now():%Y%m%d_%H%M%S}_{safe}.png"
        if self.ctx.browser.save_screenshot(path):
            logger.info(f"Screenshot of the failure saved to {path}")
            return path
        return None

    def _summary(self, started: datetime) -> Dict[str, Any]:
        collected = sum(r.get("collected", 0) for r in self.results)
        incomplete = sum(r.get("incomplete", 0) for r in self.results)
        failures = [r for r in self.results if not r.get("success")]
        duration = datetime.now() - started

        logger.info("=" * 60)
        logger.info(f"RUN FINISHED in {duration}: {collected} chests collected, {incomplete} incomplete.")
        for failure in failures:
            logger.warning(
                f"  not collected: {failure.get('account')}/{failure.get('profile')} - {failure.get('reason')}"
            )
        logger.info("=" * 60)

        return {
            "collected": collected,
            "incomplete": incomplete,
            "profiles_done": len([r for r in self.results if r.get("success")]),
            "failures": failures,
            "duration": str(duration),
            "results": self.results,
        }
