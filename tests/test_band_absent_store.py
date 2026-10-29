"""np-cfffc0e1 (a), Phill 10-29 18:12: "Absent store draws nothing; a store that exists but cannot be read still draws
the 'could not be recorded' line."

A fresh install has no questions.db: no question was ever filed, so no band was ever recorded, and the cockpit's Decisions
panel must not read as an error. A store that EXISTS but whose view cannot be stored (a lock held, a write failing) is a
different fact and keeps its line: absence_of_signal_rendered_as_health is the failure to avoid there.
"""
from __future__ import annotations

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


def test_a_healthy_store_marks_nothing_absent(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    _file(store)
    got = band_view.band_payload(store, "cockpit:browser")
    assert got["note"] == "" and got.get("store") != "absent" and got["view_id"] is not None and len(got["items"]) == 1


# --- the page ---------------------------------------------------------------------------------------------------------
_BAND_TEXT = ("(() => { const m = document.getElementById('levain-modal'); const slot = m.querySelector('[data-band-slot]');"
              " return [[...m.querySelectorAll('.ext-grouphead')].map(h => h.textContent.trim().toLowerCase()),"
              " /could not be recorded/i.test(m.innerText), !!slot && slot.childElementCount]; })()")


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
            {"eval": "new Promise(r => setTimeout(() => r(1), 1200))"},       # the band read has landed by now
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
            {"until": "document.querySelector('#levain-modal [data-band-slot] .band-note')"},
            {"eval": _BAND_TEXT},
        ])
    heads, line, slot_kids = out["values"][-1]
    assert any("the band" in h for h in heads), heads
    assert line is True and slot_kids, (heads, line, slot_kids)
