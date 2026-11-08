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


# --- the 11-08 head rulings on the L3 round (Q1-Q4) -------------------------------------------------------------------

def _env(monkeypatch, home):
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.delenv("LEVAIN_SIGN_QUEUE_DIR", raising=False)


@pytest.mark.parametrize("raw, said", [
    ("{", "is not valid JSON"),
    ("[]", "is not a JSON object"),
    ('"q"', "is not a JSON object"),
    ('{"sign_queue_dir": 5}', "is 5, not an absolute"),
    ('{"sign_queue_dir": null}', "is None, not an absolute"),
    ('{"sign_queue_dir": ""}', "is '', not an absolute"),
    ('{"sign_queue_dir": "rel/signq"}', "is 'rel/signq', not an absolute"),
])
def test_q1_a_present_but_broken_setting_is_no_queue_never_the_derived_one(tmp_path, monkeypatch, raw, said):
    """Q1: the fallback is for an ABSENT setting only. A malformed file, a non-object or a bad value yields no queue
    even though the hook queue is served (a derivation would have found it), and the hint names the file and value."""
    home, q = _hook_queue(tmp_path, None)
    (q / "claude_code.json").write_text(raw, encoding="utf-8")
    write_origin(q, "https://cockpit.example.test")
    _env(monkeypatch, home)
    found = ac.resolve_sign_queue(__import__("os").environ)
    assert found.path is None and ac.sign_queue_dir(__import__("os").environ) is None
    hint = ac.sign_queue_hint(found)
    assert said in hint and str(q.resolve() / "claude_code.json") in hint and "--sign-queue-dir" in hint, hint
    with pytest.raises(InputRefused) as e:
        dcli._sign_queue(_args())
    assert said in str(e.value)


def test_q1_an_unreadable_settings_file_is_no_queue(tmp_path, monkeypatch):
    home, q = _hook_queue(tmp_path, None)
    (q / "claude_code.json").mkdir()                       # present, but no file to read
    write_origin(q, "https://cockpit.example.test")
    _env(monkeypatch, home)
    found = ac.resolve_sign_queue(__import__("os").environ)
    assert found.path is None and "cannot be read" in ac.sign_queue_hint(found)


def test_q1_an_absent_file_or_an_object_without_the_key_still_derives(tmp_path, monkeypatch):
    home, q = _hook_queue(tmp_path, None)                  # no claude_code.json at all
    write_origin(q, "https://cockpit.example.test")
    _env(monkeypatch, home)
    assert dcli._sign_queue(_args()) == q.resolve()
    (q / "claude_code.json").write_text(json.dumps({"away_word": "auto"}), encoding="utf-8")
    assert dcli._sign_queue(_args()) == q.resolve()


def test_q2_a_stopped_cockpits_queue_is_still_found_by_its_sign_dir(tmp_path, monkeypatch):
    """Q2: `config accept-approved` after the cockpit stopped (its origin withdrawn) still finds the queue its request
    was filed in; with neither an origin nor a sign dir, nothing is derived."""
    from levain.consent.cockpit_origin import read_origin, withdraw_origin
    from levain.consent.sign import sign_dir

    home, q = _hook_queue(tmp_path, {"away_word": "auto"})
    _env(monkeypatch, home)
    write_origin(q, "https://cockpit.example.test")
    sign_dir(q).mkdir(mode=0o700)
    withdraw_origin(q, "https://cockpit.example.test")
    assert read_origin(q) is None
    assert dcli._sign_queue(_args()) == q.resolve()
    sign_dir(q).rmdir()
    assert ac.sign_queue_dir(__import__("os").environ) is None
    with pytest.raises(InputRefused):
        dcli._sign_queue(_args())


def test_q3_the_flag_path_must_be_a_directory(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(InputRefused) as e:
        dcli._sign_queue(argparse.Namespace(sign_queue_dir=missing))
    assert "does not exist" in str(e.value) and str(missing) in str(e.value)
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    with pytest.raises(InputRefused) as e:
        dcli._sign_queue(argparse.Namespace(sign_queue_dir=afile))
    assert "is not a directory" in str(e.value) and "does not exist" not in str(e.value)
    good = tmp_path / "good"
    good.mkdir()
    assert dcli._sign_queue(argparse.Namespace(sign_queue_dir=good)) == good


def test_q4_the_hint_says_not_a_directory_for_a_file_and_reads_nothing_again(tmp_path, monkeypatch):
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    home, q = _hook_queue(tmp_path, {"sign_queue_dir": str(afile)})
    _env(monkeypatch, home)
    found = ac.resolve_sign_queue(__import__("os").environ)
    assert found.path == afile
    (q / "claude_code.json").unlink()                      # the hint works from the one resolution, not a second read
    hint = ac.sign_queue_hint(found)
    assert f"{afile} is not a directory" in hint and "does not exist" not in hint, hint
