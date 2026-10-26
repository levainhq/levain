"""K1: the Levain-native activity feed, the Memory and Levain health tiles, and the opt-out (K1_DESIGN §1-3).

The hostile-repository test is the one this lane exists for: a repository an agent wrote, its ``.git/config``
planting programs, read by the feed. It first proves a planted program really runs under a plain git (the
control), so the second half cannot pass by planting something git would never have run."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
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
from tests._h4 import h4_home

KINDS = {"commit", "session", "decision", "memory", "question"}
CRED = {"class": "token", "name": "browser", "device_class": None}
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
    q = ConsentQueue(home / "consent", authority=authority_source(home / "consent", anchor=None), home=h4_home())
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
    assert len(live) == 1 and live[0]["repo"] == "ledgerline" and "end" in live[0]    # the repo's lane, not "sub"
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
        return [], [], len(repos)
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
    events, errors, n_read = act._scan([(Path("/a"), "a"), (Path("/b"), "b")], datetime.now(timezone.utc),
                                       clock=lambda: next(ticks), commits=lambda g, s, t: [1_700_000_000])
    assert len(events) == 1 and n_read == 1 and errors == ["b: skipped, the commit scan passed its 6s deadline"]
    seen: list[float] = []
    act._scan([(Path("/a"), "a")], datetime.now(timezone.utc), clock=lambda: 4.0 if seen else 0.0,
              commits=lambda g, s, t: seen.append(t) or [])
    assert seen == [act.ACTIVITY_GIT_CALL_S]


def test_the_walk_names_what_it_does_not_read(tmp_path, monkeypatch):
    wt = tmp_path / "linked"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /elsewhere\n")
    assert act.find_repo_gitdir(str(wt), str(tmp_path)) == \
        (None, "its .git file names no <repository>/worktrees/<name>, not read")
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
    for k in ("log.showSignature=false", "core.fsmonitor=", "diff.external="):
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


def test_flow_registers_on_top_with_its_opt_out(tmp_path):
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src, exclude={"activity", "memory_glance"})
    _flow_ids(ck)                                                                # raises nothing
    ids = set(ck.panel_ids)
    assert {"sky", "memory", "doctor", "health"} <= ids and not {"activity", "memory_glance"} & ids


def test_r1_activity_yields_the_hero_to_a_downstream_ambient(tmp_path):
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)                                              # no exclude: flow's own call today
    assert ck.manifest(CRED)["regions"]["hero"] == ["activity"]
    _flow_ids(ck)                                                                # flow's sky: no raise
    ids = set(ck.panel_ids)
    assert "sky" in ids and "activity" not in ids
    assert ck.manifest(CRED)["regions"]["hero"] == ["weather", "sky"]
    assert ck.panel("activity", credential_class="none") is None
    # a second hero ambient after sky is still refused: only the kernel's default yields
    with pytest.raises(Exception, match="the hero holds at most one ambient"):
        ck.register(ProviderSpec("sky2", "ambient", "Sky", "feed", lambda ctx: Read(value={}), region="hero"))
    # and the legacy header form flow also uses lands in the hero, not in operate
    ck3 = build_default_cockpit(src)
    ck3.register(ProviderSpec("sky", "ambient", "Sky", "feed", lambda ctx: Read(value={}), region="header"))
    assert ck3.manifest(CRED)["regions"]["hero"] == ["sky"] and "activity" not in ck3.panel_ids


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
    assert labels == ["hooks", "memory store", "cockpit keys"]                # R3: only what doctor checks
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
    # (b) the activity read on that repository: its include refuses it (c-2), named, and nothing runs
    root = tmp_path / "inst"
    _session(root, "hs", str(repo), timedelta(minutes=2))
    read = act.activity_reader(install_root=root, episodic_db=tmp_path / "none.db", consent=lambda: None,
                               questions=lambda: None, home=str(tmp_path))
    v = read(ReadContext(datetime.now(timezone.utc))).value
    with capsys.disabled():
        print(f"[hostile] with its include: errors {[e for e in v['errors'] if e.startswith('hostile')]}; "
              f"sentinels: {sorted(p.name for p in sentinels.iterdir())}")
    assert "hostile: its config includes another config file, not read" in v["errors"]
    assert not list(sentinels.iterdir())
    # (c) the include taken out, every other planted program still there: its commit reads, and no sentinel
    cfg = repo / ".git" / "config"
    cfg.write_text(cfg.read_text().replace(f"[include]\n\tpath = {tmp_path / 'bin' / 'included.cfg'}\n", ""))
    assert "[include]" not in cfg.read_text() and "fsmonitor" in cfg.read_text()
    v = read(ReadContext(datetime.now(timezone.utc), fresh=True)).value
    commits = [e for e in v["events"] if e["kind"] == "commit"]
    after = sorted(p.name for p in sentinels.iterdir())
    with capsys.disabled():
        print(f"[hostile] activity commits: {len(commits)} {[e['repo'] for e in commits]}; errors: "
              f"{[e for e in v['errors'] if not e.startswith(('decisions', 'questions', 'memory'))]}; "
              f"sentinels after the read: {after}")
    assert after == [], f"the activity read ran a planted program: {after}"
    assert [e["repo"] for e in commits] == ["hostile"]


# --- the partial clone (K1 L2-1, L2-2) -------------------------------------------------------------------------

def _promisor_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repository an agent wrote as a partial clone: its promisor remote names a local repository and its own
    ``uploadpack`` program, and a branch points at an object it does not hold, so any walk lazily fetches it."""
    other = _repo(tmp_path / "other")
    repo = _repo(tmp_path / "hostile_pc")
    sentinel = tmp_path / "FIRED_uploadpack"
    up = tmp_path / "up.sh"
    up.write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 1\n")
    up.chmod(0o755)
    for k, v in (("core.repositoryformatversion", "1"), ("extensions.partialClone", "origin"),
                 ("remote.origin.url", str(other)), ("remote.origin.promisor", "true"),
                 ("remote.origin.uploadpack", str(up)), ("remote.origin.partialclonefilter", "blob:none")):
        _git(repo, "config", k, v, check=True)
    (repo / ".git" / "refs" / "heads" / "ghost").write_text("c0ffee" * 6 + "c0ff\n")
    return repo, sentinel


