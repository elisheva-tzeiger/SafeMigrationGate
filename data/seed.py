"""Builds the bundled demo database.

Run directly to (re)generate data/demo.db:
    python -m data.seed
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS archive_2019;

CREATE TABLE users (
    id         INTEGER PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE orders (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    total      REAL NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE order_items (
    id       INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    sku      TEXT NOT NULL,
    qty      INTEGER NOT NULL
);

CREATE TABLE audit_log (
    id     INTEGER PRIMARY KEY,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    at     TEXT NOT NULL
);

CREATE TABLE archive_2019 (
    id      INTEGER PRIMARY KEY,
    payload TEXT NOT NULL
);
"""

_STATUSES = ("active", "inactive", "suspended")
_ORDER_STATUSES = ("pending", "paid", "shipped", "cancelled")


def build_demo_db(path: Path, large_rows: int = 150_000) -> Path:
    """Create a deterministic demo database at `path`, replacing any existing one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)

        users = [
            (i, f"user{i}@example.com", f"User {i}",
             _STATUSES[i % len(_STATUSES)], f"2024-01-{(i % 28) + 1:02d}")
            for i in range(1, 501)
        ]
        con.executemany("INSERT INTO users VALUES (?,?,?,?,?)", users)

        orders = [
            (i, (i % 500) + 1, round(10.0 + (i % 97) * 3.5, 2),
             _ORDER_STATUSES[i % len(_ORDER_STATUSES)], f"2024-02-{(i % 28) + 1:02d}")
            for i in range(1, 2_001)
        ]
        con.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)

        items = [
            (i, (i % 2000) + 1, f"SKU-{i % 300:04d}", (i % 5) + 1)
            for i in range(1, 5_001)
        ]
        con.executemany("INSERT INTO order_items VALUES (?,?,?,?)", items)

        # audit_log is intentionally large: it is what makes SG013 (>100K rows)
        # reachable in a demo without a real production database.
        con.executemany(
            "INSERT INTO audit_log VALUES (?,?,?,?)",
            ((i, f"svc-{i % 7}", "row.updated", f"2024-03-{(i % 28) + 1:02d}")
             for i in range(1, large_rows + 1)),
        )

        con.executemany(
            "INSERT INTO archive_2019 VALUES (?,?)",
            ((i, f"legacy-payload-{i}") for i in range(1, 1_001)),
        )
        con.commit()
    finally:
        con.close()
    return path


if __name__ == "__main__":  # pragma: no cover
    target = Path(__file__).resolve().parent / "demo.db"
    build_demo_db(target)
    print(f"built {target}")
