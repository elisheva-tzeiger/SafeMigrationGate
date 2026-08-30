import pytest


def pytest_collection_modifyitems(config, items):
    """Network tests are opt-in: run them with `-m network`."""
    if config.getoption("-m"):
        return
    skip = pytest.mark.skip(reason="needs network; run with -m network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)
