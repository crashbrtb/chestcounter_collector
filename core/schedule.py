"""
When each module last ran for each profile.

Most runs are started by the Windows Task Scheduler, several times a day. A
module that has to run on every one of them needs nothing from here; one that
runs once a day needs to know whether today's is done - and that has to survive
the process, so it is a small file (config/module_state.json).

'Done' means it succeeded. A run that failed is not today's run: the next start
tries again, up to the attempts the module allows per day, so one bad run does
not cost the whole day and a module that cannot work does not cost every run.
"""

import json
import os
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

from config.settings import MODULE_STATE_PATH

SCHEDULE_EVERY_RUN = "every run"
SCHEDULE_DAILY = "once a day"
SCHEDULES = (SCHEDULE_EVERY_RUN, SCHEDULE_DAILY)


class ModuleSchedule:
    def __init__(self, path: str = MODULE_STATE_PATH, clock: Callable[[], datetime] = datetime.now):
        self.path = path
        self.clock = clock
        self.state: Dict[str, Dict[str, dict]] = {}
        self.load()

    # ------------------------------------------------------------ persistence
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.state = {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2, ensure_ascii=False)
        os.replace(temporary, self.path)

    # ------------------------------------------------------------------ state
    @staticmethod
    def _who(account: str, profile: str) -> str:
        return f"{account}/{profile}"

    def entry(self, module_key: str, account: str, profile: str) -> dict:
        return dict(self.state.get(module_key, {}).get(self._who(account, profile)) or {})

    def last_success(self, module_key: str, account: str, profile: str) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.entry(module_key, account, profile).get("last_success") or "")
        except ValueError:
            return None

    def is_due(self, module_key: str, schedule: str, account: str, profile: str,
               daily_attempts: int = 3) -> Tuple[bool, str]:
        """(run it now?, why not)."""
        if schedule != SCHEDULE_DAILY:
            return True, ""

        today = self.clock().date()
        done = self.last_success(module_key, account, profile)
        if done is not None and done.date() == today:
            return False, f"already done today at {done:%H:%M}"

        entry = self.entry(module_key, account, profile)
        if entry.get("attempts_day") == today.isoformat() and int(entry.get("attempts", 0)) >= daily_attempts:
            return False, f"failed {entry['attempts']} time(s) today; next try tomorrow"
        return True, ""

    def record(self, module_key: str, account: str, profile: str, success: bool):
        """Notes one run of a module on a profile, and writes it down at once."""
        now = self.clock()
        today = now.date().isoformat()
        entry = self.entry(module_key, account, profile)
        entry["attempts"] = int(entry.get("attempts", 0)) + 1 if entry.get("attempts_day") == today else 1
        entry["attempts_day"] = today
        entry["last_attempt"] = now.isoformat(timespec="seconds")
        if success:
            entry["last_success"] = entry["last_attempt"]
        self.state.setdefault(module_key, {})[self._who(account, profile)] = entry
        self.save()
