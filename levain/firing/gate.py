"""levain.firing.gate — the EFFERENT GATE's pure classifier (keystone **K3**, ``spore-295``).

The governor that makes a sovereign entity *governed* rather than YOLO, stated as one invariant:

    **Every EFFERENT action fans in to the human. AFFERENT runs ungated. NOTHING graduates.**

Perception is free; action on the world is not. That is the whole of K3. There are deliberately
no stakes escalators, no per-domain curve, no capability recalibration and no graduation-to-
unattended tier — that machinery is ``spore-417`` and it is **v2.x, off the release spine**. A
gate that always says *"ask the human"* is not a research project, and gating HARDER than the
ungoverned always-on runtimes is the wedge, not a gap.

**This module is a dependency-isolated stdlib leaf** — same discipline as
:mod:`levain.firing.confinement`. It knows nothing about the OpenHands SDK, so the classification
rule can be exercised exhaustively without standing up a conversation. The SDK adapter that
mounts it on the runtime's confirmation seam is :mod:`levain.firing.openhands.gate`.

Three properties are load-bearing, and each exists because its absence is a known failure:

**1. The classification is STRUCTURAL — by tool identity, never by asking the model.**
The SDK's stock path can carry a ``security_risk`` field *predicted by the LLM itself*
(``ActionEvent.tool_call``), and ships an ``LLMSecurityAnalyzer`` that leans on it. Levain does
not use it, for exactly the reason ``levain.session``'s exit codes do not: **the harness reports
what it SAW, never what the agent CLAIMED.** A gate an entity can talk its way through is not a
gate — it is the false-all-clear class the trust pitch says cannot happen, relocated one layer
down from the exit code.

**2. UNRECOGNIZED IS EFFERENT — fail-closed, and it says so out loud.** A tool this module does
not know (a future hand, an MCP tool, a stock tool resolved by some other path) is gated, and
:attr:`Classification.recognized` is ``False`` so the operator-facing line reads *"unrecognized
tool — gated fail-closed"* rather than a silent, confident-looking allow. An unknown surface
reading as safe is ``absence_of_signal_rendered_as_health``, and it is the single most likely way
this gate would rot: someone adds a hand and the gate quietly keeps returning green.

**3. BASH IS ALWAYS EFFERENT, and never by parsing the command.** ``ls`` is afferent and
``rm -rf`` is not, but you cannot tell them apart by reading the string — quoting, ``$(...)``,
``eval``, ``;``, aliases and backgrounding make command classification an arms race the
classifier loses. So the shell is classified at the TOOL, where the answer is knowable. The cost
is real and accepted: an unattended seat gates on ``ls`` too. The escape from that cost is a
read-only bash LANE (a second, write-denied confinement profile — structural, still never a
parser), which belongs with the seat that needs it, not here.

**What the gate VOUCHES FOR, and what it refuses to render** (the decision-surface rule, added
2026-07-29 after an afferent layer elsewhere in the stack emitted a structurally false confidence
number that nothing in the architecture could have caught). What the gate shows at the moment of
fan-in IS the gate's product — a human is about to act on it — so every element of it is a
HARNESS OBSERVATION: the tool the runtime recorded, the arguments it recorded, and the structural
rule that classified it. Three things are deliberately absent, and their absence is the design:

  - **The model's own account of why it wants to act.** ``ActionEvent`` carries a ``thought``;
    the report does not show it. A self-report and an observation rendered on one surface in one
    typeface leaves the operator no way to tell which is which — and a model arguing for its own
    approval is the one voice that must not be next to the approve button.
  - **Any score, count or aggregate.** ``UNGATED ≠ UNVERIFIED`` is a real hole, but it is an
    AFFERENT hole: the gate governs the efferent edge and cannot vouch for the quality of what
    reached the entity's reasoning. So it renders no confidence number at all, rather than one
    it cannot stand behind. Nothing in K3 aggregates, so there is no count here that restatement
    could inflate into apparent weight.
  - **A verdict on whether the action is a good idea.** The gate says *this changes the world*
    and hands over. It does not say *this is safe* — see ``ActionClass``.

Where the gate genuinely cannot vouch for its own answer it SAYS SO in the same surface, which is
what :attr:`Classification.recognized` is for. That is the whole of the provenance K3 owns:
*this classification is structural and I stand behind it* versus *I do not know this tool.*
Anything richer — provenance for the evidence an entity reasoned FROM — is an afferent
verification layer, is not this keystone, and would be exactly the kind of scope the ``295``/``417``
split exists to keep off the spine.

Tool-name note, and it is a genuine tripwire: the confined hands keep the **stock** ``.name``
(``file_editor`` / ``terminal``) while their registry SPEC names are ``levain_file_editor`` /
``levain_bash`` — deliberately, so a weak open model sees the familiar function name. An
``ActionEvent`` carries ``tool_call.name``, i.e. the STOCK name. Matching only the registry keys
would therefore never fire, and every action would fall through to the fail-closed default: the
gate would over-gate (``view`` included), still look correct in any test that only asserts "bash
gates", and be wrong. Both spellings are accepted here, and a test pins the stock ones.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

__all__ = [
    "AFFERENT_FILE_EDITOR_COMMANDS",
    "BASH_TOOL_NAMES",
    "FILE_EDITOR_TOOL_NAMES",
    "GATE_SETTINGS",
    "INERT_TOOL_NAMES",
    "ActionClass",
    "Classification",
    "GateMode",
    "GateSetting",
    "PendingEfferent",
    "classify_action",
    "resolve_gate_mode",
]


class ActionClass(str, Enum):
    """What the harness observed an action to BE — not how risky it thinks the action is.

    ``SecurityRisk`` (the SDK's vocabulary) answers *"how dangerous?"*; this answers *"does it
    change the world?"*. They are different questions and conflating them is how a gate acquires
    opinions it cannot defend — ``git push`` is efferent and routine, reading a 4 GB file is
    afferent and expensive. K3 only ever needs the second question.
    """

    INERT = "inert"
    """Neither perception nor action: the agent finishing its turn, or thinking. Never gated."""

    AFFERENT = "afferent"
    """Perception — reading the world without changing it. Runs ungated, by definition."""

    EFFERENT = "efferent"
    """Action on the world. Fans in to the human. This is the whole point."""


GateSetting = Literal["auto", "gated", "ungated"]
"""What the operator may DECLARE in ``.levain/confinement.json`` (``efferent_gate``).

``auto`` (the default) derives the mode from whether a human is driving — see
:func:`resolve_gate_mode`."""

GateMode = Literal["gated", "ungated"]
"""What the session RESOLVED to. Always one of two concrete states; ``auto`` never survives."""

GATE_SETTINGS: tuple[GateSetting, ...] = ("auto", "gated", "ungated")
"""The accepted ``efferent_gate`` values, shared with the config loader so the two cannot drift."""

INERT_TOOL_NAMES = frozenset({"finish", "think"})
"""Turn-control tools: the agent declaring it is done, or reasoning aloud. Neither touches the
world. The SDK independently exempts a lone ``FinishAction``/``ThinkAction`` from confirmation;
naming them here too means this classifier is correct STANDALONE rather than correct only in
combination with a behaviour of the runtime we do not own."""

FILE_EDITOR_TOOL_NAMES = frozenset({"file_editor", "levain_file_editor"})
"""The file-editor hand, by BOTH spellings — the stock ``.name`` an ``ActionEvent`` actually
carries, and the Levain registry spec name. See the tool-name tripwire in the module docstring."""

BASH_TOOL_NAMES = frozenset({"terminal", "levain_bash"})
"""The bash hand, by both spellings. Bash is efferent by DECISION (see the module docstring), not
by falling off the end of the lookup — the distinction is invisible in the returned class and
very visible in :attr:`Classification.reason`, which is exactly where an operator needs it."""

AFFERENT_FILE_EDITOR_COMMANDS = frozenset({"view"})
"""The file-editor commands that only READ. The full vocabulary is ``view`` / ``create`` /
``str_replace`` / ``insert`` / ``undo_edit``; every one except ``view`` writes — including
``undo_edit``, which is a write that happens to restore. Expressed as an ALLOW-list on purpose:
a new command added upstream must default to gated, not slip in as afferent."""


@dataclass(frozen=True)
class Classification:
    """One action's class, plus the operator-facing REASON it got that class.

    The reason is not decoration. ``efferent`` reached by *"bash always fans in"* and
    ``efferent`` reached by *"unrecognized tool"* are the same gate decision and completely
    different news: the first is the design working, the second means the tool surface moved
    under the gate and nobody told it. Collapsing them into a bare enum is how the second stops
    being noticeable.
    """

    action_class: ActionClass
    reason: str
    recognized: bool
    """``False`` when the tool fell through to the fail-closed default. A monitor watching for
    gate rot should watch THIS, not the class."""

    @property
    def is_efferent(self) -> bool:
        return self.action_class is ActionClass.EFFERENT


def classify_action(
    tool_name: str | None, fields: Mapping[str, Any] | None = None
) -> Classification:
    """Classify one tool call, structurally.

    ``tool_name`` is the name the runtime reports for the call (the STOCK tool name — see the
    module docstring's tripwire). ``fields`` is the action's argument mapping; only the file
    editor's ``command`` is consulted, and only against an allow-list.

    Never raises, and never consults anything the model asserted about itself. A missing,
    empty, or non-string ``tool_name`` is unrecognized, and therefore efferent.
    """
    name = tool_name.strip() if isinstance(tool_name, str) else ""

    if name in INERT_TOOL_NAMES:
        return Classification(
            ActionClass.INERT, f"{name!r} is turn control, not world contact", recognized=True
        )

    if name in FILE_EDITOR_TOOL_NAMES:
        command = (fields or {}).get("command")
        if isinstance(command, str) and command in AFFERENT_FILE_EDITOR_COMMANDS:
            return Classification(
                ActionClass.AFFERENT, f"file editor {command!r} only reads", recognized=True
            )
        shown = command if isinstance(command, str) and command else "<unspecified>"
        return Classification(
            ActionClass.EFFERENT, f"file editor {shown!r} writes", recognized=True
        )

    if name in BASH_TOOL_NAMES:
        return Classification(
            ActionClass.EFFERENT,
            "bash always fans in — a shell command's effect is not knowable by reading it",
            recognized=True,
        )

    return Classification(
        ActionClass.EFFERENT,
        f"unrecognized tool {name or '<unnamed>'!r} — gated fail-closed",
        recognized=False,
    )


@dataclass(frozen=True)
class PendingEfferent:
    """One action the gate stopped, rendered for a human decision.

    Pure data, and it lives in the stdlib leaf on purpose: a :class:`levain.session.TurnResult`
    has to carry the gate's report, and ``levain.session`` keeps every OpenHands import LAZY so
    ``levain --help`` works without the extra. A report type that dragged the SDK into a module
    header would undo that.

    ``detail`` is the thing being judged — the actual shell command, or the file operation and its
    path. Rendering an action kind (``"TerminalAction"``) here would be a dead affordance: it
    names the tool while withholding the only content an operator can weigh.
    """

    tool_name: str
    detail: str
    reason: str
    recognized: bool = True

    def line(self) -> str:
        """A single operator-facing entry: what it wants to do, and why that fans in."""
        mark = "" if self.recognized else "⚠ "
        return f"{mark}{self.tool_name}: {self.detail}\n      ↳ {self.reason}"


def resolve_gate_mode(setting: GateSetting | str, *, human_present: bool) -> GateMode:
    """Resolve the declared ``efferent_gate`` setting against how the entity is being driven.

    **The gate binds to the ABSENCE OF THE HUMAN, and that is the design, not a shortcut.** The
    invariant K3 enforces is *"every efferent action fans in to the human"* — and an operator
    sitting at the REPL watching tool activity stream past IS that fan-in, already satisfied,
    continuously. What the gate supplies is the fan-in that presence was supplying, at the moment
    presence goes away. So ``auto`` resolves to ungated under a human and gated without one:

      - :func:`levain.run.run_entity` (the REPL) drives with ``human_present=True``;
      - :func:`levain.run.run_task` (``--task``, a scheduler, a supervisor) drives with ``False``.

    This is also why the gate could never have been a permission PROMPT. A prompt requires
    someone to answer it, and the case K3 exists to serve — the unattended governed seat — is
    definitionally the case with nobody there. The prompt was independently rejected on its
    merits too (operators bypass prompts in real life), which is why an explicit ``"gated"``
    under a REPL is an opt-IN affordance rather than the default.

    An unknown setting resolves as if ``auto``; the config loader rejects unknown values
    fail-closed long before this, so this branch is belt-and-braces for a direct caller.
    """
    if setting == "gated":
        return "gated"
    if setting == "ungated":
        return "ungated"
    return "ungated" if human_present else "gated"
