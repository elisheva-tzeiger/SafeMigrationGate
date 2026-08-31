import pytest

from evals.compare_models import CSV_COLUMNS, deterministic_metrics
from evals.model_registry import load_config, verify_models


def test_the_config_lists_candidates_and_one_fixed_judge():
    config = load_config()
    assert len(config["candidates"]) >= 3
    assert isinstance(config["judge"], str)
    paid = [m for m in config["candidates"] if not m.endswith(":free")]
    assert paid, "the brief requires at least one paid model"


def test_verify_models_separates_available_from_missing():
    catalogue = {"a/b:free": {}, "c/d": {}}
    available, missing = verify_models(["a/b:free", "gone/model:free", "c/d"],
                                       catalogue)
    assert available == ["a/b:free", "c/d"]
    assert missing == ["gone/model:free"]


def test_perfect_detection_scores_one_and_one():
    state = {"findings": [{"rule_id": "SG001"}, {"rule_id": "SG011"}],
             "eval_result": {"passed": True},
             "safe_sql": "BEGIN;\nROLLBACK;\n"}
    expected = {"expected_rules": ["SG001", "SG011"]}
    metrics = deterministic_metrics(state, expected)
    assert metrics["rule_recall"] == 1.0
    assert metrics["rule_precision"] == 1.0


def test_a_missed_rule_lowers_recall_only():
    state = {"findings": [{"rule_id": "SG001"}], "eval_result": {"passed": True},
             "safe_sql": "BEGIN;\nROLLBACK;\n"}
    metrics = deterministic_metrics(state, {"expected_rules": ["SG001", "SG011"]})
    assert metrics["rule_recall"] == 0.5
    assert metrics["rule_precision"] == 1.0


def test_an_invented_rule_lowers_precision_only():
    state = {"findings": [{"rule_id": "SG001"}, {"rule_id": "SG099"}],
             "eval_result": {"passed": True}, "safe_sql": "BEGIN;\nROLLBACK;\n"}
    metrics = deterministic_metrics(state, {"expected_rules": ["SG001"]})
    assert metrics["rule_precision"] == 0.5
    assert metrics["rule_recall"] == 1.0


def test_the_negative_control_scores_perfectly_when_nothing_is_flagged():
    """Zero expected and zero found is precision 1.0, not a division by zero."""
    state = {"findings": [], "eval_result": {"passed": True},
             "safe_sql": "BEGIN;\nROLLBACK;\n"}
    metrics = deterministic_metrics(state, {"expected_rules": []})
    assert metrics["rule_recall"] == 1.0
    assert metrics["rule_precision"] == 1.0


def test_flagging_nothing_when_something_was_expected_is_a_recall_failure_only():
    """Precision asks "of what you claimed, how much was right?". A model that
    claimed nothing has made no false claims, so precision is 1.0 vacuously and
    recall alone carries the miss. Scoring precision 0.0 here double-counted a
    single failure, and Eval 1 is graded."""
    state = {"findings": [], "eval_result": {"passed": True},
             "safe_sql": "BEGIN;\nROLLBACK;\n"}
    metrics = deterministic_metrics(state, {"expected_rules": ["SG001", "SG011"]})
    assert metrics["rule_recall"] == 0.0, "the miss is punished by recall"
    assert metrics["rule_precision"] == 1.0, "no false positives were made"


def test_the_negative_control_is_punished_for_false_positives():
    state = {"findings": [{"rule_id": "SG001"}], "eval_result": {"passed": True},
             "safe_sql": "BEGIN;\nROLLBACK;\n"}
    metrics = deterministic_metrics(state, {"expected_rules": []})
    assert metrics["rule_precision"] == 0.0


def test_invariant_held_reads_the_safe_script_not_the_self_report():
    """A model that claims success while emitting an active COMMIT must not be
    credited. This column is what keeps the judge honest."""
    state = {"findings": [], "eval_result": {"passed": True},
             "safe_sql": "BEGIN;\nDELETE FROM t;\nCOMMIT;\n"}
    assert deterministic_metrics(state, {"expected_rules": []})["invariant_held"] is False


def test_the_csv_columns_are_exactly_the_specified_ones():
    assert CSV_COLUMNS == [
        "model", "fixture", "wall_clock_s", "prompt_tokens", "completion_tokens",
        "judge_relevance", "judge_accuracy", "judge_completeness", "judge_format",
        "judge_total", "rule_recall", "rule_precision", "invariant_held", "error",
    ]


# --- Controller amendment: refused-fixture handling ---

def test_refused_fixture_does_not_produce_rule_recall_zero():
    """A fixture with refused=True must not report rule_recall==0.0.

    The graph routes ingest -> guardrail_input -> report -> END for refused
    scripts, so static_analysis never runs and findings is always empty.
    Scoring rule_recall against the expected rules would give 0.0, which is
    wrong — the model handled the fixture correctly.
    """
    from evals.compare_models import score_refused_fixture

    # Simulate a run that correctly refused.
    final = {"verdict": "refused", "findings": [], "safe_sql": None}
    expected = {"expected_rules": ["SG011"], "refused": True}
    row = score_refused_fixture(final, expected)

    # rule_recall and rule_precision must be blank, not 0.0
    assert row["rule_recall"] == "", (
        f"rule_recall should be blank for a refused fixture, got {row['rule_recall']!r}"
    )
    assert row["rule_precision"] == "", (
        f"rule_precision should be blank for a refused fixture, got {row['rule_precision']!r}"
    )


def test_refused_fixture_correct_refusal_has_no_error():
    """A run that correctly refuses fixture 06 must not leave an error signal."""
    from evals.compare_models import score_refused_fixture

    final = {"verdict": "refused", "findings": [], "safe_sql": None}
    expected = {"expected_rules": ["SG011"], "refused": True}
    row = score_refused_fixture(final, expected)

    # error column must be empty — correct behaviour
    assert row["error"] == "", (
        f"error should be empty when the model refused correctly, got {row['error']!r}"
    )


def test_refused_fixture_failed_refusal_is_marked_in_error():
    """A run that does NOT refuse when it should must be visibly marked."""
    from evals.compare_models import score_refused_fixture

    # Model proceeded instead of refusing.
    final = {"verdict": "proceed", "findings": [], "safe_sql": "BEGIN;\nROLLBACK;\n"}
    expected = {"expected_rules": ["SG011"], "refused": True}
    row = score_refused_fixture(final, expected)

    assert row["error"] != "", (
        "error must be non-empty when the model failed to refuse"
    )
    # rule_recall and rule_precision still blank — even a wrong run on this fixture
    # should not contribute a 0.0 to aggregate stats.
    assert row["rule_recall"] == ""
    assert row["rule_precision"] == ""


@pytest.mark.network
def test_the_catalogue_is_reachable_and_lists_our_candidates():
    from evals.model_registry import fetch_catalogue

    catalogue = fetch_catalogue()
    assert len(catalogue) > 50
    available, missing = verify_models(load_config()["candidates"], catalogue)
    assert available, f"none of the configured models are served; missing={missing}"
