"""P7-A1 (CONTRACT c7.14 §3.5 AUTH, §4.1, §5.1, §5.3): a ticket's ``auth`` axis. A passthrough ticket mints against the
ceiling's ``/2`` maxima and the base map's one passthrough route for its vendor, binds a dispatch session that spends
the slot's own login, and carries ``max_calls`` / ``deadline`` to the broker in ``QUEUE-RESOLVE``'s ``run``. A brokered
ticket reads exactly as it did (the key is absent from its facts).

The mint half runs ``Control.handle`` over a real signed route map and journal (``tests/test_tickets.py``'s rig). The
run half is the real socket: ``RUN-TICKET-MINT`` -> ``LEASE-OPEN`` -> ``ATTACH`` -> ``CAS`` / ``MINT`` -> ``SECRETS``
-> ``QUEUE-RESOLVE`` over CONTROL's unix socket (the s2a harness, peer uids played by bound client names)."""
from __future__ import annotations

import os

import pytest

from levain.contracts import canon
from levain.qbroker.broker import cap_digest, slot_ref
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import records as R
from levain.slot import render as RD
from levain.slot import routemaps as RM
from levain.slot import runtime_grammar as G
from levain.slot import runtime_helper as H
from levain.slot import tickets as T
from levain.slot.ids import SlotId
from tests.test_pool import F2_DISPATCH
from tests.test_slot_s2f2_runtime_helper import BRIEF_DIGEST
from tests.test_routemaps import DISPATCH_UID, st  # noqa: F401
from tests.test_slot_s2a_control import ROLE_UID, Env, cas
from tests.test_tickets import (PASSTHROUGH, Clock, Rig, mint_args, refused, rig, sync_free,  # noqa: F401
                                ticket_of, write_ceiling)

CEIL = {"calls": 1000, "bytes": 2**31, "deadline": 86400}


def write_ceiling2(L, **over) -> None:
    doc = {"format": T.CEILING_FORMAT_2, "max_reserved_microdollars_per_run": 5_000_000, "max_open_tickets": 4,
           "max_runs_per_day": 50, "max_calls_per_run": CEIL["calls"], "max_tunnel_bytes_per_run": CEIL["bytes"],
           "max_deadline_s": CEIL["deadline"], **over}
    (L / "policy").mkdir(exist_ok=True)
    (L / "policy" / "dispatch_ceiling").write_bytes(canon.canonical_bytes(doc))
    (L / "policy" / "dispatch_ceiling").chmod(0o644)


def pt_args(attempt: str = "a-pt", **kw) -> dict:
    args = {"attempt_id": attempt, "job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
            "kind": "executor", "auth": "passthrough", "vendor": "claude", "egress_routes": [],
            "reserved_microdollars": 0, "max_calls": 400, "max_tunnel_bytes": 400 * 2**20, "deadline_s": 7200}
    args.update(kw)
    return {k: v for k, v in args.items() if v is not None}


@pytest.fixture
def rig2(st):
    write_ceiling2(st.L)
    return Rig(st, Clock())


# ---- the axis defaults to brokered ---------------------------------------------------------------------------------

def test_a_brokered_ticket_carries_no_auth_key(rig):
    one = ticket_of(rig.mint("a-1"))
    (line,) = rig.lines()
    assert "auth" not in line["facts"] and "vendor" not in line["facts"]
    again = rig.mint("a-1", auth="brokered")                  # saying the default is the same request
    assert ticket_of(again) == one and len(rig.lines()) == 1


@pytest.mark.parametrize("extra", [{"vendor": "claude"}, {"max_calls": 4}, {"max_tunnel_bytes": 4}, {"deadline_s": 4}])
def test_a_brokered_ticket_forbids_the_passthrough_fields(rig, extra):
    assert refused(rig.mint(**extra)).startswith("ticket-field:")


@pytest.mark.parametrize("bad", ["subscription", "", None, 1, True])
def test_auth_is_brokered_or_passthrough(rig, bad):
    if bad is None:
        return
    assert refused(rig.mint(auth=bad)) == "ticket-field:auth"


def test_a_brokered_ticket_cannot_name_the_passthrough_route(rig):
    assert refused(rig.mint(egress_routes=[PASSTHROUGH])) == "ticket-field:egress_routes"


# ---- the passthrough mint ------------------------------------------------------------------------------------------

def test_a_passthrough_ticket_mints_and_journals_its_caps(rig2):
    ans = rig2.ask("RUN-TICKET-MINT", **pt_args())
    ticket_of(ans)
    (line,) = rig2.lines()
    assert line["facts"] == {"auth": "passthrough", "contract_digest": "sha256:" + "1" * 64, "deadline_s": 7200,
                             "egress_routes": [], "job_id": "job-1", "kind": "executor", "max_calls": 400,
                             "max_tunnel_bytes": 400 * 2**20, "reserved_microdollars": 0, "run_index": 0,
                             "vendor": "claude"}
    print("PASSTHROUGH MINT", ans)
    assert rig2.ask("RUN-TICKET-MINT", **pt_args()) == ans                       # idempotent
    assert refused(rig2.ask("RUN-TICKET-MINT", **pt_args(max_calls=399))) == "attempt-conflict"


