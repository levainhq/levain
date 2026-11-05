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


def body(host: str, name: str) -> bytes:
    if name not in EVENTS:
        raise ValueError("unknown hook %r" % (name,))
    text = (
        "#!%s %s\n" % (ROOT_PYTHON[host], PY_FLAGS)
        + "# Levain slot hook %s. Written by render, root-owned. It FAILS CLOSED: an adapter that will not import\n" % name
        + "# or answer is exit 2 with a named refusal, never exit 0 (levain.slot.hook_run).\n"
        + boot_path_code(host) + "\n"
        + "try:\n"
        + "    from levain.slot.hook_run import run\n"
        + "except BaseException as exc:\n"
        + "    sys.stderr.write('levain hook %s: refused: hook-adapter-unavailable (' + type(exc).__name__ + ')\\n')\n" % name
        + "    sys.exit(%d)\n" % hook_run.REFUSED_EXIT
        + "sys.exit(run(%r))\n" % name)
    return text.encode("utf-8")


def hook_templates(host: str) -> Dict[str, HookTemplate]:
    return {name: HookTemplate(body(host, name), event, None) for name, event in sorted(EVENTS.items())}


__all__ = ["EVENTS", "body", "hook_templates"]
