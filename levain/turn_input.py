"""levain.turn_input — the TURN BOUNDARY: what constitutes ONE message from the operator.

**Why this module exists (the defect it fixes, verified on a real entity 'ember', 2026-07-19).**
``levain run``'s REPL was ``line = input(...)`` -> ``conversation.send_message(line)``: one
LINE was one TURN, with no accumulation. Hand it an 11-line task specification — pasted, or
piped — and the entity received ELEVEN turns. It then did the only coherent thing available
to it: it read its own specification as a DIALOGUE. It treated later lines of the spec as the
operator pushing back on its answers to earlier lines, and apologized for mistakes in
response to corrections that were never sent. ``levain wrap`` then consolidated that
misreading into PERMANENT identity memory ("Phill catches what I miss, and what I miss is
where I soften") — and the wrap's grounding checks PASSED, because the episodes genuinely
supported the conclusion.

That is the sharpest available statement of ``grounding_is_not_truth``: a memory system can
be perfectly grounded in a perfectly recorded misunderstanding. No downstream check can catch
it — not the capture, not the grounding validation, not the consolidate — because by then the
false dialogue is real history. **The invariant has to fire HERE, at the input boundary.**

**The rule: a turn ends only at a boundary the operator produced.**

Three mechanisms, no heuristics that can silently mis-fire:

  - **Burst merge (a terminal).** Lines that arrive together are ONE message. A pasted block
    floods the tty's line-discipline buffer, so after the first line, more input is already
    pending; a human typing produces nothing until they hit the next key. So: read a line,
    then poll for :data:`BURST_GRACE` seconds — anything already waiting belongs to the same
    paste. Verified against a real pty: a three-line paste yields ONE turn, three typed lines
    yield THREE turns. The common case (type a line, press Enter, send) is byte-for-byte the
    behavior it always had, plus ~60ms.
  - **Block mode (explicit, deterministic).** ``:paste`` starts accumulating and ``:end``
    sends. This is the guarantee that does not depend on arrival timing — it is what an
    operator on a slow ssh link, where a paste can arrive in chunks further apart than the
    grace window, should use.
  - **A pipe or heredoc is ONE message.** Non-tty stdin has no interactive operator to mark
    boundaries, so the whole stream is a single turn unless it explicitly says otherwise with
    ``:send`` separators. Fabricating turns from line breaks is exactly what caused the
    defect; the conservative default cannot.

**Control tokens are recognized in exactly one position, and this is load-bearing.** ``:quit``
and ``:paste`` count only as the FIRST line of a message; ``:send`` separates turns only on a
pipe; inside a ``:paste`` block only ``:end``/``:send`` close it, and a quit token there is
ordinary text. The reason is that operators paste documents — shell transcripts, vim notes,
this project's own docs — and an earlier draft scanned every line of a merged paste for
tokens. A bare ``:q`` in the middle of pasted content therefore truncated the message AND
silently dropped every line still queued behind it. That is the same failure class as the
original defect (a boundary the operator never drew), reached through content instead of
timing, with data loss added. No token may ever discard queued input: ``:send`` ends a message
and the following lines simply become the next one.

**The tie-break rule, stated openly: when in doubt, MERGE.** One case is genuinely ambiguous —
an operator who types ahead while the entity is still working leaves two lines queued, and the
burst merge cannot tell them from a paste, so they arrive as one message. That is the correct
direction to be wrong in. A merged message is intelligible (the entity reads two instructions
at once); a split message FABRICATES a conversation that never happened, and a fabricated
conversation is what gets consolidated into permanent identity memory.

There is deliberately NO interactive override for that case, and saying so is more useful than
implying one: ``:send`` splits turns on a PIPE only, because on a terminal the same line
arrives inside a paste, where honouring it would fabricate a boundary out of the operator's
own content. At a terminal, wait for the reply — or use ``:paste``/``:end`` to say explicitly
where a message begins and ends.

**Known limitation, inherited and unchanged: ``MAX_CANON``.** A terminal in canonical mode
buffers at most ``MAX_CANON`` bytes per line (1024 on macOS/BSD), and input past that — up to
and including the newline that would terminate the line — is discarded by the line discipline
before any process can see it. So a single pasted line longer than ~1023 characters never
arrives, and the REPL waits for a line that will never be delivered. Measured on a real pty,
2026-07-20: at 1023 characters both the old ``input()`` REPL and this reader return the line;
at 1024 BOTH block forever, identically. It is an OS-level limit, not something either
implementation causes or can repair from user space (escaping it means putting the terminal
into raw mode and re-implementing line editing). It is recorded here because a limit nobody
wrote down is a limit somebody rediscovers as a mystery. Note it applies PER LINE, not per
paste: a multi-line specification with ordinary line lengths is unaffected, and a pipe or
heredoc has no such limit at all.

Deliberately NOT implemented: trailing-backslash continuation. It would have to REWRITE the
operator's content (strip the backslash) to be useful, and a turn-boundary module that edits
the text it is bounding is a second corruption channel in the same place as the first.

Decoding note, since this module sits directly upstream of ``levain.firing.encoding``: bytes
are decoded at LINE granularity, never per read-chunk. A newline is always a character
boundary, so a multi-byte character can never be split across two decodes here — the very
failure this REPL suffers from upstream is structurally impossible in levain's own read path.
"""
from __future__ import annotations

