"""The model's output is checked by code, not trusted.

The report is free text and the safe script is generated SQL, so the promises the
tool makes about them -- no COMMIT, every number measured, the rewrite contract
followed -- are enforced here against deliberately bad model output.
"""

import json

import pytest
from langchain_core.language_models import FakeListChatModel

from agent import nodes
from agent.rewrite import check_invariant
from agent.sqlparse import split_statements
from agent.state import initial_state


@pytest.fixture()
def fake(monkeypatch):
    def install(*responses: str):
        model = FakeListChatModel(responses=list(responses))
        monkeypatch.setattr(nodes, "_llm", lambda *a, **k: model)
    return install


def state_for(sql: str):
    state = initial_state("x.sql", sql, "postgres", None)
    state["statements"] = split_statements(sql, "postgres")
    return state


MEASURED = [{"statement_index": 0, "rows_affected": 500,
             "confidence": "measured_demo_transpiled", "note": ""}]


# --- the report ---------------------------------------------------------------

def test_a_report_that_tells_the_reader_to_commit_is_replaced(fake):
    fake("# Risk report\nRun BEGIN; ... COMMIT; to apply. 500 rows were measured.")
    state = state_for("DELETE FROM users;")
    state["impact"] = MEASURED
    update = nodes.report(state)
    assert "COMMIT;" not in update["report_md"]
    assert update["errors"][0]["node"] == "report"
    assert "COMMIT" in update["errors"][0]["message"]


def test_a_report_with_an_invented_row_count_is_replaced(fake):
    fake("# Risk report\nThis deletes about 9,000 rows. Measured: 500 rows.")
    state = state_for("DELETE FROM users;")
    state["impact"] = MEASURED
    update = nodes.report(state)
    assert "9,000" not in update["report_md"]
    assert "never measured" in update["errors"][0]["message"]


def test_a_report_that_drops_a_measured_count_is_replaced(fake):
    fake("# Risk report\nThis removes everything in the table.")
    state = state_for("DELETE FROM users;")
    state["impact"] = MEASURED
    update = nodes.report(state)
    assert "missing" in update["errors"][0]["message"]


def test_a_truthful_report_is_kept(fake):
    fake("# Risk report\n| 0 | 500 rows | measured_demo_transpiled |\n"
         "This tool never commits.")
    state = state_for("DELETE FROM users;")
    state["impact"] = MEASURED
    update = nodes.report(state)
    assert update["report_md"].startswith("# Risk report")
    assert "errors" not in update


def test_the_word_commit_in_prose_is_not_a_commit_statement():
    assert nodes.validate_report("Nothing here will commit anything.", []) == []


# --- the rewrite contract -----------------------------------------------------

DELETE = "DELETE FROM users;"
WITH_COUNT = ("BEGIN;\n-- [SafeMigrationGate] SG001: scoped.\n"
              "SELECT COUNT(*) FROM users WHERE /* FILL: condition */ 1 = 0;\n"
              "DELETE FROM users WHERE /* FILL: condition */ 1 = 0;\nROLLBACK;\n"
              "-- DELETE FROM users;\n")


def report_for(safe_sql: str, original: str = DELETE):
    return check_invariant(safe_sql, split_statements(original, "postgres"),
                           "postgres")


def test_a_destructive_statement_with_a_count_before_it_passes():
    assert report_for(WITH_COUNT)["contract_followed"] is True


def test_a_destructive_statement_without_a_count_fails():
    safe = WITH_COUNT.replace(
        "SELECT COUNT(*) FROM users WHERE /* FILL: condition */ 1 = 0;\n", "")
    report = report_for(safe)
    assert report["contract_followed"] is False
    assert any("SELECT COUNT" in r for r in report["reasons"])


def test_a_rewrite_with_no_reason_comment_fails():
    safe = WITH_COUNT.replace("-- [SafeMigrationGate] SG001: scoped.\n", "")
    report = report_for(safe)
    assert report["contract_followed"] is False
    assert any("SafeMigrationGate" in r for r in report["reasons"])


def test_the_inert_fallback_script_follows_the_contract():
    from agent.rewrite import fallback_safe_sql
    statements = split_statements(DELETE, "postgres")
    safe = fallback_safe_sql(statements, "model unavailable")
    # the fallback carries its reason in prose, so it must also carry the marker
    assert check_invariant(safe, statements, "postgres")["contract_followed"] is True


# --- the equivalence score ----------------------------------------------------

@pytest.mark.parametrize("score", [0, 7, -1])
def test_an_out_of_range_equivalence_score_is_not_accepted(fake, score):
    fake(json.dumps({"equivalence": score, "reasons": []}))
    state = state_for(DELETE)
    state["safe_sql"] = WITH_COUNT
    update = nodes.evaluate(state)
    assert update["eval_result"]["equivalence"] == 0
    assert update["errors"][0]["node"] == "evaluate"
    assert "outside 1-5" in update["errors"][0]["message"]
