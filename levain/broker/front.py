"""The gateway front's EGRESS PROXY (S1 session 3a; CONTRACT §4.1 `egress` + `subscription_passthrough`, §4.4, §4.7;
plan §2c 8, §2d 9, §2f 5). One front process per egress-slot session, run as that slot's `_levain_gw<n>`, holding no
credential. This module is the CONNECT half: the brokered data plane (3b) and the keys / control wires (3b, 3c) are
not here.

What it enforces, in order, before any upstream byte:
1. the peer (unix shape): the connecting uid must be the slot's (the slot's forwarder, inside its netns). The TCP
   shape (macOS, `127.0.0.1:<gwport_n>`) has no peer check: pf and the slot profile decide who reaches the port.
2. the head: at most HEAD_CAP bytes, CRLF only, no obs-fold, no control or non-ASCII bytes; then h11 parses it
   (a maintained parser, CONTRACT §4.4). Both Content-Length and Transfer-Encoding, a duplicate Host, an
   absolute-form or asterisk-form target, and a CONNECT carrying a body are refused as `malformed`.
3. the target: `CONNECT <host>:443` only. The host must be a name on the session's CONNECT set (the `egress` and
   `subscription_passthrough` routes of the front's policy file); an address literal, any other port, or an
   off-list name is `host-not-allowed`.
4. the address: the name is resolved ONCE, as an absolute name (no search-list expansion), with a bounded number
   of resolutions in flight; if any answer is loopback, private (RFC1918, CGNAT, ULA), link-local
   (incl. the 169.254.169.254 metadata address), IPv4-mapped / IPv4-compatible / 6to4 / Teredo, multicast,
   reserved, documentation-range or otherwise not globally reachable, the whole CONNECT is `address-refused`.
   The upstream socket is then opened to one of THOSE validated addresses, never by name, so a resolver that
   answers differently a second time (DNS rebinding) never chooses where the bytes go.
5. the TLS ClientHello, which the client sends after the 200: its SNI must be the CONNECT host, and a hello with
   no SNI, with an encrypted (ECH / ESNI) name, or that does not parse or is not a hello a TLS server could accept
   (record and legacy versions, cipher-suite and compression vectors) is refused by closing the tunnel with no
   byte sent upstream. This stops domain fronting at the SNI of the FIRST hello; an HTTP Host header inside the
   TLS session, and a later hello (after a HelloRetryRequest, or a renegotiation), are not seen (closing those
   needs interception).
Only then does the front forward the hello and relay bytes both ways without reading them (end-to-end TLS). Each
tunnel is logged at its close as host, address, route, whether the route is `subscription_passthrough`, byte counts
and how it ended: never a request line, a header or a payload byte.

Why the address check lives here and not in the vendor sandbox: srt skips its resolved-address check on a
parentProxy hop (plan §2b R-C(3)), so this proxy is the only place it can hold.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Optional, Sequence

try:
    import h11
except ImportError:   # an install without levain[isolation]: main() says so in one line
    h11 = None  # type: ignore[assignment]

from levain.broker.stub import GWPORT_BASE, SLOTS, clear_stale_socket, peer_uid

POLICY_FORMAT = "levain-front-session/1"
CONNECT_PORT = 443
#: V10's head cap (CONTRACT §11).
HEAD_CAP = 64 * 1024
#: DECISION: a client that has not finished its request head by then is closed (it holds only its own slot's front).
HEAD_TIMEOUT_S = 10.0
RESOLVE_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 10.0
#: DECISION: more answers than this from one resolution is refused rather than validated in part.
MAX_ADDRESSES = 16
#: DECISION: connections past this are closed at accept (a slot cannot pin unbounded tasks on its front).
MAX_CONNS = 256
#: DECISION: once one direction of a tunnel has ended cleanly, the other has this long to end before both close.
HALF_CLOSE_S = 30.0
#: DECISION (the head's number): a tunnel with no byte either way for this long is closed. Longer than any SSE
#: keepalive gap known to us; TCP keepalive (set on both sockets) finds a dead peer, this finds a silent one.
IDLE_S = 600.0
_CHUNK = 65536
#: after a refusal the front reads (and drops) at most this much, for at most DRAIN_S, so the close is not an RST
#: that destroys the status line the client has not read yet.
_DRAIN_CAP = 1024 * 1024
_DRAIN_S = 1.0

#: the alloc kinds of an egress-class session, the only ones with a front (CONTRACT §3.5, §5.3).
SESSION_KINDS = ("interactive", "login", "dispatch", "planner")
#: the kinds a dispatch ticket renders; their routes are the ticket's route + egress routes only (CONTRACT §3.5).
DISPATCH_KINDS = ("dispatch", "planner")
ROUTE_TYPES = ("egress", "subscription_passthrough", "brokered")
CONNECT_TYPES = ("egress", "subscription_passthrough")
_ROUTE_ID = re.compile(r"^[a-z0-9-]{1,48}\Z")
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
#: lowercase IDNA A-label host names with at least two labels; the last label is not all digits (no address literals).
_HOSTNAME = re.compile(rf"^(?=.{{1,253}}\Z){_LABEL}(?:\.{_LABEL})+\Z")
_POLICY_KEYS = frozenset({"route", "type", "hosts", "upstream", "requests", "responses",
                          "max_call_microdollars", "max_session_microdollars"})

#: refusal name -> HTTP status (CONTRACT §4.7's table, the names this half can produce).
STATUS = {"host-not-allowed": 403, "address-refused": 403, "malformed": 400, "template-miss": 404,
          "upstream-error": 502, "upstream-timeout": 504, "keys-unavailable": 503}
_REASON = {400: b"Bad Request", 403: b"Forbidden", 404: b"Not Found", 502: b"Bad Gateway",
           503: b"Service Unavailable", 504: b"Gateway Timeout"}


class PolicyError(ValueError):
    """The front's policy file is unusable: the front does not start."""


