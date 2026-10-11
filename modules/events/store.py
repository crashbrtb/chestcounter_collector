"""What the events module remembers between runs, under config/module_data/events/.

- the players' names (one file for every profile: a player id is the same in
  every clan, and the game only sends a profile it has not sent since the page
  was loaded);
- what was already sent, one file per clan site: two profiles of the same clan
  write to the same site and must not send the same tournament twice;
- when the Journal was last searched to the end, also per clan site, so the
  next search only walks the days since then.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from typing import Dict, Optional, Tuple

from config.settings import MODULE_DATA_DIR

from .api import normalize_site_url

DATA_DIR = os.path.join(MODULE_DATA_DIR, "events")


def profile_cache_path(data_dir: str = DATA_DIR) -> str:
    return os.path.join(data_dir, "player_profiles.json")


def site_slug(site_url: str) -> str:
    """A file name for a site: 'https://kokmain.counter.li/x' -> 'kokmain.counter.li_x'."""
    bare = normalize_site_url(site_url).split("://", 1)[-1].lower()
    return re.sub(r"[^a-z0-9.-]+", "_", bare).strip("_") or "site"


class SentHistory:
    """The tournaments already sent to one site: result uid -> what the site answered."""

    def __init__(self, site_url: str, data_dir: str = DATA_DIR):
        self.path = os.path.join(data_dir, f"sent_{site_slug(site_url)}.json")
        self.sent: Dict[str, dict] = {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            self.sent = {str(uid): info for uid, info in data.items() if isinstance(info, dict)}

    def info(self, result_uid: str) -> Optional[dict]:
        return self.sent.get(result_uid)

    def mark(self, result_uid: str, info: dict) -> None:
        self.sent[result_uid] = info
        self.save()

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(self.sent, fh, ensure_ascii=False, indent=1)
        os.replace(temporary, self.path)


class SearchMemory:
    """
    When one site's Journal was last searched to the end, and how many days back.

    The Journal says how old a card is in days ("yesterday", "7 days ago"), so
    this is what turns "since the last search" into a number of days to walk.
    """

    # Days walked again before the last search: a card's "yesterday" turns at
    # the game's midnight, not this computer's.
    OVERLAP_DAYS = 1

    def __init__(self, site_url: str, data_dir: str = DATA_DIR):
        self.path = os.path.join(data_dir, f"search_{site_slug(site_url)}.json")
        self.data: dict = {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            self.data = data

    def last_search(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(str(self.data.get("searched_at") or ""))
        except ValueError:
            return None

    def days_to_walk(self, days_back: int, today: date) -> Tuple[int, bool]:
        """
        (how many days back to walk now, whether the earlier searches are set aside).

        Never more than `days_back`. After a complete search, only the days
        since it; unless `days_back` was raised since then, when the days it
        now reaches were never looked at and the whole of it is walked once.
        """
        last = self.last_search()
        if last is None:
            return days_back, False
        try:
            covered = int(self.data.get("days_back"))
        except (TypeError, ValueError):
            covered = days_back
        if days_back > covered:
            return days_back, True
        since = max(0, (today - last.date()).days)
        return min(days_back, since + self.OVERLAP_DAYS), False

    def record(self, when: datetime, days_back: int) -> None:
        """Notes a complete search."""
        self.data = {"searched_at": when.isoformat(timespec="seconds"), "days_back": int(days_back)}
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        os.replace(temporary, self.path)
