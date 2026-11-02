"""The cockpit's Notices routes (N3): ``GET /webpush/notices`` and ``POST /webpush/notices/dismiss`` on a real server over a
scratch queue. The full credential's alone; a notify token or a tap token reads as no credential on them."""
from __future__ import annotations

import json

import pytest

from levain.webpush import notify_tokens as nt, presence_tokens
from levain.webpush.store import PushStore
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, T, _call
from tests.test_cockpit_webpush import _signed

LIST = "/webpush/notices"
DISMISS = "/webpush/notices/dismiss"


@pytest.fixture
def rig(tmp_path):
    q = tmp_path / "signq"
    q.mkdir(mode=0o700)
    return _install(tmp_path)[1], q


def _seed(q, source="Backup", token="Backup", title="Backup done", body="ok https://x.test <b>hi</b>", decision="recorded",
          priority="normal"):
    st = PushStore(q)
    if decision == "recorded":
        return None, st.add_notice(source, title, body, priority, token, decision)
    return st.record_notice("notify", f"notify:{source}", title, source, title, body, priority, token, decision,
                            hold=("/?view=notices", "sleep") if decision == "held" else None)


def test_the_list_carries_the_shape_newest_first_and_dismissed_are_left_out(rig):
    src, q = rig
    nt.create(q, "Backup")
    _seed(q, title="first")
    ping, nid = _seed(q, title="second", decision="pushed")
    PushStore(q).add_open(ping, "dev1")
    _seed(q, title="third")
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "GET", LIST, token=T)
        assert st == 200
        rows = got["notices"]
        assert [r["title"] for r in rows] == ["third", "second", "first"]
        assert set(rows[0]) == {"id", "at", "source", "title", "body", "priority", "decision", "opened", "revoked"}
        assert rows[0]["opened"] is False and rows[1]["opened"] is True and rows[1]["decision"] == "pushed"
        assert rows[0]["revoked"] is False and rows[0]["body"] == "ok https://x.test <b>hi</b>"   # raw text, never touched
        assert "token" not in rows[0]
        assert _call(port, "POST", DISMISS, {"id": rows[2]["id"]}, token=T)[0] == 200
        assert [r["title"] for r in _call(port, "GET", LIST, token=T)[1]["notices"]] == ["third", "second"]


def test_a_revoked_or_gone_source_is_tagged_and_history_stays(rig):
    src, q = rig
    nt.create(q, "Backup")
    _seed(q)
    _seed(q, source="Old", token="Old")           # a token that no longer exists at all
    nt.revoke(q, "Backup")
    with _signed(src, q) as (port, _):
        rows = _call(port, "GET", LIST, token=T)[1]["notices"]
        assert [(r["source"], r["revoked"]) for r in rows] == [("Old", True), ("Backup", True)]


def test_an_unreadable_token_file_tags_nothing_revoked(rig):
    src, q = rig
    _seed(q)
    (q / "webpush").mkdir(exist_ok=True)
    (q / "webpush" / "notify_tokens.json").write_text("{not json")
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "GET", LIST, token=T)
        assert st == 200 and got["notices"][0]["revoked"] is False


def test_empty_list_is_an_empty_array(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        assert _call(port, "GET", LIST, token=T)[1] == {"notices": []}


def test_dismiss_removes_one_and_an_unknown_or_repeated_id_is_404(rig):
    src, q = rig
    _, a = _seed(q, title="a")
    _, b = _seed(q, title="b")
    with _signed(src, q) as (port, _):
        assert _call(port, "POST", DISMISS, {"id": a}, token=T) [:2] == (200, {"dismissed": a})
        assert _call(port, "POST", DISMISS, {"id": a}, token=T)[0] == 404
        assert _call(port, "POST", DISMISS, {"id": 99999}, token=T)[0] == 404
    assert [n["id"] for n in PushStore(q).notices()] == [b]
    assert len(PushStore(q).notices(include_dismissed=True)) == 2      # dismissed, not deleted


@pytest.mark.parametrize("body", [{}, {"id": "1"}, {"id": True}, {"id": 1.5}, {"id": 1, "x": 2}, [1], "x"])
def test_dismiss_refuses_a_bad_body_and_changes_nothing(rig, body):
    src, q = rig
    _seed(q)
    with _signed(src, q) as (port, _):
        assert _call(port, "POST", DISMISS, body, token=T)[0] == 400
    assert len(PushStore(q).notices()) == 1


def test_no_credential_a_notify_token_or_a_tap_token_reads_nothing_and_dismisses_nothing(rig):
    src, q = rig
    tok = nt.create(q, "Hub", urgent=True)[1]
    tap = presence_tokens.create(q, "door")[1]
    _, nid = _seed(q, token="Hub", source="Hub")
    with _signed(src, q) as (port, _):
        for hdrs in (None, PROXY, {nt.HEADER: tok}, {"Authorization": f"Bearer {tok}"},
                     {"X-Levain-Presence-Token": tap}, {**PROXY, nt.HEADER: tok}):
            assert _call(port, "GET", LIST, headers=hdrs)[0] in (401, 403)
            assert _call(port, "POST", DISMISS, {"id": nid}, headers=hdrs)[0] in (401, 403)
        assert _call(port, "GET", LIST, token="wrong")[0] == 403
        assert _call(port, "POST", DISMISS, {"id": nid}, token="wrong")[0] == 403
    assert len(PushStore(q).notices()) == 1


def test_a_cross_site_read_is_refused(rig):
    src, q = rig
    _seed(q)
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "GET", LIST, token=T, headers={"Sec-Fetch-Site": "cross-site"})
        assert st == 403 and "notices" not in got
