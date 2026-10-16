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
bounded, and cut where it starts quoting a git command's own output, which stays in ``detail``); anything else (a git failure, an OS error, a bug) says only that the ledger could not be read, and its text
stays in ``detail``, which is for a log and never for a screen. Read-only: this module asks the ledger nothing that
writes, and it fetches nothing (a sync is the transport's own verb).

Synchronous. A cold history read can take a long time: a caller that serves requests holds its own timeout.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

from .ledger_api import LedgerTransport
from .tenure import _fp_or_none
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


_RAW = re.compile(r"[:;,]?\s*(?:\bgit [a-z-]+ failed\b|\bfatal:|\bhint:|\berror:)", re.I)


def person_text(text: object, *, said: str = "git could not read its history.", fallback: str = _UNREADABLE) -> str:
    """``text`` for a screen: a message that carries a git command's own output is cut where that output starts and
    ends with ``said`` instead; one that is only git output reads ``fallback`` (the full text stays in ``detail``
    and on the CLI)."""
    line = _one_line(text)
    m = _RAW.search(line)
    if m is None:
        return line
    head = line[:m.start()].rstrip(" :;,")
    return f"{head}: {said}" if head else fallback


def join_digest(d, kind: str, handle: str, fp: str | None = None) -> str | None:
    """The hex sha256 of exactly what accepting the join ``(kind, handle)`` takes, as the derivation ``d`` has it now,
    or None when no such join exists. ONE function: the Joins row carries it as ``about`` and the cockpit's accept
    store recomputes it after its fetch, so a signature over ``about`` binds the thing accepted, not just its name.

    ``key``: the handle and the pending key LINE whose fingerprint is ``fp`` (the line holds the public key, so the
    fingerprint is bound with it). ``owner-offer``: the current owner and the offer itself (its email included: a
    re-offer to the same handle with another address is another digest)."""
    ten = d.tenure
    if kind == "key":
        line = None if fp is None else next((x for x in ten.pending_keys.get(handle, []) if _fp_or_none(x) == fp), None)
        if line is None:
            return None
        what: dict = {"kind": "key", "handle": handle, "line": line}
    elif kind == "owner-offer":
        off = ten.offer
        if not off or off.get("handle") != handle:
            return None
        what = {"kind": "owner-offer", "owner": d.team.owner, "offer": dict(off)}
    else:
        return None
    raw = json.dumps(what, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def people(d, you: str | None, own_fp: str | None = None) -> tuple[list[dict], list[dict]]:
    """Who is on the team and what waits on whom, from the one derivation ``d``. Pure: no I/O. No email field exists in
    either list (team.toml holds addresses; the zone needs handles). ``since`` is the date of the commit that began the
    handle's CURRENT spell, set only when ``role_changes`` names that commit, else None. ``own_fp`` is this machine's
    signing-key fingerprint: a join is ``mine`` when it waits on THIS machine (its proposed key is that key, or the
    offer is to ``you``), so only this machine can do the act it waits on. ``about`` is ``join_digest`` of the join:
    what a signed accept of it binds (a digest, never the email it may cover)."""
    team, ten = d.team, d.tenure
    dates = {sha: date for sha, date, _ in d.role_changes}
    began = {s.handle: s.start_sha for s in d.spells if s.end is None}
    members: list[dict] = []
    # the owner leads; team.toml may name an owner its [members] table does not list (its spell is seeded apart)
    for h in [team.owner, *sorted(set(team.members) - {team.owner})]:
        keys, pending = len(ten.keys.get(h, [])), len(ten.pending_keys.get(h, []))
        since = dates.get(began.get(h, ""))
        members.append({"handle": h, "role": "owner" if h == team.owner else "member", "you": h == you,
                        "keys": keys, "since": str(since)[:10] if since else None,
                        "key_state": "in force" if keys else ("waiting for confirm" if pending else "no key")})
    joins: list[dict] = []
    # third person always: ``you`` is matched by a key IN FORCE, so the person whose key is only proposed is never
    # ``you`` here, and a "your key" line would reach almost nobody it names
    for h in sorted(ten.pending_keys):
        for n, line in enumerate(ten.pending_keys[h]):
            fp = _fp_or_none(line)
            joins.append({"kind": "key", "handle": h, "n": n, "fingerprint": fp,
                          "mine": own_fp is not None and fp == own_fp, "about": join_digest(d, "key", h, fp),
                          "line": f"{h}'s key is proposed; it comes into force when {h} runs "
                                  "`levain team key confirm` on their own machine"})
    if ten.offer:
        # the offeree accepts with a key already in force, so ``you`` does reach them here
        h = str(ten.offer["handle"])
        who, run = ("you", "you run") if h == you else (h, f"{h} runs")
        joins.append({"kind": "owner-offer", "handle": h, "n": 0, "mine": you is not None and h == you,
                      "about": join_digest(d, "owner-offer", h),
                      "line": f"ownership is offered to {who}; it moves when {run} `levain team accept`"})
    return members, joins


def _own_fp(gl: LedgerTransport) -> str | None:
    """This machine's signing-key fingerprint, or None when it has no usable key (then no join is this machine's)."""
    try:
        return gl.own_fingerprint()
    except Exception:  # noqa: BLE001 - a missing key marks no join mine; it never fails the read
        return None


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
        model["members"], model["joins"] = people(d, model.get("you"), _own_fp(gl))
        if d.judged != "full":
            waiting = int(d.waiting or 0)
            why = ("Part of the team ledger could not be fully judged on this machine, so what follows may be out of date"
                   + (f"; {waiting} newer line{'s are' if waiting != 1 else ' is'} waiting." if waiting else "."))
            return TeamRead("judged_partial", model, why, _one_line(d.frozen_why or ""))
        return TeamRead("ok", model)
    except TeamError as exc:
        raw = " ".join(str(exc).split())
        return TeamRead("unreadable", None, person_text(raw) or _UNREADABLE, f"{type(exc).__name__}: {raw}")
    except Exception as exc:  # noqa: BLE001 - a surface drawing a team must get a named state, never a traceback
        return TeamRead("unreadable", None, _UNREADABLE, f"{type(exc).__name__}: {_one_line(exc)}")
