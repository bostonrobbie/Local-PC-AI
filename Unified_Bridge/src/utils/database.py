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
        """Creates tables if they don't exist and handles migrations."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # Create initial table
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
                        details TEXT,
                        expected_price REAL,
                        executed_price REAL,
                        slippage REAL
                    )
                ''')
                
                # Migration: Check if columns exist, if not, add them (Safe check)
                existing_cols = [row[1] for row in cursor.execute("PRAGMA table_info(trades)")]
                new_cols = {
                    "expected_price": "REAL",
                    "executed_price": "REAL",
                    "slippage": "REAL"
                }
                for col, dtype in new_cols.items():
                    if col not in existing_cols:
                        try:
                            logger.info(f"Migrating DB: Adding {col}...")
                            cursor.execute(f"ALTER TABLE trades ADD COLUMN {col} {dtype}")
                        except Exception as e:
                            logger.error(f"Migration failed for {col}: {e}")
                            
                conn.commit()
        except Exception as e:
            logger.error(f"DB Init Failed: {e}")

    def log_trade(self, platform, data, status, latency_ms=0, details="", expected_price=0.0, executed_price=0.0, slippage=0.0):
        """Logs a trade execution with full metrics."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO trades (timestamp, platform, symbol, action, volume, status, latency_ms, details, expected_price, executed_price, slippage)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    datetime.now().isoformat(),
                    platform,
                    data.get('symbol'),
                    data.get('action'),
                    float(data.get('volume', 0)),
                    status,
                    latency_ms,
                    str(details),
                    expected_price,
                    executed_price,
                    slippage
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to log trade: {e}")
