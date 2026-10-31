"""levain.wrap — `levain wrap <entity>`: the consolidate for a sovereign entity.

An OpenHands Levain entity CAPTURES every turn to its own episodic store (``levain run`` +
:class:`~levain.firing.anneal.AnnealEntityFiring`), but the firing is CONSTITUTIONALLY FORBIDDEN
from consolidating on its own ("captures but never consolidates" — the afferent/efferent membrane).
So raw episodes pile up and never metabolize into felt memory: the entity never compounds identity
across sessions. ``levain wrap`` is the invocation that RUNS the consolidate.

⚠ **THIS MODULE WAS "THE HUMAN-GATED CONSOLIDATE" UNTIL K4a [6], AND THAT SENTENCE IS NOW FALSE IN
ONE CASE — read the amendment before trusting any human-presence claim below.** A scheduled seat
(``levain daemon install-seat``) consolidates ITSELF, with nobody present. Two things bound it, and
neither is the human:

- **A WALL-CLOCK BOUND** (``max_seconds``). K4a ⑥ bounded ``levain run`` and did NOT reach here — a
  fact found only by the [6] handoff audit. Unbounded, the first stalled compose on a seat's
  schedule hangs the process, launchd COALESCES per label, and the seat stops running forever behind
  a unit still reporting installed and loaded. That is ``guard_scoped_by_symptom_misses_the_class``:
  ⑥ bounded the place the symptom was found, while the CLASS is *any unattended long-running Levain
  invocation*, and this is its second member. The bound therefore lives on the COMMAND, not on the
  seat that calls it — so an operator who cron-drives ``levain wrap`` directly is covered too.
- **A CRYSTALLIZATION REFUSAL** (``unattended``). An unattended consolidate MAY METABOLIZE the
  working neocortex but MAY NEVER promote into the always-loaded bedrock tier — enforced by a
  refusing proxy, not by a convention (:mod:`levain.firing.crystallization`).

What survives unchanged, and was always the load-bearing half: **the FIRING never consolidates.** No
in-turn afferent path can trip a memory write. A seat's consolidate happens after its turn is over,
from its own invocation, under both bounds above.

The consolidate is anneal's three-beat move, and we COMPOSE with anneal's API (never reinvent it):

  1. ``prepare_wrap(store, crystal_store=…, wrap_token=…)`` — anneal opens the wrap under Levain's
     token, freezes the episode window, and emits the compression package (episodes + current
     memory + stale-pattern warnings + the compression INSTRUCTIONS themselves + association
     context).
  2. **compose** — the ONE cognitive beat: a mind reads the package and writes the
     neocortex. For a SOVEREIGN entity that mind is its OWN model (the same open Ollama model
     ``levain run`` uses), so the entity metabolizes its own episodes with its own mind — an OPEN
     model on its OWN memory, not a frontier model on foreign memory. (Per the master-plan "own the
     substrate, rent the channel": the memory and the open-model *choice* are owned; the compute
     endpoint — local Ollama or Ollama Cloud, so the episode content does leave the box unless the
     endpoint is local — is the rentable channel.) ``--composer`` overrides the compose model to a
     stronger one per-wrap when the operator wants a higher-quality wrap, WITHOUT giving up the
     sovereign default.
  3. ``validated_save_continuity(store, text, wrap_token=…, crystal_store=…, allow_shrink=False)`` —
     the gated efferent write: structure-validate → graduation → save. FAIL-CLOSED:
     a malformed compose (a weak model dropping a required section or mis-citing episodes) RAISES and
     leaves the wrap in progress — a broken identity is never written. We then cancel it so a plain
     re-run works; the frozen episodes stay visible to the next wrap (``wrap_cancelled`` records no
     completed-wrap watermark), losing nothing.

**Isolation (the #1 requirement).** The consolidate reads/writes ONLY the entity's own stores under
``<entity>/.levain/`` and NEVER the operator's ``~/.anneal-memory/``. The LLM never touches the store —
anneal does all store I/O in-process — so isolation here is simply the pure fail-closed guard
(:func:`~levain.firing.isolation.assert_entity_isolated`) over the explicitly-derived store paths,
BEFORE either store is opened. No ``$LEVAIN_ENTITY_DIR``, no firing kind: the wrap holds the
isolated ``Store`` / ``CrystalStore`` handles directly.

Requires the ``openhands`` extra for the compose beat (``pip install 'levain[openhands]'``) — the
same extra ``levain run`` needs, and the operator who is wrapping an OpenHands entity already has it.
The heavy imports are LAZY so ``levain --help`` / ``import levain.wrap`` work without the extra.
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from levain.firing.crystallization import CrystallizationRefused
from levain.firing.deadline import HARD_EXIT_GRACE_SECONDS, TurnDeadline, TurnTimeout
from levain.firing.isolation import (
    ENTITY_STORE_SUBDIR,
    IsolationError,
    assert_entity_isolated,
    entity_store_paths,
)
from levain.run import _resolve_llm_kwargs, _resolve_model, require_openhands_entity
from levain.session import EXIT_TIMEOUT

__all__ = ["wrap_entity"]

_log = logging.getLogger("levain.wrap")

# Sentinel: the per-entity single-writer lock is held by ANOTHER live consolidate.
_ANOTHER_WRAP_RUNNING = object()

# The default compose model — the sovereign default, the SAME open model `levain run` boots the
# entity on (an entity wraps its OWN memory with its OWN mind). `--composer <model>` overrides it
# (e.g. to a stronger model for a higher-quality wrap) without surrendering the sovereign default.
DEFAULT_COMPOSE_MODEL = "glm-5.2:cloud"

# Compose runs through the same /v1 native route as `levain run` (`_resolve_llm_kwargs`), so the wrap
# model matches the run model — not the `ollama/` provider, which returns EMPTY content for glm (the
# completion-mode sibling of the tool-call route bug). A REASONING model (glm-5.2) spends output tokens
# on its chain-of-thought BEFORE emitting the answer to `content`; a low cap starves the answer (glm
# returned empty content until the budget was raised). This generous ceiling leaves room for the CoT
# PLUS the full neocortex — it is a cap, not a target (the model stops when done), and a truncated
# compose still fails CLOSED at `validated_save_continuity`, never saving a partial. (bench 2026-07-17:
# glm composed a clean 6-section neocortex via /v1 at this budget; the `ollama/` route gave 0 chars.)
_COMPOSE_MAX_OUTPUT_TOKENS = 16384

# Slack added on top of (bound + hard-exit grace) before an in-progress wrap counts as an ORPHAN an
# unattended seat may discard. Covers clock skew and the gap between "the process was killed" and
# "the timestamp we are comparing against" — deliberately generous, because the asymmetry is stark:
# waiting one extra cadence costs a delayed consolidate, discarding a live peer's wrap destroys work.
_ORPHAN_MARGIN_SECONDS = 300.0
# The staleness horizon when the consolidate is explicitly UNBOUNDED, where no bound-derived horizon
# exists. One hour: longer than any healthy compose (a measured 12-episode wrap is ~22s) and short
# enough that a genuinely dead seat recovers within a cadence or two.
_ORPHAN_FALLBACK_SECONDS = 3600.0


class _ComposeUnavailable(RuntimeError):
    """The compose model could not be reached at all (missing ``openhands`` extra). Distinct from a
    model that ran and produced bad output — this is an environment error (exit 2), that is a wrap
    failure (exit 1)."""


def _lock_wrap(entity_dir: Path) -> "int | None | object":
    """Take the per-entity SINGLE-WRITER lock (``<entity>/.levain/wrap.lock``, non-blocking exclusive
    ``flock``) so only ONE consolidate touches an entity's store at a time.

    anneal's wrap lifecycle is single-writer-PER-PROCESS and DEFERS cross-process safety to the caller
    (``Store.wrap_started`` documents its SELECT→INSERT is "not a cross-connection guarantee") — so a
    new caller like ``levain wrap`` must serialize itself, exactly as anneal's own ``continuity_lock``
    and the reference dualwrite driver do. Without it, two concurrent ``levain wrap`` on one entity can
    race the prepare and the loser's failure path can cancel the winner's live wrap (apparatus L3
    complement/codex/nemotron consensus).

    Returns the held fd (release via :func:`_unlock_wrap`), ``None`` when ``fcntl`` is unavailable
    (Windows — best-effort proceed; the token-owned cancel is the point-of-use fallback), or
    :data:`_ANOTHER_WRAP_RUNNING` when a concurrent consolidate holds it."""
    return _lock_wrap_file(entity_dir / ENTITY_STORE_SUBDIR / "wrap.lock")


def _lock_wrap_file(lock_path: Path) -> "int | None | object":
    """:func:`_lock_wrap` for a lock FILE path (the store-path core takes ``<store dir>/wrap.lock``, which for an entity
    is ``<entity>/.levain/wrap.lock``). Same return values."""
    try:
        import fcntl
    except ImportError:
        return None
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return _ANOTHER_WRAP_RUNNING
    return fd


def _unlock_wrap(handle: "int | None | object") -> None:
    """Release + close a lock handle from :func:`_lock_wrap`. A no-op for ``None`` (no fcntl) or the
    ``_ANOTHER_WRAP_RUNNING`` sentinel (never acquired). Never raises."""
    if not isinstance(handle, int):
        return
    try:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001 — release is best-effort; the fd close below still runs
        pass
    finally:
        try:
            os.close(handle)
        except OSError:
            pass


AFFECT_DEPRECATED = (  # printed once by `levain wrap` (cli.py) when either flag was given
    "levain wrap: --affect-tag / --affect-intensity are deprecated and ignored (anneal no longer tags "
    "associations); the consolidate is unchanged."
)


def _wrap_in_progress(store: object) -> bool:
    """Is a wrap still in progress on ``store``? A True means a wrap is open (a refused compose leaves
    ours open); it does NOT mean "nothing was committed", and a False does not mean "committed": with
    no wrap in progress our save may have committed or another program may have cleared our wrap, so
    the callers settle that with :func:`_last_wrap_id`. Fail-safe to ``True`` on a read error."""
    try:
        return bool(store.get_wrap_started_at())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — unreadable state → assume in-progress (safe default)
        return True


def _last_wrap_id(store: object) -> int | None:
    """The id of the last COMPLETED wrap, or ``None`` when it cannot be read. Read before the save so
    that, after a failed save, "did this wrap commit" is a comparison and not an inference from the
    in-progress flag (a peer that clears our wrap also leaves that flag clear)."""
    try:
        return int(store.last_wrap_id())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — unreadable → unknown, never a guess
        return None


def _say(message: str) -> None:
    """Print ``message`` to stdout, falling back to stderr, and never raise: the one caller reports a
    result after the memory is already saved, and a broken stream must not turn that into a crash."""
    for stream in (sys.stdout, sys.stderr):
        try:
            print(message, file=stream, flush=True)
            return
        except Exception:  # noqa: BLE001
            continue


def _unwrapped_count(store: object) -> int | None:
    """How many episodes still await consolidation, or ``None`` when that cannot be read."""
    try:
        return len(store.episodes_since_wrap())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — unreadable → unknown, never a guess
        return None


_SAVE_UNREADABLE = (
    "levain wrap: the save failed ({kind}: {exc}) and the store could not be read well enough to say "
    "whether this memory was recorded.\n"
    "  Check with `levain wrap --dry-run`: it says there is nothing to consolidate if the episodes are "
    "consolidated; look at the entity's .levain/memory.continuity.md before re-running."
)

_SAVE_UNKNOWN = (
    "levain wrap: the save failed ({kind}: {exc}) and the store does not show whether THIS memory was "
    "recorded.\n"
    "  No wrap of this run is in progress: its episodes may already be consolidated, or another program "
    "may have cleared or replaced its wrap.\n"
    "  Check with `levain wrap --dry-run`: it says there is nothing to consolidate if the episodes are "
    "consolidated; look at the entity's .levain/memory.continuity.md before re-running."
)


def _cancel_outcome(store: object, wrap_token: object) -> str:
    """Cancel the in-progress wrap ONLY if it is still OURS (its token equals ``wrap_token``), and say
    which of three things happened: ``"cancelled"`` (ours was open and is now cleared),
    ``"none_open"`` (the store holds no wrap of ours: idle, or another token's wrap, which is left
    alone) or ``"failed"`` (nothing can be said: no token to compare, a cancel that raised, or
    anneal reporting a partial lifecycle state). Never raises.

    The point-of-use guard against cancelling a concurrent peer's live wrap in our failure path — the
    ``loser-cancels-winner`` race. Correct even if the process lock were somehow bypassed
    (``invariant_must_fire_at_the_point_of_use``): a bare ``wrap_cancelled`` clears WHATEVER wrap is in
    the store, which after a race is the winner's."""
    # anneal compares the token and clears the wrap in one step (expect_token); reading the snapshot
    # and then calling a bare cancel let another client's new wrap be cleared in between (codex, the
    # 0.5.6 hunk look). WrapOwnershipError means the in-progress wrap is not ours: leave it.
    # Without a token there is nothing to compare, and wrap_cancelled(expect_token=None) is anneal's
    # bare cancel, which clears whatever wrap is open. `wrap_entity` always holds one, so this only
    # guards a caller that does not: leave the store alone rather than clear a wrap we cannot name,
    # and report it unresolved, since a wrap may be open that nobody cancelled.
    if not wrap_token:
        _log.warning("cancel-if-ours: no wrap token to compare, nothing cancelled")
        return "failed"
    try:
        from anneal_memory import WrapOwnershipError

        try:
            store.wrap_cancelled(expect_token=wrap_token)  # type: ignore[attr-defined]
        except WrapOwnershipError as exc:
            # Idle or another token's wrap: nothing of ours is open. A partial state (lifecycle keys
            # set, no usable token) is not that: it may be our wrap, damaged, still blocking the next.
            if getattr(exc, "partial_state", False):
                _log.warning("cancel-if-ours: the store's wrap state is partial; not cancelled")
                return "failed"
            return "none_open"
        return "cancelled"
    except Exception as exc:  # noqa: BLE001 — a guard must not raise into a failure path
        _log.warning("cancel-if-ours failed (%s): %s", type(exc).__name__, exc)
        return "failed"