def test_a_partial_clone_runs_no_upload_pack(tmp_path, monkeypatch, capsys):
    repo, sentinel = _promisor_repo(tmp_path)
    # (a) CONTROL: a plain git log, no levain environment, lazily fetches through the planted upload-pack
    _git(repo, "log", "--branches", "--format=%H")
    fired = sentinel.exists()
    with capsys.disabled():
        print(f"\n[promisor] control (plain git log --branches) sentinel: {fired}")
    if not fired:
        pytest.fail("CONTROL FAILED: the planted upload-pack did not run under a plain git; the read proves nothing")
    sentinel.unlink()
    # (b) the feed's read: refused before git runs, and named
    root = tmp_path / "inst"
    _session(root, "pc", str(repo), timedelta(minutes=2))
    read = act.activity_reader(install_root=root, episodic_db=tmp_path / "none.db", consent=lambda: None,
                               questions=lambda: None, home=str(tmp_path))
    v = read(ReadContext(datetime.now(timezone.utc))).value
    with capsys.disabled():
        print(f"[promisor] feed read errors: {[e for e in v['errors'] if e.startswith(('hostile', 'commits'))]}; "
              f"sentinel: {sentinel.exists()}")
    assert not sentinel.exists()
    assert "hostile_pc: a partial clone (extensions.partialclone in config), not read" in v["errors"]
    assert "commits" in v["unreadable"]                     # its one repository unread: not a quiet window
    # (c) the environment alone, with the refusal bypassed: git_env's GIT_NO_LAZY_FETCH stops the fetch
    monkeypatch.setattr(act, "repo_refusal", lambda g, t=0: None)
    with pytest.raises(RuntimeError):
        act.git_commits(repo / ".git", datetime.now(timezone.utc) - timedelta(days=1), 5.0)
    with capsys.disabled():
        print(f"[promisor] env-only read (refusal bypassed) sentinel: {sentinel.exists()}")
    assert not sentinel.exists()


