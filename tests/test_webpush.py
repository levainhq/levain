"""Web push: the RFC 8291 worked example, the store's permissions and record, the subscribe validation."""
from __future__ import annotations

import json
import os
import sqlite3
import stat

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from levain.webpush import ece, routes, vapid
from levain.webpush.store import PushStore, PushStoreError
from tests._push_fixtures import journal_of, owned_devices, pair


@pytest.fixture(autouse=True)
def _owned_devices(monkeypatch):
    """Every device a test here subscribes under is a paired device of a journal beside the queue, as every production
    subscribe's is: a subscription is live only under a paired device its journal holds (``store.live_rows``)."""
    owned_devices(monkeypatch)

# RFC 8291 section 5 (and Appendix A): the message, the keys and the body the example sends.
_PLAIN = b"When I grow up, I want to be a watermelon"
_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
_UA_PUB = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
_AS_PRIV = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
_BODY = ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_"
         "yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_rfc8291_worked_example_reproduces_the_exact_body():
    expected = ece.b64d(_BODY)
    sender = ec.derive_private_key(int.from_bytes(ece.b64d(_AS_PRIV), "big"), ec.SECP256R1())
    got = ece.encrypt(_PLAIN, ece.b64d(_UA_PUB), ece.b64d(_AUTH), _salt=expected[:16], _sender=sender)
    assert got == expected


