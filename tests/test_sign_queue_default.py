"""The sign queue a dispatch config verb finds without a flag (1.0 defect): a standard install, where init writes no
``sign_queue_dir``, derives it from the cockpit that serves the hook queue; a stale named dir refuses with the fix."""
from __future__ import annotations

import argparse
import json

import pytest

from levain.consent import answer_countersign as ac
from levain.consent.cockpit_origin import write_origin
from levain.dispatch import cli as dcli
from levain.dispatch.cli import InputRefused


def _hook_queue(tmp_path, settings: dict | None):
    home = tmp_path / "lh"
    q = home / "consent"
    q.mkdir(parents=True)
    q.chmod(0o700)
    if settings is not None:
        (q / "claude_code.json").write_text(json.dumps(settings), encoding="utf-8")
    return home, q


def _args():
    return argparse.Namespace(sign_queue_dir=None)


def test_a_served_hook_queue_is_found_with_no_flag(tmp_path, monkeypatch):
    home, q = _hook_queue(tmp_path, {"cockpit_data_root": str(tmp_path / "dead"), "away_word": "auto"})
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.delenv("LEVAIN_SIGN_QUEUE_DIR", raising=False)
    assert ac.sign_queue_dir(__import__("os").environ) is None          # nothing published yet: nothing guessed
    write_origin(q, "https://cockpit.example.test")                       # `levain serve --sign-queue-dir <q>` started
    assert dcli._sign_queue(_args()) == q.resolve()


def test_a_named_key_outranks_the_derived_queue(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    home, q = _hook_queue(tmp_path, {"sign_queue_dir": str(other)})
    write_origin(q, "https://cockpit.example.test")
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.delenv("LEVAIN_SIGN_QUEUE_DIR", raising=False)
    assert dcli._sign_queue(_args()) == other


def test_nothing_named_nothing_served_refuses_with_the_path_and_the_flag(tmp_path, monkeypatch):
    home, q = _hook_queue(tmp_path, {"away_word": "auto"})
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.delenv("LEVAIN_SIGN_QUEUE_DIR", raising=False)
    with pytest.raises(InputRefused) as e:
        dcli._sign_queue(_args())
    assert "--sign-queue-dir" in str(e.value) and str(q.resolve() / "claude_code.json") in str(e.value)


def test_a_stale_named_dir_refuses_naming_it_and_the_fix(tmp_path, monkeypatch):
    gone = tmp_path / "gone" / "signq"
    home, q = _hook_queue(tmp_path, {"sign_queue_dir": str(gone)})
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.delenv("LEVAIN_SIGN_QUEUE_DIR", raising=False)
    with pytest.raises(InputRefused) as e:
        dcli._sign_queue(_args())
    msg = str(e.value)
    assert str(gone) in msg and "does not exist" in msg and "--sign-queue-dir" in msg