def _cancel_if_ours(store: object, wrap_token: object) -> bool:
    """:func:`_cancel_outcome` as a yes/no: True only when no wrap of ours can still be open (ours was
    cancelled, the store is idle, or the open wrap carries another token); False when that cannot be
    established. Callers print :data:`_CANCEL_FAILED` when it is False, and a dry run exits 1."""
    return _cancel_outcome(store, wrap_token) != "failed"


_CANCEL_FAILED = (
    "  ⚠ The prepared wrap could NOT be cancelled, so the next "
    "`levain wrap` will find it in progress. Once nothing else is using this entity, run "
    "`levain wrap --reset` to discard it; the episodes are safe."
)


def _orphan_is_stale(started: object, bound: float | None) -> bool:
    """Is an in-progress wrap old enough that no live writer could still legitimately hold it?

    **THE SECOND HALF OF THE AUTO-DISCARD'S SAFETY ARGUMENT, and it exists because the first half is
    not sufficient (codex, L3, MED).** Holding ``wrap.lock`` proves no other *Levain* consolidate is
    running — but that lock is LEVAIN'S OWN INVENTION, taken precisely because anneal defers
    cross-process safety to its caller. anneal's own lifecycle does not take it. So an
    ``anneal-memory`` CLI call, an MCP tool, or any other library user can be mid-``prepare_wrap`` on
    this same store while we hold a lock they never heard of, and "in progress ⇒ orphan" is false for
    exactly that writer. Discarding there would cancel someone's LIVE wrap.

    AGE CLOSES IT, and not as a heuristic — as a consequence of how orphans are made. A wrap is
    stranded only when its process DIED mid-flight, and the thing that kills it is the wall-clock
    bound (or a crash). So an orphan is necessarily OLDER than the bound that killed it, while a live
    peer's wrap is necessarily YOUNGER than the bound it is still running under. Requiring the age to
    exceed bound + grace + margin therefore discards the one and spares the other.

    Fails SAFE in every ambiguous direction: an unparseable or absent timestamp returns ``False``
    (refuse to discard), because the cost of skipping one consolidate is a delayed wrap, and the cost
    of a wrong discard is destroying someone's live work.
    """
    from datetime import datetime, timezone

    if not started:
        return False
    # A bound of None (explicitly unbounded) has no natural staleness horizon, so fall back to a
    # conservative fixed one rather than inventing a smaller number from nothing.
    horizon = (bound + HARD_EXIT_GRACE_SECONDS + _ORPHAN_MARGIN_SECONDS) if bound else _ORPHAN_FALLBACK_SECONDS
    try:
        text = str(started).replace("Z", "+00:00")
        ts = datetime.fromisoformat(text)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError) as exc:
        _log.debug("orphan staleness undecidable (%s): %s", type(exc).__name__, exc)
        return False
    return age >= horizon


