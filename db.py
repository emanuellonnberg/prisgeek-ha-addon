"""SQLite storage layer (WAL mode) for PrisGeek price watches, discovery rules, history, and alerts."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _resolve_db_path() -> Path:
    """Resolve SQLite database path, preferring PRISGEEK_DB_PATH or HA add-on /data."""
    env_path = os.environ.get("PRISGEEK_DB_PATH", "").strip()
    if env_path:
        return Path(env_path)
    data_dir = Path("/data")
    if os.name != "nt" and data_dir.is_dir():
        return data_dir / "prisgeek_tracker.db"
    return Path(__file__).resolve().parent / "prisgeek_tracker.db"


DB_PATH = _resolve_db_path()


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    """Open a SQLite connection configured with WAL mode and Row factory."""
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    """Create all tracker tables and indexes if they do not exist."""
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS product_watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id INTEGER NOT NULL UNIQUE,
                product_uuid TEXT,
                product_name TEXT NOT NULL,
                product_url TEXT NOT NULL,
                target_price REAL NOT NULL,
                in_stock_only INTEGER NOT NULL DEFAULT 1,
                exclude_refurbished INTEGER NOT NULL DEFAULT 1,
                last_price REAL,
                last_total_price REAL,
                last_retailer TEXT,
                lowest_seen_price REAL,
                lowest_30d_price REAL,
                lowest_all_time_price REAL,
                last_alerted_price REAL,
                last_checked_at TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS query_watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                search_query TEXT NOT NULL,
                must_include_keywords TEXT NOT NULL DEFAULT '[]',
                exclude_keywords TEXT NOT NULL DEFAULT '[]',
                target_price REAL NOT NULL,
                in_stock_only INTEGER NOT NULL DEFAULT 1,
                exclude_refurbished INTEGER NOT NULL DEFAULT 1,
                last_checked_at TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                price REAL NOT NULL,
                total_price REAL,
                retailer TEXT,
                in_stock INTEGER,
                recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_price_history_public_id
                ON price_history (public_id, id DESC);

            CREATE TABLE IF NOT EXISTS alert_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_type TEXT NOT NULL,
                watch_id INTEGER NOT NULL,
                public_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                price REAL NOT NULL,
                retailer TEXT,
                alerted_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_alert_log_lookup
                ON alert_log (watch_type, watch_id, public_id);

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        _migrate_schema(conn)
        conn.commit()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply non-destructive schema migrations for existing SQLite databases."""
    alter_statements = [
        "ALTER TABLE product_watches ADD COLUMN alert_on_all_time_low INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE product_watches ADD COLUMN alert_below_30d_pct REAL NOT NULL DEFAULT 0",
        "ALTER TABLE product_watches ADD COLUMN ai_requirements TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE product_watches ADD COLUMN last_ai_verdict_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE query_watches ADD COLUMN include_outlet INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE query_watches ADD COLUMN best_match_public_id INTEGER",
        "ALTER TABLE query_watches ADD COLUMN best_match_name TEXT",
        "ALTER TABLE query_watches ADD COLUMN best_match_price REAL",
        "ALTER TABLE query_watches ADD COLUMN best_match_retailer TEXT",
        "ALTER TABLE query_watches ADD COLUMN best_match_url TEXT",
        "ALTER TABLE query_watches ADD COLUMN match_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE query_watches ADD COLUMN top_matches_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE query_watches ADD COLUMN ai_requirements TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE query_watches ADD COLUMN last_ai_summary_json TEXT NOT NULL DEFAULT '{}'",
    ]
    for stmt in alter_statements:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a SQLite Row to a plain dictionary, decoding JSON fields when present."""
    data = dict(row)
    for key in ("must_include_keywords", "exclude_keywords"):
        if key in data and isinstance(data[key], str):
            try:
                data[key] = json.loads(data[key])
            except json.JSONDecodeError:
                data[key] = []
    if "top_matches_json" in data:
        raw_top = data.get("top_matches_json")
        if isinstance(raw_top, str):
            try:
                data["top_matches"] = json.loads(raw_top)
            except json.JSONDecodeError:
                data["top_matches"] = []
        else:
            data["top_matches"] = []
    for src_key, dst_key in (
        ("last_ai_verdict_json", "ai_verdict"),
        ("last_ai_summary_json", "ai_summary"),
    ):
        if src_key in data:
            raw_val = data.get(src_key)
            if isinstance(raw_val, str) and raw_val.strip():
                try:
                    data[dst_key] = json.loads(raw_val)
                except json.JSONDecodeError:
                    data[dst_key] = {}
            else:
                data[dst_key] = {}
    return data


def upsert_product_watch(
    *,
    public_id: int,
    product_uuid: str | None,
    product_name: str,
    product_url: str,
    target_price: float,
    in_stock_only: bool = True,
    exclude_refurbished: bool = True,
    alert_on_all_time_low: bool = True,
    alert_below_30d_pct: float = 0.0,
    ai_requirements: str = "",
    last_price: float | None = None,
    last_total_price: float | None = None,
    last_retailer: str | None = None,
    lowest_30d_price: float | None = None,
    lowest_all_time_price: float | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Insert or update a product watch by PrisGeek public_id and return the saved row."""
    init_db()
    now = utc_now_iso()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT lowest_seen_price FROM product_watches WHERE public_id = ?",
            (public_id,),
        ).fetchone()

        lowest_seen = last_price
        if existing and existing["lowest_seen_price"] is not None:
            if lowest_seen is None or existing["lowest_seen_price"] < lowest_seen:
                lowest_seen = existing["lowest_seen_price"]

        conn.execute(
            """
            INSERT INTO product_watches (
                public_id, product_uuid, product_name, product_url, target_price,
                in_stock_only, exclude_refurbished, alert_on_all_time_low, alert_below_30d_pct,
                ai_requirements, last_price, last_total_price, last_retailer, lowest_seen_price,
                lowest_30d_price, lowest_all_time_price, last_checked_at, active, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(public_id) DO UPDATE SET
                product_uuid = COALESCE(excluded.product_uuid, product_watches.product_uuid),
                product_name = excluded.product_name,
                product_url = excluded.product_url,
                target_price = excluded.target_price,
                in_stock_only = excluded.in_stock_only,
                exclude_refurbished = excluded.exclude_refurbished,
                alert_on_all_time_low = excluded.alert_on_all_time_low,
                alert_below_30d_pct = excluded.alert_below_30d_pct,
                ai_requirements = CASE
                    WHEN excluded.ai_requirements != '' THEN excluded.ai_requirements
                    ELSE product_watches.ai_requirements
                END,
                last_price = COALESCE(excluded.last_price, product_watches.last_price),
                last_total_price = COALESCE(excluded.last_total_price, product_watches.last_total_price),
                last_retailer = COALESCE(excluded.last_retailer, product_watches.last_retailer),
                lowest_seen_price = COALESCE(excluded.lowest_seen_price, product_watches.lowest_seen_price),
                lowest_30d_price = COALESCE(excluded.lowest_30d_price, product_watches.lowest_30d_price),
                lowest_all_time_price = COALESCE(excluded.lowest_all_time_price, product_watches.lowest_all_time_price),
                last_checked_at = excluded.last_checked_at,
                active = 1,
                notes = CASE
                    WHEN excluded.notes != '' THEN excluded.notes
                    ELSE product_watches.notes
                END
            """,
            (
                public_id,
                product_uuid,
                product_name,
                product_url,
                float(target_price),
                int(in_stock_only),
                int(exclude_refurbished),
                int(alert_on_all_time_low),
                float(alert_below_30d_pct),
                ai_requirements.strip(),
                last_price,
                last_total_price,
                last_retailer,
                lowest_seen,
                lowest_30d_price,
                lowest_all_time_price,
                now if last_price is not None else None,
                notes,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM product_watches WHERE public_id = ?",
            (public_id,),
        ).fetchone()
    return _row_to_dict(row)


def add_query_watch(
    *,
    name: str,
    search_query: str,
    target_price: float,
    must_include_keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    in_stock_only: bool = True,
    exclude_refurbished: bool = True,
    include_outlet: bool = False,
    ai_requirements: str = "",
) -> dict[str, Any]:
    """Create a new keyword/interest discovery watch rule and return the saved row."""
    init_db()
    must_list = [kw.strip() for kw in (must_include_keywords or []) if kw.strip()]
    excl_list = [kw.strip() for kw in (exclude_keywords or []) if kw.strip()]
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO query_watches (
                name, search_query, must_include_keywords, exclude_keywords,
                target_price, in_stock_only, exclude_refurbished, include_outlet,
                ai_requirements, active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                name.strip(),
                search_query.strip(),
                json.dumps(must_list, ensure_ascii=False),
                json.dumps(excl_list, ensure_ascii=False),
                float(target_price),
                int(in_stock_only),
                int(exclude_refurbished),
                int(include_outlet),
                ai_requirements.strip(),
            ),
        )
        watch_id = cursor.lastrowid
        conn.commit()
        row = conn.execute(
            "SELECT * FROM query_watches WHERE id = ?",
            (watch_id,),
        ).fetchone()
    return _row_to_dict(row)


