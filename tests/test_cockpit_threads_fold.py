"""CH-U1 slice 3, the server half: the thread stream's model read (the model the session reported, none when absent or unreadable) and that nothing
is offered to choose, through the real ``make_server`` beside a ChatService over a ScriptedSession."""
from __future__ import annotations

import pytest

from levain.cockpit import threads
from levain.desk.chat import ChatStore
from levain.desk.driver import ScriptedSession, SessionMeta
from levain.desk.envelopes import EnvelopeStore
from tests.test_cockpit_threads import clock  # noqa: F401  (fixture)
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)
from tests.test_desk_chat_send import _world, ready, send


class _Modelled(ScriptedSession):
    def __init__(self, model, **kw):
        super().__init__(**kw)
        self._model = model

    def meta(self):
        if isinstance(self._model, Exception):
            raise self._model
        return SessionMeta("scripted-session", "default", self._model, "scripted-1", None, (), ())


def _live(qdir, clock, session):
    store = EnvelopeStore(qdir, clock=clock)
    world = _world(store)
    cs = ChatStore(store)
    cs._connect().close()
    svc = ready(cs, world, session)
    return svc, cs, world


def _tooled(qdir, clock, model="claude-test-1"):
    s = _Modelled(model)
    svc, cs, world = _live(qdir, clock, s)
    send(svc, cs, world, "go")
    s.show("tool_call", "c1", body="Bash: ls\nsecond line", call_id="call-1")
    s.show("tool_result", "r1", body="file-a\nfile-b", call_id="call-1")
    s.show("tool_call", "c2", body="Bash: pwd", call_id="call-2")
    s.inbound("req-77", "can_use_tool", tool_name="Bash", tool_input={"command": "rm x"})
    svc.ingest(world["launch"], s.read("00000000"))
    return svc, cs, world, s


def test_the_stream_names_the_model_the_session_reported(qdir, clock):
    svc, _cs, world, _s = _tooled(qdir, clock)
    assert threads.thread_stream(qdir, world["thread"], svc)["model"] == "claude-test-1"


@pytest.mark.parametrize("model", [None, "", 7, RuntimeError("meta broke")])
def test_a_model_that_is_absent_unreadable_or_not_a_string_is_none(qdir, clock, model):
    svc, _cs, world, _s = _tooled(qdir, clock, model)
    assert threads.thread_stream(qdir, world["thread"], svc)["model"] is None


def test_without_a_service_or_a_session_there_is_no_model(qdir, clock):
    svc, _cs, world, _s = _tooled(qdir, clock)
    assert threads.thread_stream(qdir, world["thread"])["model"] is None
    assert threads.model_in_use(svc, None) is None and threads.model_in_use(svc, "no-such-launch") is None


def test_no_model_list_or_choice_is_offered_by_the_body(qdir, clock):
    """The driver capabilities carry no model list and the service has no verb that sets a model: nothing is offered."""
    svc, _cs, world, _s = _tooled(qdir, clock)
    body = threads.thread_stream(qdir, world["thread"], svc)
    assert "models" not in body and "composer" not in body