def _cancel_our_wrap(store: object, wrap_token: str) -> str:
    """Cancel our wrap on the OUT-OF-BAND exit path (timeout / interrupt / refusal), by TOKEN.

    The sibling of :func:`_cancel_if_ours` for the ``except BaseException`` clause. Levain mints the
    token BEFORE ``prepare_wrap`` and hands it to anneal (``wrap_token=``), so an exit that lands
    inside ``prepare_wrap``, after anneal has marked the wrap started and before it returned,
    still holds the token that names that wrap. The cancel is anneal's compare-and-swap: it clears
    the wrap only if it carries this token, so it cannot clear a wrap some other program started
    (``wrap.lock`` is Levain's own lock; an ``anneal-memory`` CLI or MCP client never takes it). A
    wrap opened with a caller token is also token-bound in anneal, so no tokenless cancel can clear
    it by accident. Before ``prepare_wrap`` has opened anything the compare finds the store idle and
    does nothing.

    Never raises: cleanup must not replace the exception it is cleaning up after. Returns what
    :func:`_cancel_outcome` found (``cancelled`` / ``none_open`` / ``failed``)."""
    try:
        return _cancel_outcome(store, wrap_token)
    except Exception as exc:  # noqa: BLE001 — never mask the out-of-band exit being propagated
        _log.debug("out-of-band wrap cancel skipped (%s): %s", type(exc).__name__, exc)
        return "failed"


