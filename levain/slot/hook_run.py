"""levain.slot.hook_run — what a slot's root-owned hook scripts run: :func:`run` for the gate and the queue
(``levain_gate``, ``levain_queue``), :func:`config_guard` for the ConfigChange alarm and :func:`repo_filter` for a repo's
own hook on a gate event.

The script :mod:`levain.slot.hook_templates` renders into ``<S>/root/hooks/<name>`` puts the import tree on
``sys.path`` and calls its entry. This module is stdlib only and 3.9-valid (the slot's hook runs under
``/usr/bin/python3``), and it holds the ONE decision the operator's own activation hook makes the other way:

**FAIL CLOSED.** The operator's hook (``templates/activation/hooks/consent_claude_code.py``) exits 0 with no output
when its adapter will not import, so the call passes. That is right on the operator's own machine and wrong for a
gate inside an unattended slot: a gate that opens when it breaks is not a gate. Here every way the adapter cannot
answer is a deny with a NAMED refusal, never exit 0 with no output. Claude Code blocks a PreToolUse on exit 2, so on
that event (``levain_gate``) the refusal is exit 2 and the line on stderr. On a PermissionRequest its hooks reference
says exit 2 is NOT honoured (the permission flow proceeds unchanged and the stderr is discarded; a deny there is a
``decision`` object on stdout), so on that event (``levain_queue``) the refusal is that object, exit 0, as
:func:`_deny` answers it (and :func:`repo_filter`'s deny on the same event). The refusals:

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
#: a hook FAILURE (any exit but 0 or 2): what a PermissionRequest deny answers when its decision object cannot be
#: written. Claude Code's hooks reference: with ``"onFailure": "block"`` (render wires it on every slot hook) "a failure
#: does what exit code 2 does on that event, except on ``PermissionRequest``, where it denies the request"; without
#: it (a Claude Code before 2.1.295) it is a non-blocking error, no worse there than the exit 2 it replaces.
FAILED_EXIT = 1

ADAPTER_MODULE = "levain.consent.adapters.claude_code"

#: the event each hook :func:`run` serves is wired under (equal by test to ``hook_templates.EVENTS``; not imported:
#: hook_templates imports this module). A refusal is answered as :func:`_deny` answers it on that event.
RUN_EVENTS = {"levain_gate": "PreToolUse", "levain_queue": "PermissionRequest"}


def _refusal(name: str, why: str, detail: str) -> str:
    return "levain hook %s: refused: %s (%s)" % (name, why, detail[:200])


def refuse(name: str, why: str, detail: str, stderr: TextIO) -> int:
    try:
        stderr.write(_refusal(name, why, detail) + "\n")
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
    """Run the hook ``name``: 0 with the adapter's output, or a named refusal answered as :func:`_deny` answers it on
    the event ``name`` is wired under (:data:`RUN_EVENTS`): on PreToolUse exit :data:`REFUSED_EXIT` and the line on
    stderr; on PermissionRequest (``levain_queue``) the deny decision object with the line as its message, exit 0,
    except when the adapter's answer broke while being written to stdout: then :data:`FAILED_EXIT`, no second document.
    ``environ`` (default ``os.environ``) is the ONE environment: :func:`in_slot` is checked against it and the
    adapter's ``main`` is handed it, so the slot check and the adapter can never read two different environments."""
    event = RUN_EVENTS.get(name)
    err: Any = stderr

    def no(why: str, detail: str) -> int:
        return _deny(event, _refusal(name, why, detail), stdout if stdout is not None else sys.stdout, err)

    try:
        err = stderr if stderr is not None else sys.stderr
        stdin = stdin if stdin is not None else sys.stdin
        stdout = stdout if stdout is not None else sys.stdout
        env = os.environ if environ is None else environ
        from levain.consent.broker_client import in_slot
    except BaseException as exc:  # noqa: BLE001 - fail closed
        return no("hook-broker-client-unavailable", "%s: %s" % (type(exc).__name__, exc))
    try:
        adapter = (load_adapter or _load_adapter)()
        main = adapter.main
    except BaseException as exc:  # noqa: BLE001 - fail closed: nothing that cannot import is a gate
        return no("hook-adapter-unavailable", "%s: %s" % (type(exc).__name__, exc))
    try:
        slot = in_slot(env)
    except BaseException as exc:  # noqa: BLE001
        return no("hook-adapter-failed", "%s: %s" % (type(exc).__name__, exc))
    if not slot:
        return no("hook-not-in-slot", "no broker socket in the environment")
    out = io.StringIO()
    try:
        code = main([], stdin=stdin, stdout=out, environ=env)
    except BaseException as exc:  # noqa: BLE001 - SystemExit included: an adapter that exits is not an answer
        return no("hook-adapter-failed", "%s: %s" % (type(exc).__name__, exc))
    if type(code) is not int or code != 0:  # exactly the int 0: False / 0.0 / a zero-equal object is no success
        # an invalid return is described by its type: formatting an arbitrary object can raise or allocate without bound
        return no("hook-adapter-exit", "exit %r" % (code,) if type(code) is int else "exit of type %s" % type(code).__name__)
    failed = _write_whole(stdout, out.getvalue())
    if failed is not None:
        # part of the answer may be on stdout: a deny document after it would be two documents (DIOGAP-A3)
        return _deny(event, _refusal(name, "hook-adapter-failed", "writing the answer: %s" % failed), None, err)
    return 0


