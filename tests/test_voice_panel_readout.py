"""The Settings Voice block's read-only readout (levain 74, key 1108-39): the dictation provider and model the SERVER picked
(or why it is off), per-provider spend from the ledger, and where to reload credit. Everything runs on a scratch ledger in
tmp; nothing here resolves a path under the operator's sign home or flow's state."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from levain.cockpit import voice as cv
from levain.voice import meter as vm
from tests.test_pair_card_w1b import needs_browser
from tests.test_voice_player_browser import READY_BLOCK, _fixture, _run

NOW = datetime(2026, 11, 8, 17, 0, tzinfo=timezone.utc)          # one frozen moment, shared by the seed and the meter


def _rec(uses):
    return {"id": "vk-x", "uses": uses}


def _seed(path: Path, extra=()) -> vm.Meter:
    """The scratch ledger holds exactly these rows after every call (a second call replaces, never adds), the connection is
    closed, and the meter's clock is NOW, the moment the rows are stamped with."""
    meter = vm.Meter(path, clock=lambda: NOW)
    meter.spent()                                        # creates the schema in the scratch file
    now = NOW.strftime(vm._STAMP)
    rows = [("deepgram", "stt", "nova-3-stream", 9, 1155), ("inworld", "tts", "inworld-tts-2-flash", 184, 2760),
            ("inworld", "tts", "inworld-tts-2-flash", 160, 2400), *extra]
    with closing(sqlite3.connect(path)) as c:
        with c:
            c.execute("DELETE FROM calls")
            for p, u, m, n, cost in rows:
                c.execute("INSERT INTO calls (at, provider, use, model, units, cost_micros, status) VALUES (?,?,?,?,?,?,'ok')",
                          (now, p, u, m, n, cost))
    return meter


def _body(tmp_path, monkeypatch, keys, setting=None, extra=()):
    monkeypatch.setattr(cv, "_grants_for", lambda root: ({"g": 1}, "ok"))
    monkeypatch.setattr(cv, "voice_key_for", lambda grants, name: _rec(keys[name]) if name in keys else None)
    meter = _seed(tmp_path / "voice.db", extra)
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


@needs_browser
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
    assert len(got["spend"]) == 2 and not any("1 calls" in s for s in got["spend"]) and any("2 calls" in s for s in got["spend"]) and any("(1 call)" in s for s in got["spend"]), got
    assert sorted(got["links"]) == ["https://console.deepgram.com/", "https://platform.inworld.ai/billing"], got
    assert got["wide"] <= 0, got


def _micros(usd: str) -> int:
    return round(float(usd) * 1_000_000)


def test_rows_add_up_to_the_caps_line_and_unkeyed_spend_is_one_other_row(tmp_path, monkeypatch):
    b = _body(tmp_path, monkeypatch, {"deepgram": ["stt", "tts"]})         # inworld spent, then lost its key
    other = b["spend_other"]
    assert other["day_calls"] == 2 and other["month_calls"] == 2 and other["day_est_usd"] == "0.01", other
    rows = [r["spend"] for r in b["providers"] if r["key"] == "set"] + [other]
    for period in ("day", "month"):
        shown = sum(_micros(r[period + "_est_usd"]) for r in rows)
        total = _micros(b["caps"]["spent_" + period + "_est"])
        assert abs(shown - total) <= 10_000 * len(rows), (period, shown, total)   # display rounding only
        assert sum(r[period + "_calls"] for r in rows) == 3
    assert _body(tmp_path, monkeypatch, {"deepgram": ["stt"], "inworld": ["tts"]})["spend_other"] is None


def test_the_totals_and_the_rows_come_from_one_read(tmp_path, monkeypatch):
    meter = _seed(tmp_path / "voice.db")
    calls = []
    real = meter._clock
    meter._clock = lambda: (calls.append(1), real())[1]
    spend, by = meter.snapshot_if_present()
    assert len(calls) == 1
    assert spend.month_micros == sum(v["month"][1] for v in by.values()) == 6315


def test_an_unreadable_ledger_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "_grants_for", lambda root: ({"g": 1}, "ok"))
    monkeypatch.setattr(cv, "voice_key_for", lambda grants, name: _rec(["tts"]) if name == "inworld" else None)
    (tmp_path / "voice.db").write_bytes(b"not a ledger at all" * 50)
    b = cv.serve_voice(tmp_path, vm.Meter(tmp_path / "voice.db"), last_read={})
    assert b["meter"] == "unreadable" and b["caps"]["spent_day_est"] is None and b["spend_other"] is None
    assert [r["spend"] for r in b["providers"]] == [None] * len(b["providers"])


def test_an_unreadable_key_log_is_the_reason_and_spend_is_not_split(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "_grants_for", lambda root: ({}, "unreadable"))
    meter = _seed(tmp_path / "voice.db")
    b = cv.serve_voice(tmp_path, meter, last_read={})
    assert b["mic_in"] == {"on": False, "reason": "unreadable"} and b["authority"] == "unreadable"
    assert b["spend_other"] is None and b["meter"] == "ok"


def test_no_stream_key_and_no_key_at_all_read_differently(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "STREAM_PROVIDERS", ("openai",))
    assert _body(tmp_path, monkeypatch, {"deepgram": ["stt"]}, setting=True)["mic_in"] == {"on": False, "reason": "no_stream_key"}
    assert _body(tmp_path, monkeypatch, {}, setting=True)["mic_in"] == {"on": False, "reason": "no_stt_key"}


@needs_browser
def test_the_panel_says_when_spend_cannot_be_read_and_shows_the_other_row(tmp_path, monkeypatch):
    b = _body(tmp_path, monkeypatch, {"deepgram": ["stt", "tts"]})
    bad = dict(b, meter="unreadable", authority="ok", spend_other=None,
               providers=[dict(r, spend=None) for r in b["providers"]])
    v = _run(tmp_path, monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": "JSON.stringify({other: [...document.querySelectorAll('.vc-spend-other')].map((r) => r.textContent),"
                 " why: document.querySelector('.vc-dict-val') && document.querySelector('.vc-dict-val').textContent})"},
    ], b)
    got = json.loads(v[1])
    assert len(got["other"]) == 1 and "Other providers (no key now)" in got["other"][0] and "2 calls" in got["other"][0], got
    v = _run(tmp_path / "two", monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": "JSON.stringify([...document.querySelectorAll('.vc-spend-gone, .vc-spend-val')].map((r) => r.textContent))"},
    ], bad)
    assert any("the ledger can't be read" in t for t in json.loads(v[1])), v
