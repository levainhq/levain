"""The seam verbs (C15 S3): ``levain coord close``, ``coord open`` (and ``--ack``) and ``coord handoff show``, over the
writers and readers underneath (:mod:`seam_handoffs`, :mod:`seam_build`, :mod:`seam_open`, :mod:`seam`). Each ``run_*``
takes the parsed arguments, the output stream and the store, prints, and returns the exit code; a refusal is a
:class:`CoordError` the caller (``cli._run_coord``) prints and turns into exit 1, as every other coord verb's.

``coord close --plan`` (the operator-walk checklist) is not here: no read computes the walk's checklist yet, so the flag
does not exist rather than print an invented one.
"""
from __future__ import annotations

import argparse
import json
import secrets
import shlex
from pathlib import Path
from typing import Any

from levain import unshowable
from levain.autonomic.durable_sqlite import read_transaction
from levain.coordinator import lease as _leases
from levain.coordinator import seam_handoffs, seam_open
from levain.coordinator.seam import open_brief, verbose_lines
from levain.coordinator.seam_build import build
from levain.coordinator.store import CoordError, StoreAbsent

__all__ = ["open_kind", "run_close", "run_handoff_show", "run_open"]

_BODY_FILE_MAX = seam_handoffs.MAX_BODY_BYTES * 4      # the file may be loosely written; the writer holds the real cap
_READ_BACK_FILE_MAX = 32768


def _once(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"key {k!r} appears twice")
        seen[k] = v
    return seen


def _emit(out: Any, as_json: bool, obj: Any, text: str) -> None:
    if as_json:
        # ensure_ascii=False keeps text readable, but U+2028, U+2029 and NEL are line breaks to a terminal and a
        # reader of the stream: written as escapes they stay inside their JSON strings
        text = json.dumps(obj, ensure_ascii=False, indent=2)
        for ch in ("\u2028", "\u2029", "\x85"):
            text = text.replace(ch, f"\\u{ord(ch):04x}")
    out.write(text + "\n")


def open_kind(store: Any) -> str:
    """The seam kind a coordinator starting now opens: the latest handoff's kind, or ``light`` when there is none."""
    h = seam_handoffs.latest(store)
    return "light" if h is None else h.seam_kind


def _lv(launch: Any) -> str:
    """How the commands these verbs print spell levain: the launch brief's own spelling, from the one rule
    (:func:`levain.coordinator.launch.coordinator_command`), for the active coordinator launch's directory, else for
    the directory this runs in (a coordinator's session runs its commands from its own install)."""
    from levain.coordinator.launch import coordinator_command

    try:
        return coordinator_command(Path.cwd() if launch is None else launch.cwd)
    except OSError:                  # a directory removed or unreadable: no install to read the rule from
        return coordinator_command(None)


def _fenced(body: str) -> str:
    """The open's body between a fence this print makes up: stored text cannot know the fence, so nothing inside can
    close the block and pass for a line outside it."""
    fence = secrets.token_hex(4)
    return f"~~~ OPEN {fence}\n{body}\n~~~ END OPEN {fence}"


def _tail(store: Any) -> str:
    """The store this read used, spelled as the launch brief spells it: from the store's own directory, so a queue
    directory that came from the environment names the store the ack will run against."""
    return f" --queue-dir {shlex.quote(str(store.directory))}"


def _prev_line(handoff: Any) -> str:
    if handoff is None:
        return "no handoff exists yet: your close names no --prev-handoff-id (omit the flag)"
    return f"your next close names --prev-handoff-id {handoff.handoff_id}"


def run_close(args: argparse.Namespace, out: Any, store: Any) -> int:
    from levain.coordinator.cli import _read_file

    text = _read_file(args.body_file, _BODY_FILE_MAX, "the handoff body")
    try:
        body = json.loads(text, object_pairs_hook=_once)
    except ValueError as exc:
        raise CoordError(f"the handoff body {args.body_file} is not one JSON object with each key once "
                         f"({str(exc)[:120]}); write it with keys {', '.join(seam_handoffs.BODY_KEYS)}") from None
    if not isinstance(body, dict):
        raise CoordError(f"the handoff body {args.body_file} is not one JSON object with each key once (it is "
                         f"JSON {type(body).__name__}); write it with keys {', '.join(seam_handoffs.BODY_KEYS)}")
    h = seam_handoffs.close(store, kind=args.kind, body=body, request_id=args.request_id,
                            prev_handoff_id=args.prev_handoff_id, token=args.token)
    lv = _lv(_active(store, args.token)[0])
    _emit(out, args.json, {"handoff_id": h.handoff_id, "kind": h.seam_kind, "writer": h.writer, "gseq": h.gseq,
                           "covers_gseq": h.covers_gseq, "prev_handoff_id": h.prev_handoff_id},
          f"closed: handoff {h.handoff_id} ({h.seam_kind}) written by {h.writer}, covering counted rows through gseq "
          f"{h.covers_gseq}\nthe next coordinator opens from it ({lv} coord open); a retry of this close names "
          f"request id {h.close_key!r} again")
    return 0


