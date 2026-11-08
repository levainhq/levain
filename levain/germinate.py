"""``levain spores germinate``: an agenda spore that has come due is put in front of the operator (TS-2).

Deterministic, no model call. For each DUE agenda (open, disposition ``agenda``, no ``germinated`` stamp, ``next``
parses and is on or before the install's local day; an agenda with no ``next`` is never due) it does, in order:

1. adopt an open question whose ``q-…`` id the spore's text or notes name (file nothing; ref = that id);
2. ``band``: file ONE band row through ``QuestionStore.file_once``, no ping;
3. ``nudge``: the same filing, then one ping. The ping is made whenever the row is still open and the spore is not yet
   stamped, so a ping that failed (returned False or raised) leaves the spore open and due and the next run retries it
   (``ping_failed`` in the result, non-zero exit); only a ping that went out is followed by the stamp. A crash between
   the ping and the stamp therefore pings again on the re-run (at-least-once: a lost nudge is the failure this exists
   to prevent), still into the ONE row;
4. ``board:<repo>``: a coordinator drop (``outbox_id`` derived from the spore, words always checked against
   the spore as it is now), or, with no coordinator store that can take a drop (``coordinator_present``), the band
   filing with the reason in ``blocks``;
5. stamp ``germinated={on, target, ref}`` and then compost the spore, each as a compare-and-set against the spore as
   it was read (its version and ``agenda`` disposition), so a spore edited while this ran is left open, reported under
   ``skipped``, and filed again from its new words on the next run.

One bad spore never stops the others: its error is recorded under ``errors`` and the run goes on; the CLI prints every
outcome that did complete and exits non-zero when any spore errored. A missing ``.levain/memory.spores.json`` is
refused with a reason (exit 2), never reported as "nothing due". A run holds a per-install lock (``--dry-run`` does
not), so two overlapping runs (cron plus a hand run) take turns.

The dedupe key is the spore's immutable ``origin_key``, never its id: the question store is per USER while ``spore-NNN``
ids are per INSTALL, so an id key would let install B's due ``spore-004`` adopt install A's held row and compost with no
row of its own. Filing is idempotent (``file_once`` / the outbox id), so a crash between any two steps re-runs into the
same one row; a spore stamped but not yet composted is finished on the next run.

Known edges (closed under the reachability line, TS-2 L3): ``board:<repo>`` does not carry the repo into the drop: a
drop's ``Source`` takes only an absolute repo path, a ``refs/heads/`` ref and a full commit id, none of which a repo
NAME determines, and the words stay his verbatim; the repo is recorded in the spore's ``germinated`` stamp (target)
only. A drop has no field for the spore's ``created`` or ``next`` either (``sent_at`` is the sender's clock stamp, not an
authorship date, and a bare date is not a moment), so neither is carried. A board spore edited AFTER its drop was
filed raises ``DropConflict`` on every run (his new words are never silently dropped, and the old drop is never
re-labelled), until he resolves it by hand. A band row already filed keeps its first ``what`` text if the spore is
edited before the stamp. A nudge whose ping can never succeed (no channel) stays open and errors each run; the band row
is still there. Two due spores naming the same open ``q-…`` both adopt it. ``--dry-run`` reads the spore store through
anneal's ``list_open``, which backfills a missing ``origin_key`` on a legacy file. The default ``--agent`` is
``claude_code`` (a Codex install passes ``--agent codex``). An empty-text spore cannot exist (anneal refuses it). The
band row's ``Due MM-DD`` carries no year.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

__all__ = ["GerminateRefused", "KEY_PREFIX", "Outcome", "add_parsers", "germinate", "is_due", "outbox_id_for",
           "run_germinate"]

KEY_PREFIX = "levain-germinate:"
TARGET_BAND, TARGET_NUDGE, TARGET_BOARD = "band", "nudge", "board"
_QID = re.compile(r"\bq-[0-9a-f]{24}\b")
_TITLE_MAX = 160


class GerminateRefused(RuntimeError):
    """The run cannot start (no spores file): said with a reason, never reported as 'nothing due'."""


@dataclass(frozen=True)
class Outcome:
    spore_id: str
    origin_key: str
    target: str          # band | nudge | board:<repo> as the spore asked
    via: str             # what actually happened: adopted | band | nudge | board | band-fallback | finished
    ref: str             # the q-… id or the drop id
    filed: bool          # this run created the row or drop
    pinged: bool
    done: bool           # stamped and composted (False on a dry run)

    def as_dict(self) -> dict[str, Any]:
        return {"spore_id": self.spore_id, "origin_key": self.origin_key, "target": self.target, "via": self.via,
                "ref": self.ref, "filed": self.filed, "pinged": self.pinged, "done": self.done}


def outbox_id_for(origin_key: str) -> str:
    """The coordinator outbox id for a spore: the store's grammar is ``[A-Za-z0-9._-]`` (no colon, so the frame's
    ``germinate:<origin_key>`` cannot be one), so it is a hash of the origin key, still one id per origin key."""
    return "germinate-" + hashlib.sha256(origin_key.encode("utf-8")).hexdigest()[:40]


def _day(value: Any) -> date | None:
    """anneal's lenient read: a ``YYYY-MM-DD`` prefix, else None (display only; the DUE test is :func:`is_due`)."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _is_agenda(item: dict[str, Any]) -> bool:
    d = item.get("disposition")
    return isinstance(d, str) and d.strip().casefold() == "agenda"


