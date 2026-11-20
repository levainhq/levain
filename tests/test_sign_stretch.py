"""levain#208: the sign-failure counter's output shape, from real sigfail records (levain#191, copied from the key's
sign-failures directory) plus a record that carries a good signature (the 11-18 one from sk_empty_signatures_1119.json).
Nothing here touches ~/.levain: the directory is tmp_path."""
from __future__ import annotations

import json
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
    days, bad = sign_stretch.read_days(d)
    assert bad == 0
    assert [(x.day, x.empty_failures, x.failed_signs, x.other_failures) for x in days] == [
        ("2026-11-19", 2, 1, 0),      # one sign, empty at attempts 1 and 2
        ("2026-11-20", 1, 2, 1),      # the 00:19Z empty one, and the good-signature record
    ]
    out = sign_stretch.render(days)
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
    assert sign_stretch.render(*sign_stretch.read_days(tmp_path / "none")).splitlines() == [
        "no sign-failure records", f"clean_run={sign_stretch.NO_SUCCESS_LEDGER}"]
