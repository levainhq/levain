"""P7-1 slice S8c: the launcher's half of the brief channel (levain.agents.session_launcher), against the S4 rig (FAKE
control port, FAKE slot runtime, FAKE usage sources; nothing starts).

Refusals first. Then the binding facts (the digest rides the mint and reaches the runtime as the same value), then the
rule that a session is ``done`` only from the child's exit status AND a proven END, driven through the real
``run_session`` with a fake gated child.

What no test here holds (UNVERIFIED until the VM rows): the production ``SlotRuntime`` adapter (not built) honouring the
obligations in the ``SlotRuntime`` docstring, one real ``done`` occurrence, a vendor failing inside the slot recording
``session_exit_2``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import secrets

import pytest

from levain.agents import controller as ctl
from levain.agents import session as S
from levain.agents import session_launcher as L
from levain.slot import Refused, stage
from tests.test_agents_session_launch import EXP, _occurrence, _run, rig  # noqa: F401
from tests.test_agents_session_launcher import (KEY, FakeChild, FakeControl, FakeProc, FakeRuntime, FakeSources,
                                                build)

needs_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def launch_with(brief, key=KEY, occurrence="occ-1"):
    return S.SessionLaunch(occurrence, key, "claude", "sonnet", brief, "passthrough", EXP)


def index_text(tmp_path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted((tmp_path / "idx").iterdir()) if p.is_file())


# --- refusals first ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("brief,why", [
    ("", "brief-invalid"),
    ("SENTINEL-NUL \x00 text", "brief-invalid"),
    ("SENTINEL-SURROGATE \ud800 text", "brief-invalid"),
    ("SENTINEL-" + "a" * (stage.BRIEF_CAP - len("SENTINEL-") + 1), "brief-cap"),
    (b"bytes are not text", "brief-invalid"),
])
def test_an_unusable_brief_is_refused_by_name_before_anything_is_allocated(tmp_path, brief, why):
    ln, control, runtime = build(tmp_path)
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch_with(brief))
    assert e.value.name == why and e.value.reason_class == "standing"
    assert "SENTINEL" not in e.value.detail
    assert "RUN-TICKET-MINT" not in control.ops() and control.calls == [] and runtime.starts == []
    rec = ln.index.get(KEY)
    assert rec["state"] == "refused" and rec["reason"] == why
    assert b"SENTINEL" not in index_text(tmp_path)


def test_a_brief_of_exactly_the_cap_is_accepted(tmp_path):
    ln, control, runtime = build(tmp_path)
    ln.spawn(launch_with("a" * stage.BRIEF_CAP))
    assert control.ops()[0] == "RUN-TICKET-MINT" and len(runtime.starts) == 1


def test_stage_brief_for_refuses_a_digest_that_does_not_match_and_leaves_no_file(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    req = L.SlotStart("t" * 43, "r-0123456789abcdef", KEY, "claude", "sonnet", "SENTINEL brief",
                      "sha256:" + "0" * 64, L.HOOKS)
    with pytest.raises(L.LauncherRefused) as e:
        L.stage_brief_for(req, staging)
    assert e.value.name == "brief-digest-drift" and "SENTINEL" not in e.value.detail
    assert list(staging.iterdir()) == []


def test_stage_brief_for_refuses_an_existing_brief_and_leaves_it_as_it_was(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "brief.md").write_bytes(b"old")
    text = "new"
    req = L.SlotStart("t" * 43, "r-0123456789abcdef", KEY, "claude", "sonnet", text,
                      stage.brief_digest(text.encode()), L.HOOKS)
    with pytest.raises(Refused) as e:
        L.stage_brief_for(req, staging)
    assert e.value.name == "staging-dest-exists"
    assert (staging / "brief.md").read_bytes() == b"old"


def test_stage_brief_for_an_existing_brief_is_not_removed_on_a_drift_either(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "brief.md").write_bytes(b"old")
    req = L.SlotStart("t" * 43, "r-0123456789abcdef", KEY, "claude", "sonnet", "new", "sha256:" + "0" * 64, L.HOOKS)
    with pytest.raises(Refused):
        L.stage_brief_for(req, staging)
    assert (staging / "brief.md").read_bytes() == b"old"


def test_stage_brief_for_writes_the_exact_bytes_when_the_digest_matches(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    text = "café ☃ \"q\"\n\ttab"
    req = L.SlotStart("t" * 43, "r-0123456789abcdef", KEY, "claude", "sonnet", text,
                      "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(), L.HOOKS)
    assert L.stage_brief_for(req, staging) is None
    assert (staging / "brief.md").read_bytes() == text.encode("utf-8")


def test_an_older_control_that_does_not_know_brief_digest_is_a_standing_refusal(tmp_path):
    control = FakeControl(**{"RUN-TICKET-MINT": Refused("unknown-key")})
    ln, _, runtime = build(tmp_path, control=control)
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch_with("the brief"))
    assert e.value.name == "mint-refused:unknown-key" and e.value.reason_class == "standing"
    assert "unknown-key" not in L._TRANSIENT
    assert ln.index.get(KEY)["state"] == "refused" and ln.index.get(KEY)["reason_class"] == "standing"
    assert runtime.starts == []
    with pytest.raises(L.LauncherRefused) as again:                # a standing record is never retaken
        ln.spawn(launch_with("the brief"))
    assert again.value.name == "session-key-already-used"


def test_a_transient_refusal_is_retaken_only_by_the_occurrence_that_was_refused(tmp_path):
    control = FakeControl(**{"RUN-TICKET-MINT": Refused("no-free-slot")})
    ln, _, runtime = build(tmp_path, control=control)
    with pytest.raises(L.LauncherRefused):
        ln.spawn(launch_with("the brief", occurrence="occ-1"))
    assert ln.index.get(KEY)["occurrence_id"] == "occ-1"
    del control.script["RUN-TICKET-MINT"]
    with pytest.raises(L.LauncherRefused) as e:                    # another occurrence supplying the same key
        ln.spawn(launch_with("the brief", occurrence="occ-2"))
    assert e.value.name == "session-key-already-used" and runtime.starts == []
    assert ln.index.get(KEY)["state"] == "refused" and ln.index.get(KEY)["occurrence_id"] == "occ-1"
    ln.spawn(launch_with("the brief", occurrence="occ-1"))         # the refused occurrence itself retakes
    assert len(runtime.starts) == 1 and ln.index.get(KEY)["state"] == "started"


def test_a_transient_record_that_names_no_occurrence_is_not_retaken(tmp_path):
    ln, control, runtime = build(tmp_path)
    ln.index.create(KEY, "refused", reason="mint:no-free-slot", reason_class="transient")
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch_with("the brief"))
    assert e.value.name == "session-key-already-used" and control.calls == [] and runtime.starts == []


# --- binding ----------------------------------------------------------------------------------------------------------

def test_the_digest_rides_the_mint_and_reaches_the_runtime_as_the_same_value(tmp_path):
    brief = "  café ☃ \"quoted\"\nnew line\ttab\n"
    ln, control, runtime = build(tmp_path)
    ln.spawn(launch_with(brief))
    (op, fields), *_ = control.calls
    want = "sha256:" + hashlib.sha256(brief.encode("utf-8")).hexdigest()
    assert op == "RUN-TICKET-MINT" and fields["brief_digest"] == want
    (start,) = runtime.starts
    assert start.brief_digest == want and start.brief == brief
    assert fields["model_requested"] == "sonnet" == start.model


def test_the_digest_is_of_the_exact_bytes_never_a_stripped_copy(tmp_path):
    brief = "  padded brief \n\n"
    ln, control, runtime = build(tmp_path)
    ln.spawn(launch_with(brief))
    fields = control.calls[0][1]
    assert fields["brief_digest"] != "sha256:" + hashlib.sha256(brief.strip().encode()).hexdigest()
    assert fields["brief_digest"] == "sha256:" + hashlib.sha256(brief.encode()).hexdigest()


def test_the_model_always_rides_the_mint(tmp_path):
    ln, control, _ = build(tmp_path)
    ln.spawn(launch_with("the brief"))
    assert control.calls[0][1].get("model_requested") == "sonnet"


def test_the_brief_never_reaches_a_record_a_mint_field_or_a_log(tmp_path, caplog):
    sentinel = "BRIEF-SENTINEL-" + secrets.token_hex(8)
    caplog.set_level(logging.DEBUG)
    ln, control, runtime = build(tmp_path)
    child = ln.spawn(launch_with(f"task text {sentinel} end"))
    assert child.process.wait() == 0
    assert ln.index.get(KEY)["state"] == "ended"
    assert sentinel.encode() not in index_text(tmp_path)
    assert sentinel not in json.dumps(control.calls)
    assert sentinel not in caplog.text and all(sentinel not in r.getMessage() for r in caplog.records)
    (start,) = runtime.starts
    assert start.brief.count(sentinel) == 1


# --- done only from the exit status AND a proven END (D8, D9) -----------------------------------------------------------

class TrapStdout:
    """A child's stdout that carries a helper-shaped answer and fails the test if anything reads it."""

    def __init__(self, line):
        self.line = line

    def _no(self, *a, **k):
        raise AssertionError("the launcher read the child's stdout")

    read = readline = readlines = fileno = __iter__ = __next__ = _no


