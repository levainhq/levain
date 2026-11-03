"""The push and the hold diverge only for a filed approval ping (the intent module's docstring names it); the old second case, a follow with no laptop reading, is pinned closed here."""
from __future__ import annotations

import time

from levain.webpush import intent, presence

ACTIVE, INACTIVE = 10.0, 100000.0   # idle seconds: well under / well over the default follow window


def test_a_filed_approval_ping_pushes_where_the_hold_does_not(tmp_path):
    """Case 2: ``filed`` (decide's branch). Follow, him active at the laptop: the hold's question says no, the ping of a
    request already filed says yes; unfiled, the push agrees with the hold."""
    from levain.webpush import desk

    # 1a (Phill 11-03): "active" is a typed desk message just now; the laptop's own input decides nothing
    desk._write(tmp_path, desk.DESK_MESSAGE_NAME, {"at": time.time() - ACTIVE, "source": "test", "desk": ""})
    assert intent.approvals_to_devices(tmp_path, idle=lambda: ACTIVE)[0] is False
    assert presence.should_push(tmp_path, "consent", idle=lambda: ACTIVE, suggest=False).push is False
    assert presence.should_push(tmp_path, "consent", idle=lambda: ACTIVE, suggest=False, filed=True).push is True


def test_a_follow_with_no_laptop_reading_is_not_blind_and_consent_holds(tmp_path):
    """Case 1 is gone (1a, Phill 11-03): follow reads the typed desk message alone, so no laptop input reading is not a blind
    spot. With no desk message the route sends approvals to the devices and does NOT say ``blind``: consent holds there."""
    to_devices, _why, blind = intent.approvals_route(tmp_path, idle=lambda: None)
    assert to_devices is True and blind is False
    assert intent.approvals_route(tmp_path, idle=lambda: INACTIVE)[2] is False
