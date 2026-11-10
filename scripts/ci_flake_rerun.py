#!/usr/bin/env python3
"""One rerun for a test on the flake ledger, and nothing else (CI move 2).

Usage: ci_flake_rerun.py <pytest exit code> <pytest log> <record dir> <label>

The shard step runs pytest with -rfE and tees the log. When pytest failed, this reads the `FAILED <node id>` lines and
the ledger (tests/ci/flake_ledger.txt: one node id per line, `#` comments; a missing file is an empty ledger, so with no
ledger nothing ever reruns). It exits 1, with no rerun, unless pytest exited 1 (tests failed; any other code is an
interrupt, usage or collection error), every failure is a FAILED line (no ERROR line), and every failed node id is on
the ledger. Otherwise it reruns those node ids once and exits with that rerun's status. A test not on the ledger never
reruns.

The rerun is printed, appended to $GITHUB_STEP_SUMMARY when that is set, and written as a JSON row to
<record dir>/rows.jsonl for the workflow to upload. This script writes nothing else and commits nothing.
"""
import json
import os
import re
import subprocess
import sys
import time
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


def parse_log(text):
    """(failed node ids in order, whether anything errored). Only the region after the "short test summary info"
    header is read, so a captured "ERROR ..." / "FAILED ..." output line cannot mislead it; a log with no such header
    yields no failures (the caller then does not rerun). An "ERRORS" section header counts as errored."""
    failed, errored, in_summary = [], False, False
    for ln in text.splitlines():
        m = _HEADER.match(ln)
        if m:
            title = m.group(1).strip()
            if title == "ERRORS":
                errored = True
            in_summary = title == "short test summary info"
            continue
        if not in_summary:
            continue
        if ln.startswith("FAILED ") or ln.startswith("ERROR "):
            nid = _split_reason(ln.split(" ", 1)[1])
            if not _NODE.match(nid):
                continue
            if ln.startswith("ERROR "):
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
    rerun = _run(["pytest", "-q", "-rf", "--basetemp=/tmp/levain-pt-rerun", *failed])
    passed = rerun.returncode == 0
    row = {"label": label, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tests": failed,
           "rerun_passed": passed, "sha": os.environ.get("GITHUB_SHA", ""), "run_id": os.environ.get("GITHUB_RUN_ID", "")}
    record_dir.mkdir(parents=True, exist_ok=True)
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
