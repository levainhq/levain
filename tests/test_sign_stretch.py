"""scripts/sign_stretch.py (levain#208): each source reports only what it alone records; no cross-source number.
The real sigfail records of levain#191 (tests/fixtures/sign_stretch, copied from project_memory/1120-58_l191) are
replayed. Always a tmp key dir."""
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


def _key_dir(tmp_path, fixtures=True):
    kd = tmp_path / "keys"
    if fixtures:
        shutil.copytree(FIXTURES, kd / "sign-failures")
    else:
        kd.mkdir()
    return kd


def _sign_log(kd, *pairs):
    (kd / "sign-log.jsonl").write_text("".join(json.dumps({"time": t, "outcome": o}) + "\n" for t, o in pairs))


def _ledger(tmp_path, *isos, extra=()):
    p = tmp_path / "statements.jsonl"
    rows = [json.dumps({"event": "statement", "id": None, "signed": {"format": "x"},
                        "ts": datetime.datetime.fromisoformat(i).timestamp()}) for i in isos]
    p.write_text("".join(r + "\n" for r in list(rows) + list(extra)))
    return p


def _run(capsys, *argv):
    rc = ss.main(list(argv))
    return rc, capsys.readouterr().out


def test_real_sigfail_records_apple_columns_per_day(tmp_path, capsys):
    # 11 signs with an empty-reason attempt (5 on 11-19, 6 on 11-20); the one record with an armored non-empty body and
    # no reason (...141050, 11-19) is the one other failure. No sign-log: `helper none`, helper_clean_run=none.
    kd = _key_dir(tmp_path)
    before = sorted(p.name for p in kd.rglob("*"))
    rc, out = _run(capsys, "--key-dir", str(kd), "--days", "3", "--until", "2026-11-21")
    assert rc == 0
    lines = out.splitlines()
    assert lines[:3] == [
        "2026-11-19 helper none | apple signs_with_empty=5 other=1",
        "2026-11-20 helper none | apple signs_with_empty=6 other=0",
        "2026-11-21 helper none | apple signs_with_empty=0 other=0",
    ]
    assert lines[3] == "helper_clean_run=none"
    assert lines[4].startswith("apple_last_empty=2026-11-20T16:52:")
    assert ("source sign-failures: 12 failed signs from 20 attempt files (attempts grouped by pid); "
            "0 files skipped as unreadable/corrupt; 0 helper-written files skipped because the sign-log carries "
            "those signs") in out
    assert "source sign-log: no data" in out
    assert "ledger" not in "".join(lines[:3])
    assert sorted(p.name for p in kd.rglob("*")) == before       # nothing written under the key dir


def test_sign_log_out_of_order_counts_clean_run_and_names_the_ender(tmp_path, capsys):
    kd = _key_dir(tmp_path, fixtures=False)
    _sign_log(kd,
              ("2026-11-21T10:05:00+00:00", "signed"),
              ("2026-11-21T10:00:00+00:00", "presence-failed"),
              ("2026-11-21T10:04:00+00:00", "cancelled"),         # a cancel does not end the run
              ("2026-11-21T10:01:00+00:00", "signed"),
              ("2026-11-21T10:03:00+00:00", "no-signature"),      # the newest failure
              ("2026-11-21T10:06:00+00:00", "signed"),
              ("2026-11-20T09:00:00+00:00", "signed"))
    rc, out = _run(capsys, "--key-dir", str(kd), "--days", "3", "--until", "2026-11-21")
    lines = out.splitlines()
    assert lines[:3] == [
        "2026-11-19 apple signs_with_empty=0 other=0",                  # no sign-log line that day: helper part omitted
        "2026-11-20 helper signs=1 failures=none | apple signs_with_empty=0 other=0",
        "2026-11-21 helper signs=3 failures=cancelled:1,no-signature:1,presence-failed:1 | apple signs_with_empty=0 other=0",
    ]
    assert lines[3] == "helper_clean_run=2 (the run ended at a no-signature line, 2026-11-21T10:03:00+00:00)"
    assert lines[4] == "apple_last_empty=none"
    assert "source sign-log: 7 lines usable of 7; 0 skipped" in out


def test_cancel_alone_never_breaks_the_run(tmp_path):
    kd = _key_dir(tmp_path, fixtures=False)
    _sign_log(kd, ("2026-11-21T10:00:00+00:00", "signed"), ("2026-11-21T10:01:00+00:00", "cancelled"),
              ("2026-11-21T10:02:00+00:00", "signed"))
    res = ss.tally(kd, None, 1, datetime.date(2026, 11, 21))
    assert res["helper_clean_run"] == 2 and res["helper_run_ended_by"] is None
    assert "no failure line" in ss.render(res)


def test_equal_timestamp_failure_is_newer_than_a_signed_line(tmp_path):
    kd = _key_dir(tmp_path, fixtures=False)
    _sign_log(kd, ("2026-11-21T10:00:00+00:00", "signed"), ("2026-11-21T10:00:00+00:00", "no-signature"),
              ("2026-11-21T09:00:00+00:00", "signed"))
    res = ss.tally(kd, None, 1, datetime.date(2026, 11, 21))
    assert res["helper_clean_run"] == 0                            # the failure sorts after the tied signed line
    assert res["helper_run_ended_by"]["outcome"] == "no-signature"
    # and in the file's other order, too
    _sign_log(kd, ("2026-11-21T10:00:00+00:00", "no-signature"), ("2026-11-21T10:00:00+00:00", "signed"))
    assert ss.tally(kd, None, 1, datetime.date(2026, 11, 21))["helper_clean_run"] == 0


