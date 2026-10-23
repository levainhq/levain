"""S1 session 5, piece 5c-ii-a: the sealed run's classifier and publish transaction (``levain/slot/export_run.py``).

The commit calls run for REAL on ``tmp_path`` (darwin ``renameatx_np``, Linux ``renameat2``), so CI's Linux job is the
run of the exchange and the no-replace rename on its filesystem (DESIGN §1 step 11, r12 S15-1 / r13 S16-6). Every other
refusal comes from an injected seam. The line parser is checked against the importer's own writer, never a copy of
its schema.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import export_run as X
from levain.slot import importer as IMP

S = "s1"
OID_A = "a" * 40
OID_B = "b" * 40


def _line(*, terminal=True, enospc=False, refused=None, result="delta", detail="") -> bytes:
    return IMP.result_line(IMP.Progress(terminal=terminal, entries=3, bytes=10), refused=refused, detail=detail,
                           enospc=enospc, result=result)


def _digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode() + b"\0")
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def _dfd(p: Path) -> int:
    return os.open(p, os.O_RDONLY | os.O_DIRECTORY)


class Log:
    def __init__(self):
        self.events = []

    def __call__(self, e):
        self.events.append(e)


def _seams(log=None, **kw) -> X.PublishSeams:
    return X.PublishSeams(log=log or Log(), **kw)


def _fault_at(point: str, err: int):
    def fault(p):
        if p == point:
            raise OSError(err, os.strerror(err))
    return fault


def _unsupported(kind: str):
    def rename2(fd1, n1, fd2, n2, flag):
        if kind == "symbol":
            raise X.ExchangeUnsupported("no symbol")
        raise X.ExchangeUnsupported(kind)
    return rename2


# --- the real commit calls (the CI Linux discharge) -------------------------------------------------------------

def test_real_exchange_and_noreplace_on_this_filesystem(tmp_path):
    for n, tag in (("s", "OLD"), ("s.new", "NEW"), ("m", "OLD"), ("m.new", "NEW"), ("n.new", "NEW")):
        (tmp_path / n).mkdir()
        (tmp_path / n / "f").write_text(tag)
    (tmp_path / "e").mkdir()
    (tmp_path / "e.new").mkdir()
    d = _dfd(tmp_path)
    try:
        X.rename2(d, "s.new", d, "s", X.EXCHANGE)
        assert (tmp_path / "s/f").read_text() == "NEW" and (tmp_path / "s.new/f").read_text() == "OLD"
        X.rename2(d, "n.new", d, "n", X.NOREPLACE)
        assert (tmp_path / "n/f").read_text() == "NEW" and not (tmp_path / "n.new").exists()
        for dst in ("m", "e"):            # onto a non-empty dir, and onto an EMPTY dir: EEXIST, nothing changed
            with pytest.raises(OSError) as ei:
                X.rename2(d, dst + ".new", d, dst, X.NOREPLACE)
            assert ei.value.errno == errno.EEXIST
        assert (tmp_path / "m/f").read_text() == "OLD" and list((tmp_path / "e").iterdir()) == []
    finally:
        os.close(d)


# --- the early probe and stale staging --------------------------------------------------------------------------

def _class_dir(tmp_path: Path, older: bool) -> Path:
    c = tmp_path / "operator"
    c.mkdir()
    if older:
        (c / S).mkdir()
        (c / S / "bundle").write_bytes(b"OLD")
    return c


def test_probe_real_exchange_leaves_nothing(tmp_path):
    c = _class_dir(tmp_path, older=True)
    for n in X.PROBE_NAMES:                       # a crash's pair is deleted first
        (c / n).mkdir()
        (c / n / "junk").write_text("x")
    before = _digest(c / S)
    log = Log()
    d = _dfd(c)
    try:
        X.probe_exchange(d, X.IMPORTER, _seams(log))
    finally:
        os.close(d)
    assert sorted(os.listdir(c)) == [S] and _digest(c / S) == before
    assert "probe exchange ok" in log.events and log.events.count("probe residue deleted .xchg-probe.a") == 1


@pytest.mark.parametrize("kind", ["EINVAL", "ENOTSUP", "ENOSYS", "symbol"])
@pytest.mark.parametrize("role", [X.IMPORTER, X.VERIFIER])
def test_probe_refused_is_exchange_unsupported_before_the_run(tmp_path, kind, role):
    c = _class_dir(tmp_path, older=True)
    before = _digest(c / S)
    d = _dfd(c)
    try:
        with pytest.raises(Refused) as ei:
            X.probe_exchange(d, role, _seams(rename2=_unsupported(kind)))
    finally:
        os.close(d)
    assert (ei.value.name, ei.value.detail) == (f"{role.failed}:publish", "exchange-unsupported")
    assert "does not support atomic exchange" in str(ei.value.__cause__)
    assert sorted(os.listdir(c)) == [S] and _digest(c / S) == before


def test_clear_staging_deletes_and_refuses_what_it_cannot(tmp_path):
    c = _class_dir(tmp_path, older=False)
    (c / f"{S}.new").mkdir()
    (c / f"{S}.new" / "f").write_text("stale")
    d = _dfd(c)
    try:
        X.clear_staging(d, f"{S}.new", X.IMPORTER, _seams())
        assert os.listdir(c) == []
        (c / f"{S}.new").mkdir()

        def stuck(dfd, name):
            raise OSError(errno.EPERM, "seam")
        with pytest.raises(Refused) as ei:
            X.clear_staging(d, f"{S}.new", X.IMPORTER, _seams(delete=stuck))
        assert (ei.value.name, ei.value.detail) == ("importer-failed:publish", "stale-staging")
        assert "L/export/<class>/<s>.new" in str(ei.value.__cause__)
    finally:
        os.close(d)


@pytest.mark.skipif(sys.platform != "darwin", reason="UF_IMMUTABLE is settable by the owner on darwin only")
def test_clear_staging_clears_an_immutable_flag_first(tmp_path):
    c = _class_dir(tmp_path, older=False)
    (c / f"{S}.new").mkdir()
    f = c / f"{S}.new" / "locked"
    f.write_text("x")
    os.chflags(f, stat.UF_IMMUTABLE)
    d = _dfd(c)
    try:
        X.clear_staging(d, f"{S}.new", X.IMPORTER, _seams())
    finally:
        os.close(d)
    assert os.listdir(c) == []


# --- the classifier ---------------------------------------------------------------------------------------------

def _obs(exit="exit", code=0, line=None, **kw):
    return X.RunObservation(exit=exit, code=code, line=line, **kw)


CLASSIFY_ROWS = [
    # (observation, expected name or None for SUCCESS)
    (_obs(line=_line()), None),
    (_obs(line=_line(result="adopt-nothing-new")), None),
    (_obs(line=_line(), host_short=True), None),                                   # the host reading names a failed run only
    (_obs(line=_line(), memory_killed=True), "adopt-cap:memory"),                  # a late oom_kill / watchdog kill
    (_obs(line=_line(), memory_unreadable=True), "export-image-unavailable"),
    (_obs(code=1, line=_line(result=None), memory_unreadable=True, host_short=True), "export-image-unavailable"),
    (_obs(code=1, line=_line(result=None, enospc=True), memory_killed=True, host_short=True), "adopt-cap:memory"),
    (_obs(code=1, line=_line(result=None, enospc=True), host_short=True), "adopt-space-exhausted"),
    (_obs(code=1, line=_line(result=None, enospc=True)), "adopt-work-cap"),
    (_obs(exit="time", code=None, line=_line(result=None, enospc=True)), "importer-failed:time"),
    (_obs(exit="signal", code=None, line=_line(result=None, enospc=True)), "importer-failed:signal"),
    (_obs(exit="time", code=None, line=b"x" * (X.RESULT_CAP + 1)), "importer-failed:time"),
    (_obs(exit="time", code=None, host_short=True), "adopt-space-exhausted"),
    (_obs(line=_line(enospc=True)), "importer-failed:result-malformed"),           # exit 0 + enospc true
    (_obs(line=_line(enospc=True), host_short=True), "adopt-space-exhausted"),
    (_obs(code=1, line=_line(result=None).replace(b'"enospc":false', b'"enospc":"true"')), "importer-failed:result-malformed"),
    (_obs(code=1, line=_line(result=None).replace(b'"enospc":false', b'"enospc":1')), "importer-failed:result-malformed"),
    (_obs(code=1, line=b"{" + b"x" * X.RESULT_CAP + b"\n"), "importer-failed:result-oversize"),
    (_obs(code=1, line=_line(result=None, terminal=False, refused="frame-bad:terminal")), "importer-failed:terminal-missing"),
    (_obs(code=1, line=None), "importer-failed:result-malformed"),
    (_obs(code=1, line=_line(result=None, refused="ref-head-loop")), "importer-failed:ref-head-loop"),
    (_obs(code=1, line=_line(result=None)), "importer-failed:exit"),
    (_obs(code=1, line=_line(result="delta")), "importer-failed:result-malformed"),  # a result on a non-zero exit
    (_obs(line=_line(result=None)), "importer-failed:result-malformed"),            # exit 0 without a result
    (_obs(line=_line(refused="ref-head-loop")), "importer-failed:result-malformed"),
    (_obs(line=_line() + _line()), "importer-failed:result-malformed"),
    (_obs(line=_line().replace(b'{"detail"', b'{ "detail"')), "importer-failed:result-malformed"),  # not canonical
    (_obs(line=_line().replace(b'"v":1', b'"v":true')), "importer-failed:result-malformed"),
    (_obs(code=1, line=_line(result=None, refused="bad\x1bname")), "importer-failed:result-malformed"),
]


@pytest.mark.parametrize("obs,expected", CLASSIFY_ROWS)
def test_classify_total_order(obs, expected):
    out = X.classify(obs)
    assert (None if out.success else out.name) == expected, out


def test_classify_terminal_missing_keeps_the_importers_name_as_detail():
    out = X.classify(_obs(code=1, line=_line(result=None, terminal=False, refused="frame-bad:path_b64")))
    assert (out.name, out.detail) == ("importer-failed:terminal-missing", "frame-bad:path_b64")


def test_the_writers_fallback_line_parses():
    raw = IMP.canon.canonical_bytes({"v": 1, "terminal": False, "enospc": False, "refused": "result-unbuildable",
                                     "result": None, "detail": "", "walk": {"entries": 0, "bytes": 0},
                                     "ignored_git_paths": None}) + b"\n"
    out = X.classify(_obs(code=1, line=raw))      # terminal false: the order names it terminal-missing first
    assert (out.name, out.detail) == ("importer-failed:terminal-missing", "result-unbuildable")


# --- the publish transaction ------------------------------------------------------------------------------------

def _bundle(*, prereq=False, refs=(f"refs/levain/{S}/head",), sig=b"# v2 git bundle\n") -> bytes:
    head = sig + (f"-{OID_B}\n".encode() if prereq else b"")
    head += b"".join(f"{OID_A} {r}\n".encode() for r in refs)
    return head + b"\nPACK-bytes-not-parsed-by-root"


def _work(tmp_path: Path, *, bundle: bytes | None, report: bytes = b'{"r":1}') -> Path:
    w = tmp_path / ".work"
    (w / "out").mkdir(parents=True)
    if bundle is not None:
        (w / "out" / "bundle").write_bytes(bundle)
    (w / "out" / "report.json").write_bytes(report)
    return w


CAPS = {"bundle": 1 << 20, "report.json": 1 << 16}


def _publish(tmp_path, outcome, *, seams=None, older=False, bundle=None, nothing_new=False, owner=None):
    w = _work(tmp_path, bundle=None if nothing_new else (bundle if bundle is not None else _bundle()))
    c = _class_dir(tmp_path, older=older)
    wd, cd = _dfd(w), _dfd(c)
    try:
        return c, X.publish_if_success(outcome, wd, cd, S, object_format="sha1",
                                       owner_uid=os.getuid() if owner is None else owner, caps=CAPS,
                                       seams=seams or _seams())
    finally:
        os.close(wd)
        os.close(cd)


def _ok(result="delta"):
    return X.classify(_obs(line=_line(result=result)))


def test_first_publish_uses_noreplace_and_answers_from_the_header(tmp_path):
    log = Log()
    c, (result, pub) = _publish(tmp_path, _ok(), seams=_seams(log))
    assert result == "unrelated" and not pub.exchanged
    assert sorted(os.listdir(c)) == [S] and sorted(os.listdir(c / S)) == ["bundle", "report.json"]
    assert pub.digests["bundle"] == hashlib.sha256(_bundle()).hexdigest()
    assert stat.S_IMODE(os.stat(c / S / "bundle").st_mode) == 0o640
    assert log.events == ["fsync bundle", "fsync report.json", "fsync staged", "noreplace", "fsync class"]


def test_republish_exchanges_then_deletes_the_older_tree_in_order(tmp_path):
    log = Log()
    c, (result, pub) = _publish(tmp_path, _ok(), seams=_seams(log), older=True, bundle=_bundle(prereq=True))
    assert result == "delta" and pub.exchanged
    assert sorted(os.listdir(c)) == [S] and (c / S / "bundle").read_bytes() == _bundle(prereq=True)
    assert log.events == ["fsync bundle", "fsync report.json", "fsync staged", "exchange", "fsync class", "delete older"]


def test_nothing_new_publishes_the_report_alone(tmp_path):
    c, (result, _pub) = _publish(tmp_path, _ok("adopt-nothing-new"), nothing_new=True)
    assert result == "adopt-nothing-new" and os.listdir(c / S) == ["report.json"]


@pytest.mark.parametrize("point,err,name", [
    ("copy:bundle", errno.ENOSPC, "adopt-space-exhausted"),
    ("fsync:report.json", errno.EDQUOT, "adopt-space-exhausted"),
    ("copy:report.json", errno.EIO, "importer-failed:publish"),
    ("commit", errno.EIO, "importer-failed:publish"),
])
@pytest.mark.parametrize("older", [False, True])
def test_a_failed_transaction_removes_staging_and_keeps_the_older_tree(tmp_path, point, err, name, older):
    c = tmp_path / "operator"
    with pytest.raises(Refused) as ei:
        _publish(tmp_path, _ok(), seams=_seams(fault=_fault_at(point, err)), older=older)
    assert ei.value.name == name
    assert sorted(os.listdir(c)) == ([S] if older else [])
    if older:
        assert (c / S / "bundle").read_bytes() == b"OLD"


@pytest.mark.parametrize("kind", ["EINVAL", "ENOTSUP", "ENOSYS", "symbol"])
@pytest.mark.parametrize("older", [False, True])
def test_a_refused_commit_call_fails_closed(tmp_path, kind, older):
    c = tmp_path / "operator"
    with pytest.raises(Refused) as ei:
        _publish(tmp_path, _ok(), seams=_seams(rename2=_unsupported(kind)), older=older)
    assert (ei.value.name, ei.value.detail) == ("importer-failed:publish", "exchange-unsupported")
    assert sorted(os.listdir(c)) == ([S] if older else [])


def test_noreplace_meeting_an_appeared_tree_is_exists(tmp_path):
    def appear(fd1, n1, fd2, n2, flag):
        os.mkdir(n2, dir_fd=fd2)                  # an <s> appears between the lookup and the call, an empty dir
        X.rename2(fd1, n1, fd2, n2, flag)
    c = tmp_path / "operator"
    with pytest.raises(Refused) as ei:
        _publish(tmp_path, _ok(), seams=_seams(rename2=appear))
    assert (ei.value.name, ei.value.detail) == ("importer-failed:publish", "exists")
    assert os.listdir(c) == [S] and os.listdir(c / S) == []


def test_a_failed_older_delete_still_commits(tmp_path):
    def no_delete(dfd, name):
        raise OSError(errno.EPERM, "seam")
    log = Log()
    c, (_r, pub) = _publish(tmp_path, _ok(), seams=_seams(log, delete=no_delete), older=True)
    assert pub.exchanged and sorted(os.listdir(c)) == [S, f"{S}.new"]
    assert (c / f"{S}.new" / "bundle").read_bytes() == b"OLD"
    assert log.events[-1].startswith("delete older failed")


@pytest.mark.parametrize("bundle", [
    _bundle(refs=("refs/heads/main",)),
    _bundle(refs=(f"refs/levain/{S}/head", f"refs/levain/{S}/head")),
    _bundle(refs=()),
    _bundle(sig=b"# v3 git bundle\n@object-format=sha256\n"),
    b"# v2 git bundle\n" + OID_A.encode() + b" refs/levain/s1/head\n",       # no blank line
])
def test_a_bundle_header_off_the_check_publishes_nothing(tmp_path, bundle):
    with pytest.raises(Refused) as ei:
        _publish(tmp_path, _ok(), bundle=bundle)
    assert ei.value.name == "bundle-header-mismatch"
    assert os.listdir(tmp_path / "operator") == []


@pytest.mark.parametrize("obs", [
    _obs(code=1, line=_line(result=None)),
    _obs(exit="signal", code=None, line=_line()),
    _obs(code=0, line=None),
    _obs(line=_line(), memory_killed=True),
    _obs(line=_line(enospc=True)),
])
def test_a_denied_run_with_outputs_present_publishes_nothing(tmp_path, obs):
    """The 5b2 carry (CONTRACT_EDITS, head 1022+8): out/bundle and out/report.json are present and well-formed, the run
    is not SUCCESS, and nothing reaches the class dir."""
    with pytest.raises(Refused) as ei:
        _publish(tmp_path, X.classify(obs), older=True)
    assert ei.value.name == X.classify(obs).name
    assert os.listdir(tmp_path / "operator") == [S] and (tmp_path / "operator" / S / "bundle").read_bytes() == b"OLD"


def test_output_shapes(tmp_path):
    w = _work(tmp_path, bundle=_bundle())
    c = _class_dir(tmp_path, older=False)

    def run(**kw):
        wd, cd = _dfd(w), _dfd(c)
        try:
            X.publish_if_success(kw.pop("outcome", _ok()), wd, cd, S, object_format="sha1",
                                 owner_uid=kw.pop("owner", os.getuid()), caps=kw.pop("caps", CAPS), seams=_seams())
        finally:
            os.close(wd)
            os.close(cd)

    def name_of(**kw):
        with pytest.raises(Refused) as ei:
            run(**kw)
        return ei.value.name

    assert name_of(owner=os.getuid() + 1) == "export-output-shape:owner"
    assert name_of(caps={"bundle": 4, "report.json": 1 << 16}) == "export-output-shape:size"
    os.link(w / "out" / "bundle", tmp_path / "second")
    assert name_of() == "export-output-shape:nlink"
    os.unlink(tmp_path / "second")
    assert name_of(outcome=_ok("adopt-nothing-new")) == "export-result-mismatch"   # a stray bundle
    os.rename(w / "out" / "bundle", tmp_path / "b")
    os.symlink(tmp_path / "b", w / "out" / "bundle")
    assert name_of() == "export-output-shape:symlink"
    os.unlink(w / "out" / "bundle")
    os.mkfifo(w / "out" / "bundle")
    assert name_of() == "export-output-shape:type"
    os.unlink(w / "out" / "bundle")
    assert name_of() == "export-result-mismatch"                                   # delta with no bundle
    os.rename(w / "out", tmp_path / "out2")
    os.symlink(tmp_path / "out2", w / "out")
    assert name_of() == "export-output-shape:symlink"
    assert os.listdir(c) == []