CONFIG_PAYLOAD_MAX = 64 * 1024
_FIELD_MAX = 300


def _field(event: Any, key: str) -> str:
    v = event.get(key) if isinstance(event, dict) else None
    # every unprintable character (a newline, a carriage return, an escape) becomes a space: one line, cursor unmoved
    return "".join(c if c.isprintable() else " " for c in v[:_FIELD_MAX]) if isinstance(v, str) else "?"


#: the most the ConfigChange guard waits on the broker for its alarm's ack (well inside the hook's own timeout)
ALARM_TIMEOUT = 5.0


def config_guard(name: str, *, stdin: Optional[TextIO] = None, stderr: Optional[TextIO] = None,
                 environ: Optional[Mapping[str, str]] = None) -> int:
    """The ``ConfigChange`` hook: every settings change Claude Code sees in a running slot session is BLOCKED (exit
    :data:`REFUSED_EXIT`: the running session does not take it up) and named in one stderr line. It blocks only the
    running session's reload: the file on disk keeps the change (measured), so an "always allow" Claude Code writes to
    ``settings.local.json`` is still there for the next launch to read. A frozen session reads no settings file live,
    so the block costs nothing but the alarm. Nothing is filed slot-side: from a slot the consent directory resolves
    under the slot's own home (measured), where no operator reader looks. In a slot (``environ``, default
    ``os.environ``, names the broker socket) the same words go to the broker as one ``ALARM`` (row 166), which records
    it on the operator's side; an alarm that cannot be sent adds one stderr line saying so. The payload is never
    trusted (it may be empty, not JSON, or not an object), and nothing it holds, nor anything the broker answers, can
    make this return 0."""
    import json

    err: Any = None
    what = "source=? file_path=?"
    try:
        err = stderr if stderr is not None else sys.stderr
        raw = (stdin if stdin is not None else sys.stdin).read(CONFIG_PAYLOAD_MAX)
        try:
            event = json.loads(raw)
        except ValueError:
            event = None
        what = "source=%s file_path=%s" % (_field(event, "source"), _field(event, "file_path"))
        err.write("levain hook %s: blocked a settings change (%s)\n" % (name, what))
        err.flush()
    except BaseException:  # noqa: BLE001 - whatever failed, the change stays blocked
        pass
    try:
        env = os.environ if environ is None else environ
        from levain.consent.broker_client import BrokerClient, in_slot
        if in_slot(env):
            BrokerClient.from_env(env, timeout=ALARM_TIMEOUT).alarm("config-change", what)
    except BaseException as exc:  # noqa: BLE001 - an alarm that is not recorded never unblocks the change
        why = getattr(exc, "name", None)
        try:
            err.write("levain hook %s: alarm not recorded (%s)\n"
                      % (name, why[:80] if isinstance(why, str) and why else type(exc).__name__))
            err.flush()
        except BaseException:  # noqa: BLE001
            pass
    return REFUSED_EXIT


# --- the repo hook filter (row 161, L2 HIGH 1+2) ----------------------------------------------------------------------