def test_the_refusals_name_partial_clones_and_borrowed_objects(tmp_path):
    def fresh(name: str) -> Path:
        return _repo(tmp_path / name) / ".git"
    assert act.repo_refusal(fresh("plain")) is None
    g = fresh("ext")
    _git(g.parent, "config", "extensions.partialClone", "origin", check=True)
    assert act.repo_refusal(g) == "a partial clone (extensions.partialclone in config), not read"
    g = fresh("prom")
    _git(g.parent, "config", "remote.agentname.promisor", "true", check=True)
    assert act.repo_refusal(g) == "a partial clone (remote.<name>.promisor in config), not read"
    g = fresh("promoff")                                      # any promisor key, whatever its value (L2-2)
    _git(g.parent, "config", "remote.o.promisor", "false", check=True)
    assert act.repo_refusal(g) == "a partial clone (remote.<name>.promisor in config), not read"
    g = fresh("incl")                                         # any include, whatever it holds (c-2)
    (tmp_path / "inc.cfg").write_text("[remote \"o\"]\n\tpromisor = true\n")
    _git(g.parent, "config", "include.path", str(tmp_path / "inc.cfg"), check=True)
    assert act.repo_refusal(g) == "its config includes another config file, not read"
    g = fresh("incif")                                        # true only where git runs with --git-dir
    _git(g.parent, "config", f"includeIf.gitdir:{os.path.realpath(g.parent)}/.path", str(tmp_path / "inc.cfg"),
         check=True)
    seen = subprocess.run(["git", f"--git-dir={g}", "config", "--get", "remote.o.promisor"], cwd="/", text=True,
                          env={**GIT_ISOLATED, "HOME": str(tmp_path)}, capture_output=True, timeout=30)
    assert seen.stdout.strip() == "true", "CONTROL: git under --git-dir does not see the conditional include"
    assert act.repo_refusal(g) == "its config includes another config file, not read"
    g = fresh("pack")
    (g / "objects" / "pack" / "pack-1.promisor").write_text("")
    assert act.repo_refusal(g) == "a partial clone (a .promisor pack), not read"
    g = fresh("alt")
    (g / "objects" / "info").mkdir(exist_ok=True)
    (g / "objects" / "info" / "alternates").write_text("")
    assert act.repo_refusal(g) is None                        # an empty alternates borrows nothing
    (g / "objects" / "info" / "alternates").write_text(str(tmp_path / "plain" / ".git" / "objects") + "\n")
    assert act.repo_refusal(g) == "it borrows objects (objects/info/alternates), not read"
    for msg in [act.repo_refusal(g)]:
        assert str(tmp_path) not in msg


def test_each_git_call_gets_only_the_time_left_in_its_budget(tmp_path, monkeypatch):
    """L2 L1: the refusal's two config reads, then the worktree read's own calls, share one budget: no call is given
    more time than is left of it, so a tree cannot overrun the scan deadline by a call's timeout."""
    g = _repo(tmp_path / "r") / ".git"
    (g / "config.worktree").write_text("[core]\n\tbare = false\n")
    real_run = act._run
    given: list[tuple[float, float]] = []

    def slow(argv: list[str], timeout: float) -> tuple[int, str]:
        given.append((timeout, time.monotonic()))
        time.sleep(0.3)
        return real_run(argv, timeout)

    monkeypatch.setattr(act, "_run", slow)
    t0 = time.monotonic()
    assert act.repo_refusal(g, 0.5) is None
    assert len(given) == 2 and given[0][0] <= 0.5
    assert given[1][0] <= 0.5 - (given[1][1] - t0) + 0.01, given          # the second call: what is left, not 0.5
    given.clear()
    with pytest.raises(subprocess.TimeoutExpired):
        act.git_worktree_state(g, g, g.parent, 0.5)
    assert given and all(t <= 0.5 for t, _ in given)
    assert time.monotonic() - t0 < 0.5 + 0.5 + 1.0                            # both budgets, plus a call's kill grace


