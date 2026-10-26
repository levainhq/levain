"""The ``com.levain.pf`` boot one-shot (CONTRACT §23, §24): after a reboot pf is disabled, the ``com.apple/levain``
anchor is empty and lo0 has none of the slots' ``127.0.1.x`` aliases (measured in r3c10-vm 10-26, adoptvm RETURN
"Environment notes"), so every session start refused ``firewall-open`` / ``loopback-alias-down`` until an operator
redid them by hand. The unit was in :data:`levain.slot.units.SERVICES` with no program, so setup and repair printed
``pending: ... com.levain.pf not installed`` and installed nothing.

Plan and program assertions run against fakes; nothing here touches pf, lo0 or launchd."""
from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from levain.slot import Refused, pf, service_boot, units
from levain.slot.ids import SlotId, loopback_alias
from levain.slot.services import pf as PS

ROOT = Path(__file__).resolve().parents[1]
EGRESS = (SlotId("egress", 0), SlotId("egress", 1))
VERIFY = (SlotId("none", 0),)


# --- the reproduced failure: setup / repair installed no com.levain.pf ---------------------------------------------


def test_the_pf_program_exists_so_setup_installs_the_unit():
    have, pending = units.built_services("darwin", ROOT / "levain")
    assert "pf" in {s.name for s in have}
    assert not [p for p in pending if "com.levain.pf" in p], pending


def test_linux_has_no_pf_unit():
    have, _ = units.built_services("linux", ROOT / "levain")
    assert "pf" not in {s.name for s in have}


def test_the_unit_runs_this_program_as_root_and_is_retried_only_on_failure():
    import plistlib

    svc = units.by_name("pf")
    job = plistlib.loads(units.launchd_plist_text(svc).encode())
    assert job["UserName"] == "root" and job["RunAtLoad"] is True
    assert job["KeepAlive"] == {"SuccessfulExit": False}
    assert job["ProgramArguments"][-1] == "pf"
    # service-entry runs levain.slot.services.<name> for a root service with no run-dir wait
    assert svc.user == "root" and "pf" not in service_boot.RUN_DIR_OF


# --- boot_steps ---------------------------------------------------------------------------------------------------


def test_boot_steps_are_setups_aliases_then_the_enable_reference_then_the_recorded_rules_last():
    """L3 r1 codex HIGH: the anchor load is the LAST readiness-publishing step, so a failed enable (an unreadable token
    path fails before ``pfctl -E``) leaves the anchor empty and every egress start refused, never pf off with the rules
    and alias in place."""
    steps = pf.boot_steps(EGRESS + VERIFY)
    aliases = pf.alias_steps(EGRESS + VERIFY)
    assert steps[:len(aliases)] == aliases
    assert [s.argv[3] for s in aliases] == [loopback_alias(s) for s in EGRESS]   # verifier pairs get none
    rest = steps[len(aliases):]
    assert [s.why for s in rest] == [
        f"create {pf.PF_TOKEN.parent}",
        f"take pf's enable reference, its token kept in {pf.PF_TOKEN}",
        f"load the recorded rules {pf.ANCHOR_FILE} into the pf anchor {pf.ANCHOR}",
    ]
    # the one-shot never writes the rules file: only setup, replenish and repair do
    assert not [s for s in steps if s.write is not None]


def test_setups_load_and_enable_steps_are_unchanged():
    ids, uid = {}, 3987            # the darwin slot range, counted down
    for s in EGRESS:
        for name in s.accounts:
            ids[name], uid = uid, uid - 1
    steps = pf.load_steps(EGRESS, ids)
    assert [s.why for s in steps[1:]] == [f"load the rules into the pf anchor {pf.ANCHOR}",
                                          f"create {pf.PF_TOKEN.parent}",
                                          f"take pf's enable reference, its token kept in {pf.PF_TOKEN}"]
    assert steps[1].argv == ("/sbin/pfctl", "-o", "none", "-a", "com.apple/levain", "-f", "/etc/pf.anchors/levain")


class _Pfctl:
    def __init__(self, rc=0, err=""):
        self.rc, self.err, self.calls = rc, err, []

    def __call__(self, argv, **kw):
        self.calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, self.rc, "", self.err)


def _rules(tmp_path: Path, text: str = pf.ANCHOR_HEADER + " (test)\nblock out quick proto tcp user 1\n",
           mode: int = 0o644) -> Path:
    p = tmp_path / "levain"
    p.write_text(text)
    os.chmod(p, mode)
    return p


