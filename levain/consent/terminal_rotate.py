"""``levain band terminal-key rotate`` (board levain#191, slice 3): the one-time re-enrolment of the terminal key onto
Levain's Secure Enclave helper.

The terminal key made by the old way (the Apple keychain provider, a CTK identity) is replaced by a helper key: the NEW
key is made under the label ``levain-terminal-next``, enrolled in the authority SIGNED BY THE OLD KEY (one Touch ID on
the old path), the OLD key is revoked SIGNED BY THE NEW KEY through the helper (one Touch ID, which proves the new key
signs and the authority accepts it), the old key's three files move to ``<keydir>/retired-<UTC stamp>/``, the new key's
files take the terminal label, and the old CTK identity is deleted (no touch).

It is RESUMABLE. ``<keydir>/rotate.json`` (0600) records each completed step, and every step also checks the AUTHORITY
(or the disk), never the journal alone, so a re-run after any interruption finishes the rest and never enrols or
revokes twice. It is the operator's act at the laptop: refused inside any agent session, exactly as ``create`` is, and
under the same real-key gate (``keys.refuse_real_key_dir_unless_live``). It never raises: a refusal, a stop and a
Ctrl-C are a report.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.cockpit import keys as _keys
from levain.cockpit.custody import (LEVAIN_SIGN, SC_AUTH, SIDECAR_SUFFIX, TOUCH_ID, _delete_identity,
                                    declared_custody, read_sidecar)
from levain.cockpit.keys import KeyError_
from levain.consent import terminal_sign as ts

NEXT_LABEL = "levain-terminal-next"
JOURNAL = "rotate.json"
JOURNAL_FORMAT = "levain-rotate/1"
KEY_CLASS = "laptop-presence"
_SUFFIXES = ("", ".pub", SIDECAR_SUFFIX)
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
EXIT_OK, EXIT_STOPPED, EXIT_REFUSED = 0, 1, 2

NOOP_TEXT = "the terminal key already signs through Levain's helper; nothing to rotate"

SignerFactory = Callable[[Path], tuple[str, Callable[[bytes], str]]]


@dataclass
class RotateReport:
    state: str            # "done" | "noop" | "refused" | "stopped"
    text: str
    code: int = 0
    lines: list[str] = field(default_factory=list)


class _Stop(Exception):
    """A step that cannot go on; ``refused`` is True when nothing was changed."""

    def __init__(self, why: str, *, refused: bool = False) -> None:
        super().__init__(why)
        self.refused = refused


def _plain(exc: BaseException) -> str:
    return ts._one_line(exc)


# ---------------------------------------------------------------------------------------------------
# the journal


def _journal_path(d: Path) -> Path:
    return d / JOURNAL


def _read_journal(d: Path) -> dict[str, Any] | None:
    path = _journal_path(d)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Stop(f"cannot read {path}: {exc.strerror or exc}", refused=True) from None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise _Stop(f"{path} is not a regular file of this user (a link is refused)", refused=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    need = ("old_fp", "old_public_key", "stamp", "steps")
    if (not isinstance(data, dict) or data.get("format") != JOURNAL_FORMAT
            or not all(k in data for k in need) or not isinstance(data["steps"], dict)):
        raise _Stop(f"{path} is not a {JOURNAL_FORMAT} journal; look at it and remove it by hand, then run rotate "
                    "again", refused=True)
    return data


def _write_journal(d: Path, journal: dict[str, Any]) -> None:
    """Atomically: a 0600 temporary file in the key directory, fsynced, then renamed over the journal."""
    final = _journal_path(d)
    tmp = d / (JOURNAL + ".tmp")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview((json.dumps(journal, sort_keys=True, indent=1) + "\n").encode("utf-8"))
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, final)


# ---------------------------------------------------------------------------------------------------
# small helpers


def _rename(src: Path, dst: Path) -> None:
    """One rename within the key directory (or into its retired directory), refused when the target is there."""
    if os.path.lexists(dst):
        raise _Stop(f"{dst} already exists; nothing was overwritten")
    os.rename(src, dst)
    if os.path.lexists(src) or not os.path.lexists(dst):
        raise _Stop(f"the rename of {src.name} to {dst} did not take")


def resolve_data_root(path: Path | None, data_root: Path | None) -> Path:
    """The authority's data root as ``levain cockpit enroll`` finds it: ``--data-root``, else the ledger root's
    ``cockpit/`` of the install ``--path`` names."""
    if data_root is not None:
        return Path(data_root)
    from levain.dashboard import _resolve_source
    try:
        scope = _resolve_source(path if path is not None else Path.cwd()).write_scope
    except SystemExit as exc:
        raise _Stop(f"the install at {path} cannot be found ({exc}); pass --data-root", refused=True) from None
    if scope is None:
        raise _Stop(f"{path} has no writable install; pass --data-root", refused=True)
    return scope.ledger_root / "cockpit"


def _row(data_root: Path, fp: str, clock: Callable[[], datetime]) -> Any:
    rows, note = _keys.keys(data_root, now=clock())
    return next((r for r in rows if r.fingerprint == fp), None), note


def _stamp(clock: Callable[[], datetime]) -> str:
    return clock().astimezone(timezone.utc).strftime(_STAMP_FORMAT)


def _files_exist(d: Path, label: str) -> list[bool]:
    return [os.path.lexists(d / (label + s)) for s in _SUFFIXES]


# ---------------------------------------------------------------------------------------------------
# the verb


def rotate(key_dir: Path | None = None, *, data_root: Path | None = None, path: Path | None = None,
           creator: Callable[..., Any] | None = None, signer_factory: SignerFactory | None = None,
           in_agent_session: Callable[[], bool] | None = None, sc_auth: Path | str = SC_AUTH,
           delete_identity: Callable[[str, Path | str], str | None] | None = None,
           clock: Callable[[], datetime] | None = None,
           declare: Callable[..., str] | None = None) -> RotateReport:
    """Rotate the terminal key onto the levain-sign helper (module docstring). ``creator`` makes the new key
    (``creator(key_dir, label)``; default ``custody.create_levain_sign``, presence on); ``signer_factory(key_path)``
    returns ``(fingerprint, sign)`` for a key (default ``keys.ssh_signer``); the rest are doors for tests. Never
    raises."""
    log: list[str] = []
    try:
        return _rotate(key_dir, data_root, path, creator, signer_factory, in_agent_session, sc_auth, delete_identity,
                       clock or (lambda: datetime.now(timezone.utc)), declare or declared_custody, log)
    except _Stop as stop:
        return _stopped(log, str(stop), stop.refused)
    except KeyboardInterrupt:
        return _stopped(log, "cancelled", False)
    except Exception as exc:  # noqa: BLE001 - the CLI prints a report, never a traceback
        return _stopped(log, _plain(exc), False)


def _stopped(log: list[str], why: str, refused: bool) -> RotateReport:
    if refused:
        return RotateReport("refused", f"refused: {why} (nothing was changed)", EXIT_REFUSED, log)
    lines = [*log, f"stopped: {why}",
             "Nothing is lost: the steps above are done and recorded. Run `levain band terminal-key rotate` again "
             "to finish; it continues from the last step and never enrols or revokes twice."]
    return RotateReport("stopped", "\n".join(lines), EXIT_STOPPED, log)


def _rotate(key_dir: Path | None, data_root: Path | None, path: Path | None, creator: Callable[..., Any] | None,
            signer_factory: SignerFactory | None, in_agent_session: Callable[[], bool] | None,
            sc_auth: Path | str, delete_identity: Callable[[str, Path | str], str | None] | None,
            clock: Callable[[], datetime], declare: Callable[..., str], log: list[str]) -> RotateReport:
    if in_agent_session is None:
        from levain.consent.band import agent_session_caller as in_agent_session
    try:
        inside = in_agent_session()
    except Exception as exc:  # noqa: BLE001 - a session that cannot be told apart is refused (fail-closed)
        raise _Stop("whether this runs inside an agent session cannot be told: " + _plain(exc), refused=True) from None
    if inside:
        raise _Stop(ts.session_refusal(), refused=True)
    try:
        d = Path(key_dir) if key_dir is not None else ts._real_key_dir()
        _keys.refuse_real_key_in_tests(d, "the key directory")
        d = _keys.refuse_real_key_dir_unless_live(d, "the key directory")
        st = os.lstat(d) if os.path.lexists(d) else None
        if st is not None and (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077):
            raise KeyError_(f"{d} is not a private directory of this user (a link is refused)")
        journal = _read_journal(d) if st is not None else None
    except _Stop:
        raise
    except Exception as exc:  # noqa: BLE001 - a directory that cannot be read is a refusal
        raise _Stop(f"the key directory cannot be read: {_plain(exc)}", refused=True) from None
    root = resolve_data_root(path, data_root)
    sign_for: SignerFactory = signer_factory or (lambda p: _keys.ssh_signer(p))
    make: Callable[..., Any] = creator or _default_creator

    if journal is not None and not journal.get("complete"):
        log.append(f"resuming the rotation recorded in {_journal_path(d)}")
    else:
        journal = _begin(d, root, clock, log)
        if journal is None:
            return RotateReport("noop", NOOP_TEXT, EXIT_OK, [NOOP_TEXT])

    _step_made(d, journal, make, log)
    _step_enrol(d, root, journal, sign_for, clock, declare, sc_auth, log)
    _step_revoke(d, root, journal, sign_for, clock, log)
    _step_move(d, journal, log)
    _step_ctk(d, journal, delete_identity or _delete_identity, sc_auth, log)
    journal["complete"] = True
    _write_journal(d, journal)
    return _done(d, journal, log)


def _default_creator(key_dir: Path, label: str) -> Any:
    from levain.cockpit.custody import create_levain_sign
    return create_levain_sign(key_dir, label)


# ---------------------------------------------------------------------------------------------------
# a fresh start


def _begin(d: Path, root: Path, clock: Callable[[], datetime], log: list[str]) -> dict[str, Any] | None:
    """The journal of a fresh rotation, or None when the key already signs through the helper. Refuses (nothing made)
    when there is no terminal key, or the authority does not hold it enrolled and active."""
    key = ts.find_key(d)
    if key is None:
        raise _Stop("there is no terminal key to rotate; make one with `levain band terminal-key create`",
                    refused=True)
    sidecar = read_sidecar(key.path)
    if sidecar is None or sidecar.custody != TOUCH_ID:
        raise _Stop("the terminal key is not a Touch ID key", refused=True)
    if sidecar.provider == LEVAIN_SIGN:
        return None
    if not sidecar.ctk_hash:
        raise _Stop("the terminal key's sidecar names no CTK identity to retire", refused=True)
    old_fp = _keys.fingerprint_of(key.public_key)
    row, note = _row(root, old_fp, clock)
    if row is None:
        raise _Stop(f"the terminal key {old_fp} is not enrolled in the authority at {root}, so it cannot sign the new "
                    "key's enrolment; the new key would be orphaned", refused=True)
    if row.revoked_seq is not None:
        raise _Stop(f"the terminal key {old_fp} is already revoked in the authority (seq {row.revoked_seq})",
                    refused=True)
    if row.key_class != KEY_CLASS:
        raise _Stop(f"the terminal key is enrolled as {row.key_class}, not {KEY_CLASS}", refused=True)
    if note:
        raise _Stop(f"the authority at {root} cannot sign right now: {note}", refused=True)
    journal: dict[str, Any] = {"format": JOURNAL_FORMAT, "old_fp": old_fp, "old_public_key": key.public_key,
                               "old_ctk_hash": sidecar.ctk_hash, "stamp": _stamp(clock),
                               "steps": {}, "complete": False}
    _write_journal(d, journal)
    log.append(f"rotating terminal key {old_fp} (journal {_journal_path(d)})")
    return journal


# ---------------------------------------------------------------------------------------------------
# the steps


def _step_made(d: Path, journal: dict[str, Any], make: Callable[..., Any], log: list[str]) -> None:
    steps = journal["steps"]
    if steps.get("made"):
        return
    present = _files_exist(d, NEXT_LABEL)
    if any(present) and not all(present):
        raise _Stop(f"{NEXT_LABEL} files are half there (a leftover); remove the {NEXT_LABEL} files in {d}, then run "
                    "rotate again")
    if all(present):
        # killed between the key landing and the journal: adopt it when it is a helper key
        sidecar = read_sidecar(d / NEXT_LABEL)
        if sidecar is None or sidecar.provider != LEVAIN_SIGN:
            raise _Stop(f"{d / NEXT_LABEL} is there but is not a levain-sign key; remove it, then run rotate again")
        pub = _keys.normalise_public_key((d / (NEXT_LABEL + ".pub")).read_text(encoding="ascii"))
        log.append(f"found the new key an earlier run made: {d / NEXT_LABEL}")
    else:
        try:
            made = make(d, NEXT_LABEL)
        except KeyError_ as exc:
            raise _Stop(f"the new key was not made: {_plain(exc)}") from None
        pub = made.public_key
        sidecar = read_sidecar(d / NEXT_LABEL)
        if sidecar is None or sidecar.provider != LEVAIN_SIGN:
            raise _Stop(f"the creator made a key that is not a levain-sign key; {d / NEXT_LABEL} is left for you to look at")
        log.append(f"made the new key (Secure Enclave, Touch ID on every sign): {d / NEXT_LABEL}")
    steps["made"] = True
    journal["new_fp"] = _keys.fingerprint_of(pub)
    journal["new_public_key"] = pub
    _write_journal(d, journal)


def _key_path_of(d: Path, journal: dict[str, Any], *, which: str) -> Path:
    """Where the old or the new key's file is NOW: the old key sits at the terminal label until its files move, the
    new key at the next label until it is renamed. Judged by the fingerprint of the ``.pub`` found there."""
    want = journal["old_fp"] if which == "old" else journal["new_fp"]
    for label in (ts.KEY_LABEL, NEXT_LABEL):
        pub = d / (label + ".pub")
        if os.path.lexists(pub) and _keys.fingerprint_of((pub).read_text(encoding="ascii")) == want:
            return d / label
    raise _Stop(f"the {which} key ({want}) is not in {d} at the terminal or the next label")


def _step_enrol(d: Path, root: Path, journal: dict[str, Any], sign_for: SignerFactory, clock: Callable[[], datetime],
                declare: Callable[..., str], sc_auth: Path | str, log: list[str]) -> None:
    steps = journal["steps"]
    new_row, _ = _row(root, journal["new_fp"], clock)
    if new_row is not None:
        if new_row.revoked_seq is not None:
            raise _Stop(f"the new key {journal['new_fp']} is revoked in the authority (seq {new_row.revoked_seq}); a "
                        "revoked key stays revoked: remove the rotation by hand")
        if steps.get("enrolled") != new_row.enrolled_seq:
            steps["enrolled"] = new_row.enrolled_seq
            _write_journal(d, journal)
        log.append(f"the new key is enrolled (authority seq {new_row.enrolled_seq})")
        return
    new_path = _key_path_of(d, journal, which="new")
    custody = declare(new_path.with_name(new_path.name + ".pub"), TOUCH_ID, KEY_CLASS, journal["new_public_key"],
                      sc_auth=sc_auth)
    old_path = _key_path_of(d, journal, which="old")
    log.append("enrolling the new key, signed by the OLD key: one Touch ID on the old path")
    signer_fp, sign = sign_for(old_path)
    if signer_fp != journal["old_fp"]:
        raise _Stop("the signer is not the old terminal key")
    row, _created, warning = _keys.enroll(root, journal["new_public_key"], KEY_CLASS, custody=custody,
                                          signer_fp=signer_fp, sign=sign, clock=clock)
    steps["enrolled"] = row.enrolled_seq
    _write_journal(d, journal)
    log.append(f"enrolled the new key {row.fingerprint} as {row.key_class}, custody {row.custody}, at authority seq "
               f"{row.enrolled_seq}")
    if warning:
        log.append(f"warning: {warning}")


def _step_revoke(d: Path, root: Path, journal: dict[str, Any], sign_for: SignerFactory,
                 clock: Callable[[], datetime], log: list[str]) -> None:
    steps = journal["steps"]
    old_row, _ = _row(root, journal["old_fp"], clock)
    if old_row is not None and old_row.revoked_seq is not None:
        if steps.get("revoked") != old_row.revoked_seq:
            steps["revoked"] = old_row.revoked_seq
            _write_journal(d, journal)
        log.append(f"the old key is revoked (authority seq {old_row.revoked_seq})")
        return
    if old_row is None:
        raise _Stop(f"the old key {journal['old_fp']} is not in the authority any more; look at `levain cockpit keys`")
    new_row, _ = _row(root, journal["new_fp"], clock)
    if new_row is None or new_row.revoked_seq is not None:
        raise _Stop("the new key is not enrolled and active in the authority, so it cannot sign the revoke")
    new_path = _key_path_of(d, journal, which="new")
    log.append("revoking the old key, signed by the NEW key through the helper: one Touch ID")
    signer_fp, sign = sign_for(new_path)
    if signer_fp != journal["new_fp"]:
        raise _Stop("the signer is not the new terminal key")
    row, warning = _keys.revoke(root, journal["old_fp"], signer_fp=signer_fp, sign=sign, clock=clock)
    steps["revoked"] = row.revoked_seq
    _write_journal(d, journal)
    log.append(f"revoked the old key {row.fingerprint} at authority seq {row.revoked_seq}")
    if warning:
        log.append(f"warning: {warning}")


def _step_move(d: Path, journal: dict[str, Any], log: list[str]) -> None:
    """The old key's three files into ``retired-<stamp>/`` (all three first), then the new key's three renamed to the
    terminal label. Each file is judged by where it is now, so a run cut off between any two renames is finished
    by the next: a file already in the retired directory is moved; one at the terminal label while the retired
    directory lacks it is the old key's (the new key is renamed only after all three old files are gone)."""
    steps = journal["steps"]
    if steps.get("moved"):
        return
    retired = d / f"retired-{journal['stamp']}"
    try:
        os.mkdir(retired, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(retired)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise _Stop(f"{retired} is not a directory of this user")
    for s in _SUFFIXES:
        # a rename is atomic, so a name at the terminal label AND in the retired directory while the new key's file is
        # still at the next label is never a cut-off run (a cut-off after the new key's rename has no next file left):
        # it is a conflict, found before any file moves
        here, there, nxt = d / (ts.KEY_LABEL + s), retired / (ts.KEY_LABEL + s), d / (NEXT_LABEL + s)
        if os.path.lexists(here) and os.path.lexists(there) and os.path.lexists(nxt):
            raise _Stop(f"{there} already exists while {here} is still in place; nothing was overwritten")
    for s in _SUFFIXES:
        src, dst = d / (ts.KEY_LABEL + s), retired / (ts.KEY_LABEL + s)
        if os.path.lexists(dst):
            continue
        if not os.path.lexists(src):
            raise _Stop(f"{src} is missing; the old key's files are not all in {d} or {retired}")
        if s == ".pub" and _keys.fingerprint_of(src.read_text(encoding="ascii")) != journal["old_fp"]:
            raise _Stop(f"{src} is not the old key; nothing was moved")
        _rename(src, dst)
    for s in _SUFFIXES:
        src, dst = d / (NEXT_LABEL + s), d / (ts.KEY_LABEL + s)
        if not os.path.lexists(src):
            if os.path.lexists(dst):
                continue
            raise _Stop(f"{src} is missing; the new key's files are not all in {d}")
        _rename(src, dst)
    steps["moved"] = True
    _write_journal(d, journal)
    log.append(f"moved the old key's files to {retired} and renamed the new key to {d / ts.KEY_LABEL}")


def _step_ctk(d: Path, journal: dict[str, Any], delete: Callable[[str, Path | str], str | None],
              sc_auth: Path | str, log: list[str]) -> None:
    """Delete the old key's CTK identity (no touch). A failure is reported and recorded, never fatal."""
    steps = journal["steps"]
    if steps.get("ctk_deleted") is not None:
        return
    digest = journal.get("old_ctk_hash")
    why = delete(digest, sc_auth) if digest else "no CTK identity hash was recorded"
    if why is None:
        steps["ctk_deleted"] = True
        log.append(f"deleted the old key's macOS keychain identity {digest}")
    else:
        steps["ctk_deleted"] = False
        steps["ctk_problem"] = why
        log.append(f"could not delete the old keychain identity ({why}); it is harmless (the old key is revoked), and "
                   f"`sc_auth delete-ctk-identity -h {digest}` removes it")
    _write_journal(d, journal)


def _done(d: Path, journal: dict[str, Any], log: list[str]) -> RotateReport:
    steps = journal["steps"]
    lines = [*log, "",
             "The terminal key is rotated.",
             f"- The new key ({journal['new_fp']}) is enrolled in the authority at seq {steps['enrolled']}, signed by "
             f"the old key; the old key ({journal['old_fp']}) is revoked at seq {steps['revoked']}, signed by the new "
             "key. Its files are in " + str(d / f"retired-{journal['stamp']}") + ".",
             "- From now on every terminal answer signs with one Touch ID through Levain's helper.",
             f"- Every sign is recorded (time, outcome, attempts; no message, no signature) in {d / 'sign-log.jsonl'}.",
             "- The key changed, so flow's signer list must change too: commit this line to "
             "scripts/needs_phill_signers (a reviewed commit), in place of the old key's line:",
             f"   {ts.signers_line(journal['new_public_key'])}"]
    return RotateReport("done", "\n".join(lines), EXIT_OK, log)


# ---------------------------------------------------------------------------------------------------
# the CLI handler


def run(args: Any) -> int:
    """``levain band terminal-key rotate``: print the report; 0 done or nothing to rotate, 2 refused, 1 stopped."""
    import sys

    rep = rotate(data_root=getattr(args, "data_root", None), path=getattr(args, "path", None))
    print(rep.text, file=sys.stdout if rep.code == EXIT_OK else sys.stderr)
    return rep.code
