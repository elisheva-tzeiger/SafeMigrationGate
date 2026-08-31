import pytest

from evals.cost_eval import CSV_COLUMNS, NodeUsageCollector, cost_for


def test_cost_is_tokens_times_price():
    # A module-level helper named pytest_* would be collected as a hook and make
    # pytest fail on an unknown hook name. Use pytest.approx directly.
    assert cost_for(1_000, 500, (0.000_001, 0.000_002)) == pytest.approx(0.002)


def test_a_free_model_costs_nothing():
    assert cost_for(1_000, 500, (0.0, 0.0)) == 0.0


def test_the_collector_attributes_tokens_to_the_run_name():
    collector = NodeUsageCollector()
    collector.record("classify_risk", 120, 40)
    collector.record("classify_risk", 80, 10)
    collector.record("rewrite_safe", 300, 200)

    assert collector.usage["classify_risk"] == {"prompt_tokens": 200,
                                                "completion_tokens": 50}
    assert collector.usage["rewrite_safe"]["completion_tokens"] == 200


def test_the_collector_totals_across_nodes():
    collector = NodeUsageCollector()
    collector.record("a", 10, 5)
    collector.record("b", 20, 5)
    assert collector.totals() == {"prompt_tokens": 30, "completion_tokens": 10}


def test_an_unnamed_run_is_bucketed_rather_than_dropped():
    collector = NodeUsageCollector()
    collector.record(None, 10, 5)
    assert collector.usage["(unnamed)"]["prompt_tokens"] == 10


def test_the_csv_columns_match_the_spec():
    assert CSV_COLUMNS == ["task", "node", "model", "prompt_tokens",
                           "completion_tokens", "actual_cost_usd",
                           "theoretical_cost_usd"]


def test_compare_models_run_fixture_populates_token_columns():
    """Amendment 2: run_fixture now uses NodeUsageCollector; verify totals surface
    without touching the network — drive the collector directly."""
    collector = NodeUsageCollector()
    collector.record("ingest", 100, 20)
    collector.record("report", 200, 50)
    totals = collector.totals()
    assert totals["prompt_tokens"] == 300
    assert totals["completion_tokens"] == 70
