"""
Logging configuration for Total Battle Collector.
Supports structured logging to both file (in execution_logs/) and console.
"""

import os
import sys
import logging
from datetime import datetime

def setup_logger(name: str = "TBCollector", log_dir: str = "execution_logs", level: int = logging.INFO) -> logging.Logger:
    """Configures and returns a structured logger."""
    os.makedirs(log_dir, exist_ok=True)
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Avoid duplicate handlers if setup_logger is called multiple times
    if logger.handlers:
        return logger

    # Log format
    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)-7s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler (UTF-8 safe)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler with current date
    date_str = datetime.now().strftime("%Y-%m-%d")
    log_file_path = os.path.join(log_dir, f"collector_{date_str}.log")
    file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

# Default application logger instance
logger = setup_logger()
