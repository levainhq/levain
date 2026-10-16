"""Doctor's advisory row for the session transcript hooks (Q8: Stop, PostToolUse, and Claude Code's
PostToolUseFailure). Measured through the CLI (1016+1): installs made by 17ad2c5, before those hooks existed,
showed the WARN, and `levain update` from this build wired them and turned the row OK."""

from __future__ import annotations

import json
from pathlib import Path

from levain import doctor
from tests.test_repin_on_install import make_install  # noqa: F401  (fixture)


def _row(results, agent: str):
    rows = [r for r in results if r.name == f"{agent} transcript hooks"]
    assert len(rows) <= 1, rows
    return rows[0] if rows else None


def _settings(install: Path) -> tuple[Path, dict]:
    p = install / ".claude" / "settings.json"
    return p, json.loads(p.read_text(encoding="utf-8"))


def test_a_fresh_claude_code_install_has_every_transcript_hook(make_install):
    cc = make_install("cc")
    r = _row(doctor._check_claude_code(cc), "Claude Code")
    assert r.ok and not r.warn and "Stop, PostToolUse, PostToolUseFailure" in r.detail, r


def test_the_auto_memory_group_alone_does_not_count_as_the_transcript_hook(make_install):
    cc = make_install("cc")
    p, doc = _settings(cc)
    doc["hooks"]["PostToolUse"] = [g for g in doc["hooks"]["PostToolUse"]
                                   if "automemory_mirror.py" in json.dumps(g)]
    del doc["hooks"]["Stop"]
    p.write_text(json.dumps(doc), encoding="utf-8")
    r = _row(doctor._check_claude_code(cc), "Claude Code")
    assert r.ok and r.warn, r
    assert "not wired: Stop, PostToolUse" in r.detail and "PostToolUseFailure" not in r.detail
    assert "levain update" in r.hint


def test_a_codex_install_has_its_transcript_hooks(make_install):
    cx = make_install("cx", adapter="codex")
    r = _row(doctor._check_codex(cx), "Codex")
    assert r.ok and not r.warn and "Stop, PostToolUse" in r.detail, r


def test_a_codex_install_that_does_not_own_the_file_gets_no_transcript_row(make_install):
    a = make_install("a", adapter="codex")
    make_install("b", adapter="codex")
    assert _row(doctor._check_codex(a), "Codex") is None


def test_a_codex_hooks_file_without_stop_warns(make_install, tmp_path):
    cx = make_install("cx", adapter="codex")
    cj = tmp_path / "codex-home" / "hooks.json"
    doc = json.loads(cj.read_text(encoding="utf-8"))
    del doc["hooks"]["Stop"]
    cj.write_text(json.dumps(doc), encoding="utf-8")
    r = _row(doctor._check_codex(cx), "Codex")
    assert r.warn and "not wired: Stop, so" in r.detail, r


# L1 (run): a hand-edited Stop/PostToolUse entry crashed doctor through this advisory row.
def test_malformed_transcript_entries_warn_and_never_crash(make_install):
    cc = make_install("cc")
    p, doc = _settings(cc)
    doc["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": 5}]},
                            {"hooks": "not a list"}, "not a group"]
    doc["hooks"]["PostToolUseFailure"] = [{"hooks": [{"type": "command", "command": "python /tmp/a\x00b.py"}]}]
    p.write_text(json.dumps(doc), encoding="utf-8")
    r = _row(doctor._check_claude_code(cc), "Claude Code")
    assert r.warn and "Stop" in r.detail and "PostToolUseFailure" in r.detail, r


def test_a_stop_hook_running_another_installs_script_is_not_wired(make_install, tmp_path):
    cc = make_install("cc")
    other = make_install("other")
    p, doc = _settings(cc)
    doc["hooks"]["Stop"] = [{"hooks": [{"type": "command",
                                        "command": f'python3 "{other}/activation/hooks/stop.py"'}]}]
    p.write_text(json.dumps(doc), encoding="utf-8")
    r = _row(doctor._check_claude_code(cc), "Claude Code")
    assert r.warn and "not wired: Stop, so" in r.detail, r
