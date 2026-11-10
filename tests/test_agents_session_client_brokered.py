"""The client half of the brokered dispatch run server: which socket a launch, its usage and its end reach.

The client is the REAL :class:`levain.agents.session_client.DispatchSessionLauncher` with an ``AuthorityFeed`` and the
store's auth reader; the servers are the REAL pair of run servers (one holder registry, one index, one replica) over
socketpairs, each connect counted. Held here: an api_key occurrence reaches the brokered server and mints with the
link's share, a subscription one reaches the passthrough server; USAGE and END follow the auth the store recorded, also
from a client built afresh; nothing but the END of a brokered session whose socket is gone is tried on the other
socket; a brokered launch with no brokered server refuses
by name with nothing minted."""
from __future__ import annotations

import pytest

from levain.agents import session as S
from levain.agents import session_client as C
from levain.agents import session_launcher as L
from tests.test_agents_session_launch import EXP, _occurrence, _reservation, _run
from tests.test_dispatch_prew import KEY_B, Pair, lrig  # noqa: F401 - the rig fixture and the server pair


@pytest.fixture
def pair(lrig, tmp_path):  # noqa: F811 - the fixture's own parameter
    p = Pair(lrig, tmp_path)
    yield p
    p.close()


class Wire:
    """The two connectors of one client: each open is counted per socket, and a socket whose server is ``None`` is not
    there (``FileNotFoundError``, what a connect to a missing socket file raises)."""

    def __init__(self, passthrough, brokered):
        self.servers = {"passthrough": passthrough, "brokered": brokered}
        self.opens = {"passthrough": 0, "brokered": 0}

    def connector(self, auth, pair):
        def connect(within=None):
            server = self.servers[auth]
            if server is None:
                raise FileNotFoundError(2, "no such socket")
            self.opens[auth] += 1
            return pair.conn(server)
        connect.accepts_within = True
        return connect


def client_of(rig, pair, *, brokered_up=True):
    wire = Wire(pair.pt, pair.br if brokered_up else None)
    client = C.DispatchSessionLauncher(
        wire.connector("passthrough", pair), lambda occ: C.link_proof(rig.store, rig.world.root, occ, now=rig.now()),
        C.AuthorityFeed(rig.store, rig.world.root), None, None, wire.connector("brokered", pair), C.session_auth_of(rig.store))
    return client, wire


def api_launch(rig, oid, key):
    figure = rig.authority.replay().policy.grants[rig.row(oid)["link_id"]]["budget_run_usd_micros"]
    return S.SessionLaunch(oid, key, "claude", "sonnet", "b", "brokered", None, None, figure), figure


def record(rig, oid, key):
    """What ``run_session`` records before it spawns: the occurrence's session key."""
    with rig.store.write() as conn:
        conn.execute("UPDATE agent_occurrences SET session_key = ? WHERE occurrence_id = ?", (key, oid))


def test_the_link_billing_check_reads_the_auths_a_launcher_serves():
    class Both:
        route, auth, auths = S.S1_DISPATCH, "passthrough", frozenset({"passthrough", "brokered"})

    class One:
        route, auth = S.S1_DISPATCH, "passthrough"

    assert S._auths_of(Both()) == {"passthrough", "brokered"} and S._auths_of(One()) == {"passthrough"}
    assert S._auths_of(C.DispatchSessionLauncher(lambda: None, lambda o: None)) == {"passthrough"}
    assert S._auths_of(C.DispatchSessionLauncher(lambda: None, lambda o: None, connect_brokered=lambda: None,
                                                 auth_of=lambda k: None)) == {"passthrough", "brokered"}


def test_an_api_key_launch_reaches_the_brokered_server_and_mints_with_the_links_share(lrig, pair):
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    client, wire = client_of(lrig, pair)
    launch, figure = api_launch(lrig, oid, KEY_B)
    child = client.spawn(launch)
    assert wire.opens == {"passthrough": 0, "brokered": 1}
    mints = [f for op, f in pair.control.calls if op == "RUN-TICKET-MINT"]
    assert len(mints) == 1 and mints[0]["auth"] == "brokered" and mints[0]["reserved_microdollars"] == figure
    assert pair.index.get(KEY_B)["auth"] == "brokered"
    child.abort()


def test_a_subscription_launch_still_reaches_run_sock(lrig, pair):
    oid = _occurrence(lrig, billing="subscription", exposure=EXP)
    client, wire = client_of(lrig, pair)
    child = client.spawn(S.SessionLaunch(oid, "s-00000000000000c2", "claude", "sonnet", "b", "passthrough", EXP))
    assert wire.opens == {"passthrough": 1, "brokered": 0}
    mints = [f for op, f in pair.control.calls if op == "RUN-TICKET-MINT"]
    assert len(mints) == 1 and mints[0]["auth"] == "passthrough"
    child.abort()


