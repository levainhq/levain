"""``levain limits``: show, set, unset and extend a session's budgets (Q13 session 3; design F4a).

``show`` prints the limits in force per agent and where they came from, then every counted session against them.
``set`` / ``unset`` edit ``<queue dir>/limits.json`` (the operator's defaults; a raise of ONE session is only ever a
signed answer, ``extend``). ``extend`` files a ``levain_limit`` request for a session a limit HALTED, pings the
operator, holds for a signed answer and, on a verified allow, lifts the halt and resumes the session the way an
answered question resumes it. A path the operator runs; the file is the operator's, like the queue beside it (without
the S1 sandbox an agent's Bash can rewrite it: a brake on a runaway, not a bound).

``extend`` with no allow by the end of its hold marks its request undecided (a gap, no halt), so a late allow ruled on
that gap is delivered and applied like any limit ruling. A resume runs under the question band's per-session lock.

``--slots`` (slot limits, ``project_memory/1025-6/limits/DESIGN.md`` §3; head rulings R1 F7): the limits SLOT
sessions run under, root's ``L/policy/limits``, which the operator's uid cannot write. ``set --slots <agent|default>
<kind> <value>`` / ``unset --slots <agent|default> <kind>`` read the current document through the queue broker's own
reader (:class:`levain.qbroker.limits.PolicyLimits`), make the one change, check the result with the slot parser
(:func:`levain.consent.limits.slot_doc`) and pipe it to ``sudo <install helper> set-limits``, which asks the password
and replaces the file whole. A file the broker would refuse is never merged: the command names ``--replace``, which
writes a document holding only the change. ``show --slots`` prints the slot limits in force per agent.

Known edges (one line each):

- an extend's resume writes no lease or settle record (its allow is a timely answer, not a leased ruling).
- ``set`` / ``unset`` take no lock: two at once can lose one of the two edits (``--slots`` too: one password each).
- two allows landing within the same moment (a late ruling on the halting request and this extend's allow) can each
  plan a resume: delivery does not hold this command's session lock.
- a halt released and a NEW limit crossed between reading the halt and filing: the allow is verified against the
  first kind and refused; the session stays stopped and `extend` can be run again.

Stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from levain.consent import limits as L
from levain.consent.adapters import claude_code, codex
from levain.consent.queue import ConsentQueue, authority_source, default_queue_dir
from levain.slot import SUDO

__all__ = ["add_parser", "run"]

_ADAPTERS = {"claude_code": claude_code, "codex": codex}
DEFAULT_EXTEND_HOLD = 600.0
# the largest value `set` stores: far past any real session, and a doubled limit stays an exact float
MAX_COUNT = 10 ** 12
MAX_HOURS = 10 ** 4
_STORE_ERRORS = (ValueError, OSError, RuntimeError, sqlite3.Error)


# --- the file ------------------------------------------------------------------------------------------------------

def _read_doc(path: Path) -> dict[str, Any]:
    """The current ``limits.json`` document, or a fresh one when there is none. A symlink, a non-file or a document
    that is not this format is refused (never rewritten over)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"format": L.LIMITS_FORMAT}
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"{path} is not a regular file (a symlink or a directory is never written through)")
    if st.st_size > L.LIMITS_MAX:
        raise ValueError(f"{path} is over {L.LIMITS_MAX // 1024} KiB, so it is not read; fix or move it aside first")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f"{path} is unreadable ({type(exc).__name__}); fix or move it aside first") from exc
    if not isinstance(doc, dict) or doc.get("format") != L.LIMITS_FORMAT:
        raise ValueError(f"{path} is not a {L.LIMITS_FORMAT} document; fix or move it aside first")
    return doc


