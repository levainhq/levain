"""levain.cockpit.keys: enrolling, revoking and listing the cockpit's signing keys in a REPORTED install.

Who may sign is a verified replay of the signed authority log (``levain.authority``, trust-tiering
DESIGN §3), never a table a writer can edit: an enrol or a revoke is a LINK, signed by a key the log
already holds (an enrol by a laptop-presence key; a revoke by a key of at least the revoked key's class),
judged by the replay at its predecessor. The first link (the genesis) is ``levain cockpit ledger init``'s
(:func:`ledger_init`), so an empty log refuses here, naming it.

``ledger init`` on a store the /2 -> /3 migration marked uninitialised re-enrols the confirmed
``legacy_keys`` rows as links, each its own transaction, the marker still set. Only a last transaction,
which finds the log's head where the last link left it, drops ``legacy_keys`` and clears the marker, so a
run cut off between links leaves a store that still refuses every signed act and that a re-run resumes.

A governed install's keys are written only by slice G's root helper, so every act here refuses a data
root a governed marker names, or a journal started under any class but ``reported``.

The broker generates its signers file from the snapshot, one line per unrevoked key:
``<principal> namespaces="levain-cockpit" <public_key>``. So ``public_key`` is held as exactly
``<type> <base64>``: an option (``cert-authority``, ``valid-before``) or a second key can never ride in
from the enrolled text.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, overload

from levain import authority as _authority
from levain import authority_sig as _authority_sig
from levain.authority import (LAPTOP, NAMESPACE, UNSTATED, AnchorError, AuthorityError, AuthorityKey,
                              AuthoritySnapshot, VerifierUnavailable, _pipe_reading, anchor_path, sign_and_append)
from levain.autonomic.confirm import _VERIFY_ENV, DEFAULT_SSH_KEYGEN
from levain.cockpit.authority import RequestAuthority
from levain.cockpit.firejournal import _CLASS_RANK, DB_NAME, UNINITIALISED, CockpitDB, _meta
from levain.cockpit.install import REPORTED, marker_path, marker_present, marker_unreadable

if TYPE_CHECKING:
    from levain.cockpit.verbs import VerbRegistry

__all__ = ["KEY_CLASSES", "InitPlan", "InitResult", "KeyError_", "KeyRow", "LegacyRow", "Signer", "anchor_reset",
           "enroll", "keys",
           "fingerprint_of", "init_plan", "ledger_init", "normalise_public_key", "policy_raise", "principal_of", "revoke",
           "signing_public_key", "ssh_signer"]

KEY_CLASSES = tuple(sorted(_CLASS_RANK, key=_CLASS_RANK.__getitem__))
# the key types ssh-keygen -Y signs with (sk- = FIDO / Secure Enclave via a security key provider)
_KEY_TYPES = frozenset({"ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
                        "ssh-rsa", "sk-ssh-ed25519@openssh.com",
                        "sk-ecdsa-sha2-nistp256@openssh.com"})
_FP = re.compile(r"\d+ (SHA256:[A-Za-z0-9+/]{43}) ")


class KeyError_(Exception):      # noqa: N801 - not the builtin
    """A refused key act; the message is the operator-facing reason."""


class RealKeyReached(KeyError_, AssertionError):
    """A test run reached the real operator key: a ``KeyError_`` (the sign step records it as an unsigned
    ``signer-error``) that is also an ``AssertionError``, so a test that expects either kind of refusal sees one."""


#: Further scratch roots (``os.pathsep``-separated) a test run names for keys beyond the temp root, set by the suite's
#: conftest only: a trust-anchor directory must live under the account home, where a world-writable temp root cannot
#: hold it. A root that holds the account's own home vouches for nothing (:func:`levain.home_guard.vouched_scratch`).
TEST_KEY_ROOTS_ENV = "LEVAIN_TEST_KEY_ROOTS"


def pwd_key_dir() -> Path:
    """``<real home>/.levain/keys`` from the password database, never ``$HOME`` or ``$LEVAIN_HOME``."""
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".levain" / "keys"


def under_test_run() -> bool:
    """True inside a pytest run: the one detection, :func:`levain.home_guard.under_test_run`."""
    from levain.home_guard import under_test_run as _under_test_run
    return _under_test_run()


@overload
def refuse_real_key_in_tests(path: None, what: str = ...) -> None: ...
@overload
def refuse_real_key_in_tests(path: Path | str, what: str = ...) -> Path: ...
def refuse_real_key_in_tests(path: Path | str | None, what: str = "the key") -> Path | None:
    """Under a pytest run a key or key directory is reachable ONLY inside the scratch root the conftest names
    (:func:`levain.home_guard.vouched_scratch`); anything else raises :class:`RealKeyReached`. This is an ALLOWLIST: it
    replaced a denylist of the operator's real key directory (resolved, case- and Unicode-folded, matched by device and
    inode), which needed a new cut for every spelling of that directory. Nothing outside the scratch root is a key a
    test may sign with, so there is no spelling of the real one to list. Fail closed: a path that will not resolve, a
    run with no scratch root named, or a temp root that holds the account's own home, is refused.

    Returns the resolved path it judged, which a caller uses for every later act so a link retargeted after the
    check is not followed again; outside a pytest run it returns ``path`` with ``~`` expanded, unchanged otherwise.
    ``path`` None returns None and judges nothing: callers pass their already-resolved default."""
    if path is None:
        return None
    if not under_test_run():
        return Path(path).expanduser()
    from levain.home_guard import TEST_SCRATCH_ROOT_ENV, vouched_scratch

    try:
        target = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise RealKeyReached(f"a test reached {what} ({path}), which cannot be resolved ({type(exc).__name__}: {exc}); "
                             "under a test run that is refused. Pass a temp key_dir and a fake signer") from None
    extra = [r for r in os.environ.get(TEST_KEY_ROOTS_ENV, "").split(os.pathsep) if os.path.isabs(r)]
    try:
        real = pwd_key_dir().resolve()
    except (OSError, RuntimeError, KeyError):
        real = None
    # the one thing no named root may do: hold the operator's real key directory (a scratch root that is a link to
    # ~/.levain would otherwise vouch for it); an unnameable real directory is refused, fail closed
    holds_real = real is None or target == real or real in target.parents
    if holds_real or not (vouched_scratch(target)
                          or any(vouched_scratch(target, {TEST_SCRATCH_ROOT_ENV: str(Path(r).resolve())}) for r in extra)):
        raise RealKeyReached(f"a test reached {what} ({target}), which is not inside the scratch root the test run names: "
                             "a test may only use a key it made there, never the operator's (a Touch ID sheet would "
                             "open on the live laptop); pass a temp key_dir and a fake signer")
    return target


@dataclass(frozen=True)
class KeyRow:
    """A key as the authority log's replay holds it: positions are authority seqs."""
    fingerprint: str
    principal: str
    key_class: str
    enrolled_seq: int
    enrolled_at: str | None
    revoked_seq: int | None
    compromised_since: int | None
    legacy_enrolled_at: str | None = None
    custody: str = UNSTATED          # what its enrol record declares (a declaration, never a proof)
    public_key: str | None = None
    # why it is QUARANTINED (levain.authority.AuthoritySnapshot.quarantined_keys), or None
    quarantined: str | None = None


# what signs a link's message (namespace ``levain-authority``); returns the armored SSHSIG
Signer = Callable[[bytes], str]


def principal_of(fingerprint: str) -> str:
    """§3 schema: ``"k" + 32 hex of sha256(fingerprint)``, inside confirm's signer charset."""
    return "k" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:32]


