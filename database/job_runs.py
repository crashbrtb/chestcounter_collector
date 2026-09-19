"""
Heartbeat of the collection, in the site's `job_runs` table.

The site's health check (`/api/v1/health`) raises the alarm when the collector
has not had a good run for too long. Watching the newest chest could not tell a
quiet day from a dead collector; a row per run can: a run that found no chests
is still a run.

A row is written as `running` when the run starts and closed when it ends, in
every database the run writes chests to - each profile may belong to a
different clan site, and each site watches its own collector. The row carries
only the profiles of that database.

Nothing here may break a collection. Every write is wrapped, and a database
that is too old to have the table is skipped quietly. A database that cannot be
reached gets no heartbeat, which is exactly what the monitor should see.

Times come from the database (`UTC_TIMESTAMP()`), not from this computer: the
site compares them with its own clock, and this PC's clock and timezone are not
the site's.
"""

import json
import socket
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from mysql.connector import Error

from config.settings import AccountConfig, DatabaseConfig
from utils.logger import logger

from .db_connection import open_connection

JOB = "collector"

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

ER_NO_SUCH_TABLE = 1146


class _Target:
    """One database the run writes to, and the profiles that write there."""

    def __init__(self, database: DatabaseConfig):
        self.database = database
        self.profiles: List[Tuple[str, str]] = []
        self.run_id: Optional[int] = None
        self.unsupported = False

    @property
    def accounts(self) -> set:
        return {account for account, _ in self.profiles}


class JobRunReporter:
    def __init__(self, accounts: Iterable[AccountConfig], only_profile: Optional[str] = None):
        self.targets: Dict[Tuple[str, int, str], _Target] = {}
        for account in accounts:
            for profile in account.collectable_profiles:
                if only_profile and only_profile.lower() not in profile.name.lower():
                    continue
                db = profile.database
                key = (db.host.lower(), int(db.port), db.database.lower())
                target = self.targets.setdefault(key, _Target(db))
                target.profiles.append((account.name, profile.name))
        self.host = (socket.gethostname() or "unknown")[:100]
        self._started = time.monotonic()

    # ---------------------------------------------------------------- public
    def start(self):
        self._started = time.monotonic()
        for target in self.targets.values():
            target.run_id = self._execute(
                target,
                "INSERT INTO job_runs (job, status, host, started_at, created) "
                "VALUES (%s, %s, %s, UTC_TIMESTAMP(), UTC_TIMESTAMP())",
                (JOB, STATUS_RUNNING, self.host),
                insert=True,
            )

    def finish(self, final_results: List[Dict[str, Any]], duration: str = "",
               error: Optional[str] = None, cancelled: bool = False):
        """
        Closes the run in every database.

        `final_results` holds one entry per profile (its last attempt), plus one
        per account that could not be opened at all. `error` is a failure that
        stopped the whole run; `cancelled` a run stopped by hand.
        """
        for target in self.targets.values():
            if target.unsupported:
                continue
            status, summary = self._outcome(target, final_results, error, cancelled)
            summary["duration"] = duration or self._elapsed()
            payload = json.dumps(summary, ensure_ascii=False)

            if target.run_id is not None:
                self._execute(
                    target,
                    "UPDATE job_runs SET status = %s, finished_at = UTC_TIMESTAMP(), summary = %s WHERE id = %s",
                    (status, payload, target.run_id),
                )
            else:
                # The start could not be written (the database was down then):
                # the run still happened, so it is recorded whole.
                seconds = int(time.monotonic() - self._started)
                self._execute(
                    target,
                    "INSERT INTO job_runs (job, status, host, started_at, finished_at, summary, created) "
                    "VALUES (%s, %s, %s, UTC_TIMESTAMP() - INTERVAL %s SECOND, UTC_TIMESTAMP(), %s, UTC_TIMESTAMP())",
                    (JOB, status, self.host, seconds, payload),
                )

    # --------------------------------------------------------------- helpers
    def _outcome(self, target: _Target, final_results: List[Dict[str, Any]],
                 error: Optional[str], cancelled: bool) -> Tuple[str, Dict[str, Any]]:
        mine = set(target.profiles)
        accounts = target.accounts
        results = [
            r for r in final_results
            if (r.get("account"), r.get("profile")) in mine
            or (not r.get("profile") and r.get("account") in accounts)
        ]
        done = [r for r in results if r.get("success") and (r.get("account"), r.get("profile")) in mine]
        failures = [
            {"account": r.get("account", ""), "profile": r.get("profile", ""), "reason": r.get("reason", "failure")}
            for r in results if not r.get("success")
        ]

        summary: Dict[str, Any] = {
            "collected": sum(int(r.get("collected", 0) or 0) for r in results),
            "incomplete": sum(int(r.get("incomplete", 0) or 0) for r in results),
            "profiles_done": len(done),
            "profiles_total": len(mine),
        }
        if failures:
            summary["failures"] = failures[:20]

        if cancelled:
            summary["message"] = error or "cancelled"
            return STATUS_CANCELLED, summary
        if error:
            summary["message"] = error[:500]
            return STATUS_FAILED, summary
        if len(done) == len(mine) and not failures:
            return STATUS_SUCCESS, summary
        return (STATUS_PARTIAL if done else STATUS_FAILED), summary

    def _elapsed(self) -> str:
        seconds = int(time.monotonic() - self._started)
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"

    def _execute(self, target: _Target, query: str, params: tuple, insert: bool = False) -> Optional[int]:
        label = target.database.database
        connection = None
        try:
            connection, _ = open_connection(target.database, timeout=10)
            cursor = connection.cursor()
            cursor.execute(query, params)
            connection.commit()
            row_id = cursor.lastrowid if insert else None
            cursor.close()
            return row_id
        except Error as exc:
            if getattr(exc, "errno", None) == ER_NO_SUCH_TABLE:
                target.unsupported = True
                logger.debug(f"Database '{label}' has no job_runs table (site not updated); no heartbeat there.")
            else:
                logger.warning(f"Could not record the run heartbeat in '{label}': {exc}")
        except Exception as exc:  # noqa: BLE001 - the heartbeat must never sink the run
            logger.warning(f"Could not record the run heartbeat in '{label}': {exc}")
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001
                    pass
        return None
