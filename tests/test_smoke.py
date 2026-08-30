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


def test_network_test_guard():
    """Test that the network test opt-in guard fires only on explicit -m network."""
    import pytest
    from tests.conftest import pytest_collection_modifyitems

    # Create a fake config with a getoption method
    class FakeConfig:
        def __init__(self, marker_expr):
            self.marker_expr = marker_expr

        def getoption(self, option):
            if option == "-m":
                return self.marker_expr
            return None

    # Create fake items: one with network marker, one without
    class FakeItem:
        def __init__(self, name, has_network_marker=False):
            self.name = name
            self.keywords = {"network": True} if has_network_marker else {}
            self.markers = []

        def add_marker(self, marker):
            self.markers.append(marker)

    # Test 1: no -m flag → network tests should be skipped
    config = FakeConfig(None)
    items = [FakeItem("test_net", has_network_marker=True), FakeItem("test_other")]
    pytest_collection_modifyitems(config, items)
    assert any(
        m.name == "skip" for m in items[0].markers
    ), "network test should be skipped when no -m flag"
    assert not items[1].markers, "non-network test should not be skipped"

    # Test 2: -m network → network tests should run (early return)
    config = FakeConfig("network")
    items = [FakeItem("test_net", has_network_marker=True), FakeItem("test_other")]
    pytest_collection_modifyitems(config, items)
    assert not items[0].markers, "network test should run with -m network"
    assert not items[1].markers, "non-network test should run with -m network"

    # Test 3: -m "not slow" → network tests should be skipped
    config = FakeConfig("not slow")
    items = [FakeItem("test_net", has_network_marker=True), FakeItem("test_other")]
    pytest_collection_modifyitems(config, items)
    assert any(
        m.name == "skip" for m in items[0].markers
    ), "network test should be skipped with -m 'not slow'"
    assert not items[1].markers, "non-network test should not be skipped"
