"""levain.firing.anneal — the real-anneal firing kind (``AnnealFiring``; relocated from vagus).

The firing leaf's ONLY heavy dependency lives HERE, never in the firing core. Registered as
kind ``"anneal"``; :func:`levain.firing.build_firing` lazily imports this module the first time
that kind is requested — so importing ``levain.firing`` stays anneal-free (the dependency-
isolated-leaf invariant), and fork/reload re-trigger the same lazy import (reconstruction is
serialization-safe from the kind alone). ``anneal-memory`` is a base ``levain`` dependency.

AFFERENT-ONLY (the constitution, ``projects/vagus/brief.md``): ``inject`` recalls REAL
crystallized patterns from the anneal crystal store into the agent's OWN context, against the
agent's OWN recent context (``req.query``). It never acts outward, never captures, never
consolidates — recall-into-own-context is pure afferent.

Recall uses anneal's Store-FREE on-demand contract ``retrieve_patterns(crystal_store, query)``
(no episodic Store opened per turn → no per-turn open cost, no write-lock contention against a
concurrent single-writer wrap). anneal's ``retrieve_patterns`` deliberately does NOT fail soft
(it surfaces a corrupt/newer-schema store rather than hiding it as empty memory), so the
firing wraps it HERE — "no recall beats a crash": any failure (anneal absent, store missing or
corrupt, OSError) degrades to a no-recall marker, never an exception into the agent's turn.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from levain.firing.contract import CaptureRequest, InjectRequest, register_firing, select_directive
from levain.firing.encoding import RECEIPT_KEY as _ENCODING_RECEIPT_KEY
from levain.firing.isolation import (
    LEVAIN_ENTITY_DIR_ENV,
    assert_entity_isolated,
    entity_store_paths,
    resolve_entity_dir,
)

_log = logging.getLogger("levain.firing.anneal")

# Resolve lazily per inject/capture (env override → default) so a moved store / late-set env
# is picked up without reconstructing the firing — and so the zero-arg registry factory stays
# serialization-safe (it re-resolves on reload rather than freezing a path at registration).
DEFAULT_CRYSTAL_PATH = Path.home() / ".anneal-memory" / "memory.crystal.json"
# Capture appends EPISODES, which live in the anneal episodic store (``memory.db``), NOT the
# crystal store recall reads — crystallization is a CONSOLIDATE concern the vagus never does.
DEFAULT_EPISODIC_PATH = Path.home() / ".anneal-memory" / "memory.db"
MAX_PATTERNS = 3  # precision bias — matches the flow recall hook's per-prompt crystal cap
# Episodes accumulated since the last consolidate before the session-end wrap-nudge fires. A
# coarse "memory is piling up unmetabolized" signal — the human runs the gated consolidate.
DEFAULT_WRAP_NUDGE_THRESHOLD = 12


def _entity_env_path(which: str) -> Path | None:
    """When an entity is bound (``$LEVAIN_ENTITY_DIR`` set), the DEFAULT (``"anneal"``-kind) store
    resolution is entity-isolated and RE-GUARDED **per op** — never a cached bind-time path (closes
    the post-bind ``.levain`` symlink-swap TOCTOU, apparatus codex round-2). So a stray bare
    ``vagus_run`` / ``wrap_nudge`` in an entity process resolves to the ENTITY store, guarded at USE
    time, not flow's. No entity bound → ``None`` (the normal ``$VAGUS_*`` / default resolution).

    Raises :class:`~levain.firing.isolation.IsolationError` if the bound entity's store escapes — its
    callers (``_recall`` / ``capture`` / ``wrap_nudge``) wrap resolution in their fail-soft boundary,
    so it degrades to no-recall / no-nudge, NEVER a leak. ``resolve_entity_dir`` is intentionally NOT
    used here (it RAISES when unbound; this must return ``None`` to fall through to normal resolution)."""
    raw = os.environ.get(LEVAIN_ENTITY_DIR_ENV, "").strip()
    if not raw:
        return None
    entity_dir = Path(raw).expanduser()
    crystal, episodic = entity_store_paths(entity_dir)
    assert_entity_isolated(crystal, episodic, entity_dir=entity_dir)
    return crystal if which == "crystal" else episodic


def _env_crystal_path() -> Path:
    """The default crystal path for the ``"anneal"`` kind. When an entity is bound it resolves under
    the entity's ``.levain/`` (re-guarded per op — :func:`_entity_env_path`); otherwise
    ``$VAGUS_CRYSTAL_PATH`` override, else flow's laptop store. (``AnnealEntityFiring`` never falls
    through here — it resolves via its own ``_entity_paths``.)"""
    entity = _entity_env_path("crystal")
    if entity is not None:
        return entity
    env = os.environ.get("VAGUS_CRYSTAL_PATH", "").strip()
    return Path(env) if env else DEFAULT_CRYSTAL_PATH


def _env_episodic_path() -> Path:
    entity = _entity_env_path("episodic")
    if entity is not None:
        return entity
    env = os.environ.get("VAGUS_EPISODIC_PATH", "").strip()
    return Path(env) if env else DEFAULT_EPISODIC_PATH


def _resolve_wrap_nudge_threshold(explicit: int | None) -> int:
    # A non-positive threshold (explicit <= 0, or env "0") would nudge on an EMPTY store
    # ("0 episodes ... wrap is due") — nonsense, so treat non-positive as the default. (codex L3 LOW.)
    if explicit is not None:
        return explicit if explicit >= 1 else DEFAULT_WRAP_NUDGE_THRESHOLD
    env = os.environ.get("VAGUS_WRAP_NUDGE_THRESHOLD", "").strip()
    if env.isdigit() and int(env) >= 1:
        return int(env)
    return DEFAULT_WRAP_NUDGE_THRESHOLD


@dataclass(frozen=True)
class ConsolidateReadiness:
    """Whether a consolidate is due — and, separately, whether we could even tell.

    **``episodes`` is TRI-STATE, and the third state is why this is a class and not an int.** A
    count means the store was read. ``None`` means it could NOT be read (absent parent dir, corrupt,
    anneal missing), which is emphatically not "zero episodes".

    Collapsing those two is harmless for the NUDGE — a human who misses one reminder gets the next
    one — and exactly wrong for a SEAT, where "unreadable" recurring every interval means the entity
    never metabolizes again, quietly, which is the ``absence_of_signal_rendered_as_health`` class
    K4a ⑥ just spent a whole slice closing. The two consumers owe different responses, so the
    predicate must not pick one for them. Same discipline as ``format_timeout_report``'s tri-state
    ``captured``: only claim what the caller can actually vouch for.
    """

    episodes: int | None
    threshold: int

    @property
    def readable(self) -> bool:
        """Was the store read? ``False`` is a fact to REPORT, never a quiet "nothing to do"."""
        return self.episodes is not None

    @property
    def due(self) -> bool:
        """Enough accumulated to be worth metabolizing? Unreadable is never due."""
        return self.episodes is not None and self.episodes >= self.threshold


def consolidate_readiness(
    episodic_path: Path | None = None, threshold: int | None = None
) -> ConsolidateReadiness:
    """Read the accumulated-episode count and compare it to the wrap threshold. Never raises.

    **THE SINGLE DEFINITION OF "a wrap is due", shared by both consumers** — the human-facing
    :func:`wrap_nudge` and the unattended seat's self-consolidate (:func:`levain.run.run_task`). Two
    surfaces answering one question from two copies of the arithmetic is how they drift, and this
    particular drift would be near-invisible: a seat consolidating on a different definition than
    the nudge reports leaves an operator reading a hint that disagrees with what the machine did.
    One function, one answer — the same move as ``levain.answers.IDENTITY_SLOTS`` shared across the
    three init surfaces, and ``resolve_gate_mode`` shared by the gate and the banners.

    Read-only throughout (``read_only=True``, counting ``episodes_since_wrap()``): no write, no
    audit sidecar, no write-lock contention against a concurrent capture or wrap — so it is safe to
    call from a seat that is about to take the wrap lock.
    """
    thr = _resolve_wrap_nudge_threshold(threshold)
    try:
        from anneal_memory import Store

        path = episodic_path or _env_episodic_path()
        if not Path(path).exists():
            # A store file that does not exist yet is READABLE and genuinely empty — a fresh entity
            # that has never captured a turn. Deliberately distinct from a store that exists and
            # cannot be read: reporting "unreadable" for a brand-new entity would cry wolf on every
            # seat's first interval.
            return ConsolidateReadiness(episodes=0, threshold=thr)
        with Store(path, read_only=True) as store:
            return ConsolidateReadiness(
                episodes=len(store.episodes_since_wrap()), threshold=thr
            )
    except Exception as e:  # noqa: BLE001 — fail-soft: must never raise into a turn or a nudge
        _log.debug("consolidate readiness unavailable (%s): %s", type(e).__name__, e)
        return ConsolidateReadiness(episodes=None, threshold=thr)


def wrap_nudge(episodic_path: Path | None = None, threshold: int | None = None) -> str | None:
    """The SessionEnd→wrap-nudge — afferent SURFACING. Returns a human-facing nudge string iff at
    least ``threshold`` episodes have accumulated since the last consolidate (a wrap is due), else
    ``None``. A thin formatter over :func:`consolidate_readiness`.

    AFFERENT-ONLY, by construction:
      - **Read-only.** :func:`consolidate_readiness` opens the store ``read_only=True`` — no write,
        no audit sidecar, no write-lock contention against a concurrent capture or wrap.
      - **Never consolidates.** It SURFACES that a wrap is due; performing one is a separate,
        explicitly-authorised act. ⚠ **Amended for K4a [6], because the old wording is now false in
        one case:** this used to read "the consolidate itself is the human-gated efferent write the
        vagus is forbidden to perform." A seat installed by ``levain daemon install-seat`` may now
        consolidate itself — bounded in wall-clock time, and structurally forbidden to crystallize
        (:mod:`levain.firing.crystallization`). What was always the load-bearing half remains
        exactly true: **the FIRING never consolidates.** No in-turn afferent path may trip a memory
        write; the seat's consolidate is driven by its own invocation, after the turn is over. The
        return here is a string for the adopter to DISPLAY — never a transport send.
      - **Fail-soft.** Any read failure returns ``None`` — no nudge beats a crash, and a missed
        reminder is low-stakes for a HUMAN reader. ⚠ **A seat must NOT inherit that collapse:** call
        :func:`consolidate_readiness` and branch on ``.readable``, because for an unattended caller
        "unreadable" every interval is silent death, not a missed hint.
    """
    r = consolidate_readiness(episodic_path, threshold)
    if not r.due:
        return None
    return (
        f"{r.episodes} episodes captured since the last consolidate — a wrap is due. "
        "(The firing never consolidates autonomously; running the wrap is the step "
        "that metabolizes these raw captures into memory.)"
    )


class AnnealFiring:
    """A real :class:`~levain.firing.FiringContract`: inject ``constitution`` + crystallized-
    pattern recall (against ``req.query``) + a ``turn_index``-rotated drift-defense directive.
    Afferent-only; fail-soft on every recall failure.

    SERIALIZATION (fork/reload): reconstructed via the zero-arg registry factory, so a forked
    child rebuilds an instance with DEFAULT config. ``crystal_path`` survives — it's re-resolved
    per inject from ``VAGUS_CRYSTAL_PATH``/default, never frozen — but a custom ``constitution``
    or ``max_patterns`` passed to ``__init__`` does NOT survive a fork (the child uses defaults).
    Acceptable for Slice 2 (the default constitution). A per-deployment constitution wants a
    serialization-safe channel (an env/config the factory reads) — deferred to the rule-of-three
    firing-contract extraction, where per-adapter config serialization is the general problem.
    """

    def __init__(
        self,
        constitution: str = "You are operating inside a governed cognitive substrate.",
        crystal_path: Path | None = None,
        max_patterns: int = MAX_PATTERNS,
        episodic_path: Path | None = None,
    ) -> None:
        self.constitution = constitution
        self._crystal_path = crystal_path  # None → resolve per-inject (env / default)
        self._max_patterns = max_patterns
        self._episodic_path = episodic_path  # None → resolve per-capture (env / default)

    # Per-op path resolution as METHODS (not the module functions directly) so an isolated
    # subclass (``AnnealEntityFiring``) can override WHERE the store lives without touching the
    # inject/capture bodies. ``_recall`` / ``capture`` call ``self._resolve_*`` — the one seam an
    # entity firing redirects. An explicit ``crystal_path``/``episodic_path`` (tests / in-process)
    # wins; else the env/default resolution.
    def _resolve_crystal_path(self) -> Path:
        return self._crystal_path or _env_crystal_path()

    def _resolve_episodic_path(self) -> Path:
        return self._episodic_path or _env_episodic_path()

    def inject(self, req: InjectRequest) -> str:
        # session_start → the static constitution alone (the adapter sets it once into its
        # persistent trusted surface). per_turn → dynamic recall + the rotating directive;
        # the constitution is NOT repeated here (it persists in the session-start suffix).
        if req.lifecycle_point == "session_start":
            return self.constitution
        recall = self._recall(req.query)
        directive = select_directive(req.turn_index)
        return f"{recall}\n{directive}"

    def _recall(self, query: str) -> str:
        """Real crystallized-pattern recall, rendered for injection. Fail-soft: ANY failure
        degrades to a marker rather than raising into the agent's turn."""
        if not query.strip():
            return "[recall: (no query context this turn)]"
        try:
            from anneal_memory.crystal import CrystalStore
            from anneal_memory.retrieval import retrieve_patterns

            path = self._resolve_crystal_path()
            if not path.exists():
                return "[recall: (no crystal store)]"
            patterns = retrieve_patterns(
                CrystalStore(path), query, max_patterns=self._max_patterns
            )
            if not patterns:
                return "[recall: (nothing relevant this turn)]"
            # Render INSIDE the fail-soft boundary: anneal API drift or a malformed pattern shape
            # (a future version returning a different object) must degrade to a marker, not raise
            # out of the agent's turn. "No recall beats a crash" covers RENDERING too — rendering
            # is part of recall (apparatus L3 codex HIGH: the dangerous code had moved just below
            # the try; an in-lineage review cleared it by assuming a well-formed RelevantPattern).
            lines = "\n".join(
                f"- {p.name} ({p.level}x, {p.activation}) — {p.explanation}" for p in patterns
            )
            return f"[recall — crystallized patterns relevant to this turn]\n{lines}"
        except Exception as e:  # noqa: BLE001 — fail-soft IS the contract ("no recall beats a crash")
            return f"[recall: unavailable ({type(e).__name__})]"

    def capture(self, req: CaptureRequest) -> bool:
        """Append ``req`` as a RAW episode to the anneal episodic store — the afferent-safe
        substrate-write. Returns ``True`` iff the episode was durably written.

        **Append-only by construction:** it calls ``Store.record`` (the plain episodic INSERT)
        and NEVER ``Store.save_continuity`` (the gated consolidate the vagus is forbidden).
        **Fail-soft but LOUD:** any write failure logs a WARNING (a lost capture is data loss)
        and returns ``False``, but never raises into the agent's turn-end."""
        content = (req.content or "").strip()
        if not content:
            return False  # nothing to capture is a no-op, not a loss — never write an empty episode

        # The encoding shield already RAN (``CaptureRequest.__post_init__`` — it cannot be
        # skipped); this is the LOUD half. A silent repair would be its own trust problem for a
        # memory store, so surface it: the receipt rides the episode's metadata into the store,
        # and this warning puts it in front of the operator in the same session. `levain run`
        # installs no handler for this logger, so it reaches stderr via logging's lastResort.
        receipt = (req.metadata or {}).get(_ENCODING_RECEIPT_KEY)
        if receipt:
            _log.warning(
                "levain capture: upstream delivered mis-decoded text — %d span(s) repaired, "
                "%d left untouched and flagged (levain does not own the streaming decode path; "
                "this is a shield, not a cure). Receipt: %s",
                receipt.get("repaired", 0),
                len(receipt.get("suspect") or ()),
                {k: receipt[k] for k in ("repairs", "suspect") if k in receipt},
            )

        try:
            from anneal_memory import Store
            from anneal_memory.types import EpisodeType

            # Degrade an invalid episode_type to "observation" rather than lose the whole episode:
            # ``Store.record`` raises ValueError on a bad type, which the fail-soft boundary would
            # swallow as "episode LOST" — fail-soft-loud applied to the TYPE means we keep the
            # content (the valuable part) and only lose the mislabel, loudly. (anneal's enum is
            # observation/decision/tension/question/outcome/context — NOT flow's "finding".)
            etype = req.episode_type
            if etype not in {e.value for e in EpisodeType}:
                _log.warning(
                    "vagus capture: invalid episode_type %r — recording as 'observation'", etype
                )
                etype = "observation"

            # ``Store.record`` has no session_id parameter (the store derives its own from its
            # wraps table), so persist the adapter's session id as metadata provenance instead of
            # dropping it silently.
            meta = dict(req.metadata or {})
            if req.session_id:
                meta.setdefault("vagus_session_id", req.session_id)

            path = self._resolve_episodic_path()
            # A per-capture open → record → commit → close. ``audit=False`` is REQUIRED: anneal's
            # audit sidecar is single-writer (concurrent AuditTrail instances read the same
            # prev_hash and append incompatible hash-chain entries → corruption), and the vagus is
            # a GENERAL layer where multiple agents may capture to one store concurrently. The
            # episode INSERT itself is SQLite/WAL-safe; the tamper-evidence audit chain is reserved
            # for the gated single-writer consolidate path, NOT the high-frequency afferent append.
            # ``record`` never acquires the continuity-lock a consolidate holds. (codex L3 HIGH.)
            with Store(path, audit=False) as store:
                store.record(
                    content=content,
                    episode_type=etype,
                    source=req.source,
                    metadata=meta or None,
                )
            # There is deliberately NO ``store.save_continuity(...)`` on this path. Consolidation
            # — metabolizing episodes into the felt neocortex — is the gated efferent write the
            # vagus may NEVER perform autonomously ("captures but never consolidates", brief.md).
            return True
        except Exception as e:  # noqa: BLE001 — fail-soft, but LOUD: a lost capture is data loss
            _log.warning("vagus capture FAILED — episode LOST (%s): %s", type(e).__name__, e)
            return False