def _note_cancel_outcome(stop: BaseException, outcome: str) -> None:
    """Record the cancel outcome on the stop being propagated, for the timeout report. Best effort: an
    exception type that rejects the attribute must not replace the stop it was raised for."""
    try:
        stop.levain_wrap_cancel = outcome  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _observe_prior_wrap(store: object) -> tuple[str | None, str | None]:
    """``(token, started_at)`` of the wrap now open, the TOKEN READ FIRST.

    The age proof (:func:`_orphan_is_stale`) is about the wrap whose ``started_at`` was read, and
    the token that gets cancelled must name a wrap no younger than that. Reading the token first
    gives that: if the wrap is replaced in between, ``started_at`` belongs to the newer wrap while
    the token names the old one, so the cancel by that token fails its compare-and-swap instead of
    clearing the newcomer (L2: with the token read after the age check, a peer's fresh wrap landing
    in the gap was the one cancelled).

    The store's bound token (the token a caller handed to ``prepare_wrap``; every wrap ``levain
    wrap`` opens carries one, and so can a wrap another program opened) is read BEFORE the snapshot,
    so that when the snapshot cannot be read the earliest observation is the one cancelled: a wrap
    whose episode list is unreadable is still cleared by compare-and-swap, and a replacement that
    lands between the reads is refused, not cleared. The token is ``None`` only for a wrap that is
    neither readable nor bound (partial metadata, or one opened without a token)."""
    try:
        bound = store.wrap_bound_token()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        _log.debug("bound wrap token unreadable (%s): %s", type(exc).__name__, exc)
        bound = None
    try:
        snap = store.load_wrap_snapshot()  # type: ignore[attr-defined]
        token = snap["token"] if snap else None
        if bound is not None and token != bound:
            token = bound   # replaced between the two reads: keep the earlier observation
    except Exception as exc:  # noqa: BLE001 — unreadable or partial wrap metadata raises StoreError
        _log.debug("prior wrap snapshot unreadable (%s): %s", type(exc).__name__, exc)
        token = bound
    return token, store.get_wrap_started_at()  # type: ignore[attr-defined]