def test_a_passthrough_mint_needs_no_keys_attachment(st):
    write_ceiling2(st.L)
    rig = Rig(st, Clock())
    rig.restart(attach=False)                                  # keys is not attached
    assert refused(rig.mint()) == "keys-unavailable"           # a brokered ticket waits for keys
    ticket_of(rig.ask("RUN-TICKET-MINT", **pt_args()))         # a passthrough one never reaches it


def test_a_planner_ticket_may_be_passthrough(rig2):
    ticket_of(rig2.ask("RUN-TICKET-MINT", **pt_args(kind="planner")))


def test_a_slash_one_ceiling_refuses_every_passthrough_mint(rig):
    assert refused(rig.ask("RUN-TICKET-MINT", **pt_args())) == "ceiling-lacks-subscription"
    ticket_of(rig.mint())                                      # and a brokered mint is as before


@pytest.mark.parametrize("field,key", [("max_calls", "calls"), ("max_tunnel_bytes", "bytes"),
                                       ("deadline_s", "deadline")])
def test_each_cap_is_held_to_the_ceiling(rig2, field, key):
    assert refused(rig2.ask("RUN-TICKET-MINT", **pt_args(**{field: CEIL[key] + 1}))) == f"ticket-ceiling:{field}"
    ticket_of(rig2.ask("RUN-TICKET-MINT", **pt_args("a-edge", **{field: CEIL[key]})))      # the ceiling itself passes


@pytest.mark.parametrize("bad", [
    {"route": "glm"}, {"routemap_generation": 1}, {"route_policy_digest": "sha256:" + "0" * 64},
    {"reserved_microdollars": 1}, {"max_calls": 0}, {"max_tunnel_bytes": 0}, {"deadline_s": 0},
    {"max_calls": -1}, {"max_calls": True}, {"max_calls": 1.5}, {"max_calls": "4"},
    {"vendor": "gemini"}, {"vendor": None}, {"max_calls": None}, {"max_tunnel_bytes": None}, {"deadline_s": None},
    {"verifier_id": "v"}, {"model_requested": "bad model"},
])
def test_passthrough_shape_refusals(rig2, bad):
    assert refused(rig2.ask("RUN-TICKET-MINT", **pt_args(**bad))).startswith("ticket-field:")


def test_a_verifier_ticket_is_never_passthrough(rig2):
    assert refused(rig2.ask("RUN-TICKET-MINT", **{**mint_args(), "auth": "passthrough"})) .startswith("ticket-field:")
    v = {"kind": "verifier", "auth": "passthrough", "egress_routes": [], "reserved_microdollars": 0}
    assert refused(rig2.ask("RUN-TICKET-MINT", **{**pt_args(), **v})) == "ticket-field:auth"


def test_the_vendor_needs_one_passthrough_route_in_the_base_map(rig2):
    assert refused(rig2.ask("RUN-TICKET-MINT", **pt_args(vendor="codex"))) == "ticket-field:vendor"
    base = canon.parse((rig2.st.routes / "base.json").read_bytes())
    base["routes"].append({"route": "claude-two", "type": "subscription_passthrough", "hosts": ["claude.ai"]})
    (rig2.st.routes / "base.json").write_bytes(canon.canonical_bytes(base))
    assert refused(rig2.ask("RUN-TICKET-MINT", **pt_args())) == "ticket-field:vendor"       # two: ambiguous


def test_no_egress_route_unless_listed_and_only_egress_ones(rig2):
    ticket_of(rig2.ask("RUN-TICKET-MINT", **pt_args("a-none")))
    ticket_of(rig2.ask("RUN-TICKET-MINT", **pt_args("a-pypi", egress_routes=["pypi"])))
    assert refused(rig2.ask("RUN-TICKET-MINT", **pt_args("a-bad", egress_routes=[PASSTHROUGH]))) \
        == "ticket-field:egress_routes"
    assert refused(rig2.ask("RUN-TICKET-MINT", **pt_args("a-bad2", egress_routes=["nope"]))) \
        == "ticket-field:egress_routes"


# ---- the run: the real socket ----------------------------------------------------------------------------------------

@pytest.fixture
def run_env(st, monkeypatch):
    monkeypatch.setitem(ROLE_UID, "dispatch", DISPATCH_UID)
    write_ceiling2(st.L)
    sdir = st.L / "sessions"
    sdir.mkdir(mode=0o700, exist_ok=True)
    clock = Clock()
    rig = Rig(st, clock)                                       # builds the base map + generation 1
    tickets = T.TicketStore(sdir / "tickets-sock.jsonl", st.L / "policy" / "dispatch_ceiling", clock=clock,
                            ceiling_uid=os.getuid())
    e = Env(routes=RM.ControlRoutes(st.routes), tickets=tickets, dispatch_uid=DISPATCH_UID, clock=clock, host="linux")
    e.control.start()
    sync_free(e.control)
    e.clock = clock
    yield e
    e.close()


