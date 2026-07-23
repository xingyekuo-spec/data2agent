"""Central test classification used by local verification and CI."""

from __future__ import annotations

from pathlib import Path

import pytest


CONTRACT_FILES = {
    "test_console_contract.py",
    "test_console_m5_contracts.py",
    "test_dataset_publish_contract.py",
    "test_lineage_contract.py",
    "test_mapping_preview_contract.py",
}

SLOW_FILES = {
    "test_publish_concurrency.py",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Assign every test to one primary layer without annotations in 60+ files."""

    for item in items:
        path = Path(str(item.path))
        if "integration" in path.parts:
            item.add_marker(pytest.mark.integration)
        elif path.name in CONTRACT_FILES:
            item.add_marker(pytest.mark.contract)
        else:
            item.add_marker(pytest.mark.unit)

        if path.name in SLOW_FILES or "integration" in path.parts:
            item.add_marker(pytest.mark.slow)