def test_run_session_of_an_api_key_occurrence_goes_through_the_brokered_socket_end_to_end(lrig, pair):
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    client, wire = client_of(lrig, pair)
    _sup, report = _run(lrig, oid, client, micros=1_000_000)
    assert (report.outcome, report.state) == ("exited", "done"), report
    assert wire.opens["passthrough"] == 0 and wire.opens["brokered"] >= 2      # LAUNCH and the USAGE read at settle
    assert _reservation(lrig, oid)[0] == "settled"
    assert _reservation(lrig, oid)[5] == 4242                                  # the brokered server's own spend reader


def test_usage_and_end_of_a_brokered_session_go_to_the_brokered_socket_also_from_a_rebuilt_client(lrig, pair):
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    first, _wire = client_of(lrig, pair)
    launch, _figure = api_launch(lrig, oid, KEY_B)
    record(lrig, oid, KEY_B)
    child = first.spawn(launch)
    child.release()
    rebuilt, wire = client_of(lrig, pair)                    # no memory of the launch: only the store says which socket
    ended = rebuilt.end_session(KEY_B, 10)
    assert ended.terminated is True and wire.opens == {"passthrough": 0, "brokered": 1}
    got = rebuilt.usage(KEY_B)
    assert (got.usd_micros, got.source) == (4242, "session:brokered") and wire.opens == {"passthrough": 0, "brokered": 2}


def test_a_policy_flip_to_passthrough_after_start_does_not_move_a_brokered_sessions_usage_and_end(lrig, pair, tmp_path,
                                                                                                    monkeypatch):
    import os
    from levain.slot import layout
    from levain.slot.services import dispatch_run as DR
    policy = tmp_path / "dispatch_auth"
    monkeypatch.setattr(layout, "policy_dispatch_auth_path", lambda host: policy)
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    client, _wire = client_of(lrig, pair)
    launch, _figure = api_launch(lrig, oid, KEY_B)
    record(lrig, oid, KEY_B)
    child = client.spawn(launch)
    child.release()
    policy.write_bytes(DR.DISPATCH_AUTH_OFF)
    policy.chmod(0o600)
    assert DR.read_dispatch_auth(policy, owner=os.getuid()) == ("passthrough", None)       # the opt-out is now in force
    rebuilt, wire = client_of(lrig, pair)
    ended = rebuilt.end_session(KEY_B, 10)
    got = rebuilt.usage(KEY_B)
    assert (got.usd_micros, got.source) == (4242, "session:brokered")
    assert ended.terminated is True and wire.opens == {"passthrough": 0, "brokered": 2}


def test_a_reservation_with_no_billing_is_brokered_to_the_auth_reader(lrig):
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    record(lrig, oid, KEY_B)
    auth_of = C.session_auth_of(lrig.store)
    with lrig.store.write() as conn:
        conn.execute("UPDATE agent_reservations SET billing = NULL WHERE occurrence_id = ?", (oid,))
    assert auth_of(KEY_B) == "brokered"


def test_a_subscription_sessions_usage_and_end_go_to_run_sock_only(lrig, pair):
    oid = _occurrence(lrig, billing="subscription", exposure=EXP)
    key = "s-00000000000000c3"
    record(lrig, oid, key)
    first, _w = client_of(lrig, pair)
    first.spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP)).release()
    rebuilt, wire = client_of(lrig, pair)
    assert rebuilt.end_session(key, 10).terminated is True
    rebuilt.usage(key)
    assert wire.opens == {"passthrough": 2, "brokered": 0}


def test_a_session_the_store_records_no_auth_for_is_passthroughs_and_never_tried_on_the_other_socket(lrig, pair):
    client, wire = client_of(lrig, pair)
    assert client.end_session("s-00000000000000c4", 5).terminated is False      # the server holds no such session
    got = client.usage("s-00000000000000c4")
    assert got.usd_micros is None and wire.opens == {"passthrough": 2, "brokered": 0}


