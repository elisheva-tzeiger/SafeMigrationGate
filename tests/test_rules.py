import pytest

from agent.rules import (
    BLOCKING_RULES,
    RULE_TITLES,
    apply_measured_severity,
    is_blocked,
    overall_risk,
    run_rules,
)
from agent.sqlparse import split_statements


def ids_for(sql: str, dialect: str = "postgres") -> set[str]:
    return {f["rule_id"] for f in run_rules(split_statements(sql, dialect), dialect)}


# --- one positive and one negative case per rule ------------------------------

@pytest.mark.parametrize("dialect", ["postgres", "mysql", "tsql", "sqlite"])
def test_sg001_delete_without_where(dialect):
    assert "SG001" in ids_for("DELETE FROM users;", dialect)
    assert "SG001" not in ids_for("DELETE FROM users WHERE id = 7;", dialect)


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "tsql", "sqlite"])
def test_sg002_update_without_where(dialect):
    assert "SG002" in ids_for("UPDATE users SET status = 'x';", dialect)
    assert "SG002" not in ids_for("UPDATE users SET status='x' WHERE id=7;", dialect)


@pytest.mark.parametrize(
    "where", ["1 = 1", "TRUE", "'a' = 'a'", "1=1 AND 2=2", "(1 = 1)"]
)
def test_sg003_tautological_where(where):
    assert "SG003" in ids_for(f"DELETE FROM users WHERE {where};")


@pytest.mark.parametrize("where", ["id = 7", "1 = 1 AND id = 7", "status <> 'x'"])
def test_sg003_leaves_real_predicates_alone(where):
    """'1=1 AND id=7' is bounded by id=7. Flagging it would be a false positive."""
    assert "SG003" not in ids_for(f"DELETE FROM users WHERE {where};")


def test_sg004_drop_table():
    assert "SG004" in ids_for("DROP TABLE archive_2019;")
    assert "SG004" not in ids_for("CREATE TABLE t (i INT);")


def test_sg005_truncate():
    assert "SG005" in ids_for("TRUNCATE TABLE audit_log;")
    assert "SG005" not in ids_for("DELETE FROM audit_log WHERE id < 5;")


def test_sg006_alter_drop_column():
    assert "SG006" in ids_for("ALTER TABLE users DROP COLUMN status;")
    assert "SG006" not in ids_for("ALTER TABLE users ADD COLUMN nickname TEXT;")


def test_sg007_alter_column_type():
    assert "SG007" in ids_for("ALTER TABLE users ALTER COLUMN name TYPE VARCHAR(10);")
    assert "SG007" not in ids_for("ALTER TABLE users ADD COLUMN nickname TEXT;")


def test_sg007_message_admits_it_did_not_compare_types():
    findings = run_rules(
        split_statements("ALTER TABLE users ALTER COLUMN name TYPE VARCHAR(10);",
                         "postgres"), "postgres")
    message = next(f["message"] for f in findings if f["rule_id"] == "SG007")
    assert "without schema access" in message


def test_sg008_update_on_key_column():
    assert "SG008" in ids_for("UPDATE orders SET user_id = 3 WHERE id = 1;")
    assert "SG008" not in ids_for("UPDATE orders SET total = 3 WHERE id = 1;")


def test_sg009_explicit_cascade():
    assert "SG009" in ids_for("DROP TABLE users CASCADE;")
    assert "SG009" not in ids_for("DROP TABLE users;")


def test_sg010_unbounded_subquery_in_where():
    assert "SG010" in ids_for(
        "DELETE FROM orders WHERE user_id IN (SELECT id FROM users);")
    assert "SG010" not in ids_for("DELETE FROM orders WHERE user_id = 3;")


def test_sg011_script_not_wrapped_in_a_transaction():
    assert "SG011" in ids_for("DELETE FROM users WHERE id = 1;")
    assert "SG011" not in ids_for(
        "BEGIN;\nDELETE FROM users WHERE id = 1;\nCOMMIT;")


def test_sg012_ddl_and_dml_mixed():
    assert "SG012" in ids_for(
        "DELETE FROM users WHERE id = 1;\nDROP TABLE archive_2019;")
    assert "SG012" not in ids_for(
        "DELETE FROM users WHERE id = 1;\nDELETE FROM orders WHERE id = 2;")


