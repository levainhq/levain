"""S1 session 5 piece 5d-ii session 3: the take (:mod:`levain.slot.adopt_take`, DESIGN §6a, CONTRACT E14).

The rows come from the session's RUN (lane adopt5dii-s3, ``1023-6/adopt/s3_run.txt``, expected text in that dir's
``s3_expected.md``, written before it ran). These are the contract's five owed T:5 tests: the take into a FILES
repository and a reftable one, the claim check, every token of the closed idx enum, the pack written from the private
copy, and the pack's trailer held to its hash. The export bundle is ``bundlefmt.write_header`` + ``pack-objects``;
the verifier's idx is ``index-pack -o`` of the same pack (what Q holds), and the claims are what an honest verifier
says, so each row edits exactly one thing.
"""
import errno
import hashlib
import os
import shutil
import stat
import struct
import subprocess
from pathlib import Path

import pytest

import levain.slot as SLOT
from levain.slot import Refused
from levain.slot import adopt_take as AT, bundlefmt as BF

GIT = shutil.which("git")
S = "s-" + "e4" * 8
ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
       "LC_ALL": "C", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a",
       "GIT_COMMITTER_EMAIL": "a@a", "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000"}
pytestmark = pytest.mark.skipif(GIT is None, reason="needs a git")


def g(*a, cwd=None, stdin=None) -> bytes:
    return subprocess.run([GIT, *a], cwd=cwd, env=ENV, check=True, capture_output=True, input=stdin).stdout


def make_his(path: Path, ref_format: str = "files") -> str:
    extra = ["--ref-format=reftable"] if ref_format == "reftable" else []
    g("init", "-q", *extra, str(path))
    (path / "a.txt").write_text("base\n")
    g("add", "a.txt", cwd=path)
    g("commit", "-q", "-m", "base", cwd=path)
    g("config", "pack.writeReverseIndex", "true", cwd=path)
    g("config", "fetch.writeCommitGraph", "true", cwd=path)
    return g("rev-parse", "HEAD", cwd=path).decode().strip()


@pytest.fixture(scope="module")
def ex(tmp_path_factory):
    """The export: his base, a session head + wip, the bundle, Q's idx (and one with large offsets), the claims."""
    t = tmp_path_factory.mktemp("take")
    his = t / "his"
    base = make_his(his)
    work = t / "work"
    g("clone", "-q", "--no-local", str(his), str(work))
    for i in range(64):                 # enough oids that two share a first byte
        (work / f"f{i:02d}").write_text(f"{i}\n")
    g("add", "-A", cwd=work)
    g("commit", "-q", "-m", "session head", cwd=work)
    head = g("rev-parse", "HEAD", cwd=work).decode().strip()
    (work / "w").write_text("wip\n")
    g("add", "w", cwd=work)
    g("commit", "-q", "-m", "session wip", cwd=work)
    wip = g("rev-parse", "HEAD", cwd=work).decode().strip()
    objs = g("rev-list", "--objects", head, wip, f"^{base}", cwd=work)
    pack = g("pack-objects", "-q", "--stdout", cwd=work,
             stdin=b"".join(line.split(b" ")[0] + b"\n" for line in objs.splitlines()))
    bundle = BF.write_header("sha1", [(head, f"refs/levain/{S}/head".encode()),
                                      (wip, f"refs/levain/{S}/wip".encode())], [base]) + pack
    q = t / "q"
    g("init", "-q", str(q))
    (q / "p.pack").write_bytes(pack)
    g("index-pack", "-o", str(q / "p.idx"), str(q / "p.pack"), cwd=q)
    g("index-pack", "--index-version=2,0x10", "-o", str(q / "large.idx"), str(q / "p.pack"), cwd=q)
    hdr = BF.parse(bundle[:bundle.index(b"\n\n") + 2])
    claims = {"refs": {"head": head, "wip": wip}, "prerequisites": [base], "pack_offset": hdr.size,
              "pack_sha256": hashlib.sha256(pack).hexdigest(), "object_count": struct.unpack(">I", pack[8:12])[0]}
    return {"t": t, "his": his, "base": base, "head": head, "wip": wip, "pack": pack, "bundle": bundle,
            "idx": (q / "p.idx").read_bytes(), "large": (q / "large.idx").read_bytes(), "claims": claims}


