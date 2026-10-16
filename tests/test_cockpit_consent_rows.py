"""Agents' consent requests in the decisions panel (Q6a): composed by the kernel, grouped, never a dark panel."""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from levain.cockpit import Cockpit, ProviderSpec, Read, RowIn
from levain.cockpit.results import Absent
from levain.consent.listing import read_listing
from levain.consent.queue import ConsentQueue, authority_source

T0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


def _filed(tmp_path: Path):
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    q = ConsentQueue(qdir, authority=authority_source(qdir, anchor=None), clock=lambda: T0)
    waiting = q.file(agent="claude_code", agent_session_id="s", tool_name="Bash",
                     tool_input={"command": "ls /w"}, cwd="/w", hold=timedelta(minutes=5), event_id="a")
    done = q.file(agent="codex", agent_session_id="s2", tool_name="Write",
                  tool_input={"file_path": "/w/x", "content": "y"}, cwd="/w", hold=timedelta(minutes=5), event_id="b")
    q.withdraw(done.id)
    return qdir, waiting, done


def _ck() -> Cockpit:
    return Cockpit(clock=lambda: T0 + timedelta(minutes=1))


def test_consent_rows_group_pending_first_counted_and_terminal_resolved(tmp_path):
    qdir, waiting, done = _filed(tmp_path)
    ck = _ck()
    ck.attach_consent_rows(lambda now: read_listing(qdir, now=now))
    p = ck.panel("decisions", credential_class="token")
    by_id = {r["id"]: r for r in p["rows"]}
    assert set(by_id) == {f"consent:{waiting.id}", f"consent:{done.id}"}
    assert by_id[f"consent:{waiting.id}"]["group"] == "needs_input"
    assert by_id[f"consent:{done.id}"]["group"] == "resolved"
    assert p["rows"][0]["id"] == f"consent:{waiting.id}"               # the counted group is first
    assert p["count"] == 1
    f = by_id[f"consent:{waiting.id}"]["facets"]
    assert f["agent"] == "claude_code" and f["tool"] == "Bash" and f["consent_state"] == "pending"
    assert by_id[f"consent:{done.id}"]["facets"]["consent_state"] == "withdrawn"
    # a terminal row's `at` is when it was filed; the decision time is its own facet, and a pending has none
    df = by_id[f"consent:{done.id}"]["facets"]
    assert df["at"] == done.received_at and df["decided_at"] >= df["at"]
    assert f["at"] == waiting.received_at and "decided_at" not in f
    one = ck.read_one("decisions", f"consent:{waiting.id}").value
    assert one["id"] == f"consent:{waiting.id}" and isinstance(ck.read_one("decisions", "consent:nope"), Absent)


def test_a_row_version_does_not_change_with_the_clock(tmp_path):
    qdir, waiting, _ = _filed(tmp_path)
    clock = [T0 + timedelta(minutes=1)]
    ck = Cockpit(clock=lambda: clock[0])
    ck.attach_consent_rows(lambda now: read_listing(qdir, now=now))
    v1 = {r["id"]: r["version"] for r in ck.panel("decisions", credential_class="token")["rows"]}
    clock[0] += timedelta(seconds=90)
    v2 = {r["id"]: r["version"] for r in ck.panel("decisions", credential_class="token")["rows"]}
    assert v1 == v2


def test_a_reader_fault_is_a_note_not_a_dark_panel_and_no_queue_adds_no_note(tmp_path):
    ck = _ck()
    def boom(now):
        raise RuntimeError("queue gone")
    ck.attach_consent_rows(boom)
    p = ck.panel("decisions", credential_class="token")
    assert p["status"] != "error" and "agent consent requests unavailable" in p["note"]
    ck2 = _ck()
    ck2.attach_consent_rows(lambda now: read_listing(tmp_path / "none", now=now))
    p2 = ck2.panel("decisions", credential_class="token")
    assert p2["rows"] == [] and "consent" not in p2.get("note", "")


