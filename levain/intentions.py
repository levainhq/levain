"""levain.intentions — the intention *disposition* vocabulary (control-plane Slice 3).

An intention's ``disposition`` routes it on the operator's List (loop fold, board item levain#4: there is NO loop
kind in Levain 1.0):
  - the **Tray** inbox (``someday`` / ``agenda``, plus a legacy ``handoff`` and the retired spelling ``seed``) —
    every open intention. A ``someday`` is the default: an intention with NO ``disposition`` key (what the entity's
    own ``intention_add`` writes, which cannot carry one) and the literal ``loop`` of an older release both READ as
    ``someday`` (:func:`disposition_of`). Nothing was stored for them, so a read-time default classifies the rows
    a write after any migration still creates; ``levain update`` also rewrites the open ones
    (:mod:`levain.intention_migrate`).
  - legacy **note** reference (``note``) — a note still in a store renders in the Tray, badged.
``loop`` is a retired write value (:data:`RETIRED_LOOP_MSG`) but stays in the READ set: a raw store holding the literal
still reads, as someday. A pinned-dormant row is the ``parked`` TIER, not a disposition; it stays on the List and
ranks last. ``seed`` resolved records keep that spelling for good, so it still reads, and no write sets it.

The field is ADDITIVE: anneal round-trips an unknown ``disposition`` untouched, which is why anneal needs NO change.

This module is the dashboard's render half: :func:`bucket_of` classifies an intention dict into the operator-facing
projection, and since the fold every row lands in the one Tray bucket.

⚠ **The "keep operator-I/O OUT of cognition" invariant, after the fold.** ``is_loop`` is the PREDICATE and it is now
False for every row: no class of intention flows into the entity's own cognition (collision recall was retired with the
loop kind, Phill np-c3029a8a (a); crystallized-pattern recall is the collision feature). The name and the byte-equal
``NON_COGNITION_DISPOSITIONS`` tuple stay for the drift contract below.

⚠ **Drift contract (load-bearing).** The taxonomy is duplicated in FOUR places BY DESIGN —
this module, the embedder's ``scripts/intentions.py``, and the TWO standalone hook copies (Claude +
Codex adapters, which can't import a package). anneal — the shared dependency — deliberately
does NOT carry the taxonomy (the de-risk keeps the canonical object's storage
disposition-blind), so every consumer that must INTERPRET disposition carries the vocab. A
NEW operator-I/O disposition added upstream must be added to ALL FOUR (and to the write
seam's ``choices``). Guards: the flow↔levain cross-repo equality test
(``flow/scripts/test_tray_disposition.py``); the hook↔hook parity + hook↔levain.intentions tests
(``levain/tests/test_hooks.py``). The ``is_loop`` definition below is the byte-for-byte twin
of the reference implementation's; keep them identical.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from levain.intention_legacy import OLD_ID_PREFIX, STORE_DOC_KEY_OPEN, STORE_DOC_KEY_OPEN_V1   # pure data; the one place the stored document's keys are spelled

__all__ = [
    "LOOP_DISPOSITION",
    "DEFAULT_DISPOSITION",
    "TRAY_DISPOSITIONS",
    "TRAY_WRITE_DISPOSITIONS",
    "RETIRED_HANDOFF_MSG",
    "RETIRED_KEEP_NOTE_MSG",
    "RETIRED_LOOP_MSG",
    "RETIRED_SEED_MSG",
    "RetiredDisposition",
    "retired_message",
    "refuse_retired_disposition",
    "guarded_intention_store",
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

LOOP_DISPOSITION = "loop"                            # RETIRED (the fold): read as someday, never written; the name stays for embedders
DEFAULT_DISPOSITION = "someday"                      # what an absent / empty / literal-loop disposition reads as
TRAY_DISPOSITIONS = ("someday", "seed", "handoff", "agenda")   # operator INBOX — the READ set (a legacy handoff still reads)
TRAY_WRITE_DISPOSITIONS = ("someday", "agenda")                # the Tray values a NEW write may take
# RETIRED: legacy rows still carry these. They stay in the READ sets (a legacy handoff, Keep note,
# seed or a literal ``loop`` still renders, closes, and re-dispositions to someday/agenda) but are
# OUT of the WRITE set (WRITE_DISPOSITIONS): a new handoff, Keep note, seed or loop is refused at
# the write seam. A resolved record keeps ``seed`` / ``loop`` for good (anneal's update cannot edit
# a resolved record).
RETIRED_DISPOSITIONS = ("handoff", "note", "seed", "loop")
KEEP_DISPOSITIONS = ("note",)                        # legacy operator REFERENCE — read-only vocabulary
# Everything the operator authored as I/O. The tuple is a drift-contract value (byte-equal to the
# hooks' ``_NON_COGNITION_DISPOSITIONS`` and the embedder's), so it stays as it was; the retired
# names stay in it too (a legacy note or handoff is still a known value).
NON_COGNITION_DISPOSITIONS = ("someday", "seed", "handoff", "agenda", "note")
# Two sets, two names. VALID: every value a row may HOLD (the read set; byte-equal to the embedder's
# ``VALID_DISPOSITIONS``, a drift-contract tuple: a raw store holding the literal ``loop`` still reads, FRAME.md D-5).
# WRITE: every value a write may SET.
VALID_DISPOSITIONS = (LOOP_DISPOSITION,) + NON_COGNITION_DISPOSITIONS
WRITE_DISPOSITIONS = TRAY_WRITE_DISPOSITIONS

RETIRED_LOOP_MSG = "loops are folded into the List: write someday, or agenda with a date"
RETIRED_HANDOFF_MSG = (
    "handoff is retired: ask the operator now with `levain ask`, "
    "or record a someday, or an agenda with a date, in the Tray"
)
RETIRED_KEEP_NOTE_MSG = (
    "the Keep is retired: a fact or command goes to memory (Durable Facts), "
    "an intention is a someday"
)
RETIRED_SEED_MSG = "seed is now called someday: write the disposition `someday`"


class RetiredDisposition(ValueError):
    """A write tried to SET a disposition outside the write set: a retired handoff / Keep note / seed / loop (any
    spelling anneal would persist as one), or a value nothing reads."""


def stored_disposition(value: Any) -> Any:
    """``value`` as anneal will persist it (``normalize_intention_field``: controls and trailing
    whitespace removed, NFC). A non-string is returned unchanged; anneal refuses it itself."""
    if not isinstance(value, str):
        return value
    from anneal_memory.spores import normalize_intention_field

    return normalize_intention_field(value)


def retired_message(value: Any) -> str | None:
    """The refusal text for a retired disposition value, in any spelling or case, else None."""
    stored = stored_disposition(value)
    key = stored.casefold() if isinstance(stored, str) else None
    if key == "handoff":
        return RETIRED_HANDOFF_MSG
    if key == "note":
        return RETIRED_KEEP_NOTE_MSG
    if key == "seed":
        return RETIRED_SEED_MSG
    if key == LOOP_DISPOSITION:
        return RETIRED_LOOP_MSG
    return None


def refuse_retired_disposition(value: Any, current: Any = None) -> None:
    """The write seams' early refusal (a clean 422 before the store is touched). A retired value
    is refused unless ``current`` already holds its persisted form: an echo, which the seam must then
    DROP from its write (the store guard, :func:`guarded_intention_store`, refuses every retired value it is
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


def guarded_intention_store(path: Any) -> Any:
    """An anneal ``IntentionStore`` that will not SET a disposition outside ``WRITE_DISPOSITIONS``
    through ``add``, ``update`` or ``apply`` (a planned cockpit effect that deserialized after an
    upgrade included), so no writer (``apply_edit``, the cockpit effects, a CLI, a migration) can
    route around the retirement. Legacy rows still read, and an update that does not name
    ``disposition`` edits one freely."""
    global _GUARDED
    _require_schema2_anneal()
    with _GUARDED_LOCK:
        if _GUARDED is None:
            _GUARDED = _build_guarded_class()
    return _GUARDED(path, id_prefix=NEW_ID_PREFIX)


def _require_schema2_anneal() -> None:
    """Refuse an installed anneal-memory that keys the store's open records differently from the key levain reads raw.

    The contract is the installed package's own ``OPEN_KEY``, compared with the one key levain spells
    (:data:`STORE_DOC_KEY_OPEN`), not a version number: it is the exact thing that breaks (an older anneal that is still
    installed through a checkout run directly, ``--no-deps`` or ``--no-pip`` accepts ``id_prefix``, loads and writes schema 1
    under the old key, and levain's raw reads of the new key then see an empty store). An anneal without the constant is
    an older one, and no hand-kept floor number is added beside the pin."""
    from anneal_memory import spores

    if getattr(spores, "OPEN_KEY", None) != STORE_DOC_KEY_OPEN:
        raise RuntimeError("levain needs anneal-memory>=2.0.0,<2.1 (the installed one keeps its open records in a schema "
                           "levain no longer reads): pip install -U 'anneal-memory>=2.0.0,<2.1'")


def _build_guarded_class() -> Any:
    from anneal_memory.spores import ApplyRefused, IntentionStore

    class GuardedIntentionStore(IntentionStore):  # type: ignore[misc]
        # keyword-only, as anneal's own add/update are: a positional disposition cannot exist
        def add(self, **kw: Any) -> Any:
            _refuse_unwritable(kw.get("disposition"))
            return super().add(**kw)

        def update(self, intention_id: str, **kw: Any) -> Any:
            _refuse_unwritable(kw.get("disposition"))
            return super().update(intention_id, **kw)

        def apply(self, effect: Any) -> Any:
            if getattr(effect, "op", None) in ("add", "update"):
                try:
                    _refuse_unwritable(dict(getattr(effect, "args", None) or {}).get("disposition"))
                except (ValueError, TypeError) as exc:   # RetiredDisposition, or malformed args: the base contract
                    raise ApplyRefused(str(exc)) from exc
            return super().apply(effect)

    return GuardedIntentionStore


# The tier value that means *deliberate dormancy* (anneal's own ``Tier`` literal — see
# ``anneal_memory.spores.VALID_TIERS``). A parked row is pinned-dormant (shown on the List, ranked last), exempt
# from the dormancy→compost prompt — NOT a disposition. Kept as a literal (not an anneal
# import) so this render-vocab module stays import-light; anneal owns the tier taxonomy.
PARKED_TIER = "parked"

# The operator-facing render buckets. Since the loop fold ``bucket_of`` returns ``BUCKET_TRAY`` for EVERY row: one List.
# ``BUCKET_LOOP`` stays exported, NEVER RETURNED since the fold, for the embedder's reads through 1.0 (flow's memory tile
# imports it; removing it would fault that tile).
BUCKET_LOOP = "loop"   # never returned since the fold (the Open loops panel is gone)
BUCKET_TRAY = "tray"   # every intention row, parked ones included, and a legacy ``note``


def disposition_of(item: Mapping[str, Any]) -> str:
    """The disposition an intention READS as. An absent, empty or ``None`` value and the literal ``loop`` (a retired
    name) all read as :data:`DEFAULT_DISPOSITION` (``someday``); any other string is returned as stored, so an
    unknown value is still shown by name.

    The default lives at READ because the entity's own write, anneal's ``intention_add``, cannot carry a disposition:
    a key-absent row is created after any migration. A client that snapshots ``someday`` for such a row is mapped to the
    raw compare-and-set value by the write seams (``levain.writes._client_snapshot``)."""
    d = item.get("disposition")
    if not d or d == LOOP_DISPOSITION:
        return DEFAULT_DISPOSITION
    return d


def is_loop(item: Mapping[str, Any]) -> bool:
    """False for every intention: there is no loop kind, so nothing flows into the entity's OWN cognition (salience /
    Top of Mind / digest / constellation agent seeds, collision recall). Loop fold, Phill np-10262267 (a) and
    np-c3029a8a (a).

    The function stays, with its name, as the drift-contract twin (the embedder's ``scripts/intentions.py:is_loop``, both
    hooks' ``_is_loop`` and this one change together); ``NON_COGNITION_DISPOSITIONS`` keeps its byte-equal tuple."""
    return False


def is_tray(item: Mapping[str, Any]) -> bool:
    """The operator session-I/O INBOX set (someday/agenda, plus a legacy handoff or seed row) — EXPLICIT membership. A
    key-absent row reads ``someday``, so it is Tray; a ``note`` is Keep reference and is not."""
    return disposition_of(item) in TRAY_DISPOSITIONS


def is_note(item: Mapping[str, Any]) -> bool:
    """The legacy durable-REFERENCE set (a ``note`` still in a store). Explicit membership."""
    return disposition_of(item) in KEEP_DISPOSITIONS


def bucket_of(item: Mapping[str, Any]) -> str:
    """The operator-facing render bucket: ``BUCKET_TRAY`` for EVERY intention (total onto the List: a loop, a someday, an
    agenda, a legacy note or handoff, an unknown disposition, any tier, a ``parked`` one included). ``BUCKET_LOOP`` is
    never returned since the loop fold; it stays exported for the embedder's reads through 1.0."""
    return BUCKET_TRAY


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


def date_on_event_agenda_refusal(intention: Mapping[str, Any] | None) -> str | None:
    """Why a DATE may not be set on ``intention`` (a row as stored), or None when it may: a part waits on an event or a date, never
    both. The one rule every write that sets ``next`` asks (the legacy writes and the planned effects), so no door is a way around it."""
    if not isinstance(intention, Mapping) or not is_event_agenda(intention):
        return None
    return (f"{intention.get('id')} waits on {intention.get('on_event')}, not on a date: a part waits on an event or a date, never "
            "both. It is asked about on its check-by day if the event has not come")


# --- the split (row 178 build 2): an agenda that waits on another intention's completion, not on a date -----------------
EVENT_DONE_PREFIX = "done:"
DEFAULT_CHECK_BY_DAYS = 7
# the descend kinds that mean the work FINISHED (anneal's per-type kinds); composted and dropped mean it did not, so
# they never wake a part that waits on completion
COMPLETION_KINDS = ("done", "answered", "explored")
_EVENT_DONE = re.compile(r"done:((?:" + OLD_ID_PREFIX.rstrip("-") + r"|intention)-[0-9A-Za-z_-]{1,56})")   # `spore-N` ids live in existing stores; the hooks pin this pattern
_CHECK_BY = re.compile(r"\d{4}-\d{2}-\d{2}")


def done_event(intention_id: str) -> str:
    """The event key that names ``intention_id`` finishing."""
    return EVENT_DONE_PREFIX + intention_id


def norm_event(key: Any) -> str:
    """``key`` with a ``done:`` id in one spelling (``spore-N`` and ``intention-N`` name the same intention), so two keys that
    name the same first part compare equal whichever spelling a writer used; anything else comes back as it is."""
    m = _EVENT_DONE.fullmatch(key) if isinstance(key, str) else None
    return f"{EVENT_DONE_PREFIX}intention-{m.group(1).partition('-')[2]}" if m else str(key or "")


def event_anchor(item: Mapping[str, Any]) -> str | None:
    """The intention id a ``done:<id>`` ``on_event`` waits on, else None (no event, or a kind this build does not wake)."""
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


def finished_by_completion(intention: Mapping[str, Any] | None) -> bool:
    """True when ``intention`` was resolved DOWNWARD with a completion kind (the thing it names finished). A missing intention, an
    open one, an ascend and a composted or dropped one are not."""
    if not isinstance(intention, Mapping) or intention.get("status") != "resolved":
        return False
    res = intention.get("resolution")
    return (isinstance(res, Mapping) and res.get("direction") == "descend" and res.get("kind") in COMPLETION_KINDS)


def is_talk_seed(item: Mapping[str, Any]) -> bool:
    """An open ``seed`` that carries ``attention: talk``: a thing to talk through with the operator (the talk queue).
    Such a seed does not go stale silently. Levain's own intention code has no TTL expiry or resurface step of its own
    (anneal's ``list_open`` ranks and ``germination_tier`` classifies; nothing in levain expires or resurfaces a seed),
    so this predicate is the contract hook for a caller that adds one."""
    d = disposition_of(item)   # a key-absent row reads someday (the loop fold)
    return (isinstance(d, str) and d.strip().casefold() in ("someday", "seed")
            and isinstance(item.get("attention"), str) and item["attention"].strip() == "talk")


def check_steering(disposition: Any, target: Any, attention: Any) -> str | None:
    """Why a write may not set ``target`` / ``attention`` on this disposition, or None when it may. ``target`` and
    ``attention`` steer how an AGENDA germinates, with ONE exception: a seed may carry ``attention: talk`` and only talk."""
    d = disposition.strip().casefold() if isinstance(disposition, str) else ""
    if target is not None and not valid_target(target):
        return f"target must be band, nudge, desk or board:<repo> (got {target!r})"
    if attention is not None and attention not in GERMINATE_ATTENTIONS:
        return f"attention must be one of {', '.join(GERMINATE_ATTENTIONS)} (got {attention!r})"
    if d in ("someday", "seed") and target is None and attention == "talk":
        return None
    if (target is not None or attention is not None) and d != "agenda":
        return ("target and attention steer how an AGENDA germinates; this intention is not one "
                "(the one exception: a seed may carry attention talk)")
    if attention not in (None, "none") and target == "desk":
        return "attention pings a band row, and a desk target files a drop, none; drop attention or use target band or nudge"
    if attention not in (None, "none") and isinstance(target, str) and target.startswith("board:"):
        return "attention pings a band row, and a board: target files none; drop attention or change the target"
    return None


def ringing_ref(intention: Mapping[str, Any], open_questions: Any) -> str | None:
    """The band row an OPEN agenda rings in, or None. The one definition of "rings": the intention is an open agenda, its
    ``germinated`` stamp names a ref that is not a desk drop (``dr-``), and that ref is one of ``open_questions``
    (the ids of the questions still open): an answered, snoozed-away or missing question does not ring. Read from the
    stored record, never a facet, so it holds whatever the List could or could not read."""
    stamp = intention.get("germinated")
    ref = stamp.get("ref") if isinstance(stamp, Mapping) else None
    if (intention.get("disposition") == "agenda" and intention.get("status", "open") == "open"
            and isinstance(ref, str) and ref and not ref.startswith("dr-") and ref in open_questions):
        return ref
    return None


def answered_descend_kind(intention_type: Any) -> str:
    """The descend kind that says an intention was answered, by the intention's type (anneal validates a kind against it)."""
    return {"task": "done", "question": "answered", "thought": "explored"}.get(str(intention_type), "composted")


def dropped_descend_kind(intention_type: Any) -> str:
    """The descend kind that says an intention was dropped, by the intention's type."""
    return {"task": "dropped", "question": "mooted", "thought": "dropped"}.get(str(intention_type), "composted")


def open_question_ids(questions: Any) -> frozenset[str]:
    """The ids of the open questions in ``questions`` (a ``QuestionStore``); a store that cannot be read has none."""
    try:
        return frozenset(q.id for q in questions.open_questions())
    except Exception:  # noqa: BLE001 - a guard that cannot read says nothing, it never blocks an unrelated act
        return frozenset()


def intention_ringing_question(intentions_json: Any, ref: Any) -> str | None:
    """The id of the open agenda whose stamp names the OPEN band row ``ref``, or None (no store, no such stamp, an unreadable
    store: a guard that cannot read says nothing, it never blocks an unrelated act)."""
    from pathlib import Path

    try:
        if not Path(intentions_json).is_file():
            return None
        for item in list_open_readonly(intentions_json):
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

    store = guarded_intention_store(path)
    data = store._load()   # noqa: SLF001 - the non-mutating read; `_load_keyed` is the one that saves
    if isinstance(data, dict) and STORE_DOC_KEY_OPEN_V1 in data and STORE_DOC_KEY_OPEN not in data:
        # this raw read must not turn a retired-key document into "no open intentions", whatever the loader above did
        raise RuntimeError(f"the store {path} is schema 1 (open records under {STORE_DOC_KEY_OPEN_V1!r}); convert it once: "
                           f"anneal-memory intention migrate-key --file {path}")
    items = [dict(s) for s in data.get(STORE_DOC_KEY_OPEN, []) if isinstance(s, dict)]
    for it in items:
        k = it.get("origin_key")
        if not isinstance(k, str) or not k:
            it["origin_key"] = "tmp-" + uuid.uuid4().hex
    now = datetime.now() if today is None else None
    return store._rank(items, today, now)   # noqa: SLF001
