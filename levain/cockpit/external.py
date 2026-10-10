"""levain.cockpit.external: a server's ``extra_panels`` callable as manifest panels (design §6.2).

A downstream (flow's Bridge) hands ``make_server`` a zero-arg callable returning legacy external-panel
dicts. Here each one becomes a manifest panel: ``lines`` -> a triage-list whose rows carry the legacy
``meta`` as the ``legacy_meta`` facet, ``markdown`` -> a prose panel, ``note``/``empty``/``error`` -> the
panel head. The ``action`` half waits for K2a: the manifest source is read-only.

The callable may do network reads (flow's inbox is HTTP to argushub), so it is called ONCE per refresh,
not once per panel: concurrent callers join the running call, and a finished call is reused for
``reuse_s`` so the panel reads that follow a manifest read share it. A reused read carries its call's own
``as_of``. The callable is operator-registered, in-process code and trusted not to be adversarial (the
engine's trust assumption); its OUTPUT is still checked shape by shape: a malformed panel or line is
skipped or counted, never a crash.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from levain.cockpit.engine import ZONES, ProviderSpec, ReadContext
from levain.cockpit.results import Fault, Read, Result, RowIn

ID_PREFIX = "ext:"
RANK_BASE = 100          # after the kernel's own panels in a zone, in the callable's order
_ZONE_IDS = frozenset(z for z, _ in ZONES)


@dataclass(frozen=True)
class _Call:
    panels: tuple[dict[str, Any], ...] | None    # None when the call failed
    error: str | None
    as_of: str


class _Flight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: _Call | None = None


class ExternalPanels:
    """Single-flight, briefly reused calls of one ``extra_panels`` callable."""

    def __init__(self, fn: Callable[[], Any], *, reuse_s: float = 5.0, wait_s: float = 8.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._fn = fn
        self._reuse_s = reuse_s
        self._wait_s = wait_s
        self._clock = clock
        self._lock = threading.Lock()
        self._flight: _Flight | None = None
        self._last: tuple[float, _Call] | None = None
        self.calls = 0           # how many times the callable ran (the burst measurement reads it)

    def take(self) -> _Call:
        with self._lock:
            if self._last is not None and self._clock() - self._last[0] < self._reuse_s:
                return self._last[1]
            flight = self._flight
            if flight is None:
                flight = self._flight = _Flight()
                self.calls += 1
                threading.Thread(target=self._run, args=(flight,), name="levain-external-panels",
                                 daemon=True).start()
        if not flight.done.wait(self._wait_s) or flight.result is None:
            return _Call(None, f"the external panels did not answer within {self._wait_s:g} s", _now_iso())
        return flight.result

    def _run(self, flight: _Flight) -> None:
        try:
            got = self._fn()
            if not isinstance(got, (list, tuple)):
                raise TypeError(f"extra_panels returned a {type(got).__name__}, not a list")
            call = _Call(tuple(got), None, _now_iso())
        except BaseException as exc:  # noqa: BLE001 - every failure is a reading, reused like one
            call = _Call(None, f"{type(exc).__name__}: {exc}", _now_iso())
        with self._lock:
            flight.result = call
            self._last = (self._clock(), call)
            self._flight = None
        flight.done.set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _str(v: Any, default: str = "") -> str:
    return v if isinstance(v, str) else default


def _usable(panels: tuple[dict[str, Any], ...]) -> list[tuple[str, dict[str, Any]]]:
    """The adaptable panels, in order: a dict with a non-empty string id; of two with one id, the first."""
    out: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for p in panels:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if not isinstance(pid, str) or not pid or pid in seen:
            continue
        seen.add(pid)
        out.append((pid, p))
    return out


_TITLE_CLAUSE = re.compile(r"^(.*\S)\s*\(([^()]*)\)$")


def _title(p: dict[str, Any], pid: str) -> tuple[str, str]:
    """A legacy title carries its live figures in a trailing parenthetical ("Inbox (3 unread)"). A manifest
    title is static and the kernel appends the row count, so the clause moves to the read's note; a bare
    number is that count already and is dropped."""
    t = _str(p.get("title"), pid)
    m = _TITLE_CLAUSE.match(t)
    if not m:
        return t, ""
    return m.group(1), "" if m.group(2).strip().isdigit() else m.group(2).strip()


def _is_prose(p: dict[str, Any]) -> bool:
    return isinstance(p.get("markdown"), str)


def _rows(p: dict[str, Any]) -> tuple[tuple[RowIn, ...], int]:
    rows: list[RowIn] = []
    bad = 0
    lines = p.get("lines")
    for i, ln in enumerate(lines if isinstance(lines, list) else []):
        if not isinstance(ln, dict) or not isinstance(ln.get("text"), str):
            bad += 1
            continue
        meta = _str(ln.get("meta"))
        emphasis = "accent" if ln.get("accent") is True else ("dim" if ln.get("dim") is True else "none")
        rows.append(RowIn(f"line-{i}", ln["text"], {"legacy_meta": meta} if meta else {}, emphasis=emphasis,
                          stored={"meta": meta, "text": ln["text"], "emphasis": emphasis}))
    return tuple(rows), bad


def _reader(ext: ExternalPanels, pid: str, prose: bool) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        call = ext.take()
        if call.panels is None:
            return Fault(call.error or "the external panels failed")
        p = dict(_usable(call.panels)).get(pid)
        if p is None:
            return Fault("the external source no longer offers this panel")
        if p.get("error"):
            return Fault(_str(p.get("error"), "the external panel reported an error"))
        if _is_prose(p) != prose:
            return Fault("the external panel changed between markdown and lines; restart the server")
        clause, note = _title(p, pid)[1], _str(p.get("note"))
        if clause and clause not in note:    # a source whose note already carries its title's figures says it once
            note = f"{clause} · {note}" if note else clause
        if prose:
            return Read(value={"markdown": p["markdown"]}, as_of=call.as_of, note=note)
        rows, bad = _rows(p)
        return Read(rows=rows, skipped=((bad, "a line that is not {meta, text}"),) if bad else (),
                    as_of=call.as_of, note=note)
    return read


def discoverer(ext: ExternalPanels) -> Callable[[ReadContext], list[ProviderSpec]]:
    """A ``Cockpit.discover`` function: one manifest panel per usable external panel."""
    def discover(ctx: ReadContext) -> list[ProviderSpec]:
        call = ext.take()
        if call.panels is None:
            raise RuntimeError(call.error or "the external panels failed")
        specs: list[ProviderSpec] = []
        for i, (pid, p) in enumerate(_usable(call.panels)):
            prose = _is_prose(p)
            zone = p.get("zone")
            # an action panel's empty sentence may point at its compose box, which this read-only source does
            # not draw, so it takes the kernel's default sentence
            empty = "" if p.get("action") else _str(p.get("empty"))
            region = zone if isinstance(zone, str) and zone in _ZONE_IDS else "operate"
            if prose:
                specs.append(ProviderSpec(ID_PREFIX + pid, "prose", _title(p, pid)[0], "feed",
                                          _reader(ext, pid, True), region=region, rank=RANK_BASE + i,
                                          empty=empty))
            else:
                specs.append(ProviderSpec(ID_PREFIX + pid, "triage-list", _title(p, pid)[0], "feed",
                                          _reader(ext, pid, False), order="legacy.source",
                                          facets=frozenset({"legacy_meta"}), version_fields=("*",),
                                          region=region, rank=RANK_BASE + i, empty=empty))
        return specs
    return discover
