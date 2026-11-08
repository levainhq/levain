"""W5a: a push carries its card (headline, options, rec, subject id, version) and NEVER an action. Phill 11-06: no tappable
buttons on the notification at all; a tap opens the card. The worker never answers: nothing here may name ``band_answer``."""
from __future__ import annotations

import json
import re
from pathlib import Path

from levain.webpush import ece, sender
from levain.webpush.store import PushStore

ROOT = Path(__file__).resolve().parent.parent
ITEM = "np-0123abcd"
DATA = {"v": 1, "kind": "decision", "id": ITEM, "repo": "levain", "what": "Pick a host.\n\nLong text.",
        "options": [["a", "Host A"], ["b", "Host B"]], "rec": "a"}


def _sent(tmp_path, monkeypatch, endpoint, **kw):
    """The plaintext deliver() hands to the push request for one live subscription (no network, no keys)."""
    seen = []
    monkeypatch.setattr(PushStore, "live_subscriptions", lambda self, **k: [{"id": 1, "endpoint": endpoint, "device_id": "paired-x"}])
    monkeypatch.setattr(sender, "_prepare", lambda qdir, sub, data, topic, origin=None: seen.append(data) or None)
    args = dict(kind="attention", ref=ITEM, title="Needs you", body="levain: Pick a host.", url="/?view=needs", data=DATA)
    args.update(kw)
    sender.send(tmp_path, **args)
    return json.loads(seen[0])


def test_no_webpush_source_or_worker_names_band_answer() -> None:
    files = list((ROOT / "levain" / "webpush").rglob("*.py")) + list((ROOT / "levain" / "webpush").rglob("*.js"))
    assert any(f.name == "sw.js" for f in files) and any(f.name == "sender.py" for f in files)
    for f in files:
        assert "band_answer" not in f.read_text(), f


def test_a_card_payload_carries_no_actions_and_no_allow(tmp_path, monkeypatch) -> None:
    got = _sent(tmp_path, monkeypatch, "https://fcm.googleapis.com/fcm/send/abc", data={**DATA, "kind": "decision"})
    assert "actions" not in got and "Allow" not in json.dumps(got) and "allow" not in got
    for key in ("headline", "options", "rec", "subject", "v"):
        assert key not in ("actions",)
    assert got["subject"] == ITEM and got["rec"] == "a" and got["options"] == [["a", "Host A"], ["b", "Host B"]]
    assert got["headline"]


def test_the_declarative_payload_carries_no_actions_with_a_card() -> None:
    legacy = sender.build_payload("Needs you", "body", ITEM, "https://cockpit.example/?view=needs&item=" + ITEM, 7, 3,
                                  headline="Pick a host", options=[["a", "A"], ["b", "B"]], rec="a", subject_id=ITEM, version="v1")
    got = json.loads(sender.declarative_payload(legacy))
    assert got["web_push"] == 8030 and got["subject"] == ITEM
    assert "actions" not in got and "actions" not in got["notification"]


def test_a_huge_card_still_fits_and_the_fixed_members_survive() -> None:
    big = [[f"k{i}", "x" * 5000] for i in range(8)]
    data = sender.build_payload("T" * 5000, "b" * 9000, "tag", "/?view=needs&item=" + ITEM, 7, 3, headline="h" * 9000,
                                options=big, rec="k0", subject_id=ITEM, version="v" * 500)
    assert len(data) <= ece.MAX_PLAINTEXT
    got = json.loads(data)
    assert got["ping"] == 7 and got["badge"] == 3 and got["url"].endswith(ITEM) and got["tag"] == "tag" and got["title"]
    assert got["body"] and got["subject"] == ITEM
    ctrl = sender.build_payload("\x01" * 400, "b", "\x01" * 200, "/" + "\\" * 500, 7, 3, headline="\x01" * 3000,
                                options=[["a", "\x01" * 900]] * 8, rec="a", subject_id=ITEM, version="1")
    assert len(ctrl) <= ece.MAX_PLAINTEXT


def test_a_huge_card_declarative_payload_stays_in_one_request() -> None:
    legacy = sender.build_payload("Needs you", "b" * 3000, ITEM, "https://cockpit.example/?view=needs&item=" + ITEM, 7, 3,
                                  headline="h" * 300, options=[["a", "x" * 300]] * 8, rec="a", subject_id=ITEM, version="v1")
    out = sender.declarative_payload(legacy)
    assert len(out) <= ece.MAX_PLAINTEXT and json.loads(out)["title"] == "Needs you"


def test_the_card_fields_round_trip_and_unknown_ones_are_omitted() -> None:
    got = json.loads(sender.build_payload("T", "b", "tag", "/", 1, headline="Pick a host", options=[("a", "Host A"), {"key": "b", "text": "B"}],
                                          rec="b", subject_id=ITEM, version="abc123"))
    assert got["headline"] == "Pick a host" and got["options"] == [["a", "Host A"], ["b", "B"]] and got["rec"] == "b"
    assert got["subject"] == ITEM and got["v"] == "abc123"
    bare = json.loads(sender.build_payload("T", "b", "tag", "/", 1))
    assert not set(bare) & {"headline", "options", "rec", "subject", "v"}
    junk = json.loads(sender.build_payload("T", "b", "tag", "/", 1, headline=5, options="x", rec=None, subject_id="", version=3))
    assert not set(junk) & {"headline", "options", "rec", "subject", "v"}
    assert "rec" not in json.loads(sender.build_payload("T", "b", "tag", "/", 1, rec="a"))   # a rec with no option means nothing


def test_the_tap_url_names_the_subject_and_a_non_band_ping_carries_no_card(tmp_path, monkeypatch) -> None:
    got = _sent(tmp_path, monkeypatch, "https://fcm.googleapis.com/fcm/send/abc")
    assert got["url"] == f"/?view=needs&item={ITEM}" and got["subject"] == ITEM
    other = _sent(tmp_path, monkeypatch, "https://fcm.googleapis.com/fcm/send/abc", kind="notify", ref="notify:x", data=None)
    assert "subject" not in other and "options" not in other


def test_boot_registers_the_worker_for_a_paired_device_and_still_never_answers() -> None:
    js = (ROOT / "levain" / "templates" / "web" / "devices_panel.js").read_text()
    boot = js.split("async function registerWorkerAtBoot()")[1].split("function start()")[0]
    assert "PAIRED_MARK_KEY" in boot and 'register("/sw.js"' in boot and "requestPermission" not in boot and "subscribe(" not in boot
    assert "registerWorkerAtBoot();" in js.split("function start()")[1]
