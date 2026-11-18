"""levain#133: the three S14 harness fixes found in the 1118 VM sitting (project_memory/1118-vm/RESULTS.md).

(a) the W2/T2 doctor-row checks take the healthy "total" row and the fresh BYOK-only row at the exact figures, and fail any other
warning row; (b) every guest invocation as the dispatch account sets HOME to the dispatch dir; (c) a nonzero guest exit's
GuestError text carries each stream's tail, with stdout redacted of the stdin text and of capabilities. The closures inside
``run_wave3`` are reached by running it against the existing fake world, not by copying their bodies."""
from __future__ import annotations

import subprocess

import pytest

from scripts.vm_harness import runbook as RB
from scripts.vm_harness import wave3_e2e as W
from scripts.vm_harness.driver import GuestError
from scripts.vm_harness.host_profile import DARWIN, LINUX
from tests.test_vm_harness_wave3_e2e import CRED, STOP_DIR, World, by_id, go


class WordingWorld(World):
    def __init__(self, row_tail, **kw):
        super().__init__(**kw)
        self.row_tail = row_tail

    def doctor_rows(self):
        return ["  [OK] slots fine", f"  [OK] invoice ceiling ({CRED}): {self.row_tail}"]


BYOK = "BYOK usage is not counted against this limit"
CAPPED = "cap $2.00 per day, not a total: spend can pass it across periods"


@pytest.mark.parametrize("tail,remaining", [
    ("key limit $2.00 total, $1.60 remaining (as keys read it now)", "1.60"),     # the doctor's healthy row
    (f"key limit $2.00, $2.00 remaining; {BYOK} (as keys read it now)", "2.00"),  # a fresh key, only BYOK uncounted
])
def test_w2_takes_the_healthy_row_and_the_fresh_byok_only_row(tmp_path, tail, remaining):
    _w, _ev, (status, steps, path) = go(tmp_path, WordingWorld(tail))
    s = by_id(steps)
    assert s["W2"].status == "PASS", path.read_text()
    assert s["W2"].raw.startswith(f"key limit $2.00, remaining ${remaining}")


@pytest.mark.parametrize("tail", [
    "key limit $2.00, $1.60 remaining",                                    # a warning row, no total and no caveat named
    f"key limit $2.00, $1.60 remaining; {BYOK}",                           # BYOK, but not the fresh remainder
    f"key limit $5.00, $5.00 remaining; {BYOK}",                           # BYOK, but not the sitting's limit
    f"key limit $2.00, $2.00 remaining; {CAPPED}",                         # a resetting cap
    f"key limit $2.00, $2.00 remaining; {CAPPED}; {BYOK}",                 # a resetting cap and BYOK
    f"key limit $2.00, $0.00 remaining; the key is exhausted: OpenRouter will refuse its calls; {BYOK}",   # exhausted
])
def test_w2_fails_a_warning_row_that_does_not_match_both_figures(tmp_path, tail):
    _w, _ev, (status, steps, _p) = go(tmp_path, WordingWorld(tail))
    assert by_id(steps)["W2"].status == "FAIL" and status == "FAIL"


def test_w2_still_refuses_a_row_that_is_not_a_key_limit_row(tmp_path):
    _w, _ev, (status, steps, _p) = go(tmp_path, WordingWorld("no ceiling read: provider unreachable"))
    s = by_id(steps)
    assert s["W2"].status == "FAIL" and status == "FAIL"


def _home_before_interpreter(a):
    home = f"HOME={STOP_DIR}"
    assert a[:5] == ["sudo", "-n", "-u", "_levain_dispatch", "/usr/bin/env"], a
    assert home in a, a
    i = a.index(home)
    assert i > 4, a
    return i


def test_every_dispatch_verb_argv_sets_home_to_the_dispatch_dir_before_levain(tmp_path):
    """``dispatch_argv`` (the verbs file/show/sign/recommend/authorise), reached through run_wave3."""
    w, _ev, (status, _s, _p) = go(tmp_path)
    assert status == "PASS"
    verbs = [a for a in w.argvs if "dispatch" in a and "levain" in a]
    assert verbs
    for a in verbs:
        assert _home_before_interpreter(a) < a.index(DARWIN.python) < a.index("levain"), a


