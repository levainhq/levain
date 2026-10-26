"""The Sessions tile (Q7a): the agent sessions this install's hooks have seen, as a display and never an authority."""
from __future__ import annotations

import hashlib
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import levain.doctor
from levain.cockpit.providers import SANDBOX_UNSEEN, SESSION_FILE_MAX, SESSIONS_SHOWN, build_default_cockpit
from levain.consent.listing import read_listing
from levain.consent.queue import ConsentQueue, authority_source
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests._h4 import h4_home

SID_CC, SID_CX, SID_Q = "zq-session-id-cc-8812", "zq-session-id-cx-8813", "zq-session-id-q-8814"
CWD_CC, CWD_CX = "/zq/full/path/cc-marker/levain-work", "/zq/full/path/cx-marker/codex-work"


def _stamp(ago: timedelta) -> str:
    # stamped from the clock at write time: a module-level "now" goes stale by the time a slow CI runner reaches the test
    return (datetime.now(timezone.utc) - ago).strftime("%Y-%m-%dT%H:%M:%SZ")


def _put(root: Path, agent: str, sid: str, cwd: str, ago: timedelta, delivered=True, **over) -> Path:
    d = root / ".levain" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"v": 1, "agent": agent, "session_id": sid, "cwd": cwd, "first_seen": _stamp(ago + timedelta(hours=1)),
           "last_seen": _stamp(ago), "last_event": "UserPromptSubmit", "context_delivered": delivered, **over}
    p = d / (hashlib.sha1(f"{agent}:{sid}".encode()).hexdigest()[:24] + ".json")
    p.write_text(json.dumps(rec))
    return p


def _fixture(tmp_path: Path, monkeypatch):
    """One active Claude Code session with a consent request waiting, one active Codex, one quiet, one older than a day."""
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    q = ConsentQueue(home / "consent", authority=authority_source(home / "consent", anchor=None), home=h4_home())
    q.file(agent="claude_code", agent_session_id=SID_CC, tool_name="Bash", tool_input={"command": "ls"}, cwd=CWD_CC,
           hold=timedelta(minutes=5), event_id="e1")
    root, src, _ids = _install(tmp_path)
    work = tmp_path / "work"
    (work / ".claude").mkdir(parents=True)
    (work / ".claude" / "settings.json").write_text(json.dumps({"sandbox": {"enabled": True}}))
    _put(root, "claude_code", SID_CC, str(work), timedelta(minutes=3))
    _put(root, "codex", SID_CX, CWD_CX, timedelta(minutes=1), delivered=False)
    _put(root, "claude_code", SID_Q, CWD_CC, timedelta(hours=3), delivered=None)
    _put(root, "codex", "zq-old", CWD_CX, timedelta(hours=30))
    return root, src


def test_sessions_group_by_recency_with_the_measured_words_and_the_consent_join(tmp_path, monkeypatch):
    root, src = _fixture(tmp_path, monkeypatch)
    (root / ".levain" / "sessions" / ("0" * 24 + ".json")).write_text("{not json")        # malformed shape: absent, counted
    (root / ".levain" / "sessions" / ("1" * 24 + ".json")).write_text(" " * (SESSION_FILE_MAX + 1))   # past the read bound
    ck = build_default_cockpit(src)
    ck.attach_consent_rows(lambda now: read_listing(now=now))
    p = ck.panel("sessions", credential_class="none")
    meta = {r["facets"]["agent"] + ":" + r["facets"]["session_activity"]: r["facets"]["legacy_meta"] for r in p["rows"]}
    assert sorted(meta) == ["claude_code:active", "claude_code:quiet", "codex:active"]    # the 30 h one is not listed
    assert p["count"] == 2 and [g["id"] for g in p["groups"]] == ["active", "quiet"]
    cc = meta["claude_code:active"]
    assert "last active 3m ago" in cc and "sandbox configured on (project settings)" in cc
    assert "memory loaded at start" in cc and "1 waiting" in cc
    cx = meta["codex:active"]
    assert "sandbox not read" in cx and "memory not loaded" in cx and "waiting" not in cx
    q = meta["claude_code:quiet"]
    assert "last active 3h ago" in q and "memory" not in q          # null: no word
    assert not any(w in " ".join(meta.values()) for w in ("running", "ended", "confined"))
    assert sum(s["count"] for s in p["skipped"]) == 2 and "does not have the registry file's shape" in str(p["skipped"])
    assert [r["facets"]["agent"] for r in p["rows"]][:2] == ["codex", "claude_code"]    # newest first within Active


