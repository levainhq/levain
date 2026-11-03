"""P7-A1 L3 r1 fixes (`project_memory/1102-5/L3_p7a1_r1.txt`): a passthrough ticket's limits are held to the bounds the
runtime downstream of the mint accepts, at the ceiling's read, at mint and at the bind (codex HIGH); a passthrough
mint is not counted against the credential-free pool it never takes (codex MED); the base map is read once, through
the bounded no-follow reader (codex MEDs); a `--dispatch` mark is read back from the written baseline (deepseek MED 2);
and, where P7-A2's front limits are in the tree, render + the front's own parser run for real (codex HIGH a).

Each repro test was run red on ed61b1ab first (the lane's return names the failures)."""
from __future__ import annotations

import os

import pytest

from levain.contracts import canon
from levain.contracts.grammar import JSON_INT_MAX
from levain.qbroker import broker as QB
from levain.qbroker.broker import cap_digest, slot_ref
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import limits_doc
from levain.slot import render as RD
from levain.slot import routemaps as RM
from levain.slot import runtime_helper as H
from levain.slot import tickets as T
from levain.slot.ids import SlotId
from tests.test_pool import F2_DISPATCH
from tests.test_routemaps import DISPATCH_UID, st  # noqa: F401
from tests.test_slot_p7a1_passthrough_ticket import (S, mark, pt_args, ptd, rig2, run_env,  # noqa: F401
                                                     run_to_rendered, write_ceiling2)
from tests.test_tickets import Clock, Rig, refused, sync_free, ticket_of

BIG = {"max_calls_per_run": 10**9 + 1, "max_tunnel_bytes_per_run": JSON_INT_MAX, "max_deadline_s": JSON_INT_MAX}


def _mint(e, **kw) -> dict:
    return e.client("dispatch").ask("RUN-TICKET-MINT", **pt_args(**kw))


# ---- (1) limits the runtime downstream rejects never mint ---------------------------------------------------------------

def test_calls_past_the_broker_cap_never_mint(run_env):
    """Repro: on ed61b1ab a ticket with max_calls = 10**9 + 1 minted and bound, and QUEUE-RESOLVE answered
    run-malformed (CONTROL's and qbroker's cap is limits_doc's 10**9)."""
    e = run_env
    write_ceiling2(e.control.tickets.ceiling_path.parent.parent, **BIG)
    ans = _mint(e, max_calls=10**9 + 1)
    print("MINT max_calls=10**9+1", ans)
    assert refused(ans) == "ticket-field:max_calls"


def test_calls_at_the_broker_cap_mint_and_resolve(run_env):
    e = run_env
    write_ceiling2(e.control.tickets.ceiling_path.parent.parent, max_calls_per_run=10**9)
    ticket = ticket_of(_mint(e, max_calls=10**9))
    run_to_rendered(e, ticket)
    secrets = e.root.ask("SECRETS", session=S)
    ans = e.client("qbroker").ask("QUEUE-RESOLVE", slot=slot_ref(0), cap_digest=cap_digest(secrets["queue_cap"]))
    print("QUEUE-RESOLVE at the cap", ans)
    assert ans["ok"] and ans["run"]["max_calls"] == 10**9


def test_a_deadline_past_the_alloc_range_never_mints(run_env):
    """Repro: on ed61b1ab deadline_s = JSON_INT_MAX minted, ATTACH bound the ticket and answered an absolute deadline
    past JSON_INT_MAX, which prepare's alloc record cannot hold (the ticket was already consumed)."""
    e = run_env
    write_ceiling2(e.control.tickets.ceiling_path.parent.parent, **BIG)
    ans = _mint(e, deadline_s=JSON_INT_MAX)
    print("MINT deadline_s=JSON_INT_MAX", ans)
    assert refused(ans) == "ticket-field:deadline_s"


def test_a_deadline_that_cannot_be_reached_from_now_never_mints(st):  # noqa: F811
    """The span is checked against the mint's clock: a ticket bound within its unused lifetime answers an absolute
    deadline the run caps accept."""
    write_ceiling2(st.L, max_deadline_s=T.DEADLINE_MAX)
    clock = Clock()
    clock.t = float(T.DEADLINE_MAX - 3600)
    rig = Rig(st, clock)
    assert refused(rig.ask("RUN-TICKET-MINT", **pt_args(deadline_s=3600))) == "ticket-field:deadline_s"
    ticket_of(rig.ask("RUN-TICKET-MINT", **pt_args("a-ok", deadline_s=3600 - T.TICKET_UNUSED_S - 1)))