#: The Claude Code events on which a repo's own hook is carried only through :func:`repo_filter`: the events a slot's
#: gate and queue answer, where a repo hook could otherwise answer allow, ask or defer, or rewrite the tool input after
#: the gate decided on the original (``updatedInput``), or add a permission rule (``updatedPermissions``).
REPO_FILTERED_EVENTS = ("PreToolUse", "PermissionRequest")
#: the stdin the filter takes from Claude Code and hands to the repo command; a larger payload is a filter failure
REPO_PAYLOAD_MAX = 8 * 1024 * 1024
#: the most the filter reads of the repo command's stdout, and of its stderr; more on either is a filter failure
REPO_OUTPUT_MAX = 1024 * 1024
REPO_REASON_MAX = 2000
#: the longest repo command timeout the filter accepts
REPO_TIMEOUT_MAX = 600
#: a deny whose reason is absent or not a string
REPO_DENY_DEFAULT = "denied by a hook in this repository's settings"
#: the fixed start of the deny a FILTER failure becomes (what failed follows it in parentheses)
REPO_FAILED = "Levain's filter around a hook in this repository's settings failed, so the call is denied"
#: the shell the repo command string runs under
REPO_SHELL = "/bin/sh"
#: what the filter waits, after it kills the repo command's group, for the command to be reaped
REPO_REAP_GRACE = 5.0
#: the signals by which the filter itself is stopped (Claude Code ending the hook, a hangup, an interrupt): each one
#: kills the repo command's group and makes the filter's answer a filter failure
_STOP_SIGNALS = ("SIGTERM", "SIGHUP", "SIGINT")


class _RepoFailed(Exception):
    """The repo command did not answer: the argument says what happened instead."""


class _Stopped(BaseException):
    """Raised by the filter's own stop-signal handler, so the run's cleanup kills the repo command's group."""


def _repo_deny_reason(event: str, code: int, out: bytes, err: bytes) -> Optional[str]:
    """The reason when the repo command's answer (``code`` 0 or 2) is a deny, else None. A deny is exit 2 (its stderr
    is the reason), or on exit 0 a JSON object that says deny: ``hookSpecificOutput.permissionDecision == "deny"``
    (PreToolUse), ``hookSpecificOutput.decision.behavior == "deny"`` (PermissionRequest), ``decision == "block"``
    (either), or the hard stop ``continue`` exactly ``false`` (either; its ``stopReason`` is the reason). Everything
    else, an allow, ask or defer and any ``updatedInput`` / ``updatedPermissions`` included, is None."""
    import json

    if code == REFUSED_EXIT:
        return err.decode("utf-8", "replace").strip()
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
    elif doc.get("continue") is False:
        why = doc.get("stopReason")
    else:
        return None
    return why if isinstance(why, str) else ""


def _run_repo(command: str, payload: bytes, timeout: int, environ: Optional[Mapping[str, str]]) -> tuple:
    """Run the repo command under :data:`REPO_SHELL` in its own process group with ``payload`` on its stdin and
    return ``(exit status, stdout, stderr)``; or raise :class:`_RepoFailed` when it cannot start, runs past
    ``timeout`` seconds, or writes more than :data:`REPO_OUTPUT_MAX` to either stream. Its output is read through
    pipes as it is written, so nothing it prints lands anywhere whole. Whatever happens, its whole group is then
    killed (a background child it left included) and the command is reaped within :data:`REPO_REAP_GRACE`.

    The group is killed after the command's own process is reaped, so its group id is no longer held by that process:
    while any member of the group lives the id stays the group's own and cannot be handed to a new process; once none
    lives, the kill finds no group (ESRCH), unless a new process were given that very id as its group in between."""
    import selectors
    import signal
    import subprocess
    import tempfile
    import time

    # Everything that can fail on its own is made BEFORE the command starts, and the start is the try's first act, so
    # nothing between the start and the cleanup below can leave the command running (X4/C3).
    sel = selectors.DefaultSelector()
    in_f: Any = None
    proc: Any = None
    try:
        in_f = tempfile.TemporaryFile()
        in_f.write(payload)
        in_f.seek(0)
        try:
            # the filter itself runs under render's `env -i` wrapper, so its environment is the one render computed
            # (the session's rendered env plus the fixed inherited names), and that is what the repo command gets
            proc = subprocess.Popen([REPO_SHELL, "-c", command], stdin=in_f, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=os.environ if environ is None else environ,
                                    start_new_session=True, close_fds=True)
        except OSError as exc:
            raise _RepoFailed("it could not start: %s" % type(exc).__name__) from None
        bufs = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
        for fd in bufs:
            sel.register(fd, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        exited = False
        while sel.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                raise _RepoFailed("it ran past its timeout of %d s" % timeout)
            # once the command has exited, only what is already written is read (a background child may hold a pipe)
            ready = sel.select(0 if exited else min(left, 0.05))
            if exited and not ready:
                break
            for key, _ev in ready:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fd)
                    continue
                bufs[key.fd] += chunk
                if len(bufs[key.fd]) > REPO_OUTPUT_MAX:
                    raise _RepoFailed("its output passed the filter's bound")
            exited = exited or proc.poll() is not None
        left = deadline - time.monotonic()
        try:
            code = proc.wait(timeout=max(left, 0.001))
        except subprocess.TimeoutExpired:
            raise _RepoFailed("it ran past its timeout of %d s" % timeout) from None
        return code, bytes(bufs[proc.stdout.fileno()]), bytes(bufs[proc.stderr.fileno()])
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=REPO_REAP_GRACE)
            except subprocess.TimeoutExpired:
                pass            # unkillable for now (an uninterruptible sleep): never wait on it without bound
        # a close that raises never replaces the cause the run is already leaving with (D5)
        for f in (sel, in_f) + ((proc.stdout, proc.stderr) if proc is not None else ()):
            if f is not None:
                try:
                    f.close()
                except Exception:   # noqa: BLE001
                    pass