def test_a_self_sealed_row_with_a_malformed_stamp_is_skipped_and_the_others_still_list(tmp_path):
    from levain.consent.record import ExternalGateRequest

    qdir, waiting, done = _filed(tmp_path)
    # the right shape, an impossible date: create() refuses it, but a raw writer can seal it itself
    fields = dict(agent="codex", agent_session_id="s9", event_id="raw", tool_name="Bash", tool_input_digest="d",
                  display="Bash {}", cwd="/w", tier="t", received_at=waiting.received_at,
                  expires_at="2026-99-99T99:99:99.999999Z")
    rid = ExternalGateRequest._seal(**fields)
    conn = sqlite3.connect(qdir / "queue.db", isolation_level=None)
    conn.execute(f"INSERT INTO requests (id, {', '.join(fields)}) VALUES (?{', ?' * len(fields)})",
                 (rid, *fields.values()))
    conn.close()
    ck = _ck()
    ck.attach_consent_rows(lambda now: read_listing(qdir, now=now))
    p = ck.panel("decisions", credential_class="token")
    assert {r["id"] for r in p["rows"]} == {f"consent:{waiting.id}", f"consent:{done.id}"}
    assert "unavailable" not in p.get("note", "")
    assert any("consent" in str(s.get("reason")) and s.get("count") == 1 for s in p["skipped"])


def test_a_downstream_row_with_a_consent_id_is_skipped(tmp_path):
    ck = _ck()
    ck.attach_decision_source(ProviderSpec(
        "decisions", "triage-list", "Decisions waiting", "gate",
        lambda ctx: Read(rows=(RowIn(id="consent:forged", title="forged", stored={"s": 1}),
                               RowIn(id="np:1", title="real", stored={"s": 1}))),
        order="decision.dated", version_fields=("*",), rowset="needs_phill",
        read_one=lambda ctx, rid: Absent("gone")))
    ck.attach_consent_rows(lambda now: read_listing(tmp_path / "none", now=now))
    p = ck.panel("decisions", credential_class="token")
    assert [r["id"] for r in p["rows"]] == ["np:1"]
    assert any("consent:" in str(s.get("reason")) for s in p["skipped"])


# --- the page ---------------------------------------------------------------------------------------------

from tests.test_cockpit_flip_parity import _drive, needs_browser          # noqa: E402
from tests.test_cockpit_k2a import _install, _serve                        # noqa: E402

TILE = "[...document.querySelectorAll('.ck-widgets .ck-card.w')].find(c => /^Decisions waiting/i.test(c.innerText))"
MARK_CMD, MARK_CWD = "zq-cmd-marker-7731", "/w/zq-cwd-marker-4419"


def _get(url: str, token: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"X-Levain-Write-Token": token} if token is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - loopback
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _home_queue(tmp_path, monkeypatch) -> ConsentQueue:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    return ConsentQueue(home / "consent", authority=authority_source(home / "consent", anchor=None))


def test_the_token_free_panel_and_manifest_carry_no_request_body_on_loopback(tmp_path, monkeypatch):
    q = _home_queue(tmp_path, monkeypatch)
    r = q.file(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={"command": f"echo {MARK_CMD}"},
               cwd=MARK_CWD, hold=timedelta(minutes=5), event_id="m1")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}) as (url, _httpd):
        st, panel = _get(f"{url}/cockpit/panel/decisions.json")
        st2, manifest = _get(f"{url}/cockpit/manifest.json")
    assert st == 200 and st2 == 200
    assert f"consent:{r.id}".encode() in panel                     # the row is there ...
    for blob in (panel, manifest):                                 # ... and nothing of what it asks
        assert MARK_CMD.encode() not in blob and b"zq-cwd-marker" not in blob
        assert b"levain consent answer" not in blob and b"signing-key" not in blob


def test_the_consent_body_route_needs_the_token_on_loopback_and_answers_with_it(tmp_path, monkeypatch):
    q = _home_queue(tmp_path, monkeypatch)
    r = q.file(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={"command": f"echo {MARK_CMD}"},
               cwd=MARK_CWD, hold=timedelta(minutes=5), event_id="m2")
    _root, src, _ids = _install(tmp_path)
    route = f"/cockpit/consent/{urllib.request.quote(r.id, safe='')}.json"
    with _serve(src, surface_tokens={"browser": "T"}) as (url, _httpd):
        none, no_body = _get(url + route)
        wrong, _ = _get(url + route, token="not-T")
        ok, body = _get(url + route, token="T")
        gone, _ = _get(url + "/cockpit/consent/pend-nope.json", token="T")
    assert (none, wrong, ok, gone) == (403, 403, 200, 404)
    assert MARK_CMD.encode() not in no_body
    b = json.loads(body)
    assert MARK_CMD in b["display"] and b["cwd"] == MARK_CWD and b["state"] == "pending"
    assert b["answer_allow"] == f"levain consent answer {r.id} allow --signing-key <key>"


