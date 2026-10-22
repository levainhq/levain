"""keys' credentials (S1 session 3b, design 1.8): the credential file, its per-exchange observation, the host binding,
the injected header, and the credential-lifecycle seam keys' shutdown calls.

A credential file `L/keys/cred/<name>.json` is OBSERVED on every exchange that needs it: opened `O_NOFOLLOW |
O_NONBLOCK` under the credential directory's fd, refused unless it is a regular file, read to at most FILE_CAP, its
`rev` extracted and validated. Bytes that do not parse, whatever the decode or a check raises on them, refuse the
exchange `credential-unavailable` and evict the cached parse. The cached parse is used when
its `rev` equals the file's, else the file is parsed and the cache replaced; ENOENT evicts at once, so a removed
credential stops working on the next exchange. `rev` is compared for equality only, never ordered.

The refresh (kind `oauth2-refresh`) and the one credential writer are not built at this tip: an upstream 401 reaches
the credential layer through `on_upstream_401`, a no-op for the static kind.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import stat
from typing import Dict, List, Optional, Protocol, Set

from levain.broker.norm import Refused
from levain.broker.upstream import _note

FORMAT = "levain-keys-credential/1"
FILE_CAP = 64 * 1024
VALUE_CAP = 8 * 1024
MAX_HOSTS = 8
NAME_RE = re.compile(r"[a-z0-9-]{1,48}")   # every use is a fullmatch: a `$` would admit a trailing newline
REV_RE = re.compile(r"[0-9a-f]{32}")
_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
INJECT_HEADERS = ("x-api-key", "authorization")
INJECT_SCHEMES = ("", "Bearer")
_COMMON = {"format", "name", "rev", "hosts", "inject", "kind"}
_KEYS = {"static": _COMMON | {"secret"}, "oauth2-refresh": _COMMON | {"access", "expires_at", "refresh"}}
_REFRESH_KEYS = {"token_url", "client_id", "refresh_token"}


# --- the credential-lifecycle seam -----------------------------------------------------------------------------------

class CredLifecycle(Protocol):
    """What keys' shutdown calls on the credential layer, and nothing else. `begin_closing()` at the shutdown latch (a
    refresh wanted after it is refused by whoever owns the refresh); `cancel_detached()` at shutdown's step (3), which
    cancels the credential layer's detached tasks. The refresh-wanted entry point is NOT on this seam."""

    def begin_closing(self) -> None: ...

    def cancel_detached(self) -> None: ...


class FakeCredLifecycle:
    """The stand-in keys is built with until the real credential writer exists: it implements the seam and holds a
    detached set. `refresh_wanted()` is a test hook of this stand-in only, never part of the seam."""

    def __init__(self) -> None:
        self.closing = False
        self.detached: Set = set()
        self.calls: List[str] = []

    def begin_closing(self) -> None:
        self.calls.append("begin_closing")
        self.closing = True

    def cancel_detached(self) -> None:
        self.calls.append("cancel_detached")
        for t in list(self.detached):
            t.cancel()

    def refresh_wanted(self) -> bool:
        """TEST HOOK: records the want; refused (False) once closing."""
        self.calls.append("refresh_wanted")
        return not self.closing


# --- the credential --------------------------------------------------------------------------------------------------

def is_a_label(host: object) -> bool:
    """A lowercase IDNA A-label host name (ASCII letters, digits and hyphens, dot-separated)."""
    if not isinstance(host, str) or not host or len(host) > 253 or host != host.lower():
        return False
    return all(_LABEL_RE.fullmatch(lab) for lab in host.split("."))


class Credential:
    __slots__ = ("name", "rev", "hosts", "header", "scheme", "kind", "secret")

    def __init__(self, name: str, rev: str, hosts: frozenset, header: str, scheme: str, kind: str,
                 secret: str) -> None:
        self.name, self.rev, self.hosts = name, rev, hosts
        self.header, self.scheme, self.kind, self.secret = header, scheme, kind, secret

    @property
    def generation(self) -> str:
        return self.rev

    def __repr__(self) -> str:   # never a byte of the secret
        return f"Credential(name={self.name!r}, rev={self.rev!r}, kind={self.kind!r})"

    def header_pair(self) -> tuple:
        """The ONE injected header: name and scheme from the file."""
        value = f"{self.scheme} {self.secret}" if self.scheme else self.secret
        return (self.header.encode("ascii"), value.encode("latin-1"))


class Unparseable(ValueError):
    pass


def _value(v: object, what: str) -> str:
    if not isinstance(v, str) or not v or len(v.encode("utf-8")) > VALUE_CAP:
        raise Unparseable(what)
    return v


#: what a file's bytes can raise out of the decode and the checks besides `Unparseable`: nesting past the recursion
#: limit, a value of the wrong type meeting an operation (an unhashable `kind`), a lone surrogate meeting an encode.
_INPUT_ERRORS = (ValueError, RecursionError, TypeError, UnicodeError)


def parse(raw: bytes, expect_name: str) -> Credential:
    """Validate one credential file's bytes; raise `Unparseable` naming the field (never echoing a value). This is the
    parse boundary: anything the bytes can make the decode or a check raise leaves as `Unparseable`."""
    try:
        return _parse(raw, expect_name)
    except Unparseable:
        raise
    except _INPUT_ERRORS as e:
        raise Unparseable(type(e).__name__) from None


