import pytest

from agent.rewrite import (
    FILL_PLACEHOLDER,
    REWRITE_CONTRACT,
    check_invariant,
    normalize,
    to_eval_result,
)
from agent.sqlparse import split_statements

ORIGINAL = "DELETE FROM users;"
STATEMENTS = split_statements(ORIGINAL, "postgres")

# [CONTROLLER] GOOD uses the block-comment placeholder from Ruling 1.
GOOD = """BEGIN;
-- [SafeMigrationGate] SG001: original had no WHERE clause; a predicate must be
-- supplied by a human. The original is preserved verbatim, commented out.
-- DELETE FROM users;
SELECT COUNT(*) FROM users WHERE /* FILL: condition */ 1 = 0;
DELETE FROM users WHERE /* FILL: condition */ 1 = 0;
ROLLBACK;
-- COMMIT;
"""


def test_a_correct_rewrite_passes_every_deterministic_check():
    report = check_invariant(GOOD, STATEMENTS, "postgres")
    assert report["validity"] is True
    assert report["no_omissions"] is True
    assert report["invariant_held"] is True
    assert report["reasons"] == []


def test_the_placeholder_keeps_safe_sql_parseable():
    """[CONTROLLER] Ruling 1's regression test. The plan's original
    `WHERE <-- FILL: condition -->` made sqlglot raise, which would have forced
    validity=False on every rewrite the agent ever produced."""
    assert "/*" in FILL_PLACEHOLDER
    assert check_invariant(GOOD, STATEMENTS, "postgres")["validity"] is True


def test_the_placeholder_is_inert_rather_than_a_guessed_predicate():
    """[CONTROLLER] `1 = 0` matches nothing, so running the safe script before a
    human fills it in changes no rows. The agent must never invent a predicate."""
    assert "1 = 0" in FILL_PLACEHOLDER


def test_missing_rollback_breaks_the_invariant():
    report = check_invariant(GOOD.replace("ROLLBACK;", ""), STATEMENTS, "postgres")
    assert report["invariant_held"] is False
    assert any("ROLLBACK" in r for r in report["reasons"])


def test_an_active_commit_breaks_the_invariant():
    """The whole promise is that running the output changes nothing."""
    broken = GOOD.replace("ROLLBACK;", "COMMIT;")
    report = check_invariant(broken, STATEMENTS, "postgres")
    assert report["invariant_held"] is False
    assert any("COMMIT" in r for r in report["reasons"])


def test_a_commented_commit_is_allowed():
    assert check_invariant(GOOD, STATEMENTS, "postgres")["invariant_held"] is True


def test_a_commit_inside_a_string_literal_is_not_an_active_commit():
    """Naive substring matching would produce a false alarm here."""
    sql = GOOD.replace(
        "SELECT COUNT(*) FROM users WHERE /* FILL: condition */ 1 = 0;",
        "SELECT COUNT(*) FROM audit_log WHERE action = 'COMMIT';",
    )
    assert check_invariant(sql, STATEMENTS, "postgres")["invariant_held"] is True


def test_rollback_must_be_the_last_executable_statement():
    trailing = GOOD.replace("-- COMMIT;", "DELETE FROM orders;")
    report = check_invariant(trailing, STATEMENTS, "postgres")
    assert report["invariant_held"] is False


def test_a_dropped_statement_is_caught():
    two = split_statements("DELETE FROM users;\nDROP TABLE orders;", "postgres")
    report = check_invariant(GOOD, two, "postgres")
    assert report["no_omissions"] is False
    assert any("DROP TABLE orders" in r for r in report["reasons"])


def test_a_statement_preserved_only_as_a_comment_still_counts():
    """Rule 3 of the contract requires commenting the original out, so a commented
    original is not an omission."""
    report = check_invariant(GOOD, STATEMENTS, "postgres")
    assert report["no_omissions"] is True


def test_unparseable_output_fails_validity():
    report = check_invariant("BEGIN; DELET FROM;;( ROLLBACK;", STATEMENTS, "postgres")
    assert report["validity"] is False


def test_normalize_ignores_whitespace_and_case():
    assert normalize("delete   FROM\n users") == normalize("DELETE FROM users")


def test_to_eval_result_requires_all_four_conditions():
    passing = {"validity": True, "no_omissions": True,
               "invariant_held": True, "reasons": []}
    assert to_eval_result(passing, equivalence=4)["passed"] is True
    assert to_eval_result(passing, equivalence=3)["passed"] is False
    failing = {**passing, "invariant_held": False, "reasons": ["no ROLLBACK"]}
    assert to_eval_result(failing, equivalence=5)["passed"] is False


def test_to_eval_result_carries_the_reasons_through():
    report = {"validity": False, "no_omissions": True,
              "invariant_held": True, "reasons": ["unparseable"]}
    result = to_eval_result(report, equivalence=5)
    assert result["reasons"] == ["unparseable"]
    assert result["validity"] is False
    assert result["equivalence"] == 5


def test_multiline_original_commented_out_is_not_an_omission():
    """Finding 1 regression.  A two-line statement commented out across two lines
    is not a missing original — the haystack must see past the '--' markers."""
    sql = "DELETE FROM users\nWHERE id > 0;\nDROP TABLE t;"
    statements = split_statements(sql, "postgres")
    # Construct a model-shaped safe script that follows rule 3: each line of the
    # original gets its own "-- " prefix, just as a real model output would look.
    safe_sql = (
        "BEGIN;\n"
        "-- DELETE FROM users\n"
        "-- WHERE id > 0;\n"
        "DELETE FROM users WHERE /* FILL: condition */ 1 = 0;\n"
        "-- DROP TABLE t;\n"
        "ALTER TABLE t RENAME TO t_deprecated_2026;\n"
        "ROLLBACK;\n"
        "-- COMMIT;\n"
    )
    report = check_invariant(safe_sql, statements, "postgres")
    assert report["no_omissions"] is True, report["reasons"]
    assert report["validity"] is True, report["reasons"]
    assert report["invariant_held"] is True, report["reasons"]


def test_the_contract_states_all_eight_rules():
    for marker in ["BEGIN;", "verbatim", FILL_PLACEHOLDER, "SELECT COUNT(*)",
                   "ROLLBACK;", "-- COMMIT;", "RENAME TO", "batching",
                   "[SafeMigrationGate]"]:
        assert marker in REWRITE_CONTRACT