def update_product_watch_config(
    watch_id: int,
    *,
    target_price: float,
    notes: str = "",
    in_stock_only: bool = True,
    exclude_refurbished: bool = True,
    alert_on_all_time_low: bool = True,
    alert_below_30d_pct: float = 0.0,
    ai_requirements: str = "",
) -> dict[str, Any]:
    """Update mutable settings on an existing product watch and return the updated row."""
    init_db()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE product_watches
            SET target_price = ?,
                notes = ?,
                in_stock_only = ?,
                exclude_refurbished = ?,
                alert_on_all_time_low = ?,
                alert_below_30d_pct = ?,
                ai_requirements = ?
            WHERE id = ? AND active = 1
            """,
            (
                float(target_price),
                notes.strip(),
                int(in_stock_only),
                int(exclude_refurbished),
                int(alert_on_all_time_low),
                float(alert_below_30d_pct),
                ai_requirements.strip(),
                watch_id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Product watch #{watch_id} not found")
        conn.commit()
        row = conn.execute("SELECT * FROM product_watches WHERE id = ?", (watch_id,)).fetchone()
    return _row_to_dict(row)


def update_query_watch_config(
    watch_id: int,
    *,
    name: str,
    search_query: str,
    target_price: float,
    must_include_keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    in_stock_only: bool = True,
    include_outlet: bool = False,
    ai_requirements: str = "",
) -> dict[str, Any]:
    """Update mutable settings on an existing interest/query watch and return the updated row."""
    init_db()
    must_list = [kw.strip() for kw in (must_include_keywords or []) if kw.strip()]
    excl_list = [kw.strip() for kw in (exclude_keywords or []) if kw.strip()]
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE query_watches
            SET name = ?,
                search_query = ?,
                must_include_keywords = ?,
                exclude_keywords = ?,
                target_price = ?,
                in_stock_only = ?,
                include_outlet = ?,
                exclude_refurbished = ?,
                ai_requirements = ?
            WHERE id = ? AND active = 1
            """,
            (
                name.strip(),
                search_query.strip(),
                json.dumps(must_list, ensure_ascii=False),
                json.dumps(excl_list, ensure_ascii=False),
                float(target_price),
                int(in_stock_only),
                int(include_outlet),
                int(not include_outlet),
                ai_requirements.strip(),
                watch_id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Interest watch #{watch_id} not found")
        conn.commit()
        row = conn.execute("SELECT * FROM query_watches WHERE id = ?", (watch_id,)).fetchone()
    return _row_to_dict(row)


def list_all_watches(active_only: bool = True) -> dict[str, Any]:
    """Return all product watches, query watches, and recent alerts."""
    init_db()
    where_clause = "WHERE active = 1" if active_only else ""
    with get_connection() as conn:
        products = [
            _row_to_dict(r)
            for r in conn.execute(
                f"SELECT * FROM product_watches {where_clause} ORDER BY id ASC"
            ).fetchall()
        ]
        queries = [
            _row_to_dict(r)
            for r in conn.execute(
                f"SELECT * FROM query_watches {where_clause} ORDER BY id ASC"
            ).fetchall()
        ]
        alerts = [
            _row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM alert_log ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ]
    return {
        "product_watches": products,
        "query_watches": queries,
        "recent_alerts": alerts,
    }


def remove_watch(watch_type: str, watch_id: int) -> bool:
    """Deactivate a watch rule by watch_type ('product' or 'query') and id."""
    init_db()
    table_map = {"product": "product_watches", "query": "query_watches"}
    table = table_map.get(watch_type.lower().strip())
    if not table:
        raise ValueError("watch_type must be 'product' or 'query'")

    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE {table} SET active = 0 WHERE id = ? AND active = 1",
            (watch_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def record_price_observation(
    *,
    public_id: int,
    product_name: str,
    price: float | None,
    total_price: float | None,
    retailer: str | None,
    in_stock: bool,
) -> bool:
    """Insert a price_history row if price or retailer changed since the latest observation."""
    if price is None:
        return False

    init_db()
    with get_connection() as conn:
        last = conn.execute(
            """
            SELECT price, total_price, retailer, in_stock
            FROM price_history
            WHERE public_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (public_id,),
        ).fetchone()

        if last is not None:
            same_price = abs(float(last["price"]) - float(price)) < 0.01
            same_total = (last["total_price"] is None and total_price is None) or (
                last["total_price"] is not None
                and total_price is not None
                and abs(float(last["total_price"]) - float(total_price)) < 0.01
            )
            same_retailer = (last["retailer"] or "") == (retailer or "")
            same_stock = bool(last["in_stock"]) == bool(in_stock)
            if same_price and same_total and same_retailer and same_stock:
                return False

        conn.execute(
            """
            INSERT INTO price_history (
                public_id, product_name, price, total_price, retailer, in_stock, recorded_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                public_id,
                product_name,
                float(price),
                float(total_price) if total_price is not None else None,
                retailer,
                int(in_stock),
                utc_now_iso(),
            ),
        )
        conn.commit()
        return True


def update_product_watch_state(
    *,
    watch_id: int,
    last_price: float | None,
    last_total_price: float | None,
    last_retailer: str | None,
    lowest_30d_price: float | None,
    lowest_all_time_price: float | None,
    last_alerted_price: float | None = None,
) -> None:
    """Update cached price metrics and check timestamp for a product watch."""
    init_db()
    now = utc_now_iso()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT lowest_seen_price, last_alerted_price FROM product_watches WHERE id = ?",
            (watch_id,),
        ).fetchone()
        if row is None:
            return

        lowest_seen = row["lowest_seen_price"]
        if last_price is not None and (lowest_seen is None or last_price < lowest_seen):
            lowest_seen = last_price

        alerted_price = (
            last_alerted_price
            if last_alerted_price is not None
            else row["last_alerted_price"]
        )

        conn.execute(
            """
            UPDATE product_watches
            SET last_price = COALESCE(?, last_price),
                last_total_price = COALESCE(?, last_total_price),
                last_retailer = COALESCE(?, last_retailer),
                lowest_seen_price = ?,
                lowest_30d_price = COALESCE(?, lowest_30d_price),
                lowest_all_time_price = COALESCE(?, lowest_all_time_price),
                last_alerted_price = ?,
                last_checked_at = ?
            WHERE id = ?
            """,
            (
                last_price,
                last_total_price,
                last_retailer,
                lowest_seen,
                lowest_30d_price,
                lowest_all_time_price,
                alerted_price,
                now,
                watch_id,
            ),
        )
        conn.commit()


def update_query_watch_checked(watch_id: int) -> None:
    """Record the last_checked_at timestamp for a query watch."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE query_watches SET last_checked_at = ? WHERE id = ?",
            (utc_now_iso(), watch_id),
        )
        conn.commit()


def update_query_watch_results(
    watch_id: int,
    *,
    match_count: int,
    best_match: dict[str, Any] | None,
    top_matches: list[dict[str, Any]],
) -> None:
    """Store latest match count, cheapest matching product, and top-5 preview for a query watch."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE query_watches
            SET match_count = ?,
                best_match_public_id = ?,
                best_match_name = ?,
                best_match_price = ?,
                best_match_retailer = ?,
                best_match_url = ?,
                top_matches_json = ?,
                last_checked_at = ?
            WHERE id = ?
            """,
            (
                int(match_count),
                best_match.get("public_id") if best_match else None,
                best_match.get("name") if best_match else None,
                best_match.get("lowest_price") if best_match else None,
                best_match.get("best_retailer") if best_match else None,
                best_match.get("url") if best_match else None,
                json.dumps(top_matches, ensure_ascii=False),
                utc_now_iso(),
                watch_id,
            ),
        )
        conn.commit()


def should_send_alert(
    watch_type: str, watch_id: int, public_id: int, current_price: float
) -> bool:
    """Return True if no prior alert exists for this watch+product, or price dropped lower."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT MIN(price) AS min_alerted_price
            FROM alert_log
            WHERE watch_type = ? AND watch_id = ? AND public_id = ?
            """,
            (watch_type, watch_id, public_id),
        ).fetchone()
    if row is None or row["min_alerted_price"] is None:
        return True
    return float(current_price) < float(row["min_alerted_price"])


def log_alert(
    *,
    watch_type: str,
    watch_id: int,
    public_id: int,
    product_name: str,
    price: float,
    retailer: str | None,
) -> None:
    """Record a triggered alert in alert_log to prevent duplicate notifications."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO alert_log (
                watch_type, watch_id, public_id, product_name, price, retailer, alerted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                watch_type,
                watch_id,
                public_id,
                product_name,
                float(price),
                retailer,
                utc_now_iso(),
            ),
        )
        conn.commit()


