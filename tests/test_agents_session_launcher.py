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
    assert not hasattr(L, "BROKERED_ROUTE_BUILT")               # no flag turns the API-key branch on
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
    runtime = FakeRuntime()
    ln = L.RealLauncher(to, runtime, FakeSources(), L.SessionIndex(tmp_path / "idx"))
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
    # bind the ticket the launcher handed the slot (what ATTACH of the ticket-bound lease journals): once it is
    # bound, a replay is attempt-closed and STATUS names the session but no run id, so the index is its only record
    (start,) = runtime.starts
    real.tickets.bind(start.ticket, "s-00000000000000aa")
    status = to.call("RUN-TICKET-STATUS", attempt_id=KEY)
    assert status["state"] == T.BOUND and status["session"] == "s-00000000000000aa" and "run_id" not in status
    with pytest.raises(Refused) as e:
        to.call("RUN-TICKET-MINT", attempt_id=KEY, job_id="job-1", run_index=0,
                contract_digest=line["facts"]["contract_digest"], kind="executor", egress_routes=[],
                reserved_microdollars=0, auth="passthrough", vendor="claude", model_requested="sonnet",
                max_calls=20, max_tunnel_bytes=5_000_000, deadline_s=3600)
    assert e.value.name == "attempt-closed"


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
    assert L.SessionIndex(tmp_path / "idx").get(KEY)["state"] == "ended"      # r3-2: a bound session may have spent


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
    assert real_put.__self__.get(KEY)["state"] == "ended"                     # r3-2: with its session, the sources


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
    assert child.process._p is inner.process and not child.released and not inner.released
    child.release()
    assert inner.released and child.released
    child.abort()
    assert inner.aborted == 1 and runtime.ended == ["sess-1"]


def test_an_end_that_raises_after_an_abort_does_not_hide_the_abort(tmp_path):
    ln, _, runtime = build(tmp_path, runtime=FakeRuntime(end=RuntimeError("boom")))
    child = ln.spawn(launch())
    assert child.abort() == ctl.GATE_UNPROVEN                   # no raise, and an unproven END proves nothing
    assert runtime.children[0].aborted == 1


# --- usage is keyed by the RECORDED session key -------------------------------------------------------------------------

def test_usage_reads_the_figures_by_run_id_and_slot_session(tmp_path):
    src = FakeSources(calls=7, bytes_=1234, seconds=42)
    ln, _, _ = build(tmp_path, sources=src)
    ended(ln.spawn(launch()))
    u = ln.usage(KEY)
    assert (u.usd_micros, u.actions, u.tunnel_bytes, u.seconds, u.source) == (0, 7, 1234, 42, "session:passthrough")
    assert src.asked == [("calls", RUN), ("bytes", "sess-1"), ("seconds", "sess-1")]


def test_a_fresh_launcher_in_another_process_reads_the_same_figures_under_the_recorded_key(tmp_path):
    ln, _, _ = build(tmp_path, sources=FakeSources(calls=3, bytes_=9, seconds=1))
    ln.spawn(launch())
    other = L.RealLauncher(FakeControl(), FakeRuntime(), FakeSources(calls=5, bytes_=6, seconds=7),
                           L.SessionIndex(tmp_path / "idx"))
    assert other.usage(KEY).actions is None                           # live: END not yet proven
    assert other.stop(KEY) == StopResult(True, "S1 END proved the session's uid group gone")
    u = other.usage(KEY)
    assert (u.actions, u.tunnel_bytes, u.seconds) == (5, 6, 7)       # from the other's sources, found by the same key


def test_an_unknown_key_is_unreadable_never_zero(tmp_path):
    ln, _, _ = build(tmp_path)
    ln.spawn(launch())
    for key in ("s-ffffffffffffffff", "../x", "", "S-UP"):
        u = ln.usage(key)
        assert (u.actions, u.tunnel_bytes, u.seconds) == (None, None, None) and u != zero()


@pytest.mark.parametrize("bad", [None, -1, True, 1.5, "7", RuntimeError("down")])
def test_a_figure_the_source_cannot_read_is_none_per_field(tmp_path, bad):
    ln, _, _ = build(tmp_path, sources=FakeSources(calls=bad, bytes_=10, seconds=20))
    ended(ln.spawn(launch()))
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
    ln, _, runtime = build(tmp_path, runtime=FakeRuntime(end=False))
    ln.spawn(launch())
    assert ln.stop(KEY).terminated is False and runtime.ended == ["sess-1"]
    runtime.end_ok = RuntimeError("x")
    assert ln.stop(KEY).terminated is False                    # never raises
    assert ln.index.get(KEY)["state"] == "started"
    runtime.end_ok = True
    assert ln.stop(KEY).terminated is True and ln.index.get(KEY)["state"] == "ended"
    assert ln.stop(KEY).terminated is True and len(runtime.ended) == 3      # answered from the record


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
    assert ln.index.get(start.session_key)["state"] == "ended"           # the normal exit ran S1 END
    assert runtime.ended == ["sess-1"]
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



