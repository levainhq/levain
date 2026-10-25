"""``receipt_log.leaf`` runs under root's Python 3.9: the queue broker computes the receipts head with it. This file
imports only root-closure modules, so the root-py39 job runs it under 3.9 (``zip(strict=True)`` raised TypeError there)."""
import json

import pytest

from levain.consent.receipt_log import LogUnreadable, leaf


def test_a_row_is_its_canonical_bytes():
    got = leaf((7, "r1", "allowed", "2026-10-25T00:00:00Z", "face"))
    assert json.loads(got) == {"seq": 7, "request_id": "r1", "state": "allowed", "created_at": "2026-10-25T00:00:00Z",
                               "face": "face"}
    assert got == json.dumps(json.loads(got), sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize("row", [(7, "r1", "allowed", "2026-10-25T00:00:00Z"),
                                 (7, "r1", "allowed", "2026-10-25T00:00:00Z", "face", "extra")])
def test_a_row_of_the_wrong_width_is_refused(row):
    with pytest.raises(ValueError):
        leaf(row)


def test_a_column_that_is_not_text_is_unreadable():
    with pytest.raises(LogUnreadable):
        leaf((7, "r1", b"allowed", "2026-10-25T00:00:00Z", "face"))