class StdoutChild(FakeChild):
    def __init__(self, rc, line):
        super().__init__(rc)
        self.stdout = TrapStdout(line)


class StdoutRuntime(FakeRuntime):
    def __init__(self, line, **kw):
        super().__init__(**kw)
        self.line = line

    def start(self, req):
        self.starts.append(req)
        child = StdoutChild(self.rc, self.line)
        self.children.append(child)
        return L.SlotHandle("sess-1", child)


@needs_keygen
def test_exit_0_with_a_proven_end_is_done_after_exactly_one_end(rig):
    oid = _occurrence(rig, billing="subscription")
    runtime = FakeRuntime(end=True, rc=0)
    ln = L.RealLauncher(FakeControl(), runtime, FakeSources(), L.SessionIndex(rig.store.directory / "idx"))
    _, report = _run(rig, oid, ln, exposure=EXP)
    assert (report.code, report.outcome, report.state) == (0, "exited", "done")
    assert rig.row(oid)["reason"] == "completed"
    assert runtime.ended == ["sess-1"]
    (start,) = runtime.starts
    assert start.brief_digest == stage.brief_digest(start.brief.encode("utf-8"))


@needs_keygen
def test_exit_2_the_vendor_failed_is_not_done_even_with_a_proven_end(rig):
    oid = _occurrence(rig, billing="subscription")
    runtime = FakeRuntime(end=True, rc=2)
    ln = L.RealLauncher(FakeControl(), runtime, FakeSources(), L.SessionIndex(rig.store.directory / "idx"))
    _, report = _run(rig, oid, ln, exposure=EXP)
    assert report.state == "failed"
    assert rig.row(oid)["reason"] == "session_exit_2"
    assert runtime.ended == ["sess-1"]


