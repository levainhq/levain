"""P7-1 slice S4: the real session launcher (levain.agents.session_launcher) against a FAKE control port, a FAKE slot
runtime and FAKE usage sources. No VM, no real ``claude``, no socket, no process.

Two facts this file pins that the research left unverified (RESEARCH_p71_dispatch.md section 7):
* ``ControlClient`` has no mint method and its constructor needs a real unix socket; its one verb entry is
  ``call(op, **fields)``, so the seam is that duck type. ``test_the_launcher_mints_through_the_real_control_handler``
  runs the launcher's mint through the REAL ``Control.handle`` (the tickets rig, as the dispatch uid) to show the fields
  it sends are the ones the server side accepts, idempotently by attempt id.
* the mint answers ``{ticket, run_id}`` and ``RUN-TICKET-STATUS`` does NOT answer the run id once a ticket is bound, so
  the launcher's index is the only record of it.

What no test here holds (UNVERIFIED until the VM rows): a real credential slot started under a real passthrough ticket,
the unmodified binary under the agent fence, a real front byte total, a real broker call count, a real S1 END.
"""
from __future__ import annotations

import json

import pytest

from levain.agents import controller as ctl
from levain.agents import session as S
from levain.agents import session_launcher as L
from levain.agents.accounting import ActionExposure, StopResult, Usage
from levain.slot import Refused
from levain.slot import tickets as T
from tests.test_routemaps import st  # noqa: F401
from tests.test_agents_session_launch import EXP, ROLE, _occurrence, _reservation, _run, rig  # noqa: F401

KEY = "s-0123456789abcdef"
RUN = "r-0123456789abcdef"


def launch(key=KEY, *, auth="passthrough", exposure=EXP, harness="claude", unit=None, occurrence="occ-1"):
    if unit is not None:
        exposure = ActionExposure(exposure.actions, unit, exposure.seconds, exposure.tunnel_bytes)
    return S.SessionLaunch(occurrence, key, harness, "sonnet", "the brief", auth, exposure)


class FakeProc:
    def __init__(self, rc=0):
        self.rc, self.returncode, self.terminated = rc, None, 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self.rc
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = 143


class FakeChild:
    def __init__(self, rc=0):
        self.process, self.released, self.aborted = FakeProc(rc), False, 0

    def release(self):
        self.released = True

    def abort(self, timeout=5.0):
        self.aborted += 1
        return 0


class FakeControl:
    """A CONTROL that mints (idempotently by attempt) and records every call. ``script`` maps an op to an exception or a
    callable that replaces its answer."""

    def __init__(self, **script):
        self.calls, self.script, self.minted = [], script, {}
        self.cancelled = []

    def call(self, op, **fields):
        self.calls.append((op, fields))
        hook = self.script.get(op)
        if isinstance(hook, BaseException):
            raise hook
        if callable(hook):
            return hook(fields)
        if op == "RUN-TICKET-MINT":
            return self.minted.setdefault(fields["attempt_id"], {"ticket": "t" * 43, "run_id": RUN})
        if op == "RUN-TICKET-CANCEL":
            self.cancelled.append(fields["ticket"])
            return {}
        if op == "RUN-TICKET-STATUS":
            raise Refused("attempt-unknown")
        raise AssertionError(op)

    def ops(self):
        return [c[0] for c in self.calls]


class FakeRuntime:
    def __init__(self, end=True, rc=0, fail=None):
        self.starts, self.ended, self.end_ok, self.rc, self.fail = [], [], end, rc, fail
        self.children = []

    def start(self, req):
        self.starts.append(req)
        if self.fail:
            raise self.fail
        child = FakeChild(self.rc)
        self.children.append(child)
        return L.SlotHandle("sess-1", child)

    def end(self, session):
        self.ended.append(session)
        if isinstance(self.end_ok, BaseException):
            raise self.end_ok
        return self.end_ok


class FakeSources:
    def __init__(self, calls=7, bytes_=1000, seconds=30):
        self.v, self.asked = {"calls": calls, "bytes": bytes_, "seconds": seconds}, []

    def _get(self, name, arg):
        self.asked.append((name, arg))
        v = self.v[name]
        if isinstance(v, BaseException):
            raise v
        return v

    def calls(self, run_id):
        return self._get("calls", run_id)

    def tunnel_bytes(self, session):
        return self._get("bytes", session)

    def seconds(self, session):
        return self._get("seconds", session)


