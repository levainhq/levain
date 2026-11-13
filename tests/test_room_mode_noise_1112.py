"""ROOMFIX5 (levain 23, key 1112-40): the host room's per-turn mode report is not drawn as an error.

Lane V (the real Room page, host profile): every turn drew "Error / bypassPermissions". The session reports its permission
mode on every ``init`` as an error-kind row (``claude_session._on_init``, ref ``error:mode:<id>``); the page hid only the
confined room's ``dontAsk``. The tripwire reads the same recorded row (``chat._is_mode_line``), so the row must stay in the
record; the page hides it by the mode the room is EXPECTED to report (the read's ``room_mode``, the service's
``room_permission_mode``), and any other mode still draws."""
from __future__ import annotations

import json
from pathlib import Path

from levain.cockpit import room as cockpit_room
from levain.desk.chat import ChatService
from tests.test_chat_room_send import LIVE
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _serve
from tests.test_cockpit_room import World
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture: the consent directory the server reads)
from tests.test_cockpit_room_join import _rig


def _service(world, mode=None):
    kw = {} if mode is None else {"room_permission_mode": mode}
    return ChatService(world.cs, **LIVE, **kw)


# --- the read names the expected mode -----------------------------------------------------------------------------

def test_the_read_names_the_mode_the_room_is_expected_to_report(qdir):
    w = World(qdir)
    assert cockpit_room.room_read(qdir, w.svc)["room_mode"] == "dontAsk"          # the confined room's
    assert cockpit_room.room_read(qdir, _service(w, "bypassPermissions"))["room_mode"] == "bypassPermissions"


def test_a_read_with_no_service_or_a_service_naming_no_mode_says_none(qdir):
    w = World(qdir)
    assert cockpit_room.room_read(qdir, None)["room_mode"] is None

    class Bare:
        room_permission_mode = ""

    class Any:
        def __getattr__(self, name):          # anything: a non-string mode is never passed on
            return object()

    assert cockpit_room._expected_mode(Bare()) is None and cockpit_room._expected_mode(Any()) is None
    assert cockpit_room._expected_mode(w.svc) == "dontAsk"


def test_the_service_reports_the_mode_it_was_built_with(qdir):
    w = World(qdir)
    assert w.svc.room_permission_mode == "dontAsk"
    assert _service(w, "bypassPermissions").room_permission_mode == "bypassPermissions"


# --- the page hides exactly that mode's report --------------------------------------------------------------------

def _hidden(tmp_path, qdir, body):
    """``roomModel(body)`` in the real browser: the refs of the rows the page keeps."""
    from tests.test_room_phone import _STYLED
    boot = "window.__m = LevainRoomView.roomModel(%s).stream.map(l => l.ref);" % json.dumps(body)
    rs, _l, src = _rig(tmp_path, qdir)
    assets = {"/rvc.html": ("text/html; charset=utf-8", _STYLED.encode()), "/rvc.js": ("text/javascript; charset=utf-8", boot.encode())}
    with _serve(src, write_token=TOKEN, room_serve=rs, extra_assets=assets) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [{"until": "window.__m"}, {"eval": "JSON.stringify(window.__m)"}])
    return json.loads(out["values"][1])


def _row(ref, body, gseq):
    return {"kind": "vendor_error", "ref": ref, "body": body, "gseq": gseq}


@needs_browser
def test_a_host_room_does_not_draw_its_own_mode_and_still_draws_any_other(tmp_path, qdir):
    rows = [_row("error:mode:u1", "bypassPermissions", 1),     # the host room's expected mode: not an error
            _row("error:mode:u2", "default", 2),               # a host room that suddenly reports `default`: drawn
            _row("error:mode:u3", "dontAsk", 3),               # the confined mode in a host room is drift: drawn (ROOMFIX6)
            _row("error:abc:4", "bypassPermissions", 4),       # the same words from a row that is NOT a mode report: drawn
            _row("error:mode:u5", "bypassPermissions", 5)]
    kept = _hidden(tmp_path, qdir, {"room_thread": "t", "room_mode": "bypassPermissions", "stream": rows})
    assert kept == ["error:mode:u2", "error:mode:u3", "error:abc:4"]


@needs_browser
def test_the_confined_room_is_unchanged(tmp_path, qdir):
    rows = [_row("error:mode:u1", "dontAsk", 1), _row("error:mode:u2", "bypassPermissions", 2),
            _row("error:mode:u3", "default", 3)]
    for body in ({"room_mode": "dontAsk"}, {}, {"room_mode": None}):
        d = tmp_path / ("c%d" % len(json.dumps(body)))
        d.mkdir()
        kept = _hidden(d, qdir, {"room_thread": "t", "stream": rows, **body})
        assert kept == ["error:mode:u2", "error:mode:u3"], (body, kept)


def test_the_page_source_still_carries_the_confined_expression():
    js = (Path(cockpit_room.__file__).resolve().parents[1] / "templates" / "web" / "room_view.js").read_text()
    assert 'it.kind === "vendor_error" && it.body === "dontAsk"' in js
