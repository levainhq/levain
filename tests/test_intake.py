"""Tests for ``levain.intake`` — THE LIST intake grammar, parity with flow's vectors."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from levain.intake import IntakeRefused, parse_intake, receipt, receipt_line

VECTORS = Path(__file__).parent / "vectors" / "list_intake_vectors.json"
VECTORS_SHA256 = "04cb93dded23889bd6a3f2dd5c4ab20047eb196fce8dc4d62312896af2d0b81b"
_DATA = json.loads(VECTORS.read_text(encoding="utf-8"))
_NOW = datetime.fromisoformat(_DATA["now"])


def test_vector_file_sha_pinned():
    assert hashlib.sha256(VECTORS.read_bytes()).hexdigest() == VECTORS_SHA256, \
        "vendored list_intake_vectors.json drifted from flow's; re-vendor and re-pin deliberately"


@pytest.mark.parametrize("case", _DATA["cases"], ids=lambda c: repr(c["line"]))
def test_vector(case):
    if case.get("refuse"):
        with pytest.raises(IntakeRefused) as ei:
            parse_intake(case["line"], _NOW)
        assert str(ei.value).strip()
    else:
        assert parse_intake(case["line"], _NOW) == case["expect"]


def test_refusal_is_a_valueerror_with_reason():
    with pytest.raises(ValueError, match="not in the future"):
        parse_intake("@+0m now", _NOW)


def test_receipts():
    line, d = receipt(parse_intake("@tomorrow check backups", _NOW), "spore-7")
    assert line == "scheduled drop spore-7 fires Mon 11-09 09:00"
    assert d == {"kind": "drop", "id": "spore-7", "fires": "2026-11-09T09:00", "text": "check backups"}
    line, d = receipt(parse_intake("? flow's future", _NOW), "spore-8")
    assert line == "talk spore-8"
    assert d == {"kind": "talk", "id": "spore-8", "fires": None, "text": "flow's future"}
    line, d = receipt(parse_intake("buy milk", _NOW))
    assert line == "sent to the desk to sort"
    assert d == {"kind": "sort", "id": None, "fires": None, "text": "buy milk"}
    assert receipt_line(d) == line


def test_parse_does_not_mutate_now():
    n = datetime(2026, 11, 8, 12, 0, 30)
    parse_intake("@+1h x", n)
    assert n.second == 30


def test_a_yearless_date_crosses_the_year_seam_and_a_written_year_does_not():
    """Diogenes 2026-11-08 (51037cbc4326), ruled by Phill (row 456 (a)): on 12-28 '@01-03' means the coming January,
    as '@mon' and '@+5d' already did; a written year is held to and a past moment in it is still refused."""
    now = datetime(2026, 12, 28, 12, 0)
    assert parse_intake("@01-03 call the landlord", now)["when"] == "2027-01-03T09:00"
    assert parse_intake("@01-03 9am pay rent", now)["when"] == "2027-01-03T09:00"
    assert parse_intake("@12-28 13:00 later today", now)["when"] == "2026-12-28T13:00"
    with pytest.raises(IntakeRefused, match="not in the future"):
        parse_intake("@2026-01-03 old", now)
    # a leap day: the next year that has one within reach, else refused as no calendar date
    assert parse_intake("@02-29 leap", datetime(2027, 3, 1, 12, 0))["when"] == "2028-02-29T09:00"
    with pytest.raises(IntakeRefused, match="not a calendar date"):
        parse_intake("@02-30 never", now)
