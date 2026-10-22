"""The echo-redaction marker (S1 session 3b, design 1.7): a canary against an upstream's accidental LITERAL echo of the
credential, never a wall. An upstream that returns the key transformed (escaped, encoded, split across websocket frames)
is outside what any byte scanner can stop; the control that matters is that brokered routes name pinned vendor hosts.

One `Scanner` per response direction per exchange: each chunk is appended to a pending buffer, every complete
occurrence of a secret is replaced by MARKER, and everything is emitted except the LONGEST SUFFIX that is a proper
prefix of some secret, which is held for the next chunk (at most `max(len(secret)) - 1` bytes). An SSE event ending in
`\\n\\n` is held only if `\\n\\n` could begin a secret, which no API key does, so streaming is not delayed."""
from __future__ import annotations

from typing import Iterable, List, Tuple

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