def normalise_public_key(text: str) -> str:
    """``<type> <base64>`` from one OpenSSH public key line (a trailing comment is dropped), or
    ``KeyError_``. Refuses options, several keys, a private key, and a blob whose embedded type is not
    the line's."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if len(lines) != 1:
        raise KeyError_(f"expected exactly one public key line, found {len(lines)}")
    parts = lines[0].split()
    if parts[0] not in _KEY_TYPES:
        raise KeyError_(f"not a public key line (or it carries options): starts {parts[0][:40]!r}")
    if len(parts) < 2:
        raise KeyError_("the key line has no key data")
    ktype, blob64 = parts[0], parts[1]
    try:
        blob = base64.b64decode(blob64, validate=True)
        (n,) = struct.unpack(">I", blob[:4])
        embedded = blob[4:4 + n].decode("ascii")
    except (binascii.Error, struct.error, UnicodeDecodeError, ValueError):
        raise KeyError_("the key data is not a valid OpenSSH key blob") from None
    if embedded != ktype:
        raise KeyError_(f"the key data is a {embedded!r} key, not {ktype!r}")
    return f"{ktype} {blob64}"


def fingerprint_of(public_key: str, *, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN, timeout: float = 10.0) -> str:
    """The ``SHA256:...`` fingerprint ``ssh-keygen -Y verify`` prints for this key (the form the
    broker looks up). The key reaches ssh-keygen through a pipe (``/dev/fd/N``), never a file, so no
    same-uid process can swap it between write and read. Raises :class:`VerifierUnavailable` when
    ssh-keygen cannot run or does not answer (never a verdict on the key), ``KeyError_`` when it reads
    the key and refuses it."""
    try:
        data = (public_key + "\n").encode("ascii")
    except UnicodeError:
        raise KeyError_("the public key is not ASCII") from None
    fd: int | None = None
    try:
        fd = _pipe_reading(data)
        out = subprocess.run([str(ssh_keygen), "-l", "-E", "sha256", "-f", f"/dev/fd/{fd}"],
                             capture_output=True, text=True, timeout=timeout, check=False, pass_fds=(fd,),
                             env=dict(_VERIFY_ENV))   # the verifier's fixed environment, so its parse matches
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        # RuntimeError: the pipe's writer thread could not start (_pipe_reading has closed that pipe)
        raise VerifierUnavailable(f"ssh-keygen could not fingerprint the key ({type(exc).__name__})") from None
    finally:
        if fd is not None:
            os.close(fd)
    m = _FP.match(out.stdout)
    if out.returncode != 0 or m is None:
        why = (out.stderr or out.stdout).replace(f"/dev/fd/{fd}", "<key>").strip()[:200]
        raise KeyError_(f"ssh-keygen could not read the key: {why}")
    return m.group(1)


def _journal(data_root: Path | str, *, marker_directory: Path | None = None) -> CockpitDB:
    """The reported install's journal at ``data_root``; never creates one."""
    root = Path(data_root)
    marker = marker_path(root, marker_directory)
    try:
        governed = marker_present(marker)
    except OSError as exc:
        raise KeyError_(marker_unreadable(marker, exc)) from exc
    if governed:
        raise KeyError_(f"a governed marker names this data root ({marker}): its keys are the root helper's")
    if not (root / DB_NAME).is_file():
        raise KeyError_(f"no cockpit journal at {root / DB_NAME}: start the cockpit once with "
                        "`levain serve --write --install-class reported`")
    # a /2 journal migrates on this open only if it is reported and no server holds cockpit.lock
    return CockpitDB(root, install_class=REPORTED)


def _check_reported(conn: object) -> None:
    """Refuse a journal not started as ``reported`` (its class is set once, insert-or-ignore)."""
    cls = _meta(conn, "install_class")  # type: ignore[arg-type]
    if cls != REPORTED:
        raise KeyError_(f"this journal's install class is {cls!r}; enrolling here is for a reported install")


def _row(k: AuthorityKey, quarantined: str | None = None) -> KeyRow:
    return KeyRow(k.fingerprint, k.principal, k.key_class, k.enrolled_seq, k.enrolled_at, k.revoked_seq,
                  k.compromised_since, k.legacy_enrolled_at, k.custody, k.public_key, quarantined)


def _capture(db: CockpitDB, now: datetime, ssh_keygen: Path | str) -> RequestAuthority:
    with db.read() as conn:
        # capture first: it reads the anchor before the transaction's first SELECT pins the snapshot
        auth = RequestAuthority.capture(conn, now=now, anchor=anchor_path(db.data_root), source=str(db.path),
                                        ssh_keygen=ssh_keygen)
        _check_reported(conn)
        return auth


def _signing_authority(auth: RequestAuthority) -> AuthoritySnapshot:
    """The snapshot a new link is judged against, or the refusal naming why there is none."""
    if auth.authority is None or auth.authority_error is not None:
        raise KeyError_(str(auth.authority_error))
    return auth.authority


def _append(db: CockpitDB, op: Mapping[str, Any], *, signer_fp: str, sign: Signer,
            clock: Callable[[], datetime], ssh_keygen: Path | str) -> AuthoritySnapshot:
    """One signed link, appended under the replay's rules (``levain.authority.sign_and_append``): the
    signature is made outside any transaction, so a touch never holds the journal's write lock."""
    try:
        with db.autocommit() as conn:
            return sign_and_append(conn, op, signer_fp=signer_fp, sign=sign, clock=clock,
                                   anchor=anchor_path(db.data_root), ssh_keygen=ssh_keygen)
    except VerifierUnavailable:
        raise
    except AuthorityError as exc:
        raise KeyError_(str(exc)) from None


def _append_at(db: CockpitDB, op: Mapping[str, Any], checked: AuthoritySnapshot, *, signer_fp: str, sign: Signer,
               clock: Callable[[], datetime], ssh_keygen: Path | str, what: str) -> AuthoritySnapshot:
    """:func:`_append`, bound to the snapshot ``checked`` that the caller judged the link against (leg 6 L3 r1, codex
    HIGH): the link is prepared, and refused unless it follows ``checked``'s head exactly; it is signed; and it is
    appended only on that same head (:func:`levain.authority.append` raises ``StaleHead`` otherwise). A revoke checked
    against one incarnation of a grant id therefore never lands on a later one made under the same id, nor on a
    renewed one. Refused before the signature when the head moved during the check, after it when it moved during the
    signature; either way nothing is written, and the refusal says to run ``what`` again."""
    moved = (f"the authority log moved since {what} was checked (another writer appended); nothing was written: "
             f"look again and run {what} again")
    try:
        with db.autocommit() as conn:
            link = _authority.prepare(conn, op, signer_fp=signer_fp, now=_now(clock), ssh_keygen=ssh_keygen)
    except VerifierUnavailable:
        raise
    except AuthorityError as exc:
        raise KeyError_(str(exc)) from None
    if (link.seq - 1, link.prev_hash) != (checked.seq, checked.head_hash):
        raise KeyError_(moved)
    signature = sign(link.message)
    try:
        with db.autocommit() as conn:
            return _authority.append(conn, link, signature, now=_now(clock), anchor=anchor_path(db.data_root),
                                     ssh_keygen=ssh_keygen)
    except VerifierUnavailable:
        raise
    except _authority.StaleHead:
        raise KeyError_(moved) from None
    except AuthorityError as exc:
        raise KeyError_(str(exc)) from None


def enroll(data_root: Path | str, public_key_text: str, key_class: str, *, signer_fp: str, sign: Signer,
           clock: Callable[[], datetime], custody: str = UNSTATED, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
           marker_directory: Path | None = None, attested: bytes | None = None) -> tuple[KeyRow, bool, str | None]:
    """Enrol one key, declaring its ``custody``, by a link ``signer_fp`` signs (a laptop-presence key).
    The custody table (``levain.authority.custody_problem``, the replay's own rule) is checked before
    anything is signed; a laptop-presence key must state a real custody. Returns ``(row, created,
    warning)``: ``created`` is False when this exact key is already enrolled, unrevoked, with this class
    and custody (nothing is signed); ``warning`` says the link committed but its anchor did not advance.

    ``attested`` is a passkey creation's authenticatorData: checked against the key before anything is signed
    (:func:`levain.cockpit.keyfacts.attested`), and its credential id and AAGUID recorded beside the key. For a key
    this call enrols they are written after the enrol link commits; for a key already enrolled the same way, nothing
    is signed and they fill a missing row (:func:`levain.cockpit.keyfacts.repair_enrolment`), so rerunning an enrol
    whose facts were never written recovers them, and ``warning`` names a different credential id already held.
    They are display facts outside the log, never authority."""
    from levain.cockpit import keyfacts

    if key_class not in _CLASS_RANK:
        raise KeyError_(f"unknown key class {key_class!r}; one of {', '.join(KEY_CLASSES)}")
    public_key = normalise_public_key(public_key_text)
    problem = _authority.custody_problem(key_class, custody, public_key.split()[0], public_key=public_key)
    if problem is not None:
        raise KeyError_(problem)
    facts = None
    if attested is not None:
        try:
            facts = keyfacts.attested(attested, public_key)
        except keyfacts.KeyFactsError as exc:
            raise KeyError_(f"--attested: {exc}") from None
    fp = fingerprint_of(public_key, ssh_keygen=ssh_keygen)
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, _now(clock), ssh_keygen))
    old = state.key(fp)
    if old is not None:
        if not old.active:
            raise KeyError_(f"{fp} was revoked at seq {old.revoked_seq}; a revoked key stays revoked")
        if old.key_class != key_class:
            raise KeyError_(f"{fp} is enrolled as {old.key_class!r}; a key keeps its class (enroll a new "
                            "key for another class)")
        if old.custody != custody:
            raise KeyError_(f"{fp} is enrolled with custody {old.custody!r}; a key keeps its custody")
        warning = None
        if facts is not None:
            try:
                keyfacts.repair_enrolment(Path(data_root), fp, facts, now=_now(clock).isoformat())
            except keyfacts.KeyFactsError as exc:
                warning = f"its credential id and provider were kept as held: {exc}"
            except (OSError, sqlite3.Error) as exc:
                warning = f"its credential id and provider were not recorded: {exc}"
        return _row(old), False, warning
    new = _append(db, {"op": "enrol", "fingerprint": fp, "public_key": public_key, "class": key_class,
                       "custody": custody}, signer_fp=signer_fp, sign=sign, clock=clock, ssh_keygen=ssh_keygen)
    key = new.key(fp)
    assert key is not None
    warning = new.anchor_warning
    if facts is not None:
        try:
            keyfacts.record_enrolment(Path(data_root), fp, facts, now=_now(clock).isoformat())
        except (OSError, sqlite3.Error) as exc:   # the key is enrolled; only the display facts are missing
            warning = "; ".join(filter(None, (warning, f"its credential id and provider were not recorded: {exc}")))
    return _row(key), True, warning


