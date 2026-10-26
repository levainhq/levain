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
import base64
import errno
import hashlib
import ipaddress
import json
import os
import re
import signal
import socket
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Awaitable, Callable, Coroutine, Mapping, Optional, Sequence

try:
    import h11
except ImportError:   # an install without levain[isolation]: main() says so in one line
    h11 = None  # type: ignore[assignment]

from levain.broker import gwwire
from levain.broker.gwwire import FrameReader, FrontSide, HoldingReader, WireError
from levain.broker.norm import REASON, STATUS, Refused  # noqa: F401 - STATUS and Refused are re-exported
from levain.broker import norm
from levain.broker.policy import (CONNECT_TYPES, ROUTE_TYPES, PolicyError, RoutePolicy,  # noqa: F401 - re-exported
                                  is_host_name, parse_route)
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
_ROUTE_ID = re.compile(r"^[a-z0-9-]{1,48}\Z")
#: V34: keys answers the front's `H` with `A` within this, else the front exits 3 without binding.
STATUS_A_S = 5.0
#: V34: after a refusal written mid-body, the front discards the slot's remaining body for at most this long.
DISCARD_S = 5.0
#: 3b-8a T1 (the head's shape: a cap and one rotated file): the tunnel log's size cap. Past it the front renames the log
#: to `<log>.1` (replacing the last one) and starts a new one, so a slot's refusal flood holds at most two caps.
#: [judged by s3b8af1, 10-26, against no measurement]
LOG_CAP = 4 * 1024 * 1024
#: the log's mode whatever the umask the front inherits: its account and the dir's group (root) only.
LOG_MODE = 0o640
_LOG_OPEN = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


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
    brokered: Mapping[str, RoutePolicy] = field(default_factory=dict)   # route id -> its policy
    #: render's incarnation for the session (the STATUS connection's `H`); None in a file without one.
    incarnation: Optional[int] = None


def load_policy(data: bytes) -> FrontPolicy:
    """Parse the front's policy file (render's `R/gw/slot<n>/session.json`, CONTRACT §4.3 "the front's view").
    Each route is parsed by `policy.parse_route`; this function keeps the file's own checks. Refuses: an unknown
    key, any `credential` field (the front holds no key), a duplicate route id, a host admitted
    by two CONNECT routes (so whether a tunnel is passthrough is never ambiguous), a CONNECT host that is also a
    brokered route's host (a brokered session's vendor API traffic must go through the data plane, never a tunnel),
    and a `subscription_passthrough` route in a dispatch-ticket session (CONTRACT §4.1: dispatch may NEVER use one)."""
    try:
        doc = json.loads(data)
    except (ValueError, UnicodeDecodeError) as e:
        raise PolicyError(f"not JSON: {e}") from None
    if not isinstance(doc, dict) or not {"format", "kind", "session", "routes"} <= set(doc) \
            or not set(doc) <= {"format", "kind", "session", "routes", "incarnation"}:
        raise PolicyError("top level must be exactly {format, kind, session, routes, incarnation?}")
    inc = doc.get("incarnation")
    if inc is not None and (not isinstance(inc, int) or isinstance(inc, bool) or inc < 0):
        raise PolicyError("incarnation must be an unsigned integer")
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
    brokered: dict[str, RoutePolicy] = {}
    for raw in doc["routes"]:
        # an entry is a priced map's (no per-call cap) or a `/1` map's (a per-call cap); the front enforces neither
        # cap (keys does), so each entry is read as the format it came from.
        per_call = isinstance(raw, dict) and "max_call_microdollars" in raw
        pol = parse_route(raw, source="dispatch-session" if doc["kind"] in DISPATCH_KINDS else "session",
                          per_call=per_call)
        route, typ = pol.route, pol.type
        if route in seen:
            raise PolicyError(f"route {route} twice")
        seen.add(route)
        if typ == "brokered":
            assert pol.upstream is not None
            brokered_hosts.update(pol.hosts)
            brokered_hosts.add(pol.upstream.host)
            brokered[route] = pol
            continue
        if typ == "subscription_passthrough" and doc["kind"] in DISPATCH_KINDS:
            raise PolicyError(f"route {route}: a {doc['kind']} session may not carry a subscription_passthrough route")
        for h in pol.hosts:
            if h in connect:
                raise PolicyError(f"host {h} admitted by two routes ({connect[h].route}, {route})")
            connect[h] = ConnectRoute(route, h, typ == "subscription_passthrough")
    both = sorted(brokered_hosts & set(connect))
    if both:
        raise PolicyError(f"CONNECT routes admit brokered hosts {both}")
    return FrontPolicy(doc["session"], doc["kind"], connect, brokered, inc)


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
    return parse_head_conn(head)[1]


def parse_head_conn(head: bytes) -> tuple[h11.Connection, h11.Request]:
    """`parse_head`, also returning the h11 connection that read it: a brokered exchange reads the body through it."""
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
    return conn, event


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


#: the most of a refused CONNECT's host the log line carries: a DNS name's length.
REFUSED_HOST_CAP = 253

