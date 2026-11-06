"""The room's own read-guard hook (``levain.coordinator.room_readguard``, levain 44, key 1105-49): the sentence the room's
model gets for a read outside its folder. The recorded real run (``fixtures/claude_stream/room_hook_denied_read.jsonl``, a
claude 2.1.319 turn with the real hook through ``--settings`` under ``--setting-sources user``) fixed the wrapper text and
the frames; a LIVE run of the same turn is gated by ``LEVAIN_LIVE_CLAUDE=1`` (one short subscription turn, never run by
default). STRUCTURAL: what the model then says about the sentence is a live room turn's to show."""
from __future__ import annotations

import ast
import io
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from levain.coordinator import room_readguard
from levain.coordinator.chat_tripwires import TripwireWindow
from levain.coordinator.claude_session import ROOM_READ_REFUSAL, room_argv, room_hook_command, room_settings_json
from levain.coordinator.room_launch import room_tripwire_context
from levain.coordinator.room_readguard import EXEMPT_ENV, decide, main
from tests.test_coordinator_claude_session import make

FIXTURE = Path(__file__).with_name("fixtures") / "claude_stream" / "room_hook_denied_read.jsonl"
ROOM_VIEW = Path(room_readguard.__file__).resolve().parents[1] / "templates" / "web" / "room_view.js"
LEAKS = ("/add-dir", "blockReadsOutsideWorkingDirectories", "permissions.", "remove that setting")


def _event(tool, path, cwd, key=None):
    key = key or ("file_path" if tool == "Read" else "path")
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {key: path}, "cwd": str(cwd)}


@pytest.fixture
def room(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "note.txt").write_text("hi\n")
    (cwd / "sub").mkdir()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    decoy = tmp_path / "scope" / "levain-room-scope-canary.txt"
    decoy.parent.mkdir()
    decoy.write_text("SCOPE DECOY\n")
    return {"cwd": cwd, "cfg": cfg, "decoy": decoy, "tmp": tmp_path}


def _decide(room, tool, path, **kw):
    return decide(_event(tool, path, room["cwd"], **kw), exempt=[str(room["decoy"])], config_dir=str(room["cfg"]))


# ---- 1. decide(): the pure decision ----------------------------------------------------------------------------------
@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_a_path_outside_the_folder_gets_exactly_the_fixed_sentence_and_nothing_the_vendor_leaks(room, tool):
    for path in ("/etc/hosts", "../x", "../../etc/passwd", "~/.ssh/id_rsa", "~", str(room["tmp"] / "elsewhere.txt")):
        got = _decide(room, tool, path)
        assert got == ROOM_READ_REFUSAL, (tool, path, got)
    for needle in (*LEAKS, str(room["cwd"]), "/etc"):
        assert needle not in ROOM_READ_REFUSAL, needle


def test_a_symlink_out_of_the_folder_is_outside_and_a_symlink_within_it_is_not(room):
    out = room["tmp"] / "outside.txt"
    out.write_text("secret\n")
    (room["cwd"] / "to_out").symlink_to(out)
    (room["cwd"] / "to_note").symlink_to(room["cwd"] / "note.txt")
    assert _decide(room, "Read", str(room["cwd"] / "to_out")) == ROOM_READ_REFUSAL
    assert _decide(room, "Read", "to_out") == ROOM_READ_REFUSAL                     # relative, resolved against the event cwd
    assert _decide(room, "Read", str(room["cwd"] / "to_note")) is None
    # a folder reached through a symlinked parent is compared on realpath on both sides
    link = room["tmp"] / "cwd_link"
    link.symlink_to(room["cwd"])
    assert decide(_event("Read", str(link / "note.txt"), room["cwd"]), exempt=[], config_dir=None) is None


@pytest.mark.parametrize("tool,path", [("Read", None), ("Read", "note.txt"), ("Read", "sub/../note.txt"), ("Read", "sub"),
                                      ("Grep", "sub"), ("Glob", "sub"), ("Read", "nope/missing.txt")])
def test_a_path_inside_the_folder_is_left_to_the_vendor(room, tool, path):
    got = _decide(room, tool, str(room["cwd"] / path) if path else str(room["cwd"] / "note.txt"))
    assert got is None
    if path:
        assert _decide(room, tool, path) is None                                    # relative to the event's cwd


