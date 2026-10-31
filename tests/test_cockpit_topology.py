"""F12a: the ``topology`` kind's contract and the Levain provider behind it (F12_SPEC, with the 14a amendment).

The hostile-repository tests are the ones the trust boundary needs: "Lost if closed" runs ``git status`` in a working
tree an agent writes, and a status read hashes files, which runs a filter driver's ``clean`` from whatever config git
reads. The read runs on a private git directory, so the repository's own config is never one of them. Each test first
proves the planted programs really run under a plain git (the control), so the second half cannot pass by planting
something git would never have run; the race test runs the agent's config swap against the read."""
from __future__ import annotations

import copy
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit import Cockpit, ProviderSpec, Read
from levain.cockpit import activity as act
from levain.cockpit import topology as topo
from levain.cockpit.engine import ReadContext
from levain.cockpit.providers import OPTIONAL_NATIVE, build_default_cockpit
from levain.cockpit.results import Fault
from levain.consent.listing import read_listing
from levain.consent.queue import ConsentQueue, authority_source
from levain.consent.questions import QuestionStore
from tests.test_cockpit_activity import _git, _repo, _session, _stamp
from tests.test_cockpit_k2a import _install
from tests._h4 import h4_home

NOW_ISO = "2026-10-19T15:00:00+00:00"


# --- the contract ----------------------------------------------------------------------------------------------

def _good() -> dict[str, Any]:
    """The F12_SPEC shape, every field present, as the LEVAIN mock frame draws it (trimmed)."""
    return {
        "hub": {"id": "levain", "label": "Sage"},
        "groups": [{"id": "repo:ledgerline", "label": "ledgerline", "repo": "ledgerline"},
                   {"id": "dir:scratch", "label": "scratch", "repo": None}],
        "nodes": [
            {"id": "s1", "group": "repo:ledgerline", "parent": None, "label": "claude_code · ledgerline",
             "agent": "claude_code", "model": "claude-opus-5-5", "state": "waiting", "tool": None, "tokens": 120000,
             "started": NOW_ISO, "last_event": NOW_ISO, "rate": 3.5, "card": "consent:r1"},
            {"id": "s1a", "group": "repo:ledgerline", "parent": "s1", "label": "subagent", "agent": None,
             "model": None, "state": "running", "tool": "Edit", "tokens": None, "started": NOW_ISO,
             "last_event": NOW_ISO, "rate": 0, "card": None},
            {"id": "s2", "group": "dir:scratch", "parent": None, "label": "codex · scratch", "agent": "codex",
             "model": None, "state": "stalled", "tool": None, "tokens": None, "started": NOW_ISO,
             "last_event": NOW_ISO, "rate": 0.0, "card": None},
        ],
        "conflicts": [{"path": "money.py", "nodes": ["s1", "s2"]}],
        "counts": {"tokens_today": None},
        "as_of": NOW_ISO,
        "tasks": [
            {"id": "consent:r1", "kind": "consent", "label": "claude_code · Bash", "state": "waiting",
             "waits_on": ["consent:r1"], "card": "consent:r1",
             "outcomes": [{"label": "guard test failed", "at": NOW_ISO, "ok": False}],
             "cost": {"usd": 3.8, "tokens": 450000, "source": "dispatch"}, "workers": ["s1", "s1a"]},
            {"id": "unattached", "kind": "unattached", "label": "unattached work", "state": "stuck", "waits_on": [],
             "card": None, "outcomes": [], "cost": {"usd": None, "tokens": None, "source": None}, "workers": ["s2"]},
        ],
        "lost_if_closed": [
            {"node": "s1", "dirty": 2, "dirty_names": ["a.py", "notes"], "unpushed": 1, "branches": ["main"],
             "uncaptured_episodes": None, "running_children": 1},
            {"node": "s2", "dirty": None, "dirty_names": [], "unpushed": None, "branches": [],
             "uncaptured_episodes": None, "running_children": None},
        ],
        "errors": [], "unreadable": [],
    }


def _panel(value: Any) -> dict[str, Any]:
    ck = Cockpit()
    ck.register(ProviderSpec(id="p", kind="topology", title="P", priority="gauge", read=lambda c: Read(value=value),
                             region="operate"))
    p = ck.panel("p")
    assert p is not None
    return p


def test_the_spec_shape_is_accepted_whole():
    p = _panel(_good())
    assert p["status"] == "ok", p
    assert p["kind"] == "topology" and p["value"]["nodes"][0]["card"] == "consent:r1"
    v = _good()
    v["conflicts"] = None                   # not tracked: null, never an empty list read as "no conflicts"
    assert _panel(v)["status"] == "ok"
    m = copy.deepcopy(_good())          # the decision mark's keys are optional, null where unknown, and a good set is accepted
    m["hub"].update(needs_open=2, needs_first="np-a1", needs_first_card="np:np-a1", needs_first_what="ship it? " * 13)
    m["nodes"][0].update(needs_open=0)
    m["nodes"][1].update(needs_open=1, needs_first=None, needs_first_card=None, needs_first_what=None)
    assert _panel(m)["status"] == "ok", _panel(m)
    # the two text strings are clipped by the page, never refused for length; a card id of exactly 120 code points (astral) is accepted
    m["hub"].update(needs_first="x" * 500, needs_first_what="y" * 500, needs_first_card="\U0001f600" * 120)
    assert _panel(m)["status"] == "ok", _panel(m)
    empty = {**_good(), "groups": [], "nodes": [], "conflicts": [], "tasks": [], "lost_if_closed": []}
    assert _panel(empty)["status"] == "empty"


@pytest.mark.parametrize("how", ["absent", None, 0, 850_000_000])
def test_the_cache_read_count_is_optional_and_a_non_negative_integer_when_sent(how):
    v = copy.deepcopy(_good())
    if how != "absent":
        v["counts"]["cache_read_today"] = how
    assert _panel(v)["status"] == "ok", _panel(v)


def _set(path: str, val: Any):
    def f(v):
        cur = v
        keys = path.split(".")
        for k in keys[:-1]:
            cur = cur[int(k)] if k.isdigit() else cur[k]
        last = keys[-1]
        if val is _DEL:
            del cur[last]
        elif last.isdigit():
            cur[int(last)] = val
        else:
            cur[last] = val
    return f


_DEL = object()

