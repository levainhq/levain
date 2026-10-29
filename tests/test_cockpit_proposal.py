"""The standing-rule proposal card (STATUS_1_0 rows 13/17): a repeated hand-allow becomes the one
``levain consent grant create`` line, never for what the log would refuse, and the page signs nothing.

Requests are filed through a real ``ConsentQueue`` and then marked decided with a raw UPDATE of the (pending) row, as
the queue's own tests do: the listing reads the rows as an index, so the seeding needs no signed answer.
"""
from __future__ import annotations

import json
import shlex
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit.proposals import MIN_HAND_ALLOWS, PROPOSAL_CAP, PROPOSED_LIFE, propose
from levain.consent.listing import KeyEvidence, read_evidence, read_listing
from levain.consent.policy import AGENT_CONFIG_TIER
from levain.consent.queue import ConsentQueue, authority_source
from tests._h4 import h4_home

# The page under test reads its proposal window against the browser's real clock, so the seed is anchored to the
# current hour: a fixed date ages out of that window (it did, nine days after 2026-10-17).
NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Seed:
    """Files requests at a moving clock and decides them in the store."""

    def __init__(self, base: Path, now: datetime = NOW) -> None:
        self.now = now          # the instant "ago" counts back from; tests that drive a live server pass the real now
        self.dir = base / "consent"
        self.dir.mkdir(mode=0o700, parents=True)
        self.clock = [NOW - timedelta(days=5)]
        self.q = ConsentQueue(self.dir, authority=authority_source(self.dir, anchor=None), clock=lambda: self.clock[0], home=h4_home())
        self.n = 0
        self.root = base / "work"
        self.ws = self.w("proj")

    def w(self, name: str) -> str:
        """A real directory under the test's own tree: the log refuses a workspace that does not exist."""
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def decide(self, state: str = "allowed", *, via: str | None = "signature", agent: str = "claude_code",
               tool: str = "Edit", cwd: str | None = None, tier: str = "T2", tool_input: dict | None = None,
               ago: timedelta = timedelta(hours=1)) -> str:
        self.n += 1
        cwd = self.ws if cwd is None else cwd
        self.clock[0] = self.now - ago
        r = self.q.file(agent=agent, agent_session_id=f"s{self.n}", tool_name=tool,
                        tool_input=tool_input or {"file_path": f"{cwd}/f{self.n}.txt", "content": "x"}, cwd=cwd,
                        hold=timedelta(minutes=5), tier=tier, event_id=f"e{self.n}")
        conn = sqlite3.connect(self.dir / "queue.db", isolation_level=None)
        try:
            conn.execute("UPDATE requests SET state = ?, decided_at = ?, via = ?, signer_fp = ? WHERE id = ?",
                         (state, _stamp(self.now - ago + timedelta(seconds=20)), via, "SHA256:testkey", r.id))
        finally:
            conn.close()
        return r.id

    def evidence(self):
        return read_evidence(self.dir, now=NOW, exclude_tier=AGENT_CONFIG_TIER, min_hand=MIN_HAND_ALLOWS)

    def propose(self):
        return propose(self.evidence())


@pytest.fixture
def seed(tmp_path):
    return Seed(tmp_path)


def test_three_hand_allows_make_a_card_with_the_exact_grant_line(seed):
    for i in range(MIN_HAND_ALLOWS):
        seed.decide(ago=timedelta(days=2, hours=i))
    got = seed.propose()
    assert len(got) == 1
    p = got[0]
    assert (p.agent, p.tool, p.workspace, p.count) == ("claude_code", "Edit", seed.ws, MIN_HAND_ALLOWS)
    assert p.command == (f"levain consent grant create --agent claude_code --tool Edit --workspace {seed.ws} "
                         "--posture default --expires 7d --signing-key <key>")
    assert PROPOSED_LIFE <= timedelta(days=30) and p.expires == "7d"


