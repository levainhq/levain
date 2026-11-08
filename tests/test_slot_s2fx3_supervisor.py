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
from tests.test_slot_s2s_supervisor import (FAKE_HELPER, SESSION, Rig, _docs, _plant, bounded,  # noqa: F401
                                          isolated_git, make_repo)

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
    if mode == "raw":   # sudo's use_pty: the terminal it is given is raw while it runs (no echo of his typing)
        import tty
        tty.setraw(0)
    if mode == "modes":
        os.write(1, b"\x1b[?1004h\x1b[?1000;1006h\x1b[?25lM")
        line({"exit": 0, "ok": True})
        sys.exit(0)
    if mode == "big":
        for _ in range(int(os.environ["FAKE_BIG"]) // 65536):
            os.write(1, b"Q" * 65536)
        line({"exit": 0, "ok": True})
        sys.exit(0)
    if mode == "stall":
        import tty
        tty.setraw(0)
        os.write(1, b"Q" * 200000)
        open(os.path.join(store, "raw"), "w").close()
        while b"q" not in os.read(0, 64):
            pass
        open(os.path.join(store, "quit"), "w").close()
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


@bounded(60)
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


@bounded(60)
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
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "probe", "teardown"]
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

@bounded(60)
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


@bounded(60)
def test_deadline_ends_a_helper_deaf_to_sigterm_with_sigkill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_HANG="render", FAKE_DEAF="1")
    r.seams.helper_deadline_s = {"render": 1.0}
    r.seams.kill_grace_s = 0.3
    assert r.run() == 1
    assert "refused helper-timeout: render did not answer in 1.0s" in r.stderr()
    assert gone(pid_of(r, "render"))                        # TERM was ignored: KILL, then reaped
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "teardown"]


@bounded(60)
def test_a_hung_teardown_is_bounded_and_says_the_session_stays_leased(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                                      ) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_HANG="teardown")
    r.seams.helper_deadline_s = {"teardown": 1.0}
    assert r.run() == 1
    assert "teardown refused helper-timeout" in r.stderr()
    assert gone(pid_of(r, "teardown"))


@bounded(60)
def test_teardown_after_a_stop_is_not_cut_short_by_that_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The first SIGTERM ends the session; teardown, which runs after it, must still run to its answer.
    r = rig(tmp_path, monkeypatch, FAKE_START="wait")
    stop_when(r.store / "started")
    assert r.run() == 1
    assert f"session {SESSION} ended RETAINED" in r.stderr()


# --- g4-3: a terminal that is not reading never stalls the relay or teardown -----------------------------------------

@bounded(60)
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
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "probe", "start", "teardown"]
    assert f"session {SESSION} ended RETAINED" in err
    assert re.search(r"your terminal did not take the session's last \d+ bytes", err)


@bounded(60)
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