class Refused(Exception):
    def __init__(self, name: str, detail: str = "") -> None:
        super().__init__(name)
        self.name = name
        self.detail = detail

    @property
    def status(self) -> int:
        return STATUS[self.name]


# --- policy ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ConnectRoute:
    route: str
    host: str
    passthrough: bool


@dataclass(frozen=True)
class FrontPolicy:
    session: str
    kind: str
    connect: Mapping[str, ConnectRoute]   # host -> the one route that admits it


def _is_inet_aton_spelling(h: str) -> bool:
    try:
        socket.inet_aton(h)
    except OSError:
        return False
    return True


def _hosts(route: str, raw: object) -> list[str]:
    """The one host grammar, for CONNECT hosts and a brokered route's `upstream.host` alike: a lowercase IDNA A-label
    name of two or more labels, with no trailing dot and no port. A host that is an address in disguise is refused
    too: a last label of digits only, and any spelling `socket.inet_aton` accepts as IPv4 (hex, octal, short forms
    such as 0x7f.0x1), because the system resolver would turn it into that address without a lookup."""
    if not isinstance(raw, list) or not raw:
        raise PolicyError(f"route {route}: hosts must be a non-empty list")
    out = []
    for h in raw:
        if (not isinstance(h, str) or not _HOSTNAME.match(h) or h.rsplit(".", 1)[1].isdigit()
                or _is_inet_aton_spelling(h)):
            raise PolicyError(f"route {route}: host {h!r} is not a lowercase A-label host name")
        out.append(h)
    return out


