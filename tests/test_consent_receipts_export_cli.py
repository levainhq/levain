"""Q19 R9: ``levain consent receipts export --format agentboundary [--out FILE]``."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain import cli
from levain.consent import agentboundary as ab
from tests.test_consent_agentboundary import build_scratch

FIXED = datetime.now(timezone.utc) + timedelta(days=60)


@pytest.fixture(autouse=True)
def _far_future_real_clock(monkeypatch):
    monkeypatch.setattr(ab, "_utcnow", lambda: FIXED)


@pytest.fixture()
def queue_dir(tmp_path):
    queue, _ids = build_scratch(tmp_path)
    return queue.directory


def test_stdout_is_the_exporters_receipts(queue_dir, capsys):
    rc = cli.main(["consent", "receipts", "export", "--format", "agentboundary", "--queue-dir", str(queue_dir)])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out.endswith("\n") and not out.endswith("\n\n")
    assert json.loads(out) == ab.export(queue_dir).receipts
    assert len(json.loads(out)) > 5
    assert "receipts" in err      # the summary is on stderr


def test_out_file_matches_and_stdout_is_empty(queue_dir, tmp_path, capsys):
    target = tmp_path / "o" / "r.json"
    target.parent.mkdir()
    rc = cli.main(["consent", "receipts", "export", "--format", "agentboundary", "--queue-dir", str(queue_dir),
                   "--out", str(target)])
    out, err = capsys.readouterr()
    assert rc == 0 and out == ""
    text = target.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.loads(text) == ab.export(queue_dir).receipts
    assert text == json.dumps(ab.export(queue_dir).receipts, ensure_ascii=False) + "\n"
    assert [p.name for p in target.parent.iterdir()] == ["r.json"]       # no temp file left
    assert "receipts" in err


def test_out_refuses_to_overwrite(queue_dir, tmp_path, capsys):
    target = tmp_path / "r.json"
    target.write_text("keep", encoding="utf-8")
    rc = cli.main(["consent", "receipts", "export", "--format", "agentboundary", "--queue-dir", str(queue_dir),
                   "--out", str(target)])
    out, err = capsys.readouterr()
    assert rc != 0 and out == "" and "exists" in err
    assert target.read_text(encoding="utf-8") == "keep"


def test_not_a_queue_fails_on_stderr(tmp_path, capsys):
    rc = cli.main(["consent", "receipts", "export", "--format", "agentboundary", "--queue-dir", str(tmp_path)])
    out, err = capsys.readouterr()
    assert rc != 0 and out == "" and "levain consent receipts export:" in err


def test_unreadable_store_fails_on_stderr(tmp_path, capsys):
    from levain.consent.queue import DB_NAME
    (tmp_path / DB_NAME).write_bytes(b"this is not sqlite" * 50)
    rc = cli.main(["consent", "receipts", "export", "--format", "agentboundary", "--queue-dir", str(tmp_path)])
    out, err = capsys.readouterr()
    assert rc != 0 and out == "" and "levain consent receipts export:" in err


def test_format_is_required_and_closed(queue_dir, capsys):
    for argv in (["--queue-dir", str(queue_dir)], ["--format", "other", "--queue-dir", str(queue_dir)]):
        with pytest.raises(SystemExit) as e:
            cli.main(["consent", "receipts", "export", *argv])
        assert e.value.code == 2
    assert capsys.readouterr().out == ""
