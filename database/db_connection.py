"""
MySQL/MariaDB connection, one per profile.

Each profile points at its own database, so the connection is opened per
profile and closed as soon as its chests are in - a run that visits six profiles
should not hold six idle connections while the game loads.

The charset is pinned to utf8mb4 on purpose: player names carry accents and
emoji, and the whole point of moving to a better OCR engine is lost if the
database mangles what the OCR got right.

Choosing a collation
--------------------
Left to itself, mysql-connector asks for `utf8mb4_0900_ai_ci` - MySQL 8's
default, which MariaDB has never heard of and rejects with error 1273. So the
collation is named explicitly, and a short fallback chain covers servers that
lack the first choice; the last resort is to name none and let the server pick.

Opening a connection lives in one place so that 'Test connection' in the
configuration interface and the worker threads during a run hit the server
the exact same way - same options, same charset, same fallback ladder.
"""

from typing import Optional, Tuple

import mysql.connector
from mysql.connector import Error, MySQLConnection

from config.settings import DatabaseConfig
from utils.logger import logger

CHARSET = "utf8mb4"

# In order of preference. utf8mb4_general_ci exists on MariaDB and on every
# MySQL since 5.5; None means "server's choice", for anything unusual.
COLLATIONS: Tuple[Optional[str], ...] = ("utf8mb4_general_ci", "utf8mb4_unicode_ci", None)

ER_UNKNOWN_COLLATION = 1273
ER_UNKNOWN_CHARSET = 1115


def open_connection(database: DatabaseConfig, timeout: Optional[int] = None
                    ) -> Tuple[MySQLConnection, Optional[str]]:
    """
    Opens a connection, trying the collations in order.

    Returns (connection, collation actually used). Raises the last error when
    none of them is accepted - an unknown collation is the only failure retried
    here, since a wrong password or an unreachable host will not improve on the
    second attempt.
    """
    last_error: Optional[Error] = None

    for collation in COLLATIONS:
        options = {
            "host": database.host,
            "port": database.port,
            "user": database.user,
            "password": database.password,
            "database": database.database,
            "charset": CHARSET,
            "use_unicode": True,
            "autocommit": False,
        }
        if collation:
            options["collation"] = collation
        if timeout:
            options["connection_timeout"] = timeout

        try:
            return mysql.connector.connect(**options), collation
        except Error as exc:
            if getattr(exc, "errno", None) in (ER_UNKNOWN_COLLATION, ER_UNKNOWN_CHARSET):
                logger.debug(f"Server rejected collation {collation or '(default)'}: {exc}")
                last_error = exc
                continue
            raise

    raise last_error if last_error else Error("Could not open database connection.")


class DatabaseConnection:
    """Context manager yielding a live connection, or None when it cannot be opened."""

    def __init__(self, database: DatabaseConfig, label: str = ""):
        self.database = database
        self.label = label or database.database
        self._connection: Optional[MySQLConnection] = None

    def connect(self) -> Optional[MySQLConnection]:
        if not self.database or not self.database.is_usable:
            logger.error(f"Incomplete database settings for '{self.label}'.")
            return None
        try:
            self._connection, collation = open_connection(self.database)
            cursor = self._connection.cursor()
            cursor.execute(f"SET NAMES {CHARSET}" + (f" COLLATE {collation}" if collation else ""))
            cursor.close()
            logger.info(
                f"Connected to database '{self.database.database}' for '{self.label}' "
                f"({CHARSET}/{collation or 'server default'})."
            )
            return self._connection
        except Error as exc:
            logger.error(f"Could not connect to '{self.database.database}' for '{self.label}': {exc}")
            self._connection = None
            return None

    def is_connected(self) -> bool:
        return self._connection is not None and self._connection.is_connected()

    def close(self):
        if self.is_connected():
            try:
                self._connection.close()
                logger.info(f"Database connection closed for '{self.label}'.")
            except Error as exc:
                logger.warning(f"Error closing the database connection: {exc}")
            finally:
                self._connection = None

    def __enter__(self) -> Optional[MySQLConnection]:
        return self.connect()

    def __exit__(self, exc_type, exc_value, traceback):
        if self.is_connected() and exc_type is not None:
            try:
                self._connection.rollback()
            except Exception:
                pass
        self.close()


def test_connection(database: DatabaseConfig) -> tuple:
    """
    (ok, message) - what the 'Test connection' button reports.

    Goes through `open_connection` so the button proves what the collection
    will find, down to the collation.
    """
    if not database or not database.is_usable:
        return False, "Fill in host, user, and database."
    try:
        connection, collation = open_connection(database, timeout=6)
        server = connection.get_server_info()
        connection.close()
        return True, f"Connected — {server}, {CHARSET}/{collation or 'server default'}."
    except Error as exc:
        return False, str(exc)
