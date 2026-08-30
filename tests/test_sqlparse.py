import pytest

from agent.sqlparse import (
    ParseFailure,
    resolve_dialect,
    split_statements,
    strip_comments,
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


# ---------------------------------------------------------------------------
# Comment-state fix tests (review finding)
# ---------------------------------------------------------------------------

def test_semicolon_in_line_comment_does_not_split():
    """A ; inside -- ... should not be treated as a statement separator."""
    sql = "DELETE FROM users WHERE id=1; -- cleanup; also see ticket 42\nDELETE FROM orders WHERE id=2;"
    statements = split_statements(sql, "postgres")
    assert len(statements) == 2
    assert statements[0]["kind"] == "DELETE"
    assert statements[0]["target_tables"] == ["users"]
    assert statements[1]["kind"] == "DELETE"
    assert statements[1]["target_tables"] == ["orders"]


def test_semicolon_in_block_comment_does_not_split():
    """A ; inside /* ... */ should not be treated as a statement separator."""
    sql = "/* step 1; step 2 */\nDELETE FROM users WHERE id=1;"
    statements = split_statements(sql, "postgres")
    assert len(statements) == 1
    assert statements[0]["kind"] == "DELETE"
    assert statements[0]["target_tables"] == ["users"]


def test_commented_out_original_statement_does_not_split():
    """The rewrite contract comments out the original statement (rule 3).
    That comment ends with ; and must not create an extra segment."""
    sql = "BEGIN;\n-- DELETE FROM users;\nDELETE FROM users WHERE id=1;\nROLLBACK;"
    statements = split_statements(sql, "postgres")
    kinds = [s["kind"] for s in statements]
    assert kinds == ["TRANSACTION", "DELETE", "ROLLBACK"]


def test_trailing_comment_after_last_semicolon_produces_no_extra_statement():
    """A trailing -- comment after the final ; must not reach sqlglot.parse_one."""
    sql = "DELETE FROM users;\n-- end of script"
    statements = split_statements(sql, "postgres")
    assert len(statements) == 1
    assert statements[0]["kind"] == "DELETE"


def test_file_containing_only_comments_produces_no_statements():
    """A file that is entirely comments should yield an empty list."""
    sql = "-- just a comment\n/* another\ncomment */"
    statements = split_statements(sql, "postgres")
    assert statements == []


def test_doubled_quote_inside_string_is_not_split():
    """Existing behaviour: doubled quote inside a string literal must not split."""
    sql = "DELETE FROM users WHERE name='it''s; fine';"
    statements = split_statements(sql, "postgres")
    assert len(statements) == 1
    assert statements[0]["kind"] == "DELETE"


def test_plain_multiple_statements_still_split():
    """Existing behaviour: two plain DELETEs separated by ; must produce two statements."""
    sql = "DELETE FROM users;\nDELETE FROM orders;"
    statements = split_statements(sql, "postgres")
    assert len(statements) == 2
    assert statements[0]["target_tables"] == ["users"]
    assert statements[1]["target_tables"] == ["orders"]


def test_strip_comments_removes_line_and_block_comments():
    """strip_comments must remove both comment forms and leave the rest intact."""
    sql = "SELECT 1; -- line comment\n/* block\ncomment */ SELECT 2;"
    result = strip_comments(sql)
    assert "--" not in result
    assert "/*" not in result
    assert "*/" not in result
    assert "SELECT 1" in result
    assert "SELECT 2" in result
