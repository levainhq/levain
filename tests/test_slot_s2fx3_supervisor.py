"""S1 session 2 fix round 3, lane FX3: the supervisor + launcher findings of L3 r1 g4 (``S2_L3_r2_TRIAGE.md``
g4-1 .. g4-11): helper failures and hangs, a terminal that is not reading, reporting modes reset at the end, overlong
UTF-8, the passthrough refusal, the prepare answer checked whole, the relay's end, deep settings JSON, and
render_blob's refusal names.

The fake runtime helper is lane S's (``tests/test_slot_s2s_supervisor.py``) with hooks added: a verb that hangs
(optionally deaf to SIGTERM), an overridden prepare answer, and start modes that set reporting modes, write a lot,
write forever, or ignore every signal.
"""
from __future__ import annotations

import io
import json
import os
import random
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import launch as L
from levain.slot import supervisor as SV
from levain.slot.supervisor import TermFilter
from tests.test_slot_s2s_supervisor import FAKE_HELPER, SESSION, Rig, _docs, _plant, isolated_git, make_repo  # noqa: F401

HOOKS = r'''
hang = os.environ.get("FAKE_HANG", "")
if hang == verb:
    import signal
    if os.environ.get("FAKE_DEAF"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(os.path.join(store, "pid-" + verb), "w") as f:
        f.write(str(os.getpid()))
    while True:
        time.sleep(0.05)
if verb == "prepare" and os.environ.get("FAKE_PREP"):
    os.makedirs(os.path.join(store, "staging", "%(session)s"))
    sys.stdout.write(os.environ["FAKE_PREP"] + "\n")
    sys.exit(0)
refuse = os.environ.get("FAKE_REFUSE", "")''' % {"session": SESSION}

START_HOOKS = r'''
    mode = os.environ.get("FAKE_START")
    if mode == "modes":
        os.write(1, b"\x1b[?1004h\x1b[?1000;1006h\x1b[?25lM")
        line({"exit": 0, "ok": True})
        sys.exit(0)
    if mode == "big":
        for _ in range(int(os.environ["FAKE_BIG"]) // 65536):
            os.write(1, b"Q" * 65536)
        line({"exit": 0, "ok": True})
        sys.exit(0)
    if mode in ("forever", "immortal"):
        import signal
        if mode == "immortal":
            for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                signal.signal(s, signal.SIG_IGN)
        with open(os.path.join(store, "pid-start"), "w") as f:
            f.write(str(os.getpid()))
        open(os.path.join(store, "started"), "w").close()
        while True:
            if mode == "forever":
                os.write(1, b"x" * 1024)
            else:
                time.sleep(0.05)'''

FX3_HELPER = (FAKE_HELPER.replace('refuse = os.environ.get("FAKE_REFUSE", "")', HOOKS, 1)
              .replace('    mode = os.environ.get("FAKE_START")', START_HOOKS, 1))
assert FX3_HELPER.count("FAKE_HANG") == 1 and FX3_HELPER.count('"immortal"') == 2


def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Rig:
    r = Rig(tmp_path, monkeypatch, **env)
    r.helper.write_text(FX3_HELPER)
    return r