S = "s-00000000000000c1"


def run_to_rendered(e: Env, ticket: str) -> None:
    disp, root = e.client("dispatch"), e.client("root")
    nonce = disp.ask("LEASE-OPEN", ticket=ticket)["lease_nonce"]
    claims = {"operator_uid": e.control.operator_uid, "supervisor_uid": DISPATCH_UID,
              "slot": {"class": "egress", "index": 0}, "class": "egress", "kind": "dispatch", "vendor": "claude",
              "auth": "passthrough", "lease_seq": 1, "slot_rev": 1, "ticket": ticket}
    ans = root.ask("ATTACH", lease_nonce=nonce, sudo_uid=DISPATCH_UID, session=S, claims=claims)
    assert ans["ok"], ans
    run_to_rendered.attach = ans
    assert cas(root, S, 0, "ALLOCATED", "RENDERING")["ok"]
    gen = root.ask("MINT", session=S, generation=1, claims={
        "routes": [PASSTHROUGH], "egress_routes": [], "queue_grant": True, "incarnation": 1,
        "front": {"uid": 3989, "pid": 4242, "start": 1700000000}})
    assert gen["ok"], gen
    assert root.ask("FRONT-ARM", session=S, generation=gen["generation"])["ok"]
    assert cas(root, S, gen["generation"], "RENDERING", "RENDERED")["ok"]
    e.root, e.disp = root, disp


def test_a_passthrough_ticket_end_to_end_queue_resolve_carries_the_caps(run_env):
    e = run_env
    d = e.client("dispatch")
    mint = d.ask("RUN-TICKET-MINT", **pt_args())
    ticket = ticket_of(mint)
    print("MINT", mint)
    run_to_rendered(e, ticket)
    print("ATTACH ticket_facts", run_to_rendered.attach["ticket_facts"])
    secrets = e.root.ask("SECRETS", session=S)
    assert secrets["ok"] and secrets.get("token") is None, secrets          # a passthrough session holds no token
    ans = e.client("qbroker").ask("QUEUE-RESOLVE", slot=slot_ref(0), cap_digest=cap_digest(secrets["queue_cap"]))
    print("QUEUE-RESOLVE", ans)
    attached = int(e.control._attached_at[S])
    assert ans["ok"] and ans["kind"] == "dispatch"
    assert ans["run"]["max_calls"] == 400 and ans["run"]["deadline"] == attached + 7200
    assert ans["run"]["run_id"] == mint["run_id"] and ans["run"]["model_requested"] is None
    assert run_to_rendered.attach["ticket_facts"]["deadline"] == attached + 7200     # render's absolute reading


def test_attach_refuses_a_session_auth_that_is_not_the_tickets(run_env):
    e = run_env
    ticket = ticket_of(e.client("dispatch").ask("RUN-TICKET-MINT", **pt_args()))
    disp, root = e.client("dispatch"), e.client("root")
    for over in ({"auth": "brokered"}, {"vendor": "codex"}):
        nonce = disp.ask("LEASE-OPEN", ticket=ticket)["lease_nonce"]
        claims = {"operator_uid": e.control.operator_uid, "supervisor_uid": DISPATCH_UID,
                  "slot": {"class": "egress", "index": 0}, "class": "egress", "kind": "dispatch", "vendor": "claude",
                  "auth": "passthrough", "lease_seq": 1, "slot_rev": 1, "ticket": ticket, **over}
        assert root.ask("ATTACH", lease_nonce=nonce, sudo_uid=DISPATCH_UID, session=S, claims=claims)["refused"] \
            == "claims-mismatch"
        disp.sock.close()
        disp = e.client("dispatch")


def test_a_brokered_ticket_cannot_attach_as_passthrough(run_env):
    e = run_env
    # a brokered ticket straight from the store (the mint verb would wait for a keys attachment this rig has none of)
    ticket = e.control.tickets.mint("a-b", T.check_facts(mint_args("a-b")), C.new_handle,
                                    slots_free=e.control.pool.free).ticket
    disp, root = e.client("dispatch"), e.client("root")
    nonce = disp.ask("LEASE-OPEN", ticket=ticket)["lease_nonce"]
    claims = {"operator_uid": e.control.operator_uid, "supervisor_uid": DISPATCH_UID,
              "slot": {"class": "egress", "index": 0}, "class": "egress", "kind": "dispatch", "vendor": "claude",
              "auth": "passthrough", "lease_seq": 1, "slot_rev": 1, "ticket": ticket}
    assert root.ask("ATTACH", lease_nonce=nonce, sudo_uid=DISPATCH_UID, session=S, claims=claims)["refused"] \
        == "claims-mismatch"


