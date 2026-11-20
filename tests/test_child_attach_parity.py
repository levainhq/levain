"""The warm derive child is used only while its cockpit is attached like the serve's (levain#183).

The serve's cockpit gets its consent reader, question store and note store attached after it is built; the child builds its
own cockpit from a factory and holds only what that factory attached. A panel derived there without a reader the serve has
would read short or blind with nothing to show for it, so the host asks the child which of the three it holds (the ready
frame's ``attached`` list) and routes to it only while it holds every one the serve's cockpit holds.

These tests use the S4a stub factory and its ``attach_factory`` variant over a real process, because the failure this closes
is one no stub child can show: every earlier test built its child from a factory that attached nothing, over a host whose
cockpit attached nothing, so the two always matched."""

from __future__ import annotations

import logging
import os
import types
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit import derive_child, derive_child_client as client_mod
from levain.cockpit.derive_child_client import PROTOCOL, DeriveChild
from levain.cockpit import snapshot_host as sh
from levain.cockpit.snapshot_host import SnapshotHost

from tests import test_derive_child_stub as stub
from tests.test_snapshot_host_route import FakeChild, Procs

ATTACH_FACTORY = "test_derive_child_stub:attach_factory"
ATTACH_ENV = "DERIVE_CHILD_STUB_ATTACH"


@pytest.fixture
def procs(tmp_path: Path):
    p = Procs(tmp_path)
    try:
        yield p
    finally:
        p.cleanup()


@pytest.fixture
def make_host(tmp_path: Path, monkeypatch):
    made: list[SnapshotHost] = []
    monkeypatch.setenv("DERIVE_CHILD_STUB_DIR", str(tmp_path))

    def make(ck: Any = None, **kw: Any) -> SnapshotHost:
        root = tmp_path / f"host{len(made)}"
        root.mkdir()
        host = SnapshotHost(ck if ck is not None else stub.factory(), root, **kw)
        made.append(host)
        return host

    yield make
    for h in made:
        h.stop(timeout=10)


def _child(procs: Procs, monkeypatch, attach: str) -> DeriveChild:
    """A started client whose child's factory attaches the readers in ``attach`` (a comma list)."""
    monkeypatch.setenv(ATTACH_ENV, attach)
    c = procs.client(ATTACH_FACTORY)
    assert c.start(30.0)
    procs.note(c)
    return c


def _who(host: SnapshotHost) -> int:
    """The pid that derived the ``pid`` panel, by one derive through the host's own decision (the loop derives a panel again
    only when something moved, so a second ``run_once`` would not decide anything)."""
    res = host._derive("pid")
    assert res.status == "ok", res
    return int(res.body["rows"][0]["title"].split()[0])


def _serve_cockpit(*attach: str) -> Any:
    ck = stub.factory()
    if "consent" in attach:
        ck.attach_consent_rows(lambda now: None)
    if "questions" in attach:
        ck.attach_question_rows(object())
    if "notes" in attach:
        ck.attach_note_rows(object())
    return ck


# ------------------------------------------------------------------------------------------------ routing by presence


def test_a_child_that_lacks_the_consent_reader_is_not_asked_and_the_gap_is_named(make_host, procs, monkeypatch):
    """The serve's cockpit holds a consent reader; the child's factory attached none. The heavy panel must derive HERE
    (the child would derive it blind), and the host must say which attachment is missing."""
    c = _child(procs, monkeypatch, "")
    host = make_host(_serve_cockpit("consent"), heavy={"pid"}, derive_child=c)
    assert _who(host) == os.getpid()
    assert host.child_attach_gap == ("consent",)
    assert host.child_fallbacks == 0


def test_a_child_that_holds_the_consent_reader_gets_the_same_panel(make_host, procs, monkeypatch):
    c = _child(procs, monkeypatch, "consent")
    host = make_host(_serve_cockpit("consent"), heavy={"pid"}, derive_child=c)
    assert _who(host) == c.pid != os.getpid()
    assert host.child_attach_gap == ()


