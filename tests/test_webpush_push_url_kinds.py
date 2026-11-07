"""Where a web push opens, for EVERY ping kind the sender can carry.

``center.push_url`` decides the payload ``url`` at send time (:func:`levain.webpush.sender.deliver`), and it overrides the
link a caller handed to ``sender.spawn`` for every kind but ``summary``. A caller that pins its own ``url=`` (flow's
band push passes ``/sign/<id>#sign=...&v=...`` for a kind ``attention`` ping) and a test that pins ``push_url`` for one
kind (``question``) never met: nothing joined the two. These tests do, so a change that lets a caller's link reach the
payload (or that moves the center link) is a red test, not a surprise on a phone.

The kinds are derived from the code that names them, not listed here: the presence map (every kind the policy knows),
the center's non-item kinds, the band's default ping kind and the test ping the CLI sends.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from levain.webpush import center, held, notify, presence, sender
from levain.webpush.store import PushStore

# the kinds a ping can carry: derived, so a kind added to the policy map is covered without editing this file
BAND_DEFAULT_KIND = "question"      # levain.consent.band._send_ping's default; the coordinator's row is "attention"
TEST_PING_KIND = "test"             # levain.webpush.cli / web_server self-test pings
ALL_KINDS = sorted(set(presence.KIND_CLASSES) | set(center._NOT_ITEMS) | {BAND_DEFAULT_KIND, TEST_PING_KIND})
ITEM_KINDS = [k for k in ALL_KINDS if k not in center._NOT_ITEMS]     # a ping of these is a center item p<id>
NOTICE_KINDS = [k for k in center._NOT_ITEMS if k != "summary"]       # notify, notify_urgent: a notice row n<id>

# what flow's band push hands the sender today (flow phone_bridge._web_url): the sign card, same-origin, fragment kept
SIGN_URL = "/sign/np-5bf5c022-00000001#sign=np-5bf5c022-00000001&v=a1b2c3d4"
CALLER_URLS = [SIGN_URL, "/?view=needs", "/x", "/sign/np-5bf5c022-00000001"]


def _center_link(item: str) -> str:
    """The center deep link for an item id (``p<ping id>`` or ``n<notice id>``): the page's own contract (``?view=`` and
    ``n=``), the one place the string is spelled in this file."""
    return f"/?view=notifications&n={item}"


def _store(tmp_path: Path) -> PushStore:
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    return PushStore(q)


def test_the_kind_set_is_derived_and_covers_the_kinds_the_policy_knows(tmp_path):
    """A guard on the derivation itself: a band ping, a consent ping, a held summary and both notice kinds are in it, and
    the item/notice split leaves nothing out."""
    assert {"attention", "question", "consent", "summary", "notify", "notify_urgent", "test"} <= set(ALL_KINDS)
    assert sorted(ITEM_KINDS + NOTICE_KINDS + ["summary"]) == ALL_KINDS


@pytest.mark.parametrize("kind", ITEM_KINDS)
@pytest.mark.parametrize("url", CALLER_URLS)
def test_an_item_kind_ping_opens_the_center_on_its_own_item_whatever_link_the_caller_gave(tmp_path, kind, url):
    s = _store(tmp_path)
    pid = s.add_ping(kind, "np-5bf5c022", "levain", "body")
    assert center.push_url(s, pid, kind, url) == _center_link(f"p{pid}")


def test_a_band_attention_ping_given_a_sign_link_goes_out_as_the_center_link_not_the_sign_link(tmp_path):
    """The case the flow band push is: kind ``attention``, url = the sign card. The payload is the center link with the
    ``n=`` correlation; the sign link is drawn inline by the center's focus view and never travels in the payload."""
    s = _store(tmp_path)
    pid = s.add_ping("attention", "np-5bf5c022", "levain", "Pick a host.\na) yes · b) no · Rec a")
    got = center.push_url(s, pid, "attention", SIGN_URL)
    assert got == _center_link(f"p{pid}")
    assert "/sign/" not in got and "#" not in got and "sign=" not in got


