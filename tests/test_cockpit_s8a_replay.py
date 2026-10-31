"""K10 S8a: Replay's server half. ``read_at(ctx, T)`` is the topology read with its clock pinned at T and every source
bounded to what was known at T; a source that cannot be rebuilt for T is named ``not kept for the past`` with its
fields null; the engine serves it as ``?at=`` on the panel's own route under the live read's gate, and refuses (400)
an instant outside the last 14 h, off the minute, or on a panel with no past read.

The class these tests guard is FUTURE LEAKAGE: a source read "as of now" inside a past read puts the present into
the past. Each leakage test carries its control (the same probe at an instant after the event sees it), so a test
cannot pass because the event was never seen at all."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit import topology as topo
from levain.cockpit.engine import QueryError, ReadContext, check_replay_at
from levain.cockpit.providers import build_default_cockpit
from levain.consent.queue import ConsentQueue, authority_source
from levain.consent.questions import QuestionStore
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_topology import _Reader
from tests._h4 import h4_home

NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
B = NOW - timedelta(hours=5)          # "10:00": the day's base in these tests


def _z(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _m(minutes: int) -> datetime:
    return B + timedelta(minutes=minutes)


def _stem(agent: str, sid: str) -> str:
    return hashlib.sha1(f"{agent}:{sid}".encode()).hexdigest()[:24]


def _node(agent: str, sid: str) -> str:
    return "session:" + _stem(agent, sid)


def _record(root: Path, sid: str, cwd: str, first: datetime, last: datetime, agent: str = "claude_code") -> None:
    d = root / ".levain" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"v": 1, "agent": agent, "session_id": sid, "cwd": cwd, "first_seen": _z(first), "last_seen": _z(last),
           "last_event": "UserPromptSubmit", "context_delivered": True}
    (d / f"{_stem(agent, sid)}.json").write_text(json.dumps(rec))


def _events(root: Path, sid: str, events: list[tuple[str, datetime, str]], agent: str = "claude_code",
            pad: int = 0) -> None:
    lines = [json.dumps({"v": 1, "at": _z(at), "kind": kind, "text": text, "cut": 0}) for kind, at, text in events]
    body = "\n".join(lines) + "\n"
    if pad:
        # a rotated log: its oldest kept line is the first one written here, and it is past half the rotation size
        body = body + "\n".join(json.dumps({"v": 1, "at": _z(events[-1][1]), "kind": "reply", "text": "x" * 3000,
                                            "cut": 0}) for _ in range(pad)) + "\n"
    (root / ".levain" / "sessions" / f"{_stem(agent, sid)}.log.jsonl").write_text(body)


def _rig(tmp_path: Path, monkeypatch) -> tuple[Any, Path, Path, Path]:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    cdir = home / "consent"
    cdir.mkdir(parents=True, mode=0o700)
    root, src, _ids = _install(tmp_path)
    work = tmp_path / "work"
    for name in ("a", "b", "late"):
        (work / name).mkdir(parents=True)
    # session a: started 10:00, busy until now; a prompt at 10:01, a tool at 11:29, an edit of x.py at 11:35, more later
    _record(root, "s-a", str(work / "a"), _m(0), NOW - timedelta(minutes=1))
    _events(root, "s-a", [("prompt", _m(1), "go"), ("tool", _m(89), "Bash: ls"), ("tool", _m(95), "Edit: /shared/x.py"),
                          ("reply", _m(200), "done")])
    # session b: started 11:00, edited x.py at 11:28, quiet since 11:40
    _record(root, "s-b", str(work / "b"), _m(60), _m(100))
    _events(root, "s-b", [("prompt", _m(61), "go"), ("tool", _m(88), "Edit: /shared/x.py"), ("reply", _m(100), "ok")])
    # session late: started 11:40 (after 11:30), live now
    _record(root, "s-late", str(work / "late"), _m(100), NOW - timedelta(minutes=2))
    _events(root, "s-late", [("prompt", _m(101), "go")])
    # a consent request session a waits on: filed 11:00, its hold ending 12:00 (expired, so decided then)
    q = ConsentQueue(cdir, authority=authority_source(cdir, anchor=None), clock=lambda: _m(60), home=h4_home())
    q.file(agent="claude_code", agent_session_id="s-a", tool_name="Bash", tool_input={"command": "git push"},
           cwd=str(work / "a"), hold=timedelta(minutes=60), event_id="e-replay")
    # a question session b waits on: filed 11:00, answered 12:00
    QuestionStore(cdir, clock=lambda: _m(60)).file(
        agent="claude_code", agent_session_id="s-b", what="which?", options=[("a", "A"), ("b", "B")], rec="a",
        blocks="x", cwd=str(work / "b"))
    qs = QuestionStore(cdir, clock=lambda: _m(120))
    qs.answer(qs.open_questions()[0].n, option="a", channel="cli")
    ck = build_default_cockpit(src)
    ck.attach_consent_rows(_Reader(cdir))
    ck.attach_question_rows(QuestionStore(cdir))
    return ck, root, cdir, work


def _at(ck: Any, t: datetime) -> dict[str, Any]:
    p = ck.panel("topology", credential_class="none", at=_z(t))
    assert p["status"] in ("ok", "empty"), p["error"]
    assert p["replay"] is True and p["at"] == t.isoformat()
    return p["value"]


def _kinds(v: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [t for t in v["tasks"] if t["kind"] == kind]


# --- the brief's red tests ---------------------------------------------------------------------------------------

def test_at_1130_the_session_is_there_its_consent_waits_and_no_open_loop_is_drawn(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    live = ck.panel("topology", credential_class="none")["value"]
    assert _kinds(live, "spore") and not _kinds(live, "consent")      # control: live has the loop, no waiting consent
    v = _at(ck, _m(90))
    assert v["as_of"] == _m(90).isoformat() and v["replay"] is True
    nodes = {n["id"]: n for n in v["nodes"]}
    a = nodes[_node("claude_code", "s-a")]
    assert a["state"] == "waiting" and a["card"].startswith("consent:")
    consent = _kinds(v, "consent")
    assert len(consent) == 1 and consent[0]["state"] == "waiting" and consent[0]["workers"] == [a["id"]]
    question = _kinds(v, "question")
    assert len(question) == 1 and question[0]["workers"] == [_node("claude_code", "s-b")]
    assert _node("claude_code", "s-late") not in nodes                # started after T
    # the open loops cannot be rebuilt for T: named, never drawn as none
    assert not _kinds(v, "spore") and "spores" in v["unreadable"]
    assert any(e.startswith("spores: not kept for the past") for e in v["errors"])
    # model and tokens are read from the transcript as it is now: null at T, and said
    assert all(n["model"] is None and n["tokens"] is None for n in v["nodes"]) and "context" in v["unreadable"]


def test_at_1230_the_consent_and_the_question_no_longer_wait(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    v = _at(ck, _m(150))
    assert not _kinds(v, "consent") and not _kinds(v, "question")
    nodes = {n["id"]: n for n in v["nodes"]}
    assert nodes[_node("claude_code", "s-a")]["state"] != "waiting"
    assert _node("claude_code", "s-late") in nodes                    # started 11:40, before 12:30


@pytest.mark.parametrize("offset", [-timedelta(hours=15), timedelta(minutes=2)])
def test_an_instant_outside_the_14_h_window_is_refused_never_clamped(tmp_path, monkeypatch, offset):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    # T from the clock at call time, not the module's import-time NOW: a long run passes NOW + 1 min and the "future" is past
    t = datetime.now(timezone.utc).replace(second=0, microsecond=0) + offset
    with pytest.raises(QueryError, match="last 14 h"):
        ck.panel("topology", credential_class="none", at=_z(t))
    read_at = ck._specs["topology"].read_at
    res = read_at(ReadContext(datetime.now(timezone.utc)), _z(t))
    assert type(res).__name__ == "Fault" and "refused" in res.message


def test_a_log_line_after_t_never_moves_state_rate_or_conflicts(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    v = _at(ck, _m(90))
    a = next(n for n in v["nodes"] if n["id"] == _node("claude_code", "s-a"))
    assert a["last_event"] == _m(89).isoformat()          # the 11:35 edit and the later reply are the future
    assert a["rate"] == pytest.approx(0.2)                 # one event (11:29) in the five minutes to 11:30
    assert v["conflicts"] == [] and v["conflicts_total"] == 0     # a's edit of x.py is at 11:35
    # control: at 11:40 both edits are in the window, and the conflict is seen
    later = _at(ck, _m(100))
    assert [c["path"] for c in later["conflicts"]] == ["x.py"]
    assert set(later["conflicts"][0]["nodes"]) == {_node("claude_code", "s-a"), _node("claude_code", "s-b")}


def test_lost_if_closed_at_t_is_unknown_never_clean(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    v = _at(ck, _m(90))
    assert v["lost_if_closed"], "the sessions live at T each have a row"
    assert all(r["dirty"] is None and r["unpushed"] is None and r["dirty_names"] == [] and r["branches"] == []
               for r in v["lost_if_closed"])
    assert "lost_if_closed" in v["unreadable"]


def test_a_log_rotated_past_t_says_so_and_unknown_conflicts_are_null(tmp_path, monkeypatch):
    ck, root, _cdir, work = _rig(tmp_path, monkeypatch)
    # session a's log rotated: its oldest kept event is 11:25, after T's window (11:15 .. 11:30) began
    _events(root, "s-a", [("tool", _m(85), "Bash: ls"), ("reply", _m(200), "done")], pad=50)
    v = _at(ck, _m(90))
    a = next(n for n in v["nodes"] if n["id"] == _node("claude_code", "s-a"))
    assert a["rate"] is None and "session_logs" in v["unreadable"]
    assert v["conflicts"] is None and v["conflicts_total"] is None and "conflicts" in v["unreadable"]
    # control: the same log read live is whole enough for its own window, nothing is said
    live = ck.panel("topology", credential_class="none")["value"]
    assert "session_logs" not in live["unreadable"]


def test_a_contract_filed_after_t_is_not_on_the_board_at_t(tmp_path, monkeypatch):
    from levain.cockpit.consent import utc_stamp
    from levain.dispatch.jobs import JobStore
    from tests.test_dispatch_contract import POLICY, make_repo, registry, submission

    ck, _root, cdir, _work = _rig(tmp_path, monkeypatch)
    path, base = make_repo(tmp_path / "dispatch-repo")
    store = JobStore(cdir)
    job, _cap = store.file(submission(path, base, expires_at=utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2))),
                           resolve_template=registry(), policy=POLICY, filer_session="s-a", idem_key=None)
    live = ck.panel("topology", credential_class="none")["value"]
    assert f"dispatch:{job.id}" in {t["id"] for t in _kinds(live, "dispatch")}      # control
    v = _at(ck, NOW - timedelta(hours=1))
    assert not _kinds(v, "dispatch") and "dispatch" not in v["unreadable"]


# --- the stores' own limits: unknown, never a guess ----------------------------------------------------------------

class _Listing:
    def __init__(self, recent: tuple = (), pending: tuple = (), undecided: tuple = ()):
        self.recent, self.pending, self.undecided = recent, pending, undecided


class _Req:
    def __init__(self, filed: datetime):
        self.received_at = _z(filed)
        self.agent, self.agent_session_id, self.id, self.tool_name = "claude_code", "s", "r", "Bash"


class _Entry:
    def __init__(self, filed: datetime, decided: datetime | None, state: str = "allowed"):
        self.request, self.state = _Req(filed), state
        self.decided_at = _z(decided) if decided else None


def test_a_consent_listing_cut_before_t_cannot_answer_for_t():
    from levain.consent.listing import RECENT_CAP

    full = tuple(_Entry(_m(100), _m(110)) for _ in range(RECENT_CAP))
    with pytest.raises(LookupError, match="decided since T"):
        topo._consent_waiting(_Listing(recent=full), _m(90), NOW)
    # control: the same cap reaching back to T answers, and a request decided after T waited at T
    reach = full[:-1] + (_Entry(_m(80), _m(85)),)
    assert topo._consent_waiting(_Listing(recent=reach), _m(90), NOW) == []
    waited = topo._consent_waiting(_Listing(recent=(_Entry(_m(60), _m(120)),)), _m(90), NOW)
    assert len(waited) == 1
    # an undecided request waits on a ruling from its filing on, whatever its decided_at says
    assert len(topo._consent_waiting(_Listing(undecided=(_Entry(_m(60), _m(70), "undecided"),)), _m(90), NOW)) == 1
    with pytest.raises(LookupError, match="no decided"):
        topo._consent_waiting(type("L", (), {"pending": (), "undecided": ()})(), _m(90), NOW)


def test_a_question_store_without_its_answered_questions_cannot_answer_for_t():
    class OpenOnly:
        def open_questions(self):
            return []

    with pytest.raises(LookupError, match="no answered"):
        topo._questions_at(OpenOnly(), _m(90))


# --- the engine: the route, its gate and its refusals ------------------------------------------------------------

def test_the_head_says_replay_only_for_a_panel_with_a_past_read(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    assert ck.panel("topology", credential_class="none")["replay"] is True
    assert "replay" not in ck.panel("health", credential_class="none")
    with pytest.raises(QueryError, match="no past read"):
        ck.panel("health", credential_class="none", at=_z(_m(90)))
    with pytest.raises(QueryError, match="no q, row or limit"):
        ck.panel("topology", credential_class="none", at=_z(_m(90)), q="x")


@pytest.mark.parametrize("bad, why", [
    ("2026-10-20T11:30:00", "zone"), ("not a time", "not an ISO"), ("", "ISO-8601"),
])
def test_check_replay_at_refuses_what_it_cannot_place(bad, why):
    with pytest.raises(QueryError, match=why):
        check_replay_at(bad, NOW)


def test_check_replay_at_takes_the_minute_and_both_ends_of_the_window():
    assert check_replay_at(_z(NOW), NOW) == NOW
    assert check_replay_at(_z(NOW - timedelta(hours=14)), NOW) == NOW - timedelta(hours=14)
    assert check_replay_at((NOW - timedelta(hours=1)).isoformat(), NOW) == NOW - timedelta(hours=1)
    with pytest.raises(QueryError, match="whole minute"):
        check_replay_at(_z(NOW - timedelta(seconds=30)), NOW)
    with pytest.raises(QueryError, match="last 14 h"):
        check_replay_at(_z(NOW - timedelta(hours=14, minutes=1)), NOW)


def test_two_instants_never_share_a_validator(tmp_path, monkeypatch):
    ck, _root, _cdir, _work = _rig(tmp_path, monkeypatch)
    e1 = ck.panel("topology", credential_class="none", at=_z(_m(90)))["etag"]
    e2 = ck.panel("topology", credential_class="none", at=_z(_m(91)))["etag"]
    live = ck.panel("topology", credential_class="none")["etag"]
    assert len({e1, e2, live}) == 3


def test_the_at_read_rides_the_panel_route_and_its_gate(tmp_path):
    from levain.dashboard import SubstrateSource
    from levain.writes import WriteScope
    from tests.test_cockpit_k1 import _http, _install as _k1_install, _serve

    root, src = _k1_install(tmp_path)
    at = _z(datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=1))
    old = _z(datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=15))
    with _serve(src) as (base, _h):
        st, _hd, body = _http(f"{base}/cockpit/panel/topology.json?at={at}")
        assert st == 200, body
        p = json.loads(body)
        assert p["replay"] is True and p["at"] == datetime.fromisoformat(at[:-1] + "+00:00").isoformat()
        assert p["value"] is None or p["value"]["as_of"] == p["at"]
        assert _http(f"{base}/cockpit/panel/topology.json?at={old}")[0] == 400
        assert _http(f"{base}/cockpit/panel/tray.json?at={at}")[0] == 400        # no past read
        assert _http(f"{base}/cockpit/panel/topology.json?at=2026-10-20T11:30:15Z")[0] == 400
    gated = SubstrateSource(anneal=src.anneal, write_scope=WriteScope(
        anneal=src.anneal, ledger_root=root / ".levain", install_root=None), context_json=src.context_json)
    with _serve(gated, surface_tokens={"flowconnect": "tok"}) as (base, httpd):   # the owner key opens nothing off-box (L-1)
        httpd.is_loopback_bind = False           # an off-box bind: the live read needs the token, so does the past one
        assert _http(f"{base}/cockpit/panel/topology.json")[0] == 403
        assert _http(f"{base}/cockpit/panel/topology.json?at={at}")[0] == 403
        assert _http(f"{base}/cockpit/panel/topology.json?at={at}", {"X-Levain-Write-Token": "tok"})[0] == 200