# --- linked worktrees (K1 L1-2, L1-3) ---------------------------------------------------------------------------

def test_a_linked_worktree_reads_its_repository_once_under_the_main_name(tmp_path):
    main = _repo(tmp_path / "work" / "mainrepo", ago=timedelta(hours=2))
    wt = tmp_path / "work" / "mainrepo-seat"
    _git(main, "worktree", "add", "-q", "-b", "seat", str(wt), check=True)
    when = {"GIT_AUTHOR_DATE": _stamp(timedelta(minutes=30)), "GIT_COMMITTER_DATE": _stamp(timedelta(minutes=30)),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _git(wt, "commit", "-q", "--allow-empty", "-m", "seat work", env=when, check=True)
    assert act.find_repo_gitdir(str(wt), str(tmp_path)) == (Path(os.path.realpath(main / ".git")), None)
    root = tmp_path / "inst"
    _session(root, "w1", str(wt), timedelta(minutes=3))
    _session(root, "m1", str(main), timedelta(minutes=5))
    read = act.activity_reader(install_root=root, episodic_db=tmp_path / "none.db", consent=lambda: None,
                               questions=lambda: None, home=str(tmp_path))
    v = read(ReadContext(datetime.now(timezone.utc))).value
    commits = [e for e in v["events"] if e["kind"] == "commit"]
    sessions = [e for e in v["events"] if e["kind"] == "session"]
    assert [e["repo"] for e in commits] == ["mainrepo", "mainrepo"]          # main's and the seat's, each once
    assert [e["repo"] for e in sessions] == ["mainrepo", "mainrepo"]         # one lane with its commits
    assert "commits" not in v["unreadable"] and not [e for e in v["errors"] if e.startswith("mainrepo")]


def test_a_forged_gitfile_is_named_and_not_read(tmp_path, monkeypatch):
    main = _repo(tmp_path / "real")
    other = _repo(tmp_path / "otherrepo")

    def forged(name: str, line: str) -> Path:
        d = tmp_path / name
        d.mkdir()
        (d / ".git").write_text(line)
        return d
    outside = forged("outside", "gitdir: /etc\n")
    assert act.find_repo_gitdir(str(outside), str(tmp_path)) == \
        (None, "its .git file names no <repository>/worktrees/<name>, not read")
    foreign = forged("foreign", "gitdir: /private/var/root/.git/worktrees/x\n")
    assert act.find_repo_gitdir(str(foreign), str(tmp_path)) == \
        (None, "its .git file names a worktree that is not a directory you own, not read")
    # worktree-shaped, but its commondir leads to another repository
    fake = main / ".git" / "worktrees" / "x"
    fake.mkdir(parents=True)
    (fake / "commondir").write_text(str(other / ".git") + "\n")
    steer = forged("steer", f"gitdir: {fake}\n")
    assert act.find_repo_gitdir(str(steer), str(tmp_path)) == \
        (None, "its worktree's commondir does not lead back to its repository, not read")
    two = forged("twolines", f"gitdir: {fake}\ngitdir: {fake}\n")
    assert act.find_repo_gitdir(str(two), str(tmp_path)) == (None, "its .git file is not one gitdir line you own, not read")
    rel = forged("relative", "gitdir: ../real/.git/worktrees/x\n")
    assert act.find_repo_gitdir(str(rel), str(tmp_path)) == (None, "its .git file names no absolute worktree path, not read")
    big = forged("big", "gitdir: /" + "a" * 5000 + "\n")
    assert act.find_repo_gitdir(str(big), str(tmp_path))[1] == "its .git file is not one gitdir line you own, not read"
    # a .git that is a symlink to a real worktree gitfile is refused before it is read
    (fake / "commondir").write_text("../..\n")
    good = forged("good", f"gitdir: {fake}\n")
    assert act.find_repo_gitdir(str(good), str(tmp_path)) == (Path(os.path.realpath(main / ".git")), None)
    ln = tmp_path / "linked_gitfile"
    ln.mkdir()
    (ln / ".git").symlink_to(good / ".git")
    assert act.find_repo_gitdir(str(ln), str(tmp_path)) == (None, "its .git is a link, not read")
    # a plain .git directory that names another repository's store
    (tmp_path / "cdir" / ".git").mkdir(parents=True)
    (tmp_path / "cdir" / ".git" / "commondir").write_text("../../real/.git\n")
    assert act.find_repo_gitdir(str(tmp_path / "cdir"), str(tmp_path)) == \
        (None, "its .git names another repository (commondir), not read")
    # a gitfile owned by someone else
    real_uid = os.getuid()
    monkeypatch.setattr(act.os, "getuid", lambda: real_uid + 1)
    assert act.find_repo_gitdir(str(good), str(tmp_path)) == (None, "its .git file is not one gitdir line you own, not read")


def test_the_same_refusal_from_many_sessions_is_one_error(tmp_path):
    root = tmp_path / "inst"
    d = tmp_path / "forged"
    d.mkdir()
    (d / ".git").write_text("gitdir: /etc\n")
    for i in range(3):
        _session(root, f"f{i}", str(d), timedelta(minutes=i + 1))
    read = act.activity_reader(install_root=root, episodic_db=tmp_path / "none.db", consent=lambda: None,
                               questions=lambda: None, home=str(tmp_path))
    v = read(ReadContext(datetime.now(timezone.utc))).value
    assert v["errors"].count("forged: its .git file names no <repository>/worktrees/<name>, not read") == 1
    assert "commits" in v["unreadable"]                       # the only repository named was refused


# --- the bounds (K1 L1-1, L1-5, L1-6, L1-7, L1-8) ---------------------------------------------------------------

def test_every_repository_failing_is_unreadable_not_a_quiet_window(tmp_path):
    root = tmp_path / "inst"
    (tmp_path / "r" / ".git").mkdir(parents=True)
    _session(root, "s", str(tmp_path / "r"), timedelta(minutes=1))

    def failing(repos, since):
        return [], [f"{lbl}: the commit read timed out" for _g, lbl in repos], 0
    read = act.activity_reader(install_root=root, episodic_db=tmp_path / "none.db", consent=lambda: None,
                               questions=lambda: None, home=str(tmp_path), scan=failing)
    v = read(ReadContext(datetime.now(timezone.utc))).value
    assert "commits" in v["unreadable"] and "r: the commit read timed out" in v["errors"]
    # no repository at all is a quiet window, not a blind one
    root2 = tmp_path / "inst2"
    _session(root2, "s", "/zq/nowhere", timedelta(minutes=1))
    read2 = act.activity_reader(install_root=root2, episodic_db=tmp_path / "none.db", consent=lambda: None,
                                questions=lambda: None, home=str(tmp_path), scan=failing)
    assert "commits" not in read2(ReadContext(datetime.now(timezone.utc))).value["unreadable"]


def test_a_child_that_escapes_the_group_cannot_hold_the_read(tmp_path):
    import sys
    import time
    pidfile = tmp_path / "child.pid"
    fake = tmp_path / "fake_git"
    fake.write_text(f"#!{sys.executable}\nimport os, sys, time\n"
                    "if os.fork() == 0:\n    os.setsid()\n"
                    f"    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n    time.sleep(60)\n    os._exit(0)\n"
                    "time.sleep(60)\n")
    fake.chmod(0o755)
    t0 = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            act._run([str(fake)], 0.5)
        took = time.monotonic() - t0
        assert pidfile.exists(), "the fake git's child never started: the test proves nothing"
        assert took < 4.0, took                               # 0.5s, the kill, one more second: never the child's 60
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 9)
            except (OSError, ValueError):
                pass


