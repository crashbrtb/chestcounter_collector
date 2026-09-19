"""
Data access layer and repositories for Chests, Player Mappings, and Event Scores.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from mysql.connector import MySQLConnection, Error
from utils.logger import logger


class ChestRepository:
    """Handles persistence of collected clan gifts, errors, and mappings."""

    def __init__(self, connection: MySQLConnection):
        self.connection = connection
        self._columns: Dict[str, bool] = {}

    def get_player_name_mapping(self, ocr_player_name: str) -> str:
        """
        Checks if an OCR player name is mapped to a canonical player name.
        Returns mapped name if found, else original name.
        """
        cursor = self.connection.cursor()
        try:
            query = "SELECT correct_name FROM player_name_mappings WHERE ocr_text = %s"
            cursor.execute(query, (ocr_player_name,))
            result = cursor.fetchone()
            if result and result[0]:
                mapped = result[0]
                logger.info(f"Player name '{ocr_player_name}' mapped to '{mapped}'")
                return mapped
            return ocr_player_name
        except Error as e:
            # Table might not exist or other SQL error
            logger.debug(f"Could not query player_name_mappings: {e}")
            return ocr_player_name
        finally:
            cursor.close()

    def insert_chest(self, name: str, player: str, source: str,
                     collected_at: Optional[datetime] = None) -> bool:
        """
        Inserts a successfully parsed chest into collected_chests table.

        `collected_at` is when the chest was GENERATED, worked out from the
        countdown the game printed next to it - not when the collector read it.
        The two are as much as twenty hours apart, and everything downstream
        counts by day against a reset at 14:00, so the difference decides which
        day a chest belongs to. Passing None leaves the column to the database's
        own `current_timestamp`, which is what happens when the countdown could
        not be read, and on a database old enough not to have the column.
        """
        # Resolve mapped player name
        mapped_player = self.get_player_name_mapping(player)
        dated = collected_at is not None and self.has_column("collected_chests", "collected_at")

        cursor = self.connection.cursor()
        try:
            if dated:
                query = ("INSERT INTO collected_chests (name, player, source, collected_at) "
                         "VALUES (%s, %s, %s, %s)")
                cursor.execute(query, (name, mapped_player, source, collected_at))
            else:
                query = "INSERT INTO collected_chests (name, player, source) VALUES (%s, %s, %s)"
                cursor.execute(query, (name, mapped_player, source))
            self.connection.commit()
            logger.debug(f"Inserted chest: '{name}', player: '{mapped_player}', source: '{source}'"
                         + (f", generated {collected_at:%Y-%m-%d %H:%M:%S}" if dated else ""))
            return True
        except Error as e:
            logger.error(f"Error inserting chest into collected_chests: {e}")
            self.connection.rollback()
            return False
        finally:
            cursor.close()

    def has_column(self, table: str, column: str) -> bool:
        """
        Whether a column exists, asked once and remembered.

        The two profiles are on databases of different ages - one belongs to an
        older version of the game and has not had the newer migrations run
        against it. Rather than fail there, the collector asks and adapts.
        """
        key = f"{table}.{column}"
        if key not in self._columns:
            cursor = self.connection.cursor()
            try:
                cursor.execute(f"SHOW COLUMNS FROM `{table}` LIKE %s", (column,))
                self._columns[key] = cursor.fetchone() is not None
            except Error as exc:
                logger.debug(f"Could not inspect {key}: {exc}")
                self._columns[key] = False
            finally:
                cursor.close()
        return self._columns[key]

    def insert_incomplete_chest(self, name: str, player: str, source: str,
                                screenshot: Optional[bytes] = None,
                                collected_at: Optional[datetime] = None) -> bool:
        """
        Files a chest that could not be read, with the picture of it.

        The picture is the point: a person can read what the OCR could not and
        finish the record by hand, from the web interface. Without it the row
        says only that something was missed, and the chest itself is already
        gone from the game.

        `collected_at` is the chest's generation time, as in `insert_chest` -
        the countdown is often legible on a chest whose name is not, and a row
        corrected months later should carry the hour the chest was made, not the
        hour someone got round to reading it.

        Where the database has no `screenshot` column - the older of the two -
        the row is still written, just without the image.
        """
        with_image = screenshot is not None and self.has_column("incomplete_chests", "screenshot")
        dated = collected_at is not None and self.has_column("incomplete_chests", "collected_at")

        columns = ["name", "player", "source"]
        values: List[Any] = [name, player, source]
        if with_image:
            columns.append("screenshot")
            values.append(screenshot)
        if dated:
            columns.append("collected_at")
            values.append(collected_at)

        cursor = self.connection.cursor()
        try:
            cursor.execute(
                f"INSERT INTO incomplete_chests ({', '.join(columns)}) "
                f"VALUES ({', '.join(['%s'] * len(values))})", tuple(values))
            self.connection.commit()
            logger.warning(
                f"Incomplete chest filed for review: '{name}', player '{player}', source '{source}'"
                + (f" (com captura, {len(screenshot) // 1024} KB)" if with_image
                   else " (sem captura)")
            )
            return True
        except Error as exc:
            logger.error(f"Error inserting incomplete chest: {exc}")
            self.connection.rollback()
            return False
        finally:
            cursor.close()

    def insert_error(self, error_value: str) -> bool:
        """
        Records a problem in `errors`, for the web interface to act on.

        Nothing in the collector calls this today, and that is deliberate. The
        table is meant for problems a person can resolve from the interface -
        a name, a chest, something with a decision behind it - and the two kinds
        of problem the collector actually meets both have a better home:

        * a chest it could not read goes to `incomplete_chests`, with the
          screenshot and a review screen built around it;
        * a run that failed - a menu that did not open, a profile it could not
          reach - is operational, nobody resolves it from a web page, and it
          belongs in the local log.

        Writing those here turned the table into a second, worse copy of the log
        file. It is kept as the way in for whatever the interface does define.

        `error_value` is a tinytext, so the message is truncated rather than
        being lost to a database error at the very moment something went wrong.
        """
        message = (error_value or "").strip()[:250]
        cursor = self.connection.cursor()
        try:
            cursor.execute("INSERT INTO errors (error_value) VALUES (%s)", (message,))
            self.connection.commit()
            logger.info(f"Registrado em errors: {message}")
            return True
        except Error as exc:
            logger.error(f"Could not write to the errors table: {exc}")
            self.connection.rollback()
            return False
        finally:
            cursor.close()


class JournalRepository:
    """Handles persistence of event results, player scores, and rankings."""

    def __init__(self, connection: MySQLConnection):
        self.connection = connection

    def ensure_table_exists(self):
        """Creates event_scores table if it doesn't exist."""
        cursor = self.connection.cursor()
        try:
            query = """
            CREATE TABLE IF NOT EXISTS event_scores (
                id INT AUTO_INCREMENT PRIMARY KEY,
                event_name VARCHAR(128) NOT NULL,
                player_name VARCHAR(128) NOT NULL,
                score BIGINT NOT NULL DEFAULT 0,
                player_rank INT NULL,
                recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_event_player (event_name, player_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
            """
            cursor.execute(query)
            self.connection.commit()
        except Error as e:
            logger.error(f"Error ensuring event_scores table exists: {e}")
        finally:
            cursor.close()

    def insert_event_score(self, event_name: str, player_name: str, score: int, player_rank: Optional[int] = None) -> bool:
        """Inserts an event score record for a player."""
        cursor = self.connection.cursor()
        try:
            self.ensure_table_exists()
            query = """
            INSERT INTO event_scores (event_name, player_name, score, player_rank)
            VALUES (%s, %s, %s, %s)
            """
            cursor.execute(query, (event_name, player_name, score, player_rank))
            self.connection.commit()
            logger.info(f"Recorded event score: {player_name} - {event_name}: {score} (Rank: {player_rank})")
            return True
        except Error as e:
            logger.error(f"Error inserting event score: {e}")
            self.connection.rollback()
            return False
        finally:
            cursor.close()
