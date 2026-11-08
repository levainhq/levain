"""``levain spores germinate``: an agenda spore that has come due is put in front of the operator (TS-2).

Deterministic, no model call. For each DUE agenda (open, disposition ``agenda``, no ``germinated`` stamp, ``next``
parses and is on or before the install's local day; an agenda with no ``next`` is never due) it does, in order:

1. adopt an open question whose ``q-…`` id the spore's text or notes name (file nothing; ref = that id);
2. ``band``: file ONE band row through ``QuestionStore.file_once``, no ping;
3. ``nudge``: the same filing, then one ping, and only when this run filed the row;
4. ``board:<repo>``: a coordinator drop (``outbox_id`` derived from the spore), or, with no coordinator store, the band
   filing with the repo named in ``blocks``;
5. stamp ``germinated={on, target, ref}`` and then compost the spore.

The dedupe key is the spore's immutable ``origin_key``, never its id: the question store is per USER while ``spore-NNN``
ids are per INSTALL, so an id key would let install B's due ``spore-004`` adopt install A's held row and compost with no
row of its own. Filing is idempotent (``file_once`` / the outbox id), so a crash between any two steps re-runs into the
same one row; a spore stamped but not yet composted is finished on the next run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

__all__ = ["KEY_PREFIX", "Outcome", "add_parsers", "germinate", "outbox_id_for", "run_germinate"]

KEY_PREFIX = "levain-germinate:"
TARGET_BAND, TARGET_NUDGE, TARGET_BOARD = "band", "nudge", "board"
_QID = re.compile(r"\bq-[0-9a-f]{24}\b")
_TITLE_MAX = 160


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
    """anneal's lenient read: a ``YYYY-MM-DD`` prefix, else None."""
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
    """``(target, repo)``: ``band`` (default and the fallback for an unknown value), ``nudge``, ``board:<repo>``."""
    raw = item.get("target")
    t = raw.strip() if isinstance(raw, str) else ""
    if t == TARGET_NUDGE:
        return TARGET_NUDGE, None
    if t.startswith(TARGET_BOARD + ":") and t[len(TARGET_BOARD) + 1:].strip():
        return t, t[len(TARGET_BOARD) + 1:].strip()
    return TARGET_BAND, None


