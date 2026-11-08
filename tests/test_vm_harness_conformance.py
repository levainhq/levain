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
        return cf.run_matrix(cells, attempt, evidence_dir=tempfile.mkdtemp(dir=tmp_path), **kw)
    return go


def test_one_verdict_per_cell_in_the_vocabulary():
    assert len(cf.EXPECTED) == sum(len(t) for t in cf.TOOLS.values()) * len(cf.CASES)
    assert set(cf.EXPECTED.values()) <= set(cf.VERDICTS)
    for v in cf.TOOLS.values():
        assert v


def test_queue_down_differs_by_vendor_and_says_why():
    # Claude Code: onFailure block, so a dead queue blocks; Codex fails open (the OS boundary alone holds it)
    assert cf.expected_verdict("claude", "Write", "queue-down") == cf.BLOCKED_QUEUE_UNREACHABLE != cf.BLOCKED
    assert cf.expected_verdict("codex", "shell", "queue-down") == cf.FAILS_OPEN
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


def test_honest_attempt_passes_all(run):
    cells = cf.build_matrix(ROUTES, VERSIONS)
    res = run(cells, honest)
    assert res["ok"] is True and res["counts"] == {"PASS": len(cells)}
    assert set(res["expected"]) == {c.key for c in cells}


def test_a_wrong_verdict_fails_exactly_that_cell(run):
    cells = cf.build_matrix(ROUTES, VERSIONS)
    bad = "codex/shell/openai/0.171.0/queue-down"

    def attempt(c):
        # a Codex that started failing closed would look like a pass to a lax runner; here it is a FAIL
        return {"verdict": cf.BLOCKED if c.key == bad else c.expected,
                "raw": cf.BLOCKED if c.key == bad else c.expected}
    res = run(cells, attempt)
    assert res["ok"] is False
    assert [r["cell"] for r in res["rows"] if r["status"] == "FAIL"] == [bad]


def test_error_and_out_of_vocabulary_never_pass(run):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]})

    def boom(c):
        raise RuntimeError("tunnel down")
    res = run(cells, boom)
    assert res["counts"] == {"ERROR": len(cells)} and res["ok"] is False
    assert "RuntimeError" in res["rows"][0]["raw"]
    res = run(cells, lambda c: {"verdict": "ok-ish", "raw": "ok-ish"})
    assert res["counts"] == {"FAIL": len(cells)}


def test_expected_table_is_on_disk_with_its_digest_before_the_first_attempt(tmp_path):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]})
    ev = tmp_path / "ev"
    ev.mkdir()
    seen = []

    def attempt(c):
        f = ev / "EXPECTED.txt"
        body, _, tail = f.read_text().rpartition("\nsha256: ") if f.exists() else ("", "", "")
        seen.append(f.exists() and tail.strip() == hashlib.sha256(body.encode()).hexdigest()
                    and not (ev / "RESULT.txt").exists() and all(c.key in body for c in cells))
        return honest(c)
    res = cf.run_matrix(cells, attempt, evidence_dir=ev, queue_reachable=down)
    assert seen and all(seen) and len(seen) == len(cells)           # an attempt before the file existed would record False
    assert f"sha256 {res['expected_sha256']}" in (ev / "RESULT.txt").read_text()
    assert "queue unreachable" in (ev / "EXPECTED.txt").read_text()


def test_a_used_evidence_dir_is_refused_before_any_attempt(tmp_path):
    (tmp_path / "old").write_text("x")
    called = []
    with pytest.raises(ValueError, match="not empty"):
        cf.run_matrix(cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}), lambda c: called.append(c) or honest(c),
                      evidence_dir=tmp_path, queue_reachable=down)
    assert called == []


@pytest.mark.parametrize("raw", [None, "", "   ", "fake", "allowed extra"])
def test_a_right_verdict_with_an_empty_or_unmatching_raw_is_never_a_pass(run, raw):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]})
    res = run(cells, lambda c: {"verdict": c.expected, "raw": raw})
    assert res["counts"] == {"FAIL": len(cells)} and res["ok"] is False
    assert run(cells, lambda c: {"verdict": c.expected}) ["counts"] == {"FAIL": len(cells)}      # no raw key at all


def test_raw_patterns_cover_every_verdict_with_one_pattern():
    assert set(cf.RAW_PATTERNS) == set(cf.VERDICTS)
    assert all("|" not in p for p in cf.RAW_PATTERNS.values())


def test_a_queue_that_silently_stayed_up_cannot_pass_queue_down(run):
    cells = cf.build_matrix({"claude": ["a"], "codex": ["o"]}, {"claude": ["1"], "codex": ["1"]})
    res = run(cells, honest, queue_reachable=lambda c: True)       # the verdict and raw are right; the queue never went down
    failed = {r["cell"] for r in res["rows"] if r["status"] == "FAIL"}
    assert failed == {c.key for c in cells if c.case == "queue-down"} and res["ok"] is False
    row = next(r for r in res["rows"] if r["cell"].endswith("queue-down"))
    assert row["queue_down_before"] is False and row["queue_down_after"] is False


def test_a_queue_that_came_back_during_the_attempt_cannot_pass(run):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]})
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
        cf.run_matrix(cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]}), honest, evidence_dir=ev)
    assert list(ev.iterdir()) == []


def test_a_crashing_queue_hook_is_an_error(run):
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]})

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
