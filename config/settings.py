"""
Global settings, default paths, and timeouts for Total Battle automation.
"""

import os

# Project Root Directory
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Asset Directories
IMAGES_DIR = os.path.join(BASE_DIR, "images")
EXECUTION_LOGS_DIR = os.path.join(BASE_DIR, "execution_logs")
CONFIG_FILE_PATH = os.path.join(BASE_DIR, "position.cfg")

# Default Timeouts & Delays (in seconds)
DEFAULT_CLICK_DELAY = 1.0
DEFAULT_ACTION_DELAY = 0.5
DEFAULT_ACCOUNT_SWITCH_WAIT = 20.0
DEFAULT_LAUNCHER_WAIT = 20.0
DEFAULT_GAME_START_WAIT = 15.0

# Image Matching Thresholds
DEFAULT_MATCH_CONFIDENCE = 0.7
DEFAULT_MAX_ATTEMPTS = 5

