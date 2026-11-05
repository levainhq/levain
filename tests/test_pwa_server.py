"""PWA package, lane SERVER (levain 27): a dead subscription is pruned and never retried, a re-subscribe of the same
endpoint is idempotent, Declarative Web Push is added only for a subscription that can read it, and the manifest carries
every field the plan lists (derived by parsing the file, never restated)."""
from __future__ import annotations

import io
import json
import os
import re
import urllib.error
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from levain.webpush import ece, routes, sender
from levain.webpush.store import PushStore
from tests import _push_fixtures as fx

STATIC = Path(routes.__file__).parent / "static"
TEMPLATES = Path(routes.__file__).resolve().parents[1] / "templates" / "web"
FCM = "https://fcm.googleapis.com/fcm/send/dead-one"
APPLE = "https://web.push.apple.com/QAbc123"


def _keys():
    ua = ec.generate_private_key(ec.SECP256R1())
    return ece.b64e(ece.public_bytes(ua)), ece.b64e(os.urandom(16))


@pytest.fixture(autouse=True)
def _owned(monkeypatch):
    fx.owned_devices(monkeypatch)


class FakePushService:
    """Stands in for the network under ``sender._OPENER``: answers every POST with ``status`` and records the endpoints."""

    def __init__(self, status):
        self.status, self.calls = status, []

    def open(self, req, timeout=None):
        self.calls.append(req.full_url)
        if self.status >= 400:
            raise urllib.error.HTTPError(req.full_url, self.status, "gone", {}, io.BytesIO(b""))
        return type("R", (), {"status": self.status, "__enter__": lambda s: s, "__exit__": lambda s, *a: None})()


# --- (3) the 404/410 prune -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [404, 410])
def test_a_push_service_answering_gone_prunes_the_subscription_and_it_is_not_retried(tmp_path, monkeypatch, status):
    store = PushStore(tmp_path)
    p, a = _keys()
    sid = store.add_subscription(FCM, p, a, "d")
    fake = FakePushService(status)
    monkeypatch.setattr(sender, "_OPENER", fake)
    assert dict(sender.send(tmp_path, kind="test", ref=None, title="t", body="b"))[sid] == status
    assert store.live_subscriptions() == []                       # deleted from the live store
    assert sender.send(tmp_path, kind="test", ref=None, title="t", body="again") == []
    assert fake.calls == [FCM]                                    # the second ping never reached the dead endpoint


# --- (3) re-subscribe is idempotent ----------------------------------------------------------------------------------

def test_the_page_resubscribing_the_same_endpoint_on_every_open_changes_nothing(tmp_path):
    store = PushStore(tmp_path)
    device = fx.pair(store, "phone")
    journal = fx.journal_of(store)
    p, a = _keys()
    body = json.dumps({"endpoint": APPLE, "keys": {"p256dh": p, "auth": a}}).encode()
    ids = []
    for _ in range(3):
        status, data, _ct = routes.handle("POST", "/webpush/subscribe", {}, body, queue_dir=tmp_path,
                                          authorized=True, device_id=device, journal=journal)
        assert status == 200
        ids.append(json.loads(data)["id"])
    assert len(set(ids)) == 1
    assert [r["id"] for r in store.live_subscriptions()] == ids[:1]
    # and a retired one that the page re-subscribes comes back live under the same row
    store.retire_subscription(APPLE)
    status, data, _ = routes.handle("POST", "/webpush/subscribe", {}, body, queue_dir=tmp_path, authorized=True,
                                    device_id=device, journal=journal)
    assert status == 200 and json.loads(data)["id"] == ids[0] and len(store.live_subscriptions()) == 1


# --- (3) Declarative Web Push only where the subscription can read it -----------------------------------------------

def _plaintext_sent(tmp_path, monkeypatch, endpoint):
    store = PushStore(tmp_path)
    p, a = _keys()
    store.add_subscription(endpoint, p, a, "d")
    seen = []
    real = ece.encrypt
    monkeypatch.setattr(ece, "encrypt", lambda data, *k: (seen.append(data), real(data, *k))[1])
    monkeypatch.setattr(sender, "_OPENER", FakePushService(201))
    sender.send(tmp_path, kind="test", ref="r", title="Needs you", body="A question", url="/?view=needs")
    return json.loads(seen[0])


