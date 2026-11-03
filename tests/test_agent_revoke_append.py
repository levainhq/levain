"""The agent revoke's steps: begin, sign, finish (A0 section 2.2 steps 1-5; BUILD_PLAN P5c-2b), and the open-bar
check in the one signing entry (``levain.agents.signing``).

Every log is real (ssh-keygen keys, links verified through the replay) and the bar store is the real agent DDL on a
scratch SQLite file. The guarded writes are tested by closing the bar (an abandon) at the instant between a step's
read and its write, one test per guard; the cross-process race the slice was built from was run by hand first (two
processes on a /tmp copy: a renewal that lands during the gesture made the append a StaleHead, the re-prepare and a
second signature landed the revoke one seq later, ``attempts`` 2).
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import timedelta

import pytest

import levain.authority as auth
from levain import agent_authority as aa
from levain.agents import bars
from levain.agents.signing import renew_agent_link, sign_agent_link
from levain.autonomic.agent_tables import create_agent_objects
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import KeyError_

pytest.importorskip("cryptography")
from tests.test_authority_agent_link import AGENT, OTHER_AGENT, STORE, World, create, link  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

REFUSAL = "a revoke is open for this agent: finish or abandon it"
LINK = "al-000000000001"


class Rig:
    def __init__(self, tmp_path, monkeypatch) -> None:
        self.tmp = tmp_path
        self.world = World(tmp_path)
        CockpitDB(self.world.root).set_install_class("reported")    # the signing entry opens a reported journal
        self.home = tmp_path / "home"
        monkeypatch.setenv("LEVAIN_HOME", str(self.home))
        (self.home / "autonomic").mkdir(parents=True)
        self.store_dir = self.home / "autonomic"
        self.store = sqlite3.connect(self.store_dir / "autonomic.db", isolation_level=None)
        create_agent_objects(self.store)
        self.cockpit = sqlite3.connect(self.world.db, isolation_level=None)
        self.fp, self.sign = self.world.keys["laptop"]
        self.anchor = auth.anchor_path(self.world.root)

    def clock(self):
        return self.world.clock.now

    def begin(self, agent: str = AGENT) -> aa.RevokeStep:
        return aa.revoke_agent_begin(self.store, self.cockpit, agent, store_id=STORE, signer_fp=self.fp,
                                     clock=self.clock)

    def signed(self, step: aa.RevokeStep) -> aa.RevokeStep:
        return aa.revoke_agent_sign(self.store, step.bar_id, self.sign, clock=self.clock)

    def finish(self, step: aa.RevokeStep) -> aa.RevokeStep:
        return aa.revoke_agent_finish(self.store, self.cockpit, step.bar_id, clock=self.clock, anchor=self.anchor)

    def bar(self, step: aa.RevokeStep) -> bars.Bar:
        row = bars.read_bar(self.store, step.bar_id)
        assert row is not None
        return row

    def abandon(self, bar_id: str) -> None:
        assert bars.close_bar(self.store, bar_id, revoke_seq=None, reason="abandoned", now=self.clock())

    def revokes(self) -> int:
        return auth.agent_last_revoke(self.world.state(), AGENT, STORE)


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Rig:
    r = Rig(tmp_path, monkeypatch)
    create(r.world, 1)
    return r


# --- the steps ----------------------------------------------------------------------------------------------------

def test_the_signature_is_on_the_bar_before_the_append_and_the_bar_closes_signed(rig, monkeypatch):
    # behaviour: step 3 records the signature and its time in one UPDATE, and step 4 appends only after that; the bar
    # closes signed at the link's seq; the epoch of a live occurrence rose when the bar did
    seen = {}
    real = auth.append

    def spy(conn, link_, signature, **kw):
        row = bars.open_bar_of(rig.store, AGENT)
        seen["on_bar"] = (row.pending_signature, row.pending_signed_at is not None)
        return real(conn, link_, signature, **kw)

    monkeypatch.setattr(auth, "append", spy)
    begun = rig.begin()
    assert (begun.state, rig.bar(begun).attempts, rig.bar(begun).pending_signature) == ("prepared", 1, None)
    signed = rig.signed(begun)
    assert signed.state == "signed" and rig.bar(begun).pending_signed_at is not None
    done = rig.finish(signed)
    assert (done.state, done.stands) == ("landed", True)
    assert seen["on_bar"] == (rig.bar(begun).pending_signature, True) and seen["on_bar"][0] is not None
    closed = rig.bar(begun)
    assert (closed.reason, closed.revoke_seq, closed.closed_at is not None) == ("signed", done.seq, True)
    assert rig.revokes() == done.seq
    print(done.text)


def test_a_second_revoke_reuses_the_open_bar_and_a_barred_occurrence_epoch_rises(rig):
    # behaviour: begin twice leaves one open bar (attempts 2); the epoch of a live occurrence rises once per bar write
    rig.store.execute(
        "INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, window_key, spec_hash, binding_id,"
        " run_id, admitted_at) VALUES ('occ-1', ?, 'manual', 'k', 'admitted', 'w', 'h', 'b', 'run-1', '2026-10-25T12:00:00Z')",
        (AGENT,))
    first = rig.begin()
    second = rig.begin()
    assert second.bar_id == first.bar_id
    assert rig.store.execute("SELECT COUNT(*) FROM agent_bars WHERE closed_at IS NULL").fetchone()[0] == 1
    assert rig.bar(second).attempts == 2
    assert rig.store.execute("SELECT epoch FROM agent_occurrences").fetchone()[0] == 2


def test_stale_head_leaves_the_bar_open_and_reprepares_with_the_signature_dropped(rig):
    # behaviour: a link that lands between the signature and the append leaves the bar open, records stale_head, and in
    # ONE update prepares on the new head with the stale signature and its time nulled together; a second gesture
    # lands the revoke at the new seq
    signed = rig.signed(rig.begin())
    old_seq = rig.bar(signed).pending_seq
    rig.world.clock.now += timedelta(days=1)
    renew_agent_link(rig.world.root, LINK, life=timedelta(days=30), signer_fp=rig.fp, sign=rig.sign, clock=rig.clock,
                     store_dir=rig.tmp / "none")        # the cross-store edge: a renew that passed before the bar
    stale = rig.finish(signed)
    row = rig.bar(signed)
    assert (stale.state, stale.stands) == ("stale_head", False)
    assert row.closed_at is None and row.last_error == "stale_head" and row.attempts == 2
    assert (row.pending_signature, row.pending_signed_at) == (None, None)
    assert row.pending_seq == old_seq + 1 and stale.seq == row.pending_seq
    assert rig.revokes() == 0
    done = rig.finish(rig.signed(stale))
    assert (done.state, done.seq) == ("landed", old_seq + 1)
    assert rig.revokes() == old_seq + 1
    print(stale.text)


def test_a_link_the_log_refuses_raises_and_is_recorded_on_the_open_bar(rig):
    # behaviour: the one raise: the log refuses the signed link (here a signature that is not the signer's), and the
    # bar stays open with the reason
    signed = rig.signed(rig.begin())
    rig.store.execute("UPDATE agent_bars SET pending_signature = ? WHERE bar_id = ?",
                      (rig.world.keys["phone"][1](b"not the message"), signed.bar_id))
    with pytest.raises(aa.AgentAuthorityError, match="refused the revoke"):
        rig.finish(signed)
    row = rig.bar(signed)
    assert row.closed_at is None and row.last_error.startswith("append_refused:")


def test_begin_with_no_link_to_remove_bars_the_agent_and_raises(rig):
    # behaviour: the agent is barred first; the log refuses to prepare a revoke that changes nothing, and the bar stays
    # open for recovery (R3) to settle
    with pytest.raises(aa.AgentAuthorityError, match="cannot be prepared"):
        rig.begin(OTHER_AGENT)
    assert bars.open_bar_of(rig.store, OTHER_AGENT) is not None


# --- every bar write is guarded: close the bar mid-step --------------------------------------------------------------

def test_guard_step_2_an_abandon_before_the_prepare_write(rig, monkeypatch):
    # behaviour: the bar closes between the open and the prepare's write: "abandoned", nothing appended, no raise
    real = auth.prepare

    def abandon_then_prepare(*args, **kw):
        rig.abandon(bars.open_bar_of(rig.store, AGENT).bar_id)
        return real(*args, **kw)

    monkeypatch.setattr(auth, "prepare", abandon_then_prepare)
    step = rig.begin()
    assert (step.state, step.text) == ("abandoned", "the revoke was abandoned; nothing was appended")
    assert rig.revokes() == 0


def test_guard_step_3_an_abandon_during_the_gesture(rig):
    # behaviour: the bar closes while he signs: the signature is not recorded, nothing is appended
    begun = rig.begin()

    def abandon_while_signing(message: bytes) -> str:
        rig.abandon(begun.bar_id)
        return rig.sign(message)

    step = aa.revoke_agent_sign(rig.store, begun.bar_id, abandon_while_signing, clock=rig.clock)
    assert step.state == "abandoned" and rig.bar(begun).pending_signature is None
    assert rig.finish(step).state == "abandoned" and rig.revokes() == 0


def test_guard_step_4_c8_an_abandon_during_the_append_leaves_the_revoke_standing(rig, monkeypatch):
    # behaviour: the bar closes (abandoned) while the append runs: the revoke lands, the step says so and exits clean
    # (never the frozen-row IntegrityError), the bar stays abandoned, and recovery writes the from_log bar (R5)
    signed = rig.signed(rig.begin())
    real = auth.append

    def abandon_then_append(*args, **kw):
        rig.abandon(signed.bar_id)
        return real(*args, **kw)

    monkeypatch.setattr(auth, "append", abandon_then_append)
    done = rig.finish(signed)
    assert (done.state, done.stands) == ("landed_bar_abandoned", True)
    assert done.text == (f"revoke landed at seq {done.seq}; the bar had been abandoned; it is recorded from_log at "
                         "the next tick (R5)")
    assert (rig.bar(signed).reason, rig.bar(signed).revoke_seq) == ("abandoned", None)
    assert rig.revokes() == done.seq
    out = bars.recover(rig.store, aa.AgentAuthority(rig.world.root), rig.cockpit, now=rig.clock(), anchor=rig.anchor,
                       store_id=STORE)
    assert [o.row for o in out] == ["R5"]


def test_guard_step_5_c6_an_abandon_during_the_stale_head_write(rig, monkeypatch):
    # behaviour: the append meets a moved head and the bar was abandoned meanwhile: the stale re-prepare touches no
    # row, "abandoned; nothing was appended", and the log is as it was
    signed = rig.signed(rig.begin())

    def move_head_and_abandon(conn, link_, signature, **kw):
        rig.abandon(signed.bar_id)
        raise auth.StaleHead("the head moved")

    monkeypatch.setattr(auth, "append", move_head_and_abandon)
    step = rig.finish(signed)
    assert (step.state, step.text) == ("abandoned", "the revoke was abandoned; nothing was appended")
    assert rig.revokes() == 0


def test_guard_step_5_the_reprepare_is_guarded_too(rig, monkeypatch):
    # behaviour: the head really moved; the bar is abandoned between the StaleHead and the re-prepare's write
    signed = rig.signed(rig.begin())
    rig.world.clock.now += timedelta(days=1)
    renew_agent_link(rig.world.root, LINK, life=timedelta(days=30), signer_fp=rig.fp, sign=rig.sign, clock=rig.clock,
                     store_dir=rig.tmp / "none")
    real = auth.prepare

    def abandon_then_prepare(*args, **kw):
        rig.abandon(signed.bar_id)
        return real(*args, **kw)

    monkeypatch.setattr(auth, "prepare", abandon_then_prepare)
    step = rig.finish(signed)
    assert step.state == "abandoned" and rig.bar(signed).reason == "abandoned"


def test_a_closed_bar_is_reported_by_every_step_not_raised(rig):
    # behaviour: sign and finish on a bar that is already closed say "abandoned" and touch nothing
    begun = rig.begin()
    rig.abandon(begun.bar_id)
    assert rig.signed(begun).state == "abandoned"
    assert rig.finish(begun).state == "abandoned"
    assert rig.revokes() == 0


# --- the open-bar check in the one signing entry ----------------------------------------------------------------------

def test_renew_and_sign_are_refused_on_an_open_bar_and_allowed_once_it_closes(rig):
    # behaviour: neither agent verb signs a link while its agent's revoke is open, with the one text; the bar of
    # another agent does not block; a closed bar does not either, and a new link is born above the revoke
    begun = rig.begin()
    before = rig.world.state().seq
    with pytest.raises(KeyError_) as renew:
        renew_agent_link(rig.world.root, LINK, life=timedelta(days=30), signer_fp=rig.fp, sign=rig.sign, clock=rig.clock)
    assert str(renew.value) == REFUSAL
    with pytest.raises(KeyError_) as sign:
        sign_agent_link(rig.world.root, link(rig.world, 2), signer_fp=rig.fp, sign=rig.sign, clock=rig.clock)
    assert str(sign.value) == REFUSAL
    assert rig.world.state().seq == before
    # another agent's link is not blocked by this bar
    sign_agent_link(rig.world.root, link(rig.world, 3, agent=OTHER_AGENT), signer_fp=rig.fp, sign=rig.sign,
                    clock=rig.clock)
    stale = rig.finish(rig.signed(begun))       # the other agent's link moved the head: one more gesture
    assert stale.state == "stale_head"
    done = rig.finish(rig.signed(stale))
    assert done.stands
    rig.world.clock.now += timedelta(minutes=5)
    after = sign_agent_link(rig.world.root, link(rig.world, 4), signer_fp=rig.fp, sign=rig.sign, clock=rig.clock)
    assert after.seq > done.seq
    assert rig.bar(begun).closed_at is not None


def test_the_cli_verbs_refuse_on_an_open_bar(rig, capsys):
    # behaviour: `levain agents renew` and `levain agents sign` reach the same entry and fail with the same text; the
    # default store is the one LEVAIN_HOME names
    from levain.agents import cli

    rig.begin()
    key = rig.tmp / "keys" / "laptop"
    rc = cli.run_renew(argparse.Namespace(data_root=rig.world.root, signing_key=key, link_id=LINK, days=29))
    assert rc != 0 and REFUSAL in capsys.readouterr().err
    record = rig.tmp / "new_link.json"
    import json
    record.write_text(json.dumps(link(rig.world, 2)))
    rc = cli.run_sign(argparse.Namespace(data_root=rig.world.root, signing_key=key, file=record, days=29))
    assert rc != 0 and REFUSAL in capsys.readouterr().err


def test_a_bar_that_cannot_be_read_refuses_and_a_missing_store_does_not(rig, tmp_path):
    # behaviour: no store (or one with no agent tables) has no bar; a file that is no database refuses, nothing signed
    assert aa.open_bar_for(AGENT, tmp_path / "nowhere") is None
    bare = tmp_path / "bare"
    bare.mkdir()
    sqlite3.connect(bare / "autonomic.db").close()
    assert aa.open_bar_for(AGENT, bare) is None
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "autonomic.db").write_bytes(b"this is not a database" * 100)
    with pytest.raises(KeyError_, match="cannot be read"):
        renew_agent_link(rig.world.root, LINK, life=timedelta(days=30), signer_fp=rig.fp, sign=rig.sign,
                         clock=rig.clock, store_dir=junk)
    assert auth.agent_last_revoke(rig.world.state(), AGENT, STORE) == 0


# --- review fixes r1 (L3_p5c2b_r1): each test is the reproduced failure -------------------------------------------

def test_a_signature_made_over_a_link_that_was_prepared_again_is_not_recorded(rig):
    # behaviour: a second begin re-prepares the bar while he signs; the old link's signature is never paired with the
    # new record: the step says "reprepared", the bar holds no signature, and one more gesture lands the revoke
    begun = rig.begin()

    def reprepare_while_signing(message: bytes) -> str:
        rig.world.clock.now += timedelta(seconds=1)     # the re-prepared record carries a later `at`
        rig.begin()
        return rig.sign(message)

    step = aa.revoke_agent_sign(rig.store, begun.bar_id, reprepare_while_signing, clock=rig.clock)
    row = rig.bar(begun)
    assert (step.state, step.stands) == ("reprepared", False)
    assert (row.pending_signature, row.pending_signed_at, row.attempts) == (None, None, 2)
    done = rig.finish(rig.signed(step))
    assert done.state == "landed" and rig.revokes() == done.seq


def test_finish_after_its_own_link_landed_closes_the_bar_landed(rig):
    # behaviour: the append committed and the process died before the close (c5); a later link moved the head. The
    # retry meets StaleHead, finds the bar's exact link in the verified log, and closes the bar signed at its seq
    signed = rig.signed(rig.begin())
    row = rig.bar(signed)
    link_ = auth.UnsignedLink(row.pending_seq, row.pending_prev, row.pending_record, row.pending_signer_fp)
    auth.append(rig.cockpit, link_, row.pending_signature, now=rig.clock(), anchor=rig.anchor)
    sign_agent_link(rig.world.root, link(rig.world, 3, agent=OTHER_AGENT), signer_fp=rig.fp, sign=rig.sign,
                    clock=rig.clock)
    assert rig.world.state().seq == row.pending_seq + 1
    done = rig.finish(signed)
    closed = rig.bar(signed)
    assert (done.state, done.seq) == ("landed", row.pending_seq)
    assert (closed.reason, closed.revoke_seq, closed.attempts) == ("signed", row.pending_seq, 1)


def test_the_stale_head_reprepare_keeps_the_store_the_bar_was_begun_for(rig):
    # behaviour: finish takes no store id; the re-prepare after a moved head names the store of the bar's own record
    signed = rig.signed(rig.begin())
    rig.world.clock.now += timedelta(days=1)
    renew_agent_link(rig.world.root, LINK, life=timedelta(days=30), signer_fp=rig.fp, sign=rig.sign, clock=rig.clock,
                     store_dir=rig.tmp / "none")
    with pytest.raises(TypeError):
        aa.revoke_agent_finish(rig.store, rig.cockpit, signed.bar_id, store_id="store-" + "cd" * 16,
                               clock=rig.clock, anchor=rig.anchor)
    stale = rig.finish(signed)
    assert stale.state == "stale_head"
    import json
    assert json.loads(rig.bar(signed).pending_record)["agent_store_id"] == STORE


def test_begin_refuses_a_store_id_that_is_not_the_autonomic_stores_own(rig):
    # behaviour: the store's own meta row names its id; a begin for another id refuses before any bar is written
    rig.store.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    rig.store.execute("INSERT INTO meta (key, value) VALUES ('store_id', ?)", ("store-" + "cd" * 16,))
    with pytest.raises(aa.AgentAuthorityError, match="not this store's"):
        rig.begin()
    assert bars.open_bar_of(rig.store, AGENT) is None


def test_a_verifier_that_cannot_run_is_raised_as_itself_never_as_a_refusal(rig, monkeypatch):
    # behaviour: ssh-keygen failing to run is "retry", not "the log refused": finish re-raises VerifierUnavailable,
    # records last_error verifier_unavailable and leaves the bar open; begin's prepare re-raises it too
    signed = rig.signed(rig.begin())
    real_append, real_prepare = auth.append, auth.prepare

    def no_verifier(*args, **kw):
        raise auth.VerifierUnavailable("ssh-keygen could not run")

    monkeypatch.setattr(auth, "append", no_verifier)
    with pytest.raises(auth.VerifierUnavailable):
        rig.finish(signed)
    row = rig.bar(signed)
    assert row.closed_at is None and row.last_error == "verifier_unavailable"
    assert row.pending_signature is not None
    monkeypatch.setattr(auth, "append", real_append)
    monkeypatch.setattr(auth, "prepare", no_verifier)
    with pytest.raises(auth.VerifierUnavailable):
        rig.begin()
    monkeypatch.setattr(auth, "prepare", real_prepare)


def test_a_bar_recovery_closed_signed_during_the_append_is_reported_landed(rig, monkeypatch):
    # behaviour: R1 closes this bar signed at the link's seq between the append and finish's own close; the guarded
    # close touches no row, and the step reads the frozen row and says landed, never landed_bar_abandoned
    signed = rig.signed(rig.begin())
    real = auth.append

    def append_then_recover(*args, **kw):
        out = real(*args, **kw)
        ran = bars.recover(rig.store, aa.AgentAuthority(rig.world.root), rig.cockpit, now=rig.clock(),
                           anchor=rig.anchor, store_id=STORE)
        assert [o.row for o in ran] == ["R1"]
        return out

    monkeypatch.setattr(auth, "append", append_then_recover)
    done = rig.finish(signed)
    assert (done.state, done.stands) == ("landed", True)
    assert (rig.bar(signed).reason, rig.bar(signed).revoke_seq) == ("signed", done.seq)


def test_an_anchor_left_behind_is_said_in_the_landed_step(rig, monkeypatch):
    # behaviour: the revoke committed and the anchor could not advance: the step is landed and its text says so
    signed = rig.signed(rig.begin())

    def anchor_fails(*args, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(auth, "advance_anchor", anchor_fails)
    done = rig.finish(signed)
    assert done.state == "landed" and "the anchor is behind" in done.text
