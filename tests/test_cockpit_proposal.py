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
from levain.consent.listing import read_history, read_listing
from levain.consent.policy import AGENT_CONFIG_TIER
from levain.consent.queue import ConsentQueue, authority_source

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
WS = "/work/proj"


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Seed:
    """Files requests at a moving clock and decides them in the store."""

    def __init__(self, base: Path) -> None:
        self.dir = base / "consent"
        self.dir.mkdir(mode=0o700)
        self.clock = [NOW - timedelta(days=5)]
        self.q = ConsentQueue(self.dir, authority=authority_source(self.dir, anchor=None), clock=lambda: self.clock[0])
        self.n = 0

    def decide(self, state: str = "allowed", *, via: str | None = "signature", agent: str = "claude_code",
               tool: str = "Edit", cwd: str = WS, tier: str = "T2", tool_input: dict | None = None,
               ago: timedelta = timedelta(hours=1)) -> None:
        self.n += 1
        self.clock[0] = NOW - ago
        r = self.q.file(agent=agent, agent_session_id=f"s{self.n}", tool_name=tool,
                        tool_input=tool_input or {"file_path": f"{cwd}/f{self.n}.txt", "content": "x"}, cwd=cwd,
                        hold=timedelta(minutes=5), tier=tier, event_id=f"e{self.n}")
        conn = sqlite3.connect(self.dir / "queue.db", isolation_level=None)
        try:
            conn.execute("UPDATE requests SET state = ?, decided_at = ?, via = ?, signer_fp = ? WHERE id = ?",
                         (state, _stamp(NOW - ago + timedelta(seconds=20)), via, "SHA256:testkey", r.id))
        finally:
            conn.close()

    def propose(self):
        return propose(read_history(self.dir, now=NOW))


@pytest.fixture
def seed(tmp_path):
    return Seed(tmp_path)


def test_three_hand_allows_make_a_card_with_the_exact_grant_line(seed):
    for i in range(MIN_HAND_ALLOWS):
        seed.decide(ago=timedelta(days=2, hours=i))
    got = seed.propose()
    assert len(got) == 1
    p = got[0]
    assert (p.agent, p.tool, p.workspace, p.count) == ("claude_code", "Edit", WS, MIN_HAND_ALLOWS)
    assert p.command == ("levain consent grant create --agent claude_code --tool Edit --workspace /work/proj "
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
        "codex", ["apply_patch"], WS, ["default"], "7d")


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
        seed.decide(tool="mcp__fs__write_file", tool_input={"path": f"{WS}/a", "content": "x"})
    assert seed.propose() == []
    for _ in range(3):
        seed.decide(tool="mcp__fs__read_text_file", tool_input={"path": f"{WS}/a"})
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
        seed.decide(cwd="/work/a")
        seed.decide(cwd="/work/b")
        seed.decide(agent="codex", tool="Edit", cwd="/work/a")
    assert seed.propose() == []


def test_a_root_or_home_workspace_is_never_proposed(seed):
    home = str(Path.home())
    for _ in range(3):
        seed.decide(cwd="/")
        seed.decide(cwd=home)
    assert seed.propose() == []


def test_a_path_with_a_space_is_quoted(seed):
    for _ in range(3):
        seed.decide(cwd="/work/my proj")
    assert "--workspace '/work/my proj'" in seed.propose()[0].command


def test_history_reaches_past_the_listing_day_but_not_past_the_window(seed):
    for i in range(3):
        seed.decide(ago=timedelta(days=10, hours=i))
    assert seed.propose() and read_listing(seed.dir, now=NOW).recent == ()
    for i in range(3):
        seed.decide(cwd="/work/old", ago=timedelta(days=20, hours=i))
    assert [p.workspace for p in seed.propose()] == [WS]


def test_cards_are_ordered_most_answered_first_and_capped(seed):
    for k in range(PROPOSAL_CAP + 2):
        for _ in range(3 + (k == 7)):
            seed.decide(cwd=f"/work/p{k}")
    got = seed.propose()
    assert len(got) == PROPOSAL_CAP and got[0].workspace == "/work/p7" and got[0].count == 4


def _route(monkeypatch, seed, *, token: bool) -> tuple[int, dict]:
    import threading
    from types import SimpleNamespace

    import levain.web_server as ws

    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    h = ws._Handler.__new__(ws._Handler)
    sent: list = []
    h.server = SimpleNamespace(request_gate=threading.Lock(), consent_reader=lambda now: read_listing(seed.dir, now=now))
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


from tests.test_cockpit_consent_rows import TILE  # noqa: E402
from tests.test_cockpit_flip_parity import _drive, needs_browser  # noqa: E402
from tests.test_cockpit_k2a import _install, _serve  # noqa: E402

_CARDS = "[...document.querySelectorAll('#levain-modal [data-consent-proposal]')]"


@needs_browser
def test_the_page_draws_the_card_as_text_with_a_copy_button_and_signs_nothing(tmp_path, monkeypatch):
    seed = Seed(tmp_path)
    monkeypatch.setenv("LEVAIN_HOME", str(seed.dir.parent))
    for i in range(3):
        seed.decide(cwd="/work/<img src=x onerror=window.__pwned=1>", ago=timedelta(hours=2 + i))
    seed.clock[0] = datetime.now(timezone.utc)
    seed.q.file(agent="claude_code", agent_session_id="live", tool_name="Edit", tool_input={"file_path": "/w/a"},
                cwd="/w", hold=timedelta(minutes=30), event_id="live")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": TILE}, {"eval": f"{TILE}.click(); 1"},
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
            {"until": TILE}, {"eval": f"{TILE}.click(); 1"},
            {"until": "document.querySelectorAll('#levain-modal .consent-row').length >= 1"},
            {"eval": "new Promise(r => setTimeout(() => r(document.querySelectorAll('#levain-modal [data-consent-proposal]').length), 1500))"},
        ])
    assert out["values"][-1] == 0
