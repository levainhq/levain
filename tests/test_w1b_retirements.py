"""W1b slice C: what retired with the phone port's run token and device registry stays retired. Three pins:
no emitted link carries a secret, the push store holds only ``paired-`` devices, and a subscribe with no device is 403."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
import pytest

import levain
from levain.consent.sign import deep_link
from levain.webpush import ece, routes, sender
from levain.webpush.store import PushStore
from tests._push_fixtures import journal_of, pair, revoke

LEVAIN = Path(levain.__file__).resolve().parent


def _string_constants(path: Path) -> "list[tuple[int, str]]":
    """Every string literal in ``path`` that is not a docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def test_no_emitted_link_carries_a_secret() -> None:
    """Reproduced red on a69ac9c8: ``phone_server.run_phone_server`` printed ``{page}#phone_token=...`` and wrote
    ``phone_token`` into the link file. The link builders now are ``deep_link`` (no credential), and the two startup
    prints that put a ``write_token`` in a fragment (loopback binds only, the page strips it)."""
    link = deep_link("https://box.ts.net:8443", "np-sign-0001", choice="a", version="a" * 64)
    assert link == f"https://box.ts.net:8443/sign/np-sign-0001#v={'a' * 16}&choice=a"
    assert "token" not in link and "secret" not in link
    # no module builds a phone_token link or a #sign= fragment link; write_token= is built at exactly two sites
    sites: dict[str, set[str]] = {}
    for path in LEVAIN.rglob("*.py"):
        for _line, text in _string_constants(path):
            for needle in ("phone_token=", "#sign=", "write_token="):
                if needle in text:
                    sites.setdefault(needle, set()).add(path.relative_to(LEVAIN).as_posix())
    assert "phone_token=" not in sites and "#sign=" not in sites, sites
    assert sites.get("write_token=", set()) <= {"web_server.py", "init_server.py"}, sites


def _sub(n: int = 1) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    return json.dumps({"endpoint": f"https://fcm.googleapis.com/fcm/send/ret{n}",
                       "keys": {"p256dh": ece.b64e(ece.public_bytes(key)), "auth": ece.b64e(os.urandom(16))}}).encode()


def test_a_subscribe_with_no_device_is_403_and_the_store_refuses_a_device_that_is_not_paired(tmp_path: Path) -> None:
    """Reproduced red on a69ac9c8: ``handle`` with ``device_id=None`` answered 200 and minted a device
    (``subscribe_unowned``), and ``add_subscription`` stored any device name."""
    status, data, _ = routes.handle("POST", "/webpush/subscribe", {}, _sub(), queue_dir=tmp_path, authorized=True,
                                    device_id=None)
    assert status == 403 and json.loads(data)["error"] == "credential_required"
    assert not hasattr(PushStore, "subscribe_unowned")
    store = PushStore(tmp_path)
    assert store.live_subscriptions() == []
    for device in ("phone", "d-abc", "", None):
        with pytest.raises(ValueError):
            store.add_subscription("https://fcm.googleapis.com/fcm/send/x", "k", "a", device)
    with pytest.raises(ValueError):                      # a paired device names its journal, as before
        store.add_subscription("https://fcm.googleapis.com/fcm/send/x", "k", "a", "paired-d1")
    # and through the route: a device id that is not paired answers 403, nothing stored
    status, _data, _ = routes.handle("POST", "/webpush/subscribe", {}, _sub(2), queue_dir=tmp_path, authorized=True,
                                     device_id="phone")
    assert status == 403 and store.live_subscriptions() == []


def test_a_subscription_row_under_a_device_that_is_not_paired_is_never_live_or_sent_to(tmp_path: Path,
                                                                                       monkeypatch) -> None:
    """Reproduced red on a69ac9c8: ``live_rows`` joined the phone port's own ``devices`` table, so a probe row under a
    phone-port device id was live. Now only a ``paired-`` device its journal holds unrevoked is."""
    store = PushStore(tmp_path)
    key = ece.b64e(ece.public_bytes(ec.generate_private_key(ec.SECP256R1())))
    legacy = "https://fcm.googleapis.com/fcm/send/legacy"
    with store.write() as c:                              # what a retired phone port wrote: no journal, a d- id
        c.execute("INSERT INTO subscriptions (device_id, endpoint, p256dh, auth, created) VALUES (?,?,?,?,?)",
                  ("d-0123456789ab", legacy, key, ece.b64e(os.urandom(16)), "2026-10-19T00:00:00Z"))
    live_ep = "https://fcm.googleapis.com/fcm/send/live"
    store.add_subscription(live_ep, key, ece.b64e(os.urandom(16)), pair(store, "d1"), journal=journal_of(store))
    posted: list[str] = []
    monkeypatch.setattr(sender, "_post", lambda req: posted.append(req.full_url) or 201)
    sender.send(tmp_path, kind="consent", ref="request:abc", title="t", body="b")
    assert [r["endpoint"] for r in store.live_subscriptions()] == [live_ep]
    assert posted == [live_ep]                            # the legacy row got nothing
    revoke(store, "d1")
    assert store.live_subscriptions() == []


def test_a_second_open_for_the_same_ping_and_device_is_a_no_op_that_answers_like_the_first(tmp_path: Path) -> None:
    """Two cockpit tabs can drain the same ``opened`` key. Reproduced red before this change: the second POST inserted a
    second ``opens`` row for the same (ping, device)."""
    store = PushStore(tmp_path)
    ping = store.add_ping("test", None, "t", "b")
    dev = pair(store, "d1")
    answers = [routes.handle("POST", "/webpush/opened", {}, json.dumps({"ping": ping}).encode(), queue_dir=tmp_path,
                             authorized=True, device_id=dev) for _ in range(2)]
    assert answers[0] == answers[1] and json.loads(answers[0][1]) == {"recorded": True}
    assert [o["device_id"] for o in store.record()[0]["opens"]] == [dev]
    other = routes.handle("POST", "/webpush/opened", {}, json.dumps({"ping": ping}).encode(), queue_dir=tmp_path,
                          authorized=True, device_id=pair(store, "d2"))
    assert json.loads(other[1]) == {"recorded": True} and len(store.record()[0]["opens"]) == 2
