"""TEST ONLY: let a binding's effect run on a test executor, which no production configuration can do.

No executor can prove today that it runs a binding's effect through the confinement floor as the
entity's separate hands user (the M2 hands wiring does not exist), so the gate refuses every binding
effect up front: ``levain.autonomic.gate._executor_is_confined`` is ``False`` for every executor. The
tests that exercise the journal, the decision and the fire path behind that refusal replace the function
here, for the one test, through pytest's ``monkeypatch`` (undone when the test ends). There is no
constructor argument, attribute or setting that does this: only code that patches the module can.
"""
from __future__ import annotations

import pytest

import levain.autonomic.gate as _gate


def assume_confined_for_this_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """For this test only, treat every executor as confined. (The patch raises if the function is
    renamed, so a seam that silently stopped applying cannot pass for one that works.)"""
    monkeypatch.setattr(_gate, "_executor_is_confined", lambda executor: True)


@pytest.fixture
def test_only_confined_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    assume_confined_for_this_test(monkeypatch)
