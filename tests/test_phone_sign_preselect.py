"""Phone preselect (project_memory 1025-18/preselect/DESIGN.md §2): a sign link may carry ``choice=<key>``, the option
the operator tapped on his phone, and the version of the request it was tapped on.

The page halves of this file (the old phone page opening on the linked card, driven in headless Chrome) were deleted
with that page (ONESURFACE OS-6); the link builder is what remains."""
from __future__ import annotations

import pytest

from levain.consent.sign import deep_link

RID = "np-pre00001-c9f2a1"


def test_the_deep_link_carries_his_pick_and_its_version_after_the_sheet_and_only_an_option_key() -> None:
    sha = "0123456789abcdef" + "f" * 48
    # the version alone is the base link an app adds `&choice=<key>` to; with a choice the link is that base plus it
    base = deep_link("https://h.ts.net:8443/", RID, version=sha)
    assert base == f"https://h.ts.net:8443/sign/{RID}#v=0123456789abcdef"
    assert deep_link("https://h.ts.net:8443/", RID, choice="b", version=sha) == base + "&choice=b"
    assert deep_link("https://h.ts.net:8443/", RID, choice="b", version=sha[:16]) == base + "&choice=b"
    assert deep_link("https://h.ts.net:8443/", RID) == f"https://h.ts.net:8443/sign/{RID}"
    for bad in ("*", "", "B", "toolongkey", "a&sign=x", "a b"):
        with pytest.raises(ValueError):
            deep_link("https://h.ts.net/", RID, choice=bad, version=sha)
    for bad_v in ("", "0123", sha.upper(), sha[:17], "stat:" + sha, sha + "&x=1"):
        with pytest.raises(ValueError):
            deep_link("https://h.ts.net/", RID, choice="b", version=bad_v)
    with pytest.raises(ValueError):                 # a pick holds only on a version: never a choice without one
        deep_link("https://h.ts.net/", RID, choice="b")