def test_the_config_dir_and_the_open_checks_decoy_are_left_to_the_vendor_but_their_neighbours_are_not(room):
    assert _decide(room, "Read", str(room["cfg"] / "tool-results" / "x.txt")) is None
    assert _decide(room, "Read", str(room["decoy"])) is None
    assert _decide(room, "Read", str(room["decoy"].parent / "other.txt")) == ROOM_READ_REFUSAL
    assert _decide(room, "Read", str(room["cfg"]) + "-sibling/x") == ROOM_READ_REFUSAL
    # the decoy exemption is by realpath: a link to it is the same file, a link to anything else is not
    link = room["cwd"] / "d"
    link.symlink_to(room["decoy"])
    assert _decide(room, "Read", str(link)) is None


def _volume_folds_case(path: Path) -> bool:
    """Measured, never assumed: does this volume find ``path`` under its last component in other casing?"""
    other = path.parent / path.name.swapcase()
    return other.exists() and os.path.samefile(path, other)


def test_the_folder_in_other_casing_is_inside_on_a_case_insensitive_volume(room):
    """L3 r1 codex MED (complement LOW 4): ``realpath`` keeps the casing it is given, so a string compare judged the
    room's own folder, written in other casing, outside (a false refusal, never a hole). Ancestry is by filesystem
    identity; the config-dir and decoy exemptions likewise. A neighbour in other casing is still outside."""
    if not _volume_folds_case(room["cwd"]):
        pytest.skip("this volume is case-sensitive: the other casing names another folder")
    cased = room["cwd"].parent / room["cwd"].name.swapcase()                        # .../CWD for .../cwd
    assert _decide(room, "Read", str(cased / "note.txt")) is None
    assert _decide(room, "Read", str(cased)) is None
    assert _decide(room, "Read", str(cased / "nope" / "missing.txt")) is None        # a tail that does not exist yet
    assert _decide(room, "Grep", str(cased / "SUB")) is None
    assert decide(_event("Read", str(room["cwd"] / "note.txt"), cased), exempt=[], config_dir=None) is None
    assert _decide(room, "Read", str(room["tmp"] / room["cfg"].name.swapcase() / "tool-results" / "x.txt")) is None
    assert _decide(room, "Read", str(room["decoy"].parent / room["decoy"].name.upper())) is None
    # a sibling whose name merely starts with the folder's, in either casing, is still outside
    (room["tmp"] / "cwd-sibling").mkdir()
    assert _decide(room, "Read", str(room["tmp"] / "CWD-sibling" / "x")) == ROOM_READ_REFUSAL
    assert _decide(room, "Read", str(room["tmp"] / "CWDX" / "x")) == ROOM_READ_REFUSAL
    assert _decide(room, "Read", str(room["decoy"].parent / "OTHER.txt")) == ROOM_READ_REFUSAL


@pytest.mark.parametrize("event", [
    None, [], "x", {}, {"tool_name": "Read"}, {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/tmp"},
    {"tool_name": "Read", "tool_input": {}, "cwd": "/tmp"}, {"tool_name": "Read", "tool_input": {"file_path": ""}, "cwd": "/tmp"},
    {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}, "cwd": "relative"},
    {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}},
    {"tool_name": "Read", "tool_input": {"file_path": 7}, "cwd": "/tmp"},
    {"tool_name": "Read", "tool_input": {"file_path": "/etc/ho\x00sts"}, "cwd": "/tmp"},
    {"tool_name": "Grep", "tool_input": {"pattern": "x"}, "cwd": "/tmp"},                    # Grep with no path: the vendor decides
    {"tool_name": "Glob", "tool_input": {"pattern": "/etc/*"}, "cwd": "/tmp"},               # Glob with no path likewise
    {"tool_name": "Read", "tool_input": "not a mapping", "cwd": "/tmp"},
])
def test_an_unclear_event_is_never_refused(event):
    assert decide(event, exempt=[], config_dir=None) is None


# ---- 2. main(): every failure of the hook itself falls through to the vendor ----------------------------------------
def _run_main(event_text, environ):
    out = io.StringIO()
    code = main(io.StringIO(event_text), out, environ)
    return code, out.getvalue()


