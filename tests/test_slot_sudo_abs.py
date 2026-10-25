"""The root-helper call names sudo by absolute path: a bare ``sudo`` lets a PATH-dir plant answer for the helper."""
from __future__ import annotations

import os

from levain.slot import adopt_view, supervisor


def test_supervisor_helper_argv0_is_absolute() -> None:
    s = supervisor.Seams()
    assert os.path.isabs(s.helper[0]) and s.helper[0] == supervisor.SUDO


def test_adopt_view_helper_argv0_is_absolute() -> None:
    s = adopt_view.Seams()
    assert os.path.isabs(s.helper[0]) and s.helper[0] == supervisor.SUDO
