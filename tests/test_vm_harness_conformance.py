"""Dispatch wave 3 item 13: the conformance matrix table and runner, against a fake attempt. No VM, no binaries."""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from scripts.vm_harness import conformance as cf

ROUTES = {"claude": ["anthropic", "or-claude"], "codex": ["openai"]}
VERSIONS = {"claude": ["2.1.300"], "codex": ["0.170.0", "0.171.0"]}


def honest(cell):
    return {"verdict": cell.expected, "raw": cell.expected}      # the raw shape is the literal word (conformance.RAW_PATTERNS)


def down(cell):
    return False                                                  # the queue is unreachable


@pytest.fixture
def run(tmp_path):
    """run_matrix into a fresh evidence dir each call; the queue hook defaults to an unreachable queue."""
    def go(cells, attempt, **kw):
        kw.setdefault("queue_reachable", down)
        kw.setdefault("vendors", sorted({c.vendor for c in cells}))
        return cf.run_matrix(cells, attempt, evidence_dir=tempfile.mkdtemp(dir=tmp_path), **kw)
    return go


def test_one_verdict_per_cell_in_the_vocabulary():
    assert len(cf.EXPECTED) == sum(len(t) for t in cf.TOOLS.values()) * len(cf.CASES)
    assert set(cf.EXPECTED.values()) <= set(cf.VERDICTS)
    for v in cf.TOOLS.values():
        assert v


def test_queue_down_differs_by_vendor_and_says_why():
    # Claude Code: onFailure block, so a dead queue blocks; Codex's slot_gate denies (fail closed) when the broker is down
    assert cf.expected_verdict("claude", "Write", "queue-down") == cf.BLOCKED_QUEUE_UNREACHABLE != cf.BLOCKED
    assert cf.expected_verdict("codex", "shell", "queue-down") == cf.BLOCKED_QUEUE_UNREACHABLE
    assert cf.expected_verdict("codex", "shell", "blocked") == cf.BLOCKED
    assert cf.expected_verdict("codex", "shell", "allowed") == cf.ALLOWED


def test_unknown_rows_are_errors_not_defaults():
    for args in (("gemini", "x", "blocked"), ("codex", "apply_patch", "blocked"), ("claude", "Write", "maybe")):
        with pytest.raises(ValueError):
            cf.expected_verdict(*args)


def test_matrix_expands_every_tool_route_version_case():
    cells = cf.build_matrix(ROUTES, VERSIONS)
    assert len(cells) == 3 * 2 * 1 * 3 + 1 * 1 * 2 * 3
    assert len({c.key for c in cells}) == len(cells)
    assert {c.case for c in cells} == set(cf.CASES)


def test_matrix_refuses_empty_and_incomplete():
    with pytest.raises(ValueError):
        cf.build_matrix({}, {})
    with pytest.raises(ValueError):
        cf.build_matrix({"claude": ["a"]}, {})
    with pytest.raises(ValueError):
        cf.build_matrix({"claude": []}, {"claude": ["1"]})
    with pytest.raises(ValueError):
        cf.build_matrix({"bing": ["a"]}, {"bing": ["1"]})
    with pytest.raises(ValueError):
        cf.run_matrix([], honest, evidence_dir="/nonexistent")


CLAUDE_ONLY = ({"claude": ["anthropic", "or-claude"]}, {"claude": ["2.1.300"]})


def test_the_codex_queue_down_expectation_is_what_the_levain_code_does():
    """Quote the code, not memory: the broker-unreachable path of codex.slot_gate is a deny (exit 0), and rendering refuses
    Codex hooks, which is why the Codex cells are NOT_RUNNABLE."""
    import inspect
    from levain.consent.adapters import codex
    src = inspect.getsource(codex.slot_gate)
    assert "fail closed: the broker down, refusing or malformed is a deny" in src
    assert "return could_not_run(\"this call\")" in src
    render = (Path(inspect.getsourcefile(codex)).parents[2] / "slot" / "render.py").read_text()
    assert 'hooks-not-wired:codex' in render and "codex" in cf.NOT_RUNNABLE and "hooks-not-wired:codex" in cf.NOT_RUNNABLE["codex"]