def _active(store: Any, token: Any) -> tuple[Any, str]:
    """``(launch, why)``: the active coordinator launch holding the live lease under ``token``, or ``(None, why)``. A
    read: nothing is written, so the answer is only what the printing process may assume (a write re-fences)."""
    if token is None:
        return None, "no --token was given"
    try:
        with read_transaction(store.open_readonly) as conn:
            now = store._stamp()
            held = _leases.lease(conn, now)
            if held is None or held.token != token or not held.live:
                return None, f"token {token} is not a live coordinator lease's"
            if held.holder == "operator":
                return None, "the lease is the operator's (`coord take`), and an operator token never acknowledges"
            ln = _leases._launch_by_key(conn, held.holder)
            if ln is None or ln.role != "coordinator" or ln.state != "active":
                return None, f"the lease holder {held.holder} is not an active coordinator launch"
            return ln, ""
    except StoreAbsent:
        return None, "there is no coordinator store here"


def _own_launch_build(store: Any, launch: Any) -> Any:
    """The build the cold start made for this launch (signal ``launch``), or None."""
    from contextlib import closing

    with closing(store._connect()) as conn:
        if not seam_open.open_tables_present(conn):
            return None
        row = conn.execute("SELECT seam_kind FROM open_builds WHERE signal_kind = 'launch' AND signal_key = ?",
                           (launch.launch_id,)).fetchone()
        return None if row is None else seam_open.stored_build(conn, row[0], "launch", launch.launch_id)


def _readonly_text(store: Any, args: argparse.Namespace) -> tuple[str, Any]:
    brief = open_brief(store, args.queue_dir, open_kind(store), store._clock())
    return brief.text, brief


def run_open(args: argparse.Namespace, out: Any, store: Any) -> int:
    if args.ack is not None:
        return _ack(args, out, store)
    if args.read_back_file is not None:
        raise CoordError("--read-back-file goes with --ack OPEN_ID: it is the read-back an acknowledgement carries")
    launch, why = _active(store, args.token)
    tail = _tail(store)
    handoff = seam_handoffs.latest(store)
    built = None
    note = ""
    if launch is not None:
        built = _own_launch_build(store, launch)
        if built is None and handoff is not None and handoff.writer == launch.session_key:
            note = (f"a coordinator cannot acknowledge its own handoff (this launch wrote handoff {handoff.handoff_id}): "
                    "printed read-only; the coordinator after you opens it")
        elif built is None and handoff is not None and handoff.seam_kind == "rotation":
            built = build(store, kind="rotation", signal_kind="rotation", signal_key=str(handoff.handoff_id),
                          queue_dir=args.queue_dir, token=args.token)
        if built is None and not note:
            note = ("no open is built for this launch and the latest handoff is "
                    f"{'absent' if handoff is None else handoff.seam_kind + ' (its builder is a later slice)'}: "
                    "printed read-only, so it cannot be acknowledged")
    else:
        note = f"printed read-only and cannot be acknowledged ({why})"
    verbose = verbose_lines() if getattr(args, "verbose", False) else None
    extra = "".join(f"\n{ln}" for ln in verbose or ())
    more_json = {} if verbose is None else {"verbose": verbose}
    if built is None:
        text, brief = _readonly_text(store, args)
        _emit(out, args.json, {"open_id": None, "acknowledgeable": False, "why": note, "body": text,
                               "prev_handoff_id": None if handoff is None else handoff.handoff_id, **more_json},
              f"{_fenced(text)}{extra}\n\n--\n{note}\n{_prev_line(handoff)}")
        if args.token is not None and launch is None:
            # the operator's plain read (no --token) is not a refusal; a token given and not honoured is one
            raise CoordError(f"--token {args.token} was refused: {why}; the open above is read-only and cannot be "
                             "acknowledged")
        return 0
    seam_open.record_delivery(store, built.open_id, "light_verb", recipient=launch.launch_id)
    needs = " --read-back-file PATH" if built.seam_kind in ("rotation", "dream") else ""
    ack = f"{_lv(launch)} coord open --ack {built.open_id}{needs} --token {args.token}{tail}"
    more = ("; PATH holds your restatement of the handoff in your own words (required on a "
            f"{built.seam_kind} open)") if needs else ""
    _emit(out, args.json, {"open_id": built.open_id, "kind": built.seam_kind, "acknowledgeable": True, "body": built.body,
                           "ack": ack, "prev_handoff_id": None if handoff is None else handoff.handoff_id,
                           **more_json},
          f"{_fenced(built.body)}{extra}\n\n--\nopen {built.open_id} ({built.seam_kind}), built {built.built_at}; it "
          "is the open stored for you, so running coord open again prints it unchanged\n"
          f"acknowledge it, which moves the Since window past what it showed: {ack}{more}\n{_prev_line(handoff)}")
    return 0


