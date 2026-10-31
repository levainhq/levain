"""levain.dream.recover (D1-1b): finish the store side of a dream occurrence that did not end cleanly (D1_EXECUTOR §3.3).

Recovery reads ``progress.json`` and anneal's wrap history. **The commit oracle is anneal's completed wrap id and its
content hash, never the live file's sha**: a crash in anneal's save leaves the wrap committed and the live text
unchanged, and once the lock is released another writer can change the live file. The hash is the plain sha256 of the
saved text, stored in ``wraps.content_hash``; the executor wrote the same value to ``progress.json`` as
``composed_sha256`` before it saved.

The occurrence's own state (``launched`` with a lapsed lease, ``stopped``) belongs to the supervisor, so it is an input
here and the result says what the occurrence should END as; this module never writes an occurrence row. It cancels
the wrap by token, ends an orphaned compose child only after an identity check (the signalling rule), records the post
files a committed save was missing, and returns the card to file. It takes the store lock without waiting: a live
run holds it, and recovering a live run is how a wrap gets cancelled under it.
"""
from __future__ import annotations

import contextlib
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from levain.dream import snapshot as _sn

RECOVERY_RECORD = "recovery.json"
TERM_WAIT_S = 5.0
KILL_WAIT_S = 5.0

#: occurrence states recovery is asked about (the supervisor's words; ``failed`` and ``done`` are already settled).
STATES = ("launched", "stopped")

#: reasons, as the table in §3.3 names them.
KILLED_BEFORE_SAVE = "killed-before-save"
KILLED_IN_SAVE = "killed-in-save"
WRAP_CLEARED = "wrap-cleared"
UNKNOWN_COMMIT = "unknown-commit"
COMMITTED_NOT_RECORDED = "recovered: committed, not recorded"
COMMITTED_AFTER_STOP = "recovered: committed after stop"
DIED_BEFORE_CARD = "recovered: died between the effect and the card"


@dataclass
class RecoverResult:
    """``ending`` is what the occurrence should end as: ``failed``, ``done`` or ``stopped`` (the row stays stopped: A0's
    terminal rows are frozen), or ``none`` when recovery changed nothing (``reason`` says why). ``card`` is the spec to
    file (D1-1c's card.py makes the body): ``kind``, ``reason``, ``his`` (an integrity item for the operator, set only on
    ``unknown-commit``), ``dedupe_key``."""
    ending: str
    reason: str
    card: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    child: str = "none-recorded"
    wrap: str = "untouched"
    settled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"ending": self.ending, "reason": self.reason, "card": self.card, "notes": self.notes,
                "child": self.child, "wrap": self.wrap, "settled": self.settled}


# ----------------------------------------------------------------------------------------------- the orphaned child

