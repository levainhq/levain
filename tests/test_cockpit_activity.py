"""K1: the Levain-native activity feed, the Memory and Levain health tiles, and the opt-out (K1_DESIGN §1-3).

The hostile-repository test is the one this lane exists for: a repository an agent wrote, its ``.git/config``
planting programs, read by the feed. It first proves a planted program really runs under a plain git (the
control), so the second half cannot pass by planting something git would never have run."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit import ProviderSpec, Read
from levain.cockpit import activity as act
from levain.cockpit.engine import ReadContext
from levain.cockpit.providers import OPTIONAL_NATIVE, build_default_cockpit
from levain.cockpit.results import Fault
from levain.consent.listing import read_listing
from levain.consent.queue import ConsentQueue, authority_source
from levain.consent.questions import QuestionStore
from levain.firing import ws_git
from tests.test_cockpit_k2a import _install

KINDS = {"commit", "session", "decision", "memory", "question"}
GIT_ISOLATED = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0"}


def _stamp(ago: timedelta) -> str:
    return (datetime.now(timezone.utc) - ago).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session(root: Path, sid: str, cwd: str, ago: timedelta, agent: str = "claude_code") -> None:
    d = root / ".levain" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"v": 1, "agent": agent, "session_id": sid, "cwd": cwd, "first_seen": _stamp(ago + timedelta(hours=1)),
           "last_seen": _stamp(ago), "last_event": "UserPromptSubmit", "context_delivered": True}
    (d / (hashlib.sha1(f"{agent}:{sid}".encode()).hexdigest()[:24] + ".json")).write_text(json.dumps(rec))


def _git(repo: Path, *args: str, env: dict[str, str] | None = None, **kw) -> subprocess.CompletedProcess:
    e = {**GIT_ISOLATED, "HOME": str(repo.parent), **(env or {})}
    if "input" not in kw:
        kw["stdin"] = subprocess.DEVNULL      # a planted program that reads stdin must never wait on the test's
    return subprocess.run(["git", *args], cwd=repo, env=e, capture_output=True, text=True, timeout=30, **kw)


def _repo(path: Path, ago: timedelta = timedelta(hours=1)) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main", check=True)
    when = {"GIT_AUTHOR_DATE": _stamp(ago), "GIT_COMMITTER_DATE": _stamp(ago), "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _git(path, "commit", "-q", "--allow-empty", "-m", "one", env=when, check=True)
    return path


def _fixture(tmp_path: Path, monkeypatch):
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    q = ConsentQueue(home / "consent", authority=authority_source(home / "consent", anchor=None))
    q.file(agent="claude_code", agent_session_id="s1", tool_name="Bash", tool_input={"command": "rm -rf ‮ x"},
           cwd="/w/levain-work", hold=timedelta(minutes=5), event_id="e1")
    qs = QuestionStore(home / "consent")
    qs.file(agent="codex", agent_session_id="s2", what="SECRET-WORDS ship it?", options=[("a", "A"), ("b", "B")],
            rec="a", blocks="x", cwd="/w/codex-work")
    root, src, _ids = _install(tmp_path)
    repo = _repo(tmp_path / "work" / "ledgerline")
    (repo / "sub").mkdir()
    _session(root, "s1", str(repo / "sub"), timedelta(minutes=3))
    _session(root, "s9", "/zq/nowhere", timedelta(hours=2), agent="codex")
    ck = build_default_cockpit(src)
    ck.attach_consent_rows(lambda now: read_listing(now=now))
    ck.attach_question_rows(qs)
    return ck, root, src


def test_contract_shape_every_kind_sorted_and_no_agent_body_on_the_wire(tmp_path, monkeypatch):
    ck, _root, _src = _fixture(tmp_path, monkeypatch)
    p = ck.panel("activity", credential_class="none")
    assert p["status"] == "ok", p
    v = p["value"]
    assert set(v) >= {"events", "legend", "window", "errors", "unreadable"} and v["unreadable"] == []
    assert isinstance(v["legend"], str) and v["window"]["hours"] == 14
    ev = v["events"]
    assert {e["kind"] for e in ev} == KINDS, ev
    for e in ev:
        assert set(e) >= {"kind", "at", "repo", "agent", "outcome"}
        assert datetime.fromisoformat(e["at"]).tzinfo is not None
    assert [datetime.fromisoformat(e["at"]) for e in ev] == sorted(datetime.fromisoformat(e["at"]) for e in ev)
    by = {e["kind"]: e for e in ev}
    assert by["commit"] == {"kind": "commit", "at": by["commit"]["at"], "repo": "ledgerline", "agent": None, "outcome": "ok"}
    live = [e for e in ev if e["kind"] == "session" and e["outcome"] == "live"]
    assert len(live) == 1 and live[0]["repo"] == "sub" and "end" in live[0]
    assert by["decision"]["outcome"] == "waiting" and by["decision"]["label"] == "Bash"
    assert by["question"]["outcome"] == "open" and by["question"]["label"].startswith("question #")
    wire = json.dumps(v)
    assert "SECRET-WORDS" not in wire and "rm -rf" not in wire        # agent-written bodies stay token-gated
    assert "/zq/nowhere: " not in wire and str(tmp_path) not in wire    # no absolute path reaches the wire


def test_each_source_unreadable_is_named_and_all_unreadable_is_a_fault(tmp_path, monkeypatch):
    ck, _root, src = _fixture(tmp_path, monkeypatch)

    def boom(now):
        raise OSError("no")
    ck.attach_consent_rows(boom)
    v = ck.panel("activity", credential_class="none")["value"]
    assert v["unreadable"] == ["decisions"] and any(e.startswith("decisions: unreadable") for e in v["errors"])
    read = act.activity_reader(install_root=None, episodic_db=tmp_path / "none.db", consent=lambda: None,
                               questions=lambda: None)
    got = read(ReadContext(datetime.now(timezone.utc)))
    assert isinstance(got, Fault) and got.message.startswith("activity is blind: ")
    for name in ("sessions", "commits", "decisions", "questions", "memory"):
        assert f"{name}: " in got.message
    # one source left readable is no Fault: an empty window with sources read is an empty list
    read2 = act.activity_reader(install_root=None, episodic_db=src.anneal.episodic_db, consent=lambda: None,
                                questions=lambda: None)
    got2 = read2(ReadContext(datetime.now(timezone.utc)))
    assert isinstance(got2, Read) and set(got2.value["unreadable"]) == {"sessions", "commits", "decisions", "questions"}


def test_the_repo_cap_the_deadline_and_the_cache(tmp_path):
    root = tmp_path / "inst"
    for i in range(act.ACTIVITY_REPOS_MAX + 2):
        (tmp_path / f"r{i}" / ".git").mkdir(parents=True)
        _session(root, f"s{i}", str(tmp_path / f"r{i}"), timedelta(minutes=i + 1))
    calls: list[list[str]] = []

    def scan(repos, since):
        calls.append([lbl for _g, lbl in repos])
        return [], []
    read = act.activity_reader(install_root=root, episodic_db=tmp_path / "none.db", consent=lambda: None,
                               questions=lambda: None, home=str(tmp_path), scan=scan)
    v = read(ReadContext(datetime.now(timezone.utc))).value
    assert calls == [[f"r{i}" for i in range(act.ACTIVITY_REPOS_MAX)]]          # the most recently worked
    assert "2 repositories past the cap of 12 not read (the most recently worked are)" in v["errors"]
    read(ReadContext(datetime.now(timezone.utc)))
    assert len(calls) == 1                                                       # cached
    read(ReadContext(datetime.now(timezone.utc), fresh=True))
    assert len(calls) == 2                                                       # a write's fresh read rescans
    # the deadline: a clock past it after the first repository names every later one
    ticks = iter([0.0, 0.0, 7.0, 7.0])
    events, errors = act._scan([(Path("/a"), "a"), (Path("/b"), "b")], datetime.now(timezone.utc),
                               clock=lambda: next(ticks), commits=lambda g, s, t: [1_700_000_000])
    assert len(events) == 1 and errors == ["b: skipped, the commit scan passed its 6s deadline"]
    seen: list[float] = []
    act._scan([(Path("/a"), "a")], datetime.now(timezone.utc), clock=lambda: 4.0 if seen else 0.0,
              commits=lambda g, s, t: seen.append(t) or [])
    assert seen == [act.ACTIVITY_GIT_CALL_S]


def test_the_walk_names_what_it_does_not_read(tmp_path, monkeypatch):
    wt = tmp_path / "linked"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /elsewhere\n")
    assert act.find_repo_gitdir(str(wt), str(tmp_path)) == (None, "linked worktree, not read")
    ln = tmp_path / "linky"
    ln.mkdir()
    (ln / ".git").symlink_to(tmp_path)
    assert act.find_repo_gitdir(str(ln), str(tmp_path)) == (None, "its .git is a link, not read")
    (tmp_path / "home_repo" / ".git").mkdir(parents=True)
    (tmp_path / "home_repo" / "a" / "b").mkdir(parents=True)
    assert act.find_repo_gitdir(str(tmp_path / "home_repo" / "a" / "b"), str(tmp_path)) == \
        (Path(os.path.realpath(tmp_path / "home_repo" / ".git")), None)
    (tmp_path / ".git").mkdir()
    assert act.find_repo_gitdir(str(tmp_path / "plain"), str(tmp_path)) == (None, None)   # home itself is not read
    assert act.find_repo_gitdir("relative/path", str(tmp_path)) == (None, "not an absolute path")
    real = os.getuid()
    monkeypatch.setattr(act.os, "getuid", lambda: real + 1)
    assert act.find_repo_gitdir(str(tmp_path / "home_repo"), str(tmp_path)) == (None, "not owned by you, not read")


def test_the_override_tuple_is_a_superset_of_ws_gits():
    pairs = lambda t: {t[i + 1] for i in range(0, len(t), 2) if t[i] == "-c"}    # noqa: E731
    assert len(act.NEUTRALISE) % 2 == 0 and all(act.NEUTRALISE[i] == "-c" for i in range(0, len(act.NEUTRALISE), 2))
    assert pairs(ws_git._NEUTRALISE) <= pairs(act.NEUTRALISE), pairs(ws_git._NEUTRALISE) - pairs(act.NEUTRALISE)
    for k in ("log.showSignature=false", "core.fsmonitor=", "diff.external=", "core.untrackedCache=false"):
        assert k in pairs(act.NEUTRALISE)


def _flow_ids(ck) -> None:
    """flow's Bridge panels, by id, kind and region, as scripts/flow_cockpit.py registers them."""
    r = lambda ctx: Read(value={})                                               # noqa: E731
    ck.register(ProviderSpec("brief", "brief", "Brief", "gauge", r, region="brief", rank=-2))
    ck.register(ProviderSpec("weather", "reading", "Weather", "gauge", r, region="hero", rank=1, optional=True))
    ck.register(ProviderSpec("sky", "ambient", "Sky", "feed", r, region="hero", rank=2))
    ck.register(ProviderSpec("inbox", "list", "Inbox", "feed", r, region="operate", rank=22, order="time.desc",
                             version_fields=("*",)))
    ck.register(ProviderSpec("stream", "log", "Stream", "feed", r, region="operate", rank=26))
    ck.register(ProviderSpec("ops", "status-dots", "Ops", "gauge", r, region="widgets", rank=22))
    ck.register(ProviderSpec("roster", "roster", "Fleet", "gauge", r, region="operate", rank=24))
    ck.register(ProviderSpec("memory", "metric-grid", "Memory", "gauge", r, region="widgets", rank=25))


