"""Smoke test: the package imports and the toolchain is installed."""


def test_agent_package_imports():
    import agent

    assert agent.__version__ == "0.1.0"


def test_core_dependencies_are_installed():
    import langgraph  # noqa: F401
    import langchain_openai  # noqa: F401
    import sqlglot
    import sqlalchemy
    import rank_bm25  # noqa: F401
    import rich  # noqa: F401

    assert int(sqlglot.__version__.split(".")[0]) >= 30
    assert sqlalchemy.__version__.startswith("2.")


def test_old_skeleton_is_gone():
    from pathlib import Path

    assert not Path("src").exists(), "the generic Anthropic skeleton must be deleted"
