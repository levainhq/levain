"""F12a: the ``topology`` kind's contract and the Levain provider behind it (F12_SPEC, with the 14a amendment).

The hostile-repository test is the one the trust boundary needs: "Lost if closed" runs ``git status`` in a working tree
an agent writes, and a status read hashes files, which runs a filter driver's ``clean`` from that repository's config.
It first proves the planted programs really run under a plain git (the control), so the second half cannot pass by
planting something git would never have run."""
from __future__ import annotations

import copy
import json
import os
import subprocess
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
    empty = {**_good(), "groups": [], "nodes": [], "conflicts": [], "tasks": [], "lost_if_closed": []}
    assert _panel(empty)["status"] == "empty"


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
    ("groups.0.id", "repo:ledgerline‮", "invisible or format character"),
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
    ("conflicts.0.nodes", ["s1"], "two distinct node ids"),
    ("conflicts.0.nodes", ["s1", "s1"], "two distinct node ids"),
    ("counts", {}, "tokens_today is required"),
    ("counts.tokens_today", "lots", "non-negative integer"),
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
    ("tasks.0.workers.0", "ghost", "must name a node id"),
    ("lost_if_closed", _DEL, "value.lost_if_closed is required"),
    ("lost_if_closed.0.node", "ghost", "must name a node id"),
    ("lost_if_closed.0.dirty", -2, "non-negative integer"),
    ("lost_if_closed.0.unpushed", _DEL, "null when not read"),
    ("lost_if_closed.0.dirty_names.0", "src/a.py", "basename"),
    ("lost_if_closed.0.branches", "main", "list of strings"),
    ("errors", "nope", "list of strings"),
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
    q = ConsentQueue(cdir, authority=authority_source(cdir, anchor=None))
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
    q.file(agent="claude_code", agent_session_id="s-wait", tool_name="Bash", tool_input={"command": "rm -rf ‮ x"},
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
    assert v["conflicts"] is None and v["counts"] == {"tokens_today": None}
    # the open loop from the install is a task
    assert any(t["kind"] == "spore" and t["label"] == "a loop" for t in v["tasks"])
    assert {g["id"] for g in v["groups"]} >= {"dir:wait", "dir:stall"}
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
    assert ta["label"] == "dispatch · fix-lint" and "Fix the lint" not in json.dumps(v)    # the goal stays off
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
                 f"[core]\n\tfsmonitor = {bin_dir / 'fsmonitor'}\n\thooksPath = {hooks}\n")


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
                    "refusal proves nothing")
    for p in sentinels.iterdir():
        p.unlink()
    # (b) the topology read: the filter driver refuses the tree, named, nothing runs, the counts are unknown
    _touch_tracked(repo)
    (repo / "new.txt").write_text("x\n")
    root = tmp_path / "inst"
    _session(root, "hs", str(repo), timedelta(minutes=2))
    read = topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: None,
                                hub_label=lambda c: "x", home=str(tmp_path))
    v = read(ReadContext(datetime.now(timezone.utc))).value
    row = _lost(v, _stem("claude_code", "hs"))
    with capsys.disabled():
        print(f"[topology-hostile] with its filter driver: errors {[e for e in v['errors'] if e.startswith('hostile')]}; "
              f"lost {row}; sentinels: {sorted(p.name for p in sentinels.iterdir())}")
    assert "hostile: its config defines a filter driver (a program a status read would run), not read" in v["errors"]
    assert row["dirty"] is None and row["unpushed"] is None
    assert not list(sentinels.iterdir())
    # (c) the driver taken out of the config (the attributes still name it), fsmonitor and hooks still planted:
    # the status reads, and no sentinel
    cfg = repo / ".git" / "config"
    text = cfg.read_text()
    start = text.index('[filter "evil"]')
    end = text.index("[core]", start)
    cfg.write_text(text[:start] + text[end:])
    assert "filter" not in cfg.read_text() and "fsmonitor" in cfg.read_text()
    _touch_tracked(repo)
    v = read(ReadContext(datetime.now(timezone.utc), fresh=True)).value
    row = _lost(v, _stem("claude_code", "hs"))
    after = sorted(p.name for p in sentinels.iterdir())
    with capsys.disabled():
        print(f"[topology-hostile] driver removed: lost {row}; errors {[e for e in v['errors'] if e.startswith('hostile')]}; "
              f"sentinels after the read: {after}")
    assert after == [], f"the topology read ran a planted program: {after}"
    assert row["dirty"] == 1 and row["dirty_names"] == ["new.txt"] and row["unpushed"] == 1, row
    # (d) an include is refused unread, as on the commit read
    inc = tmp_path / "bin" / "inc.cfg"
    inc.write_text("[core]\n\tpager = cat\n")
    with open(cfg, "a") as fh:
        fh.write(f"[include]\n\tpath = {inc}\n")
    v = read(ReadContext(datetime.now(timezone.utc), fresh=True)).value
    assert "hostile: its config includes another config file, not read" in v["errors"]
    assert not list(sentinels.iterdir())


def test_a_linked_worktrees_own_config_worktree_is_checked(tmp_path):
    repo = _repo(tmp_path / "work" / "main")
    wt = tmp_path / "work" / "main-seat"
    _git(repo, "worktree", "add", "-q", "-b", "seat", str(wt), check=True)
    found, why = act.find_worktree(str(wt), str(tmp_path))
    assert why is None and found is not None
    common, gitdir, top = found
    assert common == (repo / ".git").resolve() and gitdir.parent.name == "worktrees" and top == wt.resolve()
    (gitdir / "config.worktree").write_text("[filter \"x\"]\n\tclean = /bin/false\n")
    assert act.worktree_refusal(common, gitdir) == \
        "its worktree's config.worktree defines a filter driver (a program a status read would run), not read"


def test_an_open_loop_is_running_only_with_a_worker_never_by_recent_touch():
    from types import SimpleNamespace

    today = datetime.now(timezone.utc).date()
    fresh = SimpleNamespace(id="spore-1", text="touched today", seen=today.isoformat(), created=today.isoformat())
    old = SimpleNamespace(id="spore-2", text="touched long ago", seen="2020-01-01", created="2020-01-01")
    undated = SimpleNamespace(id="spore-3", text="never dated", seen=None, created=None)
    tasks = topo._spore_tasks([fresh, old, undated])
    # a spore links no session, so no worker is ever on it: silence with no record is STUCK, however recent
    assert [t["state"] for t in tasks] == ["stuck", "stuck", "stuck"]
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
    ("a‮b", True, "a⟨U+202E⟩b"),
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
        got = topo._wire("x" * lead + "‮" * 4 + "tail")
        assert len(got) <= topo.LABEL_MAX and _marker_whole(got.removesuffix("…")), (lead, got)
    assert topo._wire("x" * 200).endswith("…") and len(topo._wire("x" * 200)) == topo.LABEL_MAX


def _stub_scan(names: list[str], branch: str | None = "main"):
    def scan(trees):
        return ({str(top): {"dirty": len(names), "dirty_names": list(names), "branch": branch, "unpushed": 1}
                 for (_c, _g, top), _label in trees}, [])
    return scan


@pytest.mark.parametrize("names", [["a\\b.txt"], [" "], ["ㅤ"], ["a‮txt.exe"], ["x" * 60], ["a/b"]])
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
