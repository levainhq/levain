"""The launch fallback (2026-11-06): on the iPhone Home Screen app a tapped push can open the app at the bare '/' with no
notificationclick. The worker writes a 'shown' entry on push; the page takes it at a bare standalone '/' launch when the
notification has left the tray. Node rigs: tests/_sw_worker_rig.js (push/click) and tests/_sw_open_rig.js (scenario 'shown')."""
from __future__ import annotations

from tests.test_sw_worker import _open_rig, _run, needs_node

NC = "https://box.example/?view=notifications&n=p607"


@needs_node
def test_a_push_writes_one_shown_entry_and_a_newer_push_replaces_it() -> None:
    got = _run("push", "ok")
    (e,) = got["shownAfterPush"]
    assert (e["for"], e["ping"], e["tag"], e["url"]) == ("shown", 42, "x", "/"), got
    assert e["key"] == "/__levain_open/shown" and isinstance(e["at"], int) and isinstance(e["nonce"], str), got
    got = _run("push", "ok+second")
    (e,) = got["shownAfterPush"]
    assert (e["ping"], e["tag"], e["url"]) == (43, "y", "/?view=notifications&n=p43"), got   # one entry, the newer
    assert any(t["step"] == "shown-pending" for t in got["trace"]), got


@needs_node
def test_no_entry_without_a_url_a_ping_or_for_a_held_summary_and_a_cross_origin_url_becomes_the_plain_launch() -> None:
    assert _run("push", "ok+nourl")["shownAfterPush"] == []
    assert _run("push", "ok+summary")["shownAfterPush"] == []
    assert _run("push", "ok+noping")["shownAfterPush"] == []          # the page cannot tell a tap without the ping
    (e,) = _run("push", "ok+crossurl")["shownAfterPush"]
    assert e["url"] == "/", e


@needs_node
def test_the_notification_is_asked_for_and_receipted_beside_the_pending_write() -> None:
    got = _run("push", "ok")
    assert got["shownCalls"] == [{"title": "T", "tag": "x"}] and len(got["posts"]) == 2, got


@needs_node
def test_a_bare_standalone_launch_with_the_notification_gone_goes_to_the_entry_url_and_consumes_it() -> None:
    got = _open_rig("shown")
    assert got["launch"] == {"assigned": [NC], "kept": False}, got["launch"]
    assert any(t["step"] == "shown-launch" and t["ping"] == 607 and t["url"] == "/?view=notifications" for t in got["trace"]), got["trace"]
    assert got["fresh"] == {"assigned": [NC], "kept": False}, got["fresh"]            # 170 s old: inside the 180 s window


@needs_node
def test_no_redirect_when_in_tray_or_not_bare_or_not_standalone_or_expired_or_unlistable() -> None:
    got = _open_rig("shown")
    for name in ("inTray", "query", "fragment", "signPath", "notStandalone", "throws", "noApi", "crossOrigin", "noPing"):
        assert got[name] == {"assigned": [], "kept": True}, (name, got[name])        # untouched: a later bare launch may still use it
    assert got["otherPingInTray"]["assigned"] == [NC], got["otherPingInTray"]        # an older ping under the tag is not this one
    assert got["expired"] == {"assigned": [], "kept": False}, got["expired"]         # expired: swept, no move
    assert got["none"] == {"assigned": [], "kept": False}, got
    assert got["getNCalls"] and all(c == {"tag": "np-607"} for c in got["getNCalls"]), got


@needs_node
def test_a_new_entry_is_claimed_by_the_old_path_and_a_shown_entry_is_never_claimable_as_new() -> None:
    got = _open_rig("shown")
    assert got["newWins"]["assigned"] == ["https://box.example/sign/4"], got["newWins"]
    assert got["newWinsLeft"] == ["shown"], got                                       # the shown entry was not touched


@needs_node
def test_a_click_that_does_fire_removes_the_shown_entry_of_its_ping_only() -> None:
    got = _run("push", "ok+then:ok+click+clickping")
    assert len(got["shownAfterPush"]) == 1 and got["shownAfterClick"] == [], got
    got = _run("push", "ok+then:ok+click+clickother")
    assert len(got["shownAfterClick"]) == 1, got                                      # another ping's tap leaves it
