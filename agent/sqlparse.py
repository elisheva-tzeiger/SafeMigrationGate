"""Parsing the input script into statements, and deciding which dialect to read it in.

Note: sqlglot has no dialect-detection API (checked against 30.17.0). What follows
is an explicit trial-parse heuristic, and it is documented as such in the report.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from agent.state import Statement

_RE_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_RE_LINE_COMMENT = re.compile(r"--[^\n]*")


def strip_comments(sql: str) -> str:
    """Remove SQL line comments (--) and block comments (/* */) from *sql*.

    Block comments are stripped before line comments so that a ``--`` inside
    a block comment does not confuse the line-comment regex.  The rest of the
    text (including whitespace) is returned unchanged.
    """
    sql = _RE_BLOCK_COMMENT.sub("", sql)
    sql = _RE_LINE_COMMENT.sub("", sql)
    return sql

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


# ---------------------------------------------------------------------------
# Transaction control
# ---------------------------------------------------------------------------
#
# A dry run must never execute transaction control: a COMMIT inside the user's
# script ends the transaction we were going to roll back, and everything before
# it becomes permanent.
#
# The AST is the primary signal, but it is not sufficient on its own.  Verified
# against sqlglot 30.17.0 across all five supported dialects:
#
#   'END'               -> exp.Commit  (postgres)      exp.Column  (mysql,
#                          sqlite, oracle)             exp.Command (tsql)
#   'END TRANSACTION'   -> exp.Commit  (postgres)      exp.Alias   (mysql,
#                          sqlite, oracle)             exp.Command (tsql)
#   'START TRANSACTION' -> exp.Transaction (mysql, oracle)
#                          exp.Alias    (postgres, tsql, sqlite)
#
# So `END`, which is a full COMMIT synonym in SQLite, parses as a bare column
# reference in the very dialect where it is most dangerous.  Relying on node
# type alone is exactly the blind spot that let a COMMIT through.  We therefore
# also normalise the statement's keyword text and match a small closed set of
# transaction-control spellings.  This is not a regex over arbitrary SQL: it is
# applied only after comments are stripped, and only to the leading keywords.

TRANSACTION_CONTROL_KINDS: frozenset[str] = frozenset(
    {"COMMIT", "ROLLBACK", "TRANSACTION"}
)

# Leading-keyword spellings that are transaction control regardless of how the
# dialect happened to parse them.  Matched against the statement's collapsed,
# comment-free, upper-cased leading words.
_TXN_CONTROL_PHRASES: tuple[str, ...] = (
    "COMMIT",
    "END",
    "ROLLBACK",
    "BEGIN",
    "START TRANSACTION",
    "SAVEPOINT",
    "RELEASE",
)

_TXN_LEADING_WORDS = re.compile(r"^\s*([A-Za-z]+(?:\s+[A-Za-z]+)?)")


def _leading_keywords(sql: str) -> str:
    """The first one or two bare words of *sql*, comment-free and upper-cased."""
    match = _TXN_LEADING_WORDS.match(strip_comments(sql).strip())
    return re.sub(r"\s+", " ", match.group(1)).upper() if match else ""


def is_transaction_control(expression: exp.Expression | None, text: str) -> bool:
    """True if *text* / *expression* is a transaction-control statement.

    Checks the parsed node type first, then falls back to the leading keywords
    for the spellings that degenerate into ``Column`` / ``Alias`` / ``Command``
    in some dialects (notably SQLite's ``END``).
    """
    if expression is not None and isinstance(
        expression, (exp.Commit, exp.Rollback, exp.Transaction)
    ):
        return True

    words = _leading_keywords(text)
    if not words:
        return False
    first = words.split(" ")[0]
    return words in _TXN_CONTROL_PHRASES or first in _TXN_CONTROL_PHRASES


def is_commit_like(expression: exp.Expression | None, text: str) -> bool:
    """True if the statement makes pending work permanent.

    ``COMMIT``, ``COMMIT TRANSACTION``, ``COMMIT WORK``, and ``END`` / ``END
    TRANSACTION`` — which are COMMIT synonyms in SQLite and Postgres — all
    qualify.  ``RELEASE [SAVEPOINT]`` commits a savepoint, so it counts too.
    """
    if expression is not None and isinstance(expression, exp.Commit):
        return True

    words = _leading_keywords(text)
    if not words:
        return False
    return words.split(" ")[0] in {"COMMIT", "END", "RELEASE"}


def statement_kind(text: str, dialect: str) -> str:
    """The parsed kind of a single statement, or '' if it does not parse."""
    try:
        expression = sqlglot.parse_one(text, read=dialect)
    except Exception:
        return ""
    return _kind_of(expression) if expression is not None else ""


def classify_transaction_control(text: str, dialect: str) -> tuple[bool, bool]:
    """Parse *text* in *dialect* and report (is_transaction_control, is_commit_like).

    Parsing is best-effort: an unparseable segment still gets the keyword check,
    because a statement we cannot parse is not a statement we may assume is safe.
    """
    try:
        expression = sqlglot.parse_one(text, read=dialect)
    except Exception:
        expression = None
    return (is_transaction_control(expression, text),
            is_commit_like(expression, text))


def _raw_segments(sql: str) -> list[str]:
    """Split on ';' while keeping each segment's original spelling.

    Respects single-quoted strings, line comments (``--``), and block comments
    (``/* */``).  A ``;`` that appears inside any of those contexts is *not*
    treated as a statement separator.
    """
    segments: list[str] = []
    buffer: list[str] = []
    in_string = False
    in_line_comment = False
    in_block_comment = False
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]
        next_ch = sql[i + 1] if i + 1 < n else ""

        # -- Enter / exit block comment
        if not in_string and not in_line_comment and ch == "/" and next_ch == "*":
            in_block_comment = True
            buffer.append(ch)
            buffer.append(next_ch)
            i += 2
            continue

        if in_block_comment and ch == "*" and next_ch == "/":
            in_block_comment = False
            buffer.append(ch)
            buffer.append(next_ch)
            i += 2
            continue

        # -- Enter line comment
        if not in_string and not in_block_comment and ch == "-" and next_ch == "-":
            in_line_comment = True
            buffer.append(ch)
            buffer.append(next_ch)
            i += 2
            continue

        # -- Exit line comment on newline (keep the newline in the buffer)
        if in_line_comment and ch == "\n":
            in_line_comment = False
            buffer.append(ch)
            i += 1
            continue

        # -- Toggle string literal on single-quote (only outside comments)
        if not in_block_comment and not in_line_comment and ch == "'":
            in_string = not in_string

        # -- Semicolon is a separator only when outside strings and comments
        if ch == ";" and not in_string and not in_block_comment and not in_line_comment:
            segments.append("".join(buffer))
            buffer = []
        else:
            buffer.append(ch)

        i += 1

    segments.append("".join(buffer))
    # Filter segments that contain nothing but whitespace and comments.
    return [s for s in segments if strip_comments(s).strip()]


#: Public name for the statement splitter, used by agent.rewrite.
split_segments = _raw_segments


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
