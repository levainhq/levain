#!/usr/bin/env python3
"""Levain activation — SessionStart hook.

Layer A (primacy) + Layer D (the start-catch half).

Wired to Claude Code's SessionStart event (startup | resume | clear | compact).
Injects, at primacy position:

  1. The starter posture string from activation/posture.md — the auto-opener
     that stands in for a hand-typed session opener.
  2. Operator-local date and time — models have no clock.
  3. On a genuinely fresh session (startup | clear) only: a wrap-check flag if
     the previous session left episodes unwrapped. `resume` and `compact` both
     carry unwrapped episodes that are ongoing work, not a missed wrap — they
     get posture re-injection but skip the wrap-check. (`compact` is the most
     important re-injection case: compaction rebuilds the context window and
     the primacy posture goes with it.)

FAIL-OPEN — structural: main()'s entire body is wrapped in a catch-all and the
process always exits 0. A hook must never crash or write stderr noise into the
operator's session. The _levain_hook import is guarded too, since it runs
before main() and a catch-all inside main() cannot cover it.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _levain_hook as hook
except Exception:
    # Shared helpers unavailable (missing / edited to a syntax error /
    # unreadable) — stay silent rather than crash the operator's session.
    sys.exit(0)


def main() -> int:
    try:
        payload = hook.read_stdin()

        if not hook.should_fire():
            return 0

        sections: list[str] = []

        # 1. Posture (Layer A) — the first `## ` block of posture.md.
        posture_blocks = hook.read_blocks(
            hook.install_root() / "activation" / "posture.md"
        )
        if posture_blocks:
            sections.append(posture_blocks[0])

        # 2. Temporal orientation.
        sections.append(f"[session orientation] {hook.temporal()}")

        # 3. Layer D — start-catch. Fires only on a genuinely fresh session;
        #    on `resume`/`compact` the unwrapped count reflects ongoing work.
        #    NOTE: the `source` vocabulary (startup / resume / clear / compact)
        #    is Claude Code's SessionStart payload — a harness-coupling point a
        #    non-Claude-Code adapter must re-verify (see _levain_hook docstring).
        if payload.get("source") in ("startup", "clear"):
            n = hook.episodes_since_wrap()
            if n is not None and n > 0:
                sections.append(
                    f"[wrap check] {n} episode(s) recorded since the last "
                    f"wrap. If your last session did real work, run the wrap "
                    f"sequence (prepare_wrap -> compress -> save_continuity) "
                    f"to consolidate it — unwrapped episodes never compound "
                    f"into continuity. They are not lost: prepare_wrap still "
                    f"sees them."
                )

        if sections:
            hook.emit("\n\n".join(sections), "SessionStart")
    except Exception:
        # Structural fail-open: no error escapes a harness entry point.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