def test_store_permissions_and_append_only_record(tmp_path):
    store = PushStore(tmp_path)
    ping = store.add_ping("test", None, "t", "b")
    sid = store.add_subscription("https://fcm.googleapis.com/x", "k", "a", "dev-a")
    store.add_delivery(ping, sid, 201)
    assert store.add_open(ping, None)
    assert stat.S_IMODE(os.stat(store.directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    with store.write() as c:
        for sql in ("UPDATE pings SET title = 'x'", "DELETE FROM deliveries", "UPDATE opens SET at = 'x'"):
            with pytest.raises(sqlite3.DatabaseError, match="never|rows"):
                c.execute(sql)
    assert store.record()[0]["deliveries"][0]["status"] == 201
    # a sub is always owned, and a re-subscribe takes the device subscribing now
    with pytest.raises(ValueError):
        store.add_subscription("https://fcm.googleapis.com/x", "k2", "a2", None)
    assert store.add_subscription("https://fcm.googleapis.com/x", "k3", "a3", "dev-b") == sid
    with store.read() as c:
        assert c.execute("SELECT device_id FROM subscriptions WHERE id = ?", (sid,)).fetchone()[0] == "paired-dev-b"
    # live subscriptions cap at 5 per device, 20 overall, and the least recently subscribed retire first: the oldest
    # row, subscribed again, stays
    for i in range(7):
        store.add_subscription(f"https://fcm.googleapis.com/n{i}", "k", "a", "dev-a")
        if i == 5:
            store.add_subscription("https://fcm.googleapis.com/n0", "k", "a", "dev-a")
    live = {r["endpoint"].rsplit("/", 1)[1] for r in store.live_subscriptions() if r["device_id"] == "paired-dev-a"}
    assert live == {"n0", "n3", "n4", "n5", "n6"}
    for i in range(30):
        store.add_subscription(f"https://fcm.googleapis.com/m{i}", "k", "a", f"d{i}")
    assert len(store.live_subscriptions()) == 20
    vapid.ensure_keys(tmp_path)   # one pair, kept across calls
    assert vapid.ensure_keys(tmp_path)[1] == vapid.ensure_keys(tmp_path)[1]


def test_symlinked_store_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "q").mkdir()
    os.symlink(real, tmp_path / "q" / "webpush")
    with pytest.raises(PushStoreError):
        PushStore(tmp_path / "q").live_subscriptions()


def test_subscribe_refuses_http_and_non_dns_hosts(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    keys = {"p256dh": ece.b64e(ece.public_bytes(key)), "auth": ece.b64e(os.urandom(16))}

    store = PushStore(tmp_path)
    dev, journal = pair(store, "p"), journal_of(store)

    def post(endpoint):
        return routes.handle("POST", "/webpush/subscribe", {}, json.dumps({"endpoint": endpoint, "keys": keys}).encode(),
                             queue_dir=tmp_path, authorized=True, device_id=dev, journal=journal)

    for bad in ("http://fcm.googleapis.com/x", "https://127.0.0.1/x", "https://localhost/x", "https://10.0.0.5/x",
                "https://[::1]/x", "https://intranet/x", "https://u:p@fcm.googleapis.com/x", "https://0x7f.0x1/x",
                "https://0177.0x0.0x0.0x1/x", "https://127.0.0.1.nip.io/x", "https://fcm.googleapis.com.evil.io/x",
                "https://evilfcm.googleapis.com.x/x", "https://notfcm.googleapis.com@127.0.0.1/x",
                "https://fcm.googleapis.com./x", "https://fcm.googleapis.com:8443/x", "https://fcm.googleapis.com/a b",
                "https://fcm.googleapis.com/a\x01b", "https://fcm.googleapis.com/a\nb", "https://push.example.net/x"):
        assert post(bad)[0] == 400, bad
    for good in ("https://fcm.googleapis.com/fcm/send/abc", "https://updates.push.services.mozilla.com/wpush/v2/x",
                 "https://web.push.apple.com/Qx", "https://wns2-par02p.notify.windows.com/w/?token=x"):
        assert post(good)[0] == 200, good
    status, data, _ = routes.handle("GET", "/webpush/record", {}, b"", queue_dir=tmp_path, authorized=True, device_id=None)
    assert status == 200 and all("outcome" in p and "deliveries" in p for p in json.loads(data)["pings"])
    assert routes.valid_endpoint("HTTPS://FCM.Googleapis.com/x") == "https://fcm.googleapis.com/x"
    for n in (10**30, 0, -1, True, 2**63):   # an out-of-range id is a 400, never an exception out of handle
        assert routes.handle("POST", "/webpush/opened", {}, json.dumps({"ping": n}).encode(), queue_dir=tmp_path,
                             authorized=True, device_id=None)[0] == 400, n
    assert routes.handle("POST", "/webpush/subscribe", {}, b"{}", queue_dir=tmp_path, authorized=False, device_id=None)[0] == 403
    assert routes.handle("GET", "/elsewhere", {}, b"", queue_dir=tmp_path, authorized=True, device_id=None) is None


def test_payload_fits_the_request_and_sender_paths(tmp_path, monkeypatch):
    from levain.webpush import sender

    assert ece.MAX_PLAINTEXT == 4096 - 86 - 16 - 1
    ua = ec.generate_private_key(ec.SECP256R1())
    for body in ('"' * 3000, "\x01" * 2900, "é€😀" * 1500, "x" * 100000):   # quote-heavy, control chars, multibyte
        data = sender.build_payload("t" * 5 + "\x01" * 300, body, "request:abc" + '"' * 400, "/" + "\\" * 900, 1)
        assert len(data) <= ece.MAX_PLAINTEXT and json.loads(data)["ping"] == 1
        assert len(ece.encrypt(data, ece.public_bytes(ua), os.urandom(16))) <= 4096
    assert len(json.loads(sender.build_payload("t", "short", "r", "/", 1))["body"]) == 5
    topic = sender.topic_for("request:abc")
    assert topic and len(topic) == 32 and sender.topic_for(None) is None
    # one aud for the claim and the cache key; the old placeholder contact migrates; a localhost contact is refused
    assert ece.vapid_aud("https://FCM.googleapis.com:443/x") == "https://fcm.googleapis.com"
    assert ece.vapid_aud("https://a.push.apple.com:8443/x") == "https://a.push.apple.com:8443"
    vapid.ensure_keys(tmp_path)
    with PushStore(tmp_path).write() as c:
        c.execute("UPDATE vapid SET sub = ?", ("mailto:levain@localhost",))
    assert vapid.ensure_keys(tmp_path)[2] == vapid.DEFAULT_SUB == "https://github.com/levainhq/levain"
    for bad in ("mailto:a@localhost", "https://localhost/x", "https://box.local/x", "https://intranet/x", "mailto:x"):
        with pytest.raises(ValueError):
            vapid.ensure_keys(tmp_path, bad)
    assert vapid.ensure_keys(tmp_path, "mailto:me@example.com")[2] == "mailto:me@example.com"
    # send: concurrent, a non-allowlisted stored endpoint is never POSTed, an unanswered one is status 0
    store = PushStore(tmp_path)
    key = ece.b64e(ece.public_bytes(ua))
    ok = store.add_subscription("https://fcm.googleapis.com/ok", key, ece.b64e(os.urandom(16)), "d")
    gone = store.add_subscription("https://web.push.apple.com/gone", key, ece.b64e(os.urandom(16)), "d")
    with store.write() as c:
        c.execute("INSERT INTO subscriptions (device_id, endpoint, p256dh, auth, created, journal) VALUES (?,?,?,?,?,?)",
                  ("paired-d", "https://127.0.0.1.nip.io/x", key, ece.b64e(os.urandom(16)), "2026-10-19T00:00:00Z",
                   str(journal_of(store))))
    seen = []

    def fake(req):
        seen.append((req.full_url, req.get_header("Topic")))
        return 410 if "apple" in req.full_url else 201

    monkeypatch.setattr(sender, "_post", fake)
    got = dict(sender.send(tmp_path, kind="consent", ref="request:abc", title="t", body='"' * 3000))
    assert got[ok] == 201 and got[gone] == 410 and sorted(got.values()) == [0, 201, 410]
    assert {u for u, _ in seen} == {"https://fcm.googleapis.com/ok", "https://web.push.apple.com/gone"}
    assert all(t == topic for _, t in seen)
    assert gone not in [r["id"] for r in store.live_subscriptions()]
    # spawn: the text rides stdin, argv holds only the queue dir and kind
    started = {}

    class P:
        def __init__(self, argv, **kw):
            started.update(argv=argv, kw=kw)
            self.stdin = type("S", (), {"write": lambda s, d: started.update(data=d), "close": lambda s: None})()

    monkeypatch.setattr(sender.subprocess, "Popen", P)
    assert sender.spawn(tmp_path, kind="consent", ref="r", title="t", body="-secret consent text")
    assert "-secret consent text" not in " ".join(started["argv"]) and started["kw"]["stdin"] == sender.subprocess.PIPE
    assert json.loads(started["data"])["body"] == "-secret consent text"




