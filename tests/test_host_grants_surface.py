"""levain.consent.host_grants (board levain#66, S5a): the cockpit's list and revoke of host grants over a FAKE process
spawn (no sudo, no root, no network). Every test names the failure it reproduces."""
from __future__ import annotations

import copy
import logging
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from levain.consent import host_grants as HG
from levain.contracts import canon
from levain.slot import adopt_view

S1 = "s-" + "1" * 16
S2 = "s-" + "2" * 16
G1 = "eh-" + "a" * 24
G2 = "eh-" + "b" * 24
G3 = "eh-" + "c" * 24


def _slot(session: str, slot: int, grants: List[str], state: str = "ACTIVE") -> Dict[str, Any]:
    return {"slot": slot, "session": session, "kind": "interactive", "state": state,
            "entries": [{"grant": g, "host": f"{g[3:6]}.example.com", "expires": "2026-11-17T00:00:00Z", "choice": "s"}
                        for g in grants]}


def _line(obj: Dict[str, Any]) -> bytes:
    return canon.canonical_bytes(obj) + b"\n"


class FakeRoot:
    """A fake process spawn playing the root helper: it records every argv after the helper path, answers the two verbs
    from ``slots`` and, like root, removes what a revoke names."""

    def __init__(self, slots: "Optional[List[Dict[str, Any]]]" = None) -> None:
        self.slots = slots if slots is not None else [_slot(S1, 0, [G1, G2]), _slot(S2, 1, [G3])]
        self.unread: List[Dict[str, Any]] = []
        self.calls: List[List[str]] = []
        self.kwargs: List[Dict[str, Any]] = []
        self.delay = 0.0
        self.fail: Optional[str] = None            # oserror | silent | junk | refused:<name>
        self.revoke_refuse: Optional[str] = None
        self.raw: Optional[bytes] = None
        self.lock = threading.Lock()

    def verbs(self) -> List[str]:
        return [c[0] for c in self.calls]

    def __call__(self, argv: Any, **kw: Any) -> "subprocess.CompletedProcess[bytes]":
        with self.lock:
            self.calls.append(list(argv)[3:])
            self.kwargs.append(dict(kw, argv=list(argv)))
        if self.delay:
            if self.delay > kw["timeout"]:
                time.sleep(kw["timeout"])
                raise subprocess.TimeoutExpired(argv, kw["timeout"])
            time.sleep(self.delay)
        if self.fail == "oserror":
            raise OSError(2, "No such file or directory")
        if self.fail == "silent":
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"sudo: a password is required\n")
        if self.fail == "junk":
            return subprocess.CompletedProcess(argv, 0, stdout=b"not json\n", stderr=b"")
        if self.fail and self.fail.startswith("refused:"):
            return subprocess.CompletedProcess(argv, 0, stdout=_line({"ok": False, "refused": self.fail[8:]}), stderr=b"")
        if self.raw is not None:
            return subprocess.CompletedProcess(argv, 0, stdout=self.raw, stderr=b"")
        verb = argv[3]
        if verb == "egress-grants-all":
            with self.lock:
                body = {"ok": True, "store": "store-1", "slots": copy.deepcopy(self.slots), "unread": list(self.unread)}
            return subprocess.CompletedProcess(argv, 0, stdout=_line(body), stderr=b"")
        assert verb == "egress-grant-revoke", verb
        if self.revoke_refuse:
            return subprocess.CompletedProcess(argv, 0, stdout=_line({"ok": False, "refused": self.revoke_refuse}),
                                               stderr=b"")
        session, target = argv[4], argv[5]
        removed: List[str] = []
        with self.lock:
            for s in self.slots:
                if s["session"] != session:
                    continue
                for e in list(s["entries"]):
                    if target in ("all", e["grant"]):
                        s["entries"].remove(e)
                        removed.append(e["grant"])
        recorded = [] if removed or target == "all" else [target]
        return subprocess.CompletedProcess(argv, 0, stdout=_line({"ok": True, "removed": removed, "recorded": recorded}),
                                           stderr=b"")


def _far() -> float:
    return time.monotonic() + 30.0


def _surface(root: FakeRoot, **kw: Any) -> HG.HostGrantSurface:
    return HG.HostGrantSurface(spawn=root, **kw)


# -- the listing ------------------------------------------------------------------------------------------------------


