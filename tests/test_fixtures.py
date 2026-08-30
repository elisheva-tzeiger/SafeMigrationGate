import json
from pathlib import Path

import pytest

from agent.rules import is_blocked, overall_risk, run_rules
from agent.sqlparse import resolve_dialect, split_statements

FIXTURES = Path("evals/fixtures")
GROUND_TRUTH = json.loads((FIXTURES / "ground_truth.json").read_text(encoding="utf-8"))


def test_there_are_seven_fixtures():
    assert len(sorted(FIXTURES.glob("*.sql"))) == 7


def test_every_fixture_has_ground_truth():
    on_disk = {p.name for p in FIXTURES.glob("*.sql")}
    assert on_disk == set(GROUND_TRUTH)


@pytest.mark.parametrize("name", sorted(GROUND_TRUTH))
def test_the_ground_truth_matches_what_the_rules_actually_find(name):
    """The ground truth is the yardstick for Eval 1. If it drifts from the engine,
    every score computed against it is meaningless."""
    expected = GROUND_TRUTH[name]
    if expected["refused"]:
        pytest.skip("refusal fixtures are scored by the guardrail, not the rules")

    sql = (FIXTURES / name).read_text(encoding="utf-8")
    dialect = resolve_dialect(sql)
    findings = run_rules(split_statements(sql, dialect), dialect)

    assert sorted({f["rule_id"] for f in findings}) == sorted(expected["expected_rules"])
    assert overall_risk(findings) == expected["risk_level"]
    assert is_blocked(findings) is expected["blocked"]


def test_the_negative_control_is_genuinely_clean():
    """Without 05, the evals measure sensitivity only, and an agent that flags
    everything scores perfectly."""
    entry = GROUND_TRUTH["05_safe_migration.sql"]
    assert entry["expected_rules"] == []
    assert entry["risk_level"] == "LOW"
    assert entry["blocked"] is False