MALFORMED = [
    ("hub", "x", "value.hub"),
    ("hub.id", "", "value.hub.id"),
    ("hub.label", "x" * 49, "longer than 48"),
    ("groups.0.id", "repo:ledgerline\u202e", "invisible or format character"),
    ("groups.1.id", "repo:ledgerline", "repeated"),
    ("groups.0.repo", 3, "value.groups[0].repo"),
    ("nodes.0.group", "repo:nope", "must name a group id"),
    ("nodes.0.parent", "s1", "another node's id"),
    ("nodes.1.parent", "ghost", "another node's id"),
    ("nodes.0.state", "stuck", "value.nodes[0].state"),
    ("nodes.0.tokens", -1, "non-negative integer"),
    ("nodes.0.tokens", True, "non-negative integer"),
    ("nodes.0.started", "yesterday", "ISO-8601"),
    ("nodes.0.rate", float("nan"), "events per minute"),
    ("nodes.0.rate", -0.5, "events per minute"),
    ("nodes.0.label", "a​b", "invisible or format character"),
    ("nodes.0.model", "m" * 49, "longer than 48"),
    ("conflicts", _DEL, "value.conflicts is required"),
    ("conflicts.0.path", "src/money.py", "basename"),
    ("conflicts.0.nodes", ["s1"], "two or more distinct node ids"),
    ("conflicts.0.nodes", ["s1", "s1"], "two or more distinct node ids"),
    ("counts", {}, "tokens_today is required"),
    ("counts.tokens_today", "lots", "non-negative integer"),
    ("counts.cache_read_today", -5, "value.counts.cache_read_today"),
    ("counts.cache_read_today", 1.5, "value.counts.cache_read_today"),
    ("counts.cache_read_today", "12", "value.counts.cache_read_today"),
    ("counts.cache_read_today", True, "value.counts.cache_read_today"),
    ("as_of", None, "value.as_of"),
    ("tasks", _DEL, "value.tasks is required"),
    ("tasks.0.kind", "process", "value.tasks[0].kind"),
    ("tasks.0.state", "stalled", "value.tasks[0].state"),
    ("tasks.1.id", "consent:r1", "repeated"),
    ("tasks.0.waits_on", "consent:r1", "list of strings"),
    ("tasks.0.outcomes.0.ok", "yes", "boolean"),
    ("tasks.0.outcomes.0.at", "now", "ISO-8601"),
    ("tasks.0.cost", None, "value.tasks[0].cost"),
    ("tasks.0.cost.source", "estimate", "value.tasks[0].cost.source"),
    ("tasks.1.cost", {"usd": 1.0, "tokens": None, "source": None}, "never an estimate"),
    ("tasks.1.cost", {"usd": 1.0, "tokens": 5, "source": "transcript"}, "no price"),
    ("tasks.1.cost", {"usd": None, "tokens": None, "source": "transcript"}, "carries tokens"),
    ("tasks.0.workers.0", "ghost", "must name a node id"),
    ("lost_if_closed", _DEL, "value.lost_if_closed is required"),
    ("lost_if_closed.0.node", "ghost", "must name a node id"),
    ("lost_if_closed.0.dirty", -2, "non-negative integer"),
    ("lost_if_closed.0.unpushed", _DEL, "null when not read"),
    ("lost_if_closed.0.dirty_names.0", "src/a.py", "basename"),
    ("lost_if_closed.0.branches", "main", "list of strings"),
    ("errors", "nope", "list of strings"),
    ("hub.needs_open", -1, "value.hub.needs_open"),
    ("hub.needs_open", 1.5, "value.hub.needs_open"),
    ("hub.needs_open", "2", "value.hub.needs_open"),
    ("hub.needs_first_card", "np:a\u202eb", "invisible or format character"),
    ("nodes.0.needs_first_card", "x" * 121, "longer than 120"),
    ("nodes.1.needs_first_what", "a\u200bb", "invisible or format character"),
    ("nodes.1.needs_first_what", 7, "value.nodes[1].needs_first_what"),
]


@pytest.mark.parametrize("path,val,needle", MALFORMED, ids=[f"{m[0]}={m[1]!r}"[:60] for m in MALFORMED])
def test_each_malformed_field_is_refused_by_name(path, val, needle):
    v = copy.deepcopy(_good())
    _set(path, val)(v)
    p = _panel(v)
    assert p["status"] == "error", p
    assert needle in p["error"] and "'p'" in p["error"], p["error"]


def test_the_compact_profile_is_counts_and_a_link():
    ck = Cockpit()
    ck.register(ProviderSpec(id="p", kind="topology", title="P", priority="gauge", read=lambda c: Read(value=_good()),
                             region="operate"))
    p = ck.panel("p", profile="compact")
    assert p["value"] == {"as_of": NOW_ISO, "nodes": 3, "tasks": 2, "waiting": 1}
    assert p["next"] == "/cockpit/panel/p.json?profile=full"


# --- the provider ----------------------------------------------------------------------------------------------

def _log(root: Path, agent: str, sid: str, events: list[tuple[str, timedelta]]) -> None:
    import hashlib

    stem = hashlib.sha1(f"{agent}:{sid}".encode()).hexdigest()[:24]
    lines = [json.dumps({"v": 1, "at": _stamp(ago), "kind": kind, "text": "SECRET-LOG-WORDS", "cut": 0})
             for kind, ago in events]
    (root / ".levain" / "sessions" / f"{stem}.log.jsonl").write_text("\n".join(lines) + "\n")


def _stem(agent: str, sid: str) -> str:
    import hashlib

    return "session:" + hashlib.sha1(f"{agent}:{sid}".encode()).hexdigest()[:24]


def _queue(tmp_path: Path, monkeypatch) -> tuple[Path, ConsentQueue, QuestionStore]:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    cdir = home / "consent"
    cdir.mkdir(parents=True, mode=0o700)
    q = ConsentQueue(cdir, authority=authority_source(cdir, anchor=None), home=h4_home())
    return cdir, q, QuestionStore(cdir)


class _Reader:
    """The server's consent reader: a callable listing with the queue's directory, as web_server attaches it."""

    def __init__(self, directory: Path):
        self.directory = directory

    def __call__(self, now):
        return read_listing(now=now)