def load_policy(data: bytes) -> FrontPolicy:
    """Parse the front's policy file (render's `R/gw/slot<n>/session.json`, CONTRACT §4.3 "the front's view").
    Refuses: an unknown key, any `credential` field (the front holds no key), a duplicate route id, a host admitted
    by two CONNECT routes (so whether a tunnel is passthrough is never ambiguous), a CONNECT host that is also a
    brokered route's host (a brokered session's vendor API traffic must go through the data plane, never a tunnel),
    and a `subscription_passthrough` route in a dispatch-ticket session (CONTRACT §4.1: dispatch may NEVER use one)."""
    try:
        doc = json.loads(data)
    except (ValueError, UnicodeDecodeError) as e:
        raise PolicyError(f"not JSON: {e}") from None
    if not isinstance(doc, dict) or set(doc) != {"format", "kind", "session", "routes"}:
        raise PolicyError("top level must be exactly {format, kind, session, routes}")
    if doc["format"] != POLICY_FORMAT:
        raise PolicyError(f"format must be {POLICY_FORMAT}")
    if doc["kind"] not in SESSION_KINDS:
        raise PolicyError(f"kind {doc['kind']!r} not in {SESSION_KINDS}")
    if not isinstance(doc["session"], str) or not doc["session"]:
        raise PolicyError("session must be a non-empty string")
    if not isinstance(doc["routes"], list):
        raise PolicyError("routes must be a list")
    seen: set[str] = set()
    connect: dict[str, ConnectRoute] = {}
    brokered_hosts: set[str] = set()
    for pol in doc["routes"]:
        if not isinstance(pol, dict):
            raise PolicyError("a route must be an object")
        route = pol.get("route")
        if not isinstance(route, str) or not _ROUTE_ID.match(route):
            raise PolicyError(f"bad route id {route!r}")
        if "credential" in pol:
            raise PolicyError(f"route {route}: the front's policy carries no credential field")
        unknown = set(pol) - _POLICY_KEYS
        if unknown:
            raise PolicyError(f"route {route}: unknown keys {sorted(unknown)}")
        if route in seen:
            raise PolicyError(f"route {route} twice")
        seen.add(route)
        typ = pol.get("type")
        if typ not in ROUTE_TYPES:
            raise PolicyError(f"route {route}: type {typ!r} not in {ROUTE_TYPES}")
        if typ == "brokered":
            if "hosts" in pol:
                brokered_hosts.update(_hosts(route, pol["hosts"]))
            up = pol.get("upstream")
            if not isinstance(up, dict):
                raise PolicyError(f"route {route}: a brokered route's upstream must be an object")
            brokered_hosts.update(_hosts(route, [up.get("host")]))
            continue
        if typ == "subscription_passthrough" and doc["kind"] in DISPATCH_KINDS:
            raise PolicyError(f"route {route}: a {doc['kind']} session may not carry a subscription_passthrough route")
        for h in _hosts(route, pol.get("hosts")):
            if h in connect:
                raise PolicyError(f"host {h} admitted by two routes ({connect[h].route}, {route})")
            connect[h] = ConnectRoute(route, h, typ == "subscription_passthrough")
    both = sorted(brokered_hosts & set(connect))
    if both:
        raise PolicyError(f"CONNECT routes admit brokered hosts {both}")
    return FrontPolicy(doc["session"], doc["kind"], connect)


# --- addresses ------------------------------------------------------------------------------------------------

