"""K2b-2: the kill-point crash harness for the cockpit broker's SPORE effects (K2B_DESIGN §7).

A RUN, not a reasoned test. Per spore verb x kill point: a broker SUBPROCESS (``tests/_k2b_crash_entry.py``,
``kill_at=`` passed as an argument) over a throwaway data root and a real anneal ``SporeStore`` drives one
reported write to the kill (exit 137). Then a CONTROL reads ``cockpit.db`` and the store BEFORE any
recovery and asserts the op is not terminal (and the store holds the pre-image or the consent value the
kill point implies), so a kill point that did not fire fails the control. Then a fresh in-process broker
recovers and the invariants are asserted: inv 3 (APPLIED exactly once, the store at the signed
post-values), inv 4 (every field of every ``changes`` holds its pre-image or its consent value).

Episode tombstone and section edit: see the K2b-3 block at the end. Not covered: inbox, decision, config and policy effects.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from anneal_memory.spores import SporeStore

from levain.cockpit.broker import Broker, BrokerError, Lifecycle
from levain.cockpit.consent import value_sha256
from levain.cockpit.effects import Effect, SporeEffectStore, TransientApplyFault

ENTRY = Path(__file__).with_name("_k2b_crash_entry.py")
BROWSER = {"class": "token", "name": "browser"}
KILL_POINTS = ("issued", "verified", "decided", "applied:0", "journaled:0", "finished")
# what the journal and the store hold when the process dies at each point, BEFORE any recovery
EXPECT_STATE = {"issued": "WAITING", "verified": "WAITING", "decided": "DECIDED", "applied:0": "DECIDED",
                "journaled:0": "DECIDED", "finished": "COMMITTED"}
EXPECT_STATUS = {"decided": "PLANNED", "applied:0": "PLANNED", "journaled:0": "APPLIED",
                 "finished": "APPLIED"}
STORE_APPLIED = ("applied:0", "journaled:0", "finished")      # the store write is durable from here

# fixture name -> (verb, params, origin_key of the target row or None for a create)
VERBS = {
    "seed": ("spore_seed", {"text": "captured", "type": "task"}, None),
    "update_text": ("spore_update", {"text": "edited"}, "k.loop"),
    "set_disposition": ("spore_set_disposition", {"disposition": "note"}, "k.seed"),
    "surface_at": ("spore_surface_at", {"surface_at": "2026-10-11"}, "k.loop"),
    "descend": ("spore_descend", {"spore_kind": "done"}, "k.loop"),
    "ascend": ("spore_ascend", {"spore_kind": "project", "ref": "commit abc"}, "k.loop"),
    "undo_edit": ("spore_undo_edit", {"restore": {"tier": "cold"}}, "k.loop"),
    "undo_seed": ("spore_undo_seed", {}, "k.seed"),
}


from tests.autonomic_confirm_keys import account_home

@pytest.fixture
def home() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix=".levain-k2b-crash-", dir=account_home()))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def spores(home: Path) -> Path:
    p = home / "spores.json"
    st = SporeStore(p)
    st.add(type="task", text="a loop", tier="warm", origin_key="k.loop")
    st.add(type="thought", text="a tray seed", disposition="seed", origin_key="k.seed")
    return p


# --- helpers -----------------------------------------------------------------------------------------

def _row(spores: Path, key: str) -> dict[str, Any] | None:
    return SporeStore(spores).get_by_origin_key(key)


def _count(spores: Path, key: str) -> int:
    """Rows carrying ``origin_key`` in the raw store file, resolved ones included."""
    def walk(o: Any) -> int:
        if isinstance(o, dict):
            return (1 if o.get("origin_key") == key else 0) + sum(walk(v) for v in o.values())
        if isinstance(o, list):
            return sum(walk(v) for v in o)
        return 0
    raw = json.loads(spores.read_text())     # the "deleted" ledger of tombstones is not a row
    return walk(raw.get("spores", [])) + walk(raw.get("resolved", []))


def _target(spores: Path, key: str | None) -> dict[str, Any]:
    if key is None:
        return {"kind": "create", "panel_id": "tray"}
    row = _row(spores, key)
    assert row is not None
    version = SporeEffectStore(spores).snapshot(f"spore:{key}").version
    return {"kind": "row", "panel_id": "tray", "row_id": f"spore:{row['id']}", "row_version": version}


def _sql(data: Path, sql: str, *args: Any) -> list[tuple]:
    conn = sqlite3.connect(data / "cockpit.db")
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _ops(data: Path) -> list[tuple[str, str]]:
    return _sql(data, "SELECT pending_id, state FROM pendings")


def _effects(data: Path, pid: str) -> list[tuple]:
    return _sql(data, "SELECT idx, status, native_id, reason, resource_key FROM effects "
                      "WHERE operation_id = ? ORDER BY idx", pid)


def _record(data: Path, pid: str) -> dict[str, Any]:
    return json.loads(_sql(data, "SELECT record FROM pendings WHERE pending_id = ?", pid)[0][0])


def _consent_effects(data: Path, pid: str) -> list[Effect]:
    return [Effect.from_record(e) for e in _record(data, pid)["effects"]]


def _run(home: Path, spores: Path, name: str, kill_at: str | None, *, key: str = "idem-1",
         verb: tuple | None = None) -> subprocess.CompletedProcess:
    v, params, ok = verb or VERBS[name]
    cfg = {"data_root": str(home / "data"), "spores": str(spores), "kill_at": kill_at, "verb": v,
           "params": params, "target": _target(spores, ok), "idempotency_key": key}
    return subprocess.run([sys.executable, str(ENTRY), json.dumps(cfg)], capture_output=True, text=True,
                          timeout=120, cwd=str(Path(__file__).parent.parent))


def _broker(home: Path, spores: Path, store: Any = None, **kw: Any) -> Broker:
    b = Broker(home / "data", stores={"spores": store or SporeEffectStore(spores)},
               store_paths=[spores], install_class="reported", **kw)
    b.start()
    return b


def _field_state(spores: Path, key: str, field: str, value: Any, pre_row: dict | None) -> set[str]:
    """What one field of one effect's ``changes`` holds now: {'consent'}, {'pre'}, both (the field was
    unchanged by the effect) or {'other'}."""
    row = _row(spores, key)
    if field == "deleted":
        got_consent, got_pre = row is None, row is not None
    elif row is None:
        got_consent, got_pre = False, pre_row is None
    else:
        have = _project(row, field)
        got_consent = _contains(have, value)
        got_pre = pre_row is not None and _project(pre_row, field) == have
        if pre_row is None:
            got_pre = False
    return {n for n, ok in (("consent", got_consent), ("pre", got_pre)) if ok} or {"other"}


def _project(row: dict, field: str) -> Any:
    return row.get(field) or None


def _contains(have: Any, want: Any) -> bool:
    if isinstance(want, dict) and isinstance(have, dict):
        return all(_contains(have.get(k), v) for k, v in want.items())
    return (have or None) == (want or None)


def _assert_inv4(spores: Path, effects: list[Effect], pre_rows: dict[str, dict | None],
                 allowed: tuple[str, ...]) -> None:
    for e in effects:
        key = e.resource_key.removeprefix("spore:")
        for field, value in e.changes.items():
            st = _field_state(spores, key, field, value, pre_rows.get(key))
            assert st & set(allowed), (f"inv 4: {key}.{field} holds {st}, wanted one of {allowed}: "
                                   f"{_row(spores, key)}")


def _assert_inv3(home: Path, spores: Path, pid: str, effects: list[Effect]) -> None:
    rows = _effects(home / "data", pid)
    assert len(rows) == len(effects)
    for (idx, status, native_id, reason, rk), e in zip(rows, effects):
        assert status == "APPLIED", f"effect {idx} is {status}: {reason}"
        key = rk.removeprefix("spore:")
        if e.is_create:
            assert _count(spores, key) == 1, "a create applied exactly once, by origin_key"
            assert native_id == _row(spores, key)["id"], "the create's native_id is the created row"
        elif e.is_delete:
            assert _count(spores, key) == 0
        else:
            assert _count(spores, key) == 1


# --- the base matrix: verb x kill point --------------------------------------------------------------

@pytest.mark.parametrize("point", KILL_POINTS)
@pytest.mark.parametrize("name", list(VERBS))
def test_kill_matrix(home: Path, spores: Path, name: str, point: str) -> None:
    verb, params, ok = VERBS[name]
    data = home / "data"
    pre_rows = {k: _row(spores, k) for k in ("k.loop", "k.seed")}

    proc = _run(home, spores, name, point)
    assert proc.returncode == 137, f"the kill point {point!r} did not fire: rc={proc.returncode}\n{proc.stderr}"

    # --- CONTROL: read the journal and the store before any recovery -------------------------------
    ops = _ops(data)
    assert len(ops) == 1
    pid, state = ops[0]
    assert state == EXPECT_STATE[point], f"at the kill the op is {state}, not {EXPECT_STATE[point]}"
    if point != "finished":
        assert state in ("WAITING", "DECIDED"), "CONTROL: the op must NOT be terminal at the kill"
    else:
        assert state == "COMMITTED", "the finished point fires after the terminal commit"
    effects = _consent_effects(data, pid)
    jrows = _effects(data, pid)
    if point in EXPECT_STATUS:
        assert [r[1] for r in jrows] == [EXPECT_STATUS[point]] * len(effects)
    else:
        assert jrows == [], "no effect row exists before the commit point"
    if ok is None:
        key = effects[0].resource_key.removeprefix("spore:")
        pre_rows[key] = None
    # inv 4 at the kill: the pre-image until the store write is durable, the consent value after
    _assert_inv4(spores, effects, pre_rows, ("consent",) if point in STORE_APPLIED else ("pre",))

    # --- recover in a fresh process-equivalent: a new broker on the same root ----------------------
    b = _broker(home, spores)
    try:
        assert b.state is Lifecycle.ACTIVE
        b.recover()
        if point in ("issued", "verified"):
            # CONTROL: recovery settles only DECIDED ops; an undecided reported pending stays put and
            # nothing is applied until the idempotent re-drive decides it
            assert _ops(data) == [(pid, "WAITING")]
            _assert_inv4(spores, effects, pre_rows, ("pre",))
            out = b.issue(verb=verb, params=params, target=_target(spores, ok), tier="T2",
                          credential=BROWSER, intent="reported", idempotency_key="idem-1")
            assert out["pending_id"] == pid, "the replay found the pending the dead process made"
        assert _wait_state(data, pid, "COMMITTED"), _ops(data)
    finally:
        b.stop()

    # --- inv 3 / inv 4 after recovery ---------------------------------------------------------------
    _assert_inv3(home, spores, pid, effects)
    _assert_inv4(spores, effects, pre_rows, ("consent",))
    assert len(_ops(data)) == 1, "recovery made no second op"
    # exactly-once survives a second recovery pass
    b2 = _broker(home, spores)
    try:
        b2.recover()
    finally:
        b2.stop()
    _assert_inv3(home, spores, pid, effects)
    assert _ops(data) == [(pid, "COMMITTED")]


def _wait_state(data: Path, pid: str, state: str, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if dict(_ops(data)).get(pid) == state:
            return True
        time.sleep(0.05)
    return dict(_ops(data)).get(pid) == state


# --- inv 6: cancel vs decide -------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["update_text", "seed", "descend"])
def test_cancelled_before_kill_stays_cancelled(home: Path, spores: Path, name: str) -> None:
    """A pending killed at ``issued`` and then cancelled by its issuer is never fired by recovery or by
    the idempotent replay (the cancel/decide race ends in exactly one terminal state)."""
    verb, params, ok = VERBS[name]
    data = home / "data"
    assert _run(home, spores, name, "issued").returncode == 137
    pid, state = _ops(data)[0]
    assert state == "WAITING"                                           # CONTROL
    pre = {k: _row(spores, k) for k in ("k.loop", "k.seed")}
    b = _broker(home, spores)
    try:
        out = b.cancel(pid, credential=BROWSER)
        assert out["state"] == "CANCELLED"
        b.recover()
        replay = b.issue(verb=verb, params=params, target=_target(spores, ok), tier="T2",
                         credential=BROWSER, intent="reported", idempotency_key="idem-1")
        assert replay["state"] == "CANCELLED"
    finally:
        b.stop()
    assert _ops(data) == [(pid, "CANCELLED")]
    assert _effects(data, pid) == []
    assert {k: _row(spores, k) for k in ("k.loop", "k.seed")} == pre


class _Gated(Broker):
    """A broker whose ``verified`` kill point parks until released: the barrier §7 puts between the
    recompute and the decision commit."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.at_verified = threading.Event()
        self.release = threading.Event()

    def _kp(self, name: str) -> None:
        if name == "verified":
            self.at_verified.set()
            assert self.release.wait(20)
        super()._kp(name)