def test_mint_claims_must_be_the_vendors_route_plus_the_listed_egress(run_env):
    e = run_env
    ticket = ticket_of(e.client("dispatch").ask("RUN-TICKET-MINT", **pt_args()))
    disp, root = e.client("dispatch"), e.client("root")
    nonce = disp.ask("LEASE-OPEN", ticket=ticket)["lease_nonce"]
    claims = {"operator_uid": e.control.operator_uid, "supervisor_uid": DISPATCH_UID,
              "slot": {"class": "egress", "index": 0}, "class": "egress", "kind": "dispatch", "vendor": "claude",
              "auth": "passthrough", "lease_seq": 1, "slot_rev": 1, "ticket": ticket}
    assert root.ask("ATTACH", lease_nonce=nonce, sudo_uid=DISPATCH_UID, session=S, claims=claims)["ok"]
    assert cas(root, S, 0, "ALLOCATED", "RENDERING")["ok"]
    front = {"uid": 3989, "pid": 4242, "start": 1700000000}
    for routes, egress in ((["pypi", PASSTHROUGH], ["pypi"]),          # an egress route the ticket did not list
                           ([], []), (["glm"], [])):                  # no passthrough route / a dispatch-map one
        got = root.ask("MINT", session=S, generation=1, claims={
            "routes": routes, "egress_routes": egress, "queue_grant": True, "incarnation": 1, "front": front})
        assert got["refused"] == "claims-mismatch", (routes, got)


# ---- prepare's credential-slot rule ------------------------------------------------------------------------------------

def _view(i: int, *, creds=(), baseline=None, dispatch=None, state="reusable") -> H.SlotView:
    slot = SlotId("egress", i)
    st = R.SlotState(slot, 0, 0, state, True, "2026-10-20T00:00:00Z", None)
    return H.SlotView(st, slot, frozenset(creds), baseline, dispatch)


def pick(views, **kw):
    base = dict(slot_class="egress", kind="dispatch", vendor="claude", auth="passthrough", named=None, ticketed=True)
    base.update(kw)
    return H.pick_slot(views, **base)


def test_a_passthrough_ticket_takes_only_a_marked_reusable_slot_for_its_vendor():
    marked = _view(2, creds=["claude"], baseline="claude", dispatch="claude")
    views = [_view(0), _view(1, creds=["claude"], baseline="claude"), marked, _view(3)]
    assert pick(views).index == 2


@pytest.mark.parametrize("views", [
    [_view(0), _view(1)],                                                                     # credential-free only
    [_view(0, creds=["claude"], baseline="claude")],                                           # a credential, no mark
    [_view(0, creds=["codex"], baseline="codex", dispatch="codex")],                           # marked for the other vendor
    [_view(0, creds=["claude"], baseline="claude", dispatch="claude", state="leased")],        # not reusable
    [_view(0, baseline="claude", dispatch="claude")],                                          # mark, no credential parked
])
def test_no_dispatch_credential_names_the_vendor(views):
    with pytest.raises(Refused) as e:
        pick(views)
    assert e.value.name == "no-dispatch-credential:claude"


def test_the_lowest_marked_slot_and_never_a_named_one():
    views = [_view(3, creds=["claude"], baseline="claude", dispatch="claude"),
             _view(1, creds=["claude"], baseline="claude", dispatch="claude")]
    assert pick(views).index == 1
    with pytest.raises(Refused) as e:
        pick(views, named=3)
    assert e.value.name == "ticket-slot-argument"


def test_a_brokered_ticket_never_takes_a_marked_slot():
    views = [_view(0, creds=["claude"], baseline="claude", dispatch="claude"), _view(1), _view(2)]
    assert pick(views, auth="brokered").index == 1


def test_the_mark_is_read_from_the_baseline_and_only_as_the_text_yes():
    def b(**kw):
        return canon.canonical_bytes({"format": "levain-baseline/1", "vendor": "claude", "observed_id": "x",
                                      "confirmed_at": "2026-10-20T00:00:00Z", "session": "s-0", **kw})
    assert H.baseline_dispatch_vendor(b(dispatch="yes")) == "claude"
    for data in (b(), b(dispatch="no"), b(dispatch=True), b(dispatch="YES"), None, b"not json",
                 canon.canonical_bytes({"format": "levain-baseline/9", "vendor": "claude", "dispatch": "yes"}),
                 canon.canonical_bytes({"format": "levain-baseline/1", "vendor": "gemini", "dispatch": "yes"})):
        assert H.baseline_dispatch_vendor(data) is None, data


# ---- confirm-identity --dispatch -------------------------------------------------------------------------------------

