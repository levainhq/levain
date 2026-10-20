"""The paths a Codex ``apply_patch`` names, read as Codex's own parser reads them. Every refused shape here was
reproduced against the installed Codex (codex-cli 0.162.1, ``codex --codex-run-as-apply-patch``): Codex wrote a file
whose name the old reader (``str.splitlines`` + ``\\s``) did not give, so a consent grant on the name it did give
could cover a write elsewhere. Run: levain-memory 1020-f11/sec_patch_run/ (RUN_main_087c2bf5.txt, RUN_fix.txt)."""
from __future__ import annotations

import pytest

from levain.consent.adapters.codex import patch_targets, policy_targets

BEGIN, END = "*** Begin Patch", "*** End Patch"


def patch(*lines: str) -> str:
    return "\n".join((BEGIN, *lines, END)) + "\n"


# (shape, patch, what Codex wrote, what the old reader named)
REPRODUCED = [
    ("CR inside a header line", patch("*** Add File: r1.txt\r*** Add File: r2.txt", "+hi"),
     "r1.txt*** Add File: r2.txt", ["r1.txt", "r2.txt"]),
    ("CR inside a path", patch("*** Add File: r3\rx.txt", "+hi"), "r3x.txt", ["r3"]),
    ("U+2028 inside a path", patch("*** Add File: u1 x.txt", "+hi"), "u1 x.txt", ["u1"]),
    ("U+0085 inside a path", patch("*** Add File: u2\u0085x.txt", "+hi"), "u2\u0085x.txt", ["u2"]),
    ("VT inside a path", patch("*** Add File: u3\x0bx.txt", "+hi"), "u3\x0bx.txt", ["u3"]),
    ("FF inside a path", patch("*** Add File: u4\x0cx.txt", "+hi"), "u4\x0cx.txt", ["u4"]),
    ("unit separator after a path", patch("*** Add File: u5.txt\x1f", "+hi"), "u5.txt\x1f", ["u5.txt"]),
]


@pytest.mark.parametrize("shape, text, written, old", REPRODUCED, ids=[r[0] for r in REPRODUCED])
def test_a_header_codex_reads_as_another_name_names_nothing(shape, text, written, old):
    assert written not in old                                   # the reproduced gap: Codex wrote what was not named
    assert patch_targets(text) is None
    # so a consent grant's criteria match nothing for it (the call goes to the queue)
    assert policy_targets("apply_patch", {"command": text}) is None


@pytest.mark.parametrize("text, names", [
    (patch("*** Add File: a.txt", "+hi"), ["a.txt"]),
    (patch("*** Add File: a.txt", "+hi").replace("\n", "\r\n"), ["a.txt"]),                # CRLF, as Codex drops \r
    (patch("*** Update File: a.txt", "@@", "-a", "+ nbsp\x0cff\tand sep"), ["a.txt"]),  # content lines
    (patch("*** Update File: a.txt", "*** Move to: b/c.txt", "@@", "-a", "+b"), ["a.txt", "b/c.txt"]),
    (patch("  *** Add File: d.txt  ", "+hi"), ["d.txt"]),                                   # spaces trim alike
])
def test_ordinary_patches_are_still_read(text, names):
    assert patch_targets(text) == names
