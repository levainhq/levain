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
    b = _body(tmp_path, monkeypatch, {"deepgram": ["stt", "tts"]})        # inworld spent, then lost its key: still its own row
    v = _run(tmp_path, monkeypatch, [
        {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}},
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": "JSON.stringify({dict: document.querySelector('.vc-dictation').textContent,"
                 " spend: [...document.querySelectorAll('.vc-spend')].map((r) => r.textContent),"
                 " other: document.querySelectorAll('.vc-spend-other').length,"
                 " links: [...document.querySelectorAll('.vc-console')].map((a) => a.href),"
                 " wide: document.documentElement.scrollWidth - document.documentElement.clientWidth})"},
        {"shot": "/tmp/lv1108-39-after_390.png", "full": True},
    ], b)
    got = json.loads(v[1])
    assert "Deepgram (nova-3-stream)" in got["dict"], got
    assert len(got["spend"]) == 2 and not any("1 calls" in s for s in got["spend"]) and any("2 calls" in s for s in got["spend"]) and any("(1 call)" in s for s in got["spend"]), got
    inworld = [s for s in got["spend"] if s.startswith("Inworld")]
    assert len(inworld) == 1 and "no key now" in inworld[0] and "2 calls" in inworld[0], got
    assert not any("no key now" in s for s in got["spend"] if s.startswith("Deepgram")), got
    assert got["other"] == 0, got
    assert sorted(got["links"]) == ["https://console.deepgram.com/", "https://platform.inworld.ai/billing"], got
    assert got["wide"] <= 0, got


def _micros(usd: str) -> int:
    return round(float(usd) * 1_000_000)


def test_every_provider_with_spend_or_a_key_has_a_row_and_the_rows_add_up_to_the_caps_line(tmp_path, monkeypatch):
    b = _body(tmp_path, monkeypatch, {"deepgram": ["stt", "tts"]})         # inworld spent, then lost its key
    drawn = {r["name"]: r["spend"] for r in b["providers"] if r["spend"] is not None}
    assert set(drawn) == {"deepgram", "inworld"}, drawn                     # spend is set exactly for the rows drawn
    assert b["spend_other"] is None
    assert drawn["inworld"]["day_calls"] == 2 and drawn["inworld"]["day_est_usd"] == "0.01"
    for period in ("day", "month"):
        shown = sum(_micros(r[period + "_est_usd"]) for r in drawn.values())
        total = _micros(b["caps"]["spent_" + period + "_est"])
        assert abs(shown - total) <= 10_000 * len(drawn), (period, shown, total)   # display rounding only
        assert sum(r[period + "_calls"] for r in drawn.values()) == 3
    keyless = _body(tmp_path, monkeypatch, {})                              # a spent provider with no key is drawn too
    assert {r["name"] for r in keyless["providers"] if r["spend"] is not None} == {"deepgram", "inworld"}


def test_spend_from_a_provider_that_is_not_offered_is_the_other_row(tmp_path, monkeypatch):
    b = _body(tmp_path, monkeypatch, {"deepgram": ["stt"], "inworld": ["tts"]}, extra=[("acme", "tts", "m", 5, 20000)])
    other = b["spend_other"]
    assert other["day_calls"] == 1 and other["month_calls"] == 1 and other["day_est_usd"] == "0.02", other
    assert all(r["spend"] is not None for r in b["providers"] if r["name"] in ("deepgram", "inworld"))
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


def test_an_unreadable_key_log_is_the_reason_and_spend_is_still_by_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "_grants_for", lambda root: ({}, "unreadable"))
    meter = _seed(tmp_path / "voice.db")
    b = cv.serve_voice(tmp_path, meter, last_read={})
    assert b["mic_in"] == {"on": False, "reason": "unreadable"} and b["authority"] == "unreadable"
    assert b["spend_other"] is None and b["meter"] == "ok"
    assert {r["name"] for r in b["providers"] if r["spend"] is not None} == {"deepgram", "inworld"}


def test_no_stream_key_and_no_key_at_all_read_differently(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "STREAM_PROVIDERS", ("openai",))
    assert _body(tmp_path, monkeypatch, {"deepgram": ["stt"]}, setting=True)["mic_in"] == {"on": False, "reason": "no_stream_key"}
    assert _body(tmp_path, monkeypatch, {}, setting=True)["mic_in"] == {"on": False, "reason": "no_stt_key"}