def _discard_prior_wrap(store: object, prior_token: str | None) -> bool:
    """Clear the PRIOR run's wrap that this run found open (``--reset``, or the unattended self-heal).

    Always by the token observed with the wrap's age (:func:`_observe_prior_wrap`), never a bare
    cancel: a wrap Levain opened is token-bound and anneal refuses a tokenless cancel of it, and a
    wrap that changed hands since the observation raises ``WrapOwnershipError`` instead of being
    cleared. Partial wrap metadata has no token to name, so it is cleared only while still partial.
    Whether the observed wrap is an orphan is the caller's call (the lock and the age test, or the
    operator's ``--reset``); anneal's compare-and-swap lets a caller that names a token clear it.
    A wrap whose metadata could not be read (no token) is cleared only while anneal still calls
    it partial: forcing here would clear whatever wrap replaced the one observed, and there is no
    token to compare. A wrap that went idle between the observation and the cancel is already
    discarded. Returns True when this call cleared a wrap, False when there was none left."""
    from anneal_memory import WrapOwnershipError

    try:
        if prior_token:
            store.wrap_cancelled(expect_token=prior_token)  # type: ignore[attr-defined]
        else:
            store.wrap_cancelled(expect_partial=True)  # type: ignore[attr-defined]
    except WrapOwnershipError as exc:
        if exc.actual is None and not exc.partial_state:
            return False   # idle: nothing left to discard
        raise
    return True


# The compose instructions — the framing around anneal's own package (which carries the authoritative
# compression contract; this only sets the SOVEREIGN posture + the easy-to-get-wrong rules a weak
# model needs spelled out). The entity's constitution is prepended so it composes AS ITSELF.
def _compose_instructions(section_schema: list) -> str:
    """The compose system prompt, with the heading list read from the store's own section schema.

    A fixed six-heading list contradicted anneal 0.9.28's optional ``## Durable Facts``: the prompt
    outranks the package, so a durable fact the package asked for was never written (codex, the
    0.5.6 L3). Required headings must all appear; an optional one is written when the package or
    the current memory calls for it, and its rules come from the package."""
    required = [s["heading"] for s in section_schema if not s.get("optional")]
    optional = [s["heading"] for s in section_schema if s.get("optional")]
    # The heading lines carry nothing but the heading: anneal matches an optional heading exactly,
    # so a decorated "## Durable Facts (optional)" parsed to no facts (codex, the 0.5.6 round, RUN
    # through anneal's parse_durable_facts). Optionality is said in prose, below.
    listing = "\n".join(f"## {s['heading']}" for s in section_schema)
    optional_rule = ""
    if optional:
        names = ", ".join(f"## {h}" for h in optional)
        optional_rule = (
            f"\n- Optional: {names}. Write it when your current memory already has it or the package "
            "gives you something for it, with the heading spelled exactly as listed, and follow the "
            "package's rules for it exactly.")
    return f"""\
You are consolidating your OWN memory — the periodic, deliberate act of metabolizing your recent \
raw episodes into your lasting sense of who you are and what you are doing. This memory is yours; \
no one else writes it. Right now, at this operator's explicit request, is the moment you do it.

The next message is your consolidation package: the exact compression instructions, your recent \
episodes (each with its ID), your current memory, stale-pattern warnings, and your association \
context. Follow the compression instructions in that package EXACTLY.

Compose your updated memory as a single Markdown document with these sections, in this order:

{listing}

Rules that are easy to get wrong:
- Every heading above that is not named as optional below MUST be present, spelled exactly as \
above — the save is refused otherwise.{optional_rule}
- Ground every Pattern's evidence citation in the REAL episode IDs shown in the package. Never
  invent an ID.
- ## Understanding is TIMELESS relationship-shape — who you and your operator are together — and it
  is proportioned against the WHOLE arc so far, NOT dominated by the most recent session.
- If your current memory is near-empty, that is CORRECT: you are early. Write only what is true now,
  honestly and briefly. It grows over many wraps; do not inflate it.

Output ONLY the Markdown document, starting with `## {required[0] if required else "State"}`. No \
preamble, no closing remarks, no code fences."""