def test_ledger_counts_only_non_empty_signed_statements(tmp_path, capsys):
    kd = _key_dir(tmp_path, fixtures=False)
    extra = ['{"event":"statement","signed":{},"ts":%s}' % datetime.datetime(2026, 11, 21, 12, tzinfo=datetime.timezone.utc).timestamp(),
             "{not json",
             json.dumps({"event": "other", "signed": {"a": 1}, "ts": 1.0})]    # another event: neither counted nor unreadable
    ledger = _ledger(tmp_path, "2026-11-21T10:00:00+00:00", "2026-11-21T11:00:00+00:00", extra=extra)
    rc, out = _run(capsys, "--key-dir", str(kd), "--ledger", str(ledger), "--days", "1", "--until", "2026-11-21")
    assert out.splitlines()[0] == "2026-11-21 helper none | apple signs_with_empty=0 other=0 | ledger statements>=2"
    assert "source ledger: 2 statements counted" in out and "2 lines skipped as unreadable/corrupt" in out
    assert "every line is unreadable" not in out


def test_ledger_with_every_line_unreadable_says_so(tmp_path, capsys):
    kd = _key_dir(tmp_path, fixtures=False)
    ledger = tmp_path / "statements.jsonl"
    ledger.write_text('{bad\n{"event":"statement","signed":{},"ts":1}\n[1]\n')
    _, out = _run(capsys, "--key-dir", str(kd), "--ledger", str(ledger), "--days", "1", "--until", "2026-11-21")
    assert "3 lines skipped as unreadable/corrupt; every line is unreadable" in out
    assert "ledger statements>=0" in out.splitlines()[0]


def test_non_finite_and_huge_times_are_unreadable_not_a_crash(tmp_path):
    kd = _key_dir(tmp_path, fixtures=False)
    rows = ['{"event":"statement","signed":{"a":1},"ts":%s}' % v for v in ("NaN", "Infinity", "-1", "1e20")]
    ledger = tmp_path / "statements.jsonl"
    ledger.write_text("\n".join(rows) + "\n")
    (kd / "sign-log.jsonl").write_text('{"time":"0001-01-01T00:00:00","outcome":"signed"}\n'
                                       '{"time":"9999-12-31T23:59:59+00:00","outcome":"signed"}\n')
    res = ss.tally(kd, ledger, 2, datetime.date(2026, 11, 20))
    assert "4 lines skipped as unreadable/corrupt; every line is unreadable" in res["sources"]["ledger"]
    assert "0 lines usable of 2; 2 skipped" in res["sources"]["sign-log"]


def test_helper_written_sigfail_files_are_skipped_and_counted(tmp_path):
    kd = _key_dir(tmp_path, fixtures=False)
    d = kd / "sign-failures"
    d.mkdir()
    (d / "sigfail-20261121T100000000000-0000000007-1.json").write_text(
        json.dumps({"time": "2026-11-21T10:00:00+00:00", "attempt": 1, "reason": "empty", "signer": "levain-sign"}))
    res = ss.tally(kd, None, 1, datetime.date(2026, 11, 21))
    assert res["days"][0]["apple_signs_with_empty"] == 0 and res["apple_last_empty"] is None
    assert "1 helper-written files skipped" in res["sources"]["sign-failures"]


def test_sign_failures_that_is_not_a_readable_directory_is_said(tmp_path):
    kd = _key_dir(tmp_path, fixtures=False)
    (kd / "sign-failures").write_text("a file, not a directory")
    res = ss.tally(kd, None, 1, datetime.date(2026, 11, 21))
    assert res["sources"]["sign-failures"].startswith("unreadable: ") and "not a readable directory" in res["sources"]["sign-failures"]


def test_days_is_bounded(tmp_path):
    for bad in ("0", "367"):
        with pytest.raises(SystemExit):
            ss.main(["--key-dir", str(tmp_path), "--days", bad])


def test_day_buckets_follow_the_local_zone(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    kd = _key_dir(tmp_path, fixtures=False)
    ledger = _ledger(tmp_path, "2026-11-20T23:30:00-05:00")       # 04:30Z on the 21st, still the 20th in New York
    res = ss.tally(kd, ledger, 2, datetime.date(2026, 11, 21))
    assert [d["ledger_statements_at_least"] for d in res["days"]] == [1, 0]


def test_missing_key_dir_is_no_data_exit_zero(tmp_path, capsys):
    rc, out = _run(capsys, "--key-dir", str(tmp_path / "absent"), "--days", "1", "--until", "2026-11-21")
    assert rc == 0
    assert out.splitlines()[0] == "2026-11-21 helper none | apple signs_with_empty=0 other=0"
    assert "helper_clean_run=none" in out and "source sign-failures: no data" in out
    assert not (tmp_path / "absent").exists()


def test_no_ledger_named_means_no_ledger_part_never_a_guessed_path(tmp_path, monkeypatch):
    monkeypatch.delenv("NEEDS_PHILL_STATEMENTS", raising=False)
    assert ss.default_ledger() is None
    result = ss.tally(tmp_path / "keys", None, 3)
    assert "ledger" not in result["sources"] and all("ledger_statements_at_least" not in r for r in result["days"])
