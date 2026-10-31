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
WRAP_OPEN_UNATTRIBUTED = "wrap-open-unattributed"
STORE_MISMATCH = "store-mismatch"
#: the flag the executor passes the compose child to name the occurrence dir (its argv is recorded in progress.json)
ADD_DIR_FLAG = "--add-dir"
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
    this occurrence's dir is the value of the recorded ``--add-dir`` argument (an exact argv element, never a substring
    or a basename: ``o1`` must not match ``o10``). True = it matches; False = a live process is there that does not
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
    return _names_occurrence(argv, occ_dir)


def _names_occurrence(argv: Any, occ_dir: Path) -> bool:
    if not isinstance(argv, list):
        return False
    for flag, value in zip(argv, argv[1:]):
        if flag == ADD_DIR_FLAG and isinstance(value, str) and \
                (value == str(occ_dir) or os.path.realpath(value) == os.path.realpath(occ_dir)):
            return True
    return False


def _by_hand(pgid: int, occ: str) -> str:
    """The exact manual remedy for a compose group recovery will not signal by number."""
    return (f"Check process group {pgid} with `ps -g {pgid} -o pid,command`; if it still lists the compose program, "
            f"end it with `kill -KILL -{pgid}`; then rerun `levain dream recover {occ}`")


def end_orphan_child(prog: dict[str, Any], occ_dir: Path, *, term_wait: float = TERM_WAIT_S,
                     kill_wait: float = KILL_WAIT_S) -> tuple[str, list[str]]:
    """End the compose child ``progress.json`` names, if it outlived its fire. A signal goes to a process whose
    identity was checked just before it, never to a stored integer: SIGTERM to the group, then SIGKILL only while the
    leader is still verified to be the recorded process (a group whose leader has exited is never signalled by number),
    and only ``ProcessLookupError`` on the group counts as ended. Returns ``(state, notes)``, ``state`` one of
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
        from levain.consent.delivery import process_identity
        ident = process_identity(pid)
        if ident is not None and isinstance(started, str) and started and \
                " ".join(started.split()) != " ".join(str(ident[0]).split()):
            # a live process holds the number and started at another time than the recorded child: positive evidence
            # of a different process, and POSIX hands a pid out only when no process group still has that id, so the
            # recorded group has ended. A matching start time with a different command line proves nothing of the
            # kind (the recorded argv can be truncated or shaped differently by ps), so that case is not "ended"
            return "ended", [f"child pid {pid}: another process holds that number now, so the recorded group is gone"]
        if ident is not None:
            return "not-confirmed", [f"child pid {pid}: a live process holds that number with the recorded start time "
                                     f"but not the recorded command line; no signal was sent. "
                                     f"{_by_hand(pid, occ_dir.name)}"]
        if _group_gone(pid):
            return "ended", [f"child pid {pid}: no longer running"]
        return "not-confirmed", [f"child pid {pid}: the leader is gone but its process group is not seen gone; no "
                                 f"signal was sent. {_by_hand(pid, occ_dir.name)}"]
    try:
        leads = os.getpgid(pid) == pid
    except ProcessLookupError:
        leads = None                          # gone since the identity check: the group probe below says the rest
    except OSError:
        leads = False
    if leads is False:
        # the executor starts the child as its own group leader. A process that is not one is not what was recorded,
        # and a group signal would miss it (or hit another group): no signal, and never "ended"
        return "not-confirmed", [f"child pid {pid}: is not a process-group leader; no signal was sent"]
    if leads:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + term_wait
    while time.monotonic() < deadline and _group_gone(pid) is not True:
        time.sleep(0.05)
    if _group_gone(pid) is False and _identity_matches(pid, started, argv, occ_dir) is True:
        # the leader is still the recorded process, so its number is still its own and the group id cannot belong to
        # anyone else. Once the leader has gone nothing here reserves the number (recovery holds no handle to it), so
        # the group is never signalled by number again: it is reported not seen gone and recovery stays retryable
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
    if _group_gone(pid) is not True:
        deadline = time.monotonic() + kill_wait
        while time.monotonic() < deadline and _group_gone(pid) is not True:
            time.sleep(0.05)
    if _group_gone(pid) is True:
        notes.append(f"child pid {pid}: ended")
        return "ended", notes
    return "not-confirmed", [f"child pid {pid}: signalled but its group was not seen gone (a group whose leader has "
                             f"exited is not signalled by number). {_by_hand(pid, occ_dir.name)}"]


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
    """Write the settled result. It is the idempotency record, so a result that could not be written, or that leaves a
    compose child not seen ended, is NOT settled: a rerun repeats the cleanup instead of returning a stale record."""
    if not res.settled:
        return res
    if res.child == "not-confirmed":
        res.settled = False
        res.notes.append("the compose child was not confirmed ended, so recovery is not recorded; rerun it once the "
                         "process can be checked")
        return res
    try:
        _sn._write_json(occ_dir / RECOVERY_RECORD, res.as_dict())
    except OSError as exc:
        res.settled = False
        res.notes.append(f"the recovery record could not be written ({type(exc).__name__}: {exc}); rerun recover")
    return res


def _open_state(store: Any) -> str:
    """``idle``, ``open`` or ``unreadable`` for the store's wrap, asked of the store itself."""
    try:
        return "open" if store.get_wrap_started_at() else "idle"
    except Exception:  # noqa: BLE001
        return "unreadable"