def build(tmp_path, control=None, runtime=None, sources=None, auth="passthrough"):
    control, runtime, sources = control or FakeControl(), runtime or FakeRuntime(), sources or FakeSources()
    return L.RealLauncher(control, runtime, sources, L.SessionIndex(tmp_path / "idx"), auth=auth), control, runtime


def zero():
    return Usage(0, 0, 0, "session:not-started", actions=0, tunnel_bytes=0, seconds=0)


# --- the seam facts ---------------------------------------------------------------------------------------------------

def test_the_launcher_and_its_seams_satisfy_the_protocols(tmp_path):
    ln, control, runtime = build(tmp_path)
    assert isinstance(ln, S.SessionLauncher) and ln.route == S.S1_DISPATCH and ln.auth == "passthrough"
    assert isinstance(control, L.ControlPort) and isinstance(runtime, L.SlotRuntime)
    assert isinstance(FakeSources(), L.UsageSources)
    from levain.slot.control_client import ControlClient
    assert isinstance(ControlClient.__new__(ControlClient), L.ControlPort)     # the real client has the one verb entry


def test_nothing_in_the_agents_package_other_than_the_launcher_mints():
    import pathlib
    root = pathlib.Path(L.__file__).parent
    hits = [p.name for p in root.glob("*.py") if "RUN-TICKET-MINT" in p.read_text() and p.name != "session_launcher.py"]
    assert hits == []
    text = pathlib.Path(L.__file__).read_text()
    assert text.count('self.control.call("RUN-TICKET-MINT"') == 1


# --- refusals first ---------------------------------------------------------------------------------------------------

def test_the_api_key_branch_is_refused_by_name_before_anything_is_allocated(tmp_path):
    assert L.BROKERED_ROUTE_BUILT is False
    ln, control, runtime = build(tmp_path, auth="brokered")
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch(auth="brokered", exposure=None))
    assert e.value.name == "brokered-route-not-built"
    assert control.calls == [] and runtime.starts == []
    assert ln.usage(KEY) == zero()                       # nothing ran: zero, not the caps


@pytest.mark.parametrize("kw,why", [
    ({"auth": "brokered"}, "launch-auth-not-the-launchers"),
    ({"harness": "codex"}, "harness-not-claude"),
    ({"exposure": None}, "no-action-exposure"),
    ({"unit": "turns"}, "exposure-unit-not-tool-calls"),
    ({"exposure": ActionExposure(20, "tool_calls", 3600, None)}, "no-tunnel-byte-cap"),
])
def test_each_refusal_names_its_reason_and_mints_nothing(tmp_path, kw, why):
    ln, control, runtime = build(tmp_path)
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch(**kw))
    assert e.value.name == why
    assert control.calls == [] and runtime.starts == []
    assert ln.usage(KEY) == zero()


@pytest.mark.parametrize("kw", [
    {"key": "S-UPPER"}, {"key": "s_underscore"}, {"key": "../etc"}, {"key": ""}, {"key": "s-" + "a" * 70},
    {"occurrence": "has space"}, {"occurrence": ""},
])
def test_a_key_or_job_id_outside_the_grammar_is_refused_with_no_file_outside_the_index(tmp_path, kw):
    ln, control, runtime = build(tmp_path)
    with pytest.raises((L.LauncherRefused, ValueError)):
        ln.spawn(launch(**kw))
    assert control.calls == [] and runtime.starts == []
    assert not any(p for p in tmp_path.iterdir() if p.name != "idx")


def test_a_zero_budget_or_a_cap_outside_the_servers_range_is_refused_before_the_wire(tmp_path):
    ln, control, _ = build(tmp_path)
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch(exposure=ActionExposure(0, "tool_calls", 3600, 5)))
    assert e.value.name.startswith("mint-facts:ticket-field:max_calls") and control.calls == []


# --- the mint ----------------------------------------------------------------------------------------------------------