def _parse(raw: bytes, expect_name: str) -> Credential:
    try:
        d = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup)
    except (UnicodeDecodeError, ValueError) as e:
        raise Unparseable("json") from e
    if not isinstance(d, dict) or d.get("format") != FORMAT:
        raise Unparseable("format")
    kind = d.get("kind")
    if not isinstance(kind, str) or kind not in _KEYS:
        raise Unparseable("kind")
    if set(d) != _KEYS[kind]:
        raise Unparseable("keys")
    if d["name"] != expect_name or not isinstance(d["name"], str) or not NAME_RE.fullmatch(d["name"]):
        raise Unparseable("name")
    if not isinstance(d["rev"], str) or not REV_RE.fullmatch(d["rev"]):
        raise Unparseable("rev")
    hosts = d["hosts"]
    if (not isinstance(hosts, list) or not 1 <= len(hosts) <= MAX_HOSTS or not all(is_a_label(h) for h in hosts)
            or len(set(hosts)) != len(hosts)):
        raise Unparseable("hosts")
    inj = d["inject"]
    if (not isinstance(inj, dict) or set(inj) != {"header", "scheme"} or inj["header"] not in INJECT_HEADERS
            or inj["scheme"] not in INJECT_SCHEMES):
        raise Unparseable("inject")
    if kind == "static":
        secret = _value(d["secret"], "secret")
    else:
        secret = _value(d["access"], "access")
        if not isinstance(d["expires_at"], int) or isinstance(d["expires_at"], bool):
            raise Unparseable("expires_at")
        r = d["refresh"]
        if not isinstance(r, dict) or set(r) != _REFRESH_KEYS:
            raise Unparseable("refresh")
        for k in _REFRESH_KEYS:
            _value(r[k], "refresh." + k)
        url = r["token_url"]
        if not url.startswith("https://"):
            raise Unparseable("refresh.token_url")
        host = url[len("https://"):].split("/", 1)[0]
        if host not in hosts:
            raise Unparseable("refresh.token_url host")
    if not all(0x20 <= ord(c) < 0x7f for c in secret):   # printable ASCII: the value goes into a header
        raise Unparseable("secret charset")
    return Credential(d["name"], d["rev"], frozenset(hosts), inj["header"], inj["scheme"], kind, secret)


def _no_dup(pairs: list) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _rev_of(raw: bytes) -> Optional[str]:
    """The file's `rev`, read with the full parse's JSON decoder (cheap at this size); None if absent or invalid."""
    try:
        d = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup)
    except _INPUT_ERRORS:
        return None
    rev = d.get("rev") if isinstance(d, dict) else None
    return rev if isinstance(rev, str) and REV_RE.fullmatch(rev) else None


class CredStore:
    """Credentials observed under one directory fd. `observe(name)` is called once per exchange that needs `name`."""

    def __init__(self, dir_fd: int) -> None:
        self.dir_fd = dir_fd
        self.cache: Dict[str, Credential] = {}

    def _read(self, name: str) -> Optional[bytes]:
        # O_NONBLOCK: opening a FIFO with no writer would otherwise block keys' one loop; the fstat then refuses
        # anything but a regular file before a byte is read (O_NONBLOCK changes nothing for a regular file's read).
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(name + ".json", flags, dir_fd=self.dir_fd)
        except OSError as e:
            if e.errno == errno.ENOENT:
                return None
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Unparseable("not a regular file")
            chunks, got = [], 0
            while got <= FILE_CAP:
                b = os.read(fd, FILE_CAP + 1 - got)
                if not b:
                    break
                chunks.append(b)
                got += len(b)
        finally:
            os.close(fd)
        raw = b"".join(chunks)
        if len(raw) > FILE_CAP:
            raise Unparseable("size")
        return raw

    def observe(self, name: str) -> Credential:
        """The credential `name` as the file holds it NOW, or `Refused("credential-unavailable")`."""
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise Refused("credential-unavailable")
        try:
            raw = self._read(name)
        except (OSError, Unparseable) as e:
            self.cache.pop(name, None)
            _note(logging.WARNING, "levain gw-keys: credential %s unreadable (%s)", name, type(e).__name__)
            raise Refused("credential-unavailable") from None
        if raw is None:
            self.cache.pop(name, None)   # ENOENT evicts at once
            raise Refused("credential-unavailable")
        rev = _rev_of(raw)
        cur = self.cache.get(name)
        if cur is not None and rev is not None and cur.rev == rev:
            return cur
        try:
            cred = parse(raw, name)
        except Unparseable as e:
            self.cache.pop(name, None)
            _note(logging.WARNING, "levain gw-keys: credential %s unparseable (%s)", name, e)
            raise Refused("credential-unavailable") from None
        self.cache[name] = cred
        return cred


def check_host(cred: Credential, host: str, route: str) -> None:
    """The host binding, every exchange, after the credential is loaded and before the upstream opens."""
    if host not in cred.hosts:
        _note(logging.WARNING, "levain gw-keys: credential-host-mismatch route=%s credential=%s host=%s",
              route, cred.name, host)
        raise Refused("credential-host-mismatch")


def on_upstream_401(credential: Credential, generation: str) -> None:
    """An upstream 401 on an exchange that sent `credential` at `generation`. The static kind relays the 401 as is,
    so this is a no-op for it; the refresh kind's handling is not built at this tip."""
    return None
