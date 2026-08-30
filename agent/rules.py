"""SG001-SG015 over the sqlglot AST.

Detection is deterministic on purpose. A security tool that relies on a
probabilistic model to notice DROP TABLE is a tool that fails.

Statements carry their text, not their AST, because GateState must stay
serialisable for the checkpointer. Re-parsing here is cheap and keeps the
contract clean.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from agent.state import Finding, ImpactRow, Severity, Statement, max_severity

BLOCKING_RULES: frozenset[str] = frozenset({"SG014", "SG015"})
LARGE_SCOPE_ROWS = 100_000

RULE_TITLES: dict[str, str] = {
    "SG001": "DELETE without WHERE",
    "SG002": "UPDATE without WHERE",
    "SG003": "Tautological WHERE clause",
    "SG004": "DROP TABLE",
    "SG005": "TRUNCATE",
    "SG006": "ALTER TABLE ... DROP COLUMN",
    "SG007": "Column type change",
    "SG008": "UPDATE on a key column",
    "SG009": "Explicit CASCADE",
    "SG010": "WHERE depends on an unbounded subquery",
    "SG011": "Script is not wrapped in a transaction",
    "SG012": "DDL and DML mixed in one script",
    "SG013": "Large-scope DML without LIMIT or batching",
    "SG014": "DROP DATABASE / DROP SCHEMA",
    "SG015": "Privilege change",
}

RULE_SEVERITY: dict[str, Severity] = {
    "SG001": "CRITICAL", "SG002": "CRITICAL", "SG003": "CRITICAL",
    "SG004": "CRITICAL", "SG005": "CRITICAL", "SG006": "HIGH",
    "SG007": "HIGH", "SG008": "HIGH", "SG009": "HIGH",
    "SG010": "MEDIUM", "SG011": "MEDIUM", "SG012": "MEDIUM",
    "SG013": "HIGH", "SG014": "CRITICAL", "SG015": "HIGH",
}

DDL_KINDS = frozenset({"DROP", "ALTER", "CREATE", "TRUNCATE"})
DML_KINDS = frozenset({"DELETE", "UPDATE", "INSERT"})

# sqlglot does not parse CREATE USER / ALTER ROLE; it returns a generic Command
# node. This regex runs only on Command nodes -- i.e. only after sqlglot itself
# has reported that it gave up. It is the single documented regex in the ruleset.
_PRIVILEGE_COMMAND = re.compile(
    r"^\s*(CREATE|ALTER|DROP)\s+(USER|ROLE|LOGIN)\b", re.IGNORECASE
)

_KEY_COLUMN_NAMES = frozenset({"id", "uuid", "guid", "pk"})


def _finding(index: int, rule_id: str, message: str) -> Finding:
    return Finding(statement_index=index, rule_id=rule_id,
                   severity=RULE_SEVERITY[rule_id], message=message)


def _is_tautology(node: exp.Expression | None) -> bool:
    """True when the condition is always true regardless of the data.

    'WHERE 1=1 AND id=7' is NOT a tautology -- id=7 bounds it -- so AND requires
    every branch to be tautological, while OR requires only one.
    """
    if node is None:
        return False
    if isinstance(node, exp.Paren):
        return _is_tautology(node.this)
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.EQ):
        left, right = node.left, node.right
        return (isinstance(left, exp.Literal) and isinstance(right, exp.Literal)
                and left.sql() == right.sql())
    if isinstance(node, exp.And):
        return _is_tautology(node.left) and _is_tautology(node.right)
    if isinstance(node, exp.Or):
        return _is_tautology(node.left) or _is_tautology(node.right)
    return False


def _looks_like_a_key(column_name: str) -> bool:
    name = column_name.lower()
    return name in _KEY_COLUMN_NAMES or name.endswith("_id")


def _statement_rules(
    statement: Statement, expression: exp.Expression, dialect: str
) -> list[Finding]:
    index = statement["index"]
    found: list[Finding] = []
    where = expression.args.get("where")

    if isinstance(expression, exp.Delete) and where is None:
        found.append(_finding(index, "SG001",
                              "DELETE with no WHERE clause removes every row in "
                              f"{', '.join(statement['target_tables']) or 'the table'}."))

    if isinstance(expression, exp.Update) and where is None:
        found.append(_finding(index, "SG002",
                              "UPDATE with no WHERE clause rewrites every row in "
                              f"{', '.join(statement['target_tables']) or 'the table'}."))

    if where is not None and _is_tautology(where.this):
        found.append(_finding(index, "SG003",
                              f"The WHERE clause '{where.this.sql(dialect=dialect)}' is "
                              "always true, so it filters nothing."))

    if isinstance(expression, exp.Drop):
        kind = str(expression.args.get("kind") or "").upper()
        if kind == "TABLE":
            found.append(_finding(index, "SG004",
                                  "DROP TABLE destroys the table and all of its data."))
        elif kind in {"DATABASE", "SCHEMA"}:
            found.append(_finding(index, "SG014",
                                  f"DROP {kind} has no safe rewrite. This script is blocked."))

    if isinstance(expression, exp.TruncateTable):
        found.append(_finding(index, "SG005",
                              "TRUNCATE empties the table and is often not logged "
                              "row by row, which limits recovery."))

    if isinstance(expression, exp.Alter):
        for action in expression.args.get("actions") or []:
            if isinstance(action, exp.Drop) and \
                    str(action.args.get("kind") or "").upper() == "COLUMN":
                found.append(_finding(index, "SG006",
                                      "DROP COLUMN discards the column's data permanently."))
            if isinstance(action, exp.AlterColumn) and action.args.get("dtype") is not None:
                new_type = action.args["dtype"].sql(dialect=dialect)
                found.append(_finding(index, "SG007",
                                      f"The column type is changed to {new_type}; without "
                                      "schema access the current type was not compared, so "
                                      "narrowing and truncation cannot be ruled out."))

    if isinstance(expression, exp.Update):
        for assignment in expression.args.get("expressions") or []:
            column = assignment.this
            name = column.name if hasattr(column, "name") else str(column)
            if _looks_like_a_key(name):
                found.append(_finding(index, "SG008",
                                      f"'{name}' looks like a key column (matched by name, "
                                      "not by schema). Updating it breaks existing "
                                      "references."))
                break

    if _has_cascade(expression):
        found.append(_finding(index, "SG009",
                              "CASCADE propagates the operation to dependent objects "
                              "that are not named in this script."))

    if where is not None:
        for subquery in where.find_all(exp.Select):
            if subquery.args.get("limit") is None:
                found.append(_finding(index, "SG010",
                                      "The WHERE clause depends on a subquery with no "
                                      "LIMIT, so its scope is whatever that query returns "
                                      "at run time."))
                break

    if isinstance(expression, (exp.Grant, exp.Revoke)):
        found.append(_finding(index, "SG015",
                              "This statement changes access privileges. It is blocked: "
                              "there is no safe rewrite of a permission change."))
    elif isinstance(expression, exp.Command) and _PRIVILEGE_COMMAND.match(statement["text"]):
        found.append(_finding(index, "SG015",
                              "This statement creates or alters a database principal. It "
                              "is blocked: there is no safe rewrite of a permission change."))

    return found


def _has_cascade(expression: exp.Expression) -> bool:
    if expression.args.get("cascade") is True:
        return True
    return any(node.args.get("cascade") is True
               for node in expression.find_all(exp.Expression)
               if node is not expression)


def _script_rules(statements: list[Statement]) -> list[Finding]:
    found: list[Finding] = []
    kinds = {s["kind"] for s in statements}

    opens_transaction = "TRANSACTION" in kinds or any(
        re.match(r"^\s*(START\s+TRANSACTION|BEGIN)\b", s["text"], re.IGNORECASE)
        for s in statements
    )
    if not opens_transaction:
        found.append(_finding(0, "SG011",
                              "The script is not wrapped in a transaction, so a failure "
                              "half-way through leaves the database partially changed."))

    if kinds & DDL_KINDS and kinds & DML_KINDS:
        found.append(_finding(0, "SG012",
                              "The script mixes schema changes with data changes. Many "
                              "engines cannot roll those back together."))

    return found


def run_rules(statements: list[Statement], dialect: str) -> list[Finding]:
    """Every static finding for the script, ordered by statement then rule id."""
    found: list[Finding] = []

    for statement in statements:
        try:
            expression = sqlglot.parse_one(statement["text"], read=dialect)
        except Exception:
            continue
        if expression is None:
            continue
        found.extend(_statement_rules(statement, expression, dialect))

    if statements:
        found.extend(_script_rules(statements))

    return sorted(found, key=lambda f: (f["statement_index"], f["rule_id"]))


def apply_measured_severity(
    findings: list[Finding],
    statements: list[Statement],
    impact: list[ImpactRow],
) -> list[Finding]:
    """Add SG013, the one rule whose severity comes from measurement rather than
    from the AST. If nothing was measured, nothing is claimed."""
    by_index = {s["index"]: s for s in statements}
    result = list(findings)

    for row in impact:
        rows_affected = row["rows_affected"]
        if rows_affected is None or rows_affected <= LARGE_SCOPE_ROWS:
            continue
        if row["confidence"] == "unverified":
            continue
        statement = by_index.get(row["statement_index"])
        if statement is None or statement["kind"] not in DML_KINDS:
            continue
        if re.search(r"\bLIMIT\b|\bTOP\s*\(", statement["text"], re.IGNORECASE):
            continue
        result.append(_finding(
            row["statement_index"], "SG013",
            f"{rows_affected:,} rows measured. Statements this large should be "
            "batched so they can be paused, monitored, and interrupted."))

    return sorted(result, key=lambda f: (f["statement_index"], f["rule_id"]))


def overall_risk(findings: list[Finding]) -> Severity:
    return max_severity([f["severity"] for f in findings])


def is_blocked(findings: list[Finding]) -> bool:
    """The gate checks this and only this. Severity is informational."""
    return any(f["rule_id"] in BLOCKING_RULES for f in findings)