def test_the_mint_request_is_the_passthrough_ticket_the_exposure_states(tmp_path):
    ln, control, runtime = build(tmp_path)
    ln.spawn(launch())
    (op, f), *_ = control.calls
    assert op == "RUN-TICKET-MINT"
    assert f == {"attempt_id": KEY, "job_id": "occ-1", "run_index": 0, "contract_digest": f["contract_digest"],
                 "kind": "executor", "egress_routes": [], "reserved_microdollars": 0, "auth": "passthrough",
                 "vendor": "claude", "model_requested": "sonnet", "max_calls": 20, "max_tunnel_bytes": 5_000_000,
                 "deadline_s": 3600}
    assert f["contract_digest"].startswith("sha256:") and len(f["contract_digest"]) == 71
    T.check_facts(f)                                            # the server's own shape function agrees
    (start,) = runtime.starts
    assert (start.run_id, start.session_key, start.harness, start.model, start.brief, start.hooks) \
        == (RUN, KEY, "claude", "sonnet", "the brief", ("levain_gate", "levain_queue"))


def test_the_launcher_mints_through_the_real_control_handler(st, tmp_path):
    from tests.test_slot_p7a1_passthrough_ticket import write_ceiling2
    from tests.test_tickets import Clock, Rig

    write_ceiling2(st.L)
    real = Rig(st, Clock())

    class ToControl:
        def __init__(self):
            self.n = 0

        def call(self, op, **fields):
            ans = real.ask(op, **fields)
            if ans.get("ok") is not True:
                raise Refused(ans["refused"], op)
            return {k: v for k, v in ans.items() if k not in ("id", "ok")}

    to = ToControl()
    ln = L.RealLauncher(to, FakeRuntime(), FakeSources(), L.SessionIndex(tmp_path / "idx"))
    ln.spawn(launch(occurrence="job-1"))
    (line,) = real.lines()
    assert line["attempt_id"] == KEY and line["facts"]["auth"] == "passthrough" and line["facts"]["max_calls"] == 20
    rec = L.SessionIndex(tmp_path / "idx").get(KEY)
    assert rec["state"] == "started" and T.RUN_ID_RE.fullmatch(rec["run_id"])
    assert rec["run_id"] == line["run_id"]
    # a replay of the same attempt answers the same ticket and run id (the idempotence the key-as-attempt rests on)
    again = to.call("RUN-TICKET-MINT", attempt_id=KEY, job_id="job-1", run_index=0,
                    contract_digest=line["facts"]["contract_digest"], kind="executor", egress_routes=[],
                    reserved_microdollars=0, auth="passthrough", vendor="claude", model_requested="sonnet",
                    max_calls=20, max_tunnel_bytes=5_000_000, deadline_s=3600)
    assert again["run_id"] == rec["run_id"]
    # and once the ticket is bound, STATUS gives no run id: the index is the only record of it
    status = to.call("RUN-TICKET-STATUS", attempt_id=KEY)
    assert "run_id" not in status


def test_a_refused_mint_is_a_named_refusal_and_reads_zero(tmp_path):
    ln, control, runtime = build(tmp_path, control=FakeControl(**{"RUN-TICKET-MINT": Refused("no-free-slot:egress")}))
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch())
    assert e.value.name == "mint-refused:no-free-slot:egress" and runtime.starts == []
    assert ln.usage(KEY) == zero()


def test_control_down_at_the_mint_may_have_minted_so_usage_reads_the_caps(tmp_path):
    ln, control, runtime = build(tmp_path, control=FakeControl(**{"RUN-TICKET-MINT": Refused("control-down")}))
    with pytest.raises(L.LauncherRefused):
        ln.spawn(launch())
    assert runtime.starts == []
    u = ln.usage(KEY)
    assert (u.actions, u.tunnel_bytes, u.seconds) == (None, None, None)


@pytest.mark.parametrize("ans", [{}, {"ticket": "t"}, {"run_id": RUN}, {"ticket": "t", "run_id": "r-short"},
                                 {"ticket": 7, "run_id": RUN}, {"ticket": "", "run_id": RUN}])
def test_a_malformed_mint_answer_starts_nothing(tmp_path, ans):
    ln, control, runtime = build(tmp_path, control=FakeControl(**{"RUN-TICKET-MINT": lambda f: ans}))
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch())
    assert e.value.name == "mint-answer-malformed" and runtime.starts == []


# --- failure-atomic spawn -----------------------------------------------------------------------------------------------