def test_the_listing_calls_the_aggregate_verb_once_per_refresh_and_never_status() -> None:
    """T2: v1 walked `status` (every session ever prepared). One `egress-grants-all`, nothing else."""
    root = FakeRoot()
    out = _surface(root).listing(_far())
    assert root.calls == [["egress-grants-all"]]
    assert [(s["session"], [g["grant"] for g in s["grants"]]) for s in out["slots"]] == [(S1, [G1, G2]), (S2, [G3])]
    assert out["unread"] == [] and "unreadable" not in out


def test_the_helper_is_spawned_as_one_argv_with_no_shell_empty_stdin_and_the_launch_env() -> None:
    """C1/X5: sudo -n <helper> <verb>, argv as a list (never a shell string), stdin DEVNULL, child_env()."""
    root = FakeRoot()
    _surface(root).listing(_far())
    kw = root.kwargs[0]
    assert isinstance(kw["argv"], list) and kw["argv"][1] == "-n" and kw["argv"][3:] == ["egress-grants-all"]
    assert kw["stdin"] == subprocess.DEVNULL and kw["signal_child"] is False and "shell" not in kw
    assert isinstance(kw["env"], dict) and kw["timeout"] <= HG.LIST_TIMEOUT_S


@pytest.mark.parametrize("fail, name", [("oserror", "helper-unrunnable"), ("silent", "helper-silent"),
                                        ("junk", "helper-output-malformed"),
                                        ("refused:egress-grant-not-ready", "egress-grant-not-ready")])
def test_an_unreadable_helper_answers_a_note_never_an_empty_list(fail: str, name: str) -> None:
    """T3/T4: an unreadable list must not read as "no grants": no `slots` key, a named note."""
    root = FakeRoot()
    root.fail = fail
    out = _surface(root).listing(_far())
    assert out["unreadable"] is True and out["name"] == name and "slots" not in out and out["note"]
    if name == "egress-grant-not-ready":
        assert "not set up" in out["note"]


def test_a_timed_out_helper_is_a_note() -> None:
    root = FakeRoot()
    root.delay = 5.0
    out = _surface(root).listing(time.monotonic() + 1.6)
    assert out["unreadable"] is True and out["name"] == "helper-timeout" and "slots" not in out


@pytest.mark.parametrize("mutate", [
    lambda a: a.update(slots="x"), lambda a: a["slots"][0].update(session="s-short"),
    lambda a: a["slots"][0].update(state="DEAD"), lambda a: a["slots"][0]["entries"][0].update(grant="eh-1"),
    lambda a: a["slots"][0]["entries"][0].update(choice="x"), lambda a: a["slots"][0]["entries"][0].update(host=3),
    lambda a: a.update(unread=[{"slot": "0", "name": "x"}])])
def test_a_misshapen_answer_is_a_note_not_a_partial_list(mutate: Any) -> None:
    body = {"slots": [_slot(S1, 0, [G1])], "unread": []}
    mutate(body)
    with pytest.raises(Exception) as e:
        HG.parse_listing(body)
    assert getattr(e.value, "name", "") == "host-grants-answer-malformed"
    root = FakeRoot()
    root.raw = _line({"ok": True, "store": "s", **body})
    out = _surface(root).listing(_far())
    assert out["unreadable"] is True and out["name"] == "host-grants-answer-malformed" and "slots" not in out


def test_an_answer_over_the_cap_is_malformed() -> None:
    root = FakeRoot()
    root.raw = b"x" * (HG.ANSWER_MAX + 1)
    out = _surface(root).listing(_far())
    assert out["unreadable"] is True and out["name"] == "host-grants-answer-malformed"


def test_an_unread_slot_is_a_named_row_while_the_other_slots_list() -> None:
    """T5: root's per-slot `unread` shows as a note next to the slots that did list."""
    root = FakeRoot([_slot(S1, 0, [G1])])
    root.unread = [{"slot": 3, "name": "egress-grants-unreadable"}]
    out = _surface(root).listing(_far())
    assert [s["session"] for s in out["slots"]] == [S1] and out["unread"] == [{"slot": 3, "name": "egress-grants-unreadable"}]


def test_a_braked_session_lists_as_paused_and_stays_revocable() -> None:
    root = FakeRoot([_slot(S1, 0, [G1], state="BRAKED")])
    surf = _surface(root)
    assert surf.listing(_far())["slots"][0]["paused"] is True
    body, status = surf.revoke(S1, G1, "browser", _far())
    assert status == 200 and body["result"] == "revoked"


