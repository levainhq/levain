"""The Team zone: one shared read per refresh, the panel set follows the reader's state, and every unhappy state is named."""
from __future__ import annotations

import time

import pytest

from levain.cockpit.engine import ZONES, Cockpit
from levain.cockpit.team_panels import TeamSnapshot, TeamSource, team_discoverer
from levain.team.reader import TeamRead

CRED = {"class": "none", "device_class": None}
MODEL = {"project": "p", "owner": "ana", "mode": "ask", "you": "ben", "canon_status": "no canon yet", "problems": 0,
         "waiting": [{"id": "q1", "type": "question", "kind": None, "owner": "ben", "words": "", "summary": "Discounts first?",
                      "reason": "", "recheck": "", "paths": ["src/a.py"], "recorded_by": "ana", "age": "1d ago",
                      "age_days": 1, "pack": ""}],
         "stopped": [], "held": [], "in_force": [],
         "members": [{"handle": "ana", "role": "owner", "you": False, "keys": 1, "key_state": "in force", "since": "2026-10-01"},
                     {"handle": "ben", "role": "member", "you": True, "keys": 0, "key_state": "waiting for confirm", "since": None}],
         "joins": [{"kind": "key", "handle": "ben", "fingerprint": "SHA256:abc",
                    "line": "ben's key is proposed; it comes into force when ben runs `levain team key confirm` on their own machine"}]}


def _cockpit(reads: list, loads: list[int]) -> Cockpit:
    def load() -> TeamSnapshot:
        loads.append(1)
        return TeamSnapshot(reads[0], {})
    ck = Cockpit(entity=lambda ctx: {"name": "t"})
    ck.discover(team_discoverer(TeamSource(load=load, ttl_s=0.0)), retire=True)
    return ck


def _panels(ck: Cockpit, want: int | None = None) -> list[str]:
    """The Team zone's panel ids. A refresh serves the last answer while a worker renews it, so a changed state shows
    on a later manifest: ``want`` polls (briefly) for that many panels."""
    for _ in range(100):
        zone = [z for z in ck.manifest(CRED)["regions"]["zones"] if z["id"] == "team"][0]
        if want is None or len(zone["panels"]) == want:
            break
        time.sleep(0.02)
    return zone["panels"]


def test_team_is_a_peer_zone_and_the_panel_set_follows_the_state():
    assert ("team", "Team") in ZONES
    reads = [TeamRead("ok", MODEL)]
    ck = _cockpit(reads, [])
    assert _panels(ck) == ["team-members", "team-waiting", "team-stopped", "team-held", "team-canon", "team-joins", "team-sync"]
    reads[0] = TeamRead("no_team", None, "This install has no team ledger.")
    assert _panels(ck, 1) == ["team-status"]                   # the four panes are RETIRED, not left reading Absent
    reads[0] = TeamRead("ok", MODEL)
    assert len(_panels(ck, 7)) == 7


def test_unreadable_draws_one_card_with_the_why_and_never_the_detail():
    ck = _cockpit([TeamRead("unreadable", None, "The team ledger could not be read.", "OSError: /secret/path")], [])
    assert _panels(ck) == ["team-status"]
    rows = ck.panel("team-status")["rows"]
    assert [r["title"] for r in rows] == ["The team ledger could not be read."]
    assert "/secret/path" not in str(ck.panel("team-status")) and "/secret/path" not in str(ck.manifest(CRED))


def test_judged_partial_draws_every_pane_with_the_why_beside_the_rows():
    ck = _cockpit([TeamRead("judged_partial", MODEL, "Part of the ledger could not be judged.")], [])
    assert len(_panels(ck)) == 7
    waiting = ck.panel("team-waiting")
    assert waiting["note"] == "Part of the ledger could not be judged." and [r["id"] for r in waiting["rows"]] == ["q1"]
    assert "Discounts first?" in waiting["rows"][0]["body"]    # the line a list row draws is its body


def test_one_ledger_read_feeds_the_manifest_and_every_panel():
    loads: list[int] = []
    clock = [0.0]
    src = TeamSource(load=lambda: (loads.append(1), TeamSnapshot(TeamRead("ok", MODEL), {}))[1], ttl_s=15.0,
                     clock=lambda: clock[0])
    ck = Cockpit(entity=lambda ctx: {"name": "t"})
    ck.discover(team_discoverer(src), retire=True)
    ck.manifest(CRED)
    for pid in _panels(ck):
        ck.panel(pid)
    assert len(loads) == 1