def test_flow_registers_on_top_with_its_opt_out_and_the_hero_slot_is_named_without_it(tmp_path):
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src, exclude={"activity", "memory_glance"})
    _flow_ids(ck)                                                                # raises nothing
    ids = set(ck.panel_ids)
    assert {"sky", "memory", "doctor", "health"} <= ids and not {"activity", "memory_glance"} & ids
    # no id collides, but the hero holds one ambient: a downstream with its own sky must exclude activity
    ck2 = build_default_cockpit(src)
    with pytest.raises(Exception, match="the hero holds at most one ambient"):
        ck2.register(ProviderSpec("sky", "ambient", "Sky", "feed", lambda ctx: Read(value={}), region="hero"))


def test_exclude_leaves_out_and_refuses_anything_else(tmp_path):
    _root, src, _ids = _install(tmp_path)
    full = build_default_cockpit(src)
    for pid in OPTIONAL_NATIVE | {"health"}:
        assert full.panel(pid, credential_class="none")["status"] == "ok", pid
    none = build_default_cockpit(src, exclude=OPTIONAL_NATIVE)
    assert not OPTIONAL_NATIVE & set(none.panel_ids) and OPTIONAL_NATIVE <= set(full.panel_ids)
    for pid in OPTIONAL_NATIVE:
        assert none.panel(pid, credential_class="none") is None
    assert none.panel("health", credential_class="none")["status"] == "ok"
    for bad in ({"health"}, {"activty"}, {"tray"}, "activity"):
        with pytest.raises(ValueError):
            build_default_cockpit(src, exclude=bad)


