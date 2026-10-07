"""The team-view registry and the cockpit's /team_views.json back-link. A temp HOME/LEVAIN_HOME throughout: these
never read or write the real ~/.levain. The registry's liveness is an OS file lock, so most of what is asserted here
is run against real file descriptors and, where a process boundary is the point, real processes."""
import errno
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from levain.team import registry as R
from levain.team import view as V
from tests.test_team_view import TEAM as _TEAM
from tests.test_team_view import _Stub

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the lifetime lock is POSIX-only")


@pytest.fixture(autouse=True)
def levain_home(tmp_path, monkeypatch):
    # autouse: no test in this file can reach the real ~/.levain (LEVAIN_HOME and HOME both point into tmp_path)
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    assert str(R.registry_dir()).startswith(str(tmp_path))
    return tmp_path / "home"


@pytest.fixture
def pub():
    """Publish entries in this process; every registration is released at teardown."""
    made = []

    def make(project="ledgerline", port=None, repo="/work/ledgerline"):
        r = R.register(repo, f"http://127.0.0.1:{port or 41000 + len(made)}/", project)
        made.append(r)
        return r
    yield make
    for r in made:
        r.unpublish()
        r.close()


def _names():
    d = R.registry_dir()
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def _forge(name, body=b"{}", hold=None):
    """A file in the registry with the live-entry name; ``hold`` ('EX') also takes the lock and returns the fd."""
    import fcntl
    d = R.registry_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(body)
    if hold == "EX":
        fd = os.open(d / name, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    return None


def _hex(i):
    return f"{i:032x}"


class _GL(_Stub):
    class repo:
        toplevel = Path("/work/ledgerline")

    def team(self):
        return _TEAM


def _serve_with(monkeypatch, serve_forever):
    """serve() against the stub ledger with serve_forever replaced; returns the server it built."""
    real = V.make_view_server
    seen = {}

    def make(*a, **k):
        h = real(*a, **k)
        h.serve_forever = serve_forever(h, seen)
        seen["h"] = h
        return h
    monkeypatch.setattr(V, "make_view_server", make)
    return seen


def _interrupt(h, seen):
    def sf():
        seen["files"] = _names()
        raise KeyboardInterrupt
    return sf


def test_home_follows_levain_home_then_HOME(monkeypatch, tmp_path):
    assert R.home() == tmp_path / "home"
    monkeypatch.delenv("LEVAIN_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert R.home() == tmp_path / "h" / ".levain"


def test_entry_holds_only_four_fields_and_nothing_per_person(pub):
    r = pub()
    raw = json.loads(r.path.read_text())
    assert set(raw) == {"v", "repo", "url", "project", "started"} and raw["v"] == 2
    assert R._NAME_RE.fullmatch(r.name) and (R.registry_dir() / r.name).stat().st_mode & 0o777 == 0o600
    assert R.registry_dir().stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "https://127.0.0.1:1/", "http://example.com:80/",
                                 "http://[::1]:7450/", "http://0.0.0.0:7450/", "http://127.0.0.1/", "file:///etc/passwd",
                                 "http://u:p@127.0.0.1:7450/", None, 5])
def test_only_loopback_http_with_a_port_is_ever_registered_or_listed(bad):
    assert R.loopback_http_url(bad) is None
    with pytest.raises(ValueError):
        R.register("/r", bad, "p")
    assert _names() == []                                   # nothing was even created
    # a hand-written locked entry with that URL is never listed
    fd = _forge(f"lock1-{_hex(1)}.json", json.dumps({"v": 2, "repo": "/r", "url": bad, "project": "p",
                                                      "started": "t"}).encode(), hold="EX")
    try:
        assert R.live_views() == []
    finally:
        os.close(fd)


def test_register_refuses_a_url_without_an_explicit_port():
    with pytest.raises(ValueError):
        R.register("/r", "http://127.0.0.1/", "p")
    assert _names() == []


# ---- liveness is the lock -------------------------------------------------------------------------------------

def test_a_published_view_is_listed_and_unpublish_or_close_ends_it(pub):
    a, b = pub("alpha"), pub("beta")
    assert [v["project"] for v in R.live_views()] == ["alpha", "beta"]
    a.unpublish()
    assert [v["project"] for v in R.live_views()] == ["beta"]
    b.close()                                               # the lock released, the file still there: dead
    assert b.name in _names() and R.live_views() == []
    a.unpublish(); a.close(); a.close()                     # idempotent


def test_t4_listing_makes_no_network_connection(pub, monkeypatch):
    pub("ledgerline")

    def boom(*a, **k):
        raise AssertionError("the listing opened a socket")
    monkeypatch.setattr(socket, "socket", boom)
    assert [v["project"] for v in R.live_views()] == ["ledgerline"]