def revoke(data_root: Path | str, ref: str, *, signer_fp: str, sign: Signer, clock: Callable[[], datetime],
           compromised_since: int | None = None, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
           marker_directory: Path | None = None) -> tuple[KeyRow, str | None]:
    """Revoke the key ``ref`` names (its fingerprint or principal) by a link ``signer_fp`` signs (a key
    of at least its class). The replay refuses a plain revoke of the last unrevoked laptop-presence
    key (it would leave nothing to sign a T3); with ``compromised_since`` (an authority seq, which
    ``levain cockpit keys`` lists) it lands, and that key's fires decided at or after that seq read
    "signed by a revoked key". Returns ``(row, warning)`` as :func:`enroll`."""
    if compromised_since is not None and (type(compromised_since) is not int or compromised_since < 1):
        raise KeyError_("--compromised-since is an authority seq (an integer >= 1)")
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, _now(clock), ssh_keygen))
    target = next((k for k in state.keys.values() if ref in (k.fingerprint, k.principal)), None)
    if target is None:
        raise KeyError_(f"no enrolled key {ref!r}")
    if not target.active:
        raise KeyError_(f"{target.fingerprint} is already revoked (at seq {target.revoked_seq})")
    op: dict[str, Any] = {"op": "revoke_key", "fingerprint": target.fingerprint}
    if compromised_since is not None:
        op["compromised_since"] = compromised_since
    new = _append(db, op, signer_fp=signer_fp, sign=sign, clock=clock, ssh_keygen=ssh_keygen)
    key = new.key(target.fingerprint)
    assert key is not None
    return _row(key), new.anchor_warning


def policy_raise(data_root: Path | str, verb: str, tier: str, *, signer_fp: str, sign: Signer,
                 clock: Callable[[], datetime], ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                 marker_directory: Path | None = None,
                 registry: VerbRegistry | None = None) -> tuple[str, int, str | None]:
    """Raise ``verb``'s floor to ``tier`` in the standing policy (trust-tiering DESIGN §4.3), by a link
    ``signer_fp`` signs. The replay classifies the change itself: a raise that tightens needs a
    display-phone key or above, one that would lower a floor is refused as a loosen, and one that
    changes nothing is refused. Returns ``(tier, seq, warning)``: the verb's floor in the policy after
    the link, the link's seq, and the anchor warning as :func:`enroll`.

    ``verb`` is checked against ``registry`` (an embedder passes the one it serves), else against the
    kernel's verbs and the record of the verbs the deployment's server serves (:func:`_raisable`)."""
    if not isinstance(verb, str) or not verb:
        raise KeyError_("a raise names a verb")
    _raisable(verb, registry=registry, data_root=data_root)
    db = _journal(data_root, marker_directory=marker_directory)
    _signing_authority(_capture(db, _now(clock), ssh_keygen))     # refused before anything is signed
    new = _append(db, {"op": "raise", "verb": verb, "tier": tier}, signer_fp=signer_fp, sign=sign, clock=clock,
                  ssh_keygen=ssh_keygen)
    return new.policy.raise_to[verb], new.seq, new.anchor_warning


def _raisable(verb: str, *, registry: VerbRegistry | None = None, data_root: Path | str | None = None) -> None:
    """Refuse a raise that would be signed and then honoured by nothing: a verb the deployment does not
    serve, or an internal verb that honours no floor.

    The verbs served are ``registry``'s when the caller holds it (an embedder passing its
    ``make_server(extra_verbs=)`` set); else the kernel's, and for any other name the record the
    deployment's server wrote under ``data_root`` when it was built (``served_verbs.json``,
    :func:`levain.cockpit.verbs.record_served_verbs`), checked by exact membership. With no record the
    raise of a non-kernel verb is refused."""
    from levain.cockpit.broker import FLOOR_INTERNAL_VERBS, INTERNAL_VERBS
    from levain.cockpit.verbs import SERVED_VERBS_NAME, VerbRegistry, served_verbs

    if verb in INTERNAL_VERBS and verb not in FLOOR_INTERNAL_VERBS:
        raise KeyError_(f"{verb!r} honours no standing-policy floor (it is always T3); nothing to raise")
    if verb in FLOOR_INTERNAL_VERBS or verb in (registry if registry is not None else VerbRegistry()).names():
        return
    if registry is not None:
        raise KeyError_(f"{verb!r} is not a verb this deployment registers; a raise of it would protect nothing")
    if data_root is None:
        raise KeyError_(f"{verb!r} is not a cockpit verb; a raise of it would protect nothing")
    record = Path(data_root) / SERVED_VERBS_NAME
    try:
        served, served_at = served_verbs(data_root)
    except FileNotFoundError:
        raise KeyError_(f"{verb!r} is not a kernel verb and no record of the verbs this deployment serves is at "
                        f"{record}: start the deployment's cockpit server once (it writes the record when it "
                        "is built), or embed and pass registry=") from None
    except (OSError, ValueError) as exc:
        raise KeyError_(f"{verb!r} is not a kernel verb and the record of the verbs this deployment serves is "
                        f"unreadable ({exc}): start the deployment's cockpit server once to rewrite it") from exc
    if verb not in served:
        raise KeyError_(f"{verb!r} is not a verb this deployment serves (its server recorded its verbs at "
                        f"{served_at}); a raise of it would protect nothing")


def keys(data_root: Path | str, *, now: datetime, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
         marker_directory: Path | None = None) -> tuple[list[KeyRow], str | None]:
    """Every key the authority log's replay holds, revoked ones included, in enrol order, and the
    reason nothing may sign right now (None when keys may sign)."""
    db = _journal(data_root, marker_directory=marker_directory)
    auth = _capture(db, now, ssh_keygen)
    if auth.authority is None:
        if not auth.log_empty:
            raise KeyError_(str(auth.authority_error))
        return [], auth.authority_error
    rows = sorted(auth.authority.keys.values(), key=lambda k: k.enrolled_seq)
    held = {k.fingerprint: why for k, why in auth.authority.quarantined_keys()}
    return [_row(k, held.get(k.fingerprint)) for k in rows], auth.authority_error


# ---------------------------------------------------------------------------------------------------
# `levain cockpit anchor reset` (DESIGN §3.8)