def test_the_glance_tiles_serve_from_the_install(tmp_path):
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    m = ck.panel("memory_glance", credential_class="none")["value"]
    nums = {n["label"]: n["value"] for n in m["numbers"]}
    assert nums["open spores"] == 3 and nums["episodes"] >= 0 and m["top"] == "a tray seed"
    d = ck.panel("doctor", credential_class="none")["value"]["metrics"]
    labels = [x["label"] for x in d]
    assert labels[:3] == ["hooks", "memory store", "cockpit keys"]
    for name in ("serve", "phone", "push", "limits"):                          # doctor has no check: never a green dot
        row = next(x for x in d if x["label"] == name)
        assert row["status"] == "unknown"
    assert str(tmp_path) not in json.dumps(d)


# --- the hostile repository (K1_DESIGN §2.6) -----------------------------------------------------------------

def _plant(repo: Path, bin_dir: Path, sentinels: Path) -> dict[str, Path]:
    """Planted programs, each touching its own sentinel; a commit carrying a (fake) signature, so a log that
    shows signatures runs gpg.program; and an include pulling in a second gpg.program."""
    bin_dir.mkdir()
    sentinels.mkdir()
    progs = {}
    for name in ("fsmonitor", "pager", "gpg", "gpg_included", "diff_external", "hook"):
        p = bin_dir / name
        p.write_text(f"#!/bin/sh\ntouch {sentinels / name}\nexit 1\n")
        p.chmod(0o755)
        progs[name] = p
    hooks = bin_dir / "hooks"
    hooks.mkdir()
    for h in ("post-checkout", "pre-commit", "post-merge", "reference-transaction", "post-index-change"):
        (hooks / h).write_text(f"#!/bin/sh\ntouch {sentinels / 'hook'}\n")
        (hooks / h).chmod(0o755)
    tree = _git(repo, "write-tree", check=True).stdout.strip()
    ts = int((datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp())
    body = (f"tree {tree}\nauthor t <t@t> {ts} +0000\ncommitter t <t@t> {ts} +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEEAAAA\n -----END PGP SIGNATURE-----\n\nsigned\n")
    sha = _git(repo, "hash-object", "-t", "commit", "-w", "--stdin", input=body, check=True).stdout.strip()
    _git(repo, "update-ref", "refs/heads/main", sha, check=True)
    included = bin_dir / "included.cfg"
    included.write_text(f"[gpg]\n\tprogram = {progs['gpg_included']}\n")
    with open(repo / ".git" / "config", "a") as fh:
        fh.write(f"[core]\n\tfsmonitor = {progs['fsmonitor']}\n\tpager = {progs['pager']}\n\thooksPath = {hooks}\n"
                 f"[log]\n\tshowSignature = true\n[gpg]\n\tprogram = {progs['gpg']}\n"
                 f"[diff]\n\texternal = {progs['diff_external']}\n[include]\n\tpath = {included}\n")
    return progs


def test_a_hostile_git_config_runs_nothing_and_the_commit_still_reads(tmp_path, capsys):
    repo = tmp_path / "hostile"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", check=True)
    sentinels = tmp_path / "sentinels"
    _plant(repo, tmp_path / "bin", sentinels)
    # (a) CONTROL: a plain git, as anyone would run it in that repository, with no neutralisation
    _git(repo, "log", "--show-signature", "-1")
    _git(repo, "status")
    _git(repo, "log", "-p", "-1")
    fired = sorted(p.name for p in sentinels.iterdir())
    with capsys.disabled():
        print(f"\n[hostile] control fired: {fired}")
    if not fired:
        pytest.fail("CONTROL FAILED: no planted vector fired under a plain git, so the neutralised read proves nothing")
    for p in sentinels.iterdir():
        p.unlink()
    # (b) the activity read on that repository: its commit, and no sentinel
    root = tmp_path / "inst"
    _session(root, "hs", str(repo), timedelta(minutes=2))
    read = act.activity_reader(install_root=root, episodic_db=tmp_path / "none.db", consent=lambda: None,
                               questions=lambda: None, home=str(tmp_path))
    v = read(ReadContext(datetime.now(timezone.utc))).value
    commits = [e for e in v["events"] if e["kind"] == "commit"]
    after = sorted(p.name for p in sentinels.iterdir())
    with capsys.disabled():
        print(f"[hostile] activity commits: {len(commits)} {[e['repo'] for e in commits]}; errors: "
              f"{[e for e in v['errors'] if not e.startswith(('decisions', 'questions', 'memory'))]}; "
              f"sentinels after the read: {after}")
    assert after == [], f"the activity read ran a planted program: {after}"
    assert [e["repo"] for e in commits] == ["hostile"]