# --- p71s4 fix round 1 (FINDINGS_p71s4_r1_L1.md, FINDINGS_p71s4_r1_L2.md) ----------------------------------------------

BOUND9 = {"RUN-TICKET-CANCEL": Refused("ticket-bound"),
          "RUN-TICKET-STATUS": lambda f: {"ticket": "t", "state": "bound", "session": "sess-9"}}


def ended(child):
    """The run's own wait: the gate exits and S1 END runs behind it."""
    return child.process.wait()


# L1-1: a start that raises with a bound session whose END is unproven returns a held child (the CONTRACT), never raises

def test_l1_1_a_bound_session_whose_end_is_unproven_is_held_not_raised(tmp_path):
    ln, _, runtime = build(tmp_path, control=FakeControl(**BOUND9), runtime=FakeRuntime(end=False, fail=OSError("x")))
    child = ln.spawn(launch())                                   # no raise: run_session would free the slot on one
    assert isinstance(child, L.HeldChild) and child.session == "sess-9"
    assert child.process.wait() == ctl.GATE_UNPROVEN and not ctl.gate_proves_termination(child.process.poll())
    rec = ln.index.get(KEY)
    assert rec["state"] == "started" and rec["session"] == "sess-9" and rec["run_id"] == RUN
    u = ln.usage(KEY)
    assert (u.actions, u.tunnel_bytes, u.seconds) == (None, None, None) and u != zero()
    assert ln.stop(KEY).terminated is False and runtime.ended[-1] == "sess-9"
    runtime.end_ok = True
    assert child.abort() == ctl.GATE_REFUSED and ln.index.get(KEY)["state"] == "ended"


@pytest.mark.parametrize("cancel", [Refused("control-down"), Refused("attempt-closed"), OSError("wire")])
def test_l1_1_any_failed_cancel_consults_status_and_holds_a_live_session(tmp_path, cancel):
    ctl_ = FakeControl(**{"RUN-TICKET-CANCEL": cancel, "RUN-TICKET-STATUS": BOUND9["RUN-TICKET-STATUS"]})
    ln, control, runtime = build(tmp_path, control=ctl_, runtime=FakeRuntime(end=False, fail=OSError("x")))
    assert isinstance(ln.spawn(launch()), L.HeldChild)
    assert "RUN-TICKET-STATUS" in control.ops() and runtime.ended == ["sess-9"]


def test_l1_1_a_failed_cancel_with_a_proven_end_of_the_bound_session_raises_and_reads_the_sources(tmp_path):
    ctl_ = FakeControl(**{"RUN-TICKET-CANCEL": Refused("control-down"),
                          "RUN-TICKET-STATUS": BOUND9["RUN-TICKET-STATUS"]})
    ln, _, runtime = build(tmp_path, control=ctl_, runtime=FakeRuntime(fail=OSError("x")))
    with pytest.raises(OSError):
        ln.spawn(launch())
    assert runtime.ended == ["sess-9"] and ln.index.get(KEY)["state"] == "ended"      # r3-2: never a quiet zero
    assert (ln.usage(KEY).actions, ln.usage(KEY).tunnel_bytes) == (7, 1000)


def test_l1_1_no_session_known_raises_honestly_and_the_record_stays_minted(tmp_path):
    ln, _, runtime = build(tmp_path, control=FakeControl(**{"RUN-TICKET-CANCEL": Refused("control-down"),
                                                           "RUN-TICKET-STATUS": Refused("control-down")}),
                           runtime=FakeRuntime(fail=OSError("x")))
    with pytest.raises(OSError):
        ln.spawn(launch())
    assert ln.index.get(KEY)["state"] == "minted" and runtime.ended == []
    u = ln.usage(KEY)
    assert (u.actions, u.tunnel_bytes, u.seconds) == (None, None, None)


def test_l1_1_through_run_session_a_held_session_keeps_the_row_launched(rig, tmp_path):
    oid = _occurrence(rig, billing="subscription")
    ln, _, _ = build(tmp_path, control=FakeControl(**BOUND9), runtime=FakeRuntime(end=False, fail=OSError("x")))
    _, report = _run(rig, oid, ln, exposure=EXP)
    assert report.state == "launched" and rig.row(oid)["state"] == "launched"


# L1-2: a refusal tombstone never overwrites a live record (the probe, as a test)

@pytest.mark.parametrize("kw", [{"harness": "codex"}, {"occurrence": "has space"}, {"auth": "brokered"}])
def test_l1_2_a_refused_second_spawn_of_a_started_key_leaves_its_record(tmp_path, kw):
    ln, _, runtime = build(tmp_path)
    child = ln.spawn(launch())
    with pytest.raises(L.LauncherRefused):
        ln.spawn(launch(**kw))
    assert ln.index.get(KEY)["state"] == "started"
    assert ln.usage(KEY) != zero()
    assert ln.stop(KEY) == StopResult(True, "S1 END proved the session's uid group gone") and runtime.ended == ["sess-1"]
    assert child is not None