class _Req:
    def __init__(self, at: str):
        self.received_at, self.agent, self.tool_name, self.cwd = at, "claude_code", "Bash", None


class _Entry:
    def __init__(self, at: str, state: str):
        self.request, self.state = _Req(at), state


class _Listing:
    def __init__(self, *entries):
        self.pending, self.undecided, self.recent = entries, (), ()


def test_waiting_since_before_the_window_sits_at_its_start_with_the_true_time(tmp_path):
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=act.ACTIVITY_WINDOW_H)
    old = _stamp(timedelta(hours=act.ACTIVITY_WINDOW_H + 3))
    got, bad = act._decision_events(_Listing(_Entry(old, "pending"), _Entry(old, "denied"),
                                             _Entry(_stamp(timedelta(minutes=1)), "withdrawn")), start)
    assert bad == 0 and len(got) == 1          # the old denied one is gone, the withdrawn draws nothing (R2)
    ev = got[0][0]
    assert ev["at"] == act._iso(start) and ev["since"] == act._iso(datetime.fromisoformat(old[:-1] + "+00:00"))
    recent = _stamp(timedelta(minutes=5))
    ev2 = act._decision_events(_Listing(_Entry(recent, "pending")), start)[0][0][0]
    assert "since" not in ev2


def test_the_cache_gives_copies_and_the_event_cap_keeps_the_newest(tmp_path):
    root = tmp_path / "inst"
    (tmp_path / "r" / ".git").mkdir(parents=True)
    _session(root, "s", str(tmp_path / "r"), timedelta(minutes=1))
    now = datetime.now(timezone.utc)
    n = act.ACTIVITY_EVENTS_MAX + 5
    base = int((now - timedelta(hours=2)).timestamp())

    def many(repos, since):
        return [{"kind": "commit", "at": act._iso(datetime.fromtimestamp(base + i, timezone.utc)), "repo": "r",
                 "agent": None, "outcome": "ok"} for i in range(n)], [], 1
    read = act.activity_reader(install_root=root, episodic_db=tmp_path / "none.db", consent=lambda: None,
                               questions=lambda: None, home=str(tmp_path), scan=many)
    v = read(ReadContext(now)).value
    assert len(v["events"]) == act.ACTIVITY_EVENTS_MAX
    assert (f"6 older events past the cap of {act.ACTIVITY_EVENTS_MAX} not shown (the newest, and every item "
            "still waiting, are)") in v["errors"]
    assert v["events"][-1]["kind"] == "session"                 # the newest survive the cap
    for e in v["events"]:
        e["repo"] = "MUTATED"
    v2 = read(ReadContext(now)).value                           # cached: the same scan, untouched
    assert not any(e["repo"] == "MUTATED" for e in v2["events"])


