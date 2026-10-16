"""The per-session registry the hooks write for the cockpit's Sessions panel (Q7a, lane A):
`<install>/.levain/sessions/<sha1(agent:session_id)[:24]>.json`. A DISPLAY, never an authority;
fail-soft. The real hook scripts run as subprocesses against a tmp install, for both adapters."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_TEMPLATES = Path(__file__).resolve().parents[1] / "levain" / "templates"
_TREES = {
    "claude_code": _TEMPLATES / "activation" / "hooks",
    "codex": _TEMPLATES / "adapters" / "codex" / "activation" / "hooks",
}
_HOOK_FILES = ("_levain_hook.py", "session_start.py", "user_prompt_submit.py", "automemory_mirror.py",
               "stop.py", "post_tool_use.py")


def _install(root: Path, tree: Path) -> Path:
    hooks = root / "activation" / "hooks"
    hooks.mkdir(parents=True)
    for name in _HOOK_FILES:
        if (tree / name).exists():
            shutil.copy(tree / name, hooks / name)
    (root / "activation" / "recency_directives.md").write_text(
        "# directives\n\n## one\nRECENCY-DIRECTIVE\n", encoding="utf-8")
    (root / ".levain").mkdir()
    return hooks


def _run(root: Path, script: str, payload: dict):
    return subprocess.run(
        [sys.executable, str(root / "activation" / "hooks" / script)], input=json.dumps(payload),
        capture_output=True, text=True, cwd=root, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(root)})


def _record(root: Path, agent: str, sid: str) -> dict:
    key = hashlib.sha1(f"{agent}:{sid}".encode()).hexdigest()[:24]
    return json.loads((root / ".levain" / "sessions" / f"{key}.json").read_text(encoding="utf-8"))


@pytest.fixture(params=sorted(_TREES))
def agent_root(request, tmp_path):
    _install(tmp_path, _TREES[request.param])
    return request.param, tmp_path


def test_session_start_writes_the_contract_shape(agent_root):
    agent, root = agent_root
    proc = _run(root, "session_start.py", {"session_id": "s1", "source": "startup", "cwd": "/work/proj"})
    assert proc.returncode == 0 and proc.stderr == ""
    rec = _record(root, agent, "s1")
    assert set(rec) == {"v", "agent", "session_id", "cwd", "first_seen", "last_seen", "last_event",
                        "context_delivered"}
    assert rec["v"] == 1 and rec["agent"] == agent and rec["session_id"] == "s1"
    assert rec["cwd"] == "/work/proj" and rec["last_event"] == "SessionStart"
    assert rec["context_delivered"] is True
    assert rec["first_seen"] == rec["last_seen"] and rec["first_seen"].endswith("Z")


def test_user_prompt_submit_updates_last_seen_and_keeps_the_rest(agent_root):
    agent, root = agent_root
    _run(root, "session_start.py", {"session_id": "s1", "source": "startup", "cwd": "/w"})
    path = next((root / ".levain" / "sessions").glob("*.json"))
    first = json.loads(path.read_text())
    first["first_seen"] = "2020-01-01T00:00:00Z"
    first["last_seen"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(first))
    proc = _run(root, "user_prompt_submit.py", {"session_id": "s1", "prompt": "hello", "cwd": "/w"})
    assert proc.returncode == 0 and proc.stderr == ""
    rec = _record(root, agent, "s1")
    assert rec["first_seen"] == "2020-01-01T00:00:00Z"
    assert rec["last_seen"] != "2020-01-01T00:00:00Z" and rec["last_event"] == "UserPromptSubmit"
    assert rec["context_delivered"] is True


def test_unwritable_sessions_dir_changes_nothing_the_hook_says(agent_root, tmp_path_factory):
    agent, root = agent_root
    other = tmp_path_factory.mktemp("baseline")
    _install(other, _TREES[agent])
    payload = {"session_id": "s1", "source": "startup", "cwd": "/w"}
    base = _run(other, "session_start.py", payload)
    # a FILE where the sessions directory should be: mkdir fails, so the write cannot happen
    (root / ".levain" / "sessions").write_text("not a directory")
    proc = _run(root, "session_start.py", payload)
    assert (proc.returncode, proc.stderr) == (base.returncode, base.stderr) == (0, "")

    def ctx(p):  # the orientation line carries a clock; everything else must match
        c = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
        return [s for s in c.split("\n\n") if not s.startswith("[session orientation]")]

    assert ctx(proc) == ctx(base)
    up = _run(root, "user_prompt_submit.py", {"session_id": "s1", "prompt": "x", "cwd": "/w"})
    assert up.returncode == 0 and up.stderr == ""


# ---- Q8: the transcript log (<stem>.log.jsonl) beside the registry file ----

_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def _key(agent: str, sid: str) -> str:
    return hashlib.sha1(f"{agent}:{sid}".encode()).hexdigest()[:24]


def _event(line: str):
    """The lane-B reader's validation (levain/cockpit/session_log.py `_event`), per the Contract."""
    d = json.loads(line)
    assert isinstance(d, dict) and set(d) == {"v", "at", "kind", "text", "cut"}
    assert type(d["v"]) is int and d["v"] == 1 and d["kind"] in ("prompt", "reply", "tool")
    assert isinstance(d["at"], str) and _STAMP.fullmatch(d["at"])
    assert isinstance(d["text"], str) and type(d["cut"]) is int and d["cut"] >= 0
    return d