def argv(*a, caller="operator"):
    return G.parse_runtime_argv(list(a), configured_slots={"egress": 8, "none": 2}, caller=caller)


def test_confirm_identity_takes_dispatch_only_with_yes():
    s = "s-00000000000000c1"
    assert argv("confirm-identity", s, "yes").dispatch is False
    assert argv("confirm-identity", s, "yes", "--dispatch").dispatch is True
    for bad, name in ((("confirm-identity", s, "no", "--dispatch"), "bad-token"),
                      (("confirm-identity", s, "yes", "yes"), "extra-argv"),
                      (("confirm-identity", s, "yes", "--dispatch", "x"), "extra-argv")):
        with pytest.raises(Refused) as e:
            argv(*bad)
        assert e.value.name == name, bad
    with pytest.raises(Refused) as e:
        argv("confirm-identity", s, "yes", "--dispatch", caller="dispatch")        # the verb stays the operator's
    assert e.value.name == "verb-not-allowed-for-caller"


# ---- render: the routes rule and the API-key refusal ------------------------------------------------------------------

TF = {"kind": "executor", "job_id": "j", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
      "egress_routes": ["github"], "reserved_microdollars": 0, "auth": "passthrough", "vendor": "claude",
      "max_calls": 40, "max_tunnel_bytes": 2**20, "deadline_s": 600, "deadline": 1_900_000_000,
      "brief_digest": BRIEF_DIGEST}


def rblob(routes, vendor="claude", model=None):
    return G.RenderBlob(vendor=vendor, routes=tuple(routes), model=model)


def check(routes, facts=TF, **kw):
    RD.check_ticket(rblob(routes, **kw), "dispatch", "egress", "tk", facts, auth="passthrough")


def test_check_ticket_passthrough_routes_rule():
    check(["claude-subscription", "github"])
    check(["github", "claude-subscription"])
    for routes in (["claude-subscription"],                                  # drops a listed egress route
                   ["claude-subscription", "github", "pypi"],                # an egress route the ticket did not list
                   ["claude-subscription", "claude-other", "github"],        # two non-egress
                   ["github"], ["github", "github"]):
        with pytest.raises(Refused) as e:
            check(routes)
        assert e.value.name == "ticket-routes-mismatch", routes


def test_check_ticket_passthrough_none_by_default():
    check(["claude-subscription"], {**TF, "egress_routes": []})
    with pytest.raises(Refused) as e:
        check(["claude-subscription", "github"], {**TF, "egress_routes": []})
    assert e.value.name == "ticket-routes-mismatch"


def test_check_ticket_passthrough_other_gates():
    with pytest.raises(Refused) as e:
        check(["claude-subscription", "github"], vendor="codex")
    assert e.value.name == "ticket-vendor-mismatch"
    with pytest.raises(Refused) as e:
        check(["claude-subscription", "github"], model="m")
    assert e.value.name == "ticket-model-mismatch"
    for k in RD.PASSTHROUGH_TICKET_KEYS:
        with pytest.raises(Refused) as e:
            check(["claude-subscription", "github"], {x: v for x, v in TF.items() if x != k})
        assert e.value.name == "ticket-facts-missing", k
    with pytest.raises(Refused) as e:                                        # alloc says brokered, the ticket passthrough
        RD.check_ticket(rblob(["claude-subscription", "github"]), "dispatch", "egress", "tk", TF, auth="brokered")
    assert e.value.name == "ticket-auth-mismatch"
    with pytest.raises(Refused) as e:                                        # alloc says passthrough, the ticket brokered
        RD.check_ticket(rblob(["glm", "github"], model="m"), "dispatch", "egress", "tk",
                        {**{k: v for k, v in TF.items() if k not in RD.PASSTHROUGH_TICKET_KEYS and k != "auth"},
                         "route": "glm", "model_requested": "m", "reserved_microdollars": 5},
                        auth="passthrough")
    assert e.value.name == "ticket-auth-mismatch"


def test_a_brokered_ticket_check_is_as_before():
    f = {"kind": "executor", "job_id": "j", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
         "egress_routes": ["github"], "reserved_microdollars": 5, "route": "glm", "model_requested": "m",
         "routemap_generation": 1, "route_policy_digest": "sha256:" + "2" * 64,
         "brief_digest": BRIEF_DIGEST}
    RD.check_ticket(rblob(["glm", "github"], model="m"), "dispatch", "egress", "tk", f)                  # no auth given
    RD.check_ticket(rblob(["glm", "github"], model="m"), "dispatch", "egress", "tk", f, auth="brokered")
    with pytest.raises(Refused) as e:
        RD.check_ticket(rblob(["glm", "pypi"], model="m"), "dispatch", "egress", "tk", f, auth="brokered")
    assert e.value.name == "ticket-routes-mismatch"


@pytest.mark.parametrize("vendor,name", [("claude", "ANTHROPIC_API_KEY"), ("claude", "ANTHROPIC_AUTH_TOKEN"),
                                         ("codex", "OPENAI_API_KEY"), ("codex", "CODEX_API_KEY")])
