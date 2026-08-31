"""Database access. One layer, via SQLAlchemy, for both the bundled SQLite demo
and a user-supplied connection string.

This module never commits. There is deliberately no code path that does."""

from __future__ import annotations

import re
from typing import TypedDict

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine

from agent.sqlparse import classify_transaction_control

# SQLAlchemy dialect name -> the sqlglot dialect to parse statements as.
_SQLGLOT_DIALECT: dict[str, str] = {
    "sqlite": "sqlite",
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "mssql": "tsql",
    "oracle": "oracle",
}

# user:password@host  ->  user:***@host
#
# The password runs up to the last '@' *before the host*, not the last '@' in
# the whole string. Anchoring to end-of-string (the previous `(?=[^@]*$)`) meant
# that with two connection strings on one line the match ran across both, so
# 'mysql://root:pw@h1/db and postgres://bob:pw2@h2/db' collapsed to
# 'mysql://root:***@h2/db' -- h1 and the entire second URL silently destroyed.
#
# `[^\s]*?` keeps the match inside a single URL (a connection string has no
# spaces), and the trailing lookahead requires what follows to look like a host,
# which still allows '@' inside the password itself: 'sa:P@ssw0rd!@host'.
_CREDENTIALS = re.compile(r"://([^:/@\s]+):([^\s]*?)@(?=[^@/\s]+[/:\s]|[^@/\s]+$)")

# Passwords also travel outside userinfo: as a query parameter on the URL, and
# as a PWD=/PASSWORD= key in an ODBC connection string. The ODBC form is the
# standard way to pass MSSQL credentials, and scrub() is the single chokepoint
# for both the terminal and LangSmith traces, so both are masked here.
_QUERY_PASSWORD = re.compile(r"(?i)\b(password|pwd)(\s*=\s*)([^;&\s\"']+)")


class DryRunRow(TypedDict):
    index: int
    rows_affected: int | None
    error: str | None


class DryRunFailure(Exception):
    """Raised only when the database could not be reached at all. A statement
    that fails on its own merits is reported per-row, not raised."""


def mask_url(url: str) -> str:
    """Never print a connection string without running it through this first."""
    masked = _CREDENTIALS.sub(r"://\1:***@", url)
    return _QUERY_PASSWORD.sub(r"\1\2***", masked)


def make_engine(db_url: str) -> Engine:
    """Build an engine whose transactions actually roll back.

    pysqlite manages transactions itself by default and issues an implicit COMMIT
    before any DDL statement. That silently defeats ROLLBACK: a `DROP TABLE` in a
    "dry run" really drops the table. The two listeners below hand transaction
    control back to us. Verified against sqlite 3.50.4 / SQLAlchemy 2.0.52.
    """
    engine = sa.create_engine(db_url, future=True)

    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _disable_driver_transactions(dbapi_connection, _record):  # noqa: ANN001
            dbapi_connection.isolation_level = None

        @event.listens_for(engine, "begin")
        def _emit_our_own_begin(connection):  # noqa: ANN001
            connection.exec_driver_sql("BEGIN")

    return engine


def dry_run_script(engine: Engine, sql_statements: list[str]) -> list[DryRunRow]:
    """Run every statement inside one transaction, record its row count, roll back.

    One transaction, not one per statement, because the statements are dependent:
    a DELETE followed by a DROP must see the DELETE.

    Transaction-control statements are never executed. A COMMIT (or its ``END``
    synonym) in the caller's list would end the transaction we are about to roll
    back and make every preceding statement permanent. Callers are expected to
    filter those out, but the guarantee lives here too: this is the layer that
    claims "measuring does not change your database", so this is the layer that
    enforces it rather than trusting its caller to have done so.
    """
    rows: list[DryRunRow] = []
    read_dialect = _SQLGLOT_DIALECT.get(engine.dialect.name, "sqlite")

    try:
        connection = engine.connect()
    except Exception as exc:  # pragma: no cover - depends on the driver
        raise DryRunFailure(str(exc)) from exc

    with connection:
        try:
            transaction = connection.begin()
        except Exception as exc:
            raise DryRunFailure(str(exc)) from exc

        failed = False
        try:
            for index, statement in enumerate(sql_statements):
                if failed:
                    rows.append(DryRunRow(index=index, rows_affected=None,
                                          error="skipped after an earlier failure"))
                    continue

                is_txn, _ = classify_transaction_control(statement, read_dialect)
                if is_txn:
                    # Not executed, on purpose. It carries no row count, and
                    # running it would either end or restart the transaction
                    # this whole function exists to roll back.
                    rows.append(DryRunRow(
                        index=index, rows_affected=None,
                        error="transaction control is not executed during a dry run"))
                    continue

                try:
                    # exec_driver_sql, not text(): text() interprets ':name' as a
                    # bind parameter, which breaks Postgres '::' casts.
                    result = connection.exec_driver_sql(statement)
                    count = result.rowcount
                    # An open result cursor holds a read lock; a later DROP on the
                    # same table then fails with "database table is locked".
                    result.close()
                except sa.exc.SQLAlchemyError as exc:
                    # A statement that fails on its own merits is data, not a crash:
                    # record it, mark the rest unmeasurable, and keep going to the
                    # rollback. Once SQLite reports an error the transaction may be
                    # aborted, so later statements are not attempted.
                    rows.append(DryRunRow(index=index, rows_affected=None,
                                          error=str(getattr(exc, "orig", exc))))
                    failed = True
                    continue

                rows.append(DryRunRow(
                    index=index,
                    # rowcount is -1 for DDL and for SELECT. There is no honest
                    # number to report there, so report no number.
                    rows_affected=None if count is None or count < 0 else count,
                    error=None,
                ))
        finally:
            transaction.rollback()

    return rows