@bounded(60)
def test_input_still_reaches_the_vendor_while_his_terminal_is_stalled(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    # The terminal takes nothing until the vendor has seen the operator's key: a relay blocked in a write to it
    # would never forward the key.
    r = rig(tmp_path, monkeypatch, FAKE_START="stall")
    out_r, out_w = os.pipe()
    r.seams.stdout = out_w
    seen, got = [], bytearray()

    def reader() -> None:
        deadline = time.monotonic() + 10
        while not (r.store / "quit").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        seen.append((r.store / "quit").exists())
        while True:
            chunk = os.read(out_r, 65536)
            if not chunk:
                return
            got.extend(chunk)
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    r.feed(b"q")
    try:
        assert r.run() == 0
    finally:
        os.close(out_w)
    t.join(20)
    os.close(out_r)
    assert seen == [True]
    assert got.count(b"Q") == 200000


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
    assert f.reset() == b"\x1b[?1049l\x1b[?1000;1004;1006l"   # the alternate screen is left first
    assert f.reset() == b""
    assert TermFilter().reset() == b""
    g = TermFilter()
    g.feed(b"\x1b[?1003r\x1b[?9h")           # XTRESTORE may set it; X10 mouse
    assert g.reset() == b"\x1b[?9;1003l"
    h = TermFilter()
    h.feed(b"\x1b[6n\x1b[?1000$p")           # a dropped sequence sets nothing
    assert h.reset() == b""


# --- PREL3 2b-5: the other modes the slot's output can set are undone at the end too ----------------------------------

@pytest.mark.parametrize("set_, undo", [
    (b"\x1b[?1049h", b"\x1b[?1049l"),
    (b"\x1b[?1047h", b"\x1b[?1047l"),
    (b"\x1b[?47h", b"\x1b[?47l"),
    (b"\x1b[?1h", b"\x1b[?1l"),                       # application cursor keys
    (b"\x1b[?1r", b"\x1b[?1l"),                       # XTRESTORE may set it
    (b"\x1b[>1u", b"\x1b[<1u"),                       # one kitty push: one pop, nothing more
    (b"\x1b[>1u\x1b[>3u\x1b[<u\x1b[>8u", b"\x1b[<2u"),  # pushes net of pops
    (b"\x1b[=5u", b"\x1b[=0u"),                       # a set in place: the current flags zeroed
    (b"\x1b[=5;1u\x1b[>1u", b"\x1b[<1u\x1b[=0u"),
    (b"\x1b[>4;2m", b"\x1b[>4m"),                     # modifyOtherKeys
    (b"\x1b[>1;3m\x1b[>4;1m", b"\x1b[>1m\x1b[>4m"),
])
def test_reset_undoes_each_mode_the_slot_set(set_, undo) -> None:
    f = TermFilter()
    assert f.feed(set_) == set_                # it passes
    assert f.reset() == undo
    assert f.reset() == b""


@pytest.mark.parametrize("data", [
    b"\x1b[?1049h\x1b[?1049l", b"\x1b[?1h\x1b[?1l", b"\x1b[>1u\x1b[<1u",
    b"\x1b[>4;2m\x1b[>4m", b"\x1b[>4;2m\x1b[>1;2m\x1b[>m", b"\x1b[?u", b"\x1b[?25l\x1b[?7h\x1b[u",
])
def test_reset_emits_nothing_for_a_mode_the_slot_undid_itself_or_never_set(data) -> None:
    f = TermFilter()
    f.feed(data)
    assert f.reset() == b""


def test_kitty_entries_pushed_on_the_alternate_screen_are_popped_there_before_it_is_left() -> None:
    f = TermFilter()
    f.feed(b"\x1b[>1u\x1b[?1049h\x1b[>3u\x1b[>3u\x1b[?1000h")
    assert f.reset() == b"\x1b[<2u\x1b[?1049l\x1b[<1u\x1b[?1000l"


def test_kitty_entries_left_on_an_alternate_screen_the_slot_already_left_are_reached_by_entering_it() -> None:
    f = TermFilter()
    f.feed(b"\x1b[?1049h\x1b[>1u\x1b[?1049l")
    assert f.reset() == b"\x1b[?47h\x1b[<1u\x1b[?47l"      # the non-clearing pair: 1049h would clear that screen


def test_the_alternate_screen_is_left_once_by_the_mode_that_entered_it_from_main() -> None:
    f = TermFilter()
    f.feed(b"\x1b[?47h\x1b[?1049h\x1b[?1h\x1b[>4;2m")
    assert f.reset() == b"\x1b[?47l\x1b[?1l\x1b[>4m"


def test_supervisor_resets_passed_modes_when_the_session_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_START="modes")
    assert r.run() == 0
    out = r.out.read_bytes()
    assert b"\x1b[?1004h\x1b[?1000;1006h" in out
    assert out.endswith(b"\x1b[?1000;1004;1006l")


@bounded(60)
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

def test_interactive_passthrough_is_refused_before_a_slot_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(L, "IDENTITY_GATED", ("codex",))      # a vendor outside the set keeps the refusal
    with pytest.raises(Refused) as e:
        L.parse(["claude", "--repo", "/r", "--auth", "passthrough"])
    assert e.value.name == "passthrough-needs-identity-gate"
    assert L.parse(["claude", "--repo", "/r", "--auth", "passthrough", "--login"]).kind == "login"


