"""CH-D2 S8 (second slice): 19a's renew-due trigger at launch (`levain.chat_egress.renew`, `start_egress`).

The expiry is read from the AUTHORITY record (the replay's `expires_at`), never from the route snapshot; a record due
within `RENEW_DUE` reaches `on_renew_due` once, with the launch id; a failing callback never ends the launch.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from levain.chat_egress import keys, renew
from levain.chat_egress import launch as lm
from levain.cockpit.consent import utc_stamp

from .test_launch import Origin, _node_children, _start

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def _route(rid: str, host: str) -> keys.RouteSnapshot:
    return keys.RouteSnapshot(record_id=rid, canonical_host=host, credential_source="svc", declared_repos=frozenset(),
                              attach=True, credential_account="acct", incarnation="1:x")


def _authority(**expiries: timedelta) -> SimpleNamespace:
    return SimpleNamespace(policy=SimpleNamespace(grants={rid: {"expires_at": utc_stamp(NOW + d)}
                                                          for rid, d in expiries.items()}))


ROUTES = keys.RouteSet.of([_route("k-soon", "a.example"), _route("k-later", "b.example"),
                           _route("k-edge", "c.example"), _route("k-gone", "d.example")])


def test_renew_due_reads_the_authority_records_expiry() -> None:
    auth = _authority(**{"k-soon": timedelta(days=1), "k-later": timedelta(days=20),
                         "k-edge": renew.RENEW_DUE})                     # k-gone: no longer in the authority
    due = renew.renew_due(auth, ROUTES, now=NOW)
    assert [(d.record_id, d.host) for d in due] == [("k-soon", "a.example"), ("k-edge", "c.example")]
    assert due[0].expires_at == NOW + timedelta(days=1)
    # the same routes, the authority's expiry moved later (a renew): nothing is due; the snapshot was not consulted
    assert renew.renew_due(_authority(**{"k-soon": timedelta(days=30)}), ROUTES, now=NOW) == []


@pytest.fixture
def origin():
    o = Origin()
    yield o
    o.close()


@pytest.mark.chat_egress_srt
def test_a_launch_pings_once_for_a_key_due_and_a_failing_ping_does_not_end_it(tmp_path, origin, monkeypatch) -> None:
    calls = []
    real = lm.start_egress

    def start(**kw):
        return real(**{**kw, "routes": ROUTES, "authority": _authority(**{"k-soon": timedelta(days=2),
                                                                          "k-later": timedelta(days=29)}),
                       "on_renew_due": lambda lid, due: calls.append((lid, [d.record_id for d in due])), "now": NOW})
    monkeypatch.setattr(lm, "start_egress", start)
    (tmp_path / "a").mkdir()
    e = _start(tmp_path / "a", origin, monkeypatch)
    lm.stop_egress(e)
    print(f"RUN S8B 19a: on_renew_due calls {calls}; launch.renew_due {e.renew_due}")
    assert calls == [(e.launch_id, ["k-soon"])] and [d.record_id for d in e.renew_due] == ["k-soon"]

    def boom(lid, due):
        raise RuntimeError("push down")

    def start2(**kw):
        return real(**{**kw, "routes": ROUTES, "authority": _authority(**{"k-soon": timedelta(days=2)}),
                       "on_renew_due": boom, "now": NOW})
    monkeypatch.setattr(lm, "start_egress", start2)
    (tmp_path / "b").mkdir()
    e2 = _start(tmp_path / "b", origin, monkeypatch)
    out = lm.stop_egress(e2)
    assert out.glue_exit == 0


# --- S8b r1 item 3: renewal data validated before any allocation; the ping off the start path ----------------------

def _fds() -> set[int]:
    return {int(x) for x in os.listdir("/dev/fd")} - {max(int(x) for x in os.listdir("/dev/fd"))}


@pytest.mark.chat_egress_srt
@pytest.mark.parametrize("bad", ["naive-now", "no-policy"])
def test_bad_renewal_data_is_refused_before_anything_is_allocated(bad, tmp_path, origin, monkeypatch) -> None:
    real = lm.start_egress
    auth = _authority(**{"k-soon": timedelta(days=2)}) if bad == "naive-now" else SimpleNamespace()
    when = NOW.replace(tzinfo=None) if bad == "naive-now" else NOW

    def start(**kw):
        return real(**{**kw, "routes": ROUTES, "authority": auth, "on_renew_due": lambda lid, due: None, "now": when})
    monkeypatch.setattr(lm, "start_egress", start)
    before, kids = _fds(), _node_children()
    with pytest.raises(lm.EgressRefused, match="renew") as got:
        lm.stop_egress(_start(tmp_path, origin, monkeypatch))
    print(f"RUN S8D item 3 {bad}: {got.value}")
    assert _fds() == before and len(_node_children()) == len(kids)
    assert not (tmp_path / "events.jsonl.glue-stderr").exists()        # refused before the glue's files exist


@pytest.mark.chat_egress_srt
def test_a_blocking_renew_ping_does_not_hold_the_launch(tmp_path, origin, monkeypatch) -> None:
    real = lm.start_egress
    release, seen = threading.Event(), []

    def slow(lid, due):
        seen.append(lid)
        release.wait(20.0)
        raise RuntimeError("push timed out")

    def start(**kw):
        return real(**{**kw, "routes": ROUTES, "authority": _authority(**{"k-soon": timedelta(days=2)}),
                       "on_renew_due": slow, "now": NOW})
    monkeypatch.setattr(lm, "start_egress", start)
    t0 = time.monotonic()
    e = _start(tmp_path, origin, monkeypatch)
    took = time.monotonic() - t0
    try:
        print(f"RUN S8D item 3 blocking ping: start_egress returned after {took:.2f}s; ping called {len(seen)}x")
        assert took < lm.RENEW_PING_WAIT_S + 5.0 and seen == [e.launch_id]
        assert e.renew_ping is not None and e.renew_ping.is_alive()
    finally:
        release.set()
        out = lm.stop_egress(e)
    e.renew_ping.join(5.0)
    assert not e.renew_ping.is_alive() and out.glue_exit == 0


# --- S8D r1 fix item 1: a ping thread that cannot start never orphans the launch -----------------------------------

class _NoStartThread(threading.Thread):
    def start(self) -> None:
        raise RuntimeError("can't start new thread")


@pytest.mark.chat_egress_srt
def test_a_ping_thread_that_cannot_start_still_returns_the_launch(tmp_path, origin, monkeypatch) -> None:
    real = lm.start_egress
    seen = []

    def start(**kw):
        return real(**{**kw, "routes": ROUTES, "authority": _authority(**{"k-soon": timedelta(days=2)}),
                       "on_renew_due": lambda lid, due: seen.append(lid), "now": NOW})
    monkeypatch.setattr(lm, "start_egress", start)
    monkeypatch.setattr(lm, "threading", SimpleNamespace(Thread=_NoStartThread))
    kids = _node_children()
    e = _start(tmp_path, origin, monkeypatch)
    try:
        print(f"RUN S8D fix 1: launch returned with renew_ping={e.renew_ping}; glue alive={e.proc.poll() is None}")
        assert e.proc.poll() is None and e.renew_ping is None and seen == []
        assert [d.record_id for d in e.renew_due] == ["k-soon"]
    finally:
        out = lm.stop_egress(e)
    assert out.glue_exit == 0 and len(_node_children()) == len(kids)