@needs_browser
def test_the_page_draws_a_consent_request_as_text_with_a_countdown_and_copy_buttons(tmp_path, monkeypatch):
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    qdir = home / "consent"
    q = ConsentQueue(qdir, authority=authority_source(qdir, anchor=None))
    r = q.file(agent="claude_code", agent_session_id="s", tool_name="Bash",
               tool_input={"command": "echo '<img src=x onerror=window.__pwned=1>'"}, cwd="/w",
               hold=timedelta(minutes=5), event_id="p1")
    gone = q.file(agent="codex", agent_session_id="s", tool_name="Bash", tool_input={"command": "ls"}, cwd="/w",
                  hold=timedelta(minutes=5), event_id="p2")
    q.withdraw(gone.id)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": f"{TILE} && /1/.test({TILE}.innerText)"},
            {"eval": f"{TILE}.click(); 1"},
            {"until": "document.querySelector('#levain-modal .consent-row .consent-code')"},
            {"eval": "(() => { const m = document.getElementById('levain-modal'); const row = m.querySelector('.consent-row');"
                     " return [row.querySelector('.consent-agent').textContent, row.querySelector('.consent-tool').textContent,"
                     " row.querySelector('.consent-left').textContent, row.querySelector('.consent-display').textContent,"
                     " [...row.querySelectorAll('.consent-code')].map(c => c.textContent), row.querySelectorAll('img').length,"
                     " !!window.__pwned, /agents waiting on you/i.test(m.innerText),"
                     " [...m.querySelectorAll('.consent-row')].map(r => [r.querySelector('.consent-tool').textContent,"
                     " !!r.querySelector('.consent-body')])]; })()"},
        ])["values"]
    agent, tool, left, display, cmds, imgs, pwned, grouphead, rows = out[-1]
    # only a pending request has a body slot (and so a fetch); a decided one is its state word and times
    assert sorted(rows) == [["Bash", False], ["Bash", True]]
    assert (agent, tool) == ("claude_code", "Bash") and left.endswith("left")
    assert "<img src=x" in display and imgs == 0 and not pwned and grouphead
    assert cmds == [f"levain consent answer {r.id} allow --signing-key <key>",
                    f"levain consent answer {r.id} deny --signing-key <key>"]


def test_decided_consent_rows_list_newest_decision_first_and_broker_rows_keep_their_order():
    from levain.cockpit.registry import _queue_key
    today = T0.date()
    def row(i, **f):
        return RowIn(id=i, title=i, facets=f)
    old_filed_new_decided = row("consent:a", consent_state="allowed", at="2026-10-15T10:00:00Z", decided_at="2026-10-15T11:50:00Z")
    new_filed_old_decided = row("consent:b", consent_state="denied", at="2026-10-15T11:00:00Z", decided_at="2026-10-15T11:10:00Z")
    got = sorted([new_filed_old_decided, old_filed_new_decided], key=lambda r: _queue_key(r, today))
    assert [r.id for r in got] == ["consent:a", "consent:b"]            # by decision, not by filing
    b1, b2 = row("x1", at="2026-10-15T09:00:00Z"), row("x2", at="2026-10-15T08:00:00Z")
    assert [r.id for r in sorted([b2, b1], key=lambda r: _queue_key(r, today))] == ["x1", "x2"]


@needs_browser
def test_a_decided_row_has_a_details_button_and_a_malformed_body_releases_its_slot(tmp_path, monkeypatch):
    q = _home_queue(tmp_path, monkeypatch)
    for n in ("p1", "p2"):
        q.file(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={"command": "echo " + n},
               cwd="/w", hold=timedelta(minutes=5), event_id=n)
    gone = q.file(agent="codex", agent_session_id="s", tool_name="Bash", tool_input={"command": "echo zq-decided-marker"},
                  cwd="/w", hold=timedelta(minutes=5), event_id="p3")
    q.withdraw(gone.id)
    _root, src, _ids = _install(tmp_path)
    # the first two body fetches answer 200 with an unreadable body; a freed slot lets the third (details) through
    stub = ("(() => { const real = window.fetch.bind(window); let n = 0; window.fetch = (u, o) => String(u).includes('/cockpit/consent/') && n++ < 2"
            " ? Promise.resolve({ ok: true, status: 200, json: () => Promise.reject(new Error('bad json')) }) : real(u, o); return 1; })()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": f"{TILE} && /2/.test({TILE}.innerText)"},
            {"eval": stub},
            {"eval": f"{TILE}.click(); 1"},
            {"until": "document.querySelectorAll('#levain-modal .consent-row .consent-note').length >= 2"},
            {"eval": "(() => { const r = [...document.querySelectorAll('#levain-modal .consent-row')].find(x => x.querySelector('.consent-details'));"
                     " const before = !!r.querySelector('.consent-display'); r.querySelector('.consent-details').click(); return before; })()"},
            {"until": "[...document.querySelectorAll('#levain-modal .consent-row')].some(r => r.querySelector('.consent-display'))"},
            {"eval": "[...document.querySelectorAll('#levain-modal .consent-row')].filter(r => r.querySelector('.consent-display')).map(r => r.querySelector('.consent-display').textContent)"},
        ])["values"]
    assert out[-3] is False, out        # before the click the decided row had no body
    assert any("zq-decided-marker" in t for t in out[-1])


