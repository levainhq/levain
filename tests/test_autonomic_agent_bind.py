"""The AgentSpec-to-Binding compiler (levain.autonomic.agent_bind) and the dispatcher's refusal of its output."""
from __future__ import annotations

import hashlib

import pytest

from levain.autonomic import agent_bind as ab
from tests.test_autonomic_firepath import RecordingExecutor, _dispatcher, _store

ROLE_TEXT = '{"brief":{"slots":{},"template":"go"},"name":"scribe"}'
SPEC_HASH = hashlib.sha256(b"spec-v1").hexdigest()


def _kw(**over):
    kw = dict(agent_id="agent-1", role_hash=ab.role_text_hash(ROLE_TEXT), role_text=ROLE_TEXT, spec_hash=SPEC_HASH,
              capabilities={"lands": ["levain"]}, created_at="2026-11-02T00:00:00")
    kw.update(over)
    return kw


def test_compile_is_deterministic_and_sensitive():
    a, b = ab.compile_binding(**_kw()), ab.compile_binding(**_kw())
    assert a == b and a.binding_id == b.binding_id == ab.seal(**_kw())
    assert a.seal_matches() and not a.status.is_active
    changed = ab.compile_binding(**_kw(capabilities={"lands": ["levaim"]}))
    assert changed.binding_id != a.binding_id
    assert ab.compile_binding(**_kw(spec_hash=hashlib.sha256(b"spec-v2").hexdigest())).binding_id != a.binding_id


def test_role_text_not_hashing_to_role_hash_is_refused():
    with pytest.raises(ab.AgentBindError, match="role_hash"):
        ab.compile_binding(**_kw(role_text=ROLE_TEXT + " "))
    with pytest.raises(ab.AgentBindError, match="role_hash"):
        ab.seal(**_kw(role_hash=hashlib.sha256(b"other").hexdigest()))


def test_fire_dispatcher_refuses_a_named_binding(tmp_path):
    ex = RecordingExecutor()
    st = _store(tmp_path)
    named = ab.compile_binding(**_kw())
    assert ab.is_agent_binding(named)
    fd = _dispatcher(tmp_path, st, executor=ex)
    assert fd._dispatch_one(named, {"type": ab.AGENT_TRIGGER_TYPE, "id": "e1", "fields": {}}) is None
    assert ex.calls == []


def test_the_leaf_test_file_is_unchanged_by_this_slice():
    # The leaf boundary itself is tests/test_autonomic_leaf.py, run unmodified beside this file; here the module
    # is asserted to import nothing of levain outside the autonomic package.
    import ast
    from pathlib import Path
    tree = ast.parse(Path(ab.__file__).read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            assert n.level == 0 or n.module, "a bare relative import hides its target"
            mods.add(("." * n.level) + (n.module or ""))
    allowed_plain = {"__future__", "typing", "hashlib", "json"}
    assert all(m in allowed_plain or m.startswith("levain.autonomic") for m in mods), sorted(mods)