def _events(root: Path, agent: str, sid: str) -> list[dict]:
    path = root / ".levain" / "sessions" / f"{_key(agent, sid)}.log.jsonl"
    return [_event(line) for line in path.read_text(encoding="utf-8").splitlines()]


_PAYLOADS = {   # shapes from Q8_PROBE/REPORT.md
    "claude_code": {
        "tool": {"session_id": "s1", "cwd": "/w", "tool_name": "Bash",
                 "tool_input": {"command": "ls -la\necho second line", "description": "list"},
                 "tool_response": {"stdout": "SECRET-OUT", "stderr": "", "interrupted": False}},
        "file": {"session_id": "s1", "cwd": "/w", "tool_name": "Edit",
                 "tool_input": {"file_path": "/w/a.py", "old_string": "x", "new_string": "y"},
                 "tool_response": {"filePath": "/w/a.py"}},
    },
    "codex": {
        "tool": {"session_id": "s1", "cwd": "/w", "tool_name": "Bash", "tool_input": {"command": "ls -la\necho second line"},
                 "tool_response": "SECRET-OUT", "turn_id": "t1"},
        "file": {"session_id": "s1", "cwd": "/w", "tool_name": "apply_patch", "tool_input": {"file_path": "/w/a.py"},
                 "tool_response": "ok", "turn_id": "t1"},
    },
}


def test_the_three_hooks_write_contract_lines(agent_root):
    agent, root = agent_root
    pl = _PAYLOADS[agent]
    stops = []
    for script, payload in (
        ("user_prompt_submit.py", {"session_id": "s1", "prompt": "do the thing", "cwd": "/w"}),
        ("post_tool_use.py", pl["tool"]),
        ("post_tool_use.py", pl["file"]),
        ("stop.py", {"session_id": "s1", "cwd": "/w", "last_assistant_message": "all done",
                     "stop_hook_active": False}),
    ):
        proc = _run(root, script, payload)
        assert proc.returncode == 0 and proc.stderr == ""
        stops.append(proc.stdout)
    assert stops[1] == stops[2] == stops[3] == ""     # PostToolUse and Stop print nothing
    ev = _events(root, agent, "s1")
    assert [(e["kind"], e["text"], e["cut"]) for e in ev] == [
        ("prompt", "do the thing", 0),
        ("tool", "Bash: ls -la", 0),
        ("tool", f"{pl['file']['tool_name']}: /w/a.py", 0),
        ("reply", "all done", 0),
    ]
    raw = (root / ".levain" / "sessions" / f"{_key(agent, 's1')}.log.jsonl").read_text()
    assert "SECRET-OUT" not in raw                    # tool_response is never read
    path = root / ".levain" / "sessions" / f"{_key(agent, 's1')}.log.jsonl"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    # the registry record is untouched by Stop / PostToolUse (last_event stays a registry value)
    assert _record(root, agent, "s1")["last_event"] == "UserPromptSubmit"


