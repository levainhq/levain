"""levain.agents.refuse — what `register` refuses (DESIGN §4.1 rules 1-6 and refinements a-e; A0 r6 §2.3, §3.2, §3.6).

Each refusal is a :class:`Refusal` (the rule it belongs to and a message naming the field). Parsing (``spec.parse_spec``)
is rule 3's first half: an unknown field or a wrong type raises there. Here:

1. no ``input``; no ``output`` reader and moment; no ``win`` (the ruled triple), with the refinements that bend it:
   (a) a ``corpus`` output names the query path as its reader and "when asked" as its moment, so it needs no push
   moment; (b) a ``win`` of kind ``inherited`` is allowed only to a member of a feeder group (``feeds`` non-empty), and
   the group still needs a real win at the end of every chain: a stage whose downstream has no win is refused with it;
   (c) a ``recovery`` output comes with a ``drill_cadence`` and a ``verified-restore`` win.
2. an expiry/review date at signing: it lives on the authority link (``expires_at``), so it is NOT evaluated here.
3. a role that does not resolve (the first half is the parse).
4. a name held by a live (not retired) agent. A spawned instance holds a name in ``@spawn/``; F0 has no table for
   instances, so that half of the rule has nothing to read and is not enforced here.
5. an empty ceiling, or one that says "all" (the role's ceiling), in ``lands``, ``pushes``, ``publishes`` or
   ``where``: a blank entry, a word for all (all, everything, any, anything), or an entry that is nothing but
   wildcard and root characters once runs of ``*`` are collapsed and trailing slashes and dots stripped (``*``,
   ``**/*``, ``*.*``, ``/``, ``.``, ``~/``), optionally after a home or drive root (``$HOME``, ``%USERPROFILE%``,
   ``C:\\``): see :func:`all_spelling`.
6. a schedule with no zone while the install has no default zone (the message names ``levain agents zone``); a feeder
   cycle.
(d) review is a DERIVED flag: required when the role calls a model or acts outward (``pushes`` or ``publishes`` of its
   ceiling non-empty, A0 §3.2). No ``review_by`` exists; the date is the link's ``expires_at`` (:func:`derived_flags`).
(e) the one default zone per install fills a zone-less schedule (``spec.finalize_spec``).
``stop_at`` (A0 §2.3, §5.10): a spec with no ``stop_at`` is refused, naming it.

:func:`audit_verdicts` reads an R1-AUDIT file (a deployment's amended audit, one row per job). A row carries a class, a
refinement, the review flags and the audit's own verdict; those rows hold no AgentSpec (no input, output or win text to
re-judge), so the verdict is the audit's, and what this module adds is the checks that the row is coherent: the class and
refinement agree, the review flag is the derived flag, and a refused row has a reason.
"""
from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["Refusal", "check_spec", "refinement_of", "derived_flags", "ceiling_problem", "audit_verdicts",
           "AuditError", "ZONE_VERB", "all_spelling"]

ZONE_VERB = "levain agents zone <IANA>"
# rule 5: the words for "all", and the characters an entry may be made of and still name nothing in particular
_ALL_WORDS = frozenset({"all", "everything", "any", "anything"})
_ROOT_CHARS = frozenset("*?/\\.~")
# a root that names a whole tree: a home or system env var in any of the shells' spellings, or a drive letter
_ROOT_VARS = "home|userprofile|homepath|homedrive|systemdrive|systemroot"
_ROOT_PREFIX = re.compile(rf"\$(?:{_ROOT_VARS})\b|\$\{{(?:{_ROOT_VARS})\}}|%(?:{_ROOT_VARS})%|[a-z]:")


@dataclass(frozen=True)
class Refusal:
    rule: str
    message: str

    def __str__(self) -> str:
        return f"rule {self.rule}: {self.message}"


def refinement_of(spec: Mapping[str, Any]) -> str | None:
    """The DESIGN §4.1 refinement a spec leans on: a corpus output (a), an inherited win (b), a recovery output (c)."""
    out, win = spec.get("output") or {}, spec.get("win") or {}
    if win.get("kind") == "inherited":
        return "b"
    if out.get("class") == "recovery":
        return "c"
    if out.get("class") == "corpus":
        return "a"
    return None


