"""The per-prompt, change-only state line (spore-1410): session_start shows the operator's
state line and seeds a per-session marker; user_prompt_submit speaks only when it changed or
went away. The real hook scripts run as subprocesses against a tmp install, for both
adapters. Behaviour reference: flow's scripts/state_line_hook.py."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TEMPLATES = Path(__file__).resolve().parents[1] / "levain" / "templates"
_TREES = {
    "claude": _TEMPLATES / "activation" / "hooks",
    "codex": _TEMPLATES / "adapters" / "codex" / "activation" / "hooks",
}
_HOOK_FILES = ("_levain_hook.py", "session_start.py", "user_prompt_submit.py", "automemory_mirror.py")
CLEARED = "[state] line cleared or expired"


def _iso(hours_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


class Install:
    def __init__(self, root: Path, tree: Path):
        self.root = root
        hooks = root / "activation" / "hooks"
        hooks.mkdir(parents=True)
        for name in _HOOK_FILES:
            if (tree / name).exists():
                shutil.copy(tree / name, hooks / name)
        (root / "activation" / "recency_directives.md").write_text(
            "# directives\n\n## one\nRECENCY-DIRECTIVE\n", encoding="utf-8")
        (root / ".levain").mkdir()
        self.hooks = hooks

    def set_state(self, text: str | None, hours_ago: float = 0.0, source: str = "web") -> None:
        body = {} if text is None else {
            "state": text, "state_set_at": _iso(hours_ago), "state_source": source}
        (self.root / ".levain" / "context.json").write_text(json.dumps(body), encoding="utf-8")

    def run(self, script: str, payload: dict) -> str:
        proc = subprocess.run(
            [sys.executable, str(self.hooks / script)], input=json.dumps(payload),
            capture_output=True, text=True, cwd=self.root, timeout=60,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.root)})
        assert proc.returncode == 0 and proc.stderr == ""
        if not proc.stdout.strip():
            return ""
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]

    def start(self, sid="s1", source="startup") -> str:
        return self.run("session_start.py", {"session_id": sid, "source": source})

    def prompt(self, sid="s1", text="hello there") -> str:
        return self.run("user_prompt_submit.py", {"session_id": sid, "prompt": text})

    def markers(self) -> list[Path]:
        d = self.root / ".levain" / "state_line_seen"
        return sorted(d.iterdir()) if d.exists() else []


@pytest.fixture(params=sorted(_TREES))
def inst(request, tmp_path) -> Install:
    return Install(tmp_path, _TREES[request.param])


def _state_lines(out: str) -> list[str]:
    return [p for p in out.split("\n\n") if p.startswith("[state]")]


def test_seeded_at_start_then_same_line_is_silent(inst):
    inst.set_state("shipping the release")
    assert "shipping the release" in inst.start()
    assert _state_lines(inst.prompt()) == []
    assert _state_lines(inst.prompt()) == []


def test_a_change_is_emitted_verbatim_once(inst):
    inst.set_state("shipping the release")
    inst.start()
    inst.set_state("out walking")
    first = _state_lines(inst.prompt())
    assert len(first) == 1 and '"out walking"' in first[0]
    assert first[0].startswith("[state] The operator's own words")
    assert _state_lines(inst.prompt()) == []


def test_the_prompt_line_is_the_session_start_line(inst):
    inst.set_state("same words", source="web")
    started = _state_lines(inst.start())[0]
    inst.set_state("same words", hours_ago=0.0, source="web")  # fresh set-at: a change
    emitted = _state_lines(inst.prompt())[0]
    assert emitted.split(": ", 1)[1] == started.split(": ", 1)[1]  # same text, age differs


def test_a_clear_is_announced_once(inst):
    inst.set_state("shipping the release")
    inst.start()
    inst.set_state(None)
    assert _state_lines(inst.prompt()) == [CLEARED]
    assert _state_lines(inst.prompt()) == []


def test_expiry_past_twelve_hours_reads_as_cleared_once(inst):
    inst.set_state("old news", hours_ago=11.9)
    assert "old news" in inst.start()
    inst.set_state("old news", hours_ago=12.5)
    assert _state_lines(inst.prompt()) == [CLEARED]
    assert _state_lines(inst.prompt()) == []


def test_seeded_with_no_line_stays_silent_until_one_appears(inst):
    inst.set_state(None)
    inst.start()
    assert _state_lines(inst.prompt()) == []
    assert [p.read_text() for p in inst.markers()] == [""]  # seeded even with no live line
    inst.set_state("now there is one")
    assert '"now there is one"' in _state_lines(inst.prompt())[0]


def test_unknown_marker_shows_the_live_line(inst):
    inst.set_state("never seeded")
    assert '"never seeded"' in _state_lines(inst.prompt("fresh"))[0]
    assert _state_lines(inst.prompt("fresh")) == []


def test_unknown_marker_and_no_line_is_silent(inst):
    inst.set_state(None)
    assert _state_lines(inst.prompt("fresh")) == []


def test_corrupt_context_is_silent_and_marker_unchanged(inst):
    inst.set_state("shipping the release")
    inst.start()
    before = [(p.name, p.read_text()) for p in inst.markers()]
    (inst.root / ".levain" / "context.json").write_text("{not json", encoding="utf-8")
    assert _state_lines(inst.prompt()) == []
    assert [(p.name, p.read_text()) for p in inst.markers()] == before
    inst.set_state("shipping the release")  # parseable again, different set-at
    assert len(_state_lines(inst.prompt())) == 1  # the change was not swallowed


def test_no_session_id_is_silent_and_writes_no_marker(inst):
    inst.set_state("a line")
    out = inst.run("user_prompt_submit.py", {"prompt": "hello there"})
    assert _state_lines(out) == [] and "RECENCY-DIRECTIVE" in out
    inst.run("session_start.py", {"source": "startup"})
    assert inst.markers() == []


def test_compact_reseeds_the_marker(inst):
    inst.set_state("first")
    inst.start()
    inst.set_state("second")
    assert "second" in inst.start(source="compact")  # compact re-shows and re-seeds
    assert _state_lines(inst.prompt()) == []


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
def test_every_source_seeds(inst, source):
    inst.set_state("a line")
    inst.start(source=source)
    assert len(inst.markers()) == 1 and inst.markers()[0].read_text() != ""


def test_markers_are_per_session_and_hold_no_state_text(inst):
    inst.set_state("a private sentence")
    inst.start("a")
    inst.start("b")
    assert len(inst.markers()) == 2
    assert all("private" not in p.read_text() and len(p.name) == 24 for p in inst.markers())
    inst.set_state("changed")
    assert len(_state_lines(inst.prompt("a"))) == 1
    assert len(_state_lines(inst.prompt("b"))) == 1


def test_marker_store_is_pruned_and_stale_tmp_removed(inst):
    import os
    import time
    d = inst.root / ".levain" / "state_line_seen"
    d.mkdir()
    for i in range(205):
        (d / f"old{i:03d}").write_text("")
        os.utime(d / f"old{i:03d}", (1000 + i, 1000 + i))
    stale = d / "x.1.tmp"
    stale.write_text("")
    os.utime(stale, (time.time() - 7200,) * 2)
    inst.set_state("a line")
    inst.start("new")
    names = {p.name for p in d.iterdir()}
    assert len(names) <= 200 and "old000" not in names and not stale.exists()
    assert any(len(n) == 24 for n in names)


def test_a_fault_in_the_state_section_drops_only_it(inst):
    inst.set_state("a line")
    inst.start()
    with (inst.hooks / "_levain_hook.py").open("a", encoding="utf-8") as f:
        f.write("\n\ndef state_line_pending(session_id):\n    raise RuntimeError('boom')\n"
                "\n\ndef state_line():\n    raise RuntimeError('boom')\n")
    inst.set_state("changed")
    out = inst.prompt()
    assert "RECENCY-DIRECTIVE" in out and _state_lines(out) == []
    assert "[session orientation]" in inst.start(sid="other")


def _run_closed_stdout(inst, script, payload):
    # The harness closed the pipe: emit() cannot deliver, whatever else the hook did.
    proc = subprocess.Popen(
        [sys.executable, str(inst.hooks / script)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=inst.root,
        env={"PATH": "/usr/bin:/bin", "HOME": str(inst.root)})
    proc.stdout.close()
    proc.stdin.write(json.dumps(payload).encode())
    proc.stdin.close()
    err = proc.stderr.read()
    proc.wait(timeout=60)
    # silent exit 0: no 'Exception ignored on flushing sys.stdout' / exit 120 at shutdown
    assert proc.returncode == 0 and err == b"", (proc.returncode, err)


def test_a_marker_is_not_advanced_when_the_prompt_output_was_not_delivered(inst):
    inst.set_state("first")
    inst.start()
    before = [m.read_text() for m in inst.markers()]
    inst.set_state("second")
    _run_closed_stdout(inst, "user_prompt_submit.py", {"session_id": "s1", "prompt": "hi"})
    assert [m.read_text() for m in inst.markers()] == before
    assert "second" in "\n".join(_state_lines(inst.prompt()))  # the change is still owed


def test_a_marker_is_not_seeded_when_the_start_output_was_not_delivered(inst):
    inst.set_state("first")
    _run_closed_stdout(inst, "session_start.py", {"session_id": "s1", "source": "startup"})
    assert inst.markers() == []
    assert "first" in "\n".join(_state_lines(inst.prompt()))


def test_a_later_section_raising_keeps_the_state_line_and_the_marker(inst):
    inst.set_state("a line")
    with (inst.hooks / "_levain_hook.py").open("a", encoding="utf-8") as f:
        f.write("\n\ndef pack_drift():\n    raise RuntimeError('boom')\n")
    out = inst.start()
    assert "a line" in out
    assert inst.prompt().count("a line") == 0 and len(inst.markers()) == 1


def test_an_old_session_start_calling_focus_notice_loses_nothing(inst):
    inst.set_state("a line")
    p = inst.hooks / "session_start.py"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "        sections: list[str] = []\n",
        "        sections: list[str] = []\n        hook.focus_notice()\n", 1), encoding="utf-8")
    out = inst.start()
    assert "[session orientation]" in out and "a line" in out
