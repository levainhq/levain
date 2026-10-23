"""The echo-redaction marker (S1 session 3b, design 1.7): a canary against an upstream's accidental LITERAL echo of the
credential, never a wall. An upstream that returns the key transformed (escaped, encoded, split across websocket frames)
is outside what any byte scanner can stop; the control that matters is that brokered routes name pinned vendor hosts.

One `Scanner` per response direction per exchange: each chunk is appended to a pending buffer, every complete
occurrence of a secret is replaced by MARKER, and everything is emitted except the LONGEST SUFFIX that is a proper
prefix of some secret, which is held for the next chunk (at most `max(len(secret)) - 1` bytes). An SSE event ending in
`\\n\\n` is held only if `\\n\\n` could begin a secret, which no API key does, so streaming is not delayed."""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

MARKER = b"[levain-redacted]"


def unsafe_with_marker(secret: bytes) -> bool:
    """True iff MARKER could hold `secret` or rebuild it with the bytes around it: the secret is inside MARKER,
    contains it, begins with one of its non-empty suffixes, or ends with one of its non-empty prefixes. Those are the
    four ways an occurrence in the output can overlap an inserted MARKER, and an occurrence that overlaps none lies in
    the input outside every replaced span, where the scan has already replaced it; so a secret set none of whose
    members is unsafe leaves no secret in the output. The credential load refuses an unsafe secret."""
    if secret in MARKER or MARKER in secret:
        return True
    n = len(MARKER)
    return any(secret.startswith(MARKER[i:]) or secret.endswith(MARKER[:n - i]) for i in range(1, n))


class Scanner:
    __slots__ = ("_secrets", "_held", "count")

    def __init__(self, secrets: Iterable[bytes]) -> None:
        # longest first, so a secret that contains another is replaced whole
        self._secrets: List[bytes] = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._held = b""
        #: replacements made so far (the exchange record's `redactions`)
        self.count = 0

    def _replace(self, buf: bytes) -> bytes:
        for s in self._secrets:
            n = buf.count(s)
            if n:
                self.count += n
                buf = buf.replace(s, MARKER)
        return buf

    def _hold_len(self, buf: bytes) -> int:
        best = 0
        for s in self._secrets:
            for k in range(min(len(s) - 1, len(buf)), best, -1):
                if buf.endswith(s[:k]):
                    best = k
                    break
        return best

    def feed(self, chunk: bytes) -> bytes:
        """What may be emitted now for `chunk`; the possible start of a secret stays held."""
        if not self._secrets:
            return chunk
        buf = self._replace(self._held + chunk)
        k = self._hold_len(buf)
        self._held = buf[len(buf) - k:] if k else b""
        return buf[:len(buf) - k] if k else buf

    def flush(self) -> bytes:
        """End of stream: what is held is not a secret (it never completed), so it goes out."""
        out, self._held = self._held, b""
        return out

    def value(self, v: bytes) -> Tuple[bytes, bool]:
        """A header value, scanned as one block: any secret in it replaces the WHOLE value by MARKER."""
        if any(s in v for s in self._secrets):
            self.count += 1
            return MARKER, True
        return v, False

    def frames(self) -> "FrameScanner":
        """The frame-aware form of this scanner, for a websocket's server -> client direction (design 1.6, r1-18). Its
        matches count in this scanner's `count`."""
        return FrameScanner(self)


#: FrameScanner.feed's end reasons: a secret matched in a frame's payload (the frame is cut where it began), and a
#: server frame keys does not relay (masked, a reserved bit set, a reserved opcode, or a 64-bit length with its top bit
#: set).
REDACTED = "redacted"
BAD_FRAME = "bad-frame"
#: RFC 6455 §5.2's defined opcodes: continuation, text, binary, close, ping, pong
_OPCODES = frozenset({0x0, 0x1, 0x2, 0x8, 0x9, 0xA})


