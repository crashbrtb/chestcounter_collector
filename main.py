"""
Total Battle Chest Collector - entry point.

Started with no arguments it does what the Windows Task Scheduler needs: read
config/config.json, run the collection, write the log, and exit with a status
code the scheduler can act on (0 done, 1 failed, 2 misconfigured).

    main.py                 collect, using config/config.json
    main.py --gui           open the configuration and calibration interface
    main.py --calibrate     open the calibration wizard directly
    main.py --check         validate configuration and calibration, collect nothing
    main.py --account NAME  restrict the run to one account
    main.py --profile NAME  restrict the run to one profile
"""

import argparse
import sys

from config.settings import ConfigManager, migrate_legacy_config
from utils.cancel import Cancelled, cancellation, escape_watcher
from utils.logger import configure, logger

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_MISCONFIGURED = 2
EXIT_CANCELLED = 3

# Player names carry accents; a cp1252 console would kill the process on print.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


def parse_arguments():
    parser = argparse.ArgumentParser(description="Total Battle Chest Collector")
    parser.add_argument("--gui", action="store_true", help="open the configuration interface")
    parser.add_argument("--calibrate", action="store_true", help="open the calibration wizard directly")
    parser.add_argument("--check", action="store_true", help="validate configuration and calibration only")
    parser.add_argument("--account", help="run only this account")
    parser.add_argument("--profile", help="run only this profile")
    parser.add_argument("--keep-open", action="store_true", help="do not close the browser when finished")
    return parser.parse_args()


def load_configuration() -> ConfigManager:
    """
    Loads config.json, creating it on first run.

    A first run on a machine that had the old collector inherits its database
    credentials from position.cfg - those were typed in by hand once and there is
    no reason to make anyone type them again. The old coordinates are not
    imported: they were positions on the desktop client's screen and mean nothing
    in the browser.
    """
    config = ConfigManager()
    if config.exists:
        return config

    logger.info("config/config.json not found; creating it with the default values.")
    imported = migrate_legacy_config()
    if imported:
        config.accounts = imported
        profiles = sum(len(a.profiles) for a in imported)
        logger.info(
            f"Imported {profiles} profile(s) from position.cfg. Open the interface to fill in "
            f"the account login and password, then run the calibration."
        )
    config.save()
    return config


def main() -> int:
    args = parse_arguments()

    try:
        config = load_configuration()
    except ValueError as exc:
        logger.error(str(exc))
        return EXIT_MISCONFIGURED

    configure(
        log_dir=config.resolved_log_dir(),
        level=config.get("execution", "log_level", "INFO"),
        retention_days=int(config.get("execution", "log_retention_days", 7)),
    )

    if args.keep_open:
        config.set("execution", "close_browser_on_finish", False)

    if args.gui or args.calibrate:
        try:
            from gui.app import launch
        except ImportError as exc:
            logger.error(f"Interface unavailable ({exc}). Run install.bat to install customtkinter.")
            return EXIT_MISCONFIGURED
        launch(config, open_calibration=args.calibrate)
        return EXIT_OK

    from core.context import build_context
    from core.runner import CollectorRunner, RunnerError

    context = build_context(config)
    runner = CollectorRunner(context)

    if args.check:
        problems = runner.preflight()
        for problem in problems:
            logger.error(problem)
        if problems:
            return EXIT_MISCONFIGURED
        logger.info("Configuration and calibration are complete.")
        return EXIT_OK

    module = config.get("execution", "module", "chests")
    logger.info(f"Starting Total Battle Chest Collector (module: {module})")

    summary = None
    cancellation.reset()
    escape_watcher.start()
    logger.info("Hold ESC for a moment to cancel execution.")
    try:
        if module in ("chests", "all"):
            summary = runner.run(only_account=args.account, only_profile=args.profile)

        # The journal and chat modules are still placeholders waiting on
        # calibration steps of their own; they say so rather than acting blind.
        if module in ("journal", "all"):
            from modules.journal_parser import JournalParser

            context.start_browser()
            logger.info(JournalParser(context).run().get("reason", "Journal module executed"))

        if module in ("chat", "all"):
            from modules.chat_automator import ChatAutomator

            context.start_browser()
            logger.info(ChatAutomator(context).run().get("reason", "Chat module executed"))
    except Cancelled as exc:
        logger.warning(f"Execution cancelled ({exc}). Already collected chests remain saved.")
        return EXIT_CANCELLED
    except RunnerError as exc:
        logger.error(str(exc))
        return EXIT_MISCONFIGURED
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Unhandled failure: {exc}")
        return EXIT_FAILED
    finally:
        escape_watcher.stop()
        try:
            context.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not close the browser cleanly: {exc}")

    if summary is None:
        return EXIT_OK
    return EXIT_FAILED if summary["failures"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