def test_the_proposed_line_parses_with_the_real_cli_parser(seed, monkeypatch):
    import levain.cli as cli

    for _ in range(3):
        seed.decide(agent="codex", tool="apply_patch", tool_input={"input": "*** Begin Patch"})
    seen = []
    monkeypatch.setattr(cli, "_cmd_consent_grant_create", lambda args: seen.append(args) or 0)
    argv = shlex.split(seed.propose()[0].command.replace("<key>", "/k/id"))
    assert argv[0] == "levain" and cli.main(argv[1:]) == 0
    ns = seen[0]
    assert (ns.agent, ns.tools, str(ns.workspace), ns.postures, ns.expires) == (
        "codex", ["apply_patch"], seed.ws, ["default"], "7d")


def test_below_the_threshold_no_card(seed):
    for _ in range(MIN_HAND_ALLOWS - 1):
        seed.decide()
    assert seed.propose() == []


def test_a_shell_tool_is_never_proposed(seed):
    for _ in range(5):
        seed.decide(tool="Bash", tool_input={"command": "ls"})
    assert seed.propose() == []


def test_an_mcp_write_tool_is_never_proposed_but_a_read_only_one_is(seed):
    for _ in range(4):
        seed.decide(tool="mcp__fs__write_file", tool_input={"path": f"{seed.ws}/a", "content": "x"})
    assert seed.propose() == []
    for _ in range(3):
        seed.decide(tool="mcp__fs__read_text_file", tool_input={"path": f"{seed.ws}/a"})
    got = seed.propose()
    assert [p.tool for p in got] == ["mcp__fs__read_text_file"]


def test_agent_config_tier_requests_do_not_count(seed):
    for _ in range(4):
        seed.decide(tier=AGENT_CONFIG_TIER)
    assert seed.propose() == []
    for _ in range(2):
        seed.decide()
    assert seed.propose() == []          # two ordinary ones beside the config ones are still two


def test_a_denial_withdraws_the_card(seed):
    for i in range(4):
        seed.decide(ago=timedelta(days=3, hours=i))
    seed.decide("denied", ago=timedelta(days=1))
    assert seed.propose() == []


def test_a_standing_grant_already_answering_withdraws_the_card(seed):
    for i in range(3):
        seed.decide(ago=timedelta(days=3, hours=i))
    assert len(seed.propose()) == 1
    seed.decide(via="grant", ago=timedelta(hours=2))
    assert seed.propose() == []


def test_keys_are_agent_tool_and_workspace(seed):
    for _ in range(2):
        seed.decide(cwd=seed.w("a"))
        seed.decide(cwd=seed.w("b"))
        seed.decide(agent="codex", tool="Edit", cwd=seed.w("a"))
    assert seed.propose() == []


def test_a_root_or_home_workspace_is_never_proposed(seed):
    home = str(Path.home())
    for _ in range(3):
        seed.decide(cwd="/")
        seed.decide(cwd=home)
    assert seed.propose() == []


def test_a_path_with_a_space_is_quoted(seed):
    for _ in range(3):
        seed.decide(cwd=seed.w("my proj"))
    assert f"--workspace {shlex.quote(seed.w('my proj'))}" in seed.propose()[0].command


def test_history_reaches_past_the_listing_day_but_not_past_the_window(seed):
    for i in range(3):
        seed.decide(ago=timedelta(days=10, hours=i))
    assert seed.propose() and read_listing(seed.dir, now=NOW).recent == ()
    for i in range(3):
        seed.decide(cwd=seed.w("old"), ago=timedelta(days=20, hours=i))
    assert [p.workspace for p in seed.propose()] == [seed.ws]


def test_cards_are_ordered_most_answered_first_and_capped(seed):
    for k in range(PROPOSAL_CAP + 2):
        for _ in range(3 + (k == PROPOSAL_CAP + 1)):
            seed.decide(cwd=seed.w(f"p{k}"))
    got = seed.propose()
    assert len(got) == PROPOSAL_CAP and got[0].workspace == seed.w(f"p{PROPOSAL_CAP + 1}") and got[0].count == 4


def _route(monkeypatch, seed, *, token: bool) -> tuple[int, dict]:
    import threading
    from types import SimpleNamespace

    import levain.web_server as ws

    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    h = ws._Handler.__new__(ws._Handler)
    sent: list = []
    h.server = SimpleNamespace(request_gate=threading.Lock(), consent_reader=ws._ConsentReader(seed.dir))
    h._off_box_token_valid = lambda: token
    h._send = lambda body, ctype, **kw: sent.append((kw.get("status", 200), json.loads(body)))
    h._serve_consent_body("/cockpit/consent/proposals.json", head=False)
    return sent[0]


