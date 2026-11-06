"""The reaper's launchd log (``/var/log/com.levain.reaper.log``, its inherited stderr) is bounded by the program.

launchd opens ``StandardErrorPath`` once and hands it down as fd 2; newsyslog can only rename that file (it has no
copytruncate), after which the daemon keeps writing into the renamed inode, so the bound is the reaper's own, in
process, like ``pf.bound_log`` and the dispatch run server's ``_bound_log`` [measured by the newsyslog research lane,
RETURN_newsyslog, 1106-1]. Whether launchd's own open is ``O_APPEND`` is what only a real Mac shows (the fork test
below pins the property the design leans on, on a descriptor opened that way)."""
from __future__ import annotations

import os
import sys

import pytest

from levain.slot import reaper as RP
from levain.slot import units
from levain.slot.services import reaper as SR


def _log_fd(tmp_path, size, name="reaper.log"):
    """A log as launchd opens it (appending), already ``size`` bytes long (sparse: the bytes are never read)."""
    path = tmp_path / name
    with open(path, "wb") as f:
        f.truncate(size)
    return path, os.open(path, os.O_WRONLY | os.O_APPEND)


def test_an_over_cap_log_is_emptied_and_an_at_cap_one_is_left_alone(tmp_path):
    path, fd = _log_fd(tmp_path, RP.LOG_MAX_BYTES + 1)
    try:
        RP.bound_log(fd, path)
        assert path.stat().st_size == 0
    finally:
        os.close(fd)
    path, fd = _log_fd(tmp_path, RP.LOG_MAX_BYTES)
    try:
        RP.bound_log(fd, path)
        assert path.stat().st_size == RP.LOG_MAX_BYTES       # at the cap, not past it
    finally:
        os.close(fd)


def test_a_pipe_and_a_closed_descriptor_are_left_alone():
    r, w = os.pipe()
    try:
        RP.bound_log(w)                                      # not a regular file: no truncate, no raise
    finally:
        os.close(r)
        os.close(w)
    RP.bound_log(w)                                          # closed: the OSError is swallowed


def test_only_the_units_own_log_is_trimmed(tmp_path, monkeypatch):
    """A regular file that is not the unit's log (a shell redirect over fd 2) is never emptied."""
    path, fd = _log_fd(tmp_path, RP.LOG_MAX_BYTES + 1)
    other = tmp_path / "somewhere_else.log"
    other.write_bytes(b"x")
    try:
        RP.bound_log(fd, other)                              # fd is not that file (another inode)
        assert path.stat().st_size == RP.LOG_MAX_BYTES + 1
        RP.bound_log(fd, tmp_path / "absent.log")            # the expected file is missing: nothing to trim
        assert path.stat().st_size == RP.LOG_MAX_BYTES + 1
        monkeypatch.setattr(RP, "_expected_log", lambda: other)
        RP.bound_log(fd)                                     # the default path is the module's, not the fd's
        assert path.stat().st_size == RP.LOG_MAX_BYTES + 1
        monkeypatch.setattr(RP, "_expected_log", lambda: path)
        RP.bound_log(fd)
        assert path.stat().st_size == 0
    finally:
        os.close(fd)


def test_the_expected_log_is_the_path_the_unit_gives_launchd():
    assert RP._expected_log() == units.launchd_log_path(units.by_name("reaper"))


def test_the_cap_is_the_dispatch_run_servers():
    from levain.slot.services import dispatch_run as D
    assert RP.LOG_MAX_BYTES == D.LOG_MAX_BYTES == 4 << 20


def test_both_log_sinks_bound_the_log_before_they_write(monkeypatch):
    events = []

    class Spy:
        def write(self, text):
            events.append("write")
            return len(text)

        def flush(self):
            events.append("flush")

    monkeypatch.setattr(RP, "bound_log", lambda fd=2, expected=None: events.append("bound"))
    monkeypatch.setattr(sys, "stderr", Spy())
    RP._log_to_stderr("a line")
    assert events[:2] == ["bound", "write"], events
    events.clear()
    SR._log("a line")
    assert events[:2] == ["bound", "write"], events


def test_the_service_bounds_its_log_once_at_startup_after_the_root_check(monkeypatch):
    events = []
    monkeypatch.setattr(RP, "bound_log", lambda fd=2, expected=None: events.append("bound"))
    monkeypatch.setattr(SR, "serve", lambda seams: events.append("serve") or 0)
    monkeypatch.setattr(SR.H, "Seams", lambda: None)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert SR.main([]) == 77
    assert events == []                                      # a refused non-root start touches nothing
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert SR.main([]) == 0
    assert events == ["bound", "serve"]                      # a KeepAlive restart starts from a bounded file


def test_a_trim_by_one_process_leaves_no_hole_for_the_others_on_one_shared_descriptor(tmp_path, monkeypatch):
    """The reaper is three processes (main, worker, revoker) on ONE open file description. One trims past the cap; the
    other, which wrote before the trim and writes after it, must neither leave a sparse NUL hole nor lose its later lines."""
    path, fd = _log_fd(tmp_path, RP.LOG_MAX_BYTES + 1)
    monkeypatch.setattr(RP, "_expected_log", lambda: path)
    to_parent_r, to_parent_w = os.pipe()
    to_child_r, to_child_w = os.pipe()
    pid = os.fork()
    if pid == 0:                                             # the other process: never returns into pytest
        code = 1
        try:
            os.close(to_parent_r)
            os.close(to_child_w)
            for i in range(10):
                os.write(fd, b"child before %d\n" % i)
            os.write(to_parent_w, b"x")                      # "I have written; trim now"
            os.read(to_child_r, 1)                           # "I have trimmed; write now"
            for i in range(10):
                os.write(fd, b"child after %d\n" % i)
            code = 0
        finally:
            os._exit(code)
    os.close(to_parent_w)
    os.close(to_child_r)
    try:
        assert os.read(to_parent_r, 1) == b"x"
        before = path.stat().st_size
        assert before > RP.LOG_MAX_BYTES                     # the child's lines landed after the cap
        RP.bound_log(fd)
        assert path.stat().st_size == 0
        for i in range(10):
            os.write(fd, b"parent after %d\n" % i)
        os.write(to_child_w, b"x")
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    finally:
        os.close(fd)
        os.close(to_parent_r)
        os.close(to_child_w)
    data = path.read_bytes()
    assert b"\0" not in data                                 # no sparse hole at the old offset
    lines = data.decode().splitlines()
    assert sorted(lines) == sorted([f"child after {i}" for i in range(10)] + [f"parent after {i}" for i in range(10)])
