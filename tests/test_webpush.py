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
    sid = store.add_subscription("https://push.example.net/x", "k", "a")
    store.add_delivery(ping, sid, 201)
    assert store.add_open(ping, None)
    assert stat.S_IMODE(os.stat(store.directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    with store.write() as c:
        for sql in ("UPDATE pings SET title = 'x'", "DELETE FROM deliveries", "UPDATE opens SET at = 'x'"):
            with pytest.raises(sqlite3.DatabaseError, match="never|rows"):
                c.execute(sql)
    assert store.record()[0]["deliveries"][0]["status"] == 201
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

    def post(endpoint):
        return routes.handle("POST", "/webpush/subscribe", {}, json.dumps({"endpoint": endpoint, "keys": keys}).encode(),
                             queue_dir=tmp_path, authorized=True, device_id=None)

    for bad in ("http://fcm.googleapis.com/x", "https://127.0.0.1/x", "https://localhost/x", "https://10.0.0.5/x",
                "https://[::1]/x", "https://intranet/x", "https://u:p@fcm.googleapis.com/x"):
        assert post(bad)[0] == 400, bad
    assert post("https://fcm.googleapis.com/fcm/send/abc")[0] == 200
    assert routes.handle("POST", "/webpush/subscribe", {}, b"{}", queue_dir=tmp_path, authorized=False, device_id=None)[0] == 403
    assert routes.handle("GET", "/elsewhere", {}, b"", queue_dir=tmp_path, authorized=True, device_id=None) is None