#: WF-1b (wf1a return ruling 3): an off-list refusal's line names how the operator adds a host. The agent's own error
#: text names no host (measured by lane-wfetch on Claude Code 2.1.310: "Error: proxy refused the connection", wfetch
#: DESIGN §1.4), so this line is where the remedy can be read.
OFF_LIST_REMEDY = "sudo install-helper routes-egress"


def refused_host_fields(target: object) -> dict:
    """The requested host of a refused CONNECT, for its log line (wfetch DESIGN §2.3): ``{"host": h}`` only when the
    target is an unbracketed ``host[:port]`` whose host part is at most :data:`REFUSED_HOST_CAP` bytes and, lowercased
    as :func:`connect_target` matches it, passes the policy's host grammar (:func:`levain.broker.policy.is_host_name`);
    else ``{"host_invalid": True, "host_len": n}`` (an address literal and a bracketed authority included, which the
    CONNECT grammar refuses), and ``{"host_invalid": True}`` for a target that is not bytes. The host is slot-supplied,
    so nothing else of it is logged."""
    if type(target) is not bytes:
        return {"host_invalid": True}
    m = re.match(rb"\[([^\]]*)\](?::[0-9]*)?\Z", target)
    raw = m.group(1) if m else (target.rpartition(b":")[0] if b":" in target else target)
    if not m and len(raw) <= REFUSED_HOST_CAP:
        try:
            low = raw.decode("ascii").lower()
        except UnicodeDecodeError:
            low = None
        if low is not None and is_host_name(low):
            return {"host": low}
    return {"host_invalid": True, "host_len": len(raw)}


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


# --- the brokered half (3b): keys' wire, the exchange -------------------------------------------------------------

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
#: a forwarded header value the wire can carry: visible ASCII and tab.
_FWD_VALUE = re.compile(r"[\t\x20-\x7e]*\Z")
#: the events queue between keys' reader and the relay: a full queue makes the reader wait, so a slot that reads
#: slowly slows keys down instead of the front buffering the upstream's response.
_EVENTS_MAX = 8
#: slot-task outcomes that never end an exchange: the writer finished ("end"), or a write to keys failed
#: ("write-error"), and keys' reader decides the outcome. On a websocket the GET's writer may report one after `U`.
_BODY_SKIP = frozenset({"end", "write-error"})


async def open_keys(path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """The front's ONE keys-side client: a connection to keys' `R/keys/front.sock`, for the STATUS connection and for
    each brokered exchange (one connection per exchange). Its reader is a `HoldingReader` over the connection's
    socket, so a frame keys sent before a reset is still read."""
    loop = asyncio.get_running_loop()
    reader = HoldingReader(limit=_CHUNK, loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    transport, _ = await loop.create_unix_connection(lambda: protocol, path)
    reader.sock = transport.get_extra_info("socket")
    return reader, asyncio.StreamWriter(transport, protocol, reader, loop)


def split_route_target(target: bytes) -> tuple[str, bytes, Optional[bytes]]:
    """`/r/<route>[/path][?query]` -> (route, path, query or None). A malformed route id is `route-not-allowed`."""
    rest = target[3:]
    path_part, q, query = rest.partition(b"?")
    route_b, slash, tail = path_part.partition(b"/")
    try:
        route = route_b.decode("ascii")
    except UnicodeDecodeError:
        raise Refused("route-not-allowed", "route-id") from None
    if not _ROUTE_ID.match(route):
        raise Refused("route-not-allowed", "route-id")
    return route, slash + tail, (query if q else None)


async def open_status(path: str, session: str, incarnation: int) -> tuple[FrameReader, asyncio.StreamWriter]:
    """The STATUS connection: `H`, then keys' `A` within STATUS_A_S. Anything else raises (the caller exits 3)."""
    kr, kw = await asyncio.wait_for(open_keys(path), STATUS_A_S)
    try:
        kw.write(gwwire.encode(b"H", {"v": 1, "session": session, "incarnation": incarnation}))
        await kw.drain()
        frames = FrameReader(kr)
        got = await asyncio.wait_for(frames.read(FrontSide(status=True).allowed()), STATUS_A_S)
        if got is None:
            raise ConnectionError("keys closed the STATUS connection before A")
        return frames, kw
    except BaseException:
        kw.close()
        raise


class _SlotGone(Exception):
    """The slot closed before its request body ended."""


class _SlotBody:
    """The slot's request body, read through the h11 connection that parsed its head. `next()` is cancel-safe: state
    changes only after an await returns."""

    def __init__(self, conn: h11.Connection, reader: asyncio.StreamReader) -> None:
        self.conn, self.reader = conn, reader
        self.done = False

    async def next(self, limit: int = _CHUNK) -> Optional[bytes]:
        """The next body bytes, or None once the body ended. Raises _SlotGone at an EOF mid-body and
        Refused("malformed") for a body h11 refuses. At most `limit` bytes are read from the slot for this call
        (h11 may still return bytes it already held)."""
        while not self.done:
            try:
                ev = self.conn.next_event()
            except h11.RemoteProtocolError:
                if self.conn.their_state is h11.ERROR and getattr(self, "_eof", False):
                    raise _SlotGone() from None
                raise Refused("malformed", "body") from None
            if ev is h11.NEED_DATA:
                data = await self.reader.read(max(1, min(_CHUNK, limit)))
                if not data:
                    self._eof = True
                self.conn.receive_data(data)
                continue
            if isinstance(ev, h11.Data):
                if ev.data:
                    return bytes(ev.data)
                continue
            if isinstance(ev, h11.EndOfMessage):   # its trailers, if any, are dropped
                self.done = True
                return None
            raise _SlotGone()
        return None


def _ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode("ascii") + _WS_GUID).digest()).decode("ascii")


