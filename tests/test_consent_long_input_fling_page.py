"""1118-12 (desk 25 urgent, 11-18): on his phone Phill opened a long Bash consent (a multi-line heredoc), scrolled to the very end
and the sheet still said part of the input was not shown, and Approve stayed shut.

The cause (reproduced red below): the gate counts a chunk SHOWN only when the browser reports it wholly inside the view at a
rendering update (card_format.js, "a chunk the browser moved past between two rendering updates ... stays not shown until it
is scrolled back over"). A touch fling moves the block by more than its own height between two updates, so the chunks it
passed were never reported, and the line gave only a COUNT of what was left: nothing said where those parts were, and the
only way to find them was to scroll the whole block again at a pace that skips none.

Here the fling is a scroll in steps of 1.6 of the block's own height. The product's rule stays (a skipped part stays not
shown); the fix makes the skipped parts findable: they are marked in the text, and a button on the line jumps to the first one
(a short step, so the part is shown whole)."""
from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

from tests.test_cockpit_consent_sheet_page import _ConsentRig
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_sign_card import needs_ssh
from tests.test_consent_long_input import _long_command
from tests.test_consent_long_input_coverage_page import PIPE, probed_drive, state, stepped

PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}}
INPUT = "document.getElementById('sheet-input')"
FULL = f"{INPUT}.querySelector('.long-whole > pre.long-full')"
ALLOW = "document.querySelector('#sheet-actions [data-choice=\"allow\"]')"
LINE = "document.getElementById('sheet-unseen')"
SUMMARY = f"{INPUT}.querySelector('.long-whole > summary')"
JUMP = f"{INPUT}.querySelector('.long-whole > .long-jump')"
MARKS = f"{FULL}.querySelectorAll('span.ns').length"
# tap the jump button until the whole input is shown (each tap waits for the gate to act on what it delivered), at most 60 taps
JUMPS = (f"(async () => {{ {PIPE} const box = {INPUT}.querySelector('.long-text') || {INPUT}; let n = 0, max = 0; "
         f"for (; n < 60 && box.dataset.seen !== '1'; n++) {{ const t = {FULL}.scrollTop; {JUMP}.click(); await pipe(box); "
         f"max = Math.max(max, Math.abs({FULL}.scrollTop - t)); }} return JSON.stringify({{taps: n, seen: box.dataset.seen, "
         f"max: max, ch: {FULL}.clientHeight}}); }})()")


def _heredoc(rig) -> str:
    command = _long_command(16_489)
    return rig.queue.file(agent="claude_code", agent_session_id="s1", tool_name="Bash", tool_input={"command": command},
                          cwd="/w", hold=timedelta(minutes=30), tier="T2").id


@needs_browser
@needs_ssh
def test_a_fling_past_chunks_leaves_the_gate_shut_and_the_page_gives_a_way_to_the_skipped_parts(tmp_path,
                                                                                                monkeypatch) -> None:
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _heredoc(rig)
    shots = Path(os.environ.get("LEVAIN_LONG_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    s = state(ALLOW, LINE, INPUT, FULL)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *rig.open_top(rid),                                               # 0 1 (+open)
            {"tap": SUMMARY},
            {"until": f"{INPUT}.classList.contains('open') && 1", "ms": 10000},
            {"eval": stepped(FULL, 1.6, down=True)},                                                      # the fling
            {"eval": s},                                                                                  # after it
            {"eval": f"JSON.stringify({{marks: {MARKS}, jump: !{JUMP}.hidden, label: {JUMP}.textContent}})"},   # marks, button
            {"shot": str(shots / "fling_1_at_the_end.png")},
            {"eval": JUMPS},                                                                              # tap to the end
            {"eval": s},
            {"eval": f"JSON.stringify({{marks: {MARKS}, jump_hidden: {JUMP}.hidden}})"},
            {"shot": str(shots / "fling_2_after_the_jumps.png")},
            {"tap": ALLOW},
            {"until": "/^signed/.test(document.getElementById('sheet-note').textContent) && 1", "ms": 30000},
        ])
    v = out["values"]
    after = json.loads(v[-9])
    assert after["allow"] is True and after["unseen"] > 0, after            # the fling left parts unshown: Allow shut
    marked = json.loads(v[-8])
    assert marked["marks"] > 0 and marked["jump"] is True and "characters left" in marked["label"], marked
    jumps = json.loads(v[-6])
    assert jumps["seen"] == "1" and jumps["taps"] >= 1, jumps
    end = json.loads(v[-5])
    assert end["allow"] is False and end["unseen"] == 0, end
    done = json.loads(v[-4])
    assert done == {"marks": 0, "jump_hidden": True}, done
    assert rig.state(rid) == "allowed"