def test_sg014_drop_database_and_schema():
    assert "SG014" in ids_for("DROP DATABASE prod;")
    assert "SG014" in ids_for("DROP SCHEMA reporting CASCADE;")
    assert "SG014" not in ids_for("DROP TABLE prod;")


@pytest.mark.parametrize(
    "sql",
    ["GRANT SELECT ON users TO bob;",
     "REVOKE ALL ON users FROM bob;",
     "CREATE USER bob PASSWORD 'x';",
     "ALTER ROLE bob SUPERUSER;"],
)
def test_sg015_privilege_changes(sql):
    assert "SG015" in ids_for(sql)


def test_sg015_does_not_fire_on_ordinary_dml():
    assert "SG015" not in ids_for("UPDATE users SET status='granted' WHERE id=1;")


# --- the two axes -------------------------------------------------------------

def test_blocking_is_exactly_sg014_and_sg015():
    assert BLOCKING_RULES == frozenset({"SG014", "SG015"})


def test_critical_delete_is_not_blocked():
    """The flagship case. If DELETE-without-WHERE blocked, the tool would refuse
    to fix the very thing it exists to fix."""
    findings = run_rules(split_statements("DELETE FROM users;", "postgres"), "postgres")
    assert overall_risk(findings) == "CRITICAL"
    assert is_blocked(findings) is False


def test_high_severity_sg015_is_blocked():
    """The axes are genuinely independent: HIGH but blocking."""
    findings = run_rules(
        split_statements("GRANT SELECT ON users TO bob;", "postgres"), "postgres")
    assert is_blocked(findings) is True
    assert overall_risk(findings) == "HIGH"


def test_no_findings_means_low_risk_not_an_error():
    assert overall_risk([]) == "LOW"
    assert is_blocked([]) is False


def test_the_negative_control_produces_nothing():
    """A safe migration must come back clean, or the whole risk model is noise."""
    sql = (
        "BEGIN;\n"
        "ALTER TABLE users ADD COLUMN nickname TEXT;\n"
        "COMMIT;\n"
    )
    assert ids_for(sql) == set()


def test_sg012_also_fires_when_the_ddl_is_an_alter():
    """MySQL commits implicitly before ALTER too, so the preceding DML can no
    longer be rolled back. Narrowing SG012 to DROP/TRUNCATE would lose this."""
    assert "SG012" in ids_for(
        "UPDATE users SET status='x' WHERE id=1;\nALTER TABLE users ADD COLUMN n TEXT;")


# --- SG013 is measured, not guessed -------------------------------------------

def test_sg013_fires_above_the_measured_threshold():
    statements = split_statements("DELETE FROM audit_log WHERE at < '2024-01-01';",
                                  "postgres")
    impact = [{"statement_index": 0, "rows_affected": 150_000,
               "confidence": "measured_target", "note": ""}]
    findings = apply_measured_severity([], statements, impact)
    sg013 = [f for f in findings if f["rule_id"] == "SG013"]
    assert len(sg013) == 1
    assert sg013[0]["severity"] == "HIGH"


def test_sg013_stays_silent_below_the_threshold():
    statements = split_statements("DELETE FROM users WHERE id < 5;", "postgres")
    impact = [{"statement_index": 0, "rows_affected": 4,
               "confidence": "measured_target", "note": ""}]
    assert apply_measured_severity([], statements, impact) == []


def test_sg013_stays_silent_when_nothing_was_measured():
    """No number, no claim. An unverified row must not manufacture a finding."""
    statements = split_statements("DELETE FROM audit_log;", "postgres")
    impact = [{"statement_index": 0, "rows_affected": None,
               "confidence": "unverified", "note": "database unreachable"}]
    assert apply_measured_severity([], statements, impact) == []


def test_sg013_ignores_statements_that_already_batch():
    statements = split_statements("DELETE FROM audit_log LIMIT 1000;", "mysql")
    impact = [{"statement_index": 0, "rows_affected": 150_000,
               "confidence": "measured_target", "note": ""}]
    assert apply_measured_severity([], statements, impact) == []


def test_every_rule_has_a_title():
    expected = {f"SG{n:03d}" for n in range(1, 16)}
    assert set(RULE_TITLES) == expected
