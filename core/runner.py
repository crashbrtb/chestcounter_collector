"""
The run itself: accounts in order, inside each one its profiles, and on each
profile the modules that are due.

This is the order the whole redesign was for. An account is opened by logging
in, its profiles are visited one after another, and only then is the session
ended and the next account opened. Nothing here assumes a session left behind by
a previous run.

What is done on a profile is not decided here: each enabled module
(modules/registry.py) is asked whether the profile has what it needs, the
schedule (core/schedule.py) says whether it is due, and the ones left are run
one after the other with the game already on that profile. A profile with
nothing to do is not even switched to, and a run with nothing to do does not
open the browser.

Failures are contained at the smallest level that still makes sense: a module
that fails on a profile does not stop the next module, a profile that fails is
retried, then abandoned with a reason recorded, and the run moves on. An account
that cannot be logged into loses its profiles but not the rest of the run.
`execution.stop_on_error` turns that off for anyone who would rather have the
run halt. A module that cannot work at all (its screens were never calibrated,
say) is left out of the run and reported, and the others go on without it.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config.settings import AccountConfig, ProfileConfig
from modules.base_module import BaseModule
from modules.registry import MODULES, MODULES_BY_KEY, enabled_modules
from utils.cancel import Cancelled, cancellation
from utils.logger import logger, screenshot_dir

from .calibration import SHARED_REQUIRED_STEPS
from .schedule import SCHEDULE_EVERY_RUN, ModuleSchedule
from .session import SessionError

# (account, profile) -> the modules to run there, in order.
Plan = Dict[Tuple[str, str], List[BaseModule]]


class RunnerError(RuntimeError):
    pass


def final_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    One result per module and profile: its last attempt.

    Every attempt is appended to the results, so a profile retried after a
    failure shows up once failed and once successful; only the last one says how
    it ended. A failure of a whole account (no profile, no module) stays as is.
    """
    last: Dict[tuple, Dict[str, Any]] = {}
    for result in results:
        last[(result.get("module", ""), result.get("account", ""), result.get("profile", ""))] = result
    return list(last.values())


