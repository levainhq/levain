"""W-2d W-T: the read-set clone template and ``start``'s post-clone check (CONTRACT §5.4 step 3, §2.1, §3.4).

Built on 2f3's harness (a tmp store, the REAL CONTROL, a fake process table), with the clone and the check children
run as REAL git, as the test user, by the exact argv and env file ``start`` hands the wall: the store's path in each
string is mapped to the test's tmp store, nothing else is changed. A ``dispatch`` alloc is the operator's
``interactive`` one with its kind rewritten (a ticketed prepare is not built here). The same run as a slot uid,
through the real wall, is a VM row.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace

import pytest

from levain.contracts import canon
from levain.dispatch import contract as DC
from levain.slot import render as RD
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot import start as SS
from levain.slot import wall
from levain.slot.layout import STORE
from tests.test_slot_s2f2_runtime_helper import _blob
from tests.test_slot_s2f3_start import _world
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

GENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_TERMINAL_PROMPT": "0"}
#: The read set P11 converts under ``--template=``: an ``ident`` + ``eol=crlf`` attributes file and a ``$Id$`` file.
FILES = {".gitattributes": b"* ident\n*.txt eol=crlf\n", "id.txt": b"$Id$\nline1\nline2\n", "plain": b"plain\n"}


def _git(*args, cwd, inp=None):
    r = subprocess.run(["/usr/bin/git", *args], cwd=cwd, env={**GENV, "HOME": str(cwd)}, input=inp,
                       capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _bundle(tmp) -> bytes:
    src = tmp / "src"
    src.mkdir()
    _git("init", "-q", "--template=", "-b", "readset", cwd=src)
    for name, data in FILES.items():
        (src / name).write_bytes(data)
    _git("add", *FILES, cwd=src)
    _git("commit", "-q", "-m", "one", cwd=src)
    _git("bundle", "create", "-q", str(tmp / "rs.bundle"), "HEAD", "readset", cwd=src)
    return (tmp / "rs.bundle").read_bytes()


def _bundle_of(tmp, files) -> bytes:
    """A bundle (``HEAD`` + ``readset``) whose one commit holds ``files`` ({path: bytes}, all 100644), its index
    built with ``update-index --cacheinfo`` (no worktree, so a path's length is never the host's to refuse)."""
    src = tmp / "src"
    _git("init", "-q", "--template=", "-b", "readset", str(src), cwd=tmp)
    for path, data in files.items():
        sha = _git("hash-object", "-w", "--stdin", cwd=src, inp=data).decode().strip()
        _git("update-index", "--add", "--cacheinfo", f"100644,{sha},{path}", cwd=src)
    tree = _git("write-tree", cwd=src).decode().strip()
    _git("update-ref", "refs/heads/readset", _git("commit-tree", tree, "-m", "one", cwd=src).decode().strip(), cwd=src)
    _git("bundle", "create", "-q", str(tmp / "rs.bundle"), "HEAD", "readset", cwd=src)
    return (tmp / "rs.bundle").read_bytes()


class RealGit:
    """The wall's clone and clone-check targets as real git: each spec's argv, cwd and env file (the store path
    mapped to the test's store) run as the test user; the check children's stdout through the helper's own drain.
    Every spec is first parsed as the stage parses it (``wall.parse_spec``: its cap and shape)."""

    def __init__(self, w, la) -> None:
        self.w, self.la = w, la
        self.store, self.root = str(STORE[w.seams.host]), str(w.L)
        self.clone_rc: int | None = None
        self.clone_id_txt: bytes | None = None
        self.captures: list[dict] = []
        self._spawn = la.spawn
        w.seams.spawn, w.seams.capture = self.spawn, self.capture

    def _m(self, s: str) -> str:
        return s.replace(self.store, self.root)

    def _run(self, spec: dict) -> subprocess.Popen:
        env = {k: self._m(v) for k, v in wall.parse_env(open(self._m(spec["env_file"]), "rb").read()).items()}
        return subprocess.Popen([self._m(a) for a in spec["argv"]], cwd=self._m(spec["cwd"]), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def spawn(self, argv, spec_json, cgroup, exec_w, detach) -> int:
        pid = self._spawn(argv, spec_json, cgroup, exec_w, detach)
        spec = canon.parse(spec_json)
        if spec["target"] == "clone":
            p = self._run(spec)
            p.communicate(timeout=60)
            self.clone_rc = self.la.codes["clone"] = p.returncode
            idp = os.path.join(self._m(spec["cwd"]), "id.txt")
            self.clone_id_txt = open(idp, "rb").read() if os.path.exists(idp) else None
        return pid

    def capture(self, argv, spec_json, cgroup, bound):
        wall.parse_spec(spec_json)
        spec = canon.parse(spec_json)
        self.captures.append({"argv": tuple(argv), "spec": spec, "cgroup": cgroup, "bound": bound})
        p = self._run(spec)
        try:
            out = H.drain(p.stdout.fileno(), bound, deadline_s=60)
        finally:
            p.stdout.close()
        return p.wait(timeout=60), out


def _dispatch_rendered(tmp_path, files=None):
    """A rendered ``dispatch`` session whose staged bundle is :data:`FILES`' read set (or ``files``')."""
    w, la, table = _world(tmp_path)
    line = w.prepare()
    assert line["ok"], line
    s = w.session = line["session"]
    a = w.alloc(s)
    (w.L / "alloc" / f"{s}.json").write_bytes(R.alloc_bytes(replace(a, kind="dispatch")))
    w.stage(s, _bundle(tmp_path) if files is None else _bundle_of(tmp_path, files))
    assert w.main(["render", s], _blob())[1]["ok"]
    return w, la, table, s


def test_slot_clone_template_neutralises_attributes(tmp_path):
    """T:2 (C1, C2): the bundle holds ``* ident`` + ``*.txt eol=crlf`` + a ``$Id$`` file; the clone runs the exact
    argv ``clone_argv(S, "dispatch")`` builds and ``start``'s C7 (b) checks it; every worktree file hashes
    ``--no-filters`` to its blob and the check passes. Reproduced first: P11's control (``--template=``) expands
    ``$Id$`` and converts LF to CRLF."""
    w, la, _, s = _dispatch_rendered(tmp_path)
    S = w.L / "live" / s
    assert (S / RD.READSET_ATTRIBUTES_PATH).read_bytes() == RD.READSET_ATTRIBUTES
    tdir = f"live/{s}/{RD.READSET_TEMPLATE}"
    assert w.chown_of(tdir) == (0, 0) and w.mode(tdir) == 0o755
    g = RealGit(w, la)
    assert w.main(["start", s])[1]["ok"], w.last_err
    (c,) = la.of("clone")
    SP = str(STORE["linux"]) + f"/live/{s}"
    assert tuple(c["spec"]["argv"]) == wall.clone_argv(wall.PurePosixPath(SP), "dispatch")
    assert f"--template={SP}/{RD.READSET_TEMPLATE}" in c["spec"]["argv"]
    assert g.clone_rc == 0 and g.clone_id_txt == FILES["id.txt"]           # not expanded, still LF
    assert [x["spec"]["argv"][7] for x in g.captures] == ["ls-files", "hash-object"]
    for x in g.captures:                                                   # as the slot, by the clone's own path
        assert (x["spec"]["target"], x["spec"]["uid"], x["spec"]["cwd"]) == ("clone-check", c["spec"]["uid"],
                                                                            c["spec"]["cwd"])
        assert x["argv"][:2] == c["argv"][:2] and x["cgroup"] == c["cgroup"]
    idx = _git("ls-files", "-s", "-z", cwd=S / "repo").split(b"\0")[:-1]
    assert len(idx) == len(FILES)
    for rec in idx:
        meta, path = rec.split(b"\t", 1)
        assert _git("hash-object", "--no-filters", "--", path.decode(), cwd=S / "repo").strip() == meta.split()[1]
    assert (w.L / "alloc" / f"{s}.cloned").exists()
    assert w.steps.index("clone") < w.steps.index("clone-check") < w.steps.index("exec")


def test_slot_clone_missing_template_refused(tmp_path):
    """T:2 (C7's negative test): with the rendered template absent the clone still exits 0, converted (P14a);
    ``start`` refuses ``clone-failed`` naming (a), ``<S>/repo`` is emptied, no clone marker, no later spawn."""
    w, la, _, s = _dispatch_rendered(tmp_path)
    S = w.L / "live" / s
    shutil.rmtree(S / RD.READSET_TEMPLATE)
    g = RealGit(w, la)
    line = w.main(["start", s])[1]
    assert line["refused"] == "clone-failed", line
    assert "(a)" in w.last_err
    assert g.clone_rc == 0 and g.clone_id_txt != FILES["id.txt"]           # P14a: rc 0, and converted
    assert os.listdir(S / "repo") == []
    assert not (w.L / "alloc" / f"{s}.cloned").exists()
    assert [r["target"] for r in la.spawns] == ["clone"] and g.captures == []
    assert w.state(s).state == R.RENDERED


def test_slot_clone_check_batches_fit_the_spec_cap(tmp_path):
    """V9 against the stage's real bound: a legal read set of 64 nested 1023-byte paths (the longest this macOS host
    checks out; PATH_MAX counts its NUL) makes ``hash-object`` specs that ``wall.parse_spec`` accepts, every one at
    most ``SPEC_CAP``. Reproduced first: the 128 KiB argv cap built one 66,089-byte spec, the stage refused it
    ``spec-malformed`` and ``start`` turned a good clone into ``clone-failed``."""
    dirs = "/".join(c * 200 for c in "pqrs")
    files = {f"{dirs}/{i:03d}".ljust(1023, "x"): f"{i}\n".encode() for i in range(64)}
    w, la, _, s = _dispatch_rendered(tmp_path, files)
    g = RealGit(w, la)
    assert w.main(["start", s])[1]["ok"], w.last_err
    hashed = [x for x in g.captures if x["spec"]["argv"][7] == "hash-object"]
    sizes = [len(canon.canonical_bytes(x["spec"])) for x in hashed]
    assert len(hashed) >= 2 and max(sizes) <= wall.SPEC_CAP, sizes
    assert [p for x in hashed for p in x["spec"]["argv"][len(wall.HASH_OBJECT_PREFIX):]] == sorted(files)
    assert (w.L / "alloc" / f"{s}.cloned").exists()


def test_slot_clone_check_refuses_a_path_past_the_read_set_cap():
    """codex r1 MED: an ``ls-files`` record whose path is past the read set's 1024 bytes is refused by name before
    it is decoded; start.py's restated caps equal the contract's, so a drift fails here. Reproduced first: a
    1025-byte nested path parsed as one entry and passed."""
    assert (SS.READSET_PATHS_MAX, SS.READSET_PATH_BYTES) == (DC.PATHS_MAX, DC.PATH_MAX_BYTES)
    head = "/".join(c * 200 for c in "pqrs") + "/"
    ok, long = head.ljust(1024, "x"), head.ljust(1025, "x")
    rec = b"100644 " + b"0" * 40 + b" 0\t"
    assert [e.path for e in SS.parse_ls_files(rec + ok.encode() + b"\0")] == [ok]
    with pytest.raises(SS.CheckFailed, match="a path of 1025 bytes, past the read set's 1024"):
        SS.parse_ls_files(rec + ok.encode() + b"\0" + rec + long.encode() + b"\0")
