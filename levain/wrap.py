"""levain.wrap — `levain wrap <entity>`: the HUMAN-GATED consolidate for a sovereign entity.

An OpenHands Levain entity CAPTURES every turn to its own episodic store (``levain run`` +
:class:`~levain.firing.anneal.AnnealEntityFiring`), but the firing is CONSTITUTIONALLY FORBIDDEN
from consolidating on its own ("captures but never consolidates" — the afferent/efferent membrane).
So raw episodes pile up and never metabolize into felt memory: the entity never compounds identity
across sessions. ``levain wrap`` is the explicit operator command that RUNS the consolidate — the
command invocation IS the human gate the autonomous firing may never trip itself.

The consolidate is anneal's three-beat move, and we COMPOSE with anneal's API (never reinvent it):

  1. ``prepare_wrap(store, crystal_store=…)`` — mint a ``wrap_token``, freeze the episode window,
     and emit the compression package (episodes + current memory + stale-pattern warnings + the
     compression INSTRUCTIONS themselves + association context).
  2. **compose** — the ONE cognitive beat: a mind reads the package and writes the 6-section
     neocortex. For a SOVEREIGN entity that mind is its OWN model (the same open Ollama model
     ``levain run`` uses), so the entity metabolizes its own episodes with its own mind — an OPEN
     model on its OWN memory, not a frontier model on foreign memory. (Per the master-plan "own the
     substrate, rent the channel": the memory and the open-model *choice* are owned; the compute
     endpoint — local Ollama or Ollama Cloud, so the episode content does leave the box unless the
     endpoint is local — is the rentable channel.) ``--composer`` overrides the compose model to a
     stronger one per-wrap when the operator wants a higher-quality wrap, WITHOUT giving up the
     sovereign default.
  3. ``validated_save_continuity(store, text, wrap_token=…, crystal_store=…, allow_shrink=False)`` —
     the gated efferent write: structure-validate → graduation → save → Hebbian → decay. FAIL-CLOSED:
     a malformed compose (a weak model dropping a required section or mis-citing episodes) RAISES and
     leaves the wrap in progress — a broken identity is never written. We then cancel it so a plain
     re-run works; the frozen episodes stay visible to the next wrap (``wrap_cancelled`` records no
     completed-wrap watermark), losing nothing.

**Isolation (the #1 requirement).** The consolidate reads/writes ONLY the entity's own stores under
``<entity>/.levain/`` and NEVER flow's ``~/.anneal-memory/``. The LLM never touches the store —
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
from collections.abc import Mapping
from pathlib import Path

from levain.firing.isolation import (
    ENTITY_STORE_SUBDIR,
    IsolationError,
    assert_entity_isolated,
    entity_store_paths,
)
from levain.run import _resolve_model, require_openhands_entity

__all__ = ["wrap_entity"]

_log = logging.getLogger("levain.wrap")

# Sentinel: the per-entity single-writer lock is held by ANOTHER live consolidate.
_ANOTHER_WRAP_RUNNING = object()

# The default compose model — the sovereign default, the SAME open model `levain run` boots the
# entity on. `--composer <model>` overrides it (e.g. to a stronger model for a higher-quality wrap)
# without surrendering the sovereign default.
DEFAULT_COMPOSE_MODEL = "minimax-m3:cloud"


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
    try:
        import fcntl
    except ImportError:
        return None
    fd = os.open(
        str(entity_dir / ENTITY_STORE_SUBDIR / "wrap.lock"), os.O_RDWR | os.O_CREAT, 0o644
    )
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


def _wrap_in_progress(store: object) -> bool:
    """Is a wrap still in progress on ``store``? The state boundary that distinguishes a save that
    committed NOTHING (validation/pre-commit failure — wrap still in progress) from one that COMMITTED
    the DB but failed to externalize the file (``wrap_completed`` cleared the in-progress metadata) —
    codex's catch that exception TYPE alone is not the boundary. Fail-safe to ``True`` on a read error
    (the safer default: "re-run", never a false "already committed, don't re-run")."""
    try:
        return bool(store.get_wrap_started_at())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — unreadable state → assume in-progress (safe default)
        return True


def _cancel_if_ours(store: object, wrap_token: object) -> None:
    """Cancel the in-progress wrap ONLY if it is still OURS (its snapshot token equals ``wrap_token``).

    The point-of-use guard against cancelling a concurrent peer's live wrap in our failure path — the
    ``loser-cancels-winner`` race. Correct even if the process lock were somehow bypassed
    (``invariant_must_fire_at_the_point_of_use``): a bare ``wrap_cancelled`` clears WHATEVER wrap is in
    the store, which after a race is the winner's. No-op if idle or the token changed; never raises."""
    try:
        snapshot = store.load_wrap_snapshot()  # type: ignore[attr-defined]
        if snapshot is not None and snapshot.get("token") == wrap_token:
            store.wrap_cancelled()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — a guard must not raise into a failure path
        _log.debug("cancel-if-ours skipped (%s): %s", type(exc).__name__, exc)


