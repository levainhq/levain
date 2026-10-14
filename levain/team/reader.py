"""The team reader: one typed answer to "what does this clone's team ledger say", for any surface that draws it.

A surface that draws a team (the cockpit's Team zone, ``levain team view``) must never turn "I could not read it"
into an empty team, and must never draw a partly-judged ledger as a fully judged one. ``read_team`` therefore returns
one of four states and never raises:

``ok``              the whole ledger was judged on this clone; ``model`` is the four panes (``view.build_model``).
``judged_partial``  the ledger was read but only part of it could be judged here (a rewrite, an unaccepted merge, lines
                    waiting past this clone's last judged commit). ``model`` is what the judged part says; ``why`` says
                    what is held back, in words, and the surface must show it beside the model.
``unreadable``      the ledger could not be read or judged. ``model`` is None and ``why`` NAMES the reason.
``no_team``         this clone has never joined or created a team ledger (a solo install). Not an error.

``why`` is for a person. A ``TeamError`` carries a message written for a person and is passed through (one line,
bounded); anything else (a git failure, an OS error, a bug) says only that the ledger could not be read, and its text
stays in ``detail``, which is for a log and never for a screen. Read-only: this module asks the ledger nothing that
writes, and it fetches nothing (a sync is the transport's own verb).

Synchronous. A cold history read can take a long time: a caller that serves requests holds its own timeout.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .ledger_api import LedgerTransport
from .transport import TeamError
from .view import DEFAULT_ACK_FLAG, DEFAULT_RECHECK_DAYS, build_model

State = Literal["ok", "judged_partial", "unreadable", "no_team"]

_WHY_MAX = 300
_UNREADABLE = "The team ledger could not be read on this machine."
_NO_TEAM = "This install has no team ledger. Create one or join one with `levain team`."


@dataclass(frozen=True)
class TeamRead:
    state: State
    model: dict | None
    why: str = ""       # for a screen: empty when state is ok
    detail: str = ""    # for a log: the raw reason, never drawn


def _one_line(text: object) -> str:
    """A bounded single line of ``text``; an exception whose own text raises still yields something drawable."""
    try:
        line = " ".join(str(text).split())
    except Exception:  # noqa: BLE001
        return ""
    if len(line) <= _WHY_MAX:
        return line
    cut = line[:_WHY_MAX - 1]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip() + "\u2026"


def read_team(gl: LedgerTransport, *, path_filter: str = "", recheck_days: int = DEFAULT_RECHECK_DAYS,
              ack_flag: int = DEFAULT_ACK_FLAG) -> TeamRead:
    """The team this clone's ledger describes, as one of the four states. Never raises."""
    try:
        if gl.pinned_root is None and not gl.legacy_only() and not gl.has_local_ledger() and not gl.has_remote_ledger():
            return TeamRead("no_team", None, _NO_TEAM)
        # ONE derivation feeds the team, the ledger, the judgement, the handle and the canon: a tip that
        # moves or a trust state that changes between separate reads must not mix two judgements in one model
        d = gl.derivation()
        team = d.team
        ledger = gl.ledger_of(d)
        model = build_model(team, ledger, gl.handle(team, d=d), gl.read_canon(d=d), gl.state_hash(ledger, team),
                            recheck_days=recheck_days, ack_flag=ack_flag, path_filter=path_filter)
        if d.judged != "full":
            waiting = int(d.waiting or 0)
            why = ("Part of the team ledger could not be fully judged on this machine, so what follows may be out of date"
                   + (f"; {waiting} newer line{'s are' if waiting != 1 else ' is'} waiting." if waiting else "."))
            return TeamRead("judged_partial", model, why, _one_line(d.frozen_why or ""))
        return TeamRead("ok", model)
    except TeamError as exc:
        text = _one_line(exc)
        return TeamRead("unreadable", None, text or _UNREADABLE, f"{type(exc).__name__}: {text}")
    except Exception as exc:  # noqa: BLE001 - a surface drawing a team must get a named state, never a traceback
        return TeamRead("unreadable", None, _UNREADABLE, f"{type(exc).__name__}: {_one_line(exc)}")
