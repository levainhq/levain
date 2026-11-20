"""The page gate's server-side fields for a test's stub server (a ``SimpleNamespace`` handed to a handler built with
``__new__``). Every GET read of the cockpit takes a ``page_gate`` slot (levain#157), so a stub that calls such a handler
needs these three beside whatever else it carries."""
from __future__ import annotations

import threading
from typing import Any


def page_gate_fields(gate: "Any | None" = None) -> dict[str, Any]:
    """``page_gate``, ``page_waiters`` and ``page_waiters_lock``, as ``make_server`` sets them (``gate`` overrides the
    semaphore, for a test that watches it)."""
    return {"page_gate": gate if gate is not None else threading.BoundedSemaphore(4),
            "page_waiters": 0, "page_waiters_lock": threading.Lock()}