def test_two_sessions_in_one_workspace_say_when_each_started_and_a_unique_row_does_not(tmp_path, monkeypatch):
    root, src = _fixture(tmp_path, monkeypatch)
    # a second Claude Code session in the active one's workspace (the Q7a RUN's two "claude_code · ccinst" rows)
    _put(root, "claude_code", "zq-session-id-cc2-8815", str(tmp_path / "work"), timedelta(minutes=2),
         first_seen=_stamp(timedelta(minutes=40)))
    ck = build_default_cockpit(src)
    rows = ck.panel("sessions", credential_class="none")["rows"]
    twins = [r for r in rows if r["title"] == "claude_code \u00b7 work"]
    assert len(twins) == 2
    started = sorted(c for r in twins for c in r["facets"]["legacy_meta"].split(" \u00b7 ") if c.startswith("started "))
    assert started == ["started 1h ago", "started 40m ago"]
    others = [r for r in rows if r["title"] != "claude_code \u00b7 work"]
    assert others and not any("started " in r["facets"]["legacy_meta"] for r in others)
    assert "zq-session-id-cc2-8815" not in json.dumps(rows)
    # the started words are display only: three minutes later they read differently and the row version does not move
    pid = next(r["id"] for r in twins if "40m ago" in r["facets"]["legacy_meta"])
    later = build_default_cockpit(src, clock=lambda: datetime.now(timezone.utc) + timedelta(minutes=3))
    moved = next(r for r in later.panel("sessions", credential_class="none")["rows"] if r["id"] == pid)
    assert "started 43m ago" in moved["facets"]["legacy_meta"]
    assert moved["version"] == next(r for r in twins if r["id"] == pid)["version"]


def test_a_start_after_the_last_hook_is_not_shown_as_a_start(tmp_path, monkeypatch):
    root, src = _fixture(tmp_path, monkeypatch)
    _put(root, "claude_code", "zq-session-id-cc3-8816", str(tmp_path / "work"), timedelta(minutes=2),
         first_seen=_stamp(-timedelta(days=2)))                       # a damaged record: started in the future
    rows = build_default_cockpit(src).panel("sessions", credential_class="none")["rows"]
    metas = [r["facets"]["legacy_meta"] for r in rows if r["title"] == "claude_code \u00b7 work"]
    assert len(metas) == 2 and sum("started " in m for m in metas) == 1 and "started just now" not in " ".join(metas)


def test_the_listing_reads_the_newest_fifty_and_the_caveat_is_doctors_own_words(tmp_path, monkeypatch):
    root, src = _fixture(tmp_path, monkeypatch)
    for i in range(SESSIONS_SHOWN + 5):
        _put(root, "codex", f"zq-bulk-{i}", CWD_CX, timedelta(minutes=10))
    ck = build_default_cockpit(src)
    p = ck.panel("sessions", credential_class="none")
    assert len(p["rows"]) <= SESSIONS_SHOWN and any("not read (the newest 50" in f["reason"] for f in p["filtered"])
    assert SANDBOX_UNSEEN in p["note"] and "not visible to doctor" in p["note"]
    # the footer reuses the readers' own list of what they cannot see: if doctor's line changes, this does
    import inspect
    doctor = inspect.getsource(levain.doctor)
    assert "not visible to doctor: " in doctor and all(part in doctor for part in SANDBOX_UNSEEN.split(", "))


