"""The launch fallback (2026-11-06): on the iPhone Home Screen app a tapped push can open the app at the bare '/' with no
notificationclick. The worker writes one immutable 'shown' entry PER PING on push (before the notification can be tapped, and
only if it was shown); the page takes one at a bare standalone '/' launch when its notification has left the tray. Node rigs:
tests/_sw_worker_rig.js (push/click) and tests/_sw_open_rig.js (scenario 'shown')."""
from __future__ import annotations

import functools
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


@functools.lru_cache(maxsize=1)
def _shown_once() -> dict:
    return _open_rig("shown")                   # one node run for every test that only reads its result


def _shown() -> dict:
    return _shown_once()


@needs_node
def test_a_bare_standalone_launch_with_the_notification_gone_goes_to_the_entry_url_and_consumes_it() -> None:
    got = _shown()
    assert got["launch"]["assigned"] == [NC] and got["launch"]["kept"] is False, got["launch"]
    assert any(t["step"] == "shown-launch" and t["ping"] == 607 and t["url"] == "/?view=notifications" and t["candidates"] == 1
               for t in got["trace"]), got["trace"]
    assert got["fresh"]["assigned"] == [NC] and got["fresh"]["kept"] is False, got["fresh"]   # 170 s old: inside the 180 s window


@needs_node
def test_no_redirect_when_in_tray_or_not_bare_or_not_standalone_or_expired_or_unlistable_or_a_reload() -> None:
    got = _shown()
    for name in ("inTray", "query", "fragment", "signPath", "notStandalone", "throws", "noApi", "crossOrigin", "noPing", "reload"):
        assert got[name]["assigned"] == [] and got[name]["kept"] is True, (name, got[name])   # untouched: a later bare launch may still use it
    assert got["otherPingInTray"]["assigned"] == [NC], got["otherPingInTray"]        # an older ping under the tag is not this one
    assert got["expired"]["assigned"] == [] and got["expired"]["kept"] is False, got["expired"]   # expired: swept, no move
    assert got["none"]["assigned"] == [] and got["none"]["kept"] is False, got
    assert got["navigateType"]["assigned"] == [NC], got["navigateType"]              # a navigation of the ordinary kind is a launch
    assert got["getNCalls"] and all(c == {"tag": "np-607"} for c in got["getNCalls"]), got


@needs_node
def test_several_gone_entries_go_to_the_newest_and_a_showing_one_is_not_a_candidate() -> None:
    got = _shown()
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
    got = _shown()
    assert got["pushDuringCheck"]["assigned"] == [NC], got["pushDuringCheck"]
    assert got["pushDuringKeys"] == ["shown-610-z"], got["pushDuringKeys"]            # 607's entry went, 610's (its own key) stayed


@needs_node
def test_a_new_entry_is_claimed_by_the_old_path_and_a_shown_entry_is_never_claimable_as_new() -> None:
    got = _shown()
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


@needs_node
def test_a_same_tag_re_ping_replaces_the_older_entry_in_the_worker() -> None:
    """Round 2 (complement MED, codex MED): a re-ping reuses the item ref as the tag (renotify), so ping 43 replaces ping 42
    in the tray. Its older entry must not outlive it (it would read 'gone' at a manual launch)."""
    got = _run("push", "ok+sametag")
    assert [(e["ping"], e["tag"]) for e in got["shownAfterPush"]] == [(43, "x")], got
    got = _run("push", "ok+second")                                                   # a DIFFERENT tag: both stay
    assert sorted(e["ping"] for e in got["shownAfterPush"]) == [42, 43], got


@needs_node
def test_a_same_tag_older_entry_is_superseded_not_gone_when_the_newer_ping_is_in_the_tray() -> None:
    got = _shown()
    assert got["sameTagShowing"]["assigned"] == [], got["sameTagShowing"]            # 43 showing: 42 must not redirect the page
    assert len(got["sameTagShowing"]["keys"]) == 2, got["sameTagShowing"]
    assert got["sameTagQueries"] >= 1 and got["sameTagQueries"] == 1, got["sameTagQueries"]   # one query for the tag
    assert got["sameTagGone"]["assigned"] == ["https://box.example/?view=notifications&n=p43"], got["sameTagGone"]
    assert got["sameTagOlderOnly"]["assigned"] == ["https://box.example/?view=notifications&n=p43"], got["sameTagOlderOnly"]


@needs_node
def test_a_late_cache_write_never_recreates_the_entry_of_a_rejected_show() -> None:
    """Round 2 (deepseek HIGH): the write lands after the cleanup's bounded waits; the delete is chained after the put."""
    got = _run("push", "ok+showthrows+putlate")
    assert got["shownAfterPush"] == [], got
    assert any(t["step"] == "show-failed" for t in got["trace"]), got


@needs_node
def test_the_legacy_singleton_key_is_swept_by_the_worker_and_by_the_page() -> None:
    got = _run("push", "ok+legacy")
    assert got["legacyLeft"] is False, got                                            # the push's best-effort prune deleted it
    got = _shown()
    assert got["legacy"]["assigned"] == [] and got["legacyLeft"] is False, got["legacy"]   # the page's sweep: removed, never judged


# --- row 268 (1106-53): the app RESUMES (no load) and the page judges the 'shown' entries again ---------------------------

@functools.lru_cache(maxsize=1)
def _resume_once() -> dict:
    return _open_rig("resume")                  # the slow rig (each case's retry runs inside it): once for the four tests


