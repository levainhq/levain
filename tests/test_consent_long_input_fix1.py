"""Row 1704 fix round 1 (the L1 and L3 r1 findings on the long-input change): the server side.

Watched failing on the change it fixes (seat/1112-40-consent-long, 507000dc2) before the fix, except the control (a slot
request shown whole still answers) and the lone-surrogate pin (it pins what a corrected comment says):

- codex HIGH: a slot request whose input is not shown whole (over the slot path's display bound, or filed under the
  old 2000-character cut) could still be answered through the broker's own operator library and delivered by the
  broker: ``qbroker.operator.answer_payload`` minted the allow, ``write_answer`` wrote it, and ``ANSWER?`` delivered it
  for an input no signer was shown. The old cut had the same hole.
- deepseek MED: the slot path's bound assumed the broker's frame and row escape a display at most twofold; they are
  written with ``ensure_ascii=False``, so a two-byte or four-byte character stays its own UTF-8 bytes. Proven here
  through the broker's real filing (the row file on disk), at the bound and one character past it; the filing held
  before the fix too, and only the past-the-bound refusal (the codex HIGH) failed.
- codex LOW: over-bound words with a size of thousands of digits made ``int()`` raise.
- complement/deepseek LOW: a display neither over the bound nor the old cut was called "the old display cut".
- L1 F3: the refusal said "so it is not shown, so it is not signed".
- L1 F4: the comment that a lone surrogate never raises held only for the display, not the seal.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

import tests.test_qbroker as base
import tests.test_qbroker_receipts as rbase
from levain.cockpit.consent import utc_stamp
from levain.consent import record
from levain.consent.answering import refuse_cut_display
from levain.consent.broker_client import GATE_DISPLAY_MAX, gate_request
from levain.consent.listing import UNSHOWN, ConsentEntry, entry_body, filed_input, unshown_why
from levain.consent.queue import AnswerRefused
from levain.consent.record import (ANSWER_NAMESPACE, LEGACY_DISPLAY_CUT, ExternalGateRequest, answer_message,
                                   answer_payload, redact_display)
from levain.qbroker import operator, wire
from levain.qbroker.store import ROW_MAX
from tests.test_qbroker import NOW, ask, filed, head_of, row_of, write_answer

L, broker, clock, keys, resolver = base.L, base.broker, base.clock, base.keys, base.resolver
LATER = rbase.LATER


def _slot(tool_input, *, display: str | None = None) -> ExternalGateRequest:
    """A slot's request as its gate builds it (``gate_request``), optionally refiled with another display."""
    r = gate_request(agent="claude_code", session="cc-1", tool_name="Bash", tool_input=tool_input, cwd="/w",
                     event_id="e-long", tier="T2", hold=300.0, now=NOW)
    if display is None:
        return r
    f = r.fields()
    f.pop("id")
    return ExternalGateRequest.create(**{**f, "display": display})


def _legacy_cut(display: str) -> str:
    """A display as the old cut left it: its first 1999 characters and an ellipsis."""
    return display[:LEGACY_DISPLAY_CUT - 1] + "…"


# --- codex HIGH: the broker's own answer path ------------------------------------------------------------------------

def _unshown_slot_requests():
    over = _slot({"command": "x" * GATE_DISPLAY_MAX})
    long = _slot({"command": "y" * 5000})
    cut = _slot({"command": "y" * 5000}, display=_legacy_cut(long.display))
    return {"over-bound": over, "old-cut": cut}


@pytest.mark.parametrize("kind", ["over-bound", "old-cut"])
def test_the_broker_operator_never_mints_an_answer_for_an_input_not_shown_whole(L, broker, keys, kind):
    request = _unshown_slot_requests()[kind]
    assert filed_input(request) is UNSHOWN
    rid = filed(broker, fields=request.fields())
    with pytest.raises(operator.OperatorError) as got:
        operator.answer_payload(L / "queue", rid, "allow", keys["operator"].fp, now=NOW + timedelta(seconds=5))
    assert unshown_why(request) in str(got.value) and "its hold ends unanswered" in str(got.value)


@pytest.mark.parametrize("kind", ["over-bound", "old-cut"])
def test_the_broker_operator_never_writes_a_signed_answer_for_an_input_not_shown_whole(L, broker, keys, kind):
    request = _unshown_slot_requests()[kind]
    rid = filed(broker, fields=request.fields())
    row = row_of(L, rid)
    # signed by hand, as any writer holding the key could: the library still refuses to write it
    payload = answer_payload(queue_id=row["queue_id"], request=request, decision="allow", signer_fp=keys["operator"].fp,
                             signed_at=utc_stamp(NOW + timedelta(seconds=5)), receipts_head=head_of(L))
    sig = keys["operator"].sign(answer_message(payload), ANSWER_NAMESPACE)
    with pytest.raises(operator.OperatorError, match="its hold ends unanswered"):
        operator.write_answer(L / "queue", rid, payload, sig, rbase.snapshot(L), now=LATER)
    assert not (L / "queue" / "answers" / f"{rid}.json").exists()


@pytest.mark.parametrize("kind", ["over-bound", "old-cut"])
def test_the_broker_never_delivers_a_signed_allow_for_an_input_not_shown_whole(L, broker, keys, kind):
    request = _unshown_slot_requests()[kind]
    rid = filed(broker, fields=request.fields())
    write_answer(L, rid, keys["operator"])                 # a valid signature, written straight to the answer name
    got = ask(broker, rid)
    assert got.get("decision") != "allow", got
    assert operator.rows(L / "queue", signers=rbase.snapshot(L), now=LATER)[0].answered is False


