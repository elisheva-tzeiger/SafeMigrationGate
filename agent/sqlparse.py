"""Parsing the input script into statements, and deciding which dialect to read it in.

Note: sqlglot has no dialect-detection API (checked against 30.17.0). What follows
is an explicit trial-parse heuristic, and it is documented as such in the report.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from agent.state import Statement

# Ordered by preference: the first that parses wins, so the most portable and
# most common dialect goes first.
SUPPORTED_DIALECTS: tuple[str, ...] = ("postgres", "mysql", "tsql", "sqlite", "oracle")


class ParseFailure(Exception):
    """The script is not valid SQL in any dialect we support."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _parses(sql: str, dialect: str) -> bool:
    try:
        sqlglot.parse(sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
        return True
    except Exception:
        return False


def resolve_dialect(sql: str, hint: str | None = None) -> str:
    """Return the dialect to read `sql` in.

    A user-supplied hint wins if the script actually parses under it; otherwise we
    trial-parse in preference order. Raises ParseFailure if nothing parses.
    """
    if hint and _parses(sql, hint):
        return hint
    for dialect in SUPPORTED_DIALECTS:
        if _parses(sql, dialect):
            return dialect
    raise ParseFailure(
        "the script could not be parsed as SQL in any supported dialect "
        f"({', '.join(SUPPORTED_DIALECTS)})"
    )


_KIND_BY_TYPE: dict[type, str] = {
    exp.Delete: "DELETE",
    exp.Update: "UPDATE",
    exp.Insert: "INSERT",
    exp.Drop: "DROP",
    exp.TruncateTable: "TRUNCATE",
    exp.Alter: "ALTER",
    exp.Create: "CREATE",
    exp.Grant: "GRANT",
    exp.Revoke: "REVOKE",
    exp.Select: "SELECT",
    exp.Transaction: "TRANSACTION",
    exp.Commit: "COMMIT",
    exp.Rollback: "ROLLBACK",
    exp.Command: "COMMAND",
}


def _kind_of(expression: exp.Expression) -> str:
    for node_type, name in _KIND_BY_TYPE.items():
        if isinstance(expression, node_type):
            return name
    return type(expression).__name__.upper()


def _raw_segments(sql: str) -> list[str]:
    """Split on ';' while keeping each segment's original spelling."""
    segments, buffer, in_string = [], [], False
    for char in sql:
        if char == "'":
            in_string = not in_string
        if char == ";" and not in_string:
            segments.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    segments.append("".join(buffer))
    return [s for s in segments if s.strip()]


def split_statements(sql: str, dialect: str) -> list[Statement]:
    """Parse `sql` into Statement records, preserving each statement's original text."""
    segments = _raw_segments(sql)
    statements: list[Statement] = []

    for index, segment in enumerate(segments):
        try:
            expression = sqlglot.parse_one(segment, read=dialect)
        except Exception as exc:
            raise ParseFailure(f"statement {index + 1} could not be parsed: {exc}") from exc
        if expression is None:
            continue

        where = expression.args.get("where") if hasattr(expression, "args") else None
        statements.append(Statement(
            index=index,
            text=segment,
            kind=_kind_of(expression),
            target_tables=sorted({t.name for t in expression.find_all(exp.Table) if t.name}),
            has_where=where is not None,
            where_text=where.this.sql(dialect=dialect) if where is not None else None,
        ))

    return statements


def to_dialect(sql: str, read: str, write: str) -> str:
    """Translate a statement between dialects for the dry run. Any number measured
    on a translated statement is tagged 'measured_demo_transpiled', never
    'measured_target'."""
    return "; ".join(sqlglot.transpile(sql, read=read, write=write))
