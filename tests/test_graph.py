import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent import nodes
from agent.graph import (
    MAX_REWRITE_ATTEMPTS,
    build_graph,
    gate_blocked,
    route_after_evaluate,
    route_after_guardrail,
)
from agent.state import initial_state


# --- routing functions in isolation -------------------------------------------

def test_a_refused_input_routes_straight_to_the_report():
    assert route_after_guardrail({"verdict": "refused"}) == "report"


def test_an_accepted_input_routes_to_static_analysis():
    assert route_after_guardrail({"verdict": "proceed"}) == "static_analysis"


def test_a_blocked_script_routes_to_the_report_and_never_touches_the_database():
    """A blocked script is not measured. Not even inside a transaction."""
    assert gate_blocked({"blocked": True}) == "report"


def test_an_unblocked_script_routes_to_the_approval_gate():
    assert gate_blocked({"blocked": False}) == "approve_dry_run"


def test_a_failed_evaluation_loops_back_to_rewrite():
    state = {"eval_result": {"passed": False}, "rewrite_attempts": 1}
    assert route_after_evaluate(state) == "rewrite_safe"


def test_a_passing_evaluation_moves_on():
    state = {"eval_result": {"passed": True}, "rewrite_attempts": 1}
    assert route_after_evaluate(state) == "approve_write"


def test_the_rewrite_loop_is_bounded():
    state = {"eval_result": {"passed": False},
             "rewrite_attempts": MAX_REWRITE_ATTEMPTS}
    assert route_after_evaluate(state) == "approve_write"


# --- the compiled graph --------------------------------------------------------

@pytest.fixture()
def script(tmp_path):
    path = tmp_path / "cleanup.sql"
    path.write_text("DELETE FROM users;\n", encoding="utf-8")
    return path


@pytest.fixture()
def scripted_model(monkeypatch):
    """Feed the graph a fixed sequence of model replies, in node order."""
    from langchain_core.language_models import FakeListChatModel

    def install(*responses: str):
        model = FakeListChatModel(responses=list(responses))
        monkeypatch.setattr(nodes, "_llm", lambda *a, **k: model)
    return install


@pytest.fixture()
def demo_db(monkeypatch, tmp_path):
    from data.seed import build_demo_db

    db = build_demo_db(tmp_path / "demo.db", large_rows=10)
    monkeypatch.setattr("agent.tools.resources.demo_db_url", lambda: f"sqlite:///{db}")
    return db


def run(graph, state, thread: str):
    config = {"configurable": {"thread_id": thread}}
    seen = list(graph.stream(state, config=config, stream_mode="updates"))
    return config, seen


def test_a_blocked_script_reaches_the_report_without_a_dry_run(scripted_model, tmp_path):
    scripted_model('{"verdict": "proceed", "reason": ""}',
                   '{"summary": "drops the whole database", "per_finding": []}',
                   "# Risk report\nblocked")
    path = tmp_path / "nuke.sql"
    path.write_text("DROP DATABASE prod;\n", encoding="utf-8")

    graph = build_graph(checkpointer=MemorySaver())
    config, _ = run(graph, initial_state(str(path), "", "", None), "blocked")
    final = graph.get_state(config).values

    assert final["blocked"] is True
    assert "dry_run_impact" not in final["node_trail"]
    assert final["report_md"]


def test_the_graph_stops_at_the_first_approval_gate(scripted_model, script, demo_db):
    scripted_model('{"verdict": "proceed", "reason": ""}',
                   '{"summary": "empties users", "per_finding": []}')

    graph = build_graph(checkpointer=MemorySaver())
    config, _ = run(graph, initial_state(str(script), "", "", None), "hitl1")

    interrupts = graph.get_state(config).interrupts
    assert len(interrupts) == 1
    assert interrupts[0].value["kind"] == "dry_run"


def test_declining_the_dry_run_continues_with_unverified_impact(
        scripted_model, script, demo_db):
    scripted_model('{"verdict": "proceed", "reason": ""}',
                   '{"summary": "empties users", "per_finding": []}',
                   "BEGIN;\n-- [SafeMigrationGate] SG001: neutralised.\n-- DELETE FROM users;\nROLLBACK;\n-- COMMIT;\n",
                   '{"equivalence": 5, "reasons": []}',
                   "# Risk report\nbody")

    graph = build_graph(checkpointer=MemorySaver())
    config, _ = run(graph, initial_state(str(script), "", "", None), "decline1")
    list(graph.stream(Command(resume=False), config=config, stream_mode="updates"))

    final = graph.get_state(config).values
    assert "dry_run_impact" not in final["node_trail"]
    assert all(r["confidence"] == "unverified" for r in final["impact"])


