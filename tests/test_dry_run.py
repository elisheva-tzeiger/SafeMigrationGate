"""The most important test file in the project.

The tool's entire value proposition is the claim "measuring the impact does not
change your database". These tests are that claim, made falsifiable.
"""

import hashlib

import pytest
import sqlalchemy as sa

from agent.db import DryRunFailure, dry_run_script, make_engine, mask_url


@pytest.fixture()
def demo(tmp_path):
    path = tmp_path / "t.db"
    engine = make_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, tag TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO users(name, tag) VALUES "
            "('a','12:30'),('b','09:15'),('c','12:30'),('d','x'),('e','y')"
        )
    return engine, path


def _fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_measures_rows_without_changing_anything(demo):
    engine, path = demo
    before = _fingerprint(path)

    rows = dry_run_script(engine, ["DELETE FROM users"])

    assert rows == [{"index": 0, "rows_affected": 5, "error": None}]
    engine.dispose()
    assert _fingerprint(path) == before, "the database file must be byte-identical"


def test_ddl_is_rolled_back_too(demo):
    """pysqlite's default behaviour commits before DDL. If this test fails, the
    tool actively destroys the thing it promises to protect."""
    engine, path = demo

    dry_run_script(engine, ["DROP TABLE users"])

    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM users").scalar() == 5


def test_alter_drop_column_is_rolled_back(demo):
    engine, _ = demo

    dry_run_script(engine, ["ALTER TABLE users DROP COLUMN tag"])

    with engine.connect() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(users)")]
    assert "tag" in cols


def test_statements_run_in_one_transaction_so_they_see_each_other(demo):
    engine, _ = demo

    rows = dry_run_script(
        engine,
        ["DELETE FROM users WHERE tag='12:30'", "DELETE FROM users"],
    )

    assert rows[0]["rows_affected"] == 2
    assert rows[1]["rows_affected"] == 3, "the second DELETE sees the first one"


def test_select_before_drop_does_not_deadlock(demo):
    """An unclosed SELECT cursor holds a read lock and makes DROP fail with
    'database table is locked'."""
    engine, _ = demo

    rows = dry_run_script(
        engine, ["SELECT COUNT(*) FROM users", "DROP TABLE users"]
    )

    assert rows[1]["error"] is None
    assert rows[1]["rows_affected"] is None


def test_ddl_reports_none_not_a_fake_count(demo):
    """DDL has no row count. Reporting -1 or 0 would be a lie dressed as a number."""
    engine, _ = demo
    rows = dry_run_script(engine, ["DROP TABLE users"])
    assert rows[0]["rows_affected"] is None


def test_a_failing_statement_is_recorded_and_the_rest_are_skipped(demo):
    engine, path = demo
    before = _fingerprint(path)

    rows = dry_run_script(
        engine,
        ["DELETE FROM users WHERE tag='x'", "DELETE FROM no_such_table",
         "DELETE FROM users"],
    )

    assert rows[0]["rows_affected"] == 1
    assert rows[1]["error"] is not None and "no_such_table" in rows[1]["error"]
    assert rows[2] == {"index": 2, "rows_affected": None,
                       "error": "skipped after an earlier failure"}
    engine.dispose()
    assert _fingerprint(path) == before


def test_a_commit_in_the_script_cannot_defeat_the_rollback(demo):
    """The bug this test exists for: sqlglot treats COMMIT as a first-class
    statement and passed it through verbatim, so it *executed* and the outer
    rollback had nothing left to undo. `END` and `COMMIT TRANSACTION` are COMMIT
    synonyms and did the same. A dry run must never execute transaction control.
    """
    engine, path = demo
    before = _fingerprint(path)

    rows = dry_run_script(
        engine,
        ["DELETE FROM users", "COMMIT", "END", "COMMIT TRANSACTION", "commit"],
    )

    assert rows[0]["rows_affected"] == 5, "the DELETE is still measured"
    for row in rows[1:]:
        assert row["rows_affected"] is None
        assert "transaction control is not executed" in row["error"]

    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM users").scalar() == 5
    engine.dispose()
    assert _fingerprint(path) == before, "the database file must be byte-identical"


def test_a_commit_does_not_make_a_following_drop_permanent(demo):
    """`COMMIT; DROP TABLE users;` permanently dropped the table."""
    engine, path = demo
    before = _fingerprint(path)

    dry_run_script(engine, ["DELETE FROM users", "COMMIT", "DROP TABLE users"])

    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM users").scalar() == 5
    engine.dispose()
    assert _fingerprint(path) == before


def test_unreachable_database_raises_dry_run_failure(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'nested' / 'missing' / 'x.db'}")
    with pytest.raises(DryRunFailure):
        dry_run_script(engine, ["SELECT 1"])


@pytest.mark.parametrize(
    "raw, masked",
    [
        ("postgresql://alice:s3cr3t@db.internal:5432/prod",
         "postgresql://alice:***@db.internal:5432/prod"),
        ("mssql+pyodbc://sa:P@ssw0rd!@host/db",
         "mssql+pyodbc://sa:***@host/db"),
        ("sqlite:///C:/data/demo.db", "sqlite:///C:/data/demo.db"),
        ("postgresql://alice@db.internal/prod", "postgresql://alice@db.internal/prod"),
        # The lookahead anchored to the last '@' in the entire string, so a line
        # holding two connection strings was masked across both: h1 and the
        # whole second URL were silently destroyed.
        ("mysql://root:pw@h1/db and postgres://bob:pw2@h2/db",
         "mysql://root:***@h1/db and postgres://bob:***@h2/db"),
        # Passwords outside the userinfo were missed entirely.
        ("postgresql://u:p@h/db?password=topsecret",
         "postgresql://u:***@h/db?password=***"),
        ("mssql+pyodbc:///?odbc_connect=SERVER=s;UID=sa;PWD=SuperSecret;",
         "mssql+pyodbc:///?odbc_connect=SERVER=s;UID=sa;PWD=***;"),
    ],
)
def test_mask_url_hides_the_password_and_nothing_else(raw, masked):
    assert mask_url(raw) == masked