def _rig(tmp_path: Path, monkeypatch):
    cdir, q, qs = _queue(tmp_path, monkeypatch)
    root, src, _ids = _install(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    for name in ("wait", "stall", "run", "done", "q", "old"):
        (work / name).mkdir()
    q.file(agent="claude_code", agent_session_id="s-wait", tool_name="Bash", tool_input={"command": "rm -rf \u202e x"},
           cwd=str(work / "wait"), hold=timedelta(minutes=5), event_id="e1")
    qs.file(agent="codex", agent_session_id="s-q", what="SECRET-WORDS ship it?", options=[("a", "A"), ("b", "B")],
            rec="a", blocks="x", cwd=str(work / "q"))
    _session(root, "s-wait", str(work / "wait"), timedelta(minutes=9))
    _session(root, "s-stall", str(work / "stall"), timedelta(minutes=9))
    _log(root, "claude_code", "s-stall", [("prompt", timedelta(minutes=9)), ("tool", timedelta(minutes=8))])
    _session(root, "s-run", str(work / "run"), timedelta(minutes=3))
    _log(root, "claude_code", "s-run", [("prompt", timedelta(minutes=3)), ("tool", timedelta(minutes=1)),
                                        ("tool", timedelta(seconds=30))])
    _session(root, "s-done", str(work / "done"), timedelta(minutes=8))
    _log(root, "claude_code", "s-done", [("prompt", timedelta(minutes=8)), ("reply", timedelta(minutes=7))])
    _session(root, "s-q", str(work / "q"), timedelta(minutes=2), agent="codex")
    _session(root, "s-old", str(work / "old"), timedelta(hours=2))
    ck = build_default_cockpit(src)
    ck.attach_consent_rows(_Reader(cdir))
    ck.attach_question_rows(qs)
    return ck, root, src, cdir, work


def test_waiting_and_stalled_are_two_states_and_unattached_work_is_never_a_guessed_task(tmp_path, monkeypatch):
    ck, _root, _src, _cdir, _work = _rig(tmp_path, monkeypatch)
    p = ck.panel("topology", credential_class="none")
    assert p["status"] == "ok", p
    v = p["value"]
    nodes = {n["id"]: n for n in v["nodes"]}
    st = {nid: n["state"] for nid, n in nodes.items()}
    assert st[_stem("claude_code", "s-wait")] == "waiting"
    assert st[_stem("codex", "s-q")] == "waiting"
    assert st[_stem("claude_code", "s-stall")] == "stalled"       # mid-turn (a tool), silent 8 m, no record
    assert st[_stem("claude_code", "s-run")] == "running"
    assert st[_stem("claude_code", "s-done")] == "idle"           # its turn ended in a reply: not stalled
    assert st[_stem("claude_code", "s-old")] == "idle"
    # waiting carries its card; stalled carries none (silence is not a record)
    assert nodes[_stem("claude_code", "s-wait")]["card"].startswith("consent:")
    assert nodes[_stem("codex", "s-q")]["card"].startswith("question:")
    assert nodes[_stem("claude_code", "s-stall")]["card"] is None
    assert nodes[_stem("claude_code", "s-run")]["rate"] == pytest.approx(0.6)    # three events in five minutes
    tasks = {t["id"]: t for t in v["tasks"]}
    consent = [t for t in v["tasks"] if t["kind"] == "consent"]
    assert len(consent) == 1 and consent[0]["state"] == "waiting" and consent[0]["card"] == consent[0]["id"]
    assert consent[0]["workers"] == [_stem("claude_code", "s-wait")] and consent[0]["label"] == "claude_code · Bash"
    question = [t for t in v["tasks"] if t["kind"] == "question"]
    assert len(question) == 1 and question[0]["workers"] == [_stem("codex", "s-q")]
    assert question[0]["label"].startswith("codex · question #")
    un = tasks["unattached"]
    assert un["kind"] == "unattached" and un["state"] == "stuck"   # a stalled worker sits there
    assert set(un["workers"]) == {_stem("claude_code", s) for s in ("s-stall", "s-run")}     # active ones only
    assert not ({w for t in v["tasks"] if t["kind"] != "unattached" for w in t["workers"]} & set(un["workers"]))
    # every cost is "not tracked" on this branch, never a guess
    assert all(t["cost"] == {"usd": None, "tokens": None, "source": None} for t in v["tasks"])
    assert v["conflicts"] == [] and v["counts"]["tokens_today"] is None and v["counts"]["tokens_read"] == 0   # no transcript on this install: unknown, named
    # the open loop from the install is a task
    assert any(t["kind"] == "spore" and t["label"] == "a loop" for t in v["tasks"])
    assert {g["label"] for g in v["groups"] if g["id"].startswith("dir:")} >= {"wait", "stall"}
    wire = json.dumps(v)
    assert "SECRET-WORDS" not in wire and "rm -rf" not in wire and "SECRET-LOG-WORDS" not in wire
    assert str(tmp_path) not in wire                                 # no absolute path reaches the wire
    assert v["unreadable"] == [], v["errors"]
    # lost-if-closed: one row per live session, the old one is not live
    assert {r["node"] for r in v["lost_if_closed"]} == {_stem("claude_code", s) for s in ("s-wait", "s-stall", "s-run", "s-done")} | {_stem("codex", "s-q")}
    assert all(r["dirty"] is None and r["uncaptured_episodes"] is None and r["running_children"] is None
               for r in v["lost_if_closed"])                       # no repository there: unknown, never 0


def test_an_unread_source_is_named_and_unread_sessions_are_a_fault(tmp_path, monkeypatch):
    ck, root, _src, _cdir, _work = _rig(tmp_path, monkeypatch)

    def boom(now):
        raise OSError("no")
    ck.attach_consent_rows(boom)
    v = ck.panel("topology", credential_class="none")["value"]
    assert "decisions" in v["unreadable"] and "dispatch" in v["unreadable"]   # no directory on a bare function
    assert any(e.startswith("decisions: unreadable") for e in v["errors"])
    # nothing waits on an unread queue, and nothing reads as "nothing waiting" either: the error names it
    assert _stem("claude_code", "s-wait") not in {n["id"] for n in v["nodes"] if n["state"] == "waiting"}
    read = topo.topology_reader(install_root=None, consent=lambda: None, questions=lambda: None,
                                hub_label=lambda c: "x")
    got = read(ReadContext(datetime.now(timezone.utc)))
    assert isinstance(got, Fault) and got.message.startswith("topology is blind")
    sdir = root / ".levain" / "sessions"
    os.chmod(sdir, 0)
    try:
        read = topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: None,
                                    hub_label=lambda c: "x")
        got = read(ReadContext(datetime.now(timezone.utc)))
        assert isinstance(got, Fault) and "session records are unreadable" in got.message
    finally:
        os.chmod(sdir, 0o700)


def test_registered_under_its_own_id_with_the_opt_out(tmp_path):
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    assert "topology" in ck.panel_ids and "topology" in OPTIONAL_NATIVE
    flow_ids = {"sky", "memory", "weather", "brief", "ops", "inbox", "stream", "roster"}
    assert "topology" not in flow_ids                                   # flow registers its own ids on top
    ck2 = build_default_cockpit(src, exclude=("topology",))
    assert "topology" not in ck2.panel_ids


# --- dispatch contracts and the ledger -------------------------------------------------------------------------

def test_dispatch_contracts_are_tasks_with_ledger_outcomes_and_no_cost(tmp_path, monkeypatch):
    from levain.cockpit.consent import utc_stamp
    from levain.dispatch.jobs import JobStore
    from levain.dispatch.outcomes import OutcomeRow, OutcomeStore
    from tests.test_dispatch_contract import POLICY, make_repo, registry, submission

    ck, root, _src, cdir, _work = _rig(tmp_path, monkeypatch)
    path, base = make_repo(tmp_path / "dispatch-repo")
    now = datetime.now(timezone.utc)
    store = JobStore(cdir)
    exp = utc_stamp(now + timedelta(hours=2))
    a, _cap = store.file(submission(path, base, expires_at=exp), resolve_template=registry(), policy=POLICY,
                         filer_session="s-run", idem_key=None)
    for to, cause in (("signed", "signature"), ("running", "slot_taken"), ("verifying", "runs_exited"),
                      ("verified", "verifier_pass"), ("authorised", "human_authorises"), ("adopted", "adopted")):
        a = store._transition(a.id, to, cause)
    OutcomeStore(cdir).append(OutcomeRow(job_id=a.id, run_index=0, model_requested="m", model_answered="m",
                                         route="r", harness="h 1", template_id="fix-lint", label="verifier_pass",
                                         source="machine", at=utc_stamp(now), verifier_kind="black-box"))
    b, _cap = store.file(submission(path, base, expires_at=exp), resolve_template=registry(), policy=POLICY,
                         filer_session="nobody", idem_key=None)
    v = ck.panel("topology", credential_class="none")["value"]
    assert "dispatch" not in v["unreadable"], v["errors"]
    ds = {t["id"]: t for t in v["tasks"] if t["kind"] == "dispatch"}
    ta, tb = ds[f"dispatch:{a.id}"], ds[f"dispatch:{b.id}"]
    assert ta["state"] == "done" and ta["workers"] == []        # the filer is not the worker
    assert [o["label"] for o in ta["outcomes"]][-1] == "verifier pass" and ta["outcomes"][-1]["ok"] is True
    assert any(o["label"] == "adopted (adopted)" and o["ok"] for o in ta["outcomes"])
    assert tb["state"] == "waiting" and tb["waits_on"] == [f"dispatch:{b.id}"] and tb["workers"] == []
    assert ta["label"] == "Fix the lint in a.py." and "Keep b as is" not in json.dumps(v)    # the goal's first line only
    assert ta["cost"] == tb["cost"] == {"usd": None, "tokens": None, "source": None}
    # a contract names no worker: the filing session stays where it was
    assert _stem("claude_code", "s-run") in next(t for t in v["tasks"] if t["kind"] == "unattached")["workers"]