def test_the_route_serves_the_cards_only_with_the_write_token(seed, monkeypatch):
    for _ in range(3):
        seed.decide()
    status, body = _route(monkeypatch, seed, token=False)
    assert status == 403 and "proposals" not in body
    status, body = _route(monkeypatch, seed, token=True)
    assert status == 200 and [p["tool"] for p in body["proposals"]] == ["Edit"]
    assert body["proposals"][0]["command"].startswith("levain consent grant create --agent claude_code ")


from tests.test_cockpit_consent_rows import OPEN_DECISIONS, TILE  # noqa: E402
from tests.test_cockpit_flip_parity import _drive, needs_browser  # noqa: E402
from tests.test_cockpit_k2a import _install, _serve  # noqa: E402

_CARDS = "[...document.querySelectorAll('#levain-modal [data-consent-proposal]')]"


@needs_browser
def test_the_page_draws_the_card_as_text_with_a_copy_button_and_signs_nothing(tmp_path, monkeypatch):
    seed = Seed(tmp_path)
    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    for i in range(3):
        seed.decide(cwd=seed.w("<img src=x onerror=window.__pwned=1>"), ago=timedelta(hours=2 + i))
    seed.clock[0] = datetime.now(timezone.utc)
    seed.q.file(agent="claude_code", agent_session_id="live", tool_name="Edit", tool_input={"file_path": "/w/a"},
                cwd="/w", hold=timedelta(minutes=30), event_id="live")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": f"{_CARDS}.length === 1"},
            {"eval": f"[{_CARDS}[0].querySelector('.consent-code').textContent, !!{_CARDS}[0].querySelector('.consent-copy'),"
                     f" !!window.__pwned, {_CARDS}[0].querySelectorAll('img').length]"},
        ])
    code, copy, pwned, imgs = out["values"][-1]
    assert code.startswith("levain consent grant create --agent claude_code --tool Edit --workspace ")
    assert "<img" in code and copy and not pwned and imgs == 0     # text, never markup
    assert out["posts"] == []                                      # the page sends nothing: it never signs


@needs_browser
def test_a_page_without_its_token_draws_no_card(tmp_path, monkeypatch):
    seed = Seed(tmp_path)
    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    for i in range(3):
        seed.decide(ago=timedelta(hours=2 + i))
    seed.clock[0] = datetime.now(timezone.utc)
    seed.q.file(agent="claude_code", agent_session_id="live", tool_name="Edit", tool_input={"file_path": "/w/a"},
                cwd="/w", hold=timedelta(minutes=30), event_id="live")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": "document.querySelectorAll('#levain-modal [data-consent-row]').length >= 1"},
            {"eval": "new Promise(r => setTimeout(() => r(document.querySelectorAll('#levain-modal [data-consent-proposal]').length), 1500))"},
        ])
    assert out["values"][-1] == 0


# ---- L3 r1 fixes (head triage 1017+4) -------------------------------------------------------------------------

def test_g2_more_than_500_newer_allows_of_other_keys_do_not_hide_a_card(seed):
    for i in range(3):
        seed.decide(ago=timedelta(days=9, hours=i))
    for i in range(520):
        seed.decide(cwd=seed.w(f"o{i % 300}"), tool="Write", ago=timedelta(hours=1 + i % 50))
    assert seed.ws in [p.workspace for p in seed.propose()]


def test_g2_more_than_500_newer_denials_of_other_keys_do_not_hide_the_keys_denial(seed):
    seed.decide("denied", ago=timedelta(days=12))
    for i in range(520):
        seed.decide("denied", cwd=seed.w(f"o{i % 300}"), tool="Write", ago=timedelta(days=2, minutes=i))
    for i in range(3):
        seed.decide(ago=timedelta(minutes=10 + i))
    assert seed.ws not in [p.workspace for p in seed.propose()]      # the older denial still withdraws the card


