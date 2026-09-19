"""
Logging for the collector.

The application is normally started by the Windows Task Scheduler, with nobody
watching: the log file is the only record of what happened. So the whole log is
written from Python - one dated file per day, plus the console - instead of
redirecting the output of run.bat. That way the encoding is UTF-8 (player names
have accents), old files are cleaned up on their own, and a crash inside the
process still lands in the same file as the rest of the run.
"""

import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional

LOG_FORMAT = "[%(asctime)s] [%(levelname)-7s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "execution_logs")

logger = logging.getLogger("TBCollector")
logger.setLevel(logging.INFO)
_configured_dir: Optional[str] = None


def _console_handler(level: int) -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    return handler


def _file_handler(log_dir: str, level: int) -> Optional[logging.Handler]:
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, f"collector_{datetime.now():%Y-%m-%d}.log")
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as exc:
        print(f"[WARN] Could not open log file in {log_dir}: {exc}")
        return None
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    return handler


def purge_old_logs(log_dir: str, retention_days: int):
    """Deletes *.log files older than the retention window (0 = keep forever).

    Purges both collector_YYYY-MM-DD.log and startup.log, which run.bat feeds
    with early stderr failures before the logger initializes.
    """
    if retention_days <= 0 or not os.path.isdir(log_dir):
        return
    cutoff = time.time() - retention_days * 86400
    for name in os.listdir(log_dir):
        if not name.endswith(".log"):
            continue
        path = os.path.join(log_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def configure(log_dir: str = DEFAULT_LOG_DIR, level: str = "INFO", retention_days: int = 7) -> logging.Logger:
    """(Re)points the shared logger at a directory and level. Safe to call twice."""
    global _configured_dir

    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    logger.setLevel(numeric_level)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    logger.addHandler(_console_handler(numeric_level))
    file_handler = _file_handler(log_dir, numeric_level)
    if file_handler:
        logger.addHandler(file_handler)

    purge_old_logs(log_dir, retention_days)
    _configured_dir = log_dir
    return logger


def screenshot_dir() -> str:
    """Where diagnostic screenshots go, next to the logs of the same run."""
    base = _configured_dir or DEFAULT_LOG_DIR
    path = os.path.join(base, "screenshots")
    os.makedirs(path, exist_ok=True)
    return path


# Console-only default, so importing a module before main() runs never loses a message.
configure(DEFAULT_LOG_DIR, "INFO", 0)
