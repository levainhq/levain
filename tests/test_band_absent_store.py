"""np-cfffc0e1 (a), Phill 10-29 18:12: "Absent store draws nothing; a store that exists but cannot be read still draws
the 'could not be recorded' line."

A fresh install has no questions.db: no question was ever filed, so no band was ever recorded, and the cockpit's Decisions
panel must not read as an error. A store that EXISTS but whose view cannot be stored (a lock held, a write failing) is a
different fact and keeps its line: absence_of_signal_rendered_as_health is the failure to avoid there.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

from levain import band_view
from levain.consent.questions import QuestionStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_proposal import OPEN_DECISIONS, TILE, Seed

OPTS = [("ship", "Ship it tonight"), ("hold", "Hold for the morning")]
NOTE = "the band could not be recorded just now"


def _file(store: QuestionStore):
    return store.file(agent="claude_code", agent_session_id="s1", what="Ship the sweep fix?", options=OPTS,
                      rec="hold", blocks="the sweep", cwd="/w")


def test_a_fresh_directory_with_no_store_says_absent_has_no_note_and_creates_no_file(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    got = band_view.band_payload(store, "cockpit:browser")
    assert got["store"] == "absent", got
    assert "note" not in got and got["items"] == []
    assert got["store_id"] is None and got["view_id"] is None and got["seen_through"] is None
    assert not store.path.exists()                        # a poll of a store nobody filed in sets nothing up
    again = band_view.band_payload(store, "cockpit:browser")
    assert again == got and not store.path.exists()


def test_an_empty_database_file_is_still_absent(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    store.directory.mkdir(mode=0o700)
    store.path.touch()                                    # a filer that created the file and has not set it up
    assert band_view.band_payload(store, "cockpit:browser")["store"] == "absent"


def test_a_store_that_exists_but_whose_view_cannot_be_stored_keeps_its_note(tmp_path, monkeypatch):
    store = QuestionStore(tmp_path / "consent")
    _file(store)
    monkeypatch.setattr(band_view, "record_view", lambda *a, **k: None)   # the store lock held / the write failing
    got = band_view.band_payload(store, "cockpit:browser")
    assert got["note"] == NOTE and got.get("store") != "absent", got
    assert got["store_id"] is None and got["view_id"] is None and got["items"] == []


def test_a_locked_questions_db_is_not_absent_keeps_its_note_and_does_not_raise(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    _file(store)                                          # a real store...
    holder = sqlite3.connect(store.path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")                     # ...held by another connection (no monkeypatch)
    try:
        got = band_view.band_payload(store, "cockpit:browser", deadline=time.monotonic() + 0.4)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert got.get("store") != "absent", got
    assert got["note"] == NOTE and got["items"] == [], got


def test_a_fresh_install_whose_authority_source_cannot_be_read_is_not_absent(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    store.directory.mkdir(mode=0o700)
    (store.directory / "queue.db").write_bytes(b"this is not a sqlite database " * 40)    # no questions.db; queue.db corrupt
    assert not store.path.exists()
    r = band_view.show(store, "cockpit:browser")
    assert "! not read" in r.text, r.text                 # the render itself says the source was not read
    got = band_view.band_payload(store, "cockpit:browser")
    assert got.get("store") != "absent" and got["note"] == NOTE, got
    assert not store.path.exists()


def test_a_store_file_without_its_store_id_row_keeps_the_note(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    _file(store)
    with sqlite3.connect(store.path) as c:
        c.execute("DELETE FROM meta WHERE key = 'store_id'")
    assert band_view._store_id_of(store) is None          # the old check's "absent": the file is not absent
    got = band_view.band_payload(store, "cockpit:browser")
    assert got.get("store") != "absent" and got["note"] == NOTE, got


def test_a_sqlite_error_in_the_absence_check_goes_to_the_note_not_a_500_and_not_absent(tmp_path, monkeypatch):
    store = QuestionStore(tmp_path / "consent")
    _file(store)
    shown = band_view.show
    armed = [False]

    def show_then_arm(*a, **k):
        r = shown(*a, **k)
        armed[0] = True                                   # from here the store cannot be opened (the lock lands now)
        return r

    real_peek = QuestionStore._peek

    def peek(self):
        if armed[0]:
            raise sqlite3.OperationalError("database is locked")
        return real_peek(self)

    monkeypatch.setattr(band_view, "show", show_then_arm)
    monkeypatch.setattr(band_view, "record_view", lambda *a, **k: None)
    monkeypatch.setattr(band_view, "_newest_view", lambda *a, **k: None)
    monkeypatch.setattr(QuestionStore, "_peek", peek)
    got = band_view.band_payload(store, "cockpit:browser")
    assert got.get("store") != "absent" and got["note"] == NOTE, got


UNREAD = "a decision source could not be read just now: the band may be missing items"


def test_a_recorded_view_with_an_unread_authority_source_says_so_and_still_lists_the_items(tmp_path):
    """codex HIGH (L3 r2): questions.db exists and a view was recorded, but queue.db could not be read: the payload used
    to carry the normal items and an empty note, so both clients drew a healthy band missing the unread items."""
    store = QuestionStore(tmp_path / "consent")
    _file(store)
    (store.directory / "queue.db").write_bytes(b"this is not a sqlite database " * 40)    # an authority source that cannot be read
    assert "! not read" in band_view.show(store, "cockpit:browser").text
    got = band_view.band_payload(store, "cockpit:browser")
    assert got["note"] == UNREAD, got
    assert got["view_id"] is not None and len(got["items"]) == 1 and got.get("store") != "absent", got


def test_a_recorded_view_whose_sources_all_read_carries_no_note(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    _file(store)
    got = band_view.band_payload(store, "cockpit:browser")
    assert got["note"] == "" and len(got["items"]) == 1, got


def test_a_failed_authority_items_read_says_so_and_still_lists_the_questions(tmp_path, monkeypatch):
    """The same class as the unread source: _extras_of raising used to leave the note empty, so both clients drew a healthy
    band missing the authority items. The question items still list, and the note says the band may be missing items."""
    store = QuestionStore(tmp_path / "consent")
    _file(store)

    def boom(*a, **k):
        raise RuntimeError("authority items could not be read")

    monkeypatch.setattr(band_view, "_extras_of", boom)
    got = band_view.band_payload(store, "cockpit:browser")
    assert got["note"] == UNREAD, got
    assert got["view_id"] is not None and len(got["items"]) == 1 and got.get("store") != "absent", got


def test_a_permission_error_in_the_absence_check_goes_to_the_note_not_a_500_and_not_absent(tmp_path, monkeypatch):
    """complement LOW (L3 r2): a PermissionError on the db file is an OSError, not a sqlite3.Error."""
    store = QuestionStore(tmp_path / "consent")
    _file(store)
    shown = band_view.show
    armed = [False]

    def show_then_arm(*a, **k):
        r = shown(*a, **k)
        armed[0] = True
        return r

    real_peek = QuestionStore._peek

    def peek(self):
        if armed[0]:
            raise PermissionError(13, "Permission denied")
        return real_peek(self)

    monkeypatch.setattr(band_view, "show", show_then_arm)
    monkeypatch.setattr(band_view, "record_view", lambda *a, **k: None)
    monkeypatch.setattr(band_view, "_newest_view", lambda *a, **k: None)
    monkeypatch.setattr(QuestionStore, "_peek", peek)
    got = band_view.band_payload(store, "cockpit:browser")
    assert got.get("store") != "absent" and got["note"] == NOTE, got
    assert band_view._store_absent(store) is False        # called straight: False, never raised


def test_a_healthy_store_marks_nothing_absent(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    _file(store)
    got = band_view.band_payload(store, "cockpit:browser")
    assert got["note"] == "" and got.get("store") != "absent" and got["view_id"] is not None and len(got["items"]) == 1


# --- the page ---------------------------------------------------------------------------------------------------------
_BAND_TEXT = ("(() => { const m = document.getElementById('levain-modal'); const slot = m.querySelector('[data-band-slot]');"
              " return [[...m.querySelectorAll('.ext-grouphead')].map(h => h.textContent.trim().toLowerCase()),"
              " /could not be recorded/i.test(m.innerText), !!slot && slot.childElementCount]; })()")


# the page's own settle marker: bandDraw sets data-band-read ("absent" | "note" | "ok") on the slot after every draw
_SETTLED = "document.querySelector('#levain-modal [data-band-slot][data-band-read=\"%s\"]')"


# the band note and the item rows the slot drew
_BAND_ITEMS = ("(() => { const s = document.querySelector('#levain-modal [data-band-slot]');"
               " const n = s && s.querySelector('.band-note');"
               " return [n ? n.textContent : null, s ? [...s.querySelectorAll('[data-band-row] .band-what')].map(x => x.textContent) : []]; })()")


def _live_request(seed: Seed) -> None:
    seed.q.file(agent="claude_code", agent_session_id="live", tool_name="Edit", tool_input={"file_path": "/w/a"},
                cwd="/w", hold=timedelta(minutes=30), event_id="live")


@needs_browser
def test_the_page_draws_no_band_and_no_line_for_an_absent_store(tmp_path, monkeypatch):
    seed = Seed(tmp_path, now=datetime.now(timezone.utc))
    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    seed.clock[0] = datetime.now(timezone.utc)
    _live_request(seed)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": "document.querySelector('#levain-modal [data-consent-row]')"},
            {"until": _SETTLED % "absent"},                                    # the band read has landed and been drawn
            {"eval": _BAND_TEXT},
        ])
    heads, line, slot_kids = out["values"][-1]
    assert not any("the band" in h for h in heads), heads
    assert line is False and not slot_kids, (heads, line, slot_kids)
    assert not (seed.dir / "questions.db").exists()


@needs_browser
def test_the_page_keeps_the_heading_and_line_for_a_store_that_exists_but_cannot_record(tmp_path, monkeypatch):
    seed = Seed(tmp_path, now=datetime.now(timezone.utc))
    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    seed.clock[0] = datetime.now(timezone.utc)
    _live_request(seed)
    _file(QuestionStore(seed.dir))                                            # the store exists
    monkeypatch.setattr(band_view, "record_view", lambda *a, **k: None)       # and cannot store a view
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": _SETTLED % "note"},
            {"eval": _BAND_TEXT},
        ])
    heads, line, slot_kids = out["values"][-1]
    assert any("the band" in h for h in heads), heads
    assert line is True and slot_kids, (heads, line, slot_kids)


@needs_browser
def test_an_absent_payload_with_an_answer_to_say_still_says_it(tmp_path, monkeypatch):
    """The store vanishing between a send and the redraw must not drop the confirmation (an answer's only evidence)."""
    seed = Seed(tmp_path, now=datetime.now(timezone.utc))
    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    seed.clock[0] = datetime.now(timezone.utc)
    _live_request(seed)
    _file(QuestionStore(seed.dir))
    # an authority source that cannot be read just now (a transient lock on queue.db): the first draw has a recorded view
    # AND items, and must say the band may be missing some (codex HIGH, L3 r2): the note beside the items, not a clean band
    def unread(*a, **k):
        raise OSError("queue.db is locked")

    monkeypatch.setattr(band_view, "read_extras", unread)
    _root, src, _ids = _install(tmp_path)
    # after the first draw: the band read now answers "absent", and the answer's binding is made stale, so the
    # server refuses it ("stale_band") and the page redraws the band WITH the sentence it has to say
    patch = ("(() => { const f = window.fetch; window.fetch = function (u, o) { const url = String((u && u.url) || u);"
             " if (/\\/cockpit\\/band\\.json/.test(url)) return Promise.resolve(new Response(JSON.stringify("
             "{seen_through: null, store_id: null, view_id: null, items: [], store: 'absent'}),"
             " {status: 200, headers: {'content-type': 'application/json'}}));"
             " if (/\\/cockpit\\/verb/.test(url) && o && typeof o.body === 'string' && /band_answer/.test(o.body)) {"
             " const b = JSON.parse(o.body); b.params.view_id = 999999; o = Object.assign({}, o, {body: JSON.stringify(b)}); }"
             " return f.call(this, u, o); }; return 1; })()")
    send = ("(() => { const box = document.querySelector('[data-band-slot] .band-box'); box.value = '1a';"
            " document.querySelector('[data-band-slot] .band-send').click(); return 1; })()")
    said = ("(() => { const s = document.querySelector('#levain-modal [data-band-slot][data-band-read=\"absent\"]');"
            " const m = s && s.querySelector('.band-msg'); return m && m.textContent ? m.textContent : null; })()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": _SETTLED % "note"},
            {"eval": _BAND_ITEMS},
            {"eval": patch}, {"eval": send},
            {"until": said},
        ])
    note_drawn, items_drawn = out["values"][-4]
    assert note_drawn == UNREAD and items_drawn == ["Ship the sweep fix?"], out["values"][-4]    # the note beside the items
    assert "the band changed since it was shown" in out["values"][-1], out["values"][-1]


