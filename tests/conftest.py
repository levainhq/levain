"""Shared pytest fixtures.

The drive-mode binding (`$LEVAIN_DRIVE_MODE`) is PROCESS-GLOBAL, and `bind_drive_mode` refuses a
rebind that would WIDEN the credential floor. pytest runs the whole suite in one process, so
without this a test that opens an unattended session would poison every later test that opens an
interactive one — cross-test contamination that is really the multi-session limitation showing up
in miniature (see `levain.firing.drive`). Each test gets a clean binding, i.e. simulates the fresh
process that a real `levain run` invocation always is.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clean_drive_mode_binding():
    from levain.firing.drive import LEVAIN_DRIVE_MODE_ENV

    os.environ.pop(LEVAIN_DRIVE_MODE_ENV, None)
    try:
        yield
    finally:
        os.environ.pop(LEVAIN_DRIVE_MODE_ENV, None)