def test_the_token_free_panel_and_manifest_carry_no_session_id_and_no_full_cwd(tmp_path, monkeypatch):
    _root, src = _fixture(tmp_path, monkeypatch)
    with _serve(src, surface_tokens={"browser": "T"}) as (url, _httpd):
        blobs = []
        for route in ("/cockpit/panel/sessions.json", "/cockpit/manifest.json"):
            with urllib.request.urlopen(url + route, timeout=10) as r:  # noqa: S310 - loopback
                blobs.append(r.read())
    titles = [r["title"] for r in json.loads(blobs[0])["rows"]]
    assert b"session:" in blobs[0] and "claude_code \u00b7 work" in titles    # the active CC session's basename is shown
    for blob in blobs:
        for marker in (SID_CC, SID_CX, SID_Q, "cc-marker", "cx-marker", "/zq/full"):
            assert marker.encode() not in blob, marker


@needs_browser
def test_the_page_draws_the_tile_and_the_modal_with_groups_chips_and_the_footer(tmp_path, monkeypatch):
    _root, src = _fixture(tmp_path, monkeypatch)
    tile = "[...document.querySelectorAll('.ck-widgets .ck-card.w')].find(c => /^Sessions/i.test(c.innerText))"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": f"{tile} && /2/.test({tile}.innerText)"},
            {"eval": f"{tile}.click(); 1"},
            {"until": "document.querySelector('#levain-modal .panel-foot')"},
            {"eval": "document.getElementById('levain-modal').innerText"},
        ])["values"]
    text = out[-1].lower()
    for want in ("\nactive\n", "\nquiet\n", "last active 3m ago", "1 waiting", "sandbox not read", "memory not loaded",
                 "not visible to doctor"):
        assert want in text, (want, text)
    assert text.index("\nactive\n") < text.index("\nquiet\n")


def test_a_record_that_vanishes_between_the_listing_and_the_stat_costs_that_entry_only(tmp_path, monkeypatch):
    import os

    import levain.cockpit.providers as prov

    root, src = _fixture(tmp_path, monkeypatch)
    real = os.scandir

    class _Entry:
        def __init__(self, e):
            self._e, self.name, self.path = e, e.name, e.path

        def is_file(self, **kw):
            return self._e.is_file(**kw)

        def stat(self, **kw):
            if self.name == sorted(os.listdir(os.path.dirname(self.path)))[0]:
                raise FileNotFoundError(self.path)      # a hook's replace or prune landed between scandir and stat
            return self._e.stat(**kw)

    class _Scan:
        def __init__(self, p):
            self._s = real(p)

        def __enter__(self):
            return (_Entry(e) for e in self._s.__enter__())

        def __exit__(self, *a):
            return self._s.__exit__(*a)

    monkeypatch.setattr(prov.os, "scandir", _Scan)
    p = build_default_cockpit(src).panel("sessions", credential_class="none")
    assert len(p["rows"]) >= 2 and "changed while it was being listed" in str(p["skipped"])


_TYPE = ("(v => { const i = document.querySelector('#levain-modal .xfilter-input'); i.value = v;"
         " i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })")


@needs_browser
def test_the_sessions_modal_carries_a_filter_that_narrows_by_agent_and_workspace_and_chip(tmp_path, monkeypatch):
    _root, src = _fixture(tmp_path, monkeypatch)
    tile = "[...document.querySelectorAll('.ck-widgets .ck-card.w')].find(c => /^Sessions/i.test(c.innerText))"
    rows = "document.querySelectorAll('#levain-modal .row').length"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": f"{tile} && /2/.test({tile}.innerText)"},
            {"eval": f"{tile}.click(); 1"},
            {"until": "document.querySelector('#levain-modal .xfilter-input')"},
            {"eval": rows},
            {"eval": f"{_TYPE}('codex'); {rows}"},
            {"eval": "document.querySelector('#levain-modal .xfilter-count').textContent"},
            {"eval": f"{_TYPE}('1 waiting'); [{rows}, document.getElementById('levain-modal').innerText.includes('1 waiting')]"},
            {"eval": f"{_TYPE}('no-such-thing'); document.querySelector('#levain-modal .empty') && document.querySelector('#levain-modal .empty').textContent"},
            {"eval": f"{_TYPE}(''); {rows}"},
        ])["values"]
    full, codex, count, waiting, none, cleared = out[-6:]
    assert full == 3 and cleared == full, out         # the quiet one is a row of the modal too
    assert codex == 1 and count == "1 of 3", out
    assert waiting[0] == 1 and waiting[1] is True, out
    assert none == "no match", out
