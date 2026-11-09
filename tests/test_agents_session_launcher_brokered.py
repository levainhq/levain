"""Dispatch wave 3 (S-B2 / S-B3 of ``1105-49/DESIGN_brokered_launcher.md``): the BROKERED branch of the real session
launcher, against fakes for the seams and against the REAL CONTROL handler for ``ROUTE-RESOLVE`` and the priced mint.

What a green file here does NOT show (UNVERIFIED until the VM row V1, with his credential): a real brokered slot started
under a real priced ticket, keys booking real provider calls under ``("run", run_id)`` against the share, and the
lineage sink holding real rows. See ``project_memory/1107-15-voice/RETURN_wave3.md``.

Each test names the line whose revert turns it red (``REVERT:``).
"""
from __future__ import annotations

import dataclasses
import json
import os

import pytest

from levain.agents import session as S
from levain.agents import session_launcher as L
from levain.agents.accounting import ActionExposure, Usage
from levain.broker.ledger import triples_digest
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import dispatch_usage as DU
from levain.slot import routemaps as RM
from levain.slot import tickets as T
from tests.test_routemaps import item, route, sha, st  # noqa: F401
from tests.test_agents_session_launcher import (FakeControl, FakeRuntime, FakeSources, KEY, RUN, build, zero)
from tests.test_tickets import Clock, Rig, write_ceiling

SHARE = 1_000_000
MODEL = "z-ai/glm-5.2"
DIGEST = "sha256:" + "a" * 64


def blaunch(key=KEY, *, share=SHARE, model=MODEL, occurrence="occ-1", exposure=None, auth="brokered"):
    return S.SessionLaunch(occurrence, key, "claude", model, "the brief", auth, exposure, None, share)


def resolver(generation=1, digest=DIGEST, route_id="glm"):
    return lambda f: {"route": route_id, "routemap_generation": generation, "route_policy_digest": digest}


def bbuild(tmp_path, *, spend=None, **script):
    control = FakeControl(**{"ROUTE-RESOLVE": resolver(), **script})
    runtime, sources = FakeRuntime(), FakeSources()
    ln = L.RealLauncher(control, runtime, sources, L.SessionIndex(tmp_path / "idx"), auth="brokered", spend=spend)
    return ln, control, runtime


# --- the priced mint --------------------------------------------------------------------------------------------------

def test_a_brokered_launch_mints_a_priced_ticket_with_the_resolved_route(tmp_path):
    ln, control, runtime = bbuild(tmp_path)
    ln.spawn(blaunch())
    assert control.ops() == ["ROUTE-RESOLVE", "RUN-TICKET-MINT"]
    assert control.calls[0][1] == {"model": MODEL}
    sent = control.calls[1][1]
    # REVERT: `fields["reserved_microdollars"] = launch.usd_micros` (the old literal 0 turns this red)
    assert sent["reserved_microdollars"] == SHARE
    assert (sent["route"], sent["routemap_generation"], sent["route_policy_digest"]) == ("glm", 1, DIGEST)
    assert sent["auth"] == "brokered" and sent["model_requested"] == MODEL and sent["kind"] == "executor"
    # a brokered ticket carries NO passthrough fact (check_facts forbids each)
    assert not {"vendor", "max_calls", "max_tunnel_bytes", "deadline_s"} & set(sent)
    assert T.check_facts(sent)["reserved_microdollars"] == SHARE
    # REVERT: `..., launch.workspace, self.auth))` in spawn (the runtime would start a brokered ticket as passthrough)
    assert runtime.starts[0].auth == "brokered"


def test_a_passthrough_launch_resolves_no_route_and_reserves_nothing(tmp_path):
    from tests.test_agents_session_launcher import launch
    ln, control, runtime = build(tmp_path)
    ln.spawn(launch())
    assert control.ops() == ["RUN-TICKET-MINT"]
    sent = control.calls[0][1]
    assert sent["reserved_microdollars"] == 0 and sent["auth"] == "passthrough" and "route" not in sent
    assert runtime.starts[0].auth == "passthrough"