def get_recent_history(public_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent price_history rows, optionally filtered by public_id."""
    init_db()
    with get_connection() as conn:
        if public_id is not None:
            rows = conn.execute(
                "SELECT * FROM price_history WHERE public_id = ? ORDER BY id DESC LIMIT ?",
                (public_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM price_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def save_product_ai_verdict(watch_id: int, verdict: dict[str, Any]) -> None:
    """Persist the latest AI deal verdict JSON for a product watch."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE product_watches SET last_ai_verdict_json = ? WHERE id = ?",
            (json.dumps(verdict, ensure_ascii=False), watch_id),
        )
        conn.commit()


def save_query_ai_summary(watch_id: int, summary: dict[str, Any]) -> None:
    """Persist the latest AI vetting summary JSON for a query/interest watch."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE query_watches SET last_ai_summary_json = ? WHERE id = ?",
            (json.dumps(summary, ensure_ascii=False), watch_id),
        )
        conn.commit()


def get_setting(key: str, default: Any = None) -> Any:
    """Read a JSON-decoded setting from app_settings, returning default if absent."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return default
    raw = row["value"]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def set_setting(key: str, value: Any) -> None:
    """Upsert a setting value (JSON-encoded) in app_settings."""
    init_db()
    encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, encoded, utc_now_iso()),
        )
        conn.commit()