def test_with_no_brokered_server_a_brokered_launch_refuses_by_name_and_nothing_is_minted(lrig, pair):
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    client, wire = client_of(lrig, pair, brokered_up=False)
    launch, _figure = api_launch(lrig, oid, KEY_B)
    with pytest.raises(L.LauncherRefused) as e:
        client.spawn(launch)
    assert e.value.name == "brokered-server-absent" and e.value.reason_class == "transient"
    assert "turned off" in e.value.detail and "down" in e.value.detail and e.value.nothing_sent is True
    assert wire.opens == {"passthrough": 0, "brokered": 0}                     # never tried on run.sock
    assert [op for op, _f in pair.control.calls if op == "RUN-TICKET-MINT"] == [] and pair.index.get(KEY_B) is None


def test_a_run_session_with_no_brokered_server_settles_at_zero(lrig, pair):
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    client, _wire = client_of(lrig, pair, brokered_up=False)
    _sup, report = _run(lrig, oid, client, micros=1_000_000)
    assert report.outcome == "session_spawn_failed" and report.code == 1
    res = _reservation(lrig, oid)
    assert res[0] == "settled" and res[5] == 0


def test_a_client_with_no_brokered_socket_configured_refuses_a_brokered_launch(lrig, pair):
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    wire = Wire(pair.pt, None)
    client = C.DispatchSessionLauncher(wire.connector("passthrough", pair),
                                       lambda occ: C.link_proof(lrig.store, lrig.world.root, occ, now=lrig.now()),
                                       C.AuthorityFeed(lrig.store, lrig.world.root))
    assert client.auths == {"passthrough"}
    launch, _figure = api_launch(lrig, oid, KEY_B)
    with pytest.raises(L.LauncherRefused) as e:
        client.spawn(launch)
    assert e.value.name == "brokered-server-absent" and wire.opens == {"passthrough": 0, "brokered": 0}


def test_the_launcher_takes_both_brokered_arguments_or_neither():
    with pytest.raises(ValueError):
        C.DispatchSessionLauncher(lambda: None, lambda o: None, connect_brokered=lambda: None)
    with pytest.raises(ValueError):
        C.DispatchSessionLauncher(lambda: None, lambda o: None, auth_of=lambda k: None)


def test_the_brokered_socket_name_is_one_constant_the_server_imports():
    from levain.slot.services import dispatch_run as DR
    assert DR.BROKERED_SOCKET_NAME is C.BROKERED_SOCKET_NAME
    assert C.default_brokered_socket() == C.default_run_socket().parent / C.BROKERED_SOCKET_NAME


# --- r1 A: a definitive refusal before any process starts settles the reservation at zero --------------------------------

@pytest.mark.parametrize("billing", ["api_key", "subscription"])
def test_a_launch_the_server_refuses_for_no_free_slot_settles_at_zero(lrig, pair, billing):
    oid = (_occurrence(lrig, billing="api_key", micros=1_000_000) if billing == "api_key"
           else _occurrence(lrig, billing="subscription", exposure=EXP))
    pair.br._pool_free = pair.pt._pool_free = lambda: False
    client, _wire = client_of(lrig, pair)
    kw = {"micros": 1_000_000} if billing == "api_key" else {"exposure": EXP}
    _sup, report = _run(lrig, oid, client, **kw)
    assert report.outcome == "session_spawn_failed" and report.code == 1
    res = _reservation(lrig, oid)
    zero = (None, None, None, 0) if billing == "api_key" else (0, 0, 0, 0)     # an api_key run carries no action caps
    assert res[:2] == ("settled", "actual") and res[2:] == zero, res
    assert [op for op, _f in pair.control.calls if op == "RUN-TICKET-MINT"] == []


@pytest.mark.parametrize("fault", ["feed", "wire", "connect"])
def test_a_local_failure_before_launch_is_marked_nothing_sent(lrig, pair, fault):
    import socket
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    client, _wire = client_of(lrig, pair)
    if fault == "feed":
        class Unreadable:
            def read(self, occ):
                raise C.ProofUnavailable("the authority log does not read", standing=False)
        client._feed = Unreadable()
    elif fault == "wire":
        def closed(within=None):
            a, b = socket.socketpair()
            b.close()
            return a
        client._connect_brokered = closed
    else:
        def refused(within=None):
            raise PermissionError(13, "denied")
        client._connect_brokered = refused
    with pytest.raises(L.LauncherRefused) as e:
        client.spawn(api_launch(lrig, oid, KEY_B)[0])
    assert e.value.nothing_sent is True


