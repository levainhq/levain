"""The phone page's Review tab, in headless Chrome at a phone's width (390), over the real phone server.

The tab shows the coordinator's morning card and its review queue (open items with feed label, title and age, and the
newest cleared ones). Its only controls are Clear on a plain open item, Reopen on a cleared one (c4mut: a paired
phone's write) and Clear with passkey on a "yours to clear" item, which also names the laptop command; drawing the tab sends no POST. A queue that
cannot be read says so. Screenshots go to ``LEVAIN_SHOT_DIR`` when it is set (the lane's RUN), else the test's tmp dir."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from levain.coordinator import review
from levain.coordinator.envelopes import EnvelopeStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_coordinator_phone import device_token
from tests.test_dispatch_phone import repo, world  # noqa: F401  (fixtures)
from tests.test_phone_dispatch import _serve

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

READY = "document.getElementById('status').textContent !== 'reading the queue…' && 1"
OPEN_TAB = "document.getElementById('tab-review').click() || 1"
TITLES = "[...document.querySelectorAll('#review-list .review-item')].map(c => c.textContent)"


def _seed(qdir: Path) -> None:
    store = EnvelopeStore(qdir)
    review.file_item(store, "seat", {"text": "claude launch ln-0001 (claude) died: lease lost"}, dedupe_key="seat:1")
    review.file_item(store, "end", {"text": "you ended claude_code session s-1 at its tool_calls limit; it stays stopped"},
                     dedupe_key="end:1", authority_relevant=True)
    done = review.file_item(store, "know", {"text": "the coordinator read your word as away and set it on"}, dedupe_key="know:1")[0]
    review.clear_item(store, done.item_id)
    review.push_sign(store, None, "np-not-in-the-queue", "sign the budget extension", rec=review.NO_REC)


@needs_browser
def test_the_review_tab_shows_the_queue_and_the_card_and_only_clear_and_reopen(world, tmp_path: Path) -> None:
    shots = Path(os.environ.get("LEVAIN_SHOT_DIR") or tmp_path)
    _seed(world["qdir"])
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": READY},
            {"eval": OPEN_TAB},
            {"until": "document.querySelectorAll('#review-list .review-item').length === 2 && 1"},
            {"shot": str(shots / "c4c_review_queue.png"), "full": True},
            {"eval": TITLES},
            {"eval": "document.getElementById('review-meta').textContent"},
            {"eval": "[...document.querySelectorAll('#review-cleared .review-item')].map(c => c.textContent)"},
            {"eval": "document.getElementById('morning').textContent"},
            {"eval": "[...document.querySelectorAll('#review button, #review input, #review textarea')].map(b => b.textContent)"},
            {"eval": "document.documentElement.scrollWidth <= window.innerWidth"},
            {"eval": "[...document.querySelectorAll('#tabs button')].map(b => b.textContent + (b.getAttribute('aria-selected') === 'true' ? '*' : ''))"},
            {"eval": "(() => { const d = document.querySelector('#morning details'); const was = d.open; d.open = true; return [was, d.textContent]; })()"},
            {"shot": str(shots / "c4c_morning_card_open.png")},
        ])
    v = out["values"]
    queue, meta, cleared, morning_text, controls, fits, tabs = v[4], v[5], v[6], v[7], v[8], v[9], v[10]
    assert any("Seat died" in t and "died: lease lost" in t and ("ago" in t or "just now" in t) for t in queue), queue
    assert any("Session ended at a limit" in t and "yours to clear" in t for t in queue), queue
    assert meta == "2 open", meta
    assert len(cleared) == 1 and "Know" in cleared[0] and "cleared" in cleared[0], cleared
    assert "1 waiting on you" not in morning_text and "0 waiting on you" in morning_text and "(1 unreadable)" in morning_text, morning_text
    assert "2 in the review queue" in morning_text, morning_text
    assert controls == ["Clear", "Clear with passkey", "Reopen"], controls    # the yours-to-clear one is signed
    assert any("or on the laptop" in t and "levain coord review clear" in t for t in queue), queue
    assert fits is True
    assert tabs[-1] == "Review*", tabs
    assert v[11][0] is False and "review queue: 2 open" in v[11][1], v[11]      # the card's own lines, closed until tapped
    assert not [p for p in out["posts"]], out["posts"]


@needs_browser
def test_an_empty_queue_says_nothing_to_review_and_a_dead_store_says_it_cannot_read(world, tmp_path: Path, monkeypatch) -> None:
    EnvelopeStore(world["qdir"]).store_id()      # a store with nothing in it (asking its identity makes it)
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": READY},
            {"eval": OPEN_TAB},
            {"until": "document.getElementById('review-meta').textContent && 1"},
            {"eval": "document.getElementById('review-meta').textContent"},
        ])
        assert out["values"][-1] == "nothing to review"
        monkeypatch.setattr(review, "queue_page", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("store gone")))
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": READY},
            {"eval": OPEN_TAB},
            {"until": "/can't read/.test(document.getElementById('review-meta').textContent) && 1"},
            {"eval": "document.getElementById('review-meta').textContent + ' | ' + document.getElementById('morning-meta').textContent"},
        ])
        assert "store gone" in out["values"][-1], out["values"]


@needs_browser
def test_the_coordinator_read_fires_only_while_the_review_tab_is_shown(world, tmp_path: Path, monkeypatch) -> None:
    """Poll cost (codex HIGH): another tab never asks /coord.json; the Review tab does; leaving it stops the asks."""
    from levain.coordinator.phone import CoordPhone

    _seed(world["qdir"])
    asked: list[float] = []
    real = CoordPhone.coord_listing

    def spy(self):
        asked.append(len(asked))
        return real(self)

    monkeypatch.setattr(CoordPhone, "coord_listing", spy)
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        url = base.replace("127.0.0.1", "localhost")
        _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": READY},
            {"eval": "document.getElementById('tab-grants').click() || 1"},
            {"until": "(window.__t = window.__t || Date.now(), Date.now() - window.__t > 1500) && 1"},
        ])
        before_review_tab = len(asked)
        _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": READY},
            {"eval": OPEN_TAB},
            {"until": "document.querySelectorAll('#review-list .review-item').length === 2 && 1"},
        ])
    assert before_review_tab == 0, "a page on another tab asked for the coordinator view"
    assert len(asked) >= 1


@needs_browser
def test_the_morning_card_says_mixed_away_unreadable_and_unreadable_budgets_in_words(world, tmp_path: Path, monkeypatch) -> None:
    """Mixed away, an unreadable section and an unreadable budget meter each read as what they are (L3 r1 3, 4, 5)."""
    from levain.coordinator import morning

    shots = Path(os.environ.get("LEVAIN_SHOT_DIR") or tmp_path)
    _seed(world["qdir"])
    real = morning.card

    def card(store, **kw):
        c = real(store, **kw)
        c["away"] = {"claude_code": {"away": True}, "codex": {"away": None}}
        c["undecided"], c["errors"] = [], ["undecided: database is locked"]
        c["budgets"] = [{"thread_id": "t-a", "envelope_id": "env-1", "budget": {"tool_calls": 5, "tokens": 5, "hours": 1},
                         "used": {"tool_calls": 9, "tokens": 0, "hours": 0}, "uncounted": [], "sign": None,
                         "crossed": ["tool_calls"], "unreadable": None, "refusal": "crossed"},
                        {"thread_id": "t-b", "envelope_id": "env-1", "budget": {"tool_calls": 5, "tokens": 5, "hours": 1},
                         "used": {"tool_calls": 0, "tokens": 0, "hours": 0}, "uncounted": [], "sign": None,
                         "crossed": [], "unreadable": "no sessions table", "refusal": "unreadable"}]
        return c

    monkeypatch.setattr(morning, "card", card)
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": READY},
            {"eval": OPEN_TAB},
            {"until": "document.querySelector('#morning .note-what') && 1"},
            {"shot": str(shots / "c4cfix_mixed_away_card.png")},
            {"eval": "document.querySelector('#morning .note-what').textContent"},
        ])
    text = out["values"][-1]
    assert "away ON for claude_code" in text and "away UNKNOWN for codex" in text, text
    assert "undecided: could not read" in text and "0 undecided" not in text, text
    assert "1 crossed" in text and "1 could not be read (new steps stop)" in text and "2 crossed" not in text, text


SPY = ("(() => { window.__reqs = []; window.__views = [];"
       " const f = window.fetch; window.fetch = (r, o) => { window.__reqs.push(String(r)); return f(r, o); };"
       " const v = window.levainViewChanged; window.levainViewChanged = (x) => { window.__views.push(x); return v(x); };"
       " return 1; })()")


@needs_browser
def test_opening_the_review_tab_asks_for_the_queue_by_itself_not_through_the_poll(world, tmp_path: Path) -> None:
    """C4 integration item 6: phone_app.js's show() calls levainViewChanged with the tab name, and that edge alone asks
    for /coord.json. The page is held until the 10 s poll has just ticked (a /consent.json request after the spy was
    installed), so the next tick is ~10 s away and the one /coord.json that follows the click is the tab-open edge's."""
    _seed(world["qdir"])
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, httpd):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={dev}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": READY},
            {"eval": SPY},
            {"until": "window.__reqs.some(r => r.includes('/consent.json')) && 1"},      # a poll tick has just happened
            {"eval": "window.__reqs.slice()"},
            {"eval": OPEN_TAB},
            {"until": "window.__reqs.some(r => r.includes('/coord.json')) && 1"},
            {"eval": "[window.__reqs.slice(), window.__views.slice()]"},
        ])
    before = out["values"][-4]
    reqs, views = out["values"][-1]
    assert not any("/coord.json" in r for r in before), before          # hidden tab: the poll never asks for it
    assert views == ["review"], views                                    # the tab's name, once
    assert sum("/consent.json" in r for r in reqs) == sum("/consent.json" in r for r in before), reqs   # no new tick
    assert sum("/coord.json" in r for r in reqs) == 1, reqs