def test_the_dispatch_user_readers_set_home_before_the_interpreter(tmp_path):
    """``dispatch_user_argv`` (ticket and record readers), reached through run_wave3 by their stdin programs."""
    w, _ev, (status, _s, _p) = go(tmp_path)
    assert status == "PASS"
    readers = [a for a in w.argvs if a[:5] == ["sudo", "-n", "-u", "_levain_dispatch", "/usr/bin/env"] and "-m" not in a]
    assert readers, "no dispatch-user reader argv was recorded"
    for a in readers:
        assert _home_before_interpreter(a) < a.index(DARWIN.python), a
        assert a[a.index(DARWIN.python) + 1] == "-I"


def _fake_run(monkeypatch, *, stdout, stderr, rc=2):
    from scripts.vm_harness import vm_run
    monkeypatch.setattr(vm_run, "_clone_guard", lambda vm: None)
    monkeypatch.setattr(vm_run, "tart_exec_argv", lambda vm, argv, stdin=False: ["tart", "exec", vm, *argv])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, rc, stdout=stdout, stderr=stderr))


def test_a_nonzero_guest_exit_carries_the_stdout_tail_as_well_as_stderr(tmp_path, monkeypatch):
    _fake_run(monkeypatch, stdout="refused: cannot read ~/.levain/consent/coordinator_workspaces.json\n", stderr="warn: x\n")
    run = RB.make_stdin_runner("vm", DARWIN, tmp_path, "tart_exec")
    with pytest.raises(GuestError) as e:
        run(["levain", "dispatch", "show"], "stdin-secret", 5)
    text = str(e.value)
    assert "coordinator_workspaces.json" in text and "warn: x" in text
    assert "stdin-secret" not in text


def test_each_stream_has_its_own_tail_so_a_long_stderr_cannot_push_the_refusal_out(tmp_path, monkeypatch):
    _fake_run(monkeypatch, stdout="refused: no consent file\n", stderr="B" * 2000)
    run = RB.make_stdin_runner("vm", DARWIN, tmp_path, "tart_exec")
    with pytest.raises(GuestError) as e:
        run(["x"], "", 5)
    text = str(e.value)
    assert "refused: no consent file" in text and text.count("B") == 350


def test_a_long_stdout_is_cut_to_its_own_tail(tmp_path, monkeypatch):
    _fake_run(monkeypatch, stdout="A" * 2000, stderr="B" * 10)
    run = RB.make_stdin_runner("vm", DARWIN, tmp_path, "tart_exec")
    with pytest.raises(GuestError) as e:
        run(["x"], "", 5)
    assert str(e.value).count("A") == 350 and str(e.value).count("B") == 10


SECRET = "sk-or-v1-0123456789abcdef0123456789abcdef"
CAP = "Zk3_9aQ-xLmN0pRsTuVwXyZ12345abcdEfGhIjKlMnO"


@pytest.mark.parametrize("transport", ["tart_exec", "ssh"])
def test_a_guest_that_echoes_the_stdin_secret_or_prints_a_capability_leaks_neither_into_the_error(tmp_path, monkeypatch, transport):
    from scripts.vm_harness import vm_run
    monkeypatch.setattr(vm_run, "_clone_guard", lambda vm: None)
    monkeypatch.setattr(vm_run, "tart_exec_argv", lambda vm, argv, stdin=False: ["tart", "exec", vm, *argv])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 255, stdout=f'rejected key {SECRET}\n{{"id": "dj-1", "capability": "{CAP}"}}\n', stderr=f"echo {SECRET}\n"))
    run = RB.make_stdin_runner("vm", DARWIN, tmp_path, transport)
    with pytest.raises(GuestError) as e:
        run(["levain", "dispatch", "file"], SECRET, 5)
    text = str(e.value)
    assert SECRET not in text and CAP not in text and "rejected key" in text