@pytest.mark.parametrize("kw,why", [
    ({"share": None}, "no-dollar-share"),
    ({"share": 0}, "no-dollar-share"),
    ({"share": -5}, "no-dollar-share"),
    ({"share": True}, "no-dollar-share"),               # a bool is not a dollar count
    ({"share": 1.5}, "no-dollar-share"),
    ({"exposure": ActionExposure(20, "tool_calls", 3600, 5_000_000)}, "exposure-on-a-brokered-launch"),
    ({"auth": "passthrough"}, "launch-auth-not-the-launchers"),
])
def test_each_brokered_refusal_names_its_reason_before_anything_is_allocated(tmp_path, kw, why):
    ln, control, runtime = bbuild(tmp_path)
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(blaunch(**kw))
    # REVERT: the `_brokered_problem` share test, or the exposure test, or the `launch.auth != self.auth` test
    assert e.value.name == why
    assert control.calls == [] and runtime.starts == []           # not even a ROUTE-RESOLVE
    assert ln.usage(KEY) == zero()                                # a tombstone: nothing ran, zero, not the caps


def test_a_brokered_share_over_the_int_bound_is_refused_by_the_ticket_shape_not_sent(tmp_path):
    ln, control, runtime = bbuild(tmp_path)
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(blaunch(share=2**53 + 1))
    assert e.value.name.startswith("mint-facts:ticket-field:") and "RUN-TICKET-MINT" not in control.ops()
    assert runtime.starts == [] and ln.usage(KEY) == zero()


# --- ROUTE-RESOLVE refusals and the one retry --------------------------------------------------------------------------

@pytest.mark.parametrize("name,cls", [("no-brokered-route", "standing"), ("route-ambiguous", "standing"),
                                      ("control-down", "transient")])
def test_a_resolve_refusal_mints_nothing_and_reads_zero(tmp_path, name, cls):
    ln, control, runtime = bbuild(tmp_path, **{"ROUTE-RESOLVE": Refused(name)})
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(blaunch())
    assert e.value.name == f"route-resolve:{name}" and e.value.reason_class == cls
    assert control.ops() == ["ROUTE-RESOLVE"] and runtime.starts == []
    rec = ln.index.get(KEY)
    assert rec["state"] == "refused" and rec["reason_class"] == cls
    assert ln.usage(KEY) == zero()


def test_a_transient_resolve_refusal_is_retaken_by_the_next_spawn_of_the_same_occurrence(tmp_path):
    flaky = {"n": 0}

    def hook(f):
        flaky["n"] += 1
        if flaky["n"] == 1:
            raise Refused("control-down")
        return resolver()(f)

    ln, control, runtime = bbuild(tmp_path, **{"ROUTE-RESOLVE": hook})
    with pytest.raises(L.LauncherRefused):
        ln.spawn(blaunch())
    ln.spawn(blaunch())                                           # same key, same occurrence: retaken
    assert ln.index.get(KEY)["state"] == "started" and control.ops().count("RUN-TICKET-MINT") == 1


@pytest.mark.parametrize("ans", [None, [], {}, {"route": "glm"}, "x"])
def test_a_malformed_resolve_answer_is_refused_by_name(tmp_path, ans):
    ln, control, _ = bbuild(tmp_path, **{"ROUTE-RESOLVE": lambda f: ans})
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(blaunch())
    assert e.value.name == "route-resolve-malformed" and "RUN-TICKET-MINT" not in control.ops()


def test_a_route_map_that_moved_between_resolve_and_mint_is_resolved_again_once(tmp_path):
    answers = iter([resolver(1)(None), resolver(2, "sha256:" + "b" * 64)(None)])
    mints = []

    def mint(f):
        mints.append(dict(f))
        if f["routemap_generation"] == 1:
            raise Refused("ticket-field:routemap_generation", "the active generation is 2")
        return {"ticket": "t" * 43, "run_id": RUN}

    ln, control, runtime = bbuild(tmp_path, **{"ROUTE-RESOLVE": lambda f: next(answers), "RUN-TICKET-MINT": mint})
    ln.spawn(blaunch())
    # REVERT: the `exc.name in _STALE_ROUTE` retry branch in `_mint` (a single mint, then a refusal)
    assert [m["routemap_generation"] for m in mints] == [1, 2]
    assert mints[1]["route_policy_digest"] == "sha256:" + "b" * 64 and mints[1]["reserved_microdollars"] == SHARE
    assert control.ops() == ["ROUTE-RESOLVE", "RUN-TICKET-MINT", "ROUTE-RESOLVE", "RUN-TICKET-MINT"]
    assert ln.index.get(KEY)["state"] == "started" and len(runtime.starts) == 1


