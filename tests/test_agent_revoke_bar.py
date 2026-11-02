"""The agent revoke bar and its recovery table R1 to R5 (A0 section 2.2; BUILD_PLAN P5c-2a).

One test per row of the table, the rows being the contract (the four-test cap is waived for this file by the plan).
Every log is real: ssh-keygen keys and links verified through the replay. The bar store is the real agent DDL on a
scratch SQLite file.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import timedelta

import pytest

import levain.authority as auth
from levain.agent_authority import AgentAuthority
from levain.agents import bars
from levain.autonomic.agent_tables import create_agent_objects
from levain.cockpit.consent import utc_stamp

pytest.importorskip("cryptography")
from tests.test_authority_agent_link import AGENT, OTHER_AGENT, STORE, World, create, revoke_agent  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


class Rig:
    def __init__(self, tmp_path) -> None:
        self.world = World(tmp_path)
        self.store = sqlite3.connect(tmp_path / "autonomic.db", isolation_level=None)
        create_agent_objects(self.store)
        self.cockpit = sqlite3.connect(self.world.db, isolation_level=None)
        self.attention: list[tuple[str, str]] = []

    def now(self):
        return self.world.clock.now + timedelta(minutes=5)

    def open(self, agent: str = AGENT) -> bars.Bar:
        return bars.open_bar(self.store, agent, head_seq=self.world.state().seq, now=self.now())

    def prepare(self, bar: bars.Bar) -> auth.UnsignedLink:
        link = auth.prepare(self.cockpit, revoke_agent(bar.agent_id), signer_fp=self.world.fp("laptop"), now=self.now(),
                            cache=auth.ReplayCache())
        assert bars.record_prepared(self.store, bar.bar_id, link)
        return link

    def sign(self, link: auth.UnsignedLink) -> str:
        return self.world.keys["laptop"][1](link.message)

    def recover(self) -> list[bars.Outcome]:
        return bars.recover(self.store, AgentAuthority(self.world.root), self.cockpit, now=self.now(),
                            anchor=auth.anchor_path(self.world.root), store_id=STORE,
                            attention=lambda key, text: self.attention.append((key, text)))

    def bar(self, bar_id: str) -> bars.Bar:
        row = bars.read_bar(self.store, bar_id)
        assert row is not None
        return row


@pytest.fixture
def rig(tmp_path) -> Rig:
    return Rig(tmp_path)


def test_r1_the_log_already_holds_the_pending_link(rig):
    # behaviour: a crash after the append and before the close leaves an open bar whose pending link is in the log;
    # recovery closes it as signed at that seq and appends nothing
    create(rig.world, 1)
    bar = rig.open()
    link = rig.prepare(bar)
    auth.append(rig.cockpit, link, rig.sign(link), now=rig.now(), anchor=auth.anchor_path(rig.world.root),
                cache=auth.ReplayCache())
    out = rig.recover()
    closed = rig.bar(bar.bar_id)
    assert [o.row for o in out] == ["R1"]
    assert (closed.closed_at is not None, closed.revoke_seq, closed.reason) == (True, link.seq, "signed")
    assert rig.world.state().seq == link.seq
    print(*[f"{o.row}: {o.text}" for o in out], sep="\n")


def test_r2_a_fresh_stored_signature_is_appended_without_a_new_gesture(rig, monkeypatch):
    # behaviour: a signature recorded on the bar and not yet appended is appended by recovery while it is under 24 h
    # old and the head has not moved; and an abandon that closes a bar while its append runs (c8) leaves the revoke
    # standing, reports it, and raises nothing
    create(rig.world, 1)
    create(rig.world, 2, agent=OTHER_AGENT)
    bar = rig.open()
    link = rig.prepare(bar)
    assert bars.record_signature(rig.store, bar.bar_id, rig.sign(link), now=rig.now())
    before = rig.world.state().seq
    out = rig.recover()
    closed = rig.bar(bar.bar_id)
    assert [o.row for o in out] == ["R2"]
    assert (closed.revoke_seq, closed.reason) == (link.seq, "signed")
    assert rig.world.state().seq == before + 1
    assert auth.agent_last_revoke(rig.world.state(), AGENT, STORE) == link.seq
    print(*[f"{o.row}: {o.text}" for o in out], sep="\n")

    raced = rig.open(OTHER_AGENT)
    second = rig.prepare(raced)
    assert bars.record_signature(rig.store, raced.bar_id, rig.sign(second), now=rig.now())
    real_append = auth.append

    def abandon_then_append(*args, **kw):
        assert bars.close_bar(rig.store, raced.bar_id, revoke_seq=None, reason="abandoned", now=rig.now())
        return real_append(*args, **kw)

    monkeypatch.setattr(auth, "append", abandon_then_append)
    out = rig.recover()
    assert [o.row for o in out] == ["R2"] and "had been abandoned" in out[0].text
    assert (rig.bar(raced.bar_id).reason, rig.bar(raced.bar_id).revoke_seq) == ("abandoned", None)
    assert auth.agent_last_revoke(rig.world.state(), OTHER_AGENT, STORE) == second.seq
    assert [o.row for o in rig.recover()] == ["R5"]
    print(*[f"{o.row}: {o.text}" for o in out], sep="\n")


def test_r3_no_agent_link_in_the_log_closes_the_bar_at_the_head(rig):
    # behaviour: a revoke_agent would change nothing when no link is present, so the bar closes nothing_present at the
    # verified head and nothing is appended
    bar = rig.open()
    head = rig.world.state().seq
    out = rig.recover()
    closed = rig.bar(bar.bar_id)
    assert [o.row for o in out] == ["R3"]
    assert (closed.revoke_seq, closed.reason) == (head, "nothing_present")
    assert rig.world.state().seq == head
    print(*[f"{o.row}: {o.text}" for o in out], sep="\n")


def test_r4_any_other_open_bar_stays_a_bar_with_one_attention_key(rig):
    # behaviour: a link is present and the stored signature is 25 h old, so the bar stays open and never clears on
    # its own; the attention key is the bar's, the same on every tick; opening a bar raised the live epoch
    create(rig.world, 1)
    rig.store.execute("INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, epoch, window_key,"
                      " spec_hash, binding_id, run_id, admitted_at) VALUES ('occ-1', ?, 'manual', 'k', 'admitted', 0,"
                      " 'w', ?, 'bind-0123456789abcdef', 'run-1', 't')", (AGENT, "b" * 64))
    bar = rig.open()
    assert rig.store.execute("SELECT epoch FROM agent_occurrences").fetchone()[0] == 1
    link = rig.prepare(bar)
    assert bars.record_signature(rig.store, bar.bar_id, rig.sign(link), now=rig.now() - timedelta(hours=25))
    before = rig.world.state().seq
    first, second = rig.recover(), rig.recover()
    assert [o.row for o in first + second] == ["R4", "R4"]
    assert rig.bar(bar.bar_id).closed_at is None and rig.world.state().seq == before
    assert {key for key, _ in rig.attention} == {f"agent-bar:{bar.bar_id}"}
    print(*[f"{o.row}: {o.text}" for o in first], sep="\n")


def test_r5_a_revoke_in_the_log_with_no_bar_gets_a_closed_from_log_bar_once(rig):
    # behaviour: a revoke signed through a path with no bar is recorded as a closed from_log bar keyed on its seq;
    # a second tick writes nothing
    create(rig.world, 1)
    gone = rig.world.link(revoke_agent())
    out = rig.recover()
    assert [o.row for o in out] == ["R5"]
    row = rig.store.execute("SELECT agent_id, revoke_seq, reason, closed_at IS NOT NULL FROM agent_bars").fetchall()
    assert row == [(AGENT, gone.seq, "from_log", 1)]
    assert rig.recover() == []
    print(*[f"{o.row}: {o.text}" for o in out], sep="\n")


# --- L3 r1: one test per fix ----------------------------------------------------------------------------------------

def _recover(rig: Rig, **kw) -> list[bars.Outcome]:
    kw.setdefault("now", rig.now())
    return bars.recover(rig.store, AgentAuthority(rig.world.root), rig.cockpit, anchor=auth.anchor_path(rig.world.root),
                        attention=lambda key, text: rig.attention.append((key, text)), **kw)


def test_the_default_store_id_is_the_stores_own_and_none_refuses(rig):
    # behaviour: with no store_id the links are looked up under the autonomic store's meta id (never the cockpit
    # log's), so a present link keeps the bar open; with no id to be had, recovery refuses and closes nothing
    create(rig.world, 1)
    bar = rig.open()
    assert [o.row for o in _recover(rig)] == ["R0"] and rig.bar(bar.bar_id).closed_at is None
    rig.store.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    rig.store.execute("INSERT INTO meta VALUES ('store_id', ?)", (STORE,))
    assert [o.row for o in _recover(rig)] == ["R4"] and rig.bar(bar.bar_id).closed_at is None
    assert [o.row for o in _recover(rig, store_id="store-" + "ef" * 16)] == ["R0"]


def test_r5_skips_a_log_row_rewritten_after_the_replay(rig, monkeypatch):
    # behaviour: a row changed between the verified replay and R5's read is not the replay's link, so no from_log bar
    # is written from it and the outcome says why
    create(rig.world, 1)
    create(rig.world, 2)
    real = AgentAuthority.replay

    def replay_then_rewrite(self):
        state = real(self)
        rig.cockpit.execute("UPDATE authority_log SET record = ? WHERE seq = ?",
                            (json.dumps(revoke_agent()), state.seq))
        return state

    monkeypatch.setattr(AgentAuthority, "replay", replay_then_rewrite)
    assert [o.row for o in _recover(rig, store_id=STORE)] == ["R0"]
    assert rig.store.execute("SELECT COUNT(*) FROM agent_bars").fetchone()[0] == 0


def test_open_bar_waits_no_longer_than_its_bound_on_a_busy_store(rig, tmp_path, monkeypatch):
    # behaviour: a connection with a 5 s busy timeout still gives up at BAR_BUSY_SECONDS, and gets its timeout back
    store = sqlite3.connect(tmp_path / "autonomic.db", isolation_level=None, timeout=5.0)
    holder = sqlite3.connect(tmp_path / "autonomic.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(bars, "BAR_BUSY_SECONDS", 0.3)
    start = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        bars.open_bar(store, AGENT, head_seq=0, now=rig.now())
    holder.execute("ROLLBACK")
    assert time.monotonic() - start < 1.5
    assert store.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_a_naive_now_is_a_stale_signature_not_a_crash(rig):
    # behaviour: a now with no timezone cannot be compared with the stored stamp; the signature is not used and the
    # bar stays a bar
    create(rig.world, 1)
    bar = rig.open()
    link = rig.prepare(bar)
    assert bars.record_signature(rig.store, bar.bar_id, rig.sign(link), now=rig.now())
    assert [o.row for o in _recover(rig, store_id=STORE, now=rig.now().replace(tzinfo=None))] == ["R4"]
    assert rig.bar(bar.bar_id).closed_at is None


def test_a_bar_whose_rows_raise_stays_open_and_the_table_goes_on(rig, monkeypatch):
    # behaviour: an append that raises something unexpected leaves that bar open with an R0 saying why, and the next
    # bar is still resolved
    create(rig.world, 1)
    first = rig.open()
    link = rig.prepare(first)
    assert bars.record_signature(rig.store, first.bar_id, rig.sign(link), now=rig.now())
    second = rig.open(OTHER_AGENT)

    def gone(*args, **kw):
        raise OSError("ssh-keygen went away")

    monkeypatch.setattr(auth, "append", gone)
    out = _recover(rig, store_id=STORE)
    assert sorted(o.row for o in out) == ["R0", "R3"]
    assert rig.bar(first.bar_id).closed_at is None and rig.bar(second.bar_id).reason == "nothing_present"


def test_r5_matches_the_agent_not_only_the_seq(rig):
    # behaviour: R3 closing another agent's bar at a head that is a revoke_agent does not hide that revoke from R5
    create(rig.world, 1)
    other = rig.open(OTHER_AGENT)
    gone = rig.world.link(revoke_agent())
    assert sorted(o.row for o in _recover(rig, store_id=STORE)) == ["R3", "R5"]
    assert rig.bar(other.bar_id).revoke_seq == gone.seq
    assert rig.store.execute("SELECT revoke_seq FROM agent_bars WHERE agent_id = ? AND reason = 'from_log'",
                             (AGENT,)).fetchall() == [(gone.seq,)]


def test_r4_writes_no_attention_for_a_bar_closed_during_the_tick(rig, monkeypatch):
    # behaviour: a bar abandoned after the tick read it gets no attention row, which would never clear
    create(rig.world, 1)
    bar = rig.open()
    real = bars._r3

    def abandon_then_r3(conn, cockpit, state, b, **kw):
        assert bars.close_bar(conn, b.bar_id, revoke_seq=None, reason="abandoned", now=rig.now())
        return real(conn, cockpit, state, b, **kw)

    monkeypatch.setattr(bars, "_r3", abandon_then_r3)
    assert [o.row for o in _recover(rig, store_id=STORE)] == ["R4"]
    assert rig.attention == [] and rig.bar(bar.bar_id).reason == "abandoned"
