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


# ------------------------------------------------------------------------------------------ a stale link after a revoke

def test_p10a_stale_link_after_a_revoke(tmp_path):
    """invariant 7: a stale link inserted after a revoke. A link prepared and signed while the head was seq 5 is held
    back while ``levain agents revoke`` lands at seq 6 (a real subprocess, real key). Appended through the real append it
    is refused as stale; put into the log file by hand it breaks the chain and the whole log is refused. Either way the
    revoked agent is armed by nothing: a hand start claims nothing, a tick launches nothing."""
    from levain import authority as auth
    from tests._p10a_rig import prepare_link

    inst = Install(tmp_path)
    assert inst.enable("--time", "00:00").rc == 0
    agent_id = inst.agent()["agent_id"]
    signed = inst.sign(inst.link_doc(run_ceiling=3))
    assert signed.rc == 0 and "at seq 5, until" in signed.out
    conn, pending, signature = prepare_link(inst, inst.link_doc(run_ceiling=3))
    assert pending.seq == 6

    revoked = inst.cli("agents", "revoke", agent_id, "--data-root", str(inst.world.root), "--signing-key", str(inst.key),
                       spawn_gate=False)
    assert revoked.rc == 0 and revoked.out == (
        "the agent is barred; the revoke link waits for a signature\nrevoke landed at seq 6; the bar is closed\n")
    refusal = ("levain dream start: no live signed link covers the dream's current spec (or the authority log could "
               "not be replayed), so nothing was claimed; the dream launches only under a live signed link\n")
    assert inst.cli("dream", "start").err == refusal                     # the revoke alone: the signed link no longer arms

    try:
        with pytest.raises(auth.StaleHead, match="the authority log's head moved to seq 6 since this link was prepared"):
            auth.append(conn, pending, signature, now=datetime.now(timezone.utc),
                        anchor=auth.anchor_path(inst.world.root), cache=auth.ReplayCache())
        assert conn.execute("SELECT MAX(seq) FROM authority_log").fetchone()[0] == 6
        # by hand, into the file: the stale link claims seq 7 on the head it was signed against (seq 5's hash)
        conn.execute("INSERT INTO authority_log (seq, prev_hash, record, signer_fp, signature, hash) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (7, pending.prev_hash, pending.record, inst.world.fp("laptop"),
                                                   signature, auth._link_hash(pending.prev_hash, pending.record)))
    finally:
        conn.close()
    broken = "authority log link 7: prev_hash is not the predecessor's hash (the chain is broken)"
    start = inst.cli("dream", "start")
    assert start.rc == 1 and start.out == "" and f"(ReplayError: {broken}); nothing is armed this tick" in start.err \
        and start.err.endswith(refusal)
    tick = inst.cli("agents", "tick", now=utc(2026, 11, 19, 4, 0, 0))
    assert tick.rc == 5 and f"the authority refresh failed (ReplayError): {broken}" in tick.err \
        and "nothing is armed this tick" in tick.err
    assert inst.rows() == [] and inst.spawned() == []                    # no occurrence claimed, no fire asked for
    resign = inst.sign(inst.link_doc(run_ceiling=3))
    assert resign.rc == 1 and resign.err == f"levain agents sign: the authority log is refused: {broken}\n"


# ------------------------------------------------------------------------------ expiry and renewal, each mid-run

T0 = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)          # the link is signed into the log here; it lasts 29 days


def _mid_run(tmp_path):
    """A dream whose link (signed at T0, expiring 2026-11-24T12:00Z) has a hand run launched at 11-23 12:00Z: the run is
    in flight when the link's last day passes."""
    inst = Install(tmp_path)
    assert inst.enable("--time", "00:00").rc == 0
    link = inst.sign_in_log_at(T0, inst.link_doc(run_ceiling=3))
    started = inst.cli("dream", "start", now=utc(2026, 11, 23, 12, 0, 0))
    assert started.rc == 0 and "claimed and launched oc-" in started.out
    (run,) = inst.rows()
    assert (run["state"], run["link_id"], run["launched_at"]) == ("launched", link, "2026-11-23T12:00:00Z")
    return inst, link, run


