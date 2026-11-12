#!/usr/bin/env python3
"""One rerun for a test on the flake ledger, and nothing else (CI move 2).

Usage: ci_flake_rerun.py <pytest exit code> <pytest log> <record dir> <label>

The shard step runs pytest with -rfE and tees the log. When pytest failed, this reads the `FAILED <node id>` lines and
the ledger (tests/ci/flake_ledger.txt: one node id per line, `#` comments; a missing file is an empty ledger, so with no
ledger nothing ever reruns). It exits 1, with no rerun, unless pytest exited 1 (tests failed; any other code is an
interrupt, usage or collection error), every failure is a FAILED line (no ERROR line), and every failed node id is on
the ledger, and the log carries no xdist worker-crash report on a line of its own. Otherwise it reruns those node ids once; the rerun counts as
passed only if pytest exited 0 and its junit file shows every requested node passed (a skip, xfail, error or deselected
node is not a pass). A test not on the ledger never reruns.

The rerun is printed, appended to $GITHUB_STEP_SUMMARY when that is set, and written as a JSON row to
<record dir>/rows.jsonl for the workflow to upload. This script writes nothing else and commits nothing.
"""
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

_run = subprocess.run  # indirection so tests patch this, not the global subprocess.run
LEDGER = Path(__file__).resolve().parent.parent / "tests" / "ci" / "flake_ledger.txt"


def ledger_ids(path=LEDGER):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    return {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")}


_HEADER = re.compile(r"^=+ (.+?) =+$")
_NODE = re.compile(r"^\S+?\.py(::.*)?$")


def _split_reason(rest):
    """The node id: everything before the first " - " that is not inside a [...] parametrize id."""
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "[":
            depth += 1
        elif ch == "]" and depth:
            depth -= 1
        elif depth == 0 and rest.startswith(" - ", i):
            return rest[:i].strip()
    return rest.strip()


_ERR_ITEM = re.compile(r"^_+ ERROR (at (setup|teardown) of|collecting) ")
# xdist's own crash reports, each only at the START of a line (optionally behind a `[gwN]` prefix): a dead worker is
# infrastructure, never a flake, so it is never rerun. The same pattern is read against the reason of a FAILED line, again
# only from its start, so an assertion message that merely quotes this text stays an ordinary failure.
_CRASH = re.compile(
    r"(?:\[gw\d+\]\s*)?\s*(?:node down:|replacing crashed worker |worker '[^']*' crashed while running |"
    r"worker \S+ crashed and worker restarting disabled|maximum crashed workers reached)")


def _rerun_all_passed(xml_path, requested):
    """True only if the rerun's junit file holds exactly one testcase per requested node and none of them carries a
    failure, error or skip (an xfail is written as a skip). pytest exits 0 for a skipped or xfailed node, so the exit
    code alone cannot say a test actually ran; an unreadable file or a deselected node is not a pass."""
    try:
        cases = list(ET.parse(xml_path).getroot().iter("testcase"))
    except (OSError, ET.ParseError):
        return False
    bad = {"failure", "error", "skipped"}
    return len(cases) == requested and not any(child.tag in bad for case in cases for child in case)


def parse_log(text):
    """(failed node ids in order, whether anything errored). Only the region after the LAST "short test summary
    info" header is read for FAILED / ERROR lines (the real summary is contiguous at the end, so a header spoofed by
    captured output earlier cannot reopen it); a log with no such header yields no failures (the caller then does not
    rerun). Fail closed: any "ERROR " line in the summary, and any unparseable "FAILED " line, counts as errored. An
    "ERRORS" section header counts only when it precedes that summary and is followed by a pytest "ERROR at
    setup/teardown of" (or "collecting") item line, so a bare header in captured output is ignored. An xdist crash
    report at the start of any line, or at the start of a FAILED line's reason, counts as errored."""
    lines = text.splitlines()
    # pytest cuts a summary reason to the terminal width, so the crash report is also read from the whole log
    crashed = any(_CRASH.match(ln) for ln in lines)
    last = max((i for i, ln in enumerate(lines)
                if (m := _HEADER.match(ln)) and m.group(1).strip() == "short test summary info"), default=None)
    if last is None:
        return [], crashed
    failed, errored = [], crashed
    for i, ln in enumerate(lines[:last]):
        m = _HEADER.match(ln)
        if m and m.group(1).strip() == "ERRORS" and any(_ERR_ITEM.match(x) for x in lines[i + 1:last]):
            errored = True
    for ln in lines[last + 1:]:
        if _HEADER.match(ln):
            break
        if ln.startswith("ERROR "):
            errored = True
        elif ln.startswith("FAILED "):
            rest = ln.split(" ", 1)[1]
            nid = _split_reason(rest)
            if not _NODE.match(nid) or _CRASH.match(rest[len(nid):].removeprefix(" - ")):
                errored = True
            elif nid not in failed:
                failed.append(nid)
    return failed, errored


def main(argv):
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    code, log_path, record_dir, label = int(argv[1]), Path(argv[2]), Path(argv[3]), argv[4]
    failed, errored = parse_log(log_path.read_text(encoding="utf-8", errors="replace"))
    if code != 1 or errored or not failed:
        print(f"no rerun: pytest exit {code}, {len(failed)} failed line(s), errors={errored}")
        return 1
    ledger = ledger_ids()
    off = [n for n in failed if n not in ledger]
    if off:
        print("no rerun: failures not on the flake ledger:")
        for n in off:
            print(f"  {n}")
        return 1
    print(f"rerunning once, all {len(failed)} failure(s) are on the flake ledger:")
    for n in failed:
        print(f"  {n}")
    record_dir.mkdir(parents=True, exist_ok=True)
    junit = record_dir / "rerun.xml"
    junit.unlink(missing_ok=True)
    rerun = _run(["pytest", "-q", "-rf", "--basetemp=/tmp/levain-pt-rerun", f"--junitxml={junit}", *failed])
    passed = rerun.returncode == 0 and _rerun_all_passed(junit, len(failed))
    row = {"label": label, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tests": failed,
           "rerun_passed": passed, "sha": os.environ.get("GITHUB_SHA", ""), "run_id": os.environ.get("GITHUB_RUN_ID", "")}
    with (record_dir / "rows.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Flake rerun ({label}): {'passed' if passed else 'failed'} on the one rerun\n\n")
            f.write("\n".join(f"- `{n}`" for n in failed) + "\n\n")
    print(f"rerun {'passed' if passed else 'failed'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