def test_a_consent_directory_with_no_dispatch_store_creates_none(tmp_path, monkeypatch):
    ck, _root, _src, cdir, _work = _rig(tmp_path, monkeypatch)
    v = ck.panel("topology", credential_class="none")["value"]
    assert not [t for t in v["tasks"] if t["kind"] == "dispatch"] and "dispatch" not in v["unreadable"]
    assert not (cdir / "dispatch.db").exists() and not (cdir / "outcomes.db").exists()


# --- lost if closed --------------------------------------------------------------------------------------------

def _commit(repo: Path, msg: str, files: dict[str, str]) -> None:
    for name, body in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(body)
    _git(repo, "add", "-A", check=True)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg, check=True)


def _lost(v: dict[str, Any], node: str) -> dict[str, Any]:
    return next(r for r in v["lost_if_closed"] if r["node"] == node)


def test_lost_if_closed_counts_dirty_files_and_unpushed_commits_on_a_real_repo(tmp_path):
    repo = _repo(tmp_path / "work" / "ledgerline")
    _commit(repo, "two", {"src/a.py": "a\n", "README": "r\n"})
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD", check=True)        # pushed up to here
    _commit(repo, "three", {"src/b.py": "b\n"})                                    # one commit no remote holds
    (repo / "src" / "a.py").write_text("changed\n")
    (repo / "notes.txt").write_text("untracked\n")
    wt = tmp_path / "work" / "ledgerline-seat"
    _git(repo, "worktree", "add", "-q", "-b", "seat", str(wt), "refs/remotes/origin/main", check=True)
    root = tmp_path / "inst"
    _session(root, "in-main", str(repo / "src"), timedelta(minutes=2))
    _session(root, "in-wt", str(wt), timedelta(minutes=2))
    read = topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: None,
                                hub_label=lambda c: "Sage", home=str(tmp_path))
    v = read(ReadContext(datetime.now(timezone.utc))).value
    main, seat = _lost(v, _stem("claude_code", "in-main")), _lost(v, _stem("claude_code", "in-wt"))
    assert main["dirty"] == 2 and sorted(main["dirty_names"]) == ["a.py", "notes.txt"], main
    assert main["unpushed"] == 1 and main["branches"] == ["main"], main
    assert seat["dirty"] == 0 and seat["unpushed"] == 0 and seat["branches"] == [], seat
    # the worktree folds into its main repository's group
    groups = {n["id"]: n["group"] for n in v["nodes"]}
    gid = groups[_stem("claude_code", "in-main")]
    assert gid == groups[_stem("claude_code", "in-wt")] and gid.startswith("repo:") and "ledgerline" not in gid
    assert next(g for g in v["groups"] if g["id"] == gid)["label"] == "ledgerline"
    assert [e for e in v["errors"] if "ledgerline" in e] == []
    assert _panel(v)["status"] == "ok"                                    # the engine's contract holds the feed


def test_the_worktree_scan_names_what_its_deadline_skips():
    t = iter([0.0, 0.0, 7.0, 7.0])
    trees = [((Path("/c1"), Path("/c1"), Path("/t1")), "one"), ((Path("/c2"), Path("/c2"), Path("/t2")), "two")]
    got, errors = topo._lost_scan(trees, clock=lambda: next(t),
                                  state=lambda c, g, top, timeout: {"dirty": 0, "dirty_names": [], "branch": None,
                                                                    "unpushed": 0})
    assert list(got) == ["/t1"]
    assert errors == [f"two: skipped, the worktree scan passed its {act.ACTIVITY_GIT_DEADLINE_S:g}s deadline"]


def _plant(repo: Path, bin_dir: Path, sentinels: Path) -> None:
    """A filter driver whose clean program a status read runs, an fsmonitor, hooks, and a tracked file whose stat no
    longer matches the index (so status must hash it, through the driver)."""
    bin_dir.mkdir()
    sentinels.mkdir()
    for name in ("filter", "fsmonitor", "hook"):
        p = bin_dir / name
        p.write_text(f"#!/bin/sh\ntouch {sentinels / name}\ncat\n")
        p.chmod(0o755)
    hooks = bin_dir / "hooks"
    hooks.mkdir()
    for h in ("post-index-change", "reference-transaction"):
        (hooks / h).write_text(f"#!/bin/sh\ntouch {sentinels / 'hook'}\n")
        (hooks / h).chmod(0o755)
    _commit(repo, "tracked", {"a.txt": "same\n", ".gitattributes": "* filter=evil\n"})
    with open(repo / ".git" / "config", "a") as fh:
        fh.write(f"[filter \"evil\"]\n\tclean = {bin_dir / 'filter'}\n\tsmudge = {bin_dir / 'filter'}\n"
                 f"\trequired = true\n[core]\n\tfsmonitor = {bin_dir / 'fsmonitor'}\n\thooksPath = {hooks}\n")


def _touch_tracked(repo: Path) -> None:
    later = time.time() + 120
    os.utime(repo / "a.txt", (later, later))       # same content, new stat: status must re-hash it