def test_a_refusal_before_spawn_is_a_retakable_tombstone_and_never_overwrites_a_live_holders_key(lrig, pair):
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    launch, _figure = api_launch(lrig, oid, KEY_B)
    pair.br._refuse_unspawned(launch, L.LauncherRefused("no-free-slot", reason_class="transient"))
    rec = pair.index.get(KEY_B)
    assert (rec["state"], rec["reason_class"], rec["occurrence_id"]) == ("refused", "transient", oid)
    client, _wire = client_of(lrig, pair)
    child = client.spawn(launch)                    # the same key, the same occurrence: the tombstone is retaken
    assert pair.index.get(KEY_B)["state"] == "started"
    pair.br._refuse_unspawned(launch, L.LauncherRefused("no-free-slot", reason_class="transient"))
    assert pair.index.get(KEY_B)["state"] == "started"      # a live holder's key is never recorded refused
    child.abort()


# --- r1 B: END of a brokered session falls back to run.sock when the brokered server is gone; USAGE and LAUNCH never --

def test_end_of_a_brokered_session_reaches_run_sock_after_the_brokered_server_is_gone(lrig, pair):
    from levain.agents import session_launcher as SL
    from levain.slot.services import dispatch_run as DR
    from tests.test_agents_session_launcher import FakeSources
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    first, _w = client_of(lrig, pair)
    launch, _figure = api_launch(lrig, oid, KEY_B)
    record(lrig, oid, KEY_B)
    first.spawn(launch).release()
    # the opt-out and a restart: the brokered server is gone, a fresh passthrough server (its own registry) over the
    # same index and runtime is all that answers
    restarted = DR.RunServer(SL.RealLauncher(pair.control, pair.runtime, FakeSources(), pair.index),
                             verify=lambda *a: None, pool_free=lambda: True, peer_ok=lambda s: True, holder_idle_s=20)
    wire = Wire(restarted, None)
    rebuilt = C.DispatchSessionLauncher(wire.connector("passthrough", pair), lambda occ: None, None, None, None,
                                        wire.connector("brokered", pair), C.session_auth_of(lrig.store))
    ended = rebuilt.end_session(KEY_B, 10)
    assert ended.terminated is True, ended
    assert wire.opens == {"passthrough": 1, "brokered": 0}
    assert pair.index.get(KEY_B)["state"] == "ended"
    # The slot's session was ended (the slot is released). Held by identity, not by call count: the released first
    # launch's holder runs its own END for the key when its connection closes, on the brokered server's job and
    # asynchronously, and the restarted server's END is a separate job; each reads "not yet ended" before the other
    # records it, so both may reach the runtime. A runtime END of an ended group is idempotent, so what is held is
    # that only the slot's own session was ever ended, at least once.
    assert pair.runtime.ended and set(pair.runtime.ended) == {"sess-1"}, pair.runtime.ended


def test_usage_of_a_brokered_session_never_falls_back_to_run_sock(lrig, pair):
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    record(lrig, oid, KEY_B)
    client, wire = client_of(lrig, pair, brokered_up=False)
    got = client.usage(KEY_B)
    assert got.usd_micros is None and got.source == "session:dispatch-unreachable"
    assert wire.opens == {"passthrough": 0, "brokered": 0}


def test_a_brokered_launch_never_falls_back_to_run_sock(lrig, pair):
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    client, wire = client_of(lrig, pair, brokered_up=False)
    with pytest.raises(L.LauncherRefused):
        client.spawn(api_launch(lrig, oid, KEY_B)[0])
    assert wire.opens == {"passthrough": 0, "brokered": 0}


# --- r1 D: the launch's auth must be the reservation's billing's, not merely one the launcher serves -------------------

def test_an_api_key_reservation_is_never_launched_with_the_passthrough_auth_on_a_two_auth_launcher():
    from tests.test_agents_session_launch import API, ROLE, SUBRES

    class Both:
        route, auth, auths = S.S1_DISPATCH, "passthrough", frozenset({"passthrough", "brokered"})

    assert S.launch_refusal(ROLE, API, Both(), None, usd_micros=100_000, auth="passthrough") \
        == "ticket-auth-not-the-links-billing"
    assert S.launch_refusal(ROLE, SUBRES, Both(), EXP, usd_micros=0, auth="brokered") \
        == "ticket-auth-not-the-links-billing"
    assert S.launch_refusal(ROLE, API, Both(), None, usd_micros=100_000, auth="brokered") is None


# --- r1 G: only a missing or refusing brokered socket reads as the brokered server absent ------------------------------

@pytest.mark.parametrize("err,name", [(FileNotFoundError(2, "gone"), "brokered-server-absent"),
                                      (ConnectionRefusedError(61, "refused"), "brokered-server-absent"),
                                      (PermissionError(13, "denied"), "dispatch-unreachable"),
                                      (TimeoutError("slow"), "dispatch-unreachable")])
