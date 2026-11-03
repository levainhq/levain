"""levain.dream.card (D1-1c): the morning card's body, computed and bounded (D1_EXECUTOR §5).

A card is COMPUTED, never authored by the dream: a diff of the snapshot against the saved text, plus the notes the
compose returned, so a dream cannot hide its own change. ``his`` holds only what flow could not settle itself (Q-D2 a,
decided 10-30): a contradiction the compose could not resolve, an integrity item recovery could not settle
(``unknown-commit``), and a retirement whose quoted words are not in the cited episode. Everything else a dream did is
``own``. ``authority_relevant`` is exactly ``bool(his)``, and it is decided from the FULL list before any bounding: a
card never loses its authority bit to a size cut.

The body is bounded (``review.py`` refuses a body over ``2 * _TEXT_MAX``). The full diff is an artifact beside the
occurrence (``card_diff.txt``); the body carries its sha256, path and per-section counts. ``own`` entries are cut to
their first line and a count when needed; ``his`` entries are never cut: if they alone pass the limit the body carries
their count and the path of ``card_his.json``.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from levain.contracts.canon import canonical
from levain.dream import usage as _usage

FEED = "agent"
KINDS = ("consolidated", "failed", "did-not-consolidate", "rolled-back")
#: flow's NOTE_KEYS, the notes a compose returns
NOTE_KEYS = ("premise_flags", "contradictions", "retired", "for_phill")
DIFF_FILE = "card_diff.txt"
HIS_FILE = "card_his.json"
OWN_FILE = "card_own.json"
#: ``review._TEXT_MAX`` doubled: the longest canonical body review.py files. Asserted equal by a test.
BODY_LIMIT = 8000
_FIRST_LINE = 140
_REFUSALS_KEPT = 5
_SHORT_QUOTE_WORDS = 4


class CardError(Exception):
    """A card could not be built within its bound."""


@dataclass
class Card:
    kind: str
    dedupe_key: str
    body: dict[str, Any]
    authority_relevant: bool
    artifacts: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "dedupe_key": self.dedupe_key, "body": self.body,
                "authority_relevant": self.authority_relevant, "artifacts": self.artifacts}


# ------------------------------------------------------------------------------------------------------------- diff

_HEADING = re.compile(r"^##\s+(.*?)\s*$")


def sections(text: str) -> dict[str, list[str]]:
    """The neocortex's ``## Heading`` sections as ``{name: lines}``; text before the first heading is ``(preamble)``."""
    out: dict[str, list[str]] = {}
    cur = "(preamble)"
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            cur = m.group(1)
            out.setdefault(cur, [])
        else:
            out.setdefault(cur, []).append(line)
    return out


def section_counts(prior: str, saved: str) -> dict[str, dict[str, int]]:
    """Per section, the lines added and removed (changed sections only)."""
    a, b = sections(prior), sections(saved)
    out: dict[str, dict[str, int]] = {}
    for name in [*a, *(n for n in b if n not in a)]:
        add = rem = 0
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a.get(name, []), b.get(name, []),
                                                           autojunk=False).get_opcodes():
            if tag != "equal":
                rem += i2 - i1
                add += j2 - j1
        if add or rem:
            out[name] = {"added": add, "removed": rem}
    return out


def full_diff(prior: str, saved: str) -> str:
    return "".join(difflib.unified_diff(prior.splitlines(True), saved.splitlines(True), "prior", "saved", n=1))


def _first(line: str, n: int = _FIRST_LINE) -> str:
    one = (line.strip().splitlines() or [""])[0]
    return one if len(one) <= n else one[: n - 1] + "…"


_BOLD = re.compile(r"\*\*(.+?)\*\*")
_PATTERN = re.compile(r"^\s*(?:-\s*)?[!~?]*\s*([A-Za-z0-9_]+)\s*\|")


def _decision_titles(lines: Sequence[str]) -> list[str]:
    titles: list[str] = []
    for line in lines:
        titles.extend(t.strip() for t in _BOLD.findall(line))
    return list(dict.fromkeys(titles))


def _pattern_names(lines: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(m.group(1) for line in lines if (m := _PATTERN.match(line))))


def _paragraphs(lines: Sequence[str]) -> list[str]:
    return [p.strip() for p in "\n".join(lines).split("\n\n") if p.strip()]