def test_a_tool_without_a_summary_is_just_its_name(agent_root):
    agent, root = agent_root
    _run(root, "post_tool_use.py", {"session_id": "s1", "tool_name": "Task", "tool_input": {"x": 1}})
    _run(root, "post_tool_use.py", {"session_id": "s1", "tool_name": "Read", "tool_input": "weird"})
    assert [e["text"] for e in _events(root, agent, "s1")] == ["Task", "Read"]


def test_long_text_is_cut_and_counted(agent_root):
    agent, root = agent_root
    _run(root, "user_prompt_submit.py", {"session_id": "s1", "prompt": "p" * 5000, "cwd": "/w"})
    _run(root, "stop.py", {"session_id": "s1", "last_assistant_message": "r" * 4001})
    _run(root, "stop.py", {"session_id": "s1", "last_assistant_message": ""})   # empty: no line
    p, r = _events(root, agent, "s1")
    assert (len(p["text"]), p["cut"]) == (4000, 1000) and (len(r["text"]), r["cut"]) == (4000, 1)


def test_the_log_is_rewritten_to_its_newest_half_by_replace(agent_root):
    agent, root = agent_root
    sdir = root / ".levain" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{_key(agent, 's1')}.log.jsonl"
    old = [json.dumps({"v": 1, "at": "2026-01-01T00:00:00Z", "kind": "tool", "text": f"old-{i}-" + "x" * 900,
                       "cut": 0}) + "\n" for i in range(280)]
    path.write_text("".join(old))
    assert path.stat().st_size > 256 * 1024
    inode = path.stat().st_ino
    proc = _run(root, "stop.py", {"session_id": "s1", "last_assistant_message": "newest"})
    assert proc.returncode == 0 and proc.stdout == ""
    assert path.stat().st_ino != inode                  # replaced, never truncated in place
    assert path.stat().st_size < 160 * 1024
    ev = _events(root, agent, "s1")                     # every line whole and valid
    assert ev[-1]["text"] == "newest" and 100 < len(ev) < 180
    texts = [e["text"] for e in ev[:-1]]
    assert texts[-1].startswith("old-279-") and texts == sorted(texts, key=lambda t: int(t.split("-")[1]))
    assert not list(sdir.glob("*.tmp"))
    assert oct(path.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("script", ["stop.py", "post_tool_use.py"])
def test_stop_and_post_tool_use_are_silent_whatever_goes_wrong(agent_root, script):
    agent, root = agent_root
    good = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"},
            "last_assistant_message": "hi"}
    (root / ".levain" / "sessions").write_text("a file where the directory should be")
    proc = _run(root, script, good)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    (root / ".levain" / "sessions").unlink()
    sdir = root / ".levain" / "sessions"
    sdir.mkdir()
    sdir.chmod(0o500)                                   # read-only directory
    try:
        proc = _run(root, script, good)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
        for stdin in ("not json at all", "", "[1, 2]", '{"session_id": 5, "tool_name": 7}'):
            proc = subprocess.run(
                [sys.executable, str(root / "activation" / "hooks" / script)], input=stdin,
                capture_output=True, text=True, cwd=root, timeout=60,
                env={"PATH": "/usr/bin:/bin", "HOME": str(root)})
            assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    finally:
        sdir.chmod(0o700)
    assert not list(sdir.iterdir())


def test_a_symlinked_log_is_not_followed(agent_root, tmp_path_factory):
    agent, root = agent_root
    target = tmp_path_factory.mktemp("elsewhere") / "victim.txt"
    target.write_text("keep")
    sdir = root / ".levain" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f"{_key(agent, 's1')}.log.jsonl").symlink_to(target)
    proc = _run(root, "stop.py", {"session_id": "s1", "last_assistant_message": "x"})
    assert (proc.returncode, proc.stdout) == (0, "") and target.read_text() == "keep"