def test_a_start_that_raises_cancels_the_unbound_ticket_and_reads_zero(tmp_path):
    ln, control, runtime = build(tmp_path, runtime=FakeRuntime(fail=OSError("no slot")))
    with pytest.raises(OSError):
        ln.spawn(launch())
    assert control.cancelled == ["t" * 43] and runtime.ended == []
    assert L.SessionIndex(tmp_path / "idx").get(KEY)["state"] == "undone"
    assert ln.usage(KEY) == zero()
    assert ln.stop(KEY) == StopResult(True, "no session was started")


def test_a_start_that_raises_after_binding_ends_the_bound_session(tmp_path):
    ctl_ = FakeControl(**{"RUN-TICKET-CANCEL": Refused("ticket-bound"),
                          "RUN-TICKET-STATUS": lambda f: {"ticket": "t", "state": "bound", "session": "sess-9"}})
    ln, control, runtime = build(tmp_path, control=ctl_, runtime=FakeRuntime(fail=OSError("half started")))
    with pytest.raises(OSError):
        ln.spawn(launch())
    assert runtime.ended == ["sess-9"]
    assert L.SessionIndex(tmp_path / "idx").get(KEY)["state"] == "undone"


def test_a_start_that_raises_and_cannot_be_shown_undone_reads_the_caps_not_zero(tmp_path):
    ctl_ = FakeControl(**{"RUN-TICKET-CANCEL": Refused("ticket-bound"),
                          "RUN-TICKET-STATUS": lambda f: {"ticket": "t", "state": "bound", "session": "sess-9"}})
    ln, _, runtime = build(tmp_path, control=ctl_, runtime=FakeRuntime(end=False, fail=OSError("half started")))
    with pytest.raises(OSError):
        ln.spawn(launch())
    assert L.SessionIndex(tmp_path / "idx").get(KEY)["state"] == "minted"
    u = ln.usage(KEY)
    assert (u.actions, u.tunnel_bytes, u.seconds) == (7, 1000, 30)    # read from the sources, never a quiet zero
    assert u != zero()


def test_an_unconfirmed_cancel_leaves_the_record_minted(tmp_path):
    ln, _, _ = build(tmp_path, control=FakeControl(**{"RUN-TICKET-CANCEL": Refused("control-down")}),
                     runtime=FakeRuntime(fail=OSError("x")))
    with pytest.raises(OSError):
        ln.spawn(launch())
    assert L.SessionIndex(tmp_path / "idx").get(KEY)["state"] == "minted"


def test_an_index_failure_after_the_start_ends_the_session_then_raises(tmp_path, monkeypatch):
    ln, control, runtime = build(tmp_path)
    real_put = ln.index.put

    def put(key, state, **kw):
        if state == "started":
            raise OSError("disk full")
        return real_put(key, state, **kw)

    monkeypatch.setattr(ln.index, "put", put)
    with pytest.raises(OSError, match="disk full"):
        ln.spawn(launch())
    assert runtime.ended == ["sess-1"] and runtime.children[0].aborted == 1
    assert real_put.__self__.get(KEY)["state"] == "undone"


def test_an_index_failure_with_an_unprovable_end_returns_a_held_child_not_a_raise(tmp_path, monkeypatch):
    rt = FakeRuntime(end=False)
    ln, control, _ = build(tmp_path, runtime=rt)
    real_put = ln.index.put
    monkeypatch.setattr(ln.index, "put", lambda k, s, **kw: (_ for _ in ()).throw(OSError("x")) if s == "started"
                        else real_put(k, s, **kw))
    child = ln.spawn(launch())
    assert isinstance(child, L.HeldChild)
    child.release()                                           # a held child runs nothing
    assert child.process.poll() is None
    assert child.process.wait() == ctl.GATE_UNPROVEN and not ctl.gate_proves_termination(child.process.wait())
    rt.end_ok = True                                          # END is retried from the run's own stop and abort
    assert child.abort() == ctl.GATE_REFUSED and ctl.gate_proves_termination(child.process.poll())


def test_a_second_spawn_under_one_key_starts_nothing_twice(tmp_path):
    ln, control, runtime = build(tmp_path)
    ln.spawn(launch())
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch())
    assert e.value.name == "session-key-already-used" and len(runtime.starts) == 1
    assert control.ops().count("RUN-TICKET-MINT") == 1


