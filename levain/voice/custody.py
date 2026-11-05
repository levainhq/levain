"""Voice key custody (voice V1-1, design 1104-voice/V1_DESIGN.md §3.1 to §3.4).

A premium voice provider's API key lives in the host's login keychain as a generic password. What the authority log
holds is a signed ``voice-key`` record (:data:`levain.authority.VOICE_KEY_KIND`) that NAMES the item (its service and
account) and the uses allowed, never the value. This module is the one place a value is written, read or judged:

* :func:`read_key` is the ONLY reader. It asks a fresh verified replay (an :class:`AuthoritySource`, never a snapshot
  a caller kept) whether the provider has a voice key RIGHT NOW, then reads the item the record names, once per call.
  Nothing is cached, so a revoke stops the very next read, and a record no longer in the policy reads nothing.
* :func:`add_key` signs the record (a create or one replacing ``loosen``, which the log ranks as laptop-presence) and
  writes the item through ONE function, :class:`SecurityStdinWriter`, that hands the value to ``security -i`` on
  stdin so it is in no argv. The writer is a seam (:class:`KeychainWriter`): the measurement that ``security -i``
  really keeps the value out of ``ps`` was not made in this slice, and a different writer replaces this one without
  touching the rest.
* :func:`revoke_key` and :func:`list_keys` are the signer-side counterparts. A revoke withdraws the AUTHORITY to read;
  the keychain item stays until its owner deletes it (a revoke can be signed from a phone, which holds no keychain).

The value never appears in an argv, an environment, a record, a log line, a repr or an exception message: it travels
as a :class:`Secret` whose repr is fixed text, every exception here carries fixed words, and the subprocess
environment is ``PATH`` alone. No real ``security`` binary is reachable under a test run (:func:`checked_security`).
"""
from __future__ import annotations

import hmac
import re
import secrets
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from levain.authority import (VOICE_KEY_KIND, VOICE_KEY_PROVIDERS, VOICE_KEY_USES, AuthorityError, _thaw, anchor_path,
                              append, prepare, voice_key_for, voice_key_record)
from levain.authority_sig import VerifierUnavailable
from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
from levain.chat_egress.key_setup import KeychainItems, SetupDeclined, SetupRefused
from levain.chat_egress.keys import CredentialUnreadable, KeychainStore
from levain.cockpit.firejournal import DB_NAME
from levain.cockpit.keys import (KeyError_, Signer, _append, _capture, _journal, _now, _signing_authority,
                                 under_test_run)
from levain.consent.queue import AuthoritySource, authority_source

__all__ = ["DEFAULT_ITEM_ACCOUNT", "DEFAULT_SECURITY", "KeychainWriteFailed", "KeychainWriter", "Secret",
           "SecurityStdinWriter", "VoiceKeyPreview", "VoiceKeyRefused", "VoiceKeyUnavailable", "add_key",
           "authority_for", "checked_security", "default_source", "list_keys", "read_key", "revoke_key"]

_REAL_SECURITY = "/usr/bin/security"
#: the binary used unless a caller hands in another; a test points this (or the call) at a stand-in
DEFAULT_SECURITY = _REAL_SECURITY
#: the keychain item account `levain voice key add` names unless told otherwise; the service is per provider
DEFAULT_ITEM_ACCOUNT = "levain-voice"

# What a provider key may be made of. `security -i` parses its stdin like a shell, so a value is written only when it
# needs no quoting: letters, digits and `. _ ~ + / = -` (the alphabets of OpenAI, ElevenLabs and Deepgram keys). Any
# other value is refused whole, never escaped.
_VALUE = re.compile(r"[A-Za-z0-9._~+/=-]{8,512}")
# A keychain name the writer will put on a `security -i` line unquoted. The record accepts wider text; the writer
# refuses what it cannot write without quoting.
_WRITABLE_NAME = re.compile(r"[A-Za-z0-9._@-]{1,128}")
_ENV = {"PATH": "/usr/bin:/bin"}


class VoiceKeyRefused(SetupRefused):
    """A key act custody refuses before anything is signed or written."""