def _unattributed(occ: str, res: RecoverResult, state: str) -> RecoverResult:
    """A wrap is open (or cannot be read) and no token proves it is this dream's: never settled, never cancelled."""
    res.reason, res.settled = WRAP_OPEN_UNATTRIBUTED, False
    res.wrap = "open-unattributed" if state == "open" else "unreadable"
    what = ("a wrap is open on the store" if state == "open" else "the store's wrap state cannot be read")
    res.notes.append(f"{what}, and this occurrence recorded no wrap token, so ownership cannot be proven; nothing "
                     "was cancelled and the occurrence stays unsettled")
    res.card = _card("failed", WRAP_OPEN_UNATTRIBUTED, occ, his=[
        f"{what.capitalize()}, and dream {occ} never recorded the token of the wrap it opened, so recovery cannot tell "
        "whether it is that dream's. Nothing was cancelled. Check what is running; if nothing is, cancel the wrap "
        "with anneal and run recover again."])
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
        res = _recover_locked(db, occ_dir, prog, state, card_filed, term_wait, kill_wait)
    finally:
        _w._unlock_wrap(handle)
    if res.child == "not-confirmed":
        pid = prog.get("child_pid")
        line = (f"The compose child (process group {pid}) was not confirmed ended and was not signalled by number. "
                f"{_by_hand(pid, occ_dir.name)}.") if type(pid) is int else \
            "The compose child was not confirmed ended; check what is running, then rerun `levain dream recover`."
        if res.card is None and card_filed:
            pass                               # this occurrence's card exists already: no second one
        elif res.card is None:
            res.card = _card("failed", "child-not-confirmed", occ_dir.name, his=[line])
        else:
            res.card = {**res.card, "his": [*res.card.get("his", []), line]}
    return res