# -- the allowlist, before any spawn --------------------------------------------------------------------------------


@pytest.mark.parametrize("verb, args", [
    ("status", ()), ("status", (S1,)), ("egress-grants-list", (S1,)), ("egress-grant", (S1,)),
    ("egress-refusals", (S1,)), ("adopt-open", ()), ("discard", (S1,)), ("restore", (S1,)), ("", ()),
    ("egress-grants-all", (S1,)), ("egress-grant-revoke", ()), ("egress-grant-revoke", (S1,)),
    ("egress-grant-revoke", (S1, G1, "x")), ("egress-grant-revoke", ("-s", "all")),
    ("egress-grant-revoke", (S1 + " ", "all")), ("egress-grant-revoke", (S1, "ALL")), ("egress-grant-revoke", (S1, "all ")),
    ("egress-grant-revoke", (S1, "all\n")), ("egress-grant-revoke", (S1, "-" + G1)), ("egress-grant-revoke", (S1, G1.upper())),
    ("egress-grant-revoke", (S1, G1 + "0")), ("egress-grant-revoke", (S1 + "\n", "all")),
    ("egress-grant-revoke", ("s-" + "g" * 16, "all")), ("egress-grant-revoke", (S1, 5)), ("egress-grant-revoke", (None, "all"))])
def test_the_spawn_function_refuses_any_other_verb_or_argument_before_it_spawns(verb: str, args: Any) -> None:
    """T16 (C1): the allowlist is inside the module's one spawn function, not in its callers."""
    root = FakeRoot()
    with pytest.raises(ValueError):
        _surface(root).call(verb, args, 5.0)
    assert root.calls == []


@pytest.mark.parametrize("session, target", [("s-xyz", "all"), (S1, "ALL"), (S1, "all "), (S1, "-" + G1), (S1, "eh-1"),
                                             ("-s", "all"), (S1, ""), (None, "all"), (S1, None), (S1, 5), (S1, ["all"])])
def test_a_malformed_revoke_is_400_with_no_spawn(session: Any, target: Any) -> None:
    """T6 (fail fast before a spawn; root's grammar plus argv-without-shell is the boundary)."""
    root = FakeRoot()
    body, status = _surface(root).revoke(session, target, "browser", _far())
    assert status == 400 and body["error"] == "bad_request" and root.calls == []


# -- the revoke ---------------------------------------------------------------------------------------------------------


def test_a_revoke_of_one_id_is_revoked_and_the_reply_carries_the_fresh_listing() -> None:
    """T7/T19: removed -> `revoked`; the reply's listing is read AFTER the revoke (the cache was cleared first)."""
    root = FakeRoot()
    surf = _surface(root)
    surf.listing(_far())                                     # cached
    body, status = surf.revoke(S1, G1, "browser", _far())
    assert status == 200 and body["result"] == "revoked" and body["count"] == 1
    assert root.verbs() == ["egress-grants-all", "egress-grant-revoke", "egress-grants-all"]
    assert [g["grant"] for g in body["listing"]["slots"][0]["grants"]] == [G2]
    assert root.calls[1] == ["egress-grant-revoke", S1, G1]


def test_revoke_all_words_the_count_of_grants_that_were_in_force_and_a_late_grant_is_a_row() -> None:
    """T21 (C4): `revoked the N grants that were in force`; a grant landing after is a row, not an error."""
    root = FakeRoot()
    surf = _surface(root)
    body, status = surf.revoke(S1, "all", "browser", _far())
    assert status == 200 and body["result"] == "revoked" and body["count"] == 2
    assert body["message"] == "revoked the 2 grants that were in force"
    assert root.calls[1] == ["egress-grant-revoke", S1, "all"] and len(root.calls) == 3
    root.slots[0]["entries"].append(_slot(S1, 0, [G3])["entries"][0])      # a grant lands seconds later
    surf._invalidate()
    out = surf.listing(_far())
    assert [g["grant"] for g in out["slots"][0]["grants"]] == [G3] and "unreadable" not in out