def test_a_brokered_connect_error_is_absent_only_when_the_socket_is_missing_or_refusing(lrig, pair, err, name):
    oid = _occurrence(lrig, billing="api_key", micros=1_000_000)
    client, wire = client_of(lrig, pair)

    def connect(within=None):
        raise err
    client._connect_brokered = connect
    with pytest.raises(L.LauncherRefused) as e:
        client.spawn(api_launch(lrig, oid, KEY_B)[0])
    assert (e.value.name, e.value.reason_class, e.value.nothing_sent) == (name, "transient", True)
    assert wire.opens["passthrough"] == 0


# --- r2 A1: a refusal the server makes from the frame's claims, before any spawn, settles at zero too ---------------------

def _forge_claims(client, forge):
    """The client's claims as it read them, with ``forge`` laid over them (what a client behind or on another chain sends)."""
    real = client._claims

    def claims(conn, occ, *, bar):
        got = real(conn, occ, bar=bar)
        return {**got, **forge(got)}
    client._claims = claims


@pytest.mark.parametrize("billing", ["api_key", "subscription"])
@pytest.mark.parametrize("forge,name", [
    (lambda got: {"ledger_head": {"seq": got["ledger_head"]["seq"], "hash": "f" * 64}}, "ledger-fork"),
    (lambda got: {"bar": {"open": False, "closed_revoke_seq": got["ledger_head"]["seq"] + 1}}, "bar-invalid"),
    (lambda got: {"bar": {"open": "no", "closed_revoke_seq": 0}}, "bad-request"),
], ids=["ledger-fork", "bar-invalid", "claims-off-the-wire"])
def test_a_launch_the_server_refuses_on_its_frame_claims_settles_at_zero(lrig, pair, billing, forge, name):
    oid = (_occurrence(lrig, billing="api_key", micros=1_000_000) if billing == "api_key"
           else _occurrence(lrig, billing="subscription", exposure=EXP))
    client, _wire = client_of(lrig, pair)
    _forge_claims(client, forge)
    kw = {"micros": 1_000_000} if billing == "api_key" else {"exposure": EXP}
    _sup, report = _run(lrig, oid, client, **kw)
    assert report.outcome == "session_spawn_failed" and report.code == 1
    res = _reservation(lrig, oid)
    zero = (None, None, None, 0) if billing == "api_key" else (0, 0, 0, 0)
    assert res[:2] == ("settled", "actual") and res[2:] == zero, res
    with lrig.store.read() as c:
        key = c.execute("SELECT session_key FROM agent_occurrences WHERE occurrence_id = ?", (oid,)).fetchone()[0]
    rec = pair.index.get(key)
    assert rec is not None and (rec["state"], rec["reason"], rec["occurrence_id"]) == ("refused", name, oid), rec
    assert [op for op, _f in pair.control.calls if op == "RUN-TICKET-MINT"] == []


# --- r2 G1: the run.sock fallback of END never runs on a deadline the brokered connect already spent ---------------------

def test_end_whose_brokered_connect_spent_the_deadline_does_not_try_run_sock(lrig, pair, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(C.time, "monotonic", lambda: clock[0])
    wire = Wire(pair.pt, None)
    tried = []

    def gone(within=None):
        clock[0] += 5.0                                  # the brokered connect took the whole deadline, then failed
        raise FileNotFoundError(2, "no such socket")
    gone.accepts_within = True

    def passthrough(within=None):
        tried.append(within)
        return pair.conn(pair.pt)
    passthrough.accepts_within = True
    client = C.DispatchSessionLauncher(passthrough, lambda occ: None, None, None, None, gone, lambda key: "brokered")
    got = client.end_session(KEY_B, 5)
    assert got.terminated is False and "TimeoutError" in got.detail, got     # what a capped connect's timeout reads
    assert tried == []                                   # nothing was connected on a spent deadline


def test_end_falls_back_to_run_sock_with_only_what_the_deadline_has_left(lrig, pair, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(C.time, "monotonic", lambda: clock[0])
    tried = []

    def gone(within=None):
        clock[0] += 3.0
        raise FileNotFoundError(2, "no such socket")
    gone.accepts_within = True

    def passthrough(within=None):
        tried.append(within)
        raise ConnectionRefusedError(61, "refused")
    passthrough.accepts_within = True
    client = C.DispatchSessionLauncher(passthrough, lambda occ: None, None, None, None, gone, lambda key: "brokered")
    got = client.end_session(KEY_B, 5)
    assert got.terminated is False and tried == [pytest.approx(2.0)], (got, tried)
