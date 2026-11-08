"""Dispatch wave 3 item 13: the conformance matrix table and runner, against a fake attempt. No VM, no binaries."""
from __future__ import annotations

import pytest

from scripts.vm_harness import conformance as cf

ROUTES = {"claude": ["anthropic", "or-claude"], "codex": ["openai"]}
VERSIONS = {"claude": ["2.1.300"], "codex": ["0.170.0", "0.171.0"]}


def honest(cell):
    return {"verdict": cell.expected, "raw": "fake"}


def test_one_verdict_per_cell_in_the_vocabulary():
    assert len(cf.EXPECTED) == sum(len(t) for t in cf.TOOLS.values()) * len(cf.CASES)
    assert set(cf.EXPECTED.values()) <= set(cf.VERDICTS)
    for v in cf.TOOLS.values():
        assert v


def test_queue_down_differs_by_vendor_and_says_why():
    # Claude Code: onFailure block, so a dead queue blocks; Codex fails open (the OS boundary alone holds it)
    assert cf.expected_verdict("claude", "Write", "queue-down") == cf.BLOCKED
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
        cf.run_matrix([], honest)


def test_honest_attempt_passes_all():
    cells = cf.build_matrix(ROUTES, VERSIONS)
    res = cf.run_matrix(cells, honest)
    assert res["ok"] is True and res["counts"] == {"PASS": len(cells)}
    assert set(res["expected"]) == {c.key for c in cells}


def test_a_wrong_verdict_fails_exactly_that_cell():
    cells = cf.build_matrix(ROUTES, VERSIONS)
    bad = "codex/shell/openai/0.171.0/queue-down"

    def attempt(c):
        # a Codex that started failing closed would look like a pass to a lax runner; here it is a FAIL
        return {"verdict": cf.BLOCKED if c.key == bad else c.expected, "raw": "x"}
    res = cf.run_matrix(cells, attempt)
    assert res["ok"] is False
    assert [r["cell"] for r in res["rows"] if r["status"] == "FAIL"] == [bad]


def test_error_and_out_of_vocabulary_never_pass():
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]})

    def boom(c):
        raise RuntimeError("tunnel down")
    res = cf.run_matrix(cells, boom)
    assert res["counts"] == {"ERROR": len(cells)} and res["ok"] is False
    assert "RuntimeError" in res["rows"][0]["raw"]
    res = cf.run_matrix(cells, lambda c: {"verdict": "ok-ish", "raw": ""})
    assert res["counts"] == {"FAIL": len(cells)}


def test_expected_table_is_frozen_before_the_first_attempt():
    cells = cf.build_matrix({"claude": ["a"]}, {"claude": ["1"]})
    seen = []

    def attempt(c):
        seen.append(c.key)
        return honest(c)
    res = cf.run_matrix(cells, attempt)
    assert list(res["expected"]) == seen


def test_cli_table(capsys):
    assert cf.main(["table"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == len(cf.EXPECTED)
    assert cf.main(["run"]) == 2
