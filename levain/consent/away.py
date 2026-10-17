"""levain.consent.away — read and set an agent's away mode in its consent settings file.

Away mode is what lets a signed answer reach the agent: with it off (the default ``levain init`` writes), the hook
asks only the standing grants and, with none matching, withdraws the request at once so the agent's own dialog
asks; with it on, the hook holds each request up to ``away_hold_seconds`` while ``levain consent answer`` or the
phone signs one. Claude Code shows its own dialog during the hold (an allow from the hook dismisses it); Codex shows
none until the hold ends. The file is the adapter's own (``<queue dir>/claude_code.json`` or ``codex.json``, read by
:func:`levain.consent.adapters.claude_code.load_settings`); this module changes only ``away_mode`` and
``away_hold_seconds`` in it and keeps every other key, ``cockpit_data_root`` included. It never creates the file:
``levain init`` does, naming the install's cockpit. Away mode grants nothing: every answer still verifies as a
signature against the authority log.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from levain.authority import anchor_path
from levain.consent.adapters import claude_code, codex
from levain.consent.adapters.claude_code import (
    CLEANUP_RESERVE,
    DEFAULT_MAX_HOLD,
    _refuse_symlink,
)
from levain.consent.queue import ConsentQueue, authority_source

__all__ = ["AGENTS", "DEFAULT_HOLDS", "MAX_HOLD", "check_queue_dir", "read", "set_away", "settings",
           "why_unanswerable"]

# agent -> its settings file name in the queue directory
AGENTS = {"claude_code": claude_code.SETTINGS_NAME, "codex": codex.SETTINGS_NAME}
# the longest wait the hook makes: installed with DEFAULT_MAX_HOLD, it stops waiting CLEANUP_RESERVE before that
MAX_HOLD = DEFAULT_MAX_HOLD - CLEANUP_RESERVE
# Claude Code shows its own dialog at once while the hook holds (an allow from the hook dismisses it), so a long
# hold costs the person at the laptop nothing; Codex shows no dialog until the hook returns, so a hold is a stall
DEFAULT_HOLDS = {"claude_code": 300.0, "codex": 120.0}
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
    ConsentQueue(qdir, authority=authority_source(qdir, anchor=None))._check_directory()


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
    bounded read of the file (:func:`read`); defaults when there is none."""
    return claude_code.settings_from(read(qdir, agent))


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


def set_away(qdir: Path, agent: str, on: bool, hold: float | None = None) -> dict[str, Any]:
    """Turn ``agent``'s away mode on (holding up to ``hold`` seconds; default: the file's hold when it is above
    zero, else the agent's :data:`DEFAULT_HOLDS`) or off; ``on`` refuses when :func:`why_unanswerable` names a
    reason (a hold nobody can answer only delays the agent), by an atomic replace of its settings file; returns the new settings.
    Raises ``ValueError`` for a hold outside (0, :data:`MAX_HOLD`], a missing settings file, or a queue directory
    that breaks the queue's own rule (``QueueStoreError``)."""
    if hold is not None and (isinstance(hold, bool) or not isinstance(hold, (int, float)) or not math.isfinite(hold)
                             or not 0 < hold <= MAX_HOLD):
        raise ValueError(f"--hold must be more than 0 and at most {MAX_HOLD:g} seconds (the longest the hook waits)")
    check_queue_dir(qdir)
    got = _read_keyed(qdir, agent)
    if got is None:
        raise ValueError(f"no {AGENTS[agent]} in {qdir}: `levain init` writes it (with the consent hook on) and "
                         "names the install's cockpit; away mode is set in that file")
    data, key = got
    if on:
        if hold is None:
            hold = _valid_hold(data.get("away_hold_seconds")) or DEFAULT_HOLDS[agent]
        # the snapshot being written is the one checked (L3 r1: a re-read could validate another file's root)
        reason = why_unanswerable(claude_code.settings_from(data))
        if reason:
            raise ValueError(f"away mode not turned on: {reason}, so a held request could only wait out its hold")
        data["away_mode"] = True
        data["away_hold_seconds"] = float(hold)
    else:
        data["away_mode"] = False
        if hold is not None:
            data["away_hold_seconds"] = float(hold)
    path = _settings_path(qdir, agent)
    body = json.dumps(data, indent=2) + "\n"
    if len(body.encode("utf-8")) > _MAX_BYTES:
        raise ValueError(f"{path} would be larger than a consent settings file once written; not changed")
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
    return data