def test_honest_attempt_passes_all(run):
    cells = cf.build_matrix(*CLAUDE_ONLY, vendors=("claude",))
    res = run(cells, honest)
    assert res["ok"] is True and res["status"] == "PASS" and res["counts"] == {"PASS": len(cells)}
    assert set(res["expected"]) == {c.key for c in cells}


def test_codex_cells_are_not_runnable_never_pass_or_fail_and_the_overall_says_how_many(run, tmp_path):
    cells = cf.build_matrix(ROUTES, VERSIONS)
    n_codex = sum(1 for c in cells if c.vendor == "codex")
    called = []
    res = run(cells, lambda c: called.append(c.vendor) or honest(c))
    assert "codex" not in called                                           # never attempted
    assert res["counts"] == {"PASS": len(cells) - n_codex, "NOT_RUNNABLE": n_codex} and res["not_runnable"] == n_codex
    assert res["ok"] is False and res["status"] == "PARTIAL"              # what ran passed; the route is not certified
    row = next(r for r in res["rows"] if r["cell"].startswith("codex/"))
    assert row["status"] == "NOT_RUNNABLE" and "hooks-not-wired:codex" in row["reason"]
    dirs = sorted(tmp_path.iterdir())
    text = (dirs[-1] / "RESULT.txt").read_text()
    assert text.startswith("RESULT: PARTIAL") and f"not runnable: {n_codex} of {len(cells)} cells" in text
    assert "NOT RUNNABLE: hooks-not-wired:codex" in (dirs[-1] / "EXPECTED.txt").read_text()
    only_codex = cf.build_matrix({"codex": ["o"]}, {"codex": ["1"]}, vendors=("codex",))
    r2 = run(only_codex, honest, queue_reachable=None)                      # no hook needed: nothing runnable
    assert r2["counts"] == {"NOT_RUNNABLE": len(only_codex)} and r2["ok"] is False


def test_a_wrong_verdict_fails_exactly_that_cell(run):
    cells = cf.build_matrix(*CLAUDE_ONLY, vendors=("claude",))
    bad = "claude/Bash/or-claude/2.1.300/queue-down"

    def attempt(c):
        # a verdict that is not the one expected is a FAIL
        return {"verdict": cf.ALLOWED if c.key == bad else c.expected,
                "raw": cf.ALLOWED if c.key == bad else c.expected}
    res = run(cells, attempt)
    assert res["ok"] is False
    assert [r["cell"] for r in res["rows"] if r["status"] == "FAIL"] == [bad]


def test_error_and_out_of_vocabulary_never_pass(run):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))

    def boom(c):
        raise RuntimeError("tunnel down")
    res = run(cells, boom)
    assert res["counts"] == {"ERROR": len(cells)} and res["ok"] is False
    assert "RuntimeError" in res["rows"][0]["raw"]
    res = run(cells, lambda c: {"verdict": "ok-ish", "raw": "ok-ish"})
    assert res["counts"] == {"FAIL": len(cells)}


def test_expected_table_is_on_disk_with_its_digest_before_the_first_attempt(tmp_path):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))
    ev = tmp_path / "ev"
    ev.mkdir()
    seen = []

    def attempt(c):
        f = ev / "EXPECTED.txt"
        body, _, tail = f.read_text().rpartition("\nsha256: ") if f.exists() else ("", "", "")
        seen.append(f.exists() and tail.strip() == hashlib.sha256(body.encode()).hexdigest()
                    and not (ev / "RESULT.txt").exists() and all(c.key in body for c in cells))
        return honest(c)
    res = cf.run_matrix(cells, attempt, evidence_dir=ev, queue_reachable=down, vendors=("claude",))
    assert seen and all(seen) and len(seen) == len(cells)           # an attempt before the file existed would record False
    assert f"sha256 {res['expected_sha256']}" in (ev / "RESULT.txt").read_text()
    assert "queue unreachable" in (ev / "EXPECTED.txt").read_text()