def test_the_decision_is_a_pretooluse_deny_with_the_sentence_and_the_hook_never_allows(room):
    env = {EXEMPT_ENV: json.dumps([str(room["decoy"])]), "CLAUDE_CONFIG_DIR": str(room["cfg"])}
    code, text = _run_main(json.dumps(_event("Read", "/etc/hosts", room["cwd"])), env)
    assert code == 0
    assert json.loads(text) == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                       "permissionDecisionReason": ROOM_READ_REFUSAL}}
    assert "allow" not in text and "ask" not in text
    # a read the hook leaves alone prints NOTHING (no decision at all, so the vendor decides)
    for ev in (_event("Read", str(room["cwd"] / "note.txt"), room["cwd"]), _event("Read", str(room["decoy"]), room["cwd"]),
               _event("Read", str(room["cfg"] / "x"), room["cwd"])):
        assert _run_main(json.dumps(ev), env) == (0, "")


@pytest.mark.parametrize("env", [
    {},                                                                  # no exemption list: the decoy would be masked, so inert
    {EXEMPT_ENV: ""}, {EXEMPT_ENV: "not json"}, {EXEMPT_ENV: "{}"}, {EXEMPT_ENV: json.dumps(["relative/p"])},
    {EXEMPT_ENV: json.dumps([3])},
    {EXEMPT_ENV: "[]"}])     # an EMPTY list exempts no decoy, so the hook would answer the canary: inert too (L3 r1 deepseek)
def test_a_missing_or_broken_exemption_list_makes_the_hook_inert_never_a_refusal(room, env):
    assert _run_main(json.dumps(_event("Read", "/etc/hosts", room["cwd"])), env) == (0, "")


# an exemption list that makes the hook live (an absolute path nothing reads), so "prints nothing" below is the hook's own
_LIVE_EXEMPT = {EXEMPT_ENV: json.dumps(["/nonexistent-levain-room-exempt"])}


@pytest.mark.parametrize("text", ["", "not json", "[]", "null", '{"tool_name": "Read"}', "\x00"])
def test_a_garbled_event_exits_zero_and_prints_nothing(text):
    assert _run_main(text, _LIVE_EXEMPT) == (0, "")