def _recover_locked(db: Path, occ_dir: Path, prog: dict[str, Any], state: str, card_filed: bool, term_wait: float,
                    kill_wait: float) -> RecoverResult:
    from levain import wrap as _w
    from anneal_memory import Store

    occ, phase, token = occ_dir.name, prog.get("phase"), prog.get("wrap_token")
    stopped = state == "stopped"
    if (occ_dir / _sn.SNAP_DIR / _sn.MANIFEST).exists():
        manifest = _sn.read_manifest(occ_dir)
        why = ("the snapshot manifest cannot be read, so it cannot be shown to be of this store." if manifest is None
               else _sn.store_identity_problem(db, manifest))
        if why:
            return RecoverResult("none", STORE_MISMATCH, notes=[why + " Nothing was changed."], settled=False)
    try:
        store = Store(str(db), section_schema=None)
    except Exception as exc:  # noqa: BLE001 — an unreadable store settles nothing
        return RecoverResult("none", "store-unreadable", notes=[f"{type(exc).__name__}: {exc}"], settled=False)
    try:
        child, notes = end_orphan_child(prog, occ_dir, term_wait=term_wait, kill_wait=kill_wait)
        res = RecoverResult("none", "", notes=notes, child=child)
        if phase in (None, "composing"):
            if not token:
                state_ = _open_state(store)
                if state_ != "idle":
                    return _unattributed(occ, res, state_)
                res.wrap = "none_open"
            else:
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
    token's wrap is never cleared). Needs a token: without one ownership is unprovable and the caller does not ask."""
    if not token:
        return "failed"
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
    if not token:
        state_ = _open_state(store)
        if state_ != "idle":
            return _unattributed(occ, res, state_)
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
    if not token:
        # no recorded token means no way to tell this dream's commit from another writer's, whatever the text hash says
        res.reason, res.settled = UNKNOWN_COMMIT, False
        res.card = _card("failed", UNKNOWN_COMMIT, occ, his=[
            f"anneal completed wrap(s) {[w for w, *_ in moved]} after this dream began, and this occurrence never "
            "recorded its wrap token, so ownership cannot be proven. Nothing was adopted and no rollback is offered."])
        res.notes.append("no wrap token was recorded: a completed wrap is never adopted without one")
        return res
    composed = prog.get("composed_sha256")
    ours = [w for w, h, pid in moved if _is_ours(h, pid, composed, token)]
    if len(ours) > 1:
        res.reason, res.settled = UNKNOWN_COMMIT, False
        res.card = _card("failed", UNKNOWN_COMMIT, occ, his=[
            f"More than one completed wrap ({ours}) matches this dream's saved text and commit identity. Recovery "
            "will not pick one. The text was not touched and no rollback is offered."])
        res.notes.append("two wraps match this dream's commit; nothing is adopted and the occurrence stays unsettled")
        return res
    if not ours or ours[-1] != moved[-1][0]:
        # a hash that is not ours, one that cannot be read, or ours with another wrap after it: nothing is adopted on
        # a guess, and the operator reads an integrity item
        res.ending, res.reason = "failed", UNKNOWN_COMMIT
        res.card = _card("failed", UNKNOWN_COMMIT, occ, his=[
            f"anneal completed wrap(s) {[w for w, *_ in moved]} after this dream began, and none of them can be shown "
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
        if not (m and m.get("post_files")):  # already recorded is fine; anything else leaves recovery unsettled
            res.notes.append(f"post files not recorded: {exc}; recovery is not settled, so a rerun can finish")
            res.ending, res.reason = ("stopped" if stopped else "done"), (COMMITTED_AFTER_STOP if stopped
                                                                         else COMMITTED_NOT_RECORDED)
            res.card = _card("consolidated", res.reason, occ)
            res.settled = False
            return res
    except Exception as exc:  # noqa: BLE001 — a write that failed leaves the commit unrecorded: retry, do not trace
        res.notes.append(f"post files could not be written ({type(exc).__name__}: {exc}); rerun recover")
        res.reason, res.settled = (COMMITTED_AFTER_STOP if stopped else COMMITTED_NOT_RECORDED), False
        return res
    res.ending, res.reason = ("stopped" if stopped else "done"), reason
    res.card = _card("consolidated", reason, occ)
    return _record(occ_dir, res)


def _is_ours(content_hash: str | None, pair_id: str | None, composed: Any, token: Any) -> bool:
    """Is this completed wrap this dream's commit? Its text hash must be the composed hash, AND its commit identity must
    be ours: anneal's pair id starts with the first 12 of the wrap token. A hash alone is never identity (another
    writer can save the same text), so a row with no pair id (a legacy store) is not adopted either."""
    if not (composed and content_hash == composed and token and pair_id):
        return False
    return str(pair_id).startswith(f"{str(token)[:12]}-")


def _recover_saved(db: Path, occ_dir: Path, prog: dict[str, Any], res: RecoverResult, stopped: bool,
                   card_filed: bool) -> RecoverResult:
    occ = occ_dir.name
    m = _sn.read_manifest(occ_dir)
    if m is not None and not m.get("post_files"):
        if not prog.get("composed_sha256"):
            res.notes.append("post files not recorded: progress.json holds no composed text hash, so the live text "
                             "cannot be shown to be the dream's; recovery is not settled, so a rerun can finish once "
                             "the hash is restored")
            res.reason, res.settled = (COMMITTED_AFTER_STOP if stopped else DIED_BEFORE_CARD), False
            return res
        else:
            try:
                _sn.record_post_for(db, occ_dir, prog.get("wrap_id"), composed_sha256=prog.get("composed_sha256"))
            except _sn.SnapshotRefused as exc:
                # the same state the saving phase leaves when the live text is not the composed text: unsettled, so a
                # rerun can finish once the text is right, and no terminal record for a save that was not recorded
                res.notes.append(f"post files not recorded: {exc}; recovery is not settled, so a rerun can finish")
                again = _sn.read_manifest(occ_dir)
                if not (again and again.get("post_files")):
                    reason = COMMITTED_AFTER_STOP if stopped else DIED_BEFORE_CARD
                    res.ending, res.reason = ("stopped" if stopped else "done"), reason
                    res.card = None if card_filed else _card("consolidated", reason, occ)
                    res.settled = False
                    return res
            except Exception as exc:  # noqa: BLE001
                res.notes.append(f"post files could not be written ({type(exc).__name__}: {exc}); rerun recover")
                res.settled = False
                return res
    reason = COMMITTED_AFTER_STOP if stopped else DIED_BEFORE_CARD
    res.ending, res.reason = ("stopped" if stopped else "done"), reason
    res.card = None if card_filed else _card("consolidated", reason, occ)
    return _record(occ_dir, res)