def test_t2_the_lock_is_taken_before_the_entry_becomes_visible(monkeypatch):
    """At rename time, a second fd on the temp file must already be unable to take a shared lock."""
    import fcntl
    real = os.rename
    seen = {}

    def rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        fd = os.open(src, os.O_RDONLY, dir_fd=src_dir_fd)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            seen["locked_at_rename"] = True
        finally:
            os.close(fd)
        return real(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
    monkeypatch.setattr(R.os, "rename", rename)
    r = R.register("/r", "http://127.0.0.1:41999/", "p")
    try:
        assert seen == {"locked_at_rename": True}
    finally:
        r.unpublish(); r.close()


def test_t2b_a_pruner_in_flight_never_reads_as_a_live_view(pub, monkeypatch):
    import fcntl
    dead = f"lock1-{_hex(5)}.json"
    body = json.dumps({"v": 2, "repo": "/r", "url": "http://127.0.0.1:41500/", "project": "ghost",
                       "started": "t"}).encode()
    _forge(dead, body)
    assert R.live_views() == []                              # nobody holds it: dead
    # a pruner has taken its shared lock and has not yet unlinked: a reader still says dead
    fd = os.open(R.registry_dir() / dead, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    try:
        assert R.live_views() == []
    finally:
        os.close(fd)
    # a starting view never prunes a live entry, and does prune the dead one
    live = pub("alive")
    R.prune_dead()
    assert live.name in _names() and dead not in _names()
    assert [v["project"] for v in R.live_views()] == ["alive"]
    # a publisher mid-unpublish (the file unlinked while a reader holds an fd) reads as dead
    d_fd = R._open_dir(create=False)
    try:
        opened = R._open_entry(d_fd, live.name)
        live.unpublish()
        monkeypatch.setattr(R, "_open_entry", lambda *_, **__: opened)
        assert R._read_live(d_fd, live.name) is None
    finally:
        os.close(d_fd)


def test_only_a_publisher_ever_takes_the_exclusive_lock(pub, monkeypatch):
    """Readers and pruners must use LOCK_SH|LOCK_NB: an exclusive lock held by a pruner would read as a live view."""
    import fcntl
    real = fcntl.flock
    ops = []
    monkeypatch.setattr(fcntl, "flock", lambda fd, op: (ops.append(op), real(fd, op))[1])
    _forge(f"lock1-{_hex(7)}.json", b"{}")
    pub("alive")
    ops.clear()
    R.live_views()
    R.prune_dead()
    assert ops and set(ops) == {fcntl.LOCK_SH | fcntl.LOCK_NB}


def test_prune_never_sweeps_a_fresh_temp_file(pub):
    # A publisher creates its temp, then locks it: a sweep in between unregistered a starting view (lane run,
    # 2026-10-05), so a temp younger than the floor is left alone whatever its lock state.
    d = R.registry_dir()
    d.mkdir(parents=True)
    temps = [f".lock1-{'a' * 32}.tmp", f".selftest-{'b' * 16}.tmp"]
    for n in temps:
        (d / n).write_text("{half")
    R.prune_dead()
    assert sorted(_names()) == sorted(temps)


def test_prune_sweeps_an_old_temp_nobody_holds_and_keeps_one_still_locked(pub):
    # A view SIGKILLed before its rename leaves its temp; without a sweep every cockpit scan pays for it forever.
    import fcntl
    d = R.registry_dir()
    d.mkdir(parents=True)
    dead = [f".lock1-{'a' * 32}.tmp", f".selftest-{'b' * 16}.tmp"]
    held = f".lock1-{'c' * 32}.tmp"
    old = time.time() - 3600                    # an hour: past any floor
    for n in dead + [held]:
        (d / n).write_text("{half")
        os.utime(d / n, (old, old))
    fd = os.open(d / held, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)          # a publisher stalled after locking, before its rename
        R.prune_dead()
        assert _names() == [held]
    finally:
        os.close(fd)


def _forked_child_keeps_the_lock(monkeypatch, pause_in, release) -> list:
    """Fork while another thread is paused inside the registry, let the parent drop its view without unpublishing
    (a crash: the lock must die with the process), and list. The child holds whatever fds it inherited until the
    listing is done. Returns the listing."""
    r_end, w_end = os.pipe()
    ready_r, ready_w = os.pipe()
    assert pause_in.wait(5)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)   # fork with threads alive is the point of the test
        pid = os.fork()
    if pid == 0:                                                # the child: hold the inherited fds, then leave
        try:
            os.close(w_end)
            os.write(ready_w, b"r")                             # the at-fork handlers have run by now
            os.read(r_end, 1)
        finally:
            os._exit(0)
    os.close(r_end)
    os.close(ready_w)
    try:
        # Wait for the child to be past its at-fork handlers: until then even a fixed child holds its copy for a
        # moment, and on a loaded machine the parent could list inside that moment.
        assert os.read(ready_r, 1) == b"r"
        release()
        return R.live_views()
    finally:
        os.write(w_end, b"x")
        os.close(w_end)
        os.close(ready_r)
        os.waitpid(pid, 0)


def test_a_fork_right_after_the_lock_fd_is_opened_does_not_leak_the_lock(monkeypatch):
    # codex 10-07: the lock fd existed before anything tracked it. This pauses INSIDE os.open, after the fd exists and
    # before it can be recorded: with the open and the record under the fork lock, the fork waits the pause out and
    # the child closes the fd; without that lock, the child inherits an fd nobody tracks.
    import os as real_os
    paused, go = threading.Event(), threading.Event()

    class OsShim:
        def __getattr__(self, name):
            return getattr(real_os, name)

        @staticmethod
        def open(path, flags, *a, **k):
            fd = real_os.open(path, flags, *a, **k)
            if str(path).startswith(".lock1-") and str(path).endswith(".tmp"):
                paused.set()
                go.wait(1.0)                        # fixed code holds the fork lock here, so the fork waits this out
            return fd
    monkeypatch.setattr(R, "os", OsShim())
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("reg", R.register("/w", "http://127.0.0.1:43996/", "o")))
    t.start()

    def release():
        go.set()
        t.join(5)
        monkeypatch.setattr(R, "os", real_os)
        out["reg"].close()                          # no unpublish: the entry stays, only its lock says dead
    assert _forked_child_keeps_the_lock(monkeypatch, paused, release) == []


