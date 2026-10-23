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
import os
import re
import sqlite3
import struct
import subprocess
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from levain import authority as _authority
from levain.authority import (LAPTOP, NAMESPACE, UNSTATED, AnchorError, AuthorityError, AuthorityKey,
                              AuthoritySnapshot, VerifierUnavailable, _pipe_reading, anchor_path, sign_and_append)
from levain.autonomic.confirm import _VERIFY_ENV, DEFAULT_SSH_KEYGEN
from levain.cockpit.authority import RequestAuthority
from levain.cockpit.firejournal import _CLASS_RANK, DB_NAME, UNINITIALISED, CockpitDB, _meta
from levain.cockpit.install import REPORTED, marker_path, marker_present, marker_unreadable

if TYPE_CHECKING:
    from levain.cockpit.verbs import VerbRegistry

__all__ = ["KEY_CLASSES", "InitPlan", "InitResult", "KeyError_", "KeyRow", "LegacyRow", "Signer", "enroll", "keys",
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


def enroll(data_root: Path | str, public_key_text: str, key_class: str, *, signer_fp: str, sign: Signer,
           clock: Callable[[], datetime], custody: str = UNSTATED, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
           marker_directory: Path | None = None) -> tuple[KeyRow, bool, str | None]:
    """Enrol one key, declaring its ``custody``, by a link ``signer_fp`` signs (a laptop-presence key).
    The custody table (``levain.authority.custody_problem``, the replay's own rule) is checked before
    anything is signed; a laptop-presence key must state a real custody. Returns ``(row, created,
    warning)``: ``created`` is False when this exact key is already enrolled, unrevoked, with this class
    and custody (nothing is signed); ``warning`` says the link committed but its anchor did not advance."""
    if key_class not in _CLASS_RANK:
        raise KeyError_(f"unknown key class {key_class!r}; one of {', '.join(KEY_CLASSES)}")
    public_key = normalise_public_key(public_key_text)
    problem = _authority.custody_problem(key_class, custody, public_key.split()[0], public_key=public_key)
    if problem is not None:
        raise KeyError_(problem)
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
        return _row(old), False, None
    new = _append(db, {"op": "enrol", "fingerprint": fp, "public_key": public_key, "class": key_class,
                       "custody": custody}, signer_fp=signer_fp, sign=sign, clock=clock, ssh_keygen=ssh_keygen)
    key = new.key(fp)
    assert key is not None
    return _row(key), True, new.anchor_warning


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


_SIGN_TIMEOUT = 120.0          # a touch-required key waits on a person
_PUBLIC_KEY_MAX = 16 * 1024


def signing_public_key(key_path: Path) -> str:
    """The normal form (``<type> <base64>``) of the signing key's public half: ``<path>.pub``, or
    ``key_path`` itself when it is the ``.pub``."""
    key_path = Path(key_path).expanduser()
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


def ssh_signer(key_path: Path, *, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
               namespace: str = NAMESPACE) -> tuple[str, Signer]:
    """``(fingerprint, sign)`` for the signing key at ``key_path``: a private key (or an agent-held
    key's public half) that ``ssh-keygen -Y sign -f`` accepts, its public half read from ``<path>.pub``
    (or ``key_path`` itself when it is the ``.pub``). ``sign`` runs ``ssh-keygen -Y sign`` in
    ``namespace`` (default: the authority log's; a consent answer passes its own) with the terminal's
    stderr, so a touch or passphrase prompt reaches the operator.

    The key's sidecar (``levain.cockpit.custody``, written by ``levain cockpit key create``) names its
    provider, which ``sign`` sets as ``SSH_SK_PROVIDER`` itself; an ``SSH_SK_PROVIDER`` in the environment
    still wins. A passphrase key (its sidecar says so, or its file is encrypted) is always signed from the
    file with no agent, so ssh-keygen asks the passphrase for every signature and nothing caches it."""
    from levain.cockpit.custody import APPLE_PROVIDER, PASSPHRASE, TOUCH_ID, is_encrypted_file, read_sidecar
    key_path = Path(key_path).expanduser()
    public_key = signing_public_key(key_path)
    fp = fingerprint_of(public_key, ssh_keygen=ssh_keygen)
    sidecar = read_sidecar(key_path)
    touch_id = sidecar is not None and sidecar.custody == TOUCH_ID
    no_agent = is_encrypted_file(key_path) or (sidecar is not None and sidecar.custody == PASSPHRASE)

    def sign(message: bytes) -> str:
        from levain.launch import child_env

        # the launch allowlist (it carries SSH_AUTH_SOCK, for an agent-held key) and a configured
        # security key provider; stderr stays the terminal's, for the touch or passphrase prompt
        env = child_env("SSH_SK_PROVIDER")
        if "SSH_SK_PROVIDER" not in env and sidecar is not None and sidecar.provider is not None:
            env["SSH_SK_PROVIDER"] = sidecar.provider
        if no_agent:
            env.pop("SSH_AUTH_SOCK", None)
        try:
            cp = subprocess.run([str(ssh_keygen), "-Y", "sign", "-f", str(key_path), "-n", namespace],
                                input=message, stdout=subprocess.PIPE, timeout=_SIGN_TIMEOUT, check=False,
                                env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KeyError_(f"ssh-keygen could not sign ({type(exc).__name__})") from None
        if cp.returncode != 0:
            raise KeyError_(f"ssh-keygen could not sign with {key_path} (exit {cp.returncode})")
        try:
            signature = cp.stdout.decode("ascii")
        except UnicodeDecodeError:
            raise KeyError_("ssh-keygen's signature is not ASCII") from None
        # Checked here, at the moment of signing, so a signature the key did not make is named for what it
        # is: a cancelled Touch ID prompt lets ssh-keygen exit 0 without one (measured 2026-10-12). Left to
        # the log's replay it would read as a bad signature, which is a different failure.
        if not _authority.verify_link_sig(message, signature, public_key=public_key, fingerprint=fp,
                                          namespace=namespace, ssh_keygen=ssh_keygen):
            # a Touch ID key is one its sidecar says is, or one signed through the Secure Enclave provider
            # (a hand-made key enrolled with --custody touch-id has no sidecar)
            if touch_id or env.get("SSH_SK_PROVIDER") == APPLE_PROVIDER:
                raise KeyError_(f"Touch ID was cancelled or did not complete: no signature was made with {fp}, "
                                "so nothing was signed")
            raise KeyError_(f"ssh-keygen returned no valid signature by {fp} ({key_path}), so nothing was signed")
        return signature

    return fp, sign
