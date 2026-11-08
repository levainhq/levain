"""The Settings Voice block's read-only readout (levain 74, key 1108-39): the dictation provider and model the SERVER picked
(or why it is off), per-provider spend from the ledger, and where to reload credit. Everything runs on a scratch ledger in
tmp; nothing here resolves a path under the operator's sign home or flow's state."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from levain.cockpit import voice as cv
from levain.voice import meter as vm
from tests.test_voice_player_browser import READY_BLOCK, _run


def _rec(uses):
    return {"id": "vk-x", "uses": uses}


def _seed(path: Path) -> vm.Meter:
    meter = vm.Meter(path)
    meter.spent()                                        # creates the schema in the scratch file
    now = datetime.now(timezone.utc).strftime(vm._STAMP)
    rows = [("deepgram", "stt", "nova-3-stream", 9, 1155), ("inworld", "tts", "inworld-tts-2-flash", 184, 2760),
            ("inworld", "tts", "inworld-tts-2-flash", 160, 2400)]
    with sqlite3.connect(path) as c:
        for p, u, m, n, cost in rows:
            c.execute("INSERT INTO calls (at, provider, use, model, units, cost_micros, status) VALUES (?,?,?,?,?,?,'ok')",
                      (now, p, u, m, n, cost))
    return meter


def _body(tmp_path, monkeypatch, keys, setting=None):
    monkeypatch.setattr(cv, "_grants_for", lambda root: ({"g": 1}, "ok"))
    monkeypatch.setattr(cv, "voice_key_for", lambda grants, name: _rec(keys[name]) if name in keys else None)
    meter = _seed(tmp_path / "voice.db")
    assert "levain-sign" not in str(meter.path) and "state/bridge" not in str(meter.path)
    return cv.serve_voice(tmp_path, meter, last_read={}, premium_stt=setting)


def test_dictation_names_the_servers_pick_and_spend_is_per_provider(tmp_path, monkeypatch):
    b = _body(tmp_path, monkeypatch, {"deepgram": ["stt", "tts"], "inworld": ["tts"]})
    assert b["mic_in"] == {"on": True, "provider": "deepgram", "model": "nova-3-stream"}
    rows = {r["name"]: r for r in b["providers"]}
    assert rows["inworld"]["spend"]["day_calls"] == 2 and rows["inworld"]["spend"]["month_calls"] == 2
    assert rows["deepgram"]["spend"]["day_calls"] == 1
    assert rows["deepgram"]["console_url"] == "https://console.deepgram.com"
    assert rows["inworld"]["console_url"] == "https://platform.inworld.ai/billing"


def test_dictation_off_says_why(tmp_path, monkeypatch):
    assert _body(tmp_path, monkeypatch, {"inworld": ["tts"]})["mic_in"] == {"on": False, "reason": "no_stt_key"}
    assert _body(tmp_path, monkeypatch, {"deepgram": ["stt"]}, setting=False)["mic_in"] == {"on": False, "reason": "turned_off"}


def test_the_panel_shows_dictation_spend_and_console_links(tmp_path, monkeypatch):
    b = _body(tmp_path, monkeypatch, {"deepgram": ["stt", "tts"], "inworld": ["tts"]})
    v = _run(tmp_path, monkeypatch, [
        {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}},
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": "JSON.stringify({dict: document.querySelector('.vc-dictation').textContent,"
                 " spend: [...document.querySelectorAll('.vc-spend')].map((r) => r.textContent),"
                 " links: [...document.querySelectorAll('.vc-console')].map((a) => a.href),"
                 " wide: document.documentElement.scrollWidth - document.documentElement.clientWidth})"},
        {"shot": "/tmp/lv1108-39-after_390.png", "full": True},
    ], b)
    got = json.loads(v[1])
    assert "Deepgram (nova-3-stream)" in got["dict"], got
    assert len(got["spend"]) == 2 and any("2 calls" in s for s in got["spend"]) and any("1 calls" in s for s in got["spend"]), got
    assert sorted(got["links"]) == ["https://console.deepgram.com/", "https://platform.inworld.ai/billing"], got
    assert got["wide"] <= 0, got