def test_cancel_wins_race_at_verified(home: Path, spores: Path) -> None:
    """The decider parks at ``verified``; the issuer's cancel commits; the decider's commit finds the
    pending no longer WAITING and fires nothing. Exactly one terminal state: CANCELLED."""
    b = _Gated(home / "data", stores={"spores": SporeEffectStore(spores)}, store_paths=[spores],
               install_class="reported")
    b.start()
    verb, params, ok = VERBS["update_text"]
    box: dict[str, Any] = {}

    def decide() -> None:
        try:
            box["out"] = b.issue(verb=verb, params=params, target=_target(spores, ok), tier="T2",
                                 credential=BROWSER, intent="reported", idempotency_key="race-1")
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc

    pre = _row(spores, ok)
    t = threading.Thread(target=decide)
    t.start()
    try:
        assert b.at_verified.wait(20), "CONTROL: the decider reached the verified barrier"
        pid, state = _ops(home / "data")[0]
        assert state == "WAITING"                                       # CONTROL: still undecided
        assert b.cancel(pid, credential=BROWSER)["state"] == "CANCELLED"
    finally:
        b.release.set()
        t.join(20)
        b.stop()
    assert "exc" not in box, box.get("exc")
    assert box["out"]["state"] == "CANCELLED"
    assert _ops(home / "data") == [(pid, "CANCELLED")]
    assert _effects(home / "data", pid) == []
    assert _row(spores, ok) == pre


