"""levain.firing.drive — the DRIVE MODE: the single authority on *how* a session is being driven.

**Why this exists as its own concept (K4a, 2026-07-29).** Levain used to answer one question about
a session — ``human_present: bool`` — and derive everything from it. That bool turned out to be an
UNDER-MODELED AXIS: it has three states, not two, and collapsing two of them was correct for the
efferent gate and wrong for the crown-jewels floor.

===============  ==================  ==========================  ========  ===================
mode             who invoked it      who reads the activity      gate      standard creds
===============  ==================  ==========================  ========  ===================
``interactive``  a human, live       a human, AS IT HAPPENS      ungated   per config (allow)
``headless``     a human             a human, AFTERWARDS         gated     per config (allow)
``unattended``   a SCHEDULER         nobody, necessarily         gated     DENIED by default
===============  ==================  ==========================  ========  ===================

**The gate is right to collapse the last two** — its job is FAN-IN, and neither a ``--task`` run nor
a scheduled seat has a human to fan an action in to at the moment it would fire. So
:func:`human_present` is a pure derivation and :func:`levain.firing.gate.resolve_gate_mode` keeps its
existing ``human_present: bool`` signature untouched. **K3's security boundary does not change here.**

**The floor is NOT right to collapse them, and the reason is specific rather than a vibe.** Note
first that the crown-jewels floor is otherwise presence-INDEPENDENT: ``~/.anneal-memory`` is denied
at the REPL too, with a human sitting right there, because the floor is *irreversibility
containment* and not fan-in. So credentials need an actual argument to be drive-dependent, and it is
this:

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
   interactive turns, where the gate is UNGATED and bash is available.* A silent unattended read can
   surface a live credential into a session that CAN exfiltrate it.

So the discriminator is not "is somebody watching". It is **whether a read can compound into durable
identity with no human anywhere in the loop** — always true for a scheduled seat, and never silently
true at the REPL, where the operator sees the ``view``, sees the echo, and can wipe the store before
a wrap ever runs.

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

import os
from typing import Literal

__all__ = [
    "DRIVE_MODES",
    "DriveModeConflict",
    "LEVAIN_DRIVE_MODE_ENV",
    "DriveMode",
    "bind_drive_mode",
    "current_drive_mode",
    "human_present",
    "resolve_cred_floor",
]

LEVAIN_DRIVE_MODE_ENV = "LEVAIN_DRIVE_MODE"
"""The fork-safe channel carrying the drive mode to policy built OUTSIDE the session object.