@needs_browser
def test_the_panel_shows_a_not_offered_providers_spend_as_the_other_row(tmp_path, monkeypatch):
    b = _body(tmp_path, monkeypatch, {"deepgram": ["stt", "tts"], "inworld": ["tts"]}, extra=[("acme", "tts", "m", 5, 20000)])
    v = _run(tmp_path, monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": "JSON.stringify([...document.querySelectorAll('.vc-spend-other')].map((r) => r.textContent))"},
    ], b)
    got = json.loads(v[1])
    assert len(got) == 1 and "Other providers (not shown above)" in got[0] and "1 call" in got[0], got


@needs_browser
def test_with_the_ledger_unreadable_the_caps_stay_the_spent_figures_go_and_one_note_says_why(tmp_path, monkeypatch):
    b = _body(tmp_path, monkeypatch, {"deepgram": ["stt", "tts"]})
    bad = dict(b, meter="unreadable", spend_other=None, providers=[dict(r, spend=None) for r in b["providers"]],
               caps=dict(b["caps"], spent_day_est=None, spent_month_est=None, month_start=None, day_start=None))
    (tmp_path / "two").mkdir()
    v = _run(tmp_path / "two", monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": "JSON.stringify({caps: [...document.querySelectorAll('.vc-caps')].map((r) => r.textContent),"
                 " gone: [...document.querySelectorAll('.vc-spend-gone')].map((r) => r.textContent),"
                 " rows: [...document.querySelectorAll('.vc-spend-val')].map((r) => r.textContent),"
                 " all: document.querySelector('.vc-panel').textContent})"},
    ], bad)
    got = json.loads(v[1])
    assert len(got["caps"]) == 1 and "$" + b["caps"]["day_usd"] in got["caps"][0] and "$" + b["caps"]["month_usd"] in got["caps"][0], got
    assert "spent" not in got["caps"][0].lower() and "null" not in got["all"] and "?" not in got["all"], got
    assert got["gone"] == ["Spend unavailable: the ledger can't be read."], got
    assert got["rows"] == ["—"], got


@needs_browser
def test_focus_and_visibility_re_read_the_spend_after_the_first_read(tmp_path, monkeypatch):
    b = _body(tmp_path, monkeypatch, {"deepgram": ["stt", "tts"]})
    later = json.loads(json.dumps(b))
    for r in later["providers"]:
        if r["spend"]:
            r["spend"]["day_est_usd"] = "9.99"
    swap = "window.__later = %s; const real = window.fetch; window.fetch = (u, o) => new URL(String(u), location.href).pathname === '/cockpit/voice.json' ? Promise.resolve(new Response(JSON.stringify(window.__later), { status: 200, headers: { 'Content-Type': 'application/json' } })) : real(u, o); 1" % json.dumps(later)
    (tmp_path / "two").mkdir()
    v = _run(tmp_path / "two", monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": "document.querySelector('.vc-panel').textContent.indexOf('9.99')"},
        {"eval": "(() => { " + swap + "; })()"},
        {"eval": "(() => { window.dispatchEvent(new Event('focus')); return 1; })()"},
        {"until": "document.querySelector('.vc-panel').textContent.indexOf('$9.99') >= 0", "ms": 8000},
        {"eval": "document.querySelectorAll('.vc-spend').length"},
    ], b)
    assert v[1] == -1 and v[5] == 2, v


def test_no_stream_key_needs_a_keyed_provider_whose_grant_includes_stt(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "STREAM_PROVIDERS", ("openai",))
    # an stt-capable provider keyed for tts only is not a key that can stream, and not a key that can transcribe either
    assert _body(tmp_path, monkeypatch, {"deepgram": ["tts"]}, setting=True)["mic_in"] == {"on": False, "reason": "no_stt_key"}
    assert _body(tmp_path, monkeypatch, {"deepgram": ["tts"], "inworld": ["tts"]}, setting=True)["mic_in"] == {"on": False, "reason": "no_stt_key"}
    assert _body(tmp_path, monkeypatch, {"deepgram": ["stt", "tts"]}, setting=True)["mic_in"] == {"on": False, "reason": "no_stream_key"}


def test_a_path_that_cannot_be_resolved_is_a_meter_error_and_the_panel_says_unreadable(tmp_path, monkeypatch):
    import pytest

    class _Unresolvable(type(Path())):
        def resolve(self, *a, **k):
            raise OSError("too many levels of symbolic links")

    meter = _seed(tmp_path / "voice.db")
    meter.path = _Unresolvable(meter.path)
    with pytest.raises(vm.MeterError):
        meter.snapshot_if_present()
    monkeypatch.setattr(cv, "_grants_for", lambda root: ({"g": 1}, "ok"))
    monkeypatch.setattr(cv, "voice_key_for", lambda grants, name: _rec(["tts"]) if name == "inworld" else None)
    assert cv.serve_voice(tmp_path, meter, last_read={})["meter"] == "unreadable"