def test_exclude_reads_a_generator_once(tmp_path):
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src, exclude=(x for x in ["activity", "doctor"]))
    assert not {"activity", "doctor"} & set(ck.panel_ids) and "memory_glance" in ck.panel_ids
    with pytest.raises(ValueError):
        build_default_cockpit(src, exclude=5)


def test_the_doctor_tile_holds_a_fault_and_counts_the_install_root_on_the_hooks_dot(tmp_path, monkeypatch):
    from levain.cockpit import glance
    calls: list[int] = []

    def boom(install):
        calls.append(1)
        raise RuntimeError("x")
    monkeypatch.setattr(glance, "_checks", boom)
    read = glance.doctor_reader(tmp_path)
    ctx = ReadContext(datetime.now(timezone.utc))
    assert isinstance(read(ctx), Fault) and isinstance(read(ctx), Fault)
    assert len(calls) == 1                                      # held for DOCTOR_FAULT_CACHE_S, not re-run per poll
    assert isinstance(read(ReadContext(datetime.now(timezone.utc), fresh=True)), Fault) and len(calls) == 2
    monkeypatch.undo()
    groups = dict(glance._checks(tmp_path / "no-such-install"))
    assert any(r.name == "install root" and not r.ok for r in groups["hooks"])
    assert glance._dot("hooks", groups["hooks"])["status"] == "bad"


