#!/usr/bin/env python3
"""Levain activation — UserPromptSubmit hook.

Layer B (recency) + the event-based prospective surface + Layer D (the
ambient-nudge half).

Wired to Claude Code's UserPromptSubmit event. Injects, at recency position
(immediately before each user prompt is processed):

  1. One directive selected at random from activation/recency_directives.md —
     the within-session-durability layer. Selecting across differently-framed
     directives fights attention habituation.
  2. Open spores (prospective layer) whose content COLLIDES with this prompt —
     the event-based germination surface: an open loop surfaces when the current
     work touches it. Precision-biased and capped, so an unrelated prompt
     injects nothing (the effortful full-list poll stays opt-in, per the seed).
  3. When episodes recorded since the last wrap reach WRAP_NUDGE_THRESHOLD, a
     one-line wrap nudge — the ambient half of Layer D.

The three are independent: a broken directives file drops (1) but must not also
drop (2)/(3), so `sections` is built additively.

FAIL-OPEN — structural: main()'s entire body is wrapped in a catch-all and the
process always exits 0. A hook must never crash or write stderr noise into the
operator's session. The _levain_hook import is guarded too, since it runs
before main() and a catch-all inside main() cannot cover it.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _levain_hook as hook
except Exception:
    # Shared helpers unavailable (missing / edited to a syntax error /
    # unreadable) — stay silent rather than crash the operator's session.
    sys.exit(0)

# Episodes-since-wrap count at which the ambient nudge starts firing.
# memory.md targets 5-15 episodes per session; 12 fires deep into a
# substantial session, before the upper bound, without nagging a short one.
WRAP_NUDGE_THRESHOLD = 12

# Per-attempt timeout for the anneal-memory queries (wrap-count + open spores).
# Tight because this hook runs before EVERY prompt — a slow or hung
# anneal-memory must not stall the turn. (session_start.py uses the 5s default:
# it fires once per session and is not latency-sensitive.)
WRAP_CHECK_TIMEOUT = 2.0
SPORE_CHECK_TIMEOUT = 2.0
CRYSTAL_CHECK_TIMEOUT = 2.0


def main() -> int:
    try:
        payload = hook.read_stdin()

        if not hook.should_fire():
            return 0

        sections: list[str] = []

        # Layer B — one recency directive, selected at random.
        directives = hook.read_blocks(
            hook.install_root() / "activation" / "recency_directives.md"
        )
        # random.choice() raises IndexError on [] — only call it when the
        # directives file parsed to at least one block. A broken directives
        # file drops Layer B but must NOT drop the sections below, so
        # `sections` is additive (cf. session_start.py).
        if directives:
            sections.append(random.choice(directives))

        # Event-based spore germination — open loops whose content collides with
        # this prompt surface on their own (the intelligent prospective surface;
        # the effortful full-list poll stays opt-in, per the seed). Precision-
        # biased + capped, so an irrelevant prompt injects nothing.
        prompt = payload.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            hits = hook.spores_colliding(
                prompt, hook.open_spores(timeout=SPORE_CHECK_TIMEOUT)
            )
            if hits:
                sections.append(hook.format_spore_collisions(hits))

        # Crystallized-pattern recall — the on-demand graduated-wisdom tier.
        # A Proven pattern crystallized OUT of the always-loaded working set is
        # recalled here the moment this prompt touches its domain (the read-side
        # twin of wrap-time crystallization routing). Independent of the spore
        # surface above; fail-silent on an empty/absent crystal store. Same tight
        # per-prompt timeout.
        if isinstance(prompt, str) and prompt.strip():
            patterns = hook.crystal_recall(prompt, timeout=CRYSTAL_CHECK_TIMEOUT)
            if patterns:
                sections.append(hook.format_crystal_recall(patterns))

        # Layer D — ambient nudge. Independent of the sections above.
        n = hook.episodes_since_wrap(timeout=WRAP_CHECK_TIMEOUT)
        if n is not None and n >= WRAP_NUDGE_THRESHOLD:
            sections.append(
                f"[wrap nudge] {n} episodes recorded since the last wrap. At "
                f"the next natural pause, run the wrap sequence (prepare_wrap "
                f"-> compress -> save_continuity) — that is where the "
                f"partnership compounds across sessions."
            )

        if sections:
            hook.emit("\n\n".join(sections), "UserPromptSubmit")
    except Exception:
        # Structural fail-open: no error escapes a harness entry point.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
