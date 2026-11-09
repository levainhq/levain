"""levain.slot.hook_templates — the producer of render's hook templates: the gate (``levain_gate``), the queue
(``levain_queue``), the ConfigChange alarm (``levain_config``) and the repo hook filter (``levain_repo_filter``).

``render.check_facts`` demands a :class:`~levain.slot.render.HookTemplate` for every hook the blob names; the
dispatch path names the gate and the queue (``levain.agents.session_launcher.HOOKS``), and the alarm and the filter go
to every Claude session whatever the blob names. This is the one place that makes them. Each body is a root-owned
script: the interpreter ``root_tree.ROOT_PYTHON`` isolated (``-IS``), the import tree's current generation on
``sys.path`` (``root_tree.boot_path_code``), then its entry in :mod:`levain.slot.hook_run`, which fails CLOSED (see
there). Stdlib only, 3.9-valid.

Events (DESIGN_p71_s8 R-V6): ``levain_gate`` is ``PreToolUse``, ``levain_queue`` is ``PermissionRequest``. The
matcher is none (every tool): a matcher is an allowlist of what the gate looks at, and a gate that looks at less
allows more. The filter is not wired under an event of its own: render wires it once per repo command on a gate
event, with that event, a timeout and the repo command as its arguments.
"""
from __future__ import annotations

from typing import Dict

from levain.slot import hook_run
from levain.slot.render import REPO_FILTER, HookTemplate
from levain.slot.root_tree import PY_FLAGS, ROOT_PYTHON, boot_path_code

#: hook name -> the Claude Code event it is wired under
EVENTS = {"levain_gate": "PreToolUse", "levain_queue": "PermissionRequest", "levain_config": "ConfigChange"}

#: hook name -> the :mod:`levain.slot.hook_run` function its body calls (every other name calls ``run``)
ENTRIES = {"levain_config": "config_guard", REPO_FILTER: "repo_filter"}

#: Claude Code's ``timeout`` for the gate's and the queue's handlers: the consent adapter's ``GATE_TIMEOUT`` (its hold
#: ceiling 840 s + its 30 s margin; equal by test, not imported: the adapter is not root-closure code). The slot gate
#: holds ``DEFAULT_GATE_HOLD`` (300 s) after a bounded broker GATE, so it always answers inside this. render wires every
#: slot hook with ``"onFailure": "block"``, so the timeout firing (a hang, not a hold) blocks the call, never lets it run.
HOOK_TIMEOUT = 870
#: the ConfigChange alarm answers at once (it reads one payload and writes one line)
CONFIG_TIMEOUT = 30


def body(host: str, name: str) -> bytes:
    """The hook script. Everything after ``import sys`` is inside one ``try``: the boot line, the import, the entry.
    Exit 0 or 2 is the entry's; ANY other outcome (an import that fails, the entry raising, a ``BaseException``
    included, or returning anything but 0 or 2) is exit 2 with a named refusal, so the script fails closed even on a
    Claude Code that ignores ``onFailure``. What it cannot cover (the interpreter or the script not starting, a kill,
    a timeout) is ``onFailure``'s (render). The filter's entry also gets the script's arguments."""
    if name not in EVENTS and name != REPO_FILTER:
        raise ValueError("unknown hook %r" % (name,))
    entry = ENTRIES.get(name, "run")
    call = "run(%r, sys.argv[1:])" % name if name == REPO_FILTER else "run(%r)" % name
    text = (
        "#!%s %s\n" % (ROOT_PYTHON[host], PY_FLAGS)
        + "# Levain slot hook %s. Written by render, root-owned. It FAILS CLOSED: an adapter that will not import\n" % name
        + "# or answer is exit 2 with a named refusal, never exit 0 (levain.slot.hook_run). Wired onFailure block.\n"
        + "import sys\n"
        + "why = 'hook-adapter-unavailable'\n"
        + "code = None\n"
        + "try:\n"
        + "    " + boot_path_code(host) + "\n"
        + "    from levain.slot.hook_run import %s as run\n" % entry
        + "    why = 'hook-run-failed'\n"
        + "    code = %s\n" % call
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
    """Every template by name. The filter's ``event`` field is not an event it is wired under; it fills the
    template's shape."""
    out = {name: HookTemplate(body(host, name), event, None,
                              CONFIG_TIMEOUT if name == "levain_config" else HOOK_TIMEOUT)
           for name, event in sorted(EVENTS.items())}
    out[REPO_FILTER] = HookTemplate(body(host, REPO_FILTER), hook_run.REPO_FILTERED_EVENTS[0], None, HOOK_TIMEOUT)
    return out


__all__ = ["CONFIG_TIMEOUT", "ENTRIES", "EVENTS", "HOOK_TIMEOUT", "body", "hook_templates"]
