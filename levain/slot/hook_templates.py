"""levain.slot.hook_templates — the producer of render's two hook templates (``levain_gate``, ``levain_queue``).

``render.check_facts`` demands a :class:`~levain.slot.render.HookTemplate` for every hook the blob names, and the
dispatch path always names both (``levain.agents.session_launcher.HOOKS``). This is the one place that makes them.
Each body is a root-owned script: the interpreter ``root_tree.ROOT_PYTHON`` isolated (``-IS``), the import tree's
current generation on ``sys.path`` (``root_tree.boot_path_code``), then :func:`levain.slot.hook_run.run`, which
fails CLOSED (see there). Stdlib only, 3.9-valid.

Events (DESIGN_p71_s8 R-V6): ``levain_gate`` is ``PreToolUse``, ``levain_queue`` is ``PermissionRequest``. The
matcher is none (every tool): a matcher is an allowlist of what the gate looks at, and a gate that looks at less
allows more.
"""
from __future__ import annotations

from typing import Dict

from levain.slot import hook_run
from levain.slot.render import HookTemplate
from levain.slot.root_tree import PY_FLAGS, ROOT_PYTHON, boot_path_code

#: hook name -> the Claude Code event it is wired under
EVENTS = {"levain_gate": "PreToolUse", "levain_queue": "PermissionRequest"}

#: Claude Code's ``timeout`` for both handlers: the consent adapter's ``GATE_TIMEOUT`` (its hold ceiling 840 s + its
#: 30 s margin; equal by test, not imported: the adapter is not root-closure code). The slot gate holds
#: ``DEFAULT_GATE_HOLD`` (300 s) after a bounded broker GATE, so it always answers inside this. render wires every slot
#: hook with ``"onFailure": "block"``, so the timeout firing (a hang, not a hold) blocks the call, never lets it run.
HOOK_TIMEOUT = 870


def body(host: str, name: str) -> bytes:
    """The hook script. Everything after ``import sys`` is inside one ``try``: the boot line, the import, ``run``.
    Exit 0 or 2 is ``run``'s; ANY other outcome (an import that fails, ``run`` raising, a ``BaseException`` included,
    or returning anything but 0 or 2) is exit 2 with a named refusal, so the script fails closed even on a Claude Code
    that ignores ``onFailure``. What it cannot cover (the interpreter or the script not starting, a kill, a timeout) is
    ``onFailure``'s (render)."""
    if name not in EVENTS:
        raise ValueError("unknown hook %r" % (name,))
    text = (
        "#!%s %s\n" % (ROOT_PYTHON[host], PY_FLAGS)
        + "# Levain slot hook %s. Written by render, root-owned. It FAILS CLOSED: an adapter that will not import\n" % name
        + "# or answer is exit 2 with a named refusal, never exit 0 (levain.slot.hook_run). Wired onFailure block.\n"
        + "import sys\n"
        + "why = 'hook-adapter-unavailable'\n"
        + "code = None\n"
        + "try:\n"
        + "    " + boot_path_code(host) + "\n"
        + "    from levain.slot.hook_run import run\n"
        + "    why = 'hook-run-failed'\n"
        + "    code = run(%r)\n" % name
        + "    if type(code) is not int or code not in (0, %d):\n" % hook_run.REFUSED_EXIT
        + "        why, code = 'hook-run-exit ' + repr(code)[:40], None\n"
        + "except BaseException as exc:\n"
        + "    why, code = why + ' (' + type(exc).__name__ + ')', None\n"
        + "if code is None:\n"
        + "    try:\n"
        + "        sys.stderr.write('levain hook %s: refused: ' + why + '\\n')\n" % name
        + "        sys.stderr.flush()\n"
        + "    except BaseException:\n"
        + "        pass\n"
        + "    code = %d\n" % hook_run.REFUSED_EXIT
        + "sys.exit(code)\n")
    return text.encode("utf-8")


def hook_templates(host: str) -> Dict[str, HookTemplate]:
    return {name: HookTemplate(body(host, name), event, None, HOOK_TIMEOUT) for name, event in sorted(EVENTS.items())}


__all__ = ["EVENTS", "HOOK_TIMEOUT", "body", "hook_templates"]
