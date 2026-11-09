"""levain.coordinator.cli — ``levain drop`` (drop words for the coordinator and go) and ``levain thread`` (the threads
your drops became: list, show, and the one-tap fixes as commands).

``levain drop "words"`` stores the words verbatim and prints the drop receipt. Run inside a git work tree on a branch,
it records that repo and branch as where the drop was made from (C0 §3.1: a CLI drop's source is the branch checked out
where you typed it); a detached HEAD or no repo records none. Words come from the arguments, or from stdin with ``-``.
``levain drops split`` is the coordinator's door: it names the threads a drop becomes; ``levain drops list|show`` read
them back.

``levain envelope`` (echo, sign, accept, show, list, kill, narrow) and ``levain drop --envelope`` are the operator's own
door to a thread envelope: what the desk may do for a thread, signed by you (y/N on your terminal, or ``--yes``).
Exit codes follow C1's convention: 0 done, 1 refused or faulted (a store that would not answer, a signer that could
not run included), the refusal on stderr; a drop whose envelope was refused still keeps its words and exits 1.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain import unshowable
from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.contracts.canon import canonical
from levain.coordinator import envelope as env
from levain.coordinator.door import door_ok, not_door
from levain.coordinator.envelope_draft import MIN_LEFT as _MIN_LEFT
from levain.coordinator.envelope_draft import empty_base
from levain.coordinator.envelopes import EnvelopeStore, seen_past
from levain.coordinator.store import (KINDS, STATES, WORDS_MAX, CoordError, Drop, Part, Source, Thread, check_source,
                                      check_words, words_sha256)
from levain.dispatch.jobs import JobStoreError

# what a store call can refuse with: the grammar's refusal, an untrustworthy store, a database it cannot use
_REFUSED = (CoordError, JobStoreError, sqlite3.Error, OSError)

__all__ = ["add_parsers", "cwd_source"]

_GIT_TIMEOUT = 10


def _git(cwd: str, *args: str) -> str | None:
    """One git call in ``cwd`` with the dispatch store's hardened environment; ``None`` when git fails or is absent
    (a drop's source is a convenience: no repo is ``none``, never an error)."""
    from levain.dispatch.contract import git_env

    try:
        cp = subprocess.run(["git", "--no-replace-objects", "-C", cwd, *args], capture_output=True,
                            timeout=_GIT_TIMEOUT, env=git_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    out = cp.stdout.decode("utf-8", "surrogateescape")
    return out[:-1] if out.endswith("\n") else out          # git's terminator only: a path may end in a space


def cwd_source(cwd: str | None = None) -> Source:
    """Where a CLI drop is made from: the work tree's top, the checked-out branch and its head; ``Source()`` (none)
    outside a repo or on a detached HEAD."""
    cwd = cwd or os.getcwd()
    top = _git(cwd, "rev-parse", "--show-toplevel")          # already absolute; one line or it is not trusted
    ref = _git(cwd, "symbolic-ref", "-q", "HEAD")
    if not top or "\n" in top or not top.startswith("/") or not ref or not ref.startswith("refs/heads/"):
        return Source()
    # the head is read from the branch just named, never from HEAD again: a checkout in between cannot pair one
    # branch with another's commit
    head = _git(cwd, "rev-parse", "--verify", "-q", "--end-of-options", f"{ref}^{{commit}}")
    if not head:
        return Source()
    source = Source("cwd", os.path.realpath(top), ref, head)
    try:
        return check_source(source)
    except CoordError:
        return Source()          # a path the store cannot hold (not UTF-8, a hidden character) is recorded as none


def _store(args: argparse.Namespace) -> EnvelopeStore:
    d = args.queue_dir.expanduser() if args.queue_dir is not None else None
    return EnvelopeStore(d)


def _board_store(args: argparse.Namespace) -> EnvelopeStore:
    """The store a board verb opens (``coord import-status``, ``coord spawn --item``): ``--queue-dir`` when given, else
    :func:`levain.coordinator.work_attach.board_store_dir`, the directory ``coord report`` queues into. Never the
    consent directory by default: board rows do not go into the live ``dispatch.db``."""
    from levain.coordinator import work_attach

    d = args.queue_dir.expanduser() if args.queue_dir is not None else work_attach.board_store_dir()
    return EnvelopeStore(d)


def _opens_board(args: argparse.Namespace) -> bool:
    """True for the verbs whose default store is the board store. A spawn with ``--item`` writes its launch and the
    item's attach in one transaction, so the launch is in the board store too."""
    cmd = args.coord_cmd
    return cmd == "import-status" or (cmd == "spawn" and getattr(args, "item", None) is not None)


NO_STORE = "no coordinator store here"


def _no_store(store: EnvelopeStore) -> bool:
    """True when no coordinator store is here (:meth:`CoordStore.coordinator_present`, which opens nothing for
    writing): a read-only verb says so and makes nothing (no directory, no dispatch.db, no awake.lock, no tables in a
    dispatch-only file)."""
    return not store.coordinator_present()


def _drop_row(d: Drop) -> dict[str, Any]:
    return {"drop_id": d.drop_id, "surface": d.surface, "outbox_id": d.outbox_id, "words": d.words,
            "words_sha256": d.words_sha256, "sent_at": d.sent_at, "received_at": d.received_at,
            "source": {"how": d.source.how, "repo": d.source.repo, "ref": d.source.ref, "head": d.source.head}}


def _thread_row(t: Thread) -> dict[str, Any]:
    return {"thread_id": t.thread_id, "kind": t.kind, "title": t.title, "state": t.state, "about": t.about,
            "repo": t.repo, "rev": t.rev, "members": list(t.members), "members_sha256": t.members_sha256,
            "created_at": t.created_at}


def _where(d: Drop) -> str:
    if d.source.how == "none":
        return "no repo or branch recorded"
    return f"from {d.source.repo} on {d.source.ref.removeprefix('refs/heads/')}"


def _thread_line(t: Thread) -> str:
    about = f" (about {t.about})" if t.about else ""
    return f"  {t.thread_id}  {t.kind.upper():5} {t.state:7} rev {t.rev}  {t.title}{about}"


def _shown(words: str) -> str:
    """Words for a terminal: quoted, with every character that would not show as itself (a control, a bidi override)
    marked by its code point. The stored words stay verbatim; this is only what a text listing prints."""
    return json.dumps(unshowable.mark(words), ensure_ascii=False)


def _emit(out: Any, as_json: bool, obj: Any, text: str) -> None:
    out.write((json.dumps(obj, ensure_ascii=False, indent=2) if as_json else text) + "\n")


def _read_words(args: argparse.Namespace) -> str:
    """The words: one argument kept as typed, several joined by one space, or ``-`` alone for stdin (strict UTF-8, one
    trailing newline dropped, so ``echo words | levain drop -`` stores what ``levain drop words`` does)."""
    if args.words == ["-"]:
        cap = WORDS_MAX * 4 + 2          # the most bytes WORDS_MAX characters can take, and a newline
        raw = sys.stdin.buffer.read(cap + 1)
        if len(raw) > cap:
            raise CoordError(f"a drop is at most {WORDS_MAX} characters; stdin holds more")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise CoordError("stdin is not UTF-8 text") from None
        return text[:-1] if text.endswith("\n") else text
    return " ".join(args.words)


def _read_input(path: Path | None) -> str:
    """All of a file (``path``) or of stdin (``None``) as strict UTF-8 text, read under the byte cap a WORDS_MAX-character
    text can take (the verb's own validator then applies its own, smaller limit), one trailing newline dropped, so
    ``echo why | levain ... --stdin`` gives the same text as ``--why why``."""
    cap = WORDS_MAX * 4 + 2
    if path is None:
        raw = sys.stdin.buffer.read(cap + 1)
    else:
        with open(path, "rb") as fh:
            raw = fh.read(cap + 1)
    if len(raw) > cap:
        raise CoordError(f"{'stdin' if path is None else str(path)} holds more than {WORDS_MAX} characters")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CoordError(f"{'stdin' if path is None else str(path)} is not UTF-8 text") from None
    if text.endswith("\r\n"):
        return text[:-2]
    return text[:-1] if text.endswith("\n") else text


def _text_input(args: argparse.Namespace, inline: str | None, flag: str) -> str | None:
    """A verb's one free-text value: the inline ``flag`` as typed, or ``--text-file PATH`` / ``--stdin`` (no shell
    quoting). Naming two of the three is refused; naming none returns ``None`` (the caller says what is missing)."""
    path = getattr(args, "text_file", None)
    use_stdin = bool(getattr(args, "stdin", False))
    given = [n for n, v in ((flag, inline is not None), ("--text-file", path is not None), ("--stdin", use_stdin)) if v]
    if len(given) > 1:
        raise CoordError(f"name the text once: {', '.join(given)} were all given")
    if path is not None:
        return _read_input(path)
    if use_stdin:
        return _read_input(None)
    return inline


def _text_flags(p: argparse.ArgumentParser | Any) -> None:
    """``--text-file`` and ``--stdin``: the quoting-free spellings of a verb's free text (a group or a parser)."""
    p.add_argument("--text-file", dest="text_file", type=Path, default=None, metavar="PATH",
                   help="Read the text from this file (no shell quoting).")
    p.add_argument("--stdin", action="store_true", help="Read the text from stdin (no shell quoting).")


def run_drop(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        store = _store(args)
        words = _read_words(args)
        source = Source() if args.no_source else cwd_source()
        if args.envelope is not None:
            return _drop_with_envelope(args, out, store, words, source)
        drop, created = store.accept_drop(words, surface="cli", outbox_id=args.outbox_id, source=source)
    except _faults() as exc:
        print(f"levain drop: {exc}", file=sys.stderr)
        return 1
    state = "delivered" if created else "already delivered (resend, nothing written)"
    _emit(out, args.json, {**_drop_row(drop), "created": created},
          f"drop receipt: {drop.drop_id} {state} at {drop.received_at}, {_where(drop)}")
    _launch_woken(store, drop)
    return 0


def _launch_woken(store: EnvelopeStore, drop: Drop) -> None:
    """Start the coordinator launch this drop's own transaction asked for, if it asked for one, and say how it went on
    stderr. The drop's exit is never changed by it: the drop was delivered either way."""
    launch_id = store.woken_launch(drop.drop_id)
    if launch_id is None:
        return
    from levain.coordinator.launch import launch_session

    try:
        got = launch_session(store, launch_id)
    except _REFUSED as exc:
        print(f"coordinator launch {launch_id}: not started ({exc})", file=sys.stderr)
        return
    print(_outcome_text(got), file=sys.stderr)


def _outcome_text(got: Any) -> str:
    if got.started:
        return f"coordinator launch {got.launch_id} started: {got.attach}"
    text = f"coordinator launch {got.launch_id}: not started: {got.refusal}"
    if got.remedy:
        text += (f"\nrun this once and accept the trust prompt:\n  {got.remedy}\nthen the next drop starts a "
                 "coordinator, or levain coord wake starts one, once the wait levain coord status shows is over")
    return text


# --- thread envelopes (C2b): the operator's own door to the one authority the desk adds -----------------------
def _faults() -> tuple[type[BaseException], ...]:
    """What an envelope command refuses with: the store's, a body's shape, the dispatch command line's own refusals, a
    log or signer that would not answer. Lazy: the dispatch CLI is only loaded when an envelope command runs."""
    from levain.authority import AuthorityError, VerifierUnavailable
    from levain.dispatch import DispatchError
    from levain.dispatch.cli import SignerUnavailable

    return (*_REFUSED, env.EnvelopeError, DispatchError, AuthorityError, VerifierUnavailable, SignerUnavailable,
            ValueError)


def _tick() -> datetime:
    return datetime.now(timezone.utc)


_DURATION = re.compile(r"(?:([0-9]{1,3})h)?(?:([0-9]{1,4})m)?")


def _parse_stop(text: str) -> timedelta:
    m = _DURATION.fullmatch(text.strip())
    if m is None or not (m.group(1) or m.group(2)):
        raise CoordError(f"--stop is a duration like 2h, 90m or 1h30m (got {text[:40]!r})")
    span = timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
    if span <= timedelta(0) or span > env_lifetime():
        raise CoordError(f"--stop is more than 0 and at most {int(env_lifetime().total_seconds() // 3600)}h "
                         f"(got {text[:40]!r})")
    return span


def _parse_stop_earlier(text: str) -> str:
    """``--stop-earlier`` as the store's stamp: a duration from now (``30m``, ``1h``) or a time with its zone
    (``2026-10-20T23:06:36Z``, ``...-04:00``)."""
    m = _DURATION.fullmatch(text.strip())
    if m is not None and (m.group(1) or m.group(2)):
        return utc_stamp(_tick().replace(microsecond=0) + timedelta(hours=int(m.group(1) or 0),
                                                                     minutes=int(m.group(2) or 0)))
    try:
        at = datetime.fromisoformat(text.strip())
    except ValueError:
        at = None
    if at is None or at.tzinfo is None:
        raise CoordError(f"--stop-earlier is a duration from now like 30m, or a time with its zone like "
                         f"2026-10-20T23:06:36Z (got {text[:40]!r})")
    try:
        return utc_stamp(at.astimezone(timezone.utc))
    except (OverflowError, ValueError):
        raise CoordError(f"--stop-earlier is a time this machine cannot place (got {text[:40]!r})") from None


def env_lifetime() -> timedelta:
    from levain.dispatch.contract import MAX_LIFETIME

    return MAX_LIFETIME


def _parse_budget(text: str | None) -> dict[str, int]:
    out = {k: 0 for k in env.BUDGET_KEYS}
    if text is None:
        return out
    for item in text.split(","):
        key, sep, val = item.partition("=")
        key = key.strip()
        if not sep or key not in out or not (val.strip().isascii() and val.strip().isdigit()):
            raise CoordError(f"--budget is {','.join(k + '=N' for k in env.BUDGET_KEYS)}, any of them (got "
                             f"{item[:40]!r})")
        out[key] = int(val)
    return out


def _named(text: str) -> set[str]:
    return {item.partition("=")[0].strip() for item in text.split(",")}


def _echo_budget(text: str | None) -> dict[str, int]:
    """``echo --budget``: parsed as :func:`_parse_budget`, and refused when it leaves out a key the gauge measures
    (head ruling C6-3): a key left out reads 0, and 0 stops the first worker, so a partial budget is never read as
    meant. Name the key with 0 to mean 0; the signed meaning of 0 is unchanged. No ``--budget`` at all is the
    all-zero budget the sign view states."""
    from levain.coordinator.budget import METERS

    out = _parse_budget(text)
    if text is not None:
        missing = [k for k in METERS if k not in _named(text)]
        if missing:
            raise CoordError(f"--budget leaves out {', '.join(missing)}, which the budget gauge measures; a key left "
                             "out would be 0, and 0 stops the first worker. Name each one (K=0 means none): "
                             f"{','.join(k + '=N' for k in METERS)}")
    return out


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _parse_where(specs: list[str]) -> list[dict[str, Any]]:
    out = []
    for spec in specs:
        repo, sep, paths = spec.partition("=")
        listed = [p for p in paths.split(",") if p]
        if not sep or not repo or not listed:
            raise CoordError("--where is REPO=PATH,PATH; the whole repo is REPO=. written out, never left off "
                             f"(got {spec[:80]!r})")
        out.append({"repo": _real(repo), "paths": listed})
    return out


def _parse_land(spec: str) -> dict[str, Any]:
    repo, sep, onto = spec.rpartition("=")
    if not sep or not repo or not onto:
        raise CoordError(f"--land is REPO=ONTO, the branch it may land onto (got {spec[:80]!r})")
    return {"repo": _real(repo), "onto": onto, "from": "named", "onto_from": "inferred", "criteria": []}


def _money(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"


# --- plain words: what a person reads before signing -------------------------------------------------------------------
NOT_IN_FORCE_PLAIN = ("NOT IN FORCE YET: until Levain's separate-user setup is in place, signing this lets nothing run "
                      "by itself; every land, push and publish still asks you separately.")
_BOUND = "bound (not in force yet)"


def _clock(dt: datetime) -> str:
    """A moment in the system's local time and in UTC: ``7:42 PM EDT, 23:42 UTC``."""
    local = dt.astimezone()
    return f"{local.strftime('%I:%M %p %Z').lstrip('0')}, {dt.astimezone(timezone.utc).strftime('%H:%M')} UTC"


def _span(delta: timedelta) -> str:
    mins = max(int(delta.total_seconds() // 60), 0)
    return f"{mins // 60}h {mins % 60}m"


def _stops(stamp: str, now: datetime) -> str:
    stop = parse_stamp(stamp)
    return f"stops in {_span(stop - now)} ({_clock(stop)})" if stop > now else f"stopped {_clock(stop)}"


def _threads_line(store: EnvelopeStore | None, thread_id: str, rev: int | None) -> str:
    title = ""
    if store is not None:
        try:
            t = store.thread(thread_id)
            title = f"  {t.kind.upper()} {t.title}  (from the store, not signed)"
        except _REFUSED:
            pass
    return f"    {thread_id} rev {rev if rev is not None else '?'}{title}"


def _effect_text(kind: str, e: dict[str, Any], stop_at: str | None = None) -> str:
    if kind == "launches":
        until = "" if stop_at is None else " until " + parse_stamp(stop_at).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        return f"may launch up to {e['count']} seats in {e['repo']}{until}"
    if kind == "lands":
        crit = ", ".join(json.dumps(c, sort_keys=True) for c in e["criteria"])
        # onto_from is signed: an inferred branch says so in the words read before signing (C0 §1, his 10-20 item 1)
        src = "the branch the drop was made from" if e["from"] == "source" else \
            "inferred, not where you dropped from" if e["onto_from"] == "inferred" else "named"
        return f"{e['repo']}  onto {e['onto']}  ({src}; {'criteria: ' + crit if crit else 'may land with no conditions'})"
    if kind == "pushes":
        return f"{e['repo']}  {e['remote']} {e['url']}  branch {e['branch']}"
    return f"{e['package']} {e['version']}  to {e['index']}"


_EFFECT_HEAD = {"lands": "lands (merges) it may do:", "pushes": "pushes it may do:",
                "publishes": "publishes it may do:", "launches": "launches (seats) it may do:"}


def _effects(body: dict[str, Any], old: dict[str, Any] | None) -> list[str]:
    """The effect lists; against ``old`` (the envelope this supersedes) each line is marked + added, = unchanged,
    ~ changed (same target, other criteria), and what ``old`` held and this does not is - removed."""
    L: list[str] = []
    for kind in env.GRANT_LISTS:
        if kind == env.LAUNCH_LIST and not body.get(kind) and not (old is not None and old.get(kind)):
            continue          # no grant, no line: a no-launch envelope reads as it always did
        L.append("  " + _EFFECT_HEAD[kind])
        before = {env.effect_key(kind, e): e for e in old.get(kind, [])} if old is not None else {}
        now_keys = set()
        for e in body.get(kind, []):
            key = env.effect_key(kind, e)
            now_keys.add(key)
            text = _effect_text(kind, e, body["stop_at"])
            # unchanged means the line READ is the same (a launch line carries its stop, so a moved stop changed it)
            mark = "" if old is None else "+ added: " if key not in before else \
                "= unchanged: " if before[key] == e and _effect_text(kind, before[key], old.get("stop_at")) == text \
                else "~ changed: "
            L.append(f"    {mark}{text}")
        for key, e in before.items():
            if key not in now_keys:
                L.append(f"    - removed: {_effect_text(kind, e, old.get('stop_at'))}")
        if not body.get(kind) and not any(k not in now_keys for k in before):
            L.append("    none")
    return L


_wider = env.widens          # the pure rule moved to envelope.py; this name stays for the call sites


def _draft_line(body: dict[str, Any]) -> str:
    """Whether a conversation draft sits behind this body. ``.get``: a stored /1 body has no ``draft`` key."""
    d = body.get("draft")
    if d is None:
        return "  draft: none: no draft reviewed, you composed this body yourself"
    return f"  draft: {d['draft_id']}#{d['revision']} (digest ...{d['digest'][-8:]})"


def _describe(body: dict[str, Any], store: EnvelopeStore | None = None, *, binds: bool = True,
              old: dict[str, Any] | None = None, now: datetime | None = None, ended: bool = False) -> list[str]:
    """An envelope body as the lines a human signs: where, each effect, the budget, the stop. One function for the
    echo, the sign view, the drop view and show, so what is signed is what was read."""
    now = now or _tick()
    L = [f"  origin: {body['origin']}", _draft_line(body), "  where it may work:"]
    for w in body["where"]:
        paths = ", ".join("the WHOLE repo (every file)" if p == "." else p for p in w["paths"])
        L.append(f"    {w['repo']}  paths: {paths}")
    if not body["where"]:
        L.append("    nowhere")
    L += _effects(body, old)
    b = body["budget"]
    L.append("  budget (the budget is a gauge: crossing it stops new steps, it is not a hard stop):")
    def _allowed(v: Any, shown: str) -> str:
        return f"{shown} allowed" if v else "0 allowed (none named: new steps stop at once)"
    L += [f"    tool calls: {_allowed(b['tool_calls'], str(b['tool_calls']))}",
          f"    tokens: {_allowed(b['tokens'], str(b['tokens']))}",
          f"    dollars: {_money(b['dollars_cents'])} (not metered: Levain counts no dollars, so this never stops a step)",
          f"    hours: {_allowed(b['hours'], str(b['hours']))}"]
    stop = (f"was to stop {_clock(parse_stamp(body['stop_at']))}" if ended else _stops(body['stop_at'], now))
    L.append(f"  issued {_clock(parse_stamp(body['issued_at']))}; {stop}")
    sup = body["supersedes"]
    if sup is not None:
        L.append(f"  supersedes {sup['envelope_id']}")
        if sup.get("seen") is not None:          # a stored /1 body has no horizon
            L.append(f"  as of store event {sup['seen']}; any narrowing of {sup['envelope_id']} after event "
                     f"{sup['seen']} refuses this body")
    if binds:
        if body["binds"]:
            L.append("  threads it binds:")
            L += [_threads_line(store, b_["thread_id"], b_["rev"]) for b_ in body["binds"]]
        elif body["origin"] == "structured":
            L.append("  threads it binds: its own drop's thread, once the drop is split into exactly one")
    return L


def _request_line(body: dict[str, Any], store: EnvelopeStore, words: str | None) -> str:
    """What the envelope is for: the drop's words, read from the store and held to the hash the body signs."""
    if words is None:
        try:
            drop = store.drop(body["drop_id"])
        except _REFUSED:
            return "  for your words: (that drop is not in this store)"
        if drop.words_sha256 != body["words_sha256"]:
            raise CoordError(f"drop {drop.drop_id} holds other words than this envelope names; echo it again")
        words = drop.words
    return f"  for your words: {_shown(words[:120])}"


def _bases(body: dict[str, Any], basis: Any, authority: Any, ssh_keygen: Any = None
           ) -> list[tuple[Any, dict[str, Any], list[str], list[str]]]:
    """Per bound thread, what the body is read against: ``(head, base, thread_ids, notes)``, one entry per distinct base
    (threads whose bases are canonically equal share one), all from the one read of ``basis``
    (:meth:`EnvelopeStore.sign_basis`). The base is the head AS NARROWED for that thread (what authority the thread
    holds now), and the empty envelope, budget zeroed, when the head is killed or ended for it, or when ``authority``
    is given and the head's own signature no longer verifies under it: every value is then a new grant."""
    head = None if basis is None else basis.head
    if head is None:
        return [(None, None, [], [])]
    out: list[tuple[Any, dict[str, Any], list[str], list[str]]] = []
    for b in body["binds"]:
        t = b["thread_id"]
        tb = basis.threads[t]
        base, note = tb.effective, ""
        if tb.end is not None:
            base = empty_base(head.body)
            note = f"envelope {head.envelope_id} was {'killed' if tb.end[0] == 'kill' else 'ended (its thread is done)'} "\
                   f"for thread {t}: every value below is a new grant"
        elif authority is not None:
            why = env.refusal(head.payload, head.signature, authority, head.body, ssh_keygen=ssh_keygen)
            if why is not None:
                base = empty_base(head.body)
                note = (f"envelope {head.envelope_id} no longer holds for thread {t}: the key that signed it, "
                        f"{head.signer_fp}, is not valid now ({why}); every value below is a new grant")
        for entry in out:
            if canonical(entry[1]) == canonical(base):
                entry[2].append(t)
                entry[3].append(note)
                break
        else:
            out.append((head, base, [t], [note]))
    return out or [(None, None, [], [])]


def _member_lines(basis: Any, thread_id: str) -> tuple[list[str], bool]:
    """The drops a thread gained or lost between the revision the head was signed for and the one this body binds."""
    tb = basis.threads[thread_id]
    lines = [f"  thread {thread_id} gained drops: {', '.join(tb.added)}" if tb.added else "",
             f"  thread {thread_id} lost drops: {', '.join(tb.removed)}" if tb.removed else ""]
    return [x for x in lines if x], bool(tb.added)


def _view(body: dict[str, Any], store: EnvelopeStore, *, words: str | None = None, authority: Any = None,
          basis: Any = None) -> str:
    """What is shown before a signature. A body that supersedes is read PER THREAD against the head as narrowed for
    that thread (never the head's signed body, which a kill or a drop would have made wider than what is in force);
    sign and accept pass their ``authority`` snapshot, so a head whose key no longer holds is read as granting
    nothing, and the ``basis`` their refusal was decided on, so the view and the refusal read one store point. Echo
    and extend pass none, and what they print is not the signing view."""
    if basis is None and body["supersedes"] is not None:          # a body that supersedes nothing reads no base
        basis = store.sign_basis(body)
    L = [f"envelope {body['envelope_id']}  digest {env.envelope_digest(body)}", NOT_IN_FORCE_PLAIN,
         _request_line(body, store, words)]
    sections = _bases(body, basis, authority, store._ssh_keygen)
    for i, (head, base, threads, notes) in enumerate(sections):
        if head is not None and len(sections) > 1:
            L.append(f"read against {head.envelope_id} as narrowed for thread{'s' if len(threads) > 1 else ''} "
                     f"{', '.join(threads)}:")
        L += _describe(body, store, old=base if head is not None else None, binds=i == 0)
        L += list(dict.fromkeys(n for n in notes if n))
        if head is None:
            continue
        for t in threads:
            members, gained = _member_lines(basis, t)
            L += members
            L.append(f"WIDER than {head.envelope_id} for thread {t}: that is why it needs your signature"
                     if env.widens(base, body) or gained else f"not wider than {head.envelope_id} for thread {t}")
    L.append("if Levain later acts on it, it may do only this, nothing outside it.")
    return "\n".join(L)


def _read_file(path: Path, high: int, what: str) -> str:
    try:
        raw = path.expanduser().read_bytes()
    except OSError as exc:
        raise CoordError(f"{what}: cannot read {path}: {exc.strerror or exc}") from None
    if len(raw) > high:
        raise CoordError(f"{what}: {path} is over {high} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CoordError(f"{what}: {path} is not UTF-8 text") from None


def _body_from_file(path: Path) -> tuple[str, dict[str, Any]]:
    text = _read_file(path, env.BODY_MAX + 2, "the envelope body")
    text = text[:-1] if text.endswith("\n") else text          # the one newline a file ends with
    return text, env.parse_body(text)


def _snapshot(args: argparse.Namespace, store: EnvelopeStore) -> Any:
    from levain.dispatch.cli import _authority_source

    return _authority_source(store.directory, args.data_root)()


def _head_reader(args: argparse.Namespace, store: EnvelopeStore) -> Any:
    """The same authority source's head reader (``AuthoritySource.head``): the log's stored head, no replay."""
    from levain.dispatch.cli import _authority_source

    return _authority_source(store.directory, args.data_root).head


def _need_terminal(args: argparse.Namespace, what: str) -> None:
    from levain.dispatch import cli as dcli

    if not args.yes and not dcli._is_tty():
        raise CoordError(f"{what} asks y/N on your terminal and this process has none; run it in your own terminal, "
                         "or pass --yes")


def _confirm(args: argparse.Namespace, label: str, prompt: str) -> None:
    """y/N on the operator's terminal (dispatch's own prompt); refused with no terminal unless ``--yes``. ``prompt``
    names what is being signed."""
    from levain.dispatch import cli as dcli

    if args.yes:
        return
    _need_terminal(args, label)
    try:
        answer = dcli._ask(args, f"{prompt}? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        raise CoordError("not confirmed; nothing was signed or stored")


def _signer_class(authority: Any, fp: str) -> str:
    key = authority.key(fp)
    return "not an enrolled key" if key is None else str(key.key_class)


def _prompt(body: dict[str, Any], fp: str, klass: str) -> str:
    return (f"sign {body['envelope_id']} (digest …{env.envelope_digest(body)[-8:]}, {len(body['binds'])} thread(s), "
            f"{len(body['lands'])} land(s), stops {_clock(parse_stamp(body['stop_at']))}) as {fp} ({klass})")


def _live_for(store: EnvelopeStore, envelope_id: str, thread_id: str, authority: Any, unchecked: str = "") -> str:
    """``live`` or why not for one bound thread (the chain's, :meth:`EnvelopeStore.chain_live_refusal`)."""
    if authority is None:
        return unchecked or "not checked (no authority log named; pass --data-root)"
    refused = store.chain_live_refusal(envelope_id, thread_id, authority=authority)
    return "live" if refused is None else refused


def _label(live: str) -> str:
    """``live`` is true of the chain and misleading alone: nothing is in force yet."""
    return _BOUND if live == "live" else live


def _write_new(path: Path, text: str, force: bool) -> None:
    p = path.expanduser()
    try:
        with open(p, "w" if force else "x", encoding="utf-8") as f:
            f.write(text)
    except FileExistsError:
        raise CoordError(f"{p} already exists; pass --force to replace it") from None
    except OSError as exc:
        raise CoordError(f"cannot write {p}: {exc.strerror or exc}") from None


def run_envelope_echo(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    threads = [store.thread(t) for t in args.threads]
    if len({t.thread_id for t in threads}) != len(threads):
        raise CoordError("a thread is named twice")
    for t in threads:
        if not t.members:
            raise CoordError(f"thread {t.thread_id} holds no drops; nothing to bind")
    shared = [d for d in threads[0].members if all(d in t.members for t in threads)]
    if not shared:
        raise CoordError("these threads share no drop; one envelope covers threads that hold the same drop")
    drop = store.drop(shared[0])
    # the head, its effective envelope per thread and `seen` in ONE read: a seen read later could cover a narrowing
    # the view never showed (store.basis raises the shared-head message)
    first, _eff, seen = store.basis([t.thread_id for t in threads])
    where = _parse_where(args.where)
    now = _tick().replace(microsecond=0)
    body = {"format": env.BODY_FORMAT, "store_id": store.store_id(), "envelope_id": "ev-" + secrets.token_hex(16),
            "origin": "worded", "drop_id": drop.drop_id, "words_sha256": drop.words_sha256,
            "binds": [{"thread_id": t.thread_id, "rev": t.rev, "members_sha256": t.members_sha256} for t in threads],
            "supersedes": None if first is None else {"envelope_id": first.envelope_id, "digest": first.digest,
                                                       "seen": seen},
            "draft": None, "where": where, "lands": [_parse_land(s) for s in args.land], "pushes": [], "publishes": [],
            "budget": _echo_budget(args.budget), "stop_at": utc_stamp(now + _parse_stop(args.stop)),
            "issued_at": utc_stamp(now)}
    text = canonical(body)
    store.check_body(text)          # the shape and the disk checks this reader will sign, before a file holds it
    shown = _view(body, store)
    if args.output is not None:
        _write_new(args.output, text + "\n", args.force)
    _emit(out, args.json, {"body": body, "digest": env.envelope_digest(body),
                           "file": None if args.output is None else str(args.output)},
          shown + (f"\nbody written to {args.output}; sign it with `levain envelope sign {args.output} "
                   "--signing-key KEY`" if args.output is not None else f"\nbody: {text}"))
    return 0


def run_envelope_extend(args: argparse.Namespace, out: Any) -> int:
    """``levain envelope extend THREAD --budget K=N,...`` (C6, T6): the body that raises a thread's budget, for YOU to
    sign. It is the thread's head envelope as narrowed (its effective where, effects and stop; never its launch grant,
    which the card shows removed) with the budget you
    name (keys you leave out keep the head's), a worded envelope bound to the thread's current revision that supersedes
    the head. Writing it grants nothing: only ``levain envelope sign`` (your key) makes it an envelope."""
    from levain.coordinator import budget

    store = _store(args)
    t = store.thread(args.thread)
    head, effs, seen = store.basis([t.thread_id])          # one read: the head, its effective envelope, `seen`
    if head is None:
        raise CoordError(f"thread {t.thread_id} has no envelope, so no budget to extend (levain envelope echo)")
    ended = store.end_of(head.envelope_id, t.thread_id)
    if ended is not None:
        raise CoordError(f"envelope {head.envelope_id} ended for this thread ({ended[0]}); a new one is an echo")
    eff = effs[t.thread_id]
    now = _tick().replace(microsecond=0)
    if parse_stamp(eff["stop_at"]) - now < _MIN_LEFT:
        raise CoordError(f"envelope {head.envelope_id} has stopped or stops within 5 minutes; echo a new one")
    given = _parse_budget(args.budget)
    named = _named(args.budget)
    lower = [f"{k}={given[k]} (now {eff['budget'][k]})" for k in env.BUDGET_KEYS
             if k in named and given[k] < eff["budget"][k]]
    if lower:
        # extend means extend (L3 r1 E1): a smaller budget is a new echo, never this command
        raise CoordError(f"--budget names a value below the thread's current budget: {', '.join(lower)}; extend only "
                         "raises")
    new_budget = {k: (given[k] if k in named else eff["budget"][k]) for k in env.BUDGET_KEYS}
    if not any(new_budget[k] > eff["budget"][k] for k in env.BUDGET_KEYS):
        raise CoordError("--budget names no value greater than the thread's current budget; nothing to extend")
    if head.drop_id not in t.members:
        raise CoordError(f"the head's drop {head.drop_id} left thread {t.thread_id}; echo a new envelope")
    drop = store.drop(head.drop_id)
    body = {"format": env.BODY_FORMAT, "store_id": store.store_id(), "envelope_id": "ev-" + secrets.token_hex(16),
            "origin": "worded", "drop_id": drop.drop_id, "words_sha256": drop.words_sha256,
            "binds": [{"thread_id": t.thread_id, "rev": t.rev, "members_sha256": t.members_sha256}],
            "supersedes": {"envelope_id": head.envelope_id, "digest": head.digest, "seen": seen},
            "draft": None, "where": eff["where"], "lands": eff["lands"], "pushes": eff["pushes"], "publishes": eff["publishes"],
            "budget": new_budget, "stop_at": eff["stop_at"], "issued_at": utc_stamp(now)}
    # extend never carries the head's launch grant: the desk counts launches per envelope, so a copy under the new id
    # would start its count again and replenish what was used (L3 r2). The card shows it removed; a grant is signed anew.
    dropped_launches = bool(eff.get(env.LAUNCH_LIST))
    text = canonical(body)
    store.check_body(text)
    with read_transaction(store._connect) as conn:
        g = budget.gauge(conn, store.directory, t.thread_id)
    used = "" if g is None else ("used so far on this thread: " + ", ".join(
        f"{k} {budget._num(k, g.used[k])}" for k in budget.METERS) + "; dollars not metered")
    shown = _view(body, store) + (f"\n{used}" if used else "")
    if dropped_launches:
        shown += ("\nthe head's launch grant is NOT carried: once this is signed the desk may start no seat without "
                  "asking; a launch grant is signed on its own (the cockpit's Add an envelope)")
    if args.output is not None:
        _write_new(args.output, text + "\n", args.force)
    _emit(out, args.json, {"body": body, "digest": env.envelope_digest(body), "used": None if g is None else g.used,
                           "launches_dropped": dropped_launches,
                           "file": None if args.output is None else str(args.output)},
          shown + (f"\nbody written to {args.output}; YOU sign it: `levain envelope sign {args.output} "
                   "--signing-key KEY`" if args.output is not None else f"\nbody: {text}"))
    return 0


def _accepted(args: argparse.Namespace, out: Any, store: EnvelopeStore, stored: Any, created: bool,
              authority: Any, *, yes: bool = False) -> None:
    from levain.coordinator.envelopes import NOT_IN_FORCE

    threads = store.threads_of_envelope(stored.envelope_id)
    live = {t: _live_for(store, stored.envelope_id, t, authority) for t in threads}
    state = "accepted" if created else "already accepted (same record, nothing written)"
    how = ", signed with --yes (no prompt)" if yes else ""
    text = [f"envelope {stored.envelope_id} {state}, signed by {stored.signer_fp} ({stored.signer_class}){how}"]
    text += [f"  {t}: {_label(live[t])}" for t in threads]
    text.append(f"  authority today: {NOT_IN_FORCE}")
    _emit(out, args.json, {"envelope_id": stored.envelope_id, "created": created, "signer_fp": stored.signer_fp,
                           "live": live, "authority_today": NOT_IN_FORCE, "signed_with_yes": yes}, "\n".join(text))


def _refuse_narrowed(body: dict[str, Any], basis: Any) -> None:
    """Before a superseding body is shown for signing or accepting, on the one ``basis`` the view then renders: refuse a
    ``seen`` past the store's high-water (no echo wrote it) and a narrowing of its head newer than ``seen`` on a bound
    thread, so the view (which reads the head as it is at that point) is the base the signature will be held to. Only
    the very body already accepted under its id is an exact retry, left to the store (L3 h11 r1: another body reusing
    an accepted id is checked like any other)."""
    sup = body["supersedes"]
    if sup is None:
        return
    if basis.stored is not None and canonical(basis.stored.body) == canonical(body):
        return
    forged = seen_past(body, basis.high)
    if forged is not None:
        raise CoordError(forged)
    if basis.narrowed:
        raise CoordError(f"envelope {sup['envelope_id']} was narrowed after this body was echoed; echo again")


def run_envelope_sign(args: argparse.Namespace, out: Any) -> int:
    from levain.dispatch import cli as dcli

    store = _store(args)
    text, body = _body_from_file(args.file)
    try:
        stored = store.envelope(body["envelope_id"])
    except CoordError:
        stored = None
    if stored is not None:
        # a second signature would be a new signed_at, which the store refuses as another record for this id
        raise CoordError(f"envelope {stored.envelope_id} is already accepted; there is nothing to sign "
                         f"(levain envelope show {stored.envelope_id})")
    body = store.check_body(text)          # a body the store can only refuse is never shown for signing
    basis = store.sign_basis(body)          # ONE store read: the refusal and the view are of the same point
    _refuse_narrowed(body, basis)
    if parse_stamp(body["stop_at"]) - _tick() < _MIN_LEFT:
        raise CoordError(f"envelope {body['envelope_id']} has stopped or stops within 5 minutes; echo again")
    snap = _snapshot(args, store)          # one authority read for the view and the prompt
    print(_view(body, store, authority=snap, basis=basis), file=sys.stderr if args.json else out, flush=True)
    _need_terminal(args, "signing")
    signer_fp, sign = dcli._signer(args, env.SIGN_NAMESPACE, "envelope sign")
    _confirm(args, "signing", _prompt(body, signer_fp, _signer_class(snap, signer_fp)))
    # the clock is read only now: the store refuses a signed_at far from its own, and a slow reader is not a stale one
    payload = env.sign_payload(body, signer_fp=signer_fp, signed_at=_tick())
    signature = dcli._sign_bytes(sign, env.sign_message(payload))
    # and the authority log too: the prompt's snapshot only names the key; a key revoked while it waited signs nothing
    authority = _snapshot(args, store)
    stored, created = store.accept_envelope(text, payload, signature, authority=authority)
    sigfile = args.file.expanduser().with_name(args.file.name + ".sig.json")
    record: dict[str, Any] = {"payload": payload, "signature": signature}
    if args.yes:
        record["note"] = "signed with --yes (no prompt)"
    try:
        sigfile.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"levain envelope sign: accepted, but cannot write {sigfile}: {exc.strerror or exc}", file=sys.stderr)
    _accepted(args, out, store, stored, created, authority, yes=args.yes)
    return 0


def run_envelope_accept(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    text, body = _body_from_file(args.file)
    raw = _read_file(args.sigfile, env.SIGNATURE_MAX + 4096, "the signature file")
    try:
        sig = json.loads(raw)
    except ValueError:
        raise CoordError("the signature file is not JSON") from None
    if not isinstance(sig, dict) or not {"payload", "signature"} <= set(sig) <= {"payload", "signature", "note"}:
        raise CoordError('the signature file is {"payload": ..., "signature": ...}')
    payload = sig["payload"]
    fp = payload.get("signer_fp") if isinstance(payload, dict) else None
    basis = store.sign_basis(body)          # ONE store read: the refusal and the view are of the same point
    _refuse_narrowed(body, basis)
    snap = _snapshot(args, store)          # one authority read for the view and the signer line
    print(_view(body, store, authority=snap, basis=basis), file=sys.stderr if args.json else out, flush=True)
    if isinstance(fp, str):
        print(f"signed by {fp[:80]} ({_signer_class(snap, fp)})"
              + (f", {str(sig['note'])[:80]}" if "note" in sig else ""), file=sys.stderr if args.json else out,
              flush=True)
    authority = _snapshot(args, store)          # read after the output, which can block, right before the write
    stored, created = store.accept_envelope(text, sig["payload"], sig["signature"], authority=authority)
    _accepted(args, out, store, stored, created, authority)
    return 0


_SPEC_KEYS = {"where", "lands", "pushes", "publishes", "launches", "budget", "stop"}


def _spec_list(spec: dict[str, Any], key: str) -> list[Any]:
    value = spec.get(key, [])
    if not isinstance(value, list):
        raise CoordError(f"{key} in an envelope spec is a list")
    return value


def _spec_body(spec: Any, words: str, source: Source, store: EnvelopeStore) -> dict[str, Any]:
    """The structured body a drop carries: the user's SPEC (where, lands, pushes, publishes, budget, stop) plus what the
    CLI picks (drop id, envelope id, the words' hash, the store, the clock)."""
    if not isinstance(spec, dict) or not set(spec) <= _SPEC_KEYS:
        raise CoordError(f"an envelope spec is a JSON object with keys among {sorted(_SPEC_KEYS)}")
    where = []
    for w in _spec_list(spec, "where"):
        if not isinstance(w, dict) or not isinstance(w.get("repo"), str) or not isinstance(w.get("paths", ["."]), list):
            raise CoordError('each where entry is {"repo": ABSOLUTE_PATH, "paths": [...]}')
        where.append({"repo": _real(w["repo"]), "paths": w.get("paths", ["."])})
    lands = []
    for land in _spec_list(spec, "lands"):
        if not isinstance(land, dict):
            raise CoordError("each land is an object")
        frm = land.get("from", "named")
        repo, onto = land.get("repo"), land.get("onto")
        if frm == "source":
            if source.how == "none":
                raise CoordError("a land from source needs the drop made inside a git work tree on a branch")
            repo = repo or source.repo
            onto = onto or (source.ref or "").removeprefix("refs/heads/")
        if not isinstance(repo, str) or not isinstance(onto, str):
            raise CoordError("each land names a repo and a branch to land onto")
        criteria = land.get("criteria", [])
        if not isinstance(criteria, list):
            raise CoordError("a land's criteria are a list")
        lands.append({"repo": _real(repo), "onto": onto, "from": frm,
                      "onto_from": land.get("onto_from", "drop" if frm == "source" else "inferred"),
                      "criteria": criteria})
    pushes = [{**p, "repo": _real(p["repo"])} if isinstance(p, dict) and isinstance(p.get("repo"), str) else p
              for p in _spec_list(spec, "pushes")]
    publishes = _spec_list(spec, "publishes")
    # the optional launch grant: written into the body only when named (absent = none, the bytes a no-grant envelope
    # always had); each entry's shape and bounds are parse_body's, so compose and verify refuse in the same words
    launches = [{**x, "repo": _real(x["repo"])} if isinstance(x, dict) and isinstance(x.get("repo"), str) else x
                for x in _spec_list(spec, "launches")]
    given = spec.get("budget", {})
    if not isinstance(given, dict):
        raise CoordError("budget in an envelope spec is an object")
    budget = {**{k: 0 for k in env.BUDGET_KEYS}, **given}
    stop = spec.get("stop", "2h")
    if not isinstance(stop, str):
        raise CoordError('stop is a duration text like "2h"')
    now = _tick().replace(microsecond=0)
    body = {"format": env.BODY_FORMAT, "store_id": store.store_id(), "envelope_id": "ev-" + secrets.token_hex(16),
            "origin": "structured", "drop_id": "dr-" + secrets.token_hex(16), "words_sha256": words_sha256(words),
            "binds": [], "supersedes": None, "draft": None, "where": where, "lands": lands, "pushes": pushes,
            "publishes": publishes, "budget": budget, "stop_at": utc_stamp(now + _parse_stop(stop)),
            "issued_at": utc_stamp(now)}
    if launches:
        body[env.LAUNCH_LIST] = launches
    return body


def _drop_with_envelope(args: argparse.Namespace, out: Any, store: EnvelopeStore, words: str, source: Source) -> int:
    from levain.dispatch import cli as dcli

    check_words(words)          # a drop the store would refuse is never signed for
    try:
        spec = json.loads(_read_file(args.envelope, 64 * 1024, "the envelope spec"))
    except ValueError:
        raise CoordError("the envelope spec is not JSON") from None
    if args.outbox_id is not None and (sent := store.drop_by_outbox(args.outbox_id)) is not None:
        # a resend returns the stored drop as it was, so an envelope signed now would never be read
        raise CoordError(f"outbox id {args.outbox_id[:60]} is already delivered as {sent.drop_id}; nothing new is "
                         f"signed for it (levain drops show {sent.drop_id})")
    body = _spec_body(spec, words, source, store)
    text = canonical(body)
    body = store.check_body(text)          # a body the store can only refuse is never shown for signing
    print(_view(body, store, words=words), file=sys.stderr if args.json else out, flush=True)
    _need_terminal(args, "a drop with an envelope")
    signer_fp, sign = dcli._signer(args, env.SIGN_NAMESPACE, "a drop with an envelope")
    _confirm(args, "a drop with an envelope",
             _prompt(body, signer_fp, _signer_class(_snapshot(args, store), signer_fp)))
    payload = env.sign_payload(body, signer_fp=signer_fp, signed_at=_tick())          # after the answer, not before
    signature = dcli._sign_bytes(sign, env.sign_message(payload))
    authority = _snapshot(args, store)          # after the answer too: a key revoked while it waited signs nothing
    drop, created = store.accept_drop(words, surface="cli", outbox_id=args.outbox_id, source=source,
                                      envelope=(text, payload, signature), authority=authority)
    if not created and drop.envelope_id != body["envelope_id"]:
        # the same outbox id delivered between the check above and this write
        raise CoordError(f"outbox id {drop.outbox_id[:60]} was delivered as {drop.drop_id} while you signed; the "
                         f"envelope you signed was not read and nothing was written (levain drops show {drop.drop_id})")
    refused = None if drop.envelope_id else (store.envelope_refused(drop.drop_id) or "the envelope was not taken")
    how = " (signed with --yes, no prompt)" if args.yes else ""
    line = f"envelope: taken ({drop.envelope_id}){how}" if drop.envelope_id \
        else f"envelope: refused: {refused}\nyour words are kept: {drop.drop_id} holds them"
    state = "delivered" if created else "already delivered (resend, nothing written)"
    _emit(out, args.json, {**_drop_row(drop), "created": created,
                           "envelope": {"taken": drop.envelope_id is not None, "envelope_id": drop.envelope_id,
                                        "refused": refused}},
          f"drop receipt: {drop.drop_id} {state} at {drop.received_at}, {_where(drop)}\n{line}")
    _launch_woken(store, drop)
    return 0 if drop.envelope_id else 1


def run_envelope_show(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    stored = store.envelope(args.envelope_id)
    threads = store.threads_of_envelope(stored.envelope_id)
    if args.thread is not None:
        if args.thread not in threads:
            raise CoordError(f"envelope {stored.envelope_id} is not bound to thread {args.thread[:40]}")
        threads = [args.thread]
    return _show(args, out, store, stored, threads)


def _ended(store: EnvelopeStore, envelope_id: str, thread_id: str) -> str | None:
    """Why the envelope is over for this thread, read from the row that ended it, or None."""
    end = store.end_of(envelope_id, thread_id)
    if end is None:
        return None
    kind, _by, at = end
    when = _clock(parse_stamp(at))
    return f"its thread is done, at {when}" if kind == "thread_done" else f"killed by you at {when}"


_SINGULAR = {"lands": "land", "pushes": "push", "publishes": "publish", "launches": "launch"}


def _identity_lines(eff: dict[str, Any]) -> list[str]:
    """The identity of each grant a thread's effective envelope holds: what ``drop_effect`` names to narrow one."""
    return [f"    {_SINGULAR[k]} identity: {env.effect_identity(k, e)}" for k in env.GRANT_LISTS for e in eff.get(k, [])]


def _show(args: argparse.Namespace, out: Any, store: EnvelopeStore, stored: Any, threads: list[str]) -> int:
    authority, unchecked = None, ""
    try:
        authority = _snapshot(args, store)
    except _faults() as exc:
        unchecked = f"not checked ({str(exc)[:160]})"
    effs = {t: store.effective(stored.envelope_id, t) for t in threads}
    ends = {t: _ended(store, stored.envelope_id, t) for t in threads}
    head = [f"envelope {stored.envelope_id}  digest {stored.digest}  signed by {stored.signer_fp} "
            f"({stored.signer_class}), accepted {stored.accepted_at}"]
    if threads and all(ends.values()):
        head.insert(0, "ENDED (" + "; ".join(dict.fromkeys(str(r) for r in ends.values())) + ")")
    rows, text = [], list(head)
    if not threads:
        text += _describe(stored.body, store)
        text.append("  not bound to any thread yet (a structured envelope binds when its drop is split into one thread)")
        text.append("  authority today: " + store.authority_refusal(stored.envelope_id, ""))
    for t in threads:
        eff = effs[t]
        live = _live_for(store, stored.envelope_id, t, authority, unchecked)
        now = store.authority_refusal(stored.envelope_id, t, authority=authority)
        if ends[t]:
            text.append(f"thread {t}: ENDED ({ends[t]})" if not all(ends.values()) else f"thread {t}:")
            text.append("  it had allowed:")
            text += _describe(stored.body, store, binds=False, ended=True)
        else:
            text.append(f"thread {t} (as narrowed):")
            text += _describe(eff, store, binds=False)
            text += _identity_lines(eff)
        text += [f"  live: {_label(live)}", f"  authority today: {now}"]
        rows.append({"thread_id": t, "effective": eff, "live": live, "authority_today": now,
                     "ended": ends[t]})
    obj = {"envelope_id": stored.envelope_id, "digest": stored.digest, "origin": stored.origin,
           "signer_fp": stored.signer_fp, "accepted_at": stored.accepted_at, "body": stored.body, "threads": rows}
    _emit(out, args.json, obj, "\n".join(text))
    return 0


def run_envelope_list(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    envelopes = store.envelopes_of(args.thread)
    if not envelopes:
        _emit(out, args.json, [], f"no envelopes bound to thread {args.thread}")
        return 0
    rc, rows = 0, []
    for e in envelopes:
        buf = _Capture()
        ns = argparse.Namespace(**{**vars(args), "json": False})
        rc |= _show(ns, buf, store, e, [args.thread])
        rows.append({"envelope_id": e.envelope_id, "text": buf.text()})
    _emit(out, args.json, rows, "\n".join(r["text"] for r in rows))
    return rc


class _Capture:
    def __init__(self) -> None:
        self._parts: list[str] = []

    def write(self, s: str) -> int:
        self._parts.append(s)
        return len(s)

    def text(self) -> str:
        return "".join(self._parts).rstrip("\n")


def run_envelope_narrow(args: argparse.Namespace, out: Any) -> int:
    store = _store(args)
    if args.envelope_cmd == "kill":
        kind, value, what, done = "kill", "", "ended", "already ended"
    elif args.stop_earlier is not None:
        kind, value = "stop_earlier", _parse_stop_earlier(args.stop_earlier)
        what, done = f"narrowed (stop moved earlier to {value})", f"already narrowed (stop already {value} or earlier)"
    else:
        kind, value = "drop_effect", args.drop_effect
        what, done = f"narrowed (effect {value} dropped)", f"already narrowed (effect {value} already dropped)"
    wrote = store.attenuate(args.envelope_id, kind, value, thread_id=args.thread, by="operator")
    scope = f" for thread {args.thread}" if args.thread else ""
    _emit(out, args.json, {"envelope_id": args.envelope_id, "kind": kind, "value": value, "thread_id": args.thread,
                           "written": wrote},
          f"envelope {args.envelope_id}{scope}: {what if wrote else done}")
    return 0


_ENVELOPE_RUN = {"echo": run_envelope_echo, "extend": run_envelope_extend, "sign": run_envelope_sign,
                 "accept": run_envelope_accept,
                 "show": run_envelope_show, "list": run_envelope_list, "kill": run_envelope_narrow,
                 "narrow": run_envelope_narrow}


def run_envelope(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        return _ENVELOPE_RUN[args.envelope_cmd](args, out)
    except _faults() as exc:
        print(f"levain envelope {args.envelope_cmd}: {exc}", file=sys.stderr)
        return 1



def _parse_part(spec: str) -> Part:
    kind, sep, title = spec.partition(":")
    if not sep or kind not in KINDS or not title.strip():
        raise CoordError(f"--thread takes KIND:TITLE with KIND one of {', '.join(KINDS)} (got {spec[:80]!r})")
    return Part(kind=kind, title=title.strip())


def _parse_attach(spec: str) -> Part:
    kind, sep, thread_id = spec.partition(":")
    if not sep or kind not in KINDS or not thread_id:
        raise CoordError(f"--attach takes KIND:THREAD, KIND the intent the drop adds (one of {', '.join(KINDS)}) "
                         f"(got {spec[:80]!r})")
    return Part(kind=kind, attach=thread_id)


def _no_dup_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for k, v in pairs:
        if k in seen:
            raise CoordError(f"the split document names {k!r} twice")
        seen[k] = v
    return seen


def _split_doc(text: str, positional: str | None) -> tuple[str, list[Part]]:
    """One JSON document for a whole split: ``{"drop_id": ID, "threads": [{"kind", "title", "about"?}, ...],
    "attach": [{"kind", "thread"}, ...]}``. Each entry goes through the same checks as ``--thread KIND:TITLE`` and
    ``--attach KIND:THREAD``; ``about`` is a thread id or ``@N`` as for ``--about``. A key named twice, at any level, is
    refused."""
    try:
        doc = json.loads(text, object_pairs_hook=_no_dup_keys)
    except RecursionError:
        raise CoordError("the split document is nested too deeply") from None
    except ValueError as exc:
        raise CoordError(f"the split document is not JSON: {str(exc)[:120]}") from None
    if not isinstance(doc, dict) or set(doc) - {"drop_id", "threads", "attach"}:
        raise CoordError('the split document is an object with only "drop_id", "threads" and "attach"')
    drop_id = doc.get("drop_id", positional)
    if not isinstance(drop_id, str) or not drop_id or (positional is not None and drop_id != positional):
        raise CoordError('the split document names its drop as "drop_id" (and it must match DROP_ID when both are given)')
    parts: list[Part] = []
    for key, fields in (("threads", ("kind", "title", "about")), ("attach", ("kind", "thread"))):
        rows = doc.get(key, [])
        if not isinstance(rows, list):
            raise CoordError(f'"{key}" is a list')
        for row in rows:
            if not isinstance(row, dict) or set(row) - set(fields):
                raise CoordError(f'each of "{key}" is an object with {", ".join(fields)} (kind required)')
            kind = row.get("kind")
            if not isinstance(kind, str) or kind not in KINDS:
                raise CoordError(f'"kind" in "{key}" is exactly one of {", ".join(KINDS)} (got {str(kind)[:80]!r})')
            if key == "threads":
                title = row.get("title")
                if not isinstance(title, str) or not title.strip():
                    raise CoordError('"title" is a non-empty string')
                about = row.get("about")
                if about is not None:
                    if not isinstance(about, str) or not about:
                        raise CoordError('"about" is a thread id or @N (a non-empty string)')
                    if kind == "work":
                        raise CoordError('"about" applies to a new ask, note or relay thread, not a work thread')
                parts.append(Part(kind, title.strip(), about=about))
            else:
                thread = row.get("thread")
                if not isinstance(thread, str) or not thread or ":" in thread:
                    raise CoordError('"thread" is a thread id (a non-empty string without ":")')
                parts.append(Part(kind=kind, attach=thread))
    if not parts:
        raise CoordError('the split document names no threads: give "threads" or "attach" at least one entry')
    return drop_id, parts


def run_split(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        store = _store(args)
        drop_id = args.drop_id
        if args.json_file is not None or args.stdin:
            if args.thread or args.attach or args.about is not None:
                raise CoordError("a JSON split carries every thread itself; leave out --thread, --attach and --about")
            if args.json_file is not None and args.stdin:
                raise CoordError("name the JSON once: --json-file or --stdin")
            drop_id, parts = _split_doc(_read_input(args.json_file), args.drop_id)
        elif args.drop_id is None:
            raise CoordError("name the drop (DROP_ID), or give the whole split as JSON (--json-file PATH or --stdin)")
        else:
            parts = [_parse_part(s) for s in args.thread]
        if args.json_file is None and not args.stdin and args.about is not None:
            if all(p.kind == "work" for p in parts):
                raise CoordError("--about applies to a new ask, note or relay thread; this split names none")
            parts = [Part(p.kind, p.title, about=None if p.kind == "work" else args.about) for p in parts]
        parts += [_parse_attach(a) for a in args.attach]
        threads = store.split(drop_id, parts, by=args.by, token=args.token)
    except _REFUSED as exc:
        print(f"levain drops split: {exc}", file=sys.stderr)
        return 1
    n = len(threads)
    _emit(out, args.json, [_thread_row(t) for t in threads],
          f"drop receipt: {drop_id} split into {n} thread{'s' if n != 1 else ''}\n"
          + "\n".join(_thread_line(t) for t in threads))
    return 0


def run_note(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        why = _text_input(args, args.why, "--why")
        if why is None:
            raise CoordError("say why: --why TEXT, --text-file PATH or --stdin")
        why = _store(args).note(args.drop_id, why, token=args.token)
    except _REFUSED as exc:
        print(f"levain drops note: {exc}", file=sys.stderr)
        return 1
    _emit(out, args.json, {"drop_id": args.drop_id, "stage": "noted", "why": why},
          f"drop receipt: {args.drop_id} noted: {why}")
    return 0


def run_drop_show(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        store = _store(args)
        if _no_store(store):
            print(f"levain drops show: {NO_STORE}", file=sys.stderr)
            return 1
        drop, threads, receipts = store.drop_view(args.drop_id)
    except _REFUSED as exc:
        print(f"levain drops show: {exc}", file=sys.stderr)
        return 1
    obj = {**_drop_row(drop), "threads": [_thread_row(t) for t in threads],
           "drop_receipts": [{"reply_id": r.reply_id, "at": r.at, **r.body} for r in receipts]}
    text = (f"{drop.drop_id}  {drop.surface}  received {drop.received_at}, {_where(drop)}\n"
            f"  words (verbatim, data): {_shown(drop.words)}\n"
            + ("\n".join(_thread_line(t) for t in threads) if threads else "  not split yet")
            + "\n  drop receipts: " + ", ".join(
                f"{r.body.get('stage')} {r.at}" + (f" ({_shown(str(r.body.get('why', '')))})" if r.body.get("stage") == "noted" else "")
                for r in receipts))
    _emit(out, args.json, obj, text)
    return 0


def _chat_line(r: Any) -> str:
    """One timeline row for a terminal. Every body is quoted (:func:`_shown`); a vendor row says ``vendor`` and that it
    is quoted data, so nothing a worker's thread showed can read as a line of yours."""
    at = r.at if r.gseq is None else f"#{r.gseq} {r.at}"
    if r.kind == "message":
        status = f" [{r.status or 'no outbox row'}]"
        return f"  {at}  {r.source} said{status}: {_shown(r.body)}"
    if r.source == "vendor":
        return f"  {at}  vendor {r.kind.removeprefix('vendor_')} (quoted data): {_shown(r.body)}"
    body = "" if r.body is None else f": {_shown(r.body)}"
    return f"  {at}  {r.kind} ({r.source}) {r.ref_id}{body}"


def run_chat(args: argparse.Namespace, out: Any = None) -> int:
    from levain.coordinator.chat import ChatStore

    out = out or sys.stdout
    try:
        before, merged = ChatStore(_store(args)).timeline(args.thread_id)
    except _REFUSED as exc:
        print(f"levain chat {args.chat_cmd}: {exc}", file=sys.stderr)
        return 1
    def rows(rs: list[Any]) -> list[dict[str, Any]]:
        return [{"gseq": r.gseq, "kind": r.kind, "source": r.source, "ref_id": r.ref_id, "launch_id": r.launch_id,
                 "status": r.status, "body": r.body, "at": r.at} for r in rs]

    text = [f"thread {args.thread_id}"]
    if before:
        text += ["  before the chat record:", *(_chat_line(r) for r in before)]
    text += [_chat_line(r) for r in merged] or ["  no chat record yet"]
    _emit(out, args.json, {"thread_id": args.thread_id, "before": rows(before), "timeline": rows(merged)},
          "\n".join(text))
    return 0


def _chat_subparsers(subparsers: argparse._SubParsersAction) -> argparse._SubParsersAction:
    """The ``chat`` command's verb table. ``levain chat key`` (the chat launch's held keys) may have registered
    ``chat`` already; its verbs and ``show`` share the one command rather than argparse refusing a second ``chat``."""
    existing = subparsers.choices.get("chat")
    if existing is None:
        ch = subparsers.add_parser("chat", help="A thread's chat record and the chat launch's network.")
        return ch.add_subparsers(dest="chat_cmd", required=True)
    for action in existing._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise RuntimeError("the chat command was registered without a verb table")


def run_drop_list(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        store = _store(args)
        if _no_store(store):
            _emit(out, args.json, [], NO_STORE)
            return 0
        pairs = store.drops_noted(limit=args.limit)
    except _REFUSED as exc:
        print(f"levain drops list: {exc}", file=sys.stderr)
        return 1
    drops = [d for d, _ in pairs]
    whys = {d.drop_id: w for d, w in pairs}
    _emit(out, args.json, [{**_drop_row(d), "noted": whys[d.drop_id]} for d in drops],
          "\n".join(f"{d.drop_id}  {d.received_at}  {_shown(d.words[:80])}"
                     + ("" if whys[d.drop_id] is None else f"  [noted: {_shown(whys[d.drop_id])}]") for d in drops)
          or "no drops")
    return 0


def run_thread(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        store = _store(args)
        cmd = args.thread_cmd
        if cmd in ("list", "show") and _no_store(store):
            if cmd == "show":
                print(f"levain thread show: {NO_STORE}", file=sys.stderr)
                return 1
            _emit(out, args.json, [], NO_STORE)
            return 0
        if cmd == "list":
            threads = store.threads(include_closed=args.all)
            _emit(out, args.json, [_thread_row(t) for t in threads],
                  "\n".join(_thread_line(t) for t in threads) or "no open threads")
            return 0
        if cmd == "show":
            t, revs, events = store.thread_view(args.thread_id)
            obj = {**_thread_row(t), "revisions": [{"rev": r, "members": list(m), "members_sha256": s, "cause": c}
                                                   for r, m, s, c in revs],
                   "events": [{"seq": e[0], "field": e[1], "value": e[2], "cause": e[3], "by": e[4], "at": e[5]}
                              for e in events]}
            text = (_thread_line(t) + "\n"
                    + "\n".join(f"    rev {r} ({c}): {', '.join(m) or '(no drops)'}" for r, m, _s, c in revs) + "\n"
                    + "\n".join(f"    #{e[0]} {e[1]} = {e[2]}  [{e[4]}: {e[3]}]" for e in events))
            _emit(out, args.json, obj, text)
            return 0
        if cmd in ("state", "set") and getattr(args, "cause_file", None) is not None:
            if args.cause:
                raise CoordError("name the cause once: --cause or --cause-file")
            args.cause = _read_input(args.cause_file)
        if cmd == "state":
            t = store.set_state(args.thread_id, args.state, by=args.by, cause=args.cause, token=args.token)
        elif cmd == "merge":
            t = store.merge(args.into, args.other, by=args.by, token=args.token)
        elif cmd == "detach":
            t = store.detach(args.thread_id, args.drop_id, by=args.by, token=args.token)
        elif cmd == "add":
            if args.about is not None and args.attach:
                raise CoordError("--about names what a NEW thread concerns; an attach joins an existing one")
            spec = _text_input(args, args.thread, "--thread") if not args.attach else None
            if args.attach and (args.text_file is not None or args.stdin):
                raise CoordError("name the thread once: --attach, or --thread / --text-file / --stdin")
            part = _parse_attach(args.attach) if args.attach else _parse_part(spec or "")
            if args.about is not None:
                part = Part(part.kind, part.title, about=args.about)
            t = store.add(args.drop_id, part, by=args.by, token=args.token)
        else:   # set
            value = _text_input(args, args.value, "VALUE")
            if value is None:
                raise CoordError("give the new value: VALUE, --text-file PATH or --stdin")
            t = store.set_field(args.thread_id, args.field, value, by=args.by, cause=args.cause, token=args.token)
    except _REFUSED as exc:
        print(f"levain thread {args.thread_cmd}: {exc}", file=sys.stderr)
        return 1
    _emit(out, args.json, _thread_row(t), _thread_line(t))
    return 0


def _lease_line(lease: Any) -> str:
    if lease is None:
        return "lease: nobody holds the coordinator role"
    live = "live" if lease["live"] else "past its until"
    return f"lease: {lease['holder']} holds it until {lease['until']} ({live}), token {lease['token']}"


def run_coord(args: argparse.Namespace, out: Any = None) -> int:
    """``levain coord ...``. A ``review feeds --max-seconds N`` run is bounded by the kernel for the WHOLE command: the
    alarm is armed before the store is opened and disarmed only after the error line and the final flush, so no step of
    the process can outlive it. The caller's SIGALRM disposition and any alarm it had outstanding are put back
    afterwards (the CLI process exits and never notices; an in-process caller keeps its own)."""
    out = out or sys.stdout
    if args.coord_cmd != "review" or getattr(args, "review_cmd", None) != "feeds":
        return _run_coord(args, out)
    limit = getattr(args, "max_seconds", None) or 0
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        print("levain coord review: --max-seconds is a whole number of seconds >= 1, or 0 for no bound",
              file=sys.stderr)
        return 1
    if not limit:
        return _run_coord(args, out)
    if not hasattr(signal, "SIGALRM"):
        print("levain coord review feeds: no run bound on this platform (no SIGALRM)", file=sys.stderr)
        return _run_coord(args, out)
    # launchd coalesces per label, so a hung scheduled run would otherwise stop every later one
    prev_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    prev_alarm = signal.alarm(limit)
    try:
        return _run_coord(args, out)
    finally:
        _flush(out)
        _flush(sys.stderr)
        signal.alarm(0)
        if prev_handler is not None:
            signal.signal(signal.SIGALRM, prev_handler)
        if prev_alarm:
            signal.alarm(prev_alarm)


def _run_coord(args: argparse.Namespace, out: Any) -> int:
    cmd = args.coord_cmd
    try:
        if cmd == "report":          # a seat's verb: it opens no coordinator store and holds no lease
            return _coord_report(args, out)
        store = _board_store(args) if _opens_board(args) else _store(args)
        if cmd == "take":
            hold = None
            if args.hold is not None:
                m = _DURATION.fullmatch(args.hold.strip())
                if m is None or not (m.group(1) or m.group(2)):
                    raise CoordError(f"--for is a duration like 30m or 2h (got {args.hold[:40]!r})")
                hold = timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
            tok = store.coord_take(hold)
            _emit(out, args.json, {"token": tok}, f"you hold the coordinator role, token {tok}")
            return 0
        if cmd == "release":
            ended = store.coord_release(args.token, by=args.by)
            _emit(out, args.json, {"ended": None if ended is None else ended.token},
                  "nobody held the lease; recorded that you looked" if ended is None
                  else f"released token {ended.token} ({ended.holder})")
            return 0
        if cmd == "beat":
            held = store.coord_beat(args.token)
            _emit(out, args.json, {"token": held.token, "until": held.until},
                  f"token {held.token} held until {held.until}")
            return 0
        if cmd == "claim":
            tok = store.coord_claim(args.launch, args.session)
            _emit(out, args.json, {"token": tok}, f"claimed: token {tok}")
            return 0
        if cmd == "handoff" and args.handoff_cmd == "worker":
            return _coord_worker_handoff(args, out, store)
        if cmd in ("close", "open", "handoff"):
            from levain.coordinator import seam_verbs

            if cmd == "close":
                return seam_verbs.run_close(args, out, store)
            if cmd == "open":
                return seam_verbs.run_open(args, out, store)
            return seam_verbs.run_handoff_show(args, out, store)
        if cmd == "agent":
            return _coord_agent(args, out, store)
        if cmd in ("questions", "answer", "escalate"):
            return _coord_first_reader(args, out, store)
        if cmd in ("morning", "away", "budget"):
            return _coord_c6(args, out, store)
        if cmd == "log":
            return _coord_log(args, out, store)
        if cmd == "reply":
            return _coord_reply(args, out, store)
        if cmd == "spawn":
            return _coord_spawn(args, out, store)
        if cmd == "review":
            return _coord_review(args, out, store)
        if cmd == "ready":
            return _coord_ready(args, out, store)
        if cmd == "import-status":
            return _coord_import_status(args, out, store)
        if cmd == "launch":
            from levain.coordinator.launch import launch_session

            launch_id = args.launch_id
            if launch_id is None:
                pending = store.pending_coordinator()
                if pending is None:
                    raise CoordError("no coordinator launch is pending (levain coord status says why; "
                                     "levain coord wake asks for one)")
                launch_id = pending.launch_id
            got = launch_session(store, launch_id)
            _emit(out, args.json, {"launch_id": got.launch_id, "started": got.started, "attach": got.attach,
                                   "refusal": got.refusal, "remedy": got.remedy,
                                   "harness_session_id": got.harness_session_id},
                  got.attach if got.started else _outcome_text(got))
            return 0 if got.started else 1
        if cmd == "wake":
            # a pending launch has a starter wherever it is written: the wake starts the one it wrote, as a drop does
            ln = store.coord_wake()
            got = None
            if ln is not None:
                from levain.coordinator.launch import launch_session

                got = launch_session(store, ln.launch_id)
            elif store.coord_status()["wake"] == "idle":
                # a coordinator running without the lease: you asked, so a done one is replaced (the watcher's listing)
                from levain.coordinator.launch import wake_idle

                got, line = wake_idle(store)
                st = store.coord_status()
                st["started"] = None if got is None else {"started": got.started, "attach": got.attach,
                                                          "refusal": got.refusal, "remedy": got.remedy}
                if got is None:
                    st["why"] = line
                    _emit(out, args.json, st, f"no launch asked for: {line}")
                    return 1
                _emit(out, args.json, st, f"{line}\n{_outcome_text(got)}")
                return 0 if got.started else 1
            st = store.coord_status()
            st["started"] = None if got is None else {"started": got.started, "attach": got.attach,
                                                      "refusal": got.refusal, "remedy": got.remedy}
            _emit(out, args.json, st, (f"asked for coordinator launch {ln.launch_id}\n{_outcome_text(got)}"
                                       if ln is not None else f"no launch asked for: {st['why']}"))
            return 0 if got is None or got.started else 1
        if cmd == "watch":
            from levain.coordinator.launch import migrate_setting, watch, watch_once

            if args.every < 1:
                raise CoordError("--every is at least 1 second")
            if args.once:
                # the pass judges a report's session against the interval it was given (row 156 L3 X6)
                lines = migrate_setting(store) + watch_once(store, every=args.every)
                _emit(out, args.json, {"lines": lines}, "\n".join(lines) or "nothing to do")
                return 0
            try:
                watch(store, every=args.every, emit=lambda line: print(line, file=out, flush=True))
            except KeyboardInterrupt:
                return 0
            return 0
        if _no_store(store):
            _emit(out, args.json, {"store": None, "lease": None, "wake": "unset", "why": NO_STORE, "setting": None,
                                   "launches": []}, NO_STORE)
            return 0
        st = store.coord_status()
    except _REFUSED as exc:
        print(f"levain coord {cmd}: {exc}", file=sys.stderr)
        return 1
    lines = [_lease_line(st["lease"]), f"next drop: {st['wake']} ({st['why']})"]
    st["install_upgrade_pending"], st["install_settings_problem"] = False, None
    if st.get("setting"):
        lines.append(user_layer_text(st["setting"]))
        if st["setting"].get("agent") == "claude":
            from levain.coordinator.launch import default_coordinator_dir
            from levain.install import coordinator_read_rule_state

            cwd = st["setting"].get("cwd") or str(default_coordinator_dir())
            pending, problem = coordinator_read_rule_state(Path(cwd))
            st["install_upgrade_pending"], st["install_settings_problem"] = pending, problem or None
            if problem:
                lines.append(f"coordinator install: settings unreadable ({problem}), so its Read allow rule cannot be "
                             "added and a read outside its workspace waits on a prompt; fix that file by hand")
            elif pending:
                lines.append(f"coordinator install: upgrade pending (no Read allow rule, so a read outside its "
                             f"workspace waits on a prompt); run: levain update --path {shlex.quote(str(cwd))}")
    for ln in st["launches"]:
        spawn = f"  {ln['spawn']}" if ln.get("spawn") else ""
        lines.append(f"  {ln['launch_id']}  {ln['role']}{spawn}  {ln['harness']}  {ln['state']} ({ln['cause']})  "
                     f"session {ln['session_key']}  asked {ln['at']}")
    _emit(out, args.json, st, "\n".join(lines))
    return 0


def user_layer_text(setting: Any) -> str:
    """The setting's user-layer line, as ``coord agent`` and ``coord status`` say it (a Setting or its dict)."""
    agent, on = (setting["agent"], setting.get("user_scope")) if isinstance(setting, dict) else (
        setting.agent, setting.user_scope)
    if agent != "claude":
        return "user layer: not applicable (not a claude coordinator)"
    if on:
        return "user layer: on (the coordinator loads your user settings, hooks and CLAUDE.md)"
    return "user layer: off (project settings only)"


def _coord_agent(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord agent``: for claude, the directory is made a Levain install for Claude Code first when it is not
    one (``levain init``, non-interactive), and the setting is recorded only once its consent hook is live. The
    workspace goes in the coordinator record before the setting names it. ``codex`` is refused: the Codex coordinator
    was removed."""
    user_scope = None if args.user_scope is None else args.user_scope == "on"
    if args.agent is None:
        # `levain coord agent --user-scope on|off` alone: the standing setting is kept whole, only the layer choice moves
        if user_scope is None:
            raise CoordError("name an agent (claude or none), or give --user-scope on|off to change only that")
        if args.cwd is not None or args.model is not None:
            raise CoordError("--user-scope alone keeps the agent, model and directory; name the agent to change them")
        checked = None
        if user_scope:
            # turning it on: the hook check the next launch runs, now, so the refusal is this command's and not every
            # launch's (a user-level disableAllHooks the off setting was not consulting)
            from levain.coordinator.launch import hook_live

            was = store.coord_setting()
            if was is not None and was.agent == "claude":
                live, detail = hook_live(Path(was.cwd), user_scope=True)
                if not live:
                    raise CoordError(f"not set: with your user layer the consent hook is not live in {was.cwd}: "
                                     f"{detail}")
                checked = was.cwd
        got = store.coord_set_user_scope(user_scope, cwd=checked)
        _emit(out, args.json, {"agent": got.agent, "model": got.model, "cwd": got.cwd, "user_scope": got.user_scope},
              f"coordinator agent: {got.agent}; {user_layer_text(got)}")
        return 0
    if args.agent == "none":
        if args.cwd is not None or args.model is not None or user_scope is not None:
            raise CoordError("agent none takes no --model, no --cwd and no --user-scope")
        store.coord_set_agent("none")
        _emit(out, args.json, {"agent": "none"}, "coordinator agent: none (drops queue for you; nothing launches)")
        return 0
    from levain.coordinator import door
    from levain.coordinator.lease import check_model

    if args.agent == "codex":        # before the model check: the removal is the reason, whatever the model says
        raise CoordError("a Codex coordinator was removed: set claude or none. A Codex session you run yourself is "
                         "still wrapped by the consent queue, and `levain session codex` runs one as a worker")
    check_model(args.model)          # before any install is made for it (the setting checks it again as it is written)
    if args.agent != "claude":
        raise CoordError(f"a {args.agent} coordinator cannot be launched yet; set claude or none")
    from levain.coordinator.launch import default_coordinator_dir, setup_install, trust_remedy

    log = sys.stderr if args.json else out
    standing = store.coord_setting()
    effective = user_scope if user_scope is not None else (standing is not None and standing.user_scope)
    target, live, detail = setup_install(args.cwd or default_coordinator_dir(),
                                         emit=lambda line: print(line, file=log, flush=True), user_scope=effective)
    if not live:
        raise CoordError(f"not set: the consent hook is not live in {target}: {detail}")
    if not door.marked(target):
        # A live install Levain did not make for the coordinator is the operator's own: the door is open there, so a
        # coordinator launched into it would run his commands. It is never marked here either, since marking it would
        # door his own sessions in it. A fresh directory gets its own marked install.
        raise CoordError(f"not set: {target} is a Levain install, but not a coordinator's own (its "
                         ".levain/config.json records no coordinator role), and a coordinator runs only in its own "
                         f"workspace; leave out --cwd to use {default_coordinator_dir()}, or name a missing or empty "
                         "directory for Levain to set up")
    _record_workspace(target)
    got = store.coord_set_agent("claude", model=args.model, cwd=str(target), user_scope=user_scope)
    remedy = trust_remedy(target)
    _emit(out, args.json, {"agent": got.agent, "model": got.model, "cwd": got.cwd, "user_scope": got.user_scope,
                           "trust": remedy},
          f"coordinator agent: claude{f' ({got.model})' if got.model else ''} in {got.cwd}\n"
          f"{user_layer_text(got)}\n"
          "Claude Code starts a session only in a directory you trusted; run this once and accept the trust prompt:\n"
          f"{remedy}")
    return 0


def _band_snapshot(args: argparse.Namespace, store: EnvelopeStore) -> tuple[Any, str]:
    """The authority snapshot the envelope check reads, or ``(None, why)``: with none, nothing is live and every
    question escalates (an unchecked envelope never makes a question answerable)."""
    try:
        return _snapshot(args, store), ""
    except _faults() as exc:
        return None, str(exc)[:200]


def _coord_first_reader(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord questions|answer|escalate`` (C5): the coordinator reads the question band first."""
    from levain.coordinator import first_reader as fr

    cmd = args.coord_cmd
    if cmd == "escalate":
        from levain.consent.band import release_held_pings

        why = _text_input(args, args.why, "--why")
        if why is None:
            raise CoordError("say why: --why TEXT, --text-file PATH or --stdin")
        reply_id = fr.escalate(store, args.ref, why, token=args.token)
        released = release_held_pings(store.directory)        # a ping held for it goes now (C5-1)
        _emit(out, args.json, {"reply_id": reply_id, "pings": released},
              f"escalated: question {args.ref} waits on the operator (ANSWER {reply_id}); the question is untouched"
              + "".join(f"\n{line}" for line in released))
        return 0
    if cmd == "questions":
        authority, unchecked = _band_snapshot(args, store)
        rows = fr.read_band(store, authority=authority)
        obj = [{"n": r.question.n, "id": r.question.id, "agent": r.question.agent,
                "session": r.question.agent_session_id, "what": r.question.what,
                "options": [{"key": k, "label": v} for k, v in r.question.options], "rec": r.question.rec,
                "open_ended": r.question.open_ended, "blocks": r.question.blocks, "may_answer": r.may_answer, "why": r.why, "launch_id": r.launch_id,
                "thread_id": r.thread_id, "envelope_id": r.envelope_id} for r in rows]
        lines = [f"#{r.question.n} {json.dumps(r.question.what, ensure_ascii=False)}\n  "
                 + ("MAY ANSWER as a KNOW: " if r.may_answer else "ESCALATE: ") + r.why for r in rows]
        if unchecked:
            lines.append(f"(the envelopes were not checked: {unchecked})")
        _emit(out, args.json, {"questions": obj, "unchecked": unchecked or None},
              "\n".join(lines) if rows else "no unread questions" + (f" ({unchecked})" if unchecked else ""))
        return 0
    # the snapshot (a verified replay) is taken just before the answer's fenced transaction, and only the log's head is
    # compared inside it (C5 L3 r1, r2); with no head reader the snapshot is never live-checked again, so none is used
    try:
        head = _head_reader(args, store)
    except _faults():
        head = None
    item, reply_id = fr.answer(store, args.ref, option=args.option,
                           text=_text_input(args, args.text, "--text"), token=args.token,
                               authority_source=(lambda: _band_snapshot(args, store)[0]) if head is not None
                               else (lambda: None), authority_head=head)
    _emit(out, args.json, {"item_id": item.item_id, "reply_id": reply_id},
          f"KNOW {reply_id} filed for your review ({item.item_id}); the seat gets it labelled as the desk's "
          "own, and the question stays open for you")
    return 0


def _coord_log(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord log --since GSEQ``: the coordinator store's records after GSEQ in the store-wide order, oldest
    first (C11 S3): the same sources the work delta in a coordinator reply is made of, which is where its ``+N more``
    points. Display only; makes nothing."""
    from levain.coordinator import work_delta

    if _no_store(store):
        _emit(out, args.json, {"records": [], "total": 0, "through": args.since}, NO_STORE)
        return 0
    conn = store.open_readonly()
    try:
        delta = work_delta.work_delta(conn, args.since, args.limit)     # both in range: the parser's types
    finally:
        conn.close()
    shown = delta.lines
    text = "\n".join(f"{g}  {t}" for g, t in shown) if shown else f"nothing after gseq {args.since}"
    if delta.total > len(shown):
        text += f"\n+{delta.total - len(shown)} more: levain coord log --since {shown[-1][0]} --limit {args.limit}"
    _emit(out, args.json, {"records": [{"gseq": g, "text": t} for g, t in shown], "total": delta.total,
                           "through": delta.through}, text)
    return 0


def _coord_reply(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord reply DROP --text ... --token N`` (C11 S4): the coordinator answers words the operator dropped from
    the cockpit or the phone. The binding rules are the store's own trigger; the block stored with the reply is frozen."""
    from levain.coordinator import drop_reply

    text = _text_input(args, args.text, "--text")
    if text is None:
        raise CoordError("say what you reply: --text TEXT, --text-file PATH or --stdin")
    r = drop_reply.write_reply(store, args.drop_id, text, token=args.token)
    _emit(out, args.json, {"reply_id": r.reply_id, "drop_id": r.drop_id, "gseq": r.gseq, "block": r.block,
                           "covers": {"after": r.after, "through": r.through}},
          f"replied to drop {r.drop_id} ({r.surface}): reply {r.reply_id}, shown on the Desk panel"
          + (f"\n{r.block}" if r.block else ""))
    return 0


def _ready_line(it: dict[str, Any]) -> str:
    held = f"  [held: {it['held_reason']}]" if it.get("held_reason") else ""
    return (f"{it['id']}  {it['repo']}  {it['status_category']}  {it['tier'] or '-'}  rank {it['rank']}  "
            f"{it['title']}{held}")


def _coord_ready(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord ready show|add|move|correct`` (C13): the ready queue. ``show`` is a read (no lease, no
    coordinator live). A write maps exactly to ``ready.add`` / ``move`` / ``correct`` and is fenced by the live lease
    token (``--token``); every refusal is printed plainly and exits 2, with nothing written. On success it prints the
    journal record's id (item and version)."""
    from levain.coordinator import ready

    verb = args.ready_cmd
    try:
        if verb == "show":
            where = str(Path(str(store.path)).parent)
            if _no_store(store):            # the same keys as a present store's answer, only empty
                _emit(out, args.json, {"schema": ready.SCHEMA_VERSION,
                                       "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
                                       "items": [], "unreadable": [], "store": None},   # None = no store; a present store names its dir
                      f"{NO_STORE}: nothing is queued")
                return 0
            got = {**ready.list_ready(store, all=args.all, repo=args.repo), "store": where}
            lines = [_ready_line(i) for i in got["items"]]
            if not lines and not got["unreadable"]:       # absence is only said when nothing was left unread
                lines = ["nothing queued"]
            lines += [f"unreadable: {u}" for u in got["unreadable"]]
            _emit(out, args.json, got, "\n".join(lines))
            return 0
        given = {"title": args.title, "tier": args.tier, "done_line": args.done_line, "rank": args.rank,
                 "depends_on": args.depends_on}
        extra = {k: v for k, v in given.items() if v is not None}
        if verb != "add":                   # --no-tier / --no-done-line: an explicit None clears the field
            if args.no_tier:
                extra["tier"] = None
            if args.no_done_line:
                extra["done_line"] = None
        if verb == "add":
            rec = ready.add(store, args.token, args.item_id, repo=args.repo, title=args.title, who=args.who,
                            why=args.why, to=args.to, tier=args.tier, done_line=args.done_line,
                            depends_on=args.depends_on or (), rank=args.rank)
        else:
            rec = (ready.move if verb == "move" else ready.correct)(store, args.token, args.item_id, to=args.to,
                                                                    who=args.who, why=args.why, **extra)
        ver = rec.ver
    except _REFUSED as exc:
        print(f"levain coord ready {verb}: {exc}", file=sys.stderr)
        return 2
    _emit(out, args.json, {"journal_id": f"{rec['id']}@{ver}", "record": rec},
          f"{verb}: {rec['id']} {rec['from_category'] or '-'} -> {rec['to_category']}; journal {rec['id']}@{ver}")
    return 0


def _coord_import_status(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord import-status PATH [--repo R]``: read a status file's lane rows into the ready queue. It is the
    coordinator's write, fenced by the live lease token; without it nothing is read or written and the exit is 1.
    One line per bucket, with its count and ids."""
    from levain.coordinator import status_import

    held = None if _no_store(store) else store.coord_lease()
    if held is None or not held.live or held.token != args.token:
        print("levain coord import-status: the coordinator lease is not held by this token (levain coord status "
              "shows the live one); nothing was read or written", file=sys.stderr)
        return 1
    try:
        got = status_import.import_status(store, args.token, args.path, repo=args.repo)
    except _REFUSED as exc:
        print(f"levain coord import-status: {exc}", file=sys.stderr)
        return 2
    buckets = {"added": got.added, "corrected": got.corrected, "unchanged": got.unchanged, "absent": got.absent,
               "unparsed": got.unparsed}
    _emit(out, args.json, buckets,
          "\n".join(f"{name}: {len(ids)}" + (f" ({', '.join(str(i) for i in ids)})" if ids else "")
                    for name, ids in buckets.items()))
    return 0


_SLOTS_FILE_MAX = 64 * 1024


def _coord_report(args: argparse.Namespace, out: Any) -> int:
    """``levain coord report ITEM --worker W --event E [...]`` (row 156): a seat reports a fact about its work on a ready
    item. It appends to the board store's ``reports.db`` queue (:func:`levain.coordinator.work_attach.queue_report`) and
    never opens ``dispatch.db`` or takes a token; the watcher's applier copies it into ``worker_attach`` or refuses it
    with a record. The session comes from ``--session`` or the harness's own environment."""
    from levain.coordinator import work_attach

    directory = args.board_store.expanduser() if args.board_store is not None else work_attach.board_store_dir()
    session = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rid, queued = work_attach.queue_report(
            directory, report_id=args.report_id, item_id=args.item, worker=args.worker, worker_kind=args.kind,
            event=args.event, at=at, launch_id=args.launch, harness_session_id=session, sha=args.sha,
            branch=args.branch, why=args.why)
    except _REFUSED as exc:
        # a malformed field or no board directory: one line, exit 2, nothing queued
        print(f"levain coord report: {' '.join(str(exc).split())}", file=sys.stderr)
        return 2
    words = work_attach.report_line(directory, rid, queued)
    _emit(out, args.json, {"report_id": rid, "queued": queued, "item": args.item, "store": str(directory)}, words)
    return 0


def _coord_reports(args: argparse.Namespace, out: Any = None) -> int:
    """``levain coord reports REPORT_ID`` (row 156 L2 F16): what became of one report, read-only (queued, applied,
    refused with its reason, or unknown). Opens no store for writing and takes no token. Exit 1 when the id is unknown."""
    from levain.coordinator import work_attach

    out = out or sys.stdout
    directory = args.board_store.expanduser() if args.board_store is not None else work_attach.board_store_dir()
    try:
        st = work_attach.report_status(directory, args.report_id)
    except (CoordError, sqlite3.Error, OSError) as exc:
        print(f"levain coord reports: {exc}", file=sys.stderr)
        return 2
    words = {"queued": "queued; the next `levain coord watch` pass applies or refuses it",
             "applied": f"applied ({st['kind']}, {st['attach_id']})",
             "refused": f"refused ({st['reason']}: {st['detail']})",
             "unknown": f"not in the queue at {directory}"}[st["state"]]
    _emit(out, args.json, st, f"report {args.report_id}: {words}")
    return 1 if st["state"] == "unknown" else 0


def _coord_spawn(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord spawn --thread T (--role @spawn/NAME | --inline FILE) --cwd DIR --token N`` (P3): the lease holder
    asks for one worker on a WORK thread (:func:`levain.agents.instantiate.spawn_worker`). The store derives the depth,
    the drop and the scope in the request's own transaction; ``coord watch`` or ``coord launch`` starts it."""
    from levain.agents import instantiate
    from levain.agents import role as role_mod
    from levain.agents.store import AgentStoreRefused, open_agent_store

    inline = None
    if args.inline is not None:
        try:
            inline = role_mod.parse_role_text(args.inline.expanduser().read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise CoordError(f"--inline {args.inline} could not be read ({type(exc).__name__}: {exc})") from None
        except role_mod.RoleError as exc:
            raise CoordError(f"--inline {args.inline} is not a role: {exc}") from None
    slots: dict[str, Any] = {}
    if args.slots_file is not None:
        try:
            raw = args.slots_file.expanduser().read_text(encoding="utf-8")
            if len(raw) > _SLOTS_FILE_MAX:
                raise CoordError(f"--slots-file is at most {_SLOTS_FILE_MAX} characters")
            got = json.loads(raw)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise CoordError(f"--slots-file {args.slots_file} is not a JSON object of slot values "
                             f"({type(exc).__name__}: {exc})") from None
        if not isinstance(got, dict):
            raise CoordError("--slots-file holds one JSON object: slot name -> value")
        slots.update(got)
    for pair in args.slot or ():
        name, eq, value = pair.partition("=")
        if not eq or not name:
            raise CoordError(f"--slot is NAME=VALUE (got {pair[:40]!r})")
        if name in slots:
            raise CoordError(f"slot {name!r} is given twice")
        slots[name] = value
    cwd = os.path.realpath(args.cwd.expanduser())
    try:
        agents = open_agent_store(args.agent_store_dir)
    except AgentStoreRefused as exc:
        raise CoordError(f"the role registry cannot be opened ({exc})") from None
    ln = instantiate.spawn_worker(store, args.token, thread_id=args.thread, cwd=cwd, ref=args.role, inline=inline,
                                  slots=slots, agent_store=agents, parent_launch=args.parent, done=args.done,
                                  item=args.item)
    rec = instantiate.verified_record(ln.role_ref)
    _emit(out, args.json, {"launch_id": ln.launch_id, "state": ln.state, "spawn": rec["spawn"],
                           "inline": rec["inline"], "depth": rec["depth"], "thread": args.thread, "cwd": cwd,
                           "envelope_id": rec["envelope_id"], "scope": rec["scope"]},
          f"asked for worker launch {ln.launch_id} ({rec['spawn']}{', inline' if rec['inline'] else ''}, depth "
          f"{rec['depth']}) on thread {args.thread} in {cwd}: {ln.state} until {ln.until}; `levain coord watch` or "
          f"`levain coord launch {ln.launch_id}` starts it")
    return 0


def _coord_worker_handoff(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord handoff worker LAUNCH_ID --token N`` (P3): the handoff a done spawned worker left, read by the lease
    holder (:meth:`CoordStore.launch_handoff`). Its text is the worker's own words, shown with every hidden character
    marked, line breaks kept."""
    got = store.launch_handoff(args.token, args.launch_id)
    text = None if got["handoff"] is None else unshowable.mark(got["handoff"], allow="\n\t")
    head = f"worker launch {got['launch_id']}: handoff kept {got['at']}"
    if text is None:
        body = f"{head}; no final message: {got['why']}"
    else:
        body = f"{head}" + (f" ({got['why']})" if got["why"] else "") + f"\n\n{text}"
    _emit(out, args.json, {**got, "handoff": text}, body)
    return 0


LOG_LIMIT_MAX = 500
_GSEQ_MAX = 2 ** 63 - 1                     # SQLite's INTEGER: a larger value is an OverflowError at the bind


def _int_in(low: int, high: int) -> Callable[[str], int]:
    """An argparse type for a whole number in ``low..high``: anything else is argparse's one-line refusal (exit 2)."""
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text[:40]!r} is not a whole number") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be {low}..{high}, got {text[:40]}")
        return value

    return parse


def _coord_c6(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord morning|away|budget`` (C6): the morning card, away from your word, the thread budget gauges."""
    from levain.coordinator import budget, morning

    cmd = args.coord_cmd
    if cmd == "morning":
        c = morning.card(store)
        _emit(out, args.json, c, morning.render(c))
        return 0
    if cmd == "away":
        got = morning.away_from_drop(store, args.drop_id, token=args.token)
        skipped = "".join(f"\n  NOT set for {a}: {w}; its SIGNs are not held as away" for a, w in
                          got["skipped"].items())
        only = " only" if got["skipped"] else ""
        _emit(out, args.json, got, f"away ON for {', '.join(got['marked'])}{only}, on the operator's word in drop "
                                   f"{args.drop_id}; KNOW {got['item_id']} tells him. Only he can leave it.{skipped}")
        return 0
    threads = [args.thread] if args.thread else [t.thread_id for t in store.threads()]
    with read_transaction(store._connect) as conn:
        gauges = [g for g in (budget.gauge(conn, store.directory, t) for t in threads) if g is not None]
    if args.thread and not gauges:
        raise CoordError(f"thread {args.thread[:40]} has no envelope, so no budget")
    lines = []
    for g in gauges:
        if g.unreadable is not None:
            # never "crossed" with zero usage: the meter could not be read (L3 r1 U1)
            lines.append(f"{g.thread_id} ({g.envelope_id}): UNREADABLE: {g.refusal}")
            continue
        used = ", ".join(f"{k} {budget._num(k, g.used[k])} of {g.budget[k]}" for k in budget.METERS)
        lines.append(f"{g.thread_id} ({g.envelope_id}): {used}; dollars not metered (Q13 counts none)")
        lines.append(f"  {g.refusal or 'within budget: new steps may start'}")
        if g.uncounted:
            lines.append(f"  not counted by Q13 yet: {', '.join(r for _a, r in g.uncounted)}")
    _emit(out, args.json, {"gauges": [g.as_dict() for g in gauges]}, "\n".join(lines) or "no thread has an envelope")
    return 0


def _coord_review_feeds(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord review feeds``: one exclusive run. The ``--max-seconds`` bound is :func:`run_coord`'s, around the
    whole command."""
    from levain.coordinator import deliver

    ran = deliver.run_exclusive(store)
    if ran is None:
        _emit(out, args.json, {"skipped": True, "feeds": {}, "delivery": {}, "new_items": 0,
                               "reason": "another feeds run holds the lock"},
              "feeds: skipped, another run is in progress")
        return 0
    delivery = ran["delivery"]
    faults = deliver.faults(ran)
    ran["new_items"] = len(ran["feeds"].get("created", []))
    lines = [f"feeds: {ran['new_items']} new review item(s)",
             f"delivery: {len(delivery['signs'])} SIGN and {len(delivery['answers'])} ANSWER pointer(s) "
             f"reached, {delivery['created']} new"]
    lines += [f"could not read: {f}" for f in faults]
    _emit(out, args.json, ran, "\n".join(lines))
    return 1 if faults else 0


def _flush(out: Any) -> None:
    flush = getattr(out, "flush", None)
    if flush is not None:
        flush()


def _coord_review(args: argparse.Namespace, out: Any, store: EnvelopeStore) -> int:
    """``levain coord review list|clear|reopen|feeds`` (C4-a): the review queue. ``list`` and ``feeds`` run the feed
    runner first (:func:`levain.coordinator.deliver.run`, the one call a panel's read path will make too)."""
    from levain.coordinator import deliver, review

    cmd = args.review_cmd
    if cmd in ("clear", "reopen"):
        act = review.clear_item if cmd == "clear" else review.reopen_item
        item = act(store, args.item_id, mode=args.mode, token=args.token)
        _emit(out, args.json, {"item_id": item.item_id, "state": item.state, "mode": args.mode},
              f"{item.item_id} {'cleared' if cmd == 'clear' else 'reopened'} ({args.mode})")
        return 0
    if cmd == "feeds":
        return _coord_review_feeds(args, out, store)
    ran = deliver.run(store)
    faults = deliver.faults(ran)
    waiting = review.waiting_on_you(store, sign_open=review.queue_request_open(store.directory),
                                    answer_open=review.question_open(store.directory))
    items = review.list_items(store, include_cleared=args.all, feed=args.feed)
    rows = [{"item_id": i.item_id, "feed": i.feed, "thread_id": i.thread_id, "state": i.state,
             "authority_relevant": i.authority_relevant, "at": i.at,
             "text": str(review.body_fields(i).get("text", ""))[:500]}
            for i in items]
    lines = [f"review queue: {len(rows)} {'item(s)' if args.all else 'open item(s)'}"]
    for r in rows:
        lines.append(f"  {r['item_id']}  {r['feed']}{' (yours to clear)' if r['authority_relevant'] else ''}"
                     f"{'' if r['state'] == 'open' else ' [cleared]'}"
                     + (f"  [thread {r['thread_id']}]" if r["thread_id"] else "") + f": {r['text'][:160]}")
    lines += deliver.waiting_lines(waiting)
    lines += [f"could not read: {f}" for f in faults]
    _emit(out, args.json, {"items": rows, "waiting": waiting, "errors": faults}, "\n".join(lines))
    return 1 if faults else 0


def _record_workspace(target: Path) -> None:
    """Put ``target`` in the coordinator record the door reads (head ruling R1), before any setting names it."""
    from levain.coordinator import door

    try:
        door.record(target)
    except (OSError, door.RecordUnreadable) as exc:
        raise CoordError(f"not set: {target} could not be put in the coordinator record ({exc})") from exc


def review_feeds() -> tuple[str, ...]:
    from levain.coordinator import review

    return review.FEEDS


def review_modes() -> tuple[str, ...]:
    from levain.coordinator import review

    return review.MODES


def _report_kinds() -> tuple[str, ...]:
    from levain.coordinator import work_attach

    return work_attach.WORKER_KINDS


def _report_events() -> tuple[str, ...]:
    from levain.coordinator import work_attach

    return work_attach.EVENTS


def _spawn_caps() -> tuple[int, int]:
    from levain.coordinator import lease

    return lease.MAX_SPAWN_DEPTH, lease.MAX_SPAWNS_LIVE


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    from levain.coordinator import seam_handoffs as _seam_handoffs

    qdir_help = "The consent directory holding dispatch.db (default: the consent queue's)."

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=qdir_help)
        p.add_argument("--json", action="store_true", help="Machine-readable output.")

    def token(p: argparse.ArgumentParser) -> None:
        p.add_argument("--token", type=int, default=None,
                       help="With --by coordinator: the coordinator lease's token (levain coord status shows it); "
                            "the write is refused unless that token is the live lease's.")

    d = subparsers.add_parser(
        "drop", help="Drop words for the desk and go (kept verbatim; never authority).", allow_abbrev=False,
        description="Store your words as a drop for the desk and print its drop receipt. The words are data: "
                    "they authorise nothing. Read them back with `levain drops`. Words that start with '-' go after "
                    "`--` (levain drop -- -words) or through stdin (levain drop -).")
    d.add_argument("words", nargs="+", help="The words (or - to read them from stdin).")
    d.add_argument("--outbox-id", dest="outbox_id", default=None,
                   help="An idempotency key: a resend with the same key and words writes nothing.")
    d.add_argument("--no-source", dest="no_source", action="store_true",
                   help="Record no repo or branch, even inside a git work tree.")
    d.add_argument("--envelope", type=Path, default=None, metavar="SPEC_FILE",
                   help="Send a structured envelope with the drop: a JSON file {where, lands, pushes, publishes, "
                        "budget, stop}, signed with --signing-key. The words are kept either way.")
    d.add_argument("--signing-key", dest="signing_key", type=Path, default=None,
                   help="An enrolled key that signs the envelope (its private key file, or the .pub of a key an "
                        "agent holds).")
    d.add_argument("--data-root", dest="data_root", type=Path, default=None,
                   help="The cockpit data root whose authority log verifies the signature.")
    d.add_argument("--yes", action="store_true",
                   help="With --envelope: do not ask y/N; the whole view is still printed.")
    common(d)
    d.set_defaults(func=run_drop)

    ds = subparsers.add_parser("drops", help="Your drops: list, show, and split (the desk's door).")
    d_sub = ds.add_subparsers(dest="drops_cmd", required=True)
    sp = d_sub.add_parser("split", help="Name the threads a drop becomes (the desk's door).")
    sp.add_argument("drop_id", nargs="?", default=None,
                    help="The drop (or leave it out and name it as drop_id in the JSON).")
    sp.add_argument("--json-file", dest="json_file", type=Path, default=None, metavar="PATH",
                    help='The whole split as one JSON document read from this file (no shell quoting): '
                         '{"drop_id": ID, "threads": [{"kind", "title", "about"?}], "attach": [{"kind", "thread"}]}.')
    sp.add_argument("--stdin", action="store_true", help="The same JSON document, read from stdin.")
    sp.add_argument("--thread", action="append", default=[], metavar="KIND:TITLE",
                    help=f"A new thread holding the drop; KIND is one of {', '.join(KINDS)}. Repeatable.")
    sp.add_argument("--attach", action="append", default=[], metavar="KIND:THREAD",
                    help="Put the drop into an existing open thread of that kind. Repeatable.")
    sp.add_argument("--about", default=None, metavar="THREAD",
                    help="The work thread each new ask/note/relay thread here concerns, or @N for this split's own "
                         "N-th --thread.")
    sp.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    token(sp)
    common(sp)
    sp.set_defaults(func=run_split)

    nt = d_sub.add_parser("note", help="Mark a drop read with nothing to do (the desk's terminal receipt).")
    nt.add_argument("drop_id")
    nt.add_argument("--why", default=None, metavar="ONE_LINE", help="Why it needs nothing; shown beside the drop.")
    _text_flags(nt)
    nt.add_argument("--by", default="coordinator", choices=("coordinator",))
    token(nt)
    common(nt)
    nt.set_defaults(func=run_note)

    sh = d_sub.add_parser("show", help="One drop: its words, its threads, its drop receipts.")
    sh.add_argument("drop_id")
    common(sh)
    sh.set_defaults(func=run_drop_show)

    ls = d_sub.add_parser("list", help="Recent drops, newest first.")
    ls.add_argument("--limit", type=int, default=50)
    common(ls)
    ls.set_defaults(func=run_drop_list)
    door_ok(sp, nt, sh, ls)      # all four of the drops tree are the coordinator's

    t = subparsers.add_parser("thread", help="The threads your drops became.")
    t_sub = t.add_subparsers(dest="thread_cmd", required=True)
    tl = t_sub.add_parser("list", help="Threads not yet closed (with --all, every thread).")
    tl.add_argument("--all", action="store_true")
    common(tl)
    ts = t_sub.add_parser("show", help="One thread: its revisions and events.")
    ts.add_argument("thread_id")
    common(ts)
    tst = t_sub.add_parser("state", help=f"Move a thread ({' -> '.join(STATES)}; reopen to open).")
    tst.add_argument("thread_id")
    tst.add_argument("state", choices=STATES)
    tst.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    token(tst)
    tst.add_argument("--cause", default="")
    tst.add_argument("--cause-file", dest="cause_file", type=Path, default=None, metavar="PATH",
                     help="Read the cause from this file (no shell quoting).")
    common(tst)
    tm = t_sub.add_parser("merge", help="Fold OTHER into INTO (one intent the split cut in two).")
    tm.add_argument("into")
    tm.add_argument("other")
    tm.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    token(tm)
    common(tm)
    td = t_sub.add_parser("detach", help="Take a drop out of a thread (it must stay in another).")
    td.add_argument("thread_id")
    td.add_argument("drop_id")
    td.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    token(td)
    common(td)
    ta = t_sub.add_parser("add", help="Put an already-split drop into one more thread.")
    ta.add_argument("drop_id")
    g = ta.add_mutually_exclusive_group(required=True)
    g.add_argument("--thread", metavar="KIND:TITLE")
    g.add_argument("--attach", metavar="KIND:THREAD")
    _text_flags(g)
    ta.add_argument("--about", default=None, metavar="THREAD")
    ta.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    token(ta)
    common(ta)
    tf = t_sub.add_parser("set", help="Re-kind, re-title or set the repo of a thread.")
    tf.add_argument("thread_id")
    tf.add_argument("field", choices=("kind", "title", "repo"))
    tf.add_argument("value", nargs="?", default=None)
    _text_flags(tf)
    tf.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    token(tf)
    tf.add_argument("--cause", default="")
    tf.add_argument("--cause-file", dest="cause_file", type=Path, default=None, metavar="PATH",
                    help="Read the cause from this file (no shell quoting).")
    common(tf)
    for p in (tl, ts, tst, tm, td, ta, tf):
        p.set_defaults(func=run_thread)
    door_ok(tl, ts, tst, tm, td, ta, tf)      # every thread verb is the coordinator's (a write goes --by coordinator)

    ch_sub = _chat_subparsers(subparsers)
    chs = ch_sub.add_parser(
        "show", help="One thread's chat timeline in the store's order: messages, what the worker's thread showed "
                     "(quoted, never your words), replies and envelope rows. Writes no row.")
    chs.add_argument("thread_id")
    common(chs)
    chs.set_defaults(func=run_chat)

    c = subparsers.add_parser(
        "coord", help="The coordinator lease: who holds the coordinator role, and the sessions launched for it.",
        description="One session holds the coordinator role at a time, or you do. Its writes carry the lease token "
                    "and are refused once the lease is lost or past its until. A drop with no holder asks for a "
                    "coordinator launch. Before S1 the lease is not authority: it decides whose thread writes count.")
    c_sub = c.add_subparsers(dest="coord_cmd", required=True)
    cs = c_sub.add_parser("status", help="Who holds the lease, what the next drop would do, the latest launches.")
    ct = c_sub.add_parser("take", help="Hold the coordinator role yourself (voids every pending launch).")
    ct.add_argument("--for", dest="hold", default=None, metavar="DURATION",
                    help="How long you hold it (like 30m or 2h; default 30m).")
    cr = c_sub.add_parser("release", help="End the lease (yours, or a session's: you looking after it).")
    cr.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    token(cr)
    cb = c_sub.add_parser("beat", help="The holder keeps the lease (moves its until on).")
    cb.add_argument("--token", type=int, required=True)
    cc = c_sub.add_parser("claim", help="An active coordinator session claims a lease nobody holds.")
    cc.add_argument("--launch", required=True, metavar="LAUNCH_ID")
    cc.add_argument("--session", required=True, metavar="SESSION_KEY")
    cw = c_sub.add_parser("wake", help="Ask for a coordinator now, by the rules a drop runs (after a backoff); a "
                                       "claude coordinator running without the lease whose session is done is "
                                       "ended and a fresh one started.")
    ca = c_sub.add_parser(
        "agent", help="Choose the coordinator a drop launches: claude (in its own workspace), or none.",
        description="claude: drops with no coordinator start a Claude Code session in DIR (default "
                    "<levain home>/coordinator), which is made a Levain install for Claude Code first when it is not "
                    "one. The workspace is put in Levain's coordinator record, which the desk's "
                    "door reads. codex: removed (a Codex coordinator is no longer launched). none: drops only queue for you. Levain never edits a harness's trust list: run the "
                    "printed line once and trust the directory.")
    ca.add_argument("agent", nargs="?", default=None, choices=("claude", "codex", "none"))
    ca.add_argument("--user-scope", choices=("on", "off"), default=None, dest="user_scope",
                    help="claude: whether the coordinator also loads your user-level Claude Code settings, hooks, "
                         "CLAUDE.md (default off: it loads the project and local layers only). Alone "
                         "(no agent) it changes only this and keeps the standing agent, model and directory.")
    ca.add_argument("--cwd", type=Path, default=None, metavar="DIR",
                    help="The directory the coordinator runs in (default: <levain home>/coordinator): "
                         "a missing or empty one, which Levain sets up, or a coordinator "
                         "workspace Levain set up before. Your own Levain install is refused.")
    ca.add_argument("--model", default=None, help="The model the session runs (default: the harness's own).")
    cl = c_sub.add_parser("launch", help="Start a pending launch's session now (default: the pending coordinator's).")
    cl.add_argument("launch_id", nargs="?", default=None, metavar="LAUNCH_ID")
    cwa = c_sub.add_parser(
        "watch", help="Keep the coordinator's sessions honest: start pending launches, end the gone, beat the live.",
        description="Each pass starts every pending launch nobody started (a drop from the phone or the cockpit), "
                    "ends a launch whose Claude Code session is no longer listed, beats the lease of the coordinator "
                    "whose session is listed, not done and writing as coordinator, and stops a session whose launch "
                    "is over. Only sessions Levain started. Launches made in the board store are started by "
                    "`coord watch --queue-dir <levain home>/board`; the board's coordinator verbs take that "
                    "--queue-dir explicitly.")
    cwa.add_argument("--every", type=float, default=30.0, metavar="S", help="Seconds between passes (default 30).")
    cwa.add_argument("--once", action="store_true", help="One pass, then exit.")
    band_root = ("The cockpit data root whose authority log re-verifies the asking session's envelope (default: the one "
                 "the consent hooks in the queue directory name).")
    cq = c_sub.add_parser(
        "questions", help="The seats' open questions you have not read yet, each with whether you may answer it.",
        description="You read the question band first (Q17). You may answer a question only when it was asked by a "
                    "worker Levain started under a live envelope, from inside a repo the envelope names, and no "
                    "option of it is tied to a consent request; everything else you escalate. Whether its subject "
                    "is inside the envelope is your judgement: the operator reviews every answer you give. A "
                    "question's directory and session are the asking process's own word, so 'asked from inside the "
                    "envelope' is a label, not proof.")
    cq.add_argument("--data-root", dest="data_root", type=Path, default=None, help=band_root)
    can = c_sub.add_parser(
        "answer", help="Answer a seat's question as your own KNOW (never the operator's answer).",
        description="Files a KNOW (the operator's review queue, never a push) and delivers it to the asking session "
                    "labelled as yours. The question stays open: the operator may still answer, and his answer is "
                    "the one that counts. Refused (escalate instead) outside the envelope.")
    can.add_argument("ref", help="The question's number or id.")
    pick = can.add_mutually_exclusive_group(required=True)
    pick.add_argument("--option", default=None, help="The option KEY you pick.")
    pick.add_argument("--text", default=None, help="Your answer in your own words.")
    pick.add_argument("--text-file", dest="text_file", type=Path, default=None, metavar="PATH",
                      help="Your answer, read from this file (no shell quoting).")
    pick.add_argument("--stdin", action="store_true", help="Your answer, read from stdin.")
    can.add_argument("--token", type=int, required=True, help="Your coordinator lease token.")
    can.add_argument("--data-root", dest="data_root", type=Path, default=None, help=band_root)
    ces = c_sub.add_parser("escalate", help="Leave a seat's question to the operator (an ANSWER waiting on him).")
    ces.add_argument("ref", help="The question's number or id.")
    ces.add_argument("--why", default=None, help="Why it is the operator's, one line.")
    _text_flags(ces)
    ces.add_argument("--token", type=int, required=True, help="Your coordinator lease token.")
    cmo = c_sub.add_parser(
        "morning", help="The morning card: away, what waits UNDECIDED, thread budgets, what waits on you.",
        description="What waits for you after a night away, read from the stores (display only: every line names the "
                    "command that decides it, which is yours).")
    clg = c_sub.add_parser(
        "log", help="The coordinator store's records after a gseq, oldest first (what a reply's +N more points at).",
        description="Replies, launch events, lease events, ready moves and chat messages, in the store-wide order. "
                    "Thread splits and state changes carry no gseq and are not here (levain thread list shows them).")
    clg.add_argument("--since", type=_int_in(0, _GSEQ_MAX), required=True, metavar="GSEQ",
                     help="List records with a greater gseq.")
    clg.add_argument("--limit", type=_int_in(1, LOG_LIMIT_MAX), default=50, metavar="N",
                     help=f"At most N records, 1..{LOG_LIMIT_MAX} (default 50).")
    crp = c_sub.add_parser(
        "reply", help="Answer words dropped from the cockpit or the phone (one reply per drop, within 24 hours).",
        description="Files your reply to a drop, shown on the Desk panel with Levain's block frozen at this "
                    "moment (what changed since his last message, and the question band). The store refuses a drop "
                    "that does not exist, a second reply to a drop, and a drop older than the window.")
    crp.add_argument("drop_id", metavar="DROP", help="The drop you answer.")
    crp.add_argument("--text", default=None, help="Your reply.")
    _text_flags(crp)
    crp.add_argument("--token", type=int, required=True, help="Your coordinator lease token.")
    csp = c_sub.add_parser(
        "spawn", help="Ask for one worker on a WORK thread, from a registered role (@spawn/NAME) or an inline one.",
        description="The worker runs the role's brief with your slot values, in DIR (outside every coordinator "
                    "workspace), under the consent hook. Its scope is the thread's envelope cut down to the role's "
                    "ceiling, once, when it is asked for; nothing about the thread's envelope changes. --parent "
                    f"declares the lineage (the active spawned worker this one is for; at most {_spawn_caps()[0]} "
                    "deep by what you declare): a worker holds no token and no coordinator verb, so you are the "
                    "only spawner, and what binds is your lease and the cap of "
                    f"{_spawn_caps()[1]} asked for or running at once. `levain coord watch` or "
                    "`levain coord launch LAUNCH_ID` starts it, `levain coord status` lists it, and "
                    "`levain coord handoff worker LAUNCH_ID` reads its final message once its turn is done. "
                    "A launch made in the board store is started by `coord watch --queue-dir <levain home>/board`.")
    csp.add_argument("--thread", required=True, metavar="THREAD", help="The WORK thread the worker holds.")
    who = csp.add_mutually_exclusive_group(required=True)
    who.add_argument("--role", default=None, metavar="@spawn/NAME", help="A registered role.")
    who.add_argument("--inline", type=Path, default=None, metavar="FILE", help="A role, as JSON in this file.")
    csp.add_argument("--slot", action="append", default=None, metavar="NAME=VALUE",
                     help="A text or path slot's value (repeat per slot).")
    csp.add_argument("--slots-file", dest="slots_file", type=Path, default=None, metavar="PATH",
                     help="Slot values as one JSON object (an int slot goes here).")
    csp.add_argument("--cwd", type=Path, required=True, metavar="DIR", help="The directory the worker runs in.")
    csp.add_argument("--done", default=None, metavar="LINE", help="The ready item's DONE: line (the worker's win).")
    csp.add_argument("--parent", default=None, metavar="LAUNCH_ID",
                     help="The active spawned worker asking for this one.")
    csp.add_argument("--item", default=None, metavar="ITEM_ID",
                     help="The ready item this worker is for: the attach is recorded with the request, or neither is. "
                          "With it the store is <levain home>/board unless --queue-dir names one, so the launch is "
                          "asked for there too.")
    csp.add_argument("--agent-store-dir", dest="agent_store_dir", type=Path, default=None, metavar="DIR",
                     help="The role registry's store (default: this Levain home's).")
    csp.add_argument("--token", type=int, required=True, help="Your coordinator lease token.")
    crt = c_sub.add_parser(
        "report", help="Report a fact about your work on a ready item (queued; no token, no lease).",
        description="Appends one report to the board store's queue and returns. It never writes the board itself: the "
                    "watcher's applier copies it into the worker-attach record, or refuses it (an unknown item, a worker "
                    "that is not a real launch) and leaves a refusal record the desk reads. Resending the same "
                    "--report-id queues nothing twice and says so. Your session id comes from the environment.")
    crt.add_argument("item", metavar="ITEM", help="The ready item's id.")
    crt.add_argument("--worker", required=True, help="Who you are: a seat or lane name, or a launch id.")
    crt.add_argument("--kind", choices=_report_kinds(), default="seat", help="What kind of worker (default seat).")
    crt.add_argument("--event", required=True, choices=_report_events(), help="The fact you report.")
    crt.add_argument("--launch", default=None, metavar="LAUNCH_ID", help="Your Levain launch id, if you are one.")
    crt.add_argument("--session", default=None, metavar="ID",
                     help="Your harness session id (default: CLAUDE_CODE_SESSION_ID from the environment).")
    crt.add_argument("--sha", default=None, metavar="OID", help="With --event landed: the full commit id.")
    crt.add_argument("--branch", default=None, help="With --event landed: the branch it landed on.")
    crt.add_argument("--why", default=None, metavar="LINE", help="One line of detail.")
    crt.add_argument("--report-id", dest="report_id", default=None, metavar="ID",
                     help="Your own id for this report; pass the same one to resend it safely (default: a fresh one).")
    crt.add_argument("--board-store", dest="board_store", type=Path, default=None, metavar="DIR",
                     help="The board store directory (default: <levain home>/board).")
    crt.add_argument("--json", action="store_true", help="Machine-readable output.")
    crt.set_defaults(func=run_coord)
    crs = c_sub.add_parser(
        "reports", help="What became of a report you queued: queued, applied, or refused and why (read-only).",
        description="Reads the board store's queue and its records for one report id. Writes nothing; no token.")
    crs.add_argument("report_id", metavar="REPORT_ID", help="The id `levain coord report` printed.")
    crs.add_argument("--board-store", dest="board_store", type=Path, default=None, metavar="DIR",
                     help="The board store directory (default: <levain home>/board).")
    crs.add_argument("--json", action="store_true", help="Machine-readable output.")
    crs.set_defaults(func=_coord_reports)
    door_ok(crs)
    caw = c_sub.add_parser(
        "away", help="Read a drop as the operator's word that he is away: set away ON (never off).",
        description="Sets away ON for both agents through `levain consent away on`'s own path, so every SIGN waits "
                    "UNDECIDED for his morning card, and files a KNOW saying so. Leaving away takes his signature.")
    caw.add_argument("drop_id", metavar="DROP", help="The drop whose words say he is away.")
    caw.add_argument("--token", type=int, required=True, help="Your coordinator lease token.")
    ccl = c_sub.add_parser(
        "close", help="Leave a handoff for the next coordinator (a rotation, the operator leaving, a dream).",
        description="Appends one seam handoff from a JSON body file with every key (state_line, in_flight, next, "
                    "contingencies, notes, attested_operator_present, attested_walked; an empty list is []). A retry "
                    "with the same --request-id returns the same handoff. Names the latest handoff you saw as "
                    "--prev-handoff-id (omit it when none exists): a late close behind a newer one is refused. "
                    "In the body, name rows by id (dr-..., item keys), never by ordinal: the next coordinator "
                    "sees ids, not your order. Each field is one line (a newline or control is refused). "
                    "--plan instead prints the operator-walk checklist (read-only; it writes nothing and takes no "
                    "--body-file): what to walk with him before you write the operator handoff.")
    ccl.add_argument("--plan", action="store_true",
                     help="Print the operator-walk checklist, derived from the stores; writes nothing.")
    ccl.add_argument("--kind", default=None, choices=_seam_handoffs.KINDS, help="Required unless --plan.")
    ccl.add_argument("--body-file", dest="body_file", type=Path, default=None, metavar="PATH",
                     help="Required unless --plan.")
    ccl.add_argument("--request-id", dest="request_id", default=None, metavar="ID", help="Required unless --plan.")
    ccl.add_argument("--prev-handoff-id", dest="prev_handoff_id", type=int, default=None, metavar="H")
    ccl.add_argument("--token", type=int, default=None, help="Your coordinator lease token (required unless --plan).")
    cop = c_sub.add_parser(
        "open", help="The brief for a seam, built from the stores; with --ack, acknowledge it.",
        description="Prints the open built for you (the one your launch was started with, else one for the latest "
                    "rotation handoff) and, last, the command that acknowledges it. With no live coordinator lease "
                    "(no --token, or the operator's) it prints the brief read-only and says it cannot be "
                    "acknowledged. --ack OPEN_ID is the desk's fenced acknowledgement: the Since window moves "
                    "only then. A rotation or dream open needs --read-back-file, your restatement of the handoff. "
                    "A --token that is given and refused exits 1 after printing the read-only open.")
    cop.add_argument("--ack", type=int, default=None, metavar="OPEN_ID")
    cop.add_argument("--light", action="store_true",
                     help="The desk's first turn after the operator spoke: print a light open (what changed since "
                          "the last acknowledged open, plus the band) only when the operator's previous activity was "
                          "--gap-hours or more before this; otherwise say none is needed.")
    cop.add_argument("--gap-hours", dest="gap_hours", type=float, default=None, metavar="H",
                     help="With --light: the gap under which no light open is needed (default 4).")
    cop.add_argument("--verbose", action="store_true",
                     help="Also print what the open does not derive or build, and its read scope.")
    cop.add_argument("--read-back-file", dest="read_back_file", type=Path, default=None, metavar="PATH")
    cop.add_argument("--token", type=int, default=None, help="Your coordinator lease token.")
    crd = c_sub.add_parser(
        "ready", help="The ready queue: show it (a read), or add, move and correct an item (the desk's write).",
        description="show reads the queue with no coordinator live. add, move and correct are the desk's: each "
                    "needs --token (the live lease's, levain coord status shows it), --who and --why, writes one "
                    "journal record, and prints its id. A refusal exits 2 and writes nothing. needs-you is the "
                    "band's, never a move target.")
    rd_sub = crd.add_subparsers(dest="ready_cmd", required=True)
    ready_leaves: list[argparse.ArgumentParser] = []
    rds = rd_sub.add_parser("show", help="The queue: effectively-ready items (--all: every column).")
    rds.add_argument("--all", action="store_true", help="Every column, not only ready.")
    rds.add_argument("--repo", default=None, help="Only this repo.")
    ready_leaves.append(rds)
    door_ok(rds)
    for verb, what in (("add", "A new item (backlog unless --to says)."), ("move", "Move an item to --to."),
                       ("correct", "Correct a wrong state: a new record, never an edit of an old one.")):
        rdw = rd_sub.add_parser(verb, help=what)
        rdw.add_argument("item_id", metavar="ITEM")
        rdw.add_argument("--token", type=int, default=None, help="The live coordinator lease's token (required).")
        rdw.add_argument("--who", default=None, help="Who makes this change.")
        rdw.add_argument("--why", default=None, help="Why (one line; goes in the journal).")
        rdw.add_argument("--to", required=verb != "add", default="backlog" if verb == "add" else None,
                         help="The category (backlog, ready, running, waiting, done).")
        rdw.add_argument("--title", default=None, required=verb == "add",
                         help="The title" + ("." if verb == "add" else " (changes it; omit to keep)."))
        tier_g = rdw.add_mutually_exclusive_group()
        done_g = rdw.add_mutually_exclusive_group()
        tier_g.add_argument("--tier", default=None, help="sonnet, opus or haiku.")
        done_g.add_argument("--done-line", dest="done_line", default=None)
        if verb != "add":
            tier_g.add_argument("--no-tier", dest="no_tier", action="store_true",
                                help="Clear the tier (omit this and --tier to leave it unchanged).")
            done_g.add_argument("--no-done-line", dest="no_done_line", action="store_true",
                                help="Clear the done line (omit this and --done-line to leave it unchanged).")
        rdw.add_argument("--rank", type=int, default=None)
        rdw.add_argument("--depends-on", dest="depends_on", nargs="*", default=None, metavar="ITEM",
                         help="Item ids it waits on (none given: clears them).")
        if verb == "add":
            rdw.add_argument("--repo", required=True, help="The item's repo (fixed at add).")
        ready_leaves.append(rdw)
        door_ok(rdw)      # add, move and correct are the coordinator's write: refused without the live lease token
    cis = c_sub.add_parser(
        "import-status", help="Read a status file's lane rows into the ready queue (the coordinator's write).",
        description="Adds new rows, corrects changed ones and leaves the rest; a second run of an unchanged file "
                    "writes nothing. Needs --token (the live lease's). Prints one line per bucket: added, corrected, "
                    "unchanged, absent (in the queue, not in the file) and unparsed (row numbers: the row shape did "
                    "not fit, or the store refused the row). The store is <levain home>/board, the board store "
                    "`coord report` queues into, unless --queue-dir names one; never the consent directory by default.")
    cis.add_argument("path", type=Path, metavar="PATH")
    cis.add_argument("--repo", default="levain", help="The repo the rows belong to.")
    cis.add_argument("--token", type=int, required=True, help="The live coordinator lease's token.")
    common(cis)
    cis.set_defaults(func=run_coord)
    door_ok(cis)
    chn = c_sub.add_parser("handoff", help="Read the handoffs coordinators left.")
    chn_sub = chn.add_subparsers(dest="handoff_cmd", required=True)
    chs = chn_sub.add_parser("show", help="The latest handoff, or the one named.",
                             description="Prints the latest handoff, or the one named. A handoff names rows by "
                                         "id (dr-..., item keys), never by ordinal.")
    chs.add_argument("handoff_id", nargs="?", type=int, default=None, metavar="HANDOFF_ID")
    chs.add_argument("--md", action="store_true", help="Render as markdown.")
    chw = chn_sub.add_parser("worker", help="A done spawned worker's handoff: its final message (the lease holder's).",
                             description="Prints the final message a spawned worker left when its turn finished, kept "
                                         "when the watcher ended it, before its session was stopped; or why it left "
                                         "none. The read is fenced by your lease token and writes nothing.")
    chw.add_argument("launch_id", metavar="LAUNCH_ID")
    chw.add_argument("--token", type=int, required=True, help="Your coordinator lease token.")
    cbu = c_sub.add_parser(
        "budget", help="Each thread's budget against what its sessions used (Q13's counts): the gauge.",
        description="The budget is the thread's head envelope's; usage is Q13's count of the worker sessions pinned to "
                    "the thread's envelopes. Crossing it stops new steps; a SIGN extends it (levain envelope extend).")
    cbu.add_argument("thread", nargs="?", default=None, metavar="THREAD")
    crv = c_sub.add_parser(
        "review", help="The review queue: what waits for a look and never pushes (list, clear, reopen, feeds).",
        description="list and feeds first run the feed runner: every mechanical feed, and the delivery pass that files a "
                    "SIGN or ANSWER pointer for each open consent request or question of a coordinator thread. "
                    "Running either again files nothing new. Clearing is yours (manual), a fresh session you asked "
                    "(fresh), or the coordinator's own mode, which is refused for an authority-relevant item.")
    rv_sub = crv.add_subparsers(dest="review_cmd", required=True)
    rvl = rv_sub.add_parser("list", help="Open items and what waits on you.")
    rvl.add_argument("--all", action="store_true", help="Cleared items too.")
    rvl.add_argument("--feed", default=None, choices=review_feeds(), help="Only this feed.")
    rvc = rv_sub.add_parser("clear", help="Clear an open item.")
    rvo = rv_sub.add_parser("reopen", help="Open a cleared item again.")
    for p in (rvc, rvo):
        p.add_argument("item_id", metavar="ITEM")
        p.add_argument("--mode", default="manual", choices=review_modes(),
                       help="manual (you, the default), fresh (a fresh session you asked), coordinator (needs --token).")
        token(p)
    rvf = rv_sub.add_parser("feeds", help="Run the feeds and the delivery pass once and say what they reached.",
                            description="Takes a lock: while another feeds run holds it this one skips (exit 0). "
                                        "levain daemon install-feeds runs this on a schedule.")
    rvf.add_argument("--max-seconds", dest="max_seconds", type=int, default=None,
                     help="Whole seconds >= 1: the kernel ends the run (SIGALRM) if it is still going after this "
                          "long. 0 or omitted: no bound.")
    for p in (rvl, rvc, rvo, rvf):
        common(p)
        p.set_defaults(func=run_coord)
    for p in ready_leaves:
        common(p)
        p.set_defaults(func=run_coord)
    for p in (cs, ct, cr, cb, cc, cw, ca, cl, cwa, cq, can, ces, cmo, caw, csp, cbu, clg, crp, ccl, cop, chs, chw):
        common(p)
        p.set_defaults(func=run_coord)
    # the coordinator's own verbs (its brief names these); the operator's verbs are declared beside them, never left out
    door_ok(crt, cs, cr, cb, cc, cq, can, ces, cmo, caw, csp, cbu, clg, crp, ccl, cop, chs, chw)
    not_door(ct, cw, ca, cl, cwa)
    not_door(rvl, rvc, rvo, rvf)

    e = subparsers.add_parser(
        "envelope", help="Thread envelopes: what the desk may do for a thread, signed by you.",
        description="An envelope is the one authority the desk adds: where work may happen, which merges, "
                    "pushes and publishes are granted, a budget and a stop time. Sign the envelope, not the plan. "
                    "Before S1 nothing here is authority: every effect is still a per-call SIGN.")
    e_sub = e.add_subparsers(dest="envelope_cmd", required=True)

    def signer(p: argparse.ArgumentParser, *, key: bool = True) -> None:
        if key:
            p.add_argument("--signing-key", dest="signing_key", type=Path, default=None,
                           help="An enrolled key that signs (its private key file, or the .pub of a key an agent "
                                "holds).")
        p.add_argument("--data-root", dest="data_root", type=Path, default=None,
                       help="The cockpit data root whose authority log verifies the signature (default: the one the "
                            "consent hooks in the queue directory name).")

    ee = e_sub.add_parser("echo", help="Write the worded envelope for threads you are about to sign.")
    ee.add_argument("threads", nargs="+", metavar="THREAD")
    ee.add_argument("--where", action="append", default=[], required=True, metavar="REPO[=PATH,PATH]",
                    help="A repo the work may happen in, with the paths it may touch; the whole repo is REPO=. "
                         "written out. Repeatable.")
    ee.add_argument("--land", action="append", default=[], metavar="REPO=ONTO",
                    help="A merge the desk may land onto that branch. Repeatable.")
    ee.add_argument("--stop", default="2h", metavar="DURATION", help="When it stops: 2h, 90m, 1h30m (at most 24h).")
    ee.add_argument("--budget", default=None, metavar="K=N,...",
                    help="tool_calls=N,tokens=N,hours=N, each named (K=0 means none), and dollars_cents=N if you "
                         "want it (not metered; left out is 0). Without --budget every key is 0.")
    ee.add_argument("-o", "--output", type=Path, default=None, metavar="FILE",
                    help="Write the canonical body here (default: print it).")
    ee.add_argument("--force", action="store_true", help="Replace an existing -o file.")
    ee.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=qdir_help)
    ee.add_argument("--json", action="store_true", help="Machine-readable output.")

    ex = e_sub.add_parser(
        "extend", help="Write the envelope that raises a thread's budget (a SIGN: only your signature extends it).",
        description="The thread's head envelope, as narrowed, with the budget you name; it supersedes the head once "
                    "you sign it with `levain envelope sign`. Keys you leave out keep the head's budget; a value "
                    "you name may not be below the current one, and at least one must be greater.")
    ex.add_argument("thread", metavar="THREAD")
    ex.add_argument("--budget", required=True, metavar="K=N,...",
                    help="tool_calls=N,tokens=N,dollars_cents=N,hours=N: the new total for any you name.")
    ex.add_argument("-o", "--output", type=Path, default=None, metavar="FILE",
                    help="Write the canonical body here (default: print it).")
    ex.add_argument("--force", action="store_true", help="Replace an existing -o file.")
    ex.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=qdir_help)
    ex.add_argument("--json", action="store_true", help="Machine-readable output.")

    es = e_sub.add_parser("sign", help="Read the whole envelope, then sign and accept it.")
    es.add_argument("file", type=Path, metavar="FILE")
    signer(es)
    es.add_argument("--yes", action="store_true", help="Do not ask y/N; the whole view is still printed.")
    common(es)

    ea = e_sub.add_parser("accept", help="Accept an envelope signed elsewhere.")
    ea.add_argument("file", type=Path, metavar="FILE")
    ea.add_argument("sigfile", type=Path, metavar="SIGFILE")
    signer(ea, key=False)
    common(ea)

    esh = e_sub.add_parser("show", help="One envelope as it is now, whether it is live, and what it grants today.")
    esh.add_argument("envelope_id")
    esh.add_argument("--thread", default=None, metavar="THREAD")
    signer(esh, key=False)
    common(esh)

    el = e_sub.add_parser("list", help="The envelopes bound to a thread, oldest first.")
    el.add_argument("--thread", required=True, metavar="THREAD")
    signer(el, key=False)
    common(el)

    ek = e_sub.add_parser("kill", help="End an envelope (no signature; never undone).")
    ek.add_argument("envelope_id")
    ek.add_argument("--thread", default=None, metavar="THREAD")
    common(ek)

    en = e_sub.add_parser("narrow", help="Narrow an envelope: stop it earlier or drop one effect.")
    en.add_argument("envelope_id")
    g = en.add_mutually_exclusive_group(required=True)
    g.add_argument("--stop-earlier", dest="stop_earlier", metavar="WHEN",
                   help="Stop sooner: a duration from now (30m, 1h) or a time with its zone (2026-10-20T23:06:36Z).")
    g.add_argument("--drop-effect", dest="drop_effect", metavar="IDENTITY",
                   help="Drop one effect, by the identity `levain envelope show` prints beside it.")
    en.add_argument("--thread", default=None, metavar="THREAD")
    common(en)
    for p in (ee, ex, es, ea, esh, el, ek, en):
        p.set_defaults(func=run_envelope)
