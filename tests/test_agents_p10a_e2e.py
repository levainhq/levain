"""P10a (levain#121): DESIGN invariant 7's cases, run end to end through the ``levain`` CLI as a subprocess.

Every case here was RUN by hand on a tmp install first (the discriminator of intention-938: a case is pinned to the
output a real run gave, never to a guess and never to a mock of the thing under test). What each case drives is the
operator's own command line (``levain agents ...``, ``levain dream ...``) against stores on disk, through
``tests/_p10a_rig.py``. The gates are named in ``tests/_p10a_entry.py`` and are only these: the clock a tick or hand start
reads, and the one door to a model-calling child (a launched row stays launched, with no fire).

Session 1 builds four cases: edit + re-sign mid-run, a manual run against the ceiling, an unreadable store, DST.
The rest are listed in ``project_memory/1118-12_lanes/ret_D_121_build.md`` as UNBUILT.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("cryptography")
from tests._p10a_rig import Install, utc  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


# ------------------------------------------------------------------------------------------ edit + re-sign mid-run

def test_p10a_edit_and_resign_mid_run(tmp_path):
    """invariant 7: edit + re-sign, mid-run. A run launched under v1's link keeps that link when the spec is edited
    (``dream enable --revise``); the edit alone arms nothing (a hand start is refused, before and after the old run
    ends); only a link signed for the new spec_hash arms it, and the next run is filed under the new link."""
    inst = Install(tmp_path)
    assert inst.enable("--time", "00:00").rc == 0
    v1 = inst.agent()
    assert v1["version"] == 1
    signed1 = inst.sign(inst.link_doc(run_ceiling=3))
    assert signed1.rc == 0 and signed1.out.startswith("signed: al-") and "at seq 5" in signed1.out
    link1 = signed1.out.split()[1]

    started = inst.cli("dream", "start")
    assert started.rc == 0 and "claimed and launched oc-" in started.out and "its fire runs detached" in started.out
    (run1,) = inst.rows()
    assert (run1["state"], run1["kind"], run1["link_id"], run1["spec_hash"]) == ("launched", "manual", link1, v1["spec_hash"])

    # the edit: a new time is a new spec (v2), registered by the operator's own command; the run in flight is untouched
    edited = inst.cli("dream", "enable", "--time", "00:10", "--revise")
    assert edited.rc == 0 and f"registered: dream {v1['agent_id']} v2 spec_hash " in edited.out
    assert "Not signed. The dream launches only under a live signed agent link for this spec_hash." in edited.out
    v2 = inst.agent()
    assert v2["version"] == 2 and v2["spec_hash"] != v1["spec_hash"] and v2["agent_id"] == v1["agent_id"]
    (still,) = inst.rows()
    assert (still["occurrence_id"], still["state"], still["link_id"], still["spec_hash"]) == (
        run1["occurrence_id"], "launched", link1, v1["spec_hash"])

    # an edit WITHOUT a re-sign is refused, with the old run still live and again once it has ended
    refusal = ("levain dream start: no live signed link covers the dream's current spec (or the authority log could "
               "not be replayed), so nothing was claimed; the dream launches only under a live signed link\n")
    unsigned = inst.cli("dream", "start")
    assert (unsigned.rc, unsigned.out, unsigned.err) == (1, "", refusal)
    assert len(inst.rows()) == 1
    ended = inst.cli("agents", "tick", now=utc(2026, 11, 18, 23, 50, 0))     # past the launch lease: the run is recovered
    assert ended.rc == 6 and "ended stopped/crash_recovered (takeover)" in ended.err
    assert inst.cli("dream", "start").err == refusal and len(inst.rows()) == 1

    # the re-sign: a link for v2's spec_hash arms it; the new run is filed under it, the old row keeps its own
    signed2 = inst.sign(inst.link_doc(run_ceiling=3))
    assert signed2.rc == 0 and "at seq 6" in signed2.out
    link2 = signed2.out.split()[1]
    assert link2 != link1
    again = inst.cli("dream", "start")
    assert again.rc == 0 and "claimed and launched oc-" in again.out
    old, new = inst.rows("SELECT * FROM agent_occurrences ORDER BY launched_at, occurrence_id")
    assert (old["state"], old["reason"], old["link_id"], old["spec_hash"]) == ("stopped", "crash_recovered", link1, v1["spec_hash"])
    assert (new["state"], new["link_id"], new["spec_hash"]) == ("launched", link2, v2["spec_hash"])
    assert inst.spawned() == [old["occurrence_id"], new["occurrence_id"]]


# ------------------------------------------------------------------------------------ a manual run against the ceiling

def test_p10a_manual_run_against_the_ceiling(tmp_path):
    """invariant 7: a manual run against the ceiling. With the link's signed run_ceiling at 1, the first hand start
    launches; a second is refused while it is in flight; once the first is ended (recovered by the tick) the second
    attempt is refused by the CEILING, ends ``skipped-budget``, and takes no slot, no reservation, no fire."""
    inst = Install(tmp_path)
    assert inst.enable("--time", "00:00").rc == 0
    assert inst.sign(inst.link_doc(run_ceiling=1)).rc == 0

    first = inst.cli("dream", "start")
    assert first.rc == 0 and "claimed and launched oc-" in first.out
    (run1,) = inst.rows()
    held = inst.cli("dream", "start")
    assert held.rc == 1 and held.out == "" and held.err == (
        f"levain dream start: {run1['occurrence_id']} (manual, launched) is still in flight, and the dream runs one "
        "occurrence at a time; nothing was claimed\n")
    assert len(inst.rows()) == 1

    recovered = inst.cli("agents", "tick", now=utc(2026, 11, 18, 23, 50, 0))
    assert recovered.rc == 6 and recovered.err.startswith(f"levain agents tick: {run1['occurrence_id']} (hand-") and (
        "was launched but no fire claimed it within its 900s launch lease: it was ended stopped/crash_recovered "
        "(takeover), its slot freed and its reservation settled\n") in recovered.err

    over = inst.cli("dream", "start")
    assert over.rc == 1 and over.out == "" and over.err.startswith("levain dream start: oc-") \
        and over.err.endswith(" ended skipped-budget: ceiling; nothing was started\n"), over.err
    first_row, second = sorted(inst.rows(), key=lambda r: r["state"] != "stopped")
    assert (first_row["state"], first_row["reason"]) == ("stopped", "crash_recovered")
    assert (second["kind"], second["state"], second["reason"], second["launched_at"]) == (
        "manual", "skipped-budget", "ceiling", None)
    # nothing was taken for the refused run: no slot, no reservation; and only the first was ever given a fire
    assert inst.rows(f"SELECT * FROM agent_slots WHERE owner_id = '{second['occurrence_id']}'") == []
    assert inst.rows(f"SELECT * FROM agent_reservations WHERE occurrence_id = '{second['occurrence_id']}'") == []
    assert inst.spawned() == [run1["occurrence_id"]]
    shown = inst.occurrences()
    assert sorted(o["state"] for o in shown) == ["skipped-budget", "stopped"]


# ----------------------------------------------------------------------------------------------- an unreadable store

def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_p10a_unreadable_store(tmp_path):
    """invariant 7: an unreadable store. Through the CLI, over a store that is junk bytes, locked, or permission
    denied: every verb refuses with its stated text and exit 1, and writes nothing (the file's bytes are unchanged and
    no sibling file is created)."""
    inst = Install(tmp_path)
    assert inst.enable("--time", "00:00").rc == 0
    db = inst.store_dir / "autonomic.db"
    agent_id = inst.agent()["agent_id"]
    before_siblings = sorted(p.name for p in inst.store_dir.iterdir())

    # junk bytes where the store was
    db.write_bytes(os.urandom(4096))
    junk = _sha(db)
    cant = "levain agents: cannot open the autonomic store: file is not a database\n"
    for argv in (("agents",), ("agents", "list"), ("agents", "show", "dream"), ("agents", "tick"),
                 ("agents", "pause", "dream"), ("agents", "zone", "America/Chicago"), ("agents", "attach", "dream"),
                 ("agents", "revoke", agent_id, "--abandon"),
                 ("agents", "revoke", agent_id, "--data-root", str(inst.world.root), "--signing-key", str(inst.key))):
        got = inst.cli(*argv, spawn_gate=False)
        assert (got.rc, got.out, got.err) == (1, "", cant), (argv, got)
    for argv, verb in ((("dream", "start"), "start"), (("dream", "enable", "--time", "00:10", "--revise"), "enable")):
        got = inst.cli(*argv, spawn_gate=False)
        assert (got.rc, got.out, got.err) == (1, "", f"levain dream {verb}: file is not a database\n"), (argv, got)
    assert _sha(db) == junk and sorted(p.name for p in inst.store_dir.iterdir()) == before_siblings

    # permission denied: the store is there and the operator may not read it
    db.chmod(0)
    try:
        for argv in (("agents", "list"), ("agents", "tick")):
            got = inst.cli(*argv, spawn_gate=False)
            assert (got.rc, got.out, got.err) == (
                1, "", "levain agents: cannot open the autonomic store: unable to open database file\n"), (argv, got)
    finally:
        db.chmod(0o600)
    assert _sha(db) == junk and sorted(p.name for p in inst.store_dir.iterdir()) == before_siblings


def test_p10a_unreadable_store_locked(tmp_path):
    """invariant 7: an unreadable store, the locked kind: another process holds the store's write lock. The tick says
    so and exits 1, and the held transaction is still the holder's (the store is not changed by the refusal)."""
    inst = Install(tmp_path)
    assert inst.enable("--time", "00:00").rc == 0
    db = inst.store_dir / "autonomic.db"
    holder = sqlite3.connect(db, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        got = inst.cli("agents", "tick", spawn_gate=False)
    finally:
        holder.close()
    assert (got.rc, got.out, got.err) == (1, "", "levain agents: cannot open the autonomic store: database is locked\n")
    assert inst.rows() == []


# --------------------------------------------------------------------------------------------------------------- DST

def _slots(first: datetime, last: datetime) -> list[str]:
    out, t = [], first
    while t <= last:
        out.append(t.strftime("%Y-%m-%dT%H:%M:%SZ"))
        t += timedelta(minutes=20)
    return out


def _night(tmp_path, signed_at: datetime, first_tick: datetime, last_tick: datetime):
    """A dream with NO new episodes (every slot the tick claims ends ``skipped-no-input``: nothing launches, so nothing
    fails and nothing pauses the agent), its link signed into the log at ``signed_at``, then ``levain agents tick`` once
    per 20 minutes of the faked clock across a DST edge."""
    inst = Install(tmp_path, episodes=0)
    assert inst.enable("--time", "00:00").rc == 0
    inst.sign_in_log_at(signed_at, inst.link_doc(run_ceiling=40))
    for at in _slots(first_tick, last_tick):
        got = inst.cli("agents", "tick", now=at)
        assert got.rc == 0 and got.err.count("did not launch: skipped-no-input (no_input)") <= 1, (at, got.text)
    return inst


def test_p10a_dst_fall_back(tmp_path):
    """invariant 7: DST, fall-back (America/New_York, 2026-11-01: 02:00 EDT becomes 01:00 EST). One occurrence per slot
    through the CLI's own tick: the 01:00 wall hour that repeats is two distinct instants (05:00Z and 06:00Z), each
    claimed once, none skipped, none doubled, all in one night's window."""
    inst = _night(tmp_path, datetime(2026, 10, 31, 12, tzinfo=timezone.utc),
                  datetime(2026, 11, 1, 3, 40, tzinfo=timezone.utc), datetime(2026, 11, 1, 9, 40, tzinfo=timezone.utc))
    rows = inst.rows("SELECT * FROM agent_occurrences ORDER BY key")
    # 00:00 EDT .. 04:40 EST: 00:xx 3, 01:xx twice 6, 02:xx 03:xx 04:xx 9 = 18 slots
    assert [r["key"] for r in rows] == _slots(datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc),
                                              datetime(2026, 11, 1, 9, 40, tzinfo=timezone.utc))
    assert len(rows) == 18 and {r["window_key"] for r in rows} == {"w:2026-10-31T22:00"}
    assert {r["kind"] for r in rows} == {"scheduled"} and {r["state"] for r in rows} == {"skipped-no-input"}
    assert "2026-11-01T05:00:00Z" in {r["key"] for r in rows} and "2026-11-01T06:00:00Z" in {r["key"] for r in rows}
    assert inst.spawned() == []


