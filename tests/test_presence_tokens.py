"""presence_tokens: the tap-token store (hashed, 0600, atomic), its verify, and the per-token rate limit."""
from __future__ import annotations

import json
import os

import pytest

from levain.webpush import presence_tokens as pt
from levain.webpush.presence import PresenceError


def test_create_returns_a_token_once_and_keeps_only_its_hash(tmp_path):
    rec, tok = pt.create(tmp_path, "door")
    assert len(tok) >= 43 and rec["words"] == ["away", "back", "quiet", "desk", "devices"] and rec["revoked"] is False
    f = tmp_path / "webpush" / pt.FILE_NAME
    assert (f.stat().st_mode & 0o777) == 0o600
    text = f.read_text()
    assert tok not in text and pt._hash(tok) in text
    assert "sha256" not in json.dumps(pt.list_public(tmp_path))
    assert pt.create(tmp_path, "bed")[1] != tok


def test_verify_accepts_the_token_and_refuses_everything_else(tmp_path):
    rec, tok = pt.create(tmp_path, "door", ["sleep"])
    assert pt.verify(tmp_path, tok)["name"] == "door" and pt.verify(tmp_path, tok)["words"] == ["sleep"]
    for bad in ("", "nope", tok[:-1], tok + "x", None, 5):
        assert pt.verify(tmp_path, bad) is None
    pt.revoke(tmp_path, "door")
    assert pt.verify(tmp_path, tok) is None


def test_verify_compares_every_record(tmp_path, monkeypatch):
    for n in "abc":
        pt.create(tmp_path, n)
    _, tok = pt.create(tmp_path, "d")
    seen = []
    real = pt.hmac.compare_digest
    monkeypatch.setattr(pt.hmac, "compare_digest", lambda a, b: seen.append(1) or real(a, b))
    assert pt.verify(tmp_path, tok)["name"] == "d"
    assert len(seen) == 4
    seen.clear()
    pt.verify(tmp_path, tok)    # a match on the first record still reads them all
    assert len(seen) == 4


@pytest.mark.parametrize("name", ["", " x", "x ", "a/b", "é", "x" * 41, "a\nb", 3])
def test_a_bad_name_is_refused(tmp_path, name):
    with pytest.raises(PresenceError):
        pt.create(tmp_path, name)


@pytest.mark.parametrize("words", [[], ["flying"], ["away", 3]])
def test_bad_words_are_refused(tmp_path, words):
    with pytest.raises(PresenceError):
        pt.create(tmp_path, "ok", words)


def test_a_live_name_is_unique_and_a_revoked_one_can_be_reused(tmp_path):
    pt.create(tmp_path, "door")
    with pytest.raises(PresenceError):
        pt.create(tmp_path, "door")
    assert pt.revoke(tmp_path, "door") is True and pt.revoke(tmp_path, "door") is False
    assert pt.list_public(tmp_path) == []
    pt.create(tmp_path, "door")
    assert [r["name"] for r in pt.list_public(tmp_path)] == ["door"]


def test_touch_stamps_last_used(tmp_path):
    pt.create(tmp_path, "door")
    assert pt.list_public(tmp_path)[0]["last_used"] is None
    pt.touch(tmp_path, "door", now=123.0)
    assert pt.list_public(tmp_path)[0]["last_used"] == 123.0
    pt.touch(tmp_path, "ghost")       # no such token: nothing, no error


def test_a_symlinked_or_foreign_file_is_not_written_or_read(tmp_path):
    d = tmp_path / "webpush"
    d.mkdir()
    target = tmp_path / "elsewhere"
    target.write_text("{}")
    os.symlink(target, d / pt.FILE_NAME)
    with pytest.raises(PresenceError):
        pt.create(tmp_path, "door")
    assert target.read_text() == "{}" and pt.list_public(tmp_path) == []


def test_a_malformed_file_reads_as_no_tokens(tmp_path):
    d = tmp_path / "webpush"
    d.mkdir()
    for junk in ("not json", "[]", json.dumps({"tokens": [1, {"name": 3}]}), "x" * 70000):
        (d / pt.FILE_NAME).write_text(junk)
        assert pt.list_public(tmp_path) == [] and pt.verify(tmp_path, "anything") is None


def test_the_rate_limit_counts_per_key_over_a_sliding_minute():
    r = pt.RateLimit(3)
    assert [r.hit("a", now=t) for t in (0, 1, 2)] == [0.0, 0.0, 0.0]
    wait = r.hit("a", now=3)
    assert wait == pytest.approx(57.0)
    assert r.hit("b", now=3) == 0.0
    assert r.hit("a", now=60.5) == 0.0


def test_a_failed_read_refuses_every_write_and_leaves_the_file(tmp_path, monkeypatch):
    import errno

    pt.create(tmp_path, "door")
    pt.create(tmp_path, "bed")
    f = tmp_path / "webpush" / pt.FILE_NAME
    before, ino = f.read_bytes(), f.stat().st_ino
    real = os.open

    def emfile(path, *a, **kw):
        if os.fspath(path).endswith(pt.FILE_NAME):
            raise OSError(errno.EMFILE, "Too many open files")
        return real(path, *a, **kw)

    monkeypatch.setattr(pt.os, "open", emfile)
    with pytest.raises(PresenceError):
        pt.create(tmp_path, "hall")
    with pytest.raises(PresenceError):
        pt.revoke(tmp_path, "door")
    pt.touch(tmp_path, "door", now=5.0)        # best effort: no error, and no write
    assert pt.verify(tmp_path, "anything") is None and pt.list_public(tmp_path) == []
    monkeypatch.setattr(pt.os, "open", real)
    assert f.read_bytes() == before and f.stat().st_ino == ino
    assert [r["name"] for r in pt.list_public(tmp_path)] == ["door", "bed"]


def test_a_malformed_file_is_not_overwritten_by_a_write(tmp_path):
    d = tmp_path / "webpush"
    d.mkdir()
    (d / pt.FILE_NAME).write_text("not json")
    with pytest.raises(PresenceError):
        pt.create(tmp_path, "door")
    assert (d / pt.FILE_NAME).read_text() == "not json"


def test_touch_with_no_match_writes_nothing(tmp_path):
    pt.create(tmp_path, "door")
    f = tmp_path / "webpush" / pt.FILE_NAME
    ino = f.stat().st_ino
    pt.touch(tmp_path, "ghost", now=1.0)
    assert f.stat().st_ino == ino


def test_a_deeply_nested_file_reads_as_no_tokens(tmp_path):
    pt.create(tmp_path, "door")
    (tmp_path / "webpush" / pt.FILE_NAME).write_text("[" * 30000 + "]" * 30000)
    assert pt.list_public(tmp_path) == []