def test_l1_2_a_refused_mint_after_a_lost_race_does_not_overwrite_the_winner(tmp_path):
    ln, _, _ = build(tmp_path)
    ln.spawn(launch())
    other, _, _ = build(tmp_path, control=FakeControl(**{"RUN-TICKET-MINT": Refused("no-free-slot:egress")}))
    with pytest.raises(L.LauncherRefused) as e:
        other.spawn(launch())
    assert e.value.name == "session-key-already-used" and ln.index.get(KEY)["state"] == "started"


# L1-3: the pending write is the existence check (create-exclusive): a concurrent loser is refused before the mint

def test_l1_3_a_concurrent_loser_is_refused_before_it_mints(tmp_path, monkeypatch):
    ln, _, runtime = build(tmp_path)
    ln.spawn(launch())
    loser, lctl, lrt = build(tmp_path)
    monkeypatch.setattr(loser.index, "get", lambda key: None)    # the window: its look found nothing
    with pytest.raises(L.LauncherRefused) as e:
        loser.spawn(launch())
    assert e.value.name == "session-key-already-used"
    assert lctl.calls == [] and lrt.starts == [] and lrt.ended == []
    assert L.SessionIndex(tmp_path / "idx").get(KEY)["state"] == "started"


def test_l1_3_create_is_exclusive_and_leaves_no_temp(tmp_path):
    idx = L.SessionIndex(tmp_path / "idx")
    assert idx.create(KEY, "pending", n=1) is True
    assert idx.create(KEY, "refused", n=2) is False
    assert idx.get(KEY)["state"] == "pending" and idx.get(KEY)["n"] == 1
    assert not list((tmp_path / "idx").glob(".*"))


# L1-4: there is no flag that turns the API-key branch on; a brokered launcher never mints and never says passthrough

def test_l1_4_no_flag_can_make_a_brokered_launcher_mint(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "BROKERED_ROUTE_BUILT", True, raising=False)
    ln, control, runtime = build(tmp_path, auth="brokered")
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch(auth="brokered"))
    assert e.value.name == "brokered-route-not-built" and control.calls == [] and runtime.starts == []


def test_l1_4_a_brokered_launchers_usage_never_claims_passthrough(tmp_path):
    ln, _, _ = build(tmp_path, auth="brokered")
    ln.index.put(KEY, "ended", run_id=RUN, session="sess-1")
    u = ln.usage(KEY)
    assert u.source != "session:passthrough" and (u.usd_micros, u.actions, u.tunnel_bytes, u.seconds) == (None,) * 4


def test_l1_4_a_launcher_of_any_other_auth_is_refused_and_the_facts_carry_the_launchers_auth(tmp_path, monkeypatch):
    ln, control, _ = build(tmp_path)
    ln.auth = "none"                                             # outside T.AUTHS: only reachable by assignment
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch(auth="none"))
    assert e.value.name == "auth-not-launchable" and control.calls == []
    ln2, control2, _ = build(tmp_path / "b")
    sent = ln2._facts(launch())
    assert sent["auth"] == "passthrough"
    ln2.auth = "brokered"                                        # _facts reads self.auth, never a literal
    with pytest.raises(L.LauncherRefused):
        ln2._facts(launch(auth="brokered"))


# L1-5: a malformed mint answer is settled through STATUS by attempt id; a raw error is a named refusal

@pytest.mark.parametrize("ans", [None, [], "x", {}, {"ticket": "t", "run_id": "r-short"}])
def test_l1_5_a_malformed_answer_cancels_the_unbound_ticket_by_attempt(tmp_path, ans):
    ctl_ = FakeControl(**{"RUN-TICKET-MINT": lambda f: ans,
                          "RUN-TICKET-STATUS": lambda f: {"ticket": "tk-1", "state": "unbound"}})
    ln, control, runtime = build(tmp_path, control=ctl_)
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch())
    assert e.value.name == "mint-answer-malformed" and runtime.starts == []
    assert control.cancelled == ["tk-1"] and ln.index.get(KEY)["state"] == "undone" and ln.usage(KEY) == zero()
    assert ("RUN-TICKET-STATUS", {"attempt_id": KEY}) in control.calls


def test_l1_5_a_malformed_answer_for_an_attempt_never_minted_reads_zero(tmp_path):
    ln, control, _ = build(tmp_path, control=FakeControl(**{"RUN-TICKET-MINT": lambda f: {}}))   # STATUS: attempt-unknown
    with pytest.raises(L.LauncherRefused):
        ln.spawn(launch())
    assert control.cancelled == [] and ln.index.get(KEY)["state"] == "refused"


