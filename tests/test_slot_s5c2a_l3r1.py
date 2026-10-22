"""5c-ii-a L3 round 1 fixes F1-F7 (project_memory/1022-18/adopt/L3_5c2a_r1_TRIAGE.md; ruling A1b), each row written
from the finding before its fix."""
from __future__ import annotations

import errno
import os
import socket
import stat
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import export_run as X
from levain.slot import importer as IMP
from levain.slot.runtime_grammar import refusal_line

S = "s1"
OID_A = "a" * 40
CAPS = {"bundle": 1 << 20, "report.json": 1 << 16}


def _line(*, terminal=True, refused=None, result="delta", enospc=False) -> bytes:
    return IMP.result_line(IMP.Progress(terminal=terminal, entries=3, bytes=10), refused=refused, detail="",
                           enospc=enospc, result=result)


def _bundle() -> bytes:
    return b"# v2 git bundle\n" + f"{OID_A} refs/levain/{S}/head\n".encode() + b"\nPACK"


def _dfd(p: Path) -> int:
    return os.open(p, os.O_RDONLY | os.O_DIRECTORY)


class Log:
    def __init__(self):
        self.events = []

    def __call__(self, e):
        self.events.append(e)


def _setup(tmp_path, *, older=False):
    w = tmp_path / ".work"
    (w / "out").mkdir(parents=True)
    (w / "out" / "bundle").write_bytes(_bundle())
    (w / "out" / "report.json").write_bytes(b'{"r":1}')
    c = tmp_path / "operator"
    c.mkdir()
    if older:
        (c / S).mkdir()
        (c / S / "bundle").write_bytes(b"OLD")
    return w, c


def _ok():
    return X.classify(X.RunObservation(exit="exit", code=0, line=_line()))


def _publish(tmp_path, *, seams, older=False, role=None, outcome=None):
    w, c = _setup(tmp_path, older=older)
    wd, cd = _dfd(w), _dfd(c)
    kw = {} if role is None else {"role": role}
    try:
        return c, X.publish_if_success(outcome or _ok(), wd, cd, S, object_format="sha1", owner_uid=os.getuid(),
                                       caps=CAPS, seams=seams, **kw)
    finally:
        os.close(wd)
        os.close(cd)


def _open_fds():
    return set(int(n) for n in os.listdir("/dev/fd"))


# --- F1: the published modes do not follow root's umask -----------------------------------------------------------

def test_f1_publish_under_umask_077_still_gives_0750_and_0640(tmp_path):
    old = os.umask(0o077)
    try:
        c, _ = _publish(tmp_path, seams=X.PublishSeams())
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(c / S).st_mode) == 0o750
    for n in ("bundle", "report.json"):
        assert stat.S_IMODE(os.stat(c / S / n).st_mode) == 0o640


# --- F2 / A1b: the wire name is the importer's BASE name, the full refusal rides in the detail --------------------

@pytest.mark.parametrize("refused,wire", [
    ("frame-bad:path_b64", "importer-failed:frame-bad"),
    ("frame-bad:trailing", "importer-failed:frame-bad"),
    ("fsck:badTimezone", "importer-failed:fsck"),
    ("ref-name-bad:refs/heads/x%00y", "importer-failed:ref-name-bad"),
    ("no-ref", "importer-failed:no-ref"),
])
def test_f2_a_qualified_importer_refusal_passes_the_refusal_line(refused, wire):
    out = X.classify(X.RunObservation(exit="exit", code=1, line=_line(refused=refused, result=None)))
    assert (out.name, out.detail) == (wire, refused)
    assert refusal_line(Refused(out.name, out.detail)) == '{"ok":false,"refused":"%s"}\n' % wire


@pytest.mark.parametrize("refused", ["a--b", "x-", "a" * 60 + ":q"])
def test_f2_a_base_the_grammar_cannot_carry_maps_through_the_table(refused):
    out = X.classify(X.RunObservation(exit="exit", code=1, line=_line(refused=refused, result=None)))
    assert out.name == f"importer-failed:{X.WIRE_FALLBACK}" and out.detail == refused
    assert '"internal"' not in refusal_line(Refused(out.name, out.detail))


def test_f2_terminal_missing_keeps_the_full_refusal_in_the_detail():
    out = X.classify(X.RunObservation(exit="exit", code=1, line=_line(terminal=False, refused="frame-bad:path_b64",
                                                                      result=None)))
    assert (out.name, out.detail) == ("importer-failed:terminal-missing", "frame-bad:path_b64")


# --- F3: a deeply nested line is malformed, never a crash ---------------------------------------------------------

@pytest.mark.parametrize("depth", [32_000, 10_000, 40])
def test_f3_a_deep_line_is_result_malformed(depth):
    raw = b'{"a":' + b"[" * depth + b"]" * depth + b"}\n"
    assert len(raw) <= IMP.RESULT_CAP
    out = X.classify(X.RunObservation(exit="exit", code=0, line=raw))
    assert out.name == "importer-failed:result-malformed"


# --- F4: a special file at an output is a shape refusal, and no fd leaks ------------------------------------------