def test_an_api_key_in_a_passthrough_env_is_refused(vendor, name):
    with pytest.raises(Refused) as e:
        RD.check_env_absent(vendor, ["PATH", name], "passthrough")
    assert e.value.name == "env-forbidden" and name in e.value.detail
    if (vendor, name) != ("claude", "ANTHROPIC_API_KEY"):
        RD.check_env_absent(vendor, ["PATH", name], "brokered")           # the brokered carrier still passes
        RD.check_env_absent(vendor, ["PATH", name])


# ---- render's front file: a passthrough ticket is no longer refused, and its limits ride in session.json ---------------

def _pt_alloc(w, session: str = "s-00000000000000e2", facts=None):
    from tests.test_slot_3b8a_f1 import BOOT, DISPATCH_UID as DU, OP_UID
    slot = SlotId("egress", 1)
    tf = {**TF, "egress_routes": ["github"], **(facts or {})}
    a = R.Alloc(session=session, slot=slot, lease_seq=1, slot_rev=1, slot_uid=897, slot_gid=897, kind="dispatch",
                vendor="claude", auth="passthrough", operator_uid=OP_UID, supervisor_uid=DU,
                staging_uid=DU, created_at="2026-10-20T00:00:00Z", boot_id=BOOT, ticket=C.new_handle(),
                netns=slot.netns, cgroup=R.cgroup_path(slot, session), ticket_facts=tf)
    (w.L / "alloc" / f"{session}.json").write_bytes(R.alloc_bytes(a))
    return session


def _stub_limits(monkeypatch):
    """Stands in for P7-A2: its `front_limit_fields` is stubbed with the shape its page gives, and the front's own
    parser (P7-A2's to extend for a passthrough route in a dispatch kind and the two keys) is stood down."""
    from levain.broker import front as FR
    monkeypatch.setattr(RD, "front_limit_fields",
                        lambda tunnel_bytes_cap=None, deadline=None: {"tunnel_bytes_cap": tunnel_bytes_cap,
                                                                      "deadline": deadline}, raising=False)
    monkeypatch.setattr(FR, "load_policy", lambda data: None)


def test_the_alloc_record_carries_a_passthrough_tickets_facts(tmp_path):
    from tests.test_slot_3b8a_front_wiring import _world
    w = _world(tmp_path)
    s = _pt_alloc(w)
    got = w.alloc(s)
    assert got.ticket_facts["deadline"] == 1_900_000_000 and got.ticket_facts["auth"] == "passthrough"
    assert H.is_passthrough_ticket(got)


def test_front_policy_for_a_passthrough_ticket_carries_its_limits(tmp_path, monkeypatch):
    from tests.test_slot_3b8a_front_wiring import _world
    _stub_limits(monkeypatch)
    w = _world(tmp_path)
    a = w.alloc(_pt_alloc(w))
    store = H.Store(w.seams)
    try:
        data = H.front_policy_bytes(store, a, ["claude-subscription", "github"])
    finally:
        store.close()
    doc = canon.parse(data)
    print("SESSION.JSON", doc)
    assert doc["tunnel_bytes_cap"] == 2**20 and doc["deadline"] == 1_900_000_000
    assert [r["route"] for r in doc["routes"]] == ["claude-subscription", "github"]


def test_front_policy_without_the_limit_function_is_refused_not_written_uncapped(tmp_path, monkeypatch):
    from tests.test_slot_3b8a_front_wiring import _world
    monkeypatch.delattr(RD, "front_limit_fields", raising=False)
    w = _world(tmp_path)
    a = w.alloc(_pt_alloc(w))
    store = H.Store(w.seams)
    try:
        with pytest.raises(Refused) as e:
            H.front_policy_bytes(store, a, ["claude-subscription", "github"])
    finally:
        store.close()
    assert e.value.name == "front-limits-unbuilt"


def test_a_brokered_ticket_is_still_refused_route_policy_unavailable(tmp_path):
    from tests.test_slot_3b8a_f1 import _ticketed
    from tests.test_slot_3b8a_front_wiring import _world
    w = _world(tmp_path)
    a = w.alloc(_ticketed(w, "anthropic"))
    with pytest.raises(Refused) as e:
        H.refuse_ticketed_front(a)
    assert e.value.name == "route-policy-unavailable"


def test_a_passthrough_alloc_with_a_brokered_ticket_fact_is_not_a_passthrough_ticket(tmp_path):
    from tests.test_slot_3b8a_front_wiring import _world
    w = _world(tmp_path)
    a = w.alloc(_pt_alloc(w, facts={"auth": "brokered"}))
    assert not H.is_passthrough_ticket(a)
    with pytest.raises(Refused):
        H.refuse_ticketed_front(a)


