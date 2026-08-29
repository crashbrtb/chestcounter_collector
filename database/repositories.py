"""
Data access layer and repositories for Chests, Player Mappings, and Event Scores.
"""

from typing import Optional, List, Dict, Any
from mysql.connector import MySQLConnection, Error
from utils.logger import logger


class ChestRepository:
    """Handles persistence of collected clan gifts, errors, and mappings."""

    def __init__(self, connection: MySQLConnection):
        self.connection = connection

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

    def insert_chest(self, name: str, player: str, source: str) -> bool:
        """Inserts a successfully parsed chest into collected_chests table."""
        # Resolve mapped player name
        mapped_player = self.get_player_name_mapping(player)

        cursor = self.connection.cursor()
        try:
            query = "INSERT INTO collected_chests (name, player, source) VALUES (%s, %s, %s)"
            cursor.execute(query, (name, mapped_player, source))
            self.connection.commit()
            logger.debug(f"Inserted chest: '{name}', player: '{mapped_player}', source: '{source}'")
            return True
        except Error as e:
            logger.error(f"Error inserting chest into collected_chests: {e}")
            self.connection.rollback()
            return False
        finally:
            cursor.close()

    def insert_incomplete_chest(self, name: str, player: str, source: str) -> bool:
        """Inserts an incomplete chest into incomplete_chests table for manual review."""
        cursor = self.connection.cursor()
        try:
            query = "INSERT INTO incomplete_chests (name, player, source) VALUES (%s, %s, %s)"
            cursor.execute(query, (name, player, source))
            self.connection.commit()
            logger.warning(f"Inserted incomplete chest: '{name}', player: '{player}', source: '{source}'")
            return True
        except Error as e:
            logger.error(f"Error inserting incomplete chest: {e}")
            self.connection.rollback()
            return False
        finally:
            cursor.close()

    def insert_error(self, error_value: str) -> bool:
        """Inserts an error record into errors table."""
        cursor = self.connection.cursor()
        try:
            query = "INSERT INTO errors (error_value) VALUES (%s)"
            cursor.execute(query, (error_value,))
            self.connection.commit()
            logger.debug(f"Inserted error log: {error_value}")
            return True
        except Error as e:
            logger.error(f"Error inserting into errors table: {e}")
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
