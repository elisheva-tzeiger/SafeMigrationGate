"""The graph's nodes.

Nodes never print and never read input. Streaming happens outside, via
graph.stream; human approval happens via interrupt(). That is what lets the CLI
and the desktop app consume the exact same graph.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent import prompts, tools
from agent.llm import invoke_json, invoke_text, make_llm
from agent.rewrite import FILL_PLACEHOLDER, check_invariant, fallback_safe_sql, to_eval_result
from agent.rules import (
    apply_measured_severity,
    is_blocked,
    overall_risk,
    run_rules,
)
from agent.sqlparse import ParseFailure, resolve_dialect, split_statements, to_dialect
from agent.state import ErrorEntry, EvalResult, GateState, ImpactRow


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
        confidence = "measured_target"
        executable = [s["text"] for s in statements]
    else:
        # The tag turns on whose database was measured, not on whether a
        # dialect translation happened. With no db_url every statement runs
        # against the bundled demo database, so the confidence is always
        # measured_demo_transpiled — even when the script is already SQLite
        # and no translation is required.
        confidence = "measured_demo_transpiled"
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


# ---------------------------------------------------------------------------
# LLM nodes
# ---------------------------------------------------------------------------

# Instructions aimed at the reader of the script rather than at the database.
# Checked deterministically, because a defence that depends on the model noticing
# an attack on the model is not a defence.
INJECTION_MARKERS: tuple[str, ...] = (
    "ignore all previous", "ignore previous instructions", "disregard the above",
    "you are now", "forget your instructions", "system prompt",
    "output commit", "act as", "jailbreak",
)

_FENCE = re.compile(r"^\s*```(?:sql)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


def _llm(*args, **kwargs):
    """Indirection so tests can swap in FakeListChatModel."""
    return make_llm(*args, **kwargs)


def guardrail_input(state: GateState) -> dict:
    lowered = state["raw_sql"].lower()
    for marker in INJECTION_MARKERS:
        if marker in lowered:
            return {
                "verdict": "refused",
                "refusal_reason": (
                    "The script contains an instruction addressed to the reviewer "
                    f"rather than to the database ({marker!r}). Refusing to analyse it."
                ),
                "node_trail": ["guardrail_input"],
            }

    try:
        answer = invoke_json(_llm(), prompts.GUARDRAIL_SYSTEM,
                             prompts.guardrail_user(state["raw_sql"]),
                             run_name="guardrail_input")
    except Exception as exc:
        # Guardrail failure stops the run. If the input was never vetted, nothing
        # downstream can be trusted.
        return {
            "verdict": "refused",
            "refusal_reason": f"the input could not be vetted: {exc}",
            "errors": [ErrorEntry(node="guardrail_input", kind="provider",
                                  message=str(exc))],
            "node_trail": ["guardrail_input"],
        }

    refused = str(answer.get("verdict", "proceed")).lower() == "refused"
    return {
        "verdict": "refused" if refused else "proceed",
        "refusal_reason": str(answer.get("reason") or "") if refused else None,
        "node_trail": ["guardrail_input"],
    }


def classify_risk(state: GateState) -> dict:
    findings = state["findings"]
    rag_context = ""
    if findings:
        from agent.knowledge import get_knowledge_base

        rag_context = get_knowledge_base().context_for(
            [f["rule_id"] for f in findings])

    deterministic = "\n".join(f"- {f['rule_id']}: {f['message']}" for f in findings) \
        or "No dangerous patterns were detected."

    try:
        answer = invoke_json(
            _llm(), prompts.CLASSIFY_SYSTEM,
            prompts.classify_user(state["statements"], findings, rag_context),
            run_name="classify_risk")
    except Exception as exc:
        return {
            "classification": deterministic,
            "errors": [ErrorEntry(node="classify_risk", kind="provider",
                                  message=str(exc))],
            "node_trail": ["classify_risk"],
        }

    parts = [str(answer.get("summary", "")).strip()]
    for item in answer.get("per_finding", []) or []:
        rule_id = str(item.get("rule_id", "")).strip().upper()
        # The model does not get to introduce rule ids that were never detected.
        if any(f["rule_id"] == rule_id for f in findings):
            parts.append(f"- {rule_id}: {str(item.get('explanation', '')).strip()}")

    return {"classification": "\n".join(p for p in parts if p) or deterministic,
            "node_trail": ["classify_risk"]}


def rewrite_safe(state: GateState) -> dict:
    attempts = state.get("rewrite_attempts", 0) + 1
    previous = state.get("eval_result")
    failure = "; ".join(previous["reasons"]) if previous and not previous["passed"] else None

    try:
        raw = invoke_text(
            _llm(), prompts.REWRITE_SYSTEM,
            prompts.rewrite_user(state["raw_sql"], state["dialect"],
                                 state["findings"], state["impact"], failure),
            run_name="rewrite_safe")
    except Exception as exc:
        return {
            "safe_sql": fallback_safe_sql(state["statements"], str(exc)),
            "rewrite_attempts": attempts,
            "errors": [ErrorEntry(node="rewrite_safe", kind="provider",
                                  message=str(exc))],
            "node_trail": ["rewrite_safe"],
        }

    fence = _FENCE.match(raw)
    return {"safe_sql": (fence.group(1) if fence else raw).strip() + "\n",
            "rewrite_attempts": attempts,
            "node_trail": ["rewrite_safe"]}


def evaluate(state: GateState) -> dict:
    safe_sql = state.get("safe_sql") or ""
    report_ = check_invariant(safe_sql, state["statements"], state["dialect"])

    try:
        answer = invoke_json(
            _llm(), prompts.EQUIVALENCE_SYSTEM,
            prompts.equivalence_user(state["raw_sql"], safe_sql),
            run_name="evaluate")
        equivalence = int(answer.get("equivalence", 0))
        report_["reasons"].extend(str(r) for r in (answer.get("reasons") or []))
        result = to_eval_result(report_, equivalence)
        errors: list[ErrorEntry] = []
    except Exception as exc:
        # Equivalence is reported as 0, not invented, and the deterministic checks
        # decide on their own. Looping three times against a dead provider would
        # cost time and change nothing.
        report_["reasons"].append("semantic equivalence was not scored: the model "
                                  "was unavailable")
        result = EvalResult(
            passed=(report_["validity"] and report_["no_omissions"]
                    and report_["invariant_held"]),
            equivalence=0, validity=report_["validity"],
            reasons=list(report_["reasons"]))
        errors = [ErrorEntry(node="evaluate", kind="provider", message=str(exc))]

    return {"eval_result": result, "errors": errors, "node_trail": ["evaluate"]}


def render_report_fallback(state: GateState) -> str:
    """A report that is always producible, with no model involved."""
    lines = [f"# Risk report — {Path(state['source_path']).name}", ""]
    if state.get("verdict") == "refused":
        lines += ["**This script was refused.**", "",
                  str(state.get("refusal_reason") or ""), ""]
        return "\n".join(lines)

    lines += [f"**Overall risk:** {state.get('risk_level', 'LOW')} | "
              f"**Blocked:** {'yes' if state.get('blocked') else 'no'}", ""]
    impact_rows = state.get("impact") or []
    if impact_rows and not any(r["confidence"] != "unverified" for r in impact_rows):
        lines += ["> **Row counts below were not measured.** No count was verified "
                  "against a database.", ""]

    lines += ["## Findings", "", "| Statement | Rule | Severity | What it means |",
              "|---|---|---|---|"]
    for finding in state.get("findings") or []:
        lines.append(f"| {finding['statement_index']} | {finding['rule_id']} | "
                     f"{finding['severity']} | {finding['message']} |")
    if not state.get("findings"):
        lines.append("| — | — | — | No dangerous patterns detected. |")

    lines += ["", "## Measured impact", "",
              "| Statement | Rows affected | Confidence | Note |", "|---|---|---|---|"]
    for row in state.get("impact") or []:
        count = "not measured" if row["rows_affected"] is None else f"{row['rows_affected']:,}"
        lines.append(f"| {row['statement_index']} | {count} | "
                     f"`{row['confidence']}` | {row['note']} |")
    if not state.get("impact"):
        lines.append("| — | not measured | `unverified` | The dry run did not run. |")

    lines += ["", "## What to do next", "",
              f"Review the safe script, replace every `{FILL_PLACEHOLDER}` "
              "placeholder, and run it yourself. This tool never commits.", ""]
    return "\n".join(lines)


def report(state: GateState) -> dict:
    if state.get("verdict") == "refused":
        return {"report_md": render_report_fallback(state), "node_trail": ["report"]}

    try:
        markdown = invoke_text(
            _llm(), prompts.REPORT_SYSTEM,
            prompts.report_user(
                state["source_path"], state.get("risk_level", "LOW"),
                bool(state.get("blocked")), state.get("classification", ""),
                state.get("findings") or [], state.get("impact") or [],
                list(state.get("cited_rules") or []),
                list(state.get("errors") or [])),
            run_name="report")
    except Exception as exc:
        return {
            "report_md": render_report_fallback(state),
            "errors": [ErrorEntry(node="report", kind="provider", message=str(exc))],
            "node_trail": ["report"],
        }

    if state.get("blocked") and "blocked" not in markdown.lower():
        markdown = "**BLOCKED — this script has no safe version.**\n\n" + markdown
    impact = state.get("impact") or []
    if impact and not any(r["confidence"] != "unverified" for r in impact):
        markdown = ("> **Row counts below were not measured.** No count was verified "
                    "against a database.\n\n") + markdown

    return {"report_md": markdown, "node_trail": ["report"]}
