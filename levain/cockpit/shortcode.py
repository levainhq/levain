"""The short pairing code's alphabet, length and typed forms: eight Crockford base32 characters (40 bits), shown as
``XXXX-XXXX``. Shared by the cockpit journal's one-row short offer (:mod:`levain.cockpit.firejournal`) and the box CLI."""
from __future__ import annotations

import unicodedata
from datetime import timedelta
from typing import Any

__all__ = ["SHORT_ALPHABET", "SHORT_LENGTH", "SHORT_PAIRING_TTL", "SHORT_MAX_FAILURES", "normalize_short",
           "display_short"]

# Long enough to walk from the laptop to the phone and type it, short enough that a code read over a shoulder is soon dead.
SHORT_PAIRING_TTL = timedelta(minutes=10)
# Wrong tries a code survives: the 5th kills it (a typeable code is safe to accept from a credential-less caller
# only because each minted code gives a guesser a handful of tries at 40 bits).
SHORT_MAX_FAILURES = 5
# Crockford base32: no I, L, O or U, so what is read off a screen is what gets typed
SHORT_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SHORT_LENGTH = 8
_LOOK_ALIKES = {"O": "0", "I": "1", "L": "1"}
# a hyphen is display only; a phone keyboard may hand over any of these for the one the operator typed
_DASHES = frozenset("-‐‑‒–—−")
_SHORT_INPUT_MAX = 64


def display_short(code: str) -> str:
    """A canonical short code as the operator reads it: ``ABCD-EFGH``."""
    return code[:4] + "-" + code[4:]


def normalize_short(text: Any) -> str | None:
    """The canonical eight-character form of a typed short code, or None when it cannot be one. Spaces and hyphens
    are dropped, and so are invisible format and control characters (a zero-width space, a direction mark, a soft
    hyphen: what a copy from Messages or Notes can carry along); case is folded, and the Crockford look-alikes are read
    as what they look like (O as 0, I and L as 1). Anything but ASCII letters and digits after that is not a code. The
    length cap is on the input as it arrived."""
    if not isinstance(text, str) or len(text) > _SHORT_INPUT_MAX:
        return None
    kept = "".join(c for c in text
                   if not c.isspace() and c not in _DASHES and unicodedata.category(c) not in ("Cf", "Cc"))
    if not kept.isascii():
        return None
    kept = "".join(_LOOK_ALIKES.get(c, c) for c in kept.upper())
    if len(kept) != SHORT_LENGTH or any(c not in SHORT_ALPHABET for c in kept):
        return None
    return kept
