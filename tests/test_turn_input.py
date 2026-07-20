"""levain.turn_input — the TURN BOUNDARY (spore-373 blocker [1]).

The defect under test is not cosmetic: reading one LINE as one TURN made an entity read its
own 11-line specification as an 11-turn DIALOGUE, apologize for corrections that were never
sent, and then consolidate that misreading into permanent identity memory. So these tests
drive REAL file descriptors — a real ``os.pipe`` and a real ``pty.openpty`` — rather than a
stubbed reader: the fix depends on how a terminal's line discipline and a pipe actually
deliver bytes, and a mock would happily confirm a fix that does not work on either.
"""
from __future__ import annotations

import os
import pty
import threading
import time

import pytest

from levain.turn_input import TurnReader

# Short in tests (default 0.06) so the "typed, not pasted" cases stay fast while remaining a
# real wall-clock gap — the same knob the REPL uses, not a test-only code path.
GRACE = 0.05
TYPING_GAP = 0.25  # comfortably outside GRACE — this is what "a human typed it" looks like

_OPEN_READ_ENDS: list[int] = []

# The exact shape that broke ember: a multi-line task specification handed to the REPL.
SPEC = """Fix the failing test in tests/test_hooks.py.
The hook walker only resolves the first token of a hook command.
It must resolve ${CLAUDE_PROJECT_DIR} in every token.
Run the test suite when you are done.
Commit the work, but do not push."""


# --------------------------------------------------------------------------------------
# pipes / heredocs — the non-interactive path
# --------------------------------------------------------------------------------------

def _pipe_reader(payload: bytes, **kw) -> TurnReader:
    """A reader over a real pipe carrying ``payload``, closed when the write completes.

    The write runs on a thread deliberately: a payload larger than the OS pipe buffer (~64KB)
    would otherwise block forever, since the writer is the same thread that would drain it.
    That is a property of pipes, not of the reader — but it makes the test hang rather than
    fail, so the fixture handles it once here."""
    r, w = os.pipe()

    def _feed() -> None:
        try:
            os.write(w, payload)
        finally:
            os.close(w)

    threading.Thread(target=_feed, daemon=True).start()
    reader = TurnReader(r, interactive=False, write=lambda _s: None, **kw)
    _OPEN_READ_ENDS.append(r)
    return reader


@pytest.fixture(autouse=True)
def _close_pipe_read_ends():
    """Close every pipe read end a test opened. TurnReader deliberately does NOT own or close
    the descriptor it was handed, which is correct — so the tests must, or a full run leaks one
    descriptor per pipe test."""
    _OPEN_READ_ENDS.clear()
    yield
    for fd in _OPEN_READ_ENDS:
        try:
            os.close(fd)
        except OSError:
            pass
    _OPEN_READ_ENDS.clear()


def test_piped_multiline_spec_is_ONE_turn():
    """THE REGRESSION. Piping a 5-line spec must produce one message, not five turns."""
    reader = _pipe_reader(SPEC.encode() + b"\n")
    assert reader.read_turn() == SPEC
    assert reader.read_turn() is None  # and nothing after it


def test_piped_spec_is_not_split_on_blank_lines_either():
    payload = b"paragraph one\n\nparagraph two\n\n\nparagraph three\n"
    reader = _pipe_reader(payload)
    turn = reader.read_turn()
    assert turn == "paragraph one\n\nparagraph two\n\n\nparagraph three"
    assert reader.read_turn() is None


def test_piped_send_separator_makes_explicit_turns():
    """Multi-turn from a pipe is possible — but only when the script SAYS so."""
    reader = _pipe_reader(b"first message\nstill first\n:send\nsecond message\n")
    assert reader.read_turn() == "first message\nstill first"
    assert reader.read_turn() == "second message"
    assert reader.read_turn() is None


def test_piped_trailing_line_without_newline_is_not_dropped():
    reader = _pipe_reader(b"line one\nline two, no trailing newline")
    assert reader.read_turn() == "line one\nline two, no trailing newline"


def test_piped_quit_token_alone_ends_the_session():
    reader = _pipe_reader(b":quit\n")
    assert reader.read_turn() is None


def test_a_quit_token_inside_a_message_is_CONTENT_and_loses_nothing():
    """A control token may only be the FIRST line of a message. Both reviewers found the same
    hole here: scanning every line meant a bare `:q` inside a pasted shell transcript or vim
    reference truncated the message AND silently dropped everything queued behind it. Nothing
    an operator pastes may be interpreted as a command."""
    reader = _pipe_reader(b"here is my transcript:\n:q\nthe rest is important\n")
    assert reader.read_turn() == "here is my transcript:\n:q\nthe rest is important"
    assert reader.read_turn() is None