def test_a_failure_inside_the_hook_exits_zero_and_prints_nothing(room, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("x")
    monkeypatch.setattr(room_readguard, "decide", boom)
    assert _run_main(json.dumps(_event("Read", "/etc/hosts", room["cwd"])), _LIVE_EXEMPT) == (0, "")


def test_the_config_dir_comes_from_the_mapping_main_is_given_never_the_processes_environment(room, monkeypatch):
    """L3 r1 deepseek LOW: ``_config_dir`` fell back to ``os.path.expanduser``, which reads the process's ``os.environ``
    and not the mapping ``main`` was given. With no HOME in the mapping the fallback is the OS user's home (the passwd
    entry, what the vendor's own home lookup falls back to), never the process environment."""
    proc_home = Path("/nonexistent-levain-proc-home")       # not under the OS user's home (the basetemp may be)
    monkeypatch.setenv("HOME", str(proc_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    user_home = pwd.getpwuid(os.getuid()).pw_dir
    assert room_readguard._config_dir({"HOME": str(room["tmp"] / "given")}) == str(room["tmp"] / "given" / ".claude")
    assert room_readguard._config_dir({}) == os.path.join(user_home, ".claude")
    env = dict(_LIVE_EXEMPT)                                      # no HOME, no CLAUDE_CONFIG_DIR in the mapping
    assert _run_main(json.dumps(_event("Read", str(proc_home / ".claude" / "x"), room["cwd"])), env)[1] != ""


def test_the_script_runs_isolated_as_the_child_runs_it_and_imports_nothing_of_levain(room):
    """The launcher copies the module file and the child runs ``python -I <copy>``: stdlib only, no levain."""
    src = Path(room_readguard.__file__)
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(ast.parse(src.read_text())) if isinstance(n, ast.Import)}
    mods |= {n.module.split(".")[0] for n in ast.walk(ast.parse(src.read_text())) if isinstance(n, ast.ImportFrom) and n.module}
    assert "levain" not in mods and mods <= set(sys.stdlib_module_names), mods
    copy = room["tmp"] / "copy" / "room_readguard.py"
    copy.parent.mkdir()
    shutil.copy(src, copy)
    env = {"PATH": os.environ.get("PATH", ""), EXEMPT_ENV: json.dumps([str(room["decoy"])]), "HOME": str(room["tmp"])}
    ran = subprocess.run([sys.executable, "-I", str(copy)], input=json.dumps(_event("Read", "/etc/hosts", room["cwd"])),
                         capture_output=True, text=True, env=env, timeout=20)
    assert ran.returncode == 0 and ran.stderr == ""
    assert json.loads(ran.stdout)["hookSpecificOutput"]["permissionDecisionReason"] == ROOM_READ_REFUSAL
    ran = subprocess.run([sys.executable, "-I", str(copy)], input=json.dumps(_event("Read", str(room["decoy"]), room["cwd"])),
                         capture_output=True, text=True, env=env, timeout=20)
    assert (ran.returncode, ran.stdout, ran.stderr) == (0, "", "")


# ---- 3. the settings document and the JS regex ----------------------------------------------------------------------
def test_the_settings_keep_the_vendors_block_and_add_the_hook_and_the_hook_has_no_allow(tmp_path):
    cmd = room_hook_command(sys.executable, tmp_path / "room_readguard.py")
    doc = json.loads(room_settings_json(str(tmp_path / "room"), [], cmd))
    assert doc["permissions"]["blockReadsOutsideWorkingDirectories"] is True            # the backstop stays on
    [entry] = doc["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Read|Grep|Glob" and entry["hooks"] == [{"type": "command", "command": cmd, "timeout": 10}]
    assert list(doc["hooks"]) == ["PreToolUse"]
    assert "hooks" not in json.loads(room_settings_json(str(tmp_path / "room"), []))   # no command, no hook entry
    with pytest.raises(ValueError):
        room_hook_command("python", tmp_path / "x.py")
    with pytest.raises(ValueError):
        room_settings_json(str(tmp_path / "room"), [], "  ")


def _js_refusal_regexes():
    """The REFUSED entries of room_view.js, as python regexes (the sources use no JS-only syntax)."""
    src = ROOM_VIEW.read_text()
    block = src[src.index("var REFUSED = ["):src.index("];", src.index("var REFUSED = ["))]
    return [re.compile(m) for m in re.findall(r"re: /(.+)/ \}", block)]


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_the_pages_refusal_regex_matches_the_constant_the_hook_prints(tool):
    """Two pieces of code computing one thing (the hook's sentence and the page's regex for it): one test ties them."""
    res = _js_refusal_regexes()
    assert len(res) == 4
    body = f"PreToolUse:{tool} hook error: {ROOM_READ_REFUSAL}"
    assert [bool(r.search(body)) for r in res] == [False, False, False, True]
    assert not res[3].search(f"PreToolUse:Bash hook error: {ROOM_READ_REFUSAL}")
    assert not res[3].search(f"1\tPreToolUse:Read hook error: {ROOM_READ_REFUSAL}")     # a file's text opens with a line number
    assert not res[3].search("PreToolUse:Read hook error: some other hook said no")
    assert not res[3].search(ROOM_READ_REFUSAL)                                       # the wrapper is part of what is recorded


def _js_escape(text: str) -> str:
    return re.sub(r"([\\^$.*+?()\[\]{}|/])", r"\\\1", text)


def test_the_pages_refusal_regex_is_the_whole_constant_in_the_recorded_wrapper_and_nothing_after_it():
    """L3 r1 codex LOW (complement LOW 6): the regex held only the sentence's first clause and no end anchor, so another
    hook's reason that opened the same way was drawn as the room's refusal. Its source is now built from
    ``ROOM_READ_REFUSAL`` (one test ties the two) inside the recorded wrapper only, anchored at the end."""
    src = ROOM_VIEW.read_text()
    block = src[src.index("var REFUSED = ["):src.index("];", src.index("var REFUSED = ["))]
    want = r"^PreToolUse:(?:Read|Grep|Glob) hook error: " + _js_escape(ROOM_READ_REFUSAL) + "$"
    assert f"re: /{want}/ }}" in block
    # the recorded wrapper is exactly this (the fixture's tool_result content)
    blocks = [b for f in _frames() if isinstance(f.get("message"), dict) and isinstance(f["message"].get("content"), list)
              for b in f["message"]["content"]]
    recorded = {b["content"] for b in blocks
                if isinstance(b, dict) and b.get("type") == "tool_result" and "hook error" in str(b.get("content"))}
    assert recorded == {f"PreToolUse:{t} hook error: {ROOM_READ_REFUSAL}" for t in ("Read", "Grep")}
    r = _js_refusal_regexes()[3]
    first = ROOM_READ_REFUSAL.split(". ")[0] + "."
    assert not r.search(f"PreToolUse:Read hook error: {first} Another hook's own, important detail.")
    assert not r.search(f"PreToolUse:Read hook error: {ROOM_READ_REFUSAL} And more.")


# ---- 4. the recorded real run ---------------------------------------------------------------------------------------
def _frames():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def _results():
    out = {}
    for f in _frames():
        if f.get("type") == "user":
            for b in f["message"]["content"]:
                if b.get("type") == "tool_result":
                    out[b["tool_use_id"]] = b
    return out


def _calls():
    return [(b["id"], b["name"], b["input"]) for f in _frames() if f.get("type") == "assistant"
            for b in f["message"]["content"] if b.get("type") == "tool_use"]


def test_the_recorded_refused_reads_carry_the_fixed_sentence_in_the_vendors_hook_wrapper_and_none_of_its_own_words():
    res, calls = _results(), _calls()
    by_path = {i["file_path"] if "file_path" in i else i["path"]: (n, i, res[t]) for t, n, i in calls}
    for path in ("/etc/hosts", "/etc"):
        name, _inp, block = by_path[path]
        assert block["is_error"] is True
        assert block["content"] == f"PreToolUse:{name} hook error: {ROOM_READ_REFUSAL}", block["content"]
        for needle in LEAKS:
            assert needle not in block["content"], needle
    inside = by_path["/tmp/room-repro/real2/cwd/hello.txt"][2]
    assert inside.get("is_error") is None and inside["content"].startswith("1\thello")      # an inside read is untouched


def test_the_scope_decoy_is_still_answered_by_the_vendors_own_block_in_the_recorded_run():
    """Canary: the open check's decoy must be answered by the vendor sentence, so it proves the block holds. The
    launcher gives the hook the decoy in its environment; the recorded run did the same."""
    res, calls = _results(), _calls()
    [(tid, _n, _i)] = [c for c in calls if c[2].get("file_path", "").endswith("levain-room-scope-canary.txt")]
    text = res[tid]["content"]
    assert text.startswith("/tmp/room-repro/real2/scope/levain-room-scope-canary.txt is outside /tmp/room-repro/real2/cwd; "
                           "the permissions.blockReadsOutsideWorkingDirectories setting blocks reads outside")
    assert "PreToolUse" not in text and res[tid]["is_error"] is True


def test_a_hook_deny_is_a_permission_denials_entry_so_a_hook_that_answered_the_decoy_would_mask_the_canary():
    """The finding behind the exemption: the turn's ``result`` lists the hook-denied reads like a vendor-denied one."""
    [result] = [f for f in _frames() if f.get("type") == "result"]
    denied = {d["tool_use_id"]: d["tool_input"] for d in result["permission_denials"]}
    assert {"/etc/hosts", "/etc"} <= {i.get("file_path") or i.get("path") for i in denied.values()}
    sysf = [f for f in _frames() if f.get("type") == "system"]
    assert [f["subtype"] for f in sysf] == ["permission_denied"]                    # the vendor's frame is for the decoy ONLY
    assert sysf[0]["tool_use_id"] not in {t for t, n, i in _calls() if i.get("file_path") == "/etc/hosts"}


def test_the_recorded_run_draws_no_error_row_and_latches_no_tripwire():
    """Check chat_tripwires / _on_hook: the hook's deny adds no ``hook_response`` event (the stream carries none), the
    refused and the allowed reads are rows like any other, and a ``reject`` from source ``hook`` is never refused."""
    s, t = make()
    t.feed(*_frames())
    evs = list(s.read(None).events)
    assert [e for e in evs if e.kind == "error"] == [] and not any(e.vendor_id.startswith("unparseable:") for e in evs)
    results = [e for e in evs if e.kind == "tool_result"]
    assert len(results) == 4 and [e.body.split(" ")[0] for e in results].count("PreToolUse:Read") == 1
    assert sorted((e.decision_source["decision"], e.decision_source["source"]) for e in results) == [
        ("accept", "user_permanent"), ("reject", "config"), ("reject", "hook"), ("reject", "hook")]
    w = TripwireWindow(room_tripwire_context("/tmp/room-repro/real2/cwd"))
    trips = []
    for e in evs:
        trip, _reply = w.observe(e)
        trips.append(trip)
    assert trips == [None] * len(evs), [t for t in trips if t]


# ---- 5. a live run (gated) -------------------------------------------------------------------------------------------
LIVE = pytest.mark.skipif(os.environ.get("LEVAIN_LIVE_CLAUDE") != "1", reason="one real claude turn; LEVAIN_LIVE_CLAUDE=1")


def _live_turn(tmp_path, *, with_hook=True):
    """One real room-shaped turn (model haiku, the room's own argv and settings): an outside Read and the decoy."""
    # the decoy lives in the system temp dir as the launcher's does: a decoy under the vendor's config dir would be left to
    # the vendor by the hook's config-dir exemption and could not show whether the decoy exemption works
    cwd, scope = tmp_path / "cwd", Path(os.path.realpath(tempfile.mkdtemp(prefix="levain-room-scope-")))
    cwd.mkdir()
    decoy = scope / "levain-room-scope-canary.txt"
    decoy.write_text("SCOPE DECOY\n")
    guard = tmp_path / "room_readguard.py"
    shutil.copy(room_readguard.__file__, guard)
    cmd = room_hook_command(sys.executable, guard) if with_hook else None
    (tmp_path / "settings.json").write_text(room_settings_json(str(cwd), [], cmd))
    (tmp_path / "mcp.json").write_text('{"mcpServers":{}}')
    argv = room_argv(mcp_config=tmp_path / "mcp.json", settings=tmp_path / "settings.json", model="haiku",
                     session_id=str(uuid.uuid4()))
    # the suite runs under a scratch HOME (conftest); a real claude needs the operator's own account, so this gated test
    # alone hands the child the real home of the user running it
    env = {**os.environ, "HOME": pwd.getpwuid(os.getuid()).pw_dir, "LEVAIN_ROOM": "1", EXEMPT_ENV: json.dumps([str(decoy)])}
    prompt = f"Use the Read tool on /etc/hosts, then on {decoy}. Then say in one sentence what happened."
    line = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n"
    try:
        ran = subprocess.run(argv, input=line, capture_output=True, text=True, cwd=cwd, env=env, timeout=170)
    finally:
        shutil.rmtree(scope, ignore_errors=True)
    assert ran.returncode == 0 and ran.stdout, ran.stderr[-400:]
    texts = {}
    for raw in ran.stdout.splitlines():
        f = json.loads(raw)
        if f.get("type") == "user":
            for b in f["message"]["content"]:
                if b.get("type") == "tool_result":
                    texts[b["tool_use_id"]] = b["content"]
    calls = {b["input"].get("file_path"): b["id"] for raw in ran.stdout.splitlines() for f in [json.loads(raw)]
             if f.get("type") == "assistant" for b in f["message"]["content"] if b.get("type") == "tool_use"}
    return texts[calls["/etc/hosts"]], texts[calls[str(decoy)]]


@LIVE
def test_live_the_real_claude_gives_the_model_the_fixed_sentence_and_the_decoy_stays_the_vendors(tmp_path):
    outside, decoy = _live_turn(tmp_path)
    assert outside == f"PreToolUse:Read hook error: {ROOM_READ_REFUSAL}", outside
    assert "blockReadsOutsideWorkingDirectories" in decoy and "PreToolUse" not in decoy, decoy


@LIVE
def test_live_without_the_hook_entry_the_vendors_leaking_sentence_returns(tmp_path):
    outside, _decoy = _live_turn(tmp_path, with_hook=False)
    assert "blockReadsOutsideWorkingDirectories" in outside and ROOM_READ_REFUSAL not in outside, outside
