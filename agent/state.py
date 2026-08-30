"""The single shared contract. Every node reads from and writes to GateState;
nodes never talk to each other directly."""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

Severity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
Confidence = Literal["measured_target", "measured_demo_transpiled", "unverified"]
Verdict = Literal["proceed", "refused"]
ErrorKind = Literal["user", "provider", "internal"]

SEVERITY_ORDER: dict[str, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def max_severity(severities: list[str]) -> Severity:
    """Highest severity in the list. An empty list means no findings, hence LOW."""
    if not severities:
        return "LOW"
    return max(severities, key=lambda s: SEVERITY_ORDER[s])  # type: ignore[return-value]


class Statement(TypedDict):
    index: int
    text: str
    kind: str                      # "DELETE" | "UPDATE" | "DROP" | "ALTER" | ...
    target_tables: list[str]
    has_where: bool
    where_text: str | None


class Finding(TypedDict):
    statement_index: int
    rule_id: str
    severity: Severity
    message: str


class RuleRef(TypedDict):
    rule_id: str
    title: str
    source_doc: str


class ImpactRow(TypedDict):
    statement_index: int
    rows_affected: int | None      # None = not measurable (DDL) or not measured
    confidence: Confidence
    note: str


class EvalResult(TypedDict):
    passed: bool
    equivalence: int               # 1..5, 0 when the LLM judge did not run
    validity: bool
    reasons: list[str]


class ErrorEntry(TypedDict):
    node: str
    kind: ErrorKind
    message: str


class GateState(TypedDict):
    # input
    source_path: str
    raw_sql: str
    dialect: str
    db_url: str | None             # None -> the bundled SQLite demo

    # guardrail
    verdict: Verdict
    refusal_reason: str | None

    # analysis
    statements: list[Statement]
    findings: list[Finding]
    risk_level: Severity
    blocked: bool
    cited_rules: list[RuleRef]

    # impact
    impact: list[ImpactRow]

    # classification — written by classify_risk, read by report
    classification: str

    # rewrite
    safe_sql: str | None
    eval_result: EvalResult | None
    rewrite_attempts: int

    # approval gates
    approved_dry_run: bool
    approved_write: bool

    # output
    report_md: str | None
    written_files: list[str]

    # operational — these two accumulate across nodes, hence the reducers
    node_trail: Annotated[list[str], operator.add]
    errors: Annotated[list[ErrorEntry], operator.add]


def initial_state(
    source_path: str, raw_sql: str, dialect: str, db_url: str | None
) -> GateState:
    return GateState(
        source_path=source_path,
        raw_sql=raw_sql,
        dialect=dialect,
        db_url=db_url,
        verdict="proceed",
        refusal_reason=None,
        statements=[],
        findings=[],
        risk_level="LOW",
        blocked=False,
        cited_rules=[],
        impact=[],
        classification="",
        safe_sql=None,
        eval_result=None,
        rewrite_attempts=0,
        approved_dry_run=False,
        approved_write=False,
        report_md=None,
        written_files=[],
        node_trail=[],
        errors=[],
    )