def test_a_panel_retired_between_the_registry_snapshot_and_its_read_answers_error_not_keyerror():
    reads = [TeamRead("ok", MODEL)]
    ck = _cockpit(reads, [])
    assert len(_panels(ck)) == 7
    spec = ck._specs["team-waiting"]               # request A's registry snapshot
    reads[0] = TeamRead("no_team", None, "This install has no team ledger.")
    assert _panels(ck, 1) == ["team-status"]       # request B's discovery retires the four panes
    from levain.cockpit.engine import ReadContext
    assert ck._snap_for(spec, ReadContext(ck._clock())).status == "error"


def test_the_card_names_a_git_failure_in_words_and_the_cli_text_keeps_the_raw_output():
    from levain.team.reader import person_text
    raw = ("cannot judge the team ledger on this clone: git rev-list failed: to convert the grafts into replace refs. "
           "hint: Turn this message off by running hint: \"git config set advice.graftFileDeprecated false\" fatal: bad object ab12")
    why = person_text(raw)
    assert why == "cannot judge the team ledger on this clone: git could not read its history."
    assert "hint" not in why and "fatal" not in why
    assert person_text("This clone has not pinned a team ledger: run `levain team join`.") == \
        "This clone has not pinned a team ledger: run `levain team join`."


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_cold_timeout_is_remembered_and_a_dying_worker_does_not_freeze_the_zone():
    import threading
    gate = threading.Event()
    calls = []

    def load() -> TeamSnapshot:
        calls.append(1)
        gate.wait(5)
        return TeamSnapshot(TeamRead("ok", MODEL), {})
    src = TeamSource(load=load, ttl_s=0.0, wait_s=0.2)
    t0 = time.monotonic()
    assert src.get().read.state == "unreadable"    # the first caller waits its wait_s once
    t1 = time.monotonic()
    assert src.get().read.state == "unreadable" and time.monotonic() - t1 < 0.1   # later callers do not wait again
    assert t1 - t0 >= 0.15 and len(calls) == 1
    gate.set()
    for _ in range(100):
        if src.get().read.state == "ok":
            break
        time.sleep(0.02)
    assert src.get().read.state == "ok"

    class Die(BaseException):
        pass

    def die() -> TeamSnapshot:
        raise Die()
    bad = TeamSource(load=die, ttl_s=0.0, wait_s=0.05)
    bad.get()
    time.sleep(0.1)
    assert bad._done is None                       # reset in finally: the next get() starts a new worker


def test_a_source_with_no_install_root_and_no_start_draws_no_team_zone(tmp_path, monkeypatch):
    from levain.cockpit import team_panels
    from levain.cockpit.providers import build_default_cockpit
    from levain.dashboard import AnnealPaths, SubstrateSource
    seen = []
    monkeypatch.setattr(team_panels, "TeamSource", lambda *a, **k: seen.append(a) or TeamSource(load=lambda: TeamSnapshot(
        TeamRead("ok", MODEL), {})))
    src = SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "m.db"))
    ck = build_default_cockpit(src)
    assert seen == []                              # the working directory is never scanned for a ledger
    assert not [z for z in ck.manifest(CRED)["regions"]["zones"] if z["id"] == "team" and z["panels"]]
    build_default_cockpit(src, team_start=tmp_path)
    assert seen == [(tmp_path,)]                   # an explicit start is honoured


def test_members_and_joins_panes_draw_rows_without_an_address_and_retire_with_the_zone():
    ck = _cockpit([TeamRead("ok", MODEL)], [])
    assert {"team-members", "team-joins"} <= set(_panels(ck))
    members, joins = ck.panel("team-members"), ck.panel("team-joins")
    assert [r["title"] for r in members["rows"]] == ["ana", "ben (you)"]
    # seen in WebKit (1015-team/shots): the list draws the body, so the handle must lead it
    assert [r["body"].split(" · ")[0] for r in members["rows"]] == ["ana", "ben (you)"]
    assert "waiting for confirm" in members["rows"][1]["body"] and "since 2026-10-01" in members["rows"][0]["body"]
    assert "levain team key confirm" in joins["rows"][0]["body"] and "SHA256:abc" in joins["rows"][0]["body"]
    assert "@" not in str(members) + str(joins)
    empty = _cockpit([TeamRead("ok", {**MODEL, "members": [], "joins": []})], [])
    assert empty.panel("team-joins")["empty"] == "No joins are waiting."
    assert empty.panel("team-joins")["rows"] == []
    gone = _cockpit([TeamRead("unreadable", None, "The team ledger could not be read.")], [])
    assert _panels(gone) == ["team-status"]


