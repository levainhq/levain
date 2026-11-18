"""levain#121 lane L1: what a REAL ``dream start`` fire settles when the stub ``claude`` reports a spend above the run
reservation (item 1) or a spend that cannot be read (item 2), observed through the CLI and the stores it wrote.

Every case runs the product's own chain (``dream start`` -> the detached ``_fire`` process -> ``ClaudeMind`` child ->
``usage.json`` -> settle); only the model binary's stdout is canned (the install fact ``claude_bin`` names the rig's
stub). Each case PRINTS what the fire did (run with ``-s``) and then pins the whole outcome: the row, the reservation, the
agent, ``usage.json``, the card queue and what the next start says.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("cryptography")
from tests._p10a_rig import Install  # noqa: E402
from tests._p10a_stub import success_doc  # noqa: E402
from tests.test_dream_executor import _VALID  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

RESERVED = 20_000_000          # the link's budget_run_usd_micros (link_doc), the reservation the row takes


# ------------------------------------------------------------------------------------------------------- helpers
def _start(tmp_path, reply, *, sleep=None) -> Install:
    """A signed install whose stub answers ``reply``, and a REAL ``dream start`` (the fire is detached)."""
    inst = Install(tmp_path, stub=True)
    assert inst.enable("--time", "00:00").rc == 0
    assert inst.sign(inst.link_doc()).rc == 0
    inst.stub.reply(reply)
    if sleep:
        inst.stub.sleep(sleep)
    started = inst.cli("dream", "start", spawn_gate=False)
    assert started.rc == 0 and "claimed and launched oc-" in started.out, started.text
    return inst


def _wait(inst: Install, what: str, ok, timeout: float = 100.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = ok()
        if got:
            return got
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what}: {inst.rows()}")


def _ended(inst: Install) -> dict:
    """The one run row once the fire has settled it (``ended_at`` set); bounded."""
    def ok():
        rows = inst.rows()
        return rows[0] if rows and rows[0]["ended_at"] else None
    return _wait(inst, "the fire to end the row", ok)


def _usage(inst: Install, occ: str) -> dict | None:
    p = inst.anneal / "dream" / occ / "usage.json"
    return json.loads(p.read_text()) if p.exists() else None


def _cards(inst: Install) -> list:
    """The agent-feed cards on the desk queue the dream files into (open or cleared): (title, state) per item."""
    got = inst.cli("desk", "review", "list", "--queue-dir", str(inst.desk), "--feed", "agent", "--all", "--json",
                   spawn_gate=False)
    assert got.rc == 0, got.text
    return json.loads(got.out)


def _picture(inst: Install, label: str) -> dict:
    """Print and return everything a settle leaves: row, reservation, agent, slots, usage.json, cards."""
    (row,) = inst.rows()
    (res,) = inst.rows("SELECT * FROM agent_reservations")
    (agent,) = inst.rows("SELECT * FROM agents")
    pic = {"row": (row["state"], row["reason"]),
           "res": (res["state"], res["reserved_usd_micros"], res["debited_usd_micros"], res["settled_reason"]),
           "agent": (agent["operation"], agent["failure_streak"]),
           "slots": inst.rows("SELECT * FROM agent_slots"),
           "usage": _usage(inst, row["occurrence_id"]),
           "cards": _cards(inst),
           "stub_calls": inst.stub.calls()}
    print(f"\n[{label}]", json.dumps({k: v for k, v in pic.items() if k != "usage"}, default=str))
    u = pic["usage"]
    print(f"[{label}] usage.json:", None if u is None else
          {k: u[k] for k in ("readable", "actual_usd_micros", "reserved_usd_micros", "outcome", "source")})
    return pic


# ------------------------------------------------------------------------------- (1) an actual above the reservation
def test_a_fire_whose_actual_is_above_the_reservation_is_debited_in_full_and_pauses_the_agent(tmp_path):
    """The stub reports 30 USD against the 20 USD reservation. The fire reads it (``usage.json`` readable, 30 USD), the
    settle debits the whole 30 (not the cap), ends the run failed/budget_breach, PAUSES the agent, and the next
    ``dream start`` is refused as paused. The same facts through ``agents show dream --json``."""
    inst = _start(tmp_path, success_doc(text=_VALID, cost=30.0))
    row = _ended(inst)
    pic = _picture(inst, "over")
    nxt = inst.cli("dream", "start", spawn_gate=False)
    print("[over] next start:", nxt.rc, repr(nxt.text))
    shown = json.loads(inst.cli("agents", "show", "dream", "--json", spawn_gate=False).out)
    print("[over] show:", shown["agent"]["operation"], [(o["state"], o["reason"]) for o in shown["occurrences"]])

    assert pic["res"] == ("settled", RESERVED, 30_000_000, "breach")
    assert pic["row"] == ("failed", "budget_breach")
    assert pic["agent"][0] == "paused"
    assert pic["slots"] == []                                   # the slot is freed
    u = pic["usage"]
    assert (u["readable"], u["actual_usd_micros"], u["reserved_usd_micros"]) == (True, 30_000_000, RESERVED)
    assert [c["usd_micros"] for c in u["composes"]] == [30_000_000]
    assert pic["stub_calls"] == 1
    assert [c["state"] for c in pic["cards"]["items"]] == ["open"] and pic["usage"]["outcome"] == "saved"
    assert nxt.rc == 1 and nxt.err == ("levain dream start: the dream is paused, so nothing starts it; "
                                       "`levain agents resume dream` lifts a pause once the cause is fixed\n")
    assert len(inst.rows()) == 1 and inst.stub.calls() == 1     # the refused start admitted nothing, ran nothing
    assert shown["agent"]["operation"] == "paused"
    assert [(o["occurrence_id"], o["state"], o["reason"]) for o in shown["occurrences"]] == \
        [(row["occurrence_id"], "failed", "budget_breach")]


# --------------------------------------------------------------------------------------- (2) unreadable usage
def _assert_whole_not_a_breach(pic):
    assert pic["res"] == ("settled", RESERVED, RESERVED, "unreadable")     # the reservation, whole: never zero
    assert pic["agent"][0] == "active"                                      # no breach, so no pause
    assert pic["slots"] == []
    u = pic["usage"]
    assert u["readable"] is False and u["actual_usd_micros"] is None and u["reserved_usd_micros"] == RESERVED
    assert pic["stub_calls"] == 1
    assert pic["row"][1] != "budget_breach"
    assert [c["state"] for c in pic["cards"]["items"]] == ["open"]         # one card for the one fire


def test_unreadable_usage_cause_i_a_json_result_with_no_cost_debits_the_reservation_whole(tmp_path):
    """A well-formed result with no ``total_cost_usd``: the compose is recorded with ``usd_micros: null``, ``usage.json``
    is unreadable, and the settle debits the whole reservation (reason ``unreadable``), with no breach and no pause."""
    inst = _start(tmp_path, success_doc(text=_VALID, cost=None))
    _ended(inst)
    pic = _picture(inst, "nocost")
    _assert_whole_not_a_breach(pic)
    assert [(c["usd_micros"], c["error"]) for c in pic["usage"]["composes"]] == [(None, None)]


def test_unreadable_usage_cause_ii_garbage_stdout_debits_the_reservation_whole(tmp_path):
    """The child exits 0 printing no JSON at all: the compose error is ``child-exit-0: no result json``, the usage is
    unreadable, and the reservation is debited whole, with no breach."""
    inst = _start(tmp_path, "this is not json\n")
    _ended(inst)
    pic = _picture(inst, "garbage")
    _assert_whole_not_a_breach(pic)
    assert [(c["usd_micros"], c["error"]) for c in pic["usage"]["composes"]] == [(None, "child-exit-0: no result json")]


# (iii) a fire SIGKILLed mid-compose
def _kill_group(pgid: int | None) -> None:
    if pgid:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _sigkill_mid_compose(tmp_path):
    inst = _start(tmp_path, success_doc(text=_VALID, cost=0.5), sleep=40)
    _wait(inst, "the stub to be running", lambda: inst.stub.calls() >= 1)
    (row,) = inst.rows()
    assert row["state"] == "launched" and row["pid"]                       # claimed by the fire
    prog = inst.anneal / "dream" / row["occurrence_id"] / "progress.json"
    child = _wait(inst, "the compose child to be journalled", lambda: json.loads(prog.read_text())["child_pid"])
    os.kill(row["pid"], signal.SIGKILL)                                    # the fire dies; no usage.json is written
    _wait(inst, "the fire to be gone", lambda: _gone(row["pid"]))
    assert _usage(inst, row["occurrence_id"]) is None
    return inst, row, child


def _tick(inst: Install, minutes: int):
    now = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return inst.cli("agents", "tick", now=now)


def test_unreadable_usage_cause_iii_a_fire_sigkilled_mid_compose_settles_whole_past_stop_at(tmp_path):
    """The fire is SIGKILLed while the stub (its compose child, a group of its own) sleeps; no ``usage.json`` is ever
    written. The compose child dies too (a machine-wide kill), and a tick past ``stop_at`` ends the row stopped/stop_at,
    frees the slot and debits the reservation whole as ``unreadable``: never zero, no breach."""
    inst, row, child = _sigkill_mid_compose(tmp_path)
    try:
        _kill_group(child)
        _wait(inst, "the compose group to be gone", lambda: not _group_alive(child))
        tick = _tick(inst, 61)
        print("\n[kill] tick:", tick.rc, repr(tick.text))
        pic = _picture(inst, "kill")
    finally:
        _kill_group(child)
    assert pic["row"] == ("stopped", "stop_at")
    assert pic["res"] == ("settled", RESERVED, RESERVED, "unreadable")
    assert pic["agent"][0] == "active" and pic["slots"] == []
    assert pic["usage"] is None                                            # no usage.json: the settle is the recovery's
    assert pic["cards"]["items"] == []                                     # a fire that never finished filed no card
    assert pic["stub_calls"] == 1


def test_unreadable_usage_cause_iii_with_the_compose_child_still_alive_nothing_is_debited_until_it_is_ended(tmp_path):
    """The fire is SIGKILLed but the stub (compose child) keeps running. The tick past ``stop_at`` cannot prove the
    child ended, so it settles NOTHING: the row stays launched holding its slot, the reservation stays open (no zero is
    debited), and an attention line names the group. Once the group is gone the next tick settles it whole, unreadable."""
    inst, row, child = _sigkill_mid_compose(tmp_path)
    occ = row["occurrence_id"]
    try:
        first = _tick(inst, 61)
        print("\n[alive] tick 1:", first.rc, repr(first.text))
        (held,) = inst.rows()
        (open_res,) = inst.rows("SELECT * FROM agent_reservations")
        slots = inst.rows("SELECT * FROM agent_slots")
        print("[alive] after tick 1:", (held["state"], held["reason"]), (open_res["state"], open_res["debited_usd_micros"]),
              len(slots))
        assert f"[agent-stop-unconfirmed:{occ}]" in first.text and "asked to stop; not confirmed ended" in first.text
        assert f"orphaned compose child: not-confirmed (child pid {child}" in first.text
        assert f"kill -KILL -{child}" in first.text and f"levain dream recover {occ}" in first.text
        assert (held["state"], held["reason"]) == ("launched", None)
        assert (open_res["state"], open_res["debited_usd_micros"]) == ("open", None)
        assert len(slots) == 1 and _usage(inst, occ) is None

        _kill_group(child)                                                 # the operator ends the group, as told
        _wait(inst, "the compose group to be gone", lambda: not _group_alive(child))
        second = _tick(inst, 61)
        print("[alive] tick 2:", second.rc, repr(second.text))
        pic = _picture(inst, "alive")
    finally:
        _kill_group(child)
    assert f"attention resolved [agent-stop-unconfirmed:{occ}]" in second.text
    assert pic["row"] == ("stopped", "stop_at")
    assert pic["res"] == ("settled", RESERVED, RESERVED, "unreadable")
    assert pic["agent"][0] == "active" and pic["slots"] == [] and pic["usage"] is None
    assert pic["cards"]["items"] == []