# --- G3-3: identity-changed and the Codex line -------------------------------------------------------------------------

PLANTED = "99999999-9999-4999-8999-999999999999"
ROOT_ID = "11111111-1111-4111-8111-111111111111"

G3_START = r'''    if os.environ.get("FAKE_START") == "idexp":
        os.write(1, b"slot 7 identity-changed identity-expired sign in again: levain session claude --slot 7\\n")
        line({"ok": False, "refused": "identity-expired"})
        sys.exit(1)
    if os.environ.get("FAKE_START") == "idunr":
        line({"ok": False, "refused": "identity-unreachable"})
        sys.exit(1)
    if os.environ.get("FAKE_START") == "idchg":
        os.write(1, ("account %s\\n" % os.environ["FAKE_PLANTED"]).encode())
        line({"ok": False, "refused": "identity-changed"})
        sys.exit(1)
    if mode == "raw":'''
G3_HELPER = (FX3_HELPER.replace('    if mode == "raw":', G3_START, 1)
             .replace('elif verb == "teardown":', '''elif verb == "status":
    ans = os.environ.get("FAKE_STATUS", "")
    if ans:
        sys.stdout.write(ans + "\\n")
    else:
        line({"ok": True, "identity": {"observed_id": os.environ["FAKE_ROOT_ID"], "result": "identity-changed"}})
elif verb == "teardown":''', 1))
assert G3_HELPER.count("idchg") == 1 and G3_HELPER.count('verb == "status"') == 1


def g3rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Rig:
    env.setdefault("FAKE_START", "idchg")
    r = Rig(tmp_path, monkeypatch, FAKE_PLANTED=PLANTED, FAKE_ROOT_ID=ROOT_ID, **env)
    r.helper.write_text(G3_HELPER)
    return r


def _said(r: Rig) -> str:
    return r.stderr()


@bounded(60)
def test_identity_changed_shows_root_status_id_not_the_pty_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                                ) -> None:
    r = g3rig(tmp_path, monkeypatch)
    assert r.run(auth="passthrough") == 1
    said = _said(r)
    assert ROOT_ID in said and PLANTED not in said
    assert f"slot 0's stored Claude login now names account {ROOT_ID}" in said
    assert "not the account recorded at this slot's login" in said and "the server's answer" not in said
    assert "Nothing was launched" in said
    assert "levain session claude --auth passthrough --login --slot 0, and answer yes." in said
    assert PLANTED.encode() in r.out.read_bytes()                       # the tail did carry the other id
    verbs = [v["verb"] for v in r.verbs()]
    assert verbs.index("status") < verbs.index("teardown") and verbs[-1] == "teardown"