def _write_doc(path: Path, doc: dict[str, Any]) -> None:
    """Atomic: a temp file in the same directory (0600), fsync, rename; then the directory is synced."""
    if os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode):
        raise ValueError(f"{path} is not a regular file (a symlink or a directory is never written through)")
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > L.LIMITS_MAX:
        raise ValueError(f"the document would be over {L.LIMITS_MAX // 1024} KiB, which is not read; nothing written")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".limits-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _parse_value(kind: str, text: str) -> int | float:
    """The value as stored: a whole-number kind parsed with ``int`` (so the typed number is stored exactly), hours
    as a float; each within its cap."""
    hours = kind == "active_hours"
    want = (f"a positive number of at most {MAX_HOURS:,}" if hours
            else f"a positive whole number of at most {MAX_COUNT:,}")
    try:
        v: Any = float(text) if hours else int(text)
    except ValueError:
        raise ValueError(f"{kind} must be {want}; {text!r} is not") from None
    if L._limit_value(kind, v) is None or v > (MAX_HOURS if hours else MAX_COUNT):
        raise ValueError(f"{kind} must be {want}; {text!r} is not")
    return v


# --- the slot limits (--slots: root's L/policy/limits) -------------------------------------------------------------


@dataclass(frozen=True)
class SlotSeams:
    """Where the slot limits live and how root writes them. ``store``: the store root ``L``; ``owner``: the uid
    ``L/policy`` and the file must belong to (root; a test passes its own); ``helper``: runs ``set-limits`` with a
    document on stdin and returns (exit status, stdout, stderr); ``None`` is :func:`_sudo_set_limits`."""

    host: str
    store: Path
    owner: int = 0
    helper: Callable[[bytes], tuple[int, str, str]] | None = None


def _host_slot_seams() -> SlotSeams:
    from levain.slot import layout

    host = "darwin" if sys.platform == "darwin" else "linux"
    return SlotSeams(host=host, store=layout.STORE[host])


