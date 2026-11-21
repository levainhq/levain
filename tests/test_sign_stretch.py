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
    # Expected values derived from the fixture files: every one of the 11 signs with an empty-reason attempt counts as
    # signs_with_empty (5 on 11-19 + 6 on 11-20, a retry rescuing one would not change that); the one record with an
    # armored non-empty body and no reason (...141050, 11-19) is the one other failure; the ledger holds one statement
    # (11-20 17:30, after the newest sign-with-empty at 16:52), so signs>= is 0/1/0 and clean_run>=1.
    kd = _key_dir(tmp_path)
    ledger = _ledger(tmp_path, "2026-11-20T17:30:00+00:00")
    before = sorted(p.name for p in kd.rglob("*"))
    rc, out = _run(capsys, "--key-dir", str(kd), "--ledger", str(ledger), "--days", "3", "--until", "2026-11-21")
    assert rc == 0
    assert out.splitlines()[:5] == [
        "2026-11-19 signs>=0 signs_with_empty=5 other_failures=1",
        "2026-11-20 signs>=1 signs_with_empty=6 other_failures=0",
        "2026-11-21 signs>=0 signs_with_empty=0 other_failures=0",
        "clean_run>=1",
        "other_outcomes: bad-signature=1",
    ]
    assert ("source sign-failures: 12 failed signs from 20 attempt files (attempts grouped by pid); "
            "0 files skipped as unreadable/corrupt; 0 helper-path files skipped because the sign-log holds them") in out
    assert ("source note: Apple-path cancels, timeouts and verifier errors write no evidence file, so other_failures "
            "counts only refused signatures.") in out
    assert sorted(p.name for p in kd.rglob("*")) == before       # nothing written under the key dir


def test_clean_run_is_signs_since_the_last_sign_with_empty(tmp_path):
    kd = _key_dir(tmp_path)
    ledger = _ledger(tmp_path, "2026-11-20T16:00:00+00:00", "2026-11-20T18:00:00+00:00", "2026-11-20T19:00:00+00:00")
    res = ss.tally(kd, ledger, 2, datetime.date(2026, 11, 20))
    assert res["clean_run"] == 2                                  # the 16:00 one precedes the later empty answers
    assert [d["signs"] for d in res["days"]] == [0, 3] and all(d["signs_lower_bound"] for d in res["days"])