def test_g2_withdrawn_rows_are_not_evidence_and_keys_come_most_answered_first(seed):
    for i in range(6):
        seed.decide("withdrawn", via=None, ago=timedelta(hours=1 + i))
    assert seed.evidence() == []
    for k in range(4):
        for _ in range(3 + k):
            seed.decide(cwd=seed.w(f"p{k}"))
    assert [e.cwd for e in seed.evidence()] == [seed.w("p3"), seed.w("p2"), seed.w("p1"), seed.w("p0")]


def test_k1_200_unproposable_keys_with_more_allows_do_not_crowd_out_a_valid_card(seed):
    for k in range(210):
        for _ in range(4):
            seed.decide(cwd=seed.w(f"d{k}"), ago=timedelta(hours=3))
        seed.decide("denied", cwd=seed.w(f"d{k}"), ago=timedelta(hours=2))
    for _ in range(3):
        seed.decide(ago=timedelta(hours=1))
    assert [p.workspace for p in seed.propose()] == [seed.ws]


def test_f3_an_mcp_tool_with_an_empty_server_is_never_proposed(seed):
    for _ in range(3):
        seed.decide(tool="mcp____read_text_file", tool_input={"path": f"{seed.ws}/a"})
    assert seed.propose() == []


def test_f4_a_tie_in_decided_at_picks_the_later_id(seed, tmp_path):
    tie = timedelta(hours=3)                                              # one stamp for every row: only the id decides
    hand = [seed.decide(ago=tie) for _ in range(3)]
    grant = seed.decide(via="grant", ago=tie)
    while grant < max(hand):                                              # ids are random: file grants until one is the later
        grant = seed.decide(via="grant", ago=tie)
    assert seed.propose() == []                                           # the later row is a grant: it is already answering
    other = Seed(tmp_path / "two")
    grant = other.decide(via="grant", ago=tie)
    hand = [other.decide(ago=tie) for _ in range(3)]
    while max(hand) < grant:
        hand.append(other.decide(ago=tie))
    assert len(other.propose()) == 1                                      # the later row is a hand allow: still proposed


def _serve_route(seed, reader):
    import threading
    from types import SimpleNamespace

    import levain.web_server as ws

    h = ws._Handler.__new__(ws._Handler)
    sent: list = []
    h.server = SimpleNamespace(request_gate=threading.Lock(), consent_reader=reader)
    h._off_box_token_valid = lambda: True
    h._send = lambda body, ctype, **kw: sent.append((kw.get("status", 200), json.loads(body)))
    h._serve_consent_body("/cockpit/consent/proposals.json", head=False)
    return sent[0]


def test_g3_the_route_reads_the_directory_the_reader_names(seed, monkeypatch, tmp_path):
    import levain.web_server as ws

    for _ in range(3):
        seed.decide()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("LEVAIN_HOME", str(elsewhere))           # the default queue is a different (empty) one
    status, body = _serve_route(seed, ws._ConsentReader(seed.dir))
    assert status == 200 and [p["tool"] for p in body["proposals"]] == ["Edit"]
    assert ws._consent_reader().directory is None               # the default reader: the default queue, named explicitly


def test_g3_a_reader_without_a_directory_yields_no_cards_never_the_default_queue(seed, monkeypatch):
    for _ in range(3):
        seed.decide()
    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))      # the default queue IS the seeded one
    status, body = _serve_route(seed, lambda now: read_listing(seed.dir, now=now))
    assert status == 200 and body == {"proposals": []}


@needs_browser
def test_f1_a_page_with_only_old_history_shows_the_card(tmp_path, monkeypatch):
    seed = Seed(tmp_path)
    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    for i in range(3):
        seed.decide(ago=timedelta(days=3 + i))        # decided days ago: no pending row, nothing in the last 24 h
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": f"{_CARDS}.length === 1"},
            {"eval": f"{_CARDS}[0].querySelector('.consent-code').textContent"},
        ])
    assert out["values"][-1].startswith("levain consent grant create --agent claude_code --tool Edit ")


