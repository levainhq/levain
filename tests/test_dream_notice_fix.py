"""D1-N fix round, finding 1: the store directory is REQUIRED wherever a card prints a command that needs it (the class
of the closed MED, in the siblings), and the tick reads ``TickResult.missed_windows`` directly."""
from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest

from levain.agents import controller as ctl
from levain.agents import tick_driver as T
from levain.dream import agent as da
from levain.dream import card as cd
from tests.test_dream_notices import Night, night  # noqa: F401  (the fixture)

LEVAIN = Path(da.__file__).resolve().parents[1]


def test_remedy_for_needs_a_store_dir_and_refuses_a_blank_one():
    sig = inspect.signature(cd.remedy_for).parameters["store_dir"]
    assert sig.default is inspect.Parameter.empty and sig.kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        cd.remedy_for("consolidated", "o1")                                       # type: ignore[call-arg]
    for bad in (None, "", "  "):
        with pytest.raises(ValueError):
            cd.remedy_for("consolidated", "o1", store_dir=bad)                    # type: ignore[arg-type]


def test_build_card_and_from_spec_cannot_omit_the_store_dir(tmp_path):
    for fn in (cd.build_card, cd.from_spec):
        p = inspect.signature(fn).parameters["store_dir"]
        assert p.default is inspect.Parameter.empty and p.kind is inspect.Parameter.KEYWORD_ONLY, fn.__name__
    with pytest.raises(TypeError):
        cd.build_card("consolidated", occurrence="o1")                            # type: ignore[call-arg]
    with pytest.raises(TypeError):
        cd.from_spec({"kind": "failed", "occurrence": "o1"}, occ_dir=tmp_path)    # type: ignore[call-arg]
    # a card whose remedy the CALLER supplies has no use for the directory, and says so in the one way: None
    shadow = cd.build_card("consolidated", occurrence="o1", store_dir=None, remedy="nothing to undo")
    assert shadow.body["remedy"] == "nothing to undo"
    # but None with no remedy is a card that would print a command with no store in it: refused, not printed
    with pytest.raises(ValueError):
        cd.build_card("consolidated", occurrence="o1", store_dir=None)


def test_the_signing_card_cannot_omit_the_store_dir():
    p = inspect.signature(da.signing_card_text).parameters["store_dir"]
    assert p.default is inspect.Parameter.empty and p.kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        da.signing_card_text("America/New_York", per_run_usd=1.0, night_usd=2.0, expires="x")   # type: ignore[call-arg]


def test_no_placeholder_for_a_missing_store_dir_is_left_in_the_source():
    for path in sorted(LEVAIN.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "<the store's directory>" not in text, path
        assert "_STORE_DIR_UNKNOWN" not in text, path


def test_the_tick_reads_missed_windows_directly_and_the_result_has_the_field(night, monkeypatch):
    assert "missed_windows" in {f.name for f in dataclasses.fields(ctl.TickResult)}
    assert 'getattr(result, "missed_windows"' not in inspect.getsource(T)

    result = ctl.TickResult()
    del result.missed_windows                      # what a renamed field looks like to the tick
    monkeypatch.setattr(ctl, "tick", lambda *a, **k: result)
    assert night.tick() == T.EXIT_NOTICES          # loud, not a silent "no laptop-slept notice"
    assert "missed_windows" in night.err
