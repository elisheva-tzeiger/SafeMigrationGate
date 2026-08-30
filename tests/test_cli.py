import pytest

from agent.cli import HELP_TEXT, Repl, parse_command


@pytest.mark.parametrize(
    "line, expected",
    [
        ("check foo.sql", ("check", "foo.sql")),
        ("  rules  ", ("rules", "")),
        ("why SG001", ("why", "SG001")),
        ("db demo", ("db", "demo")),
        ("quit", ("quit", "")),
        ("?", ("help", "")),
    ],
)
def test_parse_command_splits_verb_from_argument(line, expected):
    assert parse_command(line) == expected


def test_a_bare_path_is_treated_as_check(tmp_path):
    """Dragging a file onto the terminal should just work."""
    script = tmp_path / "a.sql"
    script.write_text("DELETE FROM t;", encoding="utf-8")
    assert parse_command(str(script)) == ("check", str(script))


def test_a_quoted_path_is_unquoted(tmp_path):
    """Windows drag-and-drop wraps paths containing spaces in quotes."""
    script = tmp_path / "my script.sql"
    script.write_text("DELETE FROM t;", encoding="utf-8")
    assert parse_command(f'"{script}"') == ("check", str(script))


def test_an_unknown_word_is_not_mistaken_for_a_path():
    assert parse_command("banana") == ("banana", "")


def test_quit_ends_the_loop():
    assert Repl().handle("quit") is False


def test_other_commands_keep_the_loop_alive():
    assert Repl().handle("help") is True


def test_db_demo_clears_the_connection_string():
    repl = Repl()
    repl.db_url = "postgresql://u:p@h/d"
    repl.handle("db demo")
    assert repl.db_url is None


def test_db_stores_the_connection_string():
    repl = Repl()
    repl.handle("db postgresql://u:p@h/d")
    assert repl.db_url == "postgresql://u:p@h/d"


def test_db_echoes_the_url_with_the_password_masked(capsys):
    Repl().handle("db postgresql://alice:s3cr3t@host/db")
    printed = capsys.readouterr().out
    assert "s3cr3t" not in printed
    assert "***" in printed


def test_rules_lists_every_rule_id(capsys):
    Repl().handle("rules")
    printed = capsys.readouterr().out
    for rule_id in ("SG001", "SG013", "SG015"):
        assert rule_id in printed


def test_why_prints_the_knowledge_document(capsys):
    Repl().handle("why SG004")
    printed = capsys.readouterr().out
    assert "DROP TABLE" in printed


def test_why_on_an_unknown_rule_says_so_without_crashing(capsys):
    Repl().handle("why SG999")
    assert "SG999" in capsys.readouterr().out


def test_a_free_text_question_goes_through_bm25_search(capsys):
    Repl().handle("what happens if I truncate a table")
    assert "SG005" in capsys.readouterr().out


def test_check_on_a_missing_file_reports_it_without_a_traceback(capsys):
    repl = Repl()
    assert repl.handle("check nope.sql") is True
    printed = capsys.readouterr().out
    assert "nope.sql" in printed
    assert "Traceback" not in printed


def test_summary_writes_the_transcript(tmp_path):
    repl = Repl()
    repl.transcript.append("check demo.sql -> CRITICAL")
    target = tmp_path / "session.txt"
    repl.handle(f"summary {target}")
    assert "CRITICAL" in target.read_text(encoding="utf-8")


def test_help_covers_every_command():
    for command in ("check", "db", "rules", "why", "summary", "help", "quit"):
        assert command in HELP_TEXT


# Controller amendment: free-text question must open with the scope sentence
def test_free_text_question_opens_with_scope_sentence(capsys):
    """Every _cmd_ask response must state scope before listing KB hits."""
    Repl().handle("what happens if I drop a table")
    printed = capsys.readouterr().out
    # The scope sentence must appear before the KB hits
    scope_idx = printed.find("I only review destructive SQL migration scripts")
    assert scope_idx != -1, "scope sentence missing from free-text response"
    hits_idx = printed.find("knowledge base")
    # scope sentence comes first
    assert scope_idx < hits_idx


# Finding 1: _confirm must use injectable ask_line, not bare input()
def test_confirm_returns_true_when_ask_line_returns_y():
    """A caller can supply the answer without touching stdin."""
    repl = Repl()
    repl.ask_line = lambda _prompt: "y"
    assert repl._confirm("Proceed?") is True


def test_confirm_returns_false_when_ask_line_returns_n():
    repl = Repl()
    repl.ask_line = lambda _prompt: "n"
    assert repl._confirm("Proceed?") is False


def test_confirm_returns_false_when_ask_line_raises_eoferror():
    repl = Repl()
    repl.ask_line = lambda _prompt: (_ for _ in ()).throw(EOFError())
    assert repl._confirm("Proceed?") is False


# Finding 2: no-hits branch of _cmd_ask must also open with scope sentence
def test_no_hits_branch_opens_with_scope_sentence(capsys):
    """The no-hits branch must state scope first, matching the hits branch."""
    # "photosynthesis biology" has no SQL-safety relevance — confirmed 0 BM25 hits
    repl = Repl()
    repl.handle("photosynthesis biology")
    printed = capsys.readouterr().out
    # Confirm we took the no-hits path (no KB hit listing)
    assert "Use `why" not in printed, (
        "Expected the no-hits path but got KB hits — pick a different query"
    )
    # Scope sentence must still be present
    assert "I only review destructive SQL migration scripts" in printed