def test_quit_token_alone_does_not_leak_into_a_later_read():
    reader = _pipe_reader(b":quit\nstill here\n")
    assert reader.read_turn() is None
    assert reader.read_turn() is None  # the session is over; nothing resurfaces


def test_piped_empty_stream_is_end_of_session():
    assert _pipe_reader(b"").read_turn() is None


def test_multibyte_character_split_across_read_chunks_survives():
    """levain's OWN read path must not commit the upstream sin it shields against: an em-dash
    whose three bytes arrive in separate reads must still decode to one character."""
    r, w = os.pipe()
    reader = TurnReader(r, interactive=False, write=lambda _s: None)
    raw = "an em—dash\n".encode()
    split = raw.index(b"\xe2") + 1  # mid-character: after the lead byte, before continuations
    os.write(w, raw[:split])
    os.write(w, raw[split:])
    os.close(w)
    assert reader.read_turn() == "an em—dash"


# --------------------------------------------------------------------------------------
# a real terminal — the interactive path
# --------------------------------------------------------------------------------------

@pytest.fixture
def tty():
    """A real pty. Yields ``(write_to_terminal, reader)``; the reader reads the slave side
    exactly as `levain run` reads stdin."""
    master, slave = pty.openpty()
    reader = TurnReader(slave, interactive=True, grace=GRACE, write=lambda _s: None)

    def send(text: str) -> None:
        os.write(master, text.encode())

    try:
        yield send, reader
    finally:
        for fd in (master, slave):
            try:
                os.close(fd)
            except OSError:
                pass


def test_pasted_block_arrives_as_ONE_turn(tty):
    """THE REGRESSION, interactive half. A paste floods the tty buffer in one burst; every
    line of it belongs to the same message."""
    send, reader = tty
    send(SPEC + "\n")
    assert reader.read_turn() == SPEC


def test_typed_lines_stay_separate_turns(tty):
    """The common case must be untouched: a line typed, Enter, sent — one line, one turn."""
    send, reader = tty
    for line in ("first question", "second question", "third question"):
        send(line + "\n")
        assert reader.read_turn() == line
        time.sleep(TYPING_GAP)


def test_empty_line_is_a_reprompt_not_an_end_of_session(tty):
    send, reader = tty
    send("\n")
    assert reader.read_turn() == ""  # falsy, but NOT None — the REPL re-prompts
    time.sleep(TYPING_GAP)
    send("real question\n")
    assert reader.read_turn() == "real question"


def test_quit_token_ends_an_interactive_session(tty):
    send, reader = tty
    send(":quit\n")
    assert reader.read_turn() is None


def test_block_mode_holds_across_a_slow_paste(tty):
    """The deterministic escape hatch: over a slow link a paste can arrive in chunks further
    apart than the grace window, so burst timing alone cannot save it. `:paste` … `:end` does
    not depend on timing at all."""
    send, reader = tty
    send(":paste\n")
    time.sleep(TYPING_GAP)
    send("first chunk of the spec\n")
    time.sleep(TYPING_GAP)  # a gap that WOULD have ended a burst-merged turn
    send("second chunk of the spec\n")
    time.sleep(TYPING_GAP)
    send(":end\n")
    assert reader.read_turn() == "first chunk of the spec\nsecond chunk of the spec"


def test_block_mode_preserves_INTERIOR_blank_lines(tty):
    send, reader = tty
    send(":paste\npara one\n\npara two\n\n\npara three\n:end\n")
    assert reader.read_turn() == "para one\n\npara two\n\n\npara three"


def test_block_mode_treats_a_bare_quit_token_as_CONTENT(tty):
    """Inside an explicit block, only `:end`/`:send` close it. A pasted vim reference or shell
    transcript containing a bare `:q` must arrive whole — the block is the operator SAYING
    "everything until :end is text"."""
    send, reader = tty
    send(":paste\nin vim you type\n:q\nto quit the editor\n:end\n")
    assert reader.read_turn() == "in vim you type\n:q\nto quit the editor"


def test_a_pasted_block_containing_a_quit_token_arrives_whole(tty):
    """Same guarantee without block mode: a burst-merged paste is content, not commands."""
    send, reader = tty
    send("here is my transcript:\n:q\nand the rest of the question\n")
    assert reader.read_turn() == "here is my transcript:\n:q\nand the rest of the question"