def test_a_used_evidence_dir_is_refused_before_any_attempt(tmp_path):
    (tmp_path / "old").write_text("x")
    called = []
    with pytest.raises(ValueError, match="not empty"):
        cf.run_matrix(cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",)), lambda c: called.append(c) or honest(c),
                      evidence_dir=tmp_path, queue_reachable=down, vendors=("claude",))
    assert called == []


@pytest.mark.parametrize("raw", [None, "", "   ", "fake", "allowed extra"])
def test_a_right_verdict_with_an_empty_or_unmatching_raw_is_never_a_pass(run, raw):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))
    res = run(cells, lambda c: {"verdict": c.expected, "raw": raw})
    assert res["counts"] == {"FAIL": len(cells)} and res["ok"] is False
    assert run(cells, lambda c: {"verdict": c.expected}) ["counts"] == {"FAIL": len(cells)}      # no raw key at all


def test_raw_patterns_cover_every_verdict_with_one_pattern():
    assert set(cf.RAW_PATTERNS) == set(cf.VERDICTS)
    assert all("|" not in p for p in cf.RAW_PATTERNS.values())


def test_a_queue_that_silently_stayed_up_cannot_pass_queue_down(run):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))
    res = run(cells, honest, queue_reachable=lambda c: True)       # the verdict and raw are right; the queue never went down
    failed = {r["cell"] for r in res["rows"] if r["status"] == "FAIL"}
    assert failed == {c.key for c in cells if c.case == "queue-down"} and res["ok"] is False
    row = next(r for r in res["rows"] if r["cell"].endswith("queue-down"))
    assert row["queue_down_before"] is False and row["queue_down_after"] is False


def test_a_queue_that_came_back_during_the_attempt_cannot_pass(run):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))
    state = {"n": 0}

    def flaky(c):
        state["n"] += 1
        return state["n"] % 2 == 0          # unreachable before the attempt, reachable after it
    res = run(cells, honest, queue_reachable=flaky)
    assert all(r["status"] == "FAIL" for r in res["rows"] if r["cell"].endswith("queue-down"))


def test_a_queue_down_matrix_without_the_hook_is_refused_before_any_evidence(tmp_path):
    ev = tmp_path / "ev"
    ev.mkdir()
    with pytest.raises(ValueError, match="queue_reachable"):
        cf.run_matrix(cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",)), honest, evidence_dir=ev, vendors=("claude",))
    assert list(ev.iterdir()) == []


def test_a_crashing_queue_hook_is_an_error(run):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))

    def boom(c):
        raise OSError("probe failed")
    res = run(cells, honest, queue_reachable=boom)
    assert res["counts"].get("ERROR") == len([c for c in cells if c.case == "queue-down"]) and res["ok"] is False


def test_the_documented_run_command_has_no_dash_I():
    doc = cf.__doc__
    assert "python3 -m scripts.vm_harness.conformance table" in doc and "python3 -I" not in doc


def test_cli_table(capsys):
    assert cf.main(["table"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == len(cf.EXPECTED)
    assert cf.main(["run"]) == 2


# ---- L3 round 1 fixes -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("answer", [None, 0, 1, "", "yes", [], 0.0])
def test_a_non_bool_queue_answer_is_an_error_never_a_pass(run, answer):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))
    res = run(cells, honest, queue_reachable=lambda c: answer)
    qd = [r for r in res["rows"] if r["cell"].endswith("queue-down")]
    assert qd and all(r["status"] == "ERROR" and "want a bool" in r["raw"] for r in qd) and res["ok"] is False


def test_only_false_before_and_after_passes_queue_down(run):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))
    res = run(cells, honest, queue_reachable=lambda c: False)
    assert res["ok"] is True