def derived_flags(role: Mapping[str, Any]) -> dict[str, bool]:
    """Rule (d), A0 §3.2: ``acts_outward`` = the ceiling's ``pushes`` or ``publishes`` non-empty; ``calls_model`` =
    the role names a model, or its executor is ``dream`` (the dream composes with a model, D1 §1.3, even when the
    role leaves ``model`` empty); review is required when either is true. Nothing is stored."""
    ceiling = role.get("ceiling") or {}
    outward = bool(ceiling.get("pushes") or ceiling.get("publishes"))
    model = bool(role.get("model")) or role.get("executor") == "dream"
    return {"calls_model": model, "acts_outward": outward, "review_required": model or outward}


def all_spelling(value: Any) -> bool:
    """True when one non-blank ceiling entry says "all": a word for it, or only wildcard and root characters after
    collapsing runs of ``*`` and stripping trailing slashes and dots, optionally behind a home or drive root."""
    if not str(value).strip():
        return False   # a blank entry is its own refusal (see _says_all), not a spelling of "all"
    text = re.sub(r"\*+", "*", str(value).strip().casefold()).rstrip("/\\.")
    if text in _ALL_WORDS:
        return True
    root = _ROOT_PREFIX.match(text)
    return all(c in _ROOT_CHARS for c in (text[root.end():] if root else text))


def _says_all(value: Any) -> str | None:
    """Why one ceiling entry is refused: blank (it names nothing, and reads as everything) or a spelling of 'all'."""
    text = str(value).strip()
    if not text:
        return "has an empty entry"
    if text.casefold() == "all":
        return "says 'all'"
    if all_spelling(text):
        return f"says {text[:60]!r}, a spelling of 'all'"
    return None


def ceiling_problem(ceiling: Mapping[str, Any] | None) -> str | None:
    """Rule 5: None when the ceiling names something and does not say "all". A blank entry is not something named:
    a ceiling of blank entries only is empty, and a blank entry beside real ones is refused like 'all'."""
    ceiling = ceiling or {}
    named = any(str(v).strip() for k in ("lands", "pushes", "publishes") for v in ceiling.get(k) or [])
    named = named or any(str(w.get("repo", "")).strip() for w in ceiling.get("where") or [])
    if not named:
        return "the role's ceiling is empty"
    for key in ("lands", "pushes", "publishes"):
        for i, v in enumerate(ceiling.get(key) or []):
            why = _says_all(v)
            if why:
                return f"the role's ceiling.{key}[{i}] {why}"
    for i, w in enumerate(ceiling.get("where") or []):
        why = _says_all(w.get("repo", ""))
        if why:
            return f"the role's ceiling.where[{i}].repo {why}"
        for j, p in enumerate(w.get("paths") or []):
            why = _says_all(p)
            if why:
                return f"the role's ceiling.where[{i}].paths[{j}] {why}"
    return None


def _win_problem(spec: Mapping[str, Any]) -> str | None:
    win = spec.get("win")
    if not win:
        return ('no win: the spec has no `win` field; add {"kind": "verifier" or "human", "check": what is '
                'checked} (a feeder-group stage may declare "inherited")')
    if win["kind"] != "inherited" and not win["check"].strip():
        return "win.check is empty (a win is a typed verifier or a named human verdict surface)"
    return None


_END = object()


def _chain_problem(name: str, spec: Mapping[str, Any], lookup: Mapping[str, Mapping[str, Any]]) -> str | None:
    """Refinement (b): None when every downstream of an inherited win ends in a real win. Depth-first with an
    explicit stack and a visited set: no recursion limit on a long chain, and a stage reached by two paths is judged
    once (a cycle is rule 6's finding, not a win finding)."""
    visited = {name}
    stack = [iter(spec.get("feeds") or [])]
    while stack:
        down = next(stack[-1], _END)
        if down is _END:
            stack.pop()
            continue
        other = lookup.get(down)
        if other is None:
            return f"inherited win ends at {down}, which is not registered (register the downstream first)"
        if down in visited:
            continue
        visited.add(down)
        if _win_problem(other):
            return f"inherited win ends at {down}, which has no win"
        if other["win"]["kind"] == "inherited":
            if not other.get("feeds"):
                return (f"inherited win ends at {down}, whose own win is inherited and which feeds nothing: a chain "
                        f"must end in an agent whose own win is real, not inherited")
            stack.append(iter(other["feeds"]))
    return None


