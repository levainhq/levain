"""The push and the hold diverge in exactly two cases, both pinned here (the intent module's docstring names them)."""
from __future__ import annotations

from levain.webpush import intent, presence

ACTIVE, INACTIVE = 10.0, 100000.0   # idle seconds: well under / well over the default follow window


def test_a_filed_approval_ping_pushes_where_the_hold_does_not(tmp_path):
    """Case 2: ``filed`` (decide's branch). Follow, him active at the laptop: the hold's question says no, the ping of a
    request already filed says yes; unfiled, the push agrees with the hold."""
    assert intent.approvals_to_devices(tmp_path, idle=lambda: ACTIVE)[0] is False
    assert presence.should_push(tmp_path, "consent", idle=lambda: ACTIVE, suggest=False).push is False
    assert presence.should_push(tmp_path, "consent", idle=lambda: ACTIVE, suggest=False, filed=True).push is True


def test_a_blind_follow_pushes_where_consent_does_not_hold(tmp_path):
    """Case 1: no laptop input reading under ``follow``: the route sends approvals to the devices and says ``blind``."""
    to_devices, _why, blind = intent.approvals_route(tmp_path, idle=lambda: None)
    assert to_devices is True and blind is True
    assert intent.approvals_route(tmp_path, idle=lambda: INACTIVE)[2] is False
