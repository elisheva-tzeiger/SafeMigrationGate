import pytest

from agent.sqlparse import (
    ParseFailure,
    resolve_dialect,
    split_statements,
    to_dialect,
)


def test_hint_wins_when_it_parses():
    assert resolve_dialect("DELETE FROM t;", hint="mysql") == "mysql"


def test_unparseable_hint_falls_back_to_detection():
    sql = "SELECT TOP 10 * FROM t"          # T-SQL only
    assert resolve_dialect(sql, hint="postgres") == "tsql"


def test_detection_defaults_to_postgres_for_portable_sql():
    """Plain SQL parses everywhere; pick a stable default rather than whichever
    dialect happens to be first in a set."""
    assert resolve_dialect("DELETE FROM users;") == "postgres"


def test_unparseable_sql_raises_parse_failure():
    with pytest.raises(ParseFailure) as excinfo:
        resolve_dialect("DELET FRM users WHERE;;;(")
    assert "could not be parsed" in str(excinfo.value)


def test_split_statements_records_index_kind_and_tables():
    sql = "DELETE FROM users;\nUPDATE orders SET total = 0 WHERE id = 3;"
    statements = split_statements(sql, "postgres")

    assert [s["index"] for s in statements] == [0, 1]
    assert [s["kind"] for s in statements] == ["DELETE", "UPDATE"]
    assert statements[0]["target_tables"] == ["users"]
    assert statements[1]["target_tables"] == ["orders"]


def test_split_statements_records_where_presence_and_text():
    statements = split_statements(
        "DELETE FROM users;\nDELETE FROM orders WHERE total = 0;", "postgres"
    )
    assert statements[0]["has_where"] is False
    assert statements[0]["where_text"] is None
    assert statements[1]["has_where"] is True
    assert "total" in statements[1]["where_text"]


def test_split_statements_preserves_original_text_verbatim():
    """The rewrite contract requires the original text word for word, so ingest
    must not normalise it."""
    sql = "delete   from   users  /* keep me */;"
    statements = split_statements(sql, "postgres")
    assert statements[0]["text"].strip() == "delete   from   users  /* keep me */"


def test_split_statements_ignores_blank_trailing_segments():
    statements = split_statements("DELETE FROM t;\n\n   \n", "postgres")
    assert len(statements) == 1


def test_transaction_statements_are_kept_as_their_own_kind():
    statements = split_statements("BEGIN;\nDELETE FROM t;\nCOMMIT;", "postgres")
    assert [s["kind"] for s in statements] == ["TRANSACTION", "DELETE", "COMMIT"]


def test_unsupported_syntax_is_kept_as_command_not_dropped():
    statements = split_statements("CREATE USER bob PASSWORD 'x';", "postgres")
    assert len(statements) == 1
    assert statements[0]["kind"] == "COMMAND"


def test_to_dialect_translates_functions():
    out = to_dialect("DELETE FROM t WHERE d < GETDATE()", read="tsql", write="sqlite")
    assert "CURRENT_TIMESTAMP" in out