def _line(text: str, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def _adopt(item: dict[str, Any], open_ids: set[str]) -> str | None:
    seen = [item.get("text") or "", *[n for n in (item.get("notes") or []) if isinstance(n, str)]]
    for chunk in seen:
        for qid in _QID.findall(chunk):
            if qid in open_ids:
                return qid
    return None


def germinate(spores_json: Path | str, queue_dir: Path | str, *, install: Path | str, agent: str = "claude_code",
              today: date | None = None, dry_run: bool = False,
              ping: Callable[..., bool] | None = None) -> dict[str, Any]:
    """Germinate every due agenda in ``spores_json``. Returns ``{"today", "due": [...], "finish": [...]}``; each entry
    is an :class:`Outcome` dict. ``dry_run`` reads and writes nothing (it creates no store, files nothing, pings nothing).
    ``ping`` is the notify seam (default :func:`levain.consent.band._send_ping`, read at call time)."""
    from levain.consent import band
    from levain.consent.questions import ANSWER_CHANNELS, QuestionStore
    from levain.coordinator.store import WORDS_MAX, CoordStore
    from levain.spores import guarded_spore_store

    spores_json, qdir, install = Path(spores_json), Path(queue_dir), Path(install)
    today = today or datetime.now().astimezone().date()
    result: dict[str, Any] = {"today": today.isoformat(), "due": [], "finish": []}
    if not spores_json.is_file():
        return result
    store = guarded_spore_store(spores_json)
    open_items = store.list_open(today=today)
    due = [s for s in open_items if is_due(s, today)]
    finish = [s for s in open_items if _is_agenda(s) and s.get("germinated")]
    questions = QuestionStore(qdir)
    open_ids = {q.id for q in questions.open_questions()}
    coord = CoordStore(qdir)
    has_board = coord.path.exists()
    ping = ping or band._send_ping

    for item in finish:
        stamp = item["germinated"] if isinstance(item["germinated"], dict) else {}
        out = Outcome(item["id"], item["origin_key"], str(stamp.get("target") or item.get("target") or ""),
                      "finished", str(stamp.get("ref") or ""), False, False, False)
        if not dry_run:
            store.descend(item["id"], kind="composted")
            out = Outcome(**{**out.__dict__, "done": True})
        result["finish"].append(out.as_dict())

    for item in due:
        sid, okey = item["id"], item["origin_key"]
        target, repo = _target_of(item)
        nxt = _day(item.get("next"))
        title = _line((item.get("text") or "").splitlines()[0] if (item.get("text") or "").strip() else sid, _TITLE_MAX)
        adopted = _adopt(item, open_ids)
        filed = pinged = False
        if adopted is not None:
            via, ref = "adopted", adopted
        elif target.startswith(TARGET_BOARD + ":") and has_board:
            via = "board"
            outbox = outbox_id_for(okey)
            drop = coord.drop_by_outbox(outbox)
            if drop is None and not dry_run:
                words = (item.get("text") or "").strip()[:WORDS_MAX]
                drop, filed = coord.accept_drop(words, surface="cli", outbox_id=outbox)
            ref = drop.drop_id if drop is not None else ""
        else:
            via = target if target in (TARGET_BAND, TARGET_NUDGE) else "band-fallback"
            if dry_run:
                ref = ""
            else:
                blocks = f"agenda {sid} in {install.name} came due {nxt.isoformat() if nxt else today.isoformat()}; it waits on you"
                if repo:
                    blocks += f" (board {repo}: no board here, so it is on the band)"
                q, filed = questions.file_once(
                    agent=agent, key=KEY_PREFIX + okey,
                    what=f"Due {(nxt or today).strftime('%m-%d')}: {title} ({sid})",
                    options=[("done", "Done")], rec="done", blocks=_line(blocks, 600), cwd=str(install.resolve()),
                    held_by=ANSWER_CHANNELS)
                ref = q.id
                if via == TARGET_NUDGE and filed:
                    pinged = bool(ping(agent, q, qdir, kind="attention"))
        done = False
        if not dry_run:
            store.update(sid, germinated={"on": today.isoformat(), "target": target, "ref": ref})
            store.descend(sid, kind="composted")
            done = True
        result["due"].append(Outcome(sid, okey, target, via, ref, filed, pinged, done).as_dict())
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
    except Exception as exc:  # noqa: BLE001 - a CLI verb says why and exits non-zero
        print(f"levain: spores germinate failed: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(res, indent=2, sort_keys=True), file=out)
        return 0
    verb = "would germinate" if args.dry_run else "germinated"
    for o in res["due"]:
        print(f"{verb} {o['spore_id']} ({o['target']}) via {o['via']}" + (f" -> {o['ref']}" if o["ref"] else ""), file=out)
    for o in res["finish"]:
        print(f"{'would finish' if args.dry_run else 'finished'} {o['spore_id']} (stamped, now composted)", file=out)
    if not res["due"] and not res["finish"]:
        print("no agenda is due", file=out)
    return 0


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("spores", help="Spore verbs for an install (germinate).", allow_abbrev=False)
    p.set_defaults(func=lambda a: (p.print_help(), 2)[1])
    sub = p.add_subparsers(dest="spores_cmd")
    g = sub.add_parser("germinate", allow_abbrev=False,
                       help="Put every due agenda in front of the operator (band row, nudge or board drop).",
                       description="Each open agenda spore whose date has come (and not yet germinated) is filed once "
                                   "as the spore's target says, stamped, and composted. Deterministic; no model call. "
                                   "Safe to run again: a re-run files nothing twice.")
    g.add_argument("--path", type=Path, default=Path.cwd(), help="Install directory (default: cwd).")
    g.add_argument("--queue-dir", type=Path, default=None, help="The band / coordinator directory (default: the "
                                                                "levain home's consent dir).")
    g.add_argument("--agent", choices=("claude_code", "codex"), default="claude_code",
                   help="The band's name for this install's harness.")
    g.add_argument("--dry-run", action="store_true", help="List the due set and write nothing.")
    g.add_argument("--json", action="store_true", dest="as_json", help="Print the result as JSON.")
    g.set_defaults(func=run_germinate)
