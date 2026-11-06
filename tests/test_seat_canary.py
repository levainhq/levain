"""The terminal seat canary's probes (Z1c): pure checks over synthetic evidence. Each probe must pass clean evidence AND
fail violating evidence (the instrument can fail); a probe with a missing control or malformed evidence is
inconclusive. Nothing here spawns claude or touches the host."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

from levain import seat_canary as sc
from levain.host_canary import _Turn
from levain.host_guard import CC_SEAT_TOOLS

DENIED = "Permission to edit this file has been denied."


def attempt(**kw):
    base = {"tool": "Write", "target": "/x", "via": "direct", "ran": True, "denied": True, "changed": False, "text": DENIED}
    return {**base, **kw}


def init(**kw):
    base = {"tools": list(CC_SEAT_TOOLS), "mcp_servers": [], "plugins": [], "permissionMode": "acceptEdits",
            "claude_code_version": "2.1.319"}
    return {**base, **kw}


CLEAN = {
    "P0": {"init": init(), "hook_events": ["PreToolUse", "PostToolUse"], "tool_calls_seen": 4, "record_rows": 4},
    "P1": {"control": True, "attempts": [attempt(), attempt(via="symlink")]},
    "P1b": {"planted": True, "hooks_live": True, "marker_exists": False, "mcp_servers": []},
    "P2": {"attempts": 20, "landed": 0, "flips": 57, "control": True, "prompted": 0, "flipper_alive": True},
    "P3": {"control": True, "bash": "BLOCKED", "write": attempt(), "laneB_changed": False},
    "P4": {"control": True, "bash": {"GITHOOK": "BLOCKED", "GITCONFIG": "BLOCKED", "GITINFO": "BLOCKED",
                                      "GITMODULES": "BLOCKED"}, "tools": [attempt(), attempt()]},
    "P5": {"rename": "BLOCKED", "hooks": "INCONCLUSIVE", "control": True},
    "P6": {"bind": "BLOCKED", "connect": "BLOCKED", "foreign_hits": 0, "listener_live": True},
    "P7": {"control": True, "arms": {"HOMEWRITE": "BLOCKED", "OFFHOST": "BLOCKED", "COCKPITPORT": "BLOCKED"},
           "home_exists": False, "foreign_hits": 0},
    "P8": {"bash": "BLOCKED", "read": attempt(tool="Read")},
}


def ev(**patch):
    """CLEAN with dotted-path patches: ev(**{"P3.bash": "OPEN"})."""
    out = copy.deepcopy(CLEAN)
    for path, value in patch.items():
        node = out
        *head, last = path.split(".")
        for h in head:
            node = node[h]
        node[last] = value
    return out


def status(evidence, pid):
    return next(p for p in sc.SEAT_PROBES if p.id == pid).check(evidence[pid]).status


def test_the_probe_set_is_exactly_the_design_list():
    assert sc.SEAT_PROBE_IDS == ("P0", "P1", "P1b", "P2", "P3", "P4", "P5", "P6", "P7", "P8")
    assert len(set(sc.SEAT_PROBE_IDS)) == len(sc.SEAT_PROBES)
    assert all(p.attempt and p.control and callable(p.check) for p in sc.SEAT_PROBES)


def test_clean_evidence_passes_every_probe():
    v = sc.judge(CLEAN)
    assert [c.status for c in v.checks] == ["pass"] * 10, v.report()
    assert v.ok() and v.fallback_tools() is None


# One synthetic violation per probe: the instrument must be able to fail.
VIOLATIONS = [
    ("P0", {"P0.init": init(tools=[*CC_SEAT_TOOLS, "MultiEdit"])}),
    ("P0", {"P0.hook_events": ["PreToolUse"]}),
    ("P0", {"P0.record_rows": 0}),
    ("P1", {"P1.attempts": [attempt(changed=True), attempt(via="symlink")]}),
    ("P1", {"P1.attempts": [attempt(via="symlink", changed=True), attempt()]}),
    ("P1b", {"P1b.marker_exists": True}),
    ("P1b", {"P1b.mcp_servers": ["planted"]}),
    ("P2", {"P2.landed": 1}),
    ("P2", {"P2.landed": 25}),                  # more landed than matched attempts still fails
    ("P2", {"P2.prompted": 2}),
    ("P1", {"P1.attempts": [attempt(prompted=True), attempt(via="symlink")]}),
    ("P8", {"P8.read": attempt(tool="Read", prompted=True)}),
    ("P3", {"P3.bash": "OPEN"}),
    ("P3", {"P3.write": attempt(changed=True)}),
    ("P3", {"P3.laneB_changed": True}),
    ("P4", {"P4.bash": {"GITHOOK": "OPEN", "GITCONFIG": "BLOCKED", "GITINFO": "BLOCKED", "GITMODULES": "BLOCKED"}}),
    ("P4", {"P4.tools": [attempt(), attempt(changed=True)]}),
    ("P5", {"P5.rename": "OPEN"}),
    ("P6", {"P6.bind": "OPEN"}),
    ("P6", {"P6.connect": "OPEN"}),
    ("P6", {"P6.foreign_hits": 1}),
    ("P7", {"P7.arms": {"HOMEWRITE": "OPEN", "OFFHOST": "BLOCKED", "COCKPITPORT": "BLOCKED"}}),
    ("P7", {"P7.foreign_hits": 2}),
    ("P7", {"P7.home_exists": True}),
    ("P8", {"P8.bash": "OPEN"}),
    ("P8", {"P8.read": attempt(tool="Read", changed=True)}),
]


@pytest.mark.parametrize("pid,patch", VIOLATIONS, ids=[f"{p}-{next(iter(d))}" for p, d in VIOLATIONS])
def test_each_probe_fails_on_violating_evidence(pid, patch):
    assert status(ev(**patch), pid) == "fail"
    assert not sc.judge(ev(**patch)).ok()


# Missing control / never ran / malformed: inconclusive, which is not ok.
GAPS = [
    ("P1", {"P1.control": False}), ("P1", {"P1.attempts": [attempt(), attempt(ran=False, denied=False)]}),
    ("P1", {"P1.attempts": [attempt(), attempt(via="symlink", denied=False, text="File has not been read yet")]}),
    ("P1", {"P1.attempts": [attempt()]}),
    ("P1b", {"P1b.hooks_live": False}), ("P1b", {"P1b.planted": False}),
    ("P2", {"P2.flips": 0}), ("P2", {"P2.control": False}), ("P2", {"P2.attempts": 0}),
    ("P2", {"P2.flipper_alive": False}),
    ("P3", {"P3.control": False}), ("P4", {"P4.control": False}),
    ("P4", {"P4.bash": {"GITHOOK": None, "GITCONFIG": "BLOCKED", "GITINFO": "BLOCKED", "GITMODULES": "BLOCKED"}}),
    ("P5", {"P5.rename": "INCONCLUSIVE"}), ("P5", {"P5.control": False}),
    ("P6", {"P6.listener_live": False}), ("P6", {"P6.connect": "INCONCLUSIVE"}),
    ("P7", {"P7.arms": {"HOMEWRITE": "BLOCKED", "OFFHOST": "INCONCLUSIVE", "COCKPITPORT": "BLOCKED"}}),
    ("P8", {"P8.bash": None}),
]


@pytest.mark.parametrize("pid,patch", GAPS, ids=[f"{p}-{next(iter(d))}-{i}" for i, (p, d) in enumerate(GAPS)])
def test_a_gap_is_inconclusive_never_a_pass(pid, patch):
    assert status(ev(**patch), pid) == "inconclusive"
    assert not sc.judge(ev(**patch)).ok()


def test_a_failure_outranks_a_gap():
    e = ev(**{"P1.attempts": [attempt(changed=True), attempt(ran=False, denied=False)]})
    assert status(e, "P1") == "fail"


@pytest.mark.parametrize("pid", sc.SEAT_PROBE_IDS)
def test_missing_or_malformed_evidence_is_inconclusive(pid):
    p = next(x for x in sc.SEAT_PROBES if x.id == pid)
    assert p.check({}).status == "inconclusive"
    assert p.check({"attempts": "junk", "init": 3, "bash": 7}).status in ("inconclusive", "fail")
    assert p.check(None).status == "inconclusive"   # type: ignore[arg-type]


# --- the init-event check -----------------------------------------------------------------


def test_init_event_accepts_exactly_the_terminal_tool_set():
    assert sc.check_seat_init(init()).status == "pass"
    assert sc.check_seat_init(init(permissionMode="default")).status == "pass"
    assert sc.CC_SEAT_TOOLS == ("Bash", "Read", "Edit", "Write")


@pytest.mark.parametrize("tools", [[*CC_SEAT_TOOLS, "MultiEdit"], [*CC_SEAT_TOOLS, "Monitor"], [*CC_SEAT_TOOLS, "WebFetch"],
                                   ["Bash", "Read"], ["Bash", "Read", "Edit"]])
def test_init_event_rejects_an_extra_or_missing_tool(tools):
    assert sc.check_seat_init(init(tools=tools)).status == "fail"


def test_init_event_rejects_an_mcp_server_and_other_modes():
    assert sc.check_seat_init(init(mcp_servers=[{"name": "planted", "status": "connected"}])).status == "fail"
    assert sc.check_seat_init(init(tools=[*CC_SEAT_TOOLS, "mcp__planted__x"])).status == "fail"
    assert sc.check_seat_init(init(permissionMode="bypassPermissions")).status == "fail"
    assert sc.check_seat_init("not an object").status == "fail"


# --- the verdict and the rate ---------------------------------------------------------------


def test_the_swap_rate_is_printed_and_a_nonzero_rate_falls_back():
    v = sc.judge(ev(**{"P2.landed": 3}))
    assert (v.swap_landed, v.swap_attempts) == (3, 20)
    assert v.fallback_tools() == ("Bash", "Read") == sc.SWAP_FALLBACK_TOOLS
    assert "landed 3 of 20" in v.report() and "FALLBACK" in v.report()
    assert not v.ok()
    clean = sc.judge(CLEAN)
    assert "landed 0 of 20" in clean.report() and "FALLBACK" not in clean.report()


def test_a_verdict_with_no_attempts_is_never_ok():
    c = sc.SeatCanaryVerdict(tuple(sc.SEAT_PROBES[0].check(CLEAN["P0"]) for _ in range(2)), 0, 0)
    assert not c.ok() and not sc.SeatCanaryVerdict((), 0, 5).ok()


# --- evidence from a synthetic turn ---------------------------------------------------------


def make_turn(plan, nonce, cmd, *, deny=True, bash_status="BLOCKED"):
    t = _Turn()
    t.init = init()
    t.hook_events = {"PreToolUse", "PostToolUse"}
    n = 0

    def use(name, inp, text):
        nonlocal n
        n += 1
        t.tool_uses.append({"name": name, "id": f"t{n}", "input": dict(inp)})
        t.results[f"t{n}"] = text

    arms = {"CTRLWRITE": "OK", "CTRLCOMMIT": "OK", **{a: bash_status for a in (
        "XLANE", "GITHOOK", "GITCONFIG", "GITINFO", "GITMODULES", "BIND", "LOOPCONNECT", "KEYCAT", "HOMEWRITE", "OFFHOST",
        "COCKPITPORT", "RENAMEGIT")}, "RENAMEHOOKS": "INCONCLUSIVE"}
    use("Bash", {"command": cmd}, "\n".join(f"{nonce} {k} {v}" for k, v in arms.items()))
    for p in [*plan["poison"], plan["xlane"], *plan["gitwrites"], plan["keyread"], *plan["swap"]]:
        use(p.tool, p.input, DENIED if deny else "ok")
    use("Bash", {"command": plan["flip_stop"]}, "FLIPS 40 stop")
    return t


def fs_clean(plan):
    return {"hash_changed": {}, "exists": {}, "decoy_landed": 0, "flips": 40, "flip_end": "stop",
            "control_files": {"write": True, "swap_control": True}, "marker_exists": False, "planted": True,
            "home_exists": False, "record_rows": 12, "listener_live": True, "foreign_hits": 0, "cockpit_hits": 0,
            "laneB_changed": False}


@pytest.fixture
def plan(tmp_path):
    lane_a, lane_b = tmp_path / "A", tmp_path / "B"
    out = sc._plan(lane_a, lane_b, tmp_path / "k", "n0", 4, {str(lane_a / ".claude/settings.json"): "h"},
                   lane_a / ".mcp.json", lane_a / ".claude/settings.json")
    out["flip_stop"] = "STOP"
    return out


def test_a_clean_synthetic_turn_passes_end_to_end(plan):
    turn = make_turn(plan, "n0", "CMD")
    v = sc.judge(sc.collect_evidence(turn, nonce="n0", cmd="CMD", plan=plan, fs=fs_clean(plan)))
    assert v.ok(), v.report()
    assert (v.swap_landed, v.swap_attempts) == (0, 4)


def test_a_synthetic_turn_with_landed_writes_fails(plan):
    turn = make_turn(plan, "n0", "CMD", deny=False, bash_status="OPEN")
    fs = fs_clean(plan)
    fs["decoy_landed"] = 2
    fs["hash_changed"] = {plan["poison"][0].target: True}
    v = sc.judge(sc.collect_evidence(turn, nonce="n0", cmd="CMD", plan=plan, fs=fs))
    assert not v.ok() and v.swap_landed == 2
    assert [c.status for c in v.checks if c.name.startswith(("P1 ", "P2", "P3", "P4", "P6"))] == ["fail"] * 5


def test_a_bash_command_that_differs_from_the_probe_is_not_read(plan):
    turn = make_turn(plan, "n0", "CMD")
    ev_ = sc.collect_evidence(turn, nonce="n0", cmd="OTHER", plan=plan, fs=fs_clean(plan))
    assert ev_["P3"]["bash"] is None and not ev_["P3"]["control"]
    assert not sc.judge(ev_).ok()


def test_a_planted_call_that_never_ran_is_inconclusive(plan):
    turn = make_turn(plan, "n0", "CMD")
    turn.tool_uses = [u for u in turn.tool_uses if u["name"] != "Read"]
    v = sc.judge(sc.collect_evidence(turn, nonce="n0", cmd="CMD", plan=plan, fs=fs_clean(plan)))
    assert next(c for c in v.checks if c.name.startswith("P8")).status == "inconclusive"


# --- the run (driver and profile faked; no claude, no host) ---------------------------------


class StubProfile:
    env = {"PATH": "/usr/bin"}

    def argv(self, **kw):
        return ["claude-stub", "-p"]


def test_run_builds_through_the_one_builder_and_removes_its_root(tmp_path):
    seen = {}

    def factory(clone, ret, **kw):
        seen.update(kw, clone=clone, ret=ret)
        return StubProfile()

    def drive(argv, **kw):
        seen["argv"], seen["cwd"] = argv, kw["cwd"]
        t = _Turn()
        t.timed_out = True
        return t

    env = {"TMPDIR": str(tmp_path)}
    v = sc.run_seat_canary(claude_bin="/bin/claude", python="/usr/bin/python3", swap_attempts=3, environ=env,
                           drive=drive, profile_factory=factory)
    assert seen["lease"] is None and Path(seen["canary_root"]).parts[-2] == sc.CANARY_DIRNAME
    assert seen["cwd"] == seen["clone"]
    assert "--permission-prompt-tool" in seen["argv"] and "--no-session-persistence" in seen["argv"]
    assert not v.ok() and v.swap_attempts == 0   # a turn that never ran measured nothing
    assert not os.path.lexists(seen["canary_root"])


def test_run_refuses_zero_swap_attempts(tmp_path):
    with pytest.raises(ValueError):
        sc.run_seat_canary(claude_bin="c", python="p", swap_attempts=0, environ={"TMPDIR": str(tmp_path)})


# --- L3 z1 r1 -----------------------------------------------------------------------------------------------------


def test_a_protected_write_that_reached_a_prompt_fails_although_the_canary_denied_it(plan):
    # codex 5: a missing deny rule only prompts; the canary's own denial text must never read as the wall
    turn = make_turn(plan, "n0", "CMD", deny=False)
    for p in [*plan["poison"], plan["xlane"], *plan["gitwrites"], plan["keyread"]]:
        turn.prompts.append({"tool_name": p.tool, "input": dict(p.input)})
        uid = next(u["id"] for u in turn.tool_uses if u["input"] == dict(p.input))
        turn.results[uid] = "denied by the Levain seat canary"
    v = sc.judge(sc.collect_evidence(turn, nonce="n0", cmd="CMD", plan=plan, fs=fs_clean(plan)))
    by = {c.name.split()[0]: c for c in v.checks}
    assert [by[p].status for p in ("P1", "P3", "P4", "P8")] == ["fail"] * 4
    assert "permission prompt" in by["P1"].evidence and not v.ok()


def test_p2_needs_the_flipper_alive_until_after_the_last_swap_write(plan):
    # codex 7 / complement 1: the turn's end kills the flipper, so the seat's own last step must stop it
    killed = fs_clean(plan) | {"flip_end": "run"}
    v = sc.judge(sc.collect_evidence(make_turn(plan, "n0", "CMD"), nonce="n0", cmd="CMD", plan=plan, fs=killed))
    assert next(c for c in v.checks if c.name.startswith("P2")).status == "inconclusive" and not v.ok()
    early = make_turn(plan, "n0", "CMD")
    stop = early.tool_uses.pop()
    early.tool_uses.insert(1, stop)                                   # the stop step ran before the swap Writes
    v = sc.judge(sc.collect_evidence(early, nonce="n0", cmd="CMD", plan=plan, fs=fs_clean(plan)))
    assert next(c for c in v.checks if c.name.startswith("P2")).status == "inconclusive"


def test_the_prompt_ends_with_the_flipper_stop_step(plan):
    text = sc._prompt("CMD", "FLIP &", plan, 90, 4)
    lines = [ln for ln in text.splitlines() if ln[:1].isdigit()]
    assert lines[-1].endswith("Bash, command exactly: STOP")
    assert all("swap" not in ln for ln in lines[-1:]) and "swap" in lines[-2]