@pytest.mark.parametrize("url", [held.SUMMARY_URL, held.NOTICES_SUMMARY_URL])
def test_a_held_summary_keeps_the_link_it_was_given(tmp_path, url):
    """A summary is no item: it is the one kind whose own link reaches the payload (``/?view=needs`` for the held
    Needs-you digest, ``/?view=notifications`` for the held notices digest)."""
    s = _store(tmp_path)
    pid = s.add_ping("summary", None, "levain", "3 held")
    assert center.push_url(s, pid, "summary", url) == url


def test_a_summary_keeps_its_link_even_when_a_notice_row_points_at_its_ping(tmp_path):
    """Without a notice row the non-item branch also keeps the link, so the summary special case is only observable when
    a notice row names the summary's ping: it must still not become ``n<id>``."""
    s = _store(tmp_path)
    pid = s.add_ping("summary", None, "levain", "3 held")
    s.add_notice("svc", "t", "b", "normal", "tok", "pushed", ping_id=pid)
    assert center.push_url(s, pid, "summary", held.SUMMARY_URL) == held.SUMMARY_URL


def test_the_held_needs_summary_link_is_the_needs_view():
    assert held.SUMMARY_URL == "/?view=needs"


@pytest.mark.parametrize("kind", NOTICE_KINDS)
def test_a_notice_kind_ping_opens_its_notice_and_a_pruned_notice_keeps_the_link(tmp_path, kind):
    s = _store(tmp_path)
    ping_id, nid = s.record_notice(kind, "notice:1", "[svc] t", "svc", "t", "b", "urgent" if kind == "notify_urgent" else "normal",
                                   "tok", "pushed")
    assert center.push_url(s, ping_id, kind, notify.NOTICES_URL) == _center_link(f"n{nid}")
    orphan = s.add_ping(kind, None, "levain", "x")                  # a ping with no notice row (the record pruned it)
    assert center.push_url(s, orphan, kind, notify.NOTICES_URL) == notify.NOTICES_URL


def test_an_unreadable_record_keeps_the_given_link_for_an_item_kind(tmp_path):
    """The documented degrade: a push is never lost to a record it cannot read."""
    s = _store(tmp_path)
    pid = s.add_ping("attention", None, "levain", "x")

    class _Broken:
        def read(self):
            raise RuntimeError("db gone")

    assert center.push_url(_Broken(), pid, "attention", SIGN_URL) == SIGN_URL      # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_the_sender_builds_its_payload_from_push_url_not_from_the_callers_link(tmp_path, monkeypatch, kind):
    """End to end through :func:`sender.deliver`: the ``url`` handed to the payload builder for a band-shaped call is the
    one ``push_url`` returns, for every kind. Only a ``summary`` carries the caller's link through."""
    s = _store(tmp_path)
    pid = s.add_ping(kind, "np-5bf5c022", "levain", "body")
    seen: list[str] = []
    real = sender.build_payload

    def spy(title, body, tag, url, ping_id, badge=None):
        seen.append(url)
        return real(title, body, tag, url, ping_id, badge)

    monkeypatch.setattr(sender, "build_payload", spy)
    sender.deliver(tmp_path / "q", s, pid, kind=kind, ref="np-5bf5c022", title="levain",
                   body="body", url=SIGN_URL)
    assert seen == [center.push_url(s, pid, kind, SIGN_URL, ref="np-5bf5c022")]
    if kind == "attention":
        assert seen == ["/?view=needs&item=np-5bf5c022"]      # a band row's ping opens its Needs card (Phill 11-07, np-029b717e (a))
    elif kind == "summary":
        assert seen == [SIGN_URL]
    elif kind in NOTICE_KINDS:
        assert seen == [SIGN_URL]          # no notice row was recorded for this ping: it keeps the link it was given
    else:
        assert seen == [_center_link(f"p{pid}")]
        assert not re.search(r"/sign/|#", seen[0])


def test_the_payload_json_the_phone_receives_carries_the_center_link_for_a_band_ping(tmp_path):
    """The bytes, not the builder's argument: the ``url`` field of the JSON the service worker reads."""
    s = _store(tmp_path)
    pid = s.add_ping("attention", "np-5bf5c022", "levain", "Pick a host.")
    data = json.loads(sender.build_payload("levain", "Pick a host.", "np-5bf5c022",
                                           center.push_url(s, pid, "attention", SIGN_URL), pid))
    assert data["url"] == _center_link(f"p{pid}")
    assert data["ping"] == pid