def test_attach_refuses_a_deadline_past_the_range_and_leaves_the_ticket_unbound(run_env, monkeypatch):
    """The bind is the last place the absolute deadline is computed. The mint's check (its clock + the unused
    lifetime) leaves this backstop unreachable on one clock, so the bound is lowered under a minted ticket to stand
    for a clock that moved: the ATTACH is refused before the bind, and the ticket is not consumed."""
    e = run_env
    ticket = ticket_of(_mint(e, deadline_s=7200))
    monkeypatch.setattr(T, "DEADLINE_MAX", int(e.clock()) + 7200 - 1)
    disp, root = e.client("dispatch"), e.client("root")
    nonce = disp.ask("LEASE-OPEN", ticket=ticket)["lease_nonce"]
    claims = {"operator_uid": e.control.operator_uid, "supervisor_uid": DISPATCH_UID,
              "slot": {"class": "egress", "index": 0}, "class": "egress", "kind": "dispatch", "vendor": "claude",
              "auth": "passthrough", "lease_seq": 1, "slot_rev": 1, "ticket": ticket}
    ans = root.ask("ATTACH", lease_nonce=nonce, sudo_uid=DISPATCH_UID, session=S, claims=claims)
    print("ATTACH past the range", ans)
    assert ans["refused"] == "ticket-deadline-range"
    assert e.control.tickets.holdable(ticket).ticket == ticket                 # still unbound, mintable work intact


@pytest.mark.parametrize("key,value", [("max_calls_per_run", 10**9 + 1), ("max_deadline_s", 10**12 + 1)])
def test_a_ceiling_past_the_downstream_bounds_does_not_load(st, key, value):  # noqa: F811
    write_ceiling2(st.L, **{key: value})
    with pytest.raises(Refused) as e:
        T.read_ceiling(st.L / "policy" / "dispatch_ceiling", os.getuid())
    assert e.value.name == "ticket-ceiling:file"


def test_the_bounds_are_the_downstream_bounds():
    """One definition per bound, each no wider than the reader downstream of the mint (a bound found wider here fails
    this test, not a run)."""
    assert T.CALLS_MAX == limits_doc.SLOT_RANGE["calls"][1] == C._RUN_CALLS_MAX
    assert QB._calls_ok(T.CALLS_MAX) and not QB._calls_ok(T.CALLS_MAX + 1)
    assert T.DEADLINE_MAX == C._RUN_DEADLINE_MAX
    assert QB._deadline_ok(T.DEADLINE_MAX) and not QB._deadline_ok(T.DEADLINE_MAX + 1)
    assert T.DEADLINE_MAX <= JSON_INT_MAX                                   # the alloc record's integer range
    assert T.TUNNEL_BYTES_MAX <= min(2**53, JSON_INT_MAX)
    from levain.broker import norm
    for name, mine in (("PT_CAP_MAX", T.TUNNEL_BYTES_MAX), ("PT_DEADLINE_MAX", T.DEADLINE_MAX)):
        if hasattr(norm, name):                                             # P7-A2's front bounds, once merged
            assert mine <= getattr(norm, name), name


@pytest.mark.parametrize("field,value", [("max_calls", 10**9 + 1), ("deadline_s", 10**12 + 1),
                                         ("max_tunnel_bytes", JSON_INT_MAX + 1)])
def test_check_facts_refuses_past_each_bound(field, value):
    with pytest.raises(Refused) as e:
        T.check_facts(pt_args(**{field: value}))
    assert e.value.name == f"ticket-field:{field}"


# ---- (2) a passthrough ticket is not counted against the credential-free pool ----------------------------------------

def test_a_passthrough_mint_does_not_need_a_credential_free_slot(rig2):  # noqa: F811
    """Repro: with only the operator's credential-free egress slot listed, a passthrough mint was refused
    no-free-slot:egress (the brokered count), though it takes a marked credential slot."""
    sync_free(rig2.control, egress=1)
    assert rig2.ask("POOL")["egress_free"] == 0
    ans = rig2.ask("RUN-TICKET-MINT", **pt_args())
    print("PASSTHROUGH MINT, no credential-free slot", ans)
    ticket_of(ans)


def test_an_unbound_passthrough_ticket_leaves_the_brokered_count_alone(rig2):  # noqa: F811
    sync_free(rig2.control, egress=3)
    before = rig2.ask("POOL")["egress_free"]
    ticket_of(rig2.ask("RUN-TICKET-MINT", **pt_args("a-1")))
    ticket_of(rig2.ask("RUN-TICKET-MINT", **pt_args("a-2")))
    assert rig2.ask("POOL")["egress_free"] == before == 2


# ---- (3) + (4) one bounded, no-follow read of the base map ------------------------------------------------------------

