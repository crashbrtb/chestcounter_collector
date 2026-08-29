"""
Total Battle Automation Orchestrator - Main Entry Point.
Coordinates window management, account switching, chest collection, and future modules.
"""

import sys
import argparse
from datetime import datetime
from config.config_loader import ConfigLoader
from core.window_manager import WindowManager
from core.vision import Vision
from core.ocr_engine import OCREngine
from core.bot_controller import BotController
from core.game_state import GameStateHelper
from modules.account_manager import AccountManager
from modules.chest_collector import ChestCollector
from modules.journal_parser import JournalParser
from modules.chat_automator import ChatAutomator
from utils.logger import logger

# Ensure UTF-8 output
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def parse_arguments():
    parser = argparse.ArgumentParser(description="Total Battle Multi-Account Bot & Collector")
    parser.add_argument(
        "--module",
        choices=["chests", "journal", "chat", "all"],
        default="chests",
        help="Feature module to run (default: chests)",
    )
    parser.add_argument(
        "--account",
        type=str,
        default=None,
        help="Run only for a specific account name instead of all configured accounts",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Do not close the game window after completion",
    )
    return parser.parse_args()


def run_chest_workflow(
    app_config,
    window_mgr,
    game_state,
    account_mgr,
    chest_collector,
    target_account_name=None,
    keep_open=False,
):
    coords = app_config.coordinates
    accounts = app_config.accounts

    if not accounts:
        logger.error("No accounts found in position.cfg. Exiting.")
        return False

    # Filter accounts if a single account was requested
    if target_account_name:
        accounts = [acc for acc in accounts if target_account_name.lower() in acc.account.lower()]
        if not accounts:
            logger.error(f"Requested account '{target_account_name}' not found in configuration.")
            return False

    # 1. Ensure Total Battle is open and store is closed
    if not window_mgr.ensure_game_open(coords.coords_play_button):
        logger.error("Failed to start or locate Total Battle window.")
        return False

    if coords.screen_area:
        game_state.wait_and_close_store(coords.screen_area, max_wait_seconds=15.0)

    # 2. Iterate through accounts
    total_collected = 0
    total_errors = 0

    for idx, acc in enumerate(accounts, start=1):
        logger.info(f"=== Starting Account #{idx} [{acc.section}]: '{acc.account}' ===")

        # Switch to account
        switched = account_mgr.select_account(acc.account)
        if not switched:
            logger.error(f"Could not switch to account '{acc.account}'. Skipping to next.")
            continue

        # Double check if active
        if coords.account_name_display_area:
            if not account_mgr.is_account_active(acc.account):
                logger.warning(f"Active account does not match '{acc.account}'. Skipping collection.")
                continue

        # Collect gifts
        result = chest_collector.collect_for_account(acc)
        total_collected += result.get("collected", 0)
        total_errors += result.get("incomplete", 0)
        logger.info(f"Completed collection for '{acc.account}'.")

    logger.info("=" * 50)
    logger.info(f"ALL ACCOUNTS FINISHED: {total_collected} collected, {total_errors} incomplete/errors.")
    logger.info("=" * 50)

    # 3. Close game unless --keep-open is specified
    if not keep_open:
        window_mgr.close_window(coords.window_title)
        logger.info("Game closed.")

    return True


def main():
    args = parse_arguments()
    start_time = datetime.now()
    logger.info(f"Starting Total Battle Collector at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        # Load Configuration
        config_loader = ConfigLoader()
        app_config = config_loader.load()

        # Initialize Core Services
        window_mgr = WindowManager(
            window_title=app_config.coordinates.window_title,
            launcher_title=app_config.coordinates.launcher_title,
            launcher_path=app_config.coordinates.path_total_battle,
        )
        vision = Vision()
        ocr = OCREngine(vision)
        controller = BotController()
        game_state = GameStateHelper(vision, controller)

        # Initialize Modules
        account_mgr = AccountManager(app_config, window_mgr, vision, ocr, controller, game_state)
        chest_collector = ChestCollector(app_config, window_mgr, vision, ocr, controller, game_state)
        journal_parser = JournalParser(app_config, window_mgr, vision, ocr, controller, game_state)
        chat_automator = ChatAutomator(app_config, window_mgr, vision, ocr, controller, game_state)

        # Dispatch module
        if args.module in ("chests", "all"):
            run_chest_workflow(
                app_config,
                window_mgr,
                game_state,
                account_mgr,
                chest_collector,
                target_account_name=args.account,
                keep_open=args.keep_open,
            )

        if args.module == "journal":
            logger.info("Running Journal module...")
            journal_parser.run()

        if args.module == "chat":
            logger.info("Running Chat module...")
            chat_automator.run()

    except Exception as e:
        logger.exception(f"Unhandled exception in bot execution: {e}")
        sys.exit(1)
    finally:
        end_time = datetime.now()
        duration = end_time - start_time
        logger.info(f"Execution finished at {end_time.strftime('%Y-%m-%d %H:%M:%S')} (Duration: {duration})")


if __name__ == "__main__":
    main()