def anchor_reset(data_root: Path | str, *, signer_fp: str, sign: Signer, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                 marker_directory: Path | None = None) -> tuple[Mapping[str, Any] | None, _authority.Anchor | None]:
    """Move the anchor to the log's head on a laptop-presence signature over ``{store_id, old_anchor, new_head}``
    (namespace ``levain-authority-reset``; ``sign`` must sign in it), verified against the log's OWN keys as of
    that head (:func:`levain.authority.reset_anchor`, which stores the proof in the anchor file). Returns
    ``(old_anchor, new)``; ``new`` is None, and nothing is signed, when the anchor is absent or already agrees
    with the log. The log is replayed in full with a fresh cache: this is the trust decision, so no chain this
    process verified earlier stands in for it.

    Everything that can refuse is judged before ``sign`` asks for a touch: an anchor of another store (a reset is
    for a rollback of the same store, DESIGN §3.8), a signer that is not one of the log's laptop-presence keys that
    can sign (:meth:`~levain.authority.AuthoritySnapshot.signing_laptop_keys`: unrevoked, not quarantined), and a
    live cockpit server. The reset lock (:func:`levain.authority.reset_lock`) is held from the read of the old
    anchor through the signature, a re-read of the log's head and the write, so no server starts, no advance lands
    and no link is appended unseen in between; only the signature itself is judged after the touch."""
    db = _journal(data_root, marker_directory=marker_directory)
    path = anchor_path(db.data_root)
    cache = _authority.ReplayCache()
    with db.read() as conn:
        # anchor=None: the log as it stands, which the anchor is about to be judged against, not by
        auth = RequestAuthority.capture(conn, now=datetime.now(timezone.utc), anchor=None, source=str(db.path),
                                        cache=cache, ssh_keygen=ssh_keygen)
        _check_reported(conn)
    if auth.authority is None:
        if auth.log_empty:
            init = _authority.init_hint(db.data_root)
            if _authority._old_anchor_view(path) is None:
                raise KeyError_(f"the authority log is empty and has no anchor: run {init}")
            raise KeyError_(f"the authority log is empty, so it holds no key a reset can verify against; if this "
                            f"store was emptied on purpose, move {path} aside, then run {init}")
        raise KeyError_(str(auth.authority_error))
    snap = auth.authority

    def agrees() -> Mapping[str, Any] | None | bool:
        """The anchor as it stands when it needs no reset (absent, at the head, or behind it: the next open or
        append writes or advances it), else False (unreadable, or the log is behind it: the case a reset is for)."""
        try:
            current = _authority.read_anchor(path)
            _authority.check_anchor(snap, current, path=path)
        except _authority.ForeignAnchor as exc:
            raise KeyError_(f"{exc} (nothing signed)") from None
        except AnchorError:
            return False
        return None if current is None else current.as_dict()

    # first without the lock, so an anchor that needs nothing is answered while a server runs
    if (stands := agrees()) is not False:
        return stands, None
    with _authority.reset_lock(path) as held:
        if (stands := agrees()) is not False:     # again under the lock: an advance may have landed
            return stands, None
        key = snap.key(signer_fp)
        if key is None or not key.active:
            raise KeyError_(f"{signer_fp} is not an unrevoked key of this log at seq {snap.seq} (nothing signed)")
        if key.key_class != LAPTOP:
            raise KeyError_(f"an anchor reset needs a {LAPTOP} key, not {key.key_class} (nothing signed)")
        if signer_fp not in {k.fingerprint for k in snap.signing_laptop_keys()}:
            why = dict((k.fingerprint, w) for k, w in snap.quarantined_keys()).get(signer_fp, "it cannot sign")
            raise KeyError_(f"{signer_fp} is quarantined, so it cannot sign an anchor reset: {why} (nothing signed)")
        old = _authority._old_anchor_view(path)
        signature = sign(_authority.reset_message(snap.store_id, old, snap.seq, snap.head_hash))
        with db.read() as conn:
            # a CLI append between the capture and here (the store lock keeps out a server, not the CLI)
            again = RequestAuthority.capture(conn, now=datetime.now(timezone.utc), anchor=None, source=str(db.path),
                                             cache=cache, ssh_keygen=ssh_keygen).authority
        if again is None or (again.seq, again.head_hash) != (snap.seq, snap.head_hash):
            at = "unreadable" if again is None else f"at seq {again.seq}"
            raise KeyError_(f"the authority log moved while the reset was signed (it was at seq {snap.seq}, now "
                            f"{at}); nothing written, run the reset again")
        try:
            new = _authority.reset_anchor(path, snap, signer_fp=signer_fp, signature=signature,
                                          ssh_keygen=ssh_keygen, held=held)
        except VerifierUnavailable:
            raise
        except AuthorityError as exc:
            raise KeyError_(str(exc)) from None
    return old, new


# ---------------------------------------------------------------------------------------------------
# `levain cockpit ledger init` (DESIGN §3.2 genesis, §3.7)


@dataclass(frozen=True)
class LegacyRow:
    """A ``legacy_keys`` row the /2 -> /3 migration kept, as ``ledger init`` lists it."""
    fingerprint: str
    key_class: str
    key_type: str
    enrolled_at: str | None
    revoked: bool
    compromised: bool                # /2 recorded a compromised_since time for it
    public_key: str | None           # its normal form; None when the row's key cannot be read
    problem: str | None              # why it cannot be re-enrolled (listed and skipped); None when it can
    in_log: KeyRow | None = None     # what an earlier, interrupted init already appended for it


@dataclass(frozen=True)
class InitPlan:
    """What ``ledger init`` finds: a fresh store (no migration marker, an empty log) or a migrated one,
    its log's head (0 when empty) and genesis key, the legacy rows in enrol order, and every key the log
    already holds (an interrupted init's), in enrol order, the genesis first."""
    migrated: bool
    seq: int
    genesis: str | None
    rows: tuple[LegacyRow, ...]
    log_keys: tuple[KeyRow, ...] = ()


@dataclass(frozen=True)
class InitResult:
    links: tuple[str, ...]           # each link this run appended, as "seq N: what"
    kept: tuple[str, ...]            # legacy fingerprints now in the log (revoked ones included)
    dropped: tuple[str, ...]         # legacy fingerprints not confirmed
    skipped: tuple[str, ...]         # legacy fingerprints that cannot be re-enrolled
    seq: int
    store_id: str
    warning: str | None              # the anchor is behind the log's head (the log stands)


def _legacy_row(raw: Any, state: AuthoritySnapshot | None, ssh_keygen: Path | str) -> LegacyRow:
    fp, pub, cls, enrolled_at, revoked_at, compromised = (raw[0], raw[1], raw[2], raw[3], raw[4], raw[5])
    fp = str(fp)
    words = pub.split() if isinstance(pub, str) else []
    key_type = words[0] if words else "?"
    normal: str | None = None
    problem: str | None = None
    if cls not in _CLASS_RANK:
        problem = f"unknown class {cls!r}"
    else:
        try:
            normal = normalise_public_key(pub if isinstance(pub, str) else "")
            if fingerprint_of(normal, ssh_keygen=ssh_keygen) != fp:
                problem = "its public key's fingerprint is not the row's"
        except KeyError_ as exc:
            normal, problem = None, f"its public key cannot be read ({exc})"
        # read through the module at call time: the floor is authority's custody table, never a copy here.
        # A carried row is re-enrolled with its custody unstated (/2 never recorded one), which the replay
        # admits for a laptop-presence key only on a record carrying legacy_enrolled_at (its enrol time).
        if problem is None:
            problem = _authority.custody_problem(cls, UNSTATED, key_type, legacy=isinstance(enrolled_at, str),
                                                 public_key=normal)
    held = None if state is None else state.key(fp)
    return LegacyRow(fp, str(cls), key_type, enrolled_at if isinstance(enrolled_at, str) else None,
                     revoked_at is not None, compromised is not None, normal, problem,
                     None if held is None else _row(held))


def _init_state(db: CockpitDB, now: datetime,
                ssh_keygen: Path | str) -> tuple[InitPlan, AuthoritySnapshot | None]:
    with db.read() as conn:
        # capture first: it reads the anchor before the transaction's first SELECT pins the snapshot
        auth = RequestAuthority.capture(conn, now=now, anchor=anchor_path(db.data_root), source=str(db.path),
                                        ssh_keygen=ssh_keygen)
        _check_reported(conn)
        marker = _meta(conn, UNINITIALISED)
        has_legacy = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                                  "AND name = 'legacy_keys'").fetchone() is not None
        legacy = conn.execute("SELECT fingerprint, public_key, class, enrolled_at, revoked_at, compromised_since "
                              "FROM legacy_keys ORDER BY enrolled_at, fingerprint").fetchall() if has_legacy else []
    if auth.authority is None and not auth.log_empty:
        raise KeyError_(str(auth.authority_error))
    state = auth.authority
    if marker is None:
        if state is not None:
            raise KeyError_(f"the authority log is already initialised (head at seq {state.seq}); "
                            "`levain cockpit enroll` adds a key")
        if has_legacy:
            raise KeyError_("this store holds legacy_keys but no migration marker; `ledger init` does not "
                            "recognise it")
    rows = tuple(_legacy_row(r, state, ssh_keygen) for r in legacy)
    genesis = None if state is None else next(
        (k.fingerprint for k in state.keys.values() if k.enrolled_seq == 1), None)
    held = () if state is None else tuple(_row(k) for k in sorted(state.keys.values(),
                                                                   key=lambda k: k.enrolled_seq))
    return InitPlan(marker is not None, 0 if state is None else state.seq, genesis, rows, held), state


