"""The cockpit Inbox routes (1101-6): list, unread_count and read on a real server over a scratch queue. The full credential's
alone; a notify token or a tap token reads as no credential on them."""
from __future__ import annotations

import pytest

from levain.webpush import notify_tokens as nt, presence_tokens
from levain.webpush.store import PushStore
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, T, _call
from tests.test_cockpit_webpush import _signed

LIST, COUNT, READ = "/webpush/inbox", "/webpush/inbox/unread_count", "/webpush/inbox/read"


@pytest.fixture
def rig(tmp_path):
    q = tmp_path / "signq"
    q.mkdir(mode=0o700)
    return _install(tmp_path)[1], q


def _seed(q):
    st = PushStore(q)
    p = st.add_ping("attention", "digest", "Daily Email Digest", "line <b>1</b>")
    _, nid = st.record_notice("notify", "notify:Backup", "Backup: ok", "Backup", "ok", "https://x.test", "normal", "Backup",
                              "pushed")
    return p, nid


def test_the_list_carries_the_shape_with_the_count_and_a_notice_is_dismissable(rig):
    src, q = rig
    p, nid = _seed(q)
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "GET", LIST, token=T)
        assert st == 200 and got["unread"] == 2 and got["pings"] == 1 and got["notices"] == 1
        notice, ping = got["items"]
        assert (notice["id"], notice["kind"], notice["dismissable"]) == (f"n{nid}", "notice", True)
        assert (ping["id"], ping["kind"], ping["dismissable"]) == (f"p{p}", "ping", False)
        assert ping["body"] == "line <b>1</b>" and ping["read"] is False
        assert set(notice) == {"id", "kind", "title", "body", "at", "source", "priority", "decision", "opened", "read",
                               "dismissable"}
        assert _call(port, "GET", COUNT, token=T)[1] == {"unread": 2, "pings": 1, "notices": 1}


def test_read_one_up_to_and_all_and_the_bad_shapes(rig):
    src, q = rig
    p, nid = _seed(q)
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", READ, {"id": f"p{p}"}, token=T)
        assert st == 200 and got["read"] == f"p{p}" and got["unread"] == 1
        assert _call(port, "POST", READ, {"id": "p99999"}, token=T)[0] == 404
        assert _call(port, "POST", READ, {"up_to": f"n{nid}"}, token=T)[1]["unread"] == 0
        assert _call(port, "POST", READ, {"all": True}, token=T)[0] == 200
        for bad in ({}, {"id": 5}, {"id": "p1", "all": True}, {"all": False}, {"up_to": None}, {"x": "p1"}):
            assert _call(port, "POST", READ, bad, token=T)[0] == 400, bad


def test_a_notify_token_or_a_tap_token_or_no_credential_reads_and_writes_nothing(rig):
    src, q = rig
    tok = nt.create(q, "Hub", urgent=True)[1]
    tap = presence_tokens.create(q, "door")[1]
    _seed(q)
    with _signed(src, q) as (port, _):
        for hdrs in (None, PROXY, {nt.HEADER: tok}, {"Authorization": f"Bearer {tok}"},
                     {"X-Levain-Presence-Token": tap}, {**PROXY, nt.HEADER: tok}):
            assert _call(port, "GET", LIST, headers=hdrs)[0] in (401, 403)
            assert _call(port, "GET", COUNT, headers=hdrs)[0] in (401, 403)
            assert _call(port, "POST", READ, {"all": True}, headers=hdrs)[0] in (401, 403)
        assert _call(port, "GET", LIST, token="wrong")[0] == 403
        assert _call(port, "POST", READ, {"all": True}, token="wrong")[0] == 403
        assert _call(port, "GET", LIST, token=T, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert PushStore(q).inbox_counts()["unread"] == 2


def test_the_route_does_not_break_the_notices_routes(rig):
    src, q = rig
    _, nid = _seed(q)
    with _signed(src, q) as (port, _):
        assert [n["id"] for n in _call(port, "GET", "/webpush/notices", token=T)[1]["notices"]] == [nid]
        assert _call(port, "POST", "/webpush/notices/dismiss", {"id": nid}, token=T)[0] == 200
        assert _call(port, "GET", LIST, token=T)[1]["items"][0]["kind"] == "ping"     # the dismissed notice left the list