def test_a_hostile_repo_runs_nothing_through_lost_if_closed(tmp_path, capsys):
    repo = tmp_path / "hostile"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", check=True)
    sentinels = tmp_path / "sentinels"
    _plant(repo, tmp_path / "bin", sentinels)
    # (a) CONTROL: a plain git status, as anyone would run it there, with no neutralisation
    for p in sentinels.iterdir():
        p.unlink()
    _touch_tracked(repo)
    _git(repo, "status")
    fired = sorted(p.name for p in sentinels.iterdir())
    with capsys.disabled():
        print(f"\n[topology-hostile] control (plain git status) fired: {fired}")
    if "filter" not in fired:
        pytest.fail(f"CONTROL FAILED: the filter driver did not fire under a plain git status ({fired}), so the "
                    "read below proves nothing")
    for p in sentinels.iterdir():
        p.unlink()
    # (b) the topology read, the driver (required) still in the repository's config: the tree reads on a private git
    # directory, nothing runs, and the counts are true (the touched file's content is unchanged: not dirty)
    _touch_tracked(repo)
    (repo / "new.txt").write_text("x\n")
    root = tmp_path / "inst"
    _session(root, "hs", str(repo), timedelta(minutes=2))
    read = topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: None,
                                hub_label=lambda c: "x", home=str(tmp_path))
    idx = repo / ".git" / "index"
    before = (idx.read_bytes(), idx.stat().st_mtime_ns)
    v = read(ReadContext(datetime.now(timezone.utc))).value
    row = _lost(v, _stem("claude_code", "hs"))
    after = sorted(p.name for p in sentinels.iterdir())
    with capsys.disabled():
        print(f"[topology-hostile] driver (required) in its config: lost {row}; errors "
              f"{[e for e in v['errors'] if e.startswith('hostile')]}; sentinels after the read: {after}")
    assert after == [], f"the topology read ran a planted program: {after}"
    assert [e for e in v["errors"] if e.startswith("hostile")] == []
    assert row["dirty"] == 1 and row["dirty_names"] == ["new.txt"] and row["unpushed"] == 1, row
    assert (idx.read_bytes(), idx.stat().st_mtime_ns) == before                # the real index is never written
    # (c) the tracked file really changed: counted, and still nothing runs (git hashed it with no driver)
    (repo / "a.txt").write_text("changed\n")
    v = read(ReadContext(datetime.now(timezone.utc), fresh=True)).value
    row = _lost(v, _stem("claude_code", "hs"))
    assert row["dirty"] == 2 and sorted(row["dirty_names"]) == ["a.txt", "new.txt"], row
    assert not list(sentinels.iterdir())
    cfg = repo / ".git" / "config"
    # (d) an include is refused unread, as on the commit read
    inc = tmp_path / "bin" / "inc.cfg"
    inc.write_text("[core]\n\tpager = cat\n")
    with open(cfg, "a") as fh:
        fh.write(f"[include]\n\tpath = {inc}\n")
    v = read(ReadContext(datetime.now(timezone.utc), fresh=True)).value
    assert "hostile: its config includes another config file, not read" in v["errors"]
    assert not list(sentinels.iterdir())


def test_a_linked_worktrees_own_config_worktree_runs_nothing(tmp_path):
    repo = _repo(tmp_path / "work" / "main")
    _commit(repo, "attrs", {"a.txt": "same\n", ".gitattributes": "* filter=x\n"})
    wt = tmp_path / "work" / "main-seat"
    _git(repo, "worktree", "add", "-q", "-b", "seat", str(wt), check=True)
    _git(repo, "config", "extensions.worktreeConfig", "true", check=True)
    found, why = act.find_worktree(str(wt), str(tmp_path))
    assert why is None and found is not None
    common, gitdir, top = found
    assert common == (repo / ".git").resolve() and gitdir.parent.name == "worktrees" and top == wt.resolve()
    sent = tmp_path / "sent"
    sent.mkdir()
    prog = tmp_path / "prog"
    prog.write_text(f"#!/bin/sh\ntouch {sent / 'x'}\ncat\n")
    prog.chmod(0o755)
    (gitdir / "config.worktree").write_text(f"[filter \"x\"]\n\tclean = {prog}\n\trequired = true\n")
    _touch_tracked(wt)
    _git(wt, "status")                                                     # control: git reads it for this worktree
    assert (sent / "x").exists(), "CONTROL FAILED: the worktree's config.worktree driver did not run under git status"
    (sent / "x").unlink()
    _touch_tracked(wt)
    got = act.git_worktree_state(common, gitdir, top, 3.0)
    assert not (sent / "x").exists()
    assert got == {"dirty": 0, "dirty_names": [], "branch": "seat", "unpushed": 2}, got


def test_the_agents_config_swapped_under_the_read_runs_nothing(tmp_path, capsys):
    """L2 H1: the agent swaps a filter driver in and out of its config while the cockpit reads, and keeps the index
    racily clean (its mtime no later than the entry's), so git compares content through whatever driver it knows."""
    import threading

    repo = _repo(tmp_path / "r")
    _commit(repo, "attrs", {"a.txt": "same\n", ".gitattributes": "* filter=evil\n"})
    sent = tmp_path / "sent"
    sent.mkdir()
    prog = tmp_path / "prog"
    prog.write_text(f"#!/bin/sh\ntouch {sent / 'filter'}\ncat\n")
    prog.chmod(0o755)
    cfg = repo / ".git" / "config"
    clean = cfg.read_text()
    evil = clean + f"[filter \"evil\"]\n\tclean = {prog}\n\trequired = true\n"
    common, gitdir, top = act.find_worktree(str(repo), str(tmp_path))[0]

    def racy() -> None:
        st = os.stat(repo / "a.txt")
        os.utime(repo / ".git" / "index", (st.st_mtime - 10, st.st_mtime - 10))

    cfg.write_text(evil)                                                   # control: the racy entry runs the driver
    racy()
    _git(repo, "diff-files", "--name-only", env={"GIT_OPTIONAL_LOCKS": "0"})
    assert (sent / "filter").exists(), "CONTROL FAILED: a racily clean entry did not run the driver"
    (sent / "filter").unlink()
    cfg.write_text(clean)
    stop = threading.Event()

    def flipper() -> None:
        i = 0
        while not stop.is_set():
            tmp = repo / ".git" / "c.tmp"
            tmp.write_text(evil if i % 2 else clean)
            os.replace(tmp, cfg)
            i += 1
            time.sleep(0.002)

    t = threading.Thread(target=flipper, daemon=True)
    t.start()
    got: list[Any] = []
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.0:
            racy()
            try:
                got.append(act.git_worktree_state(common, gitdir, top, 3.0)["dirty"])
            except Exception as exc:  # noqa: BLE001 - counted, and failed below
                got.append(type(exc).__name__)
    finally:
        stop.set()
        t.join()
    with capsys.disabled():
        print(f"\n[topology-race] {len(got)} reads under the swap; results {sorted(set(map(str, got)))}; "
              f"fired: {(sent / 'filter').exists()}")
    assert not (sent / "filter").exists(), "the read ran the agent's driver"
    assert len(got) >= 5 and set(got) == {0}, got


def test_a_status_git_cannot_read_is_unknown_never_clean(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r")
    _commit(repo, "one", {"a.txt": "a\n"})
    (repo / "b.txt").write_text("untracked\n")
    common, gitdir, top = act.find_worktree(str(repo), str(tmp_path))[0]
    made: list[str] = []
    real = act.tempfile.mkdtemp

    def spy(*a: Any, **k: Any) -> str:
        d = real(*a, **k)
        made.append(d)
        assert (os.stat(d).st_mode & 0o777) == 0o700
        return d

    monkeypatch.setattr(act.tempfile, "mkdtemp", spy)
    assert act.git_worktree_state(common, gitdir, top, 3.0)["dirty"] == 1
    (repo / ".git" / "index").write_bytes(b"DIRC not an index")
    with pytest.raises(RuntimeError):
        act.git_worktree_state(common, gitdir, top, 3.0)
    assert len(made) == 2 and not any(os.path.exists(d) for d in made)    # the private git dir never outlives a read
    root = tmp_path / "inst"
    _session(root, "s", str(repo), timedelta(minutes=2))
    v = topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: None,
                             hub_label=lambda c: "x", home=str(tmp_path))(ReadContext(datetime.now(timezone.utc))).value
    row = _lost(v, _stem("claude_code", "s"))
    assert row["dirty"] is None and row["dirty_names"] == [], row
    assert "r: worktree unreadable (RuntimeError)" in v["errors"]