@bounded(60)
def test_identity_expired_text_is_root_sourced_and_ignores_a_planted_tail(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    expired = json.dumps({"ok": True, "identity": {"result": "identity-expired"}}, sort_keys=True,
                         separators=(",", ":"))
    r = g3rig(tmp_path, monkeypatch, FAKE_START="idexp", FAKE_STATUS=expired)
    assert r.run(auth="passthrough") == 1
    assert ("slot 0's stored Claude login reports no sign-in (Levain does not see a login that has expired or been "
            "revoked on the server). Nothing was launched. Sign in again: "
            "levain session claude --auth passthrough --login --slot 0") in _said(r)
    assert "slot 7" not in _said(r) and "identity-changed" not in _said(r)
    # the tail alone (root's record says something else) cannot make the line be said
    other = json.dumps({"ok": True, "identity": {"result": "matched"}}, sort_keys=True, separators=(",", ":"))
    (tmp_path / "b").mkdir()
    r2 = g3rig(tmp_path / "b", monkeypatch, FAKE_START="idexp", FAKE_STATUS=other)
    assert r2.run(auth="passthrough") == 1
    said = _said(r2)
    assert "reports no sign-in" not in said and "slot 7" not in said and "Nothing was launched." in said
    assert "root's record does not say so" in said and "could not be read" not in said
    assert [v["verb"] for v in r2.verbs()][-1] == "teardown"


@bounded(60)
def test_a_launch_with_a_claude_route_lacking_the_token_host_is_refused_before_anything_starts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1029 ruling (np-e5b2ebed (a)): the route is refused at load, naming the host; with the host the launch goes on
    to the plain identity-unreachable retry line."""
    unr = json.dumps({"ok": True, "identity": {"result": "identity-unreachable"}}, sort_keys=True, separators=(",", ":"))
    r = g3rig(tmp_path, monkeypatch, FAKE_START="idunr", FAKE_STATUS=unr)
    p = r.store / "routes" / "base.json"
    base = json.loads(p.read_text())
    for route in base["routes"]:
        if route["route"] == "claude-sub":
            route["hosts"].remove("platform.claude.com")
    p.write_text(json.dumps(base, sort_keys=True, separators=(",", ":")))
    assert r.run(auth="passthrough") == 1
    assert ("refused passthrough-route-lacks-token-host: add platform.claude.com to the hosts of route claude-sub"
            in _said(r)) and not r.verbs()
    (tmp_path / "b").mkdir()
    r2 = g3rig(tmp_path / "b", monkeypatch, FAKE_START="idunr", FAKE_STATUS=unr)    # the rig's claude-sub has the host
    assert r2.run(auth="passthrough") == 1
    said2 = _said(r2)
    assert ("the Claude binary gave no usable answer about slot 0's sign-in (it did not answer in time, or it is "
            "older than Levain accepts); nothing was launched; run `levain doctor` or try again, and if it repeats, "
            "sign in again") in said2
    assert "sign-in server" not in said2 and "stored login is kept" not in said2


@bounded(60)
def test_identity_changed_text_needs_roots_record_to_say_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ans = json.dumps({"ok": True, "identity": {"observed_id": ROOT_ID, "result": "matched"}}, sort_keys=True,
                     separators=(",", ":"))
    r = g3rig(tmp_path, monkeypatch, FAKE_STATUS=ans)
    assert r.run(auth="passthrough") == 1
    said = _said(r)
    assert ROOT_ID not in said and PLANTED not in said and "root's record does not name an account" in said
    assert [v["verb"] for v in r.verbs()][-1] == "teardown"


@bounded(60)
def test_identity_changed_status_refused_is_said_and_teardown_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                                   ) -> None:
    r = g3rig(tmp_path, monkeypatch, FAKE_REFUSE="status=lease-dead")
    assert r.run(auth="passthrough") == 1
    assert "refused lease-dead" in _said(r) and PLANTED not in _said(r)
    assert [v["verb"] for v in r.verbs()][-1] == "teardown"


@bounded(60)
def test_identity_changed_text_makes_no_custody_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L3 r1 (complement + codex): start's park-back is best-effort, so the supervisor never says where the login
    is; teardown's own line is the only custody answer."""
    r = g3rig(tmp_path, monkeypatch)
    assert r.run(auth="passthrough") == 1
    said = _said(r)
    assert "back in the slot's keep" not in said and "is back" not in said
    assert "Nothing was launched." in said


@bounded(60)
@pytest.mark.parametrize("exc", [OSError(5, "injected"), TimeoutError("injected"), ValueError("injected")])
def test_identity_refused_never_raises_and_teardown_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         exc: Exception) -> None:
    r = g3rig(tmp_path, monkeypatch)

    def boom(ans):
        raise exc
    monkeypatch.setattr(SV, "identity_answer", boom)
    assert r.run(auth="passthrough") == 1
    said = _said(r)
    assert "identity-changed" in said and PLANTED not in said
    assert [v["verb"] for v in r.verbs()][-1] == "teardown"


def _codex_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str) -> Rig:
    probe = {"address": "192.0.2.10", "addresses": 2, "host": host, "ok": True, "probe": "closed"}
    r = rig(tmp_path, monkeypatch, FAKE_PROBES="raw",
            FAKE_PROBE_LINE=json.dumps(probe, sort_keys=True, separators=(",", ":")))
    base = json.loads(r.store.joinpath("routes", "base.json").read_text())
    base["routes"].append({"route": "codex-sub", "type": "subscription_passthrough", "hosts": ["chatgpt.com"]})
    r.store.joinpath("routes", "base.json").write_text(json.dumps(base, sort_keys=True, separators=(",", ":")))
    return r