class VoiceKeyUnavailable(Exception):
    """:func:`read_key` gave no value. ``code`` is one of ``not_set`` (no live voice key for the provider),
    ``use_not_granted`` (the record does not allow this use), ``authority_unreadable`` (the log could not be
    verified now), ``keychain_unreadable`` (the item could not be read) or ``unusable`` (the item holds a value no
    provider key looks like). The message is fixed words: no part of a value, a record or a store's reply."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class KeychainWriteFailed(KeyError_):
    """The keychain did not take the value. Fixed words, never the store's reply."""


class Secret:
    """A key value in transit. Its repr, str and pickle are closed, so a log line, a traceback's locals dump or an
    f-string cannot carry it; the one way out is :meth:`reveal`, called by a writer and a comparison only."""
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("a Secret does not change")

    def reveal(self) -> str:
        return self._value

    def well_formed(self) -> bool:
        return isinstance(self._value, str) and _VALUE.fullmatch(self._value) is not None

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("a Secret is never pickled")


def checked_security(path: str | Path | None = None) -> str:
    """The ``security`` binary to run. Under a test run the real one (``/usr/bin/security``, however spelled or
    linked) is refused, so no test can reach a person's keychain by forgetting to hand in a fake; a test passes the
    path of its own stand-in. Every default construction in this module goes through here, at call time."""
    chosen = DEFAULT_SECURITY if path is None else str(path)
    if under_test_run():
        try:
            real = Path(_REAL_SECURITY).resolve()
            same = Path(chosen).expanduser().resolve() == real
        except (OSError, RuntimeError):
            same = True                     # cannot tell: under a test run that is a refusal
        if same:
            raise VoiceKeyRefused("a test reached the real `security` binary: hand in a stand-in")
    return chosen


class KeychainWriter:
    """How a value gets into the keychain: the seam V1-1 leaves replaceable. ``write`` stores ``value`` as the
    generic password ``source`` (service) / ``account`` (updating the item if there is one), or raises
    :class:`KeychainWriteFailed`. An implementation takes the value as a :class:`Secret`, never writes it into an
    argv or an environment, and never echoes a reply of the store."""

    def write(self, source: str, account: str, value: Secret) -> None:    # pragma: no cover - the protocol
        raise NotImplementedError


class SecurityStdinWriter(KeychainWriter):
    """``security -i`` with the ``add-generic-password`` line fed on stdin: the value is in the pipe, not in ``ps``.
    UNMEASURED against the real binary in this slice (a scratch keychain was out of bounds): that ``-i`` reads the
    line this way and that its argv stays value-free are the design's inference, so the write is read back and
    compared before anything is signed (:func:`add_key`), and the tests run it over a stand-in. The reply of
    ``security`` is discarded: it is never shown, returned or logged."""

    def __init__(self, security: str | Path | None = None, timeout_s: float = 10.0) -> None:
        self._security = checked_security(security)
        self._timeout = timeout_s

    def write(self, source: str, account: str, value: Secret) -> None:
        if not (_WRITABLE_NAME.fullmatch(source) and _WRITABLE_NAME.fullmatch(account)):
            raise KeychainWriteFailed("the keychain item's names hold characters this writer cannot place "
                                      "(letters, digits and . _ @ - only)")
        if not value.well_formed():
            raise KeychainWriteFailed("the key holds characters this writer cannot place (letters, digits and "
                                      "._~+/=- only, 8 to 512 of them)")
        line = f"add-generic-password -U -s {source} -a {account} -w {value.reveal()}\nexit\n"
        try:
            subprocess.run([self._security, "-i"], input=line.encode("ascii"), capture_output=True,
                           timeout=self._timeout, check=True, env=_ENV)
        except (OSError, subprocess.SubprocessError):
            raise KeychainWriteFailed("the keychain did not take the key") from None
        finally:
            del line


# --- the reader --------------------------------------------------------------------------------------------------

def authority_for(data_root: Path | str) -> AuthoritySource:
    """The fresh-replay source ``read_key`` takes, over the cockpit journal at ``data_root``."""
    root = Path(data_root)
    return authority_source(root / DB_NAME, anchor=anchor_path(root))


