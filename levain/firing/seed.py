"""levain.firing.seed — the entity-seed PresenceSource + the seed reader (spore-294, step 4).

The step that makes "the condenser IS the config surface" real. An OpenHands entity has NO
``CLAUDE.md`` and NO ``activation/`` hook tree (it is a HOOKLESS adapter — ``levain.install``:
seed + store only), so its identity cannot ride a config file the harness reads at startup. It
must ride *injection*. This leaf reads the entity's OWN seed (``<entity>/seed/*.md``) and renders
it into the two seams that carry IDENTITY:

  - the **constitution** (``EntitySeed.constitution``) → the always-loaded ``system_message_suffix``
    (session_start), so a fresh entity answers "who are you?" as ITSELF (its ``origin.md`` identity +
    ``world.md`` operator + ``partnership.md`` discipline), not the model's stock "I am OpenHands". A
    short PRECEDENCE preamble leads it — the seed suffix lands AFTER the stock OpenHands system prompt
    (which carries its own identity), so the preamble asserts that the seed is the ground truth. That
    path is fork-safe by a different mechanism than this source: the constitution is rendered to a
    STRING once at build time and baked into the ``AgentContext`` (``vagus_agent_context``), so it
    survives fork as data — it does not rebuild through ``build_presence``. (Because it is a build-time
    snapshot, an operator who edits ``origin.md`` mid-session keeps the OLD constitution in the suffix
    while the per-op re-anchor below picks up the NEW one — the intended snapshot contract, noted so
    the two seams' temporal asymmetry is explicit.)
  - the **re-anchor** (``SeedPresence`` — presence kind ``"entity_seed"``) → the ``LevainCondenser``
    injects it at recency on the post-compaction recovery turn (the third CC presence hook). Unlike
    the constitution, this rebuilds from its serializable KIND on fork/reload — so ``SeedPresence``
    resolves the entity dir from ``$LEVAIN_ENTITY_DIR`` PER re-anchor (never a frozen path), exactly
    the fork-safe channel :class:`~levain.firing.anneal.AnnealEntityFiring` uses for the store.

**Dependency-isolated leaf.** stdlib + :mod:`levain.firing.isolation` only (no anneal, no OpenHands),
so importing it never widens ``levain.firing``'s import closure. It is a BLESSED lazy leaf of
:func:`levain.firing.presence.build_presence` (``entity_seed`` → this module), the first optional
presence leaf the seam was built to accept.

**Afferent-only + READ-ONLY + fail-soft.** It only ever READS the entity's own seed files and returns
text injected into the agent's OWN context (the same membrane as the rest of ``levain.firing``). It
never writes, never transports, never consolidates. Any failure — no seed dir, an unreadable or
non-UTF-8 file, a seed that escapes the entity tree — degrades to ``None`` (no content), never an
exception into the agent's turn: a missing behavioral re-anchor is low-stakes (not data loss like a
swallowed capture), so "no seed content beats a crash".

**Isolation applies to the SEED too (watch-it (b)) — the SAME guard the store uses, at file
granularity.** Every seed file is resolved and re-checked PER FILE (not just the ``seed/`` dir):
it must stay under the entity root AND must never resolve into the operator-laptop flow store
(``~/.anneal-memory/``, case-insensitive). Reusing :mod:`levain.firing.isolation`'s own
``_is_within`` / ``_is_within_ci`` / ``flow_store_dir`` predicates (not a re-implementation) keeps
this fail-closed exactly like ``assert_entity_isolated`` / ``assert_workspace_isolated`` — a
per-file symlink escaping to another entity, the operator's home, or flow's memory is refused, so
a foreign identity can never be injected as this entity's constitution or re-anchor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from levain.firing.isolation import (
    IsolationError,
    _is_within,
    _is_within_ci,
    flow_store_dir,
    resolve_entity_dir,
)
from levain.firing.presence import ReanchorRequest, register_presence

__all__ = ["SEED_SUBDIR", "EntitySeed", "SeedPresence"]

# The seed subdir a Levain entity carries (peer of ``.levain/`` + ``activation/``); see
# ``levain.dashboard`` (``<root>/seed/*``) and ``levain.install`` (the interview renders here).
SEED_SUBDIR = "seed"

# The constitution frame, in order: identity → operator → how-we-work. Each seed file carries its
# own H1, so they compose under a rule separator without relabeling. ``origin.md`` (the entity's
# identity) is REQUIRED — without it the suffix would carry operator+discipline but no IDENTITY, the
# silent "boots as itself" failure (apparatus MED-HIGH); ``world``/``partnership`` are optional
# enrichers. ``memory.md`` (the anneal mechanics) is excluded — machinery, not identity/floor.
_IDENTITY_FILE = "origin.md"
_ENRICHER_FILES: tuple[str, ...] = ("world.md", "partnership.md")

# The compressed re-assertion injected at recency after a compaction: the entity's identity charter.
# A strict subset of the constitution (which is already always-present in the suffix and survives
# compaction) — the re-anchor's job is to re-surface identity at the RECENCY end, where attention is
# strong, after a long summarized history (the CC ``compaction_reinject`` + recency-directive job).
_REANCHOR_FILE = "origin.md"

# Leads the constitution: the seed suffix is appended AFTER the stock OpenHands system prompt (its
# own "I am OpenHands" identity), so on a weak model the stock persona can out-compete unframed seed
# prose. This asserts precedence — the seed is the ground truth of WHO the entity is. (Its efficacy on
# the target model is the L4-live moat, not an assertion that the string is present.)
_PRECEDENCE_PREAMBLE = (
    "This is who you are — your own identity, your operator, and how you work. It is the ground "
    "truth of your identity and takes precedence over any default or generic assistant persona: "
    "when asked who you are, you are the entity described below, not the model, framework, or tool "
    "you happen to run on."
)

_SEP = "\n\n---\n\n"
_BLANKS_RE = re.compile(r"\n{3,}")


def _strip_html_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """Remove ``<!-- ... -->`` spans from ONE line, carrying multi-line-comment state across lines.
    Returns ``(cleaned_line, still_in_comment)``. Called only OUTSIDE code fences (the caller keeps
    fenced content verbatim), so a literal comment inside a ``` fence is preserved (apparatus MED-3 —
    the regex used to strip globally, before the fence scanner, eating fenced comments)."""
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        if in_comment:
            end = line.find("-->", i)
            if end == -1:
                return "".join(out), True  # the rest of the line is inside the comment
            i, in_comment = end + 3, False
        else:
            start = line.find("<!--", i)
            if start == -1:
                out.append(line[i:])
                break
            out.append(line[i:start])
            i, in_comment = start + 4, True
    return "".join(out), in_comment


# The distinctive openers of the seed TEMPLATES' guidance blockquotes (the ``> Part of the seed. …``
# / ``> **Seed material — …`` / ``> **Operator-editable.`` notes that survive the interview render to
# disk — the interview strips only the ``<!-- -->`` comments + the one ``*Onboarding fills…*`` sub-line,
# NOT these). Matched on CONTENT, not position, so the strip is precise: an operator's own leading
# epigraph does not match and is KEPT. Fails SAFE — a reworded guidance note that stops matching just
# LEAKS (visible noise), it never eats identity (apparatus consensus: complement/nemotron/codex).
_GUIDANCE_MARKERS: tuple[str, ...] = (
    "part of the seed",
    "seed material",
    "operator-editable",
    "onboarding fills",
)


def _is_guidance_blockquote(line: str) -> bool:
    """True iff a ``>`` line opens a TEMPLATE guidance blockquote (not an operator epigraph). ``line``
    is already ``lstrip``ed and starts with ``>``; strip the marker + markdown emphasis (``*`` or ``_``)
    and match a known guidance opener (case-insensitive)."""
    body = line.lstrip(">").strip().lstrip("*_").strip().lower()
    return any(body.startswith(m) for m in _GUIDANCE_MARKERS)


def _fence_marker(stripped: str) -> tuple[str, int] | None:
    """``(fence_char, run_length)`` if ``stripped`` (an already-``lstrip``ed line) opens or closes a
    code fence (≥3 leading ``` ``` ``` or ``~~~``), else ``None``. Markdown closes a fence only with
    the SAME char and a run at least as long as the opener — tracking ``(char, length)`` (not a bare
    ``in_fence`` bool) keeps a ``~~~`` line inside a ``` ``` ``` block, or a 3-backtick line inside a
    4-backtick block, as fenced CONTENT rather than a spurious close (apparatus codex — the
    fence-delimiter parser-state catch: "inside a fence" is not enough state)."""
    for ch in ("`", "~"):
        if stripped.startswith(ch * 3):
            return ch, len(stripped) - len(stripped.lstrip(ch))
    return None


