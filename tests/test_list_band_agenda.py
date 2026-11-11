"""THE LIST's band row to agenda (row 183, Phill 11-10 / 11-11 Q1-Q5): ``band_agenda`` is a row verb on an open ``band:`` row
that rings no reminder. Two acts in a fixed order, tier C1 with confirm: (1) plant a dated agenda (the row's words, the day,
target band, the question id in its pointer) under a key derived from the question id alone; THEN (2) answer the row with the
fixed deferral words. A crash between them leaves the row open with its agenda planted, and the retry lands no second agenda."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from levain import band_agenda
from levain.cockpit.verbs import dispatch
from levain.consent import band
from levain.consent.questions import SNOOZE_MAX_DAYS
from levain.writes import EditError
from tests.test_cockpit_list_panel import CRED, INSTALL_CLASS, TODAY, Rig
from tests.test_intention_controls import HostStore

PAST = (TODAY + timedelta(days=SNOOZE_MAX_DAYS + 1)).isoformat()      # the first day past the snooze window
LATER = (TODAY + timedelta(days=9)).isoformat()


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    monkeypatch.setattr(band, "deliver_answer", lambda *a, **k: None)      # the answer's delivery is not this verb's test
    return Rig(tmp_path)


def _fire(rig: Rig, rid: str, day: Any, *, confirm: bool = True) -> dict[str, Any]:
    row = rig.rows()[rid]
    return dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                    install_class=INSTALL_CLASS,
                    req={"verb": "band_agenda", "panel_id": "tray", "row_id": rid, "row_version": row["version"],
                         "params": {"day": day}, "confirm": confirm})


def _agendas(rig: Rig) -> list[dict[str, Any]]:
    return [s for s in rig.sp.list_open() if str(s.get("origin_key", "")).startswith(band_agenda.KEY_PREFIX)]


def _audit(rig: Rig) -> list[dict[str, Any]]:
    p = rig.src.write_scope.ledger_root / "edits.jsonl"
    lines = p.read_text().splitlines() if p.is_file() else []
    return [r for r in map(json.loads, lines) if r.get("kind") == "band_agenda"]


def _fail(monkeypatch: pytest.MonkeyPatch, seam: str) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError(f"injected: {seam} failed")
    monkeypatch.setattr(band_agenda, seam, boom)


def _row(rig: Rig) -> str:
    return f"band:{rig.q.id}"


# --- who is offered the verb ---------------------------------------------------------------------------------------


def test_an_open_band_row_with_no_reminder_is_offered_the_verb_and_nothing_else_new(rig: Rig) -> None:
    acts = {a["verb"]: a for a in rig.rows()[_row(rig)]["actions"]}
    assert set(acts) == {"band_agenda"} and acts["band_agenda"]["tier"] == "C1"
    for rid in ("board:levain:a",):                       # a board row stays read-only
        assert all(a["verb"] != "band_agenda" for a in rig.rows()[rid]["actions"])
    assert all(a["verb"] != "band_agenda" for a in rig.rows()[f"spore:{rig.ids['plain']}"]["actions"])


def test_a_row_that_rings_a_reminder_is_not_offered_it_and_a_plain_row_beside_it_is(tmp_path: Path) -> None:
    rig = Rig(tmp_path, band=False)
    rig.ck.attach_question_rows(HostStore())
    rows = rig.rows()
    assert "band_agenda" not in {a["verb"] for a in rows["band:np-ring0001"]["actions"]}
    assert {a["verb"] for a in rows["band:np-plain002"]["actions"]} == {"band_agenda"}


def test_a_snoozed_band_row_is_offered_it_too(rig: Rig) -> None:
    rig.qs.snooze(rig.q.id, until=(TODAY + timedelta(days=2)).isoformat(), channel="cli", via="cli")
    row = rig.rows()[_row(rig)]
    assert row["facets"].get("held_until") and {a["verb"] for a in row["actions"]} == {"band_agenda"}
    out = _fire(rig, _row(rig), PAST)
    assert out["ok"] and len(_agendas(rig)) == 1


# --- the happy path ------------------------------------------------------------------------------------------------


def test_a_fire_plants_the_agenda_then_answers_the_row_with_the_fixed_words_and_returns_the_receipt(rig: Rig) -> None:
    out = _fire(rig, _row(rig), PAST)
    [agenda] = _agendas(rig)
    assert agenda["text"] == "Ship the sweep fix tonight?" and agenda["next"] == PAST
    assert agenda["target"] == "band" and agenda["disposition"] == "agenda" and agenda["pointer"] == f"question:{rig.q.id}"
    assert agenda["id"].startswith("intention-")
    answered = rig.qs.get(rig.q.id).answer
    assert answered.kind == "text" and answered.channel == "cockpit"
    assert answered.value == f"deferred: comes back {PAST} as {agenda['id']}; do not proceed before then"
    assert out["receipt_line"] == f"-> Later, wakes {PAST} · band ({agenda['id']})"
    assert out["agenda"] == agenda["id"] and out["day"] == PAST and "earlier_day_kept" not in out
    [line] = _audit(rig)
    assert line["source"] == f"spore:{agenda['id']}" and line["question"] == rig.q.id and line["undoable"] is False


def test_the_answer_is_delivered_to_the_stopped_seat_as_any_cockpit_answer_is(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    seen: list[str] = []
    done = threading.Event()
    monkeypatch.setattr(band, "deliver_answer", lambda store, ref, **kw: (seen.append(ref), done.set()))
    _fire(rig, _row(rig), PAST)
    assert done.wait(5) and seen == [rig.q.id]


def test_the_verb_needs_confirm_as_the_keep_does(rig: Rig) -> None:
    with pytest.raises(EditError) as ei:
        _fire(rig, _row(rig), PAST, confirm=False)
    assert ei.value.code == "confirm_required"
    assert _agendas(rig) == [] and rig.qs.get(rig.q.id).answer is None


def test_the_agenda_key_is_the_question_id_alone_in_anneals_grammar() -> None:
    import hashlib
    import re

    k = band_agenda.agenda_origin_key("q-abc123")
    assert k == "band-agenda:" + hashlib.sha256(b"q-abc123").hexdigest()
    assert re.fullmatch(r"[0-9A-Za-z._:-]{1,128}", k) and k != band_agenda.agenda_origin_key("q-abc124")


# --- refusals write nothing ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("offset", [1, SNOOZE_MAX_DAYS])
def test_a_day_inside_the_snooze_window_is_refused_with_the_rule_and_the_remedy(rig: Rig, offset: int) -> None:
    day = (TODAY + timedelta(days=offset)).isoformat()
    with pytest.raises(EditError) as ei:
        _fire(rig, _row(rig), day)
    words = str(ei.value)
    assert ei.value.http_status == 400 and f"{SNOOZE_MAX_DAYS} days" in words and "snooze the row instead" in words
    assert (TODAY + timedelta(days=SNOOZE_MAX_DAYS)).isoformat() in words
    assert _agendas(rig) == [] and rig.qs.get(rig.q.id).answer is None and _audit(rig) == []


@pytest.mark.parametrize("offset", [-1, 0])
def test_a_day_not_after_today_is_refused_without_pointing_at_a_snooze_that_refuses_it_too(rig: Rig, offset: int) -> None:
    day = (TODAY + timedelta(days=offset)).isoformat()
    with pytest.raises(EditError) as ei:
        _fire(rig, _row(rig), day)
    words = str(ei.value)
    assert ei.value.http_status == 400 and "snooze" not in words and TODAY.isoformat() in words
    assert (TODAY + timedelta(days=SNOOZE_MAX_DAYS)).isoformat() in words
    assert _agendas(rig) == [] and rig.qs.get(rig.q.id).answer is None and _audit(rig) == []


@pytest.mark.parametrize("day", ["2026-13-40", "next week", "2026-11-20T09:00", "", None, 20261120, ["2026-11-20"]])
def test_a_day_that_is_not_a_date_is_refused(rig: Rig, day: Any) -> None:
    with pytest.raises(EditError) as ei:
        _fire(rig, _row(rig), day)
    assert ei.value.http_status == 400 and "YYYY-MM-DD" in str(ei.value)
    assert _agendas(rig) == [] and rig.qs.get(rig.q.id).answer is None


def test_a_day_far_ahead_has_no_cap(rig: Rig) -> None:
    far = (TODAY + timedelta(days=900)).isoformat()
    assert _fire(rig, _row(rig), far)["day"] == far


def test_the_params_are_exactly_the_day(rig: Rig) -> None:
    row = rig.rows()[_row(rig)]
    with pytest.raises(EditError) as ei:
        dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                 install_class=INSTALL_CLASS,
                 req={"verb": "band_agenda", "panel_id": "tray", "row_id": row["id"], "row_version": row["version"],
                      "params": {"day": PAST, "target": "desk"}, "confirm": True})
    assert ei.value.code == "field_not_allowed"
    assert _agendas(rig) == []


def test_a_ringing_row_and_an_answered_row_are_refused_before_any_agenda(tmp_path: Path) -> None:
    rig = Rig(tmp_path, band=False)
    host = HostStore()
    rig.ck.attach_question_rows(host)
    with pytest.raises(EditError):                         # the ringing row is not offered the verb at all
        _fire(rig, "band:np-ring0001", PAST)
    with pytest.raises(EditError) as ei:                   # the same refusal one layer down, for a caller that skips the page
        band_agenda.defer(questions=host, spores_json=rig.src.anneal.spores_json, ledger_root=rig.src.write_scope.ledger_root,
                          question_id="np-ring0001", day=PAST, today=TODAY)
    assert ei.value.code == "rings_a_reminder" and "Snooze" in str(ei.value)
    assert _agendas(rig) == []
    row = rig.rows()["band:np-plain002"]
    host.rows = [r for r in host.rows if r.id != "np-plain002"]            # the row closed since the page read it
    with pytest.raises(EditError) as ei2:
        dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                 install_class=INSTALL_CLASS,
                 req={"verb": "band_agenda", "panel_id": "tray", "row_id": row["id"], "row_version": row["version"],
                      "params": {"day": PAST}, "confirm": True})
    assert ei2.value.code == "stale" and _agendas(rig) == []


# --- the order of the acts: agenda first, the answer after, the audit line after both ------------------------------


def test_a_failure_planting_the_agenda_leaves_the_row_open_and_answers_nothing(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as m:
        _fail(m, "_plant")
        with pytest.raises(EditError) as ei:
            _fire(rig, _row(rig), PAST)
        assert ei.value.http_status == 503
    assert rig.qs.get(rig.q.id).answer is None and _agendas(rig) == [] and _audit(rig) == []


def test_a_failure_after_the_agenda_leaves_the_row_open_and_the_retry_lands_one_agenda_then_answers(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as m:
        _fail(m, "_answer_deferred")
        with pytest.raises(EditError) as ei:
            _fire(rig, _row(rig), PAST)
        assert ei.value.http_status == 503 and "planted" in str(ei.value) and "retry" in str(ei.value)
    [first] = _agendas(rig)                                # the agenda is down ...
    assert rig.qs.get(rig.q.id).answer is None             # ... and the row is still open (agenda FIRST, answer after)
    assert _audit(rig) == []                               # the audit line waits for both acts
    out = _fire(rig, _row(rig), PAST)                      # the retry
    [again] = _agendas(rig)
    assert again["id"] == first["id"] and out["agenda"] == first["id"]
    assert rig.qs.get(rig.q.id).answer.value.endswith("do not proceed before then")
    assert len(_audit(rig)) == 1


def test_a_retry_with_a_different_day_keeps_the_day_already_planted_and_says_so(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as m:
        _fail(m, "_answer_deferred")
        with pytest.raises(EditError):
            _fire(rig, _row(rig), PAST)
    out = _fire(rig, _row(rig), LATER)
    [agenda] = _agendas(rig)
    assert agenda["next"] == PAST and out["day"] == PAST and out["earlier_day_kept"] is True
    assert PAST in out["receipt_line"] and "planted earlier" in out["receipt_line"] and LATER in out["receipt_line"]
    assert rig.qs.get(rig.q.id).answer.value == f"deferred: comes back {PAST} as {agenda['id']}; do not proceed before then"


def test_a_retry_whose_earlier_agenda_is_already_resolved_is_refused_not_answered(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as m:
        _fail(m, "_answer_deferred")
        with pytest.raises(EditError):
            _fire(rig, _row(rig), PAST)
    [first] = _agendas(rig)
    rig.sp.descend(first["id"], kind="composted")
    with pytest.raises(EditError) as ei:
        _fire(rig, _row(rig), PAST)
    assert ei.value.code == "agenda_resolved" and rig.qs.get(rig.q.id).answer is None


def test_a_second_identical_call_after_success_is_stale_and_plants_no_second_agenda(rig: Rig) -> None:
    row = rig.rows()[_row(rig)]
    _fire(rig, _row(rig), PAST)
    with pytest.raises(EditError) as ei:                   # through the cockpit: the row is gone
        dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                 install_class=INSTALL_CLASS,
                 req={"verb": "band_agenda", "panel_id": "tray", "row_id": row["id"], "row_version": row["version"],
                      "params": {"day": PAST}, "confirm": True})
    assert ei.value.code == "stale"
    with pytest.raises(EditError) as ei2:                  # one layer down: the question is answered
        band_agenda.defer(questions=rig.qs, spores_json=rig.src.anneal.spores_json,
                          ledger_root=rig.src.write_scope.ledger_root, question_id=rig.q.id, day=PAST, today=TODAY)
    assert ei2.value.code == "stale"
    assert len(_agendas(rig)) == 1 and len(_audit(rig)) == 1


class AnsweringHost(HostStore):
    """A host store whose ``answer`` records like flow's: it can refuse an unsigned text answer (flow's signed-answers setting),
    or close the row and then raise (flow's desk wake failing after the row resolved)."""

    def __init__(self) -> None:
        super().__init__()
        self.answer_refuses: str | None = None
        self.answer_raises: Exception | None = None
        self.raise_after_closing = False

    def answer(self, ref, *, option=None, text=None, channel):
        from dataclasses import replace as _replace

        from levain.cockpit.consent import ConsentError
        from levain.consent.questions import Answer

        if self.answer_refuses:
            raise ConsentError(self.answer_refuses)
        if self.answer_raises is not None:
            raise self.answer_raises
        q = self.get(ref)
        self.rows = [_replace(r, answer=Answer(r.id, "text", text, channel, "2026-11-08T11:00:00Z")) if r.id == q.id else r
                     for r in self.rows]
        if self.raise_after_closing:
            raise ConsentError(f"{q.id} is resolved but the desk was NOT woken (OSError); tell it")
        return self.rows


def _host_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Any) -> Rig:
    monkeypatch.setattr(band, "deliver_answer", lambda *a, **k: None)
    rig = Rig(tmp_path, band=False)
    rig.ck.attach_question_rows(host)
    return rig


