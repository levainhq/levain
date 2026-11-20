"""levain#208: the sign-failure counter's output shape, from real sigfail records (levain#191, copied from the key's
sign-failures directory) plus a record that carries a good signature (the 11-18 one from sk_empty_signatures_1119.json).
Nothing here touches ~/.levain: the directory is tmp_path."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from levain import cli
from levain import sign_stretch

FIX = Path(__file__).parent / "fixtures"


def _dir(tmp_path: Path) -> Path:
    d = tmp_path / "keys" / "sign-failures"
    d.mkdir(parents=True)
    for f in sorted((FIX / "sign_failures_1120").glob("sigfail-*.json")):
        (d / f.name).write_bytes(f.read_bytes())
    good = json.loads((FIX / "sk_empty_signatures_1119.json").read_text())["good"]
    rec = json.loads(next((FIX / "sign_failures_1120").glob("*-1.json")).read_text())
    rec.update(time="2026-11-20T10:00:00+00:00", signature=good, reason=None)
    (d / "sigfail-20261120T100000000000-0000000001-1.json").write_text(json.dumps(rec))
    (d / "sigdiag-last.txt").write_text("not a record")
    return d


def test_table_from_real_records_and_a_good_signature(tmp_path, capsys):
    d = _dir(tmp_path)
    before = sorted((p.name, p.stat().st_mtime_ns) for p in d.iterdir())
    rep = sign_stretch.read(d)
    days = rep.days
    assert rep.unreadable == 0
    assert [(x.day, x.empty_failures, x.failed_signs, x.other_failures) for x in days] == [
        ("2026-11-19", 2, 1, 0),      # one sign, empty at attempts 1 and 2
        ("2026-11-20", 1, 2, 1),      # the 00:19Z empty one, and the good-signature record
    ]
    out = sign_stretch.render(rep)
    na = sign_stretch.NO_SUCCESS_LEDGER
    assert out.splitlines() == [
        f"2026-11-19  signs={na} empty_failures=2 failed_signs=1 other_failures=0",
        f"2026-11-20  signs={na} empty_failures=1 failed_signs=2 other_failures=1",
        f"clean_run={na}",
    ]
    assert cli.main(["cockpit", "sign-stretch", "--dir", str(tmp_path / "keys")]) == 0
    assert capsys.readouterr().out.strip() == out
    assert before == sorted((p.name, p.stat().st_mtime_ns) for p in d.iterdir())    # read-only


def test_missing_directory_is_no_days(tmp_path):
    assert sign_stretch.render(sign_stretch.read(tmp_path / "none")).splitlines() == [
        "no sign-failure records", f"clean_run={sign_stretch.NO_SUCCESS_LEDGER}"]


def test_statement_ledger_gives_signs_and_clean_run(tmp_path, capsys):
    _dir(tmp_path)
    ts = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    ledger = tmp_path / "statements.jsonl"
    good = lambda t: json.dumps({"event": "statement", "ts": ts(t), "signed": {"sig": "x"}})
    ledger.write_text("\n".join([
        good("2026-11-19T16:30:00"),                                 # before the last empty failure
        good("2026-11-20T00:30:00"),                                 # after it (00:19Z)
        json.dumps({"event": "countersign", "ts": ts("2026-11-20T01:00:00")}),   # not a statement: ignored
    ]) + "\n")
    assert cli.main(["cockpit", "sign-stretch", "--dir", str(tmp_path / "keys"), "--statements", str(ledger)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "2026-11-19  signs=1 empty_failures=2 failed_signs=1 other_failures=0",
        "2026-11-20  signs=1 empty_failures=1 failed_signs=2 other_failures=1",
        "clean_run=1",
    ]


def test_bad_ledger_lines_are_skipped_and_counted(tmp_path, capsys):
    _dir(tmp_path)
    ledger = tmp_path / "statements.jsonl"
    line = lambda ts: '{"event": "statement", "signed": {"sig": "x"}, "ts": %s}' % ts
    ledger.write_text("\n".join([line("1e20"), line('"1795000000"'), line("true"), line("NaN"), line("Infinity"),
                                 line("1795000000")]) + "\n")      # the last is a 2026-11-18 sign
    assert cli.main(["cockpit", "sign-stretch", "--dir", str(tmp_path / "keys"), "--statements", str(ledger)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "2026-11-18  signs=1 empty_failures=0 failed_signs=0 other_failures=0" in out
    assert "note: 5 unreadable record(s) skipped" in out
    assert out[-1] == "clean_run=0"       # that good sign is before the latest empty failure


def test_non_utf8_ledger_is_refused(tmp_path, capsys):
    _dir(tmp_path)
    ledger = tmp_path / "statements.jsonl"
    ledger.write_bytes(b"\xff\xfe\x00bad\n")
    assert cli.main(["cockpit", "sign-stretch", "--dir", str(tmp_path / "keys"), "--statements", str(ledger)]) == 1
    assert "sign-stretch: cannot read" in capsys.readouterr().err


def test_missing_dir_is_refused_but_no_subdirectory_is_clean(tmp_path, capsys):
    assert cli.main(["cockpit", "sign-stretch", "--dir", str(tmp_path / "nope")]) == 1
    assert "sign-stretch: no such directory" in capsys.readouterr().err
    (tmp_path / "keys").mkdir()
    assert cli.main(["cockpit", "sign-stretch", "--dir", str(tmp_path / "keys")]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "no sign-failure records"


def _ledger(tmp_path, *stamps):
    ts = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    p = tmp_path / "statements.jsonl"
    p.write_text("\n".join(json.dumps({"event": "statement", "ts": ts(t), "signed": {"sig": "x"}}) for t in stamps) + "\n")
    return p


def _run(tmp_path, *extra):
    return cli.main(["cockpit", "sign-stretch", "--dir", str(tmp_path / "keys"), *extra])


def test_unlistable_evidence_dir_is_refused(tmp_path, capsys):
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "sign-failures").write_text("a file, not a directory")
    assert _run(tmp_path) == 1
    err = capsys.readouterr().err
    assert f"cannot read {tmp_path / 'keys' / 'sign-failures'}" in err and "None" not in err


def test_unreadable_failure_record_makes_clean_run_unverified(tmp_path, capsys):
    d = _dir(tmp_path)
    (d / "sigfail-20261120T120000000000-0000000002-1.json").write_text("{not json")
    (d / "sigfail-20261120T120100000000-0000000003-1.json").write_text(json.dumps({"time": "2026-11-20T12:01:00+00:00"}))
    _run(tmp_path, "--statements", str(_ledger(tmp_path, "2026-11-20T01:00:00")))
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "clean_run=1 (unverified: 2 unreadable failure records)"


def test_directory_at_its_cap_makes_clean_run_an_upper_bound(tmp_path, capsys, monkeypatch):
    import levain.cockpit.keys as keys
    d = _dir(tmp_path)
    monkeypatch.setattr(keys, "_SIGN_FAILURE_KEEP", len(list(d.glob("sigfail-*.json"))))
    _run(tmp_path, "--statements", str(_ledger(tmp_path, "2026-11-20T01:00:00")))
    assert capsys.readouterr().out.splitlines()[-1] == "clean_run=1 (at most: the evidence directory is at its cap)"


def test_naive_time_is_read_as_utc(tmp_path, capsys):
    d = tmp_path / "keys" / "sign-failures"
    d.mkdir(parents=True)
    rec = json.loads(next((FIX / "sign_failures_1120").glob("*-1.json")).read_text())
    rec["time"] = "2026-11-20T23:30:00"
    (d / "sigfail-20261120T233000000000-0000000004-1.json").write_text(json.dumps(rec))
    _run(tmp_path)
    assert capsys.readouterr().out.splitlines()[0].startswith("2026-11-20  ")


def test_signed_must_be_a_non_empty_object(tmp_path, capsys):
    _dir(tmp_path)
    p = tmp_path / "statements.jsonl"
    p.write_text("\n".join(json.dumps({"event": "statement", "ts": 1795000000, "signed": s})
                           for s in ("yes", [1], {}, None, {"sig": "x"})) + "\n")
    _run(tmp_path, "--statements", str(p))
    assert "2026-11-18  signs=1 empty_failures=0 failed_signs=0 other_failures=0" in capsys.readouterr().out


def test_missing_default_key_dir_is_refused(tmp_path, capsys, monkeypatch):
    import levain.cockpit.custody as custody
    monkeypatch.setattr(custody, "default_key_dir", lambda: tmp_path / "fresh" / "keys")
    assert cli.main(["cockpit", "sign-stretch"]) == 1
    assert "sign-stretch: no such directory" in capsys.readouterr().err