def test_decide_wins_race_and_cancel_is_refused(home: Path, spores: Path) -> None:
    """The other order: a decided op is not cancellable, and stays in its one terminal state."""
    b = _broker(home, spores)
    verb, params, ok = VERBS["update_text"]
    try:
        out = b.issue(verb=verb, params=params, target=_target(spores, ok), tier="T2", credential=BROWSER,
                      intent="reported", idempotency_key="race-2")
        pid = out["pending_id"]
        with pytest.raises(BrokerError) as ei:
            b.cancel(pid, credential=BROWSER)
        assert ei.value.status == 409
    finally:
        b.stop()
    assert _ops(home / "data") == [(pid, "COMMITTED")]
    assert _row(spores, ok)["text"] == "edited"


# --- K2b-3: the anneal kinds (episode tombstone, section edit) x the same kill points -----------------

from anneal_memory import Store                                                    # noqa: E402

from levain.cockpit.anneal_effects import (                                         # noqa: E402
    EPISODES, SECTIONS, EpisodeEffectStore, SectionEffectStore)

NEW_BODY = "the crash-harness State body"


@pytest.fixture
def adb(home: Path) -> Path:
    p = home / "memory.db"
    (home / "memory.continuity.md").write_text(
        "# Continuity\n\n## State\nthe pre-image State body\n\n## Durable Facts\n- f1\n\n## Patterns\n- p1\n\n"
        "## Decisions\n- d1\n\n## Context\nctx\n", encoding="utf-8")
    with Store(str(p)) as st:
        st.record("the episode to tombstone", "observation", origin_key="ok.target")
        st.record("a neighbour that stays", "observation", origin_key="ok.other")
    return p


