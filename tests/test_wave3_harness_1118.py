"""levain#133: the three S14 harness fixes found in the 1118 VM sitting (project_memory/1118-vm/RESULTS.md).

(a) the W2 doctor-row regex takes both wordings of the key-limit text; (b) every guest invocation as the dispatch account sets
HOME to the dispatch dir; (c) a nonzero guest exit's GuestError text carries stdout as well as stderr. The closures inside
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


@pytest.mark.parametrize("tail", [
    "key limit $2.00 total, $1.60 remaining (read now)",                    # the old wording
    "key limit $2.00, $1.60 remaining (read now)",                          # the doctor's wording now
    "key limit $2.00, $1.60 remaining (BYOK usage $0.12, not counted)",     # a BYOK suffix after it is ignored
])
def test_w2_reads_both_key_limit_wordings(tmp_path, tail):
    _w, _ev, (status, steps, path) = go(tmp_path, WordingWorld(tail))
    s = by_id(steps)
    assert s["W2"].status == "PASS", path.read_text()
    assert s["W2"].raw.startswith("key limit $2.00, remaining $1.60")


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


def test_the_guest_error_tail_is_700_chars(tmp_path, monkeypatch):
    _fake_run(monkeypatch, stdout="A" * 2000, stderr="B" * 2000)
    run = RB.make_stdin_runner("vm", DARWIN, tmp_path, "tart_exec")
    with pytest.raises(GuestError) as e:
        run(["x"], "", 5)
    text = str(e.value)
    assert text.count("B") == 700 and "A" not in text


# ---- the two siblings the first pass left (threec10 T2, host_profile.dispatch_argv) -------------------------------------

def _t2():
    from scripts.vm_harness import threec10 as T
    import re
    sid, _n, _rc, pat, sel = next(e for e in T.EXPECTED if e[0] == "T2")
    return re.compile(pat.replace("<cred>", re.escape("or-key"))), sel.replace("<cred>", re.escape("or-key"))


@pytest.mark.parametrize("tail", [
    "key limit $2.00 total, $1.60 remaining",                                            # the old wording
    "key limit $2.00, $1.60 remaining; BYOK usage is not counted against this limit",    # the doctor's wording with a problem
])
def test_threec10_t2_takes_both_key_limit_wordings(tail):
    rx, _sel = _t2()
    assert rx.fullmatch(f"  [OK] invoice ceiling (or-key): {tail}")


def test_threec10_t2_still_refuses_a_row_with_no_limit():
    rx, _sel = _t2()
    assert not rx.fullmatch("  [OK] invoice ceiling (or-key): no credit limit is set on this key: nothing at the provider bounds the invoice")


@pytest.mark.parametrize("profile", [DARWIN, LINUX], ids=lambda p: p.name)
def test_profile_dispatch_argv_sets_home_to_the_dispatch_dir_before_the_interpreter(profile):
    a = profile.dispatch_argv()
    assert a[:5] == ("sudo", "-n", "-u", profile.dispatch_user, "/usr/bin/env")
    assert f"HOME={profile.store}/dispatch" in a
    assert a.index(f"HOME={profile.store}/dispatch") < a.index(profile.python)