def private(d: Path, bundle: bytes, idx: bytes):
    d.mkdir(mode=0o700)
    (d / "bundle").write_bytes(bundle)
    (d / "q.idx").write_bytes(idx)
    return d / "bundle", d / "q.idx"


def wrapper(t: Path) -> tuple:
    """A git that logs every argv (the take's process log), then runs the real one."""
    log = t / "process.log"
    w = t / "git-wrap"
    w.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{log}'\nexec '{GIT}' \"$@\"\n")
    w.chmod(0o755)
    return w, log


def take(ex, repo, tmp: Path, *, bundle=None, idx=None, claims=None, which="head", git=None, log=None):
    b, i = private(tmp / f"priv-{len(os.listdir(tmp))}", ex["bundle"] if bundle is None else bundle,
                   ex["idx"] if idx is None else idx)
    return AT.take(git=git or GIT, repo=repo, bundle=b, idx=i, claims=claims or ex["claims"], session=S,
                   object_format="sha1", which=which, log=log)


def refused(fn) -> str:
    with pytest.raises(Refused) as e:
        fn()
    return e.value.name


def snap(repo: Path) -> dict:
    refs = dict(line.split(" ") for line in
                g("for-each-ref", "--format=%(refname) %(objectname)", cwd=repo).decode().splitlines())
    logs = {n: subprocess.run([GIT, "reflog", "show", "--no-abbrev", "--format=%H %gs", n, "--"], cwd=repo, env=ENV,
                              capture_output=True).stdout for n in list(refs) + ["HEAD"]}
    files = {str(p.relative_to(repo / ".git")): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in (repo / ".git").rglob("*") if p.is_file() and not p.is_symlink()}
    return {"refs": refs, "logs": logs, "files": files}


@pytest.mark.parametrize("ref_format", ["files", "reftable"])
def test_take_create_only(ex, tmp_path, ref_format, monkeypatch):
    """A14: one new logical ref + its reflog, nothing old changed, only the backend's storage and the migrated pack +
    idx changed under ``.git/``; no ``index-pack`` and no hook; a second take = ``take-ref-exists``; ``wip`` beside it
    reuses the present pack; his ``levain`` branch = ``take-ref-conflict`` (s3_run T1-T5, T1r-T5r)."""
    repo = tmp_path / "his"
    try:
        base = make_his(repo, ref_format)
    except subprocess.CalledProcessError:
        pytest.skip(f"this git has no {ref_format} backend")
    assert base == ex["base"]
    marker = tmp_path / "hook-ran"
    for h in ("reference-transaction", "post-index-change"):
        p = repo / ".git" / "hooks" / h
        p.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        p.chmod(0o755)
    monkeypatch.setenv("GIT_DIR", "/nonexistent")
    git, plog = wrapper(tmp_path)
    pre = snap(repo)
    lines: list = []
    t = take(ex, repo, tmp_path, git=str(git), log=lines.append)
    post = snap(repo)
    ref = f"refs/heads/levain/{S}"
    assert t.ref == ref and t.oid == ex["head"]
    assert plog.read_text().splitlines() == [
        "-c core.hooksPath=/dev/null rev-parse --git-path objects",
        f"-c core.hooksPath=/dev/null update-ref --no-deref --create-reflog {ref} {ex['head']} {'0' * 40}"]
    assert not marker.exists()
    assert {k: v for k, v in post["refs"].items() if k != ref} == pre["refs"] and post["refs"][ref] == ex["head"]
    assert all(post["logs"][k] == v for k, v in pre["logs"].items())
    assert len(post["logs"][ref].splitlines()) == 1
    stem = "objects/pack/pack-" + ex["pack"][-20:].hex()
    changed = {k for k in set(pre["files"]) | set(post["files"]) if pre["files"].get(k) != post["files"].get(k)}
    rest = changed - {stem + ".pack", stem + ".idx"}
    if ref_format == "files":
        assert rest == {f"refs/heads/levain/{S}", f"logs/refs/heads/levain/{S}"}
    else:
        assert rest and all(c.startswith("reftable/") for c in rest)
    assert [x for x in lines if x.startswith("rename ")][-1].endswith(".idx")      # the idx LAST
    for ext in (".pack", ".idx"):
        st = (repo / ".git" / (stem + ext)).stat()
        assert stat.S_IMODE(st.st_mode) == 0o444 and st.st_nlink == 1
    assert subprocess.run([GIT, "fsck", "--strict", "--no-dangling"], cwd=repo, env=ENV).returncode == 0
    again = snap(repo)
    assert refused(lambda: take(ex, repo, tmp_path)) == "take-ref-exists"
    assert snap(repo)["refs"] == again["refs"]
    w = take(ex, repo, tmp_path, which="wip")
    assert w.ref == ref + "-wip" and w.migrated.skipped == [w.migrated.pack, w.migrated.idx]
    other = tmp_path / "other"
    make_his(other, ref_format)
    g("branch", "levain", cwd=other)
    assert refused(lambda: take(ex, other, tmp_path)) == "take-ref-conflict"


