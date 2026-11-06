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


def _sub_id(store: PushStore, endpoint: str = EP) -> int:
    with store.read() as c:
        return int(c.execute("SELECT id FROM subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()[0])


def _ping(store: PushStore, ref: str | None = None, endpoint: str = EP) -> int:
    """A ping the send is pushing to ``endpoint``'s subscription (its target recorded, no answer yet)."""
    ping = store.add_ping("sign", ref, "t", "b")
    store.add_targets(ping, [_sub_id(store, endpoint)])
    return ping


# --- the store -------------------------------------------------------------------------------------------------------

def test_a_receipt_is_appended_under_the_device_whose_endpoint_hashes_to_it(store: PushStore) -> None:
    ping = _ping(store, "np-1")
    assert store.add_receipt(H, ping, "received", _ms()) == "recorded"
    with store.read() as c:
        (row,) = c.execute("SELECT ping_id, device_id, kind, at, reported_at FROM receipts").fetchall()
    assert (row[0], row[1], row[2]) == (ping, PAIRED_PREFIX + "phone", "received")
    assert row[3].endswith("Z") and "." in row[3] and row[4].endswith("Z") and "." not in row[4]


def test_a_retried_report_is_idempotent_and_a_second_delivery_is_a_second_row(store: PushStore) -> None:
    ping = _ping(store)
    at = _ms()
    assert store.add_receipt(H, ping, "received", at) == "recorded"
    assert store.add_receipt(H, ping, "received", at) == "duplicate"
    assert store.add_receipt(H, ping, "received", at + 5000) == "recorded"        # the repeat-notification signal
    with store.read() as c:
        assert c.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2


def test_receipts_never_change_and_are_never_deleted(store: PushStore) -> None:
    ping = _ping(store)
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
    ping = _ping(s)
    assert PushStore(tmp_path).add_receipt(H, ping, "received", _ms()) == "recorded"   # a fresh opener ensures the schema


def test_unknown_device_ping_kind_and_time_are_refused_with_nothing_written(store: PushStore) -> None:
    ping = _ping(store)
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
    ping = _ping(store)
    revoke(store, "phone")
    assert store.add_receipt(H, ping, "received", _ms()) == "unknown_device"
    other = tmp_path / "q2"
    phone(other, EP)
    s2 = PushStore(other)
    p2 = _ping(s2)
    s2.retire_subscription(EP)
    assert s2.add_receipt(H, p2, "received", _ms()) == "unknown_device"


def test_a_device_cannot_report_past_its_caps(store: PushStore) -> None:
    ping = _ping(store)
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
    fresh = _ping(store)
    assert store.add_receipt(H, fresh, "received", _ms()) == "limited"              # the device's hour


EP_B = "https://web.push.apple.com/b-phone"
H_B = hashlib.sha256(EP_B.encode()).hexdigest()


def test_a_device_writes_only_about_pings_sent_to_its_own_subscription(store: PushStore, tmp_path: Path) -> None:
    phone(tmp_path, EP_B, "b")
    only_a = _ping(store)                                                   # targeted at A's subscription only
    assert store.add_receipt(H_B, only_a, "received", _ms()) == "unknown_ping"     # B cannot write about it
    never_pushed = store.add_ping("sign", None, "t", "b")
    assert store.add_receipt(H, never_pushed, "received", _ms()) == "unknown_ping"  # nor A about a ping never pushed
    assert store.add_receipt(H, only_a, "received", _ms()) == "recorded"          # A, about its own, can
    to_b = _ping(store, endpoint=EP_B)
    assert store.add_receipt(H_B, to_b, "shown", _ms()) == "recorded"
    # a delivery row (the answer, written after the push) counts like the target written before it
    answered = store.add_ping("sign", None, "t", "b")
    store.add_delivery(answered, _sub_id(store, EP_B), 201)
    assert store.add_receipt(H_B, answered, "received", _ms()) == "recorded"
    assert store.add_receipt(H, answered, "received", _ms()) == "unknown_ping"
    with store.read() as c:
        rows = sorted(tuple(r) for r in c.execute("SELECT ping_id, device_id FROM receipts"))
    assert rows == sorted([(only_a, PAIRED_PREFIX + "phone"), (to_b, PAIRED_PREFIX + "b"), (answered, PAIRED_PREFIX + "b")])
    # the route says the same thing for "not sent to you" as for "no such ping": it is not an oracle
    a = routes.receipt(json.dumps(_body(only_a, h=H_B)).encode(), queue_dir=tmp_path)
    b = routes.receipt(json.dumps(_body(10_000, h=H_B)).encode(), queue_dir=tmp_path)
    assert a == b and a[0] == 404


def test_the_send_records_its_targets_before_any_push_so_an_early_receipt_is_accepted(tmp_path: Path, monkeypatch) -> None:
    import os

    from cryptography.hazmat.primitives.asymmetric import ec

    from levain.webpush import ece, sender, vapid

    vapid.ensure_keys(tmp_path)
    s = PushStore(tmp_path)
    key = ece.b64e(ece.public_bytes(ec.generate_private_key(ec.SECP256R1())))
    s.add_subscription(EP, key, ece.b64e(os.urandom(16)), "phone")
    early = []

    def post(req):   # the phone's receipt lands while the push service has not answered yet
        ping = PushStore(tmp_path).record(1)[0]["id"]
        early.append(PushStore(tmp_path).add_receipt(H, ping, "received", _ms()))
        with PushStore(tmp_path).read() as c:
            early.append(c.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0])
        return 201

    monkeypatch.setattr(sender, "_post", post)
    sender.send(tmp_path, kind="sign", ref="np-1", title="t", body="b")
    assert early == ["recorded", 0]                      # accepted before the delivery row existed
    with s.read() as c:
        assert c.execute("SELECT COUNT(*) FROM targets").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1
        with pytest.raises(sqlite3.DatabaseError, match="never"):
            c.execute("DELETE FROM targets")


def test_an_unauthenticated_hash_never_takes_the_write_lock(store: PushStore, tmp_path: Path, monkeypatch) -> None:
    import secrets

    ping = _ping(store)
    writes = []
    real = PushStore.write

    def counted(self):
        writes.append(1)
        return real(self)

    monkeypatch.setattr(PushStore, "write", counted)
    for _ in range(50):
        assert routes.receipt(json.dumps(_body(ping, h=secrets.token_hex(32))).encode(), queue_dir=tmp_path)[0] == 403
    assert writes == []                                     # a fresh store per request, and still no write lock
    assert store.add_receipt(H, ping + 1, "received", _ms()) == "unknown_ping" and writes == []
    assert store.add_receipt(H, ping, "received", _ms()) == "recorded" and len(writes) == 1
    at = _ms()
    assert store.add_receipt(H, ping, "shown", at) == "recorded" and store.add_receipt(H, ping, "shown", at) == "duplicate"
    assert len(writes) == 2                                 # a duplicate is answered from the read


def test_a_flood_of_random_hashes_leaves_a_real_write_unblocked(store: PushStore, tmp_path: Path) -> None:
    import secrets
    import threading

    stop = threading.Event()
    refused = [0]

    def flood() -> None:
        while not stop.is_set():
            assert PushStore(tmp_path).add_receipt(secrets.token_hex(32), 1, "received", _ms()) == "unknown_device"
            refused[0] += 1

    threads = [threading.Thread(target=flood, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        time.sleep(0.2)
        worst = 0.0
        for _ in range(10):
            t0 = time.monotonic()
            ping = _ping(store)
            assert store.add_receipt(H, ping, "received", _ms()) == "recorded"
            worst = max(worst, time.monotonic() - t0)
    finally:
        stop.set()
        for t in threads:
            t.join(5)
    assert refused[0] > 50 and worst < 2.0, (refused[0], worst)


def test_a_hash_is_compared_with_compare_digest_over_every_candidate(store: PushStore, tmp_path: Path, monkeypatch) -> None:
    from levain.webpush import store as store_mod

    phone(tmp_path, EP_B, "b")
    ping = _ping(store)
    calls = []
    real = store_mod.hmac.compare_digest
    monkeypatch.setattr(store_mod.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    assert store.add_receipt(H, ping, "received", _ms()) == "recorded"     # A matches first, B is still compared
    assert len(calls) == 2
    assert store.add_receipt("not hex", ping, "received", _ms()) == "unknown_device"


# --- the route -------------------------------------------------------------------------------------------------------

def _body(ping: int, kind: str = "received", at: int | None = None, h: str = H) -> dict[str, Any]:
    return {"ping": ping, "kind": kind, "at": at or _ms(), "endpoint_sha256": h}


def test_the_receipt_route_refuses_what_it_cannot_place_and_writes_nothing(store: PushStore, tmp_path: Path) -> None:
    ping = _ping(store)
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
    ping = _ping(store)
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
        ping = _ping(PushStore(qdir), "np-7", endpoint=ep)
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
        ping = _ping(PushStore(qdir), endpoint=_sub(7)["endpoint"])
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
    return [(e["kind"], e.get("result"), e.get("status")) for e in got["trace"] if e["step"] in ("receipt", "receipt-refused")]


def _keys(queued: list[str]) -> list[tuple[int, str]]:
    """(ping, kind) of each queued key, which must be the per-report form <ping>-<kind>-<13-digit ms>-<4-digit seq>."""
    out = []
    for k in queued:
        tail = k[len("/__levain_receipt/"):]
        ping, kind, at, seq = tail.split("-")
        assert len(at) == 13 and at.isdigit() and len(seq) == 4 and seq.isdigit(), k
        out.append((int(ping), kind))
    return sorted(out)


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
    assert any(e["step"] == "sw-version" and e["version"] == "1106-51-shown-2" and e["on"] == "push" for e in got["trace"])


@needs_node
def test_the_notification_is_asked_for_before_any_receipt_and_still_shown_when_fetch_throws() -> None:
    got = _run("throw")
    assert got["order"][0] == "show", got["order"]
    assert got["shownCalls"] == [{"title": "T", "tag": "x"}]
    assert _keys(got["queued"]) == [(42, "received"), (42, "shown")], got["queued"]
    assert _receipt_steps(got) == [("received", "queued", None), ("shown", "queued", None)]


@needs_node
def test_a_hanging_post_is_cut_by_the_event_budget_and_stays_queued() -> None:
    got = _run("hang")
    assert 3800 <= got["elapsedMs"] < 4800, got["elapsedMs"]
    assert got["shownCalls"] and _keys(got["queued"]) == [(42, "received"), (42, "shown")]


@needs_node
def test_the_notification_is_shown_when_every_receipt_step_hangs() -> None:
    for mode in ("hang+subhang", "hang+cachehang", "deaf+cachehang", "deaf+subhang+cachehang"):
        got = _run(mode)
        assert got["order"][0] == "show" and got["shownCalls"] == [{"title": "T", "tag": "x"}], (mode, got["order"])
        assert got["elapsedMs"] < 4800, (mode, got["elapsedMs"])        # the event ends by the 4 s receipt budget


@needs_node
def test_twenty_queued_receipts_with_slow_posts_end_within_the_budget() -> None:
    got = _run("slow+seed20+clickonly")
    assert got["clickMs"] < 4800, got["clickMs"]
    assert len(got["posts"]) <= 5                                      # at most five re-sent in one event
    assert len(got["queued"]) >= 20 - len(got["posts"]) + 1            # the one the deadline aborted stays queued
    assert got.get("aborted", 0) == 1


@needs_node
def test_a_failed_post_is_resent_by_the_next_notification_click_oldest_first() -> None:
    got = _run("throw+then:ok+click")
    assert _keys(got["afterFirst"]["queued"]) == [(42, "received"), (42, "shown")]
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
def test_two_offline_deliveries_of_one_ping_both_survive_and_are_both_sent() -> None:
    got = _run("throw+twice+then:ok+click")
    assert _keys(got["afterFirst"]["queued"]) == [(42, "received"), (42, "received"), (42, "shown"), (42, "shown")]
    sent = sorted((p["body"]["kind"], p["body"]["at"]) for p in got["posts"])
    assert len(sent) == 4 and sent == sorted((p["body"]["kind"], p["body"]["at"]) for p in got["firstPosts"])
    assert got["queued"] == []


@needs_node
def test_a_drain_stops_at_the_first_failure_and_keeps_what_it_did_not_send() -> None:
    got = _run("throw+then:throw+click")
    assert len(got["posts"]) == 1                              # one attempt, then the drain stops
    assert _keys(got["queued"]) == [(42, "received"), (42, "shown")]
    assert ("received", "still-queued", None) in _receipt_steps(got)


@needs_node
def test_a_drain_sends_five_at_most_and_the_oldest_first() -> None:
    got = _run("ok+seed25+seedold+clickonly")
    sent = [(p["body"]["ping"], p["body"]["at"]) for p in got["posts"]]
    assert len(sent) == 5 and sent[0] == (99, 1_750_000_000_000), sent          # an entry of the old key form goes first
    ats = [a for _, a in sent[1:]]
    assert ats == sorted(got["seededAts"])[:4], ats
    assert len(got["queued"]) == 26 - 5


@needs_node
def test_a_drain_goes_past_an_entry_the_server_keeps_refusing_and_drops_one_older_than_a_day() -> None:
    # L3 r2 (complement MED): a 429 or 5xx on the oldest entry stopped the drain, so it held every newer one behind it
    got = _run("429+seed4+seedstale+clickonly")
    posted = [p["body"]["ping"] for p in got["posts"]]
    assert 98 not in posted, "an entry older than a day is dropped, never posted"
    assert sorted(posted) == [42, 42, 100, 101, 102, 103] or {100, 101, 102, 103} <= set(posted), posted   # every fresh seeded entry was tried
    assert not any(k.startswith("/__levain_receipt/98-") for k in got["queued"]), got["queued"]
    assert (None, "expired", None) in _receipt_steps(got), _receipt_steps(got)
    got = _run("throw+seed4+clickonly")
    assert len(got["posts"]) <= 2, "no answer at all (the network) still stops the drain"


@needs_node
def test_the_queue_is_capped_and_drops_the_oldest() -> None:
    got = _run("throw+seed200")
    assert len(got["queued"]) == 200
    assert (42, "received") in _keys([k for k in got["queued"] if k.startswith("/__levain_receipt/42-")])
    oldest = sorted(got["seededAts"])[:2]
    left = {int(k.split("-")[-2]) for k in got["queued"]}
    assert not left & set(oldest)                                # the two oldest seeded entries were dropped


@needs_node
def test_the_endpoint_hash_is_asked_per_event_so_a_rotated_subscription_sends_its_new_hash() -> None:
    got = _run("ok+then:ok+push+rotate")
    first = {p["body"]["endpoint_sha256"] for p in got["firstPosts"]}
    second = {p["body"]["endpoint_sha256"] for p in got["posts"]}
    assert first == {hashlib.sha256(b"https://fcm.googleapis.com/fcm/send/dev1").hexdigest()}
    assert second == {hashlib.sha256(b"https://fcm.googleapis.com/fcm/send/dev2").hexdigest()}


@needs_node
def test_a_refused_hash_is_traced_receipt_refused_and_dropped_not_retried() -> None:
    got = _run("403+then:ok+click")
    assert got["afterFirst"]["queued"] == [] and got["posts"] == []          # nothing left to retry
    refused = [e for e in got["trace"] if e["step"] == "receipt-refused"]
    assert [(e["kind"], e["status"]) for e in refused] == [("received", 403), ("shown", 403)]


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
    got = _run("404")
    assert got["queued"] == [] and _receipt_steps(got) == [("received", "refused", 404), ("shown", "refused", 404)]
    got = _run("500")
    assert len(got["queued"]) == 2 and _receipt_steps(got)[0] == ("received", "queued", 500)
    got = _run("429")                                            # a rate limit is retried later, never settled
    assert len(got["queued"]) == 2 and _receipt_steps(got)[0] == ("received", "queued", 429)


@needs_node
def test_a_show_that_fails_is_traced_and_posts_no_shown_receipt() -> None:
    got = _run("ok+showthrows")
    assert [p["body"]["kind"] for p in got["posts"]] == ["received"]
    assert any(e["step"] == "show-failed" for e in got["trace"])


@needs_node
def test_a_notification_the_agent_does_not_hold_posts_no_shown_receipt() -> None:
    """2026-11-05, ping 409: showNotification resolved on four devices and none displayed it, yet each reported `shown`.
    The rig's platform swallows the notification (the show resolves, getNotifications lists nothing): only `received`
    goes, and the trace says show-missing with the tag's length and leading word, never its text."""
    got = _run("ok+p409+swallowed")
    assert [(p["body"]["ping"], p["body"]["kind"]) for p in got["posts"]] == [(409, "received")], got["posts"]
    assert got["getNotificationsCalls"] == [{"tag": "request:pend-2026-11-05T19:54:58.556594Z-2690252d9c8e0d77"}]
    miss = [e for e in got["trace"] if e["step"] == "show-missing"]
    assert [(e["why"], e["tagLen"], e["tagKind"], e["count"]) for e in miss] == [("absent", 57, "request", 0)], miss
    assert "secret body" not in json.dumps(got["trace"]) and "secret body" not in json.dumps(got["posts"])


@needs_node
def test_a_held_notification_still_posts_shown_and_the_p409_shape_is_listed_by_its_own_tag() -> None:
    got = _run("ok+p409")
    assert [(p["body"]["ping"], p["body"]["kind"]) for p in got["posts"]] == [(409, "received"), (409, "shown")]
    assert not any(e["step"] == "show-missing" for e in got["trace"])


@needs_node
def test_only_a_notification_with_this_exact_tag_counts_as_held() -> None:
    # another ping's notification is held and the UA ignores the filter (an older WebKit listed the app's every one)
    got = _run("ok+swallowed+othertag+getnall")
    assert [p["body"]["kind"] for p in got["posts"]] == ["received"], got["posts"]
    assert [(e["why"], e["count"]) for e in got["trace"] if e["step"] == "show-missing"] == [("absent", 1)]


@needs_node
def test_a_stale_held_notification_with_the_same_tag_does_not_certify_a_new_ping() -> None:
    """A re-ping reuses the ref as its tag (renotify). The UA still holds the EARLIER ping's notification (data.ping 41)
    under that tag and the platform swallowed the new show (ping 42): the tag alone matches, the ping does not, so only
    `received` goes. A held notification carrying no ping (or no data at all) is not a match either."""
    for flag in ("staleheld", "foreigndata", "nodata"):
        got = _run("ok+swallowed+" + flag)
        assert [(p["body"]["ping"], p["body"]["kind"]) for p in got["posts"]] == [(42, "received")], (flag, got["posts"])
        assert [(e["why"], e["count"]) for e in got["trace"] if e["step"] == "show-missing"] == [("absent", 1)], flag
    # the same stale notification, but the new show was held (it replaced the old one under the tag): shown goes
    got = _run("ok+staleheld")
    assert [(p["body"]["ping"], p["body"]["kind"]) for p in got["posts"]] == [(42, "received"), (42, "shown")], got["posts"]


@needs_node
def test_a_throw_out_of_the_held_check_leaves_the_received_receipt_and_the_queue_drain_intact() -> None:
    """The shown arm's chain ends in a catch: even a failure inside heldByAgent's own failure path (a tag whose length
    throws; nothing real sends one) rejects nothing into Promise.all, so `received` goes and the queue is still drained."""
    got = _run("ok+swallowed+taghostile+seed1")
    kinds = [(p["body"]["ping"], p["body"]["kind"]) for p in got["posts"]]
    assert (42, "received") in kinds and (42, "shown") not in kinds, kinds
    assert (100, "received") in kinds, kinds                      # the seeded entry was re-sent: the drain ran


@needs_node
def test_no_way_to_list_notifications_means_no_shown_receipt_and_the_notification_still_shows() -> None:
    for flag, why in (("nogetn", "no-api"), ("getnthrows", "error")):
        got = _run("ok+" + flag)
        assert got["shownCalls"] == [{"title": "T", "tag": "x"}] and got["order"][0] == "show", flag
        assert [p["body"]["kind"] for p in got["posts"]] == ["received"], (flag, got["posts"])
        assert [e["why"] for e in got["trace"] if e["step"] == "show-missing"] == [why], flag


def test_activation_keeps_the_receipts_cache() -> None:
    import re

    text = SW.read_text(encoding="utf-8")
    assert re.search(r"CACHES_KEEP = \[OPEN_CACHE, TRACE_CACHE, RECEIPT_CACHE, SHELL_CACHE\]", text)
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
    assert "push service gone (404/410)" in out


def _two_phones(tmp_path: Path) -> tuple[PushStore, int, int]:
    phone(tmp_path, "https://fcm.googleapis.com/fcm/send/a", "a")
    phone(tmp_path, "https://web.push.apple.com/b", "b")
    s = PushStore(tmp_path)
    with s.read() as c:
        subs = {r["device_id"]: r["id"] for r in c.execute("SELECT id, device_id FROM subscriptions")}
    return s, subs[PAIRED_PREFIX + "a"], subs[PAIRED_PREFIX + "b"]


def _rows(s: PushStore, deliveries=(), receipts_=(), opens=()) -> None:
    with s.write() as c:
        c.executemany("INSERT INTO deliveries (ping_id, subscription_id, status, at) VALUES (?,?,?,?)", deliveries)
        c.executemany("INSERT INTO receipts (ping_id, device_id, kind, at, reported_at) VALUES (?,?,?,?,?)",
                      [(p, PAIRED_PREFIX + d, k, at, at[:19] + "Z") for p, d, k, at in receipts_])
        c.executemany("INSERT INTO opens (ping_id, device_id, at) VALUES (?,?,?)",
                      [(p, PAIRED_PREFIX + d, at) for p, d, at in opens])


def _ms_stamp(minutes: int, ms: int = 0) -> str:
    return _t(minutes)[:-1] + f".{ms:03d}Z"


def test_silent_is_decided_by_times_not_by_ping_id_order(tmp_path: Path) -> None:
    s, sa, _ = _two_phones(tmp_path)
    held, later = s.add_ping("sign", "np-held", "t", "b"), s.add_ping("sign", "np-2", "t", "b")   # held < later by id
    # `later` went out first and was reported; `held` was released afterwards under its old, lower id
    _rows(s, deliveries=[(later, sa, 201, _t(0)), (held, sa, 201, _t(5))],
          receipts_=[(later, "a", "received", _ms_stamp(0, 500))])
    assert _by(receipts.build(s), 0, [held], "a")["outcome"] == "no receipt yet"
    _rows(s, receipts_=[(later, "a", "shown", _ms_stamp(6))])                 # A reported something after held reached it
    assert _by(receipts.build(s), 0, [held], "a")["outcome"] == "silent"


def test_a_phone_with_a_fast_clock_is_not_silent_the_server_clock_decides(tmp_path: Path) -> None:
    # L3 r2 (complement LOW): silent compared the phone's own `at` with the server's delivery time, so a clock a day ahead
    # made every later ping silent; it compares when the SERVER received the newest report (reported_at)
    s, sa, _ = _two_phones(tmp_path)
    first, later = s.add_ping("sign", "np-1", "t", "b"), s.add_ping("sign", "np-2", "t", "b")
    _rows(s, deliveries=[(first, sa, 201, _t(0)), (later, sa, 201, _t(5))])
    with s.write() as c:       # the phone's clock says tomorrow; the server received the report at minute 1
        c.execute("INSERT INTO receipts (ping_id, device_id, kind, at, reported_at) VALUES (?,?,?,?,?)",
                  (first, PAIRED_PREFIX + "a", "received", _ms_stamp(60 * 24), _ms_stamp(1)))
    assert _by(receipts.build(s), 0, [later], "a")["outcome"] == "no receipt yet"


def test_a_tapped_ping_is_shown_even_with_its_receipts_lost(tmp_path: Path) -> None:
    s, sa, sb = _two_phones(tmp_path)
    p, q = s.add_ping("sign", None, "t", "b"), s.add_ping("sign", None, "t", "b")
    _rows(s, deliveries=[(p, sa, 201, _t(0)), (q, sa, 201, _t(1)), (p, sb, 410, _t(0))],
          receipts_=[(q, "a", "received", _ms_stamp(2))], opens=[(p, "a", _t(3)), (p, "b", _t(3))])
    rep = receipts.build(s)
    a = _by(rep, 0, [p], "a")
    assert a["outcome"] == "shown" and a["tapped_at"] == _t(3) and a["received_at"] is None    # never "silent"
    b = _by(rep, 0, [p], "b")
    assert b["outcome"] == "shown" and b["push"] == "gone"                  # a tap beats "not accepted" too
    assert {r["device"]: r["silent"] for r in rep["devices"]}[PAIRED_PREFIX + "a"] == 0


@pytest.mark.parametrize("statuses, push, accepted, outcome", [
    ([201], "accepted", True, "no receipt yet"),
    ([0], "no-answer", None, "no receipt yet"),
    ([-1], "no-answer", None, "no receipt yet"),
    ([-1, 503], "no-answer", None, "no receipt yet"),
    ([0, 201], "accepted", True, "no receipt yet"),
    ([500], "refused", False, "not accepted"),
    ([403, 429], "refused", False, "not accepted"),
    ([404], "gone", False, "not accepted"),
    ([410, 500], "gone", False, "not accepted"),
])
def test_the_five_push_states(tmp_path: Path, statuses: list[int], push: str, accepted: bool | None, outcome: str) -> None:
    s, sa, _ = _two_phones(tmp_path)
    p = s.add_ping("sign", None, "t", "b")
    _rows(s, deliveries=[(p, sa, st, _t(i)) for i, st in enumerate(statuses)])
    d = _by(receipts.build(s), 0, [p], "a")
    assert (d["push"], d["accepted"], d["outcome"]) == (push, accepted, outcome)
    assert receipts.push_state(statuses) == push
    word = {"accepted": "accepted", "no-answer": "no answer", "refused": "refused", "gone": "gone (404/410)"}[push]
    assert f"push service {word};" in receipts.text(receipts.build(s))


def test_the_fifth_push_state_is_none_without_a_delivery(tmp_path: Path) -> None:
    s, _, _ = _two_phones(tmp_path)
    p = s.add_ping("sign", None, "t", "b")
    _rows(s, receipts_=[(p, "b", "received", _ms_stamp(0))])
    d = _by(receipts.build(s), 0, [p], "b")
    assert (d["push"], d["accepted"], d["outcome"]) == ("none", None, "received")
    assert "push service no delivery on record" in receipts.text(receipts.build(s))
    assert receipts.PUSH_STATES == ("accepted", "no-answer", "gone", "refused", "none")


def test_a_long_record_reports_without_since_in_bounded_time(tmp_path: Path, capsys) -> None:
    n = 5000
    phone(tmp_path, "https://fcm.googleapis.com/fcm/send/a", "a")
    phone(tmp_path, "https://web.push.apple.com/b", "b")
    phone(tmp_path, "https://updates.push.services.mozilla.com/c", "c")
    s = PushStore(tmp_path)
    with s.write() as c:
        subs = {r["device_id"][len(PAIRED_PREFIX):]: r["id"] for r in c.execute("SELECT id, device_id FROM subscriptions")}
        c.executemany("INSERT INTO pings (at, kind, ref, title, body) VALUES (?,?,?,?,?)",
                      [(_t(i % 600), "sign", f"np-{i}", "t", "b") for i in range(n)])
        ids = [r[0] for r in c.execute("SELECT id FROM pings ORDER BY id")]
        c.executemany("INSERT INTO deliveries (ping_id, subscription_id, at, status) VALUES (?,?,?,?)",
                      [(p, sid, _t(i % 600), 201) for i, p in enumerate(ids) for sid in subs.values()])
        c.executemany("INSERT INTO receipts (ping_id, device_id, kind, at, reported_at) VALUES (?,?,?,?,?)",
                      [(p, PAIRED_PREFIX + d, k, _ms_stamp(i % 600, 100), _t(i % 600))
                       for i, p in enumerate(ids) for d in ("a", "b") for k in ("received", "shown")])
        c.executemany("INSERT INTO opens (ping_id, device_id, at) VALUES (?,?,?)",
                      [(p, PAIRED_PREFIX + "a", _t(i % 600)) for i, p in enumerate(ids[::10])])
    t0 = time.monotonic()
    rep = receipts.build(s)                                  # no --since: every ping, more than any SQLite variable limit
    took = time.monotonic() - t0
    assert len(rep["pings"]) == n and all(len(p["devices"]) == 3 for p in rep["pings"])
    roll = {r["device"]: r for r in rep["devices"]}
    assert roll[PAIRED_PREFIX + "a"]["shown"] == n and roll[PAIRED_PREFIX + "c"]["received"] == 0
    assert took < 10.0, took
    t0 = time.monotonic()
    rc, out = _cli(tmp_path, capsys=capsys)
    assert rc == 0 and f"#{ids[-1]} " in out and time.monotonic() - t0 < 15.0


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
    assert list(got["pings"][0]["devices"][0]) == ["device", "push", "accepted", "statuses", "sent_at", "received_at", "shown_at",
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
