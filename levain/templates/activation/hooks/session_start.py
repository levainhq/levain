#!/usr/bin/env python3
"""Levain activation — SessionStart hook.

Layer A (primacy) + Layer D (the start-catch half).

Wired to Claude Code's SessionStart event (startup | resume | clear | compact).
Injects, at primacy position:

  1. The starter posture string from activation/posture.md — the auto-opener
     that stands in for a hand-typed session opener.
  2. Operator-local date and time — models have no clock.
  3. On a genuinely fresh session (startup | clear) only: a wrap-check flag if
     the previous session left episodes unwrapped, AND the time-based
     prospective surface — open spores that have gone dormant or whose `next`
     date has arrived, surfaced once so nothing rots silently. `resume` and
     `compact` both carry ongoing work, not a fresh start — they get posture
     re-injection but skip the wrap-check and the due-spore surface. (`compact`
     is the most important re-injection case: compaction rebuilds the context
     window and the primacy posture goes with it.)

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



def _wrap_state_compat(hook_mod, timeout=None):
    """`(episodes, wrap_in_progress)` from whichever helper API this install has.

    ⚠ WHY THIS IS NOT JUST `hook.wrap_state(...)`. A pack may layer its own
    `activation/hooks/_levain_hook.py` over the base tree (packs own the whole
    `activation` subtree), so a 0.4.2 entry point can end up composed against a
    PRE-0.4.2 helper that has `episodes_since_wrap` and no `wrap_state`. The
    resulting AttributeError is swallowed by this file's structural fail-open
    catch, and every hook then emits NOTHING — silently, with doctor still
    green. That is the exact failure this release exists to end, so the entry
    points must not hard-depend on a helper symbol they cannot guarantee.

    Falls back to the old count-only API, which loses only the wrap-blocked
    branch — the pre-0.4.2 behaviour, which is the right degradation.
    """
    fn = getattr(hook_mod, "wrap_state", None)
    if fn is not None:
        return fn(timeout=timeout) if timeout is not None else fn()
    legacy = getattr(hook_mod, "episodes_since_wrap", None)
    if legacy is None:
        return None
    n = legacy(timeout=timeout) if timeout is not None else legacy()
    return None if n is None else (n, False)

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

        # 2b. Identity — the operator's CURRENT name for this entity, surfaced
        #     when it differs from the birth name baked into origin.md. A rename
        #     lands in .levain/config.json (Class-A, sovereign) and never rewrites
        #     the origin self-statement (Class C-view), so the entity — which reads
        #     only origin.md in its always-loaded context — would otherwise never
        #     learn it was renamed. Always-fire (identity is every-session context,
        #     like posture); self-silent when there is nothing to reconcile.
        identity = hook.entity_name_notice()
        if identity:
            sections.append(identity)

        # 2c. Live focus — the operator's declared "what I'm on now", surfaced at
        #     primacy so the partner orients to their frame. The cockpit/CLI SET the
        #     focus (.levain/context.json); this is the READ that makes the partner
        #     SEE it (was write-only into the render — a set focus never reached the
        #     session). Always-fire like posture (it's every-session orienting
        #     context); self-silent when no focus is set, and freshness-flagged so a
        #     stale one prompts a re-confirm rather than being trusted as current.
        focus = hook.focus_notice()
        if focus:
            sections.append(focus)

        # 3. Layer D — start-catch. Fires only on a genuinely fresh session;
        #    on `resume`/`compact` the unwrapped count reflects ongoing work.
        #    NOTE: the `source` vocabulary (startup / resume / clear / compact)
        #    is Claude Code's SessionStart payload — a harness-coupling point a
        #    non-Claude-Code adapter must re-verify (see _levain_hook docstring).
        if payload.get("source") in ("startup", "clear"):
            # wrap_state, not episodes_since_wrap: a wrap left open by the
            # session that just ended is EXACTLY the state a fresh session
            # opens into, and telling it to run prepare_wrap sends it at the
            # one call that cannot succeed (Alex De Groodt, 2026-08-04).
            state = _wrap_state_compat(hook)
            if state is not None:
                n, wrap_in_progress = state
                if wrap_in_progress and hasattr(hook, "format_wrap_blocked"):
                    sections.append(hook.format_wrap_blocked(fresh_session=True))
                elif n > 0:
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

            # Pack drift — a pulled pack SOURCE changed since this install was
            # composed. Same fail-silent nudge toward `levain update`, for the
            # DOWNSTREAM pack axis (the operator's doctrine, not the engine).
            pack = hook.pack_drift()
            if pack:
                sections.append(pack)

        if sections:
            hook.emit("\n\n".join(sections), "SessionStart")
    except Exception:
        # Structural fail-open: no error escapes a harness entry point.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