class BrokeredExchange:
    """One brokered request: the slot's head checked (design 1.3 steps 1-4), `Q` to keys, then keys' frames relayed
    back while the body streams forward. One task READS keys from the moment `Q` is sent and another WRITES `B`
    frames: the reader is parked in its read loop before the first write (`parked`), so an `F` keys wrote is never
    lost to a write error. After ANY refusal written to the slot mid-body the front half-closes the slot and
    DISCARDS its remaining body (≤ the route's `body_cap` read after the refusal, ≤ DISCARD_S), then closes.

    `before_keys_read` / `before_body_write` are the R15 gates: events the reader awaits after parking and the
    writer awaits before each `B`. Production passes None (open); only the gate script holds them."""

    def __init__(self, front: "Front", route: RoutePolicy, conn: h11.Connection, req: h11.Request, path: bytes,
                 query: Optional[bytes], sreader: asyncio.StreamReader, swriter: asyncio.StreamWriter, *,
                 before_keys_read: Optional[asyncio.Event] = None,
                 before_body_write: Optional[asyncio.Event] = None) -> None:
        self.front, self.route, self.req = front, route, req
        self.path, self.query = path, query
        self.sreader, self.swriter = sreader, swriter
        self.body = _SlotBody(conn, sreader)
        self.before_keys_read, self.before_body_write = before_keys_read, before_body_write
        self.parked = asyncio.Event()
        #: True when the first `B` write found `parked` already set (the R26 invariant, read by the gate script).
        self.parked_before_first_write: Optional[bool] = None
        self.pairs = [(n.decode("latin-1"), v.decode("latin-1")) for n, v in req.headers]
        self.nobody = req.method == b"HEAD"
        self.rec: dict = {"event": "exchange", "route": route.route, "method": req.method.decode("ascii")}
        #: the `Q.upgrade` sent ("websocket" only after the whole handshake checked) and the key it carried.
        self.upgrade = "none"
        self.ws_key: Optional[str] = None
        #: True once a final answer (a refusal, keys' `R`, a 101) started on the slot; a `100 Continue` is not one.
        self.answered = False
        #: the slot-side tasks started through `_slot_task` whose exit `_next` has not yet seen, and those it has seen
        #: but not yet reported, each with the count of keys' events decoded when it was seen.
        self._slot: set = set()
        self._ended: list = []
        #: keys' events decoded by the reader (counted before its `put`, which a full queue blocks) and taken by `_next`.
        self._decoded = self._taken = 0

    # -- the head ------------------------------------------------------------------------------------------------

    def _q(self) -> tuple[dict, bool]:
        """-> (the `Q` payload, expect-100). Raises Refused for a head refused before keys."""
        r = self.route
        tpl = norm.match_template(r.templates, self.req.method, self.path, self.query)
        token = norm.bearer_token(self.pairs)
        fwd = norm.check_headers(self.pairs, r.headers or {}, upgrade_ok=tpl.upgrade is not None)
        if any(not _FWD_VALUE.match(v) for _, v in fwd):
            raise Refused("malformed", "header-value")   # the wire carries visible ASCII and tab only
        names = {n for n, _ in fwd}
        self.rec["dropped"] = sorted({n.lower() for n, _ in self.pairs} - names - norm.CONSUMED
                                     - {"connection"} - ({"upgrade"} if tpl.upgrade else set()))
        expect = [v.lower() for n, v in self.pairs if n == "expect"]
        if expect and expect != ["100-continue"]:
            raise Refused("malformed", "expect")
        cl = [v for n, v in self.pairs if n == "content-length"]
        te = [v for n, v in self.pairs if n == "transfer-encoding"]
        if cl and te:
            raise Refused("malformed", "cl-and-te")
        if cl:
            n = int(cl[0])   # h11 validated it
            if n > r.body_cap:
                raise Refused("body-cap", "length")
            body = f"length:{n}"
        elif te:
            body = "chunked"
        else:
            body = "none"
        upgrade = "none"
        if tpl.upgrade and self._asks_upgrade():
            self._check_websocket_handshake(fwd, body)
            upgrade = "websocket"
        self.upgrade = upgrade
        return ({"v": 1, "token": token, "route": r.route, "method": self.req.method.decode("ascii"),
                 "path": self.path.decode("ascii"), "query": (self.query or b"").decode("ascii"),
                 "headers": [[n, v] for n, v in fwd], "body": body, "upgrade": upgrade}, bool(expect))

    def _asks_upgrade(self) -> bool:
        return any(n == "upgrade" for n, _ in self.pairs) or any(
            "upgrade" in norm.connection_tokens(v) for n, v in self.pairs if n == "connection")

    def _check_websocket_handshake(self, fwd: list, body: str) -> None:
        """A request on a websocket template that asks to upgrade must carry the whole RFC 6455 §4.1 client
        handshake, else `malformed`: HTTP/1.1 or later, `upgrade` among its Connection options, `Upgrade:
        websocket`, version 13, no body, and one `Sec-WebSocket-Key` that is the canonical base64 of 16 bytes and is
        among the forwarded headers (the 101 is computed from it). Only then may keys' `U` become a 101."""
        if self.req.http_version < b"1.1":   # h11 gives "d.d", so a bytes comparison orders it
            raise Refused("malformed", "websocket-version")
        conn = [t for n, v in self.pairs if n == "connection" for t in norm.connection_tokens(v)]
        ups = [v.strip().lower() for n, v in self.pairs if n == "upgrade"]
        vers = [v.strip() for n, v in self.pairs if n == "sec-websocket-version"]
        keys = [v.strip() for n, v in self.pairs if n == "sec-websocket-key"]
        if "upgrade" not in conn or ups != ["websocket"] or vers != ["13"] or body != "none" or len(keys) != 1:
            raise Refused("malformed", "websocket-handshake")
        key = keys[0]
        try:
            raw = base64.b64decode(key, validate=True)
        except ValueError:
            raw = b""
        if len(raw) != 16 or base64.b64encode(raw).decode("ascii") != key:
            raise Refused("malformed", "websocket-key")
        if not any(n == "sec-websocket-key" and v == key for n, v in fwd):
            raise Refused("malformed", "websocket-key")
        self.ws_key = key

    # -- the run -------------------------------------------------------------------------------------------------

    async def run(self) -> None:
        """The exchange, start to end. Every path, each refusal made before keys included, writes its record."""
        try:
            await self._run()
        except WireError as e:   # a frame the wire will not encode never escapes the exchange
            self.rec["wire_error"] = str(e)
            if not self.answered:
                await self.refuse(Refused("keys-unavailable", "wire"))
        finally:
            self.front._log(self.rec)

    async def _run(self) -> None:
        try:
            q, expect = self._q()
            try:
                qframe = gwwire.encode(b"Q", q)
            except WireError:
                # a head under HEAD_CAP whose `Q` is over the wire's cap (JSON escaping grows it): refused with the
                # head cap's name, before keys is opened
                raise Refused("malformed", "q-cap") from None
        except Refused as r:
            await self.refuse(r)
            return
        if self.front.keys_path is None:
            await self.refuse(Refused("keys-unavailable", "no-keys"))
            return
        try:
            kr, kw = await asyncio.wait_for(open_keys(self.front.keys_path), CONNECT_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            await self.refuse(Refused("keys-unavailable", "keys-connect"))
            return
        self.front.live.add(kw)
        events: asyncio.Queue = asyncio.Queue(maxsize=_EVENTS_MAX)
        reader = self._reader_task = asyncio.ensure_future(self._read_keys(FrameReader(kr), events))
        writer: Optional[asyncio.Future] = None
        try:
            kw.write(qframe)
            writer = self._slot_task(kw, self._write_body(kw), b"X")
            await self._relay(kw, events, expect, writer)
        finally:
            for t in (reader, writer):
                if t is not None:
                    t.cancel()
            await asyncio.gather(*(t for t in (reader, writer) if t is not None), return_exceptions=True)
            kw.close()
            self.front.live.discard(kw)

    async def _read_keys(self, frames: FrameReader, events: asyncio.Queue) -> None:
        """keys' frames, in order, onto `events`; however it ends, the last event says so ("eof" / "error"). It
        awaits each `put`, so a full queue holds keys back. Each event is counted in `_decoded` before its `put`, so
        `_next` orders a slot task's exit after every event decoded ahead of it, including one still waiting on the
        queue."""
        side = FrontSide(status=False)
        self.parked.set()   # entered the loop: the body writer may start
        if self.before_keys_read is not None:
            await self.before_keys_read.wait()
        try:
            while side.state != "done":
                got = await frames.read(side.allowed())
                if got is None:
                    self._decoded += 1
                    await events.put(("eof", None))
                    return
                ftype, payload = got
                if ftype == b"U" and self.upgrade != "websocket":
                    raise WireError("U for a request that asked no upgrade")
                nobody = ftype == b"R" and (self.nobody or payload["status"] in (204, 304))  # type: ignore[index]
                side.got(ftype, nobody=nobody)
                self._decoded += 1
                await events.put((ftype, payload))
        except Exception as e:   # noqa: BLE001 - a reader that dies always tells the relay
            self._decoded += 1
            await events.put(("error", type(e).__name__))

    def _slot_task(self, kw: asyncio.StreamWriter, work: Coroutine, end_frame: bytes) -> asyncio.Future:
        """Start a slot-side task (the body writer, the websocket pump): the ONE way the exchange starts a task that
        reads the slot. The task's exit, by any path, IS its terminal slot event: `_next` races the task itself, so no
        exit can go unreported and none waits on a full events queue. Its result is the outcome `work` returned,
        "write-error" for an OSError (the slot reads map their own errors, so one that escapes came from a write to
        keys), or ("slot-gone", the error's type) for anything else raised. For an outcome that ends the slot's side
        (any not in `_BODY_SKIP`) keys hears `end_frame` first, unflushed: closing the keys connection flushes it, and
        a drain here could wait on a keys that has stopped reading."""
        async def run() -> object:
            try:
                end = await work
            except OSError as e:
                self.rec["write_error"] = type(e).__name__
                return "write-error"
            except Exception as e:   # noqa: BLE001 - any other exit is the slot's side gone
                end = ("slot-gone", type(e).__name__)
            if end not in _BODY_SKIP:
                try:
                    kw.write(gwwire.encode(end_frame))
                except Exception:   # noqa: BLE001 - keys' side is gone too; the slot's outcome still stands
                    pass
            return end

        task = asyncio.ensure_future(run())
        task.add_done_callback(lambda _: work.close())   # a task cancelled before it started never awaited `work`
        self._slot.add(task)
        return task

    async def _next(self, events: asyncio.Queue) -> tuple:
        """The next event, in arrival order: keys' frames, and each slot-side task's exit as ("body", its outcome),
        reported once, after every keys event the reader had decoded when the exit was seen, one still blocked on the
        full queue included (at most the queue's bound plus that one, so a keys that keeps sending cannot hold it
        back); else ("error", "reader-gone") once keys' reader has ended with none left. The wait races the queue,
        keys' reader and every live slot-side task: the relay never waits on a reader or a slot task that is gone."""
        while True:
            for t in list(self._slot):
                if t.done():
                    self._slot.discard(t)
                    if not t.cancelled():
                        self._ended.append((t, self._decoded))
            reader = self._reader_task
            # a reader that ended (cancelled while blocked on a put) never delivers what it counted
            if self._ended and (self._ended[0][1] <= self._taken or (reader.done() and events.empty())):
                return ("body", self._ended.pop(0)[0].result())
            if not events.empty():
                self._taken += 1
                return events.get_nowait()
            if reader.done():   # anything it put before it ended was taken above
                return ("error", "reader-gone")
            get = asyncio.ensure_future(events.get())
            try:
                await asyncio.wait({get, reader, *self._slot}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                if not get.done():
                    get.cancel()
            if get.done() and not get.cancelled():
                self._taken += 1
                return get.result()
            await asyncio.gather(get, return_exceptions=True)

    async def _stop(self, task: Optional[asyncio.Future]) -> None:
        """Cancel the body writer and wait until it has stopped: it may be parked in a read of the slot, which a
        discard is about to read too."""
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _write_body(self, kw: asyncio.StreamWriter) -> object:
        """Forward the slot's body as `B` frames, then `E`. Returns how it ended: "end", "slot-eof" (the slot closed
        mid-body), ("slot-gone", the error's type) (a read on the slot failed), "body-cap" (chunked past the cap), or
        "malformed"; a write to keys that fails raises. Run only through `_slot_task`, which maps a raise, sends keys
        the `X`, and is what the relay reads the outcome from. It never writes the record: the relay does, from the
        outcome, only if the exchange ends that way."""
        cap, sent = self.route.body_cap, 0
        await self.parked.wait()
        self.parked_before_first_write = self.parked.is_set()
        await kw.drain()   # the `Q`
        while True:
            try:
                chunk = await self.body.next()
            except _SlotGone:
                return "slot-eof"
            except OSError as e:   # a read error on the SLOT (a reset mid-upload): the slot is gone
                return ("slot-gone", type(e).__name__)
            except Refused:
                return "malformed"
            if chunk is None:
                kw.write(gwwire.encode(b"E"))
                await kw.drain()
                return "end"
            sent += len(chunk)
            if sent > cap:
                return "body-cap"
            for i in range(0, len(chunk), gwwire.CHUNK):
                if self.before_body_write is not None:
                    await self.before_body_write.wait()
                kw.write(gwwire.encode(b"B", chunk[i:i + gwwire.CHUNK]))
                await kw.drain()

    async def _relay(self, kw: asyncio.StreamWriter, events: asyncio.Queue, expect: bool,
                     writer: asyncio.Future) -> None:
        """keys' events -> the slot. A write to the slot that fails ends the exchange as "slot-gone"."""
        try:
            await self._relay_events(kw, events, expect, writer)
        except OSError as e:   # the slot went away mid-answer (keys-side errors are caught where they happen)
            self._end_slot_gone(type(e).__name__)

    def _end_slot_gone(self, error: str) -> None:
        """The record of an exchange that ended because the slot failed: `end: slot-gone` and the error's type."""
        self.rec["end"] = "slot-gone"
        self.rec["slot_error"] = error

    async def _relay_events(self, kw: asyncio.StreamWriter, events: asyncio.Queue, expect: bool,
                            writer: asyncio.Future) -> None:
        started = False
        held: Optional[dict] = None   # a no-body response's `R`, until its `e`
        while True:
            kind, payload = await self._next(events)
            if kind == "body":
                if payload in _BODY_SKIP:
                    continue   # the reader's verdict decides: the F it already holds, or keys-unavailable
                if payload == "slot-eof":
                    self.rec["end"] = payload
                    return
                if isinstance(payload, tuple):   # ("slot-gone", the error's type): a read on the slot failed
                    self._end_slot_gone(payload[1])
                    return
                if not started:   # the writer has returned: nothing else reads the slot
                    await self.refuse(Refused("body-cap" if payload == "body-cap" else "malformed", "body"),
                                      discard=True)
                self.rec.setdefault("end", payload)
                return
            if kind in ("eof", "error"):
                self.rec["keys_end"] = payload or kind
                if not started:
                    await self._stop(writer)
                    await self.refuse(Refused("keys-unavailable", f"keys-{kind}"), discard=True)
                self.rec.setdefault("end", "keys-" + kind)
                return
            if kind == b"A":
                if expect:
                    self.swriter.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    await self.swriter.drain()
                continue
            if kind == b"F":
                await self._stop(writer)
                if kw.can_write_eof():
                    try:
                        kw.write_eof()   # keys' drain ends at once
                    except OSError:
                        pass
                await self.refuse(Refused(payload["refused"], "keys"), discard=True)  # type: ignore[index]
                return
            if kind == b"R":
                if self.nobody or payload["status"] in (204, 304):  # type: ignore[index]
                    # the head is the whole response: written at keys' `e`, so a keys connection that ends before it
                    # is answered keys-unavailable, never a complete response the exchange did not finish
                    held = payload
                    continue
                started = self.answered = True
                self._write_response_head(payload)  # type: ignore[arg-type]
                await self.swriter.drain()
                continue
            if kind == b"b":
                self.swriter.write(b"%x\r\n%s\r\n" % (len(payload), payload))  # type: ignore[arg-type]
                await self.swriter.drain()
                continue
            if kind == b"e":
                if held is not None:
                    started = self.answered = True
                    self._write_response_head(held)
                    await self.swriter.drain()
                if not self.nobody_response:
                    self.swriter.write(b"0\r\n\r\n")
                    await self.swriter.drain()
                self.rec["status"] = self.status
                # keys may answer before the request body has all arrived: stop forwarding it, half-close, and
                # discard the rest as a refusal does, so the close is not a reset over the response just written
                await self._stop(writer)
                if self.swriter.can_write_eof():
                    try:
                        self.swriter.write_eof()
                    except OSError:   # the slot closed after the whole response: still delivered
                        pass
                self.rec["end"] = "ok"
                if not self.body.done:
                    await self._discard()
                return
            if kind == b"U":
                await self._websocket(kw, payload, events, writer)  # type: ignore[arg-type]
                return

    def _write_response_head(self, r: dict) -> None:
        """keys' `R`: the status line and R's headers (forbidden names dropped again), re-framed chunked so the front
        never trusts a length from keys, except for a response with no body by HTTP's rules (HEAD, 204, 304)."""
        self.status = r["status"]
        self.nobody_response = self.nobody or self.status in (204, 304)
        out = [b"HTTP/1.1 %d %s\r\n" % (self.status, _reason(self.status))]
        for n, v in r["headers"]:
            if not norm.is_forbidden(n):
                out.append(b"%s: %s\r\n" % (n.encode("ascii"), v.encode("ascii")))
        if not self.nobody_response:
            out.append(b"transfer-encoding: chunked\r\n")
        out.append(b"connection: close\r\n\r\n")
        self.swriter.write(b"".join(out))

    async def _websocket(self, kw: asyncio.StreamWriter, u: dict, events: asyncio.Queue,
                         writer: asyncio.Future) -> None:
        """keys' `U`: the front writes the slot's 101 itself (`sec-websocket-accept` from the key it forwarded),
        then relays raw bytes as `D` frames both ways until either side's `E` or EOF."""
        await asyncio.gather(writer, return_exceptions=True)   # the GET's body (none) is over
        out = [b"HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\nconnection: upgrade\r\n",
               b"sec-websocket-accept: %s\r\n" % _ws_accept(self.ws_key).encode("ascii")]
        for n, v in u["headers"]:
            if n == "sec-websocket-protocol" and n in self.route.response_headers:
                out.append(b"sec-websocket-protocol: %s\r\n" % v.encode("ascii"))
        self.answered = True
        self.swriter.write(b"".join(out) + b"\r\n")
        await self.swriter.drain()
        self.rec["status"] = 101
        early = self.body.conn.trailing_data[0]

        async def up() -> object:
            """The slot's bytes -> keys as `D` frames. Returns how the slot's side ended; `_slot_task` sends keys
            the `E` and the relay below ends on it."""
            if early:
                kw.write(gwwire.encode(b"D", early))
            while True:
                try:
                    data = await self.sreader.read(gwwire.CHUNK)
                except OSError as e:   # a read error on the SLOT (a reset)
                    return ("slot-gone", type(e).__name__)
                if not data:   # the slot closed cleanly (not an error)
                    return "slot-eof"
                kw.write(gwwire.encode(b"D", data))
                await kw.drain()

        pump = self._slot_task(kw, up(), b"E")
        try:
            while True:
                kind, payload = await self._next(events)
                if kind == "body" and isinstance(payload, tuple):   # the pump: a read on the slot failed
                    self._end_slot_gone(payload[1])
                    return
                if kind == "body" and payload == "slot-eof":   # the pump: the slot closed cleanly
                    self.rec["end"] = "slot-eof"
                    return
                if kind == "body" and payload in _BODY_SKIP:   # the GET's finished body writer, reported after `U`
                    continue
                if kind != b"D":
                    self.rec["end"] = "ws-" + (kind.decode("ascii") if isinstance(kind, bytes) else kind)
                    return
                self.swriter.write(payload)  # type: ignore[arg-type]
                await self.swriter.drain()
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)

    # -- refusals ------------------------------------------------------------------------------------------------

    async def refuse(self, r: Refused, *, discard: bool = True) -> None:
        """Answer the slot with `r`'s status, half-close it, then discard what is left of its body: until the body
        ends, the slot's EOF, `body_cap` bytes READ AFTER THIS REFUSAL (counted from zero here), or DISCARD_S."""
        self.rec.update({"refused": r.name, "detail": r.detail, "status": r.status})
        self.answered = True
        body = r.name.encode() + b"\n"
        try:
            self.swriter.write(b"HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n"
                               b"X-Levain-Refused: %s\r\nConnection: close\r\n\r\n%s"
                               % (r.status, REASON[r.status], len(body), r.name.encode(), body))
            await self.swriter.drain()
        except OSError as e:
            self._end_slot_gone(type(e).__name__)
            return
        if self.swriter.can_write_eof():
            try:
                self.swriter.write_eof()
            except OSError:   # the slot closed after the whole refusal: still delivered
                pass
        if discard:
            await self._discard()

    async def _discard(self) -> None:
        """Read and drop the slot's remaining body after the answer was written: until the body ends, the slot's EOF,
        `body_cap` bytes read here (counted from zero, each read bounded by what is left), or DISCARD_S."""
        loop = asyncio.get_running_loop()
        deadline, read, cap = loop.time() + DISCARD_S, 0, self.route.body_cap
        try:
            while read < cap:
                left = deadline - loop.time()
                if left <= 0:
                    break
                chunk = await asyncio.wait_for(self.body.next(cap - read), left)
                if chunk is None:
                    break
                read += len(chunk)
        except (asyncio.TimeoutError, OSError, _SlotGone, Refused):
            pass
        self.rec["discarded"] = read


def _reason(status: int) -> bytes:
    try:
        return HTTPStatus(status).phrase.encode("ascii")
    except ValueError:
        return b"Unknown"


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
                 connect: Connector = open_by_address, keys_path: Optional[str] = None,
                 log_cap: int = LOG_CAP) -> None:
        self.policy = policy
        #: keys' `R/keys/front.sock`: every brokered exchange is one connection to it. None = no keys (503).
        self.keys_path = keys_path
        self.log_path = Path(log_path) if log_path is not None else None
        self.log_cap = log_cap
        self.slot_uid = slot_uid
        self._resolve = resolve
        self._connect = connect
        #: every open stream, client and upstream alike: closed at shutdown.
        self.live: set[asyncio.StreamWriter] = set()
        #: client connections being served: the MAX_CONNS count.
        self.clients = 0
        self.bound: Optional[tuple[str, int]] = None

    def _log(self, rec: dict) -> None:
        head = {"at": round(time.time(), 3), "session": self.policy.session}
        line = json.dumps({**head, **rec}, sort_keys=True) + "\n"
        try:
            if self.log_path is None:
                sys.stderr.write(line)
                return
            data = line.encode("utf-8")
            if len(data) > self.log_cap:      # one record past the whole cap would break the two-cap bound
                data = (json.dumps({**head, "event": "log-record-dropped", "bytes": len(data)}, sort_keys=True)
                        + "\n").encode("utf-8")
            self._append(self.log_path, data)
        except OSError as e:
            print(f"levain gw-front: log write failed ({e}): {line}", file=sys.stderr, end="")

    def _append(self, path: Path, data: bytes) -> None:
        """Append ``data`` to the log: opened ``O_NOFOLLOW`` (a link there is an error, never followed), a regular
        file only, set to LOG_MODE. When ``data`` would take a non-empty log past the cap, the log is first renamed
        to ``<log>.1`` (replacing the previous one) and a new log made. The front is the log's only writer."""
        fd = self._open_log(path)
        try:
            size = os.fstat(fd).st_size
            if size and size + len(data) > self.log_cap:
                os.close(fd)
                fd = -1
                os.replace(path, path.with_name(path.name + ".1"))
                fd = self._open_log(path)
            while data:
                data = data[os.write(fd, data):]
        finally:
            if fd >= 0:
                os.close(fd)

    @staticmethod
    def _open_log(path: Path) -> int:
        fd = os.open(path, _LOG_OPEN, LOG_MODE)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.EINVAL, "the log is not a regular file", str(path))
            if stat.S_IMODE(st.st_mode) != LOG_MODE:
                os.fchmod(fd, LOG_MODE)
        except BaseException:
            os.close(fd)
            raise
        return fd

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
        target: Optional[bytes] = None
        try:
            conn, req = parse_head_conn(head)
            if req.method != b"CONNECT":
                if not req.target.startswith(b"/r/"):
                    raise Refused("template-miss", "not-a-route-target")
                route_id, path, query = split_route_target(req.target)
                pol = self.policy.brokered.get(route_id)
                if pol is None:
                    raise Refused("route-not-allowed")
                await BrokeredExchange(self, pol, conn, req, path, query, reader, writer).run()
                return
            target = req.target
            route, port = connect_target(self.policy, target)
            ureader, uwriter, addr = await self._open_upstream(route, port)
        except Refused as r:
            await self._refuse(reader, writer, r, target)
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

    async def _refuse(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, r: Refused,
                      target: Optional[bytes] = None) -> None:
        """Answer ``r`` and log it; a refused CONNECT's line also names its host (:func:`refused_host_fields`), and an
        off-list one the remedy (:data:`OFF_LIST_REMEDY`)."""
        self._log({"event": "refused", "refused": r.name, "detail": r.detail,
                   **(refused_host_fields(target) if target is not None else {}),
                   **({"remedy": OFF_LIST_REMEDY} if (r.name, r.detail) == ("host-not-allowed", "off-list") else {})})
        body = r.name.encode() + b"\n"
        writer.write(b"HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n"
                     b"X-Levain-Refused: %s\r\nConnection: close\r\n\r\n%s"
                     % (r.status, REASON[r.status], len(body), r.name.encode(), body))
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