def _defer(rig: Rig, host: Any, qid: str, day: str = PAST) -> dict[str, Any]:
    return band_agenda.defer(questions=host, spores_json=rig.src.anneal.spores_json, ledger_root=rig.src.write_scope.ledger_root,
                             question_id=qid, day=day, today=TODAY)


def _keyed(rig: Rig, qid: str) -> dict[str, Any] | None:
    return rig.sp.get_by_origin_key(band_agenda.agenda_origin_key(qid))


def test_a_signing_row_is_not_offered_the_verb_and_a_direct_call_refuses_writing_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from levain.consent.questions import Link

    host = AnsweringHost()
    host.rows = [*host.rows, replace(host.rows[1], n=13, id="np-sign0003", sign_request="sr-0003"),
                 replace(host.rows[1], n=14, id="np-link0004", link=Link("a", "push", "rq-4", "qu-4"))]
    rig = _host_rig(tmp_path, monkeypatch, host)
    rows = rig.rows()
    for qid in ("np-sign0003", "np-link0004"):
        assert "band_agenda" not in {a["verb"] for a in rows[f"band:{qid}"]["actions"]}, qid
        with pytest.raises(EditError) as ei:
            _defer(rig, host, qid)
        assert ei.value.code == "needs_signature" and "sign" in str(ei.value)
        assert _keyed(rig, qid) is None and host.get(qid).answer is None
    assert {a["verb"] for a in rows["band:np-plain002"]["actions"]} == {"band_agenda"}
    assert _audit(rig) == []