def test_a_slot_request_shown_whole_still_answers_through_the_broker(L, broker, keys):
    request = _slot({"command": "z" * 20_000})               # long, under the slot bound: shown whole
    assert filed_input(request) == {"command": "z" * 20_000}
    rid = filed(broker, fields=request.fields())
    payload, sig = rbase.signed(L, rid, keys["operator"])
    operator.write_answer(L / "queue", rid, payload, sig, rbase.snapshot(L), now=LATER)
    assert ask(broker, rid)["decision"] == "allow"


# --- deepseek MED: the slot bound for characters past ASCII, through the real filing ----------------------------------

_EMPTY = len(redact_display("Bash", {"command": ""}).encode())


@pytest.mark.parametrize("rune, width", [("é", 2), ("Ж", 2), ("😀", 4), ("𝄞", 4)])
def test_a_slot_display_of_wide_characters_files_whole_at_the_bound_and_names_its_size_past_it(L, broker, keys, rune,
                                                                                              width):
    assert len(rune.encode()) == width
    at = rune * ((GATE_DISPLAY_MAX - _EMPTY) // width)
    whole = _slot({"command": at})
    assert len(whole.display.encode()) <= GATE_DISPLAY_MAX and filed_input(whole) == {"command": at}
    # the frame the hook sends and the row the broker writes keep a non-ASCII character as its own UTF-8 bytes
    frame = wire.encode({"v": 1, "op": "REQUEST", "id": 1, "cap": "cap-a", "request": whole.fields()})
    assert len(frame) < min(wire.MAX_FRAME, ROW_MAX), len(frame)
    rid = filed(broker, fields=whole.fields())
    on_disk = (L / "queue" / "requests" / f"{rid}.json").read_bytes()
    assert len(on_disk) < ROW_MAX and rune.encode() in on_disk and b"\\u" not in on_disk
    assert filed_input(ExternalGateRequest(**row_of(L, rid)["request"])) == {"command": at}
    # one character more: the over-bound words with the size, filed (never dropped), and refused for signing
    past = _slot({"command": at + rune * 2})
    size, limit = record.over_bound("Bash", past.display)
    assert limit == GATE_DISPLAY_MAX and size == len(redact_display("Bash", {"command": at + rune * 2}, 10**9).encode())
    rid2 = filed(broker, fields=past.fields())
    with pytest.raises(operator.OperatorError, match=f"its input is {size} bytes"):
        operator.answer_payload(L / "queue", rid2, "deny", keys["operator"].fp, now=NOW + timedelta(seconds=5))


# --- the over-bound words: bounded, never a raise, and a reason that is true ------------------------------------------

def _req(display: str) -> SimpleNamespace:
    return SimpleNamespace(id="r1", tool_name="Bash", display=display)


@pytest.mark.parametrize("size, limit", [("9" * 4301, "24576"), ("12", "9" * 4301), ("100", "100"), ("5", "10"),
                                         ("0", "0"), ("0012", "10")])
def test_a_malformed_over_bound_marker_is_unshown_and_never_raises(size, limit):
    display = f"Bash [not shown: the input is {size} bytes, over the {limit}-byte bound a signer is shown whole]"
    assert record.over_bound("Bash", display) is None
    req = _req(display)
    assert filed_input(req) is UNSHOWN
    assert unshown_why(req) == "its input could not be read back"
    with pytest.raises(AnswerRefused, match="could not be read back, so it is not signed from the terminal"):
        refuse_cut_display(req, "from the terminal")


def test_a_malformed_marker_on_a_pending_request_renders_its_body(tmp_path):
    display = "Bash [not shown: the input is " + "9" * 5000 + " bytes, over the 24576-byte bound a signer is shown whole]"
    request = _slot({"command": "ls"}, display=display)
    body = entry_body(ConsentEntry(request=request, state="pending", via=None, signer_fp=None, grant_id=None,
                                   decided_at=None))
    assert body["answer_allow"] is None and body["note"].startswith("its input could not be read back: ")


def test_the_old_cut_is_named_only_for_a_display_the_old_cut_could_have_left():
    long = "Bash " + json.dumps({"command": "x" * 5000})
    assert "old display cut" in unshown_why(_req(_legacy_cut(long)))
    # unreadable, but not the old cut's shape: not at its length, or not ending in its ellipsis
    for display in (long[:1500] + "…", long[:LEGACY_DISPLAY_CUT], 'Bash {"command": "unterminated', "Bashx {}",
                    "Bash not json at all"):
        assert unshown_why(_req(display)) == "its input could not be read back", display


def test_a_refusal_says_once_why_and_once_what_follows():
    over = redact_display("Bash", {"command": "x" * record.DISPLAY_MAX})
    with pytest.raises(AnswerRefused) as got:
        refuse_cut_display(_req(over), "from a phone")
    text = str(got.value)
    assert "so it is not shown" not in text and text.count(", so ") == 1, text
    assert unshown_why(_req(over)).endswith("-byte bound a signer is shown whole")


def test_a_lone_surrogate_is_counted_in_the_display_and_refused_by_the_seal():
    display = redact_display("Bash", {"command": "\ud800"})        # counted (surrogatepass), never a raise here
    assert display == 'Bash {"command": "\ud800"}'
    with pytest.raises(UnicodeEncodeError):
        _slot({"command": "\ud800"})                                  # the seal encodes strictly


# --- complement MED: the help names both bounds, never "however long" -------------------------------------------------

def test_the_answer_help_names_each_paths_display_bound():
    import argparse

    from levain.cli import build_parser

    def find(parser, *names):
        for name in names:
            sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
            parser = sub.choices[name]
        return parser

    text = " ".join(find(build_parser(), "consent", "answer").description.split())
    assert "however long" not in text
    assert f"{record.DISPLAY_MAX // 1024} KiB" in text and f"{GATE_DISPLAY_MAX // 1024} KiB" in text, text
