"""K2b-3a section adapter: a reported section_edit of the Class-A State panel through dispatch and the
broker, over a real anneal store (SECTION_ADAPTER_DESIGN.md). Held from the L4 drive's reproduced
failures: every edit past the verbs-level version check 500'd (heading_for read a property as a method),
and the lost update the design closes (another writer between render and issue)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from anneal_memory import Store

from levain.cockpit.anneal_effects import SECTIONS, EpisodeEffectStore, SectionEffectStore
from levain.cockpit.broker import Broker
from levain.cockpit.effects import SporeEffectStore
from levain.cockpit.verbs import dispatch, retier
from tests.test_cockpit_k2a import CRED, INSTALL_CLASS, Rig, _refused
from tests.test_cockpit_k2b_broker import Key, _laptop_floor, _refused as _broker_refused, _rows, _state as _pending_state, home, keydir  # noqa: F401


@pytest.fixture
def rig_broker(home: Path):
    rig = Rig(home)
    db = Path(rig.src.anneal.episodic_db)
    spores = rig.root / ".levain" / "memory.spores.json"
    b = Broker(home / "data",
               stores={"spores": SporeEffectStore(spores), "episodes": EpisodeEffectStore(db),
                       SECTIONS: SectionEffectStore(db)},
               store_paths=[spores, db], install_class="reported",
               tier_of=lambda verb, params, target: retier(rig.reg, rig.ck, verb, dict(params), dict(target)))
    b.start()
    try:
        yield rig, b, db
    finally:
        b.stop()


def _state(db: Path) -> tuple[str, str]:
    with Store(str(db), read_only=True) as st:
        return st.read_section("State")


def _pv(rig: Rig) -> str:
    return rig.ck.panel("section:state")["value"]["value_version"]


def _edit(rig: Rig, b: Broker, pv: str, text: str, key: str, **kw: Any) -> dict[str, Any]:
    req = {"verb": "section_edit", "panel_id": "section:state", "params": {"new_body": text},
           "panel_version": pv, "confirm": True, "intent": "reported", "idempotency_key": key, "tier_seen": "T2", "gesture_seen": "direct-reported", **kw}
    return dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, req=req,
                    credential=CRED, install_class=INSTALL_CLASS, route="cockpit", broker=b)


def test_a_reported_section_edit_commits_and_a_replay_does_not_apply_twice(rig_broker):
    rig, b, db = rig_broker
    pv = _pv(rig)
    out = _edit(rig, b, pv, "the new State body", "sec-1")
    assert out["state"] == "COMMITTED", out
    assert _state(db)[0].strip() == "the new State body"
    again = _edit(rig, b, pv, "the new State body", "sec-1")
    assert again.get("replay") is True and again["pending_id"] == out["pending_id"], again
    assert _state(db)[0].strip() == "the new State body"


def test_a_write_on_a_render_another_writer_outdated_is_stale_and_their_text_survives(rig_broker):
    rig, b, db = rig_broker
    pv = _pv(rig)
    text, version = _state(db)
    with Store(str(db)) as w:
        assert w.replace_section("State", text + "\n- another writer", expected_version=version).outcome == "written"
    _refused("stale", _edit, rig, b, pv, "the operator's edit on the old render", "sec-lost")
    assert _state(db)[0].endswith("- another writer")


def test_an_open_wrap_refuses_the_edit_and_state_is_unchanged(rig_broker):
    rig, b, db = rig_broker
    before = _state(db)
    with Store(str(db)) as w:
        w.wrap_started(token="sec-test-wrap", episode_ids=[])
    try:
        err = None
        try:
            _edit(rig, b, _pv(rig), "during a wrap", "sec-wrap")
        except Exception as exc:  # BrokerError or EditError, whichever layer carries the code
            err = exc
        assert err is not None and getattr(err, "code", None) == "wrap_in_progress", repr(err)
        assert _state(db) == before
    finally:
        with Store(str(db)) as w:
            w.wrap_cancelled(expect_token="sec-test-wrap")


def test_a_body_with_a_header_line_is_refused_at_issue_and_nothing_is_consented(rig_broker):
    rig, b, db = rig_broker
    before = _state(db)
    err = _refused("invalid_body", _edit, rig, b, _pv(rig), "fine line\n## Next\nmore", "sec-hdr")
    assert err.http_status == 400
    assert _rows(b, "SELECT COUNT(*) FROM pendings")[0][0] == 0
    assert _state(db) == before


def test_a_wrap_opened_after_issue_leaves_the_signature_unspent_until_it_saves(rig_broker, keydir):
    rig, b, db = rig_broker
    laptop = Key(keydir, "laptop").enroll(b, "laptop-presence")
    before = _state(db)
    out = _edit(rig, b, _pv(rig), "signed State body", "sec-sign", intent=None)
    assert out["state"] == "WAITING", out
    pid, signature = out["pending_id"], laptop.sign(out["record"].encode())
    with Store(str(db)) as w:
        w.wrap_started(token="sec-decide-wrap", episode_ids=[])
    try:
        _broker_refused("source_unavailable", b.submit, pid, signature)
        assert _pending_state(b, pid) == "WAITING"
        assert _state(db) == before
    finally:
        with Store(str(db)) as w:
            w.wrap_cancelled(expect_token="sec-decide-wrap")
    done = b.submit(pid, signature)
    assert done["state"] == "COMMITTED", done
    assert _state(db)[0].strip() == "signed State body"