@pytest.mark.parametrize("name", ["bundle", "report.json"])
def test_f4_a_socket_at_an_output_is_shape_type_and_leaks_nothing(tmp_path, monkeypatch, name):
    w, _c = _setup(tmp_path)
    (w / "out" / name).unlink()
    monkeypatch.chdir(w / "out")                           # AF_UNIX paths are short: bind by a relative name
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(name)
    wd = _dfd(w)
    before = _open_fds()
    try:
        with pytest.raises(Refused) as ei:
            X.check_outputs(wd, ("bundle", "report.json"), (), owner_uid=os.getuid(), caps=CAPS)
        assert ei.value.name == "export-output-shape:type"
        assert _open_fds() == before
    finally:
        os.close(wd)
        s.close()


def test_f4_an_unexpected_open_error_closes_what_was_opened(tmp_path, monkeypatch):
    w, _c = _setup(tmp_path)
    real_open = os.open

    def flaky(path, flags, *a, **k):
        if path == "report.json":
            raise OSError(errno.EIO, "seam")
        return real_open(path, flags, *a, **k)
    wd = _dfd(w)
    before = _open_fds()
    monkeypatch.setattr(X.os, "open", flaky)
    try:
        with pytest.raises(OSError):
            X.check_outputs(wd, ("bundle", "report.json"), (), owner_uid=os.getuid(), caps=CAPS)
    finally:
        monkeypatch.undo()
        after = _open_fds()
        os.close(wd)
    assert after == before


# --- F5: nothing after the commit point turns into an uncommitted failure -----------------------------------------

@pytest.mark.parametrize("older,event", [(False, "noreplace"), (True, "exchange")])
def test_f5_a_raising_seam_after_the_commit_keeps_the_commit(tmp_path, older, event):
    def log(e):
        if e == event:
            raise OSError(errno.EIO, "seam")
    c, (_r, pub) = _publish(tmp_path, seams=X.PublishSeams(log=log), older=older)
    assert (c / S / "bundle").read_bytes() == _bundle()     # the new tree is <s>; the older was never deleted as staging
    assert pub.warnings and any("after the commit" in w for w in pub.warnings)


def test_f5_the_staged_dir_fd_is_closed_before_the_commit_call(tmp_path):
    seen = []

    def rename2(fd1, n1, fd2, n2, flag):
        st = os.stat(n1, dir_fd=fd1, follow_symlinks=False)
        held = []
        for fd in _open_fds():
            try:
                f = os.fstat(fd)
            except OSError:
                continue
            if (f.st_dev, f.st_ino) == (st.st_dev, st.st_ino):
                held.append(fd)
        seen.append(held)
        X.rename2(fd1, n1, fd2, n2, flag)
    _publish(tmp_path, seams=X.PublishSeams(rename2=rename2))
    assert seen == [[]]


# --- F6: the probe fails closed when its scratch names cannot be cleared ------------------------------------------

def _probe(tmp_path, delete):
    c = tmp_path / "operator"
    c.mkdir()
    cd = _dfd(c)
    try:
        X.probe_exchange(cd, X.IMPORTER, X.PublishSeams(delete=delete))
    finally:
        os.close(cd)
    return c


def test_f6_a_residue_that_cannot_be_deleted_is_a_publish_refusal(tmp_path):
    def delete(dfd, name):
        raise OSError(errno.EPERM, "seam")
    with pytest.raises(Refused) as ei:
        _probe(tmp_path, delete)
    assert ei.value.name == "importer-failed:publish"


def test_f6_a_cleanup_that_leaves_a_name_behind_is_a_publish_refusal(tmp_path):
    calls = []

    def delete(dfd, name):
        calls.append(name)
        if len(calls) > 2 and name == X.PROBE_NAMES[0]:
            return False                                       # claims done, removes nothing
        return X._store_delete(dfd, name)
    with pytest.raises(Refused) as ei:
        _probe(tmp_path, delete)
    assert ei.value.name == "importer-failed:publish"


# --- F7: the gate takes its role ------------------------------------------------------------------------------------

def test_f7_the_gate_answers_with_the_role_it_is_given(tmp_path):
    # 5c-ii-b L3 r1 B-5: a verifier SUCCESS no longer reaches the importer-shaped transaction (verifier-unbuilt);
    # the role's names are proven on the transaction itself and on the denial.
    def fault(p):
        if p == "copy:bundle":
            raise OSError(errno.ENOSPC, "seam")
    w, c = _setup(tmp_path, older=False)
    wd, cd = _dfd(w), _dfd(c)
    fds = X.check_outputs(wd, ("bundle",), (), owner_uid=os.getuid(), caps=CAPS)
    try:
        with pytest.raises(Refused) as ei:
            X.transact(cd, S, [("bundle", fds["bundle"])], X.VERIFIER, X.PublishSeams(fault=fault))
    finally:
        for fd in (wd, cd, *fds.values()):
            os.close(fd)
    assert ei.value.name == "preview-space-exhausted"
    denied = X.Outcome(False, None, "", None)
    with pytest.raises(Refused) as ei2:
        _publish(tmp_path / "b" if (tmp_path / "b").mkdir() is None else None, seams=X.PublishSeams(),
                 role=X.VERIFIER, outcome=denied)
    assert ei2.value.name == "verifier-failed:exit"
