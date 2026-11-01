"""The tap trace reports itself: the paired page posts its on-device ring to /webpush/trace and the operator reads the
newest report from the laptop (levain.webpush.tap_report). The ring's own writers are pinned in test_tap_trace.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain.web_server import load_web_asset
from levain.webpush import tap_report
from tests.test_cockpit_webpush import _signed, rig  # noqa: F401 (rig is a fixture)
from tests.test_cockpit_pairing_http import PROXY, T, _call, _pair
from tests.test_pair_card_w1b import _drive, needs_browser
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import _serve


def test_clean_keeps_short_scalars_and_refuses_a_non_list() -> None:
    assert tap_report.clean({"t": 1}) is None and tap_report.clean([1]) is None
    got = tap_report.clean([{"t": 5, "step": "x" * 500, "n": {"a": [1, 2]}, "ok": True, "none": None}])
    assert got is not None and len(got[0]["step"]) == tap_report._VALUE_CLIP
    assert got[0]["n"] == '{"a":[1,2]}' and got[0]["ok"] is True and got[0]["none"] is None
    kept = tap_report.clean([{"t": i} for i in range(500)])
    assert len(kept) == tap_report.MAX_ENTRIES and kept[-1]["t"] == 499      # the newest steps stay


def test_record_keeps_only_the_newest_reports_and_the_reader_prints_steps_in_time_order(tmp_path: Path, capsys) -> None:
    for i in range(tap_report.KEEP + 5):
        assert tap_report.record(tmp_path, "paired-x", [{"t": i, "src": "sw", "step": f"s{i}"}], page="/") <= tap_report.KEEP
    got = tap_report.reports(tmp_path)
    assert len(got) == tap_report.KEEP and got[-1]["entries"][0]["step"] == f"s{tap_report.KEEP + 4}"
    tap_report.record(tmp_path, None, [{"t": 2000, "src": "page", "step": "late"}, {"t": 1000, "src": "sw", "step": "early", "url": "/"}])
    assert tap_report.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.index("sw/early") < out.index("page/late") and "url=/" in out
    assert tap_report.main([str(tmp_path / "none")]) == 1


def test_record_replaces_the_file_whole_privately_and_answers_busy_when_the_lock_is_held(tmp_path: Path, monkeypatch) -> None:
    import fcntl
    import os
    import stat

    tap_report.record(tmp_path, None, [{"t": 1, "step": "a"}])
    assert sorted(p.name for p in tmp_path.iterdir()) == [tap_report.FILE, tap_report.FILE + ".lock"]   # no temp file left
    assert stat.S_IMODE(os.stat(tmp_path / tap_report.FILE).st_mode) == 0o600
    (tmp_path / tap_report.FILE).write_text('[1]\nnot json\n{"entries": null, "at": "x"}\n', encoding="utf-8")
    assert len(tap_report.reports(tmp_path)) == 1 and "report x" in tap_report.render(tap_report.reports(tmp_path)[0])
    monkeypatch.setattr(tap_report, "LOCK_WAIT_SECS", 0.1)
    held = os.open(tmp_path / (tap_report.FILE + ".lock"), os.O_RDWR)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        with pytest.raises(tap_report.TraceBusy):
            tap_report.record(tmp_path, None, [{"t": 2}])
    finally:
        os.close(held)
    assert tap_report.record(tmp_path, None, [{"t": 3}]) == 4   # three kept raw lines (two unreadable) plus the new row


def test_the_trace_route_answers_503_when_busy_or_unwritable(tmp_path: Path, monkeypatch) -> None:
    from levain.webpush import routes

    body = json.dumps({"entries": [{"t": 1}]}).encode()
    call = lambda: routes.handle("POST", "/webpush/trace", {}, body, queue_dir=tmp_path, authorized=True, device_id="paired-x", journal=None)
    assert call()[0] == 200
    monkeypatch.setattr(tap_report, "record", lambda *a, **k: (_ for _ in ()).throw(tap_report.TraceBusy()))
    assert call()[0] == 503
    monkeypatch.setattr(tap_report, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert call()[0] == 503


def test_the_trace_route_needs_the_credential_and_files_a_report_the_reader_prints(rig) -> None:
    src, qdir = rig
    body = {"page": "/", "entries": [{"t": 1700000000000, "src": "sw", "step": "notificationclick", "ping": 4}]}
    with _signed(src, qdir) as (port, _):
        for hdrs in (None, PROXY):
            assert _call(port, "POST", "/webpush/trace", body, headers=hdrs)[0] == 403, hdrs
        assert tap_report.reports(qdir) == []
        dev, tok = _pair(port)
        st, out, _ = _call(port, "POST", "/webpush/trace", body, token=tok)
        assert st == 200 and out == {"kept": 1}, out
        assert _call(port, "POST", "/webpush/trace", {"entries": "no"}, token=tok)[0] == 400
        assert _call(port, "POST", "/webpush/trace", {"entries": [1]}, token=tok)[0] == 400
    [rep] = tap_report.reports(qdir)
    assert rep["device"].endswith(dev) and rep["page"] == "/" and rep["entries"][0]["step"] == "notificationclick"
    assert "sw/notificationclick" in tap_report.render(rep)


def test_every_page_that_loads_the_tap_script_reports_with_the_key_it_holds_and_pair_card_does_not() -> None:
    js = load_web_asset("sw_open.js")
    assert '"/webpush/trace"' in js and '"X-Levain-Write-Token": key' in js and "TextEncoder" in js
    assert "reportTrace" not in load_web_asset("pair_card.js")
    for page in ("dashboard.html", "pair.html", "sign.html"):
        assert '<script src="/sw_open.js"></script>' in load_web_asset(page), page


@needs_browser
def test_the_trace_is_reported_once_per_new_head_and_again_after_a_refusal(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, _h):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/", [
            {"until": "window.levainTraceReport && performance.now() > 3500", "ms": 15000},   # the page's own first-run timer has fired (no key yet)
            {"eval": "(async () => { localStorage.removeItem('levain_trace_sent'); localStorage.setItem('levain_write_token', 'TKN');"
                     " window.__posts = []; window.__mode = 200; const f = window.fetch.bind(window);"
                     " window.fetch = (u, o) => { if (String(u) !== '/webpush/trace') return f(u, o);"
                     "   window.__posts.push({ tok: o.headers['X-Levain-Write-Token'], body: JSON.parse(o.body) });"
                     "   return Promise.resolve(new Response('{}', { status: window.__mode })); };"
                     " const c = await caches.open('levain-trace');"
                     " for (let i = 0; i < 3; i++) await c.put('/__levain_trace/9999999999999-z00' + i,"      # sorts after every real step
                     "   new Response(JSON.stringify({ t: 100 + i, src: 'sw', step: 's' + i })));"        # t is far in the past: key order decides
                     " await c.put('/__levain_trace/junk', new Response('null')); return 1; })()"},
            {"eval": "window.__mode = 500; window.levainTraceReport().then(() => window.__posts.length)"},   # refused: not marked sent
            {"eval": "window.__mode = 200; window.levainTraceReport().then(() => window.__posts.length)"},   # resent
            {"eval": "window.levainTraceReport().then(() => window.__posts.length)"},                        # same head: silent
            {"eval": "window.__mode = 400; localStorage.removeItem('levain_trace_sent'); window.levainTraceReport().then(() => window.__posts.length)"},
            {"eval": "window.levainTraceReport().then(() => window.__posts.length)"},                        # a 400 settles it: no retry loop
            {"eval": "JSON.stringify(window.__posts[1])"},
        ])
    v = out["values"]
    assert [int(v[2]), int(v[3]), int(v[4]), int(v[5]), int(v[6])] == [1, 2, 2, 3, 3], v
    post = json.loads(v[7])
    assert post["tok"] == "TKN" and [e["step"] for e in post["body"]["entries"] if e["step"] in ("s0", "s1", "s2")] == ["s0", "s1", "s2"], post   # the page's own steps ride along