# The compose instructions — the framing around anneal's own package (which carries the authoritative
# compression contract; this only sets the SOVEREIGN posture + the easy-to-get-wrong rules a weak
# model needs spelled out). The entity's constitution is prepended so it composes AS ITSELF.
_COMPOSE_INSTRUCTIONS = """\
You are consolidating your OWN memory — the periodic, deliberate act of metabolizing your recent \
raw episodes into your lasting sense of who you are and what you are doing. This memory is yours; \
no one else writes it. Right now, at this operator's explicit request, is the moment you do it.

The next message is your consolidation package: the exact compression instructions, your recent \
episodes (each with its ID), your current memory, stale-pattern warnings, and your association \
context. Follow the compression instructions in that package EXACTLY.

Compose your updated memory as a single Markdown document with ALL SIX sections, in this order:

## State
## Active Threads
## Patterns
## Decisions
## Context
## Understanding

Rules that are easy to get wrong:
- All six headings MUST be present, spelled exactly as above — the save is refused otherwise.
- Ground every Pattern's evidence citation in the REAL episode IDs shown in the package. Never
  invent an ID.
- ## Understanding is TIMELESS relationship-shape — who you and your operator are together — and it
  is proportioned against the WHOLE arc so far, NOT dominated by the most recent session.
- If your current memory is near-empty, that is CORRECT: you are early. Write only what is true now,
  honestly and briefly. It grows over many wraps; do not inflate it.

Output ONLY the Markdown document, starting with `## State`. No preamble, no closing remarks, no \
code fences."""


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
) -> int:
    """Consolidate the isolated entity at ``path`` — the human-gated efferent write.

    Returns a process exit code: 0 on a clean consolidate (or an empty no-op); 2 for a
    precondition/environment error where nothing started (not an entity, isolation refusal, missing
    extra, wrong schema, an unreadable store, or a wrap already in progress); 1 only when THIS
    invocation started a wrap that did not complete (the compose or the save failed) — in which case
    the entity's identity is left UNCHANGED and its episodes are safe.
    """
    entity_dir = Path(str(path)).expanduser().resolve()

    err = require_openhands_entity(entity_dir)
    if err:
        print(f"levain wrap: {err}")
        return 2

    # ISOLATION, fail-closed, BEFORE opening anything: derive the entity's OWN store paths and refuse
    # any that would reach flow's store or escape `<entity>/.levain/`. The wrap holds these handles
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

    try:
        from anneal_memory import (
            FLOW_SCHEMA,
            AnnealMemoryError,
            CrystalStore,
            Store,
            WrapInProgressError,
        )
        from anneal_memory.continuity import (
            format_wrap_package_text,
            prepare_wrap,
            validated_save_continuity,
        )
        from anneal_memory.types import AffectiveState
    except ImportError as exc:  # anneal-memory is a BASE dep, so this is a broken install
        print(f"levain wrap: anneal-memory is not importable ({exc}). Reinstall levain.")
        return 2

    # section_schema=None preserves the store's PERSISTED schema (partnership/FLOW-6, set at init);
    # audit defaults on — the consolidate is the gated single-writer path the audit chain is FOR
    # (unlike the high-frequency afferent capture, which opens audit=False). project_name is the
    # entity's own handle so the compose package header reads as ITS memory, not the generic "Agent".
    # A corrupt / locked / newer-schema store is a clean exit-2 here, not a raw traceback (the
    # honesty floor the rest of the command holds).
    try:
        store = Store(
            str(episodic_path), project_name=entity_dir.name, section_schema=None
        )
    except AnnealMemoryError as exc:
        print(
            f"levain wrap: could not open {entity_dir.name}'s memory store "
            f"({type(exc).__name__}: {exc})."
        )
        return 2

    # SINGLE-WRITER: hold the per-entity wrap lock across the whole prepare→compose→save lifecycle so
    # a concurrent `levain wrap` on this entity can't race the wrap state (apparatus L3).
    lock = _lock_wrap(entity_dir)
    if lock is _ANOTHER_WRAP_RUNNING:
        store.close()
        print(
            "levain wrap: another consolidate is already running on this entity "
            f"({entity_dir / ENTITY_STORE_SUBDIR / 'wrap.lock'} is held). "
            "Wait for it to finish, then re-run."
        )
        return 2

    try:
        # A partnership entity's store MUST be on the 6-section schema — an ops-schema store cannot
        # hold the compose, and a clear message here beats a cryptic save-time rejection.
        headings = [s["heading"] for s in store.section_schema]
        expected = [s["heading"] for s in FLOW_SCHEMA]
        if headings != expected:
            print(
                "levain wrap: this entity's store is not on the 6-section partnership schema, "
                "so it cannot consolidate.\n"
                f"  got:      {headings}\n  expected: {expected}\n"
                f"  Fix:  anneal-memory --db {episodic_path} set-schema partnership"
            )
            return 2

        # A wrap already in progress means a PRIOR consolidate crashed mid-flight (this command does
        # prepare→compose→save in one process, so it can't be a concurrent one). Refuse rather than
        # stack a second wrap; --reset discards the orphan and starts fresh (episodes are safe).
        started = store.get_wrap_started_at()
        if started:
            if not reset:
                print(
                    f"levain wrap: a prior wrap is still in progress (started {started}).\n"
                    "  A previous consolidate did not finish. Re-run with --reset to discard it and "
                    "start fresh — your captured episodes are safe (they return to the next wrap)."
                )
                return 2
            store.wrap_cancelled()
            print(f"levain wrap: discarded an unfinished prior wrap (started {started}).")

        crystal = CrystalStore(crystal_path)
        # NO session_id — that engages flow's parallel-convo consolidate-efferent gate (spore-194),
        # which is meaningless for a single sovereign entity: one entity, one wrap, no baton.
        result = prepare_wrap(store, crystal_store=crystal)
        status = result.get("status")
        if status == "empty":
            print(
                "levain wrap: nothing to consolidate — no new episodes since the last wrap.\n"
                "  Talk to the entity with `levain run` to accumulate experience, then wrap."
            )
            return 0
        if status != "ready":
            print(f"levain wrap: prepare_wrap returned an unexpected status {status!r} — aborting.")
            store.wrap_cancelled()
            return 1

        package_text = format_wrap_package_text(result)
        wrap_token = result.get("wrap_token")
        episode_count = result.get("episode_count")

        if dry_run:
            # A prepared-but-unsaved wrap would strand the store, so cancel it — --dry-run changes
            # NOTHING (a plan is not a result). The episodes stay unwrapped for the real run.
            store.wrap_cancelled()
            print(
                f"levain wrap: DRY RUN — {episode_count} episode(s) are ready to consolidate "
                f"(wrap_token {wrap_token}). Nothing was composed or saved.\n"
            )
            print(package_text)
            return 0

        model = _resolve_model(composer)
        print(
            f"levain wrap: consolidating {episode_count} episode(s) into "
            f"{entity_dir.name}'s memory with {model} …"
        )

        # COMPOSE — the entity's own mind (or the --composer override) metabolizes its episodes.
        try:
            neocortex = _compose(
                package_text,
                _entity_constitution(entity_dir),
                model=model,
                base_url=base_url,
                api_key=api_key,
            )
        except _ComposeUnavailable as exc:
            store.wrap_cancelled()  # never composed → don't strand the wrap
            print(f"levain wrap: {exc}")
            return 2
        except Exception as exc:  # noqa: BLE001 — a model/endpoint failure is a run failure, not a crash
            store.wrap_cancelled()  # self-clean so a plain re-run works; episodes return to the pool
            print(
                f"levain wrap: the compose model failed ({type(exc).__name__}: {exc}).\n"
                "  Nothing was saved; the entity's memory is unchanged and its episodes are safe. "
                "Check --composer / --base-url and re-run."
            )
            return 1

        if not neocortex.strip():
            store.wrap_cancelled()
            print(
                "levain wrap: the compose model returned no usable memory text; nothing saved. "
                "Re-run (a stronger --composer may help a weak model)."
            )
            return 1

        # SAVE — the gated efferent write. FAIL-CLOSED: validated_save_continuity REFUSES (raises) a
        # malformed neocortex BEFORE writing, so a broken identity is never persisted.
        affective = (
            AffectiveState(tag=affect_tag, intensity=affect_intensity)
            if affect_tag and affect_tag.strip()
            else None
        )
        try:
            saved = validated_save_continuity(
                store,
                neocortex,
                wrap_token=wrap_token,
                crystal_store=crystal,
                affective_state=affective,
                allow_shrink=False,
            )
        except Exception as exc:  # noqa: BLE001 — anneal raises ValueError (validation) / StoreError
            # Distinguish by STORE STATE, not exception type (codex L3). anneal validates + shrink-
            # gates BEFORE any write (a malformed compose raises here with NOTHING committed and the
            # wrap still in progress), but a VALID compose COMMITS the DB — wrap_completed stamps the
            # episodes and clears the in-progress metadata — in Phase 2, and only THEN renames the
            # continuity sidecar in Phase 3. A Phase-3 failure raises POST-commit: the wrap is already
            # saved and the episodes are no longer re-wrappable. Exception TYPE alone can't tell these
            # apart (both can be ValueError/StoreError); the in-progress flag can.
            if _wrap_in_progress(store):
                # Nothing committed — the identity is unchanged. Cancel OUR wrap (token-owned, so a
                # concurrent peer's live wrap is never collateral-cancelled) and let the operator re-run.
                debug_path = _dump_rejected(entity_dir, wrap_token, neocortex)
                _cancel_if_ours(store, wrap_token)
                print(
                    f"levain wrap: the composed memory was REFUSED — NOT saved "
                    f"({type(exc).__name__}: {exc}).\n"
                    "  The entity's identity is unchanged. The compose model most likely dropped a "
                    "required section, mis-cited an episode, or (on a later wrap) produced a too-small "
                    "memory that the catastrophic-shrink gate refused.\n"
                    f"  The rejected draft was saved for inspection: {debug_path}\n"
                    "  Re-run to try again (a stronger --composer may help)."
                )
                return 1
            # The DB COMMITTED but the file externalization (Phase-3 rename) failed — the wrap IS
            # recorded (recoverable from the ``.tmp`` the exception names) and the episodes are NOT
            # re-wrappable. Do NOT cancel (nothing is in progress) and do NOT say "re-run".
            print(
                f"levain wrap: the memory COMMITTED but writing it to disk did not finish "
                f"({type(exc).__name__}: {exc}).\n"
                "  The consolidate IS recorded — follow the recovery hint in the message above to move "
                "the preserved .tmp into place. Do NOT re-run; these episodes are already consolidated."
            )
            return 1

        _report(saved, entity_dir)
        return 0
    except WrapInProgressError as exc:
        # A concurrent `levain wrap` on the SAME entity won the prepare race after our
        # get_wrap_started_at pre-check (anneal's structural AM-PREPARE-GUARD refused the second
        # wrap). Integrity is safe — the other wrap is untouched. Do NOT blindly --reset (that would
        # cancel a peer's live wrap); guide the operator to check first.
        print(
            f"levain wrap: a wrap is already in progress ({exc}).\n"
            "  Another consolidate may be running, or a prior one crashed. If nothing else is "
            "running, re-run with --reset to discard the stale wrap (episodes are safe)."
        )
        return 2
    except AnnealMemoryError as exc:
        # A read-phase store/crystal failure (schema read, wrap-state read, prepare_wrap). anneal
        # marks the wrap in progress LAST, so nothing was written and no wrap was stranded — a clean
        # precondition error, not a failed-mid-flight wrap.
        print(
            f"levain wrap: the consolidate could not read {entity_dir.name}'s store "
            f"({type(exc).__name__}: {exc})."
        )
        return 2
    finally:
        store.close()
        _unlock_wrap(lock)


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
    model: str,
    base_url: str,
    api_key: str | None,
) -> str:
    """Run the single-shot compose completion on the entity's model and return the neocortex text.

    A plain LLM text-transform: no OpenHands Conversation, no tools, no store access (the store I/O
    is anneal's, on the guarded isolated handles). Raises :class:`_ComposeUnavailable` if the
    ``openhands`` runtime is absent; any other failure propagates for the caller to fail-clean."""
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

    llm = LLM(model=model, base_url=base_url, api_key=api_key, usage_id="levain-wrap")
    system = (constitution + "\n\n---\n\n" if constitution else "") + _COMPOSE_INSTRUCTIONS
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


def _dump_rejected(entity_dir: Path, wrap_token: object, neocortex: str) -> Path:
    """Persist a refused compose next to the store for inspection (why the save was rejected). In the
    ENTITY tree — never a shared temp dir — so it stays with the sovereign entity. Best-effort."""
    path = entity_dir / ".levain" / f"rejected-wrap.{wrap_token}.md"
    try:
        path.write_text(neocortex, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — a debug dump must not mask the real failure
        _log.debug("could not write rejected-wrap draft (%s): %s", type(exc).__name__, exc)
    return path


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
    print(
        f"  associations: {saved.get('associations_formed')} formed, "
        f"{saved.get('associations_strengthened')} strengthened"
    )
    print(
        f"\n  {entity_dir.name}'s memory consolidated — a fresh `levain run` now boots on this "
        "State/Context and recalls any crystallized patterns as they graduate."
    )
