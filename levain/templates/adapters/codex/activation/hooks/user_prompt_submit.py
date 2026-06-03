#!/usr/bin/env python3
"""Levain activation — UserPromptSubmit hook (Codex adapter).

Layer B (recency) + the event-based prospective surface + Layer D (the
ambient-nudge half).

Wired to Codex's UserPromptSubmit event. Injects, at recency position
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

Prompt-key: the collision surface reads the user's text from
`payload["prompt"]`. VERIFIED against codex-cli 0.133.0 source —
`UserPromptSubmitCommandInput` serializes `prompt: request.prompt.clone()`
(codex-rs/hooks/src/events/user_prompt_submit.rs) — so the key matches Claude
Code's UserPromptSubmit shape. If a future Codex re-keys it, the surface
degrades to silence (the `isinstance(prompt, str)` guard no-ops) — fail-open,
never a crash. SEPARATE, still-open item: Codex silent-skips lifecycle hooks
under `codex exec` through 0.133, so whether the hook FIRES at all under
*interactive* `codex` (distinct from the payload shape, which is confirmed) is
the Bucket 3 mechanism-canary verification.

Codex note: on the first prompt of a fresh session, Codex may fire
UserPromptSubmit alongside SessionStart (both inject context simultaneously
into the same model turn). The SessionStart content is operational state
(posture + temporal + start-catch); this content is partnership posture. No
content overlap by design.

Wrap discipline note: this adapter does NOT wire a Stop hook (Codex Stop
output schema does not accept hookSpecificOutput — see _levain_hook.py module
docstring). Layer D wrap-discipline therefore folds entirely into:
  (a) start-catch in session_start.py on `startup` / `clear` source values
  (b) the ambient nudge below, fired when episodes-since-wrap crosses
      WRAP_NUDGE_THRESHOLD mid-session
Same shape as the Claude Code adapter for the same architectural reason.

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
        # biased + capped, so an irrelevant prompt injects nothing. Prompt-key
        # ("prompt") verified vs codex-cli 0.133.0 source (see module docstring);
        # fail-open if a future Codex re-keys it.
        prompt = payload.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            hits = hook.spores_colliding(
                prompt, hook.open_spores(timeout=SPORE_CHECK_TIMEOUT)
            )
            if hits:
                sections.append(hook.format_spore_collisions(hits))

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
