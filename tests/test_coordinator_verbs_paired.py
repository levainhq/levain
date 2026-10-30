"""The Coordinator panel's Start, Stop and Hold fired with a PAIRED DEVICE's token on the web cockpit.

A paired token resolves to the same credential class the shared write token does (a ``token`` named
``paired-<device>``), so the three C1 panel verbs must answer it exactly as they answer the shared token, and must
answer a revoked, unknown or absent token with a refusal that writes nothing. Each success is proved on the
coordinator store itself (the lease, the launch rows), never on the HTTP status alone, and each refusal is held
against a control showing the same fire writes when the credential is good, so an equal snapshot cannot be vacuous.
The harness is the pairing tests' serve and the coordinator verb tests' store and fake ``claude``; nothing here runs
a real session."""
from __future__ import annotations

import contextlib
from datetime import timedelta
from unittest import mock

import pytest

from levain.cockpit import coordinator_status as cs
from levain.cockpit.consent import utc_stamp
from levain.cockpit.coordinator_status import COORD_STATUS_ID, COORD_VERBS, HOLD_VERB, START_VERB, STOP_VERB
from levain.dashboard import SubstrateSource
from tests.test_cockpit_pairing_http import JUNK, PROXY, T, _call, _pair, _serve, src  # noqa: F401 - `src` is a fixture
from tests.test_coordinator_verbs import (T0, _fire, _held_back, _levain_home, _panel_state, _rows, _snapshot,  # noqa: F401
                                          _verbs, clock, fake, live_hook, qdir, quick, store, workdir)


@contextlib.contextmanager
def _serving(src_, specs, **kw):
    """A write-scoped pairing serve whose coordinator verbs are ``specs`` (built over the fixture's store)."""
    with mock.patch.object(cs, "status_verbs", lambda d, **k: specs):
        with _serve(src_, coordinator_verbs=True, **kw) as pair:
            yield pair


def _fire_http(port: int, verb: str, token: "str | None", key: str, headers: "dict | None" = None):
    return _call(port, "POST", "/cockpit/verb", {"verb": verb, "panel_id": COORD_STATUS_ID, "params": {},
                                                 "idempotency_key": key}, token=token, headers=headers)


def _discover(port: int) -> None:
    """The coordinator panels are discovered on a manifest read."""
    assert _call(port, "GET", "/cockpit/manifest.json", token=T)[0] == 200


def _credential(port: int, who: str) -> str:
    return T if who == "shared" else _pair(port)[1]


WHO = pytest.mark.parametrize("who", ["shared", "paired"])


# --- (1) the three verbs, each reaching its success path -------------------------------------------------------------
@WHO
def test_hold_with_the_token_takes_the_lease_for_thirty_minutes(src, store, qdir, clock, who) -> None:
    v = _verbs(qdir, clock, launcher=_held_back)
    assert store.coord_lease() is None
    with _serving(src, v.specs()) as (port, _h):
        _discover(port)
        tok = _credential(port, who)
        st, out, _ = _fire_http(port, HOLD_VERB, tok, "h1")
    until = utc_stamp(T0 + timedelta(minutes=30))
    assert (st, out.get("ok")) == (200, True), out
    assert out["result"]["until"] == until
    held = store.coord_lease()
    assert (held.holder, held.until, held.token, held.live) == ("operator", until, out["result"]["token"], True)
    assert _panel_state(store, clock)[0] == "held"


@WHO
def test_start_with_the_token_writes_a_launch_and_starts_it(src, store, qdir, fake, live_hook, clock, who) -> None:
    v = _verbs(qdir, clock)
    assert _rows(qdir, "SELECT count(*) FROM launches") == [(0,)]
    with _serving(src, v.specs()) as (port, _h):
        _discover(port)
        tok = _credential(port, who)
        st, out, _ = _fire_http(port, START_VERB, tok, "s1")
        v.wait_starts(30)
    assert (st, out.get("ok")) == (200, True), out
    lid = out["result"]["launch_id"]
    assert out["result"]["state"] == "launching" and lid.startswith("ln-")
    assert _rows(qdir, "SELECT launch_id, role FROM launches") == [(lid, "coordinator")]
    assert store.launch(lid).state == "active" and len(fake.spawns()) == 1


@WHO
def test_stop_with_the_token_releases_the_lease_and_ends_the_launch(src, store, qdir, fake, live_hook, clock,
                                                                    who) -> None:
    v = _verbs(qdir, clock)
    lid = _fire(v, START_VERB)["launch_id"]
    v.wait_starts(30)
    held = store.coord_lease()
    assert held is not None and store.launch(lid).state == "active"
    with _serving(src, v.specs()) as (port, _h):
        _discover(port)
        tok = _credential(port, who)
        st, out, _ = _fire_http(port, STOP_VERB, tok, "p1")
    assert (st, out.get("ok")) == (200, True), out
    assert out["result"]["released"]["token"] == held.token
    ln = store.launch(lid)
    assert (ln.state, ln.cause) == ("ended", "operator release")
    assert store.coord_lease() is None