def _cycle(name: str, lookup: Mapping[str, Mapping[str, Any]]) -> list[str] | None:
    """The shortest feeder path from ``name`` back to itself, or None. Breadth-first with a visited set: each agent
    is expanded once, so neither a long chain nor a dense group costs more than one pass."""
    parent: dict[str, str | None] = {name: None}
    todo = deque([name])
    while todo:
        node = todo.popleft()
        for nxt in (lookup.get(node) or {}).get("feeds") or []:
            if nxt == name:
                path = [nxt]
                back: str | None = node
                while back is not None:
                    path.append(back)
                    back = parent[back]
                return path[::-1]
            if nxt not in parent:
                parent[nxt] = node
                todo.append(nxt)
    return None


def check_spec(spec: Mapping[str, Any], *, role: Mapping[str, Any] | None, default_zone: str | None,
               names_held: frozenset[str] | set[str] = frozenset(),
               lookup: Mapping[str, Mapping[str, Any]] | None = None) -> list[Refusal]:
    """Every refusal of a parsed spec (``spec.parse_spec``), in rule order; empty when it would be accepted.

    ``role`` is the resolved RoleSpec mapping (None: it did not resolve). ``names_held`` is the set of names of live
    agents that this registration may not take (the caller removes the spec's own name for a revision). ``lookup`` is
    name -> parsed spec for the feeder group's other members (the spec itself is added)."""
    out: list[Refusal] = []
    name = spec["name"]
    group = {**(lookup or {}), name: spec}
    inp, outp, win = spec.get("input"), spec.get("output"), spec.get("win")

    # rule 1: the triple
    if not inp or not inp["source"].strip() or not inp["liveness"].strip():
        out.append(Refusal("1", "no input: input.source and input.liveness (the liveness check) are both required"))
    if not outp:
        out.append(Refusal("1", "no output: output needs a destination, a reader and a moment"))
    else:
        cls = outp["class"]
        if not outp["destination"].strip():
            out.append(Refusal("1", "output.destination is empty (where it lands)"))
        if not outp["reader"].strip():
            out.append(Refusal("1", "no output reader: output.reader is empty"
                               + (" (a corpus output names its query path, refinement a)" if cls == "corpus" else "")))
        if cls != "corpus" and not (outp["moment"] or "").strip():
            out.append(Refusal("1", "no output moment: output.moment is empty (only a corpus output may leave it "
                                    "out; its moment is 'when asked', refinement a)"))
        if cls == "recovery" and outp["drill_cadence"] is None:
            out.append(Refusal("1", "a recovery output needs output.drill_cadence (refinement c)"))
    problem = _win_problem(spec)
    if problem:
        out.append(Refusal("1", problem))
    elif win["kind"] == "inherited":
        if not spec.get("feeds"):
            out.append(Refusal("1", "win.kind 'inherited' is for a member of a feeder group: feeds is empty "
                                    "(refinement b)"))
        else:
            chain = _chain_problem(name, spec, group)
            if chain:
                out.append(Refusal("1", chain))
    if outp and outp["class"] == "recovery" and win and win["kind"] != "verified-restore":
        out.append(Refusal("1", "a recovery output's win is a verified restore: win.kind must be 'verified-restore' "
                                "(refinement c)"))
    if win and win["kind"] == "verified-restore" and not (outp and outp["class"] == "recovery"):
        out.append(Refusal("1", "win.kind 'verified-restore' belongs to a recovery output (refinement c)"))

    # stop_at (A0 §2.3, §5.10)
    if spec.get("stop_at") is None:
        out.append(Refusal("stop_at", "no stop_at: integer seconds after launch are required (the supervisor's "
                                      "deadline is launched_at + stop_at)"))

    # rule 4
    if name in names_held:
        out.append(Refusal("4", f"name {name!r} is held by an agent that is not retired: retire it "
                                f"(`levain agents retire {name}`), or pass --revise to write its next version"))

    # rule 3 (resolution) and rule 5
    pinned = spec["role"]
    if role is None:
        out.append(Refusal("3", f"role {pinned['name']!r} ({pinned['role_hash'][:12]}) is not registered: register "
                                f"it with `levain agents role add <role file>` and pin the role_hash it prints"))
    else:
        if role.get("name") != pinned["name"]:
            out.append(Refusal("3", f"role.name {pinned['name']!r} does not match the role its role_hash "
                                    f"{pinned['role_hash'][:12]} pins, which is {role.get('name')!r}: name that role, "
                                    f"or pin the hash `levain agents role show {pinned['name']}` prints"))
        bad = ceiling_problem(role.get("ceiling"))
        if bad:
            out.append(Refusal("5", bad))

    # rule 6 (and refinement e)
    if spec["trigger"]["zone"] is None and not default_zone:
        out.append(Refusal("6", "the schedule has no zone and the install has no default zone: set one with "
                                f"`{ZONE_VERB}`, or give trigger.zone"))
    cycle = _cycle(name, group)
    if cycle:
        out.append(Refusal("6", "feeder cycle: " + " -> ".join(cycle)))
    return out


