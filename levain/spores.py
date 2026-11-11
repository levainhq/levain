"""levain.spores — the spore *disposition* vocabulary (control-plane Slice 3).

A spore's ``disposition`` routes it across THREE operator-facing classes:
  - ``loop`` (the default, absent/``loop``) — the entity's OWN prospective loop; belongs in
    its cognition (recall / salience / digest / Top of Mind).
  - the **Tray** inbox (``seed`` / ``handoff`` / ``agenda``) — operator session-I/O that
    should NOT pollute cognition; a ``seed`` metabolizes into a ``loop`` (or resolves). It
    FORMS, then resolves.
  - legacy **note** reference (``note``) — durable operator reference; persists until removed,
    resolve-EXEMPT (never germinates, never ascends; removed via descend). There is no Keep
    panel any more: a note still in a store renders in the Tray, and a pinned-dormant loop
    (the ``parked`` TIER) renders in Open loops, marked parked.
Tray + note are both ``NON_COGNITION``; lineage rides the existing ``pointer``.

The field is ADDITIVE: every pre-Slice-3 spore has no ``disposition`` key and reads as a
``loop`` (no migration; anneal round-trips an unknown ``disposition`` untouched — the
Slice-3 de-risk, proven empirically, is exactly why anneal needs NO change).

This module is the dashboard's render half: it CLASSIFIES a spore dict into the three
operator-facing projections — Open Loops / Tray — via :func:`bucket_of`.

⚠ **Scope of the "keep operator-I/O OUT of cognition" invariant.** ``is_loop`` is the
PREDICATE; who ENFORCES it depends on the surface. The embedder's own store enforces it at its
cognition reads (``scripts/spores.py`` + flow_state/constellation — the embedder's layer 2). A bare
LEVAIN install's cognition surfaces are its activation hooks
(``templates/activation/hooks/*`` — session-start dormant-surface + per-prompt collision):
3b ports ``is_loop`` there (the standalone hook copies filter on
``_NON_COGNITION_DISPOSITIONS``), so the boundary holds for a stranger too.

⚠ **Drift contract (load-bearing).** The taxonomy is duplicated in FOUR places BY DESIGN —
this module, the embedder's ``scripts/spores.py``, and the TWO standalone hook copies (Claude +
Codex adapters, which can't import a package). anneal — the shared dependency — deliberately
does NOT carry the taxonomy (the de-risk keeps the canonical object's storage
disposition-blind), so every consumer that must INTERPRET disposition carries the vocab. A
NEW operator-I/O disposition added upstream must be added to ALL FOUR (and to the write
seam's ``choices``). Guards: the flow↔levain cross-repo equality test
(``flow/scripts/test_tray_disposition.py``); the hook↔hook parity + hook↔levain.spores tests
(``levain/tests/test_hooks.py``). The ``is_loop`` definition below is the byte-for-byte twin
of the reference implementation's; keep them identical.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from datetime import datetime
from typing import Any

__all__ = [
    "LOOP_DISPOSITION",
    "TRAY_DISPOSITIONS",
    "TRAY_WRITE_DISPOSITIONS",
    "RETIRED_HANDOFF_MSG",
    "RETIRED_KEEP_NOTE_MSG",
    "RetiredDisposition",
    "retired_message",
    "refuse_retired_disposition",
    "guarded_spore_store",
    "KEEP_DISPOSITIONS",
    "RETIRED_DISPOSITIONS",
    "NON_COGNITION_DISPOSITIONS",
    "VALID_DISPOSITIONS",
    "WRITE_DISPOSITIONS",
    "stored_disposition",
    "PARKED_TIER",
    "BUCKET_LOOP",
    "BUCKET_TRAY",
    "disposition_of",
    "is_loop",
    "is_tray",
    "is_note",
    "bucket_of",
    "GERMINATE_ATTENTIONS",
    "check_steering",
    "is_talk_seed",
    "is_timed",
    "valid_next",
    "valid_target",
    "valid_retarget",
    "executor_of",
    "parse_target",
    "date_on_event_agenda_refusal",
    "EVENT_DONE_PREFIX",
    "DEFAULT_CHECK_BY_DAYS",
    "COMPLETION_KINDS",
    "done_event",
    "norm_event",
    "event_anchor",
    "valid_check_by",
    "is_event_agenda",
    "finished_by_completion",
]

LOOP_DISPOSITION = "loop"
TRAY_DISPOSITIONS = ("seed", "handoff", "agenda")   # operator INBOX — the READ set (a legacy handoff still reads)
TRAY_WRITE_DISPOSITIONS = ("seed", "agenda")        # the Tray values a NEW write may take
# RETIRED (TS-1): legacy rows still carry these. They stay in the READ sets (a legacy handoff
# or Keep note still renders, descends, and re-dispositions to seed/agenda) but are OUT of the
# WRITE set (WRITE_DISPOSITIONS): a new handoff or Keep note is refused at the write seam.
RETIRED_DISPOSITIONS = ("handoff", "note")
KEEP_DISPOSITIONS = ("note",)                        # legacy operator REFERENCE — read-only vocabulary
# Everything the operator authored as I/O — all excluded from the entity's OWN cognition.
# The retired names STAY: ``is_loop`` fails OPEN on an unknown value, so dropping them would
# flood every legacy note and handoff into the agent's collision surface. The order is the
# hook copies' tuple order (drift contract: byte-equal to ``_NON_COGNITION_DISPOSITIONS``).
NON_COGNITION_DISPOSITIONS = ("seed", "handoff", "agenda", "note")
# Two sets, two names. VALID: every value a row may HOLD (the read set; byte-equal to the embedder's
# ``VALID_DISPOSITIONS``, a drift-contract tuple). WRITE: every value a write may SET.
VALID_DISPOSITIONS = (LOOP_DISPOSITION,) + NON_COGNITION_DISPOSITIONS
WRITE_DISPOSITIONS = (LOOP_DISPOSITION,) + TRAY_WRITE_DISPOSITIONS

RETIRED_HANDOFF_MSG = (
    "handoff is retired: ask the operator now with `levain ask`, "
    "or record a seed, or an agenda with a date, in the Tray"
)
RETIRED_KEEP_NOTE_MSG = (
    "the Keep is retired: a fact or command goes to memory (Durable Facts), "
    "an intention is a seed"
)


class RetiredDisposition(ValueError):
    """A write tried to SET a disposition outside the write set: a retired handoff / Keep note (any
    spelling anneal would persist as one), or a value nothing reads."""


def stored_disposition(value: Any) -> Any:
    """``value`` as anneal will persist it (``normalize_spore_field``: controls and trailing
    whitespace removed, NFC). A non-string is returned unchanged; anneal refuses it itself."""
    if not isinstance(value, str):
        return value
    from anneal_memory.spores import normalize_spore_field

    return normalize_spore_field(value)


def retired_message(value: Any) -> str | None:
    """The refusal text for a retired disposition value, in any spelling or case, else None."""
    stored = stored_disposition(value)
    key = stored.casefold() if isinstance(stored, str) else None
    if key == "handoff":
        return RETIRED_HANDOFF_MSG
    if key == "note":
        return RETIRED_KEEP_NOTE_MSG
    return None


def refuse_retired_disposition(value: Any, current: Any = None) -> None:
    """TS-1, the write seams' early refusal (a clean 422 before the store is touched). A retired value
    is refused unless ``current`` already holds its persisted form: an echo, which the seam must then
    DROP from its write (the store guard, :func:`guarded_spore_store`, refuses every retired value it is
    handed). An undo that RESTORES a retired value is a change, so it is refused."""
    msg = retired_message(value)
    if msg is not None and stored_disposition(value) != current:
        raise RetiredDisposition(msg)


def _refuse_unwritable(value: Any) -> None:
    """THE store rule, one bound for every writer: a disposition a write NAMES is judged as anneal will
    persist it, against the write set (an allow-list, so no spelling of a retired or unknown value slips
    through). Anything else is refused, always: there is no echo. A caller that leaves a legacy row's
    disposition as it is omits the field."""
    if not isinstance(value, str):
        return   # absent / an explicit _UNSET / None (a clear); a non-str anneal refuses itself
    stored = stored_disposition(value)
    if not stored or stored in WRITE_DISPOSITIONS:
        return   # "" / whitespace is a clear, as anneal stores it
    raise RetiredDisposition(
        retired_message(stored)
        or f"disposition must be one of {list(WRITE_DISPOSITIONS)} (got {value!r})")


NEW_ID_PREFIX = "intention"   # a record this store mints (anneal 1.0.7 ``id_prefix``): ``intention-NNN``; ``spore-N`` ids still read
_GUARDED: Any = None
_GUARDED_LOCK = threading.Lock()   # anneal is imported lazily (this module stays stdlib-only), so build once under a lock


def guarded_spore_store(path: Any) -> Any:
    """An anneal ``SporeStore`` that will not SET a disposition outside ``WRITE_DISPOSITIONS``
    through ``add``, ``update`` or ``apply`` (a planned cockpit effect that deserialized after an
    upgrade included), so no writer (``apply_edit``, the cockpit effects, a CLI, a migration) can
    route around the retirement. Legacy rows still read, and an update that does not name
    ``disposition`` edits one freely."""
    global _GUARDED
    with _GUARDED_LOCK:
        if _GUARDED is None:
            _GUARDED = _build_guarded_class()
    try:
        return _GUARDED(path, id_prefix=NEW_ID_PREFIX)
    except TypeError as exc:
        if "unexpected keyword argument 'id_prefix'" not in str(exc):
            raise
        # anneal-memory before 1.0.7 has no id_prefix: say what to do, never a bare TypeError on every store open
        raise RuntimeError("levain needs anneal-memory>=1.0.7 (the installed one predates the intention id prefix): "
                           "pip install -U 'anneal-memory>=1.0.7,<1.1'") from exc


def _build_guarded_class() -> Any:
    from anneal_memory.spores import ApplyRefused, SporeStore

    class GuardedSporeStore(SporeStore):  # type: ignore[misc]
        # keyword-only, as anneal's own add/update are: a positional disposition cannot exist
        def add(self, **kw: Any) -> Any:
            _refuse_unwritable(kw.get("disposition"))
            return super().add(**kw)

        def update(self, spore_id: str, **kw: Any) -> Any:
            _refuse_unwritable(kw.get("disposition"))
            return super().update(spore_id, **kw)

        def apply(self, effect: Any) -> Any:
            if getattr(effect, "op", None) in ("add", "update"):
                try:
                    _refuse_unwritable(dict(getattr(effect, "args", None) or {}).get("disposition"))
                except (ValueError, TypeError) as exc:   # RetiredDisposition, or malformed args: the base contract
                    raise ApplyRefused(str(exc)) from exc
            return super().apply(effect)

    return GuardedSporeStore


# The tier value that means *deliberate dormancy* (anneal's own ``Tier`` literal — see
# ``anneal_memory.spores.VALID_TIERS``). A parked LOOP is pinned-dormant (shown in Open loops, marked parked), exempt
# from the dormancy→compost prompt — NOT a disposition. Kept as a literal (not an anneal
# import) so this render-vocab module stays import-light; anneal owns the tier taxonomy.
PARKED_TIER = "parked"

# The two operator-facing render buckets (the capture-UX (A) projection: ONE Tray, not
# three disposition panels — seed/handoff/agenda all land in Tray). There is no Keep panel
# (task surfaces TS-5a, Phill 11-07): a parked loop shows in Open loops with a parked marker,
# and a legacy ``note`` row still in a store shows in the Tray as legacy, never dropped.
BUCKET_LOOP = "loop"   # the entity's prospective loops, parked ones marked (Open Loops panel)
BUCKET_TRAY = "tray"   # the operator inbox awaiting AI triage + legacy notes (Tray panel)


def disposition_of(item: Mapping[str, Any]) -> str:
    """A spore with no ``disposition`` key is a normal prospective loop (the default
    for every pre-Slice-3 spore)."""
    return item.get("disposition") or LOOP_DISPOSITION


def is_loop(item: Mapping[str, Any]) -> bool:
    """True iff the spore flows into the entity's OWN cognition (salience / Top of Mind /
    digest / constellation agent seeds). The operator-I/O dispositions — the Tray inbox
    (seed/handoff/agenda) AND Keep reference (note) — are the complement, kept OUT of
    cognition.

    Fail-OPEN on the unknown: only an EXPLICITLY known operator-I/O disposition is
    excluded; a falsy/absent value (every pre-Slice-3 spore) OR an unrecognized string
    reads as in-cognition. The exclusion axis is the silent-harm one — an operator-I/O
    item leaking into a render is visible text, but a real loop wrongly bucketed out
    vanishes from the Open-Loops panel with no signal. An unknown value is almost always
    a typo / corruption / version-skew on a real loop — fail it toward the visible loop.
    (Byte-for-byte twin of the embedder's ``scripts/spores.py:is_loop``; keeping them identical
    is the drift contract above.)"""
    return disposition_of(item) not in NON_COGNITION_DISPOSITIONS


def is_tray(item: Mapping[str, Any]) -> bool:
    """The operator session-I/O INBOX set (seed/agenda, plus a legacy handoff row) — EXPLICIT membership,
    NOT ``not is_loop``: a ``note`` is also non-cognition but it is Keep reference, not
    the Tray, so the three classes (loop / tray / keep) are distinct."""
    return disposition_of(item) in TRAY_DISPOSITIONS


def is_note(item: Mapping[str, Any]) -> bool:
    """The operator durable-REFERENCE set (Keep notes) — resolve-exempt, agent-queryable,
    cognition-excluded. Explicit membership, the complement-within-Keep of a parked loop."""
    return disposition_of(item) in KEEP_DISPOSITIONS


def bucket_of(item: Mapping[str, Any]) -> str:
    """Classify a spore into its operator-facing render bucket — a TOTAL partition (every
    spore lands in exactly one):

    - ``BUCKET_TRAY`` — a Tray inbox disposition (seed/handoff/agenda), ANY tier, or a legacy
      ``note`` row (the Keep is gone; a note still in a store is shown, badged ``note``, never
      dropped). Disposition wins over tier.
    - ``BUCKET_LOOP`` — a loop on any tier, a ``parked`` one included (pinned-dormant, exempt
      from the dormancy→compost prompt; its row carries the parked marker).
    """
    d = disposition_of(item)
    if d in TRAY_DISPOSITIONS or d in KEEP_DISPOSITIONS:   # legacy handoff (in TRAY_DISPOSITIONS) and legacy note both render in the Tray
        return BUCKET_TRAY
    return BUCKET_LOOP


# --- THE LIST contract (task_surfaces_1107.md, np-7c5c8752 (a)): a timed ``next``, the ``desk`` target, talk seeds ----
GERMINATE_TARGETS = ("band", "nudge", "desk")           # plus ``board:<repo>``
GERMINATE_ATTENTIONS = ("none", "talk")                 # what an AGENDA's ``attention`` may be
_WHEN = re.compile(r"(\d{4}-\d{2}-\d{2})(?:T(\d{2}):(\d{2}))?")


def _parse_when(value: Any) -> datetime | None:
    """``next`` as a local naive datetime: exactly ``YYYY-MM-DD`` (midnight) or ``YYYY-MM-DDTHH:MM`` (local, 24 h, no
    zone, real calendar date and clock). Anything else, a hand-edited ``'2026-11-07 note'`` included, is None: every
    DUE test calls this (or :func:`is_timed` first), never a date-prefix read, which would take a timed value as midnight."""
    if not isinstance(value, str):
        return None
    m = _WHEN.fullmatch(value)
    if m is None:
        return None
    try:
        return datetime.strptime(value if m.group(2) else value + "T00:00", "%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def is_timed(value: Any) -> bool:
    """True when ``next`` carries a time of day (the ``T`` shape), valid or not: a malformed timed value is never read as a date."""
    return isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", value) is not None


def valid_next(value: Any) -> bool:
    """The strict write shape for ``next``: one of the two shapes above, nothing else."""
    return _parse_when(value) is not None


def valid_target(value: Any) -> bool:
    """A germinate target: ``band``, ``nudge``, ``desk`` or ``board:<repo>`` (a non-empty repo)."""
    if not isinstance(value, str):
        return False
    if value in GERMINATE_TARGETS:
        return True
    return value.startswith("board:") and bool(value[len("board:"):].strip())


_FLOW_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def valid_retarget(value: Any) -> bool:
    """A target the List's who control may WRITE: one the store's other reader (flow's germinate) also accepts. Flow takes a
    ``board:`` repo only as a plain repo name, a stricter reading than :func:`valid_target`'s non-empty one."""
    if not valid_target(value):
        return False
    return value in GERMINATE_TARGETS or _FLOW_REPO.fullmatch(value[len("board:"):]) is not None


def parse_target(raw: Any) -> tuple[str, str | None] | None:
    """The ONE reading of a stored ``target``, shared by germination and every caller that asks who acts: ``(kind, repo)`` with
    ``kind`` ``band``, ``nudge``, ``desk`` or ``board:<repo>``, the keyword matched without regard to case and the ends trimmed
    (the repo keeps its own case), or None when the value is not a target (no target included). A write is stricter
    (:func:`valid_target`); this is how a store that holds a hand-edited ``" Desk "`` is READ."""
    t = raw.strip() if isinstance(raw, str) else ""
    low = t.casefold()
    if low in GERMINATE_TARGETS:
        return low, None
    if low.startswith("board:") and t[len("board:"):].strip():
        repo = t[len("board:"):].strip()
        return f"board:{repo}", repo
    return None


def executor_of(target: Any) -> str | None:
    """Who acts once an agenda with this target wakes, derived and never stored: ``operator`` for a band row or a nudge (it
    rings in the band), ``machine`` for the desk or a board item. None for no target, or one that is not a target. Read through
    :func:`parse_target`, so it agrees with what germination does with the same stored value."""
    parsed = parse_target(target)
    if parsed is None:
        return None
    return "operator" if parsed[0] in ("band", "nudge") else "machine"


def date_on_event_agenda_refusal(spore: Mapping[str, Any] | None) -> str | None:
    """Why a DATE may not be set on ``spore`` (a row as stored), or None when it may: a part waits on an event or a date, never
    both. The one rule every write that sets ``next`` asks (the legacy writes and the planned effects), so no door is a way around it."""
    if not isinstance(spore, Mapping) or not is_event_agenda(spore):
        return None
    return (f"{spore.get('id')} waits on {spore.get('on_event')}, not on a date: a part waits on an event or a date, never "
            "both. It is asked about on its check-by day if the event has not come")


# --- the split (row 178 build 2): an agenda that waits on another intention's completion, not on a date -----------------
EVENT_DONE_PREFIX = "done:"
DEFAULT_CHECK_BY_DAYS = 7
# the descend kinds that mean the work FINISHED (anneal's per-type kinds); composted and dropped mean it did not, so
# they never wake a part that waits on completion
COMPLETION_KINDS = ("done", "answered", "explored")
_EVENT_DONE = re.compile(r"done:((?:spore|intention)-[0-9A-Za-z_-]{1,56})")
_CHECK_BY = re.compile(r"\d{4}-\d{2}-\d{2}")


def done_event(spore_id: str) -> str:
    """The event key that names ``spore_id`` finishing."""
    return EVENT_DONE_PREFIX + spore_id


def norm_event(key: Any) -> str:
    """``key`` with a ``done:`` id in one spelling (``spore-N`` and ``intention-N`` name the same intention), so two keys that
    name the same first part compare equal whichever spelling a writer used; anything else comes back as it is."""
    m = _EVENT_DONE.fullmatch(key) if isinstance(key, str) else None
    return f"{EVENT_DONE_PREFIX}intention-{m.group(1).partition('-')[2]}" if m else str(key or "")


def event_anchor(item: Mapping[str, Any]) -> str | None:
    """The spore id a ``done:<id>`` ``on_event`` waits on, else None (no event, or a kind this build does not wake)."""
    raw = item.get("on_event")
    m = _EVENT_DONE.fullmatch(raw) if isinstance(raw, str) else None
    return m.group(1) if m else None


def valid_check_by(value: Any) -> bool:
    """The strict write shape for ``check_by``: a real calendar day, ``YYYY-MM-DD``."""
    if not isinstance(value, str) or _CHECK_BY.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def is_event_agenda(item: Mapping[str, Any]) -> bool:
    """An open agenda that waits on an event (any ``on_event``, a kind this build wakes or not) and has not germinated: it
    wakes on its event, never on a date, and rings at its ``check_by`` so nothing waits silently."""
    d = item.get("disposition")
    return (isinstance(d, str) and d.strip().casefold() == "agenda" and bool(item.get("on_event"))
            and not item.get("germinated") and item.get("status", "open") == "open")


def finished_by_completion(spore: Mapping[str, Any] | None) -> bool:
    """True when ``spore`` was resolved DOWNWARD with a completion kind (the thing it names finished). A missing spore, an
    open one, an ascend and a composted or dropped one are not."""
    if not isinstance(spore, Mapping) or spore.get("status") != "resolved":
        return False
    res = spore.get("resolution")
    return (isinstance(res, Mapping) and res.get("direction") == "descend" and res.get("kind") in COMPLETION_KINDS)


def is_talk_seed(item: Mapping[str, Any]) -> bool:
    """An open ``seed`` that carries ``attention: talk``: a thing to talk through with the operator (the talk queue).
    Such a seed does not go stale silently. Levain's own spore code has no TTL expiry or resurface step of its own
    (anneal's ``list_open`` ranks and ``germination_tier`` classifies; nothing in levain expires or resurfaces a seed),
    so this predicate is the contract hook for a caller that adds one."""
    d = item.get("disposition")
    return (isinstance(d, str) and d.strip().casefold() == "seed"
            and isinstance(item.get("attention"), str) and item["attention"].strip() == "talk")


def check_steering(disposition: Any, target: Any, attention: Any) -> str | None:
    """Why a write may not set ``target`` / ``attention`` on this disposition, or None when it may. ``target`` and
    ``attention`` steer how an AGENDA germinates, with ONE exception: a seed may carry ``attention: talk`` and only talk."""
    d = disposition.strip().casefold() if isinstance(disposition, str) else ""
    if target is not None and not valid_target(target):
        return f"target must be band, nudge, desk or board:<repo> (got {target!r})"
    if attention is not None and attention not in GERMINATE_ATTENTIONS:
        return f"attention must be one of {', '.join(GERMINATE_ATTENTIONS)} (got {attention!r})"
    if d == "seed" and target is None and attention == "talk":
        return None
    if (target is not None or attention is not None) and d != "agenda":
        return ("target and attention steer how an AGENDA germinates; this spore is not one "
                "(the one exception: a seed may carry attention talk)")
    if attention not in (None, "none") and target == "desk":
        return "attention pings a band row, and a desk target files a drop, none; drop attention or use target band or nudge"
    if attention not in (None, "none") and isinstance(target, str) and target.startswith("board:"):
        return "attention pings a band row, and a board: target files none; drop attention or change the target"
    return None


def ringing_ref(spore: Mapping[str, Any], open_questions: Any) -> str | None:
    """The band row an OPEN agenda rings in, or None. The one definition of "rings": the spore is an open agenda, its
    ``germinated`` stamp names a ref that is not a desk drop (``dr-``), and that ref is one of ``open_questions``
    (the ids of the questions still open): an answered, snoozed-away or missing question does not ring. Read from the
    stored record, never a facet, so it holds whatever the List could or could not read."""
    stamp = spore.get("germinated")
    ref = stamp.get("ref") if isinstance(stamp, Mapping) else None
    if (spore.get("disposition") == "agenda" and spore.get("status", "open") == "open"
            and isinstance(ref, str) and ref and not ref.startswith("dr-") and ref in open_questions):
        return ref
    return None


def answered_descend_kind(spore_type: Any) -> str:
    """The descend kind that says an intention was answered, by the spore's type (anneal validates a kind against it)."""
    return {"task": "done", "question": "answered", "thought": "explored"}.get(str(spore_type), "composted")


def dropped_descend_kind(spore_type: Any) -> str:
    """The descend kind that says an intention was dropped, by the spore's type."""
    return {"task": "dropped", "question": "mooted", "thought": "dropped"}.get(str(spore_type), "composted")


def open_question_ids(questions: Any) -> frozenset[str]:
    """The ids of the open questions in ``questions`` (a ``QuestionStore``); a store that cannot be read has none."""
    try:
        return frozenset(q.id for q in questions.open_questions())
    except Exception:  # noqa: BLE001 - a guard that cannot read says nothing, it never blocks an unrelated act
        return frozenset()


def spore_ringing_question(spores_json: Any, ref: Any) -> str | None:
    """The id of the open agenda whose stamp names the OPEN band row ``ref``, or None (no store, no such stamp, an unreadable
    store: a guard that cannot read says nothing, it never blocks an unrelated act)."""
    from pathlib import Path

    try:
        if not Path(spores_json).is_file():
            return None
        for item in list_open_readonly(spores_json):
            if ringing_ref(item, (ref,)) == ref:
                return str(item.get("id"))
    except Exception:  # noqa: BLE001 - see the docstring
        return None
    return None


def list_open_readonly(path: Any, *, today: Any = None) -> list[dict[str, Any]]:
    """anneal's ``list_open`` WITHOUT its write: ``list_open`` backfills a missing ``origin_key`` and saves the store
    (and takes a ``.lock`` file), which a diagnostic or a dry run must never do. This reads the document, ranks it
    as ``list_open`` does, and gives a keyless row a TEMPORARY in-memory key (display only, never persisted)."""
    import uuid

    store = guarded_spore_store(path)
    data = store._load()   # noqa: SLF001 - the non-mutating read; `_load_keyed` is the one that saves
    items = [dict(s) for s in data.get("spores", []) if isinstance(s, dict)]
    for it in items:
        k = it.get("origin_key")
        if not isinstance(k, str) or not k:
            it["origin_key"] = "tmp-" + uuid.uuid4().hex
    now = datetime.now() if today is None else None
    return store._rank(items, today, now)   # noqa: SLF001
