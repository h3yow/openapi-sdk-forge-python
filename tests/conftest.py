"""Shared pytest configuration.

Ensures the contract-input and decision-basis log lines written by the
compatibility checker and malformed-contract tests are visible when the suite
runs with live logging (``uv run pytest --log-cli-level=INFO``), without
flooding ordinary runs.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _propagate_forge_loggers(caplog):
    # caplog attaches its handler at the root; every test using caplog at
    # INFO level will therefore capture sdk_forge.* / test.* records.
    caplog.set_level(logging.INFO, logger="sdk_forge")
    caplog.set_level(logging.INFO, logger="test")
    yield