def test_run_wave3_writes_neither_the_stdin_text_nor_a_capability_to_its_logs(tmp_path, monkeypatch):
    from scripts.vm_harness import vm_run
    monkeypatch.setattr(vm_run, "_clone_guard", lambda vm: None)
    monkeypatch.setattr(vm_run, "tart_exec_argv", lambda vm, argv, stdin=False: ["tart", "exec", vm, *argv])
    real = subprocess.run

    class Leaky(World):
        echoed: list = []

        def stdin_run(self, argv, text, timeout=60):
            if "dispatch" in argv and "file" in argv:
                first = next(ln for ln in text.splitlines() if len(ln.strip()) >= 12)
                self.echoed.append(first)
                fake = lambda *a, **k: subprocess.CompletedProcess(
                    a, 255, stdout=f'echo {first}\n{{"id": "dj-x", "capability": "{CAP}"}}\n', stderr="ssh: dropped\n")
                monkeypatch.setattr(subprocess, "run", fake)
                try:
                    RB.make_stdin_runner("vm", DARWIN, tmp_path, "tart_exec")(argv, text, timeout)
                finally:
                    monkeypatch.setattr(subprocess, "run", real)
            return super().stdin_run(argv, text, timeout)

    w = Leaky()
    _w, ev, (_status, _steps, path) = go(tmp_path, w)
    log = (ev / "steps.log").read_text() + path.read_text()
    assert CAP not in log
    echoed = w.echoed[0]
    assert echoed not in log
    assert "ssh: dropped" in log


# ---- the two siblings the first pass left (threec10 T2, host_profile.dispatch_argv) -------------------------------------

def _t2():
    from scripts.vm_harness import threec10 as T
    import re
    sid, _n, _rc, pat, sel = next(e for e in T.EXPECTED if e[0] == "T2")
    cfg = T.Config(DARWIN, "/tmp/install", cred="or-key")
    return re.compile(T._fill(pat, cfg)), sel.replace("<cred>", re.escape("or-key"))


@pytest.mark.parametrize("tail", [
    "key limit $2.00 total, $1.60 remaining",
    f"key limit $2.00, $2.00 remaining; {BYOK}",
    f"key limit $2.00, $2.00 remaining; {BYOK} (as keys read it 3s ago)",
])
def test_threec10_t2_takes_the_healthy_row_and_the_fresh_byok_only_row(tail):
    rx, _sel = _t2()
    assert rx.fullmatch(f"  [OK] invoice ceiling (or-key): {tail}")


@pytest.mark.parametrize("tail", [
    "key limit $2.00, $1.60 remaining",
    f"key limit $2.00, $1.60 remaining; {BYOK}",
    f"key limit $5.00, $5.00 remaining; {BYOK}",
    f"key limit $2.00, $2.00 remaining; {CAPPED}",
    f"key limit $2.00, $2.00 remaining; {CAPPED}; {BYOK}",
    f"key limit $2.00, $0.00 remaining; the key is exhausted: OpenRouter will refuse its calls; {BYOK}",
])
def test_threec10_t2_refuses_a_warning_row_that_does_not_match_both_figures(tail):
    rx, _sel = _t2()
    assert not rx.fullmatch(f"  [OK] invoice ceiling (or-key): {tail}")


def test_threec10_t2_still_refuses_a_row_with_no_limit():
    rx, _sel = _t2()
    assert not rx.fullmatch("  [OK] invoice ceiling (or-key): no credit limit is set on this key: nothing at the provider bounds the invoice")


@pytest.mark.parametrize("profile", [DARWIN, LINUX], ids=lambda p: p.name)
def test_profile_dispatch_argv_sets_home_to_the_dispatch_dir_before_the_interpreter(profile):
    a = profile.dispatch_argv()
    assert a[:5] == ("sudo", "-n", "-u", profile.dispatch_user, "/usr/bin/env")
    assert f"HOME={profile.store}/dispatch" in a
    assert a.index(f"HOME={profile.store}/dispatch") < a.index(profile.python)
