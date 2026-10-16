"""``levain cockpit pending`` and ``levain cockpit sign``: a cockpit pending signed from the terminal.

The journal is read straight off disk (``levain cockpit keys`` does the same while a server runs); the
decision is never made here. Only the server holding ``<data_root>/cockpit.lock`` can decide and apply,
so ``sign`` is an HTTP client of its ``POST /cockpit/pending/<id>/signature``, over the record's STORED
bytes. The server verifies over its own copy, so a read that raced a change earns ``bad_signature``
(unspent), never a wrong fire."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from levain.authority import anchor_path
from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
from levain.cockpit.authority import RequestAuthority
from levain.cockpit.consent import parse_stamp
from levain.cockpit.firejournal import DB_NAME, CockpitDB, OpView, UnknownPending, _key_problem

DEFAULT_URL = "http://127.0.0.1:7420"
TOKEN_ENV = "LEVAIN_WRITE_TOKEN"
_WRITE_TOKEN_HEADER = "X-Levain-Write-Token"
_POST_TIMEOUT = 30.0
_BODY_MAX = 64 * 1024


class SignRefused(Exception):
    """A refusal ``sign`` or ``pending`` owns, said before (or instead of) anything the server answers."""


def journal(data_root: Path | str) -> CockpitDB:
    """The journal at ``data_root``, opened to read (install class None: it migrates nothing); never
    creates one."""
    root = Path(data_root)
    if not (root / DB_NAME).is_file():
        raise SignRefused(f"no cockpit journal at {root / DB_NAME}: start the cockpit once with "
                          "`levain serve --write --install-class reported`")
    return CockpitDB(root)


def loopback_url(url: str) -> str:
    """``url`` without a trailing slash, refused unless it is plain http to a loopback host: the journal
    is read off local disk, so the server that holds it is local by construction."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parts.scheme != "http" or not loopback or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise SignRefused(f"--url must be http:// to a loopback address (e.g. {DEFAULT_URL}), not {url!r}: "
                          "the pending is read off this machine's disk, so its server is this machine's")
    return url.rstrip("/")


def _no_server(base_url: str, why: object) -> SignRefused:
    return SignRefused(f"no server answers at {base_url} ({why}): a pending is decided only by the server "
                       "holding cockpit.lock, so nothing was decided")


def server_listening(base_url: str, *, timeout: float = 3.0) -> None:
    """Refuse before anything is signed (a touch is not spent on a POST nobody receives) when nothing
    listens at ``base_url``. Only a connect: the POST's own answer still decides."""
    parts = urllib.parse.urlsplit(base_url)
    try:
        socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout).close()
    except OSError as exc:
        raise _no_server(base_url, exc.strerror or exc) from None


def waiting(db: CockpitDB, pending_id: str, now: datetime) -> OpView:
    """The pending, refused unless it is a ``sign`` pending still WAITING with its signed expiry ahead (the
    broker's effective-expiry rule: a stored WAITING row past ``expires_at`` IS expired)."""
    try:
        view = db.load(pending_id)
    except UnknownPending:
        raise SignRefused(f"no pending {pending_id!r} in {db.path}") from None
    if view.state != "WAITING":
        raise SignRefused(f"pending {pending_id} is {view.state}" + (f" ({view.reason})" if view.reason else ""))
    if parse_stamp(view.record["expires_at"]) <= now:
        raise SignRefused(f"pending {pending_id} expired at {view.record['expires_at']}")
    if view.record.get("intent") != "sign":
        raise SignRefused(f"pending {pending_id} is a {view.record.get('intent')!r} pending: it takes no signature")
    return view


def key_refusal(db: CockpitDB, fingerprint: str, tier: str, *, now: datetime,
                ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> tuple[str | None, str | None]:
    """``(refusal, key_class)`` for ``fingerprint`` signing a ``tier`` pending, by the journal's own
    ``_key_problem`` over a captured authority snapshot. Refusal-only, and said before anything is signed:
    a key the decision would refuse ends the pending REFUSED, so the operator would lose it. The broker
    still decides."""
    with db.read() as conn:
        # capture first: it reads the anchor before the transaction's first SELECT pins the snapshot
        auth = RequestAuthority.capture(conn, now=now, anchor=anchor_path(db.data_root), source=str(db.path),
                                        ssh_keygen=ssh_keygen)
        if auth.authority_error is not None:
            return str(auth.authority_error), None
        refusal, _ = _key_problem(conn, auth.authority, fingerprint, tier)
    key = auth.authority.key(fingerprint) if auth.authority is not None else None
    return refusal, (key.key_class if key is not None else None)


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(", ", ": "))


def render(view: OpView, *, signer_fp: str, signer_class: str | None) -> list[str]:
    """The record ``sign`` will sign, rendered from the very bytes (``view.record_bytes``) and nothing
    else: no field elided, nothing from the requester. The caller makes each line terminal-safe."""
    rec = json.loads(view.record_bytes)
    target = rec.get("target") if isinstance(rec.get("target"), dict) else {}
    fence = rec.get("fence") if isinstance(rec.get("fence"), dict) else {}
    lines = [f"signing {view.pending_id}: {rec.get('verb')} ({rec.get('intent')}, {rec.get('tier')})",
             f"  {'target':<15} {_dump(target)}",
             f"  {'params':<15} {_dump(rec.get('params'))}"]
    for i, eff in enumerate(rec.get("effects") or ()):
        eff = eff if isinstance(eff, dict) else {}
        lines.append(f"  {f'effect {i}':<15} {eff.get('store')} {eff.get('resource_key')}")
        lines.append(f"  {'  expect':<15} {_dump(eff.get('expect'))}")
        lines.append(f"  {'  changes':<15} {_dump(eff.get('changes'))}")
    lines += [f"  {'fence':<15} seq {fence.get('authority_seq')} of {fence.get('authority_store_id')} "
              f"(head {fence.get('authority_head_hash')})",
              f"  {'issued':<15} {rec.get('issued_at')}, expires {rec.get('expires_at')}",
              f"  {'requested by':<15} {view.principal}",
              f"  {'record sha256':<15} {hashlib.sha256(view.record_bytes).hexdigest()}",
              f"  {'signer':<15} {signer_fp} ({signer_class})"]
    return lines


@dataclass(frozen=True)
class Answer:
    status: int
    body: dict[str, Any] | None
    raw: str


def post_signature(base_url: str, pending_id: str, signature: str, token: str,
                   *, timeout: float = _POST_TIMEOUT) -> Answer:
    """``POST <base_url>/cockpit/pending/<id>/signature`` with exactly ``{"signature": ...}``. A 4xx/5xx is
    an :class:`Answer`, not an exception; no server answering is a :class:`SignRefused`."""
    url = f"{base_url}/cockpit/pending/{urllib.parse.quote(pending_id, safe='')}/signature"
    req = urllib.request.Request(url, data=json.dumps({"signature": signature}).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", _WRITE_TOKEN_HEADER: token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — loopback_url checked the scheme
            status, raw = resp.status, resp.read(_BODY_MAX)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(_BODY_MAX)
    except (urllib.error.URLError, OSError) as exc:
        raise _no_server(base_url, getattr(exc, "reason", exc)) from None
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    return Answer(status, body if isinstance(body, dict) else None, text)