def test_a_fork_between_opening_the_lock_fd_and_registering_it_does_not_leak_the_lock(monkeypatch):
    # The fd stays tracked (in _PENDING) from its open until a Registration owns it: this pauses in flock, between.
    import fcntl as real_fcntl
    paused, go = threading.Event(), threading.Event()
    calls = {"ex": 0}

    class Shim:
        LOCK_EX, LOCK_SH, LOCK_NB = real_fcntl.LOCK_EX, real_fcntl.LOCK_SH, real_fcntl.LOCK_NB

        @staticmethod
        def flock(fd, op):
            if op == real_fcntl.LOCK_EX:
                calls["ex"] += 1
                if calls["ex"] == 2:                # the first is the self-test; the second is the entry's lock fd
                    paused.set()
                    go.wait(5)
            return real_fcntl.flock(fd, op)
    monkeypatch.setattr(R, "fcntl", Shim)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("reg", R.register("/w", "http://127.0.0.1:43998/", "f")))
    t.start()

    def release():
        go.set()
        t.join(5)
        out["reg"].close()                          # no unpublish: the entry stays, only its lock says dead
    assert _forked_child_keeps_the_lock(monkeypatch, paused, release) == []


def test_a_fork_between_forgetting_a_registration_and_closing_its_fd_does_not_leak_the_lock(monkeypatch):
    # The same window on the way out: close() used to forget the Registration, then close its fds, so a fork in
    # between copied an fd nobody tracked any more.
    reg = R.register("/w", "http://127.0.0.1:43997/", "g")
    paused, go = threading.Event(), threading.Event()

    class Pausing(set):
        def discard(self, x):
            super().discard(x)
            if x is reg:
                paused.set()
                go.wait(1.0)                        # fixed code holds the fork lock here, so the fork waits this out
    monkeypatch.setattr(R, "_LIVE", Pausing(R._LIVE))
    t = threading.Thread(target=reg.close)
    t.start()

    def release():
        go.set()
        t.join(5)
    assert _forked_child_keeps_the_lock(monkeypatch, paused, release) == []


def test_prune_leaves_other_grammars_alone(pub):
    d = R.registry_dir()
    d.mkdir(parents=True)
    keep = ["4242-7000.json", "lock2-" + "b" * 32 + ".json", "notes.txt"]
    for n in keep:
        (d / n).write_text("{half")
    gone = f"lock1-{_hex(9)}.json"
    (d / gone).write_text("{half")                          # unlocked file in our grammar: dead, removed
    R.prune_dead()
    assert sorted(_names()) == sorted(keep)


def test_t2d_a_fifo_a_symlink_and_an_oversized_locked_file_are_skipped_without_blocking(pub):
    d = R.registry_dir()
    d.mkdir(parents=True)
    os.mkfifo(d / f"lock1-{_hex(1)}.json")
    (d / "target").write_text("x")
    os.symlink(d / "target", d / f"lock1-{_hex(2)}.json")
    fd = _forge(f"lock1-{_hex(3)}.json", b'{"v": 2, "pad": "' + b"x" * 10000 + b'"}', hold="EX")
    pub("real")
    try:
        t = time.monotonic()
        assert [v["project"] for v in R.live_views()] == ["real"]
        assert time.monotonic() - t < 0.5
        R.prune_dead()                                       # also must not block on the FIFO
    finally:
        os.close(fd)


def test_t2f_a_filesystem_whose_flock_never_conflicts_refuses_to_register(monkeypatch, capsys):
    import fcntl
    monkeypatch.setattr(fcntl, "flock", lambda *a, **k: None)
    with pytest.raises(R.RegistryUnavailable, match="does not support"):
        R.register("/r", "http://127.0.0.1:41800/", "p")
    assert _names() == []                                    # nothing left behind, not even the self-test file
    _serve_with(monkeypatch, _interrupt)
    assert V.serve(_GL(), host="127.0.0.1", port=0, recheck_days=30, ack_flag=3) == 0       # the view still serves
    assert "not registered with the cockpit" in capsys.readouterr().err


