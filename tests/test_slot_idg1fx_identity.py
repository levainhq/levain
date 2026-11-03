"""1027+18 lane idg1fx: the identity gate G1's fix round 1 (the L3 of d7934e4e, 1027-18/idg1/L3_r1.txt). Each test
names the finding it closes (X1-X11 in 1027-18/idg1fx/BRIEF.md).
"""
from __future__ import annotations

import io
import json
import os
import select
import threading
import time
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import identity as ID
from levain.slot import supervisor as SV
from levain.slot.runtime_grammar import OPERATOR, RuntimeRequest
from tests.test_slot_idg1_identity import E0, _baseline, _login, env  # noqa: F401 - env is a fixture
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

UUID = "4f6c1a2e-0b7d-4c9e-8a31-2d5e6f708192"
S = "s-0000000000000001"


# --- X9: no assert on the answer -----------------------------------------------------------------------------------------

def test_x9_an_answer_outside_yes_no_is_refused_by_name(env):
    """deepseek LOW 3: an explicit refusal, never an assert (stripped under -O, the yes path would run)."""
    w, la, _ = env
    s = _login(w, la, "codex")
    ctx = None                                 # refused before any read: no context is needed
    req = RuntimeRequest("confirm-identity", OPERATOR, session=s, answer="maybe")
    with pytest.raises(Refused) as e:
        ID.confirm_identity(ctx, req, None)
    assert e.value.name == "bad-answer"
    assert not _baseline(w).exists()
    with pytest.raises(Refused) as e:
        ID.confirm_identity(ctx, RuntimeRequest("confirm-identity", OPERATOR, session=None, answer="yes"), None)
    assert e.value.name == "no-session"


# --- X11: a torn slot-state file ------------------------------------------------------------------------------------------

def test_x11_a_torn_slot_state_is_slot_state_malformed(env):
    """deepseek LOW 5: the field-only CAS precheck refuses with the one slot-state name, slot-state-malformed; nothing is written."""
    w, la, _ = env
    s = _login(w, la, "codex")
    p = E0.slot_dir(w.L) / "state"
    p.write_bytes(p.read_bytes()[:20])
    assert w.main(["confirm-identity", s, "yes"])[1]["refused"] == "slot-state-malformed"
    assert not _baseline(w).exists()


# --- X6: the root-sourced identity read ------------------------------------------------------------------------------------

def test_x6_status_answers_the_records_identity(env):
    """``status <session>`` carries CONTROL's ``identity`` field, which the slot cannot write."""
    w, la, _ = env
    s = _login(w, la, "codex")
    line = w.main(["status", s])[1]
    assert line["identity"] == {"observed_id": "not-a-check"}
    assert line["records"][0]["session"] == s


def test_x6_status_answers_a_claude_logins_result(env):
    w, la, _ = env
    w.seams.capture = lambda argv, spec, cgroup, bound: (1, b'{"error":"no-token"}\n')   # G1b: the child, faked
    s = _login(w, la, "claude")
    assert w.main(["status", s])[1]["identity"] == {"result": "identity-unparsable"}


def test_x6_status_with_no_identity_has_no_key(env):
    w, _, _ = env
    s = w.prepare()["session"]
    line = w.main(["status", s])[1]
    assert "identity" not in line
    assert "identity" not in w.main(["status"])[1]


class _Sup(SV.Supervisor):
    """The call site with the helper faked per verb (``replies[verb]``: a dict answer or a Refused)."""

    def __init__(self, vendor: str, answer, identity=None):
        super().__init__(SV.Request(vendor=vendor, repo=Path("/nonexistent"), auth="passthrough", kind="login"),
                         SV.Seams(host="linux", helper=("helper",), store=Path("/L"), control_socket=Path("/c"),
                                  base_map=Path("/b"), client_factory=lambda p: None,
                                  confirm=lambda prompt: (self.prompts.append(prompt), answer)[1]))
        self.prompts: list = []
        self.calls: list = []
        self.said: list = []
        self.replies: dict = {"status": {"records": [], **({} if identity is None else {"identity": identity})},
                              "confirm-identity": {"baseline": "written"}}

    def helper(self, *args, stdin=b"", stoppable=True):
        self.calls.append(args)
        r = self.replies[args[0]]
        if isinstance(r, Refused):
            raise r
        return r

    def say(self, msg):
        self.said.append(msg)


def test_x6_a_forged_tail_exit_does_not_skip_the_prompt():
    """codex MED 2: the pty tail can say exit 1 after a real exit 0; root's record says the login observed an id."""
    sup = _Sup("codex", True, {"observed_id": "not-a-check"})
    sup._vendor_exit = 1                       # what a forged tail line would have set
    sup._confirm_identity(S)
    assert sup.calls == [("status", S), ("confirm-identity", S, "yes")]
    assert "not a check" in sup.prompts[0]


