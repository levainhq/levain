"""The cockpit's panel result as a snapshot body, and back.

What a body holds. A snapshot body is the credential-free result of one panel read, in the form a reader in any language
can decode: the panel's rows (or its value), what the read filtered and skipped, the error and note and empty text, the
value's write version, each view's row order, the label keys the request layer later turns into provenance, and what a
search merges in from the hidden part. It holds no clock reading: the instant a read was made is the snapshot row's own
timestamp, not part of the body, and the wall-clock facets of a row (the "58m" age) are taken out before encoding, so two
reads of unchanged data give the same bytes and so the same content hash. A body leads with ``"v"``, its schema version.
A reader that does not know that version refuses the body (``SnapshotBodyError``) and never builds half a result.

Two functions are the codec: ``encode_snap`` (a read's result to ``(status, body)``) and ``decode_snap`` (back to the
result the request layer works on). The change-detection text of a decoded result equals the original's for every value
that is plain JSON. Values that are not (a datetime, a path, a non-finite number) are turned into JSON the way the
engine's own canonical text already treats them, so a body always encodes. Pure functions: no cockpit, no threads, no I/O.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

from levain.cockpit.authority import LabelKey
from levain.cockpit.snapshots import KNOWN_STATUSES

if TYPE_CHECKING:
    from levain.cockpit.engine import _Snap

# The body schema's major version. A body carrying any other value is refused by ``decode_snap``.
BODY_VERSION = 1

# Every key a body carries, in the order ``body_for`` writes them ("v" first).
BODY_FIELDS = ("v", "rows", "value", "filtered", "skipped", "error", "note", "stale_hint", "empty", "value_version",
               "view_order", "label_keys", "value_label_key", "hidden_skipped", "hidden_note")

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
    rows taken out and ``as_of`` left out. ``stale_hint`` is set when the engine called the read stale."""
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
    if not (body["view_order"] is None or isinstance(body["view_order"], dict)):
        raise SnapshotBodyError("body field 'view_order': an object or null")
    if not (body["label_keys"] is None or isinstance(body["label_keys"], dict)):
        raise SnapshotBodyError("body field 'label_keys': an object or null")


def decode_snap(status: str, body: dict[str, Any] | None, *, as_of: str | None, error: str | None = None) -> "_Snap":
    """The ``_Snap`` ``body`` was encoded from. ``as_of`` is the snapshot row's derived-at, which no body carries.
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
        fresh["hidden_note"])