def _resume() -> dict:
    return _resume_once()


@needs_node
def test_a_resume_with_the_notification_gone_forwards_to_the_entry_url_exactly_once() -> None:
    """11-06 21:01, ping 640: the tap resumed a document loaded hours before, nothing re-ran the launch check."""
    got = _resume()
    assert got["forward"]["assigned"] == [NC] and got["forward"]["kept"] is False, got["forward"]
    assert got["forward"]["hows"] == ["load", "resume>shown"], got["forward"]
    assert got["forwardTwice"]["assigned"] == [NC], got["forwardTwice"]              # a second return finds nothing to take
    assert got["forwardTwice"]["hows"] == ["load", "resume>shown"], got["forwardTwice"]
    assert got["fresh"]["assigned"] == [NC], got["fresh"]                            # 170 s old: inside the 180 s window


@needs_node
def test_a_resume_does_not_forward_with_the_notification_in_the_tray_or_an_old_entry_or_a_non_bare_url() -> None:
    got = _resume()
    for name in ("inTray", "inTrayLater", "nonBare", "notStandalone", "hidden"):
        assert got[name]["assigned"] == [] and got[name]["kept"] is True, (name, got[name])   # the entry stays for a later launch
    assert got["expired"]["assigned"] == [] and got["expired"]["kept"] is False, got["expired"]   # older than 180 s: swept, no move


@needs_node
def test_a_resume_that_finds_the_notification_gone_a_moment_later_forwards_on_the_one_retry() -> None:
    got = _resume()
    assert got["retry"]["assigned"] == [NC] and got["retry"]["hows"] == ["load", "resume>shown"], got["retry"]
    # the first look took nothing (in the tray), so a resume landing is owed, but it is inside the 30 s of the load's
    assert got["inTray"]["hows"] == ["load"], got["inTray"]


@needs_node
def test_landings_carry_the_key_a_target_without_a_token_and_the_resume_one_is_told_at_most_every_30_seconds() -> None:
    got = _resume()
    first = got["forward"]["post"]
    assert first["u"] == "/webpush/landed" and first["method"] == "POST" and first["key"] == "KEY" and first["keepalive"] is True, first
    assert set(first["body"]) == {"target", "how"} and "KEY" not in str(first["body"]), first
    assert got["tokenHref"]["targets"] == ["/?view=needs&item=np-0b62cd3b"] and "SECRET" not in str(got["tokenHref"]), got["tokenHref"]   # only view and item survive
    assert got["nonBare"]["targets"] == ["/?view=needs"], got["nonBare"]            # the load landing names where the page was, query kept to view/item
    assert got["inTrayLater"]["hows"] == ["load", "resume"], got["inTrayLater"]       # 31 s after the load: a plain resume is told
    assert got["throttle"]["hows"] == ["load", "resume"], got["throttle"]            # four returns inside 30 s: told ONCE


# --- L3 round 1 fixes (1106-53, tapcard3): the busy resume, the stale retry ---------------------------------------------

@needs_node
def test_a_resume_that_retries_tells_no_plain_resume_before_the_forward() -> None:
    got = _resume()
    assert got["retrySkew"]["assigned"] == [NC] and got["retrySkew"]["hows"] == ["load", "resume>shown"], got["retrySkew"]
    # in the tray at the retry as well: nothing was taken, so ONE plain resume is told, after the retry
    assert got["retryNone"]["assigned"] == [] and got["retryNone"]["hows"] == ["load", "resume"], got["retryNone"]


@needs_node
def test_a_resume_while_a_check_is_running_tells_no_spurious_resume() -> None:
    got = _resume()
    assert got["burst"]["assigned"] == [NC] and got["burst"]["hows"] == ["load", "resume>shown"], got["burst"]


@needs_node
def test_the_retry_does_nothing_when_the_app_went_hidden_again() -> None:
    got = _resume()
    assert got["retryHidden"]["assigned"] == [] and got["retryHidden"]["kept"] is True, got["retryHidden"]   # the entry stays for a later launch


@needs_node
def test_a_landing_target_is_the_decoded_path_inside_the_grammar_or_slash_never_mangled() -> None:
    got = _resume()
    assert got["encPath"]["targets"] == ["/?view=needs"], got["encPath"]          # '/my doc' is refused, not turned into '/my20doc'
    assert got["encOk"]["targets"] == ["/sign/9?view=needs"], got["encOk"]
    assert got["encSlash"]["targets"] == ["/"], got["encSlash"]
    assert got["encItem"]["targets"] == ["/?view=needs"], got["encItem"]           # a trailing newline is not an item


@needs_node
def test_a_forwarding_landing_names_the_target_the_notification_carried_not_the_address_it_left() -> None:
    got = _resume()
    item = "/?view=needs&item=np-0b62cd3b"
    assert got["newAssign"]["assigned"] == ["https://box.example" + item] and got["newAssign"]["hows"] == ["load>new"], got["newAssign"]
    assert got["newAssign"]["targets"] == [item], got["newAssign"]           # not '/', the address the document held when it forwarded
    assert got["newHere"]["hows"] == ["load>new"] and got["newHere"]["targets"] == [item], got["newHere"]
    assert got["shownAssign"]["hows"] == ["load", "resume>shown"] and got["shownAssign"]["targets"] == ["/", item], got["shownAssign"]
