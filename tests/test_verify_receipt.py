"""The receipt writer and the ledger of scripts/verify.py (levain row 164/172, L2 session 1).

The junit fixture is a real pytest run (pass, fail, skip, xfail, error) executed in a tmp dir, so the counts are
re-derived from a file pytest wrote, not from a hand-typed one.  Nothing here touches the real ledger or run dir.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("levain_verify_script", ROOT / "scripts" / "verify.py")
verify = importlib.util.module_from_spec(_spec)
sys.modules["levain_verify_script"] = verify
_spec.loader.exec_module(verify)

FIXTURE_TESTS = textwrap.dedent('''
    import pytest

    def test_ok(): pass

    @pytest.mark.parametrize("n", [1, 2])
    def test_param(n): assert n == 1

    def test_boom(): assert False, "boom"

    @pytest.mark.skip(reason="no mac keychain")
    def test_skipped(): pass

    @pytest.mark.xfail(reason="known")
    def test_xf(): assert False

    @pytest.fixture
    def broken(): raise RuntimeError("setup died")

    def test_err(broken): pass

    class TestK:
        def test_in_class(self): assert False
''')


@pytest.fixture(scope="module")
def junit_run(tmp_path_factory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("fx")
    (d / "test_fx.py").write_text(FIXTURE_TESTS)
    j = d / "junit.xml"
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", f"--junitxml={j}",
                        "--rootdir", str(d), "-c", "/dev/null", str(d / "test_fx.py")],
                       cwd=d, capture_output=True, text=True, timeout=120)
    assert r.returncode == 1, r.stdout + r.stderr    # the fixture has reds on purpose
    return d, j


def test_counts_and_reds_rederived_from_junit(junit_run):
    d, j = junit_run
    p = verify.parse_junit(j, d)
    c = p["counts"]
    assert c == dict(collected=8, passed=2, failed=3, error=1, skipped=1, xfailed=1), c
    assert p["skips_by_reason"] == {"no mac keychain": 1}
    assert [r["nodeid"] for r in p["reds"]] == [
        "test_fx.py::test_param[2]", "test_fx.py::test_boom", "test_fx.py::test_err", "test_fx.py::TestK::test_in_class"]
    assert {r["kind"] for r in p["reds"]} == {"failure", "error"}
    assert "boom" in next(r for r in p["reds"] if r["nodeid"].endswith("test_boom"))["text"]


def test_nodeid_without_checkout_uses_class_heuristic(junit_run):
    _, j = junit_run
    ids = [r["nodeid"] for r in verify.parse_junit(j, None)["reds"]]
    assert "test_fx.py::TestK::test_in_class" in ids


def _leg(reds, collected=10, rc=1):
    return {"platform": "macos", "jobs": [{"name": "pytest-full-py312", "rc": rc, "reds": reds,
                                           "counts": dict(collected=collected, passed=0, failed=len(reds), error=0, skipped=0, xfailed=0)}]}


def test_red_without_ledger_id_makes_the_receipt_red():
    leg = _leg([{"nodeid": "tests/a.py::t", "class": "load", "evidence": "x", "ledger_id": None}])
    rec = verify.build_receipt("a" * 40, "run1", {}, "blob", [leg], True)
    assert rec["verdict"] == "red" and "red without ledger_id: tests/a.py::t" in rec["verdict_why"]


def test_regression_red_even_with_ledger_id_and_green_when_all_excused():
    bad = _leg([{"nodeid": "t", "class": "regression", "evidence": "x", "ledger_id": "lg_1"}])
    assert verify.build_receipt("a" * 40, "r", {}, "b", [bad], True)["verdict"] == "red"
    ok = _leg([{"nodeid": "t", "class": "inherited", "evidence": "x", "ledger_id": "lg_1"}])
    rec = verify.build_receipt("a" * 40, "r", {}, "b", [ok], True)
    assert rec["verdict"] == "green" and rec["schema"] == "levain-test-receipt/1"


def test_empty_run_runner_mismatch_and_pytest_crash_are_red():
    assert verify.build_receipt("a" * 40, "r", {}, "b", [_leg([], collected=0, rc=0)], True)["verdict"] == "red"
    assert verify.build_receipt("a" * 40, "r", {}, "b", [_leg([], rc=2)], True)["verdict"] == "red"
    assert verify.build_receipt("a" * 40, "r", {}, None, [_leg([], rc=0)], None)["verdict"] == "red"
    assert verify.build_receipt("a" * 40, "r", {}, "b", [_leg([], rc=0)], False)["verdict"] == "red"
    assert verify.build_receipt("a" * 40, "r", {}, "b", [_leg([], rc=0)], True)["verdict"] == "green"


def test_receipt_file_name_and_roundtrip(tmp_path):
    rec = verify.build_receipt("0123456789abcdef" * 2 + "01234567", "r", {"uv_lock_sha256": "h"}, "b", [_leg([], rc=0)], True)
    p = verify.write_receipt(rec, tmp_path / "receipts")
    assert p.name == "vr_0123456789ab.json"
    assert json.loads(p.read_text()) == rec


def test_ledger_is_append_only_with_sightings(tmp_path):
    led = verify.Ledger(tmp_path / "l" / "ledger.jsonl")
    lid, new = led.file("tests/a.py::t", "flaky", "run1", "s1", "mixed")
    assert new and led.open_entry_for("tests/a.py::t")["board_row"] is None
    before = led.path.read_text()
    lid2, new2 = led.file("tests/a.py::t", "flaky", "run2", "s2", "mixed again")
    assert lid2 == lid and not new2
    after = led.path.read_text()
    assert after.startswith(before) and after.count("\n") == before.count("\n") + 1     # only appended
    led.mark(lid, "fixed")
    assert led.open_entry_for("tests/a.py::t") is None
    lid3, new3 = led.file("tests/a.py::t", "flaky", "run3", "s3", "back")
    assert new3 and lid3 != lid                                                           # a fixed entry is history, not reopened
    e = led.entries()
    assert [s["run_id"] for s in e[lid]["sightings"]] == ["run2"] and e[lid]["status"] == "fixed"
    assert [r["type"] for r in led.lines()] == ["entry", "sighting", "status", "entry"]


def test_classification_rules_end_to_end(tmp_path):
    led = verify.Ledger(tmp_path / "ledger.jsonl")
    seq = {"load": [True] * 3, "flaky": [True, False, True], "inh": [False] * 3, "reg": [False] * 3, "touched": [], "led": [False] * 3}
    calls = {}

    def rerun(nid):
        k = nid.split("::")[1]
        calls[k] = calls.get(k, 0) + 1
        return seq[k].pop(0)

    prior, _ = led.file("tests/a.py::led", "flaky", "r0", "s0", "was flaky")
    reds = [{"nodeid": f"tests/a.py::{k}", "kind": "failure"} for k in ("load", "flaky", "inh", "reg", "led")]
    reds.append({"nodeid": "tests/touched.py::t", "kind": "failure"})
    seq["t"] = []
    out = verify.classify(reds, ledger=led, run_id="r1", sha="s1", touched={"tests/touched.py"}, rerun=rerun,
                          baseline=lambda nids: {"tests/a.py::inh": "red", "tests/a.py::reg": "green"})
    cls = {r["nodeid"].split("::")[1] if "touched" not in r["nodeid"] else "touched": r["class"] for r in out}
    assert cls == {"load": "load", "flaky": "flaky", "inh": "inherited", "reg": "regression", "led": "regression", "touched": "regression"}
    assert all(calls[k] == 3 for k in ("load", "flaky", "inh", "reg", "led")) and "t" not in calls
    ids = {r["nodeid"].split("::")[1]: r["ledger_id"] for r in out if r["class"] != "regression"}
    assert all(v for v in ids.values()) and all(r["ledger_id"] is None for r in out if r["class"] == "regression")
    assert led.entries()[prior]["sightings"] == []        # a regression never counts as a sighting
    assert verify.verdict_of([_leg(out)])[0] == "red"


def test_absent_on_main_is_a_regression(tmp_path):
    led = verify.Ledger(tmp_path / "ledger.jsonl")
    out = verify.classify([{"nodeid": "tests/new.py::t"}], ledger=led, run_id="r", sha="s", touched=set(),
                          rerun=lambda n: False, baseline=lambda n: {})
    assert out[0]["class"] == "regression" and "not present" in out[0]["evidence"]


def test_linux_and_both_exit_nonzero_not_built(capsys):
    for leg in ("linux", "both"):
        assert verify.main(["run", "--sha", "HEAD", "--leg", leg]) == 2
        assert "not built yet" in capsys.readouterr().err


def test_host_lock_is_exclusive(tmp_path):
    a = verify.lock_host(tmp_path)
    assert a is not None and verify.lock_host(tmp_path) is None
    a.close()
    b = verify.lock_host(tmp_path)
    assert b is not None
    b.close()
