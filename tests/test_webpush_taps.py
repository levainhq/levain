"""The TAP record (row 268, 1106-53): the worker's `clicked` / `opened` reports (the receipt route, hash-authenticated), the
page's `landed` report (/webpush/landed, token-gated), and what `levain ping receipts` says about them."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from levain.webpush import receipts, routes
from levain.webpush.store import MAX_LANDINGS_PER_DEVICE_HOUR, PAIRED_PREFIX, PushStore
from tests._push_fixtures import owned_devices, phone
from tests.test_cockpit_webpush import rig  # noqa: F401 (a fixture)

EP = "https://fcm.googleapis.com/fcm/send/phone"
H = hashlib.sha256(EP.encode()).hexdigest()
GOOD = "/?view=needs&item=np-0b62cd3b"


def _ms() -> int:
    return int(time.time() * 1000)


@pytest.fixture(autouse=True)
def _own(monkeypatch) -> None:
    owned_devices(monkeypatch)


@pytest.fixture
def store(tmp_path: Path) -> PushStore:
    phone(tmp_path, EP)
    return PushStore(tmp_path)


def _ping(store: PushStore, ref: str = "np-1") -> int:
    ping = store.add_ping("sign", ref, "t", "b")
    with store.read() as c:
        sid = int(c.execute("SELECT id FROM subscriptions WHERE endpoint = ?", (EP,)).fetchone()[0])
    store.add_targets(ping, [sid])
    return ping


def _taps(store: PushStore) -> list[tuple]:
    with store.read() as c:
        return [tuple(r) for r in c.execute("SELECT ping_id, device_id, kind, target, how FROM taps ORDER BY id")]


def _rbody(ping: int, kind: str, **extra: Any) -> dict[str, Any]:
    return {"ping": ping, "kind": kind, "at": _ms(), "endpoint_sha256": H, **extra}


# --- the store: clicked / opened ---------------------------------------------------------------------------------------

def test_a_click_and_an_open_are_appended_to_the_tap_record_not_the_receipts(store: PushStore) -> None:
    ping = _ping(store)
    assert store.add_receipt(H, ping, "clicked", _ms(), GOOD) == "recorded"
    assert store.add_receipt(H, ping, "opened", _ms(), GOOD, "openWindow") == "recorded"
    assert _taps(store) == [(ping, PAIRED_PREFIX + "phone", "clicked", GOOD, None),
                            (ping, PAIRED_PREFIX + "phone", "opened", GOOD, "openWindow")]
    with store.read() as c:
        assert c.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_the_base_four_key_receipts_and_a_target_less_click_are_still_valid(store: PushStore) -> None:
    ping = _ping(store)
    assert store.add_receipt(H, ping, "received", _ms()) == "recorded"
    assert store.add_receipt(H, ping, "shown", _ms()) == "recorded"
    assert store.add_receipt(H, ping, "clicked", _ms()) == "recorded"
    assert _taps(store) == [(ping, PAIRED_PREFIX + "phone", "clicked", None, None)]


def test_every_allowed_how_is_accepted(store: PushStore) -> None:
    ping = _ping(store)
    base = _ms()
    for i, how in enumerate(("ack", "late-ack", "navigate", "openWindow", "refused", "none")):
        assert store.add_receipt(H, ping, "opened", base + i, None, how) == "recorded", how


@pytest.mark.parametrize("kind,target,how", [
    ("clicked", "/?view=needs&token=secret", None),          # a query key outside view and item
    ("clicked", "/?view=needs#frag", None),                  # a fragment
    ("clicked", "https://evil.example/x", None),             # not a path
    ("clicked", "//evil.example/x", None),
    ("clicked", "/" + "a" * 61, None),                       # a path past 60
    ("clicked", "/?item=" + "a" * 65, None),                 # an item past 64
    ("clicked", "/" + "a" * 200, None),                      # oversize
    ("clicked", 7, None),
    ("opened", GOOD, "bogus"),                               # how outside the allowlist
    ("opened", GOOD, None),                                  # an open names its how
    ("clicked", GOOD, "ack"),                                # a click names no how
    ("received", GOOD, None),                                # target belongs to the tap kinds only
    ("shown", None, "ack"),
])
def test_a_tap_report_outside_the_allowlist_is_refused_and_writes_nothing(store: PushStore, kind, target, how) -> None:
    ping = _ping(store)
    assert store.add_receipt(H, ping, kind, _ms(), target, how) == "bad_report"
    assert _taps(store) == []
    with store.read() as c:
        assert c.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_the_tap_record_is_append_only(store: PushStore) -> None:
    ping = _ping(store)
    store.add_receipt(H, ping, "clicked", _ms(), GOOD)
    import sqlite3

    with pytest.raises(sqlite3.DatabaseError):
        with store.write() as c:
            c.execute("UPDATE taps SET target = '/'")
    with pytest.raises(sqlite3.DatabaseError):
        with store.write() as c:
            c.execute("DELETE FROM taps")


# --- the receipt route -------------------------------------------------------------------------------------------------

def test_the_receipt_route_takes_target_and_how_and_still_refuses_any_other_key(store: PushStore, tmp_path: Path) -> None:
    ping = _ping(store)
    ok = routes.receipt(json.dumps(_rbody(ping, "clicked", target=GOOD)).encode(), queue_dir=tmp_path)
    assert ok[0] == 200 and json.loads(ok[1]) == {"recorded": True}
    ok = routes.receipt(json.dumps(_rbody(ping, "opened", target=GOOD, how="navigate")).encode(), queue_dir=tmp_path)
    assert ok[0] == 200
    for extra in ({"title": "x"}, {"target": GOOD, "body": "x"}, {"how": "ack", "nonce": 1}):
        got = routes.receipt(json.dumps(_rbody(ping, "opened", **extra)).encode(), queue_dir=tmp_path)
        assert got[0] == 400 and json.loads(got[1])["error"] == "bad_report", extra
    got = routes.receipt(json.dumps(_rbody(ping, "opened", target="/?token=x", how="ack")).encode(), queue_dir=tmp_path)
    assert got[0] == 400
    assert len(_taps(store)) == 2


# --- /webpush/landed ---------------------------------------------------------------------------------------------------

def _landed(tmp_path: Path, body: Any, device: str | None, authorized: bool = True) -> tuple[int, dict]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    got = routes.handle("POST", "/webpush/landed", {}, raw, queue_dir=tmp_path, authorized=authorized, device_id=device)
    return got[0], json.loads(got[1])


def test_landed_needs_the_token(store: PushStore, tmp_path: Path) -> None:
    st, out = _landed(tmp_path, {"target": "/", "how": "load"}, PAIRED_PREFIX + "phone", authorized=False)
    assert st == 403 and out["error"] == "credential_required"
    assert _taps(store) == []


def test_landed_records_under_the_credentials_own_device(store: PushStore, tmp_path: Path) -> None:
    dev = PAIRED_PREFIX + "phone"
    for how in ("load", "load>shown", "resume>shown", "resume", "message", "load>new"):
        st, out = _landed(tmp_path, {"target": GOOD, "how": how}, dev)
        assert (st, out) == (200, {"recorded": True}), how
    assert [t[2:] for t in _taps(store)][0] == ("landed", GOOD, "load")
    assert {t[1] for t in _taps(store)} == {dev} and {t[0] for t in _taps(store)} == {None}


@pytest.mark.parametrize("body", [
    {"target": "/?view=needs&token=x", "how": "load"}, {"target": "https://x.example/", "how": "load"},
    {"target": "/", "how": "LOAD"}, {"target": "/", "how": "load>a>b"}, {"target": "/", "how": "x" * 40},
    {"target": "/" + "a" * 200, "how": "load"}, {"target": 1, "how": "load"}, {"target": "/", "how": None},
])
def test_landed_refuses_a_target_or_how_outside_the_allowlist(store: PushStore, tmp_path: Path, body) -> None:
    st, out = _landed(tmp_path, body, PAIRED_PREFIX + "phone")
    assert st == 400 and out["error"] == "bad_report" and _taps(store) == []


def test_landed_refuses_extra_or_missing_keys_and_a_missing_device(store: PushStore, tmp_path: Path) -> None:
    dev = PAIRED_PREFIX + "phone"
    for body in ({"target": "/"}, {"how": "load"}, {"target": "/", "how": "load", "ping": 3}, [1], b"nope"):
        assert _landed(tmp_path, body, dev)[0] == 400, body
    assert _landed(tmp_path, {"target": "/", "how": "load"}, None)[0] == 400
    assert _taps(store) == []


def test_landed_is_limited_to_120_an_hour_per_device_with_a_429(store: PushStore, tmp_path: Path) -> None:
    assert MAX_LANDINGS_PER_DEVICE_HOUR == 120
    dev = PAIRED_PREFIX + "phone"
    codes = [_landed(tmp_path, {"target": "/", "how": "load"}, dev)[0] for _ in range(122)]
    assert codes == [200] * 120 + [429] * 2
    # another device has its own hour
    other = PAIRED_PREFIX + "tablet"
    assert _landed(tmp_path, {"target": "/", "how": "load"}, other)[0] == 200
    assert len(_taps(store)) == 121


# --- the report --------------------------------------------------------------------------------------------------------

def test_the_report_carries_the_click_the_open_and_the_landings(store: PushStore, tmp_path: Path) -> None:
    ping = _ping(store)
    store.add_receipt(H, ping, "clicked", _ms(), GOOD)
    store.add_receipt(H, ping, "opened", _ms(), GOOD, "ack")
    store.add_receipt(H, ping, "opened", _ms() + 5, GOOD, "navigate")           # the newest open wins
    store.add_landing(PAIRED_PREFIX + "phone", GOOD, "load>shown")
    rep = receipts.build(store)
    (d,) = [d for p in rep["pings"] for d in p["devices"]]
    assert d["clicked_at"] and d["click_target"] == GOOD and d["opened_at"] and d["opened_how"] == "navigate"
    assert d["outcome"] == "shown"                                              # a click proves the notification was shown
    assert rep["devices"][0]["clicked"] == 1
    assert [(g["device"], g["target"], g["how"]) for g in rep["landings"]] == [(PAIRED_PREFIX + "phone", GOOD, "load>shown")]
    txt = receipts.text(rep)
    assert f"click {d['clicked_at']} -> {GOOD}, opened navigate" in txt and "cockpit landings" in txt and "(load>shown)" in txt


def test_a_ping_with_no_tap_reports_nulls_and_no_landings(store: PushStore) -> None:
    ping = _ping(store)
    with store.write() as c:
        c.execute("INSERT INTO deliveries (ping_id, subscription_id, status, at) SELECT ?, id, 201, ? FROM subscriptions",
                  (ping, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    rep = receipts.build(store)
    (d,) = [d for p in rep["pings"] for d in p["devices"]]
    assert (d["clicked_at"], d["click_target"], d["opened_at"], d["opened_how"]) == (None, None, None, None)
    assert rep["landings"] == [] and "cockpit landings" not in receipts.text(rep)


def test_the_landings_list_is_the_newest_twenty(store: PushStore) -> None:
    for i in range(25):
        store.add_landing(PAIRED_PREFIX + "phone", f"/?item=n{i}", "load")
    rep = receipts.build(store)
    assert len(rep["landings"]) == receipts.LANDINGS_SHOWN == 20
    assert rep["landings"][0]["target"] == "/?item=n24"


# --- over HTTP ---------------------------------------------------------------------------------------------------------

def test_http_landed_takes_the_pairing_token_and_records_under_the_paired_device(rig) -> None:
    from tests.test_cockpit_pairing_http import PROXY, _call, _pair
    from tests.test_cockpit_webpush import _signed

    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        dev, tok = _pair(port)
        body = {"target": GOOD, "how": "resume>shown"}
        for hdrs in (None, PROXY):                                   # no token: refused on loopback and through the proxy
            assert _call(port, "POST", "/webpush/landed", body, headers=hdrs)[0] == 403, hdrs
        st, out, _ = _call(port, "POST", "/webpush/landed", body, token=tok)
        assert (st, out) == (200, {"recorded": True})
        st, out, _ = _call(port, "POST", "/webpush/landed", {**body, "target": "/?token=x"}, token=tok)
        assert st == 400
    assert _taps(PushStore(qdir)) == [(None, PAIRED_PREFIX + dev, "landed", GOOD, "resume>shown")]


# --- L3 round 1 fixes (1106-53, tapcard3): dedupe + caps on the tap kinds, \Z, stamps, outcome, `last` ---------------------

def _tap_count(store: PushStore, kind: str | None = None) -> int:
    with store.read() as c:
        if kind is None:
            return c.execute("SELECT COUNT(*) FROM taps WHERE kind IN ('clicked','opened')").fetchone()[0]
        return c.execute("SELECT COUNT(*) FROM taps WHERE kind = ?", (kind,)).fetchone()[0]


@pytest.mark.parametrize("kind,how", [("clicked", None), ("opened", "navigate")])
def test_a_retried_tap_report_is_a_duplicate_and_writes_no_second_row(store: PushStore, kind: str, how) -> None:
    ping = _ping(store)
    at = _ms()
    assert store.add_receipt(H, ping, kind, at, GOOD, how) == "recorded"
    assert store.add_receipt(H, ping, kind, at, GOOD, how) == "duplicate"
    assert _tap_count(store, kind) == 1
    # another time is another report
    assert store.add_receipt(H, ping, kind, at + 1, GOOD, how) == "recorded"
    assert _tap_count(store, kind) == 2


@pytest.mark.parametrize("kind,how", [("clicked", None), ("opened", "ack")])
def test_a_ping_and_kind_takes_at_most_eight_tap_reports(store: PushStore, kind: str, how) -> None:
    from levain.webpush.store import MAX_RECEIPTS_PER_PING_KIND

    ping = _ping(store)
    base = _ms()
    got = [store.add_receipt(H, ping, kind, base + i, GOOD, how) for i in range(MAX_RECEIPTS_PER_PING_KIND + 2)]
    assert got == ["recorded"] * MAX_RECEIPTS_PER_PING_KIND + ["limited"] * 2
    assert _tap_count(store, kind) == MAX_RECEIPTS_PER_PING_KIND


def test_a_device_may_report_only_so_many_taps_an_hour(store: PushStore, monkeypatch) -> None:
    from levain.webpush import store as store_mod

    monkeypatch.setattr(store_mod, "MAX_RECEIPTS_PER_DEVICE_HOUR", 5)
    ping = _ping(store)
    base = _ms()
    got = [store.add_receipt(H, ping, "clicked", base + i, GOOD) for i in range(3)]
    got += [store.add_receipt(H, ping, "opened", base + i, GOOD, "ack") for i in range(4)]
    assert got == ["recorded"] * 5 + ["limited"] * 2
    # the receipts have their own count: a tap flood does not starve them
    assert store.add_receipt(H, ping, "received", base) == "recorded"


def test_receipts_do_not_spend_the_tap_allowance(store: PushStore, monkeypatch) -> None:
    from levain.webpush import store as store_mod

    monkeypatch.setattr(store_mod, "MAX_RECEIPTS_PER_DEVICE_HOUR", 3)
    ping = _ping(store)
    base = _ms()
    assert [store.add_receipt(H, ping, "received", base + i) for i in range(3)] == ["recorded"] * 3
    assert store.add_receipt(H, ping, "received", base + 9) == "limited"
    assert store.add_receipt(H, ping, "clicked", base, GOOD) == "recorded"


@pytest.mark.parametrize("target", ["/x\n", "/?view=needs\n", "/?item=a\n", "/\n"])
def test_a_trailing_newline_is_not_a_valid_target(store: PushStore, tmp_path: Path, target: str) -> None:
    ping = _ping(store)
    assert store.add_receipt(H, ping, "clicked", _ms(), target) == "bad_report"
    assert store.add_landing(PAIRED_PREFIX + "phone", target, "load") == "bad_report"
    assert _taps(store) == []


@pytest.mark.parametrize("how", ["load\n", "load>shown\n", "load\nrogue"])
def test_a_trailing_newline_is_not_a_valid_landing_how(store: PushStore, how: str) -> None:
    assert store.add_landing(PAIRED_PREFIX + "phone", "/", how) == "bad_report"
    assert _taps(store) == []


_STAMP = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$"


def test_clicked_and_opened_are_stamps_in_the_report_and_the_text(store: PushStore) -> None:
    import re

    ping = _ping(store)
    store.add_receipt(H, ping, "received", _ms())
    store.add_receipt(H, ping, "clicked", _ms(), GOOD)
    store.add_receipt(H, ping, "opened", _ms(), GOOD, "ack")
    (d,) = [d for p in receipts.build(store)["pings"] for d in p["devices"]]
    for k in ("received_at", "clicked_at", "opened_at"):
        assert re.match(_STAMP, d[k]), (k, d[k])
    assert d["clicked_at"] in receipts.text(receipts.build(store))


def _delivered(store: PushStore, ping: int, at: str) -> None:
    with store.write() as c:
        c.execute("INSERT INTO deliveries (ping_id, subscription_id, status, at) SELECT ?, id, 201, ? FROM subscriptions",
                  (ping, at))


def test_an_open_whose_click_was_lost_still_counts_as_shown(store: PushStore) -> None:
    ping = _ping(store)
    store.add_receipt(H, ping, "opened", _ms(), GOOD, "navigate")          # the `clicked` POST never arrived
    (d,) = [d for p in receipts.build(store)["pings"] for d in p["devices"]]
    assert d["clicked_at"] is None and d["opened_at"] and d["outcome"] == "shown"


def test_a_device_that_reported_only_tap_rows_is_silent_not_no_receipt_yet(store: PushStore) -> None:
    old = _ping(store, "np-old")
    _delivered(store, old, "2020-01-01T00:00:00Z")                         # delivered long ago, never reported on
    new = _ping(store, "np-new")
    store.add_receipt(H, new, "clicked", _ms(), GOOD)                      # the phone's only report is a tap row
    rep = receipts.build(store)
    outcome = {p["ping"]: p["devices"][0]["outcome"] for p in rep["pings"]}
    assert outcome[old] == "silent", outcome
    assert rep["devices"][0]["silent"] == 1


def test_landings_do_not_spend_the_tap_report_allowance(store: PushStore, monkeypatch) -> None:
    from levain.webpush import store as store_mod

    monkeypatch.setattr(store_mod, "MAX_RECEIPTS_PER_DEVICE_HOUR", 3)
    for _ in range(6):
        assert store.add_landing(PAIRED_PREFIX + "phone", "/", "load") == "recorded"
    ping = _ping(store)
    assert store.add_receipt(H, ping, "clicked", _ms(), GOOD) == "recorded"