def test_the_prune_keeps_200_stems_and_removes_a_stems_files_together(agent_root):
    agent, root = agent_root
    sdir = root / ".levain" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    old = time.time() - 10_000
    for i in range(205):                                 # 205 stems, each a pair, aged oldest-first
        for suffix in (".json", ".log.jsonl"):
            f = sdir / f"{i:024x}{suffix}"
            f.write_text("{}")
            os.utime(f, (old + i, old + i))
    # an old stem whose LOG is fresh is a live session: it must survive on the newer mtime of the pair
    os.utime(sdir / f"{0:024x}.log.jsonl", None)
    stale = sdir / f"{5:024x}.log.jsonl.123.tmp"
    stale.write_text("x")
    os.utime(stale, (old, old))
    proc = _run(root, "user_prompt_submit.py", {"session_id": "new", "prompt": "hi", "cwd": "/w"})
    assert proc.returncode == 0
    names = sorted(p.name for p in sdir.iterdir())
    stems = {n.split(".")[0] for n in names if n != ".lock"}
    assert len(stems) == 200 and _key(agent, "new") in stems
    assert f"{0:024x}" in stems                          # kept by its fresh log
    for stem in stems:                                   # never half a pair among the old ones
        if stem != _key(agent, "new"):
            assert {f"{stem}.json", f"{stem}.log.jsonl"} <= set(names)
    assert f"{1:024x}" not in stems and stale.name not in names
    assert f"{1:024x}.json" not in names and f"{1:024x}.log.jsonl" not in names


# ---- Q8 L3 r1 fixes (F1-F5): one sessions-directory lock, exact-name prune, whole writes ----

_GATED = ("import os, runpy, sys, time\n"
          "go, script = sys.argv[1], sys.argv[2]\n"
          "while not os.path.exists(go):\n"
          "    time.sleep(0.0005)\n"
          "sys.argv = [script]\n"
          "runpy.run_path(script, run_name='__main__')\n")


def _line(i: int, size: int = 900) -> str:
    return json.dumps({"v": 1, "at": "2026-01-01T00:00:00Z", "kind": "tool",
                       "text": f"fill-{i}-" + "x" * size, "cut": 0}) + "\n"


