"""levain.cockpit.graduation_cards — the graduation cards and steps for the Decisions view (14b DESIGN_REV2 §5,
DESIGN_REV3 §3, §6).

Two things, both read-only:

- a **card** for a graduation step no live anchored policy covers (:func:`card_of`, over a
  :class:`levain.autonomic.graduation.GraduationStep`): the binding, its family, the rung change, the counts, each
  action its links run with the manifest's risk for it, which clause failed, and the one ``levain binding policy
  create`` line that would cover it (narrowed to that binding's family, at the target rung, for 30 days), signed at a
  terminal with the operator's own key. The page never signs, and the card's observations are the harness's own.
- the **steps** taken (:func:`read_steps`): the ``graduation_steps`` audit rows of the autonomic store, newest
  first, read with a read-only connection that never creates or migrates the store. A FEED row: it
  authorizes nothing.

:func:`graduation_view` is what the proposals route adds to its payload.
"""
from __future__ import annotations

import shlex
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from levain.autonomic.coverage import GRADUATION_GRANT_MAX, risk_dict
from levain.autonomic.db import DB_NAME, STORE_FORMAT, default_store_dir
from levain.autonomic.risk import ActionRisk, RiskClass

__all__ = ["CARD_DAYS", "STEP_CAP", "card_of", "graduation_view", "policy_create_line", "read_steps"]

# the per-binding policy a card offers lasts the kind's ceiling (DESIGN_REV3 §3)
CARD_DAYS = GRADUATION_GRANT_MAX.days
# the newest steps the page shows
STEP_CAP = 20


def _signable(risk: ActionRisk | None) -> bool:
    # what the log's graduation kind takes (levain.authority's append_check): LOW or below, reversible, internal,
    # non-financial. A card offers no line the log would refuse
    return (risk is not None and int(risk.cls) <= int(RiskClass.LOW) and risk.reversible and not risk.external
            and not risk.financial)


def policy_create_line(actions: dict[str, ActionRisk], lowest: str, days: int, families: list[str] | None = None,
                       *, store_dir: str | None = None, data_root: str | None = None) -> str:
    """The one ``levain binding policy create`` line for these terms. ``store_dir`` is named when it is not the default
    autonomic store, ``data_root`` whenever it is known (the cockpit a bare line reaches depends on the directory it
    is run from), each absolute and resolved, so the line as copied signs that store's id into that cockpit's log."""
    parts = ["levain binding policy create"]
    for name, risk in sorted(actions.items()):
        parts += ["--action", shlex.quote(f"{name}={RiskClass(int(risk.cls)).name}")]
    parts += ["--lowest", lowest, "--days", str(days)]
    for fam in families or ():
        parts += ["--family", shlex.quote(fam)]
    # RULINGS_R2 item 3: both paths absolute and resolved, so the line signs the same store into the same cockpit
    # from whatever directory it is run in
    if store_dir is not None:
        store = Path(store_dir).expanduser().resolve()
        if store != default_store_dir():
            parts += ["--store-dir", shlex.quote(str(store))]
    if data_root is not None:
        parts += ["--data-root", shlex.quote(str(Path(data_root).expanduser().resolve()))]
    # the guard is his to author (its kill and its drill); the line names where it goes, never a guard for him
    return " ".join(parts + ["--guard <guard.json>", "--signing-key <key>"])


def card_of(step: Any) -> dict[str, Any]:
    """The card for one step :func:`~levain.autonomic.graduation.graduate_due` did not take. ``command`` is ``None``
    when no policy the log takes could cover it (an action the manifest does not declare, or one whose risk the
    graduation kind refuses, or a link whose action cannot be named)."""
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
    signable = bool(step.links) and step.outcome == "uncovered" and all(_signable(l.action_risk) for l in step.links)
    named = {link.action_name: link.action_risk for link in step.links if link.action_risk is not None}
    command = (policy_create_line(named, to, CARD_DAYS, [step.family], store_dir=getattr(step, "store_dir", None),
                                  data_root=getattr(step, "cockpit_root", None)) if signable else None)
    return {"binding_id": p.old_binding_id, "family": step.family, "from": p.from_posture.name, "to": to,
            "clean_count": p.clean_count, "fire_count": p.fire_count, "actions": actions, "why": "; ".join(why),
            "command": command,
            "note": (f"lets {', '.join(sorted(named))} run at {to} without asking, for {CARD_DAYS} days; Levain uses "
                     "it for this binding" if command else "")}


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