@pytest.mark.parametrize("status", [Refused("control-down"), lambda f: {"ticket": "tk", "state": "bound", "session": "s"}])
def test_l1_5_a_malformed_answer_that_cannot_be_settled_stays_pending_and_says_so(tmp_path, status):
    ln, control, _ = build(tmp_path, control=FakeControl(**{"RUN-TICKET-MINT": lambda f: {},
                                                           "RUN-TICKET-STATUS": status}))
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch())
    assert ln.index.get(KEY)["state"] == "pending" and "left pending" in e.value.detail
    assert control.cancelled == []


def test_l1_5_a_raw_error_from_the_mint_is_a_named_refusal_and_reads_the_caps(tmp_path):
    ln, _, runtime = build(tmp_path, control=FakeControl(**{"RUN-TICKET-MINT": OSError("broken pipe")}))
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch())
    assert e.value.name == "mint-failed" and e.value.reason_class == "transient" and runtime.starts == []
    assert ln.index.get(KEY)["state"] == "pending"


# L2-F1: a NORMAL exit runs S1 END; a clean exit is never `done` without END's proof

def test_f1_a_clean_exit_with_a_proven_end_answers_its_code_and_records_ended(tmp_path):
    ln, _, runtime = build(tmp_path)
    child = ln.spawn(launch())
    assert ended(child) == 0 and runtime.ended == ["sess-1"] and ctl.gate_proves_termination(child.process.poll())
    assert ln.index.get(KEY)["state"] == "ended"


@pytest.mark.parametrize("end", [False, RuntimeError("teardown refused")])
def test_f1_a_clean_exit_with_an_unproven_end_is_gate_unproven(tmp_path, end):
    ln, _, runtime = build(tmp_path, runtime=FakeRuntime(end=end))
    child = ln.spawn(launch())
    assert ended(child) == ctl.GATE_UNPROVEN and child.process.poll() == ctl.GATE_UNPROVEN
    assert child.process.returncode == ctl.GATE_UNPROVEN
    assert ln.index.get(KEY)["state"] == "started" and runtime.ended == ["sess-1"]   # r3-5: that poll made none
    runtime.end_ok = True                                        # one more attempt, from the next wait
    assert ended(child) == 0 and ln.index.get(KEY)["state"] == "ended"


def test_f1_through_run_session_an_unproven_end_keeps_the_row_launched(rig, tmp_path):
    oid = _occurrence(rig, billing="subscription")
    ln, _, _ = build(tmp_path, runtime=FakeRuntime(end=False))
    _, report = _run(rig, oid, ln, exposure=EXP)
    assert report.state == "launched" and rig.row(oid)["state"] == "launched"


# L2-F8: usage answers None until END proved the group gone

def test_f8_usage_of_a_live_session_is_unreadable_until_end_is_proven(tmp_path):
    src = FakeSources(calls=7, bytes_=1234, seconds=42)
    ln, _, _ = build(tmp_path, sources=src)
    child = ln.spawn(launch())
    u = ln.usage(KEY)
    assert (u.actions, u.tunnel_bytes, u.seconds) == (None, None, None) and src.asked == []
    ended(child)
    assert (ln.usage(KEY).actions, ln.usage(KEY).tunnel_bytes) == (7, 1234)


# L2-F9: the teardown mapping, and a second END after an abort

@pytest.mark.parametrize("answer,proves", [
    ({"state": "RETAINED"}, True), ({"state": "RETAINED", "released": False}, True),
    ({"state": "HELD"}, False), ({}, False), (None, False), ({"state": "SCRUBBED"}, False), ("RETAINED", False),
    ({"state": "RETAINED", "released": "no"}, False)])
def test_f9_the_teardown_answer_mapping(answer, proves):
    assert L.teardown_proves(answer) is proves


def test_f9_the_mapping_reads_the_answers_the_helpers_teardown_returns():
    import inspect
    from levain.slot import start as SS
    src = inspect.getsource(SS.teardown)
    assert 'return {"state": out}' in src and 'return {"state": R.RETAINED, "released": False}' in src
    assert 'return {"state": R.RETAINED}' in src


def test_f9_a_stop_after_a_proven_abort_answers_from_the_record_not_a_second_end(tmp_path):
    ln, _, runtime = build(tmp_path)
    child = ln.spawn(launch())
    child.abort()
    assert runtime.ended == ["sess-1"] and ln.index.get(KEY)["state"] == "ended"
    runtime.end_ok = False                                       # a second teardown after SCRUB reads no-session
    assert ln.stop(KEY).terminated is True and runtime.ended == ["sess-1"]


def test_f9_an_abort_whose_end_is_unproven_answers_gate_unproven(tmp_path):
    ln, _, _ = build(tmp_path, runtime=FakeRuntime(end=False))
    assert ln.spawn(launch()).abort() == ctl.GATE_UNPROVEN


