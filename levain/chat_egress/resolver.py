"""The decider's own resolve (design §3.8.5(o) C-3, rows R15-10-A4 and R15-12-B15; CH-D2 S5b).

The decider resolves a request's host BEFORE any card, so a non-read to a name that answers a non-public address
walls `non_public_address` and files no card (R15-10-A4); srt's resolved-address guard checks again at dial (the
generated table, `guard.py`), so a rebinding between the two meets that guard. This module is the first half only:
the lookup. `guard.address_wall` judges the answers.

An IP literal is its own answer (no lookup): `guard.address_wall` judges it as it is. A DNS name is looked up by the
system resolver's `getaddrinfo` on the ABSOLUTE name (`host.`), as `levain.broker.front`'s `system_resolve` does: a
dotless query would let the search list turn an allowed name into a name under another domain (the dot is for names
only: macOS's getaddrinfo refuses `2606:4700:4700::1111.`). It is synchronous (the decider is threaded per request),
and getaddrinfo cannot be cancelled, so the pool is bounded by SLOTS, not by threads that finish on time:
  * at most `max_lookups` lookups of one resolver exist at once, and at most MAX_LOOKUPS in the process (one slot set
    every resolver shares, so lookups that still hang after their launch closed count against the next launch);
    each lookup is on its own daemon thread; a lookup that hangs keeps its slots until the resolver returns, and its
    late answer is dropped;
  * a resolve waits for a slot at most `timeout_s`, and for its answer at most what is left of the same `timeout_s`;
    either wait running out raises `ResolveTimeout` (the request is denied), whatever the other lookups do;
  * no answer (an error, or no address) raises `ResolveFailed`.
So a hung name denies the requests that wait on it and never an unbounded thread count, and a request to another
name completes while a slot is free.

`lookup` is the seam: `(host, port) -> list[str]` of address strings. Production: `system_lookup`. The tests and the
rig pass a map (a CH-D2 test seam; S8's launch passes none).
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from typing import Callable, Sequence

#: [seat] `front`'s RESOLVE_TIMEOUT_S (design §3.8.5(a)'s cap table): one resolve's whole budget.
RESOLVE_TIMEOUT_S = 5.0
#: [seat] at most this many lookups at once, in one resolver and in the process (each one daemon thread).
MAX_LOOKUPS = 16
_PROCESS_SLOTS = threading.BoundedSemaphore(MAX_LOOKUPS)

Lookup = Callable[[str, int], Sequence[str]]


class ResolveError(Exception):
    """No usable answer for a request's host: the decider denies the request."""
    reason = "resolve_failed"


class ResolveFailed(ResolveError):
    reason = "resolve_failed"


class ResolveTimeout(ResolveError):
    reason = "resolve_timeout"


def _literal(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def system_lookup(host: str, port: int) -> list[str]:
    """getaddrinfo on the absolute name of a DNS name (`Resolver.resolve` answers an IP literal itself and never
    calls the lookup with one); the de-duplicated address strings, in the resolver's order."""
    infos = socket.getaddrinfo(host + ".", port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    out: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in out:
            out.append(addr)
    return out


class Resolver:
    """The bounded pool (module docstring). Thread-safe; one per decider service."""

    def __init__(self, lookup: Lookup = system_lookup, *, max_lookups: int = MAX_LOOKUPS,
                 timeout_s: float = RESOLVE_TIMEOUT_S) -> None:
        if type(max_lookups) is not int or max_lookups < 1:
            raise ValueError("max_lookups is a positive int")
        self._lookup = lookup
        self._timeout = float(timeout_s)
        self._slots = threading.BoundedSemaphore(max_lookups)
        self.max_lookups = max_lookups

    def resolve(self, host: str, port: int) -> list[str]:
        literal = _literal(host)
        if literal is not None:
            return [literal]
        deadline = time.monotonic() + self._timeout
        if not self._slots.acquire(timeout=self._timeout):
            raise ResolveTimeout("every resolver slot is busy")
        if not _PROCESS_SLOTS.acquire(timeout=max(0.0, deadline - time.monotonic())):
            self._slots.release()
            raise ResolveTimeout("every process resolver slot is busy")
        done = threading.Event()
        box: dict = {}

        def work() -> None:
            try:
                box["answers"] = list(self._lookup(host, port))
            except BaseException as exc:     # noqa: BLE001: any failure is "no answer"
                box["error"] = exc
            finally:
                _PROCESS_SLOTS.release()     # the slots are held until the lookup itself returns
                self._slots.release()
                done.set()

        try:
            threading.Thread(target=work, name="levain-egress-resolve", daemon=True).start()
        except BaseException:
            _PROCESS_SLOTS.release()
            self._slots.release()
            raise
        if not done.wait(max(0.0, deadline - time.monotonic())):
            raise ResolveTimeout("the resolver did not answer in time")
        if "error" in box:
            raise ResolveFailed(f"the name did not resolve ({type(box['error']).__name__})")
        answers = [a for a in box["answers"] if isinstance(a, str) and a]
        if not answers:
            raise ResolveFailed("the name resolved to no address")
        return answers