class AnnealEntityFiring(AnnealFiring):
    """An ISOLATED entity firing (kind ``"anneal_entity"``): resolves its crystal + episodic stores
    ONLY under the bound entity dir (``$LEVAIN_ENTITY_DIR`` / an explicit ``entity_dir``), behind
    the fail-closed sovereignty guard. It has NO ``~/.anneal-memory/`` fallback — the operator-
    laptop leak is structurally IMPOSSIBLE for this kind (``structural_invariants_beat_discipline``).

    Why the KIND carries isolation (not just env): ``firing_kind`` is a SERIALIZED field, so a
    ``fork()`` / reload rebuilds this as ``AnnealEntityFiring`` — never the laptop-defaulting
    ``AnnealFiring``. The entity DIR rides ``$LEVAIN_ENTITY_DIR`` (re-read per op, never frozen) —
    the one channel that survives a zero-arg registry rebuild (the same mechanism the legacy
    ``VAGUS_CRYSTAL_PATH`` override used). So even after a fork the contract holds by construction.

    FAIL-CLOSED-TO-SAFE at runtime: if no entity is bound (env unset) or the guard trips, the
    resolver RAISES — and because ``_recall`` / ``capture`` wrap resolution in the fail-soft
    boundary, that degrades to no-recall / a loud lost-capture, NEVER a silent read of the wrong
    store. The loud build-time guard (``build_entity_agent``) surfaces a misconfig before the REPL;
    this resolver is the fork/runtime backstop.
    """

    def __init__(self, entity_dir: Path | str | None = None, **kwargs: object) -> None:
        # ``entity_dir`` is for in-process / test construction; None → resolve per-op from
        # ``$LEVAIN_ENTITY_DIR`` (the fork-safe path). It is NOT frozen into crystal_path/
        # episodic_path — those must RE-resolve so a fork (zero-arg rebuild) still finds the entity
        # via env. Passing a crystal_path/episodic_path override to an entity firing is refused
        # (an explicit path could point at flow's store — the exact leak); the entity dir is the
        # single source of truth.
        if kwargs.get("crystal_path") is not None or kwargs.get("episodic_path") is not None:
            raise ValueError(
                "AnnealEntityFiring does not accept crystal_path/episodic_path — an isolated "
                "entity's stores are derived from its entity_dir only (a raw path override could "
                "escape the sovereign dir)."
            )
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._entity_dir = Path(entity_dir).expanduser() if entity_dir is not None else None

    def _entity_paths(self) -> tuple[Path, Path]:
        """Resolve + GUARD the entity's stores. Raises IsolationError if unbound or escaping."""
        entity_dir = resolve_entity_dir(self._entity_dir)
        crystal, episodic = entity_store_paths(entity_dir)
        assert_entity_isolated(crystal, episodic, entity_dir=entity_dir)
        return crystal, episodic

    def _resolve_crystal_path(self) -> Path:
        return self._entity_paths()[0]

    def _resolve_episodic_path(self) -> Path:
        return self._entity_paths()[1]


register_firing("anneal", AnnealFiring)
register_firing("anneal_entity", AnnealEntityFiring)
