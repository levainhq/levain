"""Bytecode branch coverage of one function and its nested code objects, with ``sys.monitoring`` (stdlib, 3.12+).

A branch is a conditional jump or a ``FOR_ITER``; it is covered when both of its outcomes were observed. This is
stricter than line-arc coverage (every ``and`` / ``or`` operand and every generator-expression test is a branch),
and needs no third-party tool, so CI measures it with the test suite (S1_DESIGN F9 rev 10 N1).
"""
from __future__ import annotations

import dis
import sys
import types
from contextlib import contextmanager

_BRANCH_OPS = ("POP_JUMP_IF", "FOR_ITER")


def code_objects(code: types.CodeType) -> list[types.CodeType]:
    out, stack = [], [code]
    while stack:
        c = stack.pop()
        out.append(c)
        stack.extend(k for k in c.co_consts if isinstance(k, types.CodeType))
    return out


def branch_sites(codes: list[types.CodeType]) -> set[tuple[types.CodeType, int]]:
    """Every conditional jump and FOR_ITER, except the jump of an ``except T:`` type match (CHECK_EXC_MATCH): its
    other outcome is an exception the clause does not name, which is the interpreter re-raising, not a branch of
    the function's logic (coverage.py does not count it either)."""
    out = set()
    for c in codes:
        prev = None
        for ins in dis.get_instructions(c):
            if ins.opname.startswith(_BRANCH_OPS) and not (prev is not None and prev.opname == "CHECK_EXC_MATCH"):
                out.add((c, ins.offset))
            prev = ins
    return out


class Recorder:
    def __init__(self, func):
        self.codes = code_objects(func.__code__)
        self.sites = branch_sites(self.codes)
        self.seen: dict[tuple[types.CodeType, int], set[int]] = {}

    def missing(self) -> list[str]:
        out = []
        for c, off in sorted(self.sites, key=lambda s: (s[0].co_firstlineno, s[1])):
            n = len(self.seen.get((c, off), ()))
            if n < 2:
                line = next((i.positions.lineno for i in dis.get_instructions(c) if i.offset == off), None)
                out.append(f"{c.co_name} line {line} offset {off}: {n}/2 outcomes")
        return out


@contextmanager
def record(func):
    """Yield a ``Recorder``; every call of ``func`` inside the block is measured."""
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    rec = Recorder(func)
    mon.use_tool_id(tool, "levain-branch-coverage")
    try:
        def on_branch(code, offset, dest):
            if (code, offset) in rec.sites:
                rec.seen.setdefault((code, offset), set()).add(dest)
        mon.register_callback(tool, mon.events.BRANCH, on_branch)
        for c in rec.codes:
            mon.set_local_events(tool, c, mon.events.BRANCH)
        yield rec
    finally:
        for c in rec.codes:
            mon.set_local_events(tool, c, 0)
        mon.register_callback(tool, mon.events.BRANCH, None)
        mon.free_tool_id(tool)


SUPPORTED = sys.version_info[:2] in ((3, 12), (3, 13))