# --- K1 L3 r1 -------------------------------------------------------------------------------------------------

def test_c1_a_commondir_planted_in_a_linked_worktrees_repository_is_refused(tmp_path, capsys):
    main = _repo(tmp_path / "work" / "mainrepo")
    wt = tmp_path / "work" / "mainrepo-seat"
    _git(main, "worktree", "add", "-q", "-b", "seat", str(wt), check=True)
    evil, sentinel = _promisor_repo(tmp_path)
    (main / ".git" / "commondir").write_text(str(evil / ".git") + "\n")
    # (a) CONTROL: git on exactly that git directory follows the planted commondir into the partial clone
    _git(tmp_path, f"--git-dir={main / '.git'}", "log", "--branches", "--format=%H")
    fired = sentinel.exists()
    with capsys.disabled():
        print(f"\n[commondir] control (plain git --git-dir=<common> log) sentinel: {fired}")
    if not fired:
        pytest.fail("CONTROL FAILED: git did not follow <common>/commondir; the refusal proves nothing")
    sentinel.unlink()
    # (b) refused at the walk and at the refusal, and the feed names it and runs nothing
    assert act.find_repo_gitdir(str(wt), str(tmp_path)) == \
        (None, "its repository names another repository (commondir), not read")
    assert act.repo_refusal(main / ".git") == "it names another repository (commondir), not read"
    root = tmp_path / "inst"
    _session(root, "w1", str(wt), timedelta(minutes=2))
    read = act.activity_reader(install_root=root, episodic_db=tmp_path / "none.db", consent=lambda: None,
                               questions=lambda: None, home=str(tmp_path))
    v = read(ReadContext(datetime.now(timezone.utc))).value
    with capsys.disabled():
        print(f"[commondir] feed read errors: {[e for e in v['errors'] if e.startswith(('mainrepo', 'commits'))]}; "
              f"sentinel: {sentinel.exists()}")
    assert not sentinel.exists()
    assert "mainrepo-seat: its repository names another repository (commondir), not read" in v["errors"]
    assert "commits" in v["unreadable"]


def test_c3_exclude_of_mixed_types_is_a_value_error(tmp_path):
    _root, src, _ids = _install(tmp_path)
    with pytest.raises(ValueError):
        build_default_cockpit(src, exclude={1, "activty"})


def test_c4_the_event_cap_keeps_what_is_still_waiting(tmp_path):
    root = tmp_path / "inst"
    (tmp_path / "r" / ".git").mkdir(parents=True)
    _session(root, "s", str(tmp_path / "r"), timedelta(minutes=1))
    now = datetime.now(timezone.utc)
    base = int((now - timedelta(hours=2)).timestamp())
    old = _stamp(timedelta(hours=act.ACTIVITY_WINDOW_H + 3))

    def many(repos, since):
        return [{"kind": "commit", "at": act._iso(datetime.fromtimestamp(base + i, timezone.utc)), "repo": "r",
                 "agent": None, "outcome": "ok"} for i in range(act.ACTIVITY_EVENTS_MAX + 5)], [], 1
    read = act.activity_reader(install_root=root, episodic_db=tmp_path / "none.db",
                               consent=lambda: (lambda n: _Listing(_Entry(old, "pending"))), questions=lambda: None,
                               home=str(tmp_path), scan=many)
    v = read(ReadContext(now)).value
    assert len(v["events"]) == act.ACTIVITY_EVENTS_MAX
    waiting = [e for e in v["events"] if e["kind"] == "decision"]
    assert len(waiting) == 1 and waiting[0]["outcome"] == "waiting" and "since" in waiting[0]
    assert v["events"][0] is waiting[0] and v["events"][-1]["kind"] == "session"
    assert (f"7 older events past the cap of {act.ACTIVITY_EVENTS_MAX} not shown (the newest, and every item still "
            "waiting, are)") in v["errors"]