def test_the_child_must_hold_every_attachment_the_serve_holds_and_may_hold_more(make_host, procs, monkeypatch):
    """Missing one of two is a gap (the other being held does not cover it); a child that holds more than the serve is
    not a gap, since the check is that the serve's set is inside the child's."""
    short = _child(procs, monkeypatch, "consent")
    host = make_host(_serve_cockpit("consent", "questions"), heavy={"pid"}, derive_child=short)
    assert _who(host) == os.getpid() and host.child_attach_gap == ("questions",)
    more = _child(procs, monkeypatch, "consent,questions,notes")
    host2 = make_host(_serve_cockpit("consent"), heavy={"pid"}, derive_child=more)
    assert _who(host2) == more.pid and host2.child_attach_gap == ()


def test_a_serve_with_nothing_attached_routes_to_a_child_with_nothing_attached_as_before(make_host, procs, monkeypatch):
    """An install with no readers attached, and the panels that read no attachment, must not lose the child."""
    c = _child(procs, monkeypatch, "")
    host = make_host(_serve_cockpit(), heavy={"pid"}, derive_child=c)
    assert _who(host) == c.pid
    assert host.child_attach_gap == () and host.child_fallbacks == 0


def test_a_consent_reader_attached_after_the_host_exists_moves_the_next_derive_in_process_with_no_restart(make_host, procs, monkeypatch):
    """The serve attaches after the host is built (and a reader may be swapped after the host starts), so the serve's side is
    read at each decision: a value read once at construction would route to a blind child."""
    c = _child(procs, monkeypatch, "")
    ck = _serve_cockpit()
    host = make_host(ck, heavy={"pid"}, derive_child=c)
    assert _who(host) == c.pid and host.child_attach_gap == ()
    ck.attach_consent_rows(lambda now: None)
    assert _who(host) == os.getpid()
    assert host.child_attach_gap == ("consent",)


def test_the_child_is_used_again_the_moment_the_sets_match(make_host, procs, monkeypatch):
    c = _child(procs, monkeypatch, "consent")
    ck = _serve_cockpit("consent", "notes")
    host = make_host(ck, heavy={"pid"}, derive_child=c)
    assert _who(host) == os.getpid() and host.child_attach_gap == ("notes",)
    ck._dec.notes = None            # the serve's cockpit no longer holds it: the sets match
    assert _who(host) == c.pid and host.child_attach_gap == ()


def test_a_gap_is_no_child_failure_it_counts_no_fallback_starts_no_backoff_and_leaves_the_child_alive(make_host, procs, monkeypatch):
    c = _child(procs, monkeypatch, "")
    pid = c.pid
    host = make_host(_serve_cockpit("consent", "questions", "notes"), heavy={"pid", "fast"}, derive_child=c)
    for _ in range(3):
        _who(host)
    assert host.child_fallbacks == 0
    assert c._failures == 0 and c._not_before == 0.0
    assert c.pid == pid and c.knows("pid")


def test_a_gap_keeps_every_heavy_panel_in_process_a_promoted_one_too(make_host):
    child = FakeChild()
    child.attached = frozenset()                                    # type: ignore[attr-defined]
    host = make_host(_serve_cockpit("questions"), heavy={"fast"}, derive_child=child)
    host._promoted.add("partial")
    assert not host._routed("fast") and not host._routed("partial")
    host.run_once()
    assert child.asked() == [] and host.child_attach_gap == ("questions",)


