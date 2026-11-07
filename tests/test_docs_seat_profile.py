"""docs/seat-profile.md names commands, flags, probe ids, tools and limits. Each is derived here from the code (the real
argparse tree, the canary's own parser, the constants) so the page cannot drift from it. The page's prose is not
checked; every name and number in it that the code owns is."""
from __future__ import annotations

import argparse
import contextlib
import io
import re
from pathlib import Path

import pytest

from levain import authority, seat_canary, seat_profile, seat_push
from levain.cli import build_parser
from levain.host_guard import CC_SEAT_TOOLS, PROTECTED_NAMES

DOC = Path(__file__).resolve().parents[1] / "docs" / "seat-profile.md"
TEXT = DOC.read_text(encoding="utf-8")


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _seat() -> argparse.ArgumentParser:
    return _subparsers(build_parser())["seat"]


def _options(parser: argparse.ArgumentParser) -> set[str]:
    return {o for a in parser._actions for o in a.option_strings}


def _resolve(words: list[str]) -> argparse.ArgumentParser | None:
    node = _seat()
    for w in words:
        subs = _subparsers(node)
        if w not in subs:
            return None
        node = subs[w]
    return node


def _canary_help() -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
        seat_canary.main(["-h"])
    return out.getvalue()


def _canary_defaults(monkeypatch) -> dict:
    seen: dict = {}

    class _V:
        def ok(self) -> bool:
            return True

        def report(self) -> str:
            return ""

    def fake(**kw):
        seen.update(kw)
        return _V()

    monkeypatch.setattr(seat_canary, "run_seat_canary", fake)
    with contextlib.redirect_stdout(io.StringIO()):
        seat_canary.main(["--claude-bin", "/x/claude"])
    return seen


def _fenced_commands() -> list[list[str]]:
    cmds = []
    for block in re.findall(r"```\n(.*?)```", TEXT, re.S):
        for line in block.splitlines():
            if line.startswith("levain seat"):
                cmds.append(line.split())
    return cmds


def test_the_page_names_at_least_the_documented_commands():
    heads = {tuple(c[2:4]) for c in _fenced_commands()}
    assert ("canary", "--claude-bin") in heads
    assert {("egress", s) for s in ("create", "renew", "revoke", "list")} <= heads


def test_every_fenced_command_and_flag_exists_in_the_real_parser():
    for cmd in _fenced_commands():
        path = []
        for tok in cmd[2:]:
            if tok.startswith(("-", "<", "[")):
                break
            path.append(tok)
        if path[0] == "canary":
            known = set(re.findall(r"--[a-z][a-z-]*", _canary_help()))
        else:
            node = _resolve(path)
            assert node is not None, f"`{' '.join(cmd)}`: no such subcommand path"
            known = _options(node)
        for flag in re.findall(r"--[a-z][a-z-]*", " ".join(cmd)):
            assert flag in known, f"`{' '.join(cmd)}` names {flag}, which the parser does not have"


def test_the_fenced_egress_commands_name_every_required_flag():
    for cmd in _fenced_commands():
        if cmd[2:3] != ["egress"]:
            continue
        node = _resolve(cmd[2:4])
        required = {o for a in node._actions if a.required and a.option_strings for o in a.option_strings[:1]}
        for flag in required:
            assert flag in cmd, f"`{' '.join(cmd)}` omits the required {flag}"
        positionals = [a.dest for a in node._actions if not a.option_strings and a.dest != "help"]
        for dest in positionals:
            assert f"<{dest.replace('_', ' ')}>" in cmd or dest == "grant_id", dest


def test_every_seat_subcommand_the_prose_names_exists():
    seat = _subparsers(_seat())
    for word in re.findall(r"levain seat (\w+)", TEXT):
        assert word in seat, f"`levain seat {word}` is not a subcommand"
    egress = _subparsers(seat["egress"])
    for word in re.findall(r"levain seat egress (\w+)", TEXT):
        assert word in egress
    assert set(egress) == {"create", "renew", "revoke", "list"}
    lease = _subparsers(seat["lease"])
    m = re.search(r"levain seat lease create\|renew\|revoke\|list", TEXT)
    assert m and set(lease) == {"create", "renew", "revoke", "list"}


def test_the_egress_table_covers_every_egress_subcommand():
    egress = _subparsers(_subparsers(_seat())["egress"])
    for name in egress:
        assert re.search(rf"\| `{name}` \|", TEXT), f"the egress table has no row for {name}"


def test_the_egress_flags_the_page_explains_are_the_creates_own():
    create = _resolve(["egress", "create"])
    for flag in ("--lanes-parent", "--remote", "--allow-domain"):
        assert f"`{flag}`" in TEXT and flag in _options(create)
    assert create.get_default("allow_domains") == []
    for flag in ("--path", "--data-root", "--signing-key"):
        assert flag in _options(create)