def test_a_pasted_block_is_not_split_by_a_send_token(tty):
    """`:send` separates turns on a PIPE (an authored script). In a paste it is content —
    honouring it would fabricate a turn split out of the operator's own text, which is the
    original defect arriving through content instead of timing."""
    send, reader = tty
    send("step one\n:send\nstep two\n")
    assert reader.read_turn() == "step one\n:send\nstep two"


def test_leading_indentation_of_a_pasted_block_is_preserved(tty):
    """A turn-boundary reader must not EDIT the text it is bounding. A bare .strip() dedented
    only the first line, silently breaking pasted Python, YAML and diffs."""
    send, reader = tty
    send("    def f():\n        return 1\n")
    assert reader.read_turn() == "    def f():\n        return 1"


def test_cancelling_a_block_reports_what_it_discarded(tty):
    """Ctrl-C during `:paste` used to throw the accumulated text away in silence — and to end
    block mode without saying so. The count is what lets the REPL tell the operator.

    The interrupt must land AFTER the block has actually been entered and filled, or the test
    proves nothing: patching `_fill` up front makes it raise on the very first call, before
    `:paste` is ever consumed, so `in_block` is still False and no lines have accumulated
    (caught by L3). Here the first `_fill` runs for real and only the NEXT one raises."""
    send, reader = tty
    send(":paste\npara one\npara two\n")
    time.sleep(TYPING_GAP)

    real_fill = reader._fill
    calls = {"n": 0}

    def _fill_then_interrupt():
        calls["n"] += 1
        if calls["n"] == 1:
            return real_fill()          # consumes ":paste" + both paragraphs into _pending
        raise KeyboardInterrupt         # the operator hits Ctrl-C mid-block

    reader._fill = _fill_then_interrupt
    with pytest.raises(KeyboardInterrupt):
        reader.read_turn()
    # both paragraphs were accumulated inside the block before the interrupt landed
    assert reader.cancelled_lines >= 2


def test_eof_ends_the_session(tty):
    send, reader = tty
    send("\x04")  # Ctrl-D at an empty line
    assert reader.read_turn() is None


def test_a_long_line_within_the_terminal_limit_is_delivered_whole(tty):
    """Pins the working range against MAX_CANON (1024 bytes per line on macOS/BSD — see the
    module docstring). At 1023 the line arrives intact; at 1024 the line discipline discards
    the terminating newline and NO implementation can see the line, the old `input()` REPL
    included (measured, identical). Not asserted here because the failure mode is a block, not
    a value — a test that hangs to prove a hang is a worse test than a documented limit."""
    send, reader = tty
    line = "x" * 1000
    send(line + "\n")
    assert reader.read_turn() == line


def test_a_pipe_has_no_line_length_limit():
    """The terminal limit above is the TERMINAL's. A pipe or heredoc — how a spec is most
    likely to be handed to an entity — carries arbitrarily long lines."""
    payload = ("y" * 50_000 + "\n" + "z" * 50_000 + "\n").encode()
    reader = _pipe_reader(payload)
    turn = reader.read_turn()
    assert turn == "y" * 50_000 + "\n" + "z" * 50_000


def test_discard_pending_drops_a_cancelled_paste(tty):
    """Ctrl-C must not leave the tail of a cancelled paste queued as the next message."""
    send, reader = tty
    send("line one\nline two\nline three\n")
    time.sleep(TYPING_GAP)
    reader._fill()               # the burst is now read into the reader
    reader.discard_pending()     # …and the operator hits Ctrl-C
    send("a fresh question\n")
    assert reader.read_turn() == "a fresh question"


def test_discard_pending_does_not_reopen_a_closed_stream():
    reader = _pipe_reader(b"")
    assert reader.read_turn() is None
    reader.discard_pending()
    assert reader.read_turn() is None


def test_ctrl_d_after_typed_text_delivers_that_line(tty):
    """Ctrl-D at a NON-empty line terminates the line exactly as Enter does, minus the
    newline — `input()` returned the text. Waiting for a newline the terminal will never send
    hung the REPL: a regression against the function this module replaced, found by L3."""
    send, reader = tty
    send("abc\x04")
    assert reader.read_turn() == "abc"


def test_ctrl_d_inside_a_block_flushes_the_line_and_a_second_ends_the_block(tty):
    """Consistent with the terminal: one Ctrl-D flushes the current line, a second at an empty
    line is EOF, which closes the block and delivers what it holds."""
    send, reader = tty
    send(":paste\nabc\x04\x04")
    assert reader.read_turn() == "abc"


def test_interactive_flag_defaults_to_isatty(tty):
    _send, _reader = tty
    r, w = os.pipe()
    try:
        assert TurnReader(r).interactive is False
    finally:
        os.close(r)
        os.close(w)
