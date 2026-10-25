"""R15-10-A15: the anonymous-write switch is a RELEASE decision (Phill, rec 2), so the release that flips it must say
so. The check is keyed to BEHAVIOUR, never to a constant: K4's gated control is run through the real launch (real glue
+ srt 0.0.81). While an anonymous POST under the gated mode is carded, no session-header line may claim anonymous writes
go silent. When it goes silent instead (the switch flipped), the test fails until the CHANGELOG carries a line saying
so AND the gated session header carries one too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from levain.chat_egress import launch as lm

from .decider_rig import Desk
from .test_launch import _start
from .test_launch_k4 import RecOrigin, _req

CHANGELOG = Path(lm.__file__).resolve().parents[2] / "CHANGELOG.md"
#: a line saying anonymous writes (non-reads) are no longer gated: "silent", "not gated", "no card"
SAYS_SILENT = re.compile(r"anonymous\b.*\b(writes?|non-reads?|POSTs?)\b.*\b(silent|not gated|no (longer )?card)",
                         re.IGNORECASE)


def release_gaps(*, silent: bool, changelog: str, header: tuple[str, ...]) -> list[str]:
    """What the release is missing for the behaviour observed (empty: nothing)."""
    in_log = any(SAYS_SILENT.search(line) for line in changelog.splitlines())
    in_header = any(SAYS_SILENT.search(line) for line in header)
    if silent:
        return ([] if in_log else ["a CHANGELOG line saying anonymous writes go silent"]) + \
               ([] if in_header else ["a session-header line saying anonymous writes go silent"])
    return ["a session-header line claims anonymous writes go silent; they are carded"] if in_header else []


def test_the_gap_reader_holds_both_directions() -> None:
    hdr = ("node v26.8.1",)
    said = ("node v26.8.1", "anonymous writes: not gated")
    log_line = "- Anonymous POSTs under the gated mode are now silent (no card)."
    assert release_gaps(silent=False, changelog="", header=hdr) == []
    assert release_gaps(silent=False, changelog="", header=said) != []
    assert len(release_gaps(silent=True, changelog="", header=hdr)) == 2
    assert len(release_gaps(silent=True, changelog=log_line, header=hdr)) == 1
    assert release_gaps(silent=True, changelog=log_line, header=said) == []


@pytest.fixture
def origin():
    o = RecOrigin()
    yield o
    o.close()


@pytest.mark.chat_egress_srt
def test_r15_10_a15_the_release_says_so_when_anonymous_writes_stop_carding(tmp_path, origin, monkeypatch) -> None:
    desk = Desk()
    desk.answer["card-1"] = "denied"                  # the card is answered at once: no client timeout to wait out
    e = _start(tmp_path, origin, monkeypatch, cards=desk)
    try:
        status = _req(e, "POST", origin.port, "/w", b'{"a":1}')
    finally:
        lm.stop_egress(e)
    carded = len(desk.filed) == 1 and origin.seen == []
    silent = desk.filed == [] and [s[:2] for s in origin.seen] == [("POST", "/w")]
    print(f"RUN S8C A15: gated anonymous POST -> {status}; cards filed {len(desk.filed)}; origin saw {origin.seen}")
    assert carded or silent, (desk.filed, origin.seen)
    assert release_gaps(silent=silent, changelog=CHANGELOG.read_text(), header=e.header) == []
