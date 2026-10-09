"""Raw access to the autonomic store's database, for tests that must put the store into states no
public verb produces (a tampered record, a record that is not JSON, a hold rewritten on disk). It reads
and writes the tables directly, the way the earlier tests edited the registry file.
"""
from __future__ import annotations

import json
from typing import Any


def registry_of(store) -> dict[str, Any]:
    """The registry as ``{binding_id: record}`` in order (records parsed; a non-JSON one is the text)."""
    with store.db.read() as conn:
        rows = conn.execute("SELECT binding_id, record FROM bindings ORDER BY seq").fetchall()
    out: dict[str, Any] = {}
    for key, text in rows:
        try:
            out[key] = json.loads(text)
        except ValueError:
            out[key] = text
    return out


def write_raw(store, mapping: dict[str, Any]) -> None:
    """Replace the registry with ``mapping`` (key -> record; a str record is written as raw text)."""
    with store.db.write() as conn:
        conn.execute("DELETE FROM bindings")
        for i, (key, rec) in enumerate(mapping.items()):
            text = rec if isinstance(rec, str) else json.dumps(rec)
            conn.execute("INSERT INTO bindings (binding_id, record, seq) VALUES (?, ?, ?)", (key, text, i))


def dump(store) -> str:
    """A snapshot of the registry's rows, to prove a refused write changed nothing."""
    with store.db.read() as conn:
        return json.dumps(conn.execute("SELECT binding_id, record, seq FROM bindings ORDER BY seq").fetchall())


def rewrite_holds(journal, fn) -> None:
    """Apply ``fn(hold_dict) -> hold_dict`` to every hold row (pending/chain as parsed JSON)."""
    with journal.db.write() as conn:
        rows = conn.execute("SELECT hold_id, pending, chain, chained FROM holds").fetchall()
        for hold_id, pending, chain, chained in rows:
            h = {"pending": json.loads(pending) if pending else None,
                 "chain": json.loads(chain) if chain else None, "chained": bool(chained)}
            h = fn(h)
            conn.execute("UPDATE holds SET pending = ?, chain = ?, chained = ? WHERE hold_id = ?",
                         (json.dumps(h["pending"]) if h["pending"] is not None else None,
                          json.dumps(h["chain"]) if h["chain"] is not None else None,
                          1 if h["chained"] else 0, hold_id))


# --- a journaled rig for tests that fire a binding (every binding fire is a journaled run) --------------

_RIGS: dict[str, tuple[Any, Any]] = {}
_SEQ = iter(range(10**6))


def rig(path) -> tuple[Any, Any]:
    """``(journal, registry)`` over one store at ``path``, the same objects every call (a dispatcher
    requires the gate's journal to BE the registry's)."""
    from levain.autonomic import BindingStore, RunJournal
    key = str(path)
    if key not in _RIGS:
        journal = RunJournal(path)
        _RIGS[key] = (journal, BindingStore(path, journal=journal))
    return _RIGS[key]


def admit_binding_run(journal) -> tuple[str, Any]:
    """A real, fireable binding in ``journal``'s store and an admitted run of it: ``(binding_id, RunRef)``,
    what a gate-level binding fire request carries. The gate's own posture logic reads the request's
    risk and ratified posture, not this binding's."""
    from levain.autonomic import (Binding, BindingStatus, BindingStore, Posture, RunRef, SubGoal,
                                  TightnessVector, TriggerSpec)
    store = BindingStore(journal.db.directory, journal=journal)
    n = next(_SEQ)
    b = Binding.create(
        created_by="test", created_at=f"2026-10-07T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}",
        trigger=TriggerSpec(type="email", pattern={"op": "exists", "field": "from"}),
        goal=(SubGoal(goal="g", tools=("t",), output="o"),),
        tightness=TightnessVector(goal_spec=0.9, tool_min=0.9, pattern_precision=0.9, output_bound=0.9),
        posture=Posture.ON_LOOP, guard=(), status=BindingStatus.ACTIVE)
    store.add(b)
    run = f"run-test-{n}"
    assert store.admit(b.binding_id, run) is not None
    return b.binding_id, RunRef(run, "link-0")
