"""Agents' consent requests in the decisions panel (Q6a): composed by the kernel, grouped, never a dark panel."""
from __future__ import annotations

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
    assert f["answer_allow"] == f"levain consent answer {waiting.id} allow --signing-key <key>"
    assert by_id[f"consent:{waiting.id}"]["body"] == waiting.display
    assert by_id[f"consent:{done.id}"]["facets"]["audit"] == "withdrawn"
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
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": f"{TILE} && /1/.test({TILE}.innerText)"},
            {"eval": f"{TILE}.click(); 1"},
            {"until": "document.querySelector('#levain-modal .consent-row')"},
            {"eval": "(() => { const m = document.getElementById('levain-modal'); const row = m.querySelector('.consent-row');"
                     " return [row.querySelector('.consent-agent').textContent, row.querySelector('.consent-tool').textContent,"
                     " row.querySelector('.consent-left').textContent, row.querySelector('.consent-display').textContent,"
                     " [...row.querySelectorAll('.consent-code')].map(c => c.textContent), row.querySelectorAll('img').length,"
                     " !!window.__pwned, /agents waiting on you/i.test(m.innerText)]; })()"},
        ])["values"]
    agent, tool, left, display, cmds, imgs, pwned, grouphead = out[-1]
    assert (agent, tool) == ("claude_code", "Bash") and left.endswith("left")
    assert "<img src=x" in display and imgs == 0 and not pwned and grouphead
    assert cmds == [f"levain consent answer {r.id} allow --signing-key <key>",
                    f"levain consent answer {r.id} deny --signing-key <key>"]
