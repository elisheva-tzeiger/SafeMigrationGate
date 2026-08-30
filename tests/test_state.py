from agent.state import (
    SEVERITY_ORDER,
    GateState,
    initial_state,
    max_severity,
)


def test_severity_order_is_ascending():
    assert SEVERITY_ORDER == {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def test_max_severity_picks_the_highest():
    assert max_severity(["LOW", "CRITICAL", "MEDIUM"]) == "CRITICAL"
    assert max_severity(["LOW", "MEDIUM"]) == "MEDIUM"


def test_max_severity_of_nothing_is_low():
    """No findings means no risk — not an exception, and not CRITICAL."""
    assert max_severity([]) == "LOW"


def test_initial_state_starts_neutral():
    s: GateState = initial_state("q.sql", "DELETE FROM t;", "postgres", None)
    assert s["verdict"] == "proceed"
    assert s["blocked"] is False
    assert s["risk_level"] == "LOW"
    assert s["rewrite_attempts"] == 0
    assert s["db_url"] is None
    assert s["findings"] == []
    assert s["errors"] == []
    assert s["node_trail"] == []


def test_initial_state_does_not_share_mutable_defaults():
    a = initial_state("a.sql", "", "sqlite", None)
    b = initial_state("b.sql", "", "sqlite", None)
    a["findings"].append({"statement_index": 0, "rule_id": "SG001",
                          "severity": "CRITICAL", "message": "x"})
    assert b["findings"] == []