def test_p10a_expiry_mid_run(tmp_path):
    """invariant 7: expiry, mid-run. The link lapses while a run is in flight. The next tick says so (an attention line
    naming the link and its expiry) and ends the run recovered, and no new run starts: a hand start is refused with the
    stated text. (A live fire's own stop at expiry needs a model-calling child: UNBUILT, see ret_D2_121_build.md.)"""
    inst, link, run = _mid_run(tmp_path)
    inside = inst.cli("agents", "tick", now=utc(2026, 11, 23, 12, 10, 0))                  # before expiry: no notice
    assert inside.rc == 0 and "dream-link-expired" not in inside.err
    tick = inst.cli("agents", "tick", now=utc(2026, 11, 25, 12, 0, 0))                    # after the 11-24T12:00Z expiry
    assert tick.rc == 6 and (
        f"attention [dream-link-expired:{link}]: the dream's link {link} expired at 2026-11-24T12:00:00.000000Z; "
        "no run launches until it is renewed or a new link is signed\n") in tick.err
    assert "was ended stopped/crash_recovered (takeover), its slot freed and its reservation settled" in tick.err
    (ended,) = inst.rows()
    assert (ended["state"], ended["reason"], ended["link_id"]) == ("stopped", "crash_recovered", link)
    refused = inst.cli("dream", "start", now=utc(2026, 11, 25, 12, 1, 0))
    assert refused.rc == 1 and refused.out == "" and refused.err == (
        "levain dream start: no live signed link covers the dream's current spec (or the authority log could not be "
        "replayed), so nothing was claimed; the dream launches only under a live signed link\n")
    assert len(inst.rows()) == 1


def test_p10a_renewal_mid_run(tmp_path):
    """invariant 7: renewal, mid-run. ``levain agents renew`` (a real laptop-key signature) while a run is in flight
    moves only the link's expiry: the in-flight row is untouched, the next tick past the ORIGINAL expiry raises no expiry
    notice, and the next run is filed under the same link id and the same incarnation."""
    inst, link, run = _mid_run(tmp_path)
    renewed = inst.cli("agents", "renew", link, "--data-root", str(inst.world.root), "--signing-key", str(inst.key),
                       "--days", "29", spawn_gate=False)
    assert renewed.rc == 0 and renewed.out.startswith(f"renewed: {link} at seq 6, until 2026-1") and \
        renewed.err == "Signing data on standard input\n", renewed
    assert inst.rows() == [run]                                                     # the in-flight row is byte for byte as it was
    tick = inst.cli("agents", "tick", now=utc(2026, 11, 25, 12, 0, 0))
    assert tick.rc == 6 and "dream-link-expired" not in tick.err and "ended stopped/crash_recovered (takeover)" in tick.err
    again = inst.cli("dream", "start", now=utc(2026, 11, 25, 12, 1, 0))
    assert again.rc == 0 and "claimed and launched oc-" in again.out
    old, new = inst.rows("SELECT * FROM agent_occurrences ORDER BY launched_at")
    assert (old["state"], new["state"]) == ("stopped", "launched")
    assert (new["link_id"], new["link_incarnation"]) == (link, run["link_incarnation"])
    assert (new["spec_hash"], new["binding_id"]) == (run["spec_hash"], run["binding_id"])
    import json, sqlite3
    c = sqlite3.connect(inst.world.db)
    try:
        ops = [json.loads(r[0])["op"] for r in c.execute("SELECT record FROM authority_log WHERE seq >= 5 ORDER BY seq")]
    finally:
        c.close()
    assert ops == ["create_grant", "renew"]


# --------------------------------------------------------------------------- c6/c8 and c7: a revoke racing, two real processes