def repo_filter(name: str, argv: Sequence[str], *, stdin: Optional[BinaryIO] = None,
                stdout: Optional[TextIO] = None, stderr: Optional[TextIO] = None,
                environ: Optional[Mapping[str, str]] = None) -> int:
    """Run one repo hook command on a gate event and pass on ONLY a deny. ``argv`` is what render wires:
    ``[event, timeout, command]``. The command runs as :func:`_run_repo` runs it, with Claude Code's payload on its
    stdin and this process's environment and working directory.

    The command's own exit status keeps the meaning Claude Code gives it natively: 2 is a deny, 0 is read for a deny
    (:func:`_repo_deny_reason`), and any other status (a signal included, and the shell's own status for a command it
    cannot find) is the command's non-blocking error: no decision. A FAILURE OF THE FILTER is a deny with the fixed
    :data:`REPO_FAILED` and what failed: the shell cannot start, the command runs past its timeout, a payload past
    :data:`REPO_PAYLOAD_MAX`, output past :data:`REPO_OUTPUT_MAX`, the filter stopped by a signal, or the filter
    breaking. Either deny is answered as :func:`_deny` answers it on the event: on PreToolUse exit
    :data:`REFUSED_EXIT` with one line on stderr (measured); on PermissionRequest, where Claude Code's hooks reference
    says exit 2 is not honoured, the ``decision`` object with ``behavior`` ``deny`` and the same line as its
    ``message`` on stdout, exit 0 (the documented form; not run against a live session). No decision (an allow, ask or
    defer, a rewrite, text that is not JSON, a non-blocking error) is exit 0 with nothing written, so the gate and the
    session's mode decide. Nothing the repo command prints reaches Claude Code but a deny's reason. An ``argv`` render
    never makes is a named refusal, answered as a deny on PermissionRequest when that is its first argument."""
    import signal

    err: Any = stderr if stderr is not None else sys.stderr
    answer: Any = stdout if stdout is not None else sys.stdout
    # the reason is the FIRST check that fails (DIOGAP-A2 item 4: it used to say the count whatever failed)
    if len(argv) != 3:
        why_argv: Optional[str] = "%d argument(s)" % len(argv)
    elif argv[0] not in REPO_FILTERED_EVENTS:
        why_argv = "event %r" % (argv[0][:40],)
    elif not (argv[1].isascii() and argv[1].isdigit()):
        why_argv = "timeout %r" % (argv[1][:40],)
    elif not 1 <= int(argv[1]) <= REPO_TIMEOUT_MAX:
        why_argv = "timeout out of range"
    elif not argv[2]:
        why_argv = "empty command"
    else:
        why_argv = None
    if why_argv is not None:
        if argv and argv[0] == "PermissionRequest":
            return _deny("PermissionRequest", "levain hook %s: refused: repo-filter-argv (%s)" % (name, why_argv),
                         answer, err)
        return refuse(name, "repo-filter-argv", why_argv, err)
    event, timeout, command = argv[0], int(argv[1]), argv[2]

    def stop(signum: int, _frame: Any) -> None:
        raise _Stopped(signum)

    old: dict = {}
    why: Optional[str] = None
    failed: Optional[str] = None
    try:
        for sig_name in _STOP_SIGNALS:
            try:
                sig = getattr(signal, sig_name)
                old[sig] = signal.signal(sig, stop)
            except (AttributeError, ValueError, OSError):    # not the main thread, or not this platform's signal
                pass
        payload = (stdin if stdin is not None else sys.stdin.buffer).read(REPO_PAYLOAD_MAX + 1)
        if len(payload) > REPO_PAYLOAD_MAX:
            raise _RepoFailed("the call's payload is past the filter's bound")
        code, out, errb = _run_repo(command, payload, timeout, environ)
        if code in (0, REFUSED_EXIT):      # any other status is the repo command's own non-blocking error
            why = _repo_deny_reason(event, code, out, errb)
    except _RepoFailed as exc:
        failed = str(exc)
    except _Stopped as exc:
        failed = "the filter was stopped by signal %s" % exc.args[0]
    except Exception as exc:  # noqa: BLE001 - the repo's check could not run: a failure, never an allow
        failed = "the filter broke: %s" % type(exc).__name__
    finally:
        for sig, handler in old.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
    if failed is None and why is None:
        return 0
    line = "%s (%s)" % (REPO_FAILED, failed) if failed is not None else (why[:REPO_REASON_MAX] or REPO_DENY_DEFAULT)
    return _deny(event, line, answer, err)


