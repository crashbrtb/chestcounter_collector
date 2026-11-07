"""
The run itself: accounts in order, inside each one its profiles, and on each
profile the modules that are due.

This is the order the whole redesign was for. An account is opened in its own
Chrome window, its profiles are visited one after another, and accounts work at
the same time, up to `execution.parallel_accounts`: each has its own window and its
own services (RunContext.for_account), and nothing here assumes a session left
behind by a previous run. The profiles of one account can never overlap: they share
a login, and the game drops the older session of a profile that opens twice.

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
run halt. A module that cannot work at all (its preflight says why) is left
out of the run and reported, and the others go on without it.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config.settings import RUN_LOCK_PATH, AccountConfig, ProfileConfig
from modules.base_module import BaseModule
from modules.registry import MODULES, MODULES_BY_KEY, enabled_modules
from utils.cancel import Cancelled, cancellation
from utils.logger import log_as, logger, screenshot_dir
from utils.run_lock import RunLock

from .browser import WrongWindow
from .schedule import SCHEDULE_EVERY_RUN, ModuleSchedule
from .session import NeedsPerson, SessionError

# (account, profile) -> the modules to run there, in order.
Plan = Dict[Tuple[str, str], List[BaseModule]]


class RunnerError(RuntimeError):
    pass


class RunAlreadyActive(RunnerError):
    """Another run (the scheduler's or the interface's) is driving the browser right now."""


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
                 schedule: Optional[ModuleSchedule] = None, lock_path: str = RUN_LOCK_PATH):
        self.ctx = context
        self.lock = RunLock(lock_path)
        self.execution = context.config.section("execution")
        self.timing = context.config.section("timing")
        self.modules: List[BaseModule] = (list(modules) if modules is not None
                                          else [module(context) for module in enabled_modules(context.config)])
        self.schedule = schedule or ModuleSchedule()
        self.results: List[Dict[str, Any]] = []
        # 'Stop on first error' reaching the accounts working on the other threads.
        self._halt = threading.Event()

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

        # Asked of everything enabled, not only of what this run was restricted to:
        # either of these is a mistake in the configuration, and the data is what it costs.
        problems += self.ctx.config.window_problems()
        problems += self.ctx.config.database_problems()

        return problems

    def module_problems(self, module: BaseModule, accounts: Optional[List[AccountConfig]] = None) -> List[str]:
        """What keeps one module from running. Nothing, when no profile is set up for it."""
        if accounts is None:
            accounts = self.ctx.config.enabled_accounts()
        every = [profile for account in accounts for profile in account.active_profiles]
        return list(module.preflight(every))

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
                        str(module.options.get("day_start") or module.default_day_start),
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

        Only one run at a time drives the browser (utils/run_lock.py): a run
        that finds another one going raises RunAlreadyActive before touching
        anything.
        """
        if not self.lock.acquire():
            holder = self.lock.holder()
            raise RunAlreadyActive(
                "Another run is already in progress" + (f" (process {holder})" if holder else "")
                + "; this one was not started, so the two do not fight over the browser."
            )
        try:
            return self._locked_run(only_account, only_profile, only_modules, force)
        finally:
            self.lock.release()

    def _locked_run(self, only_account: Optional[str], only_profile: Optional[str],
                    only_modules: Optional[Sequence[str]], force: bool) -> Dict[str, Any]:
        started = datetime.now()
        self.results = []
        self._halt.clear()
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

        busy = [account for account in accounts
                if any((account.name, profile.name) in plan for profile in account.active_profiles)]
        windows = [(account, self.ctx.for_account(account)) for account in busy]
        workers = min(int(self.execution.get("parallel_accounts", 4)), len(windows))
        if workers > 1:
            self._run_at_once(windows, workers, only_profile, plan)
        else:
            for index, (account, context) in enumerate(windows, start=1):
                cancellation.check()
                logger.info("=" * 60)
                logger.info(f"ACCOUNT {index}/{len(windows)}: '{account.name}'")
                logger.info("=" * 60)
                self._run_account_with_restart(account, context, only_profile, plan)

        return self._summary(started)

    def _run_at_once(self, windows: List[Tuple[AccountConfig, Any]], workers: int,
                     only_profile: Optional[str], plan: Plan):
        """The accounts on threads of their own. Raises, once they have all stopped, what would have ended the run."""
        ended: List[Exception] = []

        def work(account: AccountConfig, context):
            if self._halt.is_set():
                return
            with log_as(account.name):
                try:
                    self._run_account_with_restart(account, context, only_profile, plan)
                except Exception as exc:  # noqa: BLE001 - handed to the run's thread below
                    ended.append(exc)
                    self._halt.set()

        logger.info(f"{len(windows)} account(s), {workers} at a time: "
                    + ", ".join(f"{account.name} (port {context.browser.port})" for account, context in windows))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="account") as pool:
            for account, context in windows:
                pool.submit(work, account, context)

        for failure in ended:
            if isinstance(failure, Cancelled):
                raise failure
        if ended:
            raise ended[0]

    def _run_account_with_restart(self, account: AccountConfig, context, only_profile: Optional[str], plan: Plan):
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
                if self._run_account(account, context, only_profile, plan):
                    return
                if last:
                    return       # each profile already recorded its own failure
                reason = "nothing could be done on any profile of the account"
            except Cancelled:
                raise
            except (NeedsPerson, WrongWindow) as exc:
                # Another attempt cannot help, and a restart could close somebody else's window.
                self._record_failure(account, None, str(exc))
                if self.execution.get("stop_on_error"):
                    raise
                return
            except SessionError as exc:
                if last:
                    self._record_failure(account, None, str(exc))
                    if self.execution.get("stop_on_error"):
                        raise
                    return
                reason = str(exc)
            except Exception as exc:  # noqa: BLE001 - one account must not sink the run
                logger.exception(f"Unexpected failure on account '{account.name}': {exc}")
                self._save_error_screenshot(context, f"account_{account.name}")
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
                context.browser.restart()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Could not restart the browser: {exc}")
                self._record_failure(account, None, f"{reason}; browser restart failed: {exc}")
                if self.execution.get("stop_on_error"):
                    raise
                return

    def _run_account(self, account: AccountConfig, context, only_profile: Optional[str], plan: Plan) -> bool:
        """Runs the account's profiles in its window. False when none of those tried succeeded."""
        cancellation.check()
        context.start_browser()

        if context.login.enabled:
            if not context.login.enter_account(account):
                raise SessionError(f"Could not log into account '{account.name}'.")
        else:
            context.browser.navigate()

        context.browser.wait_for_load()
        if context.login.enabled:
            context.login.remember_session()

        # Every profile name of the account, to recognise the one the game opened on.
        siblings = [p.name for p in account.profiles if p.name]

        tried = succeeded = 0
        for profile in self._active_first(context, account, self._profiles_of(account, only_profile), siblings, plan):
            if self._halt.is_set():
                break
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
            if self._run_profile(context, account, profile, tasks):
                succeeded += 1
            cancellation.sleep(float(self.timing.get("between_profiles_wait", 3.0)))

        return succeeded > 0 or tried == 0

    def _active_first(self, context, account: AccountConfig, profiles: List[ProfileConfig], siblings: List[str],
                      plan: Plan) -> List[ProfileConfig]:
        """
        The profiles in the order to visit them: the one the game opened on first.

        The game always comes up on some profile - the one used last. Followed
        in the configured order, a run that opened on the second profile switched
        to the first, then back to the second: two switches
        and two loading waits to end up where it started. So the game is asked
        once, and if it names a profile with
        something due, that one is done before any switching. The rest keep
        their order.

        The game says which profile it is on. When it cannot be asked, the
        configured order stands - which is what the run always did.
        """
        try:
            active = context.profiles.active_among(siblings)
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - only the order depends on it
            logger.warning(f"Could not learn which profile the game opened on: {exc}")
            return profiles

        first = [p for p in profiles if p.name == active and plan.get((account.name, p.name))]
        if not first:
            if active:
                logger.info(f"The game opened on profile '{active}', which has nothing due on this run.")
            else:
                logger.info("The profile the game opened on is not one of this account's; "
                            "following the configured order.")
            return profiles
        if profiles[0] is first[0]:
            return profiles
        logger.info(f"The game opened on profile '{active}': starting with it, "
                    f"then the others in their order.")
        return first[:1] + [p for p in profiles if p is not first[0]]

    def _run_profile(self, context, account: AccountConfig, profile: ProfileConfig, tasks: List[BaseModule]) -> bool:
        """Runs the profile's modules, retrying the ones that failed. True when any of them succeeded."""
        attempts = int(self.execution.get("retries_per_profile", 2))
        pending = list(tasks)
        outcome: Dict[str, bool] = {}
        for attempt in range(1, attempts + 1):
            logger.info(f"--- Profile '{profile.label}' (attempt {attempt}/{attempts}) ---")
            try:
                if not context.profiles.switch_to(profile):
                    raise SessionError(f"could not activate profile '{profile.name}'")

                for module in list(pending):
                    cancellation.check()
                    if self._run_module(context, module, account, profile, outcome):
                        pending.remove(module)
            except Cancelled:
                raise
            except SessionError as exc:
                logger.error(f"Profile '{profile.label}': {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Unexpected failure on profile '{profile.label}': {exc}")
                self._save_error_screenshot(context, f"profile_{profile.name}")

            if not pending:
                break
            if attempt < attempts:
                logger.info("Retrying the profile...")

        for module in pending:
            outcome[module.key] = False
            self._record_failure(account, profile, "all attempts failed", module)
        for module in tasks:
            self.schedule.record(module.key, account.name, profile.name, outcome.get(module.key, False),
                                 str(module.options.get("day_start") or module.default_day_start))
        return any(outcome.values())

    def _run_module(self, context, module: BaseModule, account: AccountConfig, profile: ProfileConfig,
                    outcome: Dict[str, bool]) -> bool:
        """One module on the active profile. True when it is finished with, for better or worse."""
        logger.info(f">> {module.title}: '{profile.label}'")
        try:
            result = module.on_window(context).run_for_profile(profile)
        except Cancelled:
            raise
        except SessionError as exc:
            logger.error(f"{module.title} on '{profile.label}': {exc}")
            return False
        except Exception as exc:  # noqa: BLE001 - one module must not take the others with it
            logger.exception(f"Unexpected failure of '{module.title}' on '{profile.label}': {exc}")
            self._save_error_screenshot(context, f"{module.key}_{profile.name}")
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

    def _save_error_screenshot(self, context, label: str) -> Optional[str]:
        if not self.execution.get("screenshot_on_error", True):
            return None
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:60]
        path = f"{screenshot_dir()}/{datetime.now():%Y%m%d_%H%M%S}_{safe}.png"
        if context.browser.save_screenshot(path):
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
