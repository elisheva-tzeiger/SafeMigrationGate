import pytest

from agent import tools
from agent.nodes import (
    IngestFailure,
    dry_run_impact,
    ingest,
    static_analysis,
    write_files,
)
from agent.state import initial_state


@pytest.fixture()
def script(tmp_path):
    path = tmp_path / "cleanup.sql"
    path.write_text("DELETE FROM users;\n", encoding="utf-8")
    return path


# --- ingest -------------------------------------------------------------------

def test_ingest_reads_the_file_and_resolves_the_dialect(script):
    state = initial_state(str(script), "", "", None)
    update = ingest(state)
    assert update["raw_sql"].startswith("DELETE FROM users")
    assert update["dialect"] == "postgres"
    assert [s["kind"] for s in update["statements"]] == ["DELETE"]
    assert update["node_trail"] == ["ingest"]


def test_ingest_on_a_missing_file_raises_ingest_failure(tmp_path):
    state = initial_state(str(tmp_path / "nope.sql"), "", "", None)
    with pytest.raises(IngestFailure) as excinfo:
        ingest(state)
    assert "nope.sql" in str(excinfo.value)


def test_ingest_on_unparseable_sql_raises_ingest_failure(tmp_path):
    path = tmp_path / "broken.sql"
    path.write_text("DELET FRM;;;(", encoding="utf-8")
    with pytest.raises(IngestFailure):
        ingest(initial_state(str(path), "", "", None))


def test_ingest_reads_utf8_including_hebrew_comments(tmp_path):
    path = tmp_path / "he.sql"
    path.write_text("-- ניקוי משתמשים\nDELETE FROM users;\n", encoding="utf-8")
    assert "ניקוי" in ingest(initial_state(str(path), "", "", None))["raw_sql"]


# --- static_analysis ----------------------------------------------------------

def test_static_analysis_records_findings_and_the_two_axes():
    state = initial_state("x.sql", "DELETE FROM users;", "postgres", None)
    state["statements"] = ingest_statements("DELETE FROM users;")
    update = static_analysis(state)
    assert "SG001" in {f["rule_id"] for f in update["findings"]}
    assert update["risk_level"] == "CRITICAL"
    assert update["blocked"] is False


def test_static_analysis_blocks_only_on_sg014_and_sg015():
    state = initial_state("x.sql", "DROP DATABASE prod;", "postgres", None)
    state["statements"] = ingest_statements("DROP DATABASE prod;")
    assert static_analysis(state)["blocked"] is True


def test_static_analysis_cites_a_rule_for_every_finding():
    state = initial_state("x.sql", "DELETE FROM users;", "postgres", None)
    state["statements"] = ingest_statements("DELETE FROM users;")
    update = static_analysis(state)
    cited = {r["rule_id"] for r in update["cited_rules"]}
    assert {f["rule_id"] for f in update["findings"]} <= cited


def ingest_statements(sql: str):
    from agent.sqlparse import split_statements

    return split_statements(sql, "postgres")


# --- dry_run_impact -----------------------------------------------------------

def test_dry_run_impact_measures_against_the_demo_db(monkeypatch, tmp_path):
    from data.seed import build_demo_db

    db = build_demo_db(tmp_path / "demo.db", large_rows=10)
    monkeypatch.setattr("agent.tools.resources.demo_db_url",
                        lambda: f"sqlite:///{db}")

    state = initial_state("x.sql", "DELETE FROM users;", "postgres", None)
    state["statements"] = ingest_statements("DELETE FROM users;")
    update = dry_run_impact(state)

    assert update["impact"][0]["rows_affected"] == 500
    assert update["impact"][0]["confidence"] == "measured_demo_transpiled"


def test_dry_run_impact_tags_a_real_target_differently(monkeypatch, tmp_path):
    from data.seed import build_demo_db

    db = build_demo_db(tmp_path / "target.db", large_rows=10)
    state = initial_state("x.sql", "DELETE FROM users;", "sqlite",
                          f"sqlite:///{db}")
    state["statements"] = ingest_statements("DELETE FROM users;")
    update = dry_run_impact(state)
    assert update["impact"][0]["confidence"] == "measured_target"