def _ack(args: argparse.Namespace, out: Any, store: Any) -> int:
    from levain.coordinator.cli import _read_file

    if args.token is None:
        raise CoordError("an acknowledgement is the coordinator's: add --token N (the lease token `coord claim` "
                         "printed); nothing was written")
    read_back = None if args.read_back_file is None else _read_file(args.read_back_file, _READ_BACK_FILE_MAX,
                                                                    "the read-back")
    try:
        open_id, session, newly = seam_open.acknowledge(store, args.ack, token=args.token, read_back=read_back)
    except CoordError as exc:
        if read_back is None and "read-back" in str(exc):
            raise CoordError(f"{exc}; write your restatement to a file and add --read-back-file PATH") from None
        raise
    with read_transaction(store.open_readonly) as conn:
        cursor, _held_by = seam_open.acked_cursor(conn)
    _emit(out, args.json, {"open_id": open_id, "reader_session": session, "newly": newly, "cursor_gseq": cursor},
          (f"acknowledged open {open_id} as {session}: the Since window now starts after gseq {cursor}" if newly else
           f"open {open_id} was already acknowledged by {session}; nothing changed (the window starts after gseq "
           f"{cursor})"))
    return 0


def _marked(value: Any) -> Any:
    """``value`` with every string in it (keys of nothing: the body's own strings) marked, recursively: a stored
    newline, escape or line separator shows as a ``⟨U+XXXX⟩`` mark and can never start a line of its own."""
    if isinstance(value, str):
        return unshowable.mark(value, allow="")
    if isinstance(value, list):
        return [_marked(v) for v in value]
    if isinstance(value, dict):
        return {unshowable.mark(str(k), allow=""): _marked(v) for k, v in value.items()}
    return value


class _Shown:
    """A handoff row's printed fields, every stored string marked."""

    def __init__(self, h: Any) -> None:
        self.handoff_id = h.handoff_id
        self.seam_kind = _marked(h.seam_kind)
        self.written_at = _marked(h.written_at)
        self.writer = _marked(h.writer)
        self.covers_gseq = h.covers_gseq
        self.prev_handoff_id = h.prev_handoff_id


def _render_md(h: Any, body: dict[str, Any]) -> str:
    L = [f"# Handoff {h.handoff_id} ({h.seam_kind})", "",
         f"- written {h.written_at} by {h.writer}; covers counted rows through gseq {h.covers_gseq}",
         f"- previous handoff: {h.prev_handoff_id if h.prev_handoff_id is not None else 'none'}",
         f"- attested: operator present {body['attested_operator_present']}, walked {body['attested_walked']}", "",
         "## State", "", body["state_line"], "", "## In flight", ""]
    L += [f"- {e['item']} (owner {e['owner']}): {e['why']}" for e in body["in_flight"]] or ["(none)"]
    L += ["", "## Next", "", body["next"] or "(none)", "", "## Contingencies", ""]
    L += [f"- if {e['if']}: {e['then']}" for e in body["contingencies"]] or ["(none)"]
    L += ["", "## Notes", "", body["notes"] or "(none)"]
    return "\n".join(L)


def _render_plain(h: Any, body: dict[str, Any]) -> str:
    L = [f"handoff {h.handoff_id} ({h.seam_kind}), written {h.written_at} by {h.writer}",
         f"covers counted rows through gseq {h.covers_gseq}; previous handoff "
         f"{h.prev_handoff_id if h.prev_handoff_id is not None else 'none'}",
         f"attested: operator present {body['attested_operator_present']}, walked {body['attested_walked']}",
         f"state: {body['state_line']}", "in flight:"]
    L += [f"  - {e['item']} (owner {e['owner']}): {e['why']}" for e in body["in_flight"]] or ["  (none)"]
    L += [f"next: {body['next'] or '(none)'}", "contingencies:"]
    L += [f"  - if {e['if']}: {e['then']}" for e in body["contingencies"]] or ["  (none)"]
    L += [f"notes: {body['notes'] or '(none)'}"]
    return "\n".join(L)


def run_handoff_show(args: argparse.Namespace, out: Any, store: Any) -> int:
    h = seam_handoffs.latest(store) if args.handoff_id is None else seam_handoffs.get(store, args.handoff_id)
    if h is None:
        lv = _lv(None)
        raise CoordError(f"no handoff exists yet (a coordinator writes one with `{lv} coord close`)"
                         if args.handoff_id is None else
                         f"there is no handoff {args.handoff_id} (`{lv} coord handoff show` shows the latest)")
    render = _render_md if args.md else _render_plain
    try:
        body = json.loads(h.body)
        if not isinstance(body, dict):
            raise ValueError(f"the body is JSON {type(body).__name__}, not an object")
        # rendered whatever the format, so a body the text views cannot read is refused in --json too
        text = render(_Shown(h), _marked(body))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        reason = f"missing key {exc}" if isinstance(exc, KeyError) else f"{type(exc).__name__}: {exc}"
        raise CoordError(f"handoff {h.handoff_id} body is unreadable: "
                         f"{unshowable.mark(reason, allow='')[:160]}") from None
    _emit(out, args.json, {"handoff_id": h.handoff_id, "kind": h.seam_kind, "written_at": h.written_at,
                           "writer": h.writer, "covers_gseq": h.covers_gseq, "prev_handoff_id": h.prev_handoff_id,
                           "body": body}, text)
    return 0
