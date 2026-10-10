"""levain.cockpit.consent — the consent record and its canonical bytes (K2b design §1, §2; flow design
§4.5).

A consent record is encoded once, at issue, as JCS (RFC 8785) and those exact bytes are what a signer
signs and what the fire journal stores and re-reads (invariant 1). The value domain is the record's
own: str, int, bool, None, list and dict. Floats are refused rather than given ECMAScript number
formatting, because no record field needs one and a float is the one JSON value whose text two
encoders disagree on. Integers are held to the I-JSON safe range (±(2**53 - 1)) so every JSON reader
gets the same number back. Every string, keys included, is NFC-normalised before encoding, so two
spellings of one text sign as one record.

Timestamps in a record and in ``cockpit.db`` are UTC, fixed-width ``YYYY-MM-DDTHH:MM:SS.ffffffZ``
(:func:`utc_stamp`), so TEXT order is time order (K2b design §3.1).

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

__all__ = [
    "ConsentError", "Intent", "MAX_SAFE_INT", "build_record", "encode", "normalise", "parse_stamp",
    "record_sha256", "utc_stamp", "value_sha256",
]

Intent = Literal["sign", "reported"]
INTENTS: tuple[str, ...] = ("sign", "reported")
MAX_SAFE_INT = 2**53 - 1

_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_OPERATION_ID = re.compile(r"cp[0-9a-f]{32}")


class ConsentError(ValueError):
    """A value outside the consent record's domain, or a record missing a field it must carry."""


def utc_stamp(when: datetime) -> str:
    """``when`` as the one timestamp format. A naive datetime is refused: its zone is unknown."""
    if when.tzinfo is None or when.utcoffset() is None:
        raise ConsentError("a naive datetime has no zone; pass an aware one")
    return when.astimezone(timezone.utc).strftime(_STAMP_FORMAT)


def parse_stamp(text: str) -> datetime:
    """The aware UTC datetime a :func:`utc_stamp` string names; anything else is refused."""
    if not isinstance(text, str) or _STAMP.fullmatch(text) is None:
        raise ConsentError(f"not a fixed-width UTC timestamp: {text!r}")
    return datetime.strptime(text, _STAMP_FORMAT).replace(tzinfo=timezone.utc)


def _nfc(text: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:   # a lone surrogate has no UTF-8 form
        raise ConsentError(f"string is not encodable as UTF-8: {text!r}") from exc
    return unicodedata.normalize("NFC", text)


def normalise(value: Any) -> Any:
    """A copy of ``value`` with every string NFC-normalised, checked against the domain. A tuple
    becomes a list (JSON has one array type). Two keys that become equal under NFC are refused."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
            raise ConsentError(f"integer outside the I-JSON safe range: {value}")
        return value
    if isinstance(value, float):
        raise ConsentError("floats are outside the consent record's domain")
    if isinstance(value, str):
        return _nfc(value)
    if isinstance(value, (list, tuple)):
        return [normalise(v) for v in value]
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ConsentError(f"object key is not a string: {k!r}")
            nk = _nfc(k)
            if nk in out:
                raise ConsentError(f"two keys are equal under NFC: {nk!r}")
            out[nk] = normalise(v)
        return out
    raise ConsentError(f"{type(value).__name__} is outside the consent record's domain")


def _utf16_key(key: str) -> bytes:
    # RFC 8785 §3.2.3: members sorted by their UTF-16 code units; big-endian bytes compare the same way
    return key.encode("utf-16-be")


def _emit(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, str):
        # RFC 8785 §3.2.2.2 is ECMAScript JSON.stringify's string form: \" \\ \b \f \n \r \t, other
        # controls as lowercase \u00XX, everything else literal; json.dumps without ASCII escaping
        # produces exactly that
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, list):
        out.append("[")
        for i, v in enumerate(value):
            if i:
                out.append(",")
            _emit(v, out)
        out.append("]")
    else:
        out.append("{")
        for i, k in enumerate(sorted(value, key=_utf16_key)):
            if i:
                out.append(",")
            out.append(json.dumps(k, ensure_ascii=False))
            out.append(":")
            _emit(value[k], out)
        out.append("}")


def encode(value: Any) -> bytes:
    """The JCS bytes of ``value`` after :func:`normalise`."""
    out: list[str] = []
    _emit(normalise(value), out)
    return "".join(out).encode("utf-8")


def record_sha256(data: bytes) -> str:
    """Lowercase hex sha256 of a record's bytes, as stored beside them."""
    return hashlib.sha256(data).hexdigest()


def value_sha256(value: Any) -> str:
    """The hash a label row carries and a render compares: sha256 of the value's JCS bytes (§5)."""
    return hashlib.sha256(encode(value)).hexdigest()


def _need_str(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ConsentError(f"{name} must be a non-empty string")
    return value


def build_record(
    *,
    operation_id: str,
    verb: str,
    intent: Intent,
    target: Mapping[str, Any],
    params: Mapping[str, Any],
    effects: Sequence[Mapping[str, Any]],
    tier: str,
    fence: Mapping[str, Any],
    issued_at: str,
    expires_at: str,
) -> dict[str, Any]:
    """The consent record (flow design §4.5) as a normalised dict, ready for :func:`encode`.

    Each effect carries ``store`` and ``resource_key`` (the journal cross-checks its ``effects`` rows
    against them, K2b §2) plus the shape lane B's ``Effect`` gives it. ``fence`` carries the policy
    revision the decision compares (K2b §3.2.2) as ``policy_rev``."""
    if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
        raise ConsentError(f"operation_id is not 'cp' + 32 lowercase hex: {operation_id!r}")
    if intent not in INTENTS:
        raise ConsentError(f"intent must be one of {INTENTS}: {intent!r}")
    _need_str("verb", verb)
    _need_str("tier", tier)
    if not isinstance(target, Mapping) or not isinstance(params, Mapping):
        raise ConsentError("target and params must be objects")
    if not isinstance(fence, Mapping) or isinstance(fence.get("policy_rev"), bool) \
            or not isinstance(fence.get("policy_rev"), int):
        raise ConsentError("fence must be an object carrying an integer policy_rev")
    if isinstance(effects, (str, bytes)) or not isinstance(effects, Sequence):
        raise ConsentError("effects must be a list")
    for i, eff in enumerate(effects):
        if not isinstance(eff, Mapping):
            raise ConsentError(f"effects[{i}] is not an object")
        _need_str(f"effects[{i}].store", eff.get("store"))
        _need_str(f"effects[{i}].resource_key", eff.get("resource_key"))
    if parse_stamp(issued_at) >= parse_stamp(expires_at):
        raise ConsentError("expires_at must be after issued_at")
    record = normalise({
        "operation_id": operation_id, "verb": verb, "intent": intent, "target": target,
        "params": params, "effects": list(effects), "tier": tier, "fence": fence,
        "issued_at": issued_at, "expires_at": expires_at,
    })
    assert isinstance(record, dict)
    return record