def test_the_canary_flag_table_matches_the_canary_parser(monkeypatch):
    helptext = _canary_help()
    table = set(re.findall(r"^\| `(--[a-z-]+)` \|", TEXT, re.M))
    assert table == {"--claude-bin", "--python", "--swap-attempts", "--timeout", "--tools"}
    assert table == set(re.findall(r"--[a-z][a-z-]*", helptext)) - {"--help"}
    d = _canary_defaults(monkeypatch)
    assert d["swap_attempts"] == 20 and "| `--swap-attempts` | `20` |" in TEXT
    assert d["timeout"] == 600.0 and "| `--timeout` | `600` seconds |" in TEXT
    assert tuple(d["tools"]) == CC_SEAT_TOOLS and ",".join(CC_SEAT_TOOLS) in TEXT


def test_the_canary_passes_arguments_through_without_a_wrapper_parser():
    canary = _subparsers(_seat())["canary"]
    assert canary.prefix_chars == "\0"
    assert "-h" in _canary_help()


def test_probe_rows_are_exactly_the_probe_set():
    rows = re.findall(r"^\| (P\d+b?) ", TEXT, re.M)
    assert rows == list(seat_canary.SEAT_PROBE_IDS)
    named = {p.id: p.name for p in seat_canary.SEAT_PROBES}
    for pid, name in named.items():
        assert re.search(rf"^\| {pid} {re.escape(name)} \|", TEXT, re.M), f"{pid} {name}"


def test_the_skipped_probe_is_the_one_the_default_set_omits():
    assert "NotebookEdit" not in CC_SEAT_TOOLS
    assert seat_canary.READY_TOOL_PROBES == {"NotebookEdit": "P9"}
    assert seat_canary.TOOL_PROBES == {"Glob": "P10", "Grep": "P10"}
    assert seat_canary.SWAP_FALLBACK_TOOLS == ("Bash", "Read")
    assert "drops to Bash and Read" in TEXT
    with pytest.raises(ValueError):
        seat_canary.check_canary_tools(["Bash", "NotebookEdit"])


def test_the_verdict_words_are_the_checks_statuses():
    for word in ("PASS", "FAIL", "INCONCLUSIVE", "SKIPPED"):
        assert word in TEXT
    src = Path(seat_canary.__file__).read_text(encoding="utf-8")
    for status in ('"pass"', '"fail"', '"inconclusive"'):
        assert status in src


def test_the_tool_set_the_page_allows_is_the_code_s():
    row = next(line for line in TEXT.splitlines() if line.startswith("| Tools |"))
    assert tuple(t.strip() for t in re.search(r"\| Tools \| (.*?)\.", row).group(1).split(",")) == CC_SEAT_TOOLS


def test_protected_names_and_git_paths_are_the_codes():
    line = next(l for l in TEXT.splitlines() if l.startswith("| Agent-config names"))
    names = set(re.findall(r"`([^`]+)`", line.split("|")[2]))
    assert names == {"activation/", ".levain/", ".claude/", ".codex/", ".mcp.json"}
    assert {n.rstrip("/") for n in names} == PROTECTED_NAMES - {".git"}
    git_line = next(l for l in TEXT.splitlines() if l.startswith("| `.git` paths"))
    git_paths = set(re.findall(r"`\.git/(\w+)`", git_line))
    assert git_paths == set(seat_profile._GIT_DENY)
    assert set(seat_profile.FILE_TOOLS) == {"Edit", "Write", "MultiEdit", "NotebookEdit"}
    assert "Edit, Write, MultiEdit and NotebookEdit are denied" in TEXT


def test_the_profile_flags_the_page_names_are_the_argv_s():
    src = Path(seat_profile.__file__).read_text(encoding="utf-8")
    for flag in ("--restricted", "--strict-mcp-config", "--tools", "--settings"):
        assert flag in TEXT and f'"{flag}"' in src
    assert seat_profile.SEAT_SOURCE_ARGS == ("--setting-sources", "")
    assert '"acceptEdits"' in src and "`acceptEdits`" in TEXT


def test_the_numbers_the_page_states_are_the_codes():
    assert authority.SEAT_EGRESS_MAX.days == 14 and "14 days" in TEXT and "14 more days" in TEXT
    assert authority.SEAT_LEASE_MAX.days == 14
    assert authority._SEAT_EGRESS_DOMAINS_MAX == 16 and "At most 16" in TEXT
    assert seat_push.STOP_TIMEOUT == 180 and seat_push.SESSION_END_TIMEOUT == 60
    assert "180 seconds (`Stop`) and 60 seconds (`SessionEnd`)" in TEXT
    assert seat_push.LOG_NAME in TEXT and f"push/{seat_push.LOG_NAME}" in TEXT
    assert seat_profile.CANARY_DIRNAME in TEXT


def test_the_branch_the_page_promises_is_the_hook_s():
    src = Path(seat_push.__file__).read_text(encoding="utf-8")
    assert 'f"seat/{' in src and "seat/<lane key>" in TEXT


def test_launch_and_stop_are_left_for_when_they_land():
    assert "documented when they land" in TEXT or "documented here when the launcher lands" in TEXT
    assert "--lanes-parent" in TEXT
    # no launch or stop flag is described here
    for flag in ("--base", "--claude"):
        assert f"`{flag}`" not in TEXT
