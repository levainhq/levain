"""levain.firing.drive — the DRIVE MODE: the single authority on *how* a session is being driven.

**Why this exists as its own concept (K4a, 2026-07-29).** Levain used to answer one question about
a session — ``human_present: bool`` — and derive everything from it. That bool turned out to be an
UNDER-MODELED AXIS: it has three states, not two. The efferent gate collapses ``headless`` with
``unattended``; the crown-jewels cred floor no longer reads the drive at all (2026-10-12, see below).
The third state stays as the record that a scheduler, not a person, invoked
the run; no policy in this module splits on it today, and the autonomy policies still to come will.
(The ``--unattended`` flag's metabolize-never-crystallize rule keys on the flag itself, not on this mode.)

===============  ==================  ==========================  ========  ===================
mode             who invoked it      who reads the activity      gate      standard creds
===============  ==================  ==========================  ========  ===================
``interactive``  a human, live       a human, after each turn    ungated   DENIED by default
``headless``     a human             a human, AFTERWARDS         gated     DENIED by default
``unattended``   a SCHEDULER         nobody, necessarily         gated     DENIED by default
===============  ==================  ==========================  ========  ===================

**The gate collapses the last two** — its job is FAN-IN, and neither a ``--task`` run nor
a scheduled seat has a human to fan an action in to at the moment it would fire. So
:func:`human_present` is a pure derivation and :func:`levain.firing.gate.resolve_gate_mode` keeps its
existing ``human_present: bool`` signature untouched. **K3's security boundary does not change here.**

**Why the floor no longer follows the gate's line.** Until 2026-10-07 the floor kept ``headless`` open
and denied only ``unattended``; that default was ruled flipped (cockpit chat is ``headless``, and its
every turn is captured), which left the interactive REPL as the one drive with the standard credential
stores open by default. On 2026-10-12 (N7) he ruled that one closed too: the crown-jewels floor is
presence-INDEPENDENT, as ``~/.anneal-memory`` already was, because it is *irreversibility containment*
and not fan-in. The reasons the credential stores belong in it:

1. **The gate does not cover the READ.** ``view`` is afferent (``AFFERENT_FILE_EDITOR_COMMANDS``),
   so a fully GATED seat can read ``~/.config/gh/hosts.yml`` with nothing stopping it. Bash being
   always-efferent covers *network* exfil — it does not cover acquiring the secret.
2. **Persistence is REAL BUT CONDITIONAL, and overstating it would be its own defect.**
   ``render_turn`` deliberately excludes tool observations ("a deliberate signal/noise choice for
   episodic memory"), so file CONTENTS are not captured. Only assistant TEXT is. A credential
   therefore persists only if the model ECHOES it into its reply — which models routinely do when
   asked to debug authentication.
3. **That is where it turns bad.** A captured secret is exactly what a self-consolidate metabolizes
   into the NEOCORTEX, which is always-loaded and re-injected into every future turn — *including
   interactive turns, where the gate is UNGATED and bash is available.* A credential read in any
   session nobody reviews before a wrap can surface into a session that CAN exfiltrate it.
4. **Presence does not stop a READ.** The REPL is not live supervision: it runs a turn to completion
   (capture included) and only then shows its activity, so an operator at the terminal sees a prompt
   injection's ``cat ~/.netrc`` after the bytes are already in the model's context.

So the default is: **closed in every drive, the interactive REPL included.** An entity that needs
``gh`` or ``aws`` (in the REPL or anywhere else) opts in with ``"deny_standard_creds": false``. The
``mode`` argument :func:`resolve_cred_floor` takes no longer changes its answer; it stays so a later
drive-dependent default has its input, and so no caller moves.

**The gap this does NOT close, named so a later slice can.** ``ssh_mode="agent"`` already solved this
class properly — *use-not-steal*: the entity authenticates through the agent socket and may never
read raw key material. ``gh`` / ``aws`` / ``netrc`` have **no agent equivalent**; their tools read
plain files. That — not a considered safety-versus-utility tradeoff — is why the cred floor is a
crude all-or-nothing binary. The fine-grained successor exists in principle (``gh`` honours
``GH_TOKEN``; ``aws`` has ``credential_process``), and both would inject a SCOPED, short-lived
credential instead of exposing the operator's long-lived one.

Pure stdlib, no levain imports — a dependency-isolated leaf like :mod:`levain.firing.gate` and
:mod:`levain.firing.isolation`, so both the gate and the confinement floor can depend on it without
either depending on the other.
"""
from __future__ import annotations

from typing import Literal

__all__ = [
    "DRIVE_MODES",
    "DriveMode",
    "human_present",
    "resolve_cred_floor",
]

# There is no process-level drive-mode channel. ``$LEVAIN_DRIVE_MODE`` and its widen-refusal guard were
# retired on 2026-10-02 ("b on levain") once spore-438 moved the mode onto each conversation
# (:class:`levain.firing.binding.ConversationBinding`) and no reader of the env was left anywhere —
# in levain, flow, anneal-memory, the hub, or any child process (the confined shell and the daemon
# start from env dicts built from scratch). A process-global mode is the race spore-438 reproduced.

DriveMode = Literal["interactive", "headless", "unattended"]

DRIVE_MODES: tuple[DriveMode, ...] = ("interactive", "headless", "unattended")
"""Every valid drive mode, weakest-supervision-LAST. The order is meaningful: each rung supervises
strictly less than the one before it, so a policy that tightens as supervision falls away can be
written as a comparison rather than a table of special cases."""


def human_present(mode: DriveMode | str) -> bool:
    """Is a human DRIVING this session — at the terminal, reading each turn before typing the next?

    The gate's question, and the reason it is a derivation rather than a stored flag: fan-in either
    exists at the moment an action would fire, or it does not. Only ``interactive`` has it.

    An unrecognized mode resolves to ``False`` (no human), which is the FAIL-SAFE direction: an
    unknown drive gets the gate armed rather than waved through. This mirrors
    :func:`levain.firing.gate.resolve_gate_mode`, which treats an unknown setting as ``auto``.
    """
    return mode == "interactive"


def resolve_cred_floor(setting: bool | None, *, mode: DriveMode | str) -> bool:
    """Should the STANDARD credential stores (the paths
    :func:`levain.firing.confinement.cred_floor_label` names) be folded into the crown-jewels floor for this session?

    ``setting`` is the entity's ``deny_standard_creds`` declaration from ``confinement.json``:

    - ``True``  → deny, always. An explicit operator pin; drive mode does not soften it.
    - ``False`` → allow, always. **An explicit operator opt-IN, and it must keep working even for
      an unattended seat** — a seat whose job is "open a PR nightly" genuinely needs ``gh``. This is
      a DEFAULT being overridden, not a prohibition being defeated.
    - ``None`` (the key is ABSENT) → deny, in every drive, the interactive REPL included (N7,
      2026-10-12). ``False`` is the opt-in, and it is the only way the REPL reads these stores.

    **Why the key stays a tri-state bool** (absent distinct from ``false``) although absent now
    answers the same in every drive: the absent value is the product default, which has moved twice
    (2026-10-07, 2026-10-12) without touching any entity's file, and an explicit ``false`` stays the
    operator's own "I really do want this credential reachable", never a restated default.
    """
    if setting is not None:
        return setting
    # Every mode denies, an unrecognized one included (a conversation binding refuses an unknown mode
    # outright, `ConversationBinding.create`, so the two halves cannot disagree on a typo'd mode).
    return True
