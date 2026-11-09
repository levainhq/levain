"""levain.slot.hook_run — what a slot's root-owned hook script runs (``levain_gate``, ``levain_queue``).

The script :mod:`levain.slot.hook_templates` renders into ``<S>/root/hooks/<name>`` puts the import tree on
``sys.path`` and calls :func:`run`. This module is stdlib only and 3.9-valid (the slot's hook runs under
``/usr/bin/python3``), and it holds the ONE decision the operator's own activation hook makes the other way:

**FAIL CLOSED.** The operator's hook (``templates/activation/hooks/consent_claude_code.py``) exits 0 with no output
when its adapter will not import, so the call passes. That is right on the operator's own machine and wrong for a
gate inside an unattended slot: a gate that opens when it breaks is not a gate. Here every way the adapter cannot
answer is exit 2 with a NAMED refusal on stderr (Claude Code blocks a PreToolUse and denies a PermissionRequest on
exit 2), never exit 0 with no output:

* ``hook-not-in-slot``: the slot's broker socket variable is not in the environment. The adapter would then take its
  operator path (a local queue under the home directory), which a slot must never reach.
* ``hook-broker-client-unavailable`` / ``hook-adapter-unavailable``: the broker client / the adapter did not import.
* ``hook-adapter-failed``: the adapter raised.
* ``hook-adapter-exit``: the adapter returned non-zero.

When the adapter answers, what it printed is what the hook prints, unchanged: its own deny, ask-nothing and
no-decision outputs are its contract (a PermissionRequest in a slot gets no decision there; Claude Code's mode
answers it). This module adds no allow of its own.
"""
from __future__ import annotations

import io
import os
import sys
from typing import Any, Callable, Mapping, Optional, TextIO

REFUSED_EXIT = 2

ADAPTER_MODULE = "levain.consent.adapters.claude_code"


def refuse(name: str, why: str, detail: str, stderr: TextIO) -> int:
    try:
        stderr.write("levain hook %s: refused: %s (%s)\n" % (name, why, detail[:200]))
        stderr.flush()
    except BaseException:  # noqa: BLE001 - a refusal that cannot be written is still a refusal
        pass
    return REFUSED_EXIT


def _load_adapter() -> Any:
    # a function-local import on purpose: a failure here is a named refusal, not a traceback (tests/ci/root_closure.py
    # LAZY_SITES row "operator": the slot hook runs as the slot uid, never root; the adapter's 3.9 floor is held by
    # tests/test_slot_hook_templates.py, not by the root closure)
    from levain.consent.adapters import claude_code
    return claude_code


def run(name: str, *, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None,
        stderr: Optional[TextIO] = None, environ: Optional[Mapping[str, str]] = None,
        load_adapter: Optional[Callable[[], Any]] = None) -> int:
    """Run the hook ``name``: 0 with the adapter's output, or :data:`REFUSED_EXIT` with a named refusal. ``environ``
    (default ``os.environ``) is the ONE environment: :func:`in_slot` is checked against it and the adapter's ``main``
    is handed it, so the slot check and the adapter can never read two different environments."""
    err: Any = stderr
    try:
        err = stderr if stderr is not None else sys.stderr
        stdin = stdin if stdin is not None else sys.stdin
        stdout = stdout if stdout is not None else sys.stdout
        env = os.environ if environ is None else environ
        from levain.consent.broker_client import in_slot
    except BaseException as exc:  # noqa: BLE001 - fail closed
        return refuse(name, "hook-broker-client-unavailable", "%s: %s" % (type(exc).__name__, exc), err)
    try:
        adapter = (load_adapter or _load_adapter)()
        main = adapter.main
    except BaseException as exc:  # noqa: BLE001 - fail closed: nothing that cannot import is a gate
        return refuse(name, "hook-adapter-unavailable", "%s: %s" % (type(exc).__name__, exc), err)
    stderr = err
    try:
        slot = in_slot(env)
    except BaseException as exc:  # noqa: BLE001
        return refuse(name, "hook-adapter-failed", "%s: %s" % (type(exc).__name__, exc), stderr)
    if not slot:
        return refuse(name, "hook-not-in-slot", "no broker socket in the environment", stderr)
    out = io.StringIO()
    try:
        code = main([], stdin=stdin, stdout=out, environ=env)
    except BaseException as exc:  # noqa: BLE001 - SystemExit included: an adapter that exits is not an answer
        return refuse(name, "hook-adapter-failed", "%s: %s" % (type(exc).__name__, exc), stderr)
    if type(code) is not int or code != 0:  # exactly the int 0: False / 0.0 / a zero-equal object is no success
        # an invalid return is described by its type: formatting an arbitrary object can raise or allocate without bound
        return refuse(name, "hook-adapter-exit", "exit %r" % (code,) if type(code) is int else "exit of type %s" % type(code).__name__, stderr)
    try:
        stdout.write(out.getvalue())
        stdout.flush()
    except BaseException as exc:  # noqa: BLE001
        return refuse(name, "hook-adapter-failed", "writing the answer: %s" % type(exc).__name__, stderr)
    return 0