def test_an_attempt_that_mutates_the_tables_mid_run_cannot_pass_a_row(run, monkeypatch):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))
    exp = dict(cf.EXPECTED)
    pats = dict(cf.RAW_PATTERNS)
    monkeypatch.setattr(cf, "EXPECTED", exp)                 # mutable stand-ins, the worst case for the old code
    monkeypatch.setattr(cf, "RAW_PATTERNS", pats)

    def cheat(c):
        exp[(c.vendor, c.tool, c.case)] = cf.ALLOWED         # move the goalposts to what this attempt will say
        pats[cf.ALLOWED] = "forged"
        pats[cf.BLOCKED] = "forged"
        return {"verdict": cf.ALLOWED, "raw": "forged"}
    res = run(cells, cheat)
    blocked = [r for r in res["rows"] if r["cell"].endswith("/blocked")]
    assert blocked and all(r["status"] == "FAIL" and r["expected"] == cf.BLOCKED for r in blocked)
    # the very first cell was graded against the frozen table; the ones after it the same
    assert res["ok"] is False and "PASS" not in {r["status"] for r in res["rows"] if r["cell"].endswith("/blocked")}


def test_the_shipped_tables_are_read_only():
    with pytest.raises(TypeError):
        cf.EXPECTED[("claude", "Write", "blocked")] = cf.ALLOWED          # type: ignore[index]
    with pytest.raises(TypeError):
        cf.RAW_PATTERNS[cf.BLOCKED] = "x"                                  # type: ignore[index]


@pytest.mark.parametrize("bad", [b"allowed", 7, ["allowed"], object(), {"a": 1}])
def test_a_non_string_adapter_answer_is_a_fail_and_the_run_finishes(run, tmp_path, bad):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))
    res = run(cells, lambda c: {"verdict": bad, "raw": bad})
    assert res["counts"] == {"FAIL": len(cells)} and len(res["rows"]) == len(cells)
    assert all(len(r["raw"]) <= 300 for r in res["rows"])
    log = sorted(tmp_path.iterdir())[-1] / "steps.log"
    assert len(log.read_text().splitlines()) == len(cells)               # every row serialised
    assert (log.parent / "RESULT.txt").read_text().startswith("RESULT: FAIL")


def test_a_run_that_dies_still_writes_a_result_that_says_aborted(tmp_path):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))
    ev = tmp_path / "ev"
    ev.mkdir()

    def die(c):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        cf.run_matrix(cells, die, evidence_dir=ev, queue_reachable=down, vendors=("claude",))
    assert (ev / "RESULT.txt").read_text().startswith("RESULT: ABORTED")


def test_a_system_exit_in_an_attempt_is_an_error_row(run):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))

    def quit_(c):
        raise SystemExit(3)
    res = run(cells, quit_)
    assert res["counts"] == {"ERROR": len(cells)}


def test_a_cell_outside_the_table_is_a_value_error_before_any_evidence(tmp_path):
    ev = tmp_path / "ev"
    ev.mkdir()
    with pytest.raises(ValueError, match="not in the expected table"):
        cf.run_matrix([cf.Cell("claude", "apply_patch", "r", "1", "blocked")], honest, evidence_dir=ev, queue_reachable=down)
    assert list(ev.iterdir()) == []


def test_a_short_write_that_makes_no_progress_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(cf.os, "write", lambda fd, data: 0)
    with pytest.raises(OSError, match="no progress"):
        cf._write_durable(tmp_path / "f", "abc")


# ---- L3 round 2 fixes -------------------------------------------------------------------------------------------------