@pytest.mark.parametrize("claim", ["refs", "prerequisites", "pack_offset", "pack_sha256", "object_count"])
def test_take_claim_mismatch_refused(ex, tmp_path, claim):
    """Each of the five claims the take re-derives, off by one thing = ``verifier-claim-mismatch``, nothing written
    (s3_run C1-C6)."""
    repo = tmp_path / "his"
    make_his(repo)
    c = dict(ex["claims"], refs=dict(ex["claims"]["refs"]))
    c[claim] = {"refs": {"head": ex["base"], "wip": ex["wip"]}, "prerequisites": [],
                "pack_offset": c["pack_offset"] + 1, "pack_sha256": "0" * 64,
                "object_count": c["object_count"] + 1}[claim]
    before = os.listdir(repo / ".git" / "objects" / "pack")
    assert refused(lambda: take(ex, repo, tmp_path, claims=c)) == "verifier-claim-mismatch"
    assert os.listdir(repo / ".git" / "objects" / "pack") == before


def _retrailer(b: bytes) -> bytes:
    return b[:-20] + hashlib.sha1(b[:-20]).digest()


def _set_off(b: bytes, i: int, v: int) -> bytes:
    p = 1032 + 24 * struct.unpack(">I", b[1028:1032])[0] + 4 * i
    return b[:p] + struct.pack(">I", v) + b[p + 4:]


def _offs(b: bytes) -> list:
    n = struct.unpack(">I", b[1028:1032])[0]
    return [struct.unpack(">I", b[1032 + 24 * n + 4 * i:1032 + 24 * n + 4 * i + 4])[0] for i in range(n)]


def _seam(ex, check: str):
    """(bundle, idx, claims) with exactly the one structural fact ``check`` names made false."""
    idx, bundle, claims = ex["idx"], ex["bundle"], ex["claims"]
    n = struct.unpack(">I", idx[1028:1032])[0]
    offs = _offs(idx)
    non12 = [j for j, v in enumerate(offs) if v != 12]
    if check == "magic":
        return bundle, b"\376" + idx[1:], claims
    if check == "version":
        return bundle, _retrailer(idx[:4] + struct.pack(">I", 3) + idx[8:]), claims
    if check == "fanout":
        return bundle, _retrailer(idx[:408] + struct.pack(">I", n + 5) + idx[412:]), claims
    if check == "count":
        return bundle, _retrailer(idx[:1028] + struct.pack(">I", n + 1) + idx[1032:]), claims
    if check == "order":
        j = next(j for j in range(n - 1) if idx[1032 + 20 * j] == idx[1032 + 20 * (j + 1)])
        a = 1032 + 20 * j
        return bundle, _retrailer(idx[:a + 20] + idx[a:a + 20] + idx[a + 40:]), claims
    if check == "crc-len":
        c = 1032 + 20 * n
        return bundle, _retrailer(idx[:c] + idx[c + 4:]), claims
    if check == "offset-range":
        return bundle, _retrailer(_set_off(idx, non12[0], len(ex["pack"]))), claims
    if check == "first-offset":
        return bundle, _retrailer(_set_off(idx, offs.index(12), 13)), claims
    if check == "offset-dup":
        return bundle, _retrailer(_set_off(idx, non12[0], offs[non12[1]])), claims
    if check == "large-table":
        lg = ex["large"]
        msb = [j for j, v in enumerate(_offs(lg)) if v & 0x80000000]
        return bundle, _retrailer(_set_off(lg, msb[0], _offs(lg)[msb[1]])), claims
    if check == "pack-checksum":
        p = len(idx) - 40
        return bundle, _retrailer(idx[:p] + bytes([idx[p] ^ 1]) + idx[p + 1:]), claims
    if check == "trailer":
        return bundle, idx[:-1] + bytes([idx[-1] ^ 1]), claims
    assert check == "pack-hash"
    bb = bundle[:-1] + bytes([bundle[-1] ^ 1])
    p = len(idx) - 40
    return (bb, _retrailer(idx[:p] + bb[-20:] + idx[p + 20:]),
            dict(claims, pack_sha256=hashlib.sha256(bb[claims["pack_offset"]:]).hexdigest()))


