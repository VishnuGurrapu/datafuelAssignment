"""
Database schema and operations for QuickMart inventory and OSA reporting.
"""
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


def get_db_connection(db_path: str = "osa.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = "osa.db") -> None:
    with get_db_connection(db_path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS stores (
            store_id TEXT PRIMARY KEY,
            city TEXT NOT NULL,
            name TEXT NOT NULL,
            is_active INTEGER NOT NULL,
            is_serviceable INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sweeps (
            as_of_utc TEXT PRIMARY KEY,
            ist_date TEXT NOT NULL,
            executed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS store_sweep_status (
            store_id TEXT NOT NULL,
            as_of_utc TEXT NOT NULL,
            is_complete INTEGER NOT NULL,
            reason TEXT,
            items_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (store_id, as_of_utc),
            FOREIGN KEY (store_id) REFERENCES stores(store_id),
            FOREIGN KEY (as_of_utc) REFERENCES sweeps(as_of_utc)
        );

        CREATE TABLE IF NOT EXISTS inventory_observations (
            store_id TEXT NOT NULL,
            as_of_utc TEXT NOT NULL,
            sku_id TEXT NOT NULL,
            name TEXT NOT NULL,
            in_stock INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            price REAL NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (store_id, as_of_utc, sku_id),
            FOREIGN KEY (store_id, as_of_utc) REFERENCES store_sweep_status(store_id, as_of_utc)
        );

        CREATE INDEX IF NOT EXISTS idx_sweeps_ist_date ON sweeps(ist_date);
        CREATE INDEX IF NOT EXISTS idx_stores_city ON stores(city);
        CREATE INDEX IF NOT EXISTS idx_obs_sku ON inventory_observations(sku_id);
        CREATE INDEX IF NOT EXISTS idx_status_asof ON store_sweep_status(as_of_utc);
        """)


def upsert_stores(conn: sqlite3.Connection, stores: List[Dict]) -> None:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.executemany(
            """
            INSERT INTO stores (store_id, city, name, is_active, is_serviceable, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(store_id) DO UPDATE SET
                city = excluded.city,
                name = excluded.name,
                is_active = excluded.is_active,
                is_serviceable = excluded.is_serviceable,
                updated_at = excluded.updated_at
            """,
            [
                (
                    s["store_id"],
                    s["city"],
                    s["name"],
                    int(bool(s["is_active"])),
                    int(bool(s["is_serviceable"])),
                    now_iso,
                )
                for s in stores
            ],
        )


def record_sweep(conn: sqlite3.Connection, as_of_utc: str, ist_date: str) -> None:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute(
            """
            INSERT INTO sweeps (as_of_utc, ist_date, executed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(as_of_utc) DO UPDATE SET
                ist_date = excluded.ist_date,
                executed_at = excluded.executed_at
            """,
            (as_of_utc, ist_date, now_iso),
        )


def save_store_sweep(
    conn: sqlite3.Connection,
    store_id: str,
    as_of_utc: str,
    is_complete: bool,
    reason: Optional[str],
    items: List[Dict],
) -> None:
    """
    Save inventory observations and status for one store sweep.
    Protects existing complete data:
    - Case A (complete -> complete): replaces with new complete result.
    - Case B (complete -> incomplete): keeps existing complete data and observations.
    - Case C (incomplete -> complete): replaces with new complete result.
    - Case D (incomplete -> incomplete): updates incomplete reason without duplicates.
    """
    with conn:
        existing = conn.execute(
            "SELECT is_complete FROM store_sweep_status WHERE store_id = ? AND as_of_utc = ?",
            (store_id, as_of_utc),
        ).fetchone()

        if existing and existing[0] == 1 and not is_complete:
            # Case B: Do not overwrite complete data with incomplete data on rerun
            return

        conn.execute(
            """
            INSERT INTO store_sweep_status (store_id, as_of_utc, is_complete, reason, items_count)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(store_id, as_of_utc) DO UPDATE SET
                is_complete = excluded.is_complete,
                reason = excluded.reason,
                items_count = excluded.items_count
            """,
            (store_id, as_of_utc, int(is_complete), reason, len(items) if is_complete else 0),
        )

        conn.execute(
            "DELETE FROM inventory_observations WHERE store_id = ? AND as_of_utc = ?",
            (store_id, as_of_utc),
        )

        if is_complete and items:
            conn.executemany(
                """
                INSERT INTO inventory_observations (store_id, as_of_utc, sku_id, name, in_stock, qty, price, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        store_id,
                        as_of_utc,
                        it["sku_id"],
                        it["name"],
                        int(bool(it["in_stock"])),
                        int(it["qty"]),
                        float(it["price"]),
                        it["observed_at"],
                    )
                    for it in items
                ],
            )