def _group_gone(pgid: int) -> bool | None:
    """True when no process group ``pgid`` exists (``ProcessLookupError`` only); None when that cannot be told (a
    group holding only an unreaped zombie answers ``PermissionError`` on macOS: not confirmed)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return None
    return False


def _identity_matches(pid: int, started: Any, argv: Any, occ_dir: Path) -> bool | None:
    """Does the process at ``pid`` still look like the recorded compose child: same start time, same command line, and
    the command line carries this occurrence's dir. True = it matches; False = a live process is there that does not
    (the recorded one has ended); None = ``ps`` could not be read."""
    from levain.consent.delivery import process_identity

    ident = process_identity(pid)
    if ident is None:
        try:
            os.kill(pid, 0)          # a probe, never a signal: no process there at all is a definite "ended"
        except ProcessLookupError:
            return False
        except OSError:
            pass
        return None
    if not (isinstance(started, str) and started and " ".join(started.split()) == ident[0]):
        return False
    want = " ".join(str(a) for a in argv) if isinstance(argv, list) else ""
    if " ".join(want.split()) != " ".join(ident[1].split()):
        return False
    return str(occ_dir) in ident[1] or occ_dir.name in ident[1]


def end_orphan_child(prog: dict[str, Any], occ_dir: Path, *, term_wait: float = TERM_WAIT_S,
                     kill_wait: float = KILL_WAIT_S) -> tuple[str, list[str]]:
    """End the compose child ``progress.json`` names, if it outlived its fire. A signal goes to a process whose
    identity was checked just before it, never to a stored integer: SIGTERM to the group, then (identity re-checked)
    SIGKILL, and only ``ProcessLookupError`` on the group counts as ended. Returns ``(state, notes)``, ``state`` one of
    ``none-recorded``, ``ended`` (no process of ours is left), ``not-confirmed`` (no signal could safely be sent, or the
    group was not seen gone)."""
    pid = prog.get("child_pid")
    notes: list[str] = []
    if type(pid) is not int or pid < 1:
        return "none-recorded", notes
    started, argv = prog.get("child_started_at"), prog.get("child_argv")
    m = _identity_matches(pid, started, argv, occ_dir)
    if m is None:
        return "not-confirmed", [f"child pid {pid}: the process table could not be read; no signal was sent"]
    if m is False:
        gone = _group_gone(pid)
        if gone:
            return "ended", [f"child pid {pid}: no longer running"]
        return "not-confirmed", [f"child pid {pid}: the leader is gone or is another program now, but its process "
                                 "group is not seen gone; no signal was sent (the leader cannot be identified)"]
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + term_wait
    while time.monotonic() < deadline and _group_gone(pid) is not True:
        time.sleep(0.05)
    if _group_gone(pid) is not True:
        if _identity_matches(pid, started, argv, occ_dir) is True:  # re-checked: only the same process is killed
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
        deadline = time.monotonic() + kill_wait
        while time.monotonic() < deadline and _group_gone(pid) is not True:
            time.sleep(0.05)
    if _group_gone(pid) is True:
        notes.append(f"child pid {pid}: ended")
        return "ended", notes
    return "not-confirmed", [f"child pid {pid}: signalled but its group was not seen gone"]


# --------------------------------------------------------------------------------------------------------- recover

def _orphan_tmp(db: Path) -> list[str]:
    parent = db.parent
    try:
        return sorted(str(p) for p in parent.glob(f"{db.stem}.continuity*.tmp"))
    except OSError:
        return []


def _card(kind: str, reason: str, occ: str, *, his: list[str] | None = None) -> dict[str, Any]:
    return {"kind": kind, "reason": reason, "occurrence": occ, "his": his or [],
            "dedupe_key": f"dream:{occ}", "remedy": f"levain dream rollback {occ}"}


def _record(occ_dir: Path, res: RecoverResult) -> RecoverResult:
    if res.settled:
        with contextlib.suppress(OSError):
            _sn._write_json(occ_dir / RECOVERY_RECORD, res.as_dict())
    return res


def recover(store_path: Path | str, occ_dir: Path | str, *, state: str = "launched", card_filed: bool = False,
            lock: Callable[[Path], Any] | None = None, term_wait: float = TERM_WAIT_S,
            kill_wait: float = KILL_WAIT_S) -> RecoverResult:
    """Settle the store side of one dream occurrence (the §3.3 table).

    ``state`` is the supervisor's word for the occurrence: ``launched`` (its lease lapsed) or ``stopped`` (the
    supervisor confirmed a stop). ``card_filed`` says the occurrence's card already exists (the ``saved`` row then files
    nothing). A settled result is recorded in ``<occ>/recovery.json`` and a rerun returns it unchanged; an unsettled one
    (a live lock, an unreadable store, a committed save whose text has not landed) is recorded nowhere so a later run
    can finish it."""
    from levain import wrap as _w

    if state not in STATES:
        raise ValueError(f"state is one of {STATES}, not {state!r}")
    db, occ_dir = Path(store_path).resolve(), Path(occ_dir)
    prior = _sn._read_json(occ_dir / RECOVERY_RECORD)
    if prior and prior.get("ending") in ("failed", "done", "stopped"):
        return RecoverResult(prior["ending"], prior.get("reason", ""), prior.get("card"), list(prior.get("notes", [])),
                             prior.get("child", "none-recorded"), prior.get("wrap", "untouched"), True)
    pfile = occ_dir / "progress.json"
    if pfile.exists():
        prog = _sn._read_json(pfile)
        if prog is None:
            return RecoverResult("none", "progress-unreadable", notes=[f"{pfile} cannot be read: nothing was changed"],
                                 settled=False)
    else:
        prog = {}
    handle = (lock or (lambda d: _w._lock_wrap_file(d.parent / "wrap.lock")))(db)
    if handle is _w._ANOTHER_WRAP_RUNNING:
        return RecoverResult("none", "lock-held", notes=["a run holds the store's wrap lock: it may be live; nothing "
                                                         "was changed"], settled=False)
    try:
        return _recover_locked(db, occ_dir, prog, state, card_filed, term_wait, kill_wait)
    finally:
        _w._unlock_wrap(handle)


def _recover_locked(db: Path, occ_dir: Path, prog: dict[str, Any], state: str, card_filed: bool, term_wait: float,
                    kill_wait: float) -> RecoverResult:
    from levain import wrap as _w
    from anneal_memory import Store

    occ, phase, token = occ_dir.name, prog.get("phase"), prog.get("wrap_token")
    stopped = state == "stopped"
    try:
        store = Store(str(db), section_schema=None)
    except Exception as exc:  # noqa: BLE001 — an unreadable store settles nothing
        return RecoverResult("none", "store-unreadable", notes=[f"{type(exc).__name__}: {exc}"], settled=False)
    try:
        child, notes = end_orphan_child(prog, occ_dir, term_wait=term_wait, kill_wait=kill_wait)
        res = RecoverResult("none", "", notes=notes, child=child)
        if phase in (None, "composing"):
            res.wrap = _cancel(store, token, _w)
            if res.wrap == "failed":
                res.notes.append("our wrap could not be cancelled; the next consolidate will find it in progress")
                res.settled = False
                res.reason = KILLED_BEFORE_SAVE
                return res
            res.ending, res.reason = ("stopped", "stopped-before-save") if stopped else ("failed", KILLED_BEFORE_SAVE)
            return _record(occ_dir, res)
        if phase == "saving":
            return _recover_saving(store, db, occ_dir, prog, res, stopped, _w)
        if phase == "saved":
            return _recover_saved(db, occ_dir, prog, res, stopped, card_filed)
        res.reason = f"unknown-phase:{phase!r}"
        res.settled = False
        return res
    finally:
        with contextlib.suppress(Exception):
            store.close()


def _cancel(store: Any, token: Any, _w: Any) -> str:
    """Cancel our wrap by token: ``cancelled``, ``none_open`` or ``failed`` (anneal's compare-and-swap; another
    token's wrap is never cleared)."""
    if not token:
        return "none_open"
    return _w._cancel_outcome(store, token)


def _open_ours(store: Any, token: Any) -> bool | None:
    """True when the open wrap carries our token, False when no wrap of ours is open, None when that cannot be told."""
    try:
        started = store.get_wrap_started_at()
        bound = store.wrap_bound_token()
    except Exception:  # noqa: BLE001
        return None
    return bool(started) and bool(token) and bound == token


def _recover_saving(store: Any, db: Path, occ_dir: Path, prog: dict[str, Any], res: RecoverResult, stopped: bool,
                    _w: Any) -> RecoverResult:
    occ, token = occ_dir.name, prog.get("wrap_token")
    open_ours = _open_ours(store, token)
    if open_ours is None:
        res.reason, res.settled = "store-unreadable", False
        res.notes.append("the wrap state cannot be read; nothing was changed")
        return res
    if open_ours:
        res.wrap = _cancel(store, token, _w)
        if res.wrap == "failed":
            res.settled, res.reason = False, KILLED_IN_SAVE
            res.notes.append("our wrap could not be cancelled")
            return res
        res.ending, res.reason = ("stopped", "stopped-in-save") if stopped else ("failed", KILLED_IN_SAVE)
        return _record(occ_dir, res)
    before = prog.get("last_wrap_before")
    moved = _sn.wraps_after(db, before if isinstance(before, int) else 0)
    if moved is None:
        res.reason, res.settled = "store-unreadable", False
        res.notes.append("the wraps table cannot be read; nothing was changed")
        return res
    if not moved:
        res.ending, res.reason = ("stopped", "wrap-cleared") if stopped else ("failed", WRAP_CLEARED)
        return _record(occ_dir, res)
    composed = prog.get("composed_sha256")
    ours = [w for w, h in moved if composed and h == composed]
    if not ours or ours[-1] != moved[-1][0]:
        # a hash that is not ours, one that cannot be read, or ours with another wrap after it: nothing is adopted on
        # a guess, and the operator reads an integrity item
        res.ending, res.reason = "failed", UNKNOWN_COMMIT
        res.card = _card("failed", UNKNOWN_COMMIT, occ, his=[
            f"anneal completed wrap(s) {[w for w, _ in moved]} after this dream began, and none of them can be shown "
            "to be this dream's save (hash differs or is unreadable, or another wrap followed it). The text was not "
            "touched and no rollback is offered."])
        return _record(occ_dir, res)
    wrap_id = ours[-1]
    live = _sn.sha256_file(_sn.text_tier(db)["continuity"])
    reason = COMMITTED_AFTER_STOP if stopped else COMMITTED_NOT_RECORDED
    if live != composed:
        tmps = _orphan_tmp(db)
        res.ending, res.reason = ("stopped" if stopped else "done"), reason
        res.notes.append(f"anneal committed wrap #{wrap_id} but the live continuity text is not the composed text. "
                         + (f"Its unrenamed files: {tmps}; " if tmps else "")
                         + "finish anneal's rename by hand (mv the .md.tmp and .json.tmp into place), then rerun "
                         "recover. No post record was written, so a rollback is refused until then.")
        res.card = _card("consolidated", reason, occ)
        res.settled = False
        return res
    try:
        _sn.record_post_for(db, occ_dir, wrap_id, composed_sha256=composed)
    except _sn.SnapshotRefused as exc:
        m = _sn.read_manifest(occ_dir)
        if not (m and m.get("post_files")):  # already recorded is fine; anything else is a note, not a failure
            res.notes.append(f"post files not recorded: {exc}")
    res.ending, res.reason = ("stopped" if stopped else "done"), reason
    res.card = _card("consolidated", reason, occ)
    return _record(occ_dir, res)


def _recover_saved(db: Path, occ_dir: Path, prog: dict[str, Any], res: RecoverResult, stopped: bool,
                   card_filed: bool) -> RecoverResult:
    occ = occ_dir.name
    m = _sn.read_manifest(occ_dir)
    if m is not None and not m.get("post_files"):
        try:
            _sn.record_post_for(db, occ_dir, prog.get("wrap_id"), composed_sha256=prog.get("composed_sha256"))
        except _sn.SnapshotRefused as exc:
            res.notes.append(f"post files not recorded: {exc}")
    reason = COMMITTED_AFTER_STOP if stopped else DIED_BEFORE_CARD
    res.ending, res.reason = ("stopped" if stopped else "done"), reason
    res.card = None if card_filed else _card("consolidated", reason, occ)
    return _record(occ_dir, res)
