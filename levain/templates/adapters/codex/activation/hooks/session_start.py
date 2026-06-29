#!/usr/bin/env python3
"""Levain activation — SessionStart hook (Codex adapter).

Layer A (primacy) + Layer D (the start-catch half).

Wired to Codex's SessionStart event (matcher = startup|resume|clear|compact
per hooks.json). Injects, at primacy position:

  1. The starter posture string from activation/posture.md — the auto-opener
     that stands in for a hand-typed session opener.
  2. Operator-local date and time — models have no clock.
  3. On a genuinely fresh session (source ∈ {"startup", "clear"}) only: a
     wrap-check flag if the previous session left episodes unwrapped, AND the
     time-based prospective surface — open spores that have gone dormant or
     whose `next` date has arrived, surfaced once so nothing rots silently.
     `resume` and `compact` carry ongoing work, not a fresh start — they get
     posture re-injection but skip the wrap-check and the due-spore surface.

Codex SessionStart `source` vocabulary per Codex 0.133 schema: `startup`,
`resume`, `clear`, `compact`. (`clear` and `compact` mirror Claude Code's
parallel source values; empirical observation on Codex 0.132 saw only
`startup` and `resume` — the matcher now covers all four for forward
compatibility.)

FAIL-OPEN — structural: main()'s entire body is wrapped in a catch-all and
the process always exits 0. A hook must never crash or write stderr noise
into the operator's session. The _levain_hook import is guarded too, since
it runs before main() and a catch-all inside main() cannot cover it.
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
        #    on `resume`/`compact` the unwrapped count reflects ongoing work,
        #    not a missed wrap. (`compact` is the most important re-injection
        #    case: compaction rebuilds the context window and the primacy
        #    posture goes with it — but the unwrapped episodes belong to the
        #    same logical session.)
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

            # Time-based spore germination — open loops that have gone dormant
            # or whose `next` date has arrived, surfaced once on a fresh session
            # so nothing rots silently. Growing/resting/parked stay out of the
            # way. Fires only on a fresh session (with the wrap-check), not on
            # resume/compact mid-flow.
            due = hook.due_dormant_spores(hook.open_spores())
            if due:
                sections.append(hook.format_due_spores(due))

            # Compatibility drift — a once-per-fresh-session nudge if the version
            # SET fell out of sync (anneal changed underneath the install, or
            # unreviewed migration proposals exist). Cheap + fail-silent; the
            # authoritative multi-axis verify is `levain doctor`.
            drift = hook.compat_drift()
            if drift:
                sections.append(drift)

        if sections:
            hook.emit("\n\n".join(sections), "SessionStart")
    except Exception:
        # Structural fail-open: no error escapes a harness entry point.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