@pytest.mark.parametrize("missing", ["max_tunnel_bytes", "deadline"])
def test_a_passthrough_front_file_needs_its_limits(tmp_path, monkeypatch, missing):
    from tests.test_slot_3b8a_front_wiring import _world
    _stub_limits(monkeypatch)
    w = _world(tmp_path)
    s = _pt_alloc(w)
    a = w.alloc(s)
    facts = {k: v for k, v in a.ticket_facts.items() if k != missing}
    from dataclasses import replace
    with pytest.raises(Refused) as e:
        H.passthrough_front_limits(replace(a, ticket_facts=facts))
    assert e.value.name == "ticket-facts-missing"


# ---- prepare -> render, through the helper's own verbs (lane F2's world with a /2 ceiling and the route store) ---------

class PTDispatch:
    """`tests/test_pool.Dispatch` with a `/2` ceiling and CONTROL holding the route store, so a passthrough ticket's
    MINT claims check can find the base map's passthrough route. Tickets come from the store's own mint."""

    def __init__(self, world, request) -> None:
        self.w = world
        L = world.L
        write_ceiling2(L)
        self.tickets = T.TicketStore(L / "sessions" / T.JOURNAL, L / "policy" / "dispatch_ceiling",
                                     ceiling_uid=os.getuid())
        world.control = C.Control(C.StateStore(L / "sessions"), world.control.operator_uid, dispatch_uid=F2_DISPATCH,
                                  tickets=self.tickets, routes=RM.ControlRoutes(L / "routes"),
                                  keys_front_register=lambda *a: None, keys_front_arm=lambda s, t: None)
        world.control.start()
        request.addfinalizer(world.control.stop)
        world.seams.grant_search = lambda host, fd, path, broker, uid: None
        self.n = 0

    def ticket(self, **over) -> str:
        self.n += 1
        over = {"brief_digest": BRIEF_DIGEST, **over}
        facts = T.check_facts(pt_args(f"a-{self.n}", egress_routes=["github"], **over))
        return self.tickets.mint(f"a-{self.n}", facts, C.new_handle).ticket

    def prepare(self, ticket: str, *args: str) -> dict:
        conn = C.Conn(F2_DISPATCH)
        lo = self.w.control.handle(conn, {"v": 1, "op": "LEASE-OPEN", "id": 1, "ticket": ticket})
        assert lo["ok"], lo
        self.conn = conn
        _c, line = self.w.main(["prepare", *(args or ("egress", "dispatch", "claude", "passthrough"))],
                               canon.canonical_bytes({"lease_nonce": lo["lease_nonce"], "ticket": ticket}),
                               uid=F2_DISPATCH)
        return line

    def drop(self) -> None:
        self.w.control.lease_eof(self.conn.lease)


@pytest.fixture
def ptd(tmp_path, request, monkeypatch):
    from tests import test_slot_s2f2_runtime_helper as F2
    monkeypatch.setitem(F2.ACCOUNTS, H.QBROKER, 790)
    # `identity.check_baseline` (P7-A4's file, not this slice's) refuses a baseline carrying the new key, so
    # `baseline_vendor` reads a marked file as "?"; the stand-in reads the vendor the way it will once identity.py
    # accepts `dispatch`, and nothing else about the baseline changes.
    real = H._baseline_vendor
    monkeypatch.setattr(H, "_baseline_vendor", lambda data: (
        canon.parse(data)["vendor"] if data is not None and canon.parse(data).get("dispatch") == "yes" else real(data)))
    return PTDispatch(F2.World(tmp_path), request)


def mark(w, slot: SlotId, vendor: str = "claude") -> None:
    w.give_credential(slot, vendor)
    p = slot.slot_dir(w.L) / "baseline"
    p.write_bytes(canon.canonical_bytes({**canon.parse(p.read_bytes()), "dispatch": "yes"}))


def test_prepare_takes_the_marked_slot_for_a_passthrough_ticket(ptd):
    d, w = ptd, ptd.w
    w.give_credential(SlotId("egress", 0))                          # the operator's own login, not marked
    mark(w, SlotId("egress", 1))
    line = d.prepare(d.ticket())
    assert line["ok"], (line, w.last_err)
    assert line["slot"] == {"class": "egress", "index": 1}
    a = w.alloc(line["session"])
    assert a.auth == "passthrough" and a.kind == "dispatch" and H.is_passthrough_ticket(a)
    attached = int(w.control._attached_at[line["session"]])
    print("ALLOC ticket_facts", a.ticket_facts)
    assert a.ticket_facts["deadline"] == attached + 7200 and a.ticket_facts["max_calls"] == 400
    assert w.main(["status", line["session"]], uid=F2_DISPATCH)[1]["ok"]