# L2-F10: the model is held to render's grammar before the mint

@pytest.mark.parametrize("model", ["claude@sonnet", "a b", "", "x" * 129])
def test_f10_a_model_render_would_refuse_is_refused_before_the_mint(tmp_path, model):
    ln, control, _ = build(tmp_path)
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(S.SessionLaunch("occ-1", KEY, "claude", model, "the brief", "passthrough", EXP))
    assert e.value.name == "model-outside-render-grammar" and control.calls == []


# L2-F11: a mint refusal carries the server's name and its class

@pytest.mark.parametrize("name,cls", [("no-free-slot:egress", "transient"), ("locked", "transient"),
                                      ("internal-error", "transient"), ("journal-unavailable", "transient"),
                                      ("control-down", "transient"), ("ceiling-lacks-subscription", "standing"),
                                      ("config-invalid:base", "standing"), ("ticket-field:vendor", "standing")])
def test_f11_a_mint_refusal_names_the_server_reason_and_its_class(tmp_path, name, cls):
    ln, _, _ = build(tmp_path, control=FakeControl(**{"RUN-TICKET-MINT": Refused(name, "RUN-TICKET-MINT")}))
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch())
    assert e.value.name == f"mint-refused:{name}" and e.value.reason_class == cls
    if name != "control-down":
        rec = ln.index.get(KEY)
        assert rec["reason"] == f"mint:{name}" and rec["reason_class"] == cls


@pytest.mark.parametrize("state", ["cancelled", "expired"])
def test_l1_1_a_ticket_closed_before_any_binding_is_undone_and_reads_zero(tmp_path, state):
    ctl_ = FakeControl(**{"RUN-TICKET-CANCEL": Refused("attempt-closed", state),
                          "RUN-TICKET-STATUS": lambda f: {"ticket": "t", "state": state}})
    ln, _, runtime = build(tmp_path, control=ctl_, runtime=FakeRuntime(fail=OSError("x")))
    with pytest.raises(OSError):
        ln.spawn(launch())
    assert runtime.ended == [] and ln.index.get(KEY)["state"] == "undone" and ln.usage(KEY) == zero()


def test_l1_1_an_unbound_ticket_whose_cancel_failed_stays_minted(tmp_path):
    ctl_ = FakeControl(**{"RUN-TICKET-CANCEL": Refused("control-down"),
                          "RUN-TICKET-STATUS": lambda f: {"ticket": "t", "state": "unbound"}})
    ln, _, _ = build(tmp_path, control=ctl_, runtime=FakeRuntime(fail=OSError("x")))
    with pytest.raises(OSError):
        ln.spawn(launch())
    assert ln.index.get(KEY)["state"] == "minted" and ln.usage(KEY).actions is None


# --- p71s4 fix round 2 (FINDINGS_p71s4_r3.md) ---------------------------------------------------------------------------

def caps(u):
    return (u.actions, u.tunnel_bytes, u.seconds) == (None, None, None)


# r3-1 HIGH: an END answer is proof only when it is exactly True. The helper's raw teardown answer is a dict, and a dict
# is truthy: a runtime adapter that forwards it must not make an unproven END read as proven, at any of the sites.

NOT_TRUE = [{"state": "HELD"}, {"state": "RETAINED"}, {"state": "RETAINED", "released": False}, {"ok": True},
            1, "yes", None, object()]


@pytest.mark.parametrize("answer", NOT_TRUE)
def test_r3_1_a_normal_exit_whose_end_answers_anything_but_true_is_unproven(tmp_path, answer):
    ln, _, runtime = build(tmp_path, runtime=FakeRuntime(end=answer))
    child = ln.spawn(launch())
    assert ended(child) == ctl.GATE_UNPROVEN and child.process.returncode == ctl.GATE_UNPROVEN
    assert child.abort() == ctl.GATE_UNPROVEN
    assert ln.index.get(KEY)["state"] == "started" and caps(ln.usage(KEY))


@pytest.mark.parametrize("answer", NOT_TRUE)
def test_r3_1_stop_whose_end_answers_anything_but_true_is_not_terminated(tmp_path, answer):
    ln, _, runtime = build(tmp_path, runtime=FakeRuntime(end=answer))
    ln.spawn(launch())
    assert ln.stop(KEY).terminated is False and ln.index.get(KEY)["state"] == "started"


@pytest.mark.parametrize("answer", NOT_TRUE)
def test_r3_1_a_failed_start_with_a_bound_session_and_a_non_true_end_is_held(tmp_path, answer):
    ln, _, runtime = build(tmp_path, control=FakeControl(**BOUND9), runtime=FakeRuntime(end=answer, fail=OSError("x")))
    child = ln.spawn(launch())
    assert isinstance(child, L.HeldChild) and child.abort() == ctl.GATE_UNPROVEN
    assert child.process.wait() == ctl.GATE_UNPROVEN and ln.index.get(KEY)["state"] == "started"