# a fetch wrapper for the marker test: an answer is refused as stale (so the page redraws the SAME slot with a fresh read),
# and that fresh read is held pending until the test releases it
_HOLD = ("(() => { const f = window.fetch; window.fetch = function (u, o) { const url = String((u && u.url) || u);"
         " if (/\\/cockpit\\/verb/.test(url) && o && typeof o.body === 'string' && /band_answer/.test(o.body)) {"
         " const b = JSON.parse(o.body); b.params.view_id = 999999; o = Object.assign({}, o, {body: JSON.stringify(b)}); }"
         " if (window.__hold && /\\/cockpit\\/band\\.json/.test(url)) { const self = this; return new Promise((res) => {"
         " window.__release = () => { window.__hold = false; res(f.call(self, u, o)); }; }); }"
         " return f.call(this, u, o); }; window.__hold = true; return 1; })()")
_SEND = ("(() => { const box = document.querySelector('[data-band-slot] .band-box'); box.value = '1a';"
         " document.querySelector('[data-band-slot] .band-send').click(); return 1; })()")
_NO_MARKER = ("window.__release && (() => { const s = document.querySelector('#levain-modal [data-band-slot]');"
              " return !!s && !s.hasAttribute('data-band-read'); })()")
# the next draw throws once (bandDraw's own replaceChildren), as a draw that fails on a payload it did not expect would
_THROW_ONCE = ("(() => { const orig = Element.prototype.replaceChildren; let n = 0;"
               " Element.prototype.replaceChildren = function (...a) { if (!n++) throw new Error('boom'); return orig.apply(this, a); };"
               " window.__release(); return 1; })()")


@needs_browser
def test_the_settle_marker_is_cleared_while_a_read_is_pending_and_a_throwing_draw_ends_in_error(tmp_path, monkeypatch):
    """complement + codex LOW (L3 r2): (a) the marker of the last draw must not outlive the read that replaces it, or a
    waiter for "ok" succeeds before the new read lands; (b) an exception thrown inside bandDraw must end in "error"."""
    seed = Seed(tmp_path, now=datetime.now(timezone.utc))
    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    seed.clock[0] = datetime.now(timezone.utc)
    _live_request(seed)
    _file(QuestionStore(seed.dir))
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": _SETTLED % "ok"},
            {"eval": _HOLD}, {"eval": _SEND},
            {"until": _NO_MARKER, "ms": 8000},                                 # (a) the read is pending: no marker
            {"eval": _THROW_ONCE},
            {"until": _SETTLED % "error", "ms": 8000},                         # (b) the draw threw: the marker says error
            {"eval": "document.querySelector('#levain-modal [data-band-slot]').textContent"},
        ])
    assert "could not be drawn" in out["values"][-1], out["values"][-1]               # said on the page, not drawn as health