def test_an_unreadable_index_record_refuses_the_spawn(tmp_path):
    ln, control, runtime = build(tmp_path)
    (tmp_path / "idx").mkdir()
    (tmp_path / "idx" / f"{KEY}.json").write_text("{not json")
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch())
    assert e.value.name == "session-index-unreadable" and control.calls == []


# --- the child ---------------------------------------------------------------------------------------------------------

def test_a_spawn_answers_the_gated_child_unreleased_and_abort_ends_the_slot(tmp_path):
    ln, control, runtime = build(tmp_path)
    child = ln.spawn(launch())
    inner = runtime.children[0]
    assert child.process is inner.process and not child.released and not inner.released
    child.release()
    assert inner.released and child.released
    child.abort()
    assert inner.aborted == 1 and runtime.ended == ["sess-1"]


def test_an_end_that_raises_after_an_abort_does_not_hide_the_abort(tmp_path):
    ln, _, runtime = build(tmp_path, runtime=FakeRuntime(end=RuntimeError("boom")))
    child = ln.spawn(launch())
    assert child.abort() == 0


# --- usage is keyed by the RECORDED session key -------------------------------------------------------------------------

def test_usage_reads_the_figures_by_run_id_and_slot_session(tmp_path):
    src = FakeSources(calls=7, bytes_=1234, seconds=42)
    ln, _, _ = build(tmp_path, sources=src)
    ln.spawn(launch())
    u = ln.usage(KEY)
    assert (u.usd_micros, u.actions, u.tunnel_bytes, u.seconds, u.source) == (0, 7, 1234, 42, "session:passthrough")
    assert src.asked == [("calls", RUN), ("bytes", "sess-1"), ("seconds", "sess-1")]


def test_a_fresh_launcher_in_another_process_reads_the_same_figures_under_the_recorded_key(tmp_path):
    ln, _, _ = build(tmp_path, sources=FakeSources(calls=3, bytes_=9, seconds=1))
    ln.spawn(launch())
    other = L.RealLauncher(FakeControl(), FakeRuntime(), FakeSources(calls=5, bytes_=6, seconds=7),
                           L.SessionIndex(tmp_path / "idx"))
    u = other.usage(KEY)
    assert (u.actions, u.tunnel_bytes, u.seconds) == (5, 6, 7)       # from the other's sources, found by the same key
    assert other.stop(KEY) == StopResult(True, "S1 END proved the session's uid group gone")


def test_an_unknown_key_is_unreadable_never_zero(tmp_path):
    ln, _, _ = build(tmp_path)
    ln.spawn(launch())
    for key in ("s-ffffffffffffffff", "../x", "", "S-UP"):
        u = ln.usage(key)
        assert (u.actions, u.tunnel_bytes, u.seconds) == (None, None, None) and u != zero()


@pytest.mark.parametrize("bad", [None, -1, True, 1.5, "7", RuntimeError("down")])
def test_a_figure_the_source_cannot_read_is_none_per_field(tmp_path, bad):
    ln, _, _ = build(tmp_path, sources=FakeSources(calls=bad, bytes_=10, seconds=20))
    ln.spawn(launch())
    u = ln.usage(KEY)
    assert (u.actions, u.tunnel_bytes, u.seconds) == (None, 10, 20)


def test_a_pending_record_has_no_run_id_so_every_figure_is_unreadable(tmp_path):
    ln, _, _ = build(tmp_path)
    ln.index.put(KEY, "pending")
    u = ln.usage(KEY)
    assert (u.actions, u.tunnel_bytes, u.seconds) == (None, None, None)


def test_an_unreadable_index_is_unreadable_usage(tmp_path):
    ln, _, _ = build(tmp_path)
    (tmp_path / "idx").mkdir()
    (tmp_path / "idx" / f"{KEY}.json").write_text("[]")
    assert ln.usage(KEY).source == "session:index-unreadable"
    assert ln.stop(KEY).terminated is False


# --- stop by S1 END -----------------------------------------------------------------------------------------------------

