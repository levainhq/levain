"""levain.cockpit.external: a server's ``extra_panels`` callable as manifest panels (design §6.2).

A downstream (flow's Bridge) hands ``make_server`` a zero-arg callable returning legacy external-panel
dicts. Here each one becomes a manifest panel: ``lines`` -> a triage-list whose rows carry the legacy
``meta`` as the ``legacy_meta`` facet, ``markdown`` -> a prose panel, ``note``/``empty``/``error`` -> the
panel head. A panel's ``action`` verb is the one verb it offers (``ProviderSpec.verbs``, K2a).

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
    def __init__(self, deadline: float) -> None:
        self.done = threading.Event()
        self.result: _Call | None = None
        self.deadline = deadline       # on the instance's clock: a flight is abandoned once past it
        self.timeout: _Call | None = None   # its own timeout reading, set once when it is abandoned


class ExternalPanels:
    """Single-flight, briefly reused calls of one ``extra_panels`` callable.

    A flight carries its own deadline (``wait_s`` from its start), so every reader of one burst waits on the
    same instant. Past it the flight is abandoned: a timeout is recorded and reused like any other reading,
    so the burst's later readers return at once, and the next reuse window starts a fresh flight. A flight
    has one verdict for all its readers and the reuse: its FIRST transition under the lock, either the
    worker's answer or a reader's timeout once that reader's deadline wait ran out; the second is dropped
    (java.util.concurrent.FutureTask's rule: set() and cancel() race on one CAS from NEW, no clock). A
    callable that hangs therefore parks its thread; at most ``max_parked`` such threads live at once, and while that
    many are parked no new flight starts (the timeout reading stands). ``clock`` is a monotonic clock that
    does not raise (``time.monotonic``; tests inject a fake); a raising clock is outside this contract."""

    def __init__(self, fn: Callable[[], Any], *, reuse_s: float = 5.0, wait_s: float = 8.0,
                 max_parked: int = 2, clock: Callable[[], float] = time.monotonic) -> None:
        self._fn = fn
        self._reuse_s = reuse_s
        self._wait_s = wait_s
        self._max_parked = max_parked
        self._clock = clock
        self._lock = threading.Lock()
        self._flight: _Flight | None = None
        self._parked: set[_Flight] = set()
        self._last: tuple[float, _Call] | None = None
        self.calls = 0           # how many times the callable ran (the burst measurement reads it)

    def take(self, *, fresh: bool = False) -> _Call:
        """``fresh`` (a write's read): never the reused answer and never a flight already running,
        which may have started before the source changed. Its own flight, not shared, under the same
        parked bound; a fresh flight that times out is parked like any other."""
        with self._lock:
            now = self._clock()
            if fresh:
                flight = None
            else:
                if self._last is not None and now - self._last[0] < self._reuse_s:
                    return self._last[1]
                flight = self._flight
                if flight is not None and now >= flight.deadline:
                    return self._abandon_locked(flight, now)
            if flight is None:
                if len(self._parked) >= self._max_parked:
                    return self._record_locked(_Call(None, f"{len(self._parked)} earlier calls of the external "
                                                     "panels have not returned; not starting another", _now_iso()), now)
                flight = _Flight(now + self._wait_s)
                try:
                    threading.Thread(target=self._run, args=(flight,), name="levain-external-panels",
                                     daemon=True).start()
                except RuntimeError as exc:      # "can't start new thread": nothing is left waiting on it
                    failed = _Call(None, f"could not start the external panels call: {exc}", _now_iso())
                    return failed if fresh else self._record_locked(failed, now)
                if not fresh:
                    self._flight = flight
                self.calls += 1
        flight.done.wait(max(0.0, flight.deadline - self._clock()))
        with self._lock:
            return self._abandon_locked(flight, self._clock())

    def _record_locked(self, call: _Call, now: float) -> _Call:
        self._last = (now, call)
        return call

    def _abandon_locked(self, flight: _Flight, now: float) -> _Call:
        if flight.result is not None:
            return flight.result      # the answer was the first transition
        if flight.timeout is None:
            flight.timeout = _Call(None, f"the external panels did not answer within {self._wait_s:g} s", _now_iso())
            if self._flight is flight:
                self._flight = None
                self._record_locked(flight.timeout, now)
            self._parked.add(flight)     # a fresh flight is no one's current one, and is bounded too
        return flight.timeout     # every reader of an abandoned flight gets that flight's own reading

    def _run(self, flight: _Flight) -> None:
        call = _Call(None, "the external panels call ended without an answer", _now_iso())
        try:
            got = self._fn()
            if not isinstance(got, (list, tuple)):
                raise TypeError(f"extra_panels returned a {type(got).__name__}, not a list")
            call = _Call(tuple(got), None, _now_iso())
        except BaseException as exc:  # noqa: BLE001 - every failure is a reading, reused like one
            call = _Call(None, _describe(exc), _now_iso())
        finally:
            with self._lock:
                if flight.timeout is None:
                    flight.result = call      # the first transition; after a timeout the answer is dropped
                self._parked.discard(flight)
                # golang.org/x/sync/singleflight's rule: only the current (unforgotten) call may touch
                # the shared state
                if self._flight is flight:
                    self._flight = None
                    self._last = (self._clock(), call)
            flight.done.set()


def _describe(exc: BaseException) -> str:
    try:
        return f"{type(exc).__name__}: {exc}"
    except BaseException:  # noqa: BLE001 - an exception whose text itself raises still names its type
        return type(exc).__name__


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _str(v: Any, default: str = "") -> str:
    return v if isinstance(v, str) else default


_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
NOTE_MAX = 500          # a note is one line on the panel head; a source's runaway string is cut, never shipped whole
META_MAX = 200


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _usable(panels: tuple[dict[str, Any], ...]) -> list[tuple[str, dict[str, Any]]]:
    """The adaptable panels, in order: a dict whose id is 1-64 of [A-Za-z0-9._-] (it becomes a URL path
    segment); of two with one id, the first."""
    out: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for p in panels:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if not isinstance(pid, str) or not _ID.fullmatch(pid) or pid in seen:
            continue
        seen.add(pid)
        out.append((pid, p))
    return out


_DECORATION = re.compile(r"\s+(?:\(|[—–]\s)")


def _title(p: dict[str, Any], pid: str, n_lines: int | None) -> tuple[str, str]:
    """(static title, live clause). A legacy title carries live figures after its name, in parentheses
    ("Inbox (3 unread)") or after a dash ("Stream — stale"). A manifest title is static (the kernel appends
    the row count), so the name is the title and the rest is said in the read's note. A bare number that
    equals the rows drawn is the kernel's count already; any other number is kept."""
    t = _str(p.get("title")) or pid
    m = _DECORATION.search(t)
    if not m:
        return t, ""
    name, rest = t[: m.start()], t[m.start():].strip()
    rest = rest[1:-1].strip() if rest.startswith("(") and rest.endswith(")") else rest.lstrip("—–").strip()
    if rest.isdigit() and n_lines is not None and int(rest) == n_lines:
        rest = ""
    return name, rest


def _note(clause: str, note: str) -> str:
    """The read's note: the title's live clause first, unless the source's note already has it as one of its
    own ` · ` segments (a substring is not enough: "3 unread" is not "13 unread")."""
    if clause and clause not in {seg.strip() for seg in note.split("·")}:
        note = f"{clause} · {note}" if note else clause
    return _cut(note, NOTE_MAX)


def _is_prose(p: dict[str, Any]) -> bool:
    return isinstance(p.get("markdown"), str)


def _n_lines(p: dict[str, Any]) -> int:
    lines = p.get("lines")
    return len(lines) if isinstance(lines, (list, tuple)) else 0


def _empty(p: dict[str, Any]) -> str:
    # an action panel's empty sentence may point at its compose box, which this read-only source does not
    # draw, so it takes the kernel's default sentence
    return "" if p.get("action") else _str(p.get("empty"))


def _rows(p: dict[str, Any]) -> tuple[tuple[RowIn, ...], int]:
    """Row ids are positions: the legacy shape has no ids, and nothing writes through these rows (the
    manifest source is read-only). A verb on them needs a source id first (K2a)."""
    rows: list[RowIn] = []
    bad = 0
    lines = p.get("lines")
    for i, ln in enumerate(lines if isinstance(lines, (list, tuple)) else []):
        if not isinstance(ln, dict) or not isinstance(ln.get("text"), str):
            bad += 1
            continue
        meta = _cut(_str(ln.get("meta")), META_MAX)
        emphasis = "accent" if ln.get("accent") is True else ("dim" if ln.get("dim") is True else "none")
        rows.append(RowIn(f"line-{i}", ln["text"], {"legacy_meta": meta} if meta else {}, emphasis=emphasis,
                          stored={"meta": meta, "text": ln["text"], "emphasis": emphasis}))
    return tuple(rows), bad


def _take(ext: ExternalPanels, ctx: ReadContext) -> _Call:
    """One call per read cycle: a manifest build's discovery and every panel it reads share the cycle's
    answer even if the build outlives the reuse window."""
    got: _Call = ctx.memo(f"levain.external:{id(ext)}", lambda: ext.take(fresh=ctx.fresh))
    return got


def _reader(ext: ExternalPanels, pid: str, prose: bool) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        call = _take(ext, ctx)
        if call.panels is None:
            return Fault(call.error or "the external panels failed")
        p = dict(_usable(call.panels)).get(pid)
        if p is None:
            return Fault("the external source no longer offers this panel")
        if p.get("error"):
            return Fault(_str(p.get("error"), "the external panel reported an error"))
        if _is_prose(p) != prose:
            return Fault("the external panel changed between markdown and lines; restart the server")
        if prose:
            note = _note(_title(p, pid, None)[1], _str(p.get("note")))
            return Read(value={"markdown": p["markdown"]}, as_of=call.as_of, note=note, empty=_empty(p))
        lines = p.get("lines")
        if lines is not None and not isinstance(lines, (list, tuple)):   # absent or None is no lines, as the legacy renderer reads it
            return Fault("the external panel's lines are not a list")
        rows, bad = _rows(p)
        note = _note(_title(p, pid, len(rows))[1], _str(p.get("note")))
        return Read(rows=rows, skipped=((bad, "a line that is not {meta, text}"),) if bad else (),
                    as_of=call.as_of, note=note, empty=_empty(p))
    return read


def discoverer(ext: ExternalPanels) -> Callable[[ReadContext], list[ProviderSpec]]:
    """A ``Cockpit.discover`` function: one manifest panel per usable external panel. What the source writes
    per read (the title's live clause, the note, the empty sentence) is read per read; only the name, zone,
    kind and rank are fixed here."""
    def discover(ctx: ReadContext) -> list[ProviderSpec]:
        call = _take(ext, ctx)
        if call.panels is None:
            raise RuntimeError(call.error or "the external panels failed")
        specs: list[ProviderSpec] = []
        for i, (pid, p) in enumerate(_usable(call.panels)):
            act = p.get("action")
            # the verb a panel's compose box fires is the one verb it offers (K2a, design §4.1)
            offers = (act["verb"],) if isinstance(act, dict) and isinstance(act.get("verb"), str) and act["verb"] else ()
            zone = p.get("zone")
            region = zone if isinstance(zone, str) and zone in _ZONE_IDS else "operate"
            name = _title(p, pid, _n_lines(p))[0]
            if _is_prose(p):
                specs.append(ProviderSpec(ID_PREFIX + pid, "prose", name, "feed", _reader(ext, pid, True),
                                          region=region, rank=RANK_BASE + i, empty=_empty(p), verbs=offers))
            else:
                specs.append(ProviderSpec(ID_PREFIX + pid, "triage-list", name, "feed", _reader(ext, pid, False),
                                          order="legacy.source", facets=frozenset({"legacy_meta"}),
                                          version_fields=("*",), region=region, rank=RANK_BASE + i,
                                          empty=_empty(p), verbs=offers))
        return specs
    return discover