def test_a_host_that_refuses_the_unsigned_answer_after_the_plant_leaves_no_agenda_behind(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = AnsweringHost()
    host.answer_refuses = "needs a signed answer: open the sign link in its ping, a tap does not answer it"
    rig = _host_rig(tmp_path, monkeypatch, host)
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    words = str(ei.value)
    assert ei.value.code == "answer_refused" and "needs a signed answer" in words and "nothing remains planted" in words
    assert _agendas(rig) == [] and host.get("np-plain002").answer is None          # no orphan agenda, the row still open
    gone = _keyed(rig, "np-plain002")
    assert gone["status"] == "resolved" and gone["resolution"]["kind"] == "composted"
    assert any("needs a signed answer" in str(n) for n in gone.get("notes") or ())
    [line] = _audit(rig)
    assert line["outcome"] == "aborted" and line["question"] == "np-plain002"


def test_a_refused_answer_on_a_retry_keeps_the_agenda_an_earlier_call_planted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = AnsweringHost()
    rig = _host_rig(tmp_path, monkeypatch, host)
    with monkeypatch.context() as m:
        _fail(m, "_answer_deferred")
        with pytest.raises(EditError):
            _defer(rig, host, "np-plain002")
    host.answer_refuses = "needs a signed answer"
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    [agenda] = _agendas(rig)                                # an earlier call's agenda: the key's day stands (Q4)
    assert ei.value.code == "answer_refused" and agenda["id"] in str(ei.value) and "nothing remains planted" not in str(ei.value)


@pytest.mark.parametrize("exc", [ValueError("bad host"), AttributeError("no such field")])
def test_an_unexpected_answer_fault_with_the_row_still_open_leaves_no_agenda(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    host = AnsweringHost()
    host.answer_raises = exc
    rig = _host_rig(tmp_path, monkeypatch, host)
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    assert ei.value.code == "answer_refused" and "nothing remains planted" in str(ei.value)
    assert _agendas(rig) == [] and host.get("np-plain002").answer is None


def test_a_host_that_closes_the_row_and_then_raises_still_reports_the_deferral(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = AnsweringHost()
    host.raise_after_closing = True
    rig = _host_rig(tmp_path, monkeypatch, host)
    out = _defer(rig, host, "np-plain002")
    [agenda] = _agendas(rig)
    assert out["ok"] and out["agenda"] == agenda["id"] and "desk was NOT woken" in out["warning"]
    assert host.get("np-plain002").answer is not None and len(_audit(rig)) == 1


@pytest.mark.parametrize("answer", [None, lambda self, ref: None])
def test_a_host_with_no_text_answer_is_not_offered_the_verb_and_plants_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: Any) -> None:
    class NoAnswer(AnsweringHost):
        pass
    NoAnswer.answer = answer                                 # type: ignore[assignment]
    host = NoAnswer()
    rig = _host_rig(tmp_path, monkeypatch, host)
    assert "band_agenda" not in {a["verb"] for a in rig.rows()["band:np-plain002"]["actions"]}
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    assert ei.value.code == "not_built" and _keyed(rig, "np-plain002") is None


def test_an_older_hosts_ringing_row_is_refused_though_the_host_cannot_act_on_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_intention_controls import OlderHost

    host = OlderHost()
    rig = _host_rig(tmp_path, monkeypatch, host)
    assert "band_agenda" not in {a["verb"] for a in rig.rows()["band:np-ring0001"]["actions"]}
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-ring0001")
    assert ei.value.code == "rings_a_reminder" and "spore-9" in str(ei.value) and _keyed(rig, "np-ring0001") is None


def test_an_unreadable_band_row_is_not_offered_the_verb() -> None:
    from datetime import datetime, timezone

    from levain.cockpit import verbs
    from levain.cockpit.list_panel import _band_row

    row = _band_row({"id": "band:q-x", "title": "UNREADABLE: this band item could not be read", "unreadable": True},
                    TODAY, datetime(2026, 11, 11, 12, tzinfo=timezone.utc))
    assert row.facets["agenda_blocked"] == "unreadable"
    assert not verbs._PLAIN_BAND_ROW({"id": row.id, "facets": row.facets})


def test_the_agenda_is_one_atomic_add_with_no_second_write(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from levain import spores

    calls: list[str] = []
    real = spores.guarded_spore_store(rig.src.anneal.spores_json).__class__
    for name in ("add", "update", "apply", "descend"):
        orig = getattr(real, name)
        monkeypatch.setattr(real, name, lambda self, *a, _o=orig, _n=name, **k: (calls.append(_n), _o(self, *a, **k))[1])
    _fire(rig, _row(rig), PAST)
    assert calls == ["apply"]                              # one apply add: anneal's lock holds the key check and the write


# --- fix round 2 (L3 r1): the row read back decides; only this call's agenda is ever composted -------------------------
#
# THE INVARIANT: the operator is told a deferral landed only when the row's recorded answer IS this call's deferral; an
# agenda this call planted is never left standing when the row ended answered otherwise; a call never composts an agenda
# it did not plant.


def _other_answer(qid: str) -> Any:
    from levain.consent.questions import Answer

    return Answer(qid, "option", "a", "phone", "2026-11-08T11:00:00Z")


class RacingHost(AnsweringHost):
    """A host whose row is answered by someone else (the phone, the desk, another tab) around this call's answer."""

    def __init__(self) -> None:
        super().__init__()
        self.other: Any = None              # an Answer recorded INSTEAD of ours
        self.then_raise: Exception | None = None
        self.returns_without_recording = False
        self.get_fails_after_answer = False
        self._answered = False

    def answer(self, ref, *, option=None, text=None, channel):
        from dataclasses import replace as _replace

        self._answered = True
        if self.other is not None:
            self.rows = [_replace(r, answer=self.other) if r.id == ref else r for r in self.rows]
            if self.then_raise is not None:
                raise self.then_raise
            return self.rows
        if self.returns_without_recording:
            return None
        self._answered = False                            # the host's own read inside answer is not the read-back
        try:
            return super().answer(ref, option=option, text=text, channel=channel)
        finally:
            self._answered = True

    def get(self, ref):
        if self.get_fails_after_answer and self._answered:
            raise ValueError("1 unparseable needs_phill line(s): the open set may be incomplete")
        return super().get(ref)


@pytest.mark.parametrize("then_raise", [
    "already",          # flow's own words when the other answer won the race: the old substring shortcut said stale, kept the agenda
    "other",            # any other error after the row closed: the old re-read saw SOME answer and returned a receipt
    None,               # the host returned cleanly but the row records another answer
])
def test_a_row_answered_otherwise_is_never_reported_as_deferred_and_this_calls_agenda_is_composted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, then_raise: str | None) -> None:
    from levain.cockpit.consent import ConsentError

    host = RacingHost()
    host.other = _other_answer("np-plain002")
    host.then_raise = {"already": ConsentError("question np-plain002 was already answered: already resolved"),
                       "other": ConsentError("np-plain002 is resolved but the desk was NOT woken (OSError); tell it"),
                       None: None}[then_raise]
    rig = _host_rig(tmp_path, monkeypatch, host)
    delivered: list[str] = []
    monkeypatch.setattr(band, "deliver_answer", lambda store, ref, **kw: delivered.append(ref))
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    words = str(ei.value)
    assert ei.value.code == "stale" and "answered otherwise" in words and "did not land" in words
    assert "phone" in words and "nothing remains planted" in words
    assert _agendas(rig) == []                                            # no agenda of this call left standing
    gone = _keyed(rig, "np-plain002")
    assert gone["status"] == "resolved" and gone["resolution"]["kind"] == "composted"
    assert [line["outcome"] for line in _audit(rig)] == ["aborted"]       # no success line
    assert delivered == []                                                # nothing delivered as if it were ours


def test_a_row_answered_otherwise_around_an_earlier_attempts_agenda_leaves_that_agenda_and_says_so(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = RacingHost()
    rig = _host_rig(tmp_path, monkeypatch, host)
    with monkeypatch.context() as m:
        _fail(m, "_answer_deferred")
        with pytest.raises(EditError):
            _defer(rig, host, "np-plain002")
    [earlier] = _agendas(rig)
    host.other = _other_answer("np-plain002")
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    words = str(ei.value)
    assert ei.value.code == "stale" and "did not land" in words and earlier["id"] in words and "earlier attempt" in words
    [still] = _agendas(rig)                                               # never composts an agenda it did not plant
    assert still["id"] == earlier["id"] and still["status"] == "open"


def test_the_host_returning_with_the_row_still_open_is_refused_and_composts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = RacingHost()
    host.returns_without_recording = True
    rig = _host_rig(tmp_path, monkeypatch, host)
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    assert ei.value.code == "answer_refused" and "still open" in str(ei.value) and "nothing remains planted" in str(ei.value)
    assert _agendas(rig) == [] and host.get("np-plain002").answer is None


def test_a_clean_answer_that_cannot_be_read_back_is_not_claimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = RacingHost()
    host.get_fails_after_answer = True
    rig = _host_rig(tmp_path, monkeypatch, host)
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    words = str(ei.value)
    assert ei.value.http_status == 503 and "could not be read back" in words and "reload" in words
    [agenda] = _agendas(rig)                                              # it may be right: never composted on an unknown
    assert agenda["id"] in words
    assert [line.get("outcome") for line in _audit(rig)] == ["unconfirmed"]


def test_a_racing_call_never_composts_the_agenda_the_winner_planted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The winner's add lands between this call's key read and its own add (the old non-atomic created check)."""
    host = AnsweringHost()
    host.answer_refuses = "needs a signed answer"
    rig = _host_rig(tmp_path, monkeypatch, host)
    orig = band_agenda._plant
    winner: dict[str, Any] = {}

    def racing(store: Any, **kw: Any) -> Any:
        winner.update(rig.sp.add(type="task", text="the winner's", disposition="agenda", next=kw["day"], target="band",
                                 pointer=f"question:{kw['question_id']}",
                                 origin_key=band_agenda.agenda_origin_key(kw["question_id"]), today=kw["today"]))
        return orig(store, **kw)

    monkeypatch.setattr(band_agenda, "_plant", racing)
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    words = str(ei.value)
    assert ei.value.code == "answer_refused" and winner["id"] in words and "nothing remains planted" not in words
    [still] = _agendas(rig)
    assert still["id"] == winner["id"] and still["status"] == "open"


def test_an_agenda_changed_since_this_call_planted_it_is_left_standing_and_named(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = AnsweringHost()
    rig = _host_rig(tmp_path, monkeypatch, host)

    def refuse_after_an_edit(ref, *, option=None, text=None, channel):
        from levain.cockpit.consent import ConsentError

        mine = _keyed(rig, ref)
        rig.sp.update(mine["id"], text="the operator edited this agenda meanwhile")
        raise ConsentError("needs a signed answer")

    host.answer = refuse_after_an_edit                    # type: ignore[method-assign]
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    words = str(ei.value)
    assert ei.value.code == "answer_refused" and "changed since" in words and "compost it if it is not wanted" in words
    [still] = _agendas(rig)
    assert still["status"] == "open" and still["text"] == "the operator edited this agenda meanwhile"


def test_an_agenda_composted_meanwhile_is_reported_composted_not_as_a_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = AnsweringHost()
    rig = _host_rig(tmp_path, monkeypatch, host)

    def refuse_after_a_compost(ref, *, option=None, text=None, channel):
        from levain.cockpit.consent import ConsentError

        rig.sp.descend(_keyed(rig, ref)["id"], kind="composted")
        raise ConsentError("needs a signed answer")

    host.answer = refuse_after_a_compost                  # type: ignore[method-assign]
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    words = str(ei.value)
    assert ei.value.code == "answer_refused" and "already composted" in words and "nothing remains planted" in words
    assert "compost it" not in words


def test_plant_refuse_compost_then_retry_is_agenda_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = AnsweringHost()
    host.answer_refuses = "needs a signed answer"
    rig = _host_rig(tmp_path, monkeypatch, host)
    with pytest.raises(EditError):
        _defer(rig, host, "np-plain002")
    composted = _keyed(rig, "np-plain002")
    assert composted["status"] == "resolved" and composted["disposition"] == "agenda"   # what descend(composted) leaves
    host.answer_refuses = None
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    assert ei.value.code == "agenda_resolved" and composted["id"] in str(ei.value)
    assert host.get("np-plain002").answer is None and _agendas(rig) == []


def test_a_key_held_by_a_deleted_spore_is_agenda_resolved_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = AnsweringHost()
    rig = _host_rig(tmp_path, monkeypatch, host)
    s = rig.sp.add(type="task", text="x", disposition="agenda", next=PAST, target="band", pointer="question:np-plain002",
                   origin_key=band_agenda.agenda_origin_key("np-plain002"), today=TODAY)
    from anneal_memory.spores import spore_version

    rig.sp.delete(s["id"], expected_version=spore_version(s))
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    assert ei.value.code == "agenda_resolved" and s["id"] in str(ei.value) and host.get("np-plain002").answer is None


@pytest.mark.parametrize("fault", ["value_error", "no_id"])
def test_a_foreign_reader_fault_on_the_target_is_typed_and_plants_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    host = AnsweringHost()
    rig = _host_rig(tmp_path, monkeypatch, host)

    def get(ref):
        if fault == "value_error":
            raise ValueError("1 unparseable needs_phill line(s): the open set may be incomplete")
        return object()                                   # no id, no answer, no words

    host.get = get                                        # type: ignore[method-assign]
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    assert ei.value.code == "source_unavailable" and ei.value.http_status == 503
    assert _keyed(rig, "np-plain002") is None and _audit(rig) == []


def test_today_given_as_a_datetime_is_its_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone

    host = AnsweringHost()
    rig = _host_rig(tmp_path, monkeypatch, host)
    out = band_agenda.defer(questions=host, spores_json=rig.src.anneal.spores_json, ledger_root=rig.src.write_scope.ledger_root,
                            question_id="np-plain002", day=PAST,
                            today=datetime(TODAY.year, TODAY.month, TODAY.day, 12, tzinfo=timezone.utc))
    assert out["ok"] and out["day"] == PAST
    with pytest.raises(EditError) as ei:
        band_agenda.defer(questions=host, spores_json=rig.src.anneal.spores_json, ledger_root=rig.src.write_scope.ledger_root,
                          question_id="np-plain002", day=PAST, today=datetime(TODAY.year, TODAY.month, TODAY.day, 12))
    assert ei.value.code == "stale"                       # the row is answered now; the datetime was never compared raw


def test_a_failed_audit_write_never_masks_the_compost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from levain import writes

    host = AnsweringHost()
    host.answer_refuses = "needs a signed answer"
    rig = _host_rig(tmp_path, monkeypatch, host)

    def broken(*_a: Any, **_k: Any) -> str:
        raise RuntimeWarning("edit-log append failed")    # what _audit_verb's warn becomes under -W error

    monkeypatch.setattr(writes, "_audit_verb", broken)
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    words = str(ei.value)
    assert ei.value.code == "answer_refused" and "nothing remains planted" in words and "audit line could not be written" in words
    assert _agendas(rig) == [] and _keyed(rig, "np-plain002")["resolution"]["kind"] == "composted"


def test_a_host_that_closes_the_row_with_our_words_and_then_raises_delivers_the_answer(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    host = AnsweringHost()
    host.raise_after_closing = True
    rig = _host_rig(tmp_path, monkeypatch, host)
    seen: list[str] = []
    done = threading.Event()
    monkeypatch.setattr(band, "deliver_answer", lambda store, ref, **kw: (seen.append(ref), done.set()))
    out = _defer(rig, host, "np-plain002")
    assert out["ok"] and "desk was NOT woken" in out["warning"]
    assert done.wait(5) and seen == ["np-plain002"]


def test_a_host_whose_get_drops_the_sign_request_its_listing_carries_still_refuses_the_signing_row(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """flow's NeedsQuestions.get never passes the sign_request its open_questions attaches (desk_cards._question)."""
    from dataclasses import replace

    class FlowShaped(AnsweringHost):
        def get(self, ref):
            q = super().get(ref)
            return None if q is None else replace(q, sign_request=None)

    host = FlowShaped()
    host.rows = [*host.rows, replace(host.rows[1], n=13, id="np-sign0003", sign_request="sr-0003")]
    rig = _host_rig(tmp_path, monkeypatch, host)
    assert band_agenda.offer_block(host, "np-sign0003") == band_agenda.BLOCK_SIGNS       # the target read of a write
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-sign0003")
    assert ei.value.code == "needs_signature"
    assert _keyed(rig, "np-sign0003") is None and host.get("np-sign0003").answer is None
    assert band_agenda.offer_block(host, "np-plain002") is None                         # a plain row stays offered


def test_an_unreadable_listing_blocks_a_foreign_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = AnsweringHost()
    rig = _host_rig(tmp_path, monkeypatch, host)

    def broken(agent=None):
        raise ValueError("1 unparseable needs_phill line(s)")

    host.open_questions = broken                          # type: ignore[method-assign]
    assert band_agenda.offer_block(host, "np-plain002") == band_agenda.BLOCK_UNREADABLE
    with pytest.raises(EditError) as ei:
        _defer(rig, host, "np-plain002")
    assert ei.value.code == "source_unavailable" and _keyed(rig, "np-plain002") is None
