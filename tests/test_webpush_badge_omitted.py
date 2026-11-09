"""A push that leaves its badge out is not a push that clears it: a host-served install keeps its count in the live server, so a
process without that store reads the count the server last recorded, else the native store; an explicit unknown carries no
badge field at all; a count off its shape is unknown, never a number."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from levain.webpush import needs, sender
from tests.test_webpush_badge_host import _Host, _rows


@pytest.fixture
def caught(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_build(title, body, tag, url, ping_id, badge=None, **kw):
        seen["badge"] = badge
        return b"{}"

    monkeypatch.setattr(sender, "build_payload", fake_build)
    monkeypatch.setattr(sender.PushStore, "live_subscriptions", lambda self, device=None: [])
    return seen


def _send(q: Path, **kw: Any) -> None:
    sender.send(q, kind="attention", ref="np-abcdef12", title="t", body="b", **kw)


def test_an_explicit_unknown_carries_no_badge_field(tmp_path: Path) -> None:
    payload = json.loads(sender.build_payload("t", "b", "tag", "/", 1, None))
    assert "badge" not in payload


def test_send_with_badge_none_does_not_recompute_natively(tmp_path: Path, caught: dict[str, Any]) -> None:
    _send(tmp_path, badge=None)
    assert caught["badge"] is None                    # the host's count is unknown: the phone keeps what it shows


def test_omitted_badge_reads_the_count_the_server_recorded(tmp_path: Path, caught: dict[str, Any]) -> None:
    needs.open_needs_count(tmp_path, _Host(_rows(tmp_path)))     # the server computing its count records it
    _send(tmp_path)
    assert caught["badge"] == 2


def test_omitted_badge_without_a_record_counts_natively(tmp_path: Path, caught: dict[str, Any]) -> None:
    _send(tmp_path)
    assert caught["badge"] == 0


def test_a_stale_or_broken_record_is_not_believed(tmp_path: Path, caught: dict[str, Any]) -> None:
    f = tmp_path / needs.COUNT_FILE
    f.write_text(json.dumps({"n": 7, "at": time.time() - needs.COUNT_FRESH_S - 30}))
    _send(tmp_path)
    assert caught["badge"] == 0
    f.write_text("{not json")
    _send(tmp_path)
    assert caught["badge"] == 0
    f.write_text(json.dumps({"n": True, "at": time.time()}))
    _send(tmp_path)
    assert caught["badge"] == 0


def test_a_native_count_never_writes_the_record(tmp_path: Path) -> None:
    needs.open_needs_count(tmp_path)
    assert not (tmp_path / needs.COUNT_FILE).exists()


def test_an_unknown_host_count_withdraws_the_record(tmp_path: Path) -> None:
    host = _Host(_rows(tmp_path))
    needs.open_needs_count(tmp_path, host)
    assert (tmp_path / needs.COUNT_FILE).exists()

    class Broken:
        def open_questions(self) -> list[Any]:
            raise OSError("gone")

    assert needs.open_needs_count(tmp_path, Broken()) is None
    assert not (tmp_path / needs.COUNT_FILE).exists()


@pytest.mark.parametrize("bad", [True, False, -1, "5", 1.5, [3]])
def test_a_count_off_its_shape_is_unknown_with_a_warning(tmp_path: Path, caught: dict[str, Any], caplog, bad: Any) -> None:
    with caplog.at_level(logging.WARNING, logger="levain.webpush.sender"):
        _send(tmp_path, badge=bad)
    assert caught["badge"] is None
    assert any("badge" in r.getMessage() for r in caplog.records)


def test_a_big_count_is_clipped_and_zero_is_kept(tmp_path: Path, caught: dict[str, Any]) -> None:
    _send(tmp_path, badge=500)
    assert caught["badge"] == needs.BADGE_MAX
    _send(tmp_path, badge=0)
    assert caught["badge"] == 0


def _spawned(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    class _Child:
        class stdin:
            @staticmethod
            def write(b: bytes) -> None:
                seen["stdin"] = json.loads(b)

            @staticmethod
            def close() -> None:
                pass

    monkeypatch.setattr(sender.subprocess, "Popen", lambda *a, **k: _Child())
    return seen


def test_spawn_tells_the_child_omitted_from_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spawned(monkeypatch)
    assert sender.spawn(tmp_path, kind="attention", ref=None, title="t", body="b")
    assert "badge" not in seen["stdin"]
    assert sender.spawn(tmp_path, kind="attention", ref=None, title="t", body="b", badge=None)
    assert "badge" in seen["stdin"] and seen["stdin"]["badge"] is None
    assert sender.spawn(tmp_path, kind="attention", ref=None, title="t", body="b", badge=True)
    assert seen["stdin"]["badge"] is None
    assert sender.spawn(tmp_path, kind="attention", ref=None, title="t", body="b", badge=500)
    assert seen["stdin"]["badge"] == needs.BADGE_MAX


@pytest.mark.parametrize("fields,want", [({}, "omitted"), ({"badge": None}, None), ({"badge": 4}, 4), ({"badge": True}, None),
                                         ({"badge": -2}, None), ({"badge": "3"}, None)])
def test_the_child_keeps_omitted_apart_from_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fields: dict, want: Any) -> None:
    import io

    from levain.webpush import __main__ as child

    got: dict[str, Any] = {}
    monkeypatch.setattr(sender, "send", lambda *a, **k: got.update(k) or [])
    stdin = json.dumps({"title": "t", "body": "b", "ref": None, "url": "/", **fields}).encode()
    monkeypatch.setattr(child.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin)))
    assert child.main(["send", "--queue-dir", str(tmp_path), "--kind", "attention"]) == 0
    if want == "omitted":
        assert got["badge"] is sender.OMITTED
    else:
        assert got["badge"] is want or got["badge"] == want


def test_the_servers_own_pushes_pass_the_servers_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from levain import web_server

    host = _Host(_rows(tmp_path))
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    got: dict[str, Any] = {}
    monkeypatch.setattr(sender, "deliver", lambda *a, **k: got.update(k) or [])
    web_server._push_notice(q, 1, "notice", "notify:x", "title", "body", host)
    assert got["badge"] == 2
    got.clear()
    web_server._push_notice(q, 1, "notice", "notify:x", "title", "body")
    assert got["badge"] is sender.OMITTED                 # no host store in this process: the record, else native

    monkeypatch.setattr(sender, "send", lambda *a, **k: got.update(k) or [])
    me = SimpleNamespace(server=SimpleNamespace(signer=SimpleNamespace(queue_dir=q), question_store=host))
    web_server._Handler._push_test(me, "paired-x")
    assert got["badge"] == 2
