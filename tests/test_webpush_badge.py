"""The Needs-you app-icon badge (1101-6 lane B, Phill's np-82c2bb24 "2c"): the badge counts open Needs-you items only (pending
consent requests plus open questions), never notices; the count rides in the push payload; the service worker and the page set
it. Verified here on the server, the worker rig and a stub navigator. NOT verified on a device: that is Phill's step list."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.consent.listing import read_listing
from levain.consent.questions import QuestionStore
from levain.consent.queue import ConsentQueue, authority_source
from levain.cockpit.engine import _consent_rows_in, _question_rows_in
from levain.webpush import ece, needs, sender
from levain.webpush.store import PushStore
from tests._h4 import h4_home

HERE = Path(__file__).resolve().parent
SW = HERE.parent / "levain" / "webpush" / "static" / "sw.js"
CORE = HERE.parent / "levain" / "templates" / "web" / "dashboard_core.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
OPTS = [("a", "yes"), ("b", "no")]
_CLOCK = [lambda: datetime.now(timezone.utc)]


def _queue(tmp_path: Path) -> ConsentQueue:
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700, exist_ok=True)
    return ConsentQueue(qdir, authority=authority_source(qdir, anchor=None), home=h4_home(), clock=lambda: _CLOCK[0]())


def _consent(q: ConsentQueue, event: str, hold_min: int = 30):
    return q.file(agent="claude_code", agent_session_id="s", tool_name="Write", tool_input={"file_path": "/w/a", "content": "x"},
                  cwd="/w", hold=timedelta(minutes=hold_min), event_id=event)


def _question(q: ConsentQueue, n: int):
    return QuestionStore(q.directory).file(agent="claude_code", agent_session_id=f"s{n}", what=f"q{n}?", options=OPTS, rec="a",
                                           blocks="x", cwd="/w")


# --- the count: the cockpit's own rule, read at the same two sources --------------------------------------------------
def test_the_count_is_pending_consents_plus_open_questions_and_nothing_else(tmp_path):
    q = _queue(tmp_path)
    assert needs.open_needs_count(q.directory) == 0
    waiting, gone, _ = _consent(q, "e1"), _consent(q, "e2"), _consent(q, "e3")
    assert q.withdraw(gone.id)                                   # a decided request is not waiting
    _expired(q, "e4")                               # expired at once: reads as expired, not pending
    one, two = _question(q, 1), _question(q, 2)
    QuestionStore(q.directory).answer(two.n, option="a", channel="cockpit")   # an answered question is not open
    assert waiting and one
    assert needs.open_needs_count(q.directory) == 2 + 1          # e1 and e3 pending, question 1 open


def test_the_server_count_equals_what_the_cockpits_rows_say(tmp_path):
    """The parity pin: the cockpit page counts rows with consent_state pending and question_state open; the engine builds those
    rows from the same listing and store. Whatever the queue holds, both agree."""
    q = _queue(tmp_path)
    for i in range(3):
        _consent(q, f"e{i}")
    _expired(q, "x")
    withdrawn = _consent(q, "w")
    q.withdraw(withdrawn.id)
    for i in range(1, 4):
        _question(q, i)
    QuestionStore(q.directory).answer(2, option="b", channel="cockpit")
    now = datetime.now(timezone.utc)
    crows, _ = _consent_rows_in(read_listing(q.directory, now=now))
    qrows, _ = _question_rows_in(QuestionStore(q.directory), now)
    page = (sum(1 for r in crows if r.facets["consent_state"] == "pending")
            + sum(1 for r in qrows if r.facets["question_state"] == "open"))
    assert page == 3 + 2 and needs.open_needs_count(q.directory) == page


def test_a_notice_never_changes_the_count(tmp_path):
    q = _queue(tmp_path)
    _consent(q, "e1")
    _question(q, 1)
    before = needs.open_needs_count(q.directory)
    st = PushStore(q.directory)
    st.add_ping("attention", "digest", "A ping", "body")
    st.record_notice("notify", "notify:Backup", "Backup: ok", "Backup", "ok", "", "urgent", "Backup", "pushed")
    st.record_notice("notify", "notify:Backup", "Backup: ok 2", "Backup", "ok 2", "", "normal", "Backup", "pushed")
    assert before == 2 and needs.open_needs_count(q.directory) == 2
    assert needs.badge_value(q.directory) == 2


def test_an_unreadable_store_is_unknown_never_zero(tmp_path, monkeypatch):
    q = _queue(tmp_path)
    _consent(q, "e1")

    def boom(*a, **k):
        raise OSError("unreadable")

    monkeypatch.setattr("levain.consent.listing.read_listing", boom)
    assert needs.open_needs_count(q.directory) is None and needs.badge_value(q.directory) is None


def test_the_badge_is_clipped_for_the_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(needs, "open_needs_count", lambda d: 250)
    assert needs.badge_value(tmp_path) == 99
    monkeypatch.setattr(needs, "open_needs_count", lambda d: 7)
    assert needs.badge_value(tmp_path) == 7


# --- the payload ------------------------------------------------------------------------------------------------------
def _load(data: bytes) -> dict:
    return json.loads(data.decode("utf-8"))


def test_the_payload_carries_a_plain_int_badge_and_omits_anything_else():
    assert _load(sender.build_payload("t", "b", "tag", "/", 5, 3))["badge"] == 3
    assert _load(sender.build_payload("t", "b", "tag", "/", 5, 0))["badge"] == 0          # a real zero is sent: it clears
    for bad in (None, True, False, -1, 100, "3", 2.5):
        assert "badge" not in _load(sender.build_payload("t", "b", "tag", "/", 5, bad)), bad
    assert "badge" not in _load(sender.build_payload("t", "b", "tag", "/", 5))


def test_the_badge_costs_the_body_room_not_the_size_limit():
    big = "x" * 5000
    with_b = sender.build_payload("t", big, "tag", "/", 5, 12)
    without = sender.build_payload("t", big, "tag", "/", 5)
    assert len(with_b) <= ece.MAX_PLAINTEXT and len(without) <= ece.MAX_PLAINTEXT
    assert _load(with_b)["badge"] == 12 and len(_load(with_b)["body"]) <= len(_load(without)["body"])
    ctl = "\x01" * 4000                                          # six serialized bytes each: still inside the limit
    assert len(sender.build_payload(ctl, ctl, ctl, ctl, 5, 99)) <= ece.MAX_PLAINTEXT


def test_every_send_carries_the_open_count_at_send_time_and_a_notice_push_carries_the_needs_count(tmp_path, monkeypatch):
    q = _queue(tmp_path)
    _consent(q, "e1")
    _question(q, 1)
    seen = []
    real = sender.build_payload
    monkeypatch.setattr(sender, "build_payload", lambda *a, **k: (seen.append(a[5] if len(a) > 5 else None), real(*a, **k))[1])
    st = PushStore(q.directory)
    pid = st.add_ping("notify", "notify:Backup", "Backup: ok", "body")        # a NOTICE push: kind notify
    sender.deliver(q.directory, st, pid, kind="notify", ref="notify:Backup", title="Backup: ok", body="body", url="/?view=inbox")
    _consent(q, "e2")
    pid = st.add_ping("question", "q", "title", "body")
    sender.deliver(q.directory, st, pid, kind="question", ref="q", title="title", body="body", url="/?view=needs")
    assert seen == [2, 3]                                        # the notice push says 2 needs items (not 3, not a notice count)


def test_an_unknown_count_sends_no_badge_field(tmp_path, monkeypatch):
    q = _queue(tmp_path)
    seen = []
    real = sender.build_payload
    monkeypatch.setattr(needs, "badge_value", lambda d: None)
    monkeypatch.setattr(sender, "build_payload", lambda *a, **k: (seen.append(real(*a, **k)), seen[-1])[1])
    st = PushStore(q.directory)
    pid = st.add_ping("question", "q", "t", "b")
    sender.deliver(q.directory, st, pid, kind="question", ref="q", title="t", body="b", url="/")
    assert len(seen) == 1 and "badge" not in _load(seen[0])


# --- the service worker -----------------------------------------------------------------------------------------------
def _sw(mode: str) -> dict:
    cp = subprocess.run([NODE, str(HERE / "_sw_worker_rig.js"), str(SW), "push", mode], capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return json.loads(cp.stdout)


def test_the_worker_source_sets_and_clears_the_badge_guarded_and_holds_no_credential():
    src = SW.read_text(encoding="utf-8")
    assert "setAppBadge" in src and "clearAppBadge" in src and "d.badge" in src
    assert "typeof nav.setAppBadge === 'function'" in src and "typeof nav.clearAppBadge === 'function'" in src
    assert "Number.isInteger" in src and "} catch (_) { return Promise.resolve(); }" in src
    assert src.index("showNotification(title") < src.index("applyBadge(d.badge)")      # after the notification is asked for
    assert src.count("fetch(") == src.count("fetch('/webpush/receipt'")                 # still no new network call


@needs_node
def test_a_positive_count_sets_it_and_zero_clears_it_and_the_notification_still_shows():
    got = _sw("ok+badge4+nav")
    assert got["badgeCalls"] == [["set", 4]] and got["shownCalls"] == [{"title": "T", "tag": "x"}]
    got = _sw("ok+badge0+nav")
    assert got["badgeCalls"] == [["clear"]] and len(got["shownCalls"]) == 1


@needs_node
def test_a_missing_or_malformed_count_leaves_the_icon_alone():
    assert _sw("ok+nav")["badgeCalls"] == []                          # absent: unknown, never a clear
    for flag in ("badgestr", "badgeneg", "badgefloat"):
        got = _sw(f"ok+{flag}+nav")
        assert got["badgeCalls"] == [] and len(got["shownCalls"]) == 1, flag


@needs_node
def test_a_worker_without_the_badging_api_or_with_a_failing_one_still_shows_and_reports():
    for mode in ("ok+badge3", "ok+badge3+navset+badge0", "ok+badge3+nav+navthrow", "ok+badge3+nav+navreject", "ok+badge0+nav+navthrow"):
        got = _sw(mode)
        assert len(got["shownCalls"]) == 1, mode
        assert [p["body"]["kind"] for p in got["posts"]] == ["received", "shown"], mode      # receipts unharmed


# --- the page ---------------------------------------------------------------------------------------------------------
def _page(view, n, nav="full") -> dict:
    cp = subprocess.run([NODE, str(HERE / "_badge_page_rig.js"), str(CORE), json.dumps({"view": view, "n": n, "nav": nav})],
                        capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    got = json.loads(cp.stdout)
    assert "error" not in got, got
    return got


def test_the_page_calls_the_badge_from_the_needs_render_with_the_same_count_it_shows():
    src = CORE.read_text(encoding="utf-8")
    body = src[src.index("function renderNeeds("):src.index("// the Decisions focus view")]
    assert body.index("syncAppBadge(view, items.length)") < body.index("if (!all.length) { clearNeeds(host); return; }")
    assert body.index("syncAppBadge(view, items.length)") < body.index("host.dataset.sig === sig")   # before the unchanged early return
    assert "needsItems(((view.extra_panels || {}).decisions || {}).lines)" in body


@needs_node
def test_the_page_badge_caps_at_99_as_the_payload_does():
    from levain.webpush import needs

    ok = {"extra_panels": {"decisions": {"lines": []}}}
    assert _page(ok, 150)["calls"] == [["set", needs.BADGE_MAX]] and _page(ok, 99)["calls"] == [["set", 99]]


@needs_node
def test_the_page_sets_the_count_clears_at_zero_and_ignores_an_unread_panel():
    ok = {"extra_panels": {"decisions": {"lines": []}}}
    assert _page(ok, 3)["calls"] == [["set", 3]]
    assert _page(ok, 0)["calls"] == [["clear"]]
    for dark in ({}, {"extra_panels": {}}, {"extra_panels": {"decisions": {}}}, {"extra_panels": {"decisions": {"lines": None}}}):
        assert _page(dark, 0)["calls"] == [], dark                    # a panel that did not read says nothing: never a 0


@needs_node
def test_the_page_badge_is_feature_detected_and_a_refusal_changes_nothing():
    ok = {"extra_panels": {"decisions": {"lines": []}}}
    for nav in ("none", "set-only"):
        got = _page(ok, 0, nav)
        assert got["threw"] is False and got["calls"] == []
    for nav in ("throw", "reject"):
        assert _page(ok, 2, nav)["threw"] is False


def test_the_page_never_counts_notices():
    src = CORE.read_text(encoding="utf-8")
    fn = src[src.index("function syncAppBadge("):src.index("function renderNeeds(")]
    assert "notice" not in fn.lower().replace("notices are not in", "")


def _expired(q: ConsentQueue, event: str):
    """A request filed an hour ago with a five-minute hold: nobody swept it, so it reads as expired, never pending."""
    _CLOCK[0] = lambda: datetime.now(timezone.utc) - timedelta(hours=1)
    try:
        return _consent(q, event, hold_min=5)
    finally:
        _CLOCK[0] = lambda: datetime.now(timezone.utc)