@pytest.mark.parametrize("answer", NOT_TRUE)
def test_r3_1_an_index_failure_with_a_non_true_end_holds_the_session(tmp_path, monkeypatch, answer):
    ln, _, _ = build(tmp_path, runtime=FakeRuntime(end=answer))
    real_put = ln.index.put
    monkeypatch.setattr(ln.index, "put", lambda k, s, **kw: (_ for _ in ()).throw(OSError("x")) if s == "started"
                        else real_put(k, s, **kw))
    child = ln.spawn(launch())
    assert isinstance(child, L.HeldChild) and child.abort() == ctl.GATE_UNPROVEN


def test_r3_1_every_end_call_in_the_module_goes_through_the_one_reader():
    import pathlib
    text = pathlib.Path(L.__file__).read_text()
    assert text.count("runtime.end(") == 1 and "bool(self._runtime.end" not in text and "bool(self.runtime.end" not in text
    assert "answer is True" in text


class TeardownRuntime(FakeRuntime):
    """A runtime adapter built the way the production one must be: its ``end`` is :func:`end_by_teardown` over the
    helper's ``teardown`` call, here a fake that answers the helper's shapes."""

    def __init__(self, answer, **kw):
        super().__init__(**kw)
        self.answer = answer

    def _teardown(self, session):
        self.ended.append(session)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def end(self, session):
        return L.end_by_teardown(self._teardown, session)


@pytest.mark.parametrize("answer,proves", [
    ({"state": "RETAINED"}, True), ({"state": "RETAINED", "released": False}, True),
    ({"ok": True, "state": "RETAINED"}, True),
    ({"state": "HELD"}, False), ({"state": "RETAINED", "released": "no"}, False), ({"state": "SCRUBBED"}, False),
    ({"ok": False, "state": "RETAINED"}, False), ({"ok": 1, "state": "RETAINED"}, False), ({}, False), (None, False),
    ("RETAINED", False), (True, False), (Refused("no-session", "sess-1"), False), (OSError("socket"), False)])
def test_r3_1_through_the_adapter_only_a_retained_teardown_proves_the_end(tmp_path, answer, proves):
    ln, _, runtime = build(tmp_path, runtime=TeardownRuntime(answer))
    child = ln.spawn(launch())
    assert ended(child) == (0 if proves else ctl.GATE_UNPROVEN) and runtime.ended == ["sess-1"]
    assert ln.index.get(KEY)["state"] == ("ended" if proves else "started")
    assert L.end_by_teardown(runtime._teardown, "sess-1") is proves


# r3-2 MED: a failed start whose BOUND session END proved gone may still have spent: it records `ended` (the sources),
# never `undone` (zero). `undone` is kept for a ticket that never bound.

def test_r3_2_a_failed_start_with_a_proven_end_of_a_bound_session_reads_the_sources(tmp_path):
    src = FakeSources(calls=3, bytes_=300, seconds=9)
    ctl_ = FakeControl(**{"RUN-TICKET-CANCEL": Refused("ticket-bound"), "RUN-TICKET-STATUS": BOUND9["RUN-TICKET-STATUS"]})
    ln, _, runtime = build(tmp_path, control=ctl_, runtime=FakeRuntime(fail=OSError("x")), sources=src)
    with pytest.raises(OSError):
        ln.spawn(launch())
    rec = ln.index.get(KEY)
    assert rec["state"] == "ended" and rec["run_id"] == RUN and rec["session"] == "sess-9"
    u = ln.usage(KEY)
    assert (u.actions, u.tunnel_bytes, u.seconds) == (3, 300, 9) and u != zero()
    assert ("calls", RUN) in src.asked and ("bytes", "sess-9") in src.asked
    assert ln.stop(KEY).terminated is True and runtime.ended == ["sess-9"]     # answered from the record


def test_r3_2_through_run_session_a_bound_failed_start_settles_on_the_sources_not_zero(rig, tmp_path):
    oid = _occurrence(rig, billing="subscription")
    ctl_ = FakeControl(**{"RUN-TICKET-CANCEL": Refused("ticket-bound"), "RUN-TICKET-STATUS": BOUND9["RUN-TICKET-STATUS"]})
    ln, _, _ = build(tmp_path, control=ctl_, runtime=FakeRuntime(fail=OSError("x")),
                     sources=FakeSources(calls=5, bytes_=500, seconds=50))
    _, report = _run(rig, oid, ln, exposure=EXP)
    assert report.state == "failed" and rig.row(oid)["reason"] == "session_spawn_failed"
    assert _reservation(rig, oid)[:5] == ("settled", "actual", 5, 500, 50)


