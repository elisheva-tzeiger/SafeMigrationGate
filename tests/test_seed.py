import sqlite3

from data.seed import build_demo_db


def test_build_demo_db_creates_all_tables(tmp_path):
    db = build_demo_db(tmp_path / "demo.db", large_rows=1_000)
    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"users", "orders", "order_items", "audit_log", "archive_2019"} <= names


def test_audit_log_is_large_enough_to_exercise_sg013(tmp_path):
    db = build_demo_db(tmp_path / "demo.db", large_rows=1_000)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1_000
    con.close()


def test_orders_reference_real_users(tmp_path):
    db = build_demo_db(tmp_path / "demo.db", large_rows=100)
    con = sqlite3.connect(db)
    orphans = con.execute(
        "SELECT COUNT(*) FROM orders o "
        "LEFT JOIN users u ON u.id = o.user_id WHERE u.id IS NULL"
    ).fetchone()[0]
    con.close()
    assert orphans == 0


def test_rebuilding_is_idempotent(tmp_path):
    target = tmp_path / "demo.db"
    build_demo_db(target, large_rows=50)
    build_demo_db(target, large_rows=50)
    con = sqlite3.connect(target)
    assert con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 50
    con.close()
