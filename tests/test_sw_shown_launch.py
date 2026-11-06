"""The launch fallback (2026-11-06): on the iPhone Home Screen app a tapped push can open the app at the bare '/' with no
notificationclick. The worker writes one immutable 'shown' entry PER PING on push (before the notification can be tapped, and
only if it was shown); the page takes one at a bare standalone '/' launch when its notification has left the tray. Node rigs:
tests/_sw_worker_rig.js (push/click) and tests/_sw_open_rig.js (scenario 'shown')."""
from __future__ import annotations

import re

from tests.test_sw_worker import _open_rig, _run, needs_node

NC = "https://box.example/?view=notifications&n=p607"
NC8 = "https://box.example/?view=notifications&n=p608"
NC9 = "https://box.example/?view=notifications&n=p609"
KEY = re.compile(r"^/__levain_open/shown-(\d+)-[a-z0-9]+$")


@needs_node
def test_each_push_writes_its_own_immutable_entry_and_a_newer_push_never_replaces_an_older_one() -> None:
    got = _run("push", "ok")
    (e,) = got["shownAfterPush"]
    assert (e["for"], e["ping"], e["tag"], e["url"]) == ("shown", 42, "x", "/"), got
    m = KEY.match(e["key"])
    assert m and m.group(1) == "42" and e["key"].endswith(e["nonce"]) and isinstance(e["at"], int), got
    got = _run("push", "ok+second")
    a, b = sorted(got["shownAfterPush"], key=lambda x: x["ping"])
    assert (a["ping"], b["ping"]) == (42, 43) and a["key"] != b["key"], got            # two keys, both kept
    assert (b["tag"], b["url"]) == ("y", "/?view=notifications&n=p43"), got
    assert any(t["step"] == "shown-pending" for t in got["trace"]), got


@needs_node
def test_at_most_ten_entries_are_kept_the_oldest_go() -> None:
    got = _run("push", "ok+many")
    pings = sorted(e["ping"] for e in got["shownAfterPush"])
    assert pings == list(range(102, 112)), pings                                       # 13 pushes (42, 100..111): the newest 10


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
def test_the_entry_is_durable_before_the_notification_is_asked_for() -> None:
    got = _run("push", "ok+putslow")
    order = [o for o in got["order"] if o in ("put-start", "put-done", "show")]
    assert order == ["put-start", "put-done", "show"], order                           # a tap can find the entry
    assert len(got["shownAfterPush"]) == 1, got


@needs_node
def test_a_hung_cache_write_delays_the_notification_by_the_bound_and_no_more() -> None:
    got = _run("push", "ok+putstuck")
    assert got["shownCalls"] == [{"title": "T", "tag": "x"}], got                      # the show still goes
    assert 1400 <= got["showAfterMs"] <= 2600, got["showAfterMs"]                      # after the 1.5 s bound
    assert got["shownAfterPush"] == [], got


@needs_node
def test_a_show_that_rejects_removes_its_entry() -> None:
    got = _run("push", "ok+showthrows")
    assert got["shownAfterPush"] == [], got                                            # no entry outlives a notification not drawn
    assert any(t["step"] == "show-failed" for t in got["trace"]), got
    got = _run("push", "ok+showthrows+putslow")                                        # the write still in flight when the show fails
    assert got["shownAfterPush"] == [], got
    got = _run("push", "ok+second+showthrows")
    assert got["shownAfterPush"] == [], got


@needs_node
def test_a_bare_standalone_launch_with_the_notification_gone_goes_to_the_entry_url_and_consumes_it() -> None:
    got = _open_rig("shown")
    assert got["launch"]["assigned"] == [NC] and got["launch"]["kept"] is False, got["launch"]
    assert any(t["step"] == "shown-launch" and t["ping"] == 607 and t["url"] == "/?view=notifications" and t["candidates"] == 1
               for t in got["trace"]), got["trace"]
    assert got["fresh"]["assigned"] == [NC] and got["fresh"]["kept"] is False, got["fresh"]   # 170 s old: inside the 180 s window


@needs_node
def test_no_redirect_when_in_tray_or_not_bare_or_not_standalone_or_expired_or_unlistable_or_a_reload() -> None:
    got = _open_rig("shown")
    for name in ("inTray", "query", "fragment", "signPath", "notStandalone", "throws", "noApi", "crossOrigin", "noPing", "reload"):
        assert got[name]["assigned"] == [] and got[name]["kept"] is True, (name, got[name])   # untouched: a later bare launch may still use it
    assert got["otherPingInTray"]["assigned"] == [NC], got["otherPingInTray"]        # an older ping under the tag is not this one
    assert got["expired"]["assigned"] == [] and got["expired"]["kept"] is False, got["expired"]   # expired: swept, no move
    assert got["none"]["assigned"] == [] and got["none"]["kept"] is False, got
    assert got["navigateType"]["assigned"] == [NC], got["navigateType"]              # a navigation of the ordinary kind is a launch
    assert got["getNCalls"] and all(c == {"tag": "np-607"} for c in got["getNCalls"]), got


@needs_node
def test_several_gone_entries_go_to_the_newest_and_a_showing_one_is_not_a_candidate() -> None:
    got = _open_rig("shown")
    assert got["severalGone"]["assigned"] == [NC9] and got["severalGone"]["kept"] is False, got["severalGone"]   # newest of 3; others consumed
    assert any(t["step"] == "shown-launch" and t["ping"] == 609 and t["candidates"] == 3 for t in got["trace"]), got["trace"]
    assert got["newestStillShowing"]["assigned"] == [NC8], got["newestStillShowing"]   # 609 still showing: 608 is the newest gone
    assert got["newestStillShowing"]["keys"] == ["shown-609-c"], got["newestStillShowing"]   # the one still showing stays
    assert got["oneGone"]["assigned"] == [NC] and any(t["step"] == "shown-launch" and t["ping"] == 607 and t["candidates"] == 1
                                                     for t in got["trace"]), got["oneGone"]
    assert got["oneGone"]["keys"] == ["shown-608-b", "shown-609-c"], got["oneGone"]
    assert got["allShowing"]["assigned"] == [] and len(got["allShowing"]["keys"]) == 3, got["allShowing"]


@needs_node
def test_the_page_deletes_exactly_the_request_it_read_so_a_push_landing_meanwhile_survives() -> None:
    got = _open_rig("shown")
    assert got["pushDuringCheck"]["assigned"] == [NC], got["pushDuringCheck"]
    assert got["pushDuringKeys"] == ["shown-610-z"], got["pushDuringKeys"]            # 607's entry went, 610's (its own key) stayed


@needs_node
def test_a_new_entry_is_claimed_by_the_old_path_and_a_shown_entry_is_never_claimable_as_new() -> None:
    got = _open_rig("shown")
    assert got["newWins"]["assigned"] == ["https://box.example/sign/4"], got["newWins"]
    assert got["newWinsLeft"] == ["shown-607-sn"], got                                # the shown entry was not touched


@needs_node
def test_a_click_that_does_fire_removes_the_shown_entries_of_its_ping_only() -> None:
    got = _run("push", "ok+then:ok+click+clickping")
    assert len(got["shownAfterPush"]) == 1 and got["shownAfterClick"] == [], got
    got = _run("push", "ok+then:ok+click+clickother")
    assert len(got["shownAfterClick"]) == 1, got                                      # another ping's tap leaves it
    got = _run("push", "ok+second+then:ok+click+clickping")                           # ping 42 clicked: 43's own entry stays
    assert [e["ping"] for e in got["shownAfterClick"]] == [43], got