@needs_keygen
@pytest.mark.parametrize("end", [False, RuntimeError("teardown crashed"), {"state": "RETAINED"}])
def test_exit_0_without_a_proven_end_is_never_done(rig, end):
    oid = _occurrence(rig, billing="subscription")
    runtime = FakeRuntime(end=end, rc=0)
    ln = L.RealLauncher(FakeControl(), runtime, FakeSources(), L.SessionIndex(rig.store.directory / "idx"))
    _, report = _run(rig, oid, ln, exposure=EXP)
    assert (report.code, report.state, report.outcome) == (1, "launched", "exited")
    assert rig.row(oid)["state"] == "launched" and runtime.ended      # END was attempted: there is no END-free path


@pytest.mark.parametrize("end", [False, RuntimeError("x"), {"state": "RETAINED"}, None])
def test_the_childs_status_without_a_proven_end_reads_gate_unproven(tmp_path, end):
    ln, _, runtime = build(tmp_path, runtime=FakeRuntime(end=end, rc=0))
    child = ln.spawn(launch_with("the brief"))
    assert child.process.wait() == ctl.GATE_UNPROVEN
    assert runtime.ended == ["sess-1"]
    assert ln.index.get(KEY)["state"] == "started"


@needs_keygen
def test_stdout_is_never_read_exit_0_stays_done_when_stdout_says_failure(rig):
    oid = _occurrence(rig, billing="subscription")
    runtime = StdoutRuntime('{"ok":false,"exit":1}', end=True, rc=0)
    ln = L.RealLauncher(FakeControl(), runtime, FakeSources(), L.SessionIndex(rig.store.directory / "idx"))
    _, report = _run(rig, oid, ln, exposure=EXP)
    assert report.state == "done"


@needs_keygen
def test_stdout_is_never_read_exit_2_stays_failed_when_stdout_says_success(rig):
    oid = _occurrence(rig, billing="subscription")
    runtime = StdoutRuntime('{"ok":true,"exit":0}', end=True, rc=2)
    ln = L.RealLauncher(FakeControl(), runtime, FakeSources(), L.SessionIndex(rig.store.directory / "idx"))
    _, report = _run(rig, oid, ln, exposure=EXP)
    assert (report.state, rig.row(oid)["reason"]) == ("failed", "session_exit_2")