# --- T3 L3 r1 FIX 3: one worker per source, the epoch at return, one wait per epoch -----------------------------------


def _gated(gate, loads: list[int], live: list[int], peak: list[int]):
    """A load that counts itself, counts how many run at once, and blocks on ``gate``."""
    import threading
    mu = threading.Lock()

    def load() -> TeamSnapshot:
        with mu:
            loads.append(1)
            live.append(1)
            peak[0] = max(peak[0], len(live))
        try:
            gate.wait(5)
            return TeamSnapshot(TeamRead("ok", MODEL), {"n": len(loads)})
        finally:
            with mu:
                live.pop()
    return load


def test_a_burst_of_invalidations_runs_one_read_and_exactly_one_follow_up():
    import threading
    from levain.cockpit.team_panels import invalidate
    gate = threading.Event()
    gate.set()
    loads, live, peak = [], [], [0]
    src = TeamSource(load=_gated(gate, loads, live, peak), ttl_s=60.0, wait_s=0.01)
    assert src.get().read.state == "ok" and len(loads) == 1
    gate.clear()
    for _ in range(20):                      # a burst of verbs, each followed by a panel read
        invalidate()
        src.get()
    assert len(loads) == 2 and peak[0] == 1, (len(loads), peak)   # one read running, none beside it
    gate.set()
    for _ in range(200):
        if src.get().sync.get("n") == 3 and src._done is None:
            break
        time.sleep(0.01)
    # the read the burst found running finished stale: exactly one follow-up read the newest epoch
    assert len(loads) == 3 and peak[0] == 1 and src.get().age_s == 0.0, (len(loads), peak)


def test_a_waiter_is_not_handed_an_answer_whose_epoch_was_superseded_during_its_wait():
    import threading
    from levain.cockpit.team_panels import invalidate
    gates = [threading.Event() for _ in range(3)]    # read i returns once gates[i] is set
    gates[0].set()
    started: list[int] = []

    def load() -> TeamSnapshot:
        i = len(started)
        started.append(i)
        gates[i].wait(5)
        return TeamSnapshot(TeamRead("ok", MODEL), {"n": i})
    src = TeamSource(load=load, ttl_s=60.0, wait_s=3.0)
    assert src.get().sync["n"] == 0
    invalidate()                             # verb 1: the next read waits for a fresh answer
    got = []
    t = threading.Thread(target=lambda: got.append(src.get()))
    t.start()
    for _ in range(200):
        if len(started) == 2:
            break
        time.sleep(0.01)
    invalidate()                             # verb 2, while verb 1's read runs: that read is old before it lands
    gates[1].set()                           # verb 1's read lands; the follow-up (read 2) stays blocked
    t.join(5)
    (snap,) = got
    assert snap.sync["n"] == 1 and snap.age_s > 0, snap      # verb 1's read, served marked old, never as current
    gates[2].set()
    for _ in range(200):
        if src.get().sync.get("n") == 2:
            break
        time.sleep(0.01)
    assert src.get().sync["n"] == 2 and src.get().age_s == 0.0 and len(started) == 3


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_failing_read_costs_one_wait_per_epoch_not_one_per_get():
    from levain.cockpit.team_panels import invalidate

    class Die(BaseException):
        pass
    ok = [True]

    def load() -> TeamSnapshot:
        if ok[0]:
            return TeamSnapshot(TeamRead("ok", MODEL), {})
        time.sleep(0.3)
        raise Die()
    src = TeamSource(load=load, ttl_s=60.0, wait_s=2.0)
    assert src.get().read.state == "ok"
    ok[0] = False
    invalidate()
    t0 = time.monotonic()
    first = src.get()
    t1 = time.monotonic()
    second = src.get()
    t2 = time.monotonic()
    assert t1 - t0 >= 0.25 and t2 - t1 < 0.1, (t1 - t0, t2 - t1)     # one wait, then none again in the same epoch
    assert first.read.state == second.read.state == "ok" and first.age_s > 0 and second.age_s > 0   # marked old
