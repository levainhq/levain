"""W2 session 1: the summary read (GET /cockpit/needs.json) and the cockpit page recording band.json only when the queue
changed. Run first (1031-42 w2): two tabs on one credential; a new item arrived and the second tab's look recorded a newer
view, so the first tab's "1a", typed against an item that had not moved, was refused as stale and nothing was recorded.
These are the plan's negatives for what this session built: a summary records nothing (mutation-checked below), a view
then a summary then an answer to that view succeeds, two credentials never share presentation state."""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from levain import band_view
from levain import web_server
from levain.consent import band
from levain.consent.questions import QuestionStore
from tests.test_band_pages import BAND_ROWS, DESKTOP, HISTORY
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _get, _install, _post, _serve

OPTS = [("fix", "fixed 30 s"), ("exp", "exponential"), ("none", "fail loud")]
VIEWER = "cockpit:browser"
IPAD = "ipad-token"


@pytest.fixture
def lh(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    (tmp_path / "lh" / "consent").mkdir(parents=True, mode=0o700)
    return tmp_path / "lh" / "consent"


@pytest.fixture(autouse=True)
def _no_delivery(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(band, "deliver_answer", lambda store, ref, **kw: (
        calls.append(ref), type("D", (), {"route": "hook", "reason": "next hook delivers it"})())[1])
    return calls


def _file(store: QuestionStore, what: str):
    return store.file(agent="claude_code", agent_session_id="s1", what=what, options=OPTS, rec="exp",
                      blocks="the feed PR", cwd="/w")


def _views(store: QuestionStore, viewer: str) -> tuple[int, int | None]:
    """The viewer's band_views row count and its newest seq."""
    try:
        with sqlite3.connect(store.path) as c:
            n, top = c.execute("SELECT count(*), max(seq) FROM band_views WHERE viewer = ?", (viewer,)).fetchone()
            return int(n), top
    except sqlite3.OperationalError:               # no band_views table yet: no view was ever recorded
        return 0, None


def _band_answer(base: str, params: dict, token: str = TOKEN):
    return _post(f"{base}/cockpit/verb", {"verb": "band_answer", "panel_id": "decisions", "params": params}, token)


def _summary_records_nothing(base: str, store: QuestionStore, viewer: str = VIEWER, token: str = TOKEN) -> dict:
    """GET needs.json and assert the viewer's band_views row count and newest seq did not move. The caller has changed the
    queue since the viewer's last view, so a summary that recorded would add a row (record_view's dedupe would not hide it)."""
    before = _views(store, viewer)
    got = _get(f"{base}/cockpit/needs.json", token)
    assert _views(store, viewer) == before, "the summary recorded a band view"
    return got


def _walk_keys(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _walk_keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _walk_keys(v)}
    return set()


def test_a_summary_call_leaves_the_viewers_band_views_unchanged(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q1 = _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        _get(f"{base}/cockpit/band.json", TOKEN)
        assert _views(store, VIEWER)[0] == 1
        q2 = _file(store, "Ship the sweep fix tonight?")          # the queue moved: a recording read would add a row
        got = _summary_records_nothing(base, store)
        assert got["count"] == 2
        assert [i["key"] for i in got["items"]] == [{"source": "question", "id": q1.id},
                                                     {"source": "question", "id": q2.id}]
        assert [i["kind"] for i in got["items"]] == ["information", "information"]
        assert got["items"][1]["headline"] == "Ship the sweep fix tonight?"
        # nothing in it can answer: no view, no version, no actions, no options
        assert not _walk_keys(got) & {"view_id", "version", "actions", "options", "subject"}
        band_json = _get(f"{base}/cockpit/band.json", TOKEN)
        assert got["queue"] == band_json["queue"]
        assert got["seen_through"] == band_json["seen_through"]      # the mark a page compares; it binds nothing
        assert _views(store, VIEWER)[0] == 2                      # the band.json read after it did record: the probe can move


def test_the_summary_check_fails_against_a_summary_that_records(tmp_path, lh, monkeypatch):
    """The mutation check of the test above: a summary that records a view for its caller is caught."""
    real = band_view.summary

    def recording(store, **kw):
        band_view.band_payload(store, VIEWER)
        return real(store, **kw)
    monkeypatch.setattr(band_view, "summary", recording)
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        _get(f"{base}/cockpit/band.json", TOKEN)
        _file(store, "Ship the sweep fix tonight?")
        with pytest.raises(AssertionError, match="recorded a band view"):
            _summary_records_nothing(base, store)


def test_a_band_view_then_a_summary_then_an_answer_to_that_view_succeeds(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        shown = _get(f"{base}/cockpit/band.json", TOKEN)
        _summary_records_nothing(base, store)
        s, body = _band_answer(base, {"text": "1c", "store_id": shown["store_id"], "view_id": shown["view_id"]})
        assert s == 200 and body["recorded"] == ["1c"], body
    assert store.get(q.n).answer.value == "none"


def test_two_credentials_never_share_presentation_state(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q1 = _file(store, "Which retry policy?")
    with _serve(src, surface_tokens={"browser": TOKEN, "ipad": IPAD}) as (base, _h):
        mine = _get(f"{base}/cockpit/band.json", TOKEN)
        _file(store, "Ship the sweep fix tonight?")
        before = _views(store, VIEWER)
        theirs = _get(f"{base}/cockpit/band.json", IPAD)           # the other credential's look, after the queue moved
        assert _views(store, "cockpit:ipad")[0] == 1
        assert _views(store, VIEWER) == before                    # it recorded nothing for this one
        assert theirs["view_id"] != mine["view_id"]
        # an answer carries only its own credential's view: the other credential's binding is refused, nothing recorded
        s, body = _band_answer(base, {"text": "1a", "store_id": theirs["store_id"], "view_id": theirs["view_id"]})
        assert s == 409 and body["error"] == "stale_band", body
        assert store.get(q1.n).answer is None
        # and the other credential's newer look did not make this one's stale
        s, body = _band_answer(base, {"text": "1a", "store_id": mine["store_id"], "view_id": mine["view_id"]})
        assert s == 200 and body["recorded"] == ["1a"], body
    assert store.get(q1.n).answer.value == "fix"


def test_the_summary_rides_the_bands_gate_and_says_when_there_is_no_store(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(f"{base}/cockpit/needs.json")
        assert e.value.code == 403
        req = urllib.request.Request(f"{base}/cockpit/needs.json", method="HEAD", headers={"X-Levain-Write-Token": TOKEN})
        with urllib.request.urlopen(req, timeout=10) as r:      # noqa: S310 - loopback
            assert r.status == 200 and r.read() == b""
    assert _views(store, VIEWER) == (0, None)
    with _serve(src, write_token=TOKEN) as (base, httpd):
        httpd.question_store = None
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(f"{base}/cockpit/needs.json", TOKEN)
        assert e.value.code == 404 and json.loads(e.value.read())["message"] == "no question store"


def test_an_embedder_cannot_serve_the_summary_path(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    with pytest.raises(ValueError):
        web_server.make_server(src, port=0, extra_json={"/cockpit/needs.json": lambda: b"{}"})
    assert web_server._NEEDS_PATH in web_server._RESERVED_PATHS


def test_the_summarys_mark_moves_when_the_bands_does_and_the_page_compares_it(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        first = _get(f"{base}/cockpit/needs.json", TOKEN)
        _file(store, "Ship the sweep fix tonight?")
        second = _get(f"{base}/cockpit/needs.json", TOKEN)
    assert isinstance(first["seen_through"], int) and second["seen_through"] > first["seen_through"]
    boot = (Path(web_server.__file__).parent / "templates" / "web" / "dashboard_boot.js").read_text()
    assert "sum.seen_through !== shown.seen_through" in boot       # a moved mark makes the page re-read band.json
    assert "bandSummary," not in boot                              # the dead render opt is gone


# --- the page: band.json recorded only when the queue changed ----------------------------------------------------------

def _count_reads(monkeypatch) -> dict:
    """Count the band reads the server answers, by route."""
    seen = {"band": 0, "summary": 0}
    real = web_server._Handler._serve_band

    def counted(self, *, head, summary=False):
        if not head:
            seen["summary" if summary else "band"] += 1
        return real(self, head=head, summary=summary)
    monkeypatch.setattr(web_server._Handler, "_serve_band", counted)
    return seen


VISIBLE = "(document.dispatchEvent(new Event('visibilitychange')), 1)"


@needs_browser
def test_the_page_reads_band_json_again_only_when_the_queue_changed(tmp_path: Path, lh, monkeypatch) -> None:
    seen = _count_reads(monkeypatch)
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    _file(store, "Which retry policy?")
    marks: dict = {}
    with _serve(src, write_token=TOKEN) as (base, _h):
        import threading
        import time as _t

        def later() -> None:
            # the page has drawn the band and re-read twice with the queue unchanged; then the queue moves
            deadline = _t.monotonic() + 60
            while seen["band"] + seen["summary"] < 3 and _t.monotonic() < deadline:
                _t.sleep(0.1)
            _t.sleep(1.5)
            marks["band_before"], marks["views_before"] = seen["band"], _views(store, VIEWER)
            _file(store, "Ship the sweep fix tonight?")
            marks["filed"] = True
        t = threading.Thread(target=later, daemon=True)
        t.start()
        _drive(tmp_path, f"{base}/#write_token={TOKEN}", [
            DESKTOP,
            {"until": "document.querySelector('#ck-needs .needs-history') && 1", "ms": 30000},
            {"eval": HISTORY},
            {"until": f"{BAND_ROWS} === 1 && 1", "ms": 30000},
            {"eval": "new Promise(r => setTimeout(r, 2500)).then(() => 1)"},     # past the core's own 2 s share of a read
            {"eval": VISIBLE},
            {"eval": "new Promise(r => setTimeout(r, 2500)).then(() => 1)"},
            {"eval": VISIBLE},
            {"eval": "new Promise(r => setTimeout(r, 6000)).then(() => 1)"},     # the queue moves in here
            {"do": VISIBLE, "until": f"{BAND_ROWS} === 2 && 1", "ms": 30000, "every": 2500},
        ])
        t.join(timeout=5)
    assert marks.get("filed"), marks
    assert marks["band_before"] == 1, (marks, seen)          # the re-reads with the queue unchanged took the summary only
    assert marks["views_before"][0] == 1, marks
    assert seen["band"] == 2, seen                           # the change was read from band.json once more
    assert _views(store, VIEWER)[0] == 2


@needs_browser
def test_a_stale_refusal_drops_the_held_read_so_the_redraw_is_fresh(tmp_path: Path, lh) -> None:
    """A newer view of the same credential with the same queue (another tab, or a queue that moved and moved back) makes
    the page's held view stale while the summary still matches it: the refusal must make the next read a band.json read,
    or the page would hand back the same stale binding forever."""
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q1 = _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        import threading

        def newer_view() -> None:
            deadline = time.monotonic() + 60
            while _views(store, VIEWER)[0] < 1 and time.monotonic() < deadline:
                time.sleep(0.1)
            with sqlite3.connect(store.path) as c:
                seen, opened, sid = c.execute("SELECT seen_through, open_list, store_id FROM band_views WHERE viewer = ? "
                                              "ORDER BY seq DESC LIMIT 1", (VIEWER,)).fetchone()
                c.execute("INSERT INTO band_views (viewer, seen_through, open_list, store_id, at, gen) VALUES (?, ?, ?, ?, ?, ?)",
                          (VIEWER, seen, opened, sid, "2026-10-31T00:00:00Z", time.time_ns()))
        t = threading.Thread(target=newer_view, daemon=True)
        t.start()
        out = _drive(tmp_path, f"{base}/#write_token={TOKEN}", [
            DESKTOP,
            {"until": "document.querySelector('#ck-needs .needs-history') && 1", "ms": 30000},
            {"eval": HISTORY},
            {"until": f"{BAND_ROWS} === 1 && 1", "ms": 30000},
            {"eval": "new Promise(r => setTimeout(r, 3000)).then(() => 1)"},     # the newer view lands
            {"eval": "(() => { const b = document.querySelector('.band-box'); b.value = '1a'; b.dispatchEvent(new Event('input')); document.querySelector('.band-send').click(); return 1; })()"},
            {"until": "/changed since it was shown/.test((document.querySelector('.band-msg') || {}).textContent || '') && 1", "ms": 30000},
            {"do": "(document.querySelector('.band-send').click(), 1)", "until": f"{BAND_ROWS} === 0 && 1", "ms": 30000, "every": 3000},
        ])
        t.join(timeout=5)
    assert out["values"]
    assert store.get(q1.n).answer.value == "fix"
