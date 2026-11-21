"""scripts/sign_stretch.py (levain#208): the per-day tally replayed over the real sigfail records of levain#191
(tests/fixtures/sign_stretch, copied from project_memory/1120-58_l191) plus one good signature. Always a tmp key dir."""
import datetime
import importlib.util
import json
import shutil
import time
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "sign_stretch_script", Path(__file__).resolve().parent.parent / "scripts" / "sign_stretch.py")
ss = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ss)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sign_stretch"


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _key_dir(tmp_path):
    kd = tmp_path / "keys"
    shutil.copytree(FIXTURES, kd / "sign-failures")
    return kd


def _ledger(tmp_path, *isos):
    p = tmp_path / "statements.jsonl"
    p.write_text("".join(json.dumps({"event": "statement", "id": None, "signed": {"format": "x"},
                                     "ts": datetime.datetime.fromisoformat(i).timestamp()}) + "\n" for i in isos))
    return p


def _run(capsys, *argv):
    rc = ss.main(list(argv))
    return rc, capsys.readouterr().out


def test_real_sigfail_records_plus_a_good_sign(tmp_path, capsys):
    kd = _key_dir(tmp_path)
    ledger = _ledger(tmp_path, "2026-11-20T17:30:00+00:00")
    before = sorted(p.name for p in kd.rglob("*"))
    rc, out = _run(capsys, "--key-dir", str(kd), "--ledger", str(ledger), "--days", "3", "--until", "2026-11-21")
    assert rc == 0
    assert out.splitlines()[:6] == [
        "2026-11-19 signs=0 empty_failures=5 other_failures=1",
        "2026-11-20 signs=1 empty_failures=6 other_failures=0",
        "2026-11-21 signs=0 empty_failures=0 other_failures=0",
        "clean_run=1",
        "other_outcomes: bad-signature=1",
        "source sign-failures: 12 failed signs from 20 attempt files (attempts grouped by pid; 0 skipped)",
    ]
    assert sorted(p.name for p in kd.rglob("*")) == before       # nothing written under the key dir


def test_clean_run_is_signs_since_the_last_empty_failure(tmp_path):
    kd = _key_dir(tmp_path)
    ledger = _ledger(tmp_path, "2026-11-20T16:00:00+00:00", "2026-11-20T18:00:00+00:00", "2026-11-20T19:00:00+00:00")
    res = ss.tally(kd, ledger, 2, datetime.date(2026, 11, 20))
    assert res["clean_run"] == 2                                  # the 16:00 one precedes the later empty failures
    assert [d["signs"] for d in res["days"]] == [0, 3]


def test_sign_log_outcomes_counted_by_name(tmp_path, capsys):
    kd = tmp_path / "keys"
    kd.mkdir()
    lines = [{"time": "2026-11-21T10:0%d:00+00:00" % i, "outcome": o}
             for i, o in enumerate(("signed", "no-signature", "wat-new", "wat-new"))]
    (kd / "sign-log.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    rc, out = _run(capsys, "--key-dir", str(kd), "--ledger", str(tmp_path / "none.jsonl"), "--days", "1",
                   "--until", "2026-11-21")
    assert rc == 0
    assert out.splitlines()[0] == "2026-11-21 signs=1 empty_failures=1 other_failures=2"
    assert "other_outcomes: wat-new=2" in out
    assert "clean_run=0" in out                                   # the empty failure came after the one sign
    assert "source ledger: no data" in out


def test_missing_key_dir_is_no_data_exit_zero(tmp_path, capsys):
    rc, out = _run(capsys, "--key-dir", str(tmp_path / "absent"), "--ledger", str(tmp_path / "none.jsonl"),
                   "--days", "1", "--until", "2026-11-21")
    assert rc == 0
    assert out.splitlines()[0] == "2026-11-21 signs=? empty_failures=0 other_failures=0"
    assert "clean_run=?" in out
    assert "source sign-failures: no data" in out
    assert not (tmp_path / "absent").exists()
