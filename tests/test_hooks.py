"""Tests for the Levain activation-hook helpers — the prospective-layer
germination matchers: event-based content collision + time-based due/dormant."""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

_HOOKS = (
    Path(__file__).resolve().parents[1]
    / "levain" / "templates" / "activation" / "hooks"
)
sys.path.insert(0, str(_HOOKS))

import _levain_hook as hook  # noqa: E402

# The Codex adapter ships its OWN copy of _levain_hook.py (the installers copy
# different activation trees per adapter — shared for Claude Code, the codex
# subtree for Codex). Load it under a distinct module name so the parity tests
# can compare the two without import-cache collision.
_CODEX_HOOK_FILE = (
    Path(__file__).resolve().parents[1]
    / "levain" / "templates" / "adapters" / "codex" / "activation" / "hooks"
    / "_levain_hook.py"
)


def _load_codex_hook():
    spec = importlib.util.spec_from_file_location(
        "_levain_hook_codex", _CODEX_HOOK_FILE
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


codex_hook = _load_codex_hook()


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


class TestOpenSporesTrayExclusion:
    """Slice 3b: the stranger-side cognition-exclude. open_spores is the SINGLE chokepoint
    both germination surfaces (dormant + collision) read; operator-I/O dispositions (Tray
    inbox + Keep notes) must be filtered OUT so they can't leak into a Levain install's
    cognition."""

    def test_filters_operator_io_dispositions(self, monkeypatch):
        rows = [
            _spore(id="loop1"),                          # no disposition → a loop
            _spore(id="loop2", disposition="loop"),      # explicit loop
            _spore(id="seed1", disposition="seed"),
            _spore(id="handoff1", disposition="handoff"),
            _spore(id="agenda1", disposition="agenda"),
            _spore(id="note1", disposition="note"),      # Keep reference — also excluded
        ]
        monkeypatch.setattr(hook, "_anneal_json", lambda *a, **k: rows)
        assert [s["id"] for s in hook.open_spores()] == ["loop1", "loop2"]

    def test_unknown_disposition_fails_open_as_a_loop(self, monkeypatch):
        # a typo'd/unknown disposition is NOT silently dropped (the silent-loss direction)
        monkeypatch.setattr(hook, "_anneal_json", lambda *a, **k: [_spore(id="x", disposition="bogus")])
        assert [s["id"] for s in hook.open_spores()] == ["x"]

    def test_is_loop_predicate(self):
        assert hook._is_loop({"disposition": "loop"}) is True
        assert hook._is_loop({}) is True
        assert hook._is_loop({"disposition": "seed"}) is False
        assert hook._is_loop({"disposition": "note"}) is False  # Keep reference excluded
        assert hook._is_loop({"disposition": ""}) is True  # falsy → loop

    def test_hook_vocab_matches_canonical_levain_spores(self):
        # DRIFT GUARD: the standalone hook copy must equal the canonical taxonomy.
        import levain.spores as lv
        assert hook._NON_COGNITION_DISPOSITIONS == lv.NON_COGNITION_DISPOSITIONS


class TestCrystalRecall:
    """The on-demand graduated-wisdom surface: `crystal recall --json` shelled out
    via _anneal_json, fail-silent + bounded, the read-side twin of wrap-time
    crystallization routing."""

    def test_empty_prompt_returns_empty(self):
        assert hook.crystal_recall("   ") == []

    def test_parses_list_of_patterns(self, monkeypatch):
        rows = [
            {"name": "invisible_infrastructure_failure", "level": 3,
             "explanation": "parse the real signal", "activation": "warm"},
            {"name": "thinness_is_the_architecture", "level": 3,
             "explanation": "thin ports"},
        ]
        monkeypatch.setattr(hook, "_anneal_json", lambda *a, **k: rows)
        out = hook.crystal_recall("anything touching the apparatus")
        assert [p["name"] for p in out] == [
            "invisible_infrastructure_failure", "thinness_is_the_architecture"]

    def test_filters_non_dict_rows(self, monkeypatch):
        monkeypatch.setattr(
            hook, "_anneal_json",
            lambda *a, **k: [{"name": "ok"}, "garbage", 7, None])
        assert [p["name"] for p in hook.crystal_recall("x y")] == ["ok"]

    def test_none_from_anneal_is_empty(self, monkeypatch):
        # absent/too-old anneal, no crystal store → None → [] (fail-silent)
        monkeypatch.setattr(hook, "_anneal_json", lambda *a, **k: None)
        assert hook.crystal_recall("x y") == []

    def test_query_is_capped_and_terminator_guarded(self, monkeypatch):
        captured = {}

        def fake(sub_args, timeout, validator=None):
            captured["sub_args"] = sub_args
            return []

        monkeypatch.setattr(hook, "_anneal_json", fake)
        hook.crystal_recall("api " * 100000)
        # ["crystal", "recall", "--json", "--", <query>] — the `--` options
        # terminator (codex L3 LOW) keeps a flag-like prompt from being parsed as
        # an option; the query is the last argv element, bounded to the tokenizer cap.
        assert captured["sub_args"][:4] == ["crystal", "recall", "--json", "--"]
        assert len(captured["sub_args"][4]) == hook._MAX_TOKENIZE_CHARS

    def test_flag_like_prompt_goes_after_terminator(self, monkeypatch):
        # a prompt that itself looks like a flag must land AFTER `--`, never as an option
        captured = {}

        def fake(sub_args, timeout, validator=None):
            captured["sub_args"] = sub_args
            return []

        monkeypatch.setattr(hook, "_anneal_json", fake)
        hook.crystal_recall("--json --help -v")
        assert captured["sub_args"][3] == "--"
        assert captured["sub_args"][4] == "--json --help -v"


class TestFormatCrystalRecall:
    def test_renders_name_meta_and_explanation(self):
        out = hook.format_crystal_recall([
            {"name": "invisible_infrastructure_failure", "level": 3,
             "explanation": "parse the real signal, don't trust the surface",
             "activation": "warm"},
        ])
        assert "crystallized patterns" in out.lower()
        assert "invisible_infrastructure_failure" in out
        assert "(3x, warm)" in out
        assert "parse the real signal" in out
        assert "not new instruction" in out  # the guidance line

    def test_tolerates_missing_fields(self):
        # no level/activation/explanation — must not raise, still names the pattern
        out = hook.format_crystal_recall([{"name": "bare_pattern"}])
        assert "bare_pattern" in out
        assert "()" not in out  # no empty meta parens when level+activation absent

    def test_coerces_non_string_fields(self):
        # defensive str()-coercion against future return-shape drift
        out = hook.format_crystal_recall([{"name": None, "explanation": 42, "level": "x"}])
        assert "pattern" in out  # name=None → "pattern" fallback


# The substrate-neutral germination surface MUST stay byte-identical between the
# shared (Claude Code) hooks and the Codex adapter's copy. The two drifted once —
# Slice 2 wired germination into the shared copy only; the Codex copy was a wrap
# behind (the gap Slice 3 closed). This is the structural guard against that
# recurring until the v1.2 single-_levain_hook refactor collapses the two copies.
_PORTED_FNS = (
    "_anneal_json", "_is_int_episodes", "episodes_since_wrap",
    "open_spores", "_is_loop", "_tokens", "spores_colliding", "due_dormant_spores",
    "_format_spore_lines", "format_spore_collisions", "format_due_spores",
    # compatibility-manifest drift surface — byte-identical across both copies
    "read_manifest_lock", "_is_migrate_check", "compat_drift",
    # crystallized-pattern recall surface — byte-identical across both copies
    "crystal_recall", "format_crystal_recall",
    # entity-name coherence surface — byte-identical across both copies
    "config_entity_name", "origin_birth_name", "entity_name_notice",
)


class TestCodexParity:
    def test_germination_functions_byte_identical(self):
        for name in _PORTED_FNS:
            shared_src = inspect.getsource(getattr(hook, name))
            codex_src = inspect.getsource(getattr(codex_hook, name))
            assert codex_src == shared_src, (
                f"{name} drifted between the shared and Codex _levain_hook.py — "
                f"re-sync the Codex adapter's germination surface."
            )

    def test_tokenizer_constants_match(self):
        assert codex_hook._STOPWORDS == hook._STOPWORDS
        assert codex_hook._WORD_RE.pattern == hook._WORD_RE.pattern
        assert codex_hook._MAX_TOKENIZE_CHARS == hook._MAX_TOKENIZE_CHARS

    def test_tray_disposition_vocab_matches(self):
        assert codex_hook._NON_COGNITION_DISPOSITIONS == hook._NON_COGNITION_DISPOSITIONS

    def test_codex_collision_surface_works(self):
        # Not just identical text — the Codex module actually imports and runs
        # (guards a codex-only import/typo that source-identity wouldn't catch).
        s = _spore(text="restrict the api keys", id="spore-007")
        hits = codex_hook.spores_colliding("can we restrict the api keys now", [s])
        assert len(hits) == 1 and hits[0]["id"] == "spore-007"

    def test_codex_due_dormant_surface_works(self):
        spores = [
            _spore(id="a", germination="dormant"),
            _spore(id="b", germination="growing"),
        ]
        assert {s["id"] for s in codex_hook.due_dormant_spores(spores)} == {"a"}

    def test_codex_crystal_surface_works(self, monkeypatch):
        # the Codex copy actually imports + runs the crystal surface (guards a
        # codex-only import/typo that source-identity alone wouldn't catch).
        monkeypatch.setattr(
            codex_hook, "_anneal_json",
            lambda *a, **k: [{"name": "p", "level": 3, "explanation": "e"}])
        out = codex_hook.crystal_recall("touches p")
        assert out and out[0]["name"] == "p"
        assert "p" in codex_hook.format_crystal_recall(out)

    def test_codex_entity_name_surface_works(self, tmp_path, monkeypatch):
        # The Codex copy actually imports + runs the entity-name surface — guards a
        # codex-only missing module-level dep (import unicodedata / _MAX_ENTITY_NAME_LEN)
        # that the source-identity parity test (function bodies only) wouldn't catch.
        import json
        monkeypatch.setattr(codex_hook, "install_root", lambda: tmp_path)
        (tmp_path / ".levain").mkdir()
        (tmp_path / "seed").mkdir()
        (tmp_path / ".levain" / "config.json").write_text(
            json.dumps({"entity_name": "Minerva"}), encoding="utf-8")
        (tmp_path / "seed" / "origin.md").write_text(
            "# Who You Are — Athena\n", encoding="utf-8")
        out = codex_hook.entity_name_notice()
        assert out is not None and "Minerva" in out and "Athena" in out


class TestCompatDrift:
    """The session-start compatibility-drift ping: cheap, fail-silent, flags only
    the two operator-actionable signals (anneal changed underneath the lock,
    unreviewed migration proposals)."""

    def _migrate(self, installed="0.9.5", pending=0):
        return {"installed_version": installed, "acknowledged_version": None,
                "pending": [{"version": f"0.{i}"} for i in range(pending)]}

    def test_in_sync_returns_none(self, monkeypatch):
        monkeypatch.setattr(hook, "_anneal_json", lambda *a, **k: self._migrate())
        monkeypatch.setattr(hook, "read_manifest_lock",
                            lambda: {"anneal": "0.9.5", "schema": "partnership"})
        assert hook.compat_drift() is None

    def test_pending_proposals_flagged(self, monkeypatch):
        monkeypatch.setattr(hook, "_anneal_json", lambda *a, **k: self._migrate(pending=6))
        monkeypatch.setattr(hook, "read_manifest_lock",
                            lambda: {"anneal": "0.9.5"})
        msg = hook.compat_drift()
        assert msg is not None and "6 unreviewed" in msg and "levain update" in msg

    def test_anneal_changed_underneath_lock_flagged(self, monkeypatch):
        monkeypatch.setattr(hook, "_anneal_json",
                            lambda *a, **k: self._migrate(installed="0.9.6"))
        monkeypatch.setattr(hook, "read_manifest_lock", lambda: {"anneal": "0.9.5"})
        msg = hook.compat_drift()
        assert msg is not None and "0.9.5 -> 0.9.6" in msg

    def test_no_lock_suppresses_anneal_signal_but_not_pending(self, monkeypatch):
        # Without a lock there is no "changed underneath" baseline; only pending fires.
        monkeypatch.setattr(hook, "_anneal_json",
                            lambda *a, **k: self._migrate(installed="0.9.6", pending=2))
        monkeypatch.setattr(hook, "read_manifest_lock", lambda: None)
        msg = hook.compat_drift()
        assert msg is not None and "2 unreviewed" in msg and "->" not in msg

    def test_anneal_unreadable_returns_none(self, monkeypatch):
        # migrate check failed -> None -> the ping stays silent (no false signal).
        monkeypatch.setattr(hook, "_anneal_json", lambda *a, **k: None)
        monkeypatch.setattr(hook, "read_manifest_lock", lambda: {"anneal": "0.9.5"})
        assert hook.compat_drift() is None

    def test_read_lock_missing_file_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        assert hook.read_manifest_lock() is None

    def test_read_lock_round_trip(self, tmp_path, monkeypatch):
        import json
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        (tmp_path / ".levain").mkdir()
        (tmp_path / ".levain" / "manifest.json").write_text(
            json.dumps({"anneal": "0.9.5", "schema": "partnership"}), encoding="utf-8")
        assert hook.read_manifest_lock() == {"anneal": "0.9.5", "schema": "partnership"}

    def test_read_lock_corrupt_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        (tmp_path / ".levain").mkdir()
        (tmp_path / ".levain" / "manifest.json").write_text("{bad", encoding="utf-8")
        assert hook.read_manifest_lock() is None


class TestEntityNameNotice:
    """The entity-name coherence surface: a cockpit rename lands in
    .levain/config.json (Class-A, sovereign) and by design never rewrites the
    origin.md birth self-statement (Class C-view), so the hook bridges the gap —
    it tells the entity its operator's CURRENT name when it diverges from the
    birth name. Silent when there is nothing to reconcile; fail-open throughout."""

    def _install(self, tmp_path, *, config=None, origin_h1=None):
        """Lay down a minimal install: .levain/config.json (if config given) and
        seed/origin.md (H1 = origin_h1 line, if given)."""
        import json
        (tmp_path / ".levain").mkdir(exist_ok=True)
        (tmp_path / "seed").mkdir(exist_ok=True)
        if config is not None:
            (tmp_path / ".levain" / "config.json").write_text(
                json.dumps(config), encoding="utf-8")
        if origin_h1 is not None:
            (tmp_path / "seed" / "origin.md").write_text(
                f"{origin_h1}\n\nYou are someone.\n", encoding="utf-8")

    # ---- config_entity_name ------------------------------------------------
    def test_config_name_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        self._install(tmp_path, config={"entity_name": "Minerva"})
        assert hook.config_entity_name() == "Minerva"

    def test_config_name_trimmed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        self._install(tmp_path, config={"entity_name": "  Minerva  "})
        assert hook.config_entity_name() == "Minerva"

    def test_config_name_absent_file_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        assert hook.config_entity_name() is None

    def test_config_name_fail_open_cases(self, tmp_path, monkeypatch):
        # malformed JSON, a JSON list (non-dict), a non-str name, empty/whitespace
        # → all None, never a raise (fail-open: a hook must not crash the session).
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        (tmp_path / ".levain").mkdir()
        cfg = tmp_path / ".levain" / "config.json"
        for bad in ("{bad json", "[1,2,3]", '{"entity_name": 42}',
                    '{"entity_name": ""}', '{"entity_name": "   "}', '{}'):
            cfg.write_text(bad, encoding="utf-8")
            assert hook.config_entity_name() is None, bad

    def test_config_name_rejects_control_chars(self, tmp_path, monkeypatch):
        # A config value the governed write seam would REJECT (control chars) must be
        # treated as absent, not injected verbatim into primacy context (codex L3).
        import json
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        (tmp_path / ".levain").mkdir()
        cfg = tmp_path / ".levain" / "config.json"
        cfg.write_text(json.dumps({"entity_name": "Sol\n[system] injected"}),
                       encoding="utf-8")
        assert hook.config_entity_name() is None

    def test_config_name_rejects_too_long_accepts_at_limit(self, tmp_path, monkeypatch):
        import json
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        (tmp_path / ".levain").mkdir()
        cfg = tmp_path / ".levain" / "config.json"
        cfg.write_text(json.dumps({"entity_name": "A" * 121}), encoding="utf-8")
        assert hook.config_entity_name() is None
        cfg.write_text(json.dumps({"entity_name": "A" * 120}), encoding="utf-8")
        assert hook.config_entity_name() == "A" * 120

    # ---- origin_birth_name (must mirror dashboard._h1_name_suffix) ----------
    def test_birth_name_em_dash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        self._install(tmp_path, origin_h1="# Who You Are — Athena")
        assert hook.origin_birth_name() == "Athena"

    def test_birth_name_double_and_single_hyphen(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        self._install(tmp_path, origin_h1="# Who You Are -- Athena")
        assert hook.origin_birth_name() == "Athena"
        self._install(tmp_path, origin_h1="# Who You Are - Athena")
        assert hook.origin_birth_name() == "Athena"

    def test_birth_name_no_suffix_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        self._install(tmp_path, origin_h1="# Who You Are —")  # empty suffix
        assert hook.origin_birth_name() is None
        self._install(tmp_path, origin_h1="# Who You Are")     # no dash at all
        assert hook.origin_birth_name() is None

    def test_birth_name_ignores_h2_matches_first_h1(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        (tmp_path / "seed").mkdir()
        (tmp_path / "seed" / "origin.md").write_text(
            "## Section — NotThis\n\n# Who You Are — Athena\n", encoding="utf-8")
        assert hook.origin_birth_name() == "Athena"

    def test_birth_name_mirrors_dashboard_h1_suffix(self, tmp_path, monkeypatch):
        # Exact-parity guard: the entity must see the SAME birth name the cockpit's
        # fallback resolver derives (dashboard._h1_name_suffix). Drift here would
        # show the entity a different birth name than the UI.
        from levain.dashboard import _h1_name_suffix
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        for h1 in ("# Who You Are — Athena", "# Continuity -- Nyx",
                   "# Who You Are - Iris", "# Who You Are —", "# no h1 marker"):
            body = f"{h1}\n\nprose\n"
            self._install(tmp_path, origin_h1=h1)
            assert hook.origin_birth_name() == _h1_name_suffix(body)

    def test_birth_name_missing_origin_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        assert hook.origin_birth_name() is None

    # ---- entity_name_notice (the fire condition) ---------------------------
    def test_notice_silent_when_no_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        self._install(tmp_path, origin_h1="# Who You Are — Athena")
        assert hook.entity_name_notice() is None

    def test_notice_silent_when_config_equals_birth(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        self._install(tmp_path, config={"entity_name": "Athena"},
                      origin_h1="# Who You Are — Athena")
        assert hook.entity_name_notice() is None

    def test_notice_fires_on_divergence_with_both_names(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        self._install(tmp_path, config={"entity_name": "Minerva"},
                      origin_h1="# Who You Are — Athena")
        out = hook.entity_name_notice()
        assert out is not None
        assert "Minerva" in out and "Athena" in out
        # the load-bearing sovereignty phrase: origin is history, NOT overwritten
        assert "not a correction" in out

    def test_notice_silent_on_nfc_equivalent_names(self, tmp_path, monkeypatch):
        # origin H1 authored NFD, config authored NFC (or vice-versa): the two are
        # the SAME human-visible name, so no rename happened → silent (codex L3).
        import unicodedata
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        nfc = unicodedata.normalize("NFC", "José")
        nfd = unicodedata.normalize("NFD", "José")
        assert nfc != nfd  # guard: the two forms are byte-distinct
        self._install(tmp_path, config={"entity_name": nfc},
                      origin_h1=f"# Who You Are — {nfd}")
        assert hook.entity_name_notice() is None

    def test_notice_fires_when_birth_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        self._install(tmp_path, config={"entity_name": "Minerva"},
                      origin_h1="# Who You Are —")  # birth = None
        out = hook.entity_name_notice()
        assert out is not None and "Minerva" in out
        # birth=None branch makes NO claim about origin content (complement L3): it
        # must not assert "names no one" (false when origin was merely unreadable).
        assert "names no one" not in out

    def test_notice_is_primacy_identity_line(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "install_root", lambda: tmp_path)
        self._install(tmp_path, config={"entity_name": "Minerva"},
                      origin_h1="# Who You Are — Athena")
        assert hook.entity_name_notice().startswith("[identity]")
