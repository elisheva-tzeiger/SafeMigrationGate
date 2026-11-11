"""The rewrite contract, and the validator that enforces its central invariant:

    the safe script, run as-is, changes nothing.

That claim is checkable, so it is checked rather than assumed."""

from __future__ import annotations

import re
from typing import TypedDict

import sqlglot

from agent.sqlparse import (
    classify_transaction_control,
    split_segments,
    statement_kind,
    strip_comments,
)
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
# Strip only leading line-comment markers so that multi-line originals that
# were commented out do not lose the token that joins adjacent words.
# A trailing `-- note` after real code is intentionally left untouched.
_LEADING_COMMENT_MARKER = re.compile(r"(?m)^\s*--\s?")


class InvariantReport(TypedDict):
    validity: bool
    no_omissions: bool
    invariant_held: bool
    contract_followed: bool
    reasons: list[str]


_COUNT_SELECT = re.compile(r"^\s*select\s+count\s*\(", re.IGNORECASE)
_MARKER = "[SafeMigrationGate]"
_COUNTED_KINDS = frozenset({"DELETE", "UPDATE", "TRUNCATE"})
_CHANGING_KINDS = _COUNTED_KINDS | {"DROP", "ALTER"}


def _check_contract(safe_sql: str, statements: list[Statement],
                    dialect: str) -> list[str]:
    """The parts of the rewrite contract that code can verify, rather than trust.

    Rule 4: every executable DELETE, UPDATE or TRUNCATE is immediately preceded by
    a SELECT COUNT(*). Rule 8: a script whose original contained a destructive
    statement must carry at least one [SafeMigrationGate] reason comment.
    Commented-out statements are not executable, so the inert fallback script
    satisfies both by construction.
    """
    reasons: list[str] = []
    previous = ""
    for segment in split_segments(_executable_text(safe_sql)):
        text = segment.strip()
        if not text:
            continue
        if (statement_kind(text, dialect) in _COUNTED_KINDS
                and not _COUNT_SELECT.match(previous)):
            reasons.append("a destructive statement has no SELECT COUNT(*) "
                           f"immediately before it: {text[:80]}")
        previous = text

    destructive = any(statement_kind(s["text"], dialect) in _CHANGING_KINDS
                      for s in statements)
    if destructive and _MARKER not in safe_sql:
        reasons.append(f"no {_MARKER} comment explains any change")
    return reasons


def normalize(sql: str) -> str:
    """Collapse whitespace and case so that two spellings of the same statement
    compare equal."""
    return _WHITESPACE.sub(" ", sql).strip().lower()


def _executable_text(sql: str) -> str:
    """Comments removed and string literals blanked, so that a COMMIT mentioned
    inside a comment or a string is not mistaken for an executed COMMIT."""
    return _STRING_LITERAL.sub("''", strip_comments(sql))


# Statement kinds that change data or schema. A safe script may contain these
# only inside a transaction that is rolled back.
_MODIFYING_KINDS: frozenset[str] = frozenset({
    "DELETE", "UPDATE", "INSERT", "DROP", "TRUNCATE", "ALTER", "CREATE",
    "GRANT", "REVOKE", "MERGE",
})


def _check_rollback_invariant(safe_sql: str, dialect: str) -> tuple[bool, list[str]]:
    """Enforce the real invariant: running safe_sql as-is changes nothing.

    "Ends with ROLLBACK" is not that invariant. It is satisfied by a script whose
    *last* statement is a ROLLBACK while an earlier statement already ran outside
    any transaction and persisted. So instead of looking at the tail, walk the
    statements in order and track transaction depth:

      * a data-modifying statement at depth 0 has nothing to roll it back;
      * a COMMIT (or its END synonym) makes everything before it permanent;
      * the script must not end with modifications still pending in an open
        transaction that was never rolled back.

    Comments and string literals are removed first, so a COMMIT that is merely
    *mentioned* in a comment or held in a string literal is still allowed.
    """
    reasons: list[str] = []
    executable = _executable_text(safe_sql)

    depth = 0
    modified_in_open_transaction = False

    for segment in split_segments(executable):
        text = segment.strip()
        if not text:
            continue

        is_txn, is_commit = classify_transaction_control(text, dialect)

        if is_commit:
            # COMMIT / END / RELEASE — makes pending work permanent.
            reasons.append(
                "safe_sql contains an uncommented COMMIT (or its END synonym), "
                "which would make the preceding statements permanent")
            return False, reasons

        if is_txn:
            words = text.split()
            first = words[0].upper() if words else ""
            if first in {"BEGIN", "START"}:
                depth += 1
            elif first == "ROLLBACK":
                # A ROLLBACK undoes everything back to the matching BEGIN.
                depth = max(0, depth - 1)
                if depth == 0:
                    modified_in_open_transaction = False
            continue

        kind = statement_kind(text, dialect)
        if kind in _MODIFYING_KINDS:
            if depth == 0:
                reasons.append(
                    "safe_sql modifies data outside any open transaction, so no "
                    f"ROLLBACK can undo it: {text.strip()[:120]}")
                return False, reasons
            modified_in_open_transaction = True

    if depth > 0 and modified_in_open_transaction:
        reasons.append(
            "safe_sql leaves a transaction open with pending modifications and "
            "no ROLLBACK to undo them")
        return False, reasons

    return True, reasons


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

    # 2. no omissions -- present either as an executable statement or as a comment.
    # Strip leading line-comment markers before building the haystack so that a
    # multi-line original that was commented out (rule 3 of the rewrite contract)
    # matches even though its continuation lines each carry a "-- " prefix.
    haystack = normalize(_LEADING_COMMENT_MARKER.sub("", safe_sql))
    missing = [s["text"].strip() for s in statements
               if normalize(s["text"]) not in haystack]
    no_omissions = not missing
    for text in missing:
        reasons.append(f"the original statement is missing from safe_sql: {text}")

    # 3. the invariant
    invariant_held, invariant_reasons = _check_rollback_invariant(safe_sql, dialect)
    reasons.extend(invariant_reasons)

    # 4. the contract rules that can be checked mechanically
    contract_reasons = _check_contract(safe_sql, statements, dialect) if validity else []
    reasons.extend(contract_reasons)

    return InvariantReport(validity=validity, no_omissions=no_omissions,
                           invariant_held=invariant_held,
                           contract_followed=not contract_reasons, reasons=reasons)


def to_eval_result(report: InvariantReport, equivalence: int) -> EvalResult:
    """passed = validity AND no-omissions AND invariant AND contract AND
    equivalence >= 4."""
    passed = (report["validity"] and report["no_omissions"]
              and report["invariant_held"]
              and report.get("contract_followed", True) and equivalence >= 4)
    return EvalResult(passed=passed, equivalence=equivalence,
                      validity=report["validity"], reasons=list(report["reasons"]))


def fallback_safe_sql(statements: list[Statement], reason: str) -> str:
    """A safe script produced without the model.

    Every original statement is preserved as a comment inside a transaction that
    rolls back, so the invariant holds by construction. It is deliberately inert:
    when the model is unavailable, the honest output is "here is your script,
    disarmed" rather than a guess at what you meant.
    """
    lines = [
        "BEGIN;",
        f"-- [SafeMigrationGate] the rewrite model was not available: {reason}",
        "-- Every original statement is preserved below, commented out and unmodified.",
        "-- Review each one by hand before enabling it.",
        "",
    ]
    for statement in statements:
        for line in statement["text"].strip().splitlines():
            lines.append(f"-- {line}")
        lines.append("")
    lines += ["ROLLBACK;", "-- COMMIT;", ""]
    return "\n".join(lines)