CONFIG_PAYLOAD_MAX = 64 * 1024
_FIELD_MAX = 300


def _field(event: Any, key: str) -> str:
    v = event.get(key) if isinstance(event, dict) else None
    return v[:_FIELD_MAX].replace("\n", " ") if isinstance(v, str) else "?"


def file_config_alarm(environ: Mapping[str, str], event: Any) -> Optional[str]:
    """Record one ConfigChange alarm in the gate's own ledger (:func:`levain.consent.ledger.gate_row`, the record every
    gated call leaves): a denied-by-gate row for the tool ``ConfigChange:<source>`` whose input (kept as a digest, as every
    ledger row's is) is the event's ``source`` and ``file_path``, in the consent directory ``environ`` names. Returns the row id; None on any failure, and never
    raises."""
    try:
        from levain.consent.adapters.claude_code import AGENT
        from levain.consent.ledger import gate_row
        from levain.consent.queue import queue_dir_for

        session = event.get("session_id") if isinstance(event, dict) else None
        cwd = event.get("cwd") if isinstance(event, dict) else None
        return gate_row(AGENT, queue_dir_for(environ), event if isinstance(event, dict) else {},
                 session if isinstance(session, str) and session else "config-change",
                 "ConfigChange:" + _field(event, "source")[:40], {"source": _field(event, "source"), "file_path": _field(event, "file_path")},
                 cwd if isinstance(cwd, str) and cwd else "/", refused="config-change")
    except BaseException:  # noqa: BLE001 - the alarm is a record; the block does not depend on it
        return None


def config_guard(name: str, *, stdin: Optional[TextIO] = None, stderr: Optional[TextIO] = None,
                 environ: Optional[Mapping[str, str]] = None,
                 record: Optional[Callable[[Mapping[str, str], Any], None]] = None) -> int:
    """The ``ConfigChange`` hook: every settings change in a running slot session is BLOCKED (exit
    :data:`REFUSED_EXIT`, which stops the change from taking effect) and one alarm line goes to stderr and to the gate
    ledger. A frozen session reads no settings file live, so a change is either a mistake or a tamper; the payload
    is never trusted (it may be empty, not JSON, or not an object), and nothing it holds can make this return 0."""
    import json

    err: Any = None
    try:
        err = stderr if stderr is not None else sys.stderr
        raw = (stdin if stdin is not None else sys.stdin).read(CONFIG_PAYLOAD_MAX)
        try:
            event = json.loads(raw)
        except ValueError:
            event = None
        err.write("levain hook %s: blocked a settings change (source=%s file_path=%s)\n"
                  % (name, _field(event, "source"), _field(event, "file_path")))
        err.flush()
        (record or file_config_alarm)(os.environ if environ is None else environ, event)
    except BaseException:  # noqa: BLE001 - whatever failed, the change stays blocked
        pass
    return REFUSED_EXIT


__all__ = ["ADAPTER_MODULE", "REFUSED_EXIT", "config_guard", "file_config_alarm", "refuse", "run"]
