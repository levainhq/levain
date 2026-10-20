"""levain.consent.push: the effect half of the push grant. Every execute case here was first RUN by hand
(the lane's run report). Local bare remotes only (file:// under tmp_path); HOME is
a scratch directory, so no test reads the operator's git config."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from levain.consent import push


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for k in list(os.environ):
        if k.startswith("GIT_"):
            monkeypatch.delenv(k)
    w = _World(root)
    w.setup()
    return w


class _World:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.ws = root / "ws"
        self.proj = self.ws / "proj"
        self.remote = root / "remote.git"
        self.evil = root / "evil.git"
        self.marks = root / "marks"
        self.lhome = root / "levain-home"
        self.url = f"file://{self.remote}"
        self.evil_url = f"file://{self.evil}"
        self.grant = {"id": "pg-t", "kind": "push", "workspace": str(self.ws), "remote_url": self.url,
                      "branches": ["claude/*"]}
        self.receipts: list[dict] = []

    def git(self, *args, check=True, **kw):
        r = subprocess.run(["git", *map(str, args)], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=60, **kw)
        if check and r.returncode:
            raise AssertionError(f"git {args}: {r.stderr}")
        return r

    def setup(self):
        for d in (self.ws, self.marks, self.lhome):
            d.mkdir()
        self.git("init", "-q", "--bare", self.remote)
        self.git("init", "-q", "--bare", self.evil)
        self.git("init", "-q", "-b", "main", self.proj)
        self.commit("one")
        self.git("-C", self.proj, "checkout", "-q", "-b", "claude/x")
        self.commit("two")

    def commit(self, msg, *extra):
        self.git("-C", self.proj, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c",
                 "user.name=t", "-c", "user.email=t@x", "commit", "-q", "--allow-empty", *extra, "-m", msg)
        return self.tip()

    def tip(self, ref="HEAD"):
        return self.git("-C", self.proj, "rev-parse", ref).stdout.strip()

    def remote_sha(self, ref, repo=None):
        r = self.git(f"--git-dir={repo or self.remote}", "rev-parse", "--verify", "--quiet", ref, check=False)
        return r.stdout.strip() or None

    def refusal(self, *, path, remote, branch):
        if not path.startswith(str(self.ws) + "/"):
            return f"{path} is outside the workspace"
        if remote != self.grant["remote_url"]:
            return f"no grant covers remote {remote}"
        if not push.branch_matches(self.grant["branches"], branch):
            return f"no grant covers branch {branch}"
        return (self.grant["id"], 5)

    def run(self, branch="claude/x", *, repo=None, remote=None, refusal=None):
        return push.execute(self.grant, repo_path=repo or self.proj, remote=remote or self.url, branch=branch,
                            levain_home=self.lhome, refusal=refusal or self.refusal, receipt=self.receipts.append)

    def marker(self, name):
        p = self.marks / f"bin-{name}"
        p.write_text(f"#!/bin/sh\necho \"$0\" >> {self.marks}/{name}.fired\nexit 0\n")
        p.chmod(0o755)
        return str(p)

    def fired(self):
        return sorted(p.name for p in self.marks.glob("*.fired"))


# --- canonical_remote -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url,canon", [
    ("file:///srv/r.git", "file:///srv/r.git"),
    ("file:///srv/r.git/", "file:///srv/r.git"),
    ("FILE:///srv/R.git", "file:///srv/R.git"),
    ("SSH://git@GitHub.com:22/org/repo.git", "ssh://git@github.com:22/org/repo.git"),
    ("SSH://git@GitHub.com/org/repo.git", "ssh://git@github.com/org/repo.git"),
    ("ssh://host:2222/x", "ssh://host:2222/x"),
    ("git@GitHub.com:org/repo.git", "git@github.com:org/repo.git"),
    ("host:/abs/r.git", "ssh://host/abs/r.git"),
    ("HTTPS://Example.COM:443/a/b.git", "https://example.com/a/b.git"),
    ("https://u@h.example/a", "https://u@h.example/a"),
    ("https://h:8443/a/", "https://h:8443/a"),
])
def test_canonical_remote_accepts(url, canon):
    assert push.canonical_remote(url) == canon
    assert push.canonical_remote(canon) == canon


@pytest.mark.parametrize("url,why", [
    ("ext::x", "transport"), ("fd::3", "transport"), ("ext::sh -c x", "whitespace"),
    ("http://h/x", "scheme"), ("git://h/x", "scheme"), ("relative/path", "relative"), ("/abs/path", "relative"),
    ("file://host/x", "names no host"), ("file://relative", "names no host"), ("https://u:pw@h/x", "password"),
    ("https://h/x?y", "query"), ("https://h/x#y", "fragment"), ("https://h/a%2e", "%-escape"),
    ("https://h/a/../b", "segment"), ("https://h//b", "segment"), ("ssh://-oProxyCommand=x/y", "host"),
    ("ssh://h:0022/x", "port"), ("ssh://h:99999/x", "port"), ("https://h", "no path"), ("h:-x", "begins with '-'"),
    ("[::1]:x", "bracket"), ("ssh://hôst/x", "non-ASCII"), ("", "non-empty"),
])
def test_canonical_remote_refuses_naming_why(url, why):
    with pytest.raises(ValueError, match=why):
        push.canonical_remote(url)


# --- branches ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("rule", ["claude/*", "feature-x", "main", "a/b/*"])
def test_branch_rules_accepted(rule):
    assert push.branch_problem(rule) is None


@pytest.mark.parametrize("rule", ["*", "claude/**", "*/x", "a*/", "a*", "bad..name", "-x", "@{-1}", "HEAD",
                                  "x.lock", "claude/", "", "a b"])
def test_branch_rules_refused(rule):
    assert push.branch_problem(rule)


def test_branch_matches():
    rules = ["claude/*", "feature-x"]
    assert push.branch_matches(rules, "claude/x")
    assert push.branch_matches(rules, "claude/a/b")
    assert push.branch_matches(rules, "feature-x")
    for b in ("claude", "claude/", "claudex/y", "main", "feature-x/y", "claude/*", "claude/..x"):
        assert not push.branch_matches(rules, b), b


# --- execute (RUN cases a-g) ------------------------------------------------------------------------------------

def test_a_granted_push_lands_with_the_right_sha(world):
    tip = world.tip()
    res = world.run()
    assert (res.ok, res.status, res.old_sha, res.new_sha) == (True, "new", push.ZERO_SHA, tip)
    assert world.remote_sha("refs/heads/claude/x") == tip
    nxt = world.commit("three")
    res = world.run()
    assert (res.status, res.old_sha, res.new_sha) == ("fast-forward", tip, nxt)
    assert world.remote_sha("refs/heads/claude/x") == nxt
    assert [r["grant_id"] for r in world.receipts] == ["pg-t", "pg-t"]
    assert world.receipts[0]["grant_seq"] == 5 and world.receipts[0]["repo"] == str(world.proj)
    res = world.run()
    assert (res.ok, res.status) == (True, "up-to-date")


def test_a_refusal_stops_before_any_effect(world):
    with pytest.raises(push.PushRefused, match="no grant covers branch main"):
        world.run("main")
    assert world.remote_sha("refs/heads/main") is None
    with pytest.raises(push.PushRefused, match="is now pg-other"):
        world.run(refusal=lambda **kw: ("pg-other", 9))
    with pytest.raises(push.PushRefused, match="returned no grant"):
        world.run(refusal=lambda **kw: None)
    with pytest.raises(push.PushRefused, match="returned no grant"):
        world.run(refusal=lambda **kw: ("pg-t", True))
    assert world.remote_sha("refs/heads/claude/x") is None
    assert world.receipts == []


def test_refusal_sees_the_canonical_remote_and_realpath(world):
    seen = {}

    def refusal(**kw):
        seen.update(kw)
        return "no"
    with pytest.raises(push.PushRefused):
        world.run(remote=world.url + "/", refusal=refusal)
    assert seen == {"path": str(world.proj), "remote": world.url, "branch": "claude/x"}


def test_bad_request_shape_never_reaches_the_check(world):
    called = []

    def refusal(**kw):
        called.append(kw)
        return ("pg-t", 1)
    for kw in ({"remote": "ext::x"}, {"branch": "claude/*"}, {"branch": "bad..x"}):
        with pytest.raises(push.PushRefused):
            world.run(refusal=refusal, **kw)
    assert called == []


def test_b_non_fast_forward_is_rejected_not_forced(world):
    world.run()
    before = world.remote_sha("refs/heads/claude/x")
    world.commit("two, amended", "--amend")
    res = world.run()
    assert (res.ok, res.status, res.exit) == (False, "rejected", 1)
    assert "non-fast-forward" in res.detail
    assert world.remote_sha("refs/heads/claude/x") == before
    assert world.receipts[-1]["status"] == "rejected"


def _hostile(world, repo, hooks_path=True):
    hp = world.marks / "hookspath"
    hp.mkdir(exist_ok=True)
    for h in ("pre-push", "reference-transaction", "post-update", "pre-receive", "update", "post-receive",
              "push-to-checkout", "fsmonitor-watchman"):
        for d, tag in ((hp, "hookspath"), (Path(repo) / ".git" / "hooks", "githooks")):
            s = d / h
            s.write_text(f"#!/bin/sh\necho x >> {world.marks}/{tag}-{h}.fired\nexit 0\n")
            s.chmod(0o755)
    cfg = {"core.fsmonitor": world.marker("fsmonitor"), "uploadpack.packObjectsHook": world.marker("pack"),
           "core.sshCommand": world.marker("ssh"), "remote.origin.url": world.url,
           "remote.origin.receivepack": world.marker("receivepack"),
           "remote.origin.uploadpack": world.marker("uploadpack"), f"url.{world.evil_url}.insteadOf": world.url,
           f"url.{world.evil_url}.pushInsteadOf": world.url, "credential.helper": world.marker("cred"),
           "gpg.program": world.marker("gpg"), "push.gpgSign": "true",
           "core.alternateRefsCommand": world.marker("altrefs")}
    if hooks_path:
        cfg["core.hooksPath"] = str(hp)
    for k, v in cfg.items():
        world.git("-C", repo, "config", k, v)


@pytest.mark.parametrize("hooks_path", [True, False])
def test_c_nothing_the_agent_wrote_in_its_repo_executes(world, hooks_path):
    _hostile(world, world.proj, hooks_path=hooks_path)
    tip = world.commit("hostile")
    res = world.run()
    assert res.ok and world.remote_sha("refs/heads/claude/x") == tip
    assert world.fired() == []
    assert world.git(f"--git-dir={world.evil}", "for-each-ref").stdout == ""
    # the probe can fail: the same config does run programs under plain git
    world.git("-C", world.proj, "status", check=False)
    world.git("-C", world.proj, "-c", "push.gpgSign=false", "push", world.evil_url, "HEAD:refs/heads/ctl",
              check=False)
    fired = world.fired()
    assert "fsmonitor.fired" in fired
    assert ("hookspath-pre-push.fired" if hooks_path else "githooks-pre-push.fired") in fired


def test_c_pack_objects_hook_runs_only_from_protected_config(world):
    """The design's UNVERIFIED claim: upload-pack honours uploadpack.packObjectsHook from protected (global)
    config and never from the repo's. Repo-level: execute runs nothing; global-level: a plain fetch runs it."""
    world.git("-C", world.proj, "config", "uploadpack.packObjectsHook", world.marker("repo-pack"))
    world.run()
    assert world.fired() == []
    (world.home / ".gitconfig").write_text(f"[uploadpack]\n\tpackObjectsHook = {world.marker('global-pack')}\n")
    ctl = world.root / "ctl.git"
    world.git("init", "-q", "--bare", ctl)
    world.git(f"--git-dir={ctl}", "fetch", "-q", world.proj, "refs/heads/claude/x:refs/c", check=False)
    assert world.fired() == ["global-pack.fired"]