@needs_browser
def test_f6_a_rejected_read_is_not_cached_for_thirty_seconds(tmp_path, monkeypatch):
    seed = Seed(tmp_path)
    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    for i in range(3):
        seed.decide(ago=timedelta(days=3 + i))
    _root, src, _ids = _install(tmp_path)
    close = "(document.querySelector('#levain-modal .modal-close, #levain-modal [data-close], #levain-modal button.close')||{click(){}}).click()"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE},
            # the first proposals read is refused; the second (the view reopened) must read again, not reuse the failure
            {"eval": "(() => { const f = window.fetch; let n = 0; window.__calls = 0; window.fetch = function (u, o) {"
                     " if (String(u).includes('proposals.json')) { window.__calls++; if (n++ === 0) return Promise.reject(new TypeError('boom')); }"
                     " return f.apply(this, arguments); }; return 1; })()"},
            {"eval": OPEN_DECISIONS},
            {"until": "window.__calls >= 1"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 800))"},
            {"eval": "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); 1"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 500))"},
            {"eval": OPEN_DECISIONS},
            {"until": f"{_CARDS}.length === 1"},
            {"eval": "window.__calls"},
        ])
    assert out["values"][-1] == 2


@needs_browser
def test_f7_the_cards_follow_the_live_requests_and_precede_the_resolved(tmp_path, monkeypatch):
    seed = Seed(tmp_path, now=datetime.now(timezone.utc))
    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    for i in range(3):
        seed.decide(ago=timedelta(hours=2 + i))
    seed.clock[0] = datetime.now(timezone.utc)
    seed.q.file(agent="claude_code", agent_session_id="live", tool_name="Edit", tool_input={"file_path": "/w/a"},
                cwd="/w", hold=timedelta(minutes=30), event_id="live")
    _root, src, _ids = _install(tmp_path)
    heads = "[...document.querySelectorAll('#levain-modal .ext-grouphead')].map(h => h.textContent.trim().toLowerCase())"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": f"{_CARDS}.length === 1"},
            {"eval": heads},
        ])
    # the band block (S5b) leads the modal: this scenario has a live request and no questions.db yet, so the band draws
    # its heading with the store-absent note; the three cards keep their order beneath it
    assert out["values"][-1] == ["needs you \u00b7 the band", "agents waiting on you", "standing rule proposals", "resolved"]


@needs_browser
def test_g1_filtering_then_clearing_then_show_all_keeps_the_cards(tmp_path, monkeypatch):
    seed = Seed(tmp_path, now=datetime.now(timezone.utc))
    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    for i in range(3):
        seed.decide(ago=timedelta(hours=2 + i))
    for i in range(11):                                  # more than ten resolved rows: the "show all" control exists
        seed.decide(cwd=seed.w(f"x{i}"), tool="Write", ago=timedelta(hours=1, minutes=i))
    seed.clock[0] = datetime.now(timezone.utc)
    seed.q.file(agent="claude_code", agent_session_id="live", tool_name="Edit", tool_input={"file_path": "/w/a"},
                cwd="/w", hold=timedelta(minutes=30), event_id="live")
    _root, src, _ids = _install(tmp_path)
    box = "document.querySelector('#levain-modal input[type=search], #levain-modal input.ext-search, #levain-modal input[type=text]')"
    typed = ("(() => { const b = " + box + "; b.value = '%s'; b.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()")
    pause = "new Promise(r => setTimeout(() => r(1), 700))"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": f"{_CARDS}.length === 1"},
            {"eval": typed % "zzz-nothing-matches"}, {"eval": pause},
            {"eval": typed % ""}, {"eval": pause},
            {"eval": f"{_CARDS}.length"},
            {"eval": "(() => { const m = [...document.querySelectorAll('#levain-modal button')].find(b => /show all/i.test(b.textContent)); if (!m) return 0; m.click(); return 1; })()"},
            {"eval": pause},
            {"eval": f"{_CARDS}.length"},
        ])
    assert out["values"][-3] == 1, "the show-all control was not found, so it was never clicked"
    assert out["values"][-4] == 1 and out["values"][-1] == 1


def test_a_tool_off_the_logs_allowlist_is_never_proposed(seed):
    """0.11.5: the log refuses a grant naming a tool it does not bound to the workspace, so the card never offers one."""
    for _ in range(MIN_HAND_ALLOWS):
        seed.decide(agent="claude_code", tool="Task", cwd=seed.w("a"), tool_input={"prompt": "x"})
        seed.decide(agent="codex", tool="write_stdin", cwd=seed.w("b"), tool_input={"chars": "x"})
        seed.decide(agent="claude_code", tool="Edit", cwd=seed.w("c"))
    assert [(p.agent, p.tool) for p in seed.propose()] == [("claude_code", "Edit")]