def _as_root_owner(monkeypatch):
    """The rules file in tmp is this test's; read it as root's (the check under test is the header and the mode)."""
    real = os.fstat

    def fstat(fd):
        st = real(fd)
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0, *tuple(st)[6:10]))
    monkeypatch.setattr(pf.os, "fstat", fstat)


def test_load_recorded_anchor_loads_the_file_as_it_is(tmp_path, monkeypatch):
    _as_root_owner(monkeypatch)
    path, run = _rules(tmp_path), _Pfctl()
    assert pf.load_recorded_anchor(path, run=run) == (True, "")
    assert run.calls == [("/sbin/pfctl", "-o", "none", "-a", "com.apple/levain", "-f", str(path))]


@pytest.mark.parametrize("text,mode,why", [
    ("pass all\n", 0o644, "does not start with"),
    (pf.ANCHOR_HEADER + "\n", 0o666, "writable by root alone"),
])
def test_load_recorded_anchor_refuses_a_file_that_is_not_levains(tmp_path, monkeypatch, text, mode, why):
    _as_root_owner(monkeypatch)
    run = _Pfctl()
    ok, msg = pf.load_recorded_anchor(_rules(tmp_path, text, mode), run=run)
    assert not ok and why in msg and "nothing loaded" in msg
    assert run.calls == []


