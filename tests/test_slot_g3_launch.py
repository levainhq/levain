"""G3-3: launch lifts its interactive-passthrough refusal for the vendors that have an identity call."""
from __future__ import annotations

import pytest

from levain.slot import Refused, identity
from levain.slot import launch as L


def test_identity_gated_is_exactly_the_identity_calls() -> None:
    assert set(L.IDENTITY_GATED) == set(identity.IDENTITY_CALLS)
    assert set(L.IDENTITY_GATED) <= set(L.VENDORS)


@pytest.mark.parametrize("vendor", ["claude", "codex"])
def test_passthrough_without_login_parses_to_interactive(vendor: str) -> None:
    req = L.parse([vendor, "--repo", "/r", "--auth", "passthrough"])
    assert (req.vendor, req.auth, req.kind) == (vendor, "passthrough", "interactive")


def test_login_still_needs_passthrough() -> None:
    with pytest.raises(Refused) as e:
        L.parse(["claude", "--repo", "/r", "--login"])
    assert e.value.name == "login-needs-passthrough"


def test_auth_help_names_the_identity_check_and_codex_not_a_check() -> None:
    text = " ".join(L.build_parser().format_help().split())
    assert "for now" not in text
    assert "after the identity check" in text and "not a check" in text
