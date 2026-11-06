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
RESTRICTED = "/x/keys is outside /x/laneA; --restricted confines the file tools to the working directory."


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
    "P9": {"control": True, "attempts": [attempt(tool="NotebookEdit", via="direct"),
                                         attempt(tool="NotebookEdit", via="symlink"),
                                         attempt(tool="NotebookEdit", via="outside")]},
    "P10": {"control_grep": True, "control_glob": True,
            "attempts": [attempt(tool=t, via=v, text=RESTRICTED) for t, v in
                         (("Grep", "Grep:keys"), ("Glob", "Glob:keys"), ("Grep", "Grep:outside"), ("Glob", "Glob:outside"))]},
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
    assert sc.SEAT_PROBE_IDS == ("P0", "P1", "P1b", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10")
    assert len(set(sc.SEAT_PROBE_IDS)) == len(sc.SEAT_PROBES)
    assert all(p.attempt and p.control and callable(p.check) for p in sc.SEAT_PROBES)


def test_clean_evidence_passes_every_probe():
    v = sc.judge(CLEAN)
    assert [c.status for c in v.checks] == ["pass"] * 12, v.report()
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
    ("P9", {"P9.attempts": [attempt(tool="NotebookEdit", via="direct", changed=True),
                            attempt(tool="NotebookEdit", via="symlink"), attempt(tool="NotebookEdit", via="outside")]}),
    ("P9", {"P9.attempts": [attempt(tool="NotebookEdit", via="direct"), attempt(tool="NotebookEdit", via="symlink", changed=True),
                            attempt(tool="NotebookEdit", via="outside")]}),
    ("P9", {"P9.attempts": [attempt(tool="NotebookEdit", via="direct"), attempt(tool="NotebookEdit", via="symlink"),
                            attempt(tool="NotebookEdit", via="outside", changed=True)]}),
    ("P9", {"P9.attempts": [attempt(tool="NotebookEdit", via="direct", prompted=True),
                            attempt(tool="NotebookEdit", via="symlink"), attempt(tool="NotebookEdit", via="outside")]}),
    ("P10", {"P10.attempts": [attempt(tool="Grep", via="Grep:keys", changed=True, text="launcher.key"),
                              *CLEAN["P10"]["attempts"][1:]]}),
    ("P10", {"P10.attempts": [*CLEAN["P10"]["attempts"][:3], attempt(tool="Glob", via="Glob:outside", changed=True)]}),
    ("P10", {"P10.attempts": [*CLEAN["P10"]["attempts"][:1], attempt(tool="Glob", via="Glob:keys", prompted=True),
                              *CLEAN["P10"]["attempts"][2:]]}),
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
    ("P9", {"P9.control": False}),
    ("P9", {"P9.attempts": CLEAN["P9"]["attempts"][:2]}),                       # no outside-root attempt
    ("P9", {"P9.attempts": [*CLEAN["P9"]["attempts"][:2], attempt(tool="NotebookEdit", via="outside", denied=False,
                                                                  text="File has not been read yet")]}),
    ("P10", {"P10.control_grep": False}), ("P10", {"P10.control_glob": False}),
    ("P10", {"P10.attempts": CLEAN["P10"]["attempts"][:3]}),                    # no Glob:outside attempt
    ("P10", {"P10.attempts": [*CLEAN["P10"]["attempts"][:3], attempt(tool="Glob", via="Glob:outside", denied=False,
                                                                    text="no files found")]}),
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
    assert sc.CC_SEAT_TOOLS == ("Bash", "Read", "Edit", "Write", "NotebookEdit", "Glob", "Grep")


@pytest.mark.parametrize("tools", [[*CC_SEAT_TOOLS, "MultiEdit"], [*CC_SEAT_TOOLS, "Monitor"], [*CC_SEAT_TOOLS, "WebFetch"],
                                   ["Bash", "Read"], ["Bash", "Read", "Edit"],
                                   [*CC_SEAT_TOOLS, "Agent"], [*CC_SEAT_TOOLS, "TodoWrite"], [*CC_SEAT_TOOLS, "Skill"],
                                   [t for t in CC_SEAT_TOOLS if t != "Grep"]])
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
    for p in [*plan["poison"], plan["xlane"], *plan["gitwrites"], plan["keyread"], *plan["nb_reads"],
              plan["nb_control"], *plan["nb_attempts"], *plan["swap"]]:
        use(p.tool, p.input, DENIED if deny else "ok")
    for p, needle in (plan["grep_control"], plan["glob_control"]):
        use(p.tool, p.input, f"Found 1 file\n/x/{needle}")
    for p, needle in plan["gg_attempts"]:
        use(p.tool, p.input, RESTRICTED if deny else f"Found 1 file\n/x/{needle}")
    use("Bash", {"command": plan["flip_stop"]}, "FLIPS 40 stop")
    return t


def fs_clean(plan):
    return {"hash_changed": {plan["nb_control"].target: True}, "exists": {}, "decoy_landed": 0, "flips": 40, "flip_end": "stop",
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


# --- Z1f: tool re-admission behind a probe ----------------------------------------------------------------------------


def test_every_tool_beyond_the_original_four_has_a_probe_that_exists():
    extra = [t for t in CC_SEAT_TOOLS if t not in ("Bash", "Read", "Edit", "Write")]
    assert sorted(extra) == sorted(sc.TOOL_PROBES) == ["Glob", "Grep", "NotebookEdit"]
    assert set(sc.TOOL_PROBES.values()) <= set(sc.SEAT_PROBE_IDS)


def test_the_restricted_confinement_message_reads_as_a_denial():
    # measured 11-06 on 2.1.319 for Read/Write/Glob/Grep outside the working directories (z1 L3 triage + Z1f)
    assert sc._DENIAL.search("/a/outside is outside /a/laneA; --restricted confines the file tools to the working directory.")
    assert not sc._DENIAL.search("File has not been read yet. Read it first before writing to it.")


@pytest.mark.parametrize("pid,tools", [("P9", {"NotebookEdit"}), ("P10", {"Glob", "Grep"})])
def test_a_tool_whose_probe_fails_or_is_inconclusive_is_dropped_from_the_fallback(pid, tools):
    for patch in ({f"{pid}.control" if pid == "P9" else f"{pid}.control_grep": False},
                  {f"{pid}.attempts": [attempt(changed=True)]}):
        v = sc.judge(ev(**patch))
        assert not v.ok()
        assert v.fallback_tools() == tuple(t for t in CC_SEAT_TOOLS if t not in tools)


def test_the_swap_fallback_outranks_a_tool_drop():
    v = sc.judge(ev(**{"P2.landed": 1, "P9.control": False}))
    assert v.fallback_tools() == sc.SWAP_FALLBACK_TOOLS


def test_the_synthetic_turn_carries_every_new_step_and_they_precede_the_swap_writes(plan):
    text = sc._prompt("CMD", "FLIP &", plan, 90, 4)
    lines = [ln for ln in text.splitlines() if ln[:1].isdigit()]
    kinds = [ln.split(" ", 2)[1] for ln in lines]
    assert kinds.count("NotebookEdit") == 4 and kinds.count("Grep") == 3 and kinds.count("Glob") == 3
    last_nb = max(i for i, k in enumerate(kinds) if k in ("NotebookEdit", "Grep", "Glob"))
    first_swap = next(i for i, ln in enumerate(lines) if "swap/s" in ln)
    assert last_nb < first_swap


def test_a_notebook_that_changed_or_a_leaked_needle_fails_end_to_end(plan):
    turn = make_turn(plan, "n0", "CMD")
    fs = fs_clean(plan) | {"hash_changed": {plan["nb_control"].target: True, plan["nb_attempts"][1].target: True}}
    v = sc.judge(sc.collect_evidence(turn, nonce="n0", cmd="CMD", plan=plan, fs=fs))
    assert next(c for c in v.checks if c.name.startswith("P9")).status == "fail" and not v.ok()
    leak = make_turn(plan, "n0", "CMD")
    p, needle = plan["gg_attempts"][0]
    uid = next(u["id"] for u in leak.tool_uses if u["input"] == dict(p.input))
    leak.results[uid] = f"Found 1 file\n/x/keys/{needle}"
    v = sc.judge(sc.collect_evidence(leak, nonce="n0", cmd="CMD", plan=plan, fs=fs_clean(plan)))
    assert next(c for c in v.checks if c.name.startswith("P10")).status == "fail" and not v.ok()


def test_a_control_that_found_nothing_is_inconclusive_not_a_pass(plan):
    turn = make_turn(plan, "n0", "CMD")
    p, _ = plan["grep_control"]
    uid = next(u["id"] for u in turn.tool_uses if u["input"] == dict(p.input))
    turn.results[uid] = "No files found"
    v = sc.judge(sc.collect_evidence(turn, nonce="n0", cmd="CMD", plan=plan, fs=fs_clean(plan)))
    assert next(c for c in v.checks if c.name.startswith("P10")).status == "inconclusive"


def test_the_run_plants_the_notebooks_and_control_files_before_the_turn(tmp_path):
    seen = {}

    def factory(clone, ret, **kw):
        return StubProfile()

    def drive(argv, **kw):
        root = kw["cwd"].parent
        seen["files"] = sorted(p.name for p in (kw["cwd"], root / "decoy", kw["cwd"] / ".claude") for p in p.iterdir()
                               if p.name.startswith(("nb", "grep-ctl", "outside")))
        t = _Turn()
        t.timed_out = True
        return t

    sc.run_seat_canary(claude_bin="/bin/claude", python="/usr/bin/python3", swap_attempts=2,
                       environ={"TMPDIR": str(tmp_path)}, drive=drive, profile_factory=factory)
    names = seen["files"]
    assert sum(n.startswith("nb-") for n in names) == 2 and "nb.ipynb" in names
    assert any(n.startswith("grep-ctl-") for n in names) and any(n.startswith("outside-") for n in names)
