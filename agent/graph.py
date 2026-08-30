"""Wiring. Three conditional edges and two approval gates.

Everything here is routing; no analysis lives in this file."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent import nodes
from agent.state import GateState, ImpactRow

MAX_REWRITE_ATTEMPTS = 3


# --- approval gates -----------------------------------------------------------
# These are nodes whose entire body is the interrupt. Resuming a graph re-runs
# the interrupted node from the top, so anything else in the same node would run
# twice. Keeping them empty makes that harmless.

def approve_dry_run(state: GateState) -> dict:
    approved = interrupt({
        "kind": "dry_run",
        "risk_level": state.get("risk_level", "LOW"),
        "findings": state.get("findings") or [],
        "statements": state.get("statements") or [],
        "db_url": state.get("db_url"),
    })
    return {"approved_dry_run": bool(approved), "node_trail": ["approve_dry_run"]}


def approve_write(state: GateState) -> dict:
    from agent.tools import output_paths

    safe_path, report_path = output_paths(state["source_path"])
    approved = interrupt({
        "kind": "write_files",
        "safe_path": str(safe_path),
        "report_path": str(report_path),
        "safe_exists": safe_path.exists(),
        "report_exists": report_path.exists(),
        "impact": state.get("impact") or [],
    })
    return {"approved_write": bool(approved), "node_trail": ["approve_write"]}


def skip_dry_run(state: GateState) -> dict:
    """The user declined measurement. Say so, rather than reporting a number."""
    return {
        "impact": [ImpactRow(statement_index=s["index"], rows_affected=None,
                             confidence="unverified",
                             note="the dry run was declined by the operator")
                   for s in state["statements"]],
        "node_trail": ["skip_dry_run"],
    }


# --- routing ------------------------------------------------------------------

def route_after_guardrail(state: GateState) -> str:
    return "report" if state.get("verdict") == "refused" else "static_analysis"


def gate_blocked(state: GateState) -> str:
    """The gate checks `blocked` and nothing else. A CRITICAL-but-fixable script
    passes through here; that is the whole point of the tool."""
    return "report" if state.get("blocked") else "approve_dry_run"


def route_after_approval_1(state: GateState) -> str:
    return "dry_run_impact" if state.get("approved_dry_run") else "skip_dry_run"


def route_after_evaluate(state: GateState) -> str:
    result = state.get("eval_result") or {}
    if result.get("passed"):
        return "approve_write"
    if state.get("rewrite_attempts", 0) >= MAX_REWRITE_ATTEMPTS:
        return "approve_write"          # written with an "unverified" flag
    return "rewrite_safe"


def route_after_approval_2(state: GateState) -> str:
    return "write_files" if state.get("approved_write") else END


def route_after_report(state: GateState) -> str:
    """A refused or blocked run ends at the report; there is nothing to write.
    Everything else goes on to the write gate, which shows the report first."""
    if state.get("verdict") == "refused" or state.get("blocked"):
        return END
    return "approve_write"


# --- assembly -----------------------------------------------------------------

def build_graph(checkpointer=None):
    """ingest -> guardrail -> static_analysis -> classify_risk
                    |refused        |blocked
                    v               v
                  report          report
       gate passes -> approve_dry_run -> dry_run_impact | skip_dry_run
                   -> rewrite_safe <-> evaluate
                   -> report -> approve_write -> write_files

    report runs before approve_write on purpose: the second approval shows the
    operator the finished report, and only then asks whether to write it.
    """
    graph = StateGraph(GateState)

    graph.add_node("ingest", nodes.ingest)
    graph.add_node("guardrail_input", nodes.guardrail_input)
    graph.add_node("static_analysis", nodes.static_analysis)
    graph.add_node("classify_risk", nodes.classify_risk)
    graph.add_node("approve_dry_run", approve_dry_run)
    graph.add_node("dry_run_impact", nodes.dry_run_impact)
    graph.add_node("skip_dry_run", skip_dry_run)
    graph.add_node("rewrite_safe", nodes.rewrite_safe)
    graph.add_node("evaluate", nodes.evaluate)
    graph.add_node("approve_write", approve_write)
    graph.add_node("write_files", nodes.write_files)
    graph.add_node("report", nodes.report)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "guardrail_input")

    graph.add_conditional_edges("guardrail_input", route_after_guardrail,
                                {"report": "report",
                                 "static_analysis": "static_analysis"})
    graph.add_edge("static_analysis", "classify_risk")
    graph.add_conditional_edges("classify_risk", gate_blocked,
                                {"report": "report",
                                 "approve_dry_run": "approve_dry_run"})
    graph.add_conditional_edges("approve_dry_run", route_after_approval_1,
                                {"dry_run_impact": "dry_run_impact",
                                 "skip_dry_run": "skip_dry_run"})
    graph.add_edge("dry_run_impact", "rewrite_safe")
    graph.add_edge("skip_dry_run", "rewrite_safe")
    graph.add_edge("rewrite_safe", "evaluate")

    # evaluate -> report -> approve_write, so the operator approves a report they
    # have actually seen. `route_after_evaluate` names "approve_write" as its
    # destination label; the edge map sends that label to `report` first.
    graph.add_conditional_edges("evaluate", route_after_evaluate,
                                {"rewrite_safe": "rewrite_safe",
                                 "approve_write": "report"})
    graph.add_conditional_edges("report", route_after_report,
                                {END: END, "approve_write": "approve_write"})
    graph.add_conditional_edges("approve_write", route_after_approval_2,
                                {"write_files": "write_files", END: END})
    graph.add_edge("write_files", END)

    return graph.compile(checkpointer=checkpointer)