def _revoke_argv(inst, agent_id):
    return ("agents", "revoke", agent_id, "--data-root", str(inst.world.root), "--signing-key", str(inst.key))


def _log_ops(inst):
    import json
    c = sqlite3.connect(inst.world.db)
    try:
        return [(s, json.loads(r)["op"]) for s, r in c.execute("SELECT seq, record FROM authority_log WHERE seq >= 5 ORDER BY seq")]
    finally:
        c.close()


def _clean(*ran):
    for r in ran:
        assert "Traceback" not in r.err and "IntegrityError" not in r.err and "OperationalError" not in r.err, r.err


def test_p10a_revoke_that_cannot_sign_is_abandoned(tmp_path):
    """invariant 7 (c2/c3/c6 shape, deterministic): ``levain agents revoke`` with a key the log does not enrol opens the
    bar and then cannot prepare its link: the agent stays barred (a hand start ends ``stopped: barred``, a sign is
    refused), and the operator's ``--abandon`` closes the bar with nothing appended, exit 0; a second abandon finds no
    open revoke (exit 1), the links stand as signed and the dream starts again."""
    import subprocess
    inst = Install(tmp_path)
    assert inst.enable("--time", "00:00").rc == 0
    agent_id = inst.agent()["agent_id"]
    assert inst.sign(inst.link_doc(run_ceiling=3)).rc == 0
    other = tmp_path / "unenrolled"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    cant = inst.cli("agents", "revoke", agent_id, "--data-root", str(inst.world.root), "--signing-key", str(other),
                    spawn_gate=False)
    assert cant.rc == 1 and cant.out == "" and cant.err.startswith(
        "levain agents revoke: the revoke cannot be prepared: authority log link 6: signer SHA256:") and cant.err.endswith(
        " is not enrolled; the agent stays barred; run revoke again or --abandon\n")
    (bar,) = inst.rows("SELECT * FROM agent_bars")
    assert (bar["revoke_seq"], bar["reason"], bar["closed_at"]) == (None, None, None)

    barred = inst.cli("dream", "start")
    assert barred.rc == 1 and barred.err.startswith("levain dream start: oc-") and barred.err.endswith(
        " ended stopped: barred; nothing was started\n")
    assert inst.spawned() == []
    signed = inst.sign(inst.link_doc(run_ceiling=3))
    assert (signed.rc, signed.err) == (1, "levain agents sign: a revoke is open for this agent: finish or abandon it\n")

    abandoned = inst.cli("agents", "revoke", agent_id, "--abandon", spawn_gate=False)
    assert abandoned.rc == 0 and abandoned.out == f"abandoned: the bar {bar['bar_id']} is closed; nothing was appended\n"
    assert abandoned.err == (f"attention [agent-abandon:{bar['bar_id']}]: the revoke of {agent_id} was abandoned; "
                             "the agent's links stand as signed\n")
    (closed,) = inst.rows("SELECT * FROM agent_bars")
    assert (closed["reason"], closed["revoke_seq"]) == ("abandoned", None) and closed["closed_at"]
    again = inst.cli("agents", "revoke", agent_id, "--abandon", spawn_gate=False)
    assert (again.rc, again.err) == (1, f"levain agents revoke: no open revoke for {agent_id}; nothing to abandon\n")
    assert _log_ops(inst) == [(5, "create_grant")]                       # nothing was appended
    assert inst.cli("dream", "start").rc == 0                            # the signed link still arms the dream