async def run_front(front: Front, *, unix: Optional[str] = None, port: Optional[int] = None,
                    keys: Optional[str] = None) -> int:
    """Open the STATUS connection first and bind ONLY after keys' `A` (else return 3, never having bound); then serve
    until SIGTERM / SIGINT, or keys' `K` / EOF on STATUS, then close every stream and the listener and return 0. The
    keys socket has ONE source: `keys` here, else the front's own `keys_path`; the front's brokered exchanges use the
    same path, and two different paths are refused. A front with no keys socket, or a policy with no incarnation, is
    refused before anything is bound: with no STATUS connection no `K` could ever end it."""
    if keys is None:
        keys = front.keys_path
    elif front.keys_path is not None and front.keys_path != keys:
        raise ValueError(f"run_front(keys={keys!r}) and Front(keys_path={front.keys_path!r}) disagree")
    if keys is None or front.policy.incarnation is None:
        raise ValueError("run_front serves only with keys' socket and the session's incarnation (STATUS)")
    front.keys_path = keys
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        frames, kw = await open_status(keys, front.policy.session, front.policy.incarnation)
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, WireError, ConnectionError) as e:
        print(f"levain gw-front: keys did not accept the STATUS hello ({type(e).__name__}); not serving",
              file=sys.stderr)
        return 3
    front.live.add(kw)

    async def status_watch() -> None:
        try:
            await frames.read(frozenset({b"K"}))   # K, EOF, or anything else: keys is gone for this front
        except (OSError, asyncio.IncompleteReadError, WireError):
            pass
        finally:   # every exit ends the front, an unforeseen raise included; shutdown's own cancel finds it set
            if not stop.is_set():
                front._log({"event": "status-end"})
                stop.set()

    watch = asyncio.ensure_future(status_watch())
    server = await (front.serve_unix(unix) if unix is not None else front.serve_tcp(port))  # type: ignore[arg-type]
    try:
        await stop.wait()
    finally:
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)   # retrieves an unforeseen raise that ended the front
        server.close()
        front.close_live()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        if unix is not None:
            front.unbind()
    return 0