def test_the_repositorys_exclude_and_core_settings_still_shape_the_count(tmp_path):
    repo = _repo(tmp_path / "r")
    _commit(repo, "one", {"a.txt": "a\n"})
    (repo / "ignored.log").write_text("x\n")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("*.log\n")
    common, gitdir, top = act.find_worktree(str(repo), str(tmp_path))[0]
    assert act.git_worktree_state(common, gitdir, top, 3.0)["dirty"] == 0
    (repo / "a.txt").chmod(0o755)                                          # a mode change, seen with core.filemode
    _git(repo, "config", "core.filemode", "false", check=True)
    assert act.git_worktree_state(common, gitdir, top, 3.0)["dirty"] == 0
    _git(repo, "config", "core.filemode", "true", check=True)
    assert act.git_worktree_state(common, gitdir, top, 3.0)["dirty"] == 1


def test_the_48_char_cut_never_splits_a_hidden_character_marker():
    for pad in range(40, 48):
        got = act._short("a" * pad + "\u202e" + "b" * 20)
        assert len(got) <= 48 and got.endswith("…"), got
        body = got[:-1]
        assert body.count("⟨") == body.count("⟩"), got                    # whole marker or none
        assert body in ("a" * pad, "a" * pad + "⟨U+202E⟩" + "b" * (47 - pad - 8)), got
    assert act._short("x" * 48) == "x" * 48


def test_a_split_index_reads_through_its_shared_index_copy(tmp_path):
    repo = _repo(tmp_path / "r")
    _commit(repo, "one", {"a.txt": "a\n", "b.txt": "b\n"})
    _git(repo, "update-index", "--split-index", check=True)
    assert any(p.name.startswith("sharedindex.") for p in (repo / ".git").iterdir())
    (repo / "a.txt").write_text("changed\n")
    (repo / "u.txt").write_text("u\n")
    common, gitdir, top = act.find_worktree(str(repo), str(tmp_path))[0]
    got = act.git_worktree_state(common, gitdir, top, 3.0)
    assert got["dirty"] == 2 and sorted(got["dirty_names"]) == ["a.txt", "u.txt"], got


def test_detached_and_unborn_heads_read_as_the_real_repository_has_them(tmp_path):
    repo = _repo(tmp_path / "r")
    _commit(repo, "one", {"a.txt": "a\n"})
    common, gitdir, top = act.find_worktree(str(repo), str(tmp_path))[0]
    assert act.git_worktree_state(common, gitdir, top, 3.0)["branch"] == "main"
    _git(repo, "checkout", "-q", "--detach", check=True)
    got = act.git_worktree_state(common, gitdir, top, 3.0)
    assert got["branch"] is None and got["unpushed"] == 2 and got["dirty"] == 0, got
    fresh = tmp_path / "u"
    fresh.mkdir()
    _git(fresh, "init", "-q", "-b", "main", check=True)
    (fresh / "n.txt").write_text("n\n")
    _git(fresh, "add", "n.txt", check=True)
    common, gitdir, top = act.find_worktree(str(fresh), str(tmp_path))[0]
    assert act.git_worktree_state(common, gitdir, top, 3.0) == \
        {"dirty": 1, "dirty_names": ["n.txt"], "branch": None, "unpushed": 0}


def test_an_open_loop_is_running_only_with_a_worker_never_by_recent_touch():
    from types import SimpleNamespace

    today = datetime.now(timezone.utc).date()
    fresh = SimpleNamespace(id="spore-1", text="touched today", seen=today.isoformat(), created=today.isoformat())
    old = SimpleNamespace(id="spore-2", text="touched long ago", seen="2020-01-01", created="2020-01-01")
    undated = SimpleNamespace(id="spore-3", text="never dated", seen=None, created=None)
    tasks = topo._spore_tasks([fresh, old, undated])
    # a spore links no session, so no worker is ever on it: a loop nobody is on is OPEN (an age and a heat), never STUCK (a live worker gone silent)
    assert [t["state"] for t in tasks] == ["open", "open", "open"]
    assert all(t["workers"] == [] for t in tasks)
    # the recent-touch heuristic is gone, not renamed
    assert not hasattr(topo, "SPORE_QUIET_DAYS")


# --- F12a fix 2, lane B: one wire normaliser, empty-but-blind, workers, unattached, group ids, rate ---------------

def _marker_whole(s: str) -> bool:
    """Every ``⟨`` opens a ``⟨U+XXXX⟩`` marker that is complete: a cut never leaves half of one."""
    import re

    return re.sub(r"⟨U\+[0-9A-F]{4,6}⟩", "", s).count("⟨") == 0 and "⟩" not in re.sub(r"⟨U\+[0-9A-F]{4,6}⟩", "", s)


@pytest.mark.parametrize("raw,base,want", [
    ("a\\b.txt", True, "a⟨U+005C⟩b.txt"),
    ("src/a.py", True, "src⟨U+002F⟩a.py"),
    ("src/a.py", False, "src/a.py"),                   # a label may carry a slash (a branch is feature/x)
    ("ㅤ", True, "⟨U+3164⟩"),
    (" ", True, "(unnamed)"),
    ("", False, "(unnamed)"),
    ("\n\t ", False, "(unnamed)"),
    ("a\u202eb", True, "a⟨U+202E⟩b"),
    ("a   b\nc", False, "a b c"),
    (None, False, "None"),
])
def test_one_wire_normaliser_maps_every_hostile_string_to_something_the_validator_accepts(raw, base, want):
    from levain.cockpit.display_safe import visible

    got = topo._wire(raw, base=base)
    assert got == want
    assert visible(got) == got and got and len(got) <= topo.LABEL_MAX


def test_the_normaliser_cut_never_splits_a_marker():
    for lead in range(40, 50):
        got = topo._wire("x" * lead + "\u202e" * 4 + "tail")
        assert len(got) <= topo.LABEL_MAX and _marker_whole(got.removesuffix("…")), (lead, got)
    assert topo._wire("x" * 200).endswith("…") and len(topo._wire("x" * 200)) == topo.LABEL_MAX


def _stub_scan(names: list[str], branch: str | None = "main"):
    def scan(trees):
        return ({str(top): {"dirty": len(names), "dirty_names": list(names), "branch": branch, "unpushed": 1}
                 for (_c, _g, top), _label in trees}, [])
    return scan


@pytest.mark.parametrize("names", [["a\\b.txt"], [" "], ["ㅤ"], ["a\u202etxt.exe"], ["x" * 60], ["a/b"]])
def test_a_hostile_file_name_leaves_the_panel_a_value_never_a_fault(tmp_path, names):
    repo = _repo(tmp_path / "work" / "ledgerline")
    root = tmp_path / "inst"
    _session(root, "s", str(repo), timedelta(minutes=2))
    read = topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: None,
                                hub_label=lambda c: "Sage", home=str(tmp_path), scan=_stub_scan(names))
    v = read(ReadContext(datetime.now(timezone.utc))).value
    p = _panel(v)
    assert p["status"] in ("ok", "partial"), p
    got = _lost(v, _stem("claude_code", "s"))["dirty_names"]
    assert len(got) == 1 and "/" not in got[0] and "\\" not in got[0] and got[0]


