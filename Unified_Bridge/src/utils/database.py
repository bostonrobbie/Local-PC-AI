import sqlite3
import logging
import os
from datetime import datetime

logger = logging.getLogger("Database")

class DatabaseManager:
    def __init__(self, db_path='trades.db'):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Creates tables if they don't exist."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT,
                        platform TEXT,
                        symbol TEXT,
                        action TEXT,
                        volume REAL,
                        status TEXT,
                        latency_ms REAL,
                        details TEXT
                    )
                ''')
                conn.commit()
        except Exception as e:
            logger.error(f"DB Init Failed: {e}")

    def log_trade(self, platform, data, status, latency_ms=0, details=""):
        """Logs a trade execution."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO trades (timestamp, platform, symbol, action, volume, status, latency_ms, details)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    datetime.now().isoformat(),
                    platform,
                    data.get('symbol'),
                    data.get('action'),
                    float(data.get('volume', 0)),
                    status,
                    latency_ms,
                    str(details)
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to log trade: {e}")