def test_x6_a_forged_tail_exit_zero_does_not_make_a_prompt():
    sup = _Sup("codex", True, {"result": "login-exit-nonzero"})
    sup._vendor_exit = 0
    sup._confirm_identity(S)
    assert sup.prompts == [] and ("confirm-identity", S, "yes") not in sup.calls
    assert any("login-exit-nonzero" in m for m in sup.said)


def test_x6_the_prompt_shows_the_observed_id_from_root():
    """RULINGS G1b's root-sourced id display: a Claude login with an observed id is asked with that id shown."""
    sup = _Sup("claude", True, {"observed_id": UUID})
    sup._confirm_identity(S)
    assert UUID in sup.prompts[0]
    assert sup.calls[-1] == ("confirm-identity", S, "yes")


def test_x6_no_identity_in_the_record_is_not_asked():
    for ident in (None, {"result": "identity-call-unbuilt"}):
        sup = _Sup("claude", True, ident)
        sup._confirm_identity(S)
        assert sup.prompts == [] and [c[0] for c in sup.calls] == ["status"]


def test_x6_a_refused_or_malformed_status_read_is_not_asked():
    for reply in (Refused("no-session", "x"), {"records": [], "identity": {"observed_id": "a b"}},
                  {"records": [], "identity": "not-a-check"}):
        sup = _Sup("codex", True)
        sup.replies["status"] = reply
        sup._confirm_identity(S)
        assert sup.prompts == [] and [c[0] for c in sup.calls] == ["status"]
        assert any("no baseline written" in m for m in sup.said)


# --- X8: "baseline written" only on root's answer -------------------------------------------------------------------------

def test_x8_baseline_written_only_on_roots_written_answer():
    """deepseek MED 2: an answer that does not say root wrote it is never reported as written."""
    sup = _Sup("codex", True, {"observed_id": "not-a-check"})
    sup.replies["confirm-identity"] = {}
    sup._confirm_identity(S)
    assert not any(m.startswith("baseline written") for m in sup.said)
    sup = _Sup("codex", True, {"observed_id": "not-a-check"})
    sup.replies["confirm-identity"] = Refused("lease-dead", "x")
    sup._confirm_identity(S)
    assert "no baseline written: refused lease-dead" in sup.said


def test_x8_confirm_yes_answers_written(env):
    w, la, _ = env
    s = _login(w, la, "codex")
    assert w.main(["confirm-identity", s, "yes"])[1] == {"ok": True, "baseline": "written"}
    assert canon.parse(_baseline(w).read_bytes())["session"] == s


# --- X7: the prompt write cannot hold the lease ---------------------------------------------------------------------------

def test_x7_a_stalled_stderr_does_not_hold_the_prompt(tmp_path, monkeypatch):
    """codex MED 4: stderr's pipe is full and nobody reads it; the prompt returns no answer within the deadline (which
    starts before the write) instead of blocking teardown forever."""
    monkeypatch.setattr(SV, "CONFIRM_S", 0.5)
    r, wfd = os.pipe()
    os.set_blocking(wfd, False)
    try:
        while True:
            os.write(wfd, b"x" * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(wfd, True)
    err = os.fdopen(wfd, "w")
    master, slave = os.openpty()
    got: list = []
    try:
        sup = SV.Supervisor(SV.Request(vendor="codex", repo=tmp_path, auth="passthrough", kind="login"),
                            SV.Seams(host="linux", helper=("helper",), store=Path("/L"), control_socket=Path("/c"),
                                     base_map=Path("/b"), client_factory=lambda p: None, stdin=slave, err=err))
        t = threading.Thread(target=lambda: got.append(sup._ask("q? ")), daemon=True)
        t0 = time.monotonic()
        t.start()
        t.join(5)
        stuck = t.is_alive()
        if stuck:                              # free the blocked write so the thread ends
            os.set_blocking(r, False)
            try:
                while True:
                    os.read(r, 1 << 16)
            except BlockingIOError:
                pass
            t.join(5)
        assert not stuck, "the prompt write blocked past its deadline"
        assert got == [None] and time.monotonic() - t0 < 3
    finally:
        os.close(master)
        os.close(slave)
        os.close(r)
        try:
            err.close()
        except OSError:
            pass


# --- X10: a long line leaves nothing behind ---------------------------------------------------------------------------------

def test_x10_a_long_line_is_no_and_its_rest_is_flushed(tmp_path):
    """complement LOW 6: past the line cap the answer is no and the rest of the line does not reach his shell."""
    master, slave = os.openpty()
    try:
        sup = SV.Supervisor(SV.Request(vendor="codex", repo=tmp_path, auth="passthrough", kind="login"),
                            SV.Seams(host="linux", helper=("helper",), store=Path("/L"), control_socket=Path("/c"),
                                     base_map=Path("/b"), client_factory=lambda p: None, stdin=slave,
                                     err=io.StringIO()))
        os.write(master, b"y" * 200 + b"\n")
        assert sup._ask("q? ") is False
        assert select.select([slave], [], [], 0.3)[0] == []
    finally:
        os.close(master)
        os.close(slave)
