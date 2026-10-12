"""levain.autonomic.handle — the GateHandle: one reference to the current :class:`EfferentGate`.

A gate's policy (its manifest and its binding risk resolver) is frozen for the gate's lifetime, so a
policy change is a NEW gate. The handle is how a process changes gates without a restart: every consumer
of a gate (``FireDispatcher``, ``ChainExecutor``, the sweeps) holds the handle and reads
:meth:`GateHandle.current` ONCE per admission, passing that gate down, so an admission runs wholly on the
gate it captured: the old one or the new one, never a mix of the two.

A swap changes POLICY, never STATE. :meth:`GateHandle.swap` builds the new gate with
:meth:`EfferentGate.with_policy`, so the new gate shares the old one's stateful collaborators by
reference and pending holds, receipts and the journal stay one store across the swap. Signers are never a swap
argument: an authenticator change must not ride a policy swap.

What a swap may change is not the handle's to judge (tightening-only, or who may sign a policy, is the
standing policy's); a raw :meth:`swap` is ungoverned and must only be driven by that caller.

Stdlib-only.
"""
from __future__ import annotations

import datetime as _dt
import threading
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Any

from levain.autonomic.binding import Binding
from levain.autonomic.gate import EfferentGate, GateOutcome
from levain.autonomic.risk import ActionManifest, ActionRisk

__all__ = ["GateHandle", "Aggregate", "as_handle"]

# (the sealed tools of one link, the frozen tool catalog) -> that link's risk. The same aggregation a
# binding was ratified at; the handle supplies the catalog, so the function cannot read a live one.
Aggregate = Callable[[Iterable[Any], Mapping[str, ActionRisk]], ActionRisk]


class GateHandle:
    """The current gate of a process. :meth:`current` is a plain attribute read (no lock on the admission
    path; a single reference store is atomic); :meth:`swap` builds a new gate in full under a lock and
    publishes it with ONE assignment. A handle built with ``swappable=False`` wraps a gate that never
    changes: what a consumer given a bare gate holds."""

    def __init__(self, gate: EfferentGate, *, swappable: bool = True) -> None:
        if not isinstance(gate, EfferentGate):
            raise TypeError(f"GateHandle: needs an EfferentGate, got {type(gate).__name__}")
        self._gate = gate
        self._swappable = swappable
        self._swap_lock = threading.Lock()

    @property
    def swappable(self) -> bool:
        return self._swappable

    def current(self) -> EfferentGate:
        """The gate to run ONE admission on. Read it once at the admission's start and pass the result
        down; reading it again mid-admission is how a decision could mix two policies."""
        return self._gate

    def swap(self, manifest: ActionManifest, binding_catalog: Mapping[str, ActionRisk],
             aggregate: Aggregate) -> EfferentGate:
        """Publish a new gate on ``manifest`` whose binding risk resolver is ``aggregate`` over a frozen
        snapshot of ``binding_catalog``; returns it. The catalog is copied before the gate is built, so a
        later change to the caller's mapping reaches no admission. A callable in place of the catalog (a
        resolver of the caller's own, which could read anything at admission time) is refused.

        The returned gate is for inspection only: a caller that keeps it and admits on it goes stale at
        the next swap. Admissions read :meth:`current`."""
        if not self._swappable:
            raise RuntimeError("GateHandle: this handle wraps a fixed gate and never swaps")
        if not isinstance(manifest, ActionManifest):
            raise TypeError(f"GateHandle.swap: manifest must be an ActionManifest, got {type(manifest).__name__}")
        if callable(binding_catalog) or not isinstance(binding_catalog, Mapping):
            raise TypeError("GateHandle.swap: binding_catalog must be a Mapping of tool -> ActionRisk, never a "
                            "resolver; the handle builds the resolver over a frozen snapshot of it")
        if not callable(aggregate):
            raise TypeError("GateHandle.swap: aggregate must be callable")
        catalog = MappingProxyType(dict(binding_catalog))
        for tool, risk in catalog.items():
            if not isinstance(tool, str) or not isinstance(risk, ActionRisk):
                raise TypeError(f"GateHandle.swap: binding_catalog entry {tool!r} is not str -> ActionRisk")

        def binding_risk(binding: Binding, i: int) -> ActionRisk:
            return aggregate((binding.goal[i],), catalog)

        with self._swap_lock:
            new = self._gate.with_policy(manifest, binding_risk)
            self._gate = new   # the publish: one reference store, after the gate is fully built
        return new

    def sweep_timeouts(self, now: _dt.datetime | None = None) -> list[GateOutcome]:
        """The silence default for every expired pending (``EfferentGate._sweep``), each decided on the
        gate current for it: the handle is read once per pending, and that pending's silence decision and
        its resolve run on that one gate. This is the only sweep; a gate has none of its own, because a
        scheduler that held a gate would decide pendings proposed under one policy on a policy since
        superseded (a bare gate: ``as_handle(gate).sweep_timeouts(now)``). A chain link's hold is left to
        ``ChainExecutor.sweep_timeouts``."""
        return self.current()._sweep(now, self.current)


def as_handle(gate: EfferentGate | GateHandle) -> GateHandle:
    """``gate`` as a handle: a handle is returned as is, a bare gate is wrapped in one that never swaps."""
    if isinstance(gate, GateHandle):
        return gate
    return GateHandle(gate, swappable=False)
