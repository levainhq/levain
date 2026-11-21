"""levain#225, the verify hook of the session fire: no card, no ``done``. Driven through the PRODUCTION ``fire_factory``
path (as ``test_agents_session_wire_fire`` does) with a Stub whose ``report(session_key)`` answers what a launcher would.

The report is UNTRUSTED DATA: it is filed as text in a card's body, and the card's ``usage`` block is the launcher's
root-side reading, never anything the report says.
"""
from __future__ import annotations

import shutil

import pytest

from levain.agents import controller as ctl
from levain.agents import gauges, supervise
from levain.agents import session_wire as W
from levain.agents.accounting import Usage
from levain.contracts.canon import canonical
from levain.desk import review
from levain.desk.store import CoordStore, StoreAbsent
from tests.test_agents_session_launch import Stub, rig  # noqa: F401
from tests.test_agents_session_wire import _AcceptAll, _coord, _install, _session_occurrence
from tests.test_agents_session_wire_fire import _clock, _fire, _wired  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

CAP = 32 * 1024


@pytest.fixture(autouse=True)
def _accept(monkeypatch):
    monkeypatch.setattr(gauges, "_CHECKS", _AcceptAll())


def _night(rig, monkeypatch, *, report, coord=True, **stub_kw):
    oid = _session_occurrence(rig)
    stub = Stub(**stub_kw)
    stub.report_bytes = report
    W.register_launcher("stub", lambda store: stub)
    _install(rig, launcher="stub", **({} if coord else {"coord_dir": None}))
    code = _fire(rig, oid, monkeypatch)
    return oid, code, rig.row(oid)


def _cards(rig):
    try:
        return review.list_items(CoordStore(_coord(rig)))
    except StoreAbsent:             # nothing was ever filed: no store was made
        return []


# ---- the happy night ---------------------------------------------------------------------------------------------------

def test_a_happy_night_files_one_card_and_ends_done(rig, monkeypatch):
    oid, code, row = _night(rig, monkeypatch, report=b"  # Night done \n\nbody line\n")
    assert (code, row["state"], row["reason"]) == (0, "done", "completed")
    (card,) = _cards(rig)
    b = card.body
    assert card.feed == "agent" and card.authority_relevant is False
    assert b["kind"] == "session-report" and b["occurrence"] == oid and b["agent"]
    assert b["summary"] == "# Night done" and b["report"] == "  # Night done \n\nbody line\n"
    assert b["bytes"] == len(b"  # Night done \n\nbody line\n") and len(b["sha256"]) == 64
    assert len(canonical(b)) <= 8000


def test_a_duplicate_end_delivery_files_one_card(rig, monkeypatch):
    oid, _code, _row = _night(rig, monkeypatch, report=b"once")
    monkeypatch.setattr(gauges, "_grant_held", lambda store, occ: True)     # the first delivery released the grant
    epoch = rig.row(oid)["launch_epoch"]
    verify = W.fire_factory(rig.store, oid, epoch, clock=rig.now)["sup"]._hooks.verify
    end = supervise.End(oid, "done", "completed", Usage(0, None, None, "stub"))
    assert verify(end) is None and verify(end) is None
    assert len(_cards(rig)) == 1


def test_the_card_carries_root_usage_and_the_caps_apart_from_the_report(rig, monkeypatch):
    liar = b'{"usage": {"actions": 0, "tunnel_bytes": 0, "seconds": 0, "basis": "report"}}'
    used = Usage(0, None, None, "stub:basis", actions=11, tunnel_bytes=222, seconds=33)
    _oid, _code, row = _night(rig, monkeypatch, report=liar, used=used)
    assert row["state"] == "done"
    (card,) = _cards(rig)
    u = card.body["usage"]
    assert (u["actions"], u["tunnel_bytes"], u["seconds"], u["basis"]) == (11, 222, 33, "stub:basis")
    assert card.body["report"] == liar.decode()             # the report is data, left apart from the usage
    assert set(u["caps"]) <= {"actions", "tunnel_bytes", "seconds"} and all(type(v) is int for v in u["caps"].values())


def test_an_unread_figure_stays_null_in_the_card(rig, monkeypatch):
    _oid, _code, _row = _night(rig, monkeypatch, report=b"x", used=Usage(0, None, None, "stub"))
    (card,) = _cards(rig)
    assert card.body["usage"]["actions"] is None and card.body["usage"]["seconds"] is None


def test_a_long_report_is_cut_to_fit_with_a_visible_marker(rig, monkeypatch):
    text = ("line é中\"\\ \n" * 2000).encode()          # under the byte cap, far over the body limit
    assert len(text) < CAP
    _oid, _code, row = _night(rig, monkeypatch, report=text)
    assert row["state"] == "done"
    (card,) = _cards(rig)
    r = card.body["report"]
    assert len(canonical(card.body)) <= 8000 and r.endswith(f" of {len(text)} bytes]") and "[cut at " in r
    assert card.body["bytes"] == len(text)