class ModuleRunner:
    """Drives a full run across every enabled account, profile and module."""

    def __init__(self, context, modules: Optional[Sequence[BaseModule]] = None,
                 schedule: Optional[ModuleSchedule] = None):
        self.ctx = context
        self.execution = context.config.section("execution")
        self.timing = context.config.section("timing")
        self.modules: List[BaseModule] = (list(modules) if modules is not None
                                          else [module(context) for module in enabled_modules(context.config)])
        self.schedule = schedule or ModuleSchedule()
        self.results: List[Dict[str, Any]] = []

    # -------------------------------------------------------------- selection
    def _selected_accounts(self, only_account: Optional[str]) -> List[AccountConfig]:
        accounts = self.ctx.config.enabled_accounts()
        if only_account:
            accounts = [a for a in accounts if only_account.lower() in a.name.lower()]
        return accounts

    def _selected_modules(self, only_modules: Optional[Sequence[str]]) -> List[BaseModule]:
        if not only_modules:
            return list(self.modules)
        wanted = [key.strip().lower() for key in only_modules if key and key.strip()]
        unknown = [key for key in wanted if key not in MODULES_BY_KEY]
        if unknown:
            raise RunnerError(f"Unknown module(s): {', '.join(unknown)}. "
                              f"Available: {', '.join(MODULES_BY_KEY)}.")
        selected = [module for module in self.modules if module.key in wanted]
        disabled = [key for key in wanted if key not in {module.key for module in selected}]
        if disabled:
            raise RunnerError(f"Module(s) disabled in the Modules tab: {', '.join(disabled)}.")
        return selected

    @staticmethod
    def _set_up(profile: ProfileConfig) -> bool:
        """
        Whether the profile has anything at all to work with - a database, a clan site.

        Asked of every module there is, enabled or not: a profile whose only
        module is switched off is simply not visited, while one with nothing
        filled in is a mistake worth reporting on every run.
        """
        return any(module.configured_for(profile) for module in MODULES)

    @staticmethod
    def _profiles_of(account: AccountConfig, only_profile: Optional[str]) -> List[ProfileConfig]:
        profiles = account.active_profiles
        if only_profile:
            profiles = [p for p in profiles if only_profile.lower() in p.name.lower()]
        return profiles

    def _profiles_for(self, module: BaseModule, accounts: List[AccountConfig],
                      only_profile: Optional[str] = None) -> List[ProfileConfig]:
        return [profile for account in accounts for profile in self._profiles_of(account, only_profile)
                if module.configured_for(profile)]

    # ------------------------------------------------------------- validation
    def blocking_problems(self, accounts: Optional[List[AccountConfig]] = None) -> List[str]:
        """What no module can run without, reported before the run starts."""
        problems = []
        if accounts is None:
            accounts = self.ctx.config.enabled_accounts()

        if not self.modules:
            problems.append("No module is enabled. Turn one on in the Modules tab.")
        if not accounts:
            problems.append("No enabled accounts with profiles found. Configure in Accounts and profiles.")

        for account in accounts:
            if self.ctx.login.enabled and not (account.login and account.password):
                problems.append(f"Account '{account.name}' has no username or password.")
            if not any(self._set_up(profile) for profile in account.active_profiles):
                problems.append(
                    f"Account '{account.name}' has no profile a module can work on "
                    f"(a database for the chests, a clan site and token for the tournaments)."
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

        missing = self.ctx.calibration.require(*SHARED_REQUIRED_STEPS)
        if missing:
            problems.append(f"Incomplete calibration. Missing steps: {self.ctx.calibration.titles(missing)}.")

        # Works with or without the browser open: with it, against the live page
        # size; without it, against the size the calibration was recorded at.
        outside = self.ctx.calibration.out_of_bounds()
        if outside:
            problems.append(
                f"These calibration steps fall OUTSIDE the page and clicks will hit nothing: "
                f"{self.ctx.calibration.titles(outside)}. Redo them in the wizard, with the window "
                f"at your normal running size."
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
                    "CRITICAL WARNING: RapidOCR is not available on this machine! "
                    "Falling back to Tesseract. Tesseract frequently fails to read "
                    "profile lists and foreign/accented player names. Run install.bat to install RapidOCR."
                )

        return problems

    def module_problems(self, module: BaseModule, accounts: Optional[List[AccountConfig]] = None) -> List[str]:
        """What keeps one module from running. Nothing, when no profile is set up for it."""
        if accounts is None:
            accounts = self.ctx.config.enabled_accounts()
        every = [profile for account in accounts for profile in account.active_profiles]
        problems = list(module.preflight(every))
        if any(module.configured_for(profile) for profile in every):
            missing = self.ctx.calibration.require(*module.required_steps)
            if missing:
                problems.append(f"Incomplete calibration for '{module.title}'. Missing steps: "
                                f"{self.ctx.calibration.titles(missing)}.")
        return problems

    def preflight(self) -> List[str]:
        """Everything that would make the run pointless or partial, for --check and the interface."""
        problems = self.blocking_problems()
        for module in self.modules:
            problems += self.module_problems(module)
        return problems

    # --------------------------------------------------------------- planning
    def _plan(self, modules: List[BaseModule], accounts: List[AccountConfig],
              only_profile: Optional[str], force: bool) -> Plan:
        """Which modules run on which profile: set up for it, and due."""
        plan: Plan = {}
        for account in accounts:
            for profile in self._profiles_of(account, only_profile):
                tasks = []
                for module in modules:
                    if not module.configured_for(profile):
                        continue
                    due, why = (True, "") if force else self.schedule.is_due(
                        module.key, str(module.options.get("schedule", SCHEDULE_EVERY_RUN)),
                        account.name, profile.name, int(module.options.get("daily_attempts", 3)),
                    )
                    if due:
                        tasks.append(module)
                    else:
                        logger.info(f"'{module.title}' on '{profile.label}': {why}; not run now.")
                if tasks:
                    plan[(account.name, profile.name)] = tasks
        return plan

    # -------------------------------------------------------------- execution
    def run(self, only_account: Optional[str] = None, only_profile: Optional[str] = None,
            only_modules: Optional[Sequence[str]] = None, force: bool = False) -> Dict[str, Any]:
        """
        The run, with each module told when it starts and how it ended.

        Whatever ends the run - success, a misconfiguration, a crash, ESC - the
        modules hear of it (the chest module closes its row in the site's
        `job_runs` with it, so the site's health check sees how it went). The
        GUI and main.py both come through here.

        `only_modules` restricts the run to those module keys; `force` ignores
        the schedule, so a once-a-day module runs again.
        """
        started = datetime.now()
        self.results = []
        modules = self._selected_modules(only_modules)
        accounts = self._selected_accounts(only_account)

        # A module that cannot work is left out, and said so; the others go on.
        broken: Dict[str, List[str]] = {}
        for module in modules:
            problems = self.module_problems(module, accounts)
            if problems:
                broken[module.key] = problems
                for problem in problems:
                    logger.error(problem)
        usable = [module for module in modules if module.key not in broken]

        plan = self._plan(usable, accounts, only_profile, force)
        active = [module for module in usable if any(module in tasks for tasks in plan.values())]
        for module in active:
            self._guarded(module, "begin_run", [
                profile for account in accounts for profile in account.active_profiles
                if module in plan.get((account.name, profile.name), [])
            ])

        def end(**how):
            final = final_results(self.results)
            for module in active:
                # Its own results, plus the accounts that could not be opened at all.
                mine = [r for r in final if r.get("module", "") in (module.key, "")]
                self._guarded(module, "end_run", mine, **how)

        try:
            summary = self._run(started, modules, accounts, plan, broken, only_account, only_profile)
        except Cancelled as exc:
            end(cancelled=True, error=str(exc))
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, then passed on untouched
            end(error=str(exc) or exc.__class__.__name__)
            raise
        end(duration=summary["duration"])
        return summary

    @staticmethod
    def _guarded(module: BaseModule, hook: str, *args, **kwargs):
        """A module's bookkeeping must never sink the run."""
        try:
            getattr(module, hook)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"'{module.title}' could not {hook.replace('_', ' ')}: {exc}")

    def _run(self, started: datetime, modules: List[BaseModule], accounts: List[AccountConfig], plan: Plan,
             broken: Dict[str, List[str]], only_account: Optional[str], only_profile: Optional[str]) -> Dict[str, Any]:
        problems = self.blocking_problems(accounts if only_account else None)
        if only_account and not accounts:
            problems.append(f"Account '{only_account}' not found among enabled accounts.")
        if modules and len(broken) == len(modules):
            problems.append("No module can run (see the errors above).")
        if problems:
            for problem in problems:
                logger.error(problem)
            raise RunnerError("Execution cannot start: " + " | ".join(problems))

        for key, reasons in broken.items():
            self.results.append({"success": False, "module": key, "account": "", "profile": "",
                                 "reason": "left out of the run: " + " | ".join(reasons)})

        if not plan:
            logger.info("Nothing is due on this run: every module is either done for today or has no profile set up.")
            return self._summary(started)

        self.ctx.start_browser()

        busy = [account for account in accounts
                if any((account.name, profile.name) in plan for profile in account.active_profiles)]
        for index, account in enumerate(busy, start=1):
            cancellation.check()
            logger.info("=" * 60)
            logger.info(f"ACCOUNT {index}/{len(busy)}: '{account.name}'")
            logger.info("=" * 60)
            self._run_account_with_restart(account, only_profile, plan)

        return self._summary(started)

    def _run_account_with_restart(self, account: AccountConfig, only_profile: Optional[str], plan: Plan):
        """
        Runs the account; if nothing at all could be done, restarts the
        browser and runs it once more.

        An account where every profile fails is almost always a game that froze
        in the browser - the state a person clears with F5. Leaving it there
        failed this run and every later one, since each found the same frozen
        Chrome. A fresh browser is what gets the run going again.
        """
        restart = bool(self.execution.get("restart_browser_on_failure", True))
        attempts = 2 if restart else 1
        for attempt in range(1, attempts + 1):
            last = attempt == attempts
            try:
                if self._run_account(account, only_profile, plan):
                    return
                if last:
                    return       # each profile already recorded its own failure
                reason = "nothing could be done on any profile of the account"
            except Cancelled:
                raise
            except SessionError as exc:
                if last:
                    self._record_failure(account, None, str(exc))
                    if self.execution.get("stop_on_error"):
                        raise
                    return
                reason = str(exc)
            except Exception as exc:  # noqa: BLE001 - one account must not sink the run
                logger.exception(f"Unexpected failure on account '{account.name}': {exc}")
                self._save_error_screenshot(f"account_{account.name}")
                if last:
                    self._record_failure(account, None, f"unexpected error: {exc}")
                    if self.execution.get("stop_on_error"):
                        raise
                    return
                reason = f"unexpected error: {exc}"

            logger.warning(
                f"Account '{account.name}' failed ({reason}). The game may be frozen in the "
                f"browser; closing it and trying the account again in a fresh one."
            )
            try:
                self.ctx.browser.restart()
                self.ctx.sync_viewport()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Could not restart the browser: {exc}")
                self._record_failure(account, None, f"{reason}; browser restart failed: {exc}")
                if self.execution.get("stop_on_error"):
                    raise
                return

    def _run_account(self, account: AccountConfig, only_profile: Optional[str], plan: Plan) -> bool:
        """Runs the account's profiles. False when none of those tried succeeded."""
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

        # Every profile name of the account, so the switcher can tell near-twin
        # names apart by competition instead of by threshold.
        siblings = [p.name for p in account.profiles if p.name]

        tried = succeeded = 0
        for profile in self._profiles_of(account, only_profile):
            if not self._set_up(profile):
                logger.warning(
                    f"Profile '{profile.label}' has no database and nothing else set up, and was skipped "
                    f"(a profile without a database has nowhere to record its chests)."
                )
                self._record_failure(account, profile, "profile has no database and no module set up")
                continue
            tasks = plan.get((account.name, profile.name))
            if not tasks:
                continue
            cancellation.check()
            tried += 1
            if self._run_profile(account, profile, siblings, tasks):
                succeeded += 1
            cancellation.sleep(float(self.timing.get("between_profiles_wait", 3.0)))

        if self.ctx.login.enabled:
            self.ctx.login.end_session()
        return succeeded > 0 or tried == 0

    def _run_profile(self, account: AccountConfig, profile: ProfileConfig, siblings: List[str],
                     tasks: List[BaseModule]) -> bool:
        """Runs the profile's modules, retrying the ones that failed. True when any of them succeeded."""
        attempts = int(self.execution.get("retries_per_profile", 2))
        pending = list(tasks)
        outcome: Dict[str, bool] = {}
        for attempt in range(1, attempts + 1):
            logger.info(f"--- Profile '{profile.label}' (attempt {attempt}/{attempts}) ---")
            try:
                if not self.ctx.profiles.switch_to(profile, siblings):
                    raise SessionError(f"could not activate profile '{profile.name}'")

                for position, module in enumerate(list(pending)):
                    cancellation.check()
                    if position:
                        # Whatever the previous module left open is not this one's problem.
                        self.ctx.game_state.close_modals()
                    if self._run_module(module, account, profile, outcome):
                        pending.remove(module)
            except Cancelled:
                raise
            except SessionError as exc:
                logger.error(f"Profile '{profile.label}': {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Unexpected failure on profile '{profile.label}': {exc}")
                self._save_error_screenshot(f"profile_{profile.name}")

            if not pending:
                break
            if attempt < attempts:
                logger.info("Retrying the profile after resetting the board...")
                self.ctx.game_state.prepare_board()

        for module in pending:
            outcome[module.key] = False
            self._record_failure(account, profile, "all attempts failed", module)
        for module in tasks:
            self.schedule.record(module.key, account.name, profile.name, outcome.get(module.key, False))
        return any(outcome.values())

    def _run_module(self, module: BaseModule, account: AccountConfig, profile: ProfileConfig,
                    outcome: Dict[str, bool]) -> bool:
        """One module on the active profile. True when it is finished with, for better or worse."""
        logger.info(f">> {module.title}: '{profile.label}'")
        try:
            result = module.run_for_profile(profile)
        except Cancelled:
            raise
        except SessionError as exc:
            logger.error(f"{module.title} on '{profile.label}': {exc}")
            return False
        except Exception as exc:  # noqa: BLE001 - one module must not take the others with it
            logger.exception(f"Unexpected failure of '{module.title}' on '{profile.label}': {exc}")
            self._save_error_screenshot(f"{module.key}_{profile.name}")
            return False

        result.setdefault("account", account.name)
        result.setdefault("profile", profile.name)
        result["module"] = module.key
        self.results.append(result)
        if result.get("success"):
            outcome[module.key] = True
            return True
        logger.warning(f"{module.title} on '{profile.label}' reported: {result.get('reason', 'failure')}")
        outcome[module.key] = False
        return not result.get("retry", True)

    # ---------------------------------------------------------------- results
    def _record_failure(self, account: AccountConfig, profile: Optional[ProfileConfig], reason: str,
                        module: Optional[BaseModule] = None):
        where = account.name + (" / " + profile.name if profile else "")
        logger.error(f"FAILED: {where}{' / ' + module.title if module else ''} - {reason}")

        self.results.append({
            "success": False,
            "module": module.key if module else "",
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
        # By each module's last attempt on each profile: one that failed once and
        # then succeeded on the retry is not a failure.
        final = final_results(self.results)
        failures = [r for r in final if not r.get("success")]
        duration = datetime.now() - started

        logger.info("=" * 60)
        logger.info(f"RUN FINISHED in {duration}: {collected} chests collected, {incomplete} incomplete.")
        for module in self.modules:
            mine = [r for r in final if r.get("module") == module.key]
            if mine:
                done = sum(1 for r in mine if r.get("success"))
                logger.info(f"  {module.title}: {done} of {len(mine)} profile(s) done.")
        for failure in failures:
            title = MODULES_BY_KEY[failure["module"]].title if failure.get("module") in MODULES_BY_KEY else ""
            where = "/".join(part for part in (failure.get("account"), failure.get("profile"), title) if part)
            logger.warning(f"  not done: {where or 'run'} - {failure.get('reason')}")
        logger.info("=" * 60)

        return {
            "collected": collected,
            "incomplete": incomplete,
            "profiles_done": len({(r.get("account"), r.get("profile")) for r in final if r.get("success")}),
            "failures": failures,
            "duration": str(duration),
            "results": self.results,
        }