def init_plan(data_root: Path | str, *, now: datetime, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
              marker_directory: Path | None = None) -> InitPlan:
    """What ``ledger init`` would work from, for the operator to confirm row by row; refuses a store that
    is already initialised. Opening a /2 journal here migrates it (as every key command does)."""
    db = _journal(data_root, marker_directory=marker_directory)
    return _init_state(db, now, ssh_keygen)[0]


def ledger_init(data_root: Path | str, *, signer_public_key: str, signer_fp: str, sign: Signer,
                clock: Callable[[], datetime], confirmed: Collection[str], signer_custody: str,
                drop_unlisted: bool = False, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                marker_directory: Path | None = None) -> InitResult:
    """Initialise the authority log, every link signed by ``signer_fp`` (a laptop-presence key, of any
    custody the custody table admits, ``signer_custody``: a touch-id or fido key asks a touch per link, a
    passphrase key its passphrase).

    The genesis declares ``signer_custody`` (a real one: a laptop-presence key states its custody, a
    carried legacy signing key included, because its custody is what the operator says now); each other
    carried legacy row is re-enrolled with custody ``unstated``.

    A fresh store (no migration marker, an empty log) gets one link, the genesis: ``signer_fp``
    enrolled as laptop-presence, signed by itself (the trust on first use of DESIGN §3.2). A migrated
    store gets the same genesis (carrying ``legacy_enrolled_at`` when the signing key is a legacy row),
    then, per confirmed legacy row in enrol order, an ``enrol`` (carrying ``legacy_enrolled_at``) and,
    for a row /2 revoked, a ``revoke_key``. A revoked row /2 marked compromised is revoked with
    ``compromised_since`` at its own enrol seq: its /2-era fires carry no authority seq, so any
    ``compromised_since`` reads them "signed by a revoked key".

    Every key enrolled is confirmed, the signing key included (it is in ``confirmed``). A row that
    cannot be re-enrolled is skipped; confirming one refuses. An eligible row not confirmed is dropped
    only with ``drop_unlisted``, else this refuses naming them. Each link is its own transaction and
    the migration marker stays set while they land, so a run cut off between links leaves a store that
    still refuses every signed act; a re-run resumes from the log. A resume confirms the log's genesis
    too (whatever key wrote it), else refuses naming it; an earlier run's key that is no longer confirmed
    is revoked with ``compromised_since`` at its own enrol seq, so its seq-less /2-era fires read
    "signed by a revoked key", the nearest the log can say to a dropped key's "unverified"; one that run
    already revoked without a compromise refuses unless confirmed, as no link can make it compromised. A refusal
    after a link landed says where the log stands. Only the last transaction, which finds the log's head where the last
    link left it, drops ``legacy_keys`` and clears the marker."""
    db = _journal(data_root, marker_directory=marker_directory)
    plan, state = _init_state(db, _now(clock), ssh_keygen)
    signer_key = normalise_public_key(signer_public_key)
    if fingerprint_of(signer_key, ssh_keygen=ssh_keygen) != signer_fp:
        raise KeyError_("the signing key's public half is not the key that signs")
    problem = _authority.custody_problem(LAPTOP, signer_custody, signer_key.split()[0], public_key=signer_key)
    if problem is not None:
        raise KeyError_(f"the signing key: {problem}")
    confirmed = set(confirmed)
    by_fp = {r.fingerprint: r for r in plan.rows}
    unknown = sorted(confirmed - set(by_fp) - {signer_fp} - ({plan.genesis} if plan.genesis else set()))
    if unknown:
        raise KeyError_(f"no legacy key {', '.join(unknown)} to confirm")
    if signer_fp not in confirmed:
        raise KeyError_(f"the signing key {signer_fp} is not confirmed; every key init enrols is confirmed, "
                        "the signing key included")
    mine = by_fp.get(signer_fp)
    if mine is not None:
        if mine.key_class != LAPTOP:
            raise KeyError_(f"the signing key is a legacy {mine.key_class} key; init signs with a {LAPTOP} key")
        if mine.revoked:
            raise KeyError_("the signing key was revoked before the migration; a revoked key stays revoked")
    bad = [r for r in plan.rows if r.fingerprint in confirmed and r.problem is not None]
    if bad:
        raise KeyError_("; ".join(f"{r.fingerprint} cannot be re-enrolled: {r.problem}" for r in bad))
    unlisted = [r.fingerprint for r in plan.rows
                if r.problem is None and r.fingerprint not in confirmed and r.fingerprint != signer_fp]
    if unlisted and not drop_unlisted:
        raise KeyError_(f"not confirmed: {', '.join(unlisted)}; confirm each one to keep, or pass "
                        "--drop-unlisted to drop the rest")
    if state is not None:
        signer = state.key(signer_fp)
        if signer is None or not signer.active or signer.key_class != LAPTOP:
            raise KeyError_(f"an earlier ledger init began this log (genesis {plan.genesis}, head at seq "
                            f"{state.seq}); sign with an unrevoked {LAPTOP} key it enrolled")
        if plan.genesis not in confirmed:
            raise KeyError_(f"the log's genesis {plan.genesis} (seq 1, written by an earlier, interrupted init) "
                            "is not confirmed; a resume cannot drop the genesis: confirm it (it stays a "
                            f"{LAPTOP} key) to resume this log")
        foreign = sorted(k.fingerprint for k in state.keys.values()
                         if k.fingerprint not in by_fp and k.enrolled_seq != 1)
        if foreign:
            raise KeyError_(f"the log holds {', '.join(foreign)}, which no legacy row names; this is not an "
                            "interrupted init")
        gone = [r.fingerprint for r in plan.rows if r.fingerprint in confirmed and r.in_log is not None
                and r.in_log.revoked_seq is not None and not r.revoked]
        if gone:
            raise KeyError_(f"{', '.join(gone)} was revoked by an earlier, interrupted init (dropped there); "
                            "a revoked key stays revoked: leave it unconfirmed")
        # no op turns a plain revocation compromised, so dropping such a key would report "dropped" (its
        # /2-era fires unverified) while the log keeps them "signed by a revoked key"
        plain = [r.fingerprint for r in plan.rows if r.fingerprint not in confirmed and r.in_log is not None
                 and r.in_log.revoked_seq is not None and r.in_log.compromised_since is None]
        if plain:
            raise KeyError_("; ".join(f"{fp} is already in this log, revoked by the interrupted run; confirm it to "
                                      "keep that record (its earlier acts read as signed by a revoked key)"
                                      for fp in plain))

    links: list[str] = []
    warning: str | None = None

    landed = plan.seq                         # the log's head as this run last saw it land

    def put(op: dict[str, Any], what: str) -> AuthoritySnapshot:
        nonlocal warning, landed
        try:
            new = _append(db, op, signer_fp=signer_fp, sign=sign, clock=clock, ssh_keygen=ssh_keygen)
        except (KeyError_, AuthorityError, sqlite3.Error, OSError) as exc:    # what the CLI answers as a refusal
            if not landed:
                raise
            raise KeyError_(f"{exc}; the log is at seq {landed}; the store stays uninitialised; re-run to "
                            "resume") from exc
        landed = new.seq
        warning = new.anchor_warning          # a later advance that lands supersedes an earlier miss
        links.append(f"seq {new.seq}: {what}")
        return new

    def enrol_op(fp: str, public_key: str, cls: str, legacy_at: str | None, custody: str) -> dict[str, Any]:
        op: dict[str, Any] = {"op": "enrol", "fingerprint": fp, "public_key": public_key, "class": cls,
                              "custody": custody}
        if legacy_at is not None:
            op["legacy_enrolled_at"] = legacy_at
        return op

    if state is None:
        state = put(enrol_op(signer_fp, signer_key, LAPTOP, None if mine is None else mine.enrolled_at,
                             signer_custody),
                    f"enrol {signer_fp} as {LAPTOP} (the genesis, signed by itself)")
    kept, dropped, skipped = [], [], []
    for r in plan.rows:
        if r.fingerprint == signer_fp:
            kept.append(r.fingerprint)
            continue
        if r.problem is not None:
            skipped.append(r.fingerprint)
            continue
        have = state.key(r.fingerprint)
        if r.fingerprint not in confirmed:
            if have is not None and have.active:
                # a dropped key's /2-era fires would read UNVERIFIED (no key); the log's nearest is a
                # compromise from its own enrol, which reads those seq-less fires REVOKED (O3)
                state = put({"op": "revoke_key", "fingerprint": r.fingerprint,
                             "compromised_since": have.enrolled_seq},
                            f"revoke {r.fingerprint} (an interrupted init enrolled it; now dropped, "
                            "compromised from its enrol)")
            dropped.append(r.fingerprint)
            continue
        if have is None:
            assert r.public_key is not None
            state = put(enrol_op(r.fingerprint, r.public_key, r.key_class, r.enrolled_at, UNSTATED),
                        f"enrol {r.fingerprint} as {r.key_class}")
            have = state.key(r.fingerprint)
            assert have is not None
        if r.revoked and have.active:
            op: dict[str, Any] = {"op": "revoke_key", "fingerprint": r.fingerprint}
            if r.compromised:
                op["compromised_since"] = have.enrolled_seq
            state = put(op, f"revoke {r.fingerprint} (revoked before the migration"
                            + (", compromised)" if r.compromised else ")"))
        kept.append(r.fingerprint)

    if plan.migrated:
        for fp in kept:
            k = state.key(fp)
            if k is None or k.active == by_fp[fp].revoked:
                raise KeyError_(f"{fp} is not in the log as its legacy row says; run `ledger init` again")
        # the anchor reaches the final head before the marker clears, even when this run appended nothing
        # (a resume after a link whose advance failed); this result says so when it cannot, superseding
        # what any link here reported
        try:
            _authority.advance_anchor(anchor_path(db.data_root), state)
            warning = None
        except (AnchorError, OSError) as exc:
            warning = f"the log is at seq {state.seq}; the anchor is behind ({exc})"
        with db.write() as conn:
            if _meta(conn, UNINITIALISED) is None:
                raise KeyError_("another `ledger init` finished this store while this one ran")
            head = conn.execute("SELECT seq, hash FROM authority_log ORDER BY seq DESC LIMIT 1").fetchone()
            if head is None or (head[0], head[1]) != (state.seq, state.head_hash):
                raise KeyError_(f"the authority log moved past seq {state.seq} while init ran; run it again")
            conn.execute("DROP TABLE IF EXISTS legacy_keys")
            conn.execute("DELETE FROM meta WHERE key = ?", (UNINITIALISED,))
    return InitResult(tuple(links), tuple(kept), tuple(dropped), tuple(skipped), state.seq, state.store_id, warning)