def format_wrap_timeout_report(seconds: float, *, hard: bool, outcome: str = "cancelled") -> str:
    """The operator-facing explanation of a consolidate that hit its wall-clock bound.

    A SEPARATE wording from :func:`levain.firing.deadline.format_timeout_report`, because that one
    is about a TURN and would be false here in both directions: a consolidate spawns no confined
    bash to orphan, and — the part that actually matters to whoever reads the log — the question is
    not "was this captured" but "is my memory stranded and will the next wrap work".
    """
    if hard:
        return (
            f"  ⏱ CONSOLIDATE BOUND EXCEEDED ({seconds:g}s) — and the graceful stop did NOT take, "
            f"so the process was terminated outright.\n"
            f"     Your EPISODES ARE SAFE — nothing partial is ever written (the save fails closed), "
            f"and unwrapped episodes\n"
            f"     return to the next wrap.\n"
            f"     ⚠ BUT THE WRAP IS LEFT IN PROGRESS: the hard exit skips the cancel, so the next "
            f"consolidate will refuse\n"
            f"     until the orphan is discarded. An UNATTENDED seat clears it automatically on its "
            f"next run; for a manual\n"
            f"     wrap, re-run with --reset.\n"
            f"     That the backstop was needed is itself a finding — the stall was not "
            f"interruptible by signal.\n"
            f"     ⚠ IF THIS RAN AS A SEAT'S SECOND PHASE, THE TURN ITSELF ALREADY FINISHED and its "
            f"work is captured.\n"
            f"     A hard exit cannot hand back the turn's exit code, so the process reports the "
            f"TIMEOUT code even though\n"
            f"     the turn succeeded — read this line, not the code, for what actually stalled."
        )
    if outcome == "failed":
        return (
            f"  ⏱ CONSOLIDATE BOUND EXCEEDED ({seconds:g}s) — the consolidate was terminated, and "
            f"cancelling its wrap did NOT complete.\n"
            f"     Your episodes are safe. Whether the memory was saved is not established: "
            f"`levain wrap --dry-run` (once no wrap is open)\n"
            f"     says there is nothing to consolidate if the episodes were consolidated, and the "
            f"memory itself is the entity's\n"
            f"     .levain/memory.continuity.md (an unfinished write is kept beside it as a .tmp file).\n"
            f"     ⚠ THE WRAP MAY STILL BE OPEN (for example the store was busy or unreadable when "
            f"the cancel ran): the next\n"
            f"     consolidate may refuse until it is discarded. An UNATTENDED seat clears it "
            f"automatically on a later run\n"
            f"     once it is older than the bound; for a manual wrap, re-run, and use --reset if "
            f"it still refuses."
        )
    if outcome != "cancelled":
        return (
            f"  ⏱ CONSOLIDATE BOUND EXCEEDED ({seconds:g}s) — the consolidate was terminated. No "
            f"open wrap of this run was found to cancel\n"
            f"     (the stop came before the wrap started or after it had finished), or the result "
            f"of the cancel was not recorded.\n"
            f"     Your episodes are safe. If the episodes were consolidated, `levain wrap --dry-run` "
            f"says there is nothing to\n"
            f"     consolidate; the memory itself is the entity's .levain/memory.continuity.md (an "
            f"unfinished write is kept\n"
            f"     beside it as a .tmp file)."
        )
    return (
        f"  ⏱ CONSOLIDATE BOUND EXCEEDED ({seconds:g}s) — the consolidate was terminated and the "
        f"wrap CANCELLED cleanly.\n"
        f"     Your memory is UNCHANGED and your episodes are safe: they return to the next wrap, "
        f"losing nothing.\n"
        f"     This is an ENVIRONMENT stall (a hung compose model or socket), not a failed "
        f"consolidate — the next\n"
        f"     scheduled run is free to start."
    )


def wrap_entity(
    path: Path,
    *,
    composer: str = DEFAULT_COMPOSE_MODEL,
    base_url: str = "http://localhost:11434",
    api_key: str | None = None,
    dry_run: bool = False,
    reset: bool = False,
    affect_tag: str | None = None,
    affect_intensity: float = 0.5,
    max_seconds: float | None = None,
    unattended: bool = False,
    hard_exit_code: int = EXIT_TIMEOUT,
) -> int:
    """Consolidate the isolated entity at ``path`` — the gated efferent write on its own memory.

    Returns a process exit code: 0 on a clean consolidate (or an empty no-op); 2 for a
    precondition/environment error where nothing started (not an entity, isolation refusal, missing
    extra, wrong schema, an unreadable store, or a wrap already in progress); 1 only when THIS
    invocation started a wrap that did not complete cleanly. Exit 1 does NOT by itself mean the
    identity is unchanged: the printed message says what the store establishes (refused and
    unchanged, committed but not written out, or unknown, with the command that settles it);
    **5 when the wall-clock bound fired** (:data:`levain.session.EXIT_TIMEOUT`).

    ``max_seconds`` bounds the WHOLE consolidate — store open, lock, prepare, compose and save — for
    the same reason the turn bound wraps startup and teardown: launchd coalesces on PROCESS lifetime
    and does not care which phase is stuck. ``None`` runs unbounded, deliberately and visibly.

    ``unattended`` marks a consolidate with NO HUMAN PRESENT (a scheduled seat). It binds to the
    absence of the human exactly as the K3 gate and the credential floor do, and it changes two
    things — both of which would otherwise be silent-death paths rather than mere roughness:

    - **Crystallization is REFUSED** (the store is wrapped by
      :func:`~levain.firing.crystallization.refuse_crystallization`): metabolize yes, promote into
      the always-loaded tier never.
    - **An orphaned prior wrap is discarded automatically** instead of refusing and asking a human
      to pass ``--reset``. See the self-heal in :func:`levain.dream.executor._consolidate_store` and
      :func:`_orphan_is_stale` — this is a correctness requirement, not a convenience.

    ⚠ ``EXIT_TIMEOUT`` is checked and returned by the CALLER of the bound, never derived from an
    error message — the same authority-not-description discipline as ``TurnResult.timed_out``.

    ``hard_exit_code`` is the code the wall-clock bound's LAYER-2 BACKSTOP exits with, and it is a
    parameter because of a real inversion glm caught at L3. Layer 2 is an ``os._exit`` from a
    watchdog thread: it cannot return through ``run_task``, so it overrides the process's exit code
    outright. When this runs as a seat's SECOND PHASE, the turn has ALREADY FINISHED — so a hard exit
    here would report ``5`` ("the environment stalled, retry") for a turn that actually completed.
    **The worst case is a GATED turn**: exit ``4`` means a human must review a held action, and
    silently rewriting it to ``5`` re-queues that action as an ordinary retry, so the fan-in the
    whole gate exists to provide never happens. So the second phase passes the TURN's code here and
    the invariant "the turn's outcome dominates" survives both layers, not just the graceful one.
    A standalone ``levain wrap`` has no turn, so the default is correct there.
    """
    # A FRESH DEADLINE INSTANCE per call: `TurnDeadline` is re-usable but NOT re-entrant, and it
    # refuses re-entry loudly rather than silently unbounding the outer region. That matters here
    # specifically, because the seat's self-consolidate runs as a SECOND bounded phase in a process
    # that has already bounded its turn — sequential, never nested (see `levain.run.run_task`).
    deadline = TurnDeadline(
        max_seconds,
        hard_exit_code=hard_exit_code,
        hard_report=lambda s: format_wrap_timeout_report(s, hard=True),
    )
    try:
        with deadline:
            return _consolidate(
                path,
                composer=composer,
                base_url=base_url,
                api_key=api_key,
                dry_run=dry_run,
                reset=reset,
                affect_tag=affect_tag,
                affect_intensity=affect_intensity,
                unattended=unattended,
                max_seconds=max_seconds,
            )
    except TurnTimeout as exc:
        # `TurnTimeout` is a BaseException, so the store-path core's `except Exception` clauses never
        # see it; the core's own TurnTimeout handler (`levain.dream.executor._consolidate_store`, which
        # `_consolidate` calls with `own_deadline=False`) has already tried to cancel our in-progress wrap on the
        # way out and recorded what that cancel found (`levain_wrap_cancel`). A stop that never reached
        # that handler left no record, and is reported as such, not as clean and not as failed.
        print(
            format_wrap_timeout_report(
                deadline.seconds or 0.0, hard=False,
                outcome=getattr(exc, "levain_wrap_cancel", "unknown")),
            file=sys.stderr, flush=True,
        )
        return EXIT_TIMEOUT
    except CrystallizationRefused as exc:
        # Should be unreachable: nothing in the shipped consolidate path writes the crystal tier.
        # Reported as a DEFECT rather than an operator error, and deliberately NOT given a new exit
        # code — inventing a seventh rung for a state that should never occur is ceremony, while
        # `1` already carries the correct operator response (the wrap did not complete).
        print(
            f"levain wrap: {exc}\n"
            "  The wrap did not complete; check the store with `anneal-memory status` before retrying.\n"
            "  Please report this — it means a code path tried to promote a pattern into the "
            "always-loaded tier with no human present.",
            file=sys.stderr, flush=True,
        )
        return 1