def test_r3_2_an_index_failure_after_a_start_with_a_proven_end_records_ended_with_its_session(tmp_path, monkeypatch):
    ln, _, runtime = build(tmp_path, sources=FakeSources(calls=2, bytes_=20, seconds=4))
    real_put = ln.index.put
    monkeypatch.setattr(ln.index, "put", lambda k, s, **kw: (_ for _ in ()).throw(OSError("full")) if s == "started"
                        else real_put(k, s, **kw))
    with pytest.raises(OSError):
        ln.spawn(launch())
    rec = ln.index.get(KEY)
    assert rec["state"] == "ended" and rec["session"] == "sess-1" and rec["run_id"] == RUN
    assert (ln.usage(KEY).actions, ln.usage(KEY).tunnel_bytes) == (2, 20)


# r3-3 MED: an index record is written whole or not at all; a record that does not parse reads unreadable

def test_r3_3_a_short_os_write_still_writes_the_whole_record(tmp_path, monkeypatch):
    import os
    idx = L.SessionIndex(tmp_path / "idx")
    real = os.write
    with monkeypatch.context() as m:
        m.setattr(os, "write", lambda fd, data: real(fd, bytes(data[:7])) if fd > 2 else real(fd, data))
        idx.put(KEY, "started", run_id=RUN, session="sess-1", pad="x" * 500)
        assert idx.create("s-other", "pending", pad="y" * 500) is True
    assert idx.get(KEY)["pad"] == "x" * 500 and idx.get("s-other")["pad"] == "y" * 500
    assert not list((tmp_path / "idx").glob(".*.tmp"))


@pytest.mark.parametrize("fail_on", ["write", "fsync"])
def test_r3_3_a_failed_write_leaves_no_temp_and_the_old_record(tmp_path, monkeypatch, fail_on):
    import errno
    import os
    idx = L.SessionIndex(tmp_path / "idx")
    idx.put(KEY, "started", run_id=RUN, session="sess-1")
    real = getattr(os, fail_on)

    def boom(fd, *a):
        if fd > 2:
            raise OSError(errno.ENOSPC, "no space")
        return real(fd, *a)

    with monkeypatch.context() as m:
        m.setattr(os, fail_on, boom)
        with pytest.raises(OSError):
            idx.put(KEY, "ended", run_id=RUN, session="sess-1")
        with pytest.raises(OSError):
            idx.create("s-other", "pending")
    assert idx.get(KEY)["state"] == "started" and idx.get("s-other") is None
    assert not list((tmp_path / "idx").glob(".*.tmp"))


def test_r3_3_a_truncated_record_reads_unreadable_never_absent(tmp_path):
    ln, control, runtime = build(tmp_path)
    ln.spawn(launch())
    path = tmp_path / "idx" / f"{KEY}.json"
    path.write_bytes(path.read_bytes()[:20])
    assert caps(ln.usage(KEY)) and ln.usage(KEY).source == "session:index-unreadable"
    assert ln.stop(KEY).terminated is False and runtime.ended == []
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch())
    assert e.value.name == "session-index-unreadable"


# r3-4 LOW-MED: a transient mint refusal does not burn the key; a standing one does

def test_r3_4_a_transient_mint_refusal_leaves_the_key_retryable(tmp_path):
    answers = [Refused("locked", "RUN-TICKET-MINT")]

    def mint(f):
        if answers:
            raise answers.pop()
        return {"ticket": "t" * 43, "run_id": RUN}

    ln, control, runtime = build(tmp_path, control=FakeControl(**{"RUN-TICKET-MINT": mint}))
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch())
    assert e.value.reason_class == "transient" and ln.usage(KEY) == zero()
    ln.spawn(launch())                                           # the same key again: minted and started
    assert ln.index.get(KEY)["state"] == "started" and len(runtime.starts) == 1
    with pytest.raises(L.LauncherRefused) as e2:
        ln.spawn(launch())                                       # a started key is still held
    assert e2.value.name == "session-key-already-used"


def test_r3_4_a_refused_mint_journals_nothing_so_the_retaken_key_mints_through_the_real_control(st, tmp_path,
                                                                                               monkeypatch):
    """The retake rests on CONTROL journaling nothing for a refused mint: shown against the real handler. The refusal
    used is one this test can lift (no /2 ceiling, then one), classed transient here for the test only."""
    from tests.test_slot_p7a1_passthrough_ticket import write_ceiling2
    from tests.test_tickets import Clock, Rig

    real = Rig(st, Clock())

    class ToControl:
        def call(self, op, **fields):
            ans = real.ask(op, **fields)
            if ans.get("ok") is not True:
                raise Refused(ans["refused"], op)
            return {k: v for k, v in ans.items() if k not in ("id", "ok")}

    monkeypatch.setattr(L, "_TRANSIENT", L._TRANSIENT | {"ceiling-lacks-subscription"})
    ln = L.RealLauncher(ToControl(), FakeRuntime(), FakeSources(), L.SessionIndex(tmp_path / "idx"))
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch(occurrence="job-1"))
    assert e.value.name == "mint-refused:ceiling-lacks-subscription" and real.lines() == []
    write_ceiling2(st.L)
    ln.spawn(launch(occurrence="job-1"))
    (line,) = real.lines()
    assert line["attempt_id"] == KEY and ln.index.get(KEY)["state"] == "started"


