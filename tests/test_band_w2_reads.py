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
from levain.consent.questions import Link, QuestionStore
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


# --- W2 session 3: the versioned row answer verb (item_answer) --------------------------------------------------------

def _item_answer(base: str, params: dict, token: str = TOKEN):
    return _post(f"{base}/cockpit/verb", {"verb": "item_answer", "panel_id": "decisions", "params": params}, token)


def _subject_of(base: str, qid: str, token: str = TOKEN) -> dict:
    """The subject (kind, id, version) band.json shows for question ``qid``."""
    item = next(i for i in _get(f"{base}/cockpit/band.json", token)["items"] if i["subject"]["id"] == qid)
    return dict(item["subject"])


def _db_bytes(store: QuestionStore) -> bytes:
    with sqlite3.connect(store.path) as c:
        return json.dumps([list(map(str, r)) for t in ("questions", "answers")
                           for r in c.execute(f"SELECT * FROM {t}")] if _has(c, "answers") else
                          [list(map(str, r)) for r in c.execute("SELECT * FROM questions")]).encode()


def _has(c: sqlite3.Connection, table: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone() is not None


def _check_moved_version_refused(base: str, store: QuestionStore, q, pin: dict | None = None) -> None:
    stale = _subject_of(base, q.id)
    with sqlite3.connect(store.path) as c:                                # the item changes under the person's feet
        c.execute("DROP TRIGGER questions_frozen")                         # this test's own scratch store, no other
        c.execute("UPDATE questions SET rec = 'none' WHERE id = ?", (q.id,))
    assert _subject_of(base, q.id)["version"] != stale["version"]
    if pin is not None:
        pin["v"], pin["on"] = stale["version"], True    # the mutated comparison reads the version the person was shown
    before = _db_bytes(store)
    s, body = _item_answer(base, {**stale, "pick": "a"})
    assert s == 409 and body["error"] == "version_moved", (s, body)
    assert _db_bytes(store) == before, "a refused answer wrote"
    assert store.get(q.n).answer is None


def test_a_moved_version_is_refused_and_writes_nothing(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        _check_moved_version_refused(base, store, q)
        subj = _subject_of(base, q.id)
        s, body = _item_answer(base, {**subj, "pick": "a"})               # the current version answers
        assert s == 200 and body["recorded"] == ["a"], (s, body)
    assert store.get(q.n).answer.value == "fix"


def test_the_moved_version_check_fails_against_a_variant_without_the_guard(tmp_path, lh, monkeypatch):
    """Mutation: both version comparisons (the early look and the one inside the write) made vacuous."""
    pin: dict = {}
    real = band_view.subject_version
    monkeypatch.setattr(band_view, "subject_version", lambda q: pin.get("v") if pin.get("on") else real(q))
    monkeypatch.setattr(band_view, "_still_version", lambda subject: (lambda conn: None))
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        pin["on"] = False
        with pytest.raises(AssertionError):
            _check_moved_version_refused(base, store, q, pin)


def _check_authority_never_reaches_band_answer(base: str, store: QuestionStore, calls: list) -> None:
    before = _db_bytes(store)
    for kind in ("consent", "sign", "extend"):                             # an approve/deny on a consent row, a sign request
        s, body = _item_answer(base, {"kind": kind, "id": "req-1", "version": "v1", "pick": "a"})
        assert s == 422 and body["error"] == "authority_item", (kind, s, body)
    s, body = _item_answer(base, {"kind": "consent", "id": "req-1", "version": "v1", "text": "approve"})
    assert s == 422, (s, body)
    assert calls == [], f"an authority item reached the unsigned answer path: {calls}"
    assert _db_bytes(store) == before


def _spy_answer_paths(monkeypatch) -> list:
    """Record every call of the shorthand answer path (what band_answer fires) and the store's answer write."""
    calls: list = []
    real_text = band_view.answer_text
    real_write = QuestionStore.answer_with_context

    def text(*a, **kw):
        calls.append("answer_text")
        return real_text(*a, **kw)

    def write(self, *a, **kw):
        calls.append("answer_with_context")
        return real_write(self, *a, **kw)
    monkeypatch.setattr(band_view, "answer_text", text)
    monkeypatch.setattr(QuestionStore, "answer_with_context", write)
    return calls


def test_an_authority_item_never_reaches_band_answer_through_this_verb(tmp_path, lh, monkeypatch):
    calls = _spy_answer_paths(monkeypatch)
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        _check_authority_never_reaches_band_answer(base, store, calls)


def test_the_authority_check_fails_against_a_verb_that_routes_to_band_answer(tmp_path, lh, monkeypatch):
    """Mutation: a verb that hands every subject to the shorthand path (band_view.answer_text) is caught."""
    from levain.cockpit import verbs

    calls = _spy_answer_paths(monkeypatch)

    def leaky(store, subject, *, pick, text, channel, viewer):
        band_view.answer_text(store, f"1{pick or 'a'}", viewer=viewer, channel=channel, store_id="x", view_id=0)
        raise band_view.AuthorityItem("hidden")
    monkeypatch.setattr(band_view, "answer_item", leaky)
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        with pytest.raises(AssertionError):
            _check_authority_never_reaches_band_answer(base, store, calls)
    assert verbs                                                        # the verb module is the one under test


def _race(base: str, subj: dict) -> list:
    import threading

    gate = threading.Barrier(2, timeout=20)
    out: list = [None, None]

    def go(i: int) -> None:
        gate.wait()
        out[i] = _item_answer(base, {**subj, "pick": "a" if i == 0 else "b"})
    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    return out


def _both_pass_the_early_look(monkeypatch) -> dict:
    """Hold each request at the early (outside-the-write) version look until both have reached it, so both enter the
    write: the race the in-write check exists for. A thread's later call (inside the write) is not held."""
    import threading

    gate = threading.Barrier(2, timeout=20)
    real = band_view.subject_version
    seen = threading.local()

    arm = {"on": False}

    def held(q):
        if arm["on"] and not getattr(seen, "x", False):
            seen.x = True
            gate.wait()
        return real(q)
    monkeypatch.setattr(band_view, "subject_version", held)
    return arm


def _check_one_concurrent_answer_wins(base: str, store: QuestionStore, q, arm: dict) -> None:
    subj = _subject_of(base, q.id)
    arm["on"] = True
    a, b = _race(base, subj)
    codes = sorted([a[0], b[0]])
    assert codes == [200, 409], (a, b)
    loser = a if a[0] == 409 else b
    assert loser[1]["error"] == "version_moved", loser
    won = "fix" if a[0] == 200 else "exp"
    assert store.get(q.n).answer.value == won


def test_of_two_concurrent_answers_to_one_row_exactly_one_wins(tmp_path, lh, monkeypatch):
    arm = _both_pass_the_early_look(monkeypatch)
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        _check_one_concurrent_answer_wins(base, store, q, arm)


def test_the_concurrency_check_fails_against_a_variant_without_the_in_write_guard(tmp_path, lh, monkeypatch):
    """Mutation: no check inside the write. Both requests pass the early look, both enter the write, and the store's own
    refusal reaches the loser as a different error, so the version refusal the plan names is gone."""
    arm = _both_pass_the_early_look(monkeypatch)
    monkeypatch.setattr(band_view, "_still_version", lambda subject: (lambda conn: None))
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store, "Which retry policy?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        with pytest.raises(AssertionError):
            _check_one_concurrent_answer_wins(base, store, q, arm)


def test_item_answer_shapes_and_the_text_and_unoffered_pick_paths(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q1 = _file(store, "Which retry policy?")
    q2 = _file(store, "Ship the sweep fix tonight?")
    with _serve(src, write_token=TOKEN) as (base, _h):
        s1, s2 = _subject_of(base, q1.id), _subject_of(base, q2.id)
        assert _item_answer(base, {**s1, "pick": "a", "text": "x"})[0] == 400        # both
        assert _item_answer(base, dict(s1))[0] == 400                                  # neither
        s, body = _item_answer(base, {**s1, "pick": "z"})
        assert s == 400 and body["error"] == "pick_not_offered", (s, body)
        assert store.get(q1.n).answer is None
        s, body = _item_answer(base, {**s1, "text": "linear backoff"})
        assert s == 200, (s, body)
        s, body = _item_answer(base, {**s2, "pick": "a"})                              # q1's answer did not move q2
        assert s == 200, (s, body)
    got = store.get(q1.n).answer
    assert (got.kind, got.value) == ("text", "linear backoff")


# --- W2 session 3 fix: a REAL authority row (a question carrying a link, a question carrying a sign_request) -----------

def _linked_question(store: QuestionStore):
    """A question filed through the store's own writer with a link: the row a `levain ask --would-push` leaves."""
    return store.file(agent="claude_code", agent_session_id="s2", what="Push the fix?", options=OPTS, rec="exp",
                      blocks="the push", cwd="/w", link=Link("fix", "push", "req-1", "qid-1"))


def _count_store_writes(monkeypatch) -> list:
    writes: list = []
    real = QuestionStore.answer_with_context

    def spy(self, *a, **kw):
        writes.append(1)
        return real(self, *a, **kw)
    monkeypatch.setattr(QuestionStore, "answer_with_context", spy)
    return writes


def _post_real_subject(base: str, q, body: dict) -> tuple[int, dict]:
    """Post item_answer with the subject kind and version band.json really shows for ``q``."""
    subj = _subject_of(base, q.id)
    assert subj["kind"] == "question", subj
    return _item_answer(base, {**subj, **body})


def test_a_linked_question_is_refused_by_the_early_look_before_any_write(tmp_path, lh, monkeypatch):
    """The early look (answer_item's own kind check) refuses a REAL linked question: 422, DB unchanged, and the store's
    answer write is never reached (so the in-write check cannot be what refused it)."""
    writes = _count_store_writes(monkeypatch)
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _linked_question(store)
    assert band_view._question_kind(store.get(q.n)) == "authority"
    with _serve(src, write_token=TOKEN) as (base, _h):
        before = _db_bytes(store)
        for body in ({"pick": "a"}, {"text": "go ahead"}):
            s, resp = _post_real_subject(base, q, body)
            assert s == 422 and resp["error"] == "authority_item", (body, s, resp)
        assert _db_bytes(store) == before and store.get(q.n).answer is None
    assert writes == [], "the refusal came from inside the write, not from the early look"


def test_a_linked_question_that_slips_the_early_look_is_refused_inside_the_write(tmp_path, lh, monkeypatch):
    """The in-write check (_still_version) alone: the early look is made to see an information row once, as a row that
    changed between the look and the write would; the write's own check refuses, nothing is recorded."""
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _linked_question(store)
    real = band_view._question_kind
    with _serve(src, write_token=TOKEN) as (base, _h):
        subj = _subject_of(base, q.id)
        first = {"seen": False}

        def lies_once(row):
            if not first["seen"]:
                first["seen"] = True
                return "information"
            return real(row)
        monkeypatch.setattr(band_view, "_question_kind", lies_once)
        before = _db_bytes(store)
        s, resp = _item_answer(base, {**subj, "pick": "a"})
        assert first["seen"], "the early look was not reached"
        assert s == 422 and resp["error"] == "authority_item", (s, resp)
        assert _db_bytes(store) == before and store.get(q.n).answer is None


def test_a_foreign_question_carrying_a_sign_request_is_refused_before_the_host_is_asked():
    """No writer of the levain store sets sign_request (the kernel's store sets none), so the field is only real on a
    host's row: the foreign path refuses it before question_versions / answer_versioned."""
    from tests.test_band_reads import Host, fq

    row = fq(1, "q-sign", sign_request="req-sign-1")
    host = Host([row], {"q-sign": "v1"})
    for kw in ({"pick": "a", "text": None}, {"pick": None, "text": "go"}):
        with pytest.raises(band_view.AuthorityItem):
            band_view.answer_item(host, {"kind": "question", "id": "q-sign", "version": "v1"}, channel="cockpit",
                                  viewer=VIEWER, **kw)
    assert host.answered == []