@pytest.mark.parametrize("check", AT.IDX_CHECKS)
def test_take_idx_structural_checks(ex, tmp_path, check):
    """Every token of E14's closed enum has a seam idx that names it, and only it; the honest idx and git's own
    large-offset idx pass (s3_run I-*)."""
    bundle, idx, claims = _seam(ex, check)
    b, i = private(tmp_path / "p", bundle, idx)
    d = AT.derive(b, session=S, object_format="sha1")
    AT.check_claims(claims, d)
    assert refused(lambda: AT.check_idx(i, d)) == f"take-idx-invalid:{check}"


@pytest.mark.parametrize("which", ["idx", "large"])
def test_take_idx_honest_passes(ex, tmp_path, which):
    """Q's idx and git's large-offset idx (``index-pack --index-version=2,0x10``) pass every check."""
    b, i = private(tmp_path / "p", ex["bundle"], ex[which])
    d = AT.derive(b, session=S, object_format="sha1")
    AT.check_claims(ex["claims"], d)
    assert AT.check_idx(i, d) == hashlib.sha256(ex[which]).hexdigest()


def test_take_pack_from_private_copy(ex, tmp_path, monkeypatch):
    """The pack is written from the private copy, hashed while written: a byte flipped in the copy after step 0 =
    ``take-bytes-changed``; a write error = ``take-failed:ENOSPC``; a file of other bytes at the final name =
    ``take-pack-collision``; each leaves no temp and no ref (s3_run M2, M3, M5)."""
    repo = tmp_path / "his"
    make_his(repo)
    pd = repo / ".git" / "objects" / "pack"
    b, i = private(tmp_path / "p", ex["bundle"], ex["idx"])
    d = AT.derive(b, session=S, object_format="sha1")
    AT.check_claims(ex["claims"], d)
    isha = AT.check_idx(i, d)
    raw = bytearray(b.read_bytes())
    raw[d.pack_offset + 100] ^= 0xFF
    b.write_bytes(bytes(raw))
    assert refused(lambda: AT.migrate(GIT, repo, b, i, d, isha)) == "take-bytes-changed"
    assert os.listdir(pd) == []

    def enospc(fd, data):
        raise OSError(errno.ENOSPC, "seam")
    monkeypatch.setattr(SLOT, "write_all", enospc)
    assert refused(lambda: take(ex, repo, tmp_path)) == "take-failed:ENOSPC"
    monkeypatch.undo()
    assert os.listdir(pd) == []
    junk = pd / f"pack-{ex['pack'][-20:].hex()}.pack"
    junk.write_bytes(b"junk\n")
    assert refused(lambda: take(ex, repo, tmp_path)) == "take-pack-collision"
    assert sorted(os.listdir(pd)) == [junk.name] and junk.read_bytes() == b"junk\n"
    assert g("for-each-ref", "refs/heads/levain", cwd=repo) == b""


def test_take_pack_hash_is_trailer(ex, tmp_path):
    """The pack's own hash over pack-minus-trailer is computed in step 0's one pass and must equal the trailer, the
    name the pack is migrated under (s3_run I-pack-hash, I-real)."""
    b, _i = private(tmp_path / "p", ex["bundle"], ex["idx"])
    d = AT.derive(b, session=S, object_format="sha1")
    assert d.pack_hash_ok and d.trailer == ex["pack"][-20:]
    assert d.trailer == hashlib.sha1(ex["pack"][:-20]).digest()
    bb, idx, claims = _seam(ex, "pack-hash")
    b2, i2 = private(tmp_path / "q", bb, idx)
    d2 = AT.derive(b2, session=S, object_format="sha1")
    assert not d2.pack_hash_ok
    AT.check_claims(claims, d2)
    assert refused(lambda: AT.check_idx(i2, d2)) == "take-idx-invalid:pack-hash"
