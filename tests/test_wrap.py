"""levain.wrap — `levain wrap <entity>`: the human-gated consolidate for a sovereign entity.

Two tiers, both runnable WITHOUT the openhands extra:
  - PURE helpers: `_extract_neocortex` / `_completion_text` (the compose-reply salvage + parse).
  - The full `wrap_entity` flow with the LLM beat (`_compose`) monkeypatched — so prepare_wrap and
    validated_save_continuity run FOR REAL against a real isolated anneal store, exercising the
    precondition guards, the empty/dry-run/reset paths, and the fail-CLOSED save contract. The live
    model compose is the L4-live gate, not a unit test.
"""
from __future__ import annotations

import json
from pathlib import Path

from anneal_memory import DEFAULT_SCHEMA, FLOW_SCHEMA, Store
from levain import wrap as wrapmod
from levain.wrap import _completion_text, _extract_neocortex, wrap_entity

# A minimal, structurally-valid FLOW-6 neocortex (all six headings, no citations → nothing to
# graduate). This is what a GOOD compose produces; the tests feed it via a monkeypatched `_compose`.
_VALID_NEOCORTEX = """\
## State
First consolidation of the test entity.

## Active Threads
- Getting my bearings — pointer: this session.

## Patterns
Nothing graduated yet.

## Decisions
Nothing committed yet.

## Context
The operator ran the first wrap to metabolize the opening episodes.

## Understanding
Early days — we are just beginning to work together.
"""


def _openhands_entity(tmp_path: Path, name: str = "ent") -> Path:
    """A dir that passes `require_openhands_entity`: `.levain/` + the openhands adapter marker."""
    d = tmp_path / name
    (d / ".levain").mkdir(parents=True)
    (d / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    return d


def _with_store(entity: Path, *, schema=FLOW_SCHEMA, episodes: int = 0) -> Path:
    """Create the entity's isolated anneal store on `schema`, optionally with `episodes` recorded."""
    db = entity / ".levain" / "memory.db"
    store = Store(str(db), section_schema=schema)
    for i in range(episodes):
        store.record(
            content=f"Turn {i}: I helped the operator with the wrap command and it worked.",
            episode_type="observation",
            source="test-session",
        )
    store.close()
    return db


# ---------- pure helpers (no anneal, no openhands) ----------

def test_extract_neocortex_passthrough():
    text = "## State\nx\n## Understanding\ny"
    assert _extract_neocortex(text) == text


def test_extract_neocortex_strips_wrapping_code_fence():
    fenced = "```markdown\n## State\nx\n## Understanding\ny\n```"
    assert _extract_neocortex(fenced) == "## State\nx\n## Understanding\ny"


def test_extract_neocortex_slices_preamble_before_state():
    noisy = "Sure! Here is your memory:\n\n## State\nx\n## Understanding\ny"
    assert _extract_neocortex(noisy).startswith("## State")
    assert "Sure!" not in _extract_neocortex(noisy)


def test_extract_neocortex_does_not_slice_at_a_prefix_heading():
    # `## Statement` must NOT be mistaken for the `## State` heading (whole-line match).
    noisy = "## Statement of intent\nblah\n\n## State\nx\n## Understanding\ny"
    out = _extract_neocortex(noisy)
    assert out.startswith("## State\n")
    assert "Statement of intent" not in out


def test_extract_neocortex_leaves_garbage_for_the_save_gate():
    # No `## State` → passed through unchanged; validate_structure (in the save) is the real gate.
    assert _extract_neocortex("I refuse to answer.") == "I refuse to answer."


def test_completion_text_from_content_list():
    class _C:
        def __init__(self, text):
            self.text = text

    class _M:
        content = [_C("## State"), _C("rest")]

    class _R:
        message = _M()

    assert _completion_text(_R()) == "## State\nrest"


def test_completion_text_bare_string_and_missing_message():
    class _M:
        content = "plain"

    class _R1:
        message = _M()

    class _R2:
        message = None

    assert _completion_text(_R1()) == "plain"
    assert _completion_text(_R2()) == ""


# ---------- precondition guards (no model needed) ----------

def test_wrap_refuses_a_non_entity(tmp_path, capsys):
    assert wrap_entity(tmp_path / "nope") == 2
    assert "not an initialized Levain entity" in capsys.readouterr().out


def test_wrap_refuses_a_non_openhands_entity(tmp_path, capsys):
    d = tmp_path / "cc"
    (d / ".levain").mkdir(parents=True)  # a store but no openhands marker
    assert wrap_entity(d) == 2
    assert "not a clean OpenHands entity" in capsys.readouterr().out


def test_wrap_refuses_a_missing_store(tmp_path, capsys):
    ent = _openhands_entity(tmp_path)  # marker but no memory.db yet
    assert wrap_entity(ent) == 2
    assert "no memory store yet" in capsys.readouterr().out


def test_wrap_refuses_the_ops_schema(tmp_path, capsys):
    ent = _openhands_entity(tmp_path)
    _with_store(ent, schema=DEFAULT_SCHEMA, episodes=1)
    assert wrap_entity(ent) == 2
    assert "6-section partnership schema" in capsys.readouterr().out


def test_wrap_empty_store_is_a_clean_noop(tmp_path, capsys):
    ent = _openhands_entity(tmp_path)
    _with_store(ent, episodes=0)
    assert wrap_entity(ent) == 0
    assert "nothing to consolidate" in capsys.readouterr().out


# ---------- dry-run + reset (no model needed) ----------

def test_wrap_dry_run_shows_package_and_changes_nothing(tmp_path, capsys):
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=2)
    assert wrap_entity(ent, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    # The prepared wrap is cancelled → the store is NOT left in progress, and no memory was written.
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None
    assert not (ent / ".levain" / "memory.continuity.md").exists()


def test_wrap_isolation_refusal_is_wired_in(tmp_path, capsys, monkeypatch):
    # The #1 invariant, at the WRAP level: if the isolation guard raises, wrap REFUSES (exit 2) and
    # never opens the store. Proves the guard is wired into this command, not merely that it exists.
    from levain.firing.isolation import IsolationError

    ent = _openhands_entity(tmp_path)
    _with_store(ent, episodes=1)

    def _refuse(*_a, **_k):
        raise IsolationError("simulated store escape into the flow store")

    monkeypatch.setattr(wrapmod, "assert_entity_isolated", _refuse)
    assert wrap_entity(ent) == 2
    assert "sovereignty guard REFUSED" in capsys.readouterr().out


def test_wrap_refuses_a_wrap_already_in_progress_without_reset(tmp_path, capsys):
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=1)
    with Store(str(db), section_schema=None) as store:
        store.wrap_started(token="orphan", episode_ids=[])  # simulate a crashed prior consolidate
    # A prior stranded wrap is a PRECONDITION error (nothing started this invocation) → exit 2.
    assert wrap_entity(ent) == 2
    assert "still in progress" in capsys.readouterr().out