The crown-jewels floor has **one policy and two enforcers**, and each builds its own from
``$LEVAIN_ENTITY_DIR`` + ``confinement.json`` at TOOL-CREATION time (not per call — the executors
cache the policy they were constructed with; the earlier "rebuilt per call" wording here was simply
wrong, codex+glm L3). Neither can close over session state, because ``create()`` is a classmethod
reached through a process-global tool registry — there is no per-session channel to close over. Without a fork-safe channel for the mode, those two enforcers
resolve the SAME field differently: bash would deny the standard cred stores on an unattended seat
while the file editor allowed them — and the file editor is the ``view`` path, i.e. exactly the
afferent read this floor exists to stop. Same idiom as ``$LEVAIN_ENTITY_DIR``: bound once at session
start, re-read per op, never frozen into a closure."""

DriveMode = Literal["interactive", "headless", "unattended"]

DRIVE_MODES: tuple[DriveMode, ...] = ("interactive", "headless", "unattended")
"""Every valid drive mode, weakest-supervision-LAST. The order is meaningful: each rung supervises
strictly less than the one before it, so a policy that tightens as supervision falls away can be
written as a comparison rather than a table of special cases."""


def human_present(mode: DriveMode | str) -> bool:
    """Is a human DRIVING this session — i.e. watching the activity as it happens?

    The gate's question, and the reason it is a derivation rather than a stored flag: fan-in either
    exists at the moment an action would fire, or it does not. Only ``interactive`` has it.

    An unrecognized mode resolves to ``False`` (no human), which is the FAIL-SAFE direction: an
    unknown drive gets the gate armed rather than waved through. This mirrors
    :func:`levain.firing.gate.resolve_gate_mode`, which treats an unknown setting as ``auto``.
    """
    return mode == "interactive"


def resolve_cred_floor(setting: bool | None, *, mode: DriveMode | str) -> bool:
    """Should the STANDARD credential stores (``~/.config/gh`` · ``~/.aws/credentials`` ·
    ``~/.netrc``) be folded into the crown-jewels floor for this session?

    ``setting`` is the entity's ``deny_standard_creds`` declaration from ``confinement.json``:

    - ``True``  → deny, always. An explicit operator pin; drive mode does not soften it.
    - ``False`` → allow, always. **An explicit operator opt-IN, and it must keep working even for
      an unattended seat** — a seat whose job is "open a PR nightly" genuinely needs ``gh``. This is
      a DEFAULT being overridden, not a prohibition being defeated.
    - ``None`` (the key is ABSENT) → derive from the drive: deny for ``unattended``, allow
      otherwise.

    **Why absent-means-derive rather than a three-valued string** like the ``efferent_gate: "auto"``
    field one row away in the same dataclass, which solves the identical problem: changing
    ``deny_standard_creds`` from a bool to a string would make every existing ``true``/``false``
    config INVALID, and the loader is deliberately fail-closed — so a parse error does not degrade,
    it bricks ``levain run`` entirely. Absent-means-auto buys the same semantics for zero breakage,
    and it strengthens rather than breaks the meaning of an explicit ``false``: it stops being "the
    default, restated" and becomes "I really do want this credential reachable unattended."
    """
    if setting is not None:
        return setting
    # An UNRECOGNIZED mode denies, exactly as `current_drive_mode` maps garbage to "unattended".
    # Both halves of the authority must fail the same way: if only the env reader failed closed,
    # a direct library caller passing a typo'd mode would get "allowed" from this function and
    # "denied" from the tool policy — one authority reporting two answers. (codex L3 LOW.)
    return mode not in ("interactive", "headless")


class DriveModeConflict(RuntimeError):
    """A second session tried to rebind the drive mode in a way that would WIDEN the floor."""


def bind_drive_mode(mode: DriveMode) -> None:
    """Publish ``mode`` on the fork-safe channel, **refusing any rebind that would WIDEN the floor**.

    Called by :meth:`levain.session.EntitySession.open`, beside the ``$LEVAIN_ENTITY_DIR`` binding
    it mirrors — and it mirrors that binding's REFUSAL too, for the same reason
    (``bind_entity``: *"a silent rebind would swap the first agent's store on its next turn"*).

    **The race this closes (codex L3 HIGH ×2).** This channel is PROCESS-GLOBAL, and the two floor
    enforcers read it at DIFFERENT moments: the bash seatbelt and the file editor each call
    ``policy_for_conv_state`` from their own ``create()``, both AFTER the session resolved its own
    value. In any multi-session process a second session could flip the env between those points,
    so an unattended session's tools would be built with an interactive floor — while its banner,
    resolved earlier, still claimed the credentials were denied. Worse, a second session that
    later FAILS to start still poisons the env, because the bind happens before those failures.

    Refusing the widening direction makes the race harmless rather than merely unlikely: a rebind
    may TIGHTEN the floor (nothing is exposed by denying more) and may never LOOSEN it, so whatever
    interleaving occurs, no session's credentials become readable because of another session's
    mode. It is expressed as a comparison over the resolved FLOOR rather than over the mode names,
    because the floor is the thing that must not weaken.

    **This is a guard, not the architecture.** The real fix is codex's own diagnosis — the mode
    should travel in the CONVERSATION state being resolved, not in mutable ambient process state —
    and that is a hard prerequisite for K1 part 2, whose server holds many sessions in one process.
    Until then this refusal converts a silent widening into a loud failure, which is the same trade
    ``bind_entity`` makes ("one process hosts one entity — start a new process").
    """
    current = os.environ.get(LEVAIN_DRIVE_MODE_ENV, "").strip()
    if current in DRIVE_MODES and current != mode:
        if resolve_cred_floor(None, mode=current) and not resolve_cred_floor(None, mode=mode):
            raise DriveModeConflict(
                f"this process is already bound to drive mode {current!r}, whose credential floor "
                f"is STRICTER than {mode!r}; refusing to rebind and widen it. One process hosts "
                f"one drive mode — start a new process."
            )
    os.environ[LEVAIN_DRIVE_MODE_ENV] = str(mode)


def current_drive_mode() -> DriveMode:
    """Read the bound drive mode, **failing CLOSED to ``"unattended"``**.

    The asymmetry is the whole argument, and it runs opposite to the usual "don't surprise the
    operator" instinct:

    - Wrongly resolving to ``unattended`` DENIES credentials to a session that should have had
      them. The operator sees a refusal immediately, in the banner and at the point of use, and
      fixes it in one line of ``confinement.json``.
    - Wrongly resolving to anything else GRANTS credentials to a session that should not have had
      them — silently, unattended, with the read compounding into always-loaded memory and nobody
      watching. There is no error to see.

    The only way this is unset in a real run is a WIRING failure, and a wiring failure must never
    widen the floor. This mirrors :func:`human_present`, which also resolves an unrecognized mode
    to the governed side, and ``arm_efferent_gate``'s read-back-and-refuse discipline: never let a
    policy that failed to wire present itself as a policy that did.
    """
    raw = os.environ.get(LEVAIN_DRIVE_MODE_ENV, "").strip()
    return raw if raw in DRIVE_MODES else "unattended"  # type: ignore[return-value]