def default_source(provider: str) -> str:
    """The keychain service ``levain voice key add`` names for ``provider`` unless told otherwise."""
    return f"levain-voice-{provider}"


def read_key(provider: str, use: str, *, authority: AuthoritySource, store: Any = None) -> str:
    """The value of ``provider``'s voice key, for ``use``, or :class:`VoiceKeyUnavailable`. THE only reader.

    Each call replays the authority log afresh (``authority`` is an :class:`AuthoritySource`, which reads and
    verifies on every call; a callable that returns a kept snapshot is a TypeError, not a freshness a caller could
    break), finds the provider's one voice key, checks ``use`` against its ``uses`` and only then reads the item it
    names from ``store`` (default: the login keychain through :class:`KeychainStore`). Nothing is held between
    calls, so a revoke stops the next read and a record that is gone reads nothing."""
    if not isinstance(authority, AuthoritySource):
        raise TypeError(f"read_key takes an AuthoritySource, not {type(authority).__name__}")
    try:
        state = authority()
    except Exception:                       # noqa: BLE001 - any failure is "the log could not be verified now"
        raise VoiceKeyUnavailable("authority_unreadable", "the authority log could not be verified") from None
    record = voice_key_for(state.policy.grants, provider)
    if record is None:
        raise VoiceKeyUnavailable("not_set", f"no voice key is set for {provider}")
    if use not in record["uses"]:
        raise VoiceKeyUnavailable("use_not_granted", f"the {provider} key is not set for {use}")
    keychain = KeychainStore(checked_security()) if store is None else store
    try:
        value = keychain.read(record["source"], record["account"])
    except Exception:                       # noqa: BLE001 - a store's own words may carry anything: fixed words here
        raise VoiceKeyUnavailable("keychain_unreadable", "the key could not be read from the keychain") from None
    if not isinstance(value, str) or not _VALUE.fullmatch(value):
        raise VoiceKeyUnavailable("unusable", "the keychain item does not hold a usable key")
    return value


# --- the signer side ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class VoiceKeyPreview:
    """What ``confirm`` is shown: the record exactly as it will be signed, and the record a replace supersedes."""
    record: dict[str, Any]
    replaces: tuple[str, ...]


def _check_names(provider: str, uses: Iterable[str]) -> list[str]:
    if provider not in VOICE_KEY_PROVIDERS:
        raise VoiceKeyRefused(f"{str(provider)[:40]!r} is not a voice provider ({', '.join(VOICE_KEY_PROVIDERS)})")
    chosen = sorted(set(uses))
    if not chosen or any(u not in VOICE_KEY_USES for u in chosen):
        raise VoiceKeyRefused(f"uses are drawn from {', '.join(VOICE_KEY_USES)}")
    return chosen