def test_a_second_stale_refusal_is_not_retried_again_and_is_retakable(tmp_path):
    def mint(f):
        raise Refused("ticket-field:routemap_generation")

    ln, control, runtime = bbuild(tmp_path, **{"RUN-TICKET-MINT": mint})
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(blaunch())
    # REVERT: `for again in (False, True)` (an unbounded loop) / the `not again` guard
    assert control.ops() == ["ROUTE-RESOLVE", "RUN-TICKET-MINT", "ROUTE-RESOLVE", "RUN-TICKET-MINT"]
    assert e.value.name == "mint-refused:ticket-field:routemap_generation" and e.value.reason_class == "transient"
    rec = ln.index.get(KEY)
    assert rec["state"] == "refused" and rec["reason_class"] == "transient" and runtime.starts == []
    # REVERT: `_TRANSIENT_WHOLE` (every ticket-field refusal would read standing, and a later fire could never retake)
    assert L.RealLauncher._retakable(rec, "occ-1") is True


def test_a_non_stale_mint_refusal_is_not_retried_and_stays_standing(tmp_path):
    def mint(f):
        raise Refused("ticket-ceiling:max_reserved_microdollars_per_run")

    ln, control, _ = bbuild(tmp_path, **{"RUN-TICKET-MINT": mint})
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(blaunch())
    assert control.ops() == ["ROUTE-RESOLVE", "RUN-TICKET-MINT"]
    assert e.value.reason_class == "standing" and ln.index.get(KEY)["reason_class"] == "standing"


def test_the_retry_whose_resolve_is_refused_leaves_a_refused_record_and_no_second_mint(tmp_path):
    calls = {"n": 0}

    def resolve(f):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Refused("no-brokered-route")
        return resolver()(f)

    def mint(f):
        raise Refused("ticket-field:route_policy_digest")

    ln, control, runtime = bbuild(tmp_path, **{"ROUTE-RESOLVE": resolve, "RUN-TICKET-MINT": mint})
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(blaunch())
    assert e.value.name == "route-resolve:no-brokered-route"
    assert control.ops().count("RUN-TICKET-MINT") == 1 and runtime.starts == []
    assert ln.index.get(KEY)["state"] == "refused" and ln.usage(KEY) == zero()


def test_the_mint_call_is_still_written_once():
    import pathlib
    text = pathlib.Path(L.__file__).read_text()
    assert text.count('self.control.call("RUN-TICKET-MINT"') == 1 and text.count('"ROUTE-RESOLVE"') == 1


# --- usage: the run's spend --------------------------------------------------------------------------------------------

def _ended(ln):
    # every brokered write stamps its auth; a record with none is a passthrough one (PRE-W L3 r1)
    ln.index.put(KEY, "ended", run_id=RUN, session="sess-1", auth="brokered")


def test_a_brokered_runs_usage_is_its_spend_once_ended(tmp_path):
    asked = []
    ln, _, _ = bbuild(tmp_path, spend=lambda run_id: asked.append(run_id) or 123_456)
    _ended(ln)
    u = ln.usage(KEY)
    # REVERT: the brokered branch of `usage` (a None figure: the whole reservation would be debited)
    assert u == Usage(123_456, None, None, "session:brokered") and asked == [RUN]


@pytest.mark.parametrize("bad", [None, -1, True, 1.5, "7", 2**64])
def test_a_spend_that_does_not_read_is_none_the_whole_reservation(tmp_path, bad):
    ln, _, _ = bbuild(tmp_path, spend=lambda run_id: bad)
    _ended(ln)
    u = ln.usage(KEY)
    assert u.usd_micros is None and u.source == "session:brokered-spend-unread"


def test_a_spend_reader_that_raises_or_is_absent_is_none(tmp_path):
    def boom(run_id):
        raise OSError("sink gone")

    for spend in (boom, None):
        ln, _, _ = bbuild(tmp_path / str(spend is None), spend=spend)
        _ended(ln)
        assert ln.usage(KEY).usd_micros is None


def test_spend_is_read_only_after_end_is_proven(tmp_path):
    asked = []
    ln, _, _ = bbuild(tmp_path, spend=lambda run_id: asked.append(run_id) or 5)
    ln.index.put(KEY, "started", run_id=RUN, session="sess-1", auth="brokered")
    # REVERT: moving the `rec["state"] != "ended"` test below the brokered branch (a live run's partial spend would settle)
    assert ln.usage(KEY).usd_micros is None and asked == []
    ln.index.put("s-refused0000000001", "refused", reason="x", auth="brokered")
    assert ln.usage("s-refused0000000001") == zero() and asked == []


