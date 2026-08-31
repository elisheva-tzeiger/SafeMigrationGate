from pathlib import Path

SPEC = Path("build.spec").read_text(encoding="utf-8")


def test_the_knowledge_corpus_is_bundled():
    """resource_path resolves 'agent/knowledge' inside the bundle, so the datas
    entry must land it at exactly that path."""
    assert "agent/knowledge" in SPEC or "agent\\\\knowledge" in SPEC


def test_the_demo_database_and_rubric_are_bundled():
    assert "demo.db" in SPEC
    assert "rubric.md" in SPEC


def test_the_dynamically_loaded_modules_are_declared():
    """sqlglot dialects and langgraph internals are imported by name at run time
    and are invisible to PyInstaller's static scan."""
    for module in ("sqlglot.dialects", "langgraph", "truststore"):
        assert module in SPEC


def test_it_builds_one_file():
    assert "onefile" in SPEC.lower()


def test_the_entry_point_is_the_dual_mode_launcher():
    assert "agent/cli.py" in SPEC or "run_agent.py" in SPEC
