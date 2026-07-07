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
from pathlib import Path

from levain.firing.contract import CaptureRequest, InjectRequest, register_firing, select_directive

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


def _resolve_crystal_path() -> Path:
    env = os.environ.get("VAGUS_CRYSTAL_PATH", "").strip()
    return Path(env) if env else DEFAULT_CRYSTAL_PATH


def _resolve_episodic_path() -> Path:
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


def wrap_nudge(episodic_path: Path | None = None, threshold: int | None = None) -> str | None:
    """The SessionEnd→wrap-nudge — afferent SURFACING. Returns a human-facing nudge string iff at
    least ``threshold`` episodes have accumulated since the last consolidate (a wrap is due), else
    ``None``.

    AFFERENT-ONLY, by construction:
      - **Read-only.** It opens the episodic store ``read_only=True`` and counts
        ``episodes_since_wrap()`` — no write, no audit sidecar, no write-lock contention against a
        concurrent capture or wrap.
      - **Never consolidates.** It SURFACES that a wrap is due; the consolidate itself is the
        human-gated efferent write the vagus is forbidden to perform. The return is a string for
        the adopter to DISPLAY — it is NOT a transport send (a push/email would cross to efferent).
      - **Fail-soft.** Any read failure (store absent/corrupt, anneal missing) returns ``None`` —
        no nudge beats a crash; a missing reminder is low-stakes (not data loss like a capture)."""
    thr = _resolve_wrap_nudge_threshold(threshold)
    try:
        from anneal_memory import Store

        path = episodic_path or _resolve_episodic_path()
        if not Path(path).exists():
            return None
        with Store(path, read_only=True) as store:
            count = len(store.episodes_since_wrap())
        if count < thr:
            return None
        return (
            f"{count} episodes captured since the last consolidate — a wrap is due. "
            "(The vagus never consolidates autonomously; running the wrap is the human-gated step "
            "that metabolizes these raw captures into memory.)"
        )
    except Exception as e:  # noqa: BLE001 — fail-soft: a missing nudge is not data loss
        _log.debug("vagus wrap_nudge unavailable (%s): %s", type(e).__name__, e)
        return None


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

            path = self._crystal_path or _resolve_crystal_path()
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

            path = self._episodic_path or _resolve_episodic_path()
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


register_firing("anneal", AnnealFiring)