def test_wrap_reset_discards_the_orphan_then_proceeds(tmp_path, capsys):
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=1)
    with Store(str(db), section_schema=None) as store:
        store.wrap_started(token="orphan", episode_ids=[])
    # --reset clears the orphan, then (dry-run) prepares + cancels cleanly.
    assert wrap_entity(ent, reset=True, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "discarded an unfinished prior wrap" in out
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None


# ---------- the compose beat, monkeypatched (prepare + save run FOR REAL) ----------

def test_wrap_success_writes_the_neocortex_into_the_entity_tree(tmp_path, capsys, monkeypatch):
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=2)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    assert wrap_entity(ent) == 0
    out = capsys.readouterr().out
    assert "consolidated." in out
    assert "identity compounded" in out

    # The memory landed in the ENTITY's own tree (isolation, visible), and the wrap completed.
    continuity = ent / ".levain" / "memory.continuity.md"
    assert continuity.exists()
    assert "## Understanding" in continuity.read_text(encoding="utf-8")
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None  # completed, not left hanging


def test_wrap_fail_closed_refuses_a_malformed_compose(tmp_path, capsys, monkeypatch):
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=2)
    # A weak model drops required sections → validated_save_continuity RAISES.
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: "## State\nonly one section")

    assert wrap_entity(ent) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "NOT saved" in out

    # FAIL-CLOSED: no memory written (identity unchanged), the wrap self-cleaned (no orphan), and the
    # rejected draft was preserved for inspection inside the entity tree.
    assert not (ent / ".levain" / "memory.continuity.md").exists()
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None
    assert list((ent / ".levain").glob("rejected-wrap.*.md"))


def test_wrap_empty_compose_leaves_no_orphan(tmp_path, capsys, monkeypatch):
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=1)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: "   ")

    assert wrap_entity(ent) == 1
    assert "no usable memory text" in capsys.readouterr().out
    assert not (ent / ".levain" / "memory.continuity.md").exists()
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None


def test_wrap_refuses_when_the_single_writer_lock_is_held(tmp_path, capsys):
    # Single-writer: if another consolidate holds <entity>/.levain/wrap.lock, wrap REFUSES (exit 2)
    # rather than racing the wrap state. flock treats separate fds independently even in one process,
    # so holding the lock here deterministically blocks wrap_entity's own acquire.
    import fcntl
    import os

    ent = _openhands_entity(tmp_path)
    _with_store(ent, episodes=1)
    lock_path = ent / ".levain" / "wrap.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert wrap_entity(ent) == 2
        assert "another consolidate is already running" in capsys.readouterr().out
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_wrap_post_commit_externalization_failure_is_not_reported_as_unsaved(
    tmp_path, capsys, monkeypatch
):
    # codex's catch: a save that COMMITTED the DB then failed to rename the sidecar must NOT be
    # reported as "not saved / re-run" — the episodes are already consolidated. We distinguish by
    # store STATE (no wrap in progress = committed), so a post-commit failure hits the right branch.
    import anneal_memory.continuity as cont
    from anneal_memory import StoreError

    ent = _openhands_entity(tmp_path)
    _with_store(ent, episodes=2)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    def _post_commit_fail(store, text, **kw):
        # Simulate anneal's Phase-2 commit (which clears the in-progress metadata) then a Phase-3
        # rename failure raising post-commit.
        store.wrap_cancelled()  # observable proxy for "wrap_completed cleared the in-progress flag"
        raise StoreError(
            "Failed to rename continuity tmp — the DB has committed the wrap but externalization "
            "is incomplete; preserved at /x/memory.continuity.md.tmp."
        )

    # Patch at the anneal module (wrap_entity imports it lazily at call time, so this binds).
    monkeypatch.setattr(cont, "validated_save_continuity", _post_commit_fail)

    assert wrap_entity(ent) == 1
    out = capsys.readouterr().out
    assert "COMMITTED but writing" in out
    assert "Do NOT re-run" in out


def test_wrap_compose_crash_leaves_no_orphan(tmp_path, capsys, monkeypatch):
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=1)

    def _boom(*a, **k):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(wrapmod, "_compose", _boom)

    assert wrap_entity(ent) == 1
    assert "compose model failed" in capsys.readouterr().out
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None  # self-cleaned; episodes safe for next wrap