def test_declining_the_write_leaves_the_disk_untouched(
        scripted_model, script, demo_db):
    scripted_model('{"verdict": "proceed", "reason": ""}',
                   '{"summary": "empties users", "per_finding": []}',
                   "BEGIN;\n-- [SafeMigrationGate] SG001: neutralised.\n-- DELETE FROM users;\nROLLBACK;\n-- COMMIT;\n",
                   '{"equivalence": 5, "reasons": []}',
                   "# Risk report\nbody")

    graph = build_graph(checkpointer=MemorySaver())
    config, _ = run(graph, initial_state(str(script), "", "", None), "decline2")
    list(graph.stream(Command(resume=True), config=config, stream_mode="updates"))
    list(graph.stream(Command(resume=False), config=config, stream_mode="updates"))

    final = graph.get_state(config).values
    assert final["written_files"] == []
    assert not (script.parent / "cleanup_safe.sql").exists()


def test_the_happy_path_writes_both_files(scripted_model, script, demo_db):
    scripted_model('{"verdict": "proceed", "reason": ""}',
                   '{"summary": "empties users", "per_finding": []}',
                   "BEGIN;\n-- [SafeMigrationGate] SG001: neutralised.\n-- DELETE FROM users;\nROLLBACK;\n-- COMMIT;\n",
                   '{"equivalence": 5, "reasons": []}',
                   "# Risk report\nbody")

    graph = build_graph(checkpointer=MemorySaver())
    config, _ = run(graph, initial_state(str(script), "", "", None), "happy")
    list(graph.stream(Command(resume=True), config=config, stream_mode="updates"))
    list(graph.stream(Command(resume=True), config=config, stream_mode="updates"))

    final = graph.get_state(config).values
    assert len(final["written_files"]) == 2
    assert (script.parent / "cleanup_safe.sql").exists()
    assert (script.parent / "cleanup_report.md").exists()
    assert final["node_trail"][0] == "ingest"
    assert final["node_trail"][-1] == "write_files"
    assert final["node_trail"].index("report") < \
        final["node_trail"].index("write_files"), "approve the report you can see"


def test_a_rejected_rewrite_is_retried_then_accepted(scripted_model, script, demo_db):
    bad = "BEGIN;\n-- DELETE FROM users;\nCOMMIT;\n"     # active COMMIT
    good = "BEGIN;\n-- [SafeMigrationGate] SG001: neutralised.\n-- DELETE FROM users;\nROLLBACK;\n-- COMMIT;\n"
    scripted_model('{"verdict": "proceed", "reason": ""}',
                   '{"summary": "empties users", "per_finding": []}',
                   bad, '{"equivalence": 5, "reasons": []}',
                   good, '{"equivalence": 5, "reasons": []}',
                   "# Risk report\nbody")

    graph = build_graph(checkpointer=MemorySaver())
    config, _ = run(graph, initial_state(str(script), "", "", None), "retry")
    list(graph.stream(Command(resume=True), config=config, stream_mode="updates"))
    list(graph.stream(Command(resume=True), config=config, stream_mode="updates"))

    final = graph.get_state(config).values
    assert final["rewrite_attempts"] == 2
    assert final["eval_result"]["passed"] is True


def test_the_stream_emits_one_update_per_node(scripted_model, tmp_path):
    scripted_model('{"verdict": "proceed", "reason": ""}',
                   '{"summary": "drops the whole database", "per_finding": []}',
                   "# Risk report\nblocked")
    path = tmp_path / "nuke.sql"
    path.write_text("DROP DATABASE prod;\n", encoding="utf-8")

    graph = build_graph(checkpointer=MemorySaver())
    _, seen = run(graph, initial_state(str(path), "", "", None), "stream")

    names = [name for chunk in seen for name in chunk if not name.startswith("__")]
    assert names[:3] == ["ingest", "guardrail_input", "static_analysis"]