# --- the lineage spend reader ------------------------------------------------------------------------------------------

_LEDGER, _EPOCH = "a" * 32, "b" * 32
_NEXT_REF = [0]


def _row(run, settled="yes", cost=100, hold=150, **kw):
    _NEXT_REF[0] += 1
    r = {"format": DU._LINEAGE_FORMAT, "run_id": run, "settled": settled, "cost_microdollars": cost,
         "hold_microdollars": hold, "seq": _NEXT_REF[0],
         "source": {"ledger": _LEDGER, "epoch": _EPOCH, "ref": _NEXT_REF[0]}}
    r.update(kw)
    return r


def _final(run, sink_rows, **kw):
    """The run-final row keys writes for `rows`' run rows (its own `quarantined` is a COUNT)."""
    mine = [r for r in sink_rows if r.get("run_id") == run and r.get("format") == DU._LINEAGE_FORMAT
            and "quarantined" not in r]
    f = {"format": DU._FINAL_FORMAT, "run_id": run, "rows": len(mine), "quarantined": 0,
         "triples_digest": triples_digest([r["source"] or {"ledger": _LEDGER, "epoch": _EPOCH, "ref": 0} for r in mine]), "at": "2026-11-09T00:00:00Z", "seq": 9_000}
    f.update(kw)
    return f


def _closed(rows, run=RUN, **kw):
    return list(rows) + [_final(run, rows, **kw)]


def _sink(tmp_path, rows, tail=b"\n"):
    p = tmp_path / "lineage.jsonl"
    p.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in rows[:-1]) + (json.dumps(rows[-1]).encode() + tail if rows else b""))
    return p


def test_the_lineage_sum_is_the_runs_rows_only(tmp_path):
    other = "r-fedcba9876543210"
    rows = [_row(RUN, cost=100), _row(other, cost=9_999), _row(RUN, cost=250), _row(RUN, "no", None, 40),
            _row(RUN, "uncertain", None, 70), _row(RUN, "uncertain", 90, 60), _row(RUN, "yes", None, 11)]
    p = _sink(tmp_path, _closed(rows) + [_final(other, rows)])
    # 100 + 250 + 0 (released) + 70 (uncertain, hold) + 90 (uncertain, max(cost, hold)) + 11 (cost unsaid: the hold)
    assert DU.lineage_spend(p, RUN) == 521
    assert DU.lineage_spend_reader(p, wait_s=0)(other) == 9_999


@pytest.mark.parametrize("why,rows,tail", [
    ("no row and no final for the run", [_row("r-fedcba9876543210")], b"\n"),
    ("a torn tail", _closed([_row(RUN), _row(RUN)]), b""),
    ("a foreign format", _closed([_row(RUN)]) + [{"format": "x", "run_id": RUN}], b"\n"),
    ("a figure that is no micro-dollar count", _closed([_row(RUN, cost=-1)]), b"\n"),
    ("a bool figure", _closed([_row(RUN, cost=True)]), b"\n"),
    ("an unknown settled word", _closed([_row(RUN, settled="maybe")]), b"\n"),
    ("a missing hold", _closed([_row(RUN, hold=None)]), b"\n"),
    ("a row with no source", _closed([_row(RUN, source=None)]), b"\n"),
])
def test_a_sink_that_cannot_be_read_whole_is_none(tmp_path, why, rows, tail):
    assert DU.lineage_spend(_sink(tmp_path, rows, tail), RUN) is None, why