def test_p10a_revoke_racing_an_abandon(tmp_path):
    """invariant 7 (c6/c8): ``levain agents revoke`` and ``revoke --abandon`` as two real processes, the abandon started
    a few tens of milliseconds after the revoke so it lands inside the revoke's window. Whatever order the two land in,
    the outcome is one of the three the protocol names, both exit 0 where it says so, nothing raises, and the log
    holds at most one revoke: (a) the revoke landed first and the abandon found nothing open (exit 1); (b) the abandon
    closed the bar first, the revoke says so and the agent is NOT revoked (exit 0, nothing appended); (c) the abandon
    saw the bar open and lost the close to the revoke (exit 0, 'had already landed')."""
    import time
    from tests._p10a_rig import finish, popen

    seen: set[str] = set()
    for attempt, offset in enumerate((0.04, 0.08, 0.06, 0.05, 0.07, 0.04, 0.08, 0.06)):
        (tmp_path / f"a{attempt}").mkdir()
        inst = Install(tmp_path / f"a{attempt}")
        assert inst.enable("--time", "00:00").rc == 0
        agent_id = inst.agent()["agent_id"]
        assert inst.sign(inst.link_doc(run_ceiling=3)).rc == 0
        rev = popen(inst, *_revoke_argv(inst, agent_id))
        time.sleep(offset)
        abn = popen(inst, "agents", "revoke", agent_id, "--abandon")
        r, a = finish(rev), finish(abn)
        _clean(r, a)
        (bar,) = inst.rows("SELECT * FROM agent_bars")
        ops = _log_ops(inst)
        assert r.rc == 0, r
        if bar["reason"] == "signed":
            assert ops == [(5, "create_grant"), (6, "revoke_agent")] and bar["revoke_seq"] == 6
            assert r.out.endswith("revoke landed at seq 6; the bar is closed\n")
            if a.rc == 1:
                assert a.err == f"levain agents revoke: no open revoke for {agent_id}; nothing to abandon\n"
                seen.add("revoke first")
            else:
                assert (a.rc, a.out) == (0, "the revoke had already landed at seq 6; nothing to abandon\n"), a
                seen.add("abandon lost the close")
            assert inst.cli("dream", "start").rc == 1                      # the revoke stands: nothing arms
        else:
            assert bar["reason"] == "abandoned" and bar["revoke_seq"] is None and ops == [(5, "create_grant")]
            assert r.out.endswith("the revoke was abandoned; nothing was appended; the agent is NOT revoked\n")
            assert a.rc == 0 and a.out.startswith("abandoned: the bar bar-") and a.out.endswith(
                "is closed; nothing was appended\n")
            seen.add("abandon first")
            assert inst.cli("dream", "start").rc == 0                      # the links stand as signed
        if "abandon first" in seen or "abandon lost the close" in seen:
            break
    assert seen & {"abandon first", "abandon lost the close"}, f"the race window was not hit in 8 tries: {seen}"


def test_p10a_revoke_racing_a_renewal(tmp_path):
    """invariant 7 (c7): ``levain agents renew`` and ``levain agents revoke`` as two real processes on the same log.
    Renew started first, revoke 0.15 s later: the renew lands at seq 6 and the revoke one seq later at 7 (the revoke
    re-prepares on the moved head); started together the revoke wins the bar and the renew is refused as open. Never an
    error from the store, never two revokes, and a revoke always stands at the end."""
    import time
    from tests._p10a_rig import finish, popen

    outcomes = []
    for attempt, delay in enumerate((0.0, 0.15, 0.3)):
        (tmp_path / f"b{attempt}").mkdir()
        inst = Install(tmp_path / f"b{attempt}")
        assert inst.enable("--time", "00:00").rc == 0
        agent_id = inst.agent()["agent_id"]
        link = inst.sign(inst.link_doc(run_ceiling=3)).out.split()[1]
        renew = popen(inst, "agents", "renew", link, "--data-root", str(inst.world.root), "--signing-key", str(inst.key))
        time.sleep(delay)
        rev = popen(inst, *_revoke_argv(inst, agent_id))
        rn, rv = finish(renew), finish(rev)
        _clean(rn, rv)
        assert rv.rc == 0
        ops = _log_ops(inst)
        (bar,) = inst.rows("SELECT * FROM agent_bars")
        assert bar["reason"] == "signed"
        if rn.rc == 0:
            assert ops == [(5, "create_grant"), (6, "renew"), (7, "revoke_agent")] and bar["revoke_seq"] == 7
            assert rn.out.startswith(f"renewed: {link} at seq 6, until ") and rv.out.endswith(
                "revoke landed at seq 7; the bar is closed\n")
            outcomes.append("renew then revoke at 7")
        else:
            assert ops == [(5, "create_grant"), (6, "revoke_agent")] and bar["revoke_seq"] == 6
            assert (rn.rc, rn.err) == (1, "levain agents renew: a revoke is open for this agent: finish or abandon it\n")
            outcomes.append("revoke first, renew refused")
        assert inst.cli("dream", "start").rc == 1                           # the revoke stands whichever landed first
    assert "renew then revoke at 7" in outcomes, outcomes