# ---- one fault per reason ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("report,reason", [
    (None, "report_missing"),
    (b"a" * (CAP + 1), "report_oversize"),
    (b"", "report_malformed"),
    (b"abc\x00def", "report_malformed"),
    (b"\xff\xfe bad utf8", "report_malformed"),
])
def test_a_bad_report_turns_done_into_failed_and_files_nothing(rig, monkeypatch, report, reason):
    _oid, _code, row = _night(rig, monkeypatch, report=report)
    assert (row["state"], row["reason"]) == ("failed", reason)
    assert _cards(rig) == []


def test_a_report_of_exactly_the_cap_is_accepted(rig, monkeypatch):
    _oid, _code, row = _night(rig, monkeypatch, report=b"a" * CAP)
    assert row["state"] == "done" and len(_cards(rig)) == 1


@pytest.mark.parametrize("name,reason", [("report-oversize", "report_oversize"), ("report-malformed", "report_malformed"),
                                         ("report-unreadable", "report_missing")])
def test_a_launcher_refusal_maps_to_its_reason(rig, monkeypatch, name, reason):
    from levain.agents.session_launcher import LauncherRefused
    _oid, _code, row = _night(rig, monkeypatch, report=LauncherRefused(name, "x"))
    assert (row["state"], row["reason"]) == ("failed", reason)


def test_no_coord_dir_is_card_not_filed(rig, monkeypatch):
    _oid, _code, row = _night(rig, monkeypatch, report=b"fine", coord=False)
    assert (row["state"], row["reason"]) == ("failed", "card_not_filed")


def test_a_store_that_raises_is_card_not_filed(rig, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr(review, "file_item", boom)
    _oid, _code, row = _night(rig, monkeypatch, report=b"fine")
    assert (row["state"], row["reason"]) == ("failed", "card_not_filed")


def test_a_non_done_end_files_nothing(rig, monkeypatch):
    oid, _code, _row = _night(rig, monkeypatch, report=b"fine")
    n = len(_cards(rig))
    epoch = rig.row(oid)["launch_epoch"]
    verify = W.fire_factory(rig.store, oid, epoch, clock=rig.now)["sup"]._hooks.verify
    other = "occ-other"
    for state in ("failed", "cancelled", "launched"):
        assert verify(supervise.End(other, state, "x", Usage(0, None, None, "stub"))) is None
    assert len(_cards(rig)) == n


def test_the_win_verifiers_reason_wins_and_no_card_is_filed(rig, monkeypatch):
    class _RefuseAll(dict):
        def get(self, name, default=None):
            return lambda ctx: "win_not_met"

    monkeypatch.setattr(gauges, "_CHECKS", _RefuseAll())
    _oid, _code, row = _night(rig, monkeypatch, report=b"fine")
    assert (row["state"], row["reason"]) == ("failed", "win_not_met")
    assert _cards(rig) == []


def test_a_process_wide_verify_reason_wins_and_no_card_is_filed(rig, monkeypatch):
    monkeypatch.setattr(supervise, "HOOKS", supervise.Hooks(verify=lambda end: "audit_no"))
    _oid, _code, row = _night(rig, monkeypatch, report=b"fine")
    assert (row["state"], row["reason"]) == ("failed", "audit_no")
    assert _cards(rig) == []


def test_a_supervisor_not_built_by_session_wire_still_ends_done_with_no_report_and_no_card(rig, monkeypatch):
    # the hook is per fire: a fire whose Supervisor carries no session hooks (``hooks=None``) is judged by the win verifier alone
    monkeypatch.setattr(W, "_report_hooks", lambda *a, **k: None)
    _oid, _code, row = _night(rig, monkeypatch, report=None)
    assert (row["state"], row["reason"]) == ("done", "completed")
    assert not (_coord(rig) / "dispatch.db").exists() or _cards(rig) == []


# ---- the install ------------------------------------------------------------------------------------------------------

def test_the_install_round_trips_a_coord_dir_and_refuses_a_relative_one(rig):
    path = W.write_install(rig.store.directory, cockpit_root=rig.world.root, coord_dir=_coord(rig))
    assert W.read_install(rig.store.directory).coord_dir == _coord(rig) and path.exists()
    with pytest.raises(W.InstallRefused):
        W.write_install(rig.store.directory, cockpit_root=rig.world.root, coord_dir="relative/coord")
    W.write_install(rig.store.directory, cockpit_root=rig.world.root)
    assert W.read_install(rig.store.directory).coord_dir is None


def test_the_brief_tail_names_the_report_file_cap_and_data_status():
    from levain.agents import session as S
    role = {"brief": {"slots": {}, "template": "do it"}}
    text = S.render_brief(role, {}, occurrence_id="occ-1", session_key="s-0123456789abcdef")
    assert "`.levain-report.md`" in text and "32 KiB" in text and "as data" in text and "ends failed" in text
    assert text.index("This is occurrence") < text.index(".levain-report.md")


def test_the_card_body_bound_is_the_review_stores():
    assert W._BODY_MAX == 2 * review._TEXT_MAX