def test_p10a_dst_spring_forward(tmp_path):
    """invariant 7: DST, spring-forward (America/New_York, 2027-03-14: 02:00 EST becomes 03:00 EDT). The wall hour that
    does not exist gets no slot and no occurrence: 00:00-01:40 EST then 03:00-04:40 EDT, each once, one night's window."""
    inst = _night(tmp_path, datetime(2027, 3, 12, 12, tzinfo=timezone.utc),
                  datetime(2027, 3, 14, 4, 40, tzinfo=timezone.utc), datetime(2027, 3, 14, 8, 40, tzinfo=timezone.utc))
    rows = inst.rows("SELECT * FROM agent_occurrences ORDER BY key")
    assert [r["key"] for r in rows] == _slots(datetime(2027, 3, 14, 5, 0, tzinfo=timezone.utc),
                                              datetime(2027, 3, 14, 6, 40, tzinfo=timezone.utc)) + _slots(
        datetime(2027, 3, 14, 7, 0, tzinfo=timezone.utc), datetime(2027, 3, 14, 8, 40, tzinfo=timezone.utc))
    assert len(rows) == 12 and {r["window_key"] for r in rows} == {"w:2027-03-13T22:00"}
    assert {r["state"] for r in rows} == {"skipped-no-input"}
    assert inst.spawned() == []