class FrameScanner:
    """The websocket server -> client direction, scanned FRAME-AWARE (design 1.6, 1.7, r1-18): the server's frame
    headers are parsed (RFC 6455 §5.2: 2-10 header bytes, unmasked), each frame's payload is scanned, and the frame
    bytes are forwarded UNCHANGED, because a replacement inside a frame would break its length field. A secret in a
    payload is never replaced: the frame is cut where the occurrence begins and `feed` reports REDACTED, so the caller
    closes the websocket. The hold-back is per frame: the possible start of a secret is held only until its frame's
    payload ends, never across a frame boundary, so a frame's last byte never waits for the next frame. A secret split
    ACROSS frames, masked, or in a frame header is not caught (the canary, 1.7). Bytes held when the stream ends belong to
an unfinished frame and are never emitted."""

    __slots__ = ("_owner", "_head", "_left", "_held", "_done")

    def __init__(self, owner: Scanner) -> None:
        self._owner = owner
        #: the current frame's header bytes so far (empty between frames), and its payload bytes still to come
        #: (None while the header is incomplete)
        self._head = b""
        self._left: Optional[int] = None
        self._held = b""
        self._done = False

    def _head_len(self) -> Optional[int]:
        """The current header's full length, or None while its second byte has not arrived."""
        if len(self._head) < 2:
            return None
        n = self._head[1] & 0x7F
        return 2 + (2 if n == 126 else 8 if n == 127 else 0)

    def _payload_len(self) -> Optional[int]:
        """The complete header's payload length, or None when the frame is one keys does not relay."""
        b0, b1 = self._head[0], self._head[1]
        if b0 & 0x70 or b1 & 0x80:   # a reserved bit (no extension was negotiated), or a masked server frame
            return None
        if b0 & 0x0F not in _OPCODES:   # RFC 6455 §5.2: an unknown opcode MUST fail the connection (ruling W2)
            return None
        n = b1 & 0x7F
        if n == 126:
            return int.from_bytes(self._head[2:4], "big")
        if n == 127:
            n = int.from_bytes(self._head[2:10], "big")
            return None if n >> 63 else n
        return n

    def feed(self, data: bytes) -> Tuple[bytes, Optional[str]]:
        """-> (the bytes that may go to the client now, None | REDACTED | BAD_FRAME). Once an end is reported the
        scanner takes nothing more: every later call returns (b"", None)."""
        if self._done:
            return b"", None
        out: List[bytes] = []
        i = 0
        while i < len(data):
            if self._left is None:
                hl = self._head_len()
                want = (hl if hl is not None else 2) - len(self._head)
                self._head += data[i:i + want]
                i += want
                hl = self._head_len()
                if hl is None or len(self._head) < hl:
                    continue
                n = self._payload_len()
                if n is None:
                    self._done = True
                    return b"".join(out), BAD_FRAME
                out.append(self._head)   # a header carries no payload byte: it goes at once
                self._head, self._left = b"", n
                if n == 0:
                    self._left = None
                continue
            take = data[i:i + self._left]
            i += len(take)
            self._left -= len(take)
            buf = self._held + take
            at = self._find(buf)
            if at is not None:
                self._owner.count += 1
                self._done = True
                out.append(buf[:at])   # none of the matched bytes; the hold-back kept every one of them back
                return b"".join(out), REDACTED
            if self._left == 0:   # the frame's payload ends here: what is held cannot become a secret in it
                out.append(buf)
                self._held, self._left = b"", None
            else:
                k = self._owner._hold_len(buf)
                out.append(buf[:len(buf) - k])
                self._held = buf[len(buf) - k:]
        return b"".join(out), None

    def _find(self, buf: bytes) -> Optional[int]:
        """Where the first occurrence of any secret in `buf` begins, or None."""
        best = None
        for s in self._owner._secrets:
            at = buf.find(s)
            if at >= 0 and (best is None or at < best):
                best = at
        return best