def test_a_per_id_revoke_of_an_id_the_listing_does_not_hold_is_gone_with_no_revoke_spawn() -> None:
    """T8 (C2): a hand-made POST must not reach root's ledger. Mutation: drop the membership check -> a revoke is seen."""
    root = FakeRoot()
    body, status = _surface(root).revoke(S1, "eh-" + "9" * 24, "browser", _far())
    assert status == 200 and body["result"] == "gone" and "not in the list" in body["message"]
    assert root.verbs() == ["egress-grants-all"]                              # the listing only: no revoke verb
    # the id of ANOTHER session's grant is not this session's grant either
    body, _ = _surface(root).revoke(S1, G3, "browser", _far())
    assert body["result"] == "gone" and "egress-grant-revoke" not in root.verbs()
    # nor is `all` for a session the listing does not hold
    body, _ = _surface(root).revoke("s-" + "3" * 16, "all", "browser", _far())
    assert body["result"] == "gone" and "egress-grant-revoke" not in root.verbs()


def test_root_saying_recorded_is_gone_never_revoked() -> None:
    """The race: held at the check, gone at root's lock (root answers `recorded`): "already gone", not "revoked"."""
    root = FakeRoot()
    surf = _surface(root)
    surf.listing(_far())
    root.slots[0]["entries"] = []                            # it expired between the list and the tap
    body, status = surf.revoke(S1, G1, "browser", _far())
    assert status == 200 and body["result"] == "gone" and body["message"].startswith("that grant was not in force")


def test_a_full_ledger_at_the_race_is_gone_with_no_retry_and_no_all() -> None:
    """T9 (v2): `egress-grant-ledger-full` is "already gone": one revoke spawn, never a retry, never `all`."""
    root = FakeRoot()
    root.revoke_refuse = "egress-grant-ledger-full"
    body, status = _surface(root).revoke(S1, G1, "browser", _far())
    assert status == 200 and body["result"] == "gone"
    assert root.verbs().count("egress-grant-revoke") == 1 and ["egress-grant-revoke", S1, "all"] not in root.calls


@pytest.mark.parametrize("name, status, error", [("session-busy", 409, "busy"), ("egress-grant-busy", 409, "busy"),
                                                 ("egress-grant-write-failed", 502, "refused"),
                                                 ("egress-grant-unsure", 502, "refused"),
                                                 ("verb-not-allowed-for-caller", 502, "refused")])
def test_a_refusal_shows_by_name_the_row_stays_and_nothing_retries(name: str, status: int, error: str) -> None:
    """T7 / C6: never "revoked" on a refusal; busy says "tap again"; one revoke spawn."""
    root = FakeRoot()
    root.revoke_refuse = name
    body, st = _surface(root).revoke(S1, G1, "browser", _far())
    assert (st, body["error"]) == (status, error) and body["name"] == name and "result" not in body
    assert root.verbs().count("egress-grant-revoke") == 1
    if error == "busy":
        assert "tap again" in body["message"]
    if name == "egress-grant-unsure":
        assert "power loss" in body["message"]