def test_load_recorded_anchor_refuses_a_file_owned_by_another_user(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("as root the tmp file is root's")
    run = _Pfctl()
    ok, msg = pf.load_recorded_anchor(_rules(tmp_path), run=run)
    assert not ok and "is not root's file" in msg and run.calls == []


def test_load_recorded_anchor_refuses_a_link_and_a_missing_file(tmp_path):
    run = _Pfctl()
    target = _rules(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(target)
    assert not pf.load_recorded_anchor(link, run=run)[0]
    ok, msg = pf.load_recorded_anchor(tmp_path / "absent", run=run)
    assert not ok and "cannot open" in msg
    assert run.calls == []


def test_load_recorded_anchor_reports_pfctls_failure(tmp_path, monkeypatch):
    _as_root_owner(monkeypatch)
    ok, msg = pf.load_recorded_anchor(_rules(tmp_path), run=_Pfctl(1, "syntax error"))
    assert not ok and "syntax error" in msg


def test_after_a_reboot_the_old_token_is_released_as_gone_and_the_new_one_kept(tmp_path):
    """A reboot drops every pf reference, so the recorded token names one pf no longer knows: ``-E`` first (pf is then
    enabled), and ``-X old`` answers ``pf: token invalid`` (measured, 1c VM row C21), which is released-as-gone."""
    token = tmp_path / "pf.token"
    token.write_text("111\n")
    calls = []

    def run(argv, **kw):
        calls.append(tuple(argv))
        if argv[1] == "-E":
            return subprocess.CompletedProcess(argv, 0, "", "pf enabled\nToken : 222\n")
        return subprocess.CompletedProcess(argv, 1, "", "pf: token invalid\n")

    assert pf.take_enable_reference(token, run=run)[0]
    assert calls == [("/sbin/pfctl", "-E"), ("/sbin/pfctl", "-X", "111")]
    assert token.read_text() == "222\n"


# --- the program ----------------------------------------------------------------------------------------------------


@contextmanager
def _free():
    yield 3


@contextmanager
def _busy():
    raise Refused("install-busy", "another install-helper run holds the lock")
    yield 3   # pragma: no cover


def _main(run_rc: int = 0, **kw):
    ran: list = []
    args = dict(platform="darwin", euid=lambda: 0, lock=_free, slots=lambda: EGRESS,
                run=lambda steps: (ran.append(list(steps)), run_rc)[1])
    args.update(kw)
    return PS.main([], **args), ran


def test_main_runs_boot_steps_for_the_slots_in_service():
    rc, ran = _main()
    assert rc == 0
    assert [s.why for s in ran[0]] == [s.why for s in pf.boot_steps(EGRESS)]


def test_main_with_nothing_installed_exits_0_and_runs_nothing():
    rc, ran = _main(slots=lambda: None)
    assert rc == 0 and ran == []


def test_main_while_an_install_verb_holds_the_lock_exits_tempfail_so_launchd_retries():
    rc, ran = _main(lock=_busy)
    assert rc == PS.EX_BUSY == 75 and ran == []


def test_a_failed_step_exits_non_zero_so_launchd_retries():
    rc, _ = _main(run_rc=1)
    assert rc == 1


def test_another_refusal_under_the_lock_exits_non_zero():
    def slots():
        raise Refused("bad-root", "/var/levain is a link")
    rc, ran = _main(slots=slots)
    assert rc == 1 and ran == []


def test_main_refuses_a_non_root_caller_and_another_host():
    assert _main(euid=lambda: 501)[0] == PS.EX_NOPERM
    assert _main(platform="linux")[0] == PS.EX_CONFIG


def test_installed_egress_slots_with_no_store_is_none(monkeypatch, tmp_path):
    from levain.slot import runtime_helper as RH

    real = RH.Seams
    monkeypatch.setattr(RH, "Seams", lambda **kw: real(**{**kw, "store": tmp_path / "absent"}))
    assert PS.installed_egress_slots() is None


# --- L3 r1 fixes -----------------------------------------------------------------------------------------------------


def test_a_token_write_that_fails_gives_the_new_reference_back(tmp_path, monkeypatch):
    """L3 r1 complement MED 1 = codex MED 2: ``-E`` took a reference and the token write raised. The new reference is
    released before the step fails, so launchd's retries never pile up references no token names."""
    token = tmp_path / "pf.token"
    calls = []

    def run(argv, **kw):
        calls.append(tuple(argv))
        if argv[1] == "-E":
            return subprocess.CompletedProcess(argv, 0, "", "pf enabled\nToken : 222\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def no_space(path, value):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pf, "_write_token", no_space)
    ok, why = pf.take_enable_reference(token, run=run)
    assert not ok and "No space left on device" in why and "222" in why
    assert calls == [("/sbin/pfctl", "-E"), ("/sbin/pfctl", "-X", "222")]
    assert not token.exists()


def test_a_token_write_that_fails_and_a_give_back_that_fails_says_both(tmp_path, monkeypatch):
    def run(argv, **kw):
        if argv[1] == "-E":
            return subprocess.CompletedProcess(argv, 0, "", "Token : 222\n")
        return subprocess.CompletedProcess(argv, 1, "", "pfctl: busy\n")

    def no_space(path, value):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pf, "_write_token", no_space)
    ok, why = pf.take_enable_reference(tmp_path / "pf.token", run=run)
    assert not ok and "No space left on device" in why and "could not be given back" in why and "busy" in why


def test_main_with_no_egress_slot_and_no_rules_file_exits_0_with_a_line(capsys):
    """L3 r1 deepseek MED 2: an operator file with no egress slot in service and no rules file is nothing to load, not a
    failure launchd retries forever."""
    rc, ran = _main(slots=lambda: (), rules_present=lambda: False)
    assert rc == 0 and ran == []
    assert "nothing to load" in capsys.readouterr().err


def test_main_with_no_egress_slot_still_loads_a_rules_file_that_is_there():
    # the file also carries the verifier pairs' rules (pf.anchor_rules), so it is loaded when it is there
    rc, ran = _main(slots=lambda: (), rules_present=lambda: True)
    assert rc == 0 and [s.why for s in ran[0]] == [s.why for s in pf.boot_steps(())]


def test_the_real_run_plan_fails_on_a_step_that_answers_false():
    """L3 r1 deepseek MED 1 / MED 3, checked against the real ``run_plan`` (``ok, why = step.call()``; ``return 1`` /
    ``return 0``): a step answering ``(False, msg)`` fails the run, and a run of good steps returns 0, never None."""
    from levain.firing.hands import Step

    assert PS._run([Step("boom", call=lambda: (False, "boom"))]) == 1
    assert PS._run([Step("fine", call=lambda: (True, ""))]) == 0
    rc = PS.main([], platform="darwin", euid=lambda: 0, lock=_free, slots=lambda: EGRESS,
                 run=lambda steps: PS._run([Step("boom", call=lambda: (False, "boom"))]))
    assert rc == 1


# --- L3 verify r2 fixes (f2) -----------------------------------------------------------------------------------------


def _pfctl(x_rc: dict):
    """A fake pfctl: ``-E`` answers token 222; ``-X <token>`` exits ``x_rc.get(token, 0)`` (1 = ``pfctl: busy``)."""
    calls = []

    def run(argv, **kw):
        calls.append(tuple(argv))
        if argv[1] == "-E":
            return subprocess.CompletedProcess(argv, 0, "", "pf enabled\nToken : 222\n")
        rc = x_rc.get(argv[2], 0)
        return subprocess.CompletedProcess(argv, rc, "", "pfctl: busy\n" if rc else "")
    return run, calls


def _no_space(path, value):
    raise OSError(28, "No space left on device")


def test_a_token_write_that_fails_after_the_old_reference_was_released_keeps_the_new_one(tmp_path, monkeypatch):
    """L3 verify r2 codex HIGH (fix-introduced): the recorded reference 111 may be the only one holding pf on under
    running sessions. Once ``-X 111`` released it, giving 222 back too would turn pf off, so 222 is kept and the result
    is the non-retriable :data:`pf.ENABLE_UNCERTAIN` (a retry would take another reference no token names)."""
    token = tmp_path / "pf.token"
    token.write_text("111\n")
    run, calls = _pfctl({})
    monkeypatch.setattr(pf, "_write_token", _no_space)
    ok, why = pf.take_enable_reference(token, run=run)
    assert calls == [("/sbin/pfctl", "-E"), ("/sbin/pfctl", "-X", "111")]   # 222 is never given back
    assert not ok and why.startswith(pf.ENABLE_UNCERTAIN)
    assert "222" in why and "No space left on device" in why


def test_a_token_write_that_fails_with_no_old_reference_gives_the_new_one_back_and_is_retriable(tmp_path, monkeypatch):
    run, calls = _pfctl({})
    monkeypatch.setattr(pf, "_write_token", _no_space)
    ok, why = pf.take_enable_reference(tmp_path / "pf.token", run=run)
    assert calls == [("/sbin/pfctl", "-E"), ("/sbin/pfctl", "-X", "222")]
    assert not ok and not why.startswith(pf.ENABLE_UNCERTAIN)


def test_a_give_back_that_cannot_be_proven_is_non_retriable(tmp_path, monkeypatch):
    """L3 verify r2 codex MED 1: a give-back of 222 that fails leaves a reference no token names; launchd's retry would
    take another, so both give-back failures (after a failed write, after a failed release of the old) are
    :data:`pf.ENABLE_UNCERTAIN`."""
    run, _ = _pfctl({"222": 1})
    monkeypatch.setattr(pf, "_write_token", _no_space)
    ok, why = pf.take_enable_reference(tmp_path / "pf.token", run=run)
    assert not ok and why.startswith(pf.ENABLE_UNCERTAIN) and "busy" in why

    token = tmp_path / "pf.token2"
    token.write_text("111\n")
    run, calls = _pfctl({"111": 1, "222": 1})
    ok, why = pf.take_enable_reference(token, run=run)
    assert calls == [("/sbin/pfctl", "-E"), ("/sbin/pfctl", "-X", "111"), ("/sbin/pfctl", "-X", "222")]
    assert not ok and why.startswith(pf.ENABLE_UNCERTAIN) and token.read_text() == "111\n"


def test_an_old_release_that_fails_with_a_give_back_that_works_stays_retriable(tmp_path):
    token = tmp_path / "pf.token"
    token.write_text("111\n")
    run, _ = _pfctl({"111": 1})
    ok, why = pf.take_enable_reference(token, run=run)
    assert not ok and not why.startswith(pf.ENABLE_UNCERTAIN) and token.read_text() == "111\n"


def test_the_one_shot_exits_0_on_an_uncertain_enable_so_launchd_does_not_retry(capsys):
    """KeepAlive ``SuccessfulExit: false`` restarts every non-zero exit, so 0 is the one exit launchd leaves; the line
    on stderr names the state, and the steps after the enable (the anchor load) do not run."""
    from levain.firing.hands import Step

    later = []
    steps = [Step("enable", call=lambda: (False, f"{pf.ENABLE_UNCERTAIN}: reference 222 held, token not recorded")),
             Step("load", call=lambda: (later.append(1), (True, ""))[1])]
    rc = PS.main([], platform="darwin", euid=lambda: 0, lock=_free, slots=lambda: EGRESS,
                 run=lambda _: PS._run(steps))
    assert rc == 0 and later == []
    err = capsys.readouterr().err
    assert pf.ENABLE_UNCERTAIN in err and "not retried" in err
    # an ordinary failed step is still retried
    assert PS.main([], platform="darwin", euid=lambda: 0, lock=_free, slots=lambda: EGRESS,
                   run=lambda _: PS._run([Step("boom", call=lambda: (False, "boom"))])) == 1


# --- L3 verify r3 fixes (f3) -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("gone", pf.PF_TOKEN_GONE)
def test_a_token_write_that_fails_after_a_reboot_dropped_the_old_reference_gives_the_new_one_back(tmp_path, monkeypatch,
                                                                                                   gone):
    """L3 verify r3 complement MED / codex MED (P5): after a reboot the recorded 111 names a reference pf no longer
    holds, so ``-X 111`` answers it gone and released nothing that held pf on. A token write that fails then is the
    no-old case: 222 is given back and the failure is the ordinary, retried one, not :data:`pf.ENABLE_UNCERTAIN` (which
    exits 0 and leaves the anchor unloaded until the next boot)."""
    token = tmp_path / "pf.token"
    token.write_text("111\n")
    calls = []

    def run(argv, **kw):
        calls.append(tuple(argv))
        if argv[1] == "-E":
            return subprocess.CompletedProcess(argv, 0, "", "pf enabled\nToken : 222\n")
        if argv[2] == "111":
            return subprocess.CompletedProcess(argv, 1, "", f"{gone}\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pf, "_write_token", _no_space)
    ok, why = pf.take_enable_reference(token, run=run)
    assert calls == [("/sbin/pfctl", "-E"), ("/sbin/pfctl", "-X", "111"), ("/sbin/pfctl", "-X", "222")]
    assert not ok and not why.startswith(pf.ENABLE_UNCERTAIN) and "222" in why and "No space left on device" in why


# --- the VM reboot run: P6 the boot lines are kept; the VM blocker case is the ordinary retried failure (P11) ---------


def test_the_pf_unit_keeps_its_lines_in_a_root_log_launchd_does_not_throw_away():
    """P6 (pfbootvm check 1): the plist named no StandardErrorPath, so launchd threw the boot run's stderr away and the
    "restored pf" / "not retried" lines could not be read after a real boot."""
    import plistlib

    job = plistlib.loads(units.launchd_plist_text(units.by_name("pf")).encode())
    assert job["StandardErrorPath"] == job["StandardOutPath"] == str(units.launchd_log_path(units.by_name("pf")))
    assert units.launchd_log_path(units.by_name("pf")) == Path("/var/log/com.levain.pf.log")


def test_the_log_is_cut_back_to_empty_once_it_passes_its_cap(tmp_path):
    log = tmp_path / "pf.log"
    log.write_bytes(b"x" * (PS.LOG_MAX_BYTES + 1))
    fd = os.open(log, os.O_WRONLY | os.O_APPEND)
    try:
        PS.bound_log(fd)
        os.write(fd, b"next\n")
    finally:
        os.close(fd)
    assert log.read_bytes() == b"next\n"
    log.write_bytes(b"y" * PS.LOG_MAX_BYTES)
    fd = os.open(log, os.O_WRONLY | os.O_APPEND)
    try:
        PS.bound_log(fd)
    finally:
        os.close(fd)
    assert log.stat().st_size == PS.LOG_MAX_BYTES        # at the cap, left as it is
    r, w = os.pipe()
    try:
        PS.bound_log(w)                                  # not a file (a terminal, a pipe): left alone
    finally:
        os.close(r)
        os.close(w)


class _Refs:
    """A fake pfctl holding real reference state: ``-E`` takes the next token, ``-X`` releases a held one or answers
    ``pf: token invalid``."""

    def __init__(self, held=()):
        self.held, self.next = list(held), 222

    def __call__(self, argv, **kw):
        if argv[1] == "-E":
            tok, self.next = str(self.next), self.next + 111
            self.held.append(tok)
            return subprocess.CompletedProcess(argv, 0, "", f"pf enabled\nToken : {tok}\n")
        assert argv[1] == "-X", argv
        if argv[2] not in self.held:
            return subprocess.CompletedProcess(argv, 1, "", "pf: token invalid\n")
        self.held.remove(argv[2])
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_with_the_token_path_unwritable_the_next_run_gives_its_reference_back_and_is_retried(tmp_path):
    """The pfbootvm blocker (a directory where the token's temp file goes, so every token write fails). Run 1 released
    the live 111 and kept 222 unrecorded: uncertain, exit 0 (f3's ruled behaviour). Run 2 reads the recorded 111 as
    gone, gives its 333 back and fails: exit 1, so launchd retries it (P11: no note stops that; the retry is the right
    behaviour while the path stays unwritable). It never exits 0 with the anchor load unrun (P5)."""
    from levain.firing.hands import Step

    token = tmp_path / "pf.token"
    (tmp_path / ".pf.token.tmp").mkdir()
    token.write_text("111\n")
    pfctl = _Refs(["111"])
    ok, why = pf.take_enable_reference(token, run=pfctl)
    assert not ok and why.startswith(pf.ENABLE_UNCERTAIN) and pfctl.held == ["222"]
    loaded = []
    steps = [Step("enable", call=lambda: pf.take_enable_reference(token, run=pfctl)),
             Step("load the anchor", call=lambda: (loaded.append(1), (True, ""))[1])]
    rc = PS.main([], platform="darwin", euid=lambda: 0, lock=_free, slots=lambda: EGRESS,
                 run=lambda _steps: PS._run(steps))
    assert rc == 1 and loaded == []
    assert pfctl.held == ["222"]                                     # 333 given back: no reference piles up
    assert token.read_text() == "111\n"
