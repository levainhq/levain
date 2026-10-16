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
import os
import shutil
import socket
import subprocess
import sys
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
from levain.launch import child_env

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
    listens at ``base_url``, or when what listens there is not this OS user's (:func:`own_listener`): the token
    and the signature go only to a listener this user owns. A process of this same user can still stand there;
    the POST's answer and the journal read again after it decide the exit code."""
    parts = urllib.parse.urlsplit(base_url)
    port = 80 if parts.port is None else parts.port
    try:
        socket.create_connection((parts.hostname, port), timeout=timeout).close()
    except (OSError, ValueError) as exc:
        raise _no_server(base_url, getattr(exc, "strerror", None) or exc) from None
    if not own_listener(parts.hostname or "", port):
        raise SignRefused(f"something listens at {base_url}, but no listener this OS user owns is bound there "
                          "(another user's process, or one this check cannot see): the write token and the signature "
                          "are not sent, and nothing was decided")


def _proc_listeners(port: int) -> list[tuple[str, int]]:
    """Linux: (hex local address, uid) of each TCP socket in LISTEN on ``port``, from /proc/net/tcp and tcp6."""
    out: list[tuple[str, int]] = []
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(name, encoding="ascii") as fh:
                next(fh, None)
                for line in fh:
                    f = line.split()
                    if len(f) > 7 and f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
                        out.append((f[1].rsplit(":", 1)[0], int(f[7])))
        except (OSError, ValueError, IndexError):
            continue
    return out


_PROC_HOSTS = {"127.0.0.1": "0100007F", "::1": "00000000000000000000000001000000"}
_PROC_WILDCARD = {"00000000", "00000000000000000000000000000000"}


def own_listener(host: str, port: int) -> bool:
    """Is the TCP listener at ``host:port`` this OS user's? Linux: /proc/net/tcp names each listener's uid, and a
    wildcard listener of this user also counts (Linux refuses another bind of that port while it listens). macOS:
    lsof run as this user lists only this user's sockets, and only a listener bound to exactly ``host`` counts.
    Any other platform, or a reader that fails: False (refuse rather than hand the token to a stranger)."""
    hosts = ("127.0.0.1", "::1") if host == "localhost" else (host,)
    uid = os.getuid()
    if sys.platform.startswith("linux"):
        found = _proc_listeners(port)
        wanted = {_PROC_HOSTS[h] for h in hosts if h in _PROC_HOSTS}
        if any(addr in wanted and owner != uid for addr, owner in found):
            return False
        return any(owner == uid and (addr in wanted or addr in _PROC_WILDCARD) for addr, owner in found)
    if sys.platform == "darwin":
        lsof = "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else (shutil.which("lsof") or "/usr/sbin/lsof")
        try:
            cp = subprocess.run([lsof, "-nP", "-a", "-u", str(uid), f"-iTCP:{port}", "-sTCP:LISTEN", "-Fn"],
                                capture_output=True, text=True, timeout=5, check=False, env=child_env())
        except (OSError, subprocess.SubprocessError):
            return False
        names = {ln[1:] for ln in cp.stdout.splitlines() if ln.startswith("n")}
        return any((f"[{h}]:{port}" if ":" in h else f"{h}:{port}") in names for h in hosts)
    return False


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
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
            raise _no_server(base_url, reason) from None
        # a timeout or a reset can come after the request was sent: the server may have decided it
        raise SignRefused(f"the request to {base_url} got no answer ({reason}): the server may have applied it; "
                          "`levain cockpit pending` shows what the journal says now") from None
    except ValueError as exc:      # (UnicodeEncodeError too) a header or URL http.client will not send
        raise SignRefused(f"the request to {base_url} could not be sent ({exc}): nothing was decided") from None
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    return Answer(status, body if isinstance(body, dict) else None, text)