# ---- L3 int r1 (1017+21): nothing the log would refuse at `grant create` is proposed ----------------------------

@pytest.mark.parametrize("tool", ["mcp___srv__read_file", "mcp__bad server__read_file",
                                  "mcp__" + "s" * 130 + "__read_file"])
def test_a_tool_name_the_log_refuses_the_shape_of_is_never_proposed(seed, tool):
    for _ in range(3):
        seed.decide(tool=tool, tool_input={"path": f"{seed.ws}/a"})
    assert seed.propose() == []


def test_a_deleted_workspace_is_never_proposed(seed):
    gone = seed.w("gone")
    for _ in range(3):
        seed.decide(cwd=gone)
    assert [p.workspace for p in seed.propose()] == [gone]
    Path(gone).rmdir()
    assert seed.propose() == []


def test_a_workspace_that_resolves_to_home_is_never_proposed(seed):
    link = Path(seed.root) / "homelink"
    link.symlink_to(Path.home())
    for _ in range(3):
        seed.decide(cwd=str(link))
    assert seed.propose() == []


def test_a_symlinked_workspace_is_proposed_as_the_directory_it_resolves_to(seed):
    link = Path(seed.root) / "alias"
    link.symlink_to(seed.ws)
    for _ in range(3):
        seed.decide(cwd=str(link))
    got = seed.propose()
    assert [p.workspace for p in got] == [seed.ws] and f"--workspace {seed.ws} " in got[0].command


def test_a_nul_relative_or_tilde_cwd_is_never_proposed_and_hides_no_other_card(seed):
    """A hook's cwd is absolute and has no NUL; one malformed key must not take the other cards down with it
    (L3 int r2: a NUL made realpath raise inside propose)."""
    for bad in ("/w\x00x", "work/proj", "~/proj"):
        for _ in range(3):
            seed.decide(cwd=bad)
    for _ in range(3):
        seed.decide(cwd=seed.ws)
    assert [p.workspace for p in seed.propose()] == [seed.ws]


def test_the_proposal_agrees_with_what_grant_create_accepts(seed):
    from levain.authority import consent_resolved_workspace, consent_tool_shape_problem
    assert consent_resolved_workspace(seed.ws) == (seed.ws, None)
    assert consent_resolved_workspace(seed.root / "nope")[0] is None
    assert consent_tool_shape_problem("mcp__srv__read_file") is None
    assert consent_tool_shape_problem("mcp___srv__read_file") and consent_tool_shape_problem("a" * 129)


@needs_browser
def test_a_dashboard_reload_drops_the_cached_proposals(tmp_path, monkeypatch):
    seed = Seed(tmp_path)
    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    for i in range(3):
        seed.decide(ago=timedelta(days=3 + i))
    _root, src, _ids = _install(tmp_path)
    esc = "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); 1"
    wait = "new Promise(r => setTimeout(() => r(1), 600))"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE},
            {"eval": "(() => { const f = window.fetch; window.__calls = 0; window.fetch = function (u, o) {"
                     " if (String(u).includes('proposals.json')) window.__calls++; return f.apply(this, arguments); }; return 1; })()"},
            {"eval": OPEN_DECISIONS}, {"until": f"{_CARDS}.length === 1"},
            {"eval": esc}, {"eval": wait},
            {"eval": OPEN_DECISIONS}, {"until": f"{_CARDS}.length === 1"},
            {"eval": "window.__calls"},                                   # a fresh open reads again
            {"eval": esc}, {"eval": wait},
            {"eval": "window.LevainCockpitTransport.reload().then(() => 1)"},
            {"eval": wait},
            {"eval": OPEN_DECISIONS}, {"until": f"{_CARDS}.length === 1"},
            {"eval": "window.__calls"},                                   # after a reload: read again
        ])
    v = out["values"]
    assert v[8] == 2 and v[-1] > v[8], v   # a fresh open reads again (2nd read); a reload and open read again after it