def test_a_sqlite_script_against_the_demo_is_still_not_a_target_measurement(
        monkeypatch, tmp_path):
    """The tag turns on whose database was measured, not on whether a dialect
    translation happened. With no db_url the numbers come from the bundled demo,
    so calling them measured_target would present seeded rows as the user's own."""
    from data.seed import build_demo_db

    db = build_demo_db(tmp_path / "demo.db", large_rows=10)
    monkeypatch.setattr("agent.tools.resources.demo_db_url",
                        lambda: f"sqlite:///{db}")
    state = initial_state("x.sql", "DELETE FROM users;", "sqlite", None)
    state["statements"] = ingest_statements("DELETE FROM users;")

    update = dry_run_impact(state)

    assert update["impact"][0]["rows_affected"] == 500
    assert update["impact"][0]["confidence"] == "measured_demo_transpiled"


def test_dry_run_impact_never_executes_a_commit_from_the_script(tmp_path):
    """A COMMIT in the user's script used to run verbatim against their own
    database, ending the transaction and making the preceding DELETE permanent --
    while still reporting the row count as `measured_target`."""
    import hashlib

    from agent.sqlparse import split_statements
    from data.seed import build_demo_db

    db = build_demo_db(tmp_path / "target.db", large_rows=10)
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    sql = "DELETE FROM users;\nCOMMIT;\nDROP TABLE users;"
    state = initial_state("x.sql", sql, "sqlite", f"sqlite:///{db}")
    state["statements"] = split_statements(sql, "sqlite")

    update = dry_run_impact(state)

    # Indices stay aligned with the original script -- nothing is renumbered.
    assert [r["statement_index"] for r in update["impact"]] == [0, 1, 2]
    assert update["impact"][1]["rows_affected"] is None
    assert "transaction control is not executed" in update["impact"][1]["note"]
    # The statement after the COMMIT is still measured.
    assert update["impact"][2]["confidence"] == "measured_target"

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_dry_run_impact_degrades_instead_of_crashing(monkeypatch):
    """A database that cannot be reached must not stop the run. Every row becomes
    unverified and the graph carries on."""
    monkeypatch.setattr("agent.tools.resources.demo_db_url",
                        lambda: "sqlite:///Z:/nowhere/nested/missing.db")
    state = initial_state("x.sql", "DELETE FROM users;", "postgres", None)
    state["statements"] = ingest_statements("DELETE FROM users;")

    update = dry_run_impact(state)

    assert update["impact"][0]["confidence"] == "unverified"
    assert update["impact"][0]["rows_affected"] is None
    assert update["errors"][0]["node"] == "dry_run_impact"
    assert update["errors"][0]["kind"] == "internal"


def test_dry_run_impact_adds_sg013_from_the_measurement(monkeypatch, tmp_path):
    from data.seed import build_demo_db

    db = build_demo_db(tmp_path / "demo.db", large_rows=150_001)
    monkeypatch.setattr("agent.tools.resources.demo_db_url",
                        lambda: f"sqlite:///{db}")
    sql = "DELETE FROM audit_log;"
    state = initial_state("x.sql", sql, "postgres", None)
    state["statements"] = ingest_statements(sql)
    state["findings"] = []

    update = dry_run_impact(state)

    assert "SG013" in {f["rule_id"] for f in update["findings"]}
    assert update["risk_level"] == "HIGH"


# --- write_files --------------------------------------------------------------

def test_write_files_writes_both_outputs_next_to_the_source(script):
    state = initial_state(str(script), "DELETE FROM users;", "postgres", None)
    state["safe_sql"] = "BEGIN;\nROLLBACK;\n"
    state["report_md"] = "# Risk report\n"

    update = write_files(state)

    safe = script.parent / "cleanup_safe.sql"
    report = script.parent / "cleanup_report.md"
    assert safe.read_text(encoding="utf-8") == "BEGIN;\nROLLBACK;\n"
    assert report.read_text(encoding="utf-8") == "# Risk report\n"
    assert set(update["written_files"]) == {str(safe), str(report)}


def test_write_files_writes_the_report_even_without_a_safe_script(script):
    """A blocked script has no safe version, but the user still gets the report."""
    state = initial_state(str(script), "DROP DATABASE p;", "postgres", None)
    state["safe_sql"] = None
    state["report_md"] = "# Blocked\n"

    update = write_files(state)

    assert (script.parent / "cleanup_report.md").exists()
    assert not (script.parent / "cleanup_safe.sql").exists()
    assert len(update["written_files"]) == 1


def test_output_paths_derives_both_names_from_the_source():
    safe, report = tools.output_paths("/tmp/migrate_2024.sql")
    assert safe.name == "migrate_2024_safe.sql"
    assert report.name == "migrate_2024_report.md"
