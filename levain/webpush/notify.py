"""Notices: the text a service posts at the operator, as a doorbell and nothing more.

A notice is a title, a body and a priority. It can never be an action, a decision or a link, and it can never claim to be
a seat or a needs item: the source label shown with it comes from the token that posted it (:mod:`levain.webpush.notify_tokens`),
never from the poster. This module holds the pure parts: the sanitiser (a refusal, never a quiet truncation or rewrite),
the kind a priority files under, the displayed title, and :func:`notify_decision` (what the operator's notification
policy does with a notice). Delivery is the caller's.
"""
from __future__ import annotations

import re
import unicodedata

from levain.webpush import presence

__all__ = ["BODY_MAX", "TITLE_MAX", "PRIORITIES", "NOTICES_URL", "NotifyRefused", "clean_body", "clean_title",
           "decide_now", "display_title", "kind_for", "notice_ref", "notify_decision"]

TITLE_MAX = 80
BODY_MAX = 1000
PRIORITIES = ("low", "normal", "urgent")      # three levels; there is no ``high``
RANK = {p: i for i, p in enumerate(PRIORITIES)}
# where a notice's push opens: a fixed link of Levain's own, never one the poster chose (a page without a notices view
# falls back to the board)
NOTICES_URL = "/?view=notices"
# a title that opens with a bracketed label would read as another source's label once ours is put in front of it
_OPENS_WITH_LABEL = re.compile(r"^\s*[\[【〔〖「『［⟦⸨].{0,60}?[\]】〕〗」』］⟧⸩]")


class NotifyRefused(ValueError):
    """A post refused. ``error`` is the reply's error code, ``status`` the HTTP status it maps to, ``retry_after`` the
    seconds to wait for a rate refusal (else 0)."""

    def __init__(self, error: str, message: str, status: int = 400, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.error = error
        self.status = status
        self.retry_after = retry_after


def _refuse_chars(text: str, what: str, *, newline: bool) -> None:
    for ch in text:
        if newline and ch == "\n":
            continue
        cat = unicodedata.category(ch)
        # Cc controls, Cf format (bidi overrides and isolates, zero-width and joiners, the BOM, tag characters),
        # Cs/Co/Cn surrogates, private use and unassigned, Zl/Zp line and paragraph separators
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            raise NotifyRefused("bad_characters", f"the {what} holds a character that is not plain text (U+{ord(ch):04X})")


def clean_title(title: object) -> str:
    if not isinstance(title, str):
        raise NotifyRefused("bad_shape", "the title is a string")
    # the characters are checked BEFORE the trim, so a refused one at either end is refused, never quietly trimmed away
    text = unicodedata.normalize("NFC", title)
    _refuse_chars(text, "title", newline=False)
    text = text.strip()
    if not text:
        raise NotifyRefused("empty", "the title is empty", 422)
    if len(text) > TITLE_MAX:
        raise NotifyRefused("title_too_long", f"the title is over {TITLE_MAX} characters: shorten it", 413)
    if _OPENS_WITH_LABEL.match(unicodedata.normalize("NFKC", text)):
        raise NotifyRefused("spoofed_label", "the title opens with a bracketed label: the source label is added for you", 422)
    return text


def clean_body(body: object) -> str:
    if not isinstance(body, str):
        raise NotifyRefused("bad_shape", "the body is a string")
    # the only quiet changes: CRLF to LF, the surrounding whitespace trimmed, three or more newlines to two; every
    # character is checked before the trim
    text = unicodedata.normalize("NFC", body).replace("\r\n", "\n")
    _refuse_chars(text, "body", newline=True)
    text = text.strip()
    if not text:
        raise NotifyRefused("empty", "the body is empty", 422)
    if len(text) > BODY_MAX:
        raise NotifyRefused("body_too_long", f"the body is over {BODY_MAX} characters: shorten it", 413)
    return re.sub(r"\n{3,}", "\n\n", text)


def display_title(source: str, title: str) -> str:
    """What the phone and the cockpit show: the token's source label in front, built here and nowhere else."""
    return f"[{source}] {title}"


def kind_for(priority: str) -> str:
    """The ping kind a priority files under (:data:`levain.webpush.presence.KIND_CLASSES`): urgent is the ``attention``
    class, the rest ``other``."""
    if priority not in RANK:
        raise ValueError(f"unknown priority {priority!r}")
    return "notify_urgent" if priority == "urgent" else "notify"


def notify_decision(priority: str, now: float, word: dict | None, idle: float | None, *, policy: dict | None = None,
                    route: str = "desk") -> str:
    """``pushed``, ``held`` or ``recorded`` for a notice. ``low`` is recorded only (nothing to push, nothing to release
    later); every other priority is the operator's policy to decide, by its kind's class, through
    :func:`levain.webpush.presence.decide`. Pure: the arguments are what ``decide`` takes."""
    kind = kind_for(priority)
    if priority == "low":
        return "recorded"
    d = presence.decide(now, word, idle, class_name=presence.kind_class(kind), policy=policy, route=route)
    return "pushed" if d.push else "held"


def notice_ref(source: str) -> str:
    """The ping ref of every notice from ``source``: it is the push's tag and topic, so repeated notices from one source
    collapse into one notification and never replace a seat's or a needs item's."""
    return f"notify:{source}"


def decide_now(queue_dir, priority: str, *, now: float | None = None, idle=None) -> tuple[str, str | None]:
    """``(decision, hold reason)`` for a notice posted now, read the way every other ping is: the operator's word, policy
    and route on file and the desk's idle time, through :func:`levain.webpush.presence.should_push`. The reason is the
    word that muted a held notice (``quiet``, ``sleep``) or ``active`` (the desk was in use), else ``None``. ``low`` is
    recorded and never asks the rule."""
    if priority == "low":
        return "recorded", None
    said = presence.should_push(queue_dir, kind_for(priority), now=now, idle=idle)
    return ("pushed", None) if said.push else ("held", said.by or "active")