def test_d_callers_git_env_has_no_effect(world, monkeypatch):
    tip = world.tip()
    (world.root / "evil-global").write_text(f"[url \"{world.evil_url}\"]\n\tinsteadOf = {world.url}\n")
    env = {"GIT_DIR": str(world.evil), "GIT_WORK_TREE": "/", "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": f"url.{world.evil_url}.pushInsteadOf", "GIT_CONFIG_VALUE_0": world.url,
           "GIT_SSH_COMMAND": world.marker("gitssh"), "GIT_CONFIG_GLOBAL": str(world.root / "evil-global"),
           "GIT_EXEC_PATH": str(world.marks), "GIT_TERMINAL_PROMPT": "1"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert push.git_env()["GIT_TERMINAL_PROMPT"] == "0"
    assert sorted(k for k in push.git_env() if k.startswith("GIT_")) == [
        "GIT_ALLOW_PROTOCOL", "GIT_NO_LAZY_FETCH", "GIT_NO_REPLACE_OBJECTS", "GIT_TERMINAL_PROMPT"]
    assert push.git_env()["GIT_ALLOW_PROTOCOL"] == "" and push.git_env("file")["GIT_ALLOW_PROTOCOL"] == "file"
    res = world.run()
    assert res.ok and world.remote_sha("refs/heads/claude/x") == tip
    assert world.git(f"--git-dir={world.evil}", "for-each-ref", env={"PATH": os.environ["PATH"]}).stdout == ""
    assert world.fired() == []


def test_git_env_keeps_home_and_ssh_auth_sock(world, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    env = push.git_env()
    assert env["HOME"] == str(world.home) and env["SSH_AUTH_SOCK"] == "/tmp/agent.sock"


@pytest.mark.parametrize("key,value", [("insteadOf", "{url}"), ("pushInsteadOf", "{url}"),
                                       ("insteadOf", "file://{root}/"), ("insteadOf", "FILE://{root}/"),
                                       ("insteadOf", ""), ("pushInsteadOf", "")])  # empty: git prefixes every URL
def test_e_an_insteadof_rewrite_of_the_pin_refuses(world, key, value):
    v = value.format(url=world.url, root=world.root)
    (world.home / ".gitconfig").write_text(f"[url \"{world.evil_url}\"]\n\t{key} = {v}\n")
    # an empty prefix also rewrites the local read of the branch, which refuses first: either way, nothing moves
    with pytest.raises(push.PushRefused, match="rewrites the pinned remote" if v else "rewrites the pinned remote|could not read branch"):
        world.run()
    assert world.remote_sha("refs/heads/claude/x") is None
    assert world.git(f"--git-dir={world.evil}", "for-each-ref").stdout == ""
    assert world.receipts == []


def test_e_an_unrelated_insteadof_does_not_refuse(world):
    (world.home / ".gitconfig").write_text("[url \"ssh://git@github.com/\"]\n\tinsteadOf = https://github.com/\n")
    assert world.run().ok


def test_f_symlinks_gitfiles_and_odd_paths_refused(world):
    link = world.ws / "link"
    link.symlink_to(world.proj)
    with pytest.raises(push.PushRefused, match="symlink"):
        world.run(repo=link)
    lroot = world.root / "lroot"
    lroot.symlink_to(world.root)
    with pytest.raises(push.PushRefused, match="symlink"):
        world.run(repo=lroot / "ws" / "proj")
    wt = world.ws / "wt"
    world.git("-C", world.proj, "worktree", "add", "-q", "-b", "claude/wt", wt)
    with pytest.raises(push.PushRefused, match="gitfile"):
        world.run("claude/wt", repo=wt)
    with pytest.raises(push.PushRefused, match="absolute"):
        world.run(repo="ws/proj")
    with pytest.raises(push.PushRefused, match="No such file"):
        world.run(repo=world.ws / "nope")
    gl = world.ws / "gl"
    gl.mkdir()
    (gl / ".git").symlink_to(world.proj / ".git")
    with pytest.raises(push.PushRefused, match="not a directory"):
        world.run(repo=gl)
    assert world.remote_sha("refs/heads/claude/wt") is None


def test_f_missing_branch_and_missing_home(world):
    with pytest.raises(push.PushRefused, match="could not read branch claude/none"):
        world.run("claude/none")
    with pytest.raises(push.PushRefused, match="levain home"):
        push.execute(world.grant, repo_path=world.proj, remote=world.url, branch="claude/x",
                     levain_home=world.root / "absent", refusal=world.refusal, receipt=world.receipts.append)


def test_g_concurrent_executes_on_one_repo_serialise(world):
    world.git("-C", world.proj, "branch", "claude/g1")
    world.git("-C", world.proj, "branch", "claude/g2")
    spans, lock = [], threading.Lock()

    def slow(**kw):
        t0 = time.monotonic()
        time.sleep(0.5)
        with lock:
            spans.append((t0, time.monotonic()))
        return world.refusal(**kw)
    results = []
    threads = [threading.Thread(target=lambda b=b: results.append(world.run(b, refusal=slow)))
               for b in ("claude/g1", "claude/g2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(results) == 2 and all(r.ok for r in results)
    (a0, a1), (b0, b1) = sorted(spans)
    assert b0 >= a1
    assert world.remote_sha("refs/heads/claude/g1") and world.remote_sha("refs/heads/claude/g2")


# --- the mirror, the journal, the argv -------------------------------------------------------------------------

def test_journal_has_intent_before_and_outcome_after(world):
    tip = world.tip()
    world.run()
    key = push.repo_key(str(world.proj))
    lines = [json.loads(x) for x in (world.lhome / "push" / f"{key}.journal.jsonl").read_text().splitlines()]
    assert [x["state"] for x in lines] == ["intent", "done"]
    assert lines[0]["new_sha"] == tip and lines[0]["grant_id"] == "pg-t" and lines[0]["grant_seq"] == 5
    assert lines[1]["status"] == "new"


def test_receipt_failure_leaves_the_journal(world):
    def boom(rec):
        raise OSError("disk full")
    with pytest.raises(push.PushRecordIncomplete, match="receipt: OSError: disk full") as ei:
        push.execute(world.grant, repo_path=world.proj, remote=world.url, branch="claude/x", levain_home=world.lhome,
                     refusal=world.refusal, receipt=boom)
    assert ei.value.result.ok and ei.value.result.status == "new"
    key = push.repo_key(str(world.proj))
    states = [json.loads(x)["state"] for x in (world.lhome / "push" / f"{key}.journal.jsonl").read_text().splitlines()]
    assert states == ["intent", "done"]
    assert world.remote_sha("refs/heads/claude/x") == world.tip()


def test_mirror_is_private_and_its_config_rewritten_every_use(world):
    world.run()
    m = world.lhome / "push" / "mirrors" / "pg-t" / f"{push.repo_key(str(world.proj))}.git"
    for d in (world.lhome / "push", m.parent, m):
        assert (d.stat().st_mode & 0o777) == 0o700
    assert (m / "config").read_text() == push.MIRROR_CONFIG
    assert not (m / "hooks").exists()
    # a tamper between pushes does not survive: a pushInsteadOf and a hook planted in the mirror
    (m / "config").write_text(push.MIRROR_CONFIG + f"[url \"{world.evil_url}\"]\n\tpushInsteadOf = {world.url}\n")
    (m / "hooks").mkdir()
    (m / "hooks" / "reference-transaction").write_text(f"#!/bin/sh\necho x >> {world.marks}/mirror.fired\n")
    (m / "hooks" / "reference-transaction").chmod(0o755)
    nxt = world.commit("three")
    assert world.run().ok
    assert world.remote_sha("refs/heads/claude/x") == nxt
    assert world.fired() == [] and (m / "config").read_text() == push.MIRROR_CONFIG


def test_mirror_keyed_by_repo_so_two_repos_under_one_grant_do_not_share_a_ref(world):
    other = world.ws / "other"
    world.git("init", "-q", "-b", "claude/y", other)
    world.git("-C", other, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "--allow-empty", "-m", "o")
    world.run()
    world.run("claude/y", repo=other)
    mirrors = sorted(p.name for p in (world.lhome / "push" / "mirrors" / "pg-t").iterdir())
    assert len(mirrors) == 2


def test_grant_id_must_be_plain(world):
    world.grant["id"] = "../x"
    with pytest.raises(push.PushRefused, match="plain id"):
        world.run(refusal=lambda **kw: ("../x", 1))


def test_push_argv_is_the_only_shape():
    argv = push.push_argv(Path("/m.git"), "file:///r.git", "claude/x")
    assert argv[-5:] == ["push", "--no-verify", "--porcelain", "file:///r.git", "refs/levain/src:refs/heads/claude/x"]
    flat = " ".join(argv)
    for bad in ("--force", "+refs", "--delete", "--mirror", "--all", "--tags", "--follow-tags"):
        assert bad not in flat
    cs = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert cs == list(push.PUSH_CONFIG)


# --- round 2: the trust boundary (L3 r1) ------------------------------------------------------------------------
# Each test below was run against the round-1 code first; the run report says which ones failed there.

def _secret(world):
    """A repository OUTSIDE the workspace with a commit the agent must not be able to push."""
    sec = world.root / "secret"
    world.git("init", "-q", "-b", "main", sec)
    (sec / "s.txt").write_text("outside the workspace\n")
    world.git("-C", sec, "add", "s.txt")
    world.git("-C", sec, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "secret")
    return sec, world.git("-C", sec, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize("how", ["alternates", "http-alternates", "commondir", "objects-link", "object-file-link"])
def test_d1_objects_from_outside_the_workspace_refuse(world, how):
    sec, s = _secret(world)
    gd = world.proj / ".git"
    objs = gd / "objects"
    if how == "alternates":
        (objs / "info" / "alternates").write_text(f"{sec}/.git/objects\n")
        world.git("-C", world.proj, "update-ref", "refs/heads/claude/s", s)
    elif how == "http-alternates":
        (objs / "info" / "http-alternates").write_text("https://example.invalid/objects\n")
        world.git("-C", world.proj, "branch", "claude/s")
    elif how == "commondir":
        # an outside repository of the user's that happens to carry a matching branch
        world.git("-C", sec, "branch", "claude/s")
        (gd / "commondir").write_text(f"{sec}/.git\n")
    elif how == "objects-link":
        world.git("-C", world.proj, "branch", "claude/s")
        os.rename(objs, gd / "objects.real")
        objs.symlink_to(gd / "objects.real")
    else:
        # every loose object of the secret repo, symlinked into the agent's object store by name
        for f in (sec / ".git" / "objects").glob("??/*"):
            d = objs / f.parent.name
            d.mkdir(exist_ok=True)
            if not (d / f.name).exists():
                (d / f.name).symlink_to(f)
        world.git("-C", world.proj, "update-ref", "refs/heads/claude/s", s)
    with pytest.raises(push.PushRefused, match="object store|commondir"):
        world.run("claude/s")
    assert world.remote_sha("refs/heads/claude/s") is None
    assert world.receipts == []


def _promisor(world):
    srv = world.root / "srv"
    world.git("init", "-q", "-b", "main", srv)
    (srv / "f.txt").write_text("hello\n")
    world.git("-C", srv, "add", "f.txt")
    world.git("-C", srv, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "one")
    world.git("-C", srv, "config", "uploadpack.allowFilter", "true")
    pc = world.ws / "pc"
    world.git("clone", "-q", "--no-checkout", "--filter=blob:none", f"file://{srv}", pc)
    world.git("-C", pc, "branch", "claude/p", "main")
    world.git("-C", pc, "config", "remote.origin.url", "ssh://nowhere.invalid/x")
    world.git("-C", pc, "config", "core.sshCommand", world.marker("lazy-ssh"))
    return pc


def test_d2_a_partial_clone_refuses_and_lazy_fetch_is_off(world):
    pc = _promisor(world)
    assert list((pc / ".git" / "objects" / "pack").glob("*.promisor"))
    with pytest.raises(push.PushRefused, match="partial clone"):
        world.run("claude/p", repo=pc)
    # the config alone refuses too (the .promisor pack file removed)
    for p in (pc / ".git" / "objects" / "pack").glob("*.promisor"):
        p.unlink()
    with pytest.raises(push.PushRefused, match="partial clone"):
        world.run("claude/p", repo=pc)
    assert world.fired() == []
    assert push.git_env()["GIT_NO_LAZY_FETCH"] == "1"


def test_d2_a_callers_lazy_fetch_switch_is_overridden(world, monkeypatch):
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")
    assert push.git_env()["GIT_NO_LAZY_FETCH"] == "1"


def test_toctou_a_repo_swapped_under_the_lock_refuses(world):
    sec, s = _secret(world)
    world.git("-C", sec, "branch", "claude/x")

    def swap(**kw):
        os.rename(world.proj, world.ws / "proj.orig")
        world.proj.symlink_to(sec)
        return world.refusal(**kw)
    with pytest.raises(push.PushRefused, match="changed"):
        world.run(refusal=swap)
    assert world.remote_sha("refs/heads/claude/x") is None


def test_toctou_a_git_dir_swapped_under_the_lock_refuses(world):
    sec, s = _secret(world)
    world.git("-C", sec, "branch", "claude/x")

    def swap(**kw):
        os.rename(world.proj / ".git", world.proj / "git.orig")
        (world.proj / ".git").symlink_to(sec / ".git")
        return world.refusal(**kw)
    with pytest.raises(push.PushRefused, match="changed|not a directory"):
        world.run(refusal=swap)
    assert world.remote_sha("refs/heads/claude/x") is None


def test_path_a_planted_git_on_path_never_runs(world, monkeypatch):
    fake = world.root / "fakebin"
    fake.mkdir()
    g = fake / "git"
    g.write_text(f"#!/bin/sh\necho \"$@\" >> {world.marks}/fake-git.fired\nexit 0\n")
    g.chmod(0o755)
    path = os.environ["PATH"]
    tip = world.tip()
    monkeypatch.setenv("PATH", f"{fake}:{path}")
    res = world.run()
    monkeypatch.setenv("PATH", path)   # the test's own git calls use PATH
    assert res.ok and world.remote_sha("refs/heads/claude/x") == tip
    assert world.fired() == []


def test_path_and_loader_env_are_fixed(world, monkeypatch):
    for k in ("LD_LIBRARY_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH",
              "DYLD_INSERT_LIBRARIES"):
        monkeypatch.setenv(k, str(world.marks))
    monkeypatch.setenv("PATH", f"{world.marks}:/usr/bin")
    env = push.git_env()
    assert env["PATH"] == push.TRUSTED_PATH
    assert not [k for k in env if k.startswith(("LD_", "DYLD_"))]
    assert env["HOME"] == str(world.home)


def test_path_a_git_not_owned_by_root_refuses(world, monkeypatch):
    mine = world.root / "mygit"
    mine.write_text("#!/bin/sh\nexit 0\n")
    mine.chmod(0o755)
    monkeypatch.setattr(push, "GIT_CANDIDATES", (str(mine),))
    with pytest.raises(push.PushRefused, match="root-owned git"):
        world.run()
    monkeypatch.setattr(push, "GIT_CANDIDATES", ("/nonexistent/git",))
    with pytest.raises(push.PushRefused, match="root-owned git"):
        world.run()
    assert world.remote_sha("refs/heads/claude/x") is None


def test_ssh_explicit_port_22_is_kept():
    assert push.canonical_remote("ssh://git@host:22/r.git") == "ssh://git@host:22/r.git"
    assert push.canonical_remote("ssh://git@host/r.git") == "ssh://git@host/r.git"
    assert push.canonical_remote("ssh://git@host:22/r.git") != push.canonical_remote("ssh://git@host/r.git")


@pytest.mark.parametrize("url", ["host:/~bob/x", "u@host:/~/x", "host:/~"])
def test_d6_scp_absolute_tilde_refuses(url):
    with pytest.raises(ValueError, match="~"):
        push.canonical_remote(url)


def test_journal_writes_the_whole_record_through_short_writes(world, monkeypatch):
    real_write = os.write

    def one_byte(fd, data):
        return real_write(fd, bytes(data[:1]))
    (world.lhome / "push").mkdir()
    monkeypatch.setattr(push.os, "write", one_byte)
    push._journal(world.lhome, "k", {"state": "intent", "x": "y" * 50})
    monkeypatch.undo()
    lines = (world.lhome / "push" / "k.journal.jsonl").read_text().splitlines()
    assert [json.loads(x) for x in lines] == [{"state": "intent", "x": "y" * 50}]


def test_journal_a_write_that_makes_no_progress_raises(world, monkeypatch):
    (world.lhome / "push").mkdir()
    monkeypatch.setattr(push.os, "write", lambda fd, data: 0)
    with pytest.raises(OSError):
        push._journal(world.lhome, "k", {"state": "intent"})


def test_journal_fsyncs_the_directory_when_it_creates_the_file(world, monkeypatch):
    (world.lhome / "push").mkdir()
    real_fsync, dirs = os.fsync, []

    def spy(fd):
        import stat as st
        if st.S_ISDIR(os.fstat(fd).st_mode):
            dirs.append(fd)
        return real_fsync(fd)
    monkeypatch.setattr(push.os, "fsync", spy)
    push._journal(world.lhome, "k", {"state": "intent"})
    assert len(dirs) == 1
    push._journal(world.lhome, "k", {"state": "done"})
    assert len(dirs) == 1


def test_an_intent_journal_failure_aborts_before_the_push(world, monkeypatch):
    def fail(home, key, record):
        raise OSError("EIO")
    monkeypatch.setattr(push, "_journal", fail)
    with pytest.raises(OSError):
        world.run()
    assert world.remote_sha("refs/heads/claude/x") is None and world.receipts == []


def test_d4_a_done_journal_failure_still_reaches_the_receipt(world, monkeypatch):
    real = push._journal

    def fail_done(home, key, record):
        if record["state"] == "done":
            raise OSError("disk full")
        return real(home, key, record)
    monkeypatch.setattr(push, "_journal", fail_done)
    tip = world.tip()
    with pytest.raises(push.PushRecordIncomplete, match="push completed") as ei:
        world.run()
    assert world.remote_sha("refs/heads/claude/x") == tip
    assert [r["status"] for r in world.receipts] == ["new"]
    assert ei.value.result.status == "new"


def test_d5_a_push_that_timed_out_reaches_the_receipt_as_unknown(world, monkeypatch):
    real = push._git

    def slow_push(argv, **kw):
        r = real(argv, **kw)
        if "push" in argv:
            raise subprocess.TimeoutExpired(argv, 600)
        return r
    monkeypatch.setattr(push, "_git", slow_push)
    with pytest.raises(push.PushOutcomeUnknown):
        world.run()
    assert [r["status"] for r in world.receipts] == ["unknown"]
    key = push.repo_key(str(world.proj))
    lines = [json.loads(x) for x in (world.lhome / "push" / f"{key}.journal.jsonl").read_text().splitlines()]
    assert [(x["state"], x.get("status")) for x in lines] == [("intent", None), ("done", "unknown")]


def test_d5_unknown_when_git_cannot_even_start(world, monkeypatch):
    real = push._git

    def nogit(argv, **kw):
        if "push" in argv:
            raise OSError("exec failed")
        return real(argv, **kw)
    monkeypatch.setattr(push, "_git", nogit)
    with pytest.raises(push.PushOutcomeUnknown):
        world.run()
    assert world.receipts[-1]["status"] == "unknown"


def test_d7_a_branch_naming_a_tag_object_pushes_the_commit_in_the_receipt(world):
    tip = world.tip()
    world.git("-C", world.proj, "-c", "user.name=t", "-c", "user.email=t@x", "tag", "-a", "-m", "t", "tg", tip)
    tag = world.git("-C", world.proj, "rev-parse", "tg").stdout.strip()
    # git's update-ref refuses a tag on a branch; the agent writes the ref file itself
    (world.proj / ".git" / "refs" / "heads" / "claude" / "t").write_text(tag + "\n")
    res = world.run("claude/t")
    assert res.new_sha == tip
    assert world.remote_sha("refs/heads/claude/t") == tip
    assert world.receipts[-1]["new_sha"] == tip


def test_sha256_repositories_refuse_by_name(world):
    s2 = world.ws / "s2"
    world.git("init", "-q", "--object-format=sha256", "-b", "claude/z", s2)
    world.git("-C", s2, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "--allow-empty", "-m", "z")
    with pytest.raises(push.PushRefused, match="SHA-256 repositories are not supported yet"):
        world.run("claude/z", repo=s2)


def test_toctou_a_different_real_repo_at_the_same_path_refuses(world):
    """No symlink at all: another repository renamed onto the checked path. Only the (device, inode)
    comparison catches this one."""
    sec, s = _secret(world)
    world.git("-C", sec, "branch", "claude/x")

    def swap(**kw):
        os.rename(world.proj, world.ws / "proj.orig")
        os.rename(sec, world.proj)
        return world.refusal(**kw)
    with pytest.raises(push.PushRefused, match="a different repository"):
        world.run(refusal=swap)
    assert world.remote_sha("refs/heads/claude/x") is None


# --- round 3: transport bounded, the record after the intent line (L3 r2, DESIGN_r3) ---------------------------

def _after_the_check(monkeypatch, plant):
    """Run ``plant`` right after the under-lock topology check (the 2nd call), i.e. between the scan and the
    fetch: the agent writes into its repo once levain has looked."""
    real, calls = push._repo_topology, []

    def hooked(path):
        out = real(path)
        calls.append(path)
        if len(calls) == 2:
            plant()
        return out
    monkeypatch.setattr(push, "_repo_topology", hooked)
    return calls


@pytest.mark.parametrize("how", ["ext", "ext-via-insteadof", "ssh-command", "fd"])
def test_transport_planted_after_the_check_never_runs(world, monkeypatch, how):
    """DESIGN_r3 item 1, RUN: after the check the agent makes its repo a partial clone whose promisor is an
    ext::/fd::/ssh transport and deletes a blob the tip needs, so upload-pack must lazy-fetch it. A git that
    lazy-fetches anyway (an older one that ignores GIT_NO_LAZY_FETCH; simulated by GIT_NO_LAZY_FETCH=0, which
    re-enables it in upload-pack) must still refuse the transport: the marker stays absent."""
    (world.proj / "f.txt").write_text("needed\n")
    world.git("-C", world.proj, "add", "f.txt")
    world.commit("blob")
    blob = world.git("-C", world.proj, "rev-parse", "HEAD:f.txt").stdout.strip()
    mark = world.marks / "transport.fired"
    ssh = world.marker("lazy-ssh")
    url = {"ext": f"ext::sh -c touch% {mark}", "ext-via-insteadof": "ssh://evil.invalid/x",
           "ssh-command": "ssh://evil.invalid/x", "fd": "fd::7"}[how]

    def plant():
        g = ["-C", world.proj, "config"]
        world.git(*g, "core.repositoryformatversion", "1")
        world.git(*g, "extensions.partialclone", "evil")
        world.git(*g, "remote.evil.promisor", "true")
        world.git(*g, "remote.evil.url", url)
        world.git(*g, "protocol.ext.allow", "always")
        world.git(*g, "protocol.fd.allow", "always")
        world.git(*g, "protocol.allow", "always")
        if how == "ext-via-insteadof":
            world.git(*g, f"url.ext::sh -c touch% {mark}% #.insteadOf", "ssh://evil.invalid/")
        if how == "ssh-command":
            world.git(*g, "core.sshCommand", ssh)
        (world.proj / ".git" / "objects" / blob[:2] / blob[2:]).unlink()

    real_env = push.git_env

    def lazy_env(protocols=""):
        env = real_env(protocols)
        env["GIT_NO_LAZY_FETCH"] = "0"
        return env
    monkeypatch.setattr(push, "git_env", lazy_env)
    calls = _after_the_check(monkeypatch, plant)
    with pytest.raises(push.PushRefused, match="could not read branch"):
        world.run()
    assert len(calls) == 2
    assert not mark.exists() and world.fired() == []
    assert world.remote_sha("refs/heads/claude/x") is None and world.receipts == []


def test_push_protocol_is_the_destinations_scheme_only():
    assert push.push_protocol(push.canonical_remote("ssh://git@h/r")) == "ssh"
    assert push.push_protocol(push.canonical_remote("git@h:r.git")) == "ssh"
    assert push.push_protocol(push.canonical_remote("https://h/r")) == "https"
    assert push.push_protocol(push.canonical_remote("file:///r.git")) == "file"


def test_fetch_and_push_run_with_only_their_own_protocol(world, monkeypatch):
    real, seen = push._git, []

    def spy(argv, **kw):
        sub = next((a for a in argv if a in ("fetch", "push")), None)
        seen.append((sub, kw.get("protocols", "")))
        return real(argv, **kw)
    monkeypatch.setattr(push, "_git", spy)
    assert world.run().ok
    assert [x for x in seen if x[0]] == [("fetch", "file"), ("push", "file")]
    assert {p for sub, p in seen if not sub} == {""}


def test_d8_invalid_utf8_from_a_remote_hook_still_records(world):
    """A destination hook prints bytes that are not UTF-8 after accepting the update. The push landed: the done
    line and the receipt must both be written (round 2: UnicodeDecodeError escaped and wrote neither)."""
    hook = world.remote / "hooks" / "post-receive"
    hook.write_text("#!/bin/sh\nprintf 'bad \\377\\376 bytes\\n' >&2\nexit 0\n")
    hook.chmod(0o755)
    tip = world.tip()
    res = world.run()
    assert (res.ok, res.status) == (True, "new") and world.remote_sha("refs/heads/claude/x") == tip
    assert [r["status"] for r in world.receipts] == ["new"]
    key = push.repo_key(str(world.proj))
    lines = [json.loads(x) for x in (world.lhome / "push" / f"{key}.journal.jsonl").read_text().splitlines()]
    assert [(x["state"], x.get("status")) for x in lines] == [("intent", None), ("done", "new")]


def test_d8_any_exception_after_the_intent_is_an_unknown_outcome(world, monkeypatch):
    real = push._git

    def boom(argv, **kw):
        if "push" in argv:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return real(argv, **kw)
    monkeypatch.setattr(push, "_git", boom)
    with pytest.raises(push.PushOutcomeUnknown) as ei:
        world.run()
    assert ei.value.result.status == "unknown" and "UnicodeDecodeError" in ei.value.result.detail
    assert [r["status"] for r in world.receipts] == ["unknown"]
    key = push.repo_key(str(world.proj))
    states = [json.loads(x)["state"] for x in (world.lhome / "push" / f"{key}.journal.jsonl").read_text().splitlines()]
    assert states == ["intent", "done"]


def test_d10_git_resolved_before_the_intent_line(world, monkeypatch):
    """The push's git is resolved before the intent line, and the push itself does not resolve it again (a
    refusal there would leave an intent with no done line)."""
    order, real_gb, real_j = [], push.git_bin, push._journal

    def gb():
        order.append("git_bin")
        return real_gb()

    def j(home, key, record):
        order.append(record["state"])
        return real_j(home, key, record)
    monkeypatch.setattr(push, "git_bin", gb)
    monkeypatch.setattr(push, "_journal", j)
    assert world.run().ok
    i, d = order.index("intent"), order.index("done")
    assert order[i - 1] == "git_bin" and "git_bin" not in order[i:d]


def test_private_dir_fsyncs_the_parent_only_when_it_creates(world, monkeypatch):
    real_fsync, synced = os.fsync, []

    def spy(fd):
        st = os.fstat(fd)
        import stat as st_
        if st_.S_ISDIR(st.st_mode):
            synced.append(st.st_ino)
        return real_fsync(fd)
    monkeypatch.setattr(push.os, "fsync", spy)
    push._private_dir(world.lhome / "push")
    assert synced == [os.stat(world.lhome).st_ino]
    push._private_dir(world.lhome / "push")
    assert synced == [os.stat(world.lhome).st_ino]


def test_d11_the_object_store_walk_is_capped(world, monkeypatch):
    monkeypatch.setattr(push, "MAX_OBJECT_ENTRIES", 3)
    with pytest.raises(push.PushRefused, match="more than 3 entries"):
        world.run()
    assert world.remote_sha("refs/heads/claude/x") is None and world.receipts == []


def test_expect_sha_refuses_a_branch_moved_after_the_allow(world):
    # seen red (B6 RUN prep, 10-18): with no expected commit, a branch moved between the operator's allow and the
    # lock pushed the commit nobody was shown
    allowed = world.tip()

    def moved_then_judged(**kw):
        world.commit("moved after the allow")
        return world.refusal(**kw)

    with pytest.raises(push.PushRefused, match="not " + allowed + ", the commit that was allowed"):
        push.execute(world.grant, repo_path=world.proj, remote=world.url, branch="claude/x", levain_home=world.lhome,
                     refusal=moved_then_judged, receipt=world.receipts.append, expect_sha=allowed)
    assert world.remote_sha("refs/heads/claude/x") is None and world.receipts == []
