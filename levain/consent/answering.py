"""levain.consent.answering — what every way of answering a consent request shares, wherever the answer is signed.

``levain consent answer`` signs in the terminal; the phone port's passkey route (``levain.phone_server``, Q6b) has a phone sign. Both read the
rules here, so neither re-spells them:

  - :func:`answer_root`: the cockpit data root an answer is verified against is the one the agent's consent hook
    replays (``cockpit_data_root`` in the queue directory's adapter settings), so a recorded answer is one the hook
    can deliver;
  - :func:`refuse_cut_display`: a request whose display was cut at its limit is never signed by levain (the signer
    could not see what it signs); the agent's own dialog stands;
  - :func:`passkey_keys`: the enrolled, active passkey keys made on one https name (a passkey's rpId is part of its
    key, so a page on another name can never use it).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from levain.authority import PASSKEY, AuthorityKey, AuthoritySnapshot
from levain.consent.listing import UNSHOWN, filed_input
from levain.consent.passkey import KEY_TYPE, PasskeyError, key_application
from levain.consent.queue import AnswerRefused

__all__ = ["answer_root", "hook_roots", "passkey_keys", "refuse_cut_display"]


def answer_root(qdir: Path, request: Any, named: Path | None = None) -> Path:
    """The cockpit data root an answer to ``request`` (in the queue at ``qdir``) is verified against. ``named``
    (an operator's ``--data-root``) must be that same directory; with no hook setting it is required."""
    from levain.consent.adapters import claude_code, codex

    adapter = {"claude_code": claude_code, "codex": codex}.get(request.agent)
    if adapter is None:
        raise AnswerRefused(f"no consent hook files for agent {request.agent!r}")
    hook = adapter.load_settings(qdir).cockpit_data_root
    if hook is None:
        if named is None:
            raise AnswerRefused(f"the queue's {adapter.SETTINGS_NAME} names no cockpit_data_root for "
                                f"{request.agent}; pass --data-root")
        return _holding_a_cockpit(named)
    if named is not None:
        try:
            same = os.path.samefile(named, hook)
        except OSError:
            same = False
        if not same:
            raise AnswerRefused(f"{named} is not the cockpit the hook verifies against ({hook}, the queue's "
                                f"{adapter.SETTINGS_NAME}): an answer verified there would never be delivered")
    return _holding_a_cockpit(Path(hook))


def hook_roots(qdir: Path) -> list[Path]:
    """Every cockpit data root a consent hook in the queue at ``qdir`` verifies against (one per agent adapter that
    names one), deduplicated, in adapter order: where a key that answers those hooks' requests is enrolled."""
    from levain.consent.adapters import claude_code, codex

    roots: list[Path] = []
    for adapter in (claude_code, codex):
        root = adapter.load_settings(qdir).cockpit_data_root
        if root is not None and Path(root) not in roots:
            roots.append(Path(root))
    return roots


def _holding_a_cockpit(root: Path) -> Path:
    """``root``, refused before anything is signed when it holds no cockpit journal: an answer verified there
    could only be refused after the signature."""
    if not (root / "cockpit.db").is_file():
        raise AnswerRefused(f"{root} holds no cockpit (no cockpit.db); name the cockpit data root with --data-root")
    return root


def refuse_cut_display(request: Any, where: str) -> None:
    """Raise :class:`AnswerRefused` when ``request``'s display was cut, so what a signer reads is not all it signs.
    ``where`` names the signing surface in the refusal (``"from the terminal"``)."""
    if filed_input(request) is UNSHOWN:
        raise AnswerRefused(f"request {request.id}'s input is too long to show here (the queue keeps a display "
                            f"cut at its limit), so it is not signed {where}; the agent's own dialog stands")


def passkey_keys(authority: AuthoritySnapshot, rp_id: str) -> list[AuthorityKey]:
    """The active keys declared ``passkey`` whose OpenSSH application is ``rp_id``, in enrolment order."""
    found = []
    for key in authority.active_keys():
        if key.custody != PASSKEY or not key.public_key.startswith(KEY_TYPE + " "):
            continue
        try:
            if key_application(key.public_key) == rp_id:
                found.append(key)
        except PasskeyError:
            continue
    return sorted(found, key=lambda k: k.enrolled_seq)
