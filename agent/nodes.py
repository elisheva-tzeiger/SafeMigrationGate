"""The graph's nodes.

Nodes never print and never read input. Streaming happens outside, via
graph.stream; human approval happens via interrupt(). That is what lets the CLI
and the desktop app consume the exact same graph.
"""

from __future__ import annotations

from pathlib import Path

from agent import tools
from agent.rules import (
    apply_measured_severity,
    is_blocked,
    overall_risk,
    run_rules,
)
from agent.sqlparse import ParseFailure, resolve_dialect, split_statements, to_dialect
from agent.state import ErrorEntry, GateState, ImpactRow


class IngestFailure(Exception):
    """The script could not be read or parsed. This one does stop the graph:
    with no parse there is nothing to make claims about."""


def ingest(state: GateState) -> dict:
    path = Path(state["source_path"])
    try:
        raw_sql = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IngestFailure(f"could not read {path}: {exc}") from exc

    if not raw_sql.strip():
        raise IngestFailure(f"{path} is empty")

    try:
        dialect = resolve_dialect(raw_sql, hint=state.get("dialect") or None)
        statements = split_statements(raw_sql, dialect)
    except ParseFailure as exc:
        raise IngestFailure(str(exc)) from exc

    return {"raw_sql": raw_sql, "dialect": dialect, "statements": statements,
            "node_trail": ["ingest"]}


def static_analysis(state: GateState) -> dict:
    findings = run_rules(state["statements"], state["dialect"])
    return {
        "findings": findings,
        "risk_level": overall_risk(findings),
        # blocked is decided here, deterministically, from SG014/SG015 alone.
        # The LLM classifies severity; it does not decide what is blocked.
        "blocked": is_blocked(findings),
        "cited_rules": tools.lookup_rules([f["rule_id"] for f in findings]),
        "node_trail": ["static_analysis"],
    }


def _unverified(state: GateState, note: str) -> list[ImpactRow]:
    return [ImpactRow(statement_index=s["index"], rows_affected=None,
                      confidence="unverified", note=note)
            for s in state["statements"]]


def dry_run_impact(state: GateState) -> dict:
    """Measure, never estimate. On failure every row becomes unverified and the
    graph continues -- the report will open with a warning."""
    db_url = state.get("db_url")
    dialect = state["dialect"]
    statements = state["statements"]

    if db_url:
        target_dialect, confidence = dialect, "measured_target"
        executable = [s["text"] for s in statements]
    else:
        # The bundled demo is SQLite. A number measured on a translated statement
        # against a demo database is not a number measured against the target,
        # and the tag says so.
        target_dialect = "sqlite"
        confidence = ("measured_target" if dialect == "sqlite"
                      else "measured_demo_transpiled")
        try:
            executable = [to_dialect(s["text"], read=dialect, write="sqlite")
                          for s in statements]
        except Exception as exc:
            return {
                "impact": _unverified(state, f"could not translate to sqlite: {exc}"),
                "errors": [ErrorEntry(node="dry_run_impact", kind="internal",
                                      message=str(exc))],
                "node_trail": ["dry_run_impact"],
            }

    try:
        measured = tools.measure_impact(executable, db_url)
    except Exception as exc:
        return {
            "impact": _unverified(state, "the database could not be reached"),
            "errors": [ErrorEntry(node="dry_run_impact", kind="internal",
                                  message=str(exc))],
            "node_trail": ["dry_run_impact"],
        }

    impact = [
        ImpactRow(
            statement_index=row["index"],
            rows_affected=row["rows_affected"],
            confidence="unverified" if row["error"] else confidence,
            note=row["error"] or "",
        )
        for row in measured
    ]

    findings = apply_measured_severity(state["findings"], statements, impact)
    return {
        "impact": impact,
        "findings": findings,
        "risk_level": overall_risk(findings),
        "node_trail": ["dry_run_impact"],
    }


def write_files(state: GateState) -> dict:
    written = tools.save_outputs(state["source_path"], state.get("safe_sql"),
                                 state.get("report_md"))
    return {"written_files": written, "node_trail": ["write_files"]}
