"""levain.consent.away — read and set an agent's away mode in its consent settings file.

Away mode is what lets a signed answer reach the agent: with it off, the hook asks only the standing grants and,
with none matching, withdraws the request at once so the agent's own dialog asks; with it on, the hook holds each
request up to ``away_hold_seconds`` while ``levain consent answer`` or the phone signs one. Whether it is on is
:func:`levain.consent.auto_away.away_state`: the operator's explicit word (``away_word``, set here by
:func:`set_away`; ``auto``, the one ``levain init`` writes, is none), else input idle past ``away_idle_seconds`` on
macOS, else off. A file with no ``away_word`` was written before auto-away existed: its ``away_mode`` is the word.
Claude Code shows its own dialog during the hold (an allow from the hook
dismisses it); Codex shows none until the hold ends. The file is the adapter's own (``<queue dir>/claude_code.json``
or ``codex.json``, read by :func:`levain.consent.adapters.claude_code.load_settings`); this module changes only
``away_mode``, ``away_word``, ``away_hold_seconds`` and ``away_idle_seconds`` in it and keeps every other key,
``cockpit_data_root`` included. It never creates the file: ``levain init`` does, naming the install's cockpit. Away
mode grants nothing: every answer still verifies as a signature against the authority log.

``on`` also appends an away mark to the consent queue store (:meth:`levain.consent.queue.ConsentQueue.mark_away`),
and the hooks read away mode there too (:func:`levain.consent.adapters.claude_code.with_away_mark`): the file is
one any process of the operator's can write, so leaving a marked away mode (``off`` or ``auto``) takes a leave
signed by an enrolled key in :data:`~levain.consent.record.AWAY_NAMESPACE`, stored before the file is written (the
store is the truth; the word follows). Revoking the key that signed a leave puts away mode back on (the leave is
re-verified by each hook call). Any enrolled key may sign a leave, whatever its class (a design ruling: leaving only
lowers what the hook holds). Input-idle away has no mark: the operator's input still ends it. A store that cannot be
read is never read as no mark: ``off`` and ``auto`` refuse until it is repaired or moved aside.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Any

from levain.authority import anchor_path
from levain.consent.adapters import claude_code, codex
from levain.consent.adapters.claude_code import _refuse_symlink
from levain.consent.adapters.rules import MAX_HOLD
from levain.consent.auto_away import DEFAULT_HOLDS
from levain.consent.queue import (
    AnswerRefused,
    ConsentQueue,
    QueueStoreError,
    authority_source,
)
from levain.consent.record import AWAY_NAMESPACE, away_leave_message

__all__ = ["AGENTS", "DEFAULT_HOLDS", "MAX_HOLD", "check_queue_dir", "read", "set_away", "settings",
           "why_unanswerable"]

# agent -> its settings file name in the queue directory
AGENTS = {"claude_code": claude_code.SETTINGS_NAME, "codex": codex.SETTINGS_NAME}
# a settings file is a few keys; anything larger is not one levain wrote
_MAX_BYTES = 64 * 1024


def _settings_path(qdir: Path, agent: str) -> Path:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent {agent!r} (one of: {', '.join(AGENTS)})")
    return qdir / AGENTS[agent]


def check_queue_dir(qdir: Path) -> None:
    """``qdir`` as the queue requires it (a real directory, not a symlink, owned by this uid, 0700), never creating
    it. Raises ``ValueError`` (missing) or ``QueueStoreError``."""
    if not os.path.lexists(qdir):
        raise ValueError(f"no consent queue directory at {qdir}; run `levain init` with the consent hook on first")
    ConsentQueue(qdir, home=Path.home(), authority=authority_source(qdir, anchor=None))._check_directory()


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _read_keyed(qdir: Path, agent: str) -> tuple[dict[str, Any], tuple[int, int, int, int]] | None:
    path = _settings_path(qdir, agent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(f"{path} cannot be read as a settings file ({e}; a symlink is refused)") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{path} is not a regular file")
        raw = os.read(fd, _MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_BYTES:
        raise ValueError(f"{path} is larger than a consent settings file")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise ValueError(f"{path} does not parse ({type(e).__name__}); fix or remove it, then run `levain init`") \
            from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data, _stat_key(st)


def read(qdir: Path, agent: str) -> dict[str, Any] | None:
    """The agent's settings as a JSON object, ``None`` when there is no file. Raises ``ValueError`` for a symlink, a
    non-regular or oversized file (read bounded, never followed), or one that does not parse to an object."""
    got = _read_keyed(qdir, agent)
    return None if got is None else got[0]


def _valid_hold(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        f = float(value)
    except (OverflowError, ValueError):
        return None
    return f if math.isfinite(f) and 0 < f <= MAX_HOLD else None


def settings(qdir: Path, agent: str) -> Any:
    """``agent``'s settings as its hook reads them (the adapters' own :func:`claude_code.settings_from`), from a
    bounded read of the file (:func:`read`), with its away mark from the queue store
    (:func:`claude_code.with_away_mark`); defaults when there is no file."""
    return claude_code.with_away_mark(claude_code.settings_from(read(qdir, agent)), qdir, agent)


def why_unanswerable(adapter_settings: Any) -> str | None:
    """Why no signed answer can reach a held request under ``adapter_settings``, or ``None``: a held request needs
    a signature by an enrolled key in the cockpit the settings name (a grant answers before any hold)."""
    root = adapter_settings.cockpit_data_root
    if not root:
        return "the settings name no cockpit_data_root (levain init writes it)"
    db = Path(root) / "cockpit.db"
    if not db.is_file():
        return f"there is no cockpit database at {db}"
    try:
        snap = authority_source(db, anchor=anchor_path(root))()
    except Exception as e:  # noqa: BLE001 - an authority that does not replay verifies nothing
        return f"the cockpit's authority log does not replay ({e})"
    if not snap.active_keys():
        return "the cockpit has no enrolled key to sign an answer (`levain cockpit enroll`)"
    return None


def _unreadable(qdir: Path) -> str:
    return (f"{claude_code.store_unreadable(qdir)}. Away mode not changed: repair the store, or move it aside (a "
            "store moved aside reads as no away mark, which ends away mode too)")


def _check_signing_key(signing_key: Path) -> None:
    """``signing_key`` as ``ssh_signer`` will read it: the file readable, and its public half (``<path>.pub``, or
    the path itself when it is the ``.pub``). Raises ``ValueError``."""
    from levain.cockpit.keys import KeyError_, signing_public_key
    path = Path(signing_key).expanduser()
    try:
        with path.open("rb") as fh:
            fh.read(1)
        signing_public_key(path)
    except (OSError, KeyError_) as e:
        raise ValueError(f"--signing-key {path} cannot be used: {getattr(e, 'strerror', None) or e}") from e


def _leave(qdir: Path, agent: str, adapter_settings: Any, signing_key: Path | None) -> bool:
    """Leave ``agent``'s away mark when it has one standing (:func:`claude_code.away_mark_of`): a leave signed with
    ``signing_key`` in :data:`AWAY_NAMESPACE`, stored by the queue once it verifies. A mark whose stored leave no
    longer verifies (its key revoked since) is marked anew and that mark left. Returns whether the store changed.
    Raises ``ValueError`` when the store cannot be read, a mark stands and there is no key, or the leave is
    refused or cannot be signed or checked (saying so when a new mark was recorded first)."""
    try:
        mark = claude_code.away_mark_of(qdir, adapter_settings, agent)
    except (QueueStoreError, sqlite3.Error, OSError) as e:
        raise ValueError(_unreadable(qdir)) from e
    if mark is None or mark.left:
        return False
    if signing_key is None:
        raise ValueError("away mode was set by `levain consent away on`, and leaving it takes a signature: run it "
                         "again with --signing-key <an enrolled key>")
    from levain.authority import AuthorityError
    from levain.cockpit.consent import ConsentError
    from levain.cockpit.keys import KeyError_, ssh_signer

    queue = claude_code._queue_for(qdir, adapter_settings, home=Path.home())
    marked = False
    try:
        signer_fp, sign = ssh_signer(Path(signing_key), namespace=AWAY_NAMESPACE)
        if mark.leave_by is not None:
            queue.mark_away(agent)   # the stored leave no longer verifies and cannot be replaced: leave anew
            marked = True
        payload = queue.away_leave_payload(agent, signer_fp)
        queue.leave_away(payload, sign(away_leave_message(payload)))
    except (AnswerRefused, ConsentError, KeyError_, AuthorityError, OSError, QueueStoreError, sqlite3.Error) as e:
        # AuthorityError covers VerifierUnavailable: ssh-keygen missing, untrusted or out of time
        if not marked and isinstance(e, (QueueStoreError, sqlite3.Error)):
            raise ValueError(_unreadable(qdir)) from e
        again = "; a new away mark was recorded first, so away mode stands" if marked else ""
        raise ValueError(f"away mode not left: {e}{again}") from e
    return True


def set_away(qdir: Path, agent: str, on: bool | None, hold: float | None = None,
             idle_after: float | None = None, *, signing_key: Path | None = None) -> dict[str, Any]:
    """Set ``agent``'s explicit word: away mode on (holding up to ``hold`` seconds; default: the file's hold when
    it is above zero, else the agent's :data:`DEFAULT_HOLDS`), off, or, for ``on=None``, ``auto``: no word, so
    input idle past ``idle_after`` seconds (default: the file's, else
    :data:`levain.consent.auto_away.IDLE_THRESHOLD`) decides, holding ``hold`` when given, else the file's hold
    when above zero, else the default (:func:`levain.consent.auto_away.hold_seconds`). ``on`` refuses when
    :func:`why_unanswerable` names a reason (a hold nobody can answer
    only delays the agent); no word never holds without an enrolled key (the hook checks). ``on`` appends an away
    mark to the queue store first; ``off`` and ``auto`` leave a standing mark first, by a leave signed with
    ``signing_key`` (:func:`_leave`; refused without one, and a ``signing_key`` that cannot be read is refused
    before the store is touched). Then by an atomic replace of its settings file, whose new body is checked before
    the store changes; returns the new settings. When the store changed and the file write then fails, the
    ``ValueError`` says what stands: a recorded mark holds whatever the file says, and after a recorded leave the
    hook acts on the file's old word (an ``on`` there keeps holding until this is run again).
    Raises ``ValueError`` for a hold outside (0, :data:`MAX_HOLD`], an ``idle_after`` that is not a finite number
    above 0, a missing settings file, a store that cannot be read, or a queue directory that breaks the queue's own
    rule (``QueueStoreError``)."""
    if hold is not None and _valid_hold(hold) is None:
        raise ValueError(f"--hold must be more than 0 and at most {MAX_HOLD:g} seconds (the longest the hook waits)")
    if idle_after is not None:
        try:
            ok = not isinstance(idle_after, bool) and isinstance(idle_after, (int, float)) \
                and math.isfinite(float(idle_after)) and idle_after > 0
        except OverflowError:   # an integer too large for a float
            ok = False
        if not ok:
            raise ValueError("--idle-after must be a finite number of seconds above 0")
    check_queue_dir(qdir)
    if not on and signing_key is not None:
        _check_signing_key(signing_key)
    got = _read_keyed(qdir, agent)
    if got is None:
        raise ValueError(f"no {AGENTS[agent]} in {qdir}: `levain init` writes it (with the consent hook on) and "
                         "names the install's cockpit; away mode is set in that file")
    data, key = got
    if on and hold is None:
        hold = _valid_hold(data.get("away_hold_seconds")) or DEFAULT_HOLDS[agent]
    if on:
        # the snapshot being written is the one checked (L3 r1: a re-read could validate another file's root)
        reason = why_unanswerable(claude_code.settings_from(data))
        if reason:
            raise ValueError(f"away mode not turned on: {reason}, so a held request could only wait out its hold")
    adapter_settings = claude_code.settings_from(data)
    data["away_mode"] = on is True
    data["away_word"] = "auto" if on is None else "on" if on else "off"
    if hold is not None:
        data["away_hold_seconds"] = float(hold)
    if idle_after is not None:
        data["away_idle_seconds"] = float(idle_after)
    was = "on" if adapter_settings.away_mode else "off" if adapter_settings.explicit_off else "auto"
    path = _settings_path(qdir, agent)
    body = json.dumps(data, indent=2) + "\n"
    if len(body.encode("utf-8")) > _MAX_BYTES:
        raise ValueError(f"{path} would be larger than a consent settings file once written; not changed")
    try:
        if on:
            claude_code._queue_for(qdir, adapter_settings, home=Path.home()).mark_away(agent)
            changed = "an away mark was recorded"
        else:
            changed = "the away mark was left" if _leave(qdir, agent, adapter_settings, signing_key) else ""
    except (QueueStoreError, sqlite3.Error) as e:
        raise ValueError(_unreadable(qdir)) from e
    try:
        _write_settings(qdir, path, body, key)
    except (OSError, ValueError) as e:
        if not changed:
            raise
        # what the file says now is not asserted: another process may have changed or removed it meanwhile
        stands = ("the hook holds on that mark whatever the file says; run `levain consent away on` again to bring "
                  "the file in line" if on else
                  f"the hook acts on whatever the file says now (it said `{was}` when read); "
                  "run `levain consent away off` or `auto` again")
        raise ValueError(f"{changed} in the consent queue store, but {path} was not written ({e}): {stands}") from e
    return data


def _write_settings(qdir: Path, path: Path, body: str, key: tuple[int, int, int, int]) -> None:
    """Replace ``path`` with ``body`` atomically, unless it is no longer the file read as ``key``."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=qdir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fchmod(fh.fileno(), 0o600)
            os.fsync(fh.fileno())
        _refuse_symlink(path)
        try:
            now = _stat_key(os.lstat(path))
        except FileNotFoundError:
            now = None
        if now != key:
            raise ValueError(f"{path} changed while away mode was being set; nothing written, run it again")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