def _sudo_set_limits(host: str, data: bytes) -> tuple[int, str, str]:
    """``sudo <install helper> set-limits`` with ``data`` on stdin. sudo reads the password from the terminal, not
    from the stdin this pipe holds. sudo gets a fixed PATH and nothing of the operator's environment: the helper
    reads only the ``SUDO_UID`` sudo sets."""
    from levain.slot import layout

    helper = str(layout.install_helper_path(host))    # type: ignore[arg-type]
    if not os.path.isfile(helper):
        return 127, "", f"the install helper {helper} is not installed: slots are not set up on this host\n"
    p = subprocess.run([SUDO, helper, "set-limits"], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       check=False, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def _reader(seams: SlotSeams) -> Any:
    from levain.qbroker.limits import PolicyLimits

    return PolicyLimits(seams.store / "policy", owner=seams.owner)


@contextlib.contextmanager
def _quiet_broker() -> Iterator[None]:
    """The reader logs an unusable file for the broker's log; the command says it instead."""
    log = logging.getLogger("levain.qbroker")
    was = log.disabled
    log.disabled = True
    try:
        yield
    finally:
        log.disabled = was


def _not_set_up(seams: SlotSeams) -> str | None:
    policy = seams.store / "policy"
    return None if policy.is_dir() else f"slots are not set up on this host ({policy} is absent)"


def _parse_slot_value(kind: str, text: str) -> int:
    """A slot limit as root stores it: an integer in :data:`levain.consent.limits.SLOT_RANGE`, typed as digits."""
    if kind not in L.SLOT_KINDS:
        raise ValueError(f"{kind} cannot be set with --slots: a slot counts no tokens (it counts "
                         f"{', '.join(L.SLOT_KINDS)})")
    lo, hi = L.SLOT_RANGE[kind]
    hours = " of hours" if kind == "active_hours" else ""
    want = f"a whole number{hours} from {lo:,} to {hi:,} (a fraction is refused, never rounded)"
    if not re.fullmatch(r"[0-9]{1,12}", text) or not lo <= int(text) <= hi:
        raise ValueError(f"{kind} in a slot must be {want}; {text!r} is not")
    return int(text)


def _slot_doc_for_edit(seams: SlotSeams, replace: bool, retry: str,
                       say: Callable[[str], None]) -> dict[str, Any] | None:
    """The document to change: a copy of the one in force, an empty one when there is no file or with
    ``--replace``; ``None`` (said) when the file is unusable and ``--replace`` was not given (R1 F7: never merged)."""
    from levain.qbroker.limits import LimitsUnavailable

    missing = _not_set_up(seams)
    if missing:
        say(f"levain limits: refused, nothing changed: {missing}.")
        return None
    if replace:
        return {"format": L.LIMITS_FORMAT}
    try:
        with _quiet_broker():
            doc = _reader(seams).document()
    except LimitsUnavailable as exc:
        if exc.reason.startswith("the policy directory"):
            say(f"levain limits: refused, nothing changed: {exc.reason} (`levain doctor` shows the store).")
        else:
            say(f"levain limits: refused, nothing changed: {exc.reason}. A file the queue broker refuses is never "
                f"merged: `{retry} --replace` writes a document holding only this change (every other slot limit "
                "back to its default).")
        return None
    if doc is None:
        return {"format": L.LIMITS_FORMAT}
    return {k: dict(v) if isinstance(v, dict) else v for k, v in doc.items()}    # the reader's cache stays as read


def _helper_answer(out: str) -> dict[str, Any] | None:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    try:
        got = json.loads(lines[-1]) if lines else None
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def _slot_write(seams: SlotSeams, doc: dict[str, Any], what: str, say: Callable[[str], None]) -> int:
    """Check ``doc`` as root will, hand it to ``set-limits`` and say its answer: ok (with any warning), the refusal
    by name, or no answer. Root replaces the file whole with these bytes, re-checked and re-serialised."""
    from levain.contracts.canon import canonical_bytes
    from levain.qbroker.limits import LimitsUnavailable

    data = canonical_bytes(doc)
    try:
        L.slot_doc(data)
    except L.LimitsDocError as exc:
        say(f"levain limits: refused, nothing changed: the install helper would refuse the document ({exc}).")
        return 1
    helper = seams.helper or (lambda d: _sudo_set_limits(seams.host, d))
    try:
        rc, out, err = helper(data)
    except KeyboardInterrupt:
        say("levain limits: interrupted; `levain limits show --slots` says what is in force.")
        return 130
    except OSError as exc:
        say(f"levain limits: the install helper could not be run ({type(exc).__name__}: {exc}); nothing changed.")
        return 1
    answer = _helper_answer(out)
    relay = [f"  {ln}" for ln in err.splitlines() if ln.strip()]
    if answer is None:
        say(f"levain limits: no answer from the install helper (exit {rc}); `levain limits show --slots` says what "
            "is in force.")
    elif answer.get("ok") is not True:
        name = answer.get("refused")
        if name == "internal":
            say("levain limits: the install helper failed (internal); `levain limits show --slots` says what is in "
                "force.")
        else:
            say(f"levain limits: refused by the install helper ({name}), nothing changed.")
    else:
        say(f"levain limits: {what} (L/policy/limits, {answer.get('digest')}).")
    for ln in relay:
        say(ln)
    if answer is None or answer.get("ok") is not True:
        return 1
    try:
        _show_slots_text(seams, None, say)
    except LimitsUnavailable as exc:      # the write stands: only the read-back failed
        say(f"levain limits: the slot limits were written, but could not be read back: {exc.reason}")
    return 0


def _slot_limits(seams: SlotSeams, agent: str | None) -> tuple[str, dict[str, dict[str, int]]]:
    """(the source, the slot limits in force per agent), through the broker's reader; raises its
    ``LimitsUnavailable``."""
    reader = _reader(seams)
    with _quiet_broker():
        source = str(reader.path) if reader.document() is not None else "defaults (no file)"
        return source, {a: {k: int(v) for k, v in reader(a, "levain-limits-show").items()}
                        for a in ([agent] if agent else list(L.AGENTS))}


def _show_slots_text(seams: SlotSeams, agent: str | None, say: Callable[[str], None]) -> None:
    source, lim = _slot_limits(seams, agent)
    say(f"slot limits in force, from {source}:")
    for a, got in lim.items():
        say(f"  {a}: calls {got['calls']:,}, active hours {got['active_hours']:,}, repeat {got['repeat']}")


def _cmd_show_slots(args: argparse.Namespace, say: Callable[[str], None], seams: SlotSeams) -> int:
    from levain.qbroker.limits import LimitsUnavailable

    missing = _not_set_up(seams)
    if missing:
        say(f"levain limits: {missing}; slot limits apply to slot sessions only.")
        return 0
    try:
        if args.json:
            source, lim = _slot_limits(seams, args.agent)
            say(json.dumps({"source": source, "limits": lim}, indent=2, sort_keys=True))
        else:
            _show_slots_text(seams, args.agent, say)
    except LimitsUnavailable as exc:
        say(f"levain limits: {exc.reason}. Until it is, the queue broker refuses every slot tool call it gates "
            "(limits-failed).")
        return 1
    return 0


def _cmd_set_slots(args: argparse.Namespace, say: Callable[[str], None], seams: SlotSeams) -> int:
    agent = args.slots
    try:
        value = _parse_slot_value(args.kind, args.value)
    except ValueError as exc:
        say(f"levain limits: refused, nothing changed: {exc}")
        return 1
    doc = _slot_doc_for_edit(seams, args.replace, f"levain limits set --slots {agent} {args.kind} {args.value}", say)
    if doc is None:
        return 1
    doc.setdefault(agent, {})[args.kind] = value
    what = f"slot limits written: {agent} {args.kind} = {value:,}"
    return _slot_write(seams, doc, what + (", every other slot limit its default" if args.replace else ""), say)


def _cmd_unset_slots(args: argparse.Namespace, say: Callable[[str], None], seams: SlotSeams) -> int:
    agent = args.slots
    doc = _slot_doc_for_edit(seams, args.replace, f"levain limits unset --slots {agent} {args.kind}", say)
    if doc is None:
        return 1
    if not args.replace:
        block = doc.get(agent) or {}
        if args.kind not in block:
            say(f"levain limits: the slot limits set no {args.kind} for {agent}; nothing changed.")
            return 0
        del block[args.kind]
        if not block:
            doc.pop(agent, None)
    what = ("slot limits written: every slot limit its default" if args.replace
            else f"slot limits written: {agent} {args.kind} unset")
    return _slot_write(seams, doc, what, say)


# --- show (output) -----------------------------------------------------------------------------------------

def _iso(t: float | None) -> str | None:
    if t is None:
        return None
    try:
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _fmt(kind: str, v: float) -> str:
    return f"{v:.1f}" if kind == "active_hours" else f"{int(v):,}"


def _show_text(qdir: Path, agent: str | None, say: Callable[[str], None]) -> None:
    say(f"limits in force ({qdir}):")
    for a in ([agent] if agent else list(L.AGENTS)):
        lim, src = L.load_limits(qdir, a)
        say(f"  {a}: {src}")
        for k in L.KINDS:
            say(f"    {k:<18} {_fmt(k, lim[k])}")
    rows = L.snapshot(qdir, agent)
    if not rows:
        say("no session counted yet")
        return
    say(f"{len(rows)} counted session(s):")
    for r in rows:
        halt = r["halt"]
        tag = ""
        if halt:
            gap = f", gap #{halt['gap']}" if halt.get("gap") is not None else ", ended"
            tag = f"  HALTED on {halt['kind']}{gap}"
        say(f"  {r['agent']} {r['root']}  last {_iso(r['last_at']) or '-'}{tag}")
        say("    " + "  ".join(f"{k} {_fmt(k, r['counts'][k])}/{_fmt(k, r['limits'][k])}" for k in L.KINDS))


# --- the commands --------------------------------------------------------------------------------------------------

def _queue_dir(args: argparse.Namespace) -> Path:
    return args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()


def _cmd_show(args: argparse.Namespace, say: Callable[[str], None]) -> int:
    qdir = _queue_dir(args)
    try:
        if args.json:
            text = json.dumps(L.panel(qdir), indent=2, sort_keys=True)
        else:
            lines: list[str] = []
            _show_text(qdir, args.agent, lines.append)
            text = "\n".join(lines)
    except _STORE_ERRORS as exc:
        say(f"levain limits: the limits store in {qdir} cannot be read ({type(exc).__name__}: {exc})")
        return 1
    say(text)
    return 0


def _cmd_set(args: argparse.Namespace, say: Callable[[str], None]) -> int:
    qdir = _queue_dir(args)
    try:
        value = _parse_value(args.kind, args.value)
        path = qdir / L.LIMITS_NAME
        doc = _read_doc(path)
        block = doc.get(args.agent)
        if block is None:
            block = doc[args.agent] = {}
        if not isinstance(block, dict):
            raise ValueError(f"{path}'s {args.agent!r} block is not an object; fix or move the file aside first")
        block[args.kind] = value
        _write_doc(path, doc)
    except (ValueError, OSError) as exc:
        say(f"levain limits: refused, nothing changed: {exc}")
        return 1
    say(f"levain limits: {args.agent} {args.kind} = {_fmt(args.kind, value)} (in {qdir / L.LIMITS_NAME}). "
        "Sessions already counted keep their count; the new value applies to their next call.")
    return 0


def _cmd_unset(args: argparse.Namespace, say: Callable[[str], None]) -> int:
    qdir = _queue_dir(args)
    try:
        path = qdir / L.LIMITS_NAME
        doc = _read_doc(path)
        block = doc.get(args.agent)
        if not isinstance(block, dict) or args.kind not in block:
            say(f"levain limits: {args.agent} has no {args.kind} set; nothing changed.")
            return 0
        del block[args.kind]
        if not block:
            del doc[args.agent]
        _write_doc(path, doc)
    except (ValueError, OSError) as exc:
        say(f"levain limits: refused, nothing changed: {exc}")
        return 1
    say(f"levain limits: {args.agent} {args.kind} unset (the default {_fmt(args.kind, L.DEFAULTS[args.kind])} "
        "applies unless the other block sets it).")
    return 0


def _signable(settings: Any) -> bool:
    from levain.consent.push_gate import _signable as signable
    return signable(settings)


def _make_queue(qdir: Path, settings: Any) -> ConsentQueue:
    from levain.authority import anchor_path

    root = Path(settings.cockpit_data_root)
    return ConsentQueue(qdir, home=Path.home(),
                        authority=authority_source(root / "cockpit.db", anchor=anchor_path(root)))


def _find_halt(qdir: Path, agent: str | None, session: str) -> tuple[str, dict[str, Any]]:
    root = L.session_root(session)
    hits = [r for r in L.snapshot(qdir, agent) if r["root"] == root and r["halt"]]
    if not hits:
        raise ValueError("the session is not halted by a limit: there is nothing to extend")
    if len(hits) > 1:
        raise ValueError(f"more than one agent counted {root}; name one with --agent")
    return hits[0]["agent"], hits[0]["halt"]


_NOT_RESUMED = "the halt is lifted; the session goes on when it next runs (nothing was resumed)"
_RIDES = re.compile(r"(;| and) the extend (lands at|rides) its next step$")


def _resume(queue: ConsentQueue, agent: str, root: str, halt_request: str, text: str, say: Callable[[str], None], *,
            listing: Any, running: Any, launch: Any) -> int:
    """Start the halted session again with ``text`` as its prompt when it is ended and was recorded resumable;
    otherwise say that nothing was resumed. Under the question band's per-session lock (one delivery per session at
    a time, route through launch). Returns the exit code: 4 when the launch raised (a child may be running)."""
    from levain.consent import band
    from levain.consent import delivery as dv

    with band._session_lock(queue, agent, root) as held:
        if not held:
            say(f"levain limits: another delivery for the session held its lock for {band.SESSION_LOCK_WAIT:g} s; "
                f"{_NOT_RESUMED}.")
            return 0
        meta = queue.resume_meta(halt_request) or {}
        where, why, argv, cwd = dv.route(agent, root, meta, noun="extend", listing=listing, running=running)
        if where == "stop_resume":
            stopped = dv._stop(dv.base_session(root), meta, listing)
            if stopped is not None:
                say(f"levain limits: the background session was not stopped ({stopped}); {_NOT_RESUMED}.")
                return 0
            where, why, argv, cwd = dv.route(agent, root, meta, noun="extend", listing=listing, running=running)
        if where != "resumed":
            say(f"levain limits: {_NOT_RESUMED}: {_RIDES.sub('', why)}.")
            return 0
        if agent == "claude_code":
            now = band._claude_facts(meta.get("config_dir") if isinstance(meta.get("config_dir"), str) else None,
                                     dv.base_session(root))
            if now != (meta.get("cwd"), meta.get("permission_mode")):
                say("levain limits: the session's transcript no longer shows the start directory and permission mode "
                    f"recorded when it halted, so {_NOT_RESUMED}.")
                return 0
        if not os.path.isdir(cwd or ""):
            say(f"levain limits: the session's start directory {cwd} is gone, so {_NOT_RESUMED}.")
            return 0
        full = [*argv, text]
        logdir = Path(queue.directory) / "resumes"
        logdir.mkdir(mode=0o700, exist_ok=True)
        env = dv._env(agent, meta)
        try:
            failed = launch(full, cwd or "", env, logdir / f"limits-{halt_request}.log")
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - after a spawn: never "did not start"
            say(f"levain limits: the resume may have started (unknown: {type(exc).__name__}: {exc}): check before "
                "resuming by hand.")
            return 4
    if failed is None:
        say("levain limits: the session was resumed with the Extend.")
    else:
        say(f"levain limits: the resume did not start ({failed}); {_NOT_RESUMED}.")
    return 0


def _no_allow(queue: ConsentQueue, request_id: str, root: str, kind: str, say: Callable[[str], None]) -> int | None:
    """No verified allow by the end of the hold: a request still pending is marked undecided (no halt), so it gets a
    gap and a late RULING on it is possible, delivered and applied like any limit ruling. ``None`` when an allow
    verifies after all (it landed as the hold ended): the extend goes on."""
    try:
        marked = queue.mark_undecided(request_id)
    except Exception as exc:  # noqa: BLE001 - said, never a traceback
        say(f"levain limits: not extended: no verified allow, and the request could not be marked undecided "
            f"({type(exc).__name__}: {exc}). Nothing changed.")
        return 1
    if L.verified_allow(queue, request_id, root=root, kind=kind):
        return None
    if marked.state == "undecided" and marked.gap is not None:
        say(f"levain limits: not extended: no allow in time; a late allow ruled on gap #{marked.gap} (phone card or "
            "`levain consent rule`) resumes the session.")
        return 1
    say(f"levain limits: not extended: {marked.reason}. Nothing changed.")
    return 1


def _cmd_extend(args: argparse.Namespace, say: Callable[[str], None], *, queue: ConsentQueue | None = None,
                listing: Any = None, running: Any = None, launch: Any = None) -> int:
    from levain.consent import delivery as dv

    qdir = _queue_dir(args)
    try:
        hold = DEFAULT_EXTEND_HOLD if args.hold is None else args.hold
        if not 0 < hold < float("inf"):
            raise ValueError("--hold must be a positive number of seconds")
        hold = min(hold, claude_code.GATE_HOLD_CEILING)
        agent, halt = _find_halt(qdir, args.agent, args.session)
    except _STORE_ERRORS as exc:
        say(f"levain limits: refused, nothing was filed: {exc}")
        return 1
    root = L.session_root(args.session)
    adapter = _ADAPTERS[agent]
    try:
        settings = adapter.load_settings(qdir)
        if queue is None:
            if not _signable(settings):
                say("levain limits: NOT filed. No enrolled key can answer (the queue's "
                    f"{adapter.SETTINGS_NAME} names no cockpit with an active key), so nothing can allow it.")
                return 1
            queue = _make_queue(qdir, settings)
        old = queue.resume_meta(halt["request_id"]) if halt.get("request_id") else None
    except _STORE_ERRORS as exc:
        say(f"levain limits: refused, nothing was filed: {type(exc).__name__}: {exc}")
        return 1
    try:
        request_id, call = L.file_extend(queue, agent, root, hold=hold, cwd=(old or {}).get("cwd") or os.getcwd(),
                                         resume=old)
    except _STORE_ERRORS as exc:
        say(f"levain limits: refused, nothing was filed: {exc}")
        return 1
    say(f"levain limits: filed request {request_id}: {L.limit_what(call)}. Sign it on the phone card, or "
        f"`levain consent answer {request_id} allow`. Waiting up to {hold:g} s.")
    try:
        try:
            claude_code.notify(settings, f"levain: {L.limit_what(call)}? (request {request_id}, held up to {hold:g} s)",
                               cwd=qdir)
        except Exception:  # noqa: BLE001 - a ping never fails the command
            pass
        queue.await_decision(request_id, hold + 5.0, poll=0.25)
    except KeyboardInterrupt:
        say(f"levain limits: interrupted; request {request_id} stays pending until its hold ends.")
        return 130
    except _STORE_ERRORS as exc:
        say(f"levain limits: request {request_id} was filed, but its answer could not be read ({type(exc).__name__}: "
            f"{exc}); `levain limits show` says whether the session is still halted.")
        return 1
    try:
        if not L.verified_allow(queue, request_id, root=root, kind=halt["kind"]):
            code = _no_allow(queue, request_id, root, halt["kind"], say)
            if code is not None:
                return code
        if not L.release_if_extended(queue, agent, root):
            say("levain limits: the allow verified but the halt was not lifted; the session stays stopped.")
            return 1
        ours = L.extended_by(queue.directory, request_id)
    except KeyboardInterrupt:
        say(f"levain limits: interrupted after the answer to request {request_id}; `levain limits show` says whether "
            "the session is still halted.")
        return 130
    except _STORE_ERRORS as exc:
        say(f"levain limits: the limits store failed after the answer ({type(exc).__name__}: {exc}); "
            "`levain limits show` says whether the session is still halted.")
        return 1
    label = L.LABELS.get(halt["kind"], halt["kind"])
    if not ours:
        say(f"levain limits: EXTENDED {label} for {root}, by another allow (a ruling on an earlier request of the "
            f"session lifted the halt first), not this request's; nothing is resumed from here: {_NOT_RESUMED}.")
        return 0
    say(f"levain limits: EXTENDED {label} for {root}.")
    try:
        text = L.ruling_text("allow", halt.get("gap"), queue.get(request_id)[0].display)    # type: ignore[index]
        if text is None:
            return 0
        return _resume(queue, agent, root, halt["request_id"] or "", text, say,
                       listing=listing if listing is not None else dv.claude_listing,
                       running=running if running is not None else dv.process_running,
                       launch=launch if launch is not None else dv._launch)
    except KeyboardInterrupt:
        say("levain limits: extended; the resume was interrupted: check whether the session started before resuming "
            "it by hand.")
        return 130
    except Exception as exc:  # noqa: BLE001 - the halt is lifted already: say so, never a traceback
        say(f"levain limits: extended; resume failed: {type(exc).__name__}: {exc}")
        return 0


def run(args: argparse.Namespace, out: Any = None, *, slots: SlotSeams | None = None) -> int:
    """``slots``: where ``--slots`` reads and how it writes (default the host's store and ``sudo``)."""
    out = out if out is not None else sys.stdout

    def say(text: str) -> None:
        print(text, file=out, flush=True)

    sub = args.limits_cmd
    if sub in ("show", "set", "unset") and getattr(args, "slots", None):
        if sub != "show" and args.agent is not None:
            say("levain limits: with --slots the agent is --slots' own value (`levain limits "
                f"{sub} --slots <agent|default> ...`); --agent is not taken")
            return 2
        seams = slots or _host_slot_seams()
        return {"show": _cmd_show_slots, "set": _cmd_set_slots, "unset": _cmd_unset_slots}[sub](args, say, seams)
    if sub in ("set", "unset"):
        if args.replace:
            say("levain limits: --replace is for the slot limits only (`--slots <agent|default>`); nothing changed")
            return 2
        if args.agent is None:
            args.agent = "default"
    if sub == "show":
        return _cmd_show(args, say)
    if sub == "set":
        return _cmd_set(args, say)
    if sub == "unset":
        return _cmd_unset(args, say)
    if sub == "extend":
        try:
            return _cmd_extend(args, say)
        except KeyboardInterrupt:  # the bound for every window the inner handlers do not name: never a traceback
            say("levain limits: interrupted; `levain limits show` says whether the session is still halted, and "
                "`levain consent pending` whether a request was filed.")
            return 130
    say("levain limits: choose show, set, unset or extend; options of `limits` itself come before the command: "
        "`levain limits --queue-dir D <cmd>`")
    return 2


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """``levain limits``, under the top-level subparsers."""
    p = subparsers.add_parser(
        "limits", help="Show or set a session's budgets (tool calls, active hours, tokens, repeats), or extend a "
                       "halted session.",
        description="The per-session budgets Levain counts at every tool call. `show` prints the limits in force and "
                    "each counted session against them; `set` / `unset` edit the operator's defaults; `extend` asks "
                    "for a signed Extend of a session a limit halted, and resumes it on a verified allow.")
    p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                   help="The consent queue's directory (default: $LEVAIN_HOME/consent, else ~/.levain/consent).")
    sp = p.add_subparsers(dest="limits_cmd")
    kinds = list(L.KINDS)
    agents = list(L.AGENTS)
    blocks = [*agents, "default"]
    slots_help = ("The limits SLOT sessions run under (root's L/policy/limits, written through `sudo` and the "
                  "install helper, which asks the password) instead of limits.json; its value is the agent or "
                  "`default`. A slot reads calls, active_hours and repeat, in whole numbers.")
    replace_help = ("With --slots: write a document holding only this change, every other slot limit back to its "
                    "default (the way to replace a file the queue broker refuses, which is never merged).")
    s = sp.add_parser("show", help="Limits in force per agent, and every counted session.")
    s.add_argument("--agent", choices=agents, default=None)
    s.add_argument("--json", action="store_true", help="The cockpit panel's JSON instead of text.")
    s.add_argument("--slots", action="store_true", help="The limits slot sessions run under (L/policy/limits).")
    s = sp.add_parser("set", help="Set a limit in limits.json, or with --slots in the slot limits.")
    s.add_argument("kind", choices=kinds)
    s.add_argument("value")
    s.add_argument("--agent", choices=blocks, default=None, help="The block to set (default: default).")
    s.add_argument("--slots", choices=blocks, default=None, metavar="{" + ",".join(blocks) + "}", help=slots_help)
    s.add_argument("--replace", action="store_true", help=replace_help)
    s = sp.add_parser("unset", help="Remove a limit from limits.json, or with --slots from the slot limits (the "
                                    "default applies again).")
    s.add_argument("kind", choices=kinds)
    s.add_argument("--agent", choices=blocks, default=None, help="The block to unset from (default: default).")
    s.add_argument("--slots", choices=blocks, default=None, metavar="{" + ",".join(blocks) + "}", help=slots_help)
    s.add_argument("--replace", action="store_true", help=replace_help)
    s = sp.add_parser("extend", help="Ask for a signed Extend of a halted session, and resume it on an allow.")
    s.add_argument("session", help="The halted session id.")
    s.add_argument("--agent", choices=agents, default=None)
    s.add_argument("--hold", type=float, default=None,
                   help=f"Seconds to wait for the answer (default {DEFAULT_EXTEND_HOLD:g}; at most "
                        f"{claude_code.GATE_HOLD_CEILING:g}).")
    p.set_defaults(func=run)
