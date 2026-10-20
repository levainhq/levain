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


# --- L3 r1 (1019+49): stdout encoding, a closed pipe, a filesystem with no hard links, a bad ~user -----------------
_ARGS = ["consent", "receipts", "export", "--format", "agentboundary", "--queue-dir"]


def test_stdout_is_utf8_json_whatever_the_stream_encoding(tmp_path, monkeypatch, capsys):
    import io

    root = tmp_path / "café"                          # every receipt's target.resource_id is its working directory
    root.mkdir()
    queue_dir = build_scratch(root)[0].directory

    raw = io.BytesIO()
    ascii_out = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
    monkeypatch.setattr("sys.stdout", ascii_out)
    rc = cli.main(_ARGS + [str(queue_dir)])
    ascii_out.flush()
    assert rc == 0
    got = raw.getvalue().decode("utf-8")
    assert json.loads(got) == ab.export(queue_dir).receipts
    assert any(ord(c) > 127 for c in got)             # the scratch queue holds non-ASCII text, so this is exercised


def test_a_closed_pipe_on_stdout_is_a_quiet_exit_1(queue_dir, monkeypatch, capsys):
    import io
    import os

    r, w = os.pipe()
    os.close(r)
    piped = io.TextIOWrapper(io.FileIO(w, "wb"), encoding="utf-8")
    monkeypatch.setattr("sys.stdout", piped)
    assert cli.main(_ARGS + [str(queue_dir)]) == 1
    piped.close()


def test_out_on_a_filesystem_with_no_hard_links_still_writes_and_never_clobbers(queue_dir, tmp_path, monkeypatch,
                                                                               capsys):
    import errno
    import os

    def no_links(src, dst):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    target = tmp_path / "r.json"
    rc = cli.main(_ARGS + [str(queue_dir), "--out", str(target)])
    assert rc == 0
    assert json.loads(target.read_text(encoding="utf-8")) == ab.export(queue_dir).receipts
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
    capsys.readouterr()
    assert cli.main(_ARGS + [str(queue_dir), "--out", str(target)]) == 1      # exists: refused, kept
    assert json.loads(target.read_text(encoding="utf-8")) == ab.export(queue_dir).receipts


def test_out_naming_a_user_that_does_not_exist_is_a_clean_error(queue_dir, capsys):
    rc = cli.main(_ARGS + [str(queue_dir), "--out", "~levain_user_that_does_not_exist_1019/r.json"])
    out, err = capsys.readouterr()
    assert rc == 1 and out == "" and err.startswith("levain consent receipts export:")