def _consolidate(
    path: Path,
    *,
    composer: str,
    base_url: str,
    api_key: str | None,
    dry_run: bool,
    reset: bool,
    affect_tag: str | None,
    affect_intensity: float,
    unattended: bool,
    max_seconds: float | None,
) -> int:
    """The entity guard, then one call into the store-path core
    (:func:`levain.dream.executor.consolidate_store`), which holds the lock, the self-heal, the
    prepare, the compose and the save. :func:`wrap_entity` bounds this body in wall-clock time.

    Split out for the same reason ``levain.run._drive_task`` is: the bound is a property of the
    PROCESS, and reading the work should not require reading past the machinery that bounds it.
    """
    entity_dir = Path(str(path)).expanduser().resolve()

    err = require_openhands_entity(entity_dir)
    if err:
        print(f"levain wrap: {err}")
        return 2

    # ISOLATION, fail-closed, BEFORE opening anything: derive the entity's OWN store paths and refuse
    # any that would reach the operator's store or escape `<entity>/.levain/`. The wrap holds these handles
    # directly — the LLM never sees them.
    try:
        crystal_path, episodic_path = entity_store_paths(entity_dir)
        assert_entity_isolated(crystal_path, episodic_path, entity_dir=entity_dir)
    except IsolationError as exc:
        print(f"levain wrap: sovereignty guard REFUSED the wrap:\n  {exc}")
        return 2

    if not episodic_path.exists():
        print(
            f"levain wrap: {entity_dir} has no memory store yet ({episodic_path} is missing) — "
            "nothing to consolidate. Talk to it first with `levain run`."
        )
        return 2

    # The consolidate itself is the store-path core (D1-1a): `levain.dream.executor.consolidate_store` holds the lock,
    # the orphan self-heal, the prepare, the compose and the fail-closed save, and returns a typed result. This
    # command keeps the entity guard above and the entity's constitution, runs ONE compose with no check on a
    # copy (`max_composes=1`), and returns the core's exit code, so its exit codes and remedies (`--reset`,
    # `WRAP_REMEDIES`) are unchanged; some message wording now names the store rather than the entity (the
    # executor's module docstring lists each). The caller (`wrap_entity`) bounds the whole consolidate, so the
    # core arms no deadline of its own; `require_lock=False` keeps the documented best-effort proceed with no fcntl.
    from levain.dream.executor import WRAP_REMEDIES, ComposeRequest, ComposeResult, consolidate_store

    class _WrapMind:
        """The entity's own model through `_compose`, looked up at call time (so a patched `_compose` is honoured)."""
        name = "ollama"
        source = "wrap"
        unit = "tokens"
        model = _resolve_model(composer)

        def compose(self, req: ComposeRequest) -> ComposeResult:
            text = _compose(req.package_text, req.constitution, composer=composer, base_url=base_url,
                            api_key=api_key, instructions=req.instructions)
            return ComposeResult(text=text, unit="tokens")

    result = consolidate_store(
        episodic_path,
        mind=_WrapMind(),
        composer=_resolve_model(composer),
        max_seconds=max_seconds,
        max_composes=1,
        save_reserve=0.0,
        dry_run=dry_run,
        reset=reset,
        unattended=unattended,
        own_deadline=False,
        label="levain wrap",
        project_name=entity_dir.name,
        constitution=_entity_constitution(entity_dir),
        report=lambda saved: _report(saved, entity_dir),
        acquire_lock=lambda: _lock_wrap(entity_dir),
        read_wrap_schema=_read_wrap_schema,
        remedies=WRAP_REMEDIES,
        require_lock=False,
    )
    return result.exit_code