def test_a_gap_is_logged_when_it_changes_and_not_once_per_derive(make_host, caplog):
    child = FakeChild()
    child.attached = frozenset()                                    # type: ignore[attr-defined]
    ck = _serve_cockpit("consent")
    host = make_host(ck, heavy={"fast"}, derive_child=child)
    with caplog.at_level(logging.INFO, logger="levain.cockpit.snapshot_host"):
        for _ in range(4):
            host._routed("fast")
        first = [r for r in caplog.records if "consent" in r.getMessage()]
        assert len(first) == 1
        ck.attach_question_rows(object())
        for _ in range(4):
            host._routed("fast")
        child.attached = frozenset({"consent", "questions"})        # type: ignore[attr-defined]
        for _ in range(4):
            host._routed("fast")
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 3, lines            # the gap appearing, the gap changing, the gap closing
    assert "consent" in lines[0] and "questions" in lines[1]


def test_a_child_that_has_not_been_ready_is_no_gap_to_name_and_is_kept_out_by_knows(make_host):
    """``attached`` is None until a child has been ready: nothing is known yet, which is no gap to name. What keeps such a
    child out of the routing is ``knows`` (false until it has been ready), so the real client is never routed to then."""
    child = FakeChild()
    child.attached = None                                           # type: ignore[attr-defined]
    host = make_host(_serve_cockpit("consent"), heavy={"fast"}, derive_child=child)
    assert host.child_attach_gap == ()
    unready = DeriveChild("x:y")
    assert unready.attached is None and not unready.knows("fast")
    host2 = make_host(_serve_cockpit("consent"), heavy={"fast"}, derive_child=unready)
    assert not host2._routed("fast") and host2.child_attach_gap == ()
    unready.close()


# ------------------------------------------------------------------------------------------------ the ready frame


@pytest.mark.parametrize("held", ["", "consent", "questions", "notes", "notes,consent", "consent,questions,notes"])
def test_the_ready_frame_lists_exactly_the_cockpits_three_attachments_sorted(procs, monkeypatch, held):
    c = _child(procs, monkeypatch, held)
    assert c.attached == frozenset(n for n in held.split(",") if n)


def test_the_child_names_an_attachment_by_the_cockpit_property_that_holds_it():
    ck = types.SimpleNamespace(consent_reader=None, question_store=object(), note_store=None)
    assert derive_child._attached_of(ck) == ["questions"]
    ck = types.SimpleNamespace(consent_reader=lambda now: None, question_store=object(), note_store=object())
    assert derive_child._attached_of(ck) == ["consent", "notes", "questions"]
    assert derive_child._attached_of(types.SimpleNamespace(consent_reader=None, question_store=None, note_store=None)) == []
    assert sh.attached_of(ck) == frozenset({"consent", "notes", "questions"})


def _client_reading(frame: "dict[str, Any]") -> DeriveChild:
    c = DeriveChild("x:y")
    c._frame = lambda deadline, text: frame                         # type: ignore[method-assign]
    c._spawned_at = c._clock()
    return c


def _ready(**extra: Any) -> "dict[str, Any]":
    return dict({"op": "ready", "pid": 4242, "protocol": PROTOCOL, "panels": ["a"]}, **extra)


def test_a_ready_frame_without_the_key_reads_as_an_empty_set_never_as_all():
    c = DeriveChild("x:y")
    assert c.attached is None                       # no child has been ready
    c = _client_reading(_ready())
    c._await_ready(types.SimpleNamespace(pid=4242), c._clock() + 5)
    assert c.attached == frozenset() and c.knows("a")


def test_a_ready_frame_with_the_key_is_kept():
    c = _client_reading(_ready(attached=["consent", "notes"]))
    c._await_ready(types.SimpleNamespace(pid=4242), c._clock() + 5)
    assert c.attached == frozenset({"consent", "notes"})


@pytest.mark.parametrize("bad", ["consent", [1], [None], {"consent": 1}, ["consent"] * (getattr(client_mod, "READY_ATTACHED_MAX", 16) + 1)])
def test_a_badly_formed_attached_list_is_a_bad_reply(bad):
    c = _client_reading(_ready(attached=bad))
    with pytest.raises(client_mod._Lost) as err:
        c._await_ready(types.SimpleNamespace(pid=4242), c._clock() + 5)
    assert err.value.kind == "bad_reply" and c.attached is None