def test_apple_gets_the_legacy_payload_while_the_url_is_relative_because_webkit_drops_a_relative_navigate(tmp_path, monkeypatch):
    got = _plaintext_sent(tmp_path, monkeypatch, APPLE)
    assert "web_push" not in got and "notification" not in got and got["title"] == "Needs you"


def test_other_push_services_get_the_existing_payload_unchanged(tmp_path, monkeypatch):
    got = _plaintext_sent(tmp_path, monkeypatch, FCM)
    assert "web_push" not in got and "notification" not in got and "actions" not in got


def test_a_declarative_payload_needs_an_absolute_url_keeps_the_legacy_members_and_has_no_actions_or_mutable():
    legacy = sender.build_payload("Needs you", "A question", "tag", "https://cockpit.example/?view=needs", 7, 3)
    got = json.loads(sender.declarative_payload(legacy))
    assert got["web_push"] == 8030 and got["app_badge"] == 3
    assert got["notification"] == {"title": "Needs you", "body": "A question", "tag": "tag",
                                   "navigate": "https://cockpit.example/?view=needs"}
    assert got["title"] == "Needs you" and got["ping"] == 7          # the legacy reader still works
    assert "actions" not in got and "actions" not in got["notification"] and "mutable" not in got
    relative = sender.build_payload("T", "b", "tag", "/?view=needs", 7)
    assert sender.declarative_payload(relative) == relative


def test_a_body_is_never_clipped_for_the_declarative_members():
    legacy = sender.build_payload("T", "x" * 3000, "tag", "https://cockpit.example/?view=needs", 7)
    out = sender.declarative_payload(legacy)
    assert out == legacy or json.loads(out)["body"] == json.loads(legacy)["body"]
    assert len(out) <= ece.MAX_PLAINTEXT


# --- (6) the manifest ---------------------------------------------------------------------------------------------

def _manifest():
    return json.loads((STATIC / "app.webmanifest").read_text())


def test_the_manifest_carries_every_field_the_plan_lists():
    m = _manifest()
    for key in ("id", "scope", "description", "launch_handler", "screenshots", "icons"):
        assert m.get(key), key
    # no shortcuts: /?view=needs and /?view=drop open nothing on the dashboard (L3 pwa2 r1), so none is offered
    assert "shortcuts" not in m
    # not focus-existing: with no launchQueue consumer on the page, a launch while the app is open would only focus it (L3 r1)
    assert m["launch_handler"] == {"client_mode": ["navigate-existing", "auto"]}
    assert m["scope"] == "/" and m["id"].startswith("/")
    purposes = {(i["purpose"], i["sizes"]) for i in m["icons"]}
    assert {("any", "192x192"), ("any", "512x512"), ("maskable", "192x192"), ("maskable", "512x512")} <= purposes
    # every icon and screenshot is a file the server serves, at the size it says
    from PIL import Image
    for item in m["icons"] + m["screenshots"]:
        assert item["src"] in routes.STATIC_PATHS, item["src"]
        assert Image.open(STATIC / item["src"].lstrip("/")).size == tuple(int(n) for n in item["sizes"].split("x"))
    assert {s["form_factor"] for s in m["screenshots"]} == {"narrow", "wide"}
    # maskable icons keep the glyph inside the 80% safe zone: the outer 10% ring is the background colour only
    for size in (192, 512):
        im = Image.open(STATIC / f"icon-maskable-{size}.png").convert("RGB")
        ring = max(1, size // 10)
        bg = im.getpixel((0, 0))
        assert all(im.getpixel((x, y)) == bg for x in range(size) for y in range(size)
                   if min(x, y, size - 1 - x, size - 1 - y) < ring)


def test_the_dashboard_head_carries_the_ios_meta_tags():
    head = (TEMPLATES / "dashboard.html").read_text().split("</head>")[0]
    assert 'viewport-fit=cover' in head and 'rel="apple-touch-icon"' in head
    assert re.search(r'apple-mobile-web-app-capable"\s+content="yes"', head)
    assert re.search(r'apple-mobile-web-app-status-bar-style"\s+content="black-translucent"', head)
