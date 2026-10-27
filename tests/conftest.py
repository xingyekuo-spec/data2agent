"""Central test classification used by local verification and CI."""

from __future__ import annotations

from pathlib import Path

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Assign every test to one primary layer without annotations in 60+ files."""

    for item in items:
        path = Path(str(item.path))
        if "integration" in path.parts:
            item.add_marker(pytest.mark.integration)
            item.add_marker(pytest.mark.slow)
        elif "contract" in path.parts:
            item.add_marker(pytest.mark.contract)
        else:
            item.add_marker(pytest.mark.unit)
