"""Device receipts: a phone reports the pushes it got (``received`` when the worker's push event fired, ``shown`` when
showNotification resolved) to ``/webpush/receipt``, authenticated by the SHA-256 of its own subscription endpoint; the
store keeps them append-only; ``levain ping receipts`` joins them with the push service's answer and the tap."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.webpush import receipts, routes
from levain.webpush.store import (MAX_RECEIPTS_PER_DEVICE_HOUR, MAX_RECEIPTS_PER_PING_KIND, PAIRED_PREFIX, PushStore,
                                  now_stamp)
from tests._push_fixtures import owned_devices, pair, phone, revoke
from tests.test_cockpit_pairing_http import PROXY, T, _call, _pair
from tests.test_cockpit_webpush import _signed, _sub, rig  # noqa: F401 (rig is a fixture)

HERE = Path(__file__).resolve().parent
SW = HERE.parent / "levain" / "webpush" / "static" / "sw.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
EP = "https://fcm.googleapis.com/fcm/send/phone"
H = hashlib.sha256(EP.encode()).hexdigest()


def _ms() -> int:
    return int(time.time() * 1000)


@pytest.fixture(autouse=True)
def _own(monkeypatch) -> None:
    owned_devices(monkeypatch)


@pytest.fixture
def store(tmp_path: Path) -> PushStore:
    phone(tmp_path, EP)
    return PushStore(tmp_path)


# --- the store -------------------------------------------------------------------------------------------------------

def test_a_receipt_is_appended_under_the_device_whose_endpoint_hashes_to_it(store: PushStore) -> None:
    ping = store.add_ping("sign", "np-1", "t", "b")
    assert store.add_receipt(H, ping, "received", _ms()) == "recorded"
    with store.read() as c:
        (row,) = c.execute("SELECT ping_id, device_id, kind, at, reported_at FROM receipts").fetchall()
    assert (row[0], row[1], row[2]) == (ping, PAIRED_PREFIX + "phone", "received")
    assert row[3].endswith("Z") and "." in row[3] and row[4].endswith("Z") and "." not in row[4]


def test_a_retried_report_is_idempotent_and_a_second_delivery_is_a_second_row(store: PushStore) -> None:
    ping = store.add_ping("sign", None, "t", "b")
    at = _ms()
    assert store.add_receipt(H, ping, "received", at) == "recorded"
    assert store.add_receipt(H, ping, "received", at) == "duplicate"
    assert store.add_receipt(H, ping, "received", at + 5000) == "recorded"        # the repeat-notification signal
    with store.read() as c:
        assert c.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2


def test_receipts_never_change_and_are_never_deleted(store: PushStore) -> None:
    ping = store.add_ping("sign", None, "t", "b")
    store.add_receipt(H, ping, "shown", _ms())
    with store.write() as c:
        with pytest.raises(sqlite3.DatabaseError, match="never change"):
            c.execute("UPDATE receipts SET kind = 'received'")
        with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
            c.execute("DELETE FROM receipts")


def test_the_table_is_added_to_a_store_that_predates_it(tmp_path: Path) -> None:
    phone(tmp_path, EP)
    s = PushStore(tmp_path)
    with s.write() as c:
        c.execute("DROP TRIGGER receipts_frozen")
        c.execute("DROP TRIGGER receipts_kept")
        c.execute("DROP TABLE receipts")
    ping = s.add_ping("sign", None, "t", "b")
    assert PushStore(tmp_path).add_receipt(H, ping, "received", _ms()) == "recorded"   # a fresh opener ensures the schema


def test_unknown_device_ping_kind_and_time_are_refused_with_nothing_written(store: PushStore) -> None:
    ping = store.add_ping("sign", None, "t", "b")
    assert store.add_receipt("0" * 64, ping, "received", _ms()) == "unknown_device"
    assert store.add_receipt(H, ping + 99, "received", _ms()) == "unknown_ping"
    assert store.add_receipt(H, ping, "tapped", _ms()) == "bad_report"
    assert store.add_receipt(H, ping, "received", 5) == "bad_report"
    assert store.add_receipt(H, ping, "received", _ms() + 10 * 86_400_000) == "bad_report"
    assert store.add_receipt(H, ping, "received", True) == "bad_report"
    assert store.add_receipt(H, ping, "received", "1") == "bad_report"
    with store.read() as c:
        assert c.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_a_retired_or_revoked_subscription_reports_nothing(store: PushStore, tmp_path: Path) -> None:
    ping = store.add_ping("sign", None, "t", "b")
    revoke(store, "phone")
    assert store.add_receipt(H, ping, "received", _ms()) == "unknown_device"
    other = tmp_path / "q2"
    phone(other, EP)
    s2 = PushStore(other)
    p2 = s2.add_ping("sign", None, "t", "b")
    s2.retire_subscription(EP)
    assert s2.add_receipt(H, p2, "received", _ms()) == "unknown_device"


def test_a_device_cannot_report_past_its_caps(store: PushStore) -> None:
    ping = store.add_ping("sign", None, "t", "b")
    base = _ms()
    for i in range(MAX_RECEIPTS_PER_PING_KIND):
        assert store.add_receipt(H, ping, "received", base + i) == "recorded"
    assert store.add_receipt(H, ping, "received", base + 999) == "limited"          # one ping and kind
    assert store.add_receipt(H, ping, "shown", base) == "recorded"                  # another kind is its own count
    pings = [store.add_ping("sign", None, "t", "b") for _ in range(MAX_RECEIPTS_PER_DEVICE_HOUR)]
    done = 0
    with store.write() as c:                                                        # fill the hour directly
        for p in pings:
            c.execute("INSERT INTO receipts (ping_id, device_id, kind, at, reported_at) VALUES (?,?,?,?,?)",
                      (p, PAIRED_PREFIX + "phone", "received", "2026-10-31T00:00:00.000Z", now_stamp()))
            done += 1
    assert done == MAX_RECEIPTS_PER_DEVICE_HOUR
    fresh = store.add_ping("sign", None, "t", "b")
    assert store.add_receipt(H, fresh, "received", _ms()) == "limited"              # the device's hour


# --- the route -------------------------------------------------------------------------------------------------------

def _body(ping: int, kind: str = "received", at: int | None = None, h: str = H) -> dict[str, Any]:
    return {"ping": ping, "kind": kind, "at": at or _ms(), "endpoint_sha256": h}


def test_the_receipt_route_refuses_what_it_cannot_place_and_writes_nothing(store: PushStore, tmp_path: Path) -> None:
    ping = store.add_ping("sign", None, "t", "b")
    ok = routes.receipt(json.dumps(_body(ping)).encode(), queue_dir=tmp_path)
    assert ok[0] == 200 and json.loads(ok[1]) == {"recorded": True}
    cases = [
        (json.dumps(_body(ping, h="0" * 64)).encode(), 403, "unknown_device"),
        (json.dumps(_body(ping + 50)).encode(), 404, "unknown_ping"),
        (json.dumps({**_body(ping), "ping": "7"}).encode(), 400, "bad_report"),          # a non-integer ping
        (json.dumps({**_body(ping), "ping": True}).encode(), 400, "bad_report"),
        (json.dumps({**_body(ping), "endpoint_sha256": H.upper()}).encode(), 400, "bad_report"),
        (json.dumps({**_body(ping), "endpoint_sha256": "abc"}).encode(), 400, "bad_report"),
        (json.dumps({**_body(ping), "title": "x"}).encode(), 400, "bad_report"),         # no extra field
        (json.dumps({"ping": ping}).encode(), 400, "bad_report"),
        (json.dumps(_body(ping, kind="tapped")).encode(), 400, "bad_report"),
        (b"[1]", 400, "bad_report"), (b"not json", 400, "bad_report"), (b"\xff", 400, "bad_report"),
        (json.dumps({**_body(ping), "pad": "x" * 600}).encode(), 413, "too_large"),
    ]
    for raw, status, code in cases:
        got = routes.receipt(raw, queue_dir=tmp_path)
        assert (got[0], json.loads(got[1])["error"]) == (status, code), (raw[:60], got)
    with store.read() as c:
        assert c.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_the_flood_answers_429_and_stops_growing_the_table(store: PushStore, tmp_path: Path) -> None:
    ping = store.add_ping("sign", None, "t", "b")
    base = _ms()
    codes = [routes.receipt(json.dumps(_body(ping, at=base + i)).encode(), queue_dir=tmp_path)[0]
             for i in range(MAX_RECEIPTS_PER_PING_KIND + 4)]
    assert codes == [200] * MAX_RECEIPTS_PER_PING_KIND + [429] * 4
    with store.read() as c:
        assert c.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == MAX_RECEIPTS_PER_PING_KIND


def test_http_the_receipt_takes_no_token_and_every_other_webpush_post_still_needs_one(rig) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        dev, tok = _pair(port)
        assert _call(port, "POST", "/webpush/subscribe", _sub(7), token=tok)[0] == 200
        ep = _sub(7)["endpoint"]
        h = hashlib.sha256(ep.encode()).hexdigest()
        ping = PushStore(qdir).add_ping("sign", "np-7", "t", "b")
        for hdrs in (None, PROXY):                                   # no token, on loopback and through the proxy
            st, out, hd = _call(port, "POST", "/webpush/receipt", _body(ping, at=_ms() + (0 if hdrs is None else 1), h=h),
                                headers=hdrs)
            assert st == 200 and out == {"recorded": True} and hd["cache-control"] == "no-store", (hdrs, st, out)
        with PushStore(qdir).read() as c:
            rows = c.execute("SELECT device_id, kind FROM receipts").fetchall()
        assert [tuple(r) for r in rows] == [(PAIRED_PREFIX + dev, "received")] * 2
        # the neighbours are unchanged: a data route with no credential is refused whatever the body
        for path, body in (("/webpush/opened", {"ping": ping}), ("/webpush/subscribe", _sub(8)),
                           ("/webpush/unsubscribe", {"endpoint": ep}), ("/webpush/test", {}),
                           ("/webpush/policy/reset", {})):
            for hdrs in (None, PROXY):
                assert _call(port, "POST", path, body, headers=hdrs)[0] == 403, (path, hdrs)
        # a receipt is not a way to read: a GET of the path is not served
        assert _call(port, "GET", "/webpush/receipt", headers=None)[0] in (403, 404, 405)
        assert PushStore(qdir).live_subscriptions()                  # and nothing it did touched the subscription


def test_http_the_receipt_keeps_the_origin_json_and_size_checks(rig) -> None:
    import http.client

    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        _, tok = _pair(port)
        _call(port, "POST", "/webpush/subscribe", _sub(7), token=tok)
        h = hashlib.sha256(_sub(7)["endpoint"].encode()).hexdigest()
        ping = PushStore(qdir).add_ping("sign", None, "t", "b")
        good = json.dumps(_body(ping, h=h)).encode()

        def post(headers: dict[str, str], body: bytes = good) -> int:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            c.request("POST", "/webpush/receipt", body=body, headers=headers)
            r = c.getresponse()
            r.read()
            c.close()
            return r.status

        assert post({"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}) == 403   # CSRF layer 1
        assert post({"Content-Type": "text/plain"}) == 415                                           # CSRF layer 2
        assert post({"Content-Type": "application/json", "Host": "evil.example"}) == 403             # DNS rebinding
        assert post({"Content-Type": "application/json"}, b"x" * 2000) == 413                         # the size cap
        assert post({"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}) == 200     # a worker's own fetch


def test_http_a_cockpit_with_no_push_store_does_not_serve_the_receipt(rig) -> None:
    from tests.test_cockpit_pairing_http import _serve

    src, _ = rig
    with _serve(src) as (port, _):
        assert _call(port, "POST", "/webpush/receipt", {"ping": 1, "kind": "received", "at": _ms(),
                                                        "endpoint_sha256": H})[0] in (403, 404)


# --- the worker ------------------------------------------------------------------------------------------------------

def _run(mode: str) -> dict:
    cp = subprocess.run([NODE, str(HERE / "_sw_worker_rig.js"), str(SW), "push", mode], capture_output=True, text=True,
                        timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return json.loads(cp.stdout)


def _receipt_steps(got: dict) -> list[tuple]:
    return [(e["kind"], e.get("result"), e.get("status")) for e in got["trace"] if e["step"] == "receipt"]


@needs_node
def test_a_push_posts_received_then_shown_with_the_endpoint_hash_and_ids_only() -> None:
    got = _run("ok")
    r, s = got["posts"]
    assert (r["body"]["kind"], s["body"]["kind"]) == ("received", "shown")
    assert r["url"] == "/webpush/receipt" and r["method"] == "POST" and r["contentType"] == "application/json"
    expect = hashlib.sha256(b"https://fcm.googleapis.com/fcm/send/dev1").hexdigest()
    for p in (r, s):
        assert set(p["body"]) == {"ping", "kind", "at", "endpoint_sha256"} and p["body"]["ping"] == 42, p
        assert p["body"]["endpoint_sha256"] == expect and isinstance(p["body"]["at"], int) and p["hasSignal"], p
    assert r["body"]["at"] <= s["body"]["at"]
    assert "secret body" not in json.dumps(got["posts"])
    assert got["shownCalls"] == [{"title": "T", "tag": "x"}] and got["queued"] == []
    assert _receipt_steps(got) == [("received", "ok", None), ("shown", "ok", None)]
    assert any(e["step"] == "sw-version" and e["version"] == "1101-receipts-1" and e["on"] == "push" for e in got["trace"])


@needs_node
def test_the_notification_is_asked_for_before_any_receipt_and_still_shown_when_fetch_throws() -> None:
    got = _run("throw")
    assert got["order"][0] == "show", got["order"]
    assert got["shownCalls"] == [{"title": "T", "tag": "x"}]
    assert got["queued"] == ["/__levain_receipt/42-received", "/__levain_receipt/42-shown"], got["queued"]
    assert _receipt_steps(got) == [("received", "queued", None), ("shown", "queued", None)]


@needs_node
def test_a_hanging_post_is_cut_at_three_seconds_and_queued() -> None:
    got = _run("hang")
    assert 2800 <= got["elapsedMs"] < 5500, got["elapsedMs"]
    assert got["shownCalls"] and got["queued"] == ["/__levain_receipt/42-received", "/__levain_receipt/42-shown"]


@needs_node
def test_a_failed_post_is_resent_by_the_next_notification_click_oldest_first() -> None:
    got = _run("throw+then:ok+click")
    assert got["afterFirst"]["queued"] == ["/__levain_receipt/42-received", "/__levain_receipt/42-shown"]
    assert [p["body"]["kind"] for p in got["posts"]] == ["received", "shown"], got["posts"]     # sent order = oldest first
    assert got["queued"] == []
    assert ("received", "drained", 200) in _receipt_steps(got)


@needs_node
def test_a_failed_post_is_resent_by_the_next_push() -> None:
    got = _run("throw+then:ok+push")
    assert len(got["posts"]) == 4 and {(p["body"]["ping"], p["body"]["kind"]) for p in got["posts"]} == {
        (42, "received"), (42, "shown")}                                                   # this push's two, then the two queued
    assert got["queued"] == []


@needs_node
def test_a_drain_stops_at_the_first_failure_and_keeps_what_it_did_not_send() -> None:
    got = _run("throw+then:throw+click")
    assert len(got["posts"]) == 1                              # one attempt, then the drain stops
    assert got["queued"] == ["/__levain_receipt/42-received", "/__levain_receipt/42-shown"]
    assert ("received", "still-queued", None) in _receipt_steps(got)


@needs_node
def test_a_drain_sends_twenty_at_most_and_the_oldest_first() -> None:
    got = _run("ok+seed25+clickonly")
    ats = [p["body"]["at"] for p in got["posts"]]
    assert len(ats) == 20 and ats == sorted(ats), ats
    assert len(got["queued"]) == 5 and all(p["body"]["endpoint_sha256"] == "a" * 64 for p in got["posts"])
    left = sorted(1_800_000_000_000 + ((i * 7) % 25) * 1000 for i in range(25))[20:]
    assert sorted(int(k.split("/")[-1].split("-")[0]) for k in got["queued"]) == sorted(
        100 + i for i in range(25) if 1_800_000_000_000 + ((i * 7) % 25) * 1000 in left)


@needs_node
def test_no_subscription_queues_nothing_and_says_so() -> None:
    got = _run("ok+nosub")
    assert got["posts"] == [] and got["queued"] == [] and got["shownCalls"]
    assert [e["step"] for e in got["trace"] if e["step"].startswith("receipt")] == ["receipt-no-sub", "receipt-no-sub"]


@needs_node
def test_a_push_with_no_ping_id_makes_no_post() -> None:
    got = _run("ok+noping")
    assert got["posts"] == [] and got["shownCalls"] and got["queued"] == []


@needs_node
def test_a_refused_receipt_is_traced_with_its_status_and_not_queued_but_a_server_fault_is() -> None:
    got = _run("403")
    assert got["queued"] == [] and _receipt_steps(got) == [("received", "refused", 403), ("shown", "refused", 403)]
    got = _run("500")
    assert len(got["queued"]) == 2 and _receipt_steps(got)[0] == ("received", "queued", 500)
    got = _run("429")                                            # a rate limit is retried later, never settled
    assert len(got["queued"]) == 2 and _receipt_steps(got)[0] == ("received", "queued", 429)


@needs_node
def test_a_show_that_fails_is_traced_and_posts_no_shown_receipt() -> None:
    got = _run("ok+showthrows")
    assert [p["body"]["kind"] for p in got["posts"]] == ["received"]
    assert any(e["step"] == "show-failed" for e in got["trace"])


def test_activation_keeps_the_receipts_cache() -> None:
    import re

    text = SW.read_text(encoding="utf-8")
    assert re.search(r"CACHES_KEEP = \[OPEN_CACHE, TRACE_CACHE, RECEIPT_CACHE\]", text)
    assert "RECEIPT_CACHE = 'levain-receipts'" in text and "'/__levain_receipt/'" in text


# --- the report ------------------------------------------------------------------------------------------------------

def _t(minutes: int) -> str:
    return (datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(tmp_path: Path) -> PushStore:
    """Two phones. Ping 1: both accepted; A received+shown+tapped, B received twice (a duplicate) and shown. Ping 2: A
    nothing, B received and shown. Ping 3: A nothing (no later report from A: not yet); B refused by the push service.
    Ping 4 (a question ref): A received, accepted. A therefore reported later than ping 2, so ping 2 is silent for A."""
    phone(tmp_path, "https://fcm.googleapis.com/fcm/send/a", "a")
    phone(tmp_path, "https://web.push.apple.com/b", "b")
    s = PushStore(tmp_path)
    with s.write() as c:
        subs = {r["device_id"]: r["id"] for r in c.execute("SELECT id, device_id FROM subscriptions")}
    sa, sb = subs[PAIRED_PREFIX + "a"], subs[PAIRED_PREFIX + "b"]
    ids = [s.add_ping("sign", "np-1", "t", "b"), s.add_ping("sign", "np-2", "t", "b"), s.add_ping("answer", "np-3", "t", "b"),
           s.add_ping("answer", "question:4", "t", "b")]
    with s.write() as c:
        def delivery(ping: int, sub: int, status: int, at: str) -> None:
            c.execute("INSERT INTO deliveries (ping_id, subscription_id, at, status) VALUES (?,?,?,?)", (ping, sub, at, status))
        delivery(ids[0], sa, 201, _t(0)); delivery(ids[0], sb, 201, _t(0))
        delivery(ids[1], sa, 201, _t(10)); delivery(ids[1], sb, 201, _t(10))
        delivery(ids[2], sa, 201, _t(20)); delivery(ids[2], sb, 410, _t(20))
        delivery(ids[3], sa, 201, _t(30))

        def receipt(ping: int, dev: str, kind: str, at: str) -> None:
            c.execute("INSERT INTO receipts (ping_id, device_id, kind, at, reported_at) VALUES (?,?,?,?,?)",
                      (ping, PAIRED_PREFIX + dev, kind, at, at[:19] + "Z"))
        receipt(ids[0], "a", "received", _t(0)[:-1] + ".500Z"); receipt(ids[0], "a", "shown", _t(0)[:-1] + ".900Z")
        receipt(ids[0], "b", "received", _t(0)[:-1] + ".200Z"); receipt(ids[0], "b", "received", _t(0)[:-1] + ".300Z")
        receipt(ids[0], "b", "shown", _t(0)[:-1] + ".400Z")
        receipt(ids[1], "b", "received", _t(10)[:-1] + ".100Z"); receipt(ids[1], "b", "shown", _t(10)[:-1] + ".150Z")
        receipt(ids[3], "a", "received", _t(30)[:-1] + ".250Z")
        c.execute("INSERT INTO opens (ping_id, device_id, at) VALUES (?,?,?)", (ids[0], PAIRED_PREFIX + "a", _t(1)))
    s._ids = ids  # type: ignore[attr-defined]
    return s


def _by(report: dict, ping_index: int, ids: list[int], device: str) -> dict:
    (p,) = [x for x in report["pings"] if x["ping"] == ids[ping_index]]
    (d,) = [x for x in p["devices"] if x["device"] == PAIRED_PREFIX + device]
    return d


def test_the_report_names_duplicates_silent_and_not_yet_pings(tmp_path: Path) -> None:
    s = _seed(tmp_path)
    ids = s._ids  # type: ignore[attr-defined]
    rep = receipts.build(s)
    assert rep["format"] == receipts.FORMAT
    a1, b1 = _by(rep, 0, ids, "a"), _by(rep, 0, ids, "b")
    assert a1["outcome"] == "shown" and a1["tapped_at"] == _t(1) and a1["accepted"] is True and not a1["duplicate"]
    assert a1["sent_to_received_s"] is not None and a1["received_to_shown_s"] == 0.4
    assert b1["received_count"] == 2 and b1["duplicate"] is True and b1["outcome"] == "shown" and b1["tapped_at"] is None
    a2 = _by(rep, 1, ids, "a")
    assert a2["outcome"] == "silent" and a2["received_at"] is None            # A reported on a LATER ping
    a3, b3 = _by(rep, 2, ids, "a"), _by(rep, 2, ids, "b")
    assert a3["outcome"] == "silent"                                           # ping 4 came later from A
    assert b3["outcome"] == "not accepted" and b3["accepted"] is False and b3["statuses"] == [410]
    a4 = _by(rep, 3, ids, "a")
    assert a4["outcome"] == "received" and a4["shown_at"] is None
    # a ping no device has reported after is "no receipt yet", never lost
    late = PushStore(tmp_path)
    p5 = late.add_ping("sign", "np-5", "t", "b")
    with late.write() as c:
        sb = c.execute("SELECT id FROM subscriptions WHERE device_id = ?", (PAIRED_PREFIX + "b",)).fetchone()[0]
        c.execute("INSERT INTO deliveries (ping_id, subscription_id, at, status) VALUES (?,?,?,?)", (p5, sb, _t(40), 201))
    rep = receipts.build(late)
    (p,) = [x for x in rep["pings"] if x["ping"] == p5]
    assert [d["outcome"] for d in p["devices"]] == ["no receipt yet"]
    assert "lost" not in json.dumps(rep).lower()


def test_the_rollup_counts_each_phone(tmp_path: Path) -> None:
    s = _seed(tmp_path)
    rep = receipts.build(s)
    roll = {r["device"]: r for r in rep["devices"]}
    a, b = roll[PAIRED_PREFIX + "a"], roll[PAIRED_PREFIX + "b"]
    assert (a["sent"], a["accepted"], a["received"], a["shown"], a["tapped"], a["duplicates"], a["silent"]) == (4, 4, 2, 1, 1, 0, 2)
    assert (b["sent"], b["accepted"], b["received"], b["shown"], b["tapped"], b["duplicates"], b["silent"]) == (3, 2, 2, 2, 0, 1, 0)
    assert isinstance(a["median_sent_to_received_s"], float) and isinstance(b["median_sent_to_received_s"], float)


def test_ref_and_since_filter_the_report(tmp_path: Path) -> None:
    s = _seed(tmp_path)
    assert [p["ref"] for p in receipts.build(s, ref="np-2")["pings"]] == ["np-2"]
    assert receipts.build(s, ref="nothing")["pings"] == [] and receipts.build(s, ref="nothing")["devices"] == []
    assert len(receipts.build(s, since=timedelta(days=7))["pings"]) == 4            # every ping is just now
    future = datetime.now(timezone.utc) + timedelta(days=30)
    assert receipts.build(s, since=timedelta(days=7), now=future)["pings"] == []
    assert receipts.parse_since("7d") == timedelta(days=7) and receipts.parse_since("90m") == timedelta(minutes=90)
    for bad in ("", "7", "d", "0d", "-1d", "7w", "1.5h"):
        with pytest.raises(ValueError):
            receipts.parse_since(bad)


def test_the_text_report_says_no_receipt_is_not_lost(tmp_path: Path) -> None:
    out = receipts.text(receipts.build(_seed(tmp_path)))
    assert "DUPLICATE x2" in out and "silent" in out and "no receipt yet" in out and "not lost" in out


# --- the command -----------------------------------------------------------------------------------------------------

def _cli(tmp_path: Path, *args: str, capsys) -> tuple[int, str]:
    import argparse

    from levain.webpush import cli

    p = argparse.ArgumentParser()
    cli.register(p.add_subparsers(dest="cmd", required=True))
    ns = p.parse_args(["ping", "receipts", "--queue-dir", str(tmp_path), *args])
    rc = ns.func(ns)
    return rc, capsys.readouterr().out


def test_the_command_prints_the_stable_json_shape(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    rc, out = _cli(tmp_path, "--json", "--since", "7d", capsys=capsys)
    got = json.loads(out)
    assert rc == 0 and list(got) == ["format", "since", "ref", "pings", "devices"] and got["format"] == "levain-ping-receipts/1"
    assert list(got["pings"][0]) == ["ping", "at", "kind", "ref", "devices"]
    assert list(got["pings"][0]["devices"][0]) == ["device", "accepted", "statuses", "sent_at", "received_at", "shown_at",
                                                   "tapped_at", "received_count", "duplicate", "sent_to_received_s",
                                                   "received_to_shown_s", "outcome"]
    assert list(got["devices"][0]) == ["device", "sent", "accepted", "received", "shown", "tapped", "duplicates", "silent",
                                       "median_sent_to_received_s"]
    assert "title" not in json.dumps(got) and "body" not in got["pings"][0]


def test_the_command_filters_by_ref_prints_text_and_refuses_a_bad_since(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    rc, out = _cli(tmp_path, "--ref", "np-1", capsys=capsys)
    assert rc == 0 and "np-1" in out and "np-2" not in out
    rc, out = _cli(tmp_path, "--since", "soon", capsys=capsys)
    assert rc == 2 and "--since" in out
