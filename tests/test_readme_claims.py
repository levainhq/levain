"""README claims that name a pin or a feature state must not drift from the code."""
from pathlib import Path

from levain.consent.canary import TESTED_VERSIONS

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def test_readme_names_the_canary_pin_and_copies_no_pinned_version():
    assert "TESTED_VERSIONS" in README and "levain/consent/canary.py" in README
    for versions in TESTED_VERSIONS.values():
        for v in versions:
            assert v not in README, f"README copies pinned version {v}; point to the pin instead"


def test_readme_does_not_deny_premium_dictation():
    assert "Premium dictation is not offered" not in README
