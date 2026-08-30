"""Database access. One layer, via SQLAlchemy, for both the bundled SQLite demo
and a user-supplied connection string.

This module never commits. There is deliberately no code path that does."""

from __future__ import annotations

import re
from typing import TypedDict

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine

# user:password@  ->  user:***@   (the password is anything up to the last '@')
_CREDENTIALS = re.compile(r"://([^:/@]+):(.*)@(?=[^@]*$)")


class DryRunRow(TypedDict):
    index: int
    rows_affected: int | None
    error: str | None


class DryRunFailure(Exception):
    """Raised only when the database could not be reached at all. A statement
    that fails on its own merits is reported per-row, not raised."""


def mask_url(url: str) -> str:
    """Never print a connection string without running it through this first."""
    return _CREDENTIALS.sub(r"://\1:***@", url)


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
    """
    rows: list[DryRunRow] = []

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
