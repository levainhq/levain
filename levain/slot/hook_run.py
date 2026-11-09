"""levain.slot.hook_run — what a slot's root-owned hook scripts run: :func:`run` for the gate and the queue
(``levain_gate``, ``levain_queue``), :func:`config_guard` for the ConfigChange alarm and :func:`repo_filter` for a repo's
own hook on a gate event.

The script :mod:`levain.slot.hook_templates` renders into ``<S>/root/hooks/<name>`` puts the import tree on
``sys.path`` and calls its entry. This module is stdlib only and 3.9-valid (the slot's hook runs under
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
from typing import Any, BinaryIO, Callable, Mapping, Optional, Sequence, TextIO

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


def config_guard(name: str, *, stdin: Optional[TextIO] = None, stderr: Optional[TextIO] = None) -> int:
    """The ``ConfigChange`` hook: every settings change Claude Code sees in a running slot session is BLOCKED (exit
    :data:`REFUSED_EXIT`: the running session does not take it up) and named in one stderr line. It blocks only the
    running session's reload: the file on disk keeps the change (measured), so an "always allow" Claude Code writes to
    ``settings.local.json`` is still there for the next launch to read. A frozen session reads no settings file live,
    so the block costs nothing but the alarm. Nothing is filed: from a slot the consent directory resolves under the
    slot's own home (measured), where no operator reader looks. The payload is never trusted (it may be empty, not
    JSON, or not an object), and nothing it holds can make this return 0."""
    import json

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
    except BaseException:  # noqa: BLE001 - whatever failed, the change stays blocked
        pass
    return REFUSED_EXIT


# --- the repo hook filter (row 161, L2 HIGH 1+2) ----------------------------------------------------------------------

#: The Claude Code events on which a repo's own hook is carried only through :func:`repo_filter`: the events a slot's
#: gate and queue answer, where a repo hook could otherwise answer allow, ask or defer, or rewrite the tool input after
#: the gate decided on the original (``updatedInput``), or add a permission rule (``updatedPermissions``).
REPO_FILTERED_EVENTS = ("PreToolUse", "PermissionRequest")
#: the stdin the filter takes from Claude Code and hands to the repo command; a larger payload is no decision
REPO_PAYLOAD_MAX = 8 * 1024 * 1024
#: the repo command's stdout read for a decision, and its stderr kept for a deny's reason
REPO_OUTPUT_MAX = 1024 * 1024
REPO_REASON_MAX = 2000
#: the longest repo command timeout the filter accepts
REPO_TIMEOUT_MAX = 600
#: a deny whose reason is absent or not a string
REPO_DENY_DEFAULT = "denied by a hook in this repository's settings"
#: the shell the repo command string runs under
REPO_SHELL = "/bin/sh"


def _repo_deny_reason(event: str, code: int, out: bytes, err: bytes) -> Optional[str]:
    """The reason when the repo command's result is a deny, else None. A deny is exit 2 (its stderr is the reason), or
    on exit 0 a JSON object that says deny: ``hookSpecificOutput.permissionDecision == "deny"`` (PreToolUse),
    ``hookSpecificOutput.decision.behavior == "deny"`` (PermissionRequest), or ``decision == "block"`` (either).
    Everything else, an allow, ask or defer and any ``updatedInput`` / ``updatedPermissions`` included, is None."""
    import json

    if code == REFUSED_EXIT:
        return err.decode("utf-8", "replace").strip()
    if code != 0:
        return None         # Claude Code reads no JSON from a hook that exits anything but 0 or 2
    try:
        doc = json.loads(out.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    hso = doc.get("hookSpecificOutput")
    hso = hso if isinstance(hso, dict) else {}
    if event == "PreToolUse" and hso.get("permissionDecision") == "deny":
        why = hso.get("permissionDecisionReason")
    elif event == "PermissionRequest" and isinstance(hso.get("decision"), dict) \
            and hso["decision"].get("behavior") == "deny":
        why = hso["decision"].get("message")
    elif doc.get("decision") == "block":
        why = doc.get("reason")
    else:
        return None
    return why if isinstance(why, str) else ""


def repo_filter(name: str, argv: Sequence[str], *, stdin: Optional[BinaryIO] = None,
                stdout: Optional[TextIO] = None, stderr: Optional[TextIO] = None,
                environ: Optional[Mapping[str, str]] = None) -> int:
    """Run one repo hook command on a gate event and pass on ONLY a deny. ``argv`` is what render wires:
    ``[event, timeout, command]``. The command runs under :data:`REPO_SHELL` with Claude Code's payload on its stdin,
    this process's environment and working directory, in its own process group, killed at ``timeout`` seconds. A deny
    (:func:`_repo_deny_reason`) is exit :data:`REFUSED_EXIT` with the reason alone on stderr: a deny on PreToolUse
    (measured) and, as Claude Code documents exit 2, on PermissionRequest (not run). Any other result, a kill, a
    timeout, a payload past :data:`REPO_PAYLOAD_MAX` or a
    command that cannot start included, is exit 0 with nothing written: no decision, so the gate and the session's
    mode decide. Nothing the repo command prints reaches Claude Code but a deny's reason. An ``argv`` render never
    makes is a named refusal (exit 2), so a broken wiring denies rather than skips the repo's own check."""
    import signal
    import subprocess
    import tempfile

    err: Any = stderr if stderr is not None else sys.stderr
    if len(argv) != 3 or argv[0] not in REPO_FILTERED_EVENTS or not argv[1].isdigit() \
            or not 1 <= int(argv[1]) <= REPO_TIMEOUT_MAX or not argv[2]:
        return refuse(name, "repo-filter-argv", "%d argument(s)" % len(argv), err)
    event, timeout, command = argv[0], int(argv[1]), argv[2]
    try:
        payload = (stdin if stdin is not None else sys.stdin.buffer).read(REPO_PAYLOAD_MAX + 1)
        if len(payload) > REPO_PAYLOAD_MAX:
            return 0
        with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f, \
                tempfile.TemporaryFile() as in_f:
            in_f.write(payload)
            in_f.seek(0)
            # the filter itself runs under render's `env -i` wrapper, so its environment is the one render computed
            # (the session's rendered env plus the fixed inherited names), and that is what the repo command gets
            proc = subprocess.Popen([REPO_SHELL, "-c", command], stdin=in_f, stdout=out_f, stderr=err_f,
                                    env=os.environ if environ is None else environ, start_new_session=True,
                                    close_fds=True)
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # still unreaped, so its group id is still its own: the command and every child it started
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
                return 0
            out_f.seek(0)
            err_f.seek(0)
            out = out_f.read(REPO_OUTPUT_MAX + 1)
            why = _repo_deny_reason(event, code, out if len(out) <= REPO_OUTPUT_MAX else b"",
                                    err_f.read(REPO_OUTPUT_MAX))
    except Exception:  # noqa: BLE001 - the repo's check could not run: no decision, never an allow
        return 0
    if why is None:
        return 0
    try:
        err.write((why[:REPO_REASON_MAX] or REPO_DENY_DEFAULT) + "\n")
        err.flush()
    except BaseException:  # noqa: BLE001 - a deny whose reason cannot be written is still a deny
        pass
    return REFUSED_EXIT


__all__ = ["ADAPTER_MODULE", "REFUSED_EXIT", "REPO_DENY_DEFAULT", "REPO_FILTERED_EVENTS", "REPO_TIMEOUT_MAX",
           "config_guard", "refuse", "repo_filter", "run"]
