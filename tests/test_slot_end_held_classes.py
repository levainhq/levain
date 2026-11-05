"""1105-17 heldbuild: every refusal name END can raise is CLASSED (permanent / transient / budget / not an attempt),
and the quarantine rule that classification feeds. The scan reads end.py and root_intent.py with ``ast``, so a refusal
added without a class fails here, not in the field."""
from __future__ import annotations

import ast
import inspect
import re
from types import SimpleNamespace

import pytest

from levain.slot import Refused
from levain.slot import end as E
from levain.slot import root_intent as RI

#: The non-literal first arguments of ``Refused(...)`` in end.py, and the names each can take. A NEW non-literal raise
#: fails ``test_the_non_literal_refusals_are_the_known_ones`` until it is listed here and classed.
DYNAMIC = {
    "cap_name": {"manifest-byte-cap", "meta-byte-cap"},          # _stream_jsonl's callers pass these two
    "occupied": {"replaced-occupied", "unconfirmed-occupied"},  # _set_aside's callers pass these two
    "f'{what}-unproven'": {"freeze-unproven", "kill-unproven"},  # _prove's callers pass "freeze" and "kill"
    "f'no-credential:{facts.slot.tag}'": set(),                  # matched by prefix
}
#: Raised on the way to END (facts from alloc, the journal's path), never as a HELD's reason.
BEFORE_END = {"bad-token"}


def _refusals(module) -> tuple[set[str], set[str]]:
    literals, dynamic = set(), set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Refused" and node.args:
            a = node.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                literals.add(a.value)
            else:
                dynamic.add(ast.unparse(a))
    return literals, dynamic


def _classed(name: str) -> bool:
    return (name in E.BUDGET_REFUSALS or name in E.PERMANENT_HELD or name in E.TRANSIENT_HELD
            or name in E.NOT_AN_ATTEMPT or name.startswith(E.PERMANENT_HELD_PREFIXES))


def test_the_classes_do_not_overlap():
    sets = [E.BUDGET_REFUSALS, E.PERMANENT_HELD, E.TRANSIENT_HELD, E.NOT_AN_ATTEMPT]
    for i, a in enumerate(sets):
        for b in sets[i + 1:]:
            assert not a & b, a & b
    assert not any(n.startswith(E.PERMANENT_HELD_PREFIXES) for s in sets for n in s)


def test_every_refusal_end_can_raise_is_classed():
    literals, _ = _refusals(E)
    from_journal, _ = _refusals(RI)
    named = literals | from_journal | {n for names in DYNAMIC.values() for n in names}
    missing = sorted(n for n in named if not _classed(n) and n not in BEFORE_END)
    assert not missing, f"unclassed refusal names (add each to one set in levain/slot/end.py): {missing}"
    assert "no-credential:egress0".startswith(E.PERMANENT_HELD_PREFIXES)


def test_the_non_literal_refusals_are_the_known_ones():
    _, dynamic = _refusals(E)
    assert dynamic == set(DYNAMIC), dynamic ^ set(DYNAMIC)
    # the names behind cap_name / occupied really are the ones passed to those helpers
    src = inspect.getsource(E)
    for name in DYNAMIC["cap_name"] | DYNAMIC["occupied"]:
        assert f'"{name}"' in src
    assert set(re.findall(r'_prove\([^\n]*"(\w+)", seams', src)) <= {"freeze", "kill"}


def test_a_budget_refusal_is_quarantined_after_one_try_a_permanent_one_after_three():
    assert E.QUARANTINE_AFTER_BUDGET == 1 and E.QUARANTINE_AFTER == 3
    for name in sorted(E.BUDGET_REFUSALS):
        assert E.quarantined({"held_reason": name, "held_attempts": 1}), name
    assert not E.quarantined({"held_reason": "var-folders-residue", "held_attempts": 2})
    assert E.quarantined({"held_reason": "var-folders-residue", "held_attempts": 3})
    assert E.quarantined({"held_reason": "no-credential:egress0", "held_attempts": 3})
    assert E.quarantined({"held_reason": "var-folders-special", "held_attempts": 40})


@pytest.mark.parametrize("fields", [
    {"held_reason": "link-reader-failed", "held_attempts": 99},       # transient
    {"held_reason": "kill-unproven", "held_attempts": 99},            # transient by the head's ruling
    {"held_reason": "bad-root", "held_attempts": 99},                 # transient by the head's ruling
    {"held_reason": "a-name-nobody-classed", "held_attempts": 99},    # unknown reads as transient
    {"held_attempts": 99},                                            # unclassified (an older record)
    {"held_reason": "var-folders-residue"},                           # no tries recorded
    {"held_reason": "var-folders-residue", "held_attempts": True},    # a bool is not a count
    {"held_reason": "var-folders-residue", "held_attempts": "9"},
    {"held_reason": None, "held_attempts": 99},
    {},
])
def test_these_are_never_quarantined(fields):
    assert not E.quarantined(fields)


def test_held_reason_of_names_the_refusal_a_failed_step_carries():
    assert E.held_reason_of(Refused("var-folders-residue", "x")) == "var-folders-residue"
    assert E.held_reason_of(RI.PhaseFailed("tree", Refused("link-reader-failed"))) == "link-reader-failed"
    assert E.held_reason_of(Refused("no-credential:egress0")) == "no-credential:egress0"
    assert E.held_reason_of(RI.PhaseFailed("tree", OSError(5, "Input/output error"))) is None
    assert E.held_reason_of(RuntimeError("x")) is None
    assert E.held_reason_of(Refused("not a record-safe name/../x")) is None


def _seams(refuse):
    sent = []

    def cas(target, fields):
        sent.append((target, dict(fields)))
        if refuse and "held_reason" in fields:
            raise Refused("field-not-casable", "CAS")
    return SimpleNamespace(clock=lambda: 0.0, cas=cas, sleep=lambda s: None), sent


def test_a_control_that_predates_held_reason_still_gets_the_cas_without_it():
    """Mixed versions: an old CONTROL refuses the optional key ``field-not-casable``; END drops it and sends once
    more, so a session still ends (or is recorded HELD) instead of being refused forever."""
    seams, sent = _seams(refuse=True)
    E.send_cas(seams, E.HELD, {"failed_step": "tree", "held_attempts": 2, "held_reason": "var-folders-residue"})
    assert [t for t, _ in sent] == [E.HELD, E.HELD]
    assert "held_reason" not in sent[1][1] and sent[1][1]["held_attempts"] == 2
    seams, sent = _seams(refuse=True)
    E.send_cas(seams, E.RETAINED, {})                                  # the exit CAS names the field it clears
    assert sent[0][1] == {"failed_step": None, "held_attempts": None, "held_reason": None}
    assert "held_reason" not in sent[1][1]


def test_only_the_held_reason_key_is_ever_dropped():
    seams, sent = _seams(refuse=False)

    def cas(target, fields):
        sent.append((target, dict(fields)))
        raise Refused("field-not-casable", "CAS")
    seams.cas = cas
    with pytest.raises(E.CasNotSent):
        E.send_cas(seams, E.HELD, {"failed_step": "tree", "held_attempts": 1, "held_reason": "tree-changed"})
    assert len(sent) == 2                                              # one retry without held_reason, then refused

