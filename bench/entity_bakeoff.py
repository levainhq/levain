#!/usr/bin/env python3
"""Repeatable reliability bake-off for `levain run` entities across (model, route, tool-calling mode).

Drives the REAL levain turn logic (build_entity_agent + the narrate-without-act backstop) on a
multi-step coding task, so a config's pass-rate reflects the shipped harness, not a mock. Built to
DELINEATE open-model default-viability empirically (2026-07-17): it found the weak-open-model gap was
a ROUTE artifact — the `ollama/` litellm provider drops glm/kimi tool-calls to JSON-text (turn ends),
while the OpenAI-compatible `/v1` endpoint returns structured tool_calls (glm/kimi/minimax 10/10).

Done-oracle: run the workspace tests FROM DISK after the turn (un-fakeable — not the model's report).

Usage:
    PYTHONPATH=. python bench/entity_bakeoff.py --model glm-5.2:cloud --route prod --task hard --n 10
      --route  ollama  = the `ollama/` litellm provider (native or, without --native, prompt-mode)
               openai  = Ollama's OpenAI-compatible /v1 endpoint (hand-built LLM)
               prod    = the actual `levain.run._resolve_llm_kwargs` production routing (STOP-TIME check)
      --task   easy (single-file median fix) | hard (multi-file: 2 bugs + a missing function)
      BAKEOFF_TRACE=1 attaches a per-event trace to each trial for failure diagnosis.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

CALC_PY_BUGGY = '''\
def median(xs):
    """Return the median of a non-empty list of numbers."""
    s = sorted(xs)
    n = len(s)
    # BUG: always returns a single middle element — wrong for even-length lists.
    return s[n // 2]
'''

TEST_PY = '''\
from calc import median


def test_odd():
    assert median([3, 1, 2]) == 2


def test_even():
    assert median([1, 2, 3, 4]) == 2.5


def test_even_two():
    assert median([10, 20]) == 15.0
'''

TASK = (
    "The file calc.py in your workspace has a median() function with a bug: it is wrong for "
    "even-length lists (it should average the two middle values). A test file test_calc.py is "
    "present. Fix the bug in calc.py so every test passes, then run the tests to confirm."
)


# ---------- HARD fixture: multiple bugs across multiple files + a missing function ----------
# More steps/seams (view several files → edit stats twice → edit geometry → run tests) = more surface
# for the mid-turn stall (model does partial work, narrates the rest, yields without finishing).
STATS_PY_BUGGY = '''\
def mean(xs):
    return sum(xs) / len(xs)


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2]  # BUG: wrong for even-length lists
'''

GEOMETRY_PY_BUGGY = '''\
import math


def circle_area(r):
    return math.pi * r  # BUG: should be pi * r ** 2
'''

TEST_HARD_PY = '''\
from stats import mean, median, mode
from geometry import circle_area
import math


def test_mean():
    assert mean([2, 4, 6]) == 4


def test_median_even():
    assert median([1, 2, 3, 4]) == 2.5


def test_mode():
    assert mode([1, 2, 2, 3, 3, 3]) == 3


def test_circle_area():
    assert math.isclose(circle_area(2), math.pi * 4)
'''

TASK_HARD = (
    "The package in your workspace has failing tests (run test_suite.py to see). Do ALL of the "
    "following, then run the tests to confirm they all pass: (1) fix the median() bug in stats.py "
    "(it is wrong for even-length lists — average the two middle values); (2) add a mode() function "
    "to stats.py that returns the most frequent value; (3) fix the circle_area() bug in geometry.py "
    "(it should be pi * r squared). Make every change, then run the tests."
)


def _write_task(ws: Path, task: str):
    if task == "hard":
        (ws / "stats.py").write_text(STATS_PY_BUGGY)
        (ws / "geometry.py").write_text(GEOMETRY_PY_BUGGY)
        (ws / "test_suite.py").write_text(TEST_HARD_PY)
    else:
        (ws / "calc.py").write_text(CALC_PY_BUGGY)
        (ws / "test_calc.py").write_text(TEST_PY)


def _task_prompt(task: str) -> str:
    return TASK_HARD if task == "hard" else TASK


def _test_file(task: str) -> str:
    return "test_suite.py" if task == "hard" else "test_calc.py"


def _scaffold_entity(root: Path, task: str = "easy") -> Path:
    ent = root / "ent"
    (ent / ".levain").mkdir(parents=True)
    (ent / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    seed = ent / "seed"
    seed.mkdir()
    (seed / "origin.md").write_text(
        "# Who You Are — Tester\n\nYou are **Tester**, a sovereign coding partner.\n\n"
        "Your job: complete coding tasks for your operator.\n"
    )
    (seed / "world.md").write_text(
        "# Who Your Operator Is\n\nPhill Clapham. 46. Columbus, OH.\n"
    )
    (seed / "partnership.md").write_text(
        "# How We Work\n\nYou are a partner, not an assistant.\n"
    )
    ws = ent / "workspace"
    ws.mkdir()
    _write_task(ws, task)
    return ent


_BUGGY = {
    "easy": {"calc.py": CALC_PY_BUGGY},
    "hard": {"stats.py": STATS_PY_BUGGY, "geometry.py": GEOMETRY_PY_BUGGY},
}


def _tests_pass(ws: Path, task: str) -> bool:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", _test_file(task)],
        cwd=str(ws), capture_output=True, text=True, timeout=120,
    )
    return r.returncode == 0


def _edit_made(ws: Path, task: str) -> bool:
    # any source file changed from its buggy original (hard task: mode() is added to stats.py too)
    return any((ws / f).read_text() != orig for f, orig in _BUGGY[task].items())


def _classify_failure(events, ws: Path, task: str) -> str:
    from levain.firing.agent_reply import tool_action_summary
    acted = any(
        tool_action_summary(e) is not None
        for e in events
        if getattr(e, "source", None) == "agent"
    )
    if not acted:
        return "zero_action_stall"       # mode-1 (backstop should have caught)
    if not _edit_made(ws, task):
        return "acted_no_edit"           # mode-2: viewed/ran but never edited
    return "edited_but_wrong"            # made an edit(s), tests still fail (partial/incorrect)


def run_trial(model, native, idx, route="ollama", task="easy"):
    from levain.firing.agent_reply import LEVAIN_ACT_NUDGE, planned_without_acting
    from levain.firing.openhands.entity import build_entity_agent
    from levain.firing.openhands.tools import build_entity_tools
    from levain.run import _WORKSPACE_SUBDIR, _resolve_model, confinement_supported
    from openhands.sdk import LLM, Conversation

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as td:
        ent = _scaffold_entity(Path(td), task)
        ws = ent / _WORKSPACE_SUBDIR
        # route "ollama": production path (ollama/ litellm provider, native /api/chat).
        # route "openai": the OpenAI-compatible /v1 endpoint (what the live L4 test uses) — a distinct
        # tool-call extraction path that MAY return structured tool_calls where ollama/ returns JSON-text.
        if route == "prod":
            # Exercise the ACTUAL production routing helper (STOP-TIME check that run.py's real path
            # — not a hand-built LLM — completes the task end-to-end).
            from levain.run import _resolve_llm_kwargs
            llm = LLM(usage_id="bakeoff", **_resolve_llm_kwargs(model, "http://localhost:11434", None))
        elif route == "openai":
            bare = model if "/" not in model else model.split("/", 1)[1]
            llm = LLM(model=f"openai/{bare}", base_url="http://localhost:11434/v1",
                      api_key="ollama", native_tool_calling=native, usage_id="bakeoff")
        else:
            llm = LLM(model=_resolve_model(model), native_tool_calling=native, usage_id="bakeoff")
        tools = build_entity_tools(with_bash=confinement_supported())
        binding = build_entity_agent(ent, llm, tools=tools)
        conv = Conversation(binding.agent, workspace=str(ws), visualizer=None)
        nudged = False
        err = None
        try:
            conv.send_message(_task_prompt(task))
            conv.run()
            if planned_without_acting(conv.state.events):
                nudged = True
                conv.send_message(LEVAIN_ACT_NUDGE)
                conv.run()
        except Exception as e:  # noqa: BLE001 — a failed trial is a data point, not a crash
            err = f"{type(e).__name__}: {e}"[:160]
        dt = round(time.monotonic() - t0, 1)
        trace = _trace(conv.state.events) if os.environ.get("BAKEOFF_TRACE") else None
        if err is not None:
            return {"i": idx, "ok": False, "nudged": nudged, "failure": "exception",
                    "err": err, "secs": dt, "trace": trace}
        ok = _tests_pass(ws, task)
        return {"i": idx, "ok": ok, "nudged": nudged,
                "failure": None if ok else _classify_failure(conv.state.events, ws, task),
                "secs": dt, "trace": trace}


def _trace(events):
    """Compact per-event trace: tool actions + the agent's message/finish text (truncated)."""
    from levain.firing.agent_reply import (
        finish_message, message_event_text, tool_action_summary,
    )
    out = []
    for e in events:
        src = getattr(e, "source", "?")
        ta = tool_action_summary(e) if src == "agent" else None
        if ta is not None:
            out.append(f"[{src}:TOOL] {str(ta)[:90]}")
            continue
        txt = message_event_text(e) or (finish_message(e) if src == "agent" else None)
        if txt:
            out.append(f"[{src}] {txt[:140]}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="glm-5.2:cloud")
    ap.add_argument("--native", action="store_true", help="native tool-calling (default: prompt mode)")
    ap.add_argument("--route", choices=["ollama", "openai", "prod"], default="ollama")
    ap.add_argument("--task", choices=["easy", "hard"], default="easy")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    # `prod` and `openai` route with native FC; only bare `ollama` without --native is prompt-mode.
    mode = "prompt" if (args.route == "ollama" and not args.native) else "native"
    label = f"{args.model} [{args.route}/{mode}] task={args.task}" + (f" {args.tag}" if args.tag else "")
    print(f"=== BAKE-OFF: {label} — n={args.n} ===", flush=True)
    results = []
    for i in range(args.n):
        r = run_trial(args.model, args.native, i, route=args.route, task=args.task)
        results.append(r)
        flag = "PASS" if r["ok"] else f"FAIL({r['failure']})"
        print(f"  trial {i}: {flag}  nudged={r['nudged']}  {r['secs']}s"
              + (f"  {r.get('err','')}" if r.get("err") else ""), flush=True)

    passed = sum(1 for r in results if r["ok"])
    nudged = sum(1 for r in results if r["nudged"])
    fails = {}
    for r in results:
        if not r["ok"]:
            fails[r["failure"]] = fails.get(r["failure"], 0) + 1
    print(f"--- {label}: {passed}/{args.n} passed | nudged {nudged}/{args.n} | failures {fails or '{}'} ---", flush=True)
    print("JSON " + json.dumps({"label": label, "passed": passed, "n": args.n,
                                "nudged": nudged, "failures": fails, "trials": results}), flush=True)


if __name__ == "__main__":
    main()