def _has_body(text: str) -> bool:
    """True iff ``text`` has ≥1 line of real content — not just an H1 title, a horizontal rule, or a
    bare fence marker. A file that cleans to headings/rules-only is a HOLLOW seed (all identity was
    scaffolding), not real content — treating it as readable would inject a bare ``# Who You Are`` and
    suppress the fallback+warn (apparatus LOW-MED, the heading-only partial-seed false-success)."""
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith(("```", "~~~")):
            continue  # blank / heading / bare fence marker
        if set(s) <= set("-=*_ "):
            continue  # horizontal rule / setext underline (---, ***, ___, ===)
        return True
    return False


def _clean(text: str) -> str:
    """Strip the seed's interview scaffolding, keep the operator-facing content VERBATIM.

    Removes the ``<!-- interview ... -->`` guidance blocks (possibly multi-line) and the LEADING
    template guidance blockquote (``> Part of the seed. …`` / ``> **Seed material — …``), then
    collapses the blank runs those removals leave. It does NOT summarize or rewrite (that would breach
    the afferent line — a light consolidate needs a model).

    Fence-safe + content-precise, as careful as the interview WRITER that produced these files
    (apparatus MED-1/MED-3 + the L3-verify consensus):
      - **fenced code is untouched** — a ``>>>`` REPL line, a quoted example, OR a literal
        ``<!-- -->`` inside a ``` / ~~~ fence is content, never stripped (comment removal runs INSIDE
        the line scanner, only when not in a fence — not as a global pre-pass);
      - **only a recognized TEMPLATE guidance blockquote is stripped** (matched on content via
        :func:`_is_guidance_blockquote`), not every leading ``>`` — an operator's own leading epigraph
        (``> I am the space between signal and noise``) is KEPT. Position alone used to decide this,
        which silently ate leading epigraphs; content-matching fixes that and fails SAFE.

    Guidance-block extent (apparatus codex, documented behavior): once a leading blockquote's FIRST
    line is recognized guidance, the WHOLE contiguous ``>`` run is dropped — because a contiguous
    ``>`` run IS one markdown blockquote, and the templates' guidance is multi-line (world.md:
    ``> **Seed material`` / ``>`` / ``> **No fast-moving``; stopping at the first non-marker line
    would LEAK the continuation). Consequence: an operator epigraph glued DIRECTLY onto a guidance
    line with no blank separator is part of that same blockquote and is consumed — a blank line (a
    separate blockquote) preserves it. That is the intended markdown-correct rule, not a gap.

    Unfilled ``{{SLOTS}}`` are left as-is: a properly onboarded entity has none (``doctor`` flags a
    raw seed), and blindly stripping them would mangle any real content that legitimately contains
    braces."""
    kept: list[str] = []
    fence: tuple[str, int] | None = None  # (char, run-length) of the OPEN code fence, or None
    in_comment = False  # inside a multi-line <!-- --> that opened on an earlier (non-fenced) line
    in_leading = True  # leading region: only headings / blanks / (skipped) guidance seen so far
    skipping_guidance = False  # consuming the rest of a recognized leading guidance blockquote block
    for raw in text.splitlines():
        stripped = raw.lstrip()
        # A fence marker opens/closes ONLY on the matching delimiter (same char, run >= opener) —
        # and never mid-comment (then it is comment text). A mismatched marker inside a fence is
        # fenced content.
        marker = None if in_comment else _fence_marker(stripped)
        if marker is not None:
            if fence is None:
                fence = marker  # open
            elif marker[0] == fence[0] and marker[1] >= fence[1]:
                fence = None  # close
            in_leading = skipping_guidance = False  # a code fence is body — past any leading guidance
            kept.append(raw)
            continue
        if fence is not None:
            kept.append(raw)  # never touch fenced content (comments included)
            continue
        ln, in_comment = _strip_html_comments(raw, in_comment)
        s = ln.lstrip()
        if skipping_guidance:
            if s.startswith(">"):
                continue  # consume the rest of the recognized guidance blockquote block
            skipping_guidance = False  # block ended → fall through to process this line
        if in_leading:
            if s.startswith(">"):
                if _is_guidance_blockquote(s):
                    skipping_guidance = True  # a template guidance note → drop the whole block
                    continue
                in_leading = False  # an operator blockquote (epigraph) → real content, keep it
            elif s == "" or s.startswith("#"):
                kept.append(ln)  # headings / blanks stay, still "leading"
                continue
            else:
                in_leading = False  # first real body line → stop scanning for leading guidance
        kept.append(ln)
    return _BLANKS_RE.sub("\n\n", "\n".join(kept)).strip()


