"""Write levain/chat_egress/denied_table.json from `levain.chat_egress.guard.generate_table()` (rows R15-14-2,
R15-17-3). Run under the pinned Python (`guard.PINNED_PYTHON`); the output is a committed, reviewed artifact, so a
run that changes it is a diff to review, never a silent refresh.

usage: python scripts/gen_denied_table.py [--check]
  --check  exit 1, printing the problems, when the committed file is not the generated one; write nothing.
"""

from __future__ import annotations

import sys

from levain.chat_egress import guard


def main(argv: list[str]) -> int:
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    if running != guard.PINNED_PYTHON:
        print(f"refused: Python {running} is running, the table is pinned to {guard.PINNED_PYTHON}", file=sys.stderr)
        return 2
    text = guard.render_table(guard.generate_table())
    if "--check" in argv:
        current = guard.TABLE_PATH.read_text() if guard.TABLE_PATH.exists() else ""
        if current != text:
            print(f"{guard.TABLE_PATH} differs from the generated table", file=sys.stderr)
            return 1
        return 0
    guard.TABLE_PATH.write_text(text)
    print(f"wrote {guard.TABLE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