def test_hostile_names_on_a_real_repo_and_a_hostile_directory_name(tmp_path):
    repo = _repo(tmp_path / "work" / "ledgerline")
    (repo / " ").write_text("x")
    (repo / "a\\b.txt").write_text("x")
    weird = tmp_path / "work" / "ㅤ"
    weird.mkdir()
    root = tmp_path / "inst"
    _session(root, "in-repo", str(repo), timedelta(minutes=2))
    _session(root, "in-weird", str(weird), timedelta(minutes=2))
    read = topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: None,
                                hub_label=lambda c: "Sage", home=str(tmp_path))
    v = read(ReadContext(datetime.now(timezone.utc))).value
    assert _panel(v)["status"] in ("ok", "partial"), _panel(v)
    names = _lost(v, _stem("claude_code", "in-repo"))["dirty_names"]
    assert sorted(names) == ["(unnamed)", "a⟨U+005C⟩b.txt"], names


def test_an_all_blind_read_is_not_an_empty_panel():
    from levain.cockpit.engine import Cockpit as C

    blind = {"nodes": [], "tasks": [], "unreadable": ["decisions", "questions"]}
    assert C._value_empty("topology", blind) is False
    assert C._value_empty("topology", {"nodes": [], "tasks": [], "unreadable": []}) is True
    assert C._value_empty("topology", {"nodes": [], "tasks": []}) is True
    v = {**_good(), "groups": [], "nodes": [], "conflicts": [], "tasks": [], "lost_if_closed": [],
         "unreadable": ["decisions"], "errors": ["decisions: no consent queue is attached to this cockpit"]}
    assert _panel(v)["status"] != "empty"


def test_a_dispatch_filers_session_is_not_its_worker_and_only_a_record_waits(tmp_path, monkeypatch):
    from levain.cockpit.consent import utc_stamp
    from levain.dispatch.jobs import JobStore
    from tests.test_dispatch_contract import POLICY, make_repo, registry, submission

    _ck, _root, _src, cdir, _work = _rig(tmp_path, monkeypatch)
    path, base = make_repo(tmp_path / "dispatch-repo")
    now = datetime.now(timezone.utc)
    store = JobStore(cdir)
    j, _cap = store.file(submission(path, base, expires_at=utc_stamp(now + timedelta(hours=2))),
                         resolve_template=registry(), policy=POLICY, filer_session="s-run", idem_key=None)
    seen = {}
    for to, cause in (("filed", None), ("signed", "signature"), ("running", "slot_taken"),
                      ("verifying", "runs_exited"), ("verified", "verifier_pass")):
        if cause is not None:
            store._transition(j.id, to, cause)
        tasks, errs = topo._dispatch_tasks(cdir, now)
        t = next(x for x in tasks if x["id"] == f"dispatch:{j.id}")
        assert t["workers"] == [], (to, t)               # the filing session ("s-run") is no worker, in any state
        seen[to] = t["state"]
    # filed and verified wait on a person (a record); the states the job runtime holds are running, never silence
    assert seen == {"filed": "waiting", "signed": "running", "running": "running", "verifying": "running",
                    "verified": "waiting"}, seen


def test_unattached_is_for_active_sessions_only_and_is_never_done(tmp_path):
    root = tmp_path / "inst"
    work = tmp_path / "w"
    for n in ("run", "idle"):
        (work / n).mkdir(parents=True)
    _session(root, "s-run", str(work / "run"), timedelta(minutes=3))
    _log(root, "claude_code", "s-run", [("prompt", timedelta(minutes=3)), ("tool", timedelta(seconds=30))])
    _session(root, "s-idle", str(work / "idle"), timedelta(minutes=8))
    _log(root, "claude_code", "s-idle", [("prompt", timedelta(minutes=8)), ("reply", timedelta(minutes=7))])
    mk = lambda: topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: None,  # noqa: E731
                                      hub_label=lambda c: "x", home=str(tmp_path))
    v = mk()(ReadContext(datetime.now(timezone.utc))).value
    un = next(t for t in v["tasks"] if t["kind"] == "unattached")
    assert un["workers"] == [_stem("claude_code", "s-run")] and un["state"] == "running"
    assert _stem("claude_code", "s-idle") in {n["id"] for n in v["nodes"]}      # still in the Agents lens
    # only idle sessions: no unattached task at all, never a `done` one
    for p in (root / ".levain" / "sessions").glob(f"{_stem('claude_code', 's-run')[8:]}*"):
        p.unlink()
    v = mk()(ReadContext(datetime.now(timezone.utc))).value
    assert [t for t in v["tasks"] if t["kind"] == "unattached"] == [], v["tasks"]
    assert all(t["state"] != "done" for t in v["tasks"] if t["kind"] == "unattached")


def test_same_basename_repos_are_two_groups_with_distinct_ids_and_disambiguated_labels(tmp_path):
    a = _repo(tmp_path / "alpha" / "ledger")
    b = _repo(tmp_path / "beta" / "ledger")
    c = _repo(tmp_path / "alpha" / "solo")
    root = tmp_path / "inst"
    for sid, r in (("a", a), ("b", b), ("c", c)):
        _session(root, sid, str(r), timedelta(minutes=2))
    read = topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: None,
                                hub_label=lambda c: "x", home=str(tmp_path), scan=_stub_scan([]))
    v = read(ReadContext(datetime.now(timezone.utc))).value
    assert len(v["groups"]) == 3 and len({g["id"] for g in v["groups"]}) == 3
    labels = sorted(g["label"] for g in v["groups"])
    assert labels == ["ledger (alpha)", "ledger (beta)", "solo"], labels
    assert all(g["repo"] in ("ledger", "solo") for g in v["groups"])
    assert str(tmp_path) not in json.dumps(v)                       # the id is a hash of the gitdir, never its path
    by_node = {n["id"]: n["group"] for n in v["nodes"]}
    assert by_node[_stem("claude_code", "a")] != by_node[_stem("claude_code", "b")]
    assert _panel(v)["status"] == "ok"


def test_a_session_with_no_log_has_no_rate_not_a_zero(tmp_path, monkeypatch):
    ck, _root, _src, _cdir, _work = _rig(tmp_path, monkeypatch)
    nodes = {n["id"]: n for n in ck.panel("topology", credential_class="none")["value"]["nodes"]}
    assert nodes[_stem("claude_code", "s-wait")]["rate"] is None          # no log was written for it
    assert nodes[_stem("claude_code", "s-run")]["rate"] == pytest.approx(0.6)
    v = _good()
    v["nodes"][0]["rate"] = None
    assert _panel(v)["status"] == "ok"


# --- F12a fix 3 (L3 r1: C1-C4) ----------------------------------------------------------------------------------