@bounded(60)
def test_codex_line_is_printed_before_start_for_codex_interactive_passthrough(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _codex_rig(tmp_path, monkeypatch, "chatgpt.com")
    SV.Supervisor(SV.Request(vendor="codex", repo=r.repo, auth="passthrough"), r.seams).run()
    err = r.stderr()
    line = ("Levain reads no account for Codex, so it cannot tell which account this slot's stored login "
            "belongs to (not a check).")
    assert line in err and err.index(line) < err.index("codex exited")
    assert [v["verb"] for v in r.verbs()].index("start") >= 0


@bounded(60)
def test_codex_line_is_not_printed_for_other_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for d in "abc":
        (tmp_path / d).mkdir()
    r = _codex_rig(tmp_path / "a", monkeypatch, "api.openai.com")
    SV.Supervisor(SV.Request(vendor="codex", repo=r.repo, auth="brokered"), r.seams).run()
    assert "codex exited" in r.stderr() and "Levain reads no account for Codex" not in r.stderr()
    r = _codex_rig(tmp_path / "b", monkeypatch, "chatgpt.com")
    SV.Supervisor(SV.Request(vendor="codex", repo=r.repo, auth="passthrough", kind="login"), r.seams).run()
    assert "codex exited" in r.stderr() and "Levain reads no account for Codex" not in r.stderr()
    r = rig(tmp_path / "c", monkeypatch)
    r.run(auth="passthrough")
    assert "claude exited" in r.stderr() and "Levain reads no account for Codex" not in r.stderr()


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

@bounded(60)
def test_relay_ends_when_the_helper_exits_though_the_operator_keeps_typing(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    # FAKE_START=raw: with the slave cooked while the fake shuts down, its echo of the typing could land after
    # `start`'s line (start-result-unreadable, run() == 1); the real slave is raw under sudo until it exits.
    r = rig(tmp_path, monkeypatch, FAKE_START="raw")
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


@bounded(60)
def test_relay_ends_on_the_drained_master_when_no_hangup_comes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                               ) -> None:
    # With the helper as the pty's session leader, its exit hangs the pty up and the master reads EIO. Without a
    # hangup (no controlling terminal), only "exited, and a read pass found the master empty" ends the relay.
    r = rig(tmp_path, monkeypatch, FAKE_START="raw")
    real = r.seams.spawn
    r.seams.spawn = lambda argv, **kw: real(argv, **{**kw, "ctty": False})
    stop = threading.Event()

    def typist() -> None:
        while not stop.is_set():
            try:
                os.write(r.in_w, b"k")
            except OSError:
                return
            time.sleep(0.05)
    threading.Thread(target=typist, daemon=True).start()
    t0 = time.monotonic()
    try:
        assert r.run() == 0
    finally:
        stop.set()
    assert time.monotonic() - t0 < 30


@bounded(60)
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


# --- prel3fix r1: kitty sets on the slot's own entry, pops beyond its pushes, and 1049l without its 1049h -----------

@pytest.mark.parametrize("data, undo", [
    (b"\x1b[>1u\x1b[=5u", b"\x1b[<1u"),               # a set on the slot's own pushed entry: the pop undoes it
    (b"\x1b[>1u\x1b[=5;2u\x1b[>3u\x1b[=1u", b"\x1b[<2u"),
    (b"\x1b[?1049h\x1b[>1u\x1b[=5u", b"\x1b[<1u\x1b[?1049l"),
    (b"\x1b[>1u\x1b[<1u\x1b[=5u", b"\x1b[=0u"),       # its pushes already popped: the set is on an entry it may not own
])
def test_a_kitty_set_is_undone_by_zeroing_only_when_the_slot_had_nothing_pushed(data, undo) -> None:
    f = TermFilter()
    f.feed(data)
    assert f.reset() == undo


@pytest.mark.parametrize("data, passed, undo", [
    # a pop beyond the slot's own pushes would pop the operator's entries on the real terminal: clipped to them
    (b"\x1b[>1u\x1b[<5u", b"\x1b[>1u\x1b[<1u", b""),
    (b"\x1b[>1u\x1b[>2u\x1b[<9u", b"\x1b[>1u\x1b[>2u\x1b[<2u", b""),
    (b"\x1b[<u", b"", b""),                            # no pushes: dropped
    (b"\x1b[<3u", b"", b""),
    (b"\x1b[>1u\x1b[<u", b"\x1b[>1u\x1b[<u", b""),    # within the pushes: passed as written
    (b"\x1b[>1u\x1b[<0u", b"\x1b[>1u", b"\x1b[<1u"),  # a pop of 0: terminals differ on it, dropped
    (b"\x1b[?1049h\x1b[>1u\x1b[?1049l\x1b[<1u", b"\x1b[?1049h\x1b[>1u\x1b[?1049l", b"\x1b[?47h\x1b[<1u\x1b[?47l"),
])
def test_the_filter_never_passes_a_kitty_pop_beyond_the_slots_own_pushes(data, passed, undo) -> None:
    f = TermFilter()
    assert f.feed(data) == passed
    assert f.reset() == undo


def test_kitty_pushes_past_the_cap_are_dropped_and_so_are_their_pops() -> None:
    from levain.slot.supervisor import _KITTY_PUSH_MAX
    f = TermFilter()
    push = b"\x1b[>1u"
    assert f.feed(push * (_KITTY_PUSH_MAX + 3)) == push * _KITTY_PUSH_MAX
    assert f.feed(b"\x1b[<" + str(_KITTY_PUSH_MAX + 3).encode() + b"u") == b"\x1b[<" + str(_KITTY_PUSH_MAX).encode() + b"u"
    assert f.reset() == b""
    g = TermFilter()
    g.feed(push * (_KITTY_PUSH_MAX + 1))
    assert g.reset() == b"\x1b[<" + str(_KITTY_PUSH_MAX).encode() + b"u"


@pytest.mark.parametrize("data, undo", [
    (b"\x1b[?47h\x1b[>1u", b"\x1b[<1u\x1b[?47l"),     # left the way it was entered: no 1049l without its 1049h
    (b"\x1b[?1047h\x1b[>1u\x1b[=5u", b"\x1b[<1u\x1b[?1047l"),
])
def test_kitty_entries_on_an_alternate_screen_the_slot_is_still_on_leave_only_the_modes_it_set(data, undo) -> None:
    f = TermFilter()
    f.feed(data)
    assert f.reset() == undo


# --- prel3fix L3 r1 A1: the screen the terminal is on is one state, and kitty entries are charged to it ---------------

@pytest.mark.parametrize("data, passed, undo", [
    # codex-1: 1047l returns the terminal to the main screen, so the second push lands on the main stack
    (b"\x1b[?1049h\x1b[>1u\x1b[?1047l\x1b[>2u", b"\x1b[?1049h\x1b[>1u\x1b[?1047l\x1b[>2u",
     b"\x1b[?47h\x1b[<1u\x1b[?47l\x1b[<1u"),
    # complement-2: 47h while on the alternate screen, then 1049l: the terminal is back on main
    (b"\x1b[?1049h\x1b[?47h\x1b[?1049l\x1b[>1u", b"\x1b[?1049h\x1b[?47h\x1b[?1049l\x1b[>1u", b"\x1b[<1u"),
    # complement-2: after XTRESTORE of an alternate-screen mode the screen is unknown: a kitty push is not passed,
    # and teardown visits both stacks, landing on main
    (b"\x1b[?1049h\x1b[>1u\x1b[?1049l\x1b[?1049r\x1b[>2u", b"\x1b[?1049h\x1b[>1u\x1b[?1049l\x1b[?1049r",
     b"\x1b[?47h\x1b[<1u\x1b[?47l"),
    (b"\x1b[>1u\x1b[?47r", b"\x1b[>1u\x1b[?47r", b"\x1b[?47l\x1b[<1u"),
    (b"\x1b[>1u\x1b[?1049r\x1b[<1u\x1b[=5u", b"\x1b[>1u\x1b[?1049r", b"\x1b[?47l\x1b[<1u"),
    # an unknown screen is known again after the next alternate-screen DECSET or DECRST
    (b"\x1b[?1049r\x1b[?1049l\x1b[>1u", b"\x1b[?1049r\x1b[?1049l\x1b[>1u", b"\x1b[<1u"),
    (b"\x1b[?1049r\x1b[?1049h\x1b[>1u", b"\x1b[?1049r\x1b[?1049h\x1b[>1u", b"\x1b[<1u\x1b[?1049l"),
    # codex-6: the alternate stack is revisited with the non-clearing 47 pair, never 1049 (which clears that screen)
    (b"\x1b[?47h\x1b[>1u\x1b[?47l", b"\x1b[?47h\x1b[>1u\x1b[?47l", b"\x1b[?47h\x1b[<1u\x1b[?47l"),
    # left by the mode that entered it from main: 1049l restores the cursor 1049h saved there
    (b"\x1b[?1049h\x1b[?47h\x1b[>1u", b"\x1b[?1049h\x1b[?47h\x1b[>1u", b"\x1b[<1u\x1b[?1049l"),
])
def test_kitty_entries_are_charged_to_the_screen_the_terminal_is_on(data, passed, undo) -> None:
    f = TermFilter()
    assert f.feed(data) == passed
    assert f.reset() == undo
    assert f.reset() == b""


# --- prel3fix L3 r1 A2: kitty keyboard sequences pass only in the protocol's own grammar ------------------------------

@pytest.mark.parametrize("data", [
    b"\x1b[>1;2u",                 # codex-2: a push takes one optional flags parameter
    b"\x1b[>1:2u", b"\x1b[>1$u",   # a sub-parameter, an intermediate
    b"\x1b[=5;9u",                 # codex-2: a set's mode is 1, 2 or 3
    b"\x1b[=5;1;2u", b"\x1b[=5;u", b"\x1b[=;1u", b"\x1b[=u", b"\x1b[=5:1u",
    b"\x1b[<1;2u", b"\x1b[<1:2u",
])
def test_a_malformed_kitty_sequence_is_dropped_and_records_nothing(data) -> None:
    f = TermFilter()
    assert f.feed(b"\x1b[>1u" + data) == b"\x1b[>1u"
    assert f.reset() == b"\x1b[<1u"
    g = TermFilter()
    assert g.feed(data) == b""
    assert g.reset() == b""


@pytest.mark.parametrize("data, undo", [
    (b"\x1b[>u", b"\x1b[<1u"),     # a push with its flags omitted (zero)
    (b"\x1b[>31u", b"\x1b[<1u"),
    (b"\x1b[=5u", b"\x1b[=0u"),    # a set with its mode omitted (1)
    (b"\x1b[=5;1u", b"\x1b[=0u"), (b"\x1b[=5;2u", b"\x1b[=0u"), (b"\x1b[=5;3u", b"\x1b[=0u"),
])
def test_a_well_formed_kitty_sequence_passes(data, undo) -> None:
    f = TermFilter()
    assert f.feed(data) == data
    assert f.reset() == undo


# --- prel3fix L3 r1 A3: the push cap leaves the operator's entries room on the smallest stack sourced ---------------

def test_a_slot_passes_at_most_four_kitty_pushes_per_screen() -> None:
    # kitty keeps 8 entries per screen ("main_key_encoding_flags[8]" in kitty/screen.h) and evicts the oldest on a
    # push past them; the protocol names no size. Four leaves the operator half of that stack.
    push = b"\x1b[>1u"
    f = TermFilter()
    assert f.feed(push * 5) == push * 4
    assert f.feed(b"\x1b[?1049h" + push * 5) == b"\x1b[?1049h" + push * 4
    assert f.reset() == b"\x1b[<4u\x1b[?1049l\x1b[<4u"