def test_concurrent_appends_across_the_rotation_lose_no_event(agent_root, tmp_path_factory):
    agent, root = agent_root
    sdir = root / ".levain" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{_key(agent, 's1')}.log.jsonl"
    script = root / "activation" / "hooks" / "post_tool_use.py"
    gate = tmp_path_factory.mktemp("gate") / "go"
    n_proc, rounds, sent = 12, 6, []
    for rnd in range(rounds):
        # top the log up to just under the rotation threshold, so the first append crosses it
        with open(path, "ab") as fh:
            i = 0
            while fh.tell() < 256 * 1024 - 300:
                fh.write(_line(i).encode()); i += 1
        gate.unlink(missing_ok=True)
        procs = []
        for k in range(n_proc):
            tag = f"r{rnd}p{k}"
            sent.append(tag)
            pr = subprocess.Popen(
                [sys.executable, "-c", _GATED, str(gate), str(script)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=root,
                env={"PATH": "/usr/bin:/bin", "HOME": str(root)})
            pr.stdin.write(json.dumps({"session_id": "s1", "cwd": "/w", "tool_name": "Task",
                                       "tool_input": {"file_path": tag}}))
            pr.stdin.close()
            procs.append(pr)
        time.sleep(0.4)                       # every interpreter is up and spinning on the gate
        gate.write_text("go")
        for pr in procs:
            assert pr.wait(timeout=60) == 0 and pr.stdout.read() == "" and pr.stderr.read() == ""
        # the newest events must all be present: rotation keeps the newest half, so only filler goes
        texts = [e["text"] for e in _events(root, agent, "s1")]
        missing = [t for t in sent[-n_proc:] if f"Task: {t}" not in texts]
        assert not missing, f"round {rnd} lost {missing}"


def test_a_held_sessions_lock_drops_the_event_and_the_hook_still_exits_clean(agent_root):
    fcntl = pytest.importorskip("fcntl")
    agent, root = agent_root
    sdir = root / ".levain" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    fd = os.open(sdir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        for script, payload in (("post_tool_use.py", {"session_id": "s1", "tool_name": "Bash",
                                                       "tool_input": {"command": "ls"}}),
                                ("user_prompt_submit.py", {"session_id": "s1", "prompt": "x", "cwd": "/w"})):
            t0 = time.monotonic()
            proc = _run(root, script, payload)
            assert proc.returncode == 0 and proc.stderr == ""
            assert time.monotonic() - t0 < 1.5
            if script == "post_tool_use.py":
                assert proc.stdout == ""
        assert not list(sdir.glob("*.log.jsonl")) and not list(sdir.glob("[0-9a-f]*.json"))
    finally:
        os.close(fd)
    assert _run(root, "post_tool_use.py", {"session_id": "s1", "tool_name": "Task"}).returncode == 0
    assert [e["text"] for e in _events(root, agent, "s1")] == ["Task"]       # the lock is not sticky


def test_the_prune_never_touches_a_foreign_file(agent_root):
    agent, root = agent_root
    sdir = root / ".levain" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    old = time.time() - 10_000
    for i in range(205):
        f = sdir / f"{i:024x}.json"
        f.write_text("{}")
        os.utime(f, (old + i, old + i))
    foreign = {"index.json": "{}", "notes.tmp": "n", "README.log.jsonl": "r", "abc.json": "{}"}
    for name, body in foreign.items():
        f = sdir / name
        f.write_text(body)
        os.utime(f, (old, old))
    assert _run(root, "user_prompt_submit.py", {"session_id": "new", "prompt": "hi", "cwd": "/w"}).returncode == 0
    names = {p.name for p in sdir.iterdir()}
    assert set(foreign) <= names and ".lock" in names
    assert f"{0:024x}.json" not in names and _key(agent, "new") + ".json" in names


def test_a_log_cut_short_gets_a_fresh_line(agent_root):
    agent, root = agent_root
    sdir = root / ".levain" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{_key(agent, 's1')}.log.jsonl"
    first = _line(1, 5)
    path.write_text(first[:-1])                              # the last byte is not a newline
    assert _run(root, "post_tool_use.py", {"session_id": "s1", "tool_name": "Task"}).returncode == 0
    lines = path.read_text().split("\n")
    assert lines[-1] == "" and len(lines) == 3
    assert [json.loads(x)["text"] for x in lines[:2]] == [json.loads(first)["text"], "Task"]


@pytest.mark.parametrize("agent", sorted(_TREES))
def test_the_rewrite_does_not_follow_a_symlink(agent, tmp_path):
    spec = importlib.util.spec_from_file_location(f"_lh_{agent}", _TREES[agent] / "_levain_hook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    target = tmp_path / "victim.log"
    target.write_bytes(b"".join(_line(i).encode() for i in range(300)))
    before = target.read_bytes()
    link = tmp_path / "x.log.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        mod._log_rewrite_newest_half(link)
    assert link.is_symlink() and target.read_bytes() == before and not list(tmp_path.glob("*.tmp"))


def test_apply_patch_logs_the_patch_path_not_the_patch(agent_root):
    agent, root = agent_root
    patch = "*** Begin Patch\n*** Update File: /w/src/a.py\n@@\n-x\n+y\n*** End Patch"
    for payload in (
        {"session_id": "s1", "tool_name": "apply_patch", "tool_input": {"command": patch}},
        {"session_id": "s1", "tool_name": "apply_patch", "tool_input": {"command": "garbage"}},
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "echo hi"}},
    ):
        proc = _run(root, "post_tool_use.py", payload)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert [e["text"] for e in _events(root, agent, "s1")] == [
        "apply_patch: /w/src/a.py", "apply_patch", "Bash: echo hi"]