def _filemode_worktree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """A linked worktree on a repository whose common config says ``core.filemode=false``, a tracked file in it."""
    repo = _repo(tmp_path / "work" / "main")
    _commit(repo, "one", {"a.txt": "a\n"})
    _git(repo, "config", "core.filemode", "false", check=True)
    wt = tmp_path / "work" / "main-seat"
    _git(repo, "worktree", "add", "-q", "-b", "seat", str(wt), check=True)
    common, gitdir, top = act.find_worktree(str(wt), str(tmp_path))[0]
    return repo, common, gitdir, top


def test_a_linked_worktrees_config_worktree_core_settings_shape_the_count(tmp_path):
    repo, common, gitdir, top = _filemode_worktree(tmp_path)
    (gitdir / "config.worktree").write_text("[core]\n\tfilemode = true\n")
    (top / "a.txt").chmod(0o755)
    # without extensions.worktreeConfig git ignores config.worktree, and so does the read
    assert act.git_worktree_state(common, gitdir, top, 3.0)["dirty"] == 0
    assert _git(top, "status", "--porcelain").stdout == ""
    _git(repo, "config", "extensions.worktreeConfig", "true", check=True)
    assert _git(top, "status", "--porcelain").stdout.strip() == "M a.txt"  # control: git now counts the mode change
    assert act.git_worktree_state(common, gitdir, top, 3.0)["dirty"] == 1
    # the main worktree's own config.worktree lives in the common git dir
    (repo / "a.txt").chmod(0o755)
    mcommon, mgitdir, mtop = act.find_worktree(str(repo), str(tmp_path))[0]
    assert act.git_worktree_state(mcommon, mgitdir, mtop, 3.0)["dirty"] == 0
    (common / "config.worktree").write_text("[core]\n\tfilemode = true\n")
    assert _git(repo, "status", "--porcelain").stdout.strip() == "M a.txt"
    assert act.git_worktree_state(mcommon, mgitdir, mtop, 3.0)["dirty"] == 1


def test_only_allowlisted_shape_checked_keys_cross_from_config_worktree(tmp_path):
    repo, common, gitdir, _top = _filemode_worktree(tmp_path)
    _git(repo, "config", "extensions.worktreeConfig", "true", check=True)
    (gitdir / "config.worktree").write_text(
        "[core]\n\tfilemode = true\n\tfsmonitor = /tmp/evil\n\thooksPath = /tmp/evil\n\tautocrlf = $(evil)\n"
        "\teol = crlf\n[filter \"x\"]\n\tclean = /tmp/evil\n[extensions]\n\tobjectformat = sha256\n"
        "[include]\n\tpath = /tmp/evil.cfg\n")
    cfg = act._private_config(common, gitdir, act._budget(3.0))
    assert "\tfilemode = true" in cfg and "\teol = crlf" in cfg
    for bad in ("evil", "fsmonitor", "hookspath", "filter", "include", "sha256", "worktreeconfig", "autocrlf"):
        assert bad not in cfg.lower(), (bad, cfg)
    assert cfg.count("filemode") == 1, cfg                               # merged over common, never doubled


def test_the_status_read_passes_no_optional_locks_itself(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r")
    common, gitdir, top = act.find_worktree(str(repo), str(tmp_path))[0]
    seen: list[tuple[list[str], dict[str, str] | None]] = []
    real = act._run

    def spy(argv, timeout, extra_env=None):
        seen.append((argv, extra_env))
        return real(argv, timeout, extra_env)

    monkeypatch.setattr(act, "_run", spy)
    act.git_worktree_state(common, gitdir, top, 3.0)
    status = [a for a, _e in seen if "status" in a]
    assert len(status) == 1 and "--no-optional-locks" in status[0], status


def test_repo_labels_that_still_clash_after_the_parent_get_a_stable_fragment_and_keep_their_suffix(tmp_path):
    long = "l" * 46
    repos = {"a": _repo(tmp_path / "one" / "x" / "ledger"), "b": _repo(tmp_path / "two" / "x" / "ledger"),
             "c": _repo(tmp_path / "alpha" / long), "d": _repo(tmp_path / "beta" / long)}
    root = tmp_path / "inst"
    for sid, r in repos.items():
        _session(root, sid, str(r), timedelta(minutes=2))
    read = topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: None,
                                hub_label=lambda c: "x", home=str(tmp_path), scan=_stub_scan([]))
    v = read(ReadContext(datetime.now(timezone.utc))).value
    assert _panel(v)["status"] == "ok", _panel(v)
    labels = [g["label"] for g in v["groups"]]
    assert len(set(labels)) == 4, labels
    assert all(len(lb) <= topo.LABEL_MAX for lb in labels), labels
    assert sum(lb.endswith("(alpha)") for lb in labels) == 1 and sum(lb.endswith("(beta)") for lb in labels) == 1
    assert sum(lb.startswith("ledger (x · ") for lb in labels) == 2, labels
    again = read(ReadContext(datetime.now(timezone.utc))).value
    assert [g["label"] for g in again["groups"]] == labels                # stable across reads


def test_non_repo_directories_with_one_basename_are_two_groups(tmp_path):
    a, b = tmp_path / "alpha" / "work", tmp_path / "beta" / "work"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    root = tmp_path / "inst"
    _session(root, "a", str(a), timedelta(minutes=2))
    _session(root, "b", str(b), timedelta(minutes=2))
    v = topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: None, hub_label=lambda c: "x",
                             home=str(tmp_path), scan=_stub_scan([]))(ReadContext(datetime.now(timezone.utc))).value
    assert _panel(v)["status"] == "ok", _panel(v)
    assert len(v["groups"]) == 2 and [g["label"] for g in v["groups"]] == ["work", "work"], v["groups"]
    assert all(g["id"].startswith("dir:") and "work" not in g["id"] for g in v["groups"]), v["groups"]
    by_node = {n["id"]: n["group"] for n in v["nodes"]}
    assert by_node[_stem("claude_code", "a")] != by_node[_stem("claude_code", "b")]


def test_a_session_stem_not_of_the_registry_shape_never_becomes_an_id(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "work" / "r")
    root = tmp_path / "inst"
    _session(root, "s", str(repo), timedelta(minutes=2))
    real = topo._records

    def forged(install_root, now):
        recs = real(install_root, now)
        return [*recs, {**recs[0], "_stem": "a​b‮c", "session_id": "other"}]

    monkeypatch.setattr(topo, "_records", forged)
    v = topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: None, hub_label=lambda c: "x",
                             home=str(tmp_path), scan=_stub_scan([]))(ReadContext(datetime.now(timezone.utc))).value
    assert _panel(v)["status"] in ("ok", "partial"), _panel(v)
    assert [n["id"] for n in v["nodes"]] == [_stem("claude_code", "s")]
    assert "sessions: a record of an unknown name, not drawn" in v["errors"]
    assert [r["node"] for r in v["lost_if_closed"]] == [_stem("claude_code", "s")]


def test_a_transcript_cost_is_accepted_as_tokens_without_a_price():
    v = copy.deepcopy(_good())
    v["tasks"][1]["cost"] = {"usd": None, "tokens": 705544, "source": "transcript"}
    p = _panel(v)
    assert p["status"] == "ok", p
    assert p["value"]["tasks"][1]["cost"] == {"usd": None, "tokens": 705544, "source": "transcript"}
