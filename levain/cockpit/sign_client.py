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
    try:
        port = 80 if parts.port is None else parts.port
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise SignRefused(f"--url {url!r} has no usable port (1-65535)")
    return url.rstrip("/")


def header_safe_token(token: str) -> str:
    """``token`` stripped, refused unless it can travel as one header value (printable ASCII, nothing
    blank inside): said before anything is signed, so no touch is spent on a request that cannot be sent."""
    token = token.strip()
    if not token or not all("!" <= c <= "~" for c in token):
        raise SignRefused("the write token must be printable ASCII with no spaces or line breaks inside: "
                          "type the one `levain serve --write` printed")
    return token


def _no_server(base_url: str, why: object) -> SignRefused:
    return SignRefused(f"no server answers at {base_url} ({why}): a pending is decided only by the server "
                       "holding cockpit.lock, so nothing was decided")


def server_listening(base_url: str, *, timeout: float = 3.0) -> None:
    """Refuse before anything is signed (a touch is not spent on a POST nobody receives) when nothing
    listens at ``base_url``. Only a connect: it proves nothing about who listens; the POST's answer and the
    journal read again after it decide the exit code."""
    parts = urllib.parse.urlsplit(base_url)
    try:
        socket.create_connection((parts.hostname, 80 if parts.port is None else parts.port), timeout=timeout).close()
    except (OSError, ValueError) as exc:
        raise _no_server(base_url, getattr(exc, "strerror", None) or exc) from None


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
    """The record ``sign`` will sign, rendered from the very bytes (``view.record_bytes``): every key of
    the decoded record, in its stored order, nothing dropped (a list of objects, like ``effects``, one key
    per line). What the journal keeps beside the record follows under its own heading, because it is not
    signed. The caller makes each line terminal-safe."""
    rec = json.loads(view.record_bytes)
    rec = rec if isinstance(rec, dict) else {"record": rec}
    lines = [f"signing {view.pending_id}: {rec.get('verb')} ({rec.get('intent')}, {rec.get('tier')})",
             "  signed record (the bytes signed):"]
    for key, value in rec.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            for i, item in enumerate(value):
                lines.append(f"    {key} {i}")
                lines += [f"      {sub:<15} {_dump(v)}" for sub, v in item.items()]
        else:
            lines.append(f"    {key:<17} {_dump(value)}")
    lines += [f"    {'(sha256)':<17} {hashlib.sha256(view.record_bytes).hexdigest()}",
              "  from the journal (not part of the signed bytes):",
              f"    {'requested by':<17} {view.principal}",
              f"  {'signer':<19} {signer_fp} ({signer_class})"]
    return lines


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow no redirect: the 3xx itself comes back as the answer (an ``HTTPError``), so the token and
    the signature go to ``--url`` and nowhere else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 — stdlib signature
        return None


# no proxy (HTTP_PROXY and the system's settings would carry the token off the machine) and no redirect
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


@dataclass(frozen=True)
class Answer:
    status: int
    body: dict[str, Any] | None
    raw: str


def post_signature(base_url: str, pending_id: str, signature: str, token: str,
                   *, timeout: float = _POST_TIMEOUT) -> Answer:
    """``POST <base_url>/cockpit/pending/<id>/signature`` with exactly ``{"signature": ...}``. A 4xx/5xx is
    an :class:`Answer`, not an exception, and so is a 3xx (never followed); no proxy is used. No server
    answering, or a request that cannot be sent, is a :class:`SignRefused`."""
    url = f"{base_url}/cockpit/pending/{urllib.parse.quote(pending_id, safe='')}/signature"
    req = urllib.request.Request(url, data=json.dumps({"signature": signature}).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", _WRITE_TOKEN_HEADER: token})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # loopback_url checked the scheme and host
            status, raw = resp.status, resp.read(_BODY_MAX)
    except urllib.error.HTTPError as exc:
        with exc:
            status, raw = exc.code, exc.read(_BODY_MAX)
    except (urllib.error.URLError, OSError) as exc:
        raise _no_server(base_url, getattr(exc, "reason", exc)) from None
    except ValueError as exc:      # (UnicodeEncodeError too) a header or URL http.client will not send
        raise SignRefused(f"the request to {base_url} could not be sent ({exc}): nothing was decided") from None
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    return Answer(status, body if isinstance(body, dict) else None, text)