def _own_from_diff(prior: str, saved: str) -> list[str]:
    """What the saved text changed that is visible by itself: a Decisions entry removed or folded (with where its
    title now stands), Patterns added and removed, and the identity tier (Understanding) paragraphs that changed."""
    a, b = sections(prior), sections(saved)
    out: list[str] = []
    before, after = _decision_titles(a.get("Decisions", [])), _decision_titles(b.get("Decisions", []))
    for t in [t for t in before if t not in after]:
        elsewhere = [n for n, ls in b.items() if any(t in line for line in ls)]
        out.append(f"Decisions: {t} left the Decisions entries "
                   + (f"(its title still stands in {', '.join(elsewhere)})" if elsewhere
                      else "(no forwarding address in the saved text)"))
    for t in [t for t in after if t not in before]:
        out.append(f"Decisions: {t} added")
    pa, pb = _pattern_names(a.get("Patterns", [])), _pattern_names(b.get("Patterns", []))
    out += [f"Patterns: removed {n}" for n in pa if n not in pb]
    out += [f"Patterns: added {n}" for n in pb if n not in pa]
    ua, ub = _paragraphs(a.get("Understanding", [])), _paragraphs(b.get("Understanding", []))
    out += [f"Understanding (identity tier) changed: {_first(p)}" for p in ub if p not in ua]
    # a paragraph edited in place is one "changed" line, not a "changed" plus a "removed"
    out += [f"Understanding (identity tier) removed: {_first(p)}" for p in ua
            if p not in ub and not any(q[:20] == p[:20] for q in ub)]
    return out


# ------------------------------------------------------------------------------------------------------------- notes