def test_sign_log_only_key_dir_is_exact_without_a_ledger(tmp_path, capsys):
    kd = tmp_path / "keys"
    kd.mkdir()
    lines = [{"time": "2026-11-20T10:00:00+00:00", "outcome": "signed"}]          # T0: the 21st is wholly after it
    lines += [{"time": "2026-11-21T10:0%d:00+00:00" % i, "outcome": o}
              for i, o in enumerate(("signed", "signed", "no-signature", "wat-new", "wat-new", "signed"))]
    (kd / "sign-log.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    rc, out = _run(capsys, "--key-dir", str(kd), "--days", "1", "--until", "2026-11-21")
    assert rc == 0
    assert out.splitlines()[:2] == ["2026-11-21 signs=3 signs_with_empty=1 other_failures=2", "clean_run=1"]
    assert "other_outcomes: wat-new=2" in out
    assert "source ledger: no data" in out and "switch T0" in out


def test_sign_log_with_earlier_days_and_no_ledger_is_unknown_before_t0(tmp_path):
    kd = tmp_path / "keys"
    kd.mkdir()
    (kd / "sign-log.jsonl").write_text(json.dumps({"time": "2026-11-21T10:00:00+00:00", "outcome": "signed"}) + "\n")
    res = ss.tally(kd, None, 3, datetime.date(2026, 11, 22))
    # 11-20 is wholly before T0 and 11-21 starts before it: Apple-path territory, unknown, never 0; 11-22 is exact
    assert [d["signs"] for d in res["days"]] == [None, None, 0]
    assert res["clean_run"] is None                               # the run starts on a day with Apple-path time in it


def test_ledger_and_sign_log_overlap_counts_each_sign_once(tmp_path):
    kd = tmp_path / "keys"
    kd.mkdir()
    (kd / "sign-log.jsonl").write_text("".join(json.dumps({"time": t, "outcome": "signed"}) + "\n" for t in
                                               ("2026-11-21T10:00:00+00:00", "2026-11-21T11:00:00+00:00")))
    # one statement on the 20th and one at 09:00 on the 21st (both before T0 = 10:00); two more that the sign-log also holds
    ledger = _ledger(tmp_path, "2026-11-20T12:00:00+00:00", "2026-11-21T09:00:00+00:00",
                     "2026-11-21T10:00:01+00:00", "2026-11-21T11:00:01+00:00")
    res = ss.tally(kd, ledger, 2, datetime.date(2026, 11, 21))
    assert [d["signs"] for d in res["days"]] == [1, 3]            # 21st: 09:00 + the two logged, not the two ledger twins
    assert [d["signs_lower_bound"] for d in res["days"]] == [True, True]   # the 21st starts before T0
    assert res["clean_run"] == 4 and res["clean_run_lower_bound"] is True
    assert "2 from T0 on not counted" in res["sources"]["ledger"]
    res2 = ss.tally(kd, ledger, 1, datetime.date(2026, 11, 22))   # a day wholly after T0: exact, from the log alone
    assert res2["days"][0]["signs"] == 0 and res2["days"][0]["signs_lower_bound"] is False


def test_no_ledger_with_apple_path_files_is_unknown(tmp_path, capsys):
    kd = _key_dir(tmp_path)
    rc, out = _run(capsys, "--key-dir", str(kd), "--days", "2", "--until", "2026-11-20")
    assert rc == 0
    assert out.splitlines()[:3] == ["2026-11-19 signs=? signs_with_empty=5 other_failures=1",
                                    "2026-11-20 signs=? signs_with_empty=6 other_failures=0", "clean_run=?"]
    res = ss.tally(kd, None, 2, datetime.date(2026, 11, 20))
    assert res["clean_run"] is None and res["days"][0]["signs"] is None


def test_corrupt_sigfail_marks_clean_run_unverified(tmp_path, capsys):
    kd = _key_dir(tmp_path)
    (kd / "sign-failures" / "sigfail-20261120T170000000000-0000000001-1.json").write_text("{not json")
    ledger = _ledger(tmp_path, "2026-11-20T17:30:00+00:00")
    rc, out = _run(capsys, "--key-dir", str(kd), "--ledger", str(ledger), "--days", "1", "--until", "2026-11-20")
    assert "clean_run>=1 (unverified: 1 unreadable files)" in out
    assert "1 files skipped as unreadable/corrupt" in out
    assert ss.tally(kd, ledger, 1, datetime.date(2026, 11, 20))["clean_run_unverified"] == 1


def test_non_finite_and_huge_times_are_skipped_not_a_crash(tmp_path):
    kd = _key_dir(tmp_path)
    ledger = tmp_path / "statements.jsonl"
    good = {"event": "statement", "signed": {"format": "x"},
            "ts": datetime.datetime(2026, 11, 20, 17, 30, tzinfo=datetime.timezone.utc).timestamp()}
    rows = [json.dumps(good)] + ['{"event":"statement","signed":{"a":1},"ts":%s}' % v
                                 for v in ("NaN", "Infinity", "-1", "1e20")]
    ledger.write_text("\n".join(rows) + "\n")
    (kd / "sign-log.jsonl").write_text('{"time":"0001-01-01T00:00:00","outcome":"signed"}\n'
                                       '{"time":"9999-12-31T23:59:59+00:00","outcome":"signed"}\n')
    res = ss.tally(kd, ledger, 2, datetime.date(2026, 11, 20))
    assert res["clean_run_unverified"] == 4 + 2                   # four bad ledger ts, two bad sign-log times
    assert "0 files, 6 records skipped" in res["sources"]["unreadable"]


def test_days_is_bounded(tmp_path):
    for bad in ("0", "367"):
        with pytest.raises(SystemExit):
            ss.main(["--key-dir", str(tmp_path), "--days", bad])


def test_day_buckets_follow_the_local_zone(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    kd = tmp_path / "keys"
    kd.mkdir()
    ledger = _ledger(tmp_path, "2026-11-20T23:30:00-05:00")       # 04:30Z on the 21st, still the 20th in New York
    res = ss.tally(kd, ledger, 2, datetime.date(2026, 11, 21))
    assert [d["signs"] for d in res["days"]] == [1, 0]


def test_missing_key_dir_is_no_data_exit_zero(tmp_path, capsys):
    rc, out = _run(capsys, "--key-dir", str(tmp_path / "absent"), "--ledger", str(tmp_path / "none.jsonl"),
                   "--days", "1", "--until", "2026-11-21")
    assert rc == 0
    assert out.splitlines()[0] == "2026-11-21 signs=? signs_with_empty=0 other_failures=0"
    assert "clean_run=?" in out
    assert "source sign-failures: no data" in out
    assert not (tmp_path / "absent").exists()


def test_no_ledger_named_means_signs_unknown_never_a_guessed_path(tmp_path, monkeypatch):
    monkeypatch.delenv("NEEDS_PHILL_STATEMENTS", raising=False)
    assert ss.default_ledger() is None
    result = ss.tally(tmp_path / "keys", None, 3)
    assert "not given" in result["sources"]["ledger"]
