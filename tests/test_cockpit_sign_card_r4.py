"""The sign card's L3 r4 fixes (project_memory/1023-30/d5/FIX4_BRIEF.md H1, H2), over the rig of test_cockpit_sign_card.

H1: a make_server that fails part way through attaching undoes only its own attaches: the attach is one transaction under
the cockpit's lock, so a panel another thread registers while it runs is registered after the undo, never deleted by it.
H2: the compose bindings are bounded. Past COMPOSE_LIVE_MAX live bindings on a queue a NEW draft's compose is refused 429
``too_many_open`` with the words the operator reads, while a draft already bound is still answered. A bindings file over
the read limit anyway is pruned by the next compose once its requests have left, never a state only a hand can fix."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from levain.cockpit import ProviderSpec, Read
from levain.consent import sign
from levain.consent.sign import COMPOSE_BINDINGS, sign_dir
from levain.web_server import make_server
from tests.test_cockpit_sign_card import _Rig
from tests.test_cockpit_sign_card_r1 import _compose
from tests.test_cockpit_sign_card_r2 import _c1_rig, _signed
from tests.test_cockpit_sign_card_r3 import _send


# --- H1: the undo of a failed attach keeps what anyone else registered -------------------------------------------------


def _late() -> ProviderSpec:
    return ProviderSpec(id="late", kind="list", title="Late", priority="feed", order="time.desc", version_fields=("id",),
                        facets=frozenset({"source"}), read=lambda c: Read(rows=()))


def test_a_panel_registered_while_a_failed_attach_runs_survives_its_undo(tmp_path, monkeypatch) -> None:
    """Another thread registers a panel while make_server's attach is running, after it took what to put back, and the
    attach then raises: the undo puts back the cockpit's own attaches and the other thread's panel is registered."""
    rig = _Rig(tmp_path, monkeypatch)
    real = rig.ck._install_decisions_locked
    other: list[threading.Thread] = []
    failed: list[BaseException] = []

    def register() -> None:
        try:
            rig.ck.register(_late())
        except BaseException as exc:      # noqa: BLE001 - reported by the assertion below
            failed.append(exc)

    def boom() -> None:
        real()
        if not other:                    # once: the other thread registers while this attach runs, then it raises
            other.append(threading.Thread(target=register))
            other[0].start()
            other[0].join(0.5)           # done (no lock held here) or waiting on the cockpit's lock
            raise RuntimeError("the decisions panel was refused")

    monkeypatch.setattr(rig.ck, "_install_decisions_locked", boom)
    with pytest.raises(RuntimeError, match="was refused"):
        make_server(rig.src, host="127.0.0.1", **rig.kw)
    other[0].join(10)
    assert not other[0].is_alive() and failed == []
    assert "late" in rig.ck._specs and "late" in rig.ck._state
    assert rig.ck._verb_view is None and rig.ck._capture is None


# --- H2: the bindings are capped, and an oversize store recovers --------------------------------------------------------


def _store(rig: _Rig) -> Any:
    return sign_dir(rig.qdir) / COMPOSE_BINDINGS


def test_past_the_cap_a_new_draft_is_refused_and_a_bound_draft_is_still_answered(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sign, "COMPOSE_LIVE_MAX", 3)
    rig = _c1_rig(tmp_path, monkeypatch)
    with rig.serving() as (url, _httpd):
        ids = [_compose(url, f"m{i}", f"draft-{i:04d}-capx")[1]["request"] for i in range(3)]
        over = _compose(url, "m3", "draft-0003-capx")                  # a fourth draft: refused, nothing filed
        filed = len(rig.composed)
        again = _compose(url, "m1", "draft-0001-capx")                 # a bound draft's retry: its own request
        _signed(rig, ids[2])
        recomposed = _compose(url, "m2", "draft-0002-capx")            # a bound draft whose request was signed
        sent = _send(url, ids[2], "m2", "draft-0002-capx")
    assert over[0] == 429 and over[1]["error"] == "too_many_open", over
    assert "finish or deny open requests first" in over[1]["message"], over
    assert filed == 3, rig.composed
    assert again == (200, {"request": ids[1]}), again
    assert recomposed[0] == 200 and recomposed[1]["request"] not in ids, recomposed
    assert sent[0] == 200, sent
    assert rig.handled == [{"message": "m2", "sign_request": ids[2]}], rig.handled


def test_the_cap_counts_after_the_prune(tmp_path, monkeypatch) -> None:
    """A binding whose request left (denied here) frees its place before the cap is checked."""
    monkeypatch.setattr(sign, "COMPOSE_LIVE_MAX", 2)
    rig = _c1_rig(tmp_path, monkeypatch)
    with rig.serving() as (url, _httpd):
        ids = [_compose(url, f"m{i}", f"draft-{i:04d}-prun")[1]["request"] for i in range(2)]
        (sign_dir(rig.qdir) / f"{ids[0]}.denied").write_text("{}")
        third = _compose(url, "m2", "draft-0002-prun")
    assert third[0] == 200, third
    assert {b["request"] for b in json.loads(_store(rig).read_text())} == {ids[1], third[1]["request"]}


def test_an_oversize_store_is_pruned_by_the_next_compose_once_its_requests_left(tmp_path, monkeypatch) -> None:
    rig = _c1_rig(tmp_path, monkeypatch)
    d = sign_dir(rig.qdir)
    with rig.serving() as (url, _httpd):
        ids = [_compose(url, f"m{i}", f"draft-{i:04d}-size")[1]["request"] for i in range(4)]
        size = _store(rig).stat().st_size
        monkeypatch.setattr(sign, "_BINDINGS_MAX", size - 1)           # the store is over the read limit now
        refused = _send(url, ids[0], "m0", "draft-0000-size")
        for rid in ids:                                                 # every request expires unsigned
            req = json.loads((d / f"{rid}.json").read_text())
            req["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            (d / f"{rid}.json").write_text(json.dumps(req))
        fresh = _compose(url, "new", "draft-0009-size")
        rid = fresh[1].get("request") if fresh[0] == 200 else None
        if rid is not None:
            _signed(rig, rid)
        sent = _send(url, rid or "np-none-0000", "new", "draft-0009-size")
    assert refused[0] == 503, refused
    assert fresh[0] == 200, fresh
    assert [b["request"] for b in json.loads(_store(rig).read_text())] == [rid]
    assert sent[0] == 200, sent
