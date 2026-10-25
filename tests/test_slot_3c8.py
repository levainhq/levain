"""S1 3c-8: verifier generations (CONFIG-SUBMIT), the verifier ticket resolved at mint and re-checked at ATTACH, and
the 3c-7 carried list (1025-10/3c7/HEAD_RULINGS.md): render's facts gate, a post-CAS render refusal, the start wake.

CONTROL runs through `Control.handle` on test_tickets' rig (a real signed route map and, here, a real signed verifier
item, real `ssh-keygen` keys, the ticket journal on disk). The render path runs the real runtime helper `main` on lane
F2's world (test_pool's `dispatch` fixture). What only a VM run can show (root's real uids, the installed units) is
3c-10's."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import records as R
from levain.slot import render as RD
from levain.slot import tickets as T
from tests.test_pool import F2_DISPATCH, dispatch  # noqa: F401 - fixture
from tests.test_slot_3c7_fix_r1 import F2_FACTS, VER_FACTS, _blob, _mint, _prepared, _render


# ---- carried (b) + (c): render's facts gate never compares against an absent field --------------------------------

def _gate(facts: dict, kind: str, cls: str, blob_obj: dict) -> str | None:
    from levain.slot import runtime_grammar as G

    blob = G.parse_stdin("render", canon.canonical_bytes(blob_obj))
    try:
        RD.check_ticket(blob, kind, cls, C.new_handle(), facts)
    except Refused as e:
        return e.name
    return None


@pytest.mark.parametrize("missing", ["route", "model_requested"])
def test_executor_facts_without_a_required_field_refuse(missing):
    """deepseek LOW 4: an executor ticket's facts with no `model_requested` let a model-less blob pass the model gate
    (`None == None`); with no `route` the routes gate compared against the egress routes alone."""
    facts = {k: v for k, v in T.check_facts(F2_FACTS).items() if k != missing}
    blob = {"format": "levain-session-render/1", "vendor": "claude",
            "routes": ["github"] if missing == "route" else ["anthropic", "github"]}
    assert _gate(facts, "dispatch", "egress", blob) == "ticket-facts-missing"


def test_verifier_facts_without_steps_refuse_by_name():
    """deepseek MED 2: a verifier ticket's facts with no `verifier_steps` met the blob's steps as b"[]" vs b"null"
    (`ticket-steps-mismatch`, whatever the blob held); it is the facts that are short, so `ticket-facts-missing`."""
    from tests.test_slot_3c7_fix_r1 import STEPS, _verifier_resolved

    facts = {k: v for k, v in T.check_facts(VER_FACTS).items() if k != "verifier_steps"}
    facts["verifier"] = _verifier_resolved()
    blob = {"format": "levain-session-render/1", "vendor": "claude", "routes": [], "verifier_steps": STEPS,
            "driver": "pytest_report"}
    assert _gate(facts, "verifier", "none", blob) == "ticket-facts-missing"


# ---- carried (a): no render refusal leaves the session RENDERING ----------------------------------------------------

def test_render_policy_refusal_comes_before_the_cas(dispatch):
    """1025-10 obs 2: step (2)'s checks (here the hook template the blob names and the world does not hold) read only
    alloc and the blob, so they refuse before step (1)'s CAS: the session stays ALLOCATED."""
    d, w = dispatch, dispatch.w
    s = _prepared(d)
    calls = len(w.calls)
    line = _render(d, s, _blob(hooks=["levain_queue"]))
    assert line["refused"] == "hook-template-missing", line
    assert w.calls[calls:] == [] and w.state(s).state == R.ALLOCATED


@pytest.mark.parametrize("step", ["policy", "files", "front", "mint"])
def test_render_refused_after_its_cas_revokes_the_session(dispatch, step):
    """A refusal after step (1)'s CAS answered: root REVOKEs the session (a state the reaper's END clears), never
    leaves it RENDERING, which no later render can take up. The caller is answered the render's own refusal."""
    d, w = dispatch, dispatch.w
    s = _prepared(d)

    def boom(at):
        if at == step:
            raise Refused("io-error", "injected")

    w.on_step = boom
    line = _render(d, s, _blob())
    w.on_step = None
    assert line["refused"] == "io-error", (line, w.last_err)
    assert [op for op, _ in w.calls][-1] == "REVOKE"
    assert w.state(s).state == R.REVOKED


def test_render_refused_at_its_first_cas_revokes_nothing(dispatch):
    """A step (1) CAS CONTROL refused moved nothing: no REVOKE is sent and the record is what it was."""
    d, w = dispatch, dispatch.w
    s = _prepared(d)
    ans = w.control.handle(C.Conn(0), {"v": 1, "op": "REVOKE", "id": 1, "session": s})   # another writer moved it
    assert ans["ok"], ans
    rec = w.state(s)
    line = _render(d, s, _blob())
    assert line["refused"] == "stale-generation", line
    assert "REVOKE" not in [op for op, _ in w.calls] and w.state(s) == rec


# ---- carried (c): CONTROL's start wake after the listener ----------------------------------------------------------

def test_server_start_survives_a_wake_that_raises():
    """deepseek LOW 3: `ControlServer.start` wakes the reaper after its accept thread runs. `Control._wake` records a
    raising seam in `seam_errors` and returns, so start returns with the server serving (nothing is left half-up)."""
    d = Path(tempfile.mkdtemp(prefix="lv3c8", dir="/tmp"))
    (d / "sessions").mkdir(mode=0o700)

    def wake() -> None:
        raise OSError("the reaper's wake socket is gone")

    control = C.Control(C.StateStore(d / "sessions"), os.getuid(), wake_reaper=wake)
    server = C.ControlServer(control, d / "c.sock")
    try:
        server.start()
        assert [n for n, _s, _e in control.seam_errors] == ["wake_reaper"]
        import socket

        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            c.connect(str(d / "c.sock"))                                 # listening and accepting
        finally:
            c.close()
    finally:
        server.stop()
        shutil.rmtree(d, ignore_errors=True)
