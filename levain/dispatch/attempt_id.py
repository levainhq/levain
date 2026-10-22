"""levain.dispatch.attempt_id — the form of a dispatch attempt id, and the one validator every reader of one applies.

An attempt id names a launch attempt in the job store, in CONTROL's ticket mint, and on disk: the run's workdir is named
for it. It is therefore a path component, and an id is validated before it becomes one. The validator is exactly
CONTROL's attempt-id grammar, ``[a-z0-9][a-z0-9-]{0,63}``, as a full match on a ``str`` (no flag widens it, and no
trailing newline slips through as it would with ``$``): no ``/``, no ``.``, no NUL, no uppercase, never empty.

:func:`new_attempt_id` mints in a fixed form inside that grammar: ``a-`` followed by 128 random bits as lowercase hex.
The store mints with it; any id the grammar admits is still accepted from the store (a reader never refuses an id for
being in an older form), but nothing outside the grammar ever reaches a path join.

This module is neutral on purpose: the job store and the read-set code both import it, and neither imports the other.
Stdlib only.
"""
from __future__ import annotations

import re
import secrets

__all__ = ["ATTEMPT_ID_FORM", "ATTEMPT_ID_GRAMMAR", "AttemptIdInvalid", "check_attempt_id", "is_attempt_id",
           "new_attempt_id"]

#: CONTROL's attempt-id grammar. Applied with ``fullmatch`` only.
ATTEMPT_ID_GRAMMAR = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
#: The form :func:`new_attempt_id` mints: inside the grammar, 128 random bits.
ATTEMPT_ID_FORM = re.compile(r"a-[0-9a-f]{32}")


class AttemptIdInvalid(ValueError):
    """An attempt id outside the grammar. Nothing was built from it."""


def is_attempt_id(value: object) -> bool:
    """True only for a ``str`` the grammar matches in full."""
    return type(value) is str and ATTEMPT_ID_GRAMMAR.fullmatch(value) is not None


def check_attempt_id(value: object) -> str:
    """``value`` itself when :func:`is_attempt_id` holds, else :class:`AttemptIdInvalid` (the id is never echoed)."""
    if not is_attempt_id(value):
        raise AttemptIdInvalid("the attempt id is not in the attempt-id grammar")
    return value  # type: ignore[return-value]


def new_attempt_id() -> str:
    """A fresh id in :data:`ATTEMPT_ID_FORM`."""
    return "a-" + secrets.token_hex(16)