def test_t2f_a_symlinked_or_foreign_registry_directory_is_refused(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    home = R.home()
    home.mkdir(parents=True)
    os.symlink(real, home / "team-views")
    with pytest.raises(OSError):                             # O_NOFOLLOW: a symlinked directory is never used
        R.register("/r", "http://127.0.0.1:41801/", "p")
    assert list(real.iterdir()) == [] and R.live_views() == []


def test_t7_flock_failing_with_enolck_keeps_the_view_serving(monkeypatch, capsys):
    import fcntl

    def enolck(*a, **k):
        raise OSError(errno.ENOLCK, "no locks available")
    monkeypatch.setattr(fcntl, "flock", enolck)
    _serve_with(monkeypatch, _interrupt)
    assert V.serve(_GL(), host="127.0.0.1", port=0, recheck_days=30, ack_flag=3) == 0
    assert "not registered with the cockpit" in capsys.readouterr().err
    assert _names() == []


def test_t2e_two_views_cannot_bind_one_port():
    first = V.make_view_server(_Stub(), port=0)
    try:
        assert V._ViewServer.allow_reuse_port is False
        with pytest.raises(OSError):
            V.make_view_server(_Stub(), port=first.server_address[1])
    finally:
        first.server_close()


def test_t5_junk_old_grammar_and_poisoned_names_are_ignored_and_never_break_the_loop(pub):
    d = R.registry_dir()
    d.mkdir(parents=True)
    for n in ("99999999999999999999-1.json", "9" * 200 + "-1.json", "0-7450.json", "-5-1.json", "1234-7450.json",
              "lock1-xyz.json", "lock1-" + "A" * 32 + ".json"):
        (d / n).write_text(json.dumps({"v": 1, "repo": "/r", "url": "http://127.0.0.1:1/", "project": "x",
                                       "pid": os.getpid(), "started": "t", "nonce": "a" * 32}))
    fds = [_forge(f"lock1-{_hex(i)}.json", b"not json", hold="EX") for i in range(1, 25)]   # sort before real ones
    fds.append(_forge(f"lock1-{_hex(99)}.json", json.dumps({"v": 3, "repo": "/r", "url": "http://127.0.0.1:1/",
                                                           "project": "newer", "started": "t"}).encode(), hold="EX"))
    pub("ledgerline")
    try:
        assert [v["project"] for v in R.live_views()] == ["ledgerline"]
    finally:
        for fd in fds:
            os.close(fd)


def test_the_cap_applies_after_filtering_in_name_order_and_says_so(pub):
    """Which views show past the cap must not depend on directory order, and a capped list is not complete."""
    regs = [pub(f"p{i:02d}", port=42000 + i) for i in range(R.MAX_VIEWS + 3)]
    first = sorted(regs, key=lambda r: r.name)[:R.MAX_VIEWS]
    want = sorted(f"http://127.0.0.1:{42000 + regs.index(r)}/" for r in first)
    for _ in range(3):
        views, truncated = R.live_views_scan(budget=5)
        assert sorted(v["url"] for v in views) == want and truncated is True
    for r in regs[R.MAX_VIEWS:]:
        r.unpublish()
    views, truncated = R.live_views_scan(budget=5)
    assert len(views) == R.MAX_VIEWS and truncated is False      # exactly the cap, nothing left unjudged


def test_the_budget_is_checked_before_the_name_filter(pub):
    d = R.registry_dir()
    d.mkdir(parents=True)
    for i in range(50):
        (d / f"junk-{i}").write_bytes(b"")
    assert R.live_views_scan(budget=0) == ([], True)            # junk alone still spends the budget


def test_a_directory_read_error_part_way_reports_truncated(pub, monkeypatch):
    pub("ledgerline")
    real = R.os.scandir

    class _Broken:
        def __init__(self, fd):
            self._it = real(fd)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._it.close()

        def __iter__(self):
            yield next(iter(self._it))
            raise OSError(5, "Input/output error")
    monkeypatch.setattr(R.os, "scandir", _Broken)
    views, truncated = R.live_views_scan(budget=5)
    assert truncated is True and len(views) <= 1


def test_an_entry_that_cannot_be_judged_makes_the_list_incomplete(pub, monkeypatch):
    """A transient open failure (fd pressure, EIO, EACCES) says nothing about the view: it is not judged dead."""
    pub("ledgerline")
    real = R.os.open

    def emfile(path, flags, *a, **k):
        if isinstance(path, str) and path.startswith("lock1-"):
            raise OSError(errno.EMFILE, "Too many open files")
        return real(path, flags, *a, **k)
    monkeypatch.setattr(R.os, "open", emfile)
    assert R.live_views_scan(budget=5) == ([], True)


def test_a_long_project_or_repo_is_published_at_the_shown_length_and_listed(pub):
    """An entry over MAX_ENTRY_BYTES was published, then skipped by every reader: a live view read as absent."""
    pub("x" * 5000, repo="/w/" + "r" * 5000)
    views, truncated = R.live_views_scan(budget=5)
    assert [len(v["project"]) for v in views] == [120] and truncated is False


def test_a_dropped_registration_is_still_closed_in_a_forked_child(pub):
    import gc
    r = R.register("/w", "http://127.0.0.1:43999/", "dropped")   # not through `pub`, which keeps a reference
    name = r.name
    del r
    gc.collect()
    kept = [x for x in R._LIVE if x.name == name]
    try:
        assert kept                                                # the at-fork hook can still find its fds
    finally:
        for x in kept:
            x.unpublish()
            x.close()


def test_a_registry_fault_in_the_handler_is_an_error_not_an_empty_list(tmp_path, monkeypatch):
    import levain.team.registry as reg

    def boom(*a, **k):
        raise RuntimeError("registry fault")
    monkeypatch.setattr(reg, "live_views_scan", boom)
    httpd = _cockpit(tmp_path)
    try:
        r, body = _get(httpd.server_address[1], "/team_views.json")
        assert r.status == 500 and b"views" not in body             # the page keeps its last list (a failed fetch)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_many_dead_entries_never_hide_a_live_view(pub):
    """The old listing sliced the first 256 sorted names before judging liveness, so enough dead entries that sort
    before a live one hid it on every refresh."""
    d = R.registry_dir()
    d.mkdir(parents=True)
    for i in range(400):
        (d / f"lock1-{_hex(i)}.json").write_bytes(b"")          # all sort before any random 32-hex name starting 1+
    live = pub("ledgerline")
    assert live.name > f"lock1-{_hex(399)}.json"
    views, truncated = R.live_views_scan()
    assert [v["project"] for v in views] == ["ledgerline"] and truncated is False


def test_the_budget_ends_a_scan_and_says_so(pub):
    pub("ledgerline")
    assert R.live_views_scan(budget=0) == ([], True)            # stopped by time, with entries left unexamined
    assert R.live_views_scan(budget=5)[1] is False              # exhaustion is not truncation


def test_the_listing_response_carries_no_internal_fields(pub):
    pub("ledgerline")
    (v,) = R.live_views()
    assert set(v) == {"repo", "url", "project", "started"}


# ---- real processes -------------------------------------------------------------------------------------------------

def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _ledger_repo(tmp_path) -> Path:
    """A real clone with a team ledger, built the way tests/test_team_git.py builds one."""
    from levain.cli import main as levain_main
    _git("init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp_path)
    _git("clone", "-q", str(tmp_path / "origin.git"), "ana", cwd=tmp_path)
    ana = tmp_path / "ana"
    _git("config", "user.email", "ana@ex.com", cwd=ana)
    _git("config", "user.name", "ana", cwd=ana)
    (ana / "a.txt").write_text("x\n")
    _git("add", ".", cwd=ana)
    _git("commit", "-qm", "init", cwd=ana)
    _git("push", "-q", "origin", "HEAD:main", cwd=ana)
    assert levain_main(["team", "init", "--project", "ledgerline", "--owner", "ana", "--member", "ana=ana@ex.com",
                        "--repo", str(ana)]) == 0
    return ana


def _start_view(repo: Path, home: Path):
    """A real `python -m levain team view` process; returns it once its registry entry is published."""
    env = dict(os.environ, PYTHONPATH=os.getcwd(), LEVAIN_HOME=str(home), HOME=str(home.parent / "userhome"))
    before = set(home.glob("team-views/*.json"))
    p = subprocess.Popen([sys.executable, "-m", "levain", "team", "view", "--repo", str(repo), "--port", "0"],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if set(home.glob("team-views/*.json")) - before:
            return p
        if p.poll() is not None:
            raise AssertionError(f"view exited early: {p.stderr.read()}")
        time.sleep(0.1)
    p.kill()
    raise AssertionError("view never published its entry")


def _reap(procs):
    for p in procs:
        try:
            p.send_signal(signal.SIGCONT)
        except OSError:
            pass
        p.kill()
        p.communicate(timeout=10)


def test_t0_a_live_view_is_listed_while_wedged_ones_do_not_stall_the_listing(tmp_path, levain_home):
    """T0: three real views, two SIGSTOPped. Their pids are alive and their sockets accept, but they never answer
    (what a wedged view looks like): the live one must still be listed, and promptly. The wedged ones ARE listed:
    the registry says a view is running, not that it is answering, and this asserts that so nobody turns it back
    into a probe."""
    repo = _ledger_repo(tmp_path)
    procs = [_start_view(repo, levain_home) for _ in range(3)]
    try:
        for p in procs[:2]:
            p.send_signal(signal.SIGSTOP)
        t = time.monotonic()
        views = R.live_views()
        took = time.monotonic() - t
        assert took < 0.5, took
        assert len({v["url"] for v in views}) == 3 and all(v["project"] == "ledgerline" for v in views)
    finally:
        _reap(procs)


def test_t1_a_killed_view_stops_being_listed_and_the_next_view_prunes_its_file(tmp_path, levain_home):
    repo = _ledger_repo(tmp_path)
    first = _start_view(repo, levain_home)
    try:
        assert len(R.live_views()) == 1
        stale = _names()
        first.kill()
        first.communicate(timeout=10)
        assert R.live_views() == [] and _names() == stale     # dead at once; the cockpit deleted nothing
        second = _start_view(repo, levain_home)
        try:
            assert len(R.live_views()) == 1 and stale[0] not in _names()
        finally:
            _reap([second])
    finally:
        _reap([first])


_HELPER = r"""
import os, subprocess, sys, time
from levain.team import registry as R
mode = sys.argv[1]
r = R.register("/r", "http://127.0.0.1:41777/", "helper")
fd = r._lock_fd
print(os.get_inheritable(fd), flush=True)
if mode == "exec":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], close_fds=False)
    print(child.pid, flush=True)
else:                                         # a bare fork, no exec: the child must not keep the lock
    pid = os.fork()
    if pid == 0:
        time.sleep(30); os._exit(0)
    print(pid, flush=True)
time.sleep(30)
"""


@pytest.mark.parametrize("mode,survives", [("exec", False), ("fork", False)])
def test_t2c_neither_an_exec_child_nor_a_bare_fork_child_keeps_a_dead_view_listed(
        mode, survives, tmp_path, levain_home):
    env = dict(os.environ, PYTHONPATH=os.getcwd(), LEVAIN_HOME=str(levain_home))
    p = subprocess.Popen([sys.executable, "-c", _HELPER, mode], env=env, stdout=subprocess.PIPE, text=True)
    child = None
    try:
        inheritable = p.stdout.readline().strip()
        child = int(p.stdout.readline())
        assert inheritable == "False" and len(R.live_views()) == 1
        p.kill()
        p.wait(timeout=10)
        os.kill(child, 0)                                     # the child outlived the view
        assert (len(R.live_views()) == 1) is survives
    finally:
        p.kill()
        if child:
            try:
                os.kill(child, signal.SIGKILL)
            except OSError:
                pass


def test_the_sigterm_handler_is_restored_and_a_real_view_exits_clean(tmp_path, levain_home):
    sentinel = lambda *a: None  # noqa: E731
    prev = signal.signal(signal.SIGTERM, sentinel)
    try:
        monkeypatched = pytest.MonkeyPatch()
        try:
            _serve_with(monkeypatched, _interrupt)
            V.serve(_GL(), host="127.0.0.1", port=0, recheck_days=30, ack_flag=3)
        finally:
            monkeypatched.undo()
        assert signal.getsignal(signal.SIGTERM) is sentinel
    finally:
        signal.signal(signal.SIGTERM, prev)
    repo = _ledger_repo(tmp_path)
    p = _start_view(repo, levain_home)
    try:
        assert len(R.live_views()) == 1
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=10)
    finally:
        _reap([p])
    assert p.returncode == 0 and "Traceback" not in err
    assert _names() == [] and R.live_views() == []


# ---- the shutdown order ---------------------------------------------------------------------------------------------

def test_serve_publishes_while_running_and_removes_the_entry_on_clean_exit(monkeypatch):
    seen = _serve_with(monkeypatch, _interrupt)
    assert V.serve(_GL(), host="127.0.0.1", port=0, recheck_days=30, ack_flag=3) == 0
    assert len(seen["files"]) == 1 and _names() == []


def test_a_prune_failure_does_not_skip_registering(monkeypatch):
    seen = _serve_with(monkeypatch, _interrupt)
    monkeypatch.setattr(R, "prune_dead", lambda: (_ for _ in ()).throw(RuntimeError("poisoned")))
    assert V.serve(_GL(), host="127.0.0.1", port=0, recheck_days=30, ack_flag=3) == 0
    assert len(seen["files"]) == 1


@pytest.mark.parametrize("inject", [None, "unpublish:OSError", "unpublish:KeyboardInterrupt", "close:OSError",
                                    "close:KeyboardInterrupt", "sigterm"])
def test_t3_unpublish_then_close_then_server_close_whatever_fails_in_between(inject, monkeypatch):
    events = []
    for method in ("unpublish", "close"):
        real = getattr(R.Registration, method)

        def wrapped(self, _real=real, _m=method):
            events.append(_m)
            if inject and inject.startswith(_m + ":"):
                raise {"OSError": OSError, "KeyboardInterrupt": KeyboardInterrupt}[inject.split(":")[1]]("injected")
            return _real(self)
        monkeypatch.setattr(R.Registration, method, wrapped)

    def sf_factory(h, seen):
        real_close = h.server_close

        def sc():
            events.append("server_close")
            real_close()
        h.server_close = sc

        def sf():
            if inject == "sigterm":
                V._on_sigterm(signal.SIGTERM, None)
            raise KeyboardInterrupt
        return sf
    _serve_with(monkeypatch, sf_factory)
    if inject and inject != "sigterm":
        with pytest.raises((OSError, KeyboardInterrupt)):
            V.serve(_GL(), host="127.0.0.1", port=0, recheck_days=30, ack_flag=3)
    else:
        assert V.serve(_GL(), host="127.0.0.1", port=0, recheck_days=30, ack_flag=3) == 0
    assert events == ["unpublish", "close", "server_close"]


def test_a_keyboard_interrupt_during_prune_or_register_still_closes_the_socket():
    for target in ("prune_dead", "register"):
        with pytest.MonkeyPatch.context() as mp:
            closed = []
            _serve_with(mp, lambda h, seen: (setattr(h, "server_close", lambda: closed.append(1)), lambda: None)[1])
            mp.setattr(R, target, lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
            with pytest.raises(KeyboardInterrupt):
                V.serve(_GL(), host="127.0.0.1", port=0, recheck_days=30, ack_flag=3)
            assert closed == [1]


# ---- the cockpit route ---------------------------------------------------------------------------------------

def _cockpit(tmp_path):
    from levain.web_server import make_server
    install = tmp_path / "install"
    (install / ".levain").mkdir(parents=True)
    httpd = make_server(install, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


def test_cockpit_lists_only_live_views_and_stays_read_only(tmp_path, pub):
    httpd = _cockpit(tmp_path)
    try:
        port = httpd.server_address[1]
        r, body = _get(port, "/team_views.json")
        assert r.status == 200 and json.loads(body) == {"views": [], "truncated": False}   # no view: no tab
        v = pub("ledgerline")
        r, body = _get(port, "/team_views.json")
        assert json.loads(body) == {"views": [{"project": "ledgerline", "repo": "/work/ledgerline",
                                                "url": "http://127.0.0.1:41000/"}], "truncated": False}
        assert "pid" not in body.decode() and "started" not in body.decode()
        v.unpublish()
        assert json.loads(_get(port, "/team_views.json")[1]) == {"views": [], "truncated": False}   # gone on the next load
        # the guards every other cockpit GET has
        assert _get(port, "/team_views.json", {"Host": "evil.example"})[0].status == 403
        assert _get(port, "/team_views.json", {"Sec-Fetch-Site": "cross-site"})[0].status == 403
        for method in ("POST", "PUT", "DELETE"):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request(method, "/team_views.json", body=b"{}", headers={"Content-Type": "application/json"})
            assert c.getresponse().status >= 400
            c.close()
        # the new script is served and the page loads it
        assert _get(port, "/dashboard_team.js")[0].status == 200
        assert b"/dashboard_team.js" in _get(port, "/")[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_cockpit_script_never_uses_innerhtml_and_checks_the_scheme():
    from levain.web_server import load_web_asset
    js = load_web_asset("dashboard_team.js")
    assert "innerHTML" not in js and "outerHTML" not in js and "document.write" not in js
    assert '"http:"' in js and "textContent" in js


def test_live_views_does_not_use_the_cockpit_request_gate(tmp_path, pub):
    httpd = _cockpit(tmp_path)
    try:
        pub("ledgerline")
        gate = httpd.request_gate
        taken = 0
        while gate.acquire(blocking=False):         # the substrate gate is exhausted
            taken += 1
        try:
            assert _get(httpd.server_address[1], "/team_views.json")[0].status == 200
        finally:
            for _ in range(taken):
                gate.release()
    finally:
        httpd.shutdown(); httpd.server_close()


def test_team_views_is_404_unless_the_cockpit_and_the_peer_are_loopback(tmp_path, pub, monkeypatch):
    import levain.web_server as W
    httpd = _cockpit(tmp_path)
    try:
        pub("secret-project", repo="/work/secret-repo")
        port = httpd.server_address[1]
        assert b"secret-project" in _get(port, "/team_views.json")[1]
        from types import SimpleNamespace
        real_source = httpd.levain_source
        httpd.levain_source = SimpleNamespace(write_scope=None)   # a read-only mesh bind
        httpd.is_loopback_bind = False               # an off-box bind
        r, body = _get(port, "/team_views.json")
        assert r.status == 404 and b"secret" not in body
        httpd.is_loopback_bind = True
        httpd.levain_source = real_source
        monkeypatch.setattr(W, "_peer_is_loopback", lambda addr: False)   # a non-loopback peer
        r, body = _get(port, "/team_views.json")
        assert r.status == 404 and b"secret" not in body
    finally:
        httpd.shutdown(); httpd.server_close()


def test_team_views_with_a_foreign_host_header_is_403(tmp_path, pub):
    httpd = _cockpit(tmp_path)
    try:
        pub("secret-project", repo="/work/secret")
        r, body = _get(httpd.server_address[1], "/team_views.json", {"Host": "attacker.example"})
        assert r.status == 403 and b"secret" not in body
    finally:
        httpd.shutdown(); httpd.server_close()


def test_the_script_ignores_a_response_older_than_the_latest_request():
    from levain.web_server import load_web_asset
    import shutil
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    js = load_web_asset("dashboard_team.js")
    harness = r"""
const pending = [], made = [];
let fire = null;
global.window = { location: {} };
global.document = { querySelector: () => ({ appendChild: (c) => made.push(c) }), hidden: false,
  addEventListener: (ev, fn) => { fire = fn; },
  createElement: () => ({ alive: true, remove() { this.alive = false; }, addEventListener() {}, appendChild() {}, style: {}, setAttribute() {} }) };
global.fetch = () => new Promise((res) => pending.push(res));
%s
const ok = (v) => ({ ok: true, json: async () => ({ views: v }) });
const tick = () => new Promise((r) => setTimeout(r, 20));
(async () => {
  fire();                                   // a second load while the first is still in flight
  pending[1](ok([{ project: "p", repo: "/r", url: "http://127.0.0.1:7463/" }]));   // the newest answers first
  await tick();
  pending[0](ok([]));                       // the OLDER response arrives late, with no views: must be ignored
  await tick();
  console.log(JSON.stringify({ shown: made.filter((c) => c.alive).length }));
})();
""" % js
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {"shown": 1}


def test_a_failed_fetch_keeps_the_current_tab():
    from levain.web_server import load_web_asset
    import shutil
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    js = load_web_asset("dashboard_team.js")
    harness = r"""
const pending = [], made = [];
let fire = null;
global.window = { location: {} };
global.document = { querySelector: () => ({ appendChild: (c) => made.push(c) }), hidden: false,
  addEventListener: (ev, fn) => { fire = fn; },
  createElement: () => ({ alive: true, remove() { this.alive = false; }, addEventListener() {}, appendChild() {}, style: {}, setAttribute() {} }) };
global.fetch = () => new Promise((res, rej) => pending.push({ res, rej }));
%s
const tick = () => new Promise((r) => setTimeout(r, 20));
const shown = () => made.filter((c) => c.alive).length;
(async () => {
  pending[0].res({ ok: true, json: async () => ({ views: [{ project: "p", repo: "/r", url: "http://127.0.0.1:7463/" }] }) });
  await tick();
  const a = shown();
  fire(); pending[1].res({ ok: false, json: async () => ({}) });    // a non-ok answer
  await tick();
  const b = shown();
  fire(); pending[2].rej(new Error("network"));                      // a failed fetch
  await tick();
  console.log(JSON.stringify({ a, b, c: shown() }));
})();
""" % js
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {"a": 1, "b": 1, "c": 1}


def test_an_older_success_is_applied_when_a_newer_request_failed_but_never_over_a_newer_success():
    from levain.web_server import load_web_asset
    import shutil
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    js = load_web_asset("dashboard_team.js")
    harness = r"""
const pending = [], made = [];
let fire = null;
global.window = { location: {} };
global.document = { querySelector: () => ({ appendChild: (c) => made.push(c) }), hidden: false,
  addEventListener: (ev, fn) => { fire = fn; },
  createElement: () => ({ alive: true, remove() { this.alive = false; }, addEventListener() {}, appendChild() {}, style: {}, setAttribute() {} }) };
global.fetch = () => new Promise((res, rej) => pending.push({ res, rej }));
%s
const ok = (v) => ({ ok: true, json: async () => ({ views: v }) });
const tick = () => new Promise((r) => setTimeout(r, 20));
const shown = () => made.filter((c) => c.alive).length;
(async () => {
  fire();                                    // request 2 in flight while request 1 is still pending
  pending[1].rej(new Error("network"));      // the newer one fails
  await tick();
  pending[0].res(ok([{ project: "p", repo: "/r", url: "http://127.0.0.1:7463/" }]));   // the older success arrives
  await tick();
  const afterOlder = shown();                // must be applied: nothing newer was applied
  fire(); fire();                            // requests 3 and 4: the newer success applies, the older is then ignored
  pending[3].res(ok([]));
  await tick();
  pending[2].res(ok([{ project: "q", repo: "/r", url: "http://127.0.0.1:7464/" }]));
  await tick();
  console.log(JSON.stringify({ afterOlder, final: shown() }));
})();
""" % js
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {"afterOlder": 1, "final": 0}


def test_the_tab_says_so_when_the_server_reports_a_truncated_scan():
    from levain.web_server import load_web_asset
    import shutil
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    js = load_web_asset("dashboard_team.js")
    harness = r"""
const pending = [], made = [];
let fire = null;
global.window = { location: {} };
global.document = { querySelector: () => ({ appendChild: (c) => made.push(c) }), hidden: false,
  addEventListener: (ev, fn) => { fire = fn; },
  createElement: () => ({ alive: true, remove() { this.alive = false; }, addEventListener() {}, appendChild() {}, style: {}, setAttribute() {} }) };
global.fetch = () => new Promise((res) => pending.push(res));
%s
const tick = () => new Promise((r) => setTimeout(r, 20));
const notes = () => made.filter((c) => c.alive && c.className === "tab-team-note").map((c) => c.textContent);
(async () => {
  pending[0]({ ok: true, json: async () => ({ views: [], truncated: true }) });
  await tick();
  const a = notes();
  fire(); pending[1]({ ok: true, json: async () => ({ views: [], truncated: false }) });
  await tick();
  console.log(JSON.stringify({ a, b: notes() }));
})();
""" % js
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(got["a"]) == 1 and "incomplete" in got["a"][0] and got["b"] == []


@pytest.mark.parametrize("with_auth", [True, False])
def test_the_views_request_carries_the_cockpit_auth_headers_when_the_page_has_them(with_auth):
    # The cockpit's data routes may require a launch token (window.levainAuthHeaders, from dashboard_boot.js); this
    # script's one request must send it when the page provides it, and work unchanged when it does not.
    from levain.web_server import load_web_asset
    import shutil
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    js = load_web_asset("dashboard_team.js")
    auth = 'window.levainAuthHeaders = () => ({ "X-Levain-Token": "t0k" });' if with_auth else ""
    harness = r"""
const sent = [];
global.window = { location: {} };
%s
global.document = { querySelector: () => ({ appendChild() {} }), hidden: false, addEventListener() {},
  createElement: () => ({ remove() {}, addEventListener() {}, appendChild() {}, style: {}, setAttribute() {} }) };
global.fetch = (url, opts) => { sent.push({ url, headers: opts.headers }); return new Promise(() => {}); };
%s
console.log(JSON.stringify(sent));
""" % (auth, js)
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    sent = json.loads(out.stdout.strip().splitlines()[-1])
    want = {"Accept": "application/json", **({"X-Levain-Token": "t0k"} if with_auth else {})}
    assert sent == [{"url": "/team_views.json", "headers": want}]


def test_the_views_request_runs_again_once_the_page_has_its_token():
    # lane T, measured in Chrome: the first /team_views.json ran before token.js traded the link's one-time code for
    # the token, got a 403, and the Team tab stayed missing until a reload. The script loads again on unlock.
    from levain.web_server import load_web_asset
    import shutil
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    js = load_web_asset("dashboard_team.js")
    harness = r"""
const sent = [], made = [];
let unlock = null;
global.window = { location: {}, LevainToken: { onUnlock: (fn) => { unlock = fn; } } };
let authed = false;
window.levainAuthHeaders = () => (authed ? { "X-Levain-Token": "t0k" } : {});
global.document = { querySelector: () => ({ appendChild: (c) => made.push(c) }), hidden: false, addEventListener() {},
  createElement: () => ({ alive: true, remove() { this.alive = false; }, addEventListener() {}, appendChild() {}, style: {}, setAttribute() {} }) };
global.fetch = (url, opts) => {
  sent.push(opts.headers);
  return Promise.resolve(opts.headers["X-Levain-Token"]
    ? { ok: true, json: async () => ({ views: [{ project: "p", repo: "/r", url: "http://127.0.0.1:7463/" }] }) }
    : { ok: false, status: 403 });
};
%s
const tick = () => new Promise((r) => setTimeout(r, 20));
(async () => {
  await tick();
  const before = made.filter((c) => c.alive).length;
  authed = true;
  unlock();
  await tick();
  console.log(JSON.stringify({ before, after: made.filter((c) => c.alive).length, requests: sent.length,
                               registered: typeof unlock === "function" }));
})();
""" % js
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {"before": 0, "after": 1, "requests": 2,
                                                              "registered": True}