# -------------------------------------------------------------------- c1-c5: a SIGKILL at each step of a revoke

_REFUSED_START = ("levain dream start: no live signed link covers the dream's current spec (or the authority log could "
                  "not be replayed), so nothing was claimed; the dream launches only under a live signed link\n")


def _killed_revoke(tmp_path, point: str):
    """A dream with a signed link and a hand run in flight, then ``levain agents revoke`` SIGKILLed at ``point`` (the
    entry's P10A_KILL gate: the first call of that step). Returns the install, the agent id, the revoke argv."""
    inst = Install(tmp_path)
    assert inst.enable("--time", "00:00").rc == 0
    agent_id = inst.agent()["agent_id"]
    assert inst.sign(inst.link_doc(run_ceiling=3)).rc == 0
    assert inst.cli("dream", "start").rc == 0
    argv = _revoke_argv(inst, agent_id)
    killed = inst.cli(*argv, kill=point, spawn_gate=False)
    assert killed.rc == -9 and killed.out == "", killed                    # SIGKILL: no text, no exit code of its own
    return inst, agent_id, argv


def _bar(inst):
    return inst.rows("SELECT revoke_seq, reason, closed_at IS NULL AS open, pending_seq, "
                     "pending_signature IS NOT NULL AS signed, attempts FROM agent_bars")


LANDED = "the agent is barred; the revoke link waits for a signature\nrevoke landed at seq 6; the bar is closed\n"


def test_p10a_revoke_killed_c1_before_the_bar(tmp_path):
    """c1: killed before the bar commits. Nothing was written: no bar, no epoch bump on the run in flight, the log at
    seq 5; the rerun lands at seq 6 and the dream is then unarmed."""
    inst, agent_id, argv = _killed_revoke(tmp_path, "c1")
    assert _bar(inst) == [] and inst.rows("SELECT epoch FROM agent_occurrences")[0]["epoch"] == 0
    assert _log_ops(inst) == [(5, "create_grant")]
    rerun = inst.cli(*argv, spawn_gate=False)
    assert (rerun.rc, rerun.out) == (0, LANDED)
    assert _bar(inst) == [{"revoke_seq": 6, "reason": "signed", "open": 0, "pending_seq": 6, "signed": 1, "attempts": 1}]
    assert inst.cli("dream", "start").err.endswith(_REFUSED_START)