def test_stop_is_terminated_only_on_a_proven_end(tmp_path):
    ln, _, runtime = build(tmp_path)
    ln.spawn(launch())
    assert ln.stop(KEY).terminated is True and runtime.ended == ["sess-1"]
    runtime.end_ok = False
    assert ln.stop(KEY).terminated is False
    runtime.end_ok = RuntimeError("x")
    assert ln.stop(KEY).terminated is False                    # never raises


def test_stop_of_an_unknown_key_observed_nothing(tmp_path):
    ln, _, runtime = build(tmp_path)
    assert ln.stop(KEY).terminated is False and runtime.ended == []


def test_stop_of_a_record_without_a_session_asks_control_who_is_bound(tmp_path):
    ctl_ = FakeControl(**{"RUN-TICKET-STATUS": lambda f: {"ticket": "t", "state": "bound", "session": "sess-4"}})
    ln, _, runtime = build(tmp_path, control=ctl_)
    ln.index.put(KEY, "minted", run_id=RUN)
    assert ln.stop(KEY).terminated is True and runtime.ended == ["sess-4"]


def test_the_ticket_is_never_written_to_the_index(tmp_path):
    ln, _, _ = build(tmp_path)
    ln.spawn(launch())
    text = (tmp_path / "idx" / f"{KEY}.json").read_text()
    assert "t" * 43 not in text and oct((tmp_path / "idx" / f"{KEY}.json").stat().st_mode & 0o777) == "0o600"
    assert not list((tmp_path / "idx").glob(".*.tmp"))


# --- through run_session (w3's MED 4 depends on the key being what the real usage reader is indexed by) -----------------

def test_a_subscription_run_through_run_session_settles_on_the_figures_read_by_the_recorded_key(rig, tmp_path):
    oid = _occurrence(rig, billing="subscription")
    ln, control, runtime = build(tmp_path, sources=FakeSources(calls=11, bytes_=2222, seconds=33))
    _, report = _run(rig, oid, ln, exposure=EXP)
    assert (report.state, report.code) == ("done", 0)
    (start,) = runtime.starts
    assert ln.index.get(start.session_key)["state"] == "started"
    assert _reservation(rig, oid)[:5] == ("settled", "actual", 11, 2222, 33)


def test_a_rerun_after_a_crash_reads_usage_under_the_key_the_first_attempt_recorded(rig, tmp_path):
    oid = _occurrence(rig, billing="subscription")
    ln, control, runtime = build(tmp_path, sources=FakeSources(calls=4, bytes_=40, seconds=5))
    _run(rig, oid, ln, exposure=EXP)
    # the same occurrence again, with a launcher that is a fresh object over the same index: nothing starts twice
    ln2 = L.RealLauncher(FakeControl(), FakeRuntime(), FakeSources(calls=4, bytes_=40, seconds=5), ln.index)
    _run(rig, oid, ln2, exposure=EXP)
    assert len(runtime.starts) == 1 and ln2.runtime.starts == []


def test_an_api_key_run_through_run_session_fails_with_the_named_reason_and_settles_at_zero(rig, tmp_path):
    oid = _occurrence(rig)                                      # billing api_key
    ln, control, runtime = build(tmp_path, auth="brokered")
    sup, report = _run(rig, oid, ln)
    assert report.state == "failed" and rig.row(oid)["reason"] == "session_spawn_failed"
    assert control.calls == [] and runtime.starts == []
    res = _reservation(rig, oid)                                # an api_key run's dollars are the last column
    assert res[:2] == ("settled", "actual") and res[5] == 0


def test_a_start_failure_through_run_session_frees_the_slot_and_settles_at_zero(rig, tmp_path):
    oid = _occurrence(rig, billing="subscription")
    ln, control, runtime = build(tmp_path, runtime=FakeRuntime(fail=OSError("no slot")))
    _, report = _run(rig, oid, ln, exposure=EXP)
    assert report.state == "failed" and rig.row(oid)["reason"] == "session_spawn_failed"
    assert control.cancelled == ["t" * 43]
    assert _reservation(rig, oid)[:5] == ("settled", "actual", 0, 0, 0)
    assert rig.taken(oid) == ([], [("settled",)])


def test_the_default_launcher_is_still_unwired():
    assert S.launch_refusal(ROLE, S.Reserved("api_key", None, 0, 3600, S._hash_or_none(ROLE)), S.UnwiredLauncher(),
                            None, usd_micros=0) == "launcher-not-wired"