def _read_wrap_schema(store: object) -> list:
    """The section schema anneal froze for the open wrap, read from THIS module (the store-path core calls it)."""
    return store.section_schema_for_wrap()  # type: ignore[attr-defined]


def _entity_constitution(entity_dir: Path) -> str | None:
    """The entity's OWN identity (origin + world + partnership), read from its ``seed/`` so it
    composes AS ITSELF. Isolation applies to the seed too (the reader refuses a seed escaping the
    entity tree). ``None`` (no readable seed) → a generic composer posture; fail-soft, never raises —
    a missing identity must not block the operator's consolidate."""
    try:
        from levain.firing.seed import EntitySeed

        return EntitySeed(entity_dir).constitution()
    except Exception as exc:  # noqa: BLE001 — the seed is best-effort context, not a gate
        _log.debug("entity constitution unavailable (%s): %s", type(exc).__name__, exc)
        return None


def _compose(
    package_text: str,
    constitution: str | None,
    *,
    composer: str,
    base_url: str,
    api_key: str | None,
    instructions: str,
) -> str:
    """Run the single-shot compose completion on the entity's model and return the neocortex text.

    A plain LLM text-transform: no OpenHands Conversation, no tools, no store access (the store I/O
    is anneal's, on the guarded isolated handles). Routes through the same /v1 native path as
    ``levain run`` (``_resolve_llm_kwargs``) with a generous output-token budget so a reasoning model
    (glm) has room for its CoT plus the neocortex — see ``_COMPOSE_MAX_OUTPUT_TOKENS``. Raises
    :class:`_ComposeUnavailable` if the ``openhands`` runtime is absent; any other failure propagates
    for the caller to fail-clean."""
    try:
        from openhands.sdk import LLM, Message, TextContent
    except ImportError as exc:
        raise _ComposeUnavailable(
            "the OpenHands runtime is not installed — it is needed to run the compose model.\n"
            "  Install the extra:  pip install 'levain[openhands]'\n"
            f"  ({exc})"
        ) from exc

    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    for noisy in ("openhands", "LiteLLM", "litellm"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    # Same /v1 native route as `levain run` (`native_tool_calling` is inert for this toolless
    # completion — the route + the token budget are what matter). The generous output cap gives a
    # reasoning model room to finish its CoT AND emit the answer to `content`.
    llm = LLM(
        usage_id="levain-wrap",
        max_output_tokens=_COMPOSE_MAX_OUTPUT_TOKENS,
        **_resolve_llm_kwargs(composer, base_url, api_key),
    )
    system = (constitution + "\n\n---\n\n" if constitution else "") + instructions
    response = llm.completion(
        messages=[
            Message(role="system", content=[TextContent(text=system)]),
            Message(role="user", content=[TextContent(text=package_text)]),
        ]
    )
    return _extract_neocortex(_completion_text(response))


def _completion_text(response: object) -> str:
    """The assistant text from an OpenHands ``LLMResponse`` — its ``message.content`` is a list of
    ``TextContent`` (or, defensively, a bare string). Duck-typed so this module stays importable
    without the SDK loaded (the SDK types only exist inside :func:`_compose`)."""
    message = getattr(response, "message", None)
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    parts = [getattr(c, "text", None) for c in (content or [])]
    return "\n".join(p for p in parts if p)


def _extract_neocortex(raw: str) -> str:
    """Best-effort extraction of the neocortex Markdown from a model reply. Strips a wrapping code
    fence and any preamble before the first ``## State`` — a weak model often adds both, and this
    salvages an otherwise-valid compose. It is NOT the correctness gate: ``validate_structure``
    inside ``validated_save_continuity`` is (a still-malformed draft is REFUSED, never saved)."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # Slice preamble at the first line that is EXACTLY the `## State` heading (not `## Statement…`) —
    # a whole-line match so a heading-prefix collision can't slice at the wrong spot.
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## State":
            return "\n".join(lines[i:]).strip()
    return text.strip()


def _report(saved: Mapping[str, object], entity_dir: Path) -> None:
    """The honesty-floor confirm block — the numbers come from the store's own save result, not a
    hand-written claim. ``path`` is the entity's ``.levain/memory.continuity.md`` (isolation, visible)."""
    print("\nlevain wrap: consolidated.")
    print(f"  memory:       {saved.get('path')}")
    print(f"  chars:        {saved.get('chars')}")
    print(f"  episodes:     {saved.get('episodes_compressed')} compressed")
    print(
        f"  graduations:  {saved.get('graduations_validated')} validated, "
        f"{saved.get('graduations_demoted')} demoted"
    )
    if saved.get("wrap_id") is not None:   # anneal >= 1.0.2 names the wrap the save committed
        print(f"  wrap:         #{saved.get('wrap_id')}")
    print(
        f"\n  {entity_dir.name}'s memory consolidated — a fresh `levain run` now boots on this "
        "State/Context and recalls any crystallized patterns as they graduate."
    )