def test_two_simultaneous_taps_on_one_id_answer_one_revoked_and_one_gone() -> None:
    """v1 T13: one spawn at a time; the second finds root has nothing and the page is told so, honestly."""
    root = FakeRoot()
    root.delay = 0.15
    surf = _surface(root)
    surf.listing(_far())
    out: List[Any] = []
    ts = [threading.Thread(target=lambda: out.append(surf.revoke(S1, G1, f"c{i}", _far())[0]["result"])) for i in range(2)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert sorted(out) == ["gone", "revoked"]


def test_a_revoke_with_the_list_unreadable_is_not_revoked_and_says_so() -> None:
    root = FakeRoot()
    root.fail = "silent"
    body, status = _surface(root).revoke(S1, G1, "browser", _far())
    assert status == 503 and body["error"] == "unreadable" and "not revoked" in body["message"]
    assert root.verbs() == ["egress-grants-all"]


# -- the budget ---------------------------------------------------------------------------------------------------------


def test_the_request_answers_by_its_wall_and_starts_no_spawn_with_under_a_second_left() -> None:
    """T17 (X7): a helper that never answers is abandoned at the wall; with < MIN_SPAWN_S left nothing starts."""
    root = FakeRoot()
    root.delay = 30.0
    t0 = time.monotonic()
    out = _surface(root).listing(t0 + 2.0)
    assert time.monotonic() - t0 < 3.5 and out["unreadable"] is True and "slots" not in out
    assert root.kwargs[0]["timeout"] <= 2.0
    fresh = FakeRoot()
    out = _surface(fresh).listing(time.monotonic() + HG.MIN_SPAWN_S - 0.2)
    assert fresh.calls == [] and out["unreadable"] is True and out["name"] == "host-grants-deadline"
    body, status = _surface(fresh).revoke(S1, G1, "browser", time.monotonic() + HG.MIN_SPAWN_S - 0.2)
    assert fresh.verbs().count("egress-grant-revoke") == 0 and status in (409, 503)


def test_the_wall_is_under_the_pages_fetch_timeout() -> None:
    """Run-first: the page's reads give up at CONSENT_FETCH_TIMEOUT_MS = 15000 (dashboard_core.js)."""
    assert HG.HOSTGRANT_WALL_S < 15.0 and HG.LIST_TIMEOUT_S < HG.HOSTGRANT_WALL_S


def test_concurrent_listings_share_one_spawn_and_followers_give_back_their_gate_slot() -> None:
    """T18 (X7/C6): 8 GETs -> one helper child; the 7 followers release their request-gate slot before they wait."""
    root = FakeRoot()
    root.delay = 0.4
    surf = _surface(root)
    outs: List[Any] = []
    released: List[int] = []
    start = threading.Barrier(8)

    def go(i: int) -> None:
        start.wait()
        outs.append(surf.listing(_far(), release_gate=lambda: released.append(i)))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert root.verbs() == ["egress-grants-all"] and len(outs) == 8
    assert all(o == outs[0] for o in outs) and len(released) == 7        # the leader keeps its slot, then the handler frees it


def test_a_failure_is_cached_as_the_note_and_a_success_for_five_seconds() -> None:
    """T19: the cached failure is the note, never `{"slots": []}`; the cache lasts CACHE_S."""
    now = [100.0]
    root = FakeRoot()
    root.fail = "silent"
    surf = _surface(root, clock=lambda: now[0])
    dl = lambda: now[0] + 30.0   # noqa: E731
    a = surf.listing(dl())
    b = surf.listing(dl())
    assert a == b and a["unreadable"] is True and "slots" not in b and len(root.calls) == 1
    now[0] += HG.CACHE_S + 0.1
    root.fail = None
    c = surf.listing(dl())
    assert "slots" in c and len(root.calls) == 2
    surf.listing(dl())
    assert len(root.calls) == 2
    now[0] += HG.CACHE_S + 0.1
    surf.listing(dl())
    assert len(root.calls) == 3


def test_a_deadline_short_failure_is_not_cached() -> None:
    root = FakeRoot()
    surf = _surface(root)
    assert surf.listing(time.monotonic() + 0.3)["name"] == "host-grants-deadline"
    assert "slots" in surf.listing(_far())


# -- the rate limit and the log ---------------------------------------------------------------------------------------


def test_the_eleventh_revoke_in_a_minute_from_one_credential_is_429_and_reaches_no_root_revoke() -> None:
    """T20: per credential NAME; another credential is unaffected; the window ends. The limit counts the revoke calls
    that reach root (`all` on a listed session always does)."""
    now = [0.0]
    root = FakeRoot()
    surf = _surface(root, clock=lambda: now[0])
    for _ in range(HG.RATE_LIMIT):
        body, status = surf.revoke(S1, "all", "paired-d-1", now[0] + 30)
        assert status == 200
    n = root.verbs().count("egress-grant-revoke")
    assert n == HG.RATE_LIMIT
    body, status = surf.revoke(S1, "all", "paired-d-1", now[0] + 30)
    assert status == 429 and body["error"] == "rate_limited" and root.verbs().count("egress-grant-revoke") == n
    assert surf.revoke(S1, "all", "browser", now[0] + 30)[1] == 200
    now[0] += HG.RATE_WINDOW_S + 1
    assert surf.revoke(S1, "all", "paired-d-1", now[0] + 30)[1] == 200


def test_a_malformed_busy_or_gone_revoke_spends_no_rate_slot() -> None:
    """L3 r1 (complement 42, deepseek 73): a 400, a 409 and a gone-with-no-spawn reach no root revoke, so they cost
    no slot of the budget; reproduced: ten malformed POSTs made the next valid revoke 429."""
    root = FakeRoot()
    surf = _surface(root)
    for _ in range(HG.RATE_LIMIT):
        assert surf.revoke("s-bad", "all", "browser", _far())[1] == 400
        assert surf.revoke(S1, "eh-" + "9" * 24, "browser", _far())[1] == 200
    assert surf._spawn_lock.acquire(timeout=1)          # another request's helper holds the one spawn
    try:
        for _ in range(HG.RATE_LIMIT):
            assert surf.revoke(S1, G1, "browser", time.monotonic() + HG.MIN_SPAWN_S + 0.05)[1] == 409
    finally:
        surf._spawn_lock.release()
    body, status = surf.revoke(S1, G1, "browser", _far())
    assert status == 200 and body["result"] == "revoked"


# -- a helper past its wall (L3 r1) -------------------------------------------------------------------------------------


def test_a_timed_out_helper_keeps_the_one_spawn_until_its_child_exits() -> None:
    """L3 r1 (codex 55, complement 16): the root helper is never signalled, so a timed-out child may still run. It keeps
    the spawn lock until it exits: no second root helper meanwhile, and a request then answers busy. Reproduced on the
    first cut: the lock was free at the timeout and a second helper started beside the first."""
    starts: List[float] = []

    def spawn(argv: Any, **kw: Any) -> "subprocess.CompletedProcess[bytes]":
        starts.append(time.monotonic())    # a REAL child through the real runner, playing a root helper that is slow
        return adopt_view._run([sys.executable, "-c", "import time; time.sleep(3.0)"], **kw)

    surf = HG.HostGrantSurface(spawn=spawn)
    assert surf.listing(time.monotonic() + 1.2)["name"] == "helper-timeout"
    surf._invalidate()
    second = surf.listing(time.monotonic() + 1.5)
    assert len(starts) == 1 and second["name"] == "host-grants-helper-running" and "slots" not in second
    assert "previous root helper is still running" in second["note"]
    body, status = surf.revoke(S1, G1, "browser", time.monotonic() + 1.5)
    assert len(starts) == 1 and status == 503
    assert surf._spawn_lock.acquire(timeout=5.0)        # the child exits; the reaper gives the lock back
    surf._spawn_lock.release()


def test_a_revoke_root_did_not_answer_clearly_is_unknown_never_not_revoked() -> None:
    """L3 r1 (complement 24, codex 55): root may apply a revoke after the CLI stopped waiting, so a timed-out, silent or
    malformed revoke answer is `unknown` with a fresh listing, never "not revoked: root refused". Reproduced on the
    first cut: 502 refused "not revoked: root refused (helper-timeout)"."""
    root = FakeRoot()
    surf = _surface(root)
    surf.listing(_far())
    root.delay = 30.0                                       # the revoke runs past its wall
    body, status = surf.revoke(S1, G1, "browser", time.monotonic() + 2.5)
    assert status == 502 and body["error"] == "unknown" and body["name"] == "helper-timeout"
    assert "not revoked" not in body["message"] and "may or may not" in body["message"] and "listing" in body
    root.delay = 0.0
    for raw, name in ((b"", "helper-silent"), (_line({"ok": True, "removed": "x", "recorded": []}),
                                               "host-grants-answer-malformed")):
        surf._invalidate()                                  # the reply's own listing read the bad answer too
        assert "slots" in surf.listing(_far())
        root.raw = raw
        body, status = surf.revoke(S1, G2, "browser", _far())
        root.raw = None
        assert (status, body["error"], body["name"]) == (502, "unknown", name) and "not revoked" not in body["message"]


def _slow_revoke(root: FakeRoot, sleep_s: float, wall_s: float, starts: List[str]) -> Any:
    """A spawn: the listing from ``root``; a revoke a REAL child through the real runner that outlives ``wall_s``."""

    def spawn(argv: Any, **kw: Any) -> "subprocess.CompletedProcess[bytes]":
        starts.append(argv[3])
        if argv[3] == HG.REVOKE_VERB:
            return adopt_view._run([sys.executable, "-c", f"import time; time.sleep({sleep_s})"],
                                   **dict(kw, timeout=wall_s))
        return root(argv, **kw)

    return spawn


def test_an_unknown_revoke_says_to_refresh_after_the_helper_never_that_the_list_shows_which() -> None:
    """L3 verify (complement, codex LOW): after a revoke times out its child still runs and holds the spawn, so the
    reply's listing cannot settle it. Reproduced: the message said "the list shows which" over a busy note."""
    starts: List[str] = []
    surf = HG.HostGrantSurface(spawn=_slow_revoke(FakeRoot(), 4.0, 0.5, starts))
    body, status = surf.revoke(S1, G1, "browser", time.monotonic() + 2.5)
    assert (status, body["error"], body["name"]) == (502, "unknown", "helper-timeout")
    assert "slots" not in body["listing"] and body["listing"]["name"] == "host-grants-helper-running"
    assert "shows which" not in body["message"] and "refresh the list once root's helper has finished" in body["message"]
    assert starts.count(HG.REVOKE_VERB) == 1


def test_a_revoke_while_a_timed_out_helper_runs_says_so_not_a_bare_busy() -> None:
    """L3 verify (complement MED): the reaper's wait has no bound of its own (root's bounds end the helper; nothing here
    kills it), so the busy answer names the cause. Reproduced: a bare "busy, tap again" while the old helper ran."""
    starts: List[str] = []
    root = FakeRoot()
    surf = HG.HostGrantSurface(spawn=_slow_revoke(root, 3.0, 0.3, starts))
    held = surf.listing(_far())
    assert surf.revoke(S1, G1, "browser", time.monotonic() + 1.4)[0]["error"] == "unknown"
    surf._cache, surf._cache_until = held, time.monotonic() + HG.CACHE_S     # the list as the page last read it
    body, status = surf.revoke(S1, G2, "browser", time.monotonic() + 1.5)
    assert (status, body["error"], body["name"]) == (409, "busy", "host-grants-helper-running")
    assert "previous root helper is still running" in body["message"] and starts.count(HG.REVOKE_VERB) == 1


def test_a_reaper_that_cannot_start_leaves_no_lock_held_after_the_child_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """L3 verify (codex LOW): if the reaper thread cannot start, the child is kept and checked by the next request,
    which spawns nothing beside it while it runs and lists once it has exited. Reproduced: `_left` was cleared before
    the start, the only release was in the thread that never ran, and every later request answered busy for good."""
    starts: List[float] = []
    root = FakeRoot()

    def spawn(argv: Any, **kw: Any) -> "subprocess.CompletedProcess[bytes]":
        starts.append(time.monotonic())
        if len(starts) == 1:
            return adopt_view._run([sys.executable, "-c", "import time; time.sleep(2.5)"], **kw)
        return root(argv, **kw)

    class NoThread:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    import types
    surf = HG.HostGrantSurface(spawn=spawn)
    monkeypatch.setattr(HG, "threading", types.SimpleNamespace(Thread=NoThread))
    assert surf.listing(time.monotonic() + 1.2)["name"] == "helper-timeout"
    surf._invalidate()
    assert surf.listing(time.monotonic() + 1.5)["name"] == "host-grants-helper-running" and len(starts) == 1
    child = surf._left
    assert child is not None
    give_up = time.monotonic() + 60.0                       # bounded poll on the child itself, no sleep tuned to it
    while child.poll() is None and time.monotonic() < give_up:
        time.sleep(0.05)
    assert child.poll() is not None                         # the child has exited
    surf._invalidate()
    assert "slots" in surf.listing(time.monotonic() + 5.0) and len(starts) == 2


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_while_starting_the_reaper_is_not_swallowed(monkeypatch: pytest.MonkeyPatch, exc: type) -> None:
    """L3 verify round 2 (LOW): `except BaseException` around Thread.start() returned normally on a KeyboardInterrupt.
    The state is restored (child kept, lock given back) and the interrupt propagates."""
    surf = HG.HostGrantSurface(spawn=FakeRoot())

    class Child:
        def wait(self) -> None:
            pass

    class NoThread:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def start(self) -> None:
            raise exc()

    import types
    monkeypatch.setattr(HG, "threading", types.SimpleNamespace(Thread=NoThread))
    child = Child()
    assert surf._spawn_lock.acquire(blocking=False)
    surf._left = child
    with pytest.raises(exc):
        surf._release_spawn()
    assert surf._left is child and surf._reaping is None
    assert surf._spawn_lock.acquire(blocking=False)         # given back, not held for good
    surf._spawn_lock.release()


def test_a_revoke_that_fails_before_root_is_started_is_not_revoked_never_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """L3 verify (codex LOW): `host-grants-unreadable` from building the env (before any spawn) said root may have
    revoked it though root never ran. Reproduced: 502 unknown "may or may not be revoked" with no spawn."""
    root = FakeRoot()
    surf = _surface(root)
    surf.listing(_far())                                    # the membership check reads this cached list

    def broken_env() -> Dict[str, str]:
        raise KeyError("HOME")

    import levain.launch
    monkeypatch.setattr(levain.launch, "child_env", broken_env)
    body, status = surf.revoke(S1, G1, "browser", _far())
    assert (status, body["error"], body["name"]) == (502, "refused", "host-grants-unreadable")
    assert body["message"].startswith("not revoked") and "egress-grant-revoke" not in root.verbs()


def test_the_revoke_log_line_names_the_credential_session_target_and_roots_answer(caplog: pytest.LogCaptureFixture) -> None:
    """T22 (surface half): the surface never holds a token, only the credential's NAME."""
    root = FakeRoot()
    with caplog.at_level(logging.INFO, logger=HG.__name__):
        _surface(root).revoke(S1, G1, "paired-d-abc", _far())
    line = [r.getMessage() for r in caplog.records if "host grant revoke" in r.getMessage()]
    assert len(line) == 1 and "paired-d-abc" in line[0] and S1 in line[0] and G1 in line[0] and "removed=1" in line[0]


# -- L4 review 1116-19 (L2, L5) ------------------------------------------------------------------------------------------


def test_l2_an_exception_after_roots_revoke_answered_is_unknown_never_not_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    """L2: the catch-all wrapped the code that runs AFTER root answered; an exception there read "not revoked" though
    root had applied the revoke. Mutation: answer the catch-all "not revoked" whatever began -> this fails."""
    root = FakeRoot()
    surf = _surface(root)

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("after root answered")
    monkeypatch.setattr(surf, "_answer", boom)
    body, status = surf.revoke(S1, G1, "browser", _far())
    assert root.verbs().count("egress-grant-revoke") == 1 and root.slots[0]["entries"][0]["grant"] == G2   # root did it
    assert (status, body["error"]) == (502, "unknown") and not body["message"].startswith("not revoked")
    assert "may or may not be revoked" in body["message"]


def test_l2_an_exception_before_the_revoke_spawn_is_still_not_revoked_though_the_listing_spawned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing's own spawn sets the shared ``_began``: an exception after the LISTING but before any revoke spawn
    is "not revoked". Mutation: key the catch-all on ``self._began`` -> this reads unknown."""
    root = FakeRoot()
    surf = _surface(root)

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("before the revoke spawn")
    monkeypatch.setattr(surf, "_rate_ok", boom)
    body, status = surf.revoke(S1, G1, "browser", _far())
    assert root.verbs() == ["egress-grants-all"] and surf._began is True
    assert (status, body["error"]) == (502, "refused") and body["message"].startswith("not revoked")


def test_l5_recorded_and_ledger_full_name_a_replaced_grant_too() -> None:
    """L5: a `recorded` id (or one root could not record with the ledger full) may have been REPLACED by a newer grant
    for the same host, not only expired or revoked: the words say each, and never that the host is now unallowed."""
    root = FakeRoot()
    surf = _surface(root)
    surf.listing(_far())
    root.slots[0]["entries"][0]["grant"] = "eh-" + "d" * 24       # a newer grant for the same host replaced G1
    body, status = surf.revoke(S1, G1, "browser", _far())
    assert status == 200 and body["result"] == "gone"
    assert "replaced by a newer grant for the same host" in body["message"] and "already gone: expired or revoked" not in body["message"]
    root = FakeRoot()
    root.revoke_refuse = "egress-grant-ledger-full"
    body, status = _surface(root).revoke(S1, G1, "browser", _far())
    assert status == 200 and body["result"] == "gone"
    assert "replaced by a newer grant for the same host" in body["message"] and "ledger is full" in body["message"]


def test_a_revoke_spawns_two_listings_and_one_revoke_not_one_listing():
    """Diogenes 11-17 candidate B: the module docstring once said one listing spawn; the real sequence is the listing
    that decides the id is in force, the revoke, and the fresh listing the answer carries."""
    root = FakeRoot()
    body, status = _surface(root).revoke(S1, G1, "cred", _far())
    assert status == 200 and body["result"] == "revoked"
    assert root.verbs() == ["egress-grants-all", "egress-grant-revoke", "egress-grants-all"]
