"""Tests for the Levain activation-hook helpers — the prospective-layer
germination matchers: event-based content collision + time-based due/dormant."""

from __future__ import annotations

import sys
from pathlib import Path

_HOOKS = (
    Path(__file__).resolve().parents[1]
    / "levain" / "templates" / "activation" / "hooks"
)
sys.path.insert(0, str(_HOOKS))

import _levain_hook as hook  # noqa: E402


def _spore(**kw):
    base = {
        "id": "spore-001", "type": "task", "text": "",
        "germination": "growing", "next": None,
    }
    base.update(kw)
    return base


class TestTokens:
    def test_drops_stopwords_and_short_tokens(self):
        # "the"/"are" are stopwords; "ok" is < 3 chars — only api/keys survive.
        assert hook._tokens("the API keys are ok") == {"api", "keys"}

    def test_lowercases(self):
        assert "restrict" in hook._tokens("Restrict the Keys")


class TestSporesColliding:
    def test_collides_on_two_shared_tokens(self):
        s = _spore(text="restrict the api keys", id="spore-007")
        hits = hook.spores_colliding("can we restrict the api keys now", [s])
        assert len(hits) == 1 and hits[0]["id"] == "spore-007"

    def test_below_threshold_no_match(self):
        # Only "keys" overlaps (1) — below the 2-token precision floor.
        s = _spore(text="restrict the api keys")
        assert hook.spores_colliding("where are my house keys", [s]) == []

    def test_empty_prompt(self):
        assert hook.spores_colliding("", [_spore(text="restrict api keys")]) == []

    def test_ranks_by_overlap_and_caps(self):
        spores = [
            _spore(id="a", text="restrict api keys vault"),     # 4 overlap
            _spore(id="b", text="restrict api"),                # 2 overlap
            _spore(id="c", text="rotate api keys vault token"), # 4 overlap
            _spore(id="d", text="unrelated grocery list"),      # 0
        ]
        hits = hook.spores_colliding(
            "restrict rotate the api keys vault", spores, limit=2
        )
        assert {h["id"] for h in hits} == {"a", "c"}  # the two highest-overlap

    def test_skips_non_str_text(self):
        assert hook.spores_colliding("api keys", [_spore(text=None)]) == []


class TestDueDormant:
    def test_only_dormant(self):
        spores = [
            _spore(id="a", germination="dormant"),
            _spore(id="b", germination="growing"),
            _spore(id="c", germination="resting"),
            _spore(id="d", germination="parked"),
            _spore(id="e", germination="dormant"),
        ]
        assert {s["id"] for s in hook.due_dormant_spores(spores)} == {"a", "e"}

    def test_caps(self):
        spores = [_spore(id=str(i), germination="dormant") for i in range(10)]
        assert len(hook.due_dormant_spores(spores, limit=3)) == 3


class TestFormatting:
    def test_collisions_format(self):
        s = _spore(text="restrict api keys", id="spore-007", type="task")
        out = hook.format_spore_collisions([s])
        assert "restrict api keys" in out and "spore-007" in out
        assert "relevant" in out.lower()

    def test_due_format_includes_next(self):
        s = _spore(
            text="schedule the panel", id="spore-009", type="task",
            next="2026-06-10",
        )
        out = hook.format_due_spores([s])
        assert "schedule the panel" in out and "spore-009" in out
        assert "2026-06-10" in out


class TestAnnealJsonRobustness:
    class _FakeResult:
        def __init__(self, stdout, rc=0):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = ""

    def test_timeout_aborts_without_retrying(self, monkeypatch):
        import subprocess
        calls = {"n": 0}

        def fake_run(cmd, **kw):
            calls["n"] += 1
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        monkeypatch.setattr(hook.subprocess, "run", fake_run)
        # A hang must abort, not re-invoke the same anneal per candidate (HIGH).
        assert hook._anneal_json(["status", "--json"], 2.0) is None
        assert calls["n"] == 1

    def test_validator_skips_wrong_shape_candidate(self, monkeypatch):
        results = iter([self._FakeResult('{"wrong": 1}'), self._FakeResult("[]")])
        monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: next(results))
        out = hook._anneal_json(
            ["spore", "list", "--json"], 2.0, validator=lambda d: isinstance(d, list)
        )
        assert out == []  # skipped the dict candidate, returned the list one


def test_tokens_capped_for_huge_input():
    # A pathological huge input must not blow up tokenization (MEDIUM).
    huge = ("api keys " * 100000)
    toks = hook._tokens(huge)
    assert "api" in toks and "keys" in toks  # still works, just bounded


def test_generic_work_tokens_dont_false_collide():
    # Two generic work tokens shared with an unrelated spore are NOT a match.
    s = _spore(text="review the test file for the parser")
    assert hook.spores_colliding("please review the test file i sent", [s]) == []