def _items(notes: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    """A notes list as dicts: a bare string is ``{"what": s}``; anything unreadable is skipped by the caller's
    ``notes_problem`` line, never silently turned into content."""
    raw = notes.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        return [{"what": f"(notes[{key!r}] is a {type(raw).__name__}, not a list)"}]
    out = []
    for x in raw:
        if isinstance(x, str):
            out.append({"what": x})
        elif isinstance(x, dict):
            out.append(x)
        else:
            out.append({"what": f"(an unreadable {key} entry: {type(x).__name__})"})
    return out


def _eps(x: Mapping[str, Any]) -> str:
    e = x.get("episodes") or x.get("episode") or []
    e = [e] if isinstance(e, str) else e
    return f" [{', '.join(str(i) for i in e)}]" if e else ""


def _words_in_episode(words: str, cited: Sequence[str], source: Mapping[str, str]) -> bool:
    norm = lambda s: re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()  # noqa: E731
    w = norm(words)
    return bool(w) and any(w in norm(source.get(c, "")) for c in cited)


def split_notes(notes: Mapping[str, Any] | None, source: Mapping[str, str] | None = None
                ) -> tuple[list[str], list[str]]:
    """``(his, own)`` lines from a compose's notes. ``contradictions`` are his. ``premise_flags``, ``for_phill`` and
    ``retired`` are own; a ``retired`` entry whose ``his_words`` cannot be found verbatim in a cited episode (when
    ``source``, episode id to text, is given) is his, since a retirement not backed by his words is a contradiction."""
    notes = notes or {}
    his: list[str] = []
    own: list[str] = []
    if notes.get("notes_problem"):
        own.append(f"the dream's notes were unreadable: {_first(str(notes['notes_problem']))}")
    for x in _items(notes, "contradictions"):
        his.append(f"contradiction: {x.get('what', '?')}{_eps(x)}")
    for x in _items(notes, "premise_flags"):
        own.append(f"premise moved: {x.get('ruling', x.get('what', '?'))} — {x.get('why', '?')}")
    for x in _items(notes, "for_phill"):
        own.append(f"for Phill: {x.get('what', '?')}{_eps(x)}")
    for x in _items(notes, "retired"):
        words = str(x.get("his_words", ""))
        cited = x.get("episodes") or x.get("episode") or []
        cited = [cited] if isinstance(cited, str) else [str(c) for c in cited]
        line = f"{x.get('what', '?')} — his words: \"{words or '?'}\"{_eps(x)}"
        if source is not None and not (len(words.split()) >= _SHORT_QUOTE_WORDS
                                       and _words_in_episode(words, cited, source)):
            his.append(f"retired without his verified words: {line}")
        else:
            own.append(f"retired: {line}")
    return his, own


# ------------------------------------------------------------------------------------------------------------ the body

def _write(path: Path, data: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def _refusals(doc: Mapping[str, Any] | None) -> list[str]:
    rows = (doc or {}).get("composes") or []
    return [f"compose {r.get('n')}: {_first(str(r['error']))}" for r in rows if isinstance(r, dict) and r.get("error")]


def _models(doc: Mapping[str, Any] | None, given: Sequence[str] | None) -> list[str]:
    if given:
        return list(dict.fromkeys(str(m) for m in given))
    seen: list[str] = []
    for r in (doc or {}).get("composes") or []:
        if isinstance(r, dict):
            for m in [r.get("model"), *(r.get("models_charged") or [])]:
                if isinstance(m, str) and m and m not in seen:
                    seen.append(m)
    return seen


def build_card(kind: str, *, occurrence: str, agent_id: str = "dream", day: str | None = None, reason: str = "",
               manual: bool = False, slot: str | None = None, prior: str = "", saved: str = "",
               notes: Mapping[str, Any] | None = None, source: Mapping[str, str] | None = None,
               his_extra: Sequence[str] = (), own_extra: Sequence[str] = (), models: Sequence[str] | None = None,
               usage_doc: Mapping[str, Any] | None = None, remedy: str | None = None,
               out_dir: Path | str | None = None, limit: int = BODY_LIMIT) -> Card:
    """Build one card. ``out_dir`` (the occurrence dir) receives ``card_diff.txt`` always when ``prior`` or ``saved``
    is given, and ``card_his.json`` / ``card_own.json`` when a list is cut from the body; a cut that needs a path and
    has no ``out_dir`` raises :class:`CardError`. ``his_extra`` is recovery's integrity lines (``unknown-commit``)."""
    if kind not in KINDS:
        raise CardError(f"a card kind is one of {', '.join(KINDS)} (got {kind!r})")
    out = Path(out_dir) if out_dir is not None else None
    n_his, n_own = split_notes(notes, source)
    his = [*n_his, *(str(x) for x in his_extra)]
    own = [*(_own_from_diff(prior, saved) if (prior or saved) else []), *n_own, *(str(x) for x in own_extra)]
    authority = bool(his)                          # decided from the FULL list, before any cut
    artifacts: dict[str, str] = {}
    body: dict[str, Any] = {"kind": kind, "reason": reason, "occurrence": {"id": occurrence, "manual": manual,
                                                                          "slot": slot}}
    if day:
        body["day"] = day
    if kind == "rolled-back":
        dedupe = f"dream-rollback:{occurrence}"
    elif kind == "did-not-consolidate":
        dedupe = f"dream-night:{agent_id}:{day or occurrence}"
    else:
        dedupe = f"dream:{occurrence}"
    body["models"] = _models(usage_doc, models)
    ref = _refusals(usage_doc)
    body["composes"] = {"n": len((usage_doc or {}).get("composes") or []), "refusals": ref[:_REFUSALS_KEPT],
                        "refusals_total": len(ref)}
    if prior or saved:
        text = full_diff(prior, saved)
        entry = {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "sections": section_counts(prior, saved)}
        if out is not None:
            _write(out / DIFF_FILE, text)
            entry["path"] = str(out / DIFF_FILE)
            artifacts["diff"] = entry["path"]
        body["diff"] = entry
    body["his"] = list(his)
    body["own"] = list(own)
    body["usage"] = _usage.summary(dict(usage_doc) if usage_doc is not None else None)
    body["remedy"] = remedy or f"levain dream rollback {occurrence}"

    def size() -> int:
        return len(canonical(body))

    def need_out(what: str) -> Path:
        if out is None:
            raise CardError(f"the card body passes {limit} characters and {what} needs an out_dir for its artifact")
        return out

    if size() > limit and own:
        # own entries to their first line and a count; the full list goes beside the occurrence
        o = need_out("the own list")
        _write(o / OWN_FILE, json.dumps(own, indent=2))
        artifacts["own"] = str(o / OWN_FILE)
        body["own"] = [_first(x, 80) for x in own]
        body["own_total"] = len(own)
        body["own_path"] = artifacts["own"]
    if size() > limit and body["own"]:
        keep = len(body["own"])
        while size() > limit and keep > 0:
            keep -= 1
            body["own"] = body["own"][:keep]
        body["own_shown"] = keep
    if size() > limit and his:
        o = need_out("the his list")
        _write(o / HIS_FILE, json.dumps(his, indent=2))
        artifacts["his"] = str(o / HIS_FILE)
        body["his"] = []                           # never cut: moved whole, with its count and path
        body["his_count"] = len(his)
        body["his_path"] = artifacts["his"]
    if size() > limit:
        # the last resort keeps the identity of the card and the way to everything else
        keep_keys = ("kind", "reason", "occurrence", "day", "diff", "his", "his_count", "his_path", "own_total",
                     "own_path", "remedy")
        body = {k: body[k] for k in keep_keys if k in body}
        body["truncated"] = True
        body["reason"] = _first(str(body.get("reason", "")), 200)
        if size() > limit:
            raise CardError(f"the card body is still {size()} characters after every cut (limit {limit})")
    return Card(kind, dedupe, body, authority, artifacts)


# ------------------------------------------------------------------------------------------------- from the pieces

def texts_for(db_path: Path | str, occ_dir: Path | str) -> tuple[str, str]:
    """``(prior, saved)`` continuity text of a dream: the snapshot's copy and the store's live text. A first dream
    has no prior text. Raises :class:`CardError` when the snapshot is missing."""
    from levain.dream import snapshot as sn

    m = sn.read_manifest(occ_dir)
    if m is None:
        raise CardError(f"no snapshot manifest under {Path(occ_dir) / sn.SNAP_DIR}")
    ent = (m.get("files") or {}).get("continuity") or {}
    prior = ""
    if ent.get("sha256") and ent.get("name"):
        prior = (Path(occ_dir) / sn.SNAP_DIR / ent["name"]).read_text(encoding="utf-8")
    live = sn.text_tier(db_path)["continuity"]
    return prior, live.read_text(encoding="utf-8") if live.is_file() else ""


def from_spec(spec: Mapping[str, Any], *, occ_dir: Path | str, db_path: Path | str | None = None,
              notes: Mapping[str, Any] | None = None, source: Mapping[str, str] | None = None,
              day: str | None = None, manual: bool = False, slot: str | None = None,
              agent_id: str = "dream") -> Card:
    """A card from the spec recovery (``RecoverResult.card``) or a rollback (``RollbackResult.card``) returned, plus
    the occurrence's own files: the snapshot's prior text, the live saved text (when ``db_path`` is given) and
    ``usage.json``. The spec's ``his`` lines (``unknown-commit``) are his."""
    occ = Path(occ_dir)
    kind = str(spec.get("kind"))
    prior = saved = ""
    if kind == "consolidated" and db_path is not None:
        prior, saved = texts_for(db_path, occ)
    reason = str(spec.get("reason") or "")
    if kind == "rolled-back":
        reason = f"rolled back by {spec.get('who', '?')} at {spec.get('at', '?')}; restored: " \
                 f"{', '.join(spec.get('files') or []) or 'nothing'}. {spec.get('limit', '')}".strip()
    return build_card(kind, occurrence=str(spec.get("occurrence") or occ.name), agent_id=agent_id, day=day,
                      reason=reason, manual=manual, slot=slot, prior=prior, saved=saved, notes=notes, source=source,
                      his_extra=list(spec.get("his") or []), usage_doc=_usage.read_doc(occ),
                      remedy=spec.get("remedy"), out_dir=occ)


def file_card(store: Any, card: Card, *, feed: str = FEED, thread_id: str | None = None) -> tuple[Any, bool]:
    """File ``card`` as a review item, once per ``dedupe_key`` (``(item, new)``). ``authority_relevant`` is the card's:
    a card with a non-empty ``his`` cannot be cleared in ``coordinator`` mode. Whether the ``agent`` feed exists is
    ``review.FEEDS``'s to say (P8 adds it); a store that lacks it refuses here, and nothing is filed."""
    from levain.coordinator import review

    return review.file_item(store, feed, card.body, dedupe_key=card.dedupe_key, thread_id=thread_id,
                            authority_relevant=card.authority_relevant)