import errno
import os
import select
import sys
from collections import deque

__all__ = [
    "TurnReader",
    "QUIT_TOKENS",
    "BLOCK_START_TOKENS",
    "BLOCK_END_TOKENS",
    "TURN_SEPARATOR",
    "BURST_GRACE",
]

# Typed at an empty prompt: end the session. (EOF / Ctrl-D also ends it.)
QUIT_TOKENS = frozenset({":quit", ":q", ":exit"})
# Start accumulating: every following line is content until an end token or EOF.
BLOCK_START_TOKENS = frozenset({":paste"})
# End an accumulating block and send it.
BLOCK_END_TOKENS = frozenset({":end", ":send"})
# Outside a block, an explicit "this message is complete" separator — the way a PIPED script
# says "these lines are turn one, those are turn two" instead of relying on line breaks.
TURN_SEPARATOR = ":send"

# How long to wait for more input before deciding a turn is complete. Long enough that a paste
# arriving in several tty writes stays one message, short enough to be imperceptible when
# typing. Only ever applies to a terminal — a pipe is read to EOF.
BURST_GRACE = 0.06

_READ_SIZE = 65536


def _stdout_write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


class TurnReader:
    """Assembles the operator's keystrokes/paste/pipe into complete TURNS.

    ``fd`` is the input file descriptor (0 = stdin); ``interactive`` overrides the
    ``os.isatty`` detection (tests drive both modes over ``os.pipe`` and ``pty.openpty``).
    Reads with ``os.read`` rather than ``input()`` deliberately, and the reason is narrower
    than it first appears — stated precisely because the imprecise version would mislead
    whoever maintains this next. On a PIPE, ``input()`` pulls the whole available buffer into
    Python, so a poll on the file descriptor then reports "nothing pending" while the rest of
    the message sits in that buffer and the burst logic goes blind (measured: ``select``
    returns not-readable). On a TERMINAL it is NOT true — a canonical tty hands over one line
    per ``read(2)``, so ``select`` still reports the rest of a paste correctly even after
    ``input()`` (also measured). Owning the buffer makes both paths obey one rule instead of
    two, which is the actual argument. Nothing is given up: the REPL never imported
    ``readline``, so there was no line editing or history to lose, and the terminal stays in
    canonical mode — it still echoes and handles backspace exactly as before.
    """

    def __init__(
        self,
        fd: int = 0,
        *,
        interactive: bool | None = None,
        grace: float = BURST_GRACE,
        write=None,
    ) -> None:
        self._fd = fd
        if interactive is None:
            try:
                interactive = os.isatty(fd)
            except OSError:
                interactive = False
        self._interactive = interactive
        self._grace = grace
        self._write = write if write is not None else _stdout_write
        self._pending: deque[str] = deque()
        self._buf = b""
        self._eof = False
        self._quit = False
        # How many lines the last KeyboardInterrupt threw away (read but never delivered).
        self.cancelled_lines = 0

    @property
    def interactive(self) -> bool:
        return self._interactive

    def discard_pending(self) -> None:
        """Drop input read but not yet delivered — what Ctrl-C means.

        Without this, interrupting a paste mid-burst would leave its remaining lines queued,
        and they would silently become the NEXT message: the operator cancels, and the entity
        receives the tail of the thing they cancelled. The terminal flushes its own buffer on
        SIGINT; this flushes ours, so the two agree. EOF state is preserved — an interrupt
        cancels a message, it does not reopen a closed stream."""
        self._pending.clear()
        self._buf = b""

    # --- reading -------------------------------------------------------------------

    def _read_once(self) -> bool:
        """One ``os.read``. Returns False at EOF, flushing any unterminated trailing line
        (a pipe whose last line has no newline still carries a real message).

        Error handling is deliberately narrow. Treating every ``OSError`` as end-of-input made
        a *failure* indistinguishable from the operator pressing Ctrl-D: a stdin left in
        non-blocking mode by some earlier program raises ``BlockingIOError`` on the very first
        read, and the REPL would exit instantly with status 0 as though the session had ended
        normally — and, because ``_eof`` latches, it would never recover even once data
        arrived. So: a would-block waits for readability and retries, a genuine hangup is EOF,
        and anything else is raised rather than impersonating a clean exit."""
        while True:
            try:
                chunk = os.read(self._fd, _READ_SIZE)
                break
            except BlockingIOError:
                self._poll(None)  # block until readable, then retry the read
                continue
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.ENXIO, errno.EBADF):
                    chunk = b""  # the far end hung up (a closed pty master reads EIO) = EOF
                    break
                raise
        if not chunk:
            self._eof = True
            if self._buf:
                self._pending.append(self._decode(self._buf))
                self._buf = b""
            return False
        self._buf += chunk
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line, self._buf = self._buf[:nl], self._buf[nl + 1 :]
            self._pending.append(self._decode(line))
        return True

    @staticmethod
    def _decode(raw: bytes) -> str:
        # Decoded per COMPLETE line, so no multi-byte character can straddle the boundary.
        # `replace` rather than `strict`: a REPL must not die on one bad byte, and the U+FFFD
        # it leaves is itself a signal the capture-boundary scanner (`levain.firing.encoding`)
        # reports rather than guesses at.
        # removesuffix, not rstrip: a line is terminated by at most ONE carriage return, and
        # stripping all of them would edit content ("x\r\r\n" is x + a literal CR + CRLF).
        return raw.removesuffix(b"\r").decode("utf-8", errors="replace")

    def _poll(self, timeout: float | None) -> bool:
        try:
            return bool(select.select([self._fd], [], [], timeout)[0])
        except (OSError, ValueError):
            return False

    def _fill(self) -> bool:
        """Ensure at least one line is queued. Returns False when input is exhausted."""
        if self._pending:
            return True
        if self._eof:
            return False
        if self._interactive:
            while not self._pending:
                if not self._read_once():
                    return bool(self._pending)
                if self._pending:
                    break
                # Data arrived with no newline in it. On a canonical terminal that means the
                # LINE DISCIPLINE released a partial line, and the ordinary cause is Ctrl-D
                # pressed at a non-empty line — which terminates the line exactly as Enter
                # does, minus the newline. `input()` returned that text; an earlier draft of
                # this reader waited forever for a newline the terminal was never going to
                # send, so typing "abc" and pressing Ctrl-D HUNG the REPL. That is a
                # regression against the very function this module replaced.
                #
                # The grace poll is what tells the two cases apart, and it is the same poll
                # the burst merge already relies on: after an EOT nothing more is coming, so
                # the partial IS the operator's line; mid-paste, the rest of the block is
                # already queued behind it and we keep accumulating instead of splitting a
                # line in half.
                if self._buf and not self._poll(self._grace):
                    self._pending.append(self._decode(self._buf))
                    self._buf = b""
                    break
            # THE BURST MERGE: whatever is already waiting arrived with the line we just read
            # (a paste), so it belongs to the same message. A human cannot type the next line
            # inside the grace window; a terminal delivers a pasted block all at once.
            while self._poll(self._grace):
                if not self._read_once():
                    break
        else:
            # Not a terminal: no operator is marking boundaries, so the whole stream is the
            # message (or the `:send`-separated messages within it).
            while not self._eof:
                self._read_once()
        return bool(self._pending)

    # --- the turn boundary ---------------------------------------------------------

    def read_turn(self, prompt: str = "") -> str | None:
        """The next complete turn, or ``None`` when the session is over (EOF or a quit token).

        An empty string means "the operator pressed Enter on an empty prompt" — the caller
        re-prompts; it is NOT end-of-session.
        """
        if self._quit:
            return None
        if prompt:
            self._write(prompt)

        lines: list[str] = []
        in_block = False

        try:
            while True:
                if not self._fill():
                    if lines or in_block:
                        break  # EOF mid-message: send what the operator actually gave us
                    if self._interactive:
                        self._write("\n")  # land the cursor after a bare Ctrl-D
                    return None

                raw = self._pending.popleft()
                token = raw.strip().lower()

                if in_block:
                    # Inside an explicit block, ONLY the closing token is a token. A quit token
                    # here is content — an operator pasting a vim reference, a shell transcript
                    # or this project's own docs must get their text through intact. Cancelling
                    # a block is Ctrl-C's job, which reports what it discarded.
                    if token in BLOCK_END_TOKENS:
                        break
                    lines.append(raw)
                    continue

                if not lines:
                    if token in QUIT_TOKENS:
                        # LATCH the end of the session. Without it the reader reported "over"
                        # and then, on a stream that still held queued lines, handed one back
                        # on the next call — two paths through one state machine, and only one
                        # of them tested. `run_entity` happens to break on the first None, so
                        # this was never live; a state machine that depends on its caller's
                        # reflexes for correctness is a defect regardless.
                        self._quit = True
                        return None
                    if token in BLOCK_START_TOKENS:
                        in_block = True
                        if self._interactive:
                            self._write("  \033[2m(block mode — :end to send)\033[0m\n")
                        continue

                # A separator ends THIS message; the lines after it become the next turn, so
                # nothing is ever discarded. Honoured only on a pipe, where the stream is a
                # script the operator authored: interactively the same line arrives inside a
                # PASTE, where treating it as a boundary would fabricate a turn split out of
                # the operator's own content — the exact defect this module exists to prevent,
                # re-entering through the content rather than through timing.
                if token == TURN_SEPARATOR and not self._interactive:
                    break

                lines.append(raw)

                # Outside a block, the message ends when the burst is exhausted. Interactively
                # that is one typed line or one whole paste; on a pipe the entire stream is
                # queued, so this is reached only at its end.
                if not self._pending:
                    break
        except KeyboardInterrupt:
            # Record what the cancellation cost before unwinding, so the REPL can SAY so — a
            # silently swallowed block (`:paste` plus three paragraphs) is indistinguishable
            # from a no-op, and the operator has no way to know their text is gone.
            self.cancelled_lines = len(lines) + len(self._pending)
            self.discard_pending()
            raise

        # Trailing whitespace and surrounding blank lines go; LEADING INDENTATION STAYS. A
        # bare .strip() dedents the first line of a pasted block and nothing else, which
        # silently breaks pasted Python, YAML or a diff — and this module refuses backslash
        # continuation precisely because a turn-boundary reader must not edit the text it is
        # bounding. That rule applies to its own convenience trimming too.
        return "\n".join(lines).strip("\n").rstrip()