_REFUSED_NETS = tuple(ipaddress.ip_network(n) for n in (
    # IPv4: this-network, RFC1918, CGNAT, loopback, link-local (metadata), IETF protocol, documentation,
    # benchmarking, multicast, reserved + broadcast.
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4",
    # IPv6: IPv4-compatible (incl. :: and ::1), IPv4-mapped, IPv4-translated (SIIT), IPv4/IPv6 local-use
    # translation, discard, Teredo, documentation, 6to4, ULA, link-local, site-local, multicast.
    "::/96", "::ffff:0:0/96", "::ffff:0:0:0/96", "64:ff9b:1::/48", "100::/64", "2001::/32", "2001:db8::/32",
    "2002::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
    # IPv6 documentation (RFC 9637) and segment-routing SIDs (RFC 9602): not global on every Python's table.
    "3fff::/20", "5f00::/16",
))
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def refused_address(addr: str) -> Optional[str]:
    """Why `addr` may not be an upstream, or None. Any address carrying a scope id (`%`, e.g. "fe80::1%en0") is
    refused whatever its prefix: `ipaddress` parses a scoped address, and a scope pins the connect to one local
    interface. The named networks are checked next so the answer does not depend on the running Python's
    `is_global` table; `is_global` is then a catch-all for anything newer. A NAT64 address (64:ff9b::/96) is
    judged by the IPv4 address it embeds."""
    if "%" in addr:
        return "scoped"
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return "unparseable"
    if ip.version == 6 and ip in _NAT64:
        inner = refused_address(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
        return f"nat64:{inner}" if inner else None
    for net in _REFUSED_NETS:
        if ip.version == net.version and ip in net:
            return str(net)
    if not ip.is_global or ip.is_multicast:
        return "not-global"
    return None


Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


#: DECISION: system resolutions in flight at once. A slot is held until the resolver call itself returns, not until
#: the CONNECT that asked gives up, so a hung resolver cannot gather threads past this; a CONNECT that finds none
#: free is refused at once.
RESOLVE_CAP = 32
_RESOLVE_SLOTS = threading.BoundedSemaphore(RESOLVE_CAP)


async def system_resolve(host: str, port: int) -> list[str]:
    """Resolve `host` with the system resolver, as an ABSOLUTE name (`host.`): a dotless query would let the
    resolver's search list and ndots turn an allowed name into a name under another domain. The CONNECT and SNI
    checks keep the original spelling; only the query gains the dot.

    Each call runs on its own daemon thread, not the loop's default executor: getaddrinfo cannot be cancelled, so a
    timed-out call keeps its thread until the resolver returns, and a daemon thread never holds up the front's exit.
    `_RESOLVE_SLOTS` bounds how many such threads can exist; over it the CONNECT is refused `upstream-error`."""
    slots = _RESOLVE_SLOTS
    if not slots.acquire(blocking=False):
        raise Refused("upstream-error", "resolver-busy")
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def deliver(result, error) -> None:
        if fut.done():   # the CONNECT gave up (timeout, shutdown): the answer is dropped
            return
        if error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(result)

    def work() -> None:
        result = error = None
        try:
            result = socket.getaddrinfo(host + ".", port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        except BaseException as e:   # noqa: BLE001 - handed to the awaiting CONNECT
            error = e
        finally:
            slots.release()
        try:
            loop.call_soon_threadsafe(deliver, result, error)
        except RuntimeError:   # the loop is closed: nobody is waiting
            pass

    try:
        threading.Thread(target=work, name="levain-gw-resolve", daemon=True).start()
    except RuntimeError:   # no thread could be started: the slot was never handed to one
        slots.release()
        raise Refused("upstream-error", "resolver-busy") from None
    infos = await fut
    return list(dict.fromkeys(info[4][0] for info in infos))


async def open_by_address(addr: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open TCP to an address literal; AI_NUMERICHOST makes a name here an error instead of a lookup."""
    infos = await asyncio.get_running_loop().getaddrinfo(addr, port, type=socket.SOCK_STREAM,
                                                         flags=socket.AI_NUMERICHOST)
    family, typ, proto, _, sockaddr = infos[0]
    sock = socket.socket(family, typ, proto)
    try:
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, sockaddr)
        return await asyncio.open_connection(sock=sock, limit=_CHUNK)
    except BaseException:
        sock.close()
        raise


# --- the request head -----------------------------------------------------------------------------------------

def prescan_head(head: bytes) -> None:
    """The byte-level refusals made BEFORE h11 sees the head (CONTRACT §4.4)."""
    if len(head) > HEAD_CAP:
        raise Refused("malformed", "head-cap")
    body = head[:-4]  # the head ends CRLF CRLF (the reader stops there)
    for b in body:
        if b >= 0x80 or b == 0x7F or (b < 0x20 and b not in (0x09, 0x0D, 0x0A)):
            raise Refused("malformed", "head-byte")
    lines = body.split(b"\r\n")
    for line in lines:
        if b"\r" in line or b"\n" in line:
            raise Refused("malformed", "bare-cr-or-lf")
    for line in lines[1:]:
        if line[:1] in (b" ", b"\t"):
            raise Refused("malformed", "obs-fold")


def parse_head(head: bytes) -> h11.Request:
    prescan_head(head)
    conn = h11.Connection(h11.SERVER, max_incomplete_event_size=HEAD_CAP)
    conn.receive_data(head)
    try:
        event = conn.next_event()
    except h11.RemoteProtocolError as e:
        raise Refused("malformed", f"h11:{type(e).__name__}") from None   # never the message: it quotes client bytes
    if not isinstance(event, h11.Request):
        raise Refused("malformed", "no request")
    names = [n for n, _ in event.headers]
    if names.count(b"host") > 1:   # h11 refuses this itself; kept so the rule does not rest on its version
        raise Refused("malformed", "duplicate-host")
    if b"content-length" in names and b"transfer-encoding" in names:
        raise Refused("malformed", "cl-and-te")
    if event.method == b"CONNECT":
        if b"content-length" in names or b"transfer-encoding" in names:
            raise Refused("malformed", "connect-with-body")
    elif not event.target.startswith(b"/"):
        raise Refused("malformed", "absolute-or-asterisk-form")
    return event


_AUTHORITY = re.compile(rb"^([^:\[\]]+):([0-9]{1,5})\Z")


def connect_target(policy: FrontPolicy, target: bytes) -> tuple[ConnectRoute, int]:
    m = _AUTHORITY.match(target)
    if not m:
        raise Refused("host-not-allowed", "authority")   # incl. a bracketed IPv6 literal
    host, port = m.group(1).decode("ascii").lower(), int(m.group(2))
    if port != CONNECT_PORT:
        raise Refused("host-not-allowed", "port")
    route = policy.connect.get(host)
    if route is None:
        raise Refused("host-not-allowed", "off-list")
    return route, port


# --- the TLS ClientHello (domain fronting) ----------------------------------------------------------------------
# After the 200 the client's first bytes must be a TLS ClientHello whose SNI names the CONNECT host: without this
# check a tunnel to an allowed host's address could carry a TLS session for any other name that address serves
# (domain fronting). Only as much of the hello is parsed as that needs (RFC 8446 §4.1.2, RFC 6066 §3); it is then
# forwarded byte for byte. A hello that does not parse is refused, never relayed.

_TLS_HANDSHAKE = 22
_TLS_RECORD_MAX = 1 << 14
_CLIENT_HELLO = 1
_EXT_SERVER_NAME = 0x0000
#: encrypted_client_hello, and the draft ESNI codepoint: the name the server routes on is then encrypted, so the
#: outer SNI proves nothing. Refused whole.
_EXT_HIDDEN_NAME = (0xFE0D, 0xFFCE)
#: DECISION: a ClientHello (all of its records) larger than this is refused rather than buffered.
HELLO_CAP = 64 * 1024
#: the record-layer versions a ClientHello record may carry (TLS 1.0 .. 1.2; TLS 1.3 keeps a legacy value here).
_RECORD_VERSIONS = (0x0301, 0x0303)
#: the ClientHello's legacy_version field: SSL 3.0 .. TLS 1.2 (TLS 1.3 sends 0x0303 here and its real version in an
#: extension).
_CLIENT_VERSIONS = (0x0300, 0x0303)


def _bad_hello() -> Refused:
    return Refused("malformed", "tls-hello")


class _Cursor:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise _bad_hello()
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def uint(self, n: int) -> int:
        return int.from_bytes(self.take(n), "big")

    def vec(self, len_bytes: int) -> bytes:
        return self.take(self.uint(len_bytes))

    def done(self) -> bool:
        return self.pos == len(self.data)


async def read_client_hello(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """Read whole TLS handshake records until they hold one complete ClientHello. Returns (the raw bytes read, to
    forward verbatim; the ClientHello body). Anything else first, or anything after the hello in the same records,
    is refused. EOF mid-hello raises IncompleteReadError."""
    raw, hs = bytearray(), bytearray()
    while True:
        hdr = await reader.readexactly(5)
        length = int.from_bytes(hdr[3:5], "big")
        version = int.from_bytes(hdr[1:3], "big")
        if (hdr[0] != _TLS_HANDSHAKE or not _RECORD_VERSIONS[0] <= version <= _RECORD_VERSIONS[1]
                or not 0 < length <= _TLS_RECORD_MAX):
            raise _bad_hello()
        if len(raw) + 5 + length > HELLO_CAP:
            raise _bad_hello()
        frag = await reader.readexactly(length)
        raw += hdr + frag
        hs += frag
        if len(hs) < 4:
            continue
        if hs[0] != _CLIENT_HELLO:
            raise _bad_hello()
        need = 4 + int.from_bytes(hs[1:4], "big")
        if len(hs) < need:
            continue
        if len(hs) != need:
            raise _bad_hello()
        return bytes(raw), bytes(hs[4:])


def client_hello_sni(body: bytes) -> str:
    """The ClientHello's one host_name, lowercased. Refuses (host-not-allowed) a hello with no server_name and one
    carrying an encrypted / hidden name extension; refuses (malformed) anything that does not parse, a repeated
    extension, and a server_name list that is not exactly one host_name of printable ASCII. Also malformed: a
    legacy_version outside SSL 3.0 .. TLS 1.2, an empty or odd-length cipher_suites vector, and a compression vector
    that is empty or lacks the null method; a hello no TLS server could accept is not evidence of a TLS session."""
    c = _Cursor(body)
    if not _CLIENT_VERSIONS[0] <= c.uint(2) <= _CLIENT_VERSIONS[1]:
        raise _bad_hello()
    c.take(32)        # random
    c.vec(1)          # legacy_session_id
    suites = c.vec(2)
    if not suites or len(suites) % 2:
        raise _bad_hello()
    if 0 not in c.vec(1):   # legacy_compression_methods: non-empty, offering null
        raise _bad_hello()
    if c.done():
        raise Refused("host-not-allowed", "sni-missing")   # no extensions at all
    exts = _Cursor(c.vec(2))
    if not c.done():
        raise _bad_hello()
    seen: set[int] = set()
    name: Optional[bytes] = None
    while not exts.done():
        typ = exts.uint(2)
        data = exts.vec(2)
        if typ in seen:
            raise _bad_hello()
        seen.add(typ)
        if typ in _EXT_HIDDEN_NAME:
            raise Refused("host-not-allowed", "sni-hidden")
        if typ == _EXT_SERVER_NAME:
            lst = _Cursor(data)
            names = _Cursor(lst.vec(2))
            if not lst.done() or names.uint(1) != 0:   # name_type host_name
                raise _bad_hello()
            name = names.vec(2)
            if not names.done() or not name or any(b <= 0x20 or b >= 0x7F for b in name):
                raise _bad_hello()
    if name is None:
        raise Refused("host-not-allowed", "sni-missing")
    return name.decode("ascii").lower()


# --- the server -----------------------------------------------------------------------------------------------

def _keepalive(writer: asyncio.StreamWriter) -> None:
    """SO_KEEPALIVE on a TCP stream (a unix socket has no use for it), so a peer that vanished is found."""
    sock = writer.get_extra_info("socket")
    if sock is not None and sock.family in (socket.AF_INET, socket.AF_INET6):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass


class Front:
    def __init__(self, policy: FrontPolicy, *, log_path: Optional[Path | str] = None,
                 slot_uid: Optional[int] = None, resolve: Resolver = system_resolve,
                 connect: Connector = open_by_address) -> None:
        self.policy = policy
        self.log_path = Path(log_path) if log_path is not None else None
        self.slot_uid = slot_uid
        self._resolve = resolve
        self._connect = connect
        #: every open stream, client and upstream alike: closed at shutdown.
        self.live: set[asyncio.StreamWriter] = set()
        #: client connections being served: the MAX_CONNS count.
        self.clients = 0
        self.bound: Optional[tuple[str, int]] = None

    def _log(self, rec: dict) -> None:
        rec = {"at": round(time.time(), 3), "session": self.policy.session, **rec}
        line = json.dumps(rec, sort_keys=True) + "\n"
        try:
            if self.log_path is None:
                sys.stderr.write(line)
            else:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
        except OSError as e:
            print(f"levain gw-front: log write failed ({e}): {line}", file=sys.stderr, end="")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.clients >= MAX_CONNS:
            writer.close()
            return
        self.clients += 1
        self.live.add(writer)
        try:
            await self._serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients -= 1
            self.live.discard(writer)
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is not None and sock.family == socket.AF_UNIX:
            uid = peer_uid(sock)
            if self.slot_uid is None or uid != self.slot_uid:
                self._log({"event": "refused", "refused": "peer", "peer_uid": uid})
                return   # no answer: an unexpected peer learns nothing
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
        except asyncio.LimitOverrunError:
            await self._refuse(reader, writer, Refused("malformed", "head-cap"))
            return
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return
        try:
            req = parse_head(head)
            if req.method != b"CONNECT":
                # The brokered data plane is session 3b's; until it exists a route path has no keys to reach.
                raise Refused("keys-unavailable" if req.target.startswith(b"/r/") else "template-miss")
            route, port = connect_target(self.policy, req.target)
            ureader, uwriter, addr = await self._open_upstream(route, port)
        except Refused as r:
            await self._refuse(reader, writer, r)
            return
        # The upstream is open before the 200 so an unreachable one is still answered 502 / 504. The ClientHello
        # comes after the 200, so a refused hello leaves that connection opened with no byte sent on it.
        _keepalive(writer)
        _keepalive(uwriter)
        self.live.add(uwriter)
        counts: dict = {"up": 0, "down": 0}   # byte counts, plus "end" once the relay has finished
        relaying = False
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            try:
                hello, body = await asyncio.wait_for(read_client_hello(reader), HEAD_TIMEOUT_S)
                if client_hello_sni(body) != route.host:
                    raise Refused("host-not-allowed", "sni-mismatch")
            except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                self._log({"event": "refused", "refused": "malformed", "detail": "tls-hello-incomplete",
                           "host": route.host, "address": addr})
                return
            except Refused as r:
                # after the 200 there is no HTTP answer to give: the client sees the tunnel close
                self._log({"event": "refused", "refused": r.name, "detail": r.detail, "host": route.host,
                           "address": addr})
                return
            relaying = True
            counts["up"] = len(hello)
            uwriter.write(hello)
            await uwriter.drain()
            await self._relay(reader, writer, ureader, uwriter, counts)
        finally:
            uwriter.close()
            self.live.discard(uwriter)
            if relaying:   # also on cancellation (shutdown): a tunnel that carried bytes is never left out of the log
                # end: how the relay ended; "aborted" = the relay never reported an end (this task was cancelled,
                # or forwarding the hello failed). Shutdown closes the streams first, so a tunnel open then
                # usually ends "broken".
                self._log({"event": "tunnel", "route": route.route, "passthrough": route.passthrough,
                           "host": route.host, "port": port, "address": addr,
                           "bytes_up": counts["up"], "bytes_down": counts["down"],
                           "end": counts.get("end", "aborted")})

    async def _open_upstream(self, route: ConnectRoute, port: int):
        try:
            addrs = list(await asyncio.wait_for(self._resolve(route.host, port), RESOLVE_TIMEOUT_S))
        except asyncio.TimeoutError:
            raise Refused("upstream-timeout", "resolve") from None
        except OSError as e:
            raise Refused("upstream-error", f"resolve: {e}") from None
        addrs = list(dict.fromkeys(addrs))
        if not addrs:
            raise Refused("upstream-error", "no address")
        if len(addrs) > MAX_ADDRESSES:
            raise Refused("address-refused", "too many answers")
        bad = [(a, why) for a in addrs if (why := refused_address(a))]
        if bad:
            # all-or-nothing: an answer set that mixes a public address with a refused one is refused whole
            raise Refused("address-refused", "; ".join(f"{a} {w}" for a, w in bad))
        timed_out = 0
        for addr in addrs:   # the validated addresses, never the name again
            try:
                ur, uw = await asyncio.wait_for(self._connect(addr, port), CONNECT_TIMEOUT_S)
                return ur, uw, addr
            except asyncio.TimeoutError:
                timed_out += 1
            except OSError:
                pass
        raise Refused("upstream-timeout" if timed_out == len(addrs) else "upstream-error", "connect")

    async def _relay(self, creader, cwriter, ureader, uwriter, counts: dict) -> None:
        """Copy both ways until both ends are done. A broken direction ends both; after one direction ends cleanly
        the other gets HALF_CLOSE_S to finish (a client that fully closed looks like a half-close from here, and an
        upstream that never answers the FIN would otherwise hold the tunnel open). A tunnel on which NO byte has moved
        in either direction for IDLE_S is broken: idle is judged for the tunnel, not per direction, because during a
        long streamed response the client sends nothing while the upstream is still talking."""
        loop = asyncio.get_running_loop()
        last = [loop.time()]

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, key: str) -> str:
            """-> how this direction ended: "eof" (cleanly), "idle", "broken" (an OSError), or "error:<type>" for
            anything else raised while copying, which is recorded instead of escaping as an unhandled exception."""
            try:
                while True:
                    left = last[0] + IDLE_S - loop.time()
                    if left <= 0:
                        return "idle"   # the whole tunnel has been quiet for IDLE_S
                    try:
                        chunk = await asyncio.wait_for(src.read(_CHUNK), left)
                    except asyncio.TimeoutError:
                        continue       # re-judged against the other direction's last byte
                    if not chunk:
                        break
                    last[0] = loop.time()
                    counts[key] += len(chunk)
                    dst.write(chunk)
                    await dst.drain()
                if dst.can_write_eof():
                    dst.write_eof()   # half-close: the other direction keeps flowing
                return "eof"
            except (OSError, asyncio.IncompleteReadError):
                return "broken"
            except Exception as e:   # noqa: BLE001 - see the docstring
                return f"error:{type(e).__name__}"

        tasks = {asyncio.ensure_future(pump(creader, uwriter, "up")),
                 asyncio.ensure_future(pump(ureader, cwriter, "down"))}
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            ends = [t.result() for t in done]
            if pending and all(e == "eof" for e in ends):
                done, pending = await asyncio.wait(pending, timeout=HALF_CLOSE_S)
                ends += [t.result() for t in done]
            bad = [e for e in ends if e != "eof"]
            counts["end"] = bad[0] if bad else ("half-close-timeout" if pending else "eof")
        finally:
            cwriter.close()
            uwriter.close()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _refuse(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, r: Refused) -> None:
        self._log({"event": "refused", "refused": r.name, "detail": r.detail})
        body = r.name.encode() + b"\n"
        writer.write(b"HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n"
                     b"X-Levain-Refused: %s\r\nConnection: close\r\n\r\n%s"
                     % (r.status, _REASON[r.status], len(body), r.name.encode(), body))
        await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
        loop = asyncio.get_running_loop()
        deadline, drained = loop.time() + _DRAIN_S, 0
        try:
            while drained < _DRAIN_CAP and (left := deadline - loop.time()) > 0:
                chunk = await asyncio.wait_for(reader.read(min(_CHUNK, _DRAIN_CAP - drained)), timeout=left)
                if not chunk:
                    break
                drained += len(chunk)
        except (asyncio.TimeoutError, OSError):
            pass

    def close_live(self) -> None:
        for w in list(self.live):
            w.close()

    async def serve_unix(self, path: Path | str) -> asyncio.base_events.Server:
        """The Linux shape: `R/gw/slot<n>/gw.sock`, mode 0660 (the slot reaches it through the socket's group,
        CONTRACT §2.2), peer uid == the slot's."""
        if self.slot_uid is None:
            raise ValueError("the unix shape needs the slot uid (its peer check)")
        path = str(path)
        clear_stale_socket(path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            old = os.umask(0o117)   # the socket is created 0660: no window at a wider mode
            try:
                s.bind(path)
            finally:
                os.umask(old)
            self.bound = (path, os.lstat(path).st_ino)
            return await asyncio.start_unix_server(self._handle, sock=s, limit=HEAD_CAP)
        except BaseException:
            s.close()
            self.unbind()
            raise

    def unbind(self) -> None:
        if self.bound is None:
            return
        path, ino = self.bound
        self.bound = None
        try:
            if os.lstat(path).st_ino == ino:
                os.unlink(path)
        except FileNotFoundError:
            pass

    async def serve_tcp(self, port: int, host: str = "127.0.0.1") -> asyncio.base_events.Server:
        """The macOS shape: `127.0.0.1:<gwport_n>` (V9)."""
        if not GWPORT_BASE <= port < GWPORT_BASE + SLOTS:
            raise ValueError(f"the TCP shape serves only the gateway range {GWPORT_BASE}..{GWPORT_BASE + SLOTS - 1}")
        return await asyncio.start_server(self._handle, host=host, port=port, limit=HEAD_CAP)


async def run_front(front: Front, *, unix: Optional[str] = None, port: Optional[int] = None) -> None:
    """Serve until SIGTERM / SIGINT, then close every tunnel and the listener."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    server = await (front.serve_unix(unix) if unix is not None else front.serve_tcp(port))  # type: ignore[arg-type]
    try:
        await stop.wait()
    finally:
        server.close()
        front.close_live()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        if unix is not None:
            front.unbind()


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m levain.broker.front")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--unix", help="socket path, the Linux shape (R/gw/slot<n>/gw.sock); needs --slot-uid")
    g.add_argument("--port", type=int, help=f"TCP port on 127.0.0.1, the macOS shape ({GWPORT_BASE} + slot)")
    ap.add_argument("--slot-uid", type=int, help="the only peer uid the unix shape serves")
    ap.add_argument("--policy", required=True, help="the front's policy file (R/gw/slot<n>/session.json)")
    ap.add_argument("--log", help="JSON-lines tunnel log (default: stderr)")
    ap.add_argument("--go-fd", type=int, help="render's GO pipe: serve only after one byte arrives on it; EOF = exit "
                                              "without serving (CONTRACT §5.1 render (4)-(5b))")
    a = ap.parse_args(argv)
    # every argument is checked before the GO fd is read: a bad one exits at once instead of after render's byte
    if a.unix is not None and a.slot_uid is None:
        ap.error("--unix needs --slot-uid")
    if a.port is not None and a.slot_uid is not None:
        ap.error("--slot-uid is the unix shape's peer check; the TCP shape (--port) takes none")
    if a.port is not None and not GWPORT_BASE <= a.port < GWPORT_BASE + SLOTS:
        ap.error(f"--port must be in the gateway range {GWPORT_BASE}..{GWPORT_BASE + SLOTS - 1}")
    if h11 is None:
        print("levain gw-front: the h11 parser is not installed; install levain[isolation]", file=sys.stderr)
        return 2
    if os.geteuid() == 0:
        print("levain gw-front: refusing to run as root (it runs as the slot's _levain_gw<n>)", file=sys.stderr)
        return 2
    try:
        policy = load_policy(Path(a.policy).read_bytes())
    except (OSError, PolicyError) as e:
        print(f"levain gw-front: policy refused: {e}", file=sys.stderr)
        return 2
    if a.go_fd is not None:
        # The policy is read first: render is to write it before it starts the front (CONTRACT §4.3). Nothing is
        # bound until GO.
        try:
            go = os.read(a.go_fd, 1)
        except OSError as e:
            print(f"levain gw-front: GO pipe unreadable ({e})", file=sys.stderr)
            return 3
        finally:
            try:
                os.close(a.go_fd)
            except OSError:
                pass
        if not go:
            return 3   # EOF before GO: render refused or died, so this front never serves
    front = Front(policy, log_path=a.log, slot_uid=a.slot_uid)
    asyncio.run(run_front(front, unix=a.unix, port=a.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
