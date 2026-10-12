"""
Data access layer and repositories for Chests, Player Mappings, and Event Scores.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from mysql.connector import MySQLConnection, Error
from utils.logger import logger

# What the game says about a chest read from its traffic: the gift it belongs
# to, its type and who sent it. Columns of `collected_chests` and
# `incomplete_chests` on a site recent enough to have them.
GAME_COLUMNS = ("game_gift_id", "game_type_id", "game_player_id")


class ChestRepository:
    """Handles persistence of collected clan gifts, errors, and mappings."""

    def __init__(self, connection: MySQLConnection):
        self.connection = connection
        self._columns: Dict[str, bool] = {}
        #: id of the row the last insert_chest wrote
        self.last_id: Optional[int] = None

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
                     collected_at: Optional[datetime] = None,
                     game: Optional[Dict[str, int]] = None) -> bool:
        """
        Inserts a successfully parsed chest into collected_chests table.

        `game` is what the game itself said about the chest, when it was read
        from the traffic: `game_gift_id`, `game_type_id` and `game_player_id`.
        They are written where the database has the columns (see GAME_COLUMNS)
        and left out where it does not. The id of the row written is kept in
        `last_id`, for whoever may need to take it back.

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

        columns = ["name", "player", "source"]
        values: List[Any] = [name, mapped_player, source]
        if dated:
            columns.append("collected_at")
            values.append(collected_at)
        if game and self.has_game_columns("collected_chests"):
            for column in GAME_COLUMNS:
                columns.append(column)
                values.append(game.get(column))

        self.last_id = None
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                f"INSERT INTO collected_chests ({', '.join(columns)}) "
                f"VALUES ({', '.join(['%s'] * len(values))})", tuple(values))
            self.last_id = cursor.lastrowid
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

    def has_game_columns(self, table: str) -> bool:
        """Whether the table can hold what the game says about a chest (see GAME_COLUMNS)."""
        return all(self.has_column(table, column) for column in GAME_COLUMNS)

    # ------------------------------------------------------ the game's own data
    def can_read_from_game(self) -> bool:
        """
        Whether this database is ready for chests read from the game's traffic.

        It needs the table that names the chest types and the columns that keep
        each gift's id - without the id a chest written twice cannot be told
        from two chests. An older site has neither, and the collector reads the
        screen there as it always did.
        """
        return self.has_column("game_chest_types", "game_type_id") and self.has_game_columns("collected_chests")

    def chest_types(self) -> Dict[int, Tuple[str, str]]:
        """
        Game chest type -> (chest name, source), as they are written with a chest.

        A type whose source the clan scores takes the spelling of
        `standard_chests`, which is the one the scoring joins on. Types learned
        before their source was registered there are linked to it first.
        """
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                "UPDATE game_chest_types t JOIN standard_chests s ON s.source = t.source "
                "SET t.standard_chest_id = s.id WHERE t.standard_chest_id IS NULL")
            self.connection.commit()
            cursor.execute(
                "SELECT t.game_type_id, t.name, COALESCE(s.source, t.source) FROM game_chest_types t "
                "LEFT JOIN standard_chests s ON s.id = t.standard_chest_id")
            return {int(type_id): (name, source) for type_id, name, source in cursor.fetchall()}
        except Error as exc:
            logger.debug(f"Could not read game_chest_types: {exc}")
            self.connection.rollback()
            return {}
        finally:
            cursor.close()

    def learn_chest_type(self, type_id: int, name: str, source: str) -> bool:
        """Names a chest type met for the first time. One already named is left as it is."""
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                "INSERT IGNORE INTO game_chest_types (game_type_id, name, source, standard_chest_id, created, modified) "
                "VALUES (%s, %s, %s, (SELECT id FROM standard_chests WHERE source = %s LIMIT 1), "
                "UTC_TIMESTAMP(), UTC_TIMESTAMP())", (type_id, name, source, source))
            self.connection.commit()
            return cursor.rowcount > 0
        except Error as exc:
            logger.error(f"Could not write chest type {type_id} to game_chest_types: {exc}")
            self.connection.rollback()
            return False
        finally:
            cursor.close()

    def game_players(self) -> Dict[int, str]:
        """Player id in the game -> the name the site knows the member by."""
        cursor = self.connection.cursor()
        try:
            cursor.execute("SELECT game_player_id, player FROM members WHERE game_player_id IS NOT NULL")
            return {int(player_id): name for player_id, name in cursor.fetchall() if name}
        except Error as exc:
            logger.debug(f"Could not read the members' game ids: {exc}")
            return {}
        finally:
            cursor.close()

    def chests_of_gifts(self, first_gift: int, last_gift: int) -> Dict[int, int]:
        """
        Gift id -> how many of its chests the database already has.

        Both tables count: a chest filed as incomplete was opened as well. This
        is what makes a run that stopped halfway harmless to repeat.
        """
        counts: Dict[int, int] = {}
        cursor = self.connection.cursor()
        try:
            for table in ("collected_chests", "incomplete_chests"):
                if not self.has_column(table, "game_gift_id"):
                    continue
                cursor.execute(
                    f"SELECT game_gift_id, COUNT(*) FROM {table} "
                    f"WHERE game_gift_id BETWEEN %s AND %s GROUP BY game_gift_id", (first_gift, last_gift))
                for gift_id, count in cursor.fetchall():
                    counts[int(gift_id)] = counts.get(int(gift_id), 0) + int(count)
        except Error as exc:
            logger.debug(f"Could not count the chests already written: {exc}")
        finally:
            cursor.close()
        return counts

    def delete_chest(self, row_id: int) -> bool:
        """Takes back a row this run wrote for a chest that turned out not to have been opened."""
        cursor = self.connection.cursor()
        try:
            cursor.execute("DELETE FROM collected_chests WHERE id = %s", (row_id,))
            self.connection.commit()
            return cursor.rowcount > 0
        except Error as exc:
            logger.error(f"Could not remove collected_chests row {row_id}: {exc}")
            self.connection.rollback()
            return False
        finally:
            cursor.close()

    def insert_incomplete_chest(self, name: str, player: str, source: str,
                                screenshot: Optional[bytes] = None,
                                collected_at: Optional[datetime] = None,
                                game: Optional[Dict[str, int]] = None) -> bool:
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
        if game and self.has_game_columns("incomplete_chests"):
            for column in GAME_COLUMNS:
                columns.append(column)
                values.append(game.get(column))

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
