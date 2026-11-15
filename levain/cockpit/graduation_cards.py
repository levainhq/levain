"""levain.cockpit.graduation_cards — the graduation cards and steps for the Decisions view (14b DESIGN_REV2 §5,
DESIGN_REV3 §3, §6).

Two things, both read-only:

- a **card** for a graduation step no live anchored policy covers (:func:`card_of`, over a
  :class:`levain.autonomic.graduation.GraduationStep`): the binding, its family, the rung change, the counts, each
  action its links run with the manifest's risk for it, which clause failed, and the way back for a graduation
  policy (``NO_POLICY_NOTE``): this levain signs none, so a card offers no command. The page never
  signs, and the card's observations are the harness's own.
- the **steps** taken (:func:`read_steps`): the ``graduation_steps`` audit rows of the autonomic store, newest
  first, read with a read-only connection that never creates or migrates the store. A FEED row: it
  authorizes nothing.

:func:`graduation_view` is what the proposals route adds to its payload.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from levain.autonomic.coverage import GRADUATION_GRANT_MAX, risk_dict
from levain.autonomic.db import DB_NAME, STORE_FORMAT
from levain.autonomic.risk import ActionRisk, RiskClass

__all__ = ["CARD_DAYS", "STEP_CAP", "card_of", "graduation_view", "read_steps"]

# a card offers no command: it states what is true of a step no policy covers, in its own words (a card has no grant,
# stopped or otherwise, so a stopped grant's way back does not fit it)
NO_POLICY_NOTE = ("this levain signs no graduation policy, so nothing can cover this step here; a policy already in "
                  "the authority log can only be revoked")
CARD_DAYS = GRADUATION_GRANT_MAX.days
# the newest steps the page shows
STEP_CAP = 20


def _signable(risk: ActionRisk | None) -> bool:
    # what the log's graduation kind takes (levain.authority's append_check): LOW or below, reversible, internal,
    # non-financial. A card offers no line the log would refuse
    return (risk is not None and int(risk.cls) <= int(RiskClass.LOW) and risk.reversible and not risk.external
            and not risk.financial)


def card_of(step: Any) -> dict[str, Any]:
    """The card for one step :func:`~levain.autonomic.graduation.graduate_due` did not take. ``command`` is ``None``:
    this levain has no verb that creates a graduation policy, so the card names none and ``note`` says so."""
    p = step.proposal
    to = p.to_posture.name
    actions = [{"link": link.link_index, "action": link.action_name, "risk": risk_dict(link.action_risk),
                "covered": link.covers(p.to_posture)} for link in step.links]
    why: list[str] = []
    if step.outcome == "unresolved":
        why.append("an action one of its links runs cannot be named, so nothing covers it")
    for link in step.uncovered:
        if link.action_risk is None:
            why.append(f"the manifest does not declare {link.action_name}")
        elif not _signable(link.action_risk):
            why.append(f"{link.action_name} is above what a graduation policy may sign (LOW, reversible, internal, "
                       "non-financial)")
        else:
            why.append(f"no live policy, signed by a key root lists, lets {link.action_name} run at {to}")
    if step.outcome == "unattributed":
        why.append("a policy covers it, but which policy cannot be named, or it names no min clean fires: "
                   "nothing graduates")
    elif step.outcome not in ("uncovered", "unresolved"):
        why.append(f"the step was refused: {step.outcome}")
    return {"binding_id": p.old_binding_id, "family": step.family, "from": p.from_posture.name, "to": to,
            "clean_count": p.clean_count, "fire_count": p.fire_count, "actions": actions, "why": "; ".join(why),
            "command": None, "note": NO_POLICY_NOTE}


def read_steps(store_dir: Path | str, *, limit: int = STEP_CAP) -> list[dict[str, Any]]:
    """The newest ``limit`` audit rows of the store in ``store_dir``, each with its FEED line. No store, a store of
    another format, one without the table, or a ``limit`` below 1: no rows."""
    db = Path(store_dir) / DB_NAME
    if limit <= 0 or not db.is_file():          # sqlite reads LIMIT -1 as every row
        return []
    conn = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=0.5)
    try:
        marker = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
        if marker is None or marker[0] != STORE_FORMAT:
            return []
        rows = conn.execute("SELECT step, at, old_id, new_id, family, from_posture, to_posture, clean_count, "
                            "fire_count, policy_id, incarnation, authority_head FROM graduation_steps "
                            "ORDER BY step DESC LIMIT ?", (int(limit),)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    keys = ("step", "at", "old_id", "new_id", "family", "from", "to", "clean_count", "fire_count", "policy_id",
            "incarnation", "authority_head")
    out = []
    for row in rows:
        d = dict(zip(keys, row))
        d["line"] = (f"graduated {d['old_id']} {d['from']} -> {d['to']} under {d['policy_id']}, "
                     f"{d['clean_count']}/{d['fire_count']} clean")
        out.append(d)
    return out


def graduation_view(store_dir: Path | str | None, engine: Any = None) -> dict[str, Any]:
    """``{"steps": ..., "cards": ...}`` for the page. ``engine`` is the binding driver's graduation engine (a callable
    taking ``apply``), asked here to preview only; ``None`` (no driver wired) gives no cards. A failure of either half
    gives that half empty."""
    steps = read_steps(store_dir) if store_dir is not None else []
    cards: list[dict[str, Any]] = []
    if engine is not None:
        try:
            cards = [card_of(s) for s in engine(False) if s.outcome not in ("graduated", "preview")]
        except Exception:  # noqa: BLE001 - a card that cannot be built is not shown; the gate still holds the binding
            cards = []
    return {"steps": steps, "cards": cards}