def test_r3_4_a_standing_mint_refusal_still_holds_the_key(tmp_path):
    ln, _, runtime = build(tmp_path, control=FakeControl(**{"RUN-TICKET-MINT": Refused("ceiling-lacks-subscription")}))
    with pytest.raises(L.LauncherRefused):
        ln.spawn(launch())
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch())
    assert e.value.name == "session-key-already-used" and runtime.starts == []


def test_r3_4_a_retake_is_exclusive(tmp_path):
    idx = L.SessionIndex(tmp_path / "idx")
    idx.create(KEY, "refused", reason="mint:locked", reason_class="transient")
    retake = L.RealLauncher._retakable
    assert idx.claim(KEY, "pending", retake, n=1) is True
    assert idx.claim(KEY, "pending", retake, n=2) is False         # the record is now pending: not retakable
    assert idx.get(KEY)["n"] == 1
    assert not list((tmp_path / "idx").glob(".*.tmp"))


# r3-5 LOW-MED: poll does not drive END at the caller's poll rate

def test_r3_5_poll_makes_at_most_one_end_attempt_per_interval(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(L, "_monotonic", lambda: now[0])
    ln, _, runtime = build(tmp_path, runtime=FakeRuntime(end=False))
    child = ln.spawn(launch())
    child.process._p.wait()                                      # the gate exits; nothing has polled yet
    for _ in range(5):
        assert child.process.poll() == ctl.GATE_UNPROVEN
    assert runtime.ended == ["sess-1"]
    now[0] += L._POLL_END_INTERVAL_S
    runtime.end_ok = True
    assert child.process.poll() == 0 and runtime.ended == ["sess-1", "sess-1"]
    assert ln.index.get(KEY)["state"] == "ended"


# r3-6 LOW: an `ended` write that failed is retried by the next wait / poll / abort

def test_r3_6_a_failed_ended_write_is_retried(tmp_path, monkeypatch):
    ln, _, runtime = build(tmp_path)
    child = ln.spawn(launch())
    real_put = ln.index.put
    fail = [True]

    def put(k, s, **kw):
        if s == "ended" and fail:
            fail.pop()
            raise OSError("full")
        return real_put(k, s, **kw)

    monkeypatch.setattr(ln.index, "put", put)
    assert ended(child) == 0 and ln.index.get(KEY)["state"] == "started"
    assert ended(child) == 0 and ln.index.get(KEY)["state"] == "ended" and runtime.ended == ["sess-1"]


# r3-7 LOW: an `ended` another launcher recorded is read through before a second END

def test_r3_7_an_end_another_launcher_proved_is_read_from_the_record(tmp_path):
    ln, _, runtime = build(tmp_path)
    child = ln.spawn(launch())
    other = L.RealLauncher(FakeControl(), FakeRuntime(), FakeSources(), ln.index)
    assert other.stop(KEY).terminated is True and ln.index.get(KEY)["state"] == "ended"
    runtime.end_ok = Refused("no-session")                       # a second END after SCRUB refuses
    assert ended(child) == 0 and runtime.ended == [] and child.abort() == 0


def test_r3_7_a_held_child_reads_the_record_too(tmp_path):
    ln, _, runtime = build(tmp_path, control=FakeControl(**BOUND9), runtime=FakeRuntime(end=False, fail=OSError("x")))
    child = ln.spawn(launch())
    assert isinstance(child, L.HeldChild)
    other = L.RealLauncher(FakeControl(), FakeRuntime(), FakeSources(), ln.index)
    assert other.stop(KEY).terminated is True
    n = len(runtime.ended)
    assert child.process.wait() == ctl.GATE_REFUSED and len(runtime.ended) == n


# r3-8 LOW: the held child of an index-write failure records its END when one is proven

def test_r3_8_the_held_child_of_an_index_failure_records_ended_with_its_session(tmp_path, monkeypatch):
    rt = FakeRuntime(end=False)
    ln, _, _ = build(tmp_path, runtime=rt, sources=FakeSources(calls=6, bytes_=60, seconds=6))
    real_put = ln.index.put
    monkeypatch.setattr(ln.index, "put", lambda k, s, **kw: (_ for _ in ()).throw(OSError("x")) if s == "started"
                        else real_put(k, s, **kw))
    child = ln.spawn(launch())
    assert isinstance(child, L.HeldChild) and ln.index.get(KEY)["state"] == "minted"
    rt.end_ok = True
    assert child.abort() == ctl.GATE_REFUSED
    rec = ln.index.get(KEY)
    assert rec["state"] == "ended" and rec["session"] == "sess-1" and ln.usage(KEY).actions == 6