@needs_browser
def test_a_details_retry_after_a_failed_read_reuses_its_slot(tmp_path, monkeypatch):
    q = _home_queue(tmp_path, monkeypatch)
    gone = q.file(agent="codex", agent_session_id="s", tool_name="Bash", tool_input={"command": "echo zq-retry-marker"},
                  cwd="/w", hold=timedelta(minutes=5), event_id="r1")
    q.withdraw(gone.id)
    _root, src, _ids = _install(tmp_path)
    # the first body fetch answers 500, so "details" shows an error and re-enables; the retry reads the body
    stub = ("(() => { const real = window.fetch.bind(window); let n = 0; window.fetch = (u, o) => String(u).includes('/cockpit/consent/') && n++ < 1"
            " ? Promise.resolve({ ok: false, status: 500 }) : real(u, o); return 1; })()")
    row = "[...document.querySelectorAll('#levain-modal .consent-row')].find(x => x.querySelector('.consent-audit'))"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": f"{TILE} && /1/.test({TILE}.innerText)"},
            {"eval": stub},
            {"eval": f"{TILE}.click(); 1"},
            {"until": f"{row} && {row}.querySelector('.consent-details')"},
            {"eval": f"{row}.querySelector('.consent-details').click(); 1"},
            {"until": f"{row}.querySelector('.consent-note') && !{row}.querySelector('.consent-details').disabled"},
            {"eval": f"{row}.querySelector('.consent-details').click(); 1"},
            {"until": f"{row}.querySelector('.consent-display')"},
            {"eval": f"[{row}.querySelectorAll('.consent-body').length, {row}.querySelector('.consent-display').textContent]"},
        ])["values"]
    assert out[-1][0] == 1, out
    assert "zq-retry-marker" in out[-1][1]


@needs_browser
def test_the_decisions_modal_carries_a_filter_that_narrows_by_agent_tool_and_state(tmp_path, monkeypatch):
    q = _home_queue(tmp_path, monkeypatch)
    q.file(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={"command": "ls"}, cwd="/w",
           hold=timedelta(minutes=5), event_id="f1")
    q.file(agent="codex", agent_session_id="s", tool_name="apply_patch", tool_input={"command": "x"}, cwd="/w",
           hold=timedelta(minutes=5), event_id="f2")
    gone = q.file(agent="codex", agent_session_id="s", tool_name="Bash", tool_input={"command": "ls"}, cwd="/w",
                  hold=timedelta(minutes=5), event_id="f3")
    q.withdraw(gone.id)
    _root, src, _ids = _install(tmp_path)
    typ = ("(v => { const i = document.querySelector('#levain-modal .xfilter-input'); i.value = v;"
           " i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })")
    rows = "document.querySelectorAll('#levain-modal .consent-row').length"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": f"{TILE} && /2/.test({TILE}.innerText)"},
            {"eval": f"{TILE}.click(); 1"},
            {"until": "document.querySelector('#levain-modal .xfilter-input') && document.querySelector('#levain-modal .consent-row')"},
            {"eval": rows},
            {"eval": f"{typ}('apply_patch'); {rows}"},
            {"eval": f"{typ}('codex'); {rows}"},
            {"eval": f"{typ}('claude_code'); {rows}"},
            {"eval": f"{typ}('no-such-thing'); [{rows}, document.querySelector('#levain-modal .empty') && document.querySelector('#levain-modal .empty').textContent]"},
            {"eval": f"{typ}(''); {rows}"},
        ])["values"]
    full, patch, codex, cc, none, cleared = out[-6:]
    assert full == 3 and cleared == full, out
    assert (patch, codex, cc) == (1, 2, 1), out
    assert none == [0, "no match"], out