def stop_when(path: Path, delay: float = 0.0, sig: int = signal.SIGTERM) -> threading.Thread:
    """SIGTERM to this process (the supervisor's) once ``path`` exists."""
    def go() -> None:
        deadline = time.monotonic() + 20
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(delay)
        os.kill(os.getpid(), sig)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def gone(pid: int) -> bool:
    """The process no longer exists: not even a zombie, so it was reaped."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def pid_of(r: Rig, verb: str) -> int:
    return int((r.store / f"pid-{verb}").read_text())


# --- g4-1: a helper that cannot run, and teardown raising, never skip the pty's end -----------------------------------

def _spawn_failing(r: Rig, verb: str, exc: BaseException, started: list) -> None:
    real = r.seams.spawn

    def spawn(argv, **kw):                           # type: ignore[no-untyped-def]
        if verb in argv:
            raise exc
        p = real(argv, **kw)
        if "start" in argv:
            started.append(p)
        return p
    r.seams.spawn = spawn


@pytest.mark.timeout(60)
def test_teardown_helper_unrunnable_is_named_and_start_still_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                                  ) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_START="wait")
    r.seams.start_grace_s = 0.5
    started: list = []
    _spawn_failing(r, "teardown", FileNotFoundError(2, "No such file or directory", "sudo"), started)
    stop_when(r.store / "started")
    assert r.run() == 1
    err = r.stderr()
    assert "teardown refused helper-unrunnable" in err and "stays leased" in err
    assert started and started[0].returncode is not None   # `start` was signalled and reaped all the same
    assert r.calls[-1] == ("close",)


@pytest.mark.timeout(60)
def test_teardown_raising_anything_still_ends_start_and_closes_the_pty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                                       ) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_START="wait")
    r.seams.start_grace_s = 0.5
    started: list = []
    _spawn_failing(r, "teardown", RuntimeError("a bug"), started)
    ptys: list = []
    real_openpty = r.seams.openpty
    r.seams.openpty = lambda: ptys.append(real_openpty()) or ptys[-1]
    stop_when(r.store / "started")
    with pytest.raises(RuntimeError):
        r.run()
    assert started[0].returncode is not None
    for fd in ptys[0]:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert r.calls[-1] == ("close",)


def test_prepare_helper_unrunnable_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch)
    _spawn_failing(r, "prepare", PermissionError(13, "Permission denied", "sudo"), [])
    assert r.run() == 1
    assert "refused helper-unrunnable: prepare: Permission denied" in r.stderr()


def test_an_os_failure_at_the_pty_is_named_after_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch)

    def emfile() -> tuple:
        raise OSError(24, "Too many open files")
    r.seams.openpty = emfile
    assert r.run() == 1
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "teardown"]
    assert "refused os-error: [Errno 24] Too many open files" in r.stderr()


def test_launch_main_names_a_refusal_or_os_failure_from_the_run() -> None:
    err = io.StringIO()

    def unsupported(_req: object) -> int:
        raise Refused("unsupported-host", "freebsd14")

    def oserr(_req: object) -> int:
        raise FileNotFoundError(2, "No such file or directory", "git")
    assert L.main(["claude", "--repo", "/r"], run=unsupported, err=err) == 1
    assert L.main(["claude", "--repo", "/r"], run=oserr, err=err) == 1
    out = err.getvalue()
    assert "levain session: refused unsupported-host: freebsd14" in out
    assert "levain session: refused os-error: [Errno 2] No such file or directory: 'git'" in out


# --- g4-2: a hung helper is ended by a stop signal or its deadline ----------------------------------------------------

@pytest.mark.timeout(60)
def test_stop_signal_ends_a_hung_render_then_tears_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_HANG="render")
    r.seams.helper_deadline_s = {"render": 20.0}      # a broken stop path then shows as helper-timeout, not a hang
    stop_when(r.store / "pid-render", 0.2)
    t0 = time.monotonic()
    assert r.run() == 1
    assert time.monotonic() - t0 < 30
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "teardown"]
    assert "refused stopped: render: SIGTERM" in r.stderr()
    assert f"session {SESSION} ended RETAINED" in r.stderr()
    assert gone(pid_of(r, "render"))
    assert r.calls[-1] == ("close",)


@pytest.mark.timeout(60)
def test_deadline_ends_a_helper_deaf_to_sigterm_with_sigkill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_HANG="render", FAKE_DEAF="1")
    r.seams.helper_deadline_s = {"render": 1.0}
    r.seams.kill_grace_s = 0.3
    assert r.run() == 1
    assert "refused helper-timeout: render did not answer in 1.0s" in r.stderr()
    assert gone(pid_of(r, "render"))                        # TERM was ignored: KILL, then reaped
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "teardown"]


@pytest.mark.timeout(60)
def test_a_hung_teardown_is_bounded_and_says_the_session_stays_leased(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                                      ) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_HANG="teardown")
    r.seams.helper_deadline_s = {"teardown": 1.0}
    assert r.run() == 1
    assert "teardown refused helper-timeout" in r.stderr()
    assert gone(pid_of(r, "teardown"))


@pytest.mark.timeout(60)
def test_teardown_after_a_stop_is_not_cut_short_by_that_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The first SIGTERM ends the session; teardown, which runs after it, must still run to its answer.
    r = rig(tmp_path, monkeypatch, FAKE_START="wait")
    stop_when(r.store / "started")
    assert r.run() == 1
    assert f"session {SESSION} ended RETAINED" in r.stderr()


# --- g4-3: a terminal that is not reading never stalls the relay or teardown -----------------------------------------

@pytest.mark.timeout(60)
def test_stalled_terminal_does_not_block_a_stop_or_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_START="forever")
    r.seams.start_grace_s = 1.0
    out_r, out_w = os.pipe()                 # his terminal: nobody reads it, so it fills
    r.seams.stdout = out_w
    stop_when(r.store / "started", 0.5)
    t0 = time.monotonic()
    try:
        assert r.run() == 1
        assert time.monotonic() - t0 < 30
        assert os.get_blocking(out_w)        # put back as it was
    finally:
        os.close(out_r)
        os.close(out_w)
    err = r.stderr()
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "start", "teardown"]
    assert f"session {SESSION} ended RETAINED" in err
    assert re.search(r"your terminal did not take the session's last \d+ bytes", err)


@pytest.mark.timeout(60)
def test_a_slow_terminal_gets_every_byte(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    n = 4 << 20                              # past the output cap: the master waits, nothing is dropped
    r = rig(tmp_path, monkeypatch, FAKE_START="big", FAKE_BIG=str(n))
    out_r, out_w = os.pipe()
    r.seams.stdout = out_w
    got = bytearray()

    def reader() -> None:
        time.sleep(1.5)
        while True:
            chunk = os.read(out_r, 65536)
            if not chunk:
                return
            got.extend(chunk)
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        assert r.run() == 0
    finally:
        os.close(out_w)
    t.join(20)
    os.close(out_r)
    assert got.count(b"Q") == n


# --- g4-4 / g4-6: the filter --------------------------------------------------------------------------------------------

def _run(chunks: list) -> bytes:
    f = TermFilter()
    return b"".join(f.feed(c) for c in chunks) + f.flush()


FILTER_ROWS = [
    ("decrqlp", b"a\x1b[1'|b", b"ab"),
    ("xtreportsgr", b"a\x1b[1;1;2;2#|b", b"ab"),
    ("decrqupss", b"a\x1b[&ub", b"ab"),
    ("decrqde", b'a\x1b["vb', b"ab"),
    ("overlong-esc-3", b"a\xe0\x80\x9b[6nb", b"a\xe0[6nb"),        # never the overlong ESC + CSI 6n
    ("overlong-esc-2", b"a\xc0\x9bc", b"a\xc0c"),
    ("overlong-esc-4", b"a\xf0\x80\x80\x9bc", b"a\xf0c"),
    ("overlong-esc-then-final", b"\xe0\x80\x9bc", b"\xe0c"),        # was ESC c (RIS) in the input's own bytes
    ("surrogate", b"a\xed\xa0\x80b", b"a\xed\xa0b"),
    ("past-u10ffff", b"a\xf4\x90\x80\x80b", b"a\xf4b"),
    ("max-valid", "a\U0010ffffb\ud7ff\ue000".encode("utf-8", "surrogatepass"), None),
]


@pytest.mark.parametrize("name,data,want", FILTER_ROWS, ids=[x[0] for x in FILTER_ROWS])
def test_filter_rows_whole_split_and_bytewise(name: str, data: bytes, want: bytes) -> None:
    want = data if want is None else want
    assert _run([data]) == want
    for i in range(1, len(data)):
        assert _run([data[:i], data[i:]]) == want, f"split at {i}"
    assert _run([data[i:i + 1] for i in range(len(data))]) == want


def _loose_c1_bytes(out: bytes) -> list:
    """Every 0x80-0x9F byte the output holds OUTSIDE a well-formed UTF-8 character (a strict decoder rejects
    overlongs and surrogates, so their continuation bytes count): the bytes an overlong ESC or C1 is spelled with."""
    return [c for c in out.decode("utf-8", "surrogateescape") if 0xDC80 <= ord(c) <= 0xDC9F]


def test_fuzz_the_filter_emits_no_c1_byte_outside_a_well_formed_character() -> None:
    rnd = random.Random(20261020)
    alphabet = [*range(0x80, 0x100), 0x1B, 0x5B, 0x5D, 0x07, 0x63, 0x36, 0x6E, 0x3B, 0x41]
    for _ in range(3000):
        data = bytes(rnd.choice(alphabet) for _ in range(rnd.randint(1, 40)))
        out = _run([data])
        assert not _loose_c1_bytes(out), data
        split = rnd.randint(1, len(data))
        assert _run([data[:split], data[split:]]) == out


# --- g4-5: reporting modes pass, and are reset at the end --------------------------------------------------------------

def test_filter_tracks_reporting_modes_it_passed() -> None:
    f = TermFilter()
    data = b"\x1b[?1000;1006h\x1b[?1004h\x1b[?2004h\x1b[?25l\x1b[?1049h\x1b[?2004l"
    assert f.feed(data) == data              # they pass
    assert f.reset() == b"\x1b[?1000;1004;1006l"
    assert f.reset() == b""
    assert TermFilter().reset() == b""
    g = TermFilter()
    g.feed(b"\x1b[?1003r\x1b[?9h")           # XTRESTORE may set it; X10 mouse
    assert g.reset() == b"\x1b[?9;1003l"
    h = TermFilter()
    h.feed(b"\x1b[6n\x1b[?1000$p")           # a dropped sequence sets nothing
    assert h.reset() == b""


def test_supervisor_resets_passed_modes_when_the_session_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_START="modes")
    assert r.run() == 0
    out = r.out.read_bytes()
    assert b"\x1b[?1004h\x1b[?1000;1006h" in out
    assert out.endswith(b"\x1b[?1000;1004;1006l")


@pytest.mark.timeout(60)
def test_modes_are_reset_on_a_stop_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_START="wait")
    r.helper.write_text(FX3_HELPER.replace('open(os.path.join(store, "started"), "w").close()\n        while not',
                                           'os.write(1, b"\\x1b[?1002h")\n        time.sleep(0.3)\n'
                                           '        open(os.path.join(store, "started"), "w").close()\n'
                                           '        while not', 1))
    stop_when(r.store / "started", 0.3)
    assert r.run() == 1
    assert r.out.read_bytes().endswith(b"\x1b[?1002l")


# --- g4-7: interactive passthrough refused at parse ---------------------------------------------------------------------

def test_interactive_passthrough_is_refused_before_a_slot_is_taken() -> None:
    with pytest.raises(Refused) as e:
        L.parse(["claude", "--repo", "/r", "--auth", "passthrough"])
    assert e.value.name == "passthrough-needs-identity-gate"
    assert L.parse(["claude", "--repo", "/r", "--auth", "passthrough", "--login"]).kind == "login"


# --- g4-8: the prepare answer, whole ------------------------------------------------------------------------------------

def _prep(store: Path, **over: object) -> dict:
    good = {"format": "levain-prepare-result/1", "session": SESSION, "slot": {"class": "egress", "index": 2},
            "gwport": 47102, "staging": str(store / "staging" / SESSION)}
    good.update(over)
    return {k: v for k, v in good.items() if v is not None}


@pytest.mark.parametrize("over,what", [
    ({"format": "levain-prepare-result/2"}, "format"),
    ({"format": None}, "keys"),
    ({"gwport": None}, "keys"),
    ({"extra": 1}, "keys"),
    ({"session": "s-XYZ"}, "session"),
    ({"slot": {"class": "none", "index": 2}}, "slot"),
    ({"slot": {"class": "egress", "index": True}}, "slot"),
    ({"slot": {"class": "egress", "index": 100}}, "slot"),
    ({"slot": {"class": "egress", "index": 2, "x": 1}}, "slot"),
    ({"slot": [2]}, "slot"),
    ({"gwport": 47100}, "gwport"),
    ({"gwport": "47102"}, "gwport"),
    ({"staging": "/tmp/elsewhere"}, "staging"),
])
def test_prepare_answer_refuses_every_malformed_field(over: dict, what: str) -> None:
    store = Path("/store")
    with pytest.raises(Refused) as e:
        SV.prepare_answer(_prep(store, **over), host="darwin", store=store)
    assert e.value.name == "prepare-answer-malformed" and e.value.detail.startswith(what)


def test_prepare_answer_accepts_the_contract_shape_per_host() -> None:
    store = Path("/store")
    assert SV.prepare_answer(_prep(store), host="darwin", store=store) == (SESSION, 2)
    assert SV.prepare_answer(_prep(store, gwport=47100), host="linux", store=store) == (SESSION, 2)


def test_malformed_prepare_answer_is_refused_and_left_to_the_lease_close(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    answer = {"format": "levain-prepare-result/9", "gwport": 47100, "ok": True, "session": SESSION,
              "slot": {"class": "egress", "index": 0}, "staging": "x"}
    r = rig(tmp_path, monkeypatch, FAKE_PREP=json.dumps(answer, sort_keys=True, separators=(",", ":")))
    assert r.run() == 1
    assert [x["verb"] for x in r.verbs()] == ["prepare"]
    assert "refused prepare-answer-malformed: format" in r.stderr()
    assert r.calls[-1] == ("close",)


# --- g4-9: the relay ends on the helper's exit whatever stdin does; `start` is TERMed, KILLed, reaped -------------------

@pytest.mark.timeout(60)
def test_relay_ends_when_the_helper_exits_though_the_operator_keeps_typing(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch)
    stop = threading.Event()

    def typist() -> None:
        while not stop.is_set():
            try:
                os.write(r.in_w, b"k")
            except OSError:
                return
            time.sleep(0.05)
    t = threading.Thread(target=typist, daemon=True)
    t.start()
    t0 = time.monotonic()
    try:
        assert r.run() == 0
    finally:
        stop.set()
    assert time.monotonic() - t0 < 30
    assert "claude exited 3" in r.stderr()


@pytest.mark.timeout(60)
def test_start_deaf_to_every_signal_is_killed_and_reaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_START="immortal")
    r.seams.start_grace_s = 0.5
    started: list = []
    _spawn_failing(r, "never-a-verb", RuntimeError(), started)
    stop_when(r.store / "started", 0.2)
    assert r.run() == 1
    assert started[0].returncode == -signal.SIGKILL
    assert gone(started[0].pid)
    assert f"session {SESSION} ended RETAINED" in r.stderr()


# --- g4-10 / g4-11 -----------------------------------------------------------------------------------------------------

def test_deep_nested_settings_json_is_refused_malformed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    _plant(repo, ".claude/settings.json", b"[" * 100_000 + b"]" * 100_000)
    with pytest.raises(Refused) as e:
        _docs(tmp_path, repo)
    assert e.value.name == "repo-settings-malformed" and "not JSON" in e.value.detail


def test_render_blob_names_a_bad_route_by_the_grammar_not_as_the_identity() -> None:
    ident = {"name": "Op Erator", "email": "op@example.com"}
    with pytest.raises(Refused) as e:
        SV.render_blob("claude", "brokered", ["Not A Route"], ident, None)
    assert e.value.name == "bad-value" and e.value.detail == "routes"
    with pytest.raises(Refused) as e:
        SV.render_blob("claude", "brokered", ["anthropic"], {"name": "x<y>", "email": "a@b"}, None)
    assert e.value.name == "git-identity-invalid"
    blob = json.loads(SV.render_blob("claude", "brokered", ["anthropic"], ident, "repo"))
    assert blob["git_identity"] == ident and blob["repo_label"] == "repo"
