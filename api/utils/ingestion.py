import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, List

from api.models.gmail_datamodels import FuelSaleRecord


DEFAULT_DB_FILE = os.getenv("APP_DB_PATH", os.path.abspath("fuel_sales.db"))


class IngestionStore:
    """Thread-safe SQLite storage enforcing atomic transactions and foreign keys."""

    def __init__(self, db_path: str = DEFAULT_DB_FILE):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS processed_emails (
                    message_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL,
                    subject TEXT,
                    sender TEXT,
                    is_sales_report INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PROCESSED', 'IGNORED', 'QUARANTINED', 'FAILED'))
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS fuel_sales (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    sale_date TEXT NOT NULL,
                    branch_name TEXT NOT NULL,
                    fuel_product TEXT NOT NULL,
                    volume_sold REAL NOT NULL,
                    revenue_amount REAL NOT NULL,
                    currency TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (message_id) REFERENCES processed_emails(message_id) ON DELETE CASCADE,
                    CONSTRAINT uq_sale_item UNIQUE (message_id, sale_date, branch_name, fuel_product)
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_fuel_sales_lookup ON fuel_sales(sale_date, branch_name);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_fuel_sales_message ON fuel_sales(message_id);")
            conn.commit()

    def is_message_processed(self, message_id: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM processed_emails WHERE message_id = ?", (message_id,))
            return cursor.fetchone() is not None

    def persist_email_and_records(
        self,
        message_id: str,
        subject: str,
        sender: str,
        status: str,
        is_sales_report: bool,
        records: List[FuelSaleRecord]
    ) -> None:
        """Atomic insert: commits email status and parsed records in a single transaction."""
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute("""
                    INSERT OR REPLACE INTO processed_emails 
                    (message_id, processed_at, subject, sender, is_sales_report, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (message_id, now, subject, sender, int(is_sales_report), status))

                if records:
                    cursor.executemany("""
                        INSERT INTO fuel_sales 
                        (message_id, sale_date, branch_name, fuel_product, volume_sold, revenue_amount, currency, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(message_id, sale_date, branch_name, fuel_product) DO UPDATE SET
                            volume_sold = excluded.volume_sold,
                            revenue_amount = excluded.revenue_amount,
                            currency = excluded.currency
                    """, [
                        (r.raw_message_id, r.sale_date, r.branch_name, r.fuel_product, r.volume_sold, r.revenue_amount, r.currency, now)
                        for r in records
                    ])
                conn.commit()
            except Exception:
                conn.rollback()
                raise