def _a_broker(home: Path, spores: Path, db: Path) -> Broker:
    b = Broker(home / "data", stores={"spores": SporeEffectStore(spores), EPISODES: EpisodeEffectStore(db),
                                      SECTIONS: SectionEffectStore(db)},
               store_paths=[spores, db], install_class="reported")
    b.start()
    return b


def _ep_target(db: Path) -> dict[str, Any]:
    with Store(str(db), read_only=True) as st:
        ep = st.get_by_origin_key("ok.target")
    got = EpisodeEffectStore(db).read("ok.target")
    assert ep is not None and got is not None
    return {"kind": "row", "panel_id": "episodes", "row_id": f"episode:{ep.id}", "row_version": got.rendered}


def _sec_target(db: Path) -> dict[str, Any]:
    got = SectionEffectStore(db).read("State")
    assert got is not None
    return {"kind": "value", "panel_id": "section:state", "panel_version": got.rendered}


ANNEAL = {"tombstone": ("episode_tombstone", {}, _ep_target),
          "section": ("section_edit", {"new_body": NEW_BODY}, _sec_target)}


def _episode_state(db: Path) -> tuple[bool, bool, int]:
    """(target episode present, neighbour present, origin_key 'ok.target' retired)."""
    with Store(str(db), read_only=True) as st:
        return (st.get_by_origin_key("ok.target") is not None, st.get_by_origin_key("ok.other") is not None,
                st.origin_key_status("ok.target").state == "retired")


