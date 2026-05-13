"""Mark every unit test with the ``unit`` marker without per-file decoration."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items) -> None:  # noqa: ARG001
    for item in items:
        item.add_marker(pytest.mark.unit)