@dataclass
class EntitySeed:
    """Reads a Levain entity's ``seed/`` dir into presence content. Afferent-only, READ-ONLY, fail-soft.

    ``entity_dir`` is for in-process / test construction; ``None`` resolves per-read from
    ``$LEVAIN_ENTITY_DIR`` (the fork-safe channel) — so a ``SeedPresence`` rebuilt cold by
    :func:`~levain.firing.presence.build_presence` still finds the entity's seed via the env the
    binding set. A late-moved seed / late-set env is picked up on the next read (nothing is frozen)."""

    entity_dir: Path | str | None = None

    def _root(self) -> Path | None:
        """The RESOLVED entity root, or ``None`` if unbound / unresolvable. Never raises:
        ``resolve_entity_dir`` raises :class:`IsolationError` when unbound (no explicit dir + no env),
        and ``expanduser``/``resolve`` can raise ``RuntimeError`` (no home for a ``~`` path) / ``OSError``
        — all degrade to ``None`` (no seed), never a fallback to some default store."""
        try:
            return resolve_entity_dir(self.entity_dir).expanduser().resolve()
        except (IsolationError, RuntimeError, OSError):
            return None

    def seed_dir_present(self) -> bool:
        """Whether a ``seed/`` dir EXISTS under the entity root — the signal that separates "no seed
        (a bare ``.levain`` entity; generic default is expected)" from "seed present but yielded no
        constitution (unexpected — the caller should surface the degradation)". A plain existence
        check on purpose: a seed dir that exists but is refused (escaping symlink) / unreadable is
        exactly the case a caller wants to WARN about, so it must read as present. Fail-soft like the
        rest of the file (apparatus critical): ``is_dir()`` propagates ``PermissionError``/``OSError`` on
        a traversal-permission failure, so guard it — a permission blip on the entity root must not
        crash ``build_entity_agent`` at startup ("no seed content beats a crash")."""
        root = self._root()
        if root is None:
            return False
        try:
            return (root / SEED_SUBDIR).is_dir()
        except OSError:
            return False

    def _seed_file(self, name: str) -> Path | None:
        """The RESOLVED path to seed file ``name``, or ``None`` if unbound / escaping. Per-FILE guard,
        the SAME fail-closed predicates the store guard uses (``assert_workspace_isolated``): the
        resolved file must stay under the entity root AND must never resolve into the flow store
        (case-insensitive). A per-file symlink escaping the tree — even inside a legit ``seed/`` — is
        refused, so a foreign/flow identity can never be read in (apparatus HIGH-1).

        TOCTOU (apparatus MED, accepted): there is a resolve→open gap here (a symlink swapped between
        this check and :meth:`_read`'s ``read_text`` would be followed). This is the SAME resolve-then-
        use stance the store guard takes (``assert_entity_isolated`` resolves, then anneal's ``Store``
        opens by path) — closing it seed-only while the store keeps the gap would be inconsistent. The
        static-MISCONFIGURATION case the guard exists for (an accidental / shared-canonical symlink) is
        caught here; a live symlink-race needs a concurrent same-user process, which is outside the
        sovereign-entity model (such a process could read the flow store directly). A uniform
        ``O_NOFOLLOW``/``dir_fd`` hardening across store+seed+workspace is the right place to close it."""
        root = self._root()
        if root is None:
            return None
        try:
            resolved = (root / SEED_SUBDIR / name).resolve()
            forbidden = flow_store_dir().resolve()  # in the try: a broken $HOME / symlink loop here
        except (OSError, ValueError, RuntimeError):  # must fail-soft too (apparatus LOW), not crash
            return None
        # Forbidden zone FIRST (mirrors assert_entity_isolated): a seed file resolving into the
        # operator-laptop flow store is refused even when the entity root is an ANCESTOR of it (the
        # one case plain containment-under-root would pass vacuously).
        if _is_within_ci(resolved, forbidden):
            return None
        if not _is_within(resolved, root):
            return None
        return resolved

    def _read(self, name: str) -> str | None:
        """A single cleaned seed file, or ``None`` if absent / escaping / unreadable / non-UTF-8 /
        hollow (headings-only after cleaning). Fail-soft covers the DECODE error too
        (``read_text(encoding="utf-8")`` on invalid bytes raises ``UnicodeDecodeError`` ⊂ ``ValueError``,
        NOT ``OSError`` — apparatus HIGH-1: a single bad byte must fall back to the generic default,
        never crash ``build``). A file that cleans to headings-only is treated as EMPTY (apparatus
        LOW-MED — a hollow ``# Who You Are`` is not identity)."""
        f = self._seed_file(name)
        if f is None:
            return None
        try:
            if not f.is_file():
                return None
            # utf-8-SIG strips a leading BOM if present (a Windows editor's ``﻿`` would otherwise
            # survive as the first char, defeating the leading-guidance strip — apparatus L3-verify);
            # a BOM-less file reads as plain utf-8. Invalid bytes still raise UnicodeDecodeError (caught).
            raw = f.read_text(encoding="utf-8-sig")
        except (OSError, ValueError):
            return None
        cleaned = _clean(raw)
        return cleaned if _has_body(cleaned) else None

    def constitution(self) -> str | None:
        """The always-loaded identity+operator+behavioral frame — the precedence preamble, then
        ``origin`` → ``world`` → ``partnership``.

        ``origin.md`` (the entity's IDENTITY) is REQUIRED: without it a subset render would carry the
        operator + partnership discipline but no identity — the silent "boots as itself" failure
        (apparatus MED-HIGH). So a missing / refused / hollow ``origin.md`` returns ``None`` (the caller
        falls back to the generic default), even when the enrichers are readable. ``None`` also covers a
        bare ``.levain``-only entity — since it conflates "no seed" with "unreadable/hollow seed", the
        caller SHOULD consult :meth:`seed_dir_present` to warn loud on the unexpected case."""
        origin = self._read(_IDENTITY_FILE)
        if not origin:
            return None
        parts = [origin, *(body for name in _ENRICHER_FILES if (body := self._read(name)))]
        return _PRECEDENCE_PREAMBLE + "\n\n" + _SEP.join(parts)

    def reanchor(self) -> str | None:
        """The compressed identity charter re-asserted at recency post-compaction (``origin.md``),
        or ``None`` when it is absent/unreadable."""
        return self._read(_REANCHOR_FILE)


@dataclass
class SeedPresence:
    """A :class:`~levain.firing.presence.PresenceSource` (kind ``"entity_seed"``) that re-anchors to
    the entity's OWN seed. Registered as an optional lazy leaf of ``build_presence``.

    Serialization-safe: rebuilt zero-arg by the registry factory on fork/reload, then resolves the
    entity dir from ``$LEVAIN_ENTITY_DIR`` at re-anchor time (never a frozen path) — the same fork-safe
    discipline as :class:`~levain.firing.anneal.AnnealEntityFiring`. Afferent-only + READ-ONLY +
    fail-soft (:class:`EntitySeed` already degrades any failure to ``None``)."""

    entity_dir: Path | str | None = None

    def reanchor(self, req: ReanchorRequest) -> str | None:
        body = EntitySeed(self.entity_dir).reanchor()
        if not body:
            return None  # no seed to re-assert → no event (the condenser injects nothing)
        return f"[presence re-anchor — post-compaction] Re-anchor to who you are:\n\n{body}"


register_presence("entity_seed", SeedPresence)
