"""
Database connection manager for MySQL and MariaDB with UTF-8 support.
"""

from typing import Optional
import mysql.connector
from mysql.connector import MySQLConnection, Error
from config.config_loader import AccountConfig
from utils.logger import logger


class DatabaseConnection:
    """Manages database lifecycle and connections per account."""

    def __init__(self, account_config: AccountConfig):
        self.account_config = account_config
        self._connection: Optional[MySQLConnection] = None

    def connect(self) -> Optional[MySQLConnection]:
        """Establishes connection to the target database."""
        try:
            self._connection = mysql.connector.connect(
                host=self.account_config.host,
                user=self.account_config.user,
                password=self.account_config.password,
                database=self.account_config.database,
                charset="utf8mb4",
                collation="utf8mb4_general_ci",
                use_unicode=True,
                autocommit=False,
            )

            # Set session encoding
            cursor = self._connection.cursor()
            cursor.execute("SET NAMES utf8mb4 COLLATE utf8mb4_general_ci")
            cursor.close()

            logger.info(
                f"Successfully connected to database '{self.account_config.database}' "
                f"for account '{self.account_config.account}'."
            )
            return self._connection

        except Error as e:
            logger.error(
                f"Error connecting to MySQL database '{self.account_config.database}': {e}"
            )
            self._connection = None
            return None

    def is_connected(self) -> bool:
        """Returns True if the connection is alive."""
        return self._connection is not None and self._connection.is_connected()

    def close(self):
        """Closes the active connection if open."""
        if self._connection and self._connection.is_connected():
            try:
                self._connection.close()
                logger.info(f"Database connection closed for '{self.account_config.account}'.")
            except Error as e:
                logger.warning(f"Error closing database connection: {e}")
            finally:
                self._connection = None

    def __enter__(self) -> Optional[MySQLConnection]:
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._connection and self._connection.is_connected():
            if exc_type is not None:
                try:
                    self._connection.rollback()
                except Exception:
                    pass
            self.close()
