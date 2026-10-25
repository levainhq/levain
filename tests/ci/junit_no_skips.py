"""Fail unless a pytest junit file shows at least one passed test and zero skips, errors or failures.

Usage: python tests/ci/junit_no_skips.py <junit.xml>

The chat-egress-srt job runs the srt-marked tests through this: the marker's tests skip themselves, with a reason,
where node or srt is absent, and a gate that silently skips is not a gate. junit writes an expected failure
(`xfail`) as a `<skipped type="pytest.xfail">`; those are counted apart, as pytest counts them, and are not skips.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def main(path: str) -> int:
    root = ET.parse(path).getroot()
    skipped, xfailed, bad, total = [], 0, 0, 0
    for case in root.iter("testcase"):
        total += 1
        failed = case.find("failure") is not None or case.find("error") is not None
        sk = case.find("skipped")
        if failed:
            bad += 1
        elif sk is not None:
            if sk.get("type") == "pytest.xfail":
                xfailed += 1
            else:
                skipped.append(f"SKIPPED {case.get('classname')}::{case.get('name')}: {sk.get('message')}")
    passed = total - len(skipped) - xfailed - bad
    print(f"{passed} passed, {len(skipped)} skipped, {xfailed} xfailed, {bad} failed or errored, {total} collected")
    for line in skipped:
        print(line)
    return 0 if passed > 0 and not skipped and not bad else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__.split("\n\n")[0] + "\n\nUsage: python tests/ci/junit_no_skips.py <junit.xml>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