def is_due(item: dict[str, Any], today: date) -> bool:
    """SD-4: agenda, open (the caller lists open spores), no ``germinated``, ``next`` parses and is not after today."""
    if not _is_agenda(item) or item.get("germinated"):
        return False
    nxt = _day(item.get("next"))
    return nxt is not None and nxt <= today


def _target_of(item: dict[str, Any]) -> tuple[str, str | None]:
    """``(target, repo)``: ``band`` (default and the fallback for an unknown value), ``nudge``, ``board:<repo>``. The keyword is matched without regard to case; the repo keeps its own."""
    raw = item.get("target")
    t = raw.strip() if isinstance(raw, str) else ""
    low = t.casefold()
    if low == TARGET_NUDGE:
        return TARGET_NUDGE, None
    if low.startswith(TARGET_BOARD + ":") and t[len(TARGET_BOARD) + 1:].strip():
        repo = t[len(TARGET_BOARD) + 1:].strip()
        return f"{TARGET_BOARD}:{repo}", repo
    return TARGET_BAND, None


def _line(text: str, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def _title(item: dict[str, Any], sid: str) -> str:
    """The first non-empty line of the spore's text, else its id."""
    lines = [ln for ln in (item.get("text") or "").splitlines() if ln.strip()]
    return _line(lines[0], _TITLE_MAX) if lines else sid


def _adopt(item: dict[str, Any], open_ids: set[str]) -> str | None:
    seen = [item.get("text") or "", *[n for n in (item.get("notes") or []) if isinstance(n, str)]]
    for chunk in seen:
        for qid in _QID.findall(chunk):
            if qid in open_ids:
                return qid
    return None


@contextmanager
def _run_lock(spores_json: Path, dry_run: bool) -> Iterator[None]:
    """One germinate run at a time per install (blocking); a dry run reads only and takes none. No ``fcntl`` (Windows):
    no lock, as anneal's own guarded writes then refuse a stale one."""
    if dry_run:
        yield
        return
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        yield
        return
    path = spores_json.with_name(spores_json.name + ".germinate.lock")
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def germinate(spores_json: Path | str, queue_dir: Path | str, *, install: Path | str, agent: str = "claude_code",
              today: date | None = None, dry_run: bool = False,
              ping: Callable[..., bool] | None = None) -> dict[str, Any]:
    """Germinate every due agenda in ``spores_json`` .
    Returns ``{"today", "due": [...], "finish": [...], "errors": [...], "skipped": [...]}``; ``due`` and ``finish``
    hold :class:`Outcome` dicts. ``dry_run`` writes nothing (it files nothing, pings nothing, and creates no store).
    ``ping`` is the notify seam (default
    :func:`levain.consent.band._send_ping`, read at call time). Raises :class:`GerminateRefused` with no spores file."""
    spores_json = Path(spores_json)
    if not spores_json.is_file():
        raise GerminateRefused(f"{spores_json} does not exist: this is not a Levain install with a spore store "
                               "(check --path); nothing was read or written")
    with _run_lock(spores_json, dry_run):
        return _germinate(spores_json, Path(queue_dir), Path(install), agent, today, dry_run, ping)


def _germinate(spores_json: Path, qdir: Path, install: Path, agent: str, today: date | None,
               dry_run: bool, ping: Callable[..., bool] | None) -> dict[str, Any]:
    from anneal_memory.spores import SporeError, spore_version

    from levain.consent import band
    from levain.consent.questions import ANSWER_CHANNELS, DB_NAME, QuestionStore
    from levain.coordinator.store import WORDS_MAX, CoordStore
    from levain.spores import guarded_spore_store

    today = today or datetime.now().astimezone().date()
    result: dict[str, Any] = {"today": today.isoformat(), "due": [], "finish": [], "errors": [], "skipped": []}
    store = guarded_spore_store(spores_json)
    open_items = store.list_open(today=today)
    due = [s for s in open_items if is_due(s, today)]
    finish = [s for s in open_items if _is_agenda(s) and s.get("germinated")]
    # a dry run on a machine with no band yet must not create one
    questions = None if dry_run and not (qdir / DB_NAME).exists() else QuestionStore(qdir)
    open_ids = {q.id for q in questions.open_questions()} if questions is not None else set()
    coord = CoordStore(qdir)
    ping = ping or band._send_ping
    has_board = coord.coordinator_present()
    stamp_for = {"on": today.isoformat()}

    def _err(sid: Any, okey: Any, exc: BaseException | str, **extra: Any) -> None:
        text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        result["errors"].append({"spore_id": sid, "origin_key": okey, "error": text, **extra})

    for item in finish:
        sid, okey = item.get("id"), item.get("origin_key")
        try:
            stamp = item["germinated"] if isinstance(item["germinated"], dict) else {}
            out = Outcome(str(sid), str(okey or ""), str(stamp.get("target") or item.get("target") or ""),
                          "finished", str(stamp.get("ref") or ""), False, False, False)
            if not dry_run:
                store.descend(sid, kind="composted", expect_disposition="agenda", today=today)
                out = Outcome(**{**out.__dict__, "done": True})
            result["finish"].append(out.as_dict())
        except Exception as exc:  # noqa: BLE001 - one bad spore never stops the others
            _err(sid, okey, exc)

    for item in due:
        sid, okey = item.get("id"), item.get("origin_key")
        try:
            if not isinstance(sid, str) or not isinstance(okey, str) or not okey:
                raise ValueError("the spore has no id or no origin_key; it cannot be keyed, so it is skipped")
            target, repo = _target_of(item)
            nxt = _day(item.get("next"))
            title = _title(item, sid)
            adopted = _adopt(item, open_ids)
            filed = pinged = False
            ping_failed = ""
            drop_target = target.startswith(TARGET_BOARD + ":")
            if adopted is not None:
                via, ref = "adopted", adopted
            elif drop_target and has_board:
                via = "board"
                outbox = outbox_id_for(okey)
                if dry_run:
                    ref = ""
                else:
                    # always sent with the spore's CURRENT words: a stored drop under this id holding other words raises
                    # DropConflict (his new words are never lost to an old drop's ref)
                    text = item.get("text") or ""
                    words = text.strip()[:WORDS_MAX]
                    drop, filed = coord.accept_drop(words, surface="cli", outbox_id=outbox)
                    ref = drop.drop_id
            else:
                via = target if target in (TARGET_BAND, TARGET_NUDGE) else "band-fallback"
                ref = ""
                if not dry_run:
                    blocks = (f"agenda {sid} in {install.name} came due "
                              f"{nxt.isoformat() if nxt else today.isoformat()}; it waits on you")
                    if repo:
                        blocks += f" (board {repo}: no board here, so it is on the band)"
                    q, filed = questions.file_once(
                        agent=agent, key=KEY_PREFIX + okey,
                        what=f"Due {(nxt or today).strftime('%m-%d')}: {title} ({sid})",
                        options=[("done", "Done")], rec="done", blocks=_line(blocks, 600), cwd=str(install.resolve()),
                        held_by=ANSWER_CHANNELS)
                    ref = q.id
                    if via == TARGET_NUDGE and q.answer is None:
                        try:
                            pinged = bool(ping(agent, q, qdir, kind="attention"))
                            if not pinged:
                                ping_failed = "the ping was not sent"
                        except Exception as exc:  # noqa: BLE001 - a ping is a ping; it must not be lost silently
                            ping_failed = f"the ping failed: {type(exc).__name__}: {exc}"
            if ping_failed:
                # the row is filed; the spore stays open and due so the next run pings again
                _err(sid, okey, f"ping_failed: {ping_failed}; the spore is left open and the next run retries",
                     ping_failed=True, ref=ref, target=target)
                continue
            done = False
            if not dry_run:
                try:
                    updated = store.update(sid, germinated={**stamp_for, "target": target, "ref": ref},
                                           expect_disposition="agenda", expected_version=spore_version(item),
                                           today=today)
                    store.descend(sid, kind="composted", expect_disposition="agenda",
                                  expected_version=spore_version(updated), today=today)
                    done = True
                except SporeError as exc:
                    if "changed since read" not in str(exc) and "disposition changed" not in str(exc):
                        raise
                    result["skipped"].append({"spore_id": sid, "origin_key": okey, "ref": ref,
                                              "reason": f"changed while germinating ({exc}); left open"})
                    continue
            result["due"].append(Outcome(sid, okey, target, via, ref, filed, pinged, done).as_dict())
        except Exception as exc:  # noqa: BLE001 - one bad spore never stops the others
            _err(sid, okey, exc)
    return result


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    from levain.consent.queue import default_queue_dir

    install = Path(args.path).expanduser().resolve()
    spores = install / ".levain" / "memory.spores.json"
    qdir = args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()
    return install, spores, qdir


def run_germinate(args: argparse.Namespace, out: Any = None) -> int:
    out = out if out is not None else sys.stdout
    try:
        install, spores, qdir = _paths(args)
        res = germinate(spores, qdir, install=install, agent=args.agent, dry_run=args.dry_run)
    except GerminateRefused as exc:
        print(f"levain: spores germinate refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - a CLI verb says why and exits non-zero
        print(f"levain: spores germinate failed: {exc}", file=sys.stderr)
        return 1
    rc = 1 if res["errors"] else 0
    for e in res["errors"]:
        print(f"levain: spores germinate: {e['spore_id']}: {e['error']}", file=sys.stderr)
    if args.as_json:
        print(json.dumps(res, indent=2, sort_keys=True), file=out)
        return rc
    verb = "would germinate" if args.dry_run else "germinated"
    for o in res["due"]:
        print(f"{verb} {o['spore_id']} ({o['target']}) via {o['via']}" + (f" -> {o['ref']}" if o["ref"] else ""), file=out)
    for o in res["finish"]:
        print(f"{'would finish' if args.dry_run else 'finished'} {o['spore_id']} (stamped, now composted)", file=out)
    for s in res["skipped"]:
        print(f"skipped {s['spore_id']}: {s['reason']}", file=out)
    if not res["due"] and not res["finish"] and not res["errors"] and not res["skipped"]:
        print("no agenda is due", file=out)
    return rc


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("spores", help="Spore verbs for an install (germinate).", allow_abbrev=False)
    p.set_defaults(func=lambda a: (p.print_help(), 2)[1])
    sub = p.add_subparsers(dest="spores_cmd")
    g = sub.add_parser("germinate", allow_abbrev=False,
                       help="Put every due agenda in front of the operator (band row, nudge or board drop).",
                       description="Each open agenda spore whose date has come (and not "
                                   "yet germinated) is filed once as the spore's target says, stamped, and composted. "
                                   "Deterministic; no model call. Safe to run again: a re-run files nothing twice.")
    g.add_argument("--path", type=Path, default=Path.cwd(), help="Install directory (default: cwd).")
    g.add_argument("--queue-dir", type=Path, default=None, help="The band / coordinator directory (default: the "
                                                                "levain home's consent dir).")
    g.add_argument("--agent", choices=("claude_code", "codex"), default="claude_code",
                   help="The band's name for this install's harness.")
    g.add_argument("--dry-run", action="store_true", help="List the due set and write nothing.")
    g.add_argument("--json", action="store_true", dest="as_json", help="Print the result as JSON.")
    g.set_defaults(func=run_germinate)
