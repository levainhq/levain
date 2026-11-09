"""A host-served install keeps its Needs-you rows in the host's question store, not the native one: the app badge (the push's
``badge``, the center's ``needs_count``) is counted from that store, with the strip's desk-owed rule, and a caller may hand the
sender its own count."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from levain.consent.questions import QuestionStore
from levain.webpush import needs, sender


class _Host:
    """A host's question source: only the read the native store shares (``open_questions``), rows carrying a section."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def open_questions(self, agent: str | None = None) -> list[Any]:
        return list(self.rows)


def _rows(tmp_path: Path) -> list[Any]:
    qs = QuestionStore(tmp_path / "scratch")
    out = []
    for sid, sec in (("a", "you"), ("b", "you"), ("c", "desk")):
        qs.file(agent="claude_code", agent_session_id=sid, what=sid, options=[("a", "A"), ("b", "B")], rec="a", blocks="b", cwd="/w")
    for q, sec in zip(qs.open_questions(), ("you", "you", "desk")):
        out.append(dataclasses.replace(q, section=sec, carried=False))
    return out


def test_count_and_badge_read_the_host_source(tmp_path: Path) -> None:
    host = _Host(_rows(tmp_path))
    q = tmp_path / "q"
    assert needs.open_needs_count(q, host) == 2        # two his, the fresh desk row out; the native store holds none
    assert needs.badge_value(q, host) == 2
    assert needs.open_needs_count(q) == 0              # no source: the native store, as before


def test_center_needs_count_uses_the_server_store(tmp_path: Path) -> None:
    from levain.web_server import _Handler

    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    me = SimpleNamespace(path="/webpush/center", server=SimpleNamespace(question_store=_Host(_rows(tmp_path))))
    doc, status = _Handler._center_get(me, "/webpush/center", q)
    assert status == 200 and doc["needs_count"] == 2


def test_spawn_carries_an_explicit_badge(tmp_path: Path, monkeypatch) -> None:
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
    assert sender.spawn(tmp_path, kind="attention", ref="np-abcdef12", title="t", body="b", badge=5)
    assert seen["stdin"]["badge"] == 5
    seen.clear()
    assert sender.spawn(tmp_path, kind="attention", ref="np-abcdef12", title="t", body="b")
    assert "badge" not in seen["stdin"]


def test_send_with_a_badge_puts_it_in_the_payload(tmp_path: Path, monkeypatch) -> None:
    caught: dict[str, Any] = {}

    def fake_build(title, body, tag, url, ping_id, badge=None, **kw):
        caught["badge"] = badge
        return b"{}"

    monkeypatch.setattr(sender, "build_payload", fake_build)
    monkeypatch.setattr(sender.PushStore, "live_subscriptions", lambda self, device=None: [])
    sender.send(tmp_path, kind="attention", ref="np-abcdef12", title="t", body="b", badge=5)
    assert caught["badge"] == 5
    sender.send(tmp_path, kind="attention", ref="np-abcdef12", title="t", body="b", badge=500)
    assert caught["badge"] == needs.BADGE_MAX
    sender.send(tmp_path, kind="attention", ref="np-abcdef12", title="t", body="b")
    assert caught["badge"] == 0                       # none given: the native store's count, as before