def build_parser():
    """The front's argv grammar (root's ``runtime_helper.gateway_front_args`` builds the one it is started with)."""
    import argparse

    ap = argparse.ArgumentParser(prog="python -m levain.broker.front")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--unix", help="socket path, the Linux shape (R/gw/slot<n>/gw.sock); needs --slot-uid")
    g.add_argument("--port", type=int, help=f"TCP port on 127.0.0.1, the macOS shape ({GWPORT_BASE} + slot)")
    ap.add_argument("--slot-uid", type=int, help="the only peer uid the unix shape serves")
    ap.add_argument("--policy", required=True, help="the front's policy file (R/gw/slot<n>/session.json)")
    ap.add_argument("--log", help="JSON-lines tunnel log (default: stderr)")
    # Both are required: a front with no STATUS connection could never be revoked (no `K` reaches it), and one with
    # no GO pipe would serve before render's interlock. The standalone shape is in-process construction only.
    ap.add_argument("--go-fd", type=int, required=True,
                    help="render's GO pipe: serve only after one byte arrives on it; EOF = exit without serving "
                         "(CONTRACT §5.1 render (4)-(5b))")
    ap.add_argument("--keys", required=True,
                    help="keys' R/keys/front.sock: the STATUS connection is opened after GO and the listener bound "
                         "only after keys' A; brokered exchanges go there")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    ap = build_parser()
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
    if policy.incarnation is None:
        print("levain gw-front: policy refused: --keys needs the session's incarnation", file=sys.stderr)
        return 2
    # The policy is read first: render is to write it before it starts the front (CONTRACT §4.3). Nothing is bound
    # until GO.
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
    front = Front(policy, log_path=a.log, slot_uid=a.slot_uid, keys_path=a.keys)
    return asyncio.run(run_front(front, unix=a.unix, port=a.port, keys=a.keys))


if __name__ == "__main__":
    raise SystemExit(main())