def test_prepare_refuses_with_no_marked_slot(ptd):
    d, w = ptd, ptd.w
    ticket = d.ticket()
    assert d.prepare(ticket)["refused"] == "no-dispatch-credential:claude"          # no credential anywhere
    d.drop()
    w.give_credential(SlotId("egress", 0))                                           # a login, no mark
    assert d.prepare(ticket)["refused"] == "no-dispatch-credential:claude"
    d.drop()
    mark(w, SlotId("egress", 2), "codex")                                            # marked for the other vendor
    assert d.prepare(ticket)["refused"] == "no-dispatch-credential:claude"
    assert [op for op, _ in w.calls].count("ATTACH") == 0
    for i in range(3):
        assert w.slot_state(SlotId("egress", i)).state == "reusable"


def test_a_passthrough_ticket_prepared_as_brokered_is_refused_at_attach(ptd):
    d, w = ptd, ptd.w
    ticket = d.ticket()
    line = d.prepare(ticket, "egress", "dispatch", "claude", "brokered")            # the credential-free path
    assert line["refused"] == "claims-mismatch", (line, w.last_err)
    assert w.slot_state(SlotId("egress", 0)).state == "reusable"


def test_render_of_a_passthrough_ticket_end_to_end(ptd, monkeypatch):
    from tests.test_slot_3c7_fix_r1 import _blob
    _stub_limits(monkeypatch)
    d, w = ptd, ptd.w
    mark(w, SlotId("egress", 0))
    line = d.prepare(d.ticket())
    assert line["ok"], (line, w.last_err)
    s = line["session"]
    w.stage(s)
    code, ans = w.main(["render", s], _blob(routes=["claude-sub", "github"], model=None), uid=F2_DISPATCH)
    print("RENDER", code, ans, w.last_err)
    assert ans == {"ok": True}, w.last_err
    doc = canon.parse((w.R / "gw" / "slot0" / "session.json").read_bytes())
    assert doc["tunnel_bytes_cap"] == 400 * 2**20 and doc["deadline"] == w.alloc(s).ticket_facts["deadline"]
    print("SESSION.JSON", doc)
    assert [r["route"] for r in doc["routes"]] == ["claude-sub", "github"]
    env = (w.L / "live" / s / "root" / "env").read_text() if (w.L / "live" / s / "root" / "env").exists() else None
    if env is not None:
        assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env and "ANTHROPIC_BASE_URL" not in env


def test_render_refuses_a_passthrough_blob_that_names_another_route(ptd, monkeypatch):
    from tests.test_slot_3c7_fix_r1 import _blob
    _stub_limits(monkeypatch)
    d, w = ptd, ptd.w
    mark(w, SlotId("egress", 0))
    s = d.prepare(d.ticket())["session"]
    w.stage(s)
    calls = len(w.calls)
    for routes, name in ((["claude-sub"], "ticket-routes-mismatch"),
                         (["claude-sub", "github", "anthropic"], "ticket-routes-mismatch"),
                         (["anthropic", "github"], "auth-routes-mismatch")):      # the ticket gate passes it: one extra
        assert w.main(["render", s], _blob(routes=routes, model=None), uid=F2_DISPATCH)[1]["refused"] == name, routes
    assert w.calls[calls:] == []


# ---- the operator surface: `levain session ... --login --dispatch` --------------------------------------------------------

def test_launch_dispatch_flag_needs_login():
    from levain.slot import launch as L
    req = L.parse(["claude", "--repo", "/r", "--auth", "passthrough", "--login", "--dispatch"])
    assert req.dispatch is True and req.kind == "login"
    assert L.parse(["claude", "--repo", "/r", "--auth", "passthrough", "--login"]).dispatch is False
    with pytest.raises(Refused) as e:
        L.parse(["claude", "--repo", "/r", "--auth", "passthrough", "--dispatch"])
    assert e.value.name == "dispatch-needs-login"


def _sup(answer, dispatch):
    from pathlib import Path
    from tests.test_slot_idg1fx_identity import _Sup
    from levain.slot import supervisor as SV
    sup = _Sup("claude", answer, {"observed_id": "4b8e0c1a-1d2e-4f00-9a7b-0123456789ab"})
    sup.req = SV.Request(vendor="claude", repo=Path("/nonexistent"), auth="passthrough", kind="login",
                         dispatch=dispatch)
    return sup


def test_the_supervisor_sends_dispatch_only_with_a_yes_and_the_flag():
    S1_ = "s-00000000000000c1"
    sup = _sup(True, True)
    sup._confirm_identity(S1_)
    assert sup.calls[-1] == ("confirm-identity", S1_, "yes", "--dispatch")
    assert "unattended" in sup.prompts[0]
    sup = _sup(True, False)
    sup._confirm_identity(S1_)
    assert sup.calls[-1] == ("confirm-identity", S1_, "yes") and "unattended" not in sup.prompts[0]
    sup = _sup(False, True)
    sup._confirm_identity(S1_)
    assert sup.calls[-1] == ("confirm-identity", S1_, "no")