def test_a_paired_token_over_the_proxied_path_fires_hold_like_the_phone_does(src, store, qdir, clock) -> None:
    """The phone reaches the cockpit through the proxy header; the device credential is the same class there."""
    v = _verbs(qdir, clock, launcher=_held_back)
    with _serving(src, v.specs()) as (port, _h):
        _discover(port)
        _dev, tok = _pair(port, proxied=True)
        st, out, _ = _fire_http(port, HOLD_VERB, tok, "h1", headers=PROXY)
    assert (st, out.get("ok")) == (200, True), out
    assert store.coord_lease().token == out["result"]["token"]


# --- (2) no credential that may write ---------------------------------------------------------------------------------
@pytest.mark.parametrize("verb", COORD_VERBS)
def test_no_token_and_a_token_nobody_holds_are_refused_and_write_nothing(src, store, qdir, fake, live_hook, clock,
                                                                       verb) -> None:
    v = _verbs(qdir, clock, launcher=_held_back)
    if verb == STOP_VERB:                       # a lease must exist, or a release has nothing to change
        _fire(v, HOLD_VERB)
    with _serving(src, v.specs()) as (port, _h):
        _discover(port)
        _dev, paired = _pair(port)
        before = _snapshot(qdir)
        for i, token in enumerate((None, JUNK, "wrong-token")):
            st, out, _ = _fire_http(port, verb, token, f"r{i}")
            assert (st, out.get("error")) == (403, "credential_required"), (token, st, out)
        assert _snapshot(qdir) == before
        # the control: the same fire with a good credential writes, so the equal snapshot above was a refusal
        st, out, _ = _fire_http(port, verb, paired, "ok")
        assert (st, out.get("ok")) == (200, True), out
        assert _snapshot(qdir) != before


@pytest.mark.parametrize("verb", COORD_VERBS)
def test_a_server_without_a_write_scope_has_no_coordinator_verbs_and_writes_nothing(src, store, qdir, clock,
                                                                                   verb) -> None:
    """The coordinator verbs register only over a write scope: a read-only serve answers the shared token with a
    refusal, whatever it is asked, and the store a Start or Hold would change stays as it was. (A read-only
    source has no ledger, so no paired token can exist on it.)"""
    ro = SubstrateSource(anneal=src.anneal, write_scope=None, context_json=src.context_json)
    v = _verbs(qdir, clock, launcher=_held_back)
    if verb == STOP_VERB:
        _fire(v, HOLD_VERB)
    with mock.patch.object(cs, "status_verbs", lambda d, **k: v.specs()):
        with _serve(ro, coordinator_verbs=True, install_class=None) as (port, httpd):
            assert not set(COORD_VERBS) & set(httpd.extra_verbs)
            before = _snapshot(qdir)
            st, out, _ = _fire_http(port, verb, T, "k1")
            assert (st, out.get("error")) == (422, "read_only"), (st, out)
            assert _snapshot(qdir) == before


# --- (3) a revoked device ---------------------------------------------------------------------------------------------
def test_a_revoked_devices_token_is_refused_by_every_verb_and_the_store_is_unchanged(src, store, qdir, fake,
                                                                                     live_hook, clock) -> None:
    v = _verbs(qdir, clock)
    with _serving(src, v.specs()) as (port, _h):
        _discover(port)
        device, tok = _pair(port)
        st, out, _ = _fire_http(port, START_VERB, tok, "s1")        # alive before the revoke: a running coordinator
        v.wait_starts(30)
        assert (st, out.get("ok")) == (200, True), out
        lid = out["result"]["launch_id"]
        lease = store.coord_lease()
        assert store.launch(lid).state == "active" and lease is not None
        st, out, _ = _call(port, "POST", "/cockpit/pair/revoke", {"id": device}, token=T)
        assert st == 200 and device in out["revoked"], out
        before = _snapshot(qdir)
        for verb in (STOP_VERB, HOLD_VERB, START_VERB):
            st, out, _ = _fire_http(port, verb, tok, f"after-{verb}")
            assert st == 403, (verb, st, out)
            assert out.get("ok") is not True, out
        assert _snapshot(qdir) == before
        assert store.launch(lid).state == "active" and store.coord_lease().token == lease.token
        # the control: the shared token still stops it, so the paired refusal above was the revoke
        st, out, _ = _fire_http(port, STOP_VERB, T, "ctl")
        assert (st, out.get("ok")) == (200, True), out
        assert store.launch(lid).state == "ended" and store.coord_lease() is None