def _state_body(db: Path) -> str:
    with Store(str(db), read_only=True) as st:
        return st.read_section("State")[0].strip()


def _store_is(kind: str, db: Path, which: str) -> bool:
    if kind == "tombstone":
        present, neighbour, retired = _episode_state(db)
        assert neighbour, "the neighbour episode must survive in every state"
        return (present and not retired) if which == "pre" else (not present and retired)
    body = _state_body(db)
    return body == ("the pre-image State body" if which == "pre" else NEW_BODY)


def _run_anneal(home: Path, spores: Path, db: Path, kind: str, kill_at: str | None) -> subprocess.CompletedProcess:
    verb, params, target = ANNEAL[kind]
    cfg = {"data_root": str(home / "data"), "spores": str(spores), "db": str(db), "kill_at": kill_at,
           "verb": verb, "params": params, "target": target(db), "idempotency_key": "idem-1"}
    return subprocess.run([sys.executable, str(ENTRY), json.dumps(cfg)], capture_output=True, text=True,
                          timeout=120, cwd=str(Path(__file__).parent.parent))


@pytest.mark.parametrize("kind", list(ANNEAL))
def test_anneal_control_no_kill(home: Path, spores: Path, adb: Path, kind: str) -> None:
    """CONTROL: the same drive with no kill point commits, applies once, and the store holds the effect."""
    proc = _run_anneal(home, spores, adb, kind, None)
    assert proc.returncode == 0, proc.stderr
    data = home / "data"
    (pid, state), = _ops(data)
    assert state == "COMMITTED"
    assert [r[1] for r in _effects(data, pid)] == ["APPLIED"] * len(_effects(data, pid))
    assert _store_is(kind, adb, "consent")


@pytest.mark.parametrize("point", KILL_POINTS)
@pytest.mark.parametrize("kind", list(ANNEAL))
def test_anneal_kill_matrix(home: Path, spores: Path, adb: Path, kind: str, point: str) -> None:
    verb, params, target = ANNEAL[kind]
    data = home / "data"
    assert _store_is(kind, adb, "pre")
    proc = _run_anneal(home, spores, adb, kind, point)
    assert proc.returncode == 137, f"the kill point {point!r} did not fire: rc={proc.returncode}\n{proc.stderr}"

    # --- CONTROL: journal and store before any recovery ---------------------------------------------
    (pid, state), = _ops(data)
    assert state == EXPECT_STATE[point], f"at the kill the op is {state}, not {EXPECT_STATE[point]}"
    jrows = _effects(data, pid)
    n = len(_record(data, pid)["effects"])
    if point in EXPECT_STATUS:
        assert [r[1] for r in jrows] == [EXPECT_STATUS[point]] * n
    else:
        assert jrows == [], "no effect row exists before the commit point"
    want = "consent" if point in STORE_APPLIED else "pre"
    assert _store_is(kind, adb, want), f"at {point} the store should hold the {want} state"

    # --- recover with a fresh broker on the same root ------------------------------------------------
    b = _a_broker(home, spores, adb)
    try:
        assert b.state is Lifecycle.ACTIVE
        b.recover()
        if point in ("issued", "verified"):
            assert _ops(data) == [(pid, "WAITING")]
            assert _store_is(kind, adb, "pre")
            tgt = target(adb)
            out = b.issue(verb=verb, params=params, target=tgt, tier="T2", credential=BROWSER,
                          intent="reported", idempotency_key="idem-1")
            assert out["pending_id"] == pid, "the replay found the pending the dead process made"
        assert _wait_state(data, pid, "COMMITTED"), _ops(data)
    finally:
        b.stop()

    # --- exactly once, consistent with the journal ---------------------------------------------------
    jrows = _effects(data, pid)
    assert len(jrows) == n and all(r[1] == "APPLIED" for r in jrows), jrows
    assert _store_is(kind, adb, "consent")
    assert len(_ops(data)) == 1
    b2 = _a_broker(home, spores, adb)
    try:
        b2.recover()
    finally:
        b2.stop()
    assert _ops(data) == [(pid, "COMMITTED")]
    assert [r[1] for r in _effects(data, pid)] == ["APPLIED"] * n
    assert _store_is(kind, adb, "consent")