def test_a_sink_that_is_not_json_or_has_nan_or_is_missing_or_a_link_is_none(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_bytes(b"not json\n")
    assert DU.lineage_spend(p, RUN) is None
    r = _row(RUN)
    p.write_bytes((json.dumps(r).replace("100", "NaN") + "\n" + json.dumps(_final(RUN, [r])) + "\n").encode())
    assert DU.lineage_spend(p, RUN) is None
    assert DU.lineage_spend(tmp_path / "absent", RUN) is None
    good = _sink(tmp_path, _closed([_row(RUN)]))
    assert DU.lineage_spend(good, RUN) == 100         # the control: the same sink reads with its final
    link = tmp_path / "link"
    os.symlink(good, link)
    assert DU.lineage_spend(link, RUN) is None        # REVERT: O_NOFOLLOW
    assert DU.lineage_spend(good, "") is None and DU.lineage_spend(good, None) is None
    assert DU.lineage_spend(7, RUN) is None


def test_an_over_long_line_is_none(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_bytes(json.dumps(_row(RUN, pad="x" * DU._LINE_MAX)).encode() + b"\n")
    assert DU.lineage_spend(p, RUN) is None


# --- the real CONTROL: ROUTE-RESOLVE and the priced mint ------------------------------------------------------------

@pytest.fixture
def real(st):
    return Rig(st, Clock())


class ToControl:
    def __init__(self, rig, between=None):
        self.rig, self.between, self.calls = rig, between, []

    def call(self, op, **fields):
        self.calls.append(op)
        ans = self.rig.ask(op, **fields)
        if self.between is not None and op == "ROUTE-RESOLVE":
            self.between()
        if ans.get("ok") is not True:
            raise Refused(ans["refused"], op)
        return {k: v for k, v in ans.items() if k not in ("id", "ok")}


def test_route_resolve_answers_the_active_brokered_route(real):
    ans = real.ask("ROUTE-RESOLVE", model=MODEL)
    assert ans["ok"] is True and ans["route"] == "glm" and ans["routemap_generation"] == 1
    assert ans["route_policy_digest"] == RM.policy_digest(route()["policy"])
    assert set(ans) == {"id", "ok", "route", "routemap_generation", "route_policy_digest"}


def test_route_resolve_refuses_an_unserved_model_and_a_bad_value(real):
    assert real.ask("ROUTE-RESOLVE", model="nobody/none")["refused"] == "no-brokered-route"
    # REVERT: the `T._NAME.fullmatch` test
    assert real.ask("ROUTE-RESOLVE", model="has space")["refused"] == "bad-value"
    assert real.ask("ROUTE-RESOLVE", model=7)["refused"] == "bad-value"
    assert real.ask("ROUTE-RESOLVE")["ok"] is False and real.ask("ROUTE-RESOLVE", model=MODEL, extra=1)["ok"] is False


def test_route_resolve_with_two_serving_routes_is_ambiguous_never_chosen(real):
    two, sig = real.st.signed(item(2, [route("glm"), route("glm2", host="api.z.ai")]))
    RM_submit(real, two, sig)
    real.attach({"generation": 2, "item_sha256": sha(two)})
    # REVERT: the `len(serving) > 1` test (the first sorted route would be silently chosen)
    assert real.ask("ROUTE-RESOLVE", model=MODEL)["refused"] == "route-ambiguous"


def test_route_resolve_follows_the_active_generation(real):
    price = route()["models"][0]["price"]
    two, sig = real.st.signed(item(2, [route("glm2", models=[{"requested": "other/model", "backend": "b", "price": price}])]))
    RM_submit(real, two, sig)
    real.attach({"generation": 2, "item_sha256": sha(two)})
    assert real.ask("ROUTE-RESOLVE", model=MODEL)["refused"] == "no-brokered-route"      # generation 2 no longer serves it
    assert real.ask("ROUTE-RESOLVE", model="other/model")["route"] == "glm2"


def test_route_resolve_is_the_dispatch_peers_alone(real):
    from tests.test_routemaps import OP_UID
    ans = real.control.handle(C.Conn(OP_UID), {"v": 1, "op": "ROUTE-RESOLVE", "id": 1, "model": MODEL})
    assert ans["ok"] is False


def RM_submit(rig, it, sig):
    from tests.test_routemaps import submit
    submit(rig.cr, it, sig)


def test_the_launcher_mints_a_priced_ticket_through_the_real_control_handler(real, tmp_path):
    to = ToControl(real)
    runtime = FakeRuntime()
    ln = L.RealLauncher(to, runtime, FakeSources(), L.SessionIndex(tmp_path / "idx"), auth="brokered")
    ln.spawn(blaunch(occurrence="job-1"))
    (line,) = real.lines()
    assert to.calls == ["ROUTE-RESOLVE", "RUN-TICKET-MINT"]
    f = line["facts"]
    # the journaled facts are what keys reads at TOKEN-RESOLVE: the run's cap is exactly the share
    assert f["reserved_microdollars"] == SHARE and f["route"] == "glm" and f["routemap_generation"] == 1
    assert f["model_requested"] == MODEL and f["kind"] == "executor" and "vendor" not in f
    assert f["route_policy_digest"] == RM.policy_digest(route()["policy"])
    rec = ln.index.get(KEY)
    assert rec["state"] == "started" and rec["run_id"] == line["run_id"]
    assert runtime.starts[0].auth == "brokered" and runtime.starts[0].run_id == line["run_id"]


def test_a_share_over_the_operators_ceiling_refuses_at_the_mint_and_is_standing(real, tmp_path):
    write_ceiling(real.st.L, per_run=SHARE - 1)
    real.restart()
    ln = L.RealLauncher(ToControl(real), FakeRuntime(), FakeSources(), L.SessionIndex(tmp_path / "idx"), auth="brokered")
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(blaunch())
    assert e.value.name == "mint-refused:ticket-ceiling:max_reserved_microdollars_per_run"
    assert e.value.reason_class == "standing" and real.lines() == []
    assert ln.usage(KEY) == zero()


def test_an_activation_between_resolve_and_mint_refuses_then_succeeds_once(real, tmp_path):
    two, sig = real.st.signed(item(2))

    done = []

    def activate():
        if done:
            return
        done.append(1)
        RM_submit(real, two, sig)
        real.attach({"generation": 2, "item_sha256": sha(two)})

    to = ToControl(real, between=activate)
    runtime = FakeRuntime()
    ln = L.RealLauncher(to, runtime, FakeSources(), L.SessionIndex(tmp_path / "idx"), auth="brokered")
    ln.spawn(blaunch())
    # the first resolve said generation 1; generation 2 went active before the mint; the retry resolved afresh
    assert to.calls == ["ROUTE-RESOLVE", "RUN-TICKET-MINT", "ROUTE-RESOLVE", "RUN-TICKET-MINT"]
    (line,) = real.lines()
    assert line["facts"]["routemap_generation"] == 2 and line["facts"]["reserved_microdollars"] == SHARE
    assert ln.index.get(KEY)["state"] == "started"


def test_a_replay_of_the_same_attempt_answers_the_same_ticket(real, tmp_path):
    to = ToControl(real)
    ln = L.RealLauncher(to, FakeRuntime(), FakeSources(), L.SessionIndex(tmp_path / "idx"), auth="brokered")
    ln.spawn(blaunch(occurrence="job-1"))
    (line,) = real.lines()
    again = to.call("RUN-TICKET-MINT", attempt_id=KEY, job_id="job-1", run_index=0,
                    contract_digest=line["facts"]["contract_digest"], kind="executor", egress_routes=[],
                    reserved_microdollars=SHARE, auth="brokered", route="glm", routemap_generation=1,
                    route_policy_digest=line["facts"]["route_policy_digest"], model_requested=MODEL,
                    brief_digest=line["facts"]["brief_digest"])
    assert again["run_id"] == line["run_id"]


# --- L3 wave3 r1: codex HIGH 2, the minted route reaches render ----------------------------------------------------------

def _render_runtime(tmp_path, monkeypatch):
    """A HelperSlotRuntime whose base map is setup's own default merge (it holds the base-map brokered route
    ``anthropic``), as tests/test_slot_1105_routes_cross.py builds it."""
    from levain.contracts import canon
    from levain.slot import dispatch_runtime as DR
    from levain.slot import vendor_routes as VR
    merged, _ids, _notes = VR.merge(None)
    path = tmp_path / "base.json"
    path.write_bytes(canon.canonical_bytes(merged))
    monkeypatch.setattr(DR.layout, "base_routes_path", lambda host: path)
    return DR.HelperSlotRuntime(host="darwin", helper=("true",), store=tmp_path / "store",
                                control_socket=tmp_path / "c.sock")


def _check_at_render(tmp_path, monkeypatch, req, ticket_facts):
    from levain.slot import render as R
    from levain.slot.runtime_grammar import parse_stdin
    blob = parse_stdin("render", _render_runtime(tmp_path, monkeypatch)._render_blob(req))
    R.check_ticket(blob, "dispatch", "egress", "tkt", ticket_facts, "brokered")
    return blob


def test_a_brokered_start_renders_the_route_its_ticket_was_minted_with(tmp_path, monkeypatch):
    ln, control, runtime = bbuild(tmp_path)
    ln.spawn(blaunch())
    minted = control.calls[1][1]
    assert minted["route"] == "glm"
    # REVERT: the brokered branch of `_render_blob` (it picks the base map's own brokered route, `anthropic`, and
    # render refuses ticket-routes-mismatch against the ticket's `glm`)
    blob = _check_at_render(tmp_path, monkeypatch, runtime.starts[0], minted)
    assert list(blob.routes) == ["glm"]


def test_after_a_stale_route_retry_the_start_carries_the_refreshed_route_not_the_first(tmp_path, monkeypatch):
    answers = iter([resolver(1, route_id="glm")(None), resolver(2, "sha256:" + "b" * 64, route_id="glm-b")(None)])

    def mint(f):
        if f["routemap_generation"] == 1:
            raise Refused("ticket-field:routemap_generation", "the active generation is 2")
        return {"ticket": "t" * 43, "run_id": RUN}

    ln, control, runtime = bbuild(tmp_path, **{"ROUTE-RESOLVE": lambda f: next(answers), "RUN-TICKET-MINT": mint})
    ln.spawn(blaunch())
    effective = control.calls[3][1]
    assert effective["route"] == "glm-b"
    # REVERT: spawn deriving the routes from `facts` (the first, refused resolve) in place of the `minted` facts
    # `_mint` answers (the pre-retry `glm` would reach render and be refused ticket-routes-mismatch)
    assert tuple(runtime.starts[0].routes) == ("glm-b",)
    _check_at_render(tmp_path, monkeypatch, runtime.starts[0], effective)


def test_a_brokered_start_with_no_route_is_refused_before_anything_is_allocated(tmp_path, monkeypatch):
    req = L.SlotStart("tkt", RUN, KEY, "claude", MODEL, "b", "sha256:" + "0" * 64, L.HOOKS, None, "brokered")
    with pytest.raises(L.LauncherRefused) as e:
        _render_runtime(tmp_path, monkeypatch)._render_blob(req)
    assert e.value.name == "brokered-start-without-route"


# --- L3 wave3 r1: complement MED 1, keys lagging an activation is transient ----------------------------------------------

def test_a_mint_refused_keys_unavailable_is_transient_and_the_next_spawn_retakes_it(real, tmp_path):
    two, sig = real.st.signed(item(2))
    RM_submit(real, two, sig)                     # generation 2 active; keys still states generation 1
    to = ToControl(real)
    runtime = FakeRuntime()
    ln = L.RealLauncher(to, runtime, FakeSources(), L.SessionIndex(tmp_path / "idx"), auth="brokered")
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(blaunch())
    assert e.value.name == "mint-refused:keys-unavailable"
    # REVERT: `keys-unavailable` in `_TRANSIENT` (a momentary keys lag would tombstone the occurrence for good)
    assert e.value.reason_class == "transient" and ln.index.get(KEY)["reason_class"] == "transient"
    real.attach({"generation": 2, "item_sha256": sha(two)})      # keys catches up
    ln.spawn(blaunch())
    assert ln.index.get(KEY)["state"] == "started" and len(runtime.starts) == 1


# --- the run-final proof (W3-3b): the reader settles on the final row alone -----------------------------------------------

OTHER = "r-fedcba9876543210"


class _Wall:
    """A fake clock whose sleep advances it: a wait of any length costs no time, and its ticks are countable."""

    def __init__(self, on_sleep=None):
        self.now, self.sleeps, self.on_sleep = 0.0, 0, on_sleep

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s
        self.sleeps += 1
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps)


def test_a_partially_delivered_lineage_never_settles_low(tmp_path):
    # the run made two calls (100 and 900); keys' sender has delivered only the first when END is proven: no final yet
    first, second = _row(RUN, cost=100, hold=100), _row(RUN, cost=900, hold=900)
    p = _sink(tmp_path, [first])
    # REVERT: summing without the final row (it settles at 100 and releases 900 for good)
    assert DU.lineage_spend(p, RUN) is None
    assert DU.lineage_spend_reader(p, wait_s=0)(RUN) is None
    ln, _, _ = bbuild(tmp_path / "ln", spend=DU.lineage_spend_reader(p, wait_s=0))
    _ended(ln)
    assert ln.usage(KEY).usd_micros is None                            # the whole reservation, never 100
    # a final that counts ONE row while the sink holds two (and the reverse) is no proof
    assert DU.lineage_spend(_sink(tmp_path / "ln", [first, second, _final(RUN, [first])]), RUN) is None
    assert DU.lineage_spend(_sink(tmp_path / "ln", [first, _final(RUN, [first, second], rows=2)]), RUN) is None
    p2 = _sink(tmp_path / "ln", _closed([first, second]))
    assert DU.lineage_spend(p2, RUN) == 1_000
    assert DU.lineage_spend_reader(p2, wait_s=0)(RUN) == 1_000


def test_a_digest_that_differs_is_none(tmp_path):
    # the count agrees, one source triple is not the one the final hashed (a row swapped for another run's, a forged ref)
    a, b = _row(RUN), _row(RUN)
    forged = dict(b, source=dict(b["source"], ref=b["source"]["ref"] + 500))
    assert DU.lineage_spend(_sink(tmp_path, _closed([a, b])), RUN) == 200          # the control
    # REVERT: skipping the digest check (the count alone passes)
    assert DU.lineage_spend(_sink(tmp_path, [a, forged, _final(RUN, [a, b])]), RUN) is None
    assert DU.lineage_spend(_sink(tmp_path, _closed([a, b], triples_digest="sha256:" + "0" * 64)), RUN) is None


def test_a_final_with_a_quarantine_count_is_none_and_its_quarantined_is_not_a_quarantine_row(tmp_path):
    rows = [_row(RUN), _row(RUN)]
    # REVERT: `"quarantined" in row` read on the final row (a clean final's 0 would be taken for a quarantine row)
    assert DU.lineage_spend(_sink(tmp_path, _closed(rows)), RUN) == 200
    assert DU.lineage_spend(_sink(tmp_path, _closed(rows, quarantined=1)), RUN) is None


def test_a_quarantine_of_another_run_does_not_block_this_one(tmp_path):
    rows = [_row(RUN), _row(OTHER, cost=7), _row(RUN, cost=50)]
    quarantine = {"format": DU._LINEAGE_FORMAT, "seq": 5, "source": {"ledger": _LEDGER, "epoch": _EPOCH, "ref": 777},
                  "quarantined": "record"}
    sink = rows[:2] + [quarantine] + rows[2:]
    # REVERT: a quarantine row anywhere returns None (the old rule: it may be this run's)
    assert DU.lineage_spend(_sink(tmp_path, _closed(sink)), RUN) == 150
    assert DU.lineage_spend(_sink(tmp_path, [_final(OTHER, rows, quarantined=1)] + _closed(sink)), RUN) == 150


@pytest.mark.parametrize("why,extra", [("no final", None), ("two finals", "dup"), ("a final of another run only", "other")])
def test_no_single_final_for_the_run_is_none(tmp_path, why, extra):
    rows = [_row(RUN), _row(OTHER)]
    if extra is None:
        sink = rows
    elif extra == "dup":
        sink = _closed(rows) + [_final(RUN, rows)]
    else:
        sink = rows + [_final(OTHER, rows)]
    assert DU.lineage_spend(_sink(tmp_path, sink), RUN) is None, why


def test_the_reader_waits_for_the_final_and_answers_when_it_lands(tmp_path):
    rows = [_row(RUN, cost=100, hold=100), _row(RUN, cost=900, hold=900)]
    p = _sink(tmp_path, rows[:1])
    final = json.dumps(_final(RUN, rows)).encode() + b"\n"
    body = b"".join(json.dumps(r).encode() + b"\n" for r in rows)

    def land(n):
        if n == 3:
            p.write_bytes(body[:-5])                 # the second row mid-append: a torn tail reads again
        if n == 4:
            p.write_bytes(body + final)

    w = _Wall(land)
    assert DU.lineage_spend_reader(p, wait_s=6.0, poll_s=0.2, clock=w.clock, sleep=w.sleep)(RUN) == 1_000
    assert w.sleeps == 4                              # it answered at the poll the final landed, not at the deadline


def test_the_wait_is_bounded_and_ends_in_none(tmp_path):
    p = _sink(tmp_path, [_row(RUN)])
    w = _Wall()
    assert DU.lineage_spend_reader(p, wait_s=6.0, poll_s=0.2, clock=w.clock, sleep=w.sleep)(RUN) is None
    assert 6.0 <= w.now < 6.5 and DU.FINAL_WAIT_S < 10.0       # under the client's ten-second USAGE request
    # a torn tail that never completes is the same: None at the deadline, never a spin
    p.write_bytes(p.read_bytes() + b'{"format": "x"')
    w = _Wall()
    assert DU.lineage_spend_reader(p, wait_s=1.0, poll_s=0.5, clock=w.clock, sleep=w.sleep)(RUN) is None
    assert w.now < 2.0
    assert DU.lineage_spend_reader(p, wait_s=0)("") is None


def test_a_reader_that_breaks_is_none(tmp_path):
    def boom(_):
        raise OSError("clock gone")

    assert DU.lineage_spend_reader(_sink(tmp_path, [_row(RUN)]), wait_s=1.0, sleep=boom)(RUN) is None