class AuditError(ValueError):
    """A row of the audit file is incoherent."""


_ROW_FIELDS = ("id", "class", "refinement", "review", "verdict", "reasons")
_REVIEW_FLAGS = ("calls_model", "acts_outward", "required")


def _audit_row(i: int, row: Any) -> tuple[Any, ...]:
    """The fields of audit row ``i``, each of the shape the coherence checks read; any other shape is an AuditError
    naming the row (never a KeyError or TypeError out of a check)."""
    if not isinstance(row, dict):
        raise AuditError(f"audit row {i} is a {type(row).__name__}, not an object")
    missing = [k for k in _ROW_FIELDS if k not in row]
    if missing:
        raise AuditError(f"audit row {i} is missing {', '.join(missing)}: {str(row)[:80]}")
    jid, review, reasons = row["id"], row["review"], row["reasons"]
    if not isinstance(jid, str) or not jid:
        raise AuditError(f"audit row {i}: id {str(jid)[:40]!r} is not a non-empty string")
    if not isinstance(review, dict):
        raise AuditError(f"{jid} (audit row {i}): review is a {type(review).__name__}, not an object")
    for flag in _REVIEW_FLAGS:
        if not isinstance(review.get(flag), bool):
            raise AuditError(f"{jid} (audit row {i}): review.{flag} is {str(review.get(flag))[:20]!r}, not true or "
                             f"false")
    if not isinstance(reasons, list) or not all(
            isinstance(r, list) and len(r) == 2 and all(isinstance(x, str) for x in r) for r in reasons):
        raise AuditError(f"{jid} (audit row {i}): reasons is not a list of [rule, text] string pairs")
    return jid, row["class"], row["refinement"], review, row["verdict"], reasons


def audit_verdicts(doc: Any) -> dict[str, Any]:
    """The R1-AUDIT fixture's per-job verdicts: ``{"jobs": {id: {verdict, class, refinement, review_required,
    reason}}}``. The verdict is the audit's own (see the module docstring); incoherent rows raise AuditError."""
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), list):
        raise AuditError("not an audit file: no 'jobs' list")
    jobs: dict[str, Any] = {}
    for i, row in enumerate(doc["jobs"]):
        jid, cls, ref, review, verdict, reasons = _audit_row(i, row)
        if jid in jobs:
            raise AuditError(f"job {jid!r} appears twice")
        if cls not in ("push", "corpus", "recovery"):
            raise AuditError(f"{jid}: class {cls!r}")
        if ref not in (None, "a", "b", "c"):
            raise AuditError(f"{jid}: refinement {ref!r}")
        if (ref == "a" and cls != "corpus") or (ref == "c" and cls != "recovery"):
            raise AuditError(f"{jid}: refinement {ref} does not fit class {cls}")
        derived = bool(review["calls_model"] or review["acts_outward"])
        if review["required"] != derived:
            raise AuditError(f"{jid}: review.required {review['required']} is not the derived flag {derived}")
        if verdict not in ("pass", "refused") or (verdict == "refused") != bool(reasons):
            raise AuditError(f"{jid}: verdict {verdict!r} with {len(reasons)} reason(s)")
        jobs[jid] = {"verdict": verdict, "class": cls, "refinement": ref, "review_required": review["required"],
                     "reason": "; ".join(f"{c}: {t}" for c, t in reasons)}
    return {"jobs": jobs}
