"""levain#157 S3c (D7): a verb a signature let through (T2, T3) answers WAITING at its own route, so the server's ``_note_fired``
never sees its effect and an attached cockpit would tell its host nothing of the write. ``_note_signed`` keys the write by the
verb's registry ``touched`` from the signature's answer, for an attached cockpit only: a bare one keeps invalidating every read."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from levain.cockpit.providers import build_default_cockpit
from levain.cockpit.verbs import VerbRegistry
from tests.test_cockpit_k2a import _install

VERB = "intention_surface_at"


@pytest.fixture()
def rig(tmp_path: Path) -> SimpleNamespace:
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    reg = VerbRegistry()
    assert reg.get(VERB) is not None
    return SimpleNamespace(ck=ck, reg=reg)


def _note(rig: SimpleNamespace, answer: Any, *, attached: bool = True) -> "frozenset[str] | None":
    from levain.web_server import _Handler

    cockpit = SimpleNamespace(attached=attached, panel_ids=rig.ck.panel_ids, spec=rig.ck.spec)
    me = SimpleNamespace(server=SimpleNamespace(cockpit=cockpit, verb_registry=rig.reg), _write_touched=None)
    _Handler._note_signed(me, answer)
    return me._write_touched


def _answer(**kw: Any) -> dict[str, Any]:
    return {"ok": True, "state": "COMMITTED", "replay": False, "verb": VERB, "panel_id": "tray", **kw}


def test_a_signed_verb_that_landed_keys_the_write_by_the_panels_the_registry_says_it_touched(rig) -> None:
    want = frozenset(rig.reg.touched(rig.ck, rig.reg.get(VERB), "tray"))
    assert "tray" in want
    assert _note(rig, _answer()) == want
    assert _note(rig, _answer(state="COMMITTED_INCOMPLETE")) == want      # some effect applied: the panels may have moved


def test_a_bare_cockpit_keeps_dropping_every_read_for_a_signed_verb(rig) -> None:
    assert _note(rig, _answer(), attached=False) is None


@pytest.mark.parametrize("answer", [
    _answer(replay=True),                       # the second submit of one signature fires nothing
    _answer(state="WAITING"),
    _answer(state="REFUSED"),
    _answer(state="EXPIRED"),
    _answer(state="applying"),                  # decided, not committed: a derive begun now could read the older state
])
def test_an_answer_that_applied_nothing_leaves_the_write_unkeyed(rig, answer) -> None:
    assert _note(rig, answer) is None


def test_a_verb_the_registry_does_not_know_or_an_answer_without_a_panel_leaves_the_write_unkeyed(rig) -> None:
    assert _note(rig, _answer(verb="pending_cancel")) is None
    assert _note(rig, _answer(verb=None)) is None
    assert _note(rig, {k: v for k, v in _answer().items() if k != "panel_id"}) is None
    assert _note(rig, "not a dict") is None


def test_a_registry_that_raises_after_the_signature_was_spent_leaves_the_write_unkeyed_and_fails_nothing(rig) -> None:
    real = rig.reg

    class Raising:
        def get(self, name: str) -> Any:
            return real.get(name)

        def touched(self, *a: Any) -> Any:
            raise RuntimeError("the registry cannot say")

    rig.reg = Raising()
    assert _note(rig, _answer()) is None