def test_codex_a_doctor_fault_drops_the_cached_ok(tmp_path, monkeypatch):
    from levain.cockpit import glance
    clock = [1000.0]
    import types
    monkeypatch.setattr(glance, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))   # glance only, never the process clock
    calls: list[int] = []
    state = {"fail": False}

    def checks(install):
        calls.append(1)
        if state["fail"]:
            raise RuntimeError("x")
        return [("hooks", [])]
    monkeypatch.setattr(glance, "_checks", checks)
    read = glance.doctor_reader(tmp_path)
    assert isinstance(read(ReadContext(datetime.now(timezone.utc))), Read)
    state["fail"] = True
    clock[0] += 10
    assert isinstance(read(ReadContext(datetime.now(timezone.utc), fresh=True)), Fault)
    clock[0] += glance.DOCTOR_FAULT_CACHE_S + 5            # the fault expired; the ok is younger than its own TTL
    assert isinstance(read(ReadContext(datetime.now(timezone.utc))), Fault)
    assert len(calls) == 3                                  # the checks ran again, never the stale ok served


def test_codex_a_cached_commit_leaves_with_the_window(tmp_path):
    root = tmp_path / "inst"
    (tmp_path / "r" / ".git").mkdir(parents=True)
    now = datetime.now(timezone.utc)
    _session(root, "s", str(tmp_path / "r"), timedelta(minutes=1))
    edge = now - timedelta(hours=act.ACTIVITY_WINDOW_H) + timedelta(seconds=1)
    scans: list[int] = []

    def one(repos, since):
        scans.append(1)
        return [{"kind": "commit", "at": act._iso(edge), "repo": "r", "agent": None, "outcome": "ok"}], [], 1
    read = act.activity_reader(install_root=root, episodic_db=tmp_path / "none.db", consent=lambda: None,
                               questions=lambda: None, home=str(tmp_path), scan=one)
    assert [e for e in read(ReadContext(now)).value["events"] if e["kind"] == "commit"]
    later = read(ReadContext(now + timedelta(seconds=5))).value
    assert len(scans) == 1                                  # still the cached scan
    assert not [e for e in later["events"] if e["kind"] == "commit"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_engine_validated_activity_value_draws_as_levain_lanes_in_the_sky(tmp_path, monkeypatch):
    """K1 x K3 integration (L3 K3 r1, codex-HIGH-1): the value the ENGINE serves for `activity` (after _v_ambient),
    handed to sky.js exactly as the page hands it, takes the Levain lanes and counts both waiting items."""
    ck, _root, _src = _fixture(tmp_path, monkeypatch)
    p = ck.panel("activity", credential_class="none")
    assert p["status"] == "ok", p
    sky = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "sky.js"
    js = ("const s=require(process.argv[1]);const v=JSON.parse(require('fs').readFileSync(0,'utf8'));"
          "const ev=s.normalise(v);const m=s.buildModel(ev,{windowHours:14,topRows:5,pinnedRows:[],single:''},Date.now());"
          "console.log(JSON.stringify({lev:m.lev,rows:m.rows.map(r=>r.id),counts:m.counts}));")
    out = json.loads(subprocess.run([shutil.which("node"), "-e", js, str(sky)], input=json.dumps(p["value"]),
                                    capture_output=True, text=True, check=True).stdout)
    assert out["lev"] is True, out
    assert {"sessions", "decisions", "memory", "questions"} <= set(out["rows"]), out
    assert out["counts"]["waiting"] == 2, out        # the waiting consent and the open question
