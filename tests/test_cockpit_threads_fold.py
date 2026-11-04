"""CH-U1 slice 3: the thread view's collapsed tool lines (state kept across polls), the model read, the inert approval
line and the work thread's Stop styles, through the real ``make_server`` beside a ChatService over a ScriptedSession."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from levain.cockpit import threads
from levain.coordinator.chat import ChatStore
from levain.coordinator.driver import ScriptedSession, SessionMeta
from levain.coordinator.envelopes import EnvelopeStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_threads import clock  # noqa: F401  (fixture)
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)
from tests.test_cockpit_threads_send import _PAGE, _call
from tests.test_coordinator_chat_send import _world, ready, send

CSS = Path(threads.__file__).resolve().parents[1] / "templates" / "web" / "dashboard.css"


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


def test_no_model_list_or_choice_is_offered_by_the_body_or_the_page(qdir, clock):
    """The driver capabilities carry no model list and the service has no verb that sets a model: nothing is offered."""
    svc, _cs, world, _s = _tooled(qdir, clock)
    body = threads.thread_stream(qdir, world["thread"], svc)
    assert "models" not in body and "composer" not in body
    js = (CSS.parent / "thread_view.js").read_text()
    assert not re.search(r"createElement\(\"select\"|\"select\"", js)


def test_the_work_thread_has_styles_for_every_control_it_draws_and_no_composer_styles_remain():
    css = CSS.read_text()
    for cls in ("tv-dock", "tv-stop", "tv-sendnote", "tv-toggle", "tv-peek", "tv-model"):
        assert re.search(r"\.%s\b" % cls, css), cls
    for gone in ("tv-input", "tv-send", "tv-actions", "tv-composer", "tv-mark"):
        assert not re.search(r"\.%s\b" % gone, css), gone          # the composer and the reconcile are the room's (.rv-*)
    assert "var(--" in css and not re.search(r"\.tv-stop[^{]*\{[^}]*#[0-9a-fA-F]{3,6}\b", css)


_FOLD = """(() => { const v = [...document.querySelectorAll('.tv-fold')];
return JSON.stringify({n: v.length, open: v.map(r => !r.querySelector('.tv-body').hidden),
  expanded: v.map(r => r.querySelector('.tv-toggle').getAttribute('aria-expanded'))}); })()"""


@needs_browser
def test_a_tool_run_draws_collapsed_opens_on_click_and_keeps_its_state_across_polls(tmp_path, qdir, clock):
    svc, cs, world, s = _tooled(qdir, clock)
    _root, src, _ids = _install(tmp_path)
    boot = ('window.__tv = LevainThreadView.mount(document.getElementById("host"),'
            ' {pollMs: 150, headers: () => ({"X-Levain-Write-Token": "%s"})});' % TOKEN)
    extra = {"/tvc.html": ("text/html; charset=utf-8", _PAGE.encode()),
             "/tvc.js": ("text/javascript; charset=utf-8", boot.encode())}
    with _serve(src, write_token=TOKEN, chat_service=svc, extra_assets=extra) as (base, _h):
        out = _drive(tmp_path, base + "/tvc.html", [
            {"until": "document.querySelector('.tv-row')"},
            {"eval": "document.querySelector('.tv-row').click(); 1"},
            {"until": "document.querySelectorAll('.tv-fold').length === 3"},
            {"eval": _FOLD},
            {"eval": "document.querySelector('.tv-fold .tv-peek').textContent"},
            {"eval": "document.querySelectorAll('.tv-fold')[0].querySelector('.tv-toggle').click(); 1"},
            {"eval": _FOLD},
            {"eval": "new Promise(r => setTimeout(() => r(1), 700))"},       # several polls pass with nothing new
            {"eval": _FOLD},
            {"eval": "document.querySelector('.tv-vendor_approval_request').querySelectorAll('button, a, input').length"},
            {"eval": "!document.querySelector('.tv-vendor_approval_request').classList.contains('tv-fold')"},
            {"eval": "document.querySelector('.tv-model').textContent"},
            {"eval": "document.querySelectorAll('select').length"},
            {"eval": "document.querySelectorAll('.tv-fold')[0].querySelector('.tv-toggle').click(); 1"},
            {"eval": _FOLD},
        ])
    v = out["values"]
    first, opened, later, closed = (json.loads(v[i]) for i in (3, 6, 8, 14))
    assert first["n"] == 3 and first["open"] == [False] * 3 and first["expanded"] == ["false"] * 3
    assert v[4] == "Bash: ls"                                       # the peek is the first line only
    assert opened["open"] == [True, False, False] and later == opened   # the open line survived the polls
    assert v[9] == 0 and v[10] is True                              # an approval stays an inert line
    assert v[11] == "Model in use: claude-test-1" and v[12] == 0    # the model is shown; no picker, no empty control
    assert closed["open"] == [False] * 3


_STYLED = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           '<link rel="stylesheet" href="/dashboard.css"><body class="ke" data-mode="build"><div id="host" class="th-body"></div>'
           '<script src="/thread_view.js"></script><script src="/tvc.js"></script>')


@needs_browser
@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_the_styled_thread_fits_a_390_phone_with_no_sideways_scroll(tmp_path, qdir, clock, scheme):
    svc, cs, world, s = _tooled(qdir, clock)
    s.show("tool_call", "c3", body="Bash: " + "x" * 400, call_id="call-3")
    svc.ingest(world["launch"], s.read("00000000"))
    _root, src, _ids = _install(tmp_path)
    boot = ('document.documentElement.setAttribute("data-theme", "%s");'
            'window.__tv = LevainThreadView.mount(document.getElementById("host"),'
            ' {pollMs: 0, headers: () => ({"X-Levain-Write-Token": "%s"})});' % (scheme, TOKEN))
    extra = {"/tvc.html": ("text/html; charset=utf-8", _STYLED.encode()),
             "/tvc.js": ("text/javascript; charset=utf-8", boot.encode())}
    shot = tmp_path / f"thread-{scheme}.png"
    with _serve(src, write_token=TOKEN, chat_service=svc, extra_assets=extra) as (base, _h):
        out = _drive(tmp_path, base + "/tvc.html", [
            {"emulate": {"w": 390, "h": 800, "dpr": 2, "mobile": True}},
            {"until": "document.querySelector('.tv-row')"},
            {"eval": "document.querySelector('.tv-row').click(); 1"},
            {"until": "document.querySelectorAll('.tv-fold').length === 4"},
            {"eval": "document.querySelectorAll('.tv-fold')[1].querySelector('.tv-toggle').click(); 1"},
            {"eval": "document.documentElement.scrollWidth <= window.innerWidth"},
            {"eval": "document.querySelectorAll('.tv-input, .tv-send').length === 0"},
            {"shot": str(shot), "full": True},
        ])
    assert out["values"][4] is True and out["values"][5] is True and shot.exists()
