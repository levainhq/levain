"""The revoke's crash matrix, c1 to c8 (A0 section 2.2; BUILD_PLAN P5c-2c), end to end through ``levain agents revoke``.

A kill is a ``BaseException`` raised at the named point (nothing in the verb catches it, as nothing catches SIGKILL):
the verb stops with its last write on disk, and the next step is what an operator or the tick does. Recovery is the
real :func:`levain.agents.bars.recover` over the real replay. c9 is P6-5's, not here. c7 runs the racing renewal in a
second OS process on the same scratch log.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta

import pytest

import levain.authority as auth
from levain import agent_authority as aa
from levain.agents import bars
from levain.agent_authority import AgentAuthority, open_bar_for

from tests.test_agent_revoke_cli import AGENT, CliRig, create, rig  # noqa: F401  (rig is the fixture)

LINK = "al-000000000001"


class Kill(BaseException):
    """The process died here."""


def _kill(*a, **k):
    raise Kill


def _recover(rig: CliRig) -> list[bars.Outcome]:
    cockpit = _cockpit(rig)
    seen: list = []
    rig.attention = seen
    out = bars.recover(rig.conn, AgentAuthority(rig.world.root), cockpit, now=rig.world.clock.now,
                       anchor=auth.anchor_path(rig.world.root), store_id=rig.store_id,
                       attention=lambda key, text: seen.append((key, text)))
    cockpit.close()
    return out


def _cockpit(rig: CliRig):
    import sqlite3
    return sqlite3.connect(rig.world.db, isolation_level=None)


def _live_occurrence(rig: CliRig) -> None:
    rig.conn.execute(
        "INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, window_key, spec_hash, binding_id,"
        " run_id, admitted_at) VALUES ('occ-1', ?, 'manual', 'k', 'admitted', 'w', 'h', 'b', 'run-1', '2026-10-25T12:00:00Z')",
        (AGENT,))


def _epoch(rig: CliRig) -> int:
    return rig.conn.execute("SELECT epoch FROM agent_occurrences").fetchone()[0]


def _renew(rig: CliRig, link_id: str = LINK) -> None:
    """A renewal that passed its open-bar check before the bar committed (A0 section 6.10's edge), in this process."""
    from levain.agents.signing import renew_agent_link

    rig.world.clock.now += timedelta(days=1)         # the verb's clock is the world's, and the log's times never go back
    renew_agent_link(rig.world.root, link_id, life=timedelta(days=30), signer_fp=rig.fp, sign=rig.sign,
                     clock=lambda: rig.world.clock.now, store_dir=rig.tmp / "no-bar-store")


def test_c1_a_kill_before_the_bar_commits_leaves_nothing_and_the_rerun_lands(rig, monkeypatch, capsys):
    _live_occurrence(rig)
    with monkeypatch.context() as m:
        m.setattr(bars, "open_bar", _kill)
        with pytest.raises(Kill):
            rig.revoke()
    assert rig.bars() == [] and _epoch(rig) == 0 and rig.revokes() == 0 and open_bar_for(AGENT, rig.store_dir) is None
    assert _recover(rig) == []
    assert rig.revoke() == 0
    (bar,) = rig.bars()
    assert (bar.reason, bar.revoke_seq) == ("signed", rig.revokes()) and rig.revokes() > 0
    print(capsys.readouterr().out)


def test_c2_a_kill_with_the_bar_committed_and_nothing_prepared_keeps_the_agent_barred(rig, monkeypatch, capsys):
    _live_occurrence(rig)
    with monkeypatch.context() as m:
        m.setattr(aa, "_prepare_onto", _kill)
        with pytest.raises(Kill):
            rig.revoke()
    (bar,) = rig.bars()
    assert bar.closed_at is None and bar.pending_record is None and _epoch(rig) == 1
    assert open_bar_for(AGENT, rig.store_dir).bar_id == bar.bar_id          # the agent is barred without a signature
    out = _recover(rig)
    assert [o.row for o in out] == ["R4"] and rig.bars()[0].closed_at is None and rig.revokes() == 0
    assert rig.revoke() == 0                                                # the rerun reuses the bar
    (after,) = rig.bars()
    assert after.bar_id == bar.bar_id and (after.reason, after.revoke_seq) == ("signed", rig.revokes())
    print(capsys.readouterr().out)


def test_c3_a_kill_after_prepare_before_the_signature_is_an_attention_bar_and_the_rerun_lands(rig, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(aa, "revoke_agent_sign", _kill)
        with pytest.raises(Kill):
            rig.revoke()
    (bar,) = rig.bars()
    assert bar.closed_at is None and bar.pending_record and bar.pending_signature is None and rig.gestures == 0
    out = _recover(rig)
    assert [o.row for o in out] == ["R4"] and rig.attention == [(f"agent-bar:{bar.bar_id}", out[0].text)]
    assert rig.revokes() == 0
    assert rig.revoke() == 0
    (after,) = rig.bars()
    assert after.bar_id == bar.bar_id and after.attempts == 2 and after.reason == "signed" and rig.gestures == 1


def test_c4_a_kill_after_the_signature_is_recorded_is_appended_by_recovery_with_no_new_gesture(rig, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(aa, "revoke_agent_finish", _kill)
        with pytest.raises(Kill):
            rig.revoke()
    (bar,) = rig.bars()
    assert bar.closed_at is None and bar.pending_signature and bar.pending_signed_at and rig.revokes() == 0
    out = _recover(rig)
    assert [o.row for o in out] == ["R2"]
    (closed,) = rig.bars()
    assert (closed.reason, closed.revoke_seq) == ("signed", bar.pending_seq) and rig.revokes() == bar.pending_seq
    assert rig.gestures == 1


def test_c5_a_kill_after_the_append_before_the_close_is_closed_signed_by_recovery(rig, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(bars, "close_bar", _kill)
        with pytest.raises(Kill):
            rig.revoke()
    (bar,) = rig.bars()
    assert bar.closed_at is None and rig.revokes() == bar.pending_seq        # the revoke is in the log, the bar is open
    out = _recover(rig)
    assert [o.row for o in out] == ["R1"]
    (closed,) = rig.bars()
    assert (closed.reason, closed.revoke_seq) == ("signed", bar.pending_seq) and rig.gestures == 1


def test_c6_a_head_that_moved_reprepares_for_one_more_gesture_and_an_abandon_meanwhile_appends_nothing(
        rig, monkeypatch, capsys):
    # (a) a link lands during the gesture: StaleHead, the bar stays open, the stale signature goes, one more gesture
    real_sign, hits, target = rig.sign, [], [LINK]

    def sign_then_move_the_head(message):
        signature = real_sign(message)
        if not hits:
            hits.append(1)
            _renew(rig, target[0])
        return signature

    rig.sign = sign_then_move_the_head
    monkeypatch.setattr(keys_seam(), "ssh_signer", lambda path, **kw: (rig.fp, rig._counted))
    assert rig.revoke() == 0
    (bar,) = rig.bars()
    assert rig.gestures == 2 and bar.attempts == 2 and bar.last_error == "stale_head"
    assert (bar.reason, bar.revoke_seq) == ("signed", rig.revokes()) and bar.revoke_seq > 2
    assert "needs one more signature" in capsys.readouterr().out

    # (b) the same move, with an abandon closing the bar before the re-prepare's write: exit 0, the stated text,
    # nothing appended
    rig2_agent = "ag-" + "c3" * 16
    create(rig.world, 2, agent=rig2_agent, store=rig.store_id)
    hits.clear()
    target[0] = "al-000000000002"
    real_prepare = aa._prepare_onto

    def abandon_first(autonomic, cockpit, bar_id, *a, **kw):
        if kw.get("last_error") == "stale_head":
            assert bars.close_bar(autonomic, bar_id, revoke_seq=None, reason="abandoned", now=rig.world.clock.now)
        return real_prepare(autonomic, cockpit, bar_id, *a, **kw)

    monkeypatch.setattr(aa, "_prepare_onto", abandon_first)
    seq_before = rig.world.state().seq
    assert rig.revoke(rig2_agent) == 0
    out = capsys.readouterr().out
    assert "the revoke was abandoned; nothing was appended" in out
    assert [b.reason for b in rig.bars()][-1] == "abandoned" and rig.world.state().seq == seq_before + 1    # the renew only


def keys_seam():
    import levain.cockpit.keys as keys
    return keys


def test_c7_a_revoke_racing_a_renewal_from_a_second_process_lands_one_seq_later(rig, monkeypatch, capsys):
    # the renewal is a real second process on the same scratch log; it passed its open-bar check before the bar did
    script = (
        "import sys, json\n"
        "from datetime import datetime, timedelta\n"
        "from pathlib import Path\n"
        "from levain.agents.signing import renew_agent_link\n"
        "from levain.cockpit.keys import ssh_signer\n"
        "root, key, now = Path(sys.argv[1]), Path(sys.argv[2]), datetime.fromisoformat(sys.argv[3])\n"
        "fp, sign = ssh_signer(key)\n"
        "snap, exp = renew_agent_link(root, sys.argv[4], life=timedelta(days=30), signer_fp=fp, sign=sign,\n"
        "                             clock=lambda: now, store_dir=Path(sys.argv[5]))\n"
        "print(json.dumps({'seq': snap.seq}))\n")
    real_sign, ran = rig.sign, []

    def sign_while_the_other_process_renews(message):
        signature = real_sign(message)
        if not ran:
            rig.world.clock.now += timedelta(days=1)
            done = subprocess.run(
                [sys.executable, "-c", script, str(rig.world.root), str(rig.tmp / "keys" / "laptop"),
                 rig.world.clock.now.isoformat(), LINK, str(rig.tmp / "no-bar-store")],
                capture_output=True, text=True, timeout=120, env=_env())
            assert done.returncode == 0, done.stderr
            ran.append(json.loads(done.stdout)["seq"])
        return signature

    rig.sign = sign_while_the_other_process_renews
    monkeypatch.setattr(keys_seam(), "ssh_signer", lambda path, **kw: (rig.fp, rig._counted))
    assert rig.revoke() == 0
    (bar,) = rig.bars()
    assert ran and bar.attempts == 2 and bar.last_error == "stale_head" and rig.gestures == 2
    assert (bar.reason, bar.revoke_seq) == ("signed", rig.revokes()) and bar.revoke_seq == ran[0] + 1
    print(capsys.readouterr().out)


def _env() -> dict:
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return env


def test_c8_an_abandon_racing_the_append_leaves_the_revoke_standing_and_exits_zero(rig, monkeypatch, capsys):
    # (a) the abandon runs between the append and the close: the revoke stands, the verb says so and exits 0, and the
    # next tick (R5) writes a from_log bar for that seq; the abandoned bar is not changed
    real_append, rcs = auth.append, []

    def append_then_abandon(*a, **kw):
        new = real_append(*a, **kw)
        rcs.append(rig.abandon())
        return new

    monkeypatch.setattr(auth, "append", append_then_abandon)
    assert rig.revoke() == 0
    out = capsys.readouterr().out
    (bar,) = rig.bars()
    assert rcs == [0] and (bar.reason, bar.revoke_seq) == ("abandoned", None) and rig.revokes() > 0
    assert f"revoke landed at seq {rig.revokes()}; the bar had been abandoned; it is recorded from_log at the next tick (R5)" in out
    outcomes = _recover(rig)
    assert [o.row for o in outcomes] == ["R5"]
    abandoned, from_log = rig.bars()
    assert (abandoned.reason, abandoned.revoke_seq) == ("abandoned", None)
    assert (from_log.reason, from_log.revoke_seq) == ("from_log", rig.revokes())
    monkeypatch.setattr(auth, "append", real_append)

    # (b) step 4 closed first (the abandon saw the bar open, lost the close): it touches no row and says the revoke had
    # already landed, exit 0. With no bar ever seen open, a stale landed bar is not this (exit 1, no open revoke).
    other = "ag-" + "d4" * 16
    create(rig.world, 3, agent=other, store=rig.store_id)
    aa.revoke_agent_begin(rig.conn, _cockpit(rig), other, store_id=rig.store_id, signer_fp=rig.fp,
                          clock=lambda: rig.world.clock.now)
    real_close = bars.close_bar

    def step4_got_there_first(conn, bar_id, *, revoke_seq, reason, now):
        real_close(conn, bar_id, revoke_seq=9, reason="signed", now=now)
        return False

    monkeypatch.setattr(bars, "close_bar", step4_got_there_first)
    capsys.readouterr()
    assert rig.abandon(other) == 0
    assert "the revoke had already landed at seq 9; nothing to abandon" in capsys.readouterr().out
    monkeypatch.setattr(bars, "close_bar", real_close)
    assert rig.abandon(other) == 1