def test_a_filtered_cell_list_never_certifies(tmp_path):
    full = cf.build_matrix(*CLAUDE_ONLY, vendors=("claude",))
    one = [cf.Cell("claude", "Write", "anthropic", "2.1.300", "allowed")]
    for part in (one, [c for c in full if c.case != "blocked"], [c for c in full if c.tool != "Edit"]):
        ev = Path(tempfile.mkdtemp(dir=tmp_path))
        with pytest.raises(ValueError, match="not the full matrix"):
            cf.run_matrix(part, honest, evidence_dir=ev, queue_reachable=down, vendors=("claude",))
        assert list(ev.iterdir()) == []
    # a whole required vendor missing (default vendors = every TOOLS vendor)
    with pytest.raises(ValueError, match="not the full matrix"):
        cf.run_matrix(full, honest, evidence_dir=Path(tempfile.mkdtemp(dir=tmp_path)), queue_reachable=down)
    ok = cf.run_matrix(full, honest, evidence_dir=Path(tempfile.mkdtemp(dir=tmp_path)), queue_reachable=down, vendors=("claude",))
    assert ok["ok"] is True


def test_build_matrix_refuses_a_bare_string_a_duplicate_and_an_omitted_vendor():
    with pytest.raises(ValueError, match="list of ids"):
        cf.build_matrix({"claude": "anthropic"}, {"claude": ["1"]}, vendors=("claude",))
    with pytest.raises(ValueError, match="duplicate"):
        cf.build_matrix({"claude": ["o", "o"]}, {"claude": ["1"]}, vendors=("claude",))
    with pytest.raises(ValueError, match="required"):
        cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]})            # codex omitted, silently uncovered before


def test_cell_keys_are_unambiguous_and_duplicate_cells_refused(tmp_path):
    with pytest.raises(ValueError, match="without '/'"):
        cf.Cell("claude", "Write", "a/b", "1", "blocked")
    cell = cf.Cell("claude", "Write", "a", "1", "blocked")
    with pytest.raises(ValueError, match="duplicate cell"):
        cf.run_matrix([cell, cell], honest, evidence_dir=tmp_path, queue_reachable=down)


def test_a_post_attempt_probe_that_raises_keeps_the_observation(run):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))
    n = {"n": 0}

    def hook(c):
        n["n"] += 1
        if n["n"] % 2 == 0:
            raise OSError("probe died")
        return False
    res = run(cells, honest, queue_reachable=hook)
    row = next(r for r in res["rows"] if r["cell"].endswith("queue-down"))
    assert row["status"] == "ERROR" and "probe died" in row["error"]
    assert row["observed"] == cf.BLOCKED_QUEUE_UNREACHABLE and row["raw"] == cf.BLOCKED_QUEUE_UNREACHABLE


def test_a_missing_verdict_is_a_contract_fail_not_an_error(run):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))
    res = run(cells, lambda c: {"raw": c.expected})
    assert res["counts"] == {"FAIL": len(cells)}
    assert run(cells, lambda c: "not a mapping")["counts"] == {"FAIL": len(cells)}


def test_an_attempt_that_rewrites_the_cell_case_cannot_skip_the_queue_probe(run):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}, vendors=("claude",))

    def cheat(c):
        c.__dict__["case"] = "allowed"          # frozen dataclass, but __dict__ is still reachable from injected code
        return {"verdict": cf.BLOCKED_QUEUE_UNREACHABLE, "raw": cf.BLOCKED_QUEUE_UNREACHABLE}
    res = run(cells, cheat, queue_reachable=lambda c: True)          # the queue never went down
    qd = [r for r in res["rows"] if r["cell"].endswith("queue-down")]
    assert qd and all(r["status"] == "FAIL" and r["queue_down_before"] is False for r in qd)


def test_write_durable_fsyncs_the_directory_and_unlinks_a_partial_file(tmp_path, monkeypatch):
    synced = []
    real = cf.os.fsync
    monkeypatch.setattr(cf.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    cf._write_durable(tmp_path / "ok.txt", "x", exclusive=True)
    assert len(synced) == 2                                           # the file, then its parent directory
    calls = {"n": 0}
    real_write = cf.os.write

    def flaky(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, data[:1])
        raise OSError("disk full")
    monkeypatch.setattr(cf.os, "write", flaky)
    with pytest.raises(OSError, match="disk full"):
        cf._write_durable(tmp_path / "partial.txt", "abcdef", exclusive=True)
    assert not (tmp_path / "partial.txt").exists()
