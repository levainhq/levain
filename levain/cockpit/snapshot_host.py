"""The cockpit's panel result as a snapshot body, and back.

What a body holds. A snapshot body is the credential-free result of one panel read, in the form a reader in any language
can decode: the panel's rows (or its value), what the read filtered and skipped, the error and note and empty text, the
value's write version, each view's row order, the label keys the request layer later turns into provenance, and what a
search merges in from the hidden part. It holds no reading of the clock: the instant a read was made is the snapshot row's
own timestamp, not part of the body. The one instant a body does carry is ``source_as_of``, when the provider declared
that its source confirmed the content then (a fact about the content, so part of its hash). The wall-clock facets of a row (the "58m" age) are taken out before encoding, so two
reads of unchanged data give the same bytes and so the same content hash. A body leads with ``"v"``, its schema version.
A reader that does not know that version refuses the body (``SnapshotBodyError``) and never builds half a result.

Two functions are the codec: ``encode_snap`` (a read's result to ``(status, body)``) and ``decode_snap`` (back to the
result the request layer works on). The change-detection text of a decoded result equals the original's for every value
that is plain JSON. Values that are not (a datetime, a path, a non-finite number) are turned into JSON the way the
engine's own canonical text already treats them, so a body always encodes. The codec is pure functions: no cockpit, no
threads, no I/O.

The in-thread host. ``SnapshotHost`` is one of the two homes of the derive loop (the other is a worker process): it
owns the snapshot store's writer, runs the loop's threads inside the serving process, and has the loop call the
cockpit's own derive-side reads, so a request never runs a provider. A host is attached to the one cockpit it derives
for. Besides one plan per registered panel it runs two pseudo-plans, discovery and the entity, because those are read
once per cycle by the host and not once per manifest request. A pseudo-plan's id starts with an underscore, which no
panel id may, so the two never meet.

Why discovery publishes a row: the panels a discoverer registers appear and vanish, and the loop must be told. The
discovery row's body is the sorted set of discovered ids, so the stream's change counter moves exactly when the panel
set does. The entity row works the same way for the entity's value.

``ServerProbe`` is what the change stream watches in a served cockpit: the engine probe's counter folded with the
serve's own watch token, since the panels the serve composes itself read stores no derive publishes.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from levain.cockpit.authority import LabelKey
from levain.cockpit.derive import DeriveLoop, DeriveResult, PanelPlan
from levain.cockpit.snapshots import DB_FILENAME, KNOWN_STATUSES, SnapshotReader, SnapshotWriter

if TYPE_CHECKING:
    from levain.cockpit.engine import Cockpit, _Snap

# The body schema's major version. A body carrying any other value is refused by ``decode_snap``.
BODY_VERSION = 1

# Every key a body carries, in the order ``body_for`` writes them ("v" first).
BODY_FIELDS = ("v", "rows", "value", "filtered", "skipped", "error", "note", "stale_hint", "empty", "value_version",
               "view_order", "label_keys", "value_label_key", "hidden_skipped", "hidden_note",
               "source_as_of")

# The gate a LabelKey gets when its values were changed to fit JSON (a datetime turned into its string, say): a key whose
# values are no longer the ones the provider held must not be read as verified.
_GATE_LOSSY = "unavailable"


class SnapshotBodyError(ValueError):
    """A snapshot body this code cannot decode: an unknown schema version, a missing or mistyped field, or a value
    that is not JSON."""


def _engine() -> Any:
    # Imported on first use, so importing this module never needs the engine to be fully loaded.
    from levain.cockpit import engine
    return engine


def _key(k: Any, lossy: list[bool]) -> str:
    if isinstance(k, str):
        return str.__str__(k)
    lossy[0] = True
    if k is None:
        return "null"
    if isinstance(k, bool):
        return "true" if k else "false"
    if isinstance(k, int):
        return int.__repr__(k)
    if isinstance(k, float):
        if math.isnan(k):
            return "NaN"
        if math.isinf(k):
            return "Infinity" if k > 0 else "-Infinity"
        return float.__repr__(k)
    return str(k)


def _norm(obj: Any, lossy: list[bool]) -> Any:
    """``obj`` as plain JSON values. Mirrors the engine's canonical text (``json.dumps(default=str)``): a tuple is a
    list, a non-string dict key is its JSON string, any other object is ``str(obj)``; and a non-finite float, which JSON
    has no text for, is None. ``lossy[0]`` is set when a value was changed by more than tuple-to-list."""
    if obj is None:
        return None
    if isinstance(obj, bool):
        return bool(obj)
    if isinstance(obj, str):
        return str.__str__(obj)
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        if math.isfinite(obj):
            return float(obj)
        lossy[0] = True
        return None
    if isinstance(obj, dict):
        return {_key(k, lossy): _norm(v, lossy) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_norm(v, lossy) for v in obj]
    lossy[0] = True
    return str(obj)


def _plain(obj: Any) -> Any:
    return _norm(obj, [False])


def _status_of(snap: "_Snap") -> str:
    s = snap.status
    if s == "stale":
        # the data underneath: the row's staleness is a reader's fact (its age), not a status the store holds
        return "partial" if snap.skipped else "ok"
    return s if s in KNOWN_STATUSES else "partial"


def _encode_label_key(key: Any) -> dict[str, Any]:
    if not isinstance(key, LabelKey):
        raise SnapshotBodyError(f"a label key must be a LabelKey, not {type(key).__name__}")
    lossy = [False]
    values = _norm(dict(key.values), lossy)
    gate = key.gate
    if lossy[0] and gate in (None, "match"):
        gate = _GATE_LOSSY
    return {"resource_key": _plain(key.resource_key), "values": values, "operation_id": _plain(key.operation_id),
            "gate": _plain(gate)}


def _decode_label_key(raw: Any, where: str) -> LabelKey:
    if (not isinstance(raw, dict) or not isinstance(raw.get("resource_key"), str)
            or not isinstance(raw.get("values"), dict)
            or not (raw.get("operation_id") is None or isinstance(raw["operation_id"], str))
            or not (raw.get("gate") is None or isinstance(raw["gate"], str))):
        raise SnapshotBodyError(f"{where}: not a label key")
    return LabelKey(raw["resource_key"], raw["values"], raw["operation_id"], raw["gate"])


def body_for(snap: "_Snap") -> dict[str, Any]:
    """The body of ``snap``: every field the request layer reads, as plain JSON values, with the clock facets of the
    rows taken out and the read time left out (``source_as_of`` is the provider's own declared instant, or None).
    ``stale_hint`` is set when the engine called the read stale."""
    rows = _plain(_engine()._etag_rows(snap.rows))
    label_keys = None
    if snap.label_keys is not None:
        label_keys = {_key(i, [False]): _encode_label_key(k) for i, k in snap.label_keys.items()}
    return {
        "v": BODY_VERSION,
        "rows": rows,
        "value": _plain(snap.value),
        "filtered": _plain(snap.filtered),
        "skipped": _plain(snap.skipped),
        "error": _plain(snap.error),
        "note": _plain(snap.note),
        "stale_hint": bool(snap.stale_hint) or snap.status == "stale",
        "empty": _plain(snap.empty),
        "value_version": _plain(snap.value_version),
        "view_order": _plain(snap.view_order),
        "label_keys": label_keys,
        "value_label_key": None if snap.value_label_key is None else _encode_label_key(snap.value_label_key),
        "hidden_skipped": _plain(snap.hidden_skipped),
        "hidden_note": _plain(snap.hidden_note),
        "source_as_of": _plain(snap.source_as_of),
    }


def encode_snap(snap: "_Snap") -> tuple[str, dict[str, Any]]:
    """``(status, body)`` for ``snap``. ``status`` is one of ``snapshots.KNOWN_STATUSES``: the engine's own status
    maps to the same name; ``stale`` maps to the status of the data underneath (``partial`` when something was
    skipped, else ``ok``) with ``body["stale_hint"]`` true; any other engine status maps to ``partial``."""
    return _status_of(snap), body_for(snap)


_LIST_OR_NONE = ("rows",)
_LISTS = ("filtered", "skipped", "hidden_skipped")
_STR_OR_NONE = ("error", "empty", "value_version")
_STRS = ("note", "hidden_note")


def _check(body: dict[str, Any]) -> None:
    for f in _LIST_OR_NONE:
        if not (body[f] is None or isinstance(body[f], list)):
            raise SnapshotBodyError(f"body field {f!r}: a list or null")
    for f in _LISTS:
        if not isinstance(body[f], list):
            raise SnapshotBodyError(f"body field {f!r}: a list")
    for f in _STR_OR_NONE:
        if not (body[f] is None or isinstance(body[f], str)):
            raise SnapshotBodyError(f"body field {f!r}: a string or null")
    for f in _STRS:
        if not isinstance(body[f], str):
            raise SnapshotBodyError(f"body field {f!r}: a string")
    if not isinstance(body["stale_hint"], bool):
        raise SnapshotBodyError("body field 'stale_hint': a boolean")
    if not (body["source_as_of"] is None or isinstance(body["source_as_of"], str)):
        raise SnapshotBodyError("body field 'source_as_of': a string or null")
    if not (body["view_order"] is None or isinstance(body["view_order"], dict)):
        raise SnapshotBodyError("body field 'view_order': an object or null")
    if not (body["label_keys"] is None or isinstance(body["label_keys"], dict)):
        raise SnapshotBodyError("body field 'label_keys': an object or null")


def decode_snap(status: str, body: dict[str, Any] | None, *, as_of: str | None, error: str | None = None) -> "_Snap":
    """The ``_Snap`` ``body`` was encoded from. ``as_of`` is the snapshot row's derived-at, which no body carries (a body's own ``source_as_of`` rides on the result's
    ``source_as_of``, not in ``as_of``).
    ``error``, when given, is the row's own error text and replaces the body's (a failed derive keeps the last good body
    and puts its message on the row). Every container in the result is new: ``body`` is never aliased, so the reader's
    one shared decoded object stays untouched. A row with status ``error`` or ``timeout`` and no body at all (the store
    keeps none for a panel whose first derive failed) decodes to the bare error result: no rows, no value, ``error`` as
    given (or a generic line), empty note. Raises ``SnapshotBodyError`` for any other body this code cannot read."""
    if body is None and status in ("error", "timeout"):
        return _engine()._Snap(status, None, None, [], [], as_of, error or f"no result: the derive ended in {status}", "")
    if not isinstance(body, dict):
        raise SnapshotBodyError(f"a snapshot body is an object, not {type(body).__name__}")
    v = body.get("v")
    if type(v) is not int or v != BODY_VERSION:
        raise SnapshotBodyError(f"snapshot body version {v!r}: this reader speaks {BODY_VERSION}")
    missing = [f for f in BODY_FIELDS if f not in body]
    if missing:
        raise SnapshotBodyError(f"snapshot body lacks {', '.join(missing)}")
    if not isinstance(status, str):
        raise SnapshotBodyError(f"snapshot status {status!r}: a string")
    if error is not None and not isinstance(error, str):
        raise SnapshotBodyError("the row's error is a string or absent")
    try:
        fresh = json.loads(json.dumps(body, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise SnapshotBodyError(f"snapshot body is not JSON: {exc}") from None
    _check(fresh)
    label_keys = None
    if fresh["label_keys"] is not None:
        label_keys = {i: _decode_label_key(k, f"label_keys[{i!r}]") for i, k in fresh["label_keys"].items()}
    vkey = fresh["value_label_key"]
    snap_cls = _engine()._Snap
    return snap_cls(
        status if status in KNOWN_STATUSES else "partial", fresh["rows"], fresh["value"], fresh["filtered"],
        fresh["skipped"], as_of, fresh["error"] if error is None else error, fresh["note"], fresh["stale_hint"],
        fresh["empty"], fresh["value_version"], fresh["view_order"], label_keys,
        None if vkey is None else _decode_label_key(vkey, "value_label_key"), fresh["hidden_skipped"],
        fresh["hidden_note"], source_as_of=fresh["source_as_of"])


# --------------------------------------------------------------------------------------------------- the host

# Panels whose derive is expensive enough to be told apart from the rest (ids only; a panel absent from a cockpit is skipped).
HEAVY_PANELS = frozenset({"board", "topology", "sky"})
# The host's two pseudo-plans. An id with this prefix is never a panel's: ``SnapshotHost._plans`` skips any registered id that has it.
PSEUDO_PREFIX = "_"
DISCOVER_ID = PSEUDO_PREFIX + "discover"
ENTITY_ID = PSEUDO_PREFIX + "entity"
# Every thread a host starts has a name with this prefix, so a test can tell derive threads from request threads.
DERIVE_THREAD_PREFIX = "levain-cockpit-derive"


def _one_line(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())


class SnapshotHost:
    """Derives a cockpit's panels on threads of this process and publishes them to a snapshot store under ``data_root``.

    The loop looks at the panels' inputs and writes its heartbeat every ``poll_s``, by default the cadence the engine
    expects of a host's heartbeat (``engine.HEARTBEAT_CADENCE_S``), which is the one place that number is stated.

    Lifecycle: build (or ``try_open``), ``attach()`` the cockpit to the host's reader, ``start()``, and ``stop()`` at the
    end. A host starts once. Opening takes the store's writer lock, so a second host on the same directory is refused.
    """

    def __init__(
        self,
        cockpit: "Cockpit",
        data_root: "str | Path",
        *,
        token_fn: "Callable[[], Any] | None" = None,
        clock: Callable[[], float] = time.time,
        poll_s: "float | None" = None,
        heavy: Iterable[str] = HEAVY_PANELS,
    ) -> None:
        self._cockpit = cockpit
        self._heavy = frozenset(heavy)
        self._lock = threading.Lock()
        self._started = False
        self._closed = False
        path = Path(data_root) / DB_FILENAME
        self._writer = SnapshotWriter(path)
        reader: "SnapshotReader | None" = None
        try:
            reader = self._reader = SnapshotReader(path)
            self._loop = DeriveLoop(
                self._writer, {plan.id: plan for plan in self._plans()}, self._derive,
                poll_s=_engine().HEARTBEAT_CADENCE_S if poll_s is None else poll_s, token_fn=token_fn, clock=clock, on_publish=None,
                thread_name_prefix=DERIVE_THREAD_PREFIX.removesuffix("derive"),
            )
        except BaseException:
            # whatever was opened before the failure is released: the writer's lock and the reader's connection
            if reader is not None:
                reader.close()
            self._writer.close()
            raise

    @classmethod
    def try_open(cls, cockpit: "Cockpit", data_root: "str | Path", **kw: Any) -> "tuple[SnapshotHost | None, str]":
        """``(host, "")``, or ``(None, reason)`` when no host can be had here: the store is locked by another writer, is
        of another contract, or cannot be written, or the cockpit is attached already. Never raises, so a server that
        asked stays on its own path and says why."""
        if getattr(cockpit, "attached", False):
            return None, "this cockpit is already attached to a snapshot reader"
        try:
            return cls(cockpit, data_root, **kw), ""
        except Exception as exc:  # noqa: BLE001 - every way the store can refuse is one answer: no host
            return None, _one_line(exc)

    # ---- plans

    def _plans(self) -> list[PanelPlan]:
        """The plan of every registered panel the host derives, then the two pseudo-plans.

        A panel the serve composes itself is left out (it reads stores under the request's authority), and so is any id
        that starts with ``PSEUDO_PREFIX``. A panel with no declared inputs and no timer re-derives when the global
        watch token moves (and on the loop's backstop), which is today's cadence for an on-demand panel.
        """
        plans = []
        for pid in self._cockpit.panel_ids:
            spec = self._cockpit.spec(pid)
            if spec is None or pid.startswith(PSEUDO_PREFIX) or self._cockpit.serve_composed(pid):
                continue
            plans.append(PanelPlan(
                id=pid, inputs=tuple(spec.inputs), every_s=spec.refresh_every_s,
                on_token=(not spec.inputs and spec.refresh_every_s is None),
                heavy=pid in self._heavy, timeout_s=spec.timeout_s,
            ))
        plans.append(PanelPlan(id=DISCOVER_ID, on_token=True))
        plans.append(PanelPlan(id=ENTITY_ID, on_token=True))
        return plans

    # ---- the derive

    def _derive(self, pid: str) -> DeriveResult:
        # An exception out of here is the loop's to handle: it publishes an error row and lives on.
        if pid == DISCOVER_ID:
            return self._derive_discovery()
        if pid == ENTITY_ID:
            return self._derive_entity()
        try:
            snap = self._cockpit.derive_panel(pid)
        except KeyError:
            if self._cockpit.spec(pid) is not None:
                raise
            self._loop.retire(pid)      # unregistered since the plan was made: nothing is published for it again
            return DeriveResult("error", None, error="the panel is no longer registered")
        status, body = encode_snap(snap)
        # a failed derive's row carries its message on the row (the store keeps no body for it)
        return DeriveResult(status, body, error=body["error"] if status in ("error", "timeout") else None)

    def _derive_discovery(self) -> DeriveResult:
        ids = self._cockpit.run_discovery()
        self._loop.set_plans(self._plans())     # the panel set may have changed: new plans in, retired ones out
        return DeriveResult("ok", {"v": BODY_VERSION, "panels": sorted(ids)})

    def _derive_entity(self) -> DeriveResult:
        value = self._cockpit.read_entity()
        self._cockpit.set_entity_value(value)
        text = json.dumps(value[0], sort_keys=True, separators=(",", ":"), default=str)
        return DeriveResult("ok", {"v": BODY_VERSION, "sha": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()})

    # ---- lifecycle

    @property
    def reader(self) -> SnapshotReader:
        return self._reader

    @property
    def loop(self) -> DeriveLoop:
        return self._loop

    def attach(self) -> None:
        """Switch the cockpit to reading this host's rows."""
        self._cockpit.attach_snapshots(
            self._reader, deriving=self._deriving_panels, pending=self._loop.pending, dirty=self._loop.dirty,
        )

    def _deriving_panels(self) -> list[str]:
        # the loop is busy with the host's two pseudo-plans too; the cockpit's word is about panels
        return [pid for pid in self._loop.deriving() if not pid.startswith(PSEUDO_PREFIX)]

    def start(self) -> None:
        """Start the loop's threads; every plan's first derive is due at once. Raises RuntimeError on a second start."""
        with self._lock:
            if self._started or self._closed:
                raise RuntimeError("a SnapshotHost starts once and does not restart after stop(): make a new one")
            self._started = True
        self._loop.start()

    def run_once(self, now: "float | None" = None) -> list[str]:
        """One poll and the derives it finds due, run to completion on the calling thread (for a test to drive)."""
        return self._loop.run_once(time.time() if now is None else now)

    def stop(self, timeout: "float | None" = None) -> "list[threading.Thread]":
        """End the loop and release the store. Idempotent. Returns the threads still alive when ``timeout`` ran out; the
        store is then left open for them and a later ``stop()`` finishes the job."""
        alive = self._loop.stop(timeout)
        if alive:
            return alive
        with self._lock:
            if self._closed:
                return []
            self._closed = True
        self._writer.close()
        # ``close`` ends the calling thread's connection. A thread's reader connection can only be used and closed by
        # that thread, so those of the request and pool threads are not reachable from here; each is freed when the
        # reader is released, which ends with the cockpit that holds it.
        self._reader.close()
        return []


# --------------------------------------------------------------------------------------------------- the stream probe


class ServerProbe:
    """What a serve's change stream watches (``change_seq()`` and ``snapshot()``), over an attached cockpit.

    The engine's probe moves when a panel's snapshot did. The panels a serve composes itself (decisions, edits) read
    stores no derive publishes, and the serve's watch token is how the watcher has always seen those stores move, so the
    counter folds the two: it changes when either does. A serve with no token cannot see its own stores move at all, so
    the counter then also changes once per ``slow_s``, and the stream looks at the slow cadence a token-less watcher
    builds at. ``engine_probe()`` answers the cockpit's probe, or None for a cockpit that is not attached, whose snapshot
    ``bare_snapshot()`` gives (the same shape: ``({id: etag}, set_version)``).
    """

    def __init__(
        self,
        engine_probe: Callable[[], Any],
        token_fn: Callable[[], Any],
        bare_snapshot: Callable[[], "tuple[dict[str, str], str | None]"],
        *,
        slow_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine_probe = engine_probe
        self._token_fn = token_fn
        self._bare_snapshot = bare_snapshot
        self._slow_s = slow_s
        self._clock = clock

    def change_seq(self) -> int:
        probe = self._engine_probe()
        seq = probe.change_seq() if probe is not None else 0
        token = self._token_fn()
        bucket = int(self._clock() // self._slow_s) if token is None else None
        return hash((seq, token, bucket))

    def snapshot(self) -> "tuple[dict[str, str], str | None]":
        probe = self._engine_probe()
        return self._bare_snapshot() if probe is None else probe.snapshot()