def _write_whole(out: Any, data: str) -> Optional[str]:
    """Write ``data`` to ``out``: None only when every byte went, else what failed. With a file descriptor beneath
    ``out`` (the real hook's stdout), ``out`` is flushed first (what it still buffers would otherwise reach the descriptor after the
    document, or at exit), then the bytes go to the descriptor with ``os.write``, looped over short writes; an error
    or a zero-byte write is the failure, with no second attempt by any path. Without one (an in-process ``StringIO``),
    one ``write`` and ``flush``; an error is the failure, likewise never retried."""
    try:
        fd = out.fileno()
    except BaseException:  # noqa: BLE001 - no usable descriptor: the text layer is the one writer
        fd = None
    try:
        if fd is None:
            out.write(data)
            out.flush()
            return None
        out.flush()
        left = memoryview(data.encode("utf-8"))
        while left:
            n = os.write(fd, left)
            if n <= 0:
                return "a zero-byte write"
            left = left[n:]
        return None
    except BaseException as exc:  # noqa: BLE001 - a write that broke is not whole: the caller's failure, never a retry
        return type(exc).__name__


def _deny(event: Optional[str], line: str, out: Any, err: Any) -> int:
    """A deny on ``event`` (:func:`repo_filter`'s, and :func:`run`'s refusals), with ``line`` as its reason.

    On PermissionRequest: the ``decision`` object (``hookSpecificOutput.hookEventName`` ``PermissionRequest``,
    ``decision.behavior`` ``deny``, ``decision.message`` the line, nothing else) on stdout and 0, since exit 2 is not
    honoured there, written as :func:`_write_whole` writes it: by ONE writer, never retried by another path, so a
    partial write is never followed by a second document, and 0 only after every byte of the one document went. When
    it cannot be written whole, :data:`FAILED_EXIT` with the line on stderr: a hook failure, which the rendered
    ``onFailure: block`` turns into a deny on this event, where exit 2 would be no answer; whatever part of the
    document reached stdout is then invalid JSON under a failure exit, a failure too. Every path here ends in a deny
    on a Claude Code that honours ``onFailure`` (2.1.295+; documented, not run against a live session). ``out`` None
    means stdout may already hold part of another document (:func:`run`'s answer broke while being written): no
    document is written after it, the answer is :data:`FAILED_EXIT`. On every other event: exit :data:`REFUSED_EXIT`
    with the line on stderr."""
    code = REFUSED_EXIT
    if event == "PermissionRequest":
        import json

        data = json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                  "decision": {"behavior": "deny", "message": line}}})
        if out is not None and _write_whole(out, data) is None:
            return 0
        code = FAILED_EXIT
    try:
        err.write(line + "\n")
        err.flush()
    except BaseException:  # noqa: BLE001 - a deny whose reason cannot be written is still a deny
        pass
    return code


__all__ = ["ADAPTER_MODULE", "FAILED_EXIT", "REFUSED_EXIT", "REPO_DENY_DEFAULT", "RUN_EVENTS", "REPO_FAILED", "REPO_FILTERED_EVENTS",
           "REPO_TIMEOUT_MAX", "ALARM_TIMEOUT", "config_guard", "refuse", "repo_filter", "run"]