@pytest.mark.parametrize("point, pending, signed", [("c2", None, 0), ("c3", 6, 0), ("c4", 6, 1)])
def test_p10a_revoke_killed_c2_to_c4_the_agent_stays_barred_and_the_rerun_lands(tmp_path, point, pending, signed):
    """c2 (bar committed, nothing prepared), c3 (prepared, not signed), c4 (signature recorded, not appended): the bar
    is open and the run in flight is fenced (epoch bumped), nothing is in the log; a tick does not move the bar; the
    operator's rerun lands the revoke at seq 6 on the same bar (a new signing gesture: ``Signing data on standard input``
    again) and the dream is unarmed. (BUILD_PLAN names recovery for c4: ``bars.recover`` has no caller in the installed
    build, see test_p10a_revoke_killed_c4_c5_recovery_by_the_tick.)"""
    inst, agent_id, argv = _killed_revoke(tmp_path, point)
    (open_bar,) = _bar(inst)
    assert open_bar == {"revoke_seq": None, "reason": None, "open": 1, "pending_seq": pending, "signed": signed,
                        "attempts": 0 if point == "c2" else 1}
    assert inst.rows("SELECT epoch FROM agent_occurrences")[0]["epoch"] == 1 and _log_ops(inst) == [(5, "create_grant")]
    inst.cli("agents", "tick", now=utc(2026, 11, 19, 3, 0, 0))
    assert [b["open"] for b in _bar(inst)] == [1] and _log_ops(inst) == [(5, "create_grant")]
    rerun = inst.cli(*argv, spawn_gate=False)
    assert (rerun.rc, rerun.out, rerun.err) == (0, LANDED, "Signing data on standard input\n")
    (done,) = _bar(inst)
    assert (done["revoke_seq"], done["reason"], done["open"]) == (6, "signed", 0)
    assert done["attempts"] == (1 if point == "c2" else 2)
    assert inst.cli("dream", "start").err.endswith(_REFUSED_START)


def test_p10a_revoke_killed_c5_after_the_append(tmp_path):
    """c5: killed after the append, before the bar's close. As the installed build runs it TODAY: the revoke is in the log
    (seq 6) and the dream is unarmed, but the bar stays open; the tick does not close it; the rerun is refused ('this
    revoke_agent changes nothing; the agent stays barred'), and ``--abandon`` closes the bar 'abandoned' while saying
    'nothing was appended' (the revoke stands). The recovery the design names for this point (R1: close it signed) is the
    xfail below."""
    inst, agent_id, argv = _killed_revoke(tmp_path, "c5")
    assert _log_ops(inst) == [(5, "create_grant"), (6, "revoke_agent")]
    (bar,) = _bar(inst)
    assert (bar["open"], bar["revoke_seq"], bar["signed"]) == (1, None, 1)
    assert inst.cli("dream", "start").err.endswith(_REFUSED_START)
    tick = inst.cli("agents", "tick", now=utc(2026, 11, 19, 3, 0, 0))
    assert [b["open"] for b in _bar(inst)] == [1], tick
    rerun = inst.cli(*argv, spawn_gate=False)
    assert (rerun.rc, rerun.out, rerun.err) == (1, "", "levain agents revoke: the revoke cannot be prepared: authority "
        "log link 7: this revoke_agent changes nothing; the agent stays barred; run revoke again or --abandon\n")
    abandoned = inst.cli("agents", "revoke", agent_id, "--abandon", spawn_gate=False)
    assert abandoned.rc == 0 and abandoned.out.endswith("is closed; nothing was appended\n")
    assert _log_ops(inst) == [(5, "create_grant"), (6, "revoke_agent")]    # and the revoke is still the log's


@pytest.mark.xfail(strict=True, reason="levain#121 finding: levain.agents.bars.recover (A0 section 2.2 rows R1, R2) has no "
                   "caller in the installed build, so a revoke killed at c4/c5 is never finished by a tick")
@pytest.mark.parametrize("point", ["c4", "c5"])
def test_p10a_revoke_killed_c4_c5_recovery_by_the_tick(tmp_path, point):
    """The design's recovery for c4 (append the recorded signature, no new gesture) and c5 (close the bar signed): after
    the kill, one tick leaves the bar closed ``signed`` at seq 6 and the revoke in the log."""
    inst, agent_id, argv = _killed_revoke(tmp_path, point)
    inst.cli("agents", "tick", now=utc(2026, 11, 19, 3, 0, 0))
    (bar,) = _bar(inst)
    assert (bar["open"], bar["reason"], bar["revoke_seq"]) == (0, "signed", 6)