def test_the_base_map_is_never_read_unbounded(rig2, monkeypatch):  # noqa: F811
    """Repro: on ed61b1ab the passthrough route was read by Path.read_bytes (no size bound, follows a link, blocks on
    a FIFO) before the bounded reader ever ran. A FIFO is not planted here: on the old code it hangs the suite."""
    from pathlib import Path
    real = Path.read_bytes

    def guarded(self):
        assert self.name != "base.json", "base.json read by Path.read_bytes"
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    ticket_of(rig2.ask("RUN-TICKET-MINT", **pt_args(egress_routes=["pypi"])))


def test_a_linked_base_map_is_refused(rig2):  # noqa: F811
    """A link at base.json refuses the mint (on ed61b1ab too: the second, bounded read caught it after the first had
    followed it)."""
    routes = rig2.st.routes
    real = routes / "base-real.json"
    os.replace(routes / "base.json", real)
    os.symlink(real, routes / "base.json")
    assert refused(rig2.ask("RUN-TICKET-MINT", **pt_args(egress_routes=["pypi"]))) == "config-invalid:base"


def test_the_route_and_the_egress_types_come_from_one_snapshot(rig2, monkeypatch):  # noqa: F811
    """Repro: the base map replaced between the passthrough-route read and the egress-types read made the mint judge
    one map's route against another map's egress ids. One read now serves both."""
    routes = rig2.st.routes
    replacement = canon.parse((routes / "base.json").read_bytes())
    replacement["routes"] = [r for r in replacement["routes"] if r["route"] != "pypi"]
    real_parse, seen = C.parse_base_map, []

    def parse_then_replace(obj):
        out = real_parse(obj)
        if not seen:
            seen.append(1)
            (routes / "base.json").write_bytes(canon.canonical_bytes(replacement))
        return out

    monkeypatch.setattr(C, "parse_base_map", parse_then_replace)
    monkeypatch.setattr(RM, "parse_base_map", parse_then_replace)
    ans = rig2.ask("RUN-TICKET-MINT", **pt_args(egress_routes=["pypi"]))
    print("MINT across a base-map replacement", ans)
    ticket_of(ans)


# ---- deepseek MED 2: the dispatch mark is read back, never assumed --------------------------------------------------------

def test_a_dispatch_confirm_whose_baseline_lacks_the_mark_is_refused(monkeypatch, tmp_path):
    from levain.slot import identity as ID
    from tests.test_slot_p7a1_passthrough_ticket import argv
    monkeypatch.setattr(ID, "BASELINE_DISPATCH_KEY", "dispatch", raising=False)
    monkeypatch.setattr(ID, "confirm_identity", lambda ctx, req, blob: {"baseline": "written"})
    unmarked = canon.canonical_bytes({"format": "levain-baseline/1", "vendor": "claude", "observed_id": "x"})
    monkeypatch.setattr(H, "_written_baseline", lambda ctx, session: ("claude", unmarked))
    req = argv("confirm-identity", "s-00000000000000c1", "yes", "--dispatch")
    with pytest.raises(Refused) as e:
        H._confirm_identity(None, req, None)
    assert e.value.name == "dispatch-mark-unwritten"
    marked = canon.canonical_bytes({"format": "levain-baseline/1", "vendor": "claude", "observed_id": "x",
                                    "dispatch": "yes"})
    monkeypatch.setattr(H, "_written_baseline", lambda ctx, session: ("claude", marked))
    assert H._confirm_identity(None, req, None) == {"baseline": "written"}


# ---- codex HIGH (a): with P7-A2's front limits in the tree, render and the front's parser run for real -------------------

def _p7a2_present() -> bool:
    from levain.broker import front as FR
    return (getattr(RD, "front_limit_fields", None) is not None
            and "tunnel_bytes_cap" in getattr(FR.FrontPolicy, "__dataclass_fields__", {}))


@pytest.mark.skipif(not _p7a2_present(), reason="P7-A2 (render.front_limit_fields + the front's tunnel_bytes_cap) is "
                                                "not in this tree: the combined tree runs this test")
def test_render_of_a_passthrough_ticket_through_the_real_front_parser(ptd):  # noqa: F811
    from levain.broker import front as FR
    from tests.test_slot_3c7_fix_r1 import _blob
    d, w = ptd, ptd.w
    mark(w, SlotId("egress", 0))
    line = d.prepare(d.ticket())
    assert line["ok"], (line, w.last_err)
    s = line["session"]
    w.stage(s)
    code, ans = w.main(["render", s], _blob(routes=["claude-sub", "github"], model=None), uid=F2_DISPATCH)
    print("RENDER (real front parser)", code, ans, w.last_err)
    assert ans == {"ok": True}, w.last_err
    data = (w.R / "gw" / "slot0" / "session.json").read_bytes()
    pol = FR.load_policy(data)
    assert pol.tunnel_bytes_cap == 400 * 2**20 and pol.deadline == w.alloc(s).ticket_facts["deadline"]