def add_key(data_root: Path | str, *, provider: str, uses: Iterable[str], source: str, account: str, value: Secret,
            signer_fp: str, sign: Signer, clock: Callable[[], datetime], confirm: Callable[[VoiceKeyPreview], bool],
            writer: KeychainWriter | None = None, items: KeychainItems | None = None, store: Any = None,
            replace: bool = False, record_id: str | None = None, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
            marker_directory: Path | None = None) -> tuple[dict[str, Any], str | None]:
    """Store ``value`` as keychain item ``source``/``account`` and sign the voice key record naming it.

    Order, so nothing is left half-done in the dangerous direction: the record is judged by the log's rules
    (prepared, unsigned), ``confirm(preview)`` is asked (False: nothing written or signed, :class:`SetupDeclined`),
    the item is written and READ BACK equal, and only then is the link signed and appended. A failed sign after the
    write leaves an item no record names, which nothing reads. ``replace`` supersedes the provider's live key in the
    same ``loosen`` (a new id; the old record goes). Returns ``(record, warning)``."""
    chosen = _check_names(provider, uses)
    if not isinstance(value, Secret) or not value.well_formed():
        raise VoiceKeyRefused("the key is not shaped like a provider key (letters, digits and ._~+/=- only, 8 to 512)")
    now = _now(clock)
    rid = record_id if record_id is not None else "vk-" + secrets.token_hex(6)
    record = {"id": rid, "kind": VOICE_KEY_KIND, "provider": provider, "uses": chosen, "source": source,
              "account": account}
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, now, ssh_keygen))
    if rid in state.policy.grants:
        raise VoiceKeyRefused(f"a grant {rid!r} already exists")
    live = voice_key_for(state.policy.grants, provider)
    if live is not None and not replace:
        raise VoiceKeyRefused(f"{provider} already has a voice key ({live['id']}): pass --replace to supersede it, "
                              "or revoke it first")
    if live is None and replace:
        raise VoiceKeyRefused(f"nothing to replace: {provider} has no voice key")
    # attributes only (no value is read): how many keychains hold an item of this service and account
    seen = (items if items is not None else KeychainItems(checked_security())).count(source, account)
    if seen > 1:
        raise VoiceKeyRefused(f"{seen} keychain items have service {source[:80]!r} and account {account[:80]!r}: "
                              "which one a read gets is the search list's order; leave one")
    if live is not None:
        policy = _thaw(state.policy.as_dict())
        del policy["grants"][live["id"]]
        policy["grants"][rid] = record
        op: dict[str, Any] = {"op": "loosen", "policy": policy}
    else:
        op = {"op": "create_grant", "grant": record}
    preview = VoiceKeyPreview(_thaw(record), () if live is None else (live["id"],))
    out_writer = writer if writer is not None else SecurityStdinWriter()
    try:
        with db.autocommit() as conn:
            link = prepare(conn, op, signer_fp=signer_fp, now=_now(clock), ssh_keygen=ssh_keygen)
            if live is not None and (link.seq, link.prev_hash) != (state.seq + 1, state.head_hash):
                raise VoiceKeyRefused(f"the authority log moved (seq {state.seq} -> {link.seq - 1}) since this "
                                      "replace was built: run it again")
            if not confirm(preview):
                raise SetupDeclined("not confirmed: nothing was written or signed")
            out_writer.write(source, account, value)
            keychain = KeychainStore(checked_security()) if store is None else store
            try:
                held = keychain.read(source, account)
            except Exception:           # noqa: BLE001 - fixed words, whatever the store said
                held = None
            if not isinstance(held, str) or not hmac.compare_digest(held.encode(), value.reveal().encode()):
                raise KeychainWriteFailed("the keychain does not hold the key just written: nothing was signed")
            signature = sign(link.message)
            new = append(conn, link, signature, now=_now(clock), ssh_keygen=ssh_keygen,
                         anchor=anchor_path(db.data_root))
    except VerifierUnavailable:
        raise
    except AuthorityError as exc:
        raise VoiceKeyRefused(str(exc)) from None
    return _thaw(new.policy.grants[rid]), new.anchor_warning


def revoke_key(data_root: Path | str, record_id: str, *, signer_fp: str, sign: Signer,
               clock: Callable[[], datetime], ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
               marker_directory: Path | None = None) -> tuple[int, str | None]:
    """Sign a ``revoke_grant`` link removing voice key ``record_id`` (any enrolled key: a revoke only narrows). The
    next :func:`read_key` finds no key. The keychain item is left where it is. Returns ``(seq, warning)``."""
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, _now(clock), ssh_keygen))
    if not voice_key_record(state.policy.grants.get(record_id)):
        raise VoiceKeyRefused(f"no voice key {record_id[:80]!r}")
    try:
        new = _append(db, {"op": "revoke_grant", "grant_id": record_id}, signer_fp=signer_fp, sign=sign,
                      clock=clock, ssh_keygen=ssh_keygen)
    except KeyError_ as exc:
        raise VoiceKeyRefused(str(exc)) from None
    return new.seq, new.anchor_warning


def list_keys(data_root: Path | str, *, now: datetime, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
              marker_directory: Path | None = None) -> list[dict[str, Any]]:
    """Every voice key record the replay holds, by id (names only: there is no value to show)."""
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, now, ssh_keygen))
    return [{**_thaw(g), "link_seq": state.grant_seqs.get(gid)}
            for gid, g in sorted(state.policy.grants.items()) if voice_key_record(g)]


# the CLI's refusals
_REFUSALS = (VoiceKeyRefused, SetupRefused, KeyError_, AuthorityError, CredentialUnreadable)
