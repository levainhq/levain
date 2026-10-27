"""CH-U1a mount: the thread routes through the real ``levain serve`` envelope (make_server), behind the consent body's
gate, over a fixture ``dispatch.db`` in the default consent directory (U1a's ChatService + ScriptedSession builder)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from levain.cockpit import threads
from levain.web_server import make_server
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_questions import _get_status
from tests.test_cockpit_threads import clock, seeded  # noqa: F401  (fixtures)


@pytest.fixture
def qdir(tmp_path, monkeypatch):
    """The default consent directory (``$LEVAIN_HOME/consent``), the one the server's consent reader names; U1a's
    ``seeded`` builds its dispatch.db here."""
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    d = tmp_path / "lh" / "consent"
    d.mkdir(parents=True, mode=0o700)
    return d


def test_the_routes_refuse_without_the_write_token_and_serve_the_record_with_it(tmp_path, seeded):
    _qdir, world = seeded
    _root, src, _ids = _install(tmp_path)
    one = f"/cockpit/thread/{world['thread']}.json"
    with _serve(src, write_token=TOKEN) as (base, _h):
        for path in (threads.THREADS_LIST_PATH, one):
            for token in (None, "wrong-token"):              # loopback included: no token, or a wrong one, is 403
                status, body = _get_status(base + path, token)
                assert status == 403 and body["error"] == "forbidden", (path, token, body)
                assert world["thread"] not in str(body)
        status, body = _get_status(base + threads.THREADS_LIST_PATH, TOKEN)
        assert status == 200
        [row] = body["threads"]
        assert row["thread_id"] == world["thread"] and row["launch_id"] == world["launch"]
        status, body = _get_status(base + one, TOKEN)
        assert status == 200 and body["thread_id"] == world["thread"]
        gseqs = [it["gseq"] for it in body["stream"]]
        assert len(gseqs) >= 5 and gseqs == sorted(gseqs) and len(set(gseqs)) == len(gseqs)   # gseq order, as served
        assert body["stream"][0]["kind"] == "message" and body["stream"][0]["unverified"] is True
        assert "card" not in str(body) and "req-77" not in str(body)      # the mount adds no card field
        status, body = _get_status(base + "/cockpit/thread/th-" + "0" * 32 + ".json", TOKEN)
        assert status == 404 and body["error"] == "not_found"


def test_a_server_with_no_write_token_refuses_the_record(tmp_path, seeded):
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (base, _h):                          # `levain serve` without --write: no token can match
        status, _body = _get_status(base + threads.THREADS_LIST_PATH, None)
        assert status == 403


def test_no_dispatch_store_is_an_empty_list_and_nothing_is_created(tmp_path, qdir):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _get_status(base + threads.THREADS_LIST_PATH, TOKEN)
    assert (status, body) == (200, {"threads": [], "truncated": False})
    assert not (qdir / "dispatch.db").exists()


def test_the_view_script_is_app_shell_and_the_routes_are_reserved(tmp_path):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        with urllib.request.urlopen(base + "/thread_view.js", timeout=10) as r:  # noqa: S310 - loopback, token-free
            assert r.status == 200 and r.headers["Content-Type"].startswith("text/javascript")
            assert b"LevainThreadView" in r.read()
        with urllib.request.urlopen(base + "/", timeout=10) as r:  # noqa: S310 - loopback
            assert b'<script src="/thread_view.js"></script>' in r.read()
    for path in (threads.THREADS_LIST_PATH, "/cockpit/thread/x.json", "/thread_view.js", "/cockpit/note/9.json"):
        with pytest.raises(ValueError):
            make_server(src, port=0, extra_json={path: lambda: {}})


def test_a_busy_server_answers_503_not_a_dead_connection(tmp_path, seeded):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, httpd):
        taken = 0
        while httpd.request_gate.acquire(blocking=False):
            taken += 1
        try:
            req = urllib.request.Request(base + threads.THREADS_LIST_PATH, headers={"X-Levain-Write-Token": TOKEN})
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=10)  # noqa: S310 - loopback
            assert e.value.code == 503
        finally:
            for _ in range(taken):
                httpd.request_gate.release()


# the thread reads the page has made, from resource timing (thread_view.js binds fetch at mount, so a wrapper installed
# later would never see them)
_READS = "performance.getEntriesByType('resource').filter(e => e.name.indexOf('/cockpit/thread') > 0).length"
_COUNT = f"performance.setResourceTimingBufferSize(10000); window.__th = () => {_READS}; 1"
_WAIT = "new Promise(r => setTimeout(() => r(window.__th()), 2600))"


@needs_browser
def test_the_build_tab_zone_reads_only_while_build_is_shown_and_hides_without_a_token(tmp_path, seeded):
    _qdir, world = seeded
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        out = _drive(tmp_path, base + "/", [
            {"until": "document.querySelector('.th-zone')"},
            {"eval": "document.querySelector('.th-zone').hidden"},                      # no token held: hidden
            {"eval": f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); location.reload(); 1"},
            {"until": "document.querySelector('.th-zone') && document.querySelector('[data-mode-tab=board]')"},
            {"eval": _COUNT},
            {"eval": "document.querySelector('[data-mode-tab=board]').click(); 1"},
            {"eval": _WAIT},                                                            # Board: no thread read
            {"eval": "document.querySelector('[data-mode-tab=build]').click(); 1"},
            {"until": "document.querySelector('.th-zone .tv-row')"},
            {"eval": "document.querySelector('.th-zone .tv-row').getAttribute('data-thread')"},
            {"eval": "document.querySelector('.th-zone').hidden"},
            {"eval": "document.querySelector('[data-mode-tab=board]').click(); window.__th0 = window.__th(); 1"},
            {"eval": "new Promise(r => setTimeout(() => r(window.__th() - window.__th0), 4500))"},   # left Build: polling stopped
        ])
    v = out["values"]
    hidden_without_token, board_reads, row, hidden_with_token, after_leaving = v[1], v[6], v[9], v[10], v[12]
    assert hidden_without_token is True
    assert board_reads == 0
    assert row == world["thread"] and hidden_with_token is False
    assert after_leaving == 0


@needs_browser
def test_a_token_changed_in_this_page_updates_the_zone_without_a_reload(tmp_path, seeded):
    """``storage`` never fires in the document that changed the token, so the zone listens for the page's own event:
    a rejection that drops the token hides the zone and stops the poll; a stored token shows it and starts it."""
    _qdir, world = seeded
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        out = _drive(tmp_path, base + "/", [
            {"until": "document.querySelector('[data-mode-tab=board]')"},
            {"eval": f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); location.reload(); 1"},
            {"until": "document.querySelector('.th-zone') && document.querySelector('[data-mode-tab=build]')"},
            {"eval": _COUNT},
            {"eval": "document.querySelector('[data-mode-tab=build]').click(); 1"},
            {"until": "document.querySelector('.th-zone .tv-row')"},
            # a rotated token: the page's own read path meets the rejection and drops the stored token in this document
            {"eval": "localStorage.setItem('levain_write_token', 'rotated-away'); window.prompt = () => ''; "
                     "LevainCockpitTransport.readJson('/cockpit/threads.json').catch(() => 0).then(() => 1)"},
            {"eval": "localStorage.getItem('levain_write_token')"},
            {"eval": "document.querySelector('.th-zone').hidden"},
            {"eval": "window.__th0 = window.__th(); new Promise(r => setTimeout(() => r(window.__th() - window.__th0), 4500))"},
        ])
    v = out["values"]
    token_after_rejection, hidden, reads_after = v[7], v[8], v[9]
    assert token_after_rejection in (None, "")
    assert hidden is True
    assert reads_after == 0