def _now(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise KeyError_("the clock must give an aware time")
    return now


_SIGN_TIMEOUT = 120.0          # a touch-required key waits on a person: the whole sign step's bound, retry included (ssh_signer)
_PUBLIC_KEY_MAX = 16 * 1024


def signing_public_key(key_path: Path) -> str:
    """The normal form (``<type> <base64>``) of the signing key's public half: ``<path>.pub``, or
    ``key_path`` itself when it is the ``.pub``."""
    key_path = Path(key_path).expanduser()
    # `/` and `.` have no file name to take a `.pub` beside: a refusal, never with_name's ValueError traceback
    if not key_path.name:
        raise KeyError_(f"{key_path} names no key file")
    pub = key_path if key_path.suffix == ".pub" else key_path.with_name(key_path.name + ".pub")
    try:
        with pub.open("rb") as fh:
            raw = fh.read(_PUBLIC_KEY_MAX + 1)
    except OSError as exc:
        raise KeyError_(f"cannot read the signing key's public half {pub}: {exc.strerror or exc}") from None
    if len(raw) > _PUBLIC_KEY_MAX:
        raise KeyError_(f"{pub} is larger than a public key file")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise KeyError_(f"{pub} is not a public key file") from None
    return normalise_public_key(text)


# an EMPTY answer (the provider answered without a usable touch) is asked for again after a wait, up to this many attempts in all
_SK_EMPTY_ATTEMPTS = 3
# the waits before attempt 2 and attempt 3 after an empty answer: a measurement of 2026-11-19, ONE run of three rounds
# (probe2_run1_1119.txt in levain project_memory 1119-14_sign_probe): a sign about a second after an empty one mostly
# answered, a longer gap was needed after a second empty. The next failures carry their own timing (elapsed_s in the evidence).
_EMPTY_RETRY_WAITS_S = (1.0, 2.0)         # indexed by attempt number - 1: the tables here must cover _SK_EMPTY_ATTEMPTS
_ORDINALS = {2: "second", 3: "third"}
_SK_RETRIES = 1                # a security-key answer that does not verify is asked for once more, never twice (levain#125)
# a second touch is asked for only while at least this much of the ONE sign bound is left: the sheet comes up at once and a
# touch takes a second or two, so 5 s is a real chance; any less and the person would be shown a sheet that closes under them
_SK_RETRY_MIN_S = 5.0
# held back from each ssh-keygen sign child's wait (never more than half of what is left), so an answer that comes in the
# last moment that child is given still leaves its check room to run inside the same deadline
_VERIFY_RESERVE_S = 1.0
_SIGN_FAILURE_KEEP = 20        # evidence files kept per key directory; older ones are deleted when a new one is written
_SIGN_FAILURE_DIR = "sign-failures"
_SIGN_FAILURE_PREFIX = "sigfail-"


@dataclass(frozen=True)
class _SkRefusal:
    """An sk signature that carried the presence flag but did not verify: what ``sign`` needs to retry and to say so."""
    flags: int
    carried: str
    detail: str
    evidence: str                # the evidence file's path, or "evidence not written (<why>)"
    empty: bool = False          # the ECDSA signature inside has empty r and s: the key returned no signature (levain#153)
    touch: bool = False          # the signer is a Touch ID one (its sidecar says so, or the Apple provider signed it)


# what a person reads when the key's answer carried no signature at all (levain#153): the key said "done" and gave nothing back
# Touch ID is named only when the sign path knows the signer is one (its sidecar's custody, or the Apple provider)
def _empty_sig_words(touch: bool) -> str:
    return "the key returned no signature" + (" (Touch ID gave nothing back)" if touch else "")


_EMPTY_SIG_REASON = "empty ECDSA signature (r and s have zero length)"
_EMPTY_SIG_NEXT = "try again, or answer from your phone instead (open the cockpit sign page)"


def _write_sign_failure(key_path: Path, *, fp: str, message: bytes, signature: str, sig_type: str, flags: int,
                        counter: int | None, detail: str, namespace: str, attempt: int,
                        empty: bool = False, elapsed: float) -> str:
    """Keep what a refused sk signature was, for the next person to read: ``<key dir>/sign-failures/sigfail-*.json``
    (0600, in a 0700 directory), written beside the key and its sidecar, the one place the signer already reads from
    (it has no data root or diagnostics directory of its own). The newest :data:`_SIGN_FAILURE_KEEP` are kept; older
    ones are deleted (:func:`_prune_sign_failures`). The file holds the armored signature, its flags byte and counter,
    BOTH ssh-keygen verdicts (``detail``), the key fingerprint and the time, and ``reason``: the exact classification
    when there is one (:data:`_EMPTY_SIG_REASON`: the signature's r and s are empty), else null. ``reason`` is a field
    added to ``levain-sign-failure/1``; every earlier key is unchanged, and a record from before it has no ``reason``.
    ``elapsed_s`` (seconds the ssh-keygen sign step took, 3 decimals) is likewise added; an older record has none.

    The directory is trusted only as this uid's own 0700 directory, opened without following a symlink, and every
    write and prune goes through that one open descriptor; anything else (a symlink, another owner, a looser or
    tighter mode) and nothing is written there.

    No message bytes are stored: the message is what a person was asked to authorise (a consent answer, a ruling's
    words), and a diagnostics file on disk must not be a second copy of it. What IS stored is the message's sha256 and
    its length, the comparison key the failure analysis needs (it ties the file to the message the caller holds, and
    with the armored signature lets ``check-novalidate`` be re-run once that message is in hand). The hash is not a
    secret: whoever can read this file and can guess the message (a short answer, a known template) can confirm the
    guess offline. Returns the path, or ``evidence not written (why)``: a diagnostic never fails a sign and never
    replaces the refusal's own text."""
    dfd = None
    try:
        directory = Path(key_path).parent / _SIGN_FAILURE_DIR
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        if os.path.islink(directory):
            return f"evidence not written ({directory} is a symlink)"
        # O_NOFOLLOW on the directory itself, so a symlink swapped in after the check above is refused, not followed
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        st = os.fstat(dfd)
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            return (f"evidence not written ({directory} is not this user's 0700 directory: "
                    f"mode {stat.S_IMODE(st.st_mode):04o}, uid {st.st_uid})")
        now = datetime.now(timezone.utc)
        record = {"format": "levain-sign-failure/1", "time": now.isoformat(), "fingerprint": fp, "namespace": namespace,
                  "algorithm": sig_type, "flags": format(flags, "#04x"), "counter": counter, "attempt": attempt,
                  "message_sha256": hashlib.sha256(message).hexdigest(), "message_bytes": len(message),
                  "ssh_keygen": detail, "signature": signature,
                  "reason": _EMPTY_SIG_REASON if empty else None,
                  "elapsed_s": round(float(elapsed), 3)}
        body = (json.dumps(record, indent=1) + "\n").encode("utf-8")
        # fixed width throughout (timestamp, pid, attempt) so name order is time, then process, then attempt
        name = f"{_SIGN_FAILURE_PREFIX}{now.strftime('%Y%m%dT%H%M%S%f')}-{os.getpid():010d}-{attempt}.json"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
        except BaseException:
            # the file this call made is taken back (a full disk, an I/O error): no truncated evidence under a final name
            try:
                os.unlink(name, dir_fd=dfd)
            except OSError:
                pass
            raise
        _prune_sign_failures(dfd, just_written=name)
        return str(directory / name)
    except Exception as exc:  # noqa: BLE001 - the evidence is a courtesy; the refusal is the point
        return f"evidence not written ({type(exc).__name__}: {getattr(exc, 'strerror', None) or exc})"
    finally:
        if dfd is not None:
            os.close(dfd)


def _prune_sign_failures(dfd: int, *, just_written: str) -> None:
    """Keep the newest :data:`_SIGN_FAILURE_KEEP` evidence files in the directory open as ``dfd``, counting
    ``just_written``, which is never deleted (a clock stepped back can sort it oldest). Only regular files named
    ``sigfail-*.json`` that this uid owns are counted or deleted; a symlink, a directory or another owner's file is left
    alone. A file another sign removed first is skipped: the prune never fails the write it follows."""
    uid = os.getuid()
    ours = []
    for name in os.listdir(dfd):
        if name == just_written or not (name.startswith(_SIGN_FAILURE_PREFIX) and name.endswith(".json")):
            continue
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_uid == uid:
            ours.append(name)
    ours.sort()
    for name in ours[:max(0, len(ours) - (_SIGN_FAILURE_KEEP - 1))]:
        try:
            os.unlink(name, dir_fd=dfd)
        except OSError:
            pass


def ssh_signer(key_path: Path, *, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
               namespace: str = NAMESPACE, timeout: float | None = None) -> tuple[str, Signer]:
    """``(fingerprint, sign)`` for the signing key at ``key_path``: a private key (or an agent-held
    key's public half) that ``ssh-keygen -Y sign -f`` accepts, its public half read from ``<path>.pub``
    (or ``key_path`` itself when it is the ``.pub``). ``sign`` runs ``ssh-keygen -Y sign`` in
    ``namespace`` (default: the authority log's; a consent answer passes its own) with the terminal's
    stderr, so a touch or passphrase prompt reaches the operator.

    The key's sidecar (``levain.cockpit.custody``, written by ``levain cockpit key create``) names its
    provider, which ``sign`` sets as ``SSH_SK_PROVIDER`` itself; an ``SSH_SK_PROVIDER`` in the environment
    still wins. A passphrase key (its sidecar says so, or its file is encrypted) is always signed from the
    file with no agent, so ssh-keygen asks the passphrase for every signature and nothing caches it.

    ``timeout`` (seconds; ``None`` keeps :data:`_SIGN_TIMEOUT`) is ONE deadline for the whole of a ``sign`` call,
    its retry included: ``sign`` below says how it is shared. A caller budgets for it once. It was added so a caller
    that wants a shorter bound for its own signer does not have to change the module's global.

    A security-key answer that does not verify leaves an evidence file (:func:`_write_sign_failure`, under
    ``<key dir>/sign-failures``) and may be asked for again: once more (:data:`_SK_RETRIES`), or after a wait up to
    :data:`_SK_EMPTY_ATTEMPTS` attempts when the answers are empty (``sign`` below says which)."""
    from levain.cockpit.custody import APPLE_PROVIDER, PASSPHRASE, TOUCH_ID, is_encrypted_file, read_sidecar
    key_path = refuse_real_key_in_tests(Path(key_path).expanduser(), "the signing key")   # the judged path, from here on
    public_key = signing_public_key(key_path)
    fp = fingerprint_of(public_key, ssh_keygen=ssh_keygen)
    sidecar = read_sidecar(key_path)
    touch_id = sidecar is not None and sidecar.custody == TOUCH_ID
    no_agent = is_encrypted_file(key_path) or (sidecar is not None and sidecar.custody == PASSPHRASE)

    def sign(message: bytes) -> str:
        """One signature over ``message``. A security-key (sk-*) answer that carries NO signature (an empty one) is asked
        for again after a short wait, up to :data:`_SK_EMPTY_ATTEMPTS` attempts in all (waits :data:`_EMPTY_RETRY_WAITS_S`); a
        signature that is there and does not verify is asked for ONCE more, at once (:data:`_SK_RETRIES`); a third
        attempt is made only while every answer so far was empty (and that third answer may itself fail to verify), so
        any run with a verify failure among its first two answers stops at two. Each retry is a new sign over the same message, so a new touch; that
        retry loop is these few lines and nothing else, and ``_once`` itself never retries. Everything else (a cancel, a
        timeout, a missing presence flag, a plain key's bad signature) is final on the first attempt.

        Every attempt shares ONE deadline, ``timeout`` from now, and so does every ssh-keygen child the step runs: each
        sign, each verify of its answer and each diagnostic check is handed only what is left of it (a sign child less
        :data:`_VERIFY_RESERVE_S`, kept for its verify), and a diagnostic check is not run once nothing is left. No retry
        is asked for once less than :data:`_SK_RETRY_MIN_S` is left. So no child outlives the bound a caller budgeted
        for, however many attempts it took; what runs between children (the evidence file write, local disk I/O with no
        timeout of its own) is the only time not under it. If the retry fails some other way (cancelled, timed out, the
        verifier could not run), its own error, of its own type, is raised with the first attempt's flags and evidence
        named after it."""
        bound = _SIGN_TIMEOUT if timeout is None else timeout
        deadline = time.monotonic() + bound
        first = _once(message, 1, deadline)
        if isinstance(first, str):
            return first
        failed = [first]
        while True:
            n = len(failed)
            last = failed[-1]
            # only an unbroken run of EMPTY answers earns the longer, waited retry; a signature that is there and does not
            # verify keeps its one immediate retry, and a run that mixes the two ends at that one retry too
            cap = _SK_EMPTY_ATTEMPTS if all(f.empty for f in failed) else 1 + _SK_RETRIES
            if n >= cap:
                break
            wait = _EMPTY_RETRY_WAITS_S[n - 1] if last.empty else 0.0
            left = deadline - time.monotonic()
            if left - wait < _SK_RETRY_MIN_S:           # the wait itself must leave a real chance, and never passes the deadline
                ordinal = _ORDINALS[n + 1]
                if all(f.empty for f in failed):
                    named = (f"({last.carried}; {last.evidence})" if n == 1 else
                             f"({n} attempts: " + "; ".join(f"attempt {i} flags {f.flags:#04x} ({f.carried}; {f.evidence})"
                                                            for i, f in enumerate(failed, 1)) + ")")
                    raise KeyError_(f"{_empty_sig_words(any(f.touch for f in failed))} for {fp} {named}, and no {ordinal} "
                                    f"touch was asked for: only {max(left - wait, 0.0):.1f}s of the {bound:g}s bound would remain "
                                    f"after the {wait:g}s wait, under {_SK_RETRY_MIN_S:g}s, so nothing was signed; "
                                    f"{_EMPTY_SIG_NEXT}")
                a = failed[0]
                raise KeyError_(f"ssh-keygen returned a signature that does not verify as {fp} ({a.carried}; {a.detail}; "
                                f"{a.evidence}), and no second touch was asked for: {max(left, 0.0):.1f}s of the "
                                f"{bound:g}s bound was left, under {_SK_RETRY_MIN_S:g}s, so nothing was signed")
            if wait:
                time.sleep(wait)
            # stderr is the terminal's, where the touch prompt already goes; the words say what happened, not that it is safe
            print("the key's answer did not check out; touch again", file=sys.stderr, flush=True)
            try:
                again = _once(message, n + 1, deadline)
            except (KeyError_, _authority.VerifierUnavailable) as exc:
                # the retry's own words first (terminal_sign._classify reads them), then what the earlier attempts left
                # behind; their ssh-keygen verdicts stay in their evidence files, so no verdict text can misfile this one.
                # Re-raised as its own type: a caller that tells a verifier failure from a refusal still can
                if n == 1:
                    a = failed[0]
                    raise type(exc)(f"{exc}; before it, attempt 1's answer did not check out (flags {a.flags:#04x}; "
                                    f"{a.evidence})") from None
                raise type(exc)(f"{exc}; before it, attempts 1 to {n} did not check out ("
                                + "; ".join(f"flags {f.flags:#04x}; {f.evidence}" for f in failed) + ")") from None
            if isinstance(again, str):
                return again
            failed.append(again)
        a, b = failed[0], failed[-1]
        if a.empty and b.empty:
            count = {2: "twice", 3: "three times"}.get(len(failed), f"{len(failed)} times")
            raise KeyError_(f"{_empty_sig_words(any(f.touch for f in failed))} for {fp}, {count} ({_EMPTY_SIG_REASON}): "
                            + "".join(f"attempt {i} flags {f.flags:#04x} ({f.carried}; {f.evidence}); "
                                      for i, f in enumerate(failed, 1))
                            + f"so nothing was signed; {_EMPTY_SIG_NEXT}")
        if any(f.empty for f in failed):    # mixed: EVERY attempt is described by its own number and outcome; the phone guidance stays
            if len(failed) == 2:
                which = 1 if a.empty else 2
                head = (f"both attempts failed (one attempt returned no signature, the other returned a signature that did "
                        f"not verify; attempt {which} returned no signature)")
            else:
                head = (f"all {len(failed)} attempts failed ("
                        + "; ".join(f"attempt {i} returned " + ("no signature" if f.empty else "a signature that did not verify")
                                    for i, f in enumerate(failed, 1)) + ")")
            raise KeyError_(f"ssh-keygen did not return a verifiable signature as {fp}, and {head}: "
                            + "".join(f"attempt {i} flags {f.flags:#04x} "
                                      f"({f.carried}; {_EMPTY_SIG_REASON if f.empty else f.detail}; {f.evidence}); "
                                      for i, f in enumerate(failed, 1))
                            + f"so nothing was signed; {_EMPTY_SIG_NEXT}")
        raise KeyError_(f"ssh-keygen returned a signature that does not verify as {fp}, and both attempts failed "
                        f"(the key's answer did not check out, and touching again did not help): "
                        f"attempt 1 flags {a.flags:#04x} ({a.carried}; {a.detail}; {a.evidence}); "
                        f"attempt 2 flags {b.flags:#04x} ({b.carried}; {b.detail}; {b.evidence}); "
                        "so nothing was signed")

    def _once(message: bytes, attempt: int, deadline: float) -> "str | _SkRefusal":
        """One ``ssh-keygen -Y sign`` and its check, every child bounded by ``deadline`` (``sign``'s one deadline, on
        ``time.monotonic``): the signature, a :class:`_SkRefusal` for an sk signature that presence-checks but does not
        verify (the one retryable failure), or a raised :class:`KeyError_` (or :class:`VerifierUnavailable`, the
        verify could not run, in time or at all)."""
        from levain.launch import child_env

        def left() -> float:
            return deadline - time.monotonic()

        now_left = left()
        wait_s = now_left - min(_VERIFY_RESERVE_S, now_left / 2)

        # the launch allowlist (it carries SSH_AUTH_SOCK, for an agent-held key) and a configured
        # security key provider; stderr stays the terminal's, for the touch or passphrase prompt
        env = child_env("SSH_SK_PROVIDER")
        if "SSH_SK_PROVIDER" not in env and sidecar is not None and sidecar.provider is not None:
            env["SSH_SK_PROVIDER"] = sidecar.provider
        if no_agent:
            env.pop("SSH_AUTH_SOCK", None)
        started = time.monotonic()
        try:
            cp = subprocess.run([str(ssh_keygen), "-Y", "sign", "-f", str(key_path), "-n", namespace],
                                input=message, stdout=subprocess.PIPE,
                                timeout=wait_s, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KeyError_(f"ssh-keygen could not sign ({type(exc).__name__})") from None
        elapsed = time.monotonic() - started       # the sign step alone, never the verify below
        if cp.returncode != 0:
            raise KeyError_(f"ssh-keygen could not sign with {key_path} (exit {cp.returncode})")
        try:
            signature = cp.stdout.decode("ascii")
        except UnicodeDecodeError:
            raise KeyError_("ssh-keygen's signature is not ASCII") from None
        # Checked here, at the moment of signing, so a signature the key did not make is named for what it
        # is. Only an EMPTY result is called a cancelled Touch ID prompt (it lets ssh-keygen exit 0 without a
        # signature, measured 2026-10-12); output that is there and fails the check is named as that, with
        # what it carried, because calling a touched sign "cancelled" is a false record (11-17).
        if not _authority.verify_link_sig(message, signature, public_key=public_key, fingerprint=fp,
                                          namespace=namespace, ssh_keygen=ssh_keygen, timeout=max(left(), 0.0)):
            # a Touch ID key is one its sidecar says is, or one signed through the Secure Enclave provider
            # (a hand-made key enrolled with --custody touch-id has no sidecar)
            is_touch = touch_id or env.get("SSH_SK_PROVIDER") == APPLE_PROVIDER
            if not signature:        # empty only: whitespace is output, not a cancel
                if is_touch:
                    raise KeyError_(f"Touch ID was cancelled or did not complete: no signature was made with {fp} "
                                    f"(ssh-keygen ran {elapsed:.1f}s), so nothing was signed")
                raise KeyError_(f"ssh-keygen returned no valid signature by {fp} ({key_path}), so nothing was signed")
            try:
                sig_type, flags = _authority_sig._sshsig_type_and_flags(signature)
            except Exception:  # noqa: BLE001 - whatever the parser raises, the output is named for what it is
                raise KeyError_(f"ssh-keygen returned output that is not a readable signature for {fp} "
                                f"({len(signature.strip())} chars, ssh-keygen ran {elapsed:.1f}s), so nothing was signed") from None
            carried = (f"{sig_type!r}, flags {'none' if flags is None else format(flags, '#04x')}, "
                       f"{len(signature.strip())} chars, ssh-keygen ran {elapsed:.1f}s")
            # the presence message only for an sk signature: a plain one carries no flags byte to lack
            if flags is not None and _authority.presence_missing(signature, public_key):
                raise KeyError_(f"ssh-keygen returned a signature for {fp} without the user-presence flag ({carried}), "
                                "so nothing was signed")
            detail = _authority_sig.verify_failure_detail(message, signature, public_key=public_key,
                                                          namespace=namespace, ssh_keygen=ssh_keygen, left=left)
            if flags is not None and sig_type.startswith("sk-"):
                try:
                    counter = _authority_sig._sshsig_type_flags_counter(signature)[2]
                except Exception:  # noqa: BLE001 - the counter is evidence only
                    counter = None
                # kept as evidence, then handed to ``sign`` for its one retry; the file's path rides in the error text
                empty = _authority_sig.sk_ecdsa_signature_empty(signature)
                evidence = _write_sign_failure(key_path, fp=fp, message=message, signature=signature, sig_type=sig_type,
                                               flags=flags, counter=counter, detail=detail, namespace=namespace,
                                               attempt=attempt, empty=empty, elapsed=elapsed)
                return _SkRefusal(flags, carried, detail, evidence, empty, is_touch)
            raise KeyError_(f"ssh-keygen returned a signature that does not verify as {fp} ({carried}; {detail}), "
                            "so nothing was signed")
        return signature

    return fp, sign
