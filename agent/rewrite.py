"""The rewrite contract, and the validator that enforces its central invariant:

    the safe script, run as-is, changes nothing.

That claim is checkable, so it is checked rather than assumed."""

from __future__ import annotations

import re
from typing import TypedDict

import sqlglot

from agent.sqlparse import strip_comments
from agent.state import EvalResult, Statement

# A block comment, not `<-- ... -->`: the latter parses as a `<` operator followed
# by a line comment, which truncates the statement and makes sqlglot raise. `1 = 0`
# matches nothing, so the placeholder is inert until a human replaces it -- the
# agent must never invent a predicate.
FILL_PLACEHOLDER = "WHERE /* FILL: condition */ 1 = 0"

REWRITE_CONTRACT = f"""\
1. Open with `BEGIN;` in the target dialect.
2. Preserve every original statement verbatim, except for changes you mark explicitly.
3. A DELETE or UPDATE with no WHERE: comment the original out, and emit a replacement
   using the template `{FILL_PLACEHOLDER}`. Never invent a predicate.
4. Before each statement that destroys data (DELETE, UPDATE, TRUNCATE), inject a
   `SELECT COUNT(*)` carrying the same WHERE clause, so a human can verify the scope.
   Do not inject a count before DDL.
5. Close with `ROLLBACK;`. `-- COMMIT;` may appear only as a comment.
6. For DROP TABLE, offer `ALTER TABLE x RENAME TO x_deprecated_<date>` as the
   alternative and keep the original as a comment.
7. Above 100,000 measured rows, include a commented batching template.
8. Every change carries a `-- [SafeMigrationGate] SG0NN: <reason>` comment.
"""

_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_WHITESPACE = re.compile(r"\s+")


class InvariantReport(TypedDict):
    validity: bool
    no_omissions: bool
    invariant_held: bool
    reasons: list[str]


def normalize(sql: str) -> str:
    """Collapse whitespace and case so that two spellings of the same statement
    compare equal."""
    return _WHITESPACE.sub(" ", sql).strip().lower()


def _executable_text(sql: str) -> str:
    """Comments removed and string literals blanked, so that a COMMIT mentioned
    inside a comment or a string is not mistaken for an executed COMMIT."""
    return _STRING_LITERAL.sub("''", strip_comments(sql))


def check_invariant(
    safe_sql: str, statements: list[Statement], dialect: str
) -> InvariantReport:
    """Three deterministic checks: does it parse, is anything missing, and does
    running it change nothing."""
    reasons: list[str] = []

    # 1. validity
    validity = True
    try:
        sqlglot.parse(safe_sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
    except Exception as exc:
        validity = False
        reasons.append(f"safe_sql does not parse as {dialect}: {exc}")

    # 2. no omissions -- present either as an executable statement or as a comment
    haystack = normalize(safe_sql)
    missing = [s["text"].strip() for s in statements
               if normalize(s["text"]) not in haystack]
    no_omissions = not missing
    for text in missing:
        reasons.append(f"the original statement is missing from safe_sql: {text}")

    # 3. the invariant
    executable = _executable_text(safe_sql)
    invariant_held = True

    if re.search(r"\bCOMMIT\b", executable, re.IGNORECASE):
        invariant_held = False
        reasons.append("safe_sql contains an uncommented COMMIT")

    tail = [t for t in re.split(r";", executable) if t.strip()]
    if not tail or not re.match(r"^\s*ROLLBACK\s*$", tail[-1], re.IGNORECASE):
        invariant_held = False
        reasons.append("safe_sql does not end with ROLLBACK as its last statement")

    return InvariantReport(validity=validity, no_omissions=no_omissions,
                           invariant_held=invariant_held, reasons=reasons)


def to_eval_result(report: InvariantReport, equivalence: int) -> EvalResult:
    """passed = validity AND no-omissions AND invariant AND equivalence >= 4."""
    passed = (report["validity"] and report["no_omissions"]
              and report["invariant_held"] and equivalence >= 4)
    return EvalResult(passed=passed, equivalence=equivalence,
                      validity=report["validity"], reasons=list(report["reasons"]))
