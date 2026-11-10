"""Intention controls, S2 + S3: a band row the host says rings an intention is offered Snooze, Edit, Done and Clear in place of an
answer box (Decisions and the List alike); a machine-filed row keeps its options. The act is the host's one ``intention_act``
call and nothing else; a host without it offers nothing. Temp dirs only."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit.consent import ConsentError
from levain.cockpit.verbs import dispatch
from levain.consent.questions import Answer, IntentionStale, Question
from levain.writes import EditError
from tests.test_cockpit_list_panel import CRED, INSTALL_CLASS, Rig

FOUR = {"intention_snooze", "intention_edit", "intention_done", "intention_clear"}


def _q(n: int, qid: str, *, intention: str | None = None, answer: Answer | None = None) -> Question:
    return Question(n=n, id=qid, agent="desk", agent_session_id="-", what=f"reminder {n}",
                    options=(("a", "yes"), ("b", "no")), rec="a", blocks="the lane", cwd="/tmp/x",
                    filed_at="2026-11-08T10:00:00Z", answer=answer, intention=intention)


class HostStore:
    """The public surface of a host store (flow's NeedsQuestions) with the intention capability."""

    def __init__(self) -> None:
        self.rows = [_q(11, "np-ring0001", intention="spore-9"), _q(12, "np-plain002")]
        self.acts: list[tuple[str, str, dict[str, Any]]] = []
        self.refuse: str | None = None
        self.refuse_stale = False

    def open_questions(self, agent=None):
        return [q for q in self.rows if q.answer is None]

    def recent(self, limit=50):
        return list(self.rows)[:limit]

    def get(self, ref):
        text = str(ref).lstrip("#")
        return next((q for q in self.rows if text in (str(q.n), q.id)), None)

    def answer(self, ref, *, option=None, text=None, channel):
        raise AssertionError("an intention row is never answered through the answer box")

    def intention_act(self, ref, action, *, until=None, text=None, channel):
        if self.refuse:
            raise (IntentionStale if self.refuse_stale else ConsentError)(self.refuse)
        self.acts.append((ref, action, {"until": until, "text": text, "channel": channel}))
        hit = lambda q: ref in (q.id, q.intention) and q.answer is None   # noqa: E731 - the spore id or the row id, as flow's
        if not any(hit(q) for q in self.rows):
            raise ConsentError(f"{ref} rings no open row")
        if action == "edit":
            self.rows = [replace(q, what=text) if hit(q) else q for q in self.rows]
        else:       # snooze, done and clear all close the row
            self.rows = [replace(q, answer=Answer(q.id, "text", action, channel, "2026-11-08T11:00:00Z"))
                         if hit(q) else q for q in self.rows]
        return f"{action} done"


class OlderHost(HostStore):
    """The same rows with the capability deleted: an older flow."""

    intention_act = None        # type: ignore[assignment]


@pytest.fixture
def host_rig(tmp_path: Path) -> tuple[Rig, HostStore]:
    rig = Rig(tmp_path, band=False)
    host = HostStore()
    rig.ck.attach_question_rows(host)
    return rig, host


def _verbs(row: dict[str, Any]) -> set[str]:
    return {a["verb"] for a in row["actions"]}


def _decision(rig: Rig, n: int) -> dict[str, Any]:
    return next(r for r in rig.ck.panel("decisions", credential_class="token")["rows"] if r["id"] == f"question:{n}")


def _fire(rig: Rig, panel: str, row: dict[str, Any], verb: str, params: dict[str, Any], *, confirm: bool = False,
          version: str | None = None) -> dict:
    return dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                    install_class=INSTALL_CLASS,
                    req={"verb": verb, "panel_id": panel, "row_id": row["id"], "row_version": version or row["version"],
                         "params": params, "confirm": confirm})


def test_decisions_an_intention_row_gets_the_four_and_a_machine_row_keeps_its_answer(host_rig) -> None:
    rig, _ = host_rig
    assert _verbs(_decision(rig, 11)) == FOUR
    assert _verbs(_decision(rig, 12)) == {"question_answer"}
    assert _decision(rig, 11)["facets"]["question_intention"] == "spore-9"
    assert "question_intention" not in _decision(rig, 12)["facets"]


def test_list_an_intention_band_row_gets_the_four_and_a_machine_row_none(host_rig) -> None:
    rig, _ = host_rig
    rows = rig.rows()
    assert _verbs(rows["band:np-ring0001"]) == FOUR
    assert rows["band:np-ring0001"]["facets"]["intention"] == "spore-9"
    assert _verbs(rows["band:np-plain002"]) == set()        # read-only, as every band row was


def test_an_older_host_without_intention_act_offers_nothing_new(tmp_path: Path) -> None:
    rig = Rig(tmp_path, band=False)
    rig.ck.attach_question_rows(OlderHost())
    assert _verbs(_decision(rig, 11)) == {"question_answer"}
    assert "question_intention" not in _decision(rig, 11)["facets"]
    assert _verbs(rig.rows()["band:np-ring0001"]) == set()


def test_the_intention_verbs_never_land_on_an_answered_row_or_a_machine_row(host_rig) -> None:
    rig, host = host_rig
    plain = _decision(rig, 12)
    with pytest.raises(Exception) as exc:
        _fire(rig, "decisions", plain, "intention_done", {})
    assert getattr(exc.value, "http_status", 409) == 409 and not host.acts
    ring = _decision(rig, 11)
    with pytest.raises(Exception) as exc2:                  # and the answer box is not offered on the ringing row
        _fire(rig, "decisions", ring, "question_answer", {"option": "a"})
    assert getattr(exc2.value, "http_status", 409) == 409 and not host.acts


@pytest.mark.parametrize("panel", ["decisions", "tray"])
@pytest.mark.parametrize("verb,params,want", [
    ("intention_snooze", {"until": "2026-11-20"}, ("snooze", {"until": "2026-11-20", "text": None})),
    ("intention_snooze", {"until": "2026-11-20T09:30"}, ("snooze", {"until": "2026-11-20T09:30", "text": None})),
    ("intention_edit", {"text": "renew it Friday"}, ("edit", {"until": None, "text": "renew it Friday"})),
    ("intention_done", {}, ("done", {"until": None, "text": None})),
])
def test_each_verb_calls_intention_act_once_with_the_right_action(tmp_path: Path, panel, verb, params, want) -> None:
    rig = Rig(tmp_path, band=False)
    host = HostStore()
    rig.ck.attach_question_rows(host)
    row = _decision(rig, 11) if panel == "decisions" else rig.rows()["band:np-ring0001"]
    out = _fire(rig, panel, row, verb, params)
    assert out["intention"] == want[0] and out["ref"] == "spore-9"          # the reminder the row showed
    assert host.acts == [("spore-9", want[0], {**want[1], "channel": "cockpit"})]


@pytest.mark.parametrize("panel", ["decisions", "tray"])
def test_clear_needs_the_confirm_and_then_acts_once(tmp_path: Path, panel) -> None:
    rig = Rig(tmp_path, band=False)
    host = HostStore()
    rig.ck.attach_question_rows(host)
    row = _decision(rig, 11) if panel == "decisions" else rig.rows()["band:np-ring0001"]
    with pytest.raises(Exception) as exc:
        _fire(rig, panel, row, "intention_clear", {})
    assert not host.acts, exc.value
    _fire(rig, panel, row, "intention_clear", {}, confirm=True)
    assert [a[1] for a in host.acts] == ["clear"]


def test_done_takes_no_confirm(host_rig) -> None:
    rig, host = host_rig
    _fire(rig, "decisions", _decision(rig, 11), "intention_done", {}, confirm=False)
    assert [a[1] for a in host.acts] == ["done"]


def test_the_hosts_refusal_comes_back_in_its_own_words(host_rig) -> None:
    rig, host = host_rig
    row = rig.rows()["band:np-ring0001"]
    host.refuse, host.refuse_stale = "spore-9 is already finished", True
    with pytest.raises(EditError) as exc:
        _fire(rig, "tray", row, "intention_done", {})
    assert exc.value.http_status == 409 and "already finished" in str(exc.value)
    host.refuse, host.refuse_stale = "the date has passed", False
    with pytest.raises(EditError) as exc2:
        _fire(rig, "tray", row, "intention_snooze", {"until": "2026-11-20"})
    assert exc2.value.http_status == 400 and "date has passed" in str(exc2.value)


def test_a_bad_snooze_date_or_empty_edit_is_refused_before_the_host_hears(host_rig) -> None:
    rig, host = host_rig
    row = rig.rows()["band:np-ring0001"]
    for verb, params in (("intention_snooze", {"until": "next week"}), ("intention_snooze", {"until": ""}),
                         ("intention_edit", {"text": "   "})):
        with pytest.raises(EditError) as exc:
            _fire(rig, "tray", row, verb, params)
        assert exc.value.http_status in (400, 422), (verb, params)
    assert not host.acts


def test_a_stale_act_is_a_409_and_the_host_is_not_called(host_rig) -> None:
    rig, host = host_rig
    row = rig.rows()["band:np-ring0001"]
    old = row["version"]
    _fire(rig, "tray", row, "intention_edit", {"text": "words moved"})       # the row changed under the page
    host.acts.clear()
    with pytest.raises(EditError) as exc:
        _fire(rig, "tray", row, "intention_done", {}, version=old)
    assert exc.value.http_status == 409 and not host.acts


def test_a_row_resolved_elsewhere_is_gone_on_the_list_and_the_decisions_panel(host_rig) -> None:
    rig, host = host_rig
    row = rig.rows()["band:np-ring0001"]
    drow = _decision(rig, 11)
    host.rows = [replace(q, answer=Answer(q.id, "text", "x", "cli", "2026-11-08T11:00:00Z")) if q.id == "np-ring0001" else q
                 for q in host.rows]
    with pytest.raises(EditError) as exc:
        _fire(rig, "tray", row, "intention_done", {})
    assert exc.value.http_status == 409
    with pytest.raises(EditError) as exc2:
        _fire(rig, "decisions", drow, "intention_done", {})
    assert exc2.value.http_status == 409 and not host.acts


def test_a_band_row_without_an_intention_is_still_no_write_target(host_rig) -> None:
    rig, host = host_rig
    row = rig.rows()["band:np-plain002"]
    with pytest.raises(EditError) as exc:
        _fire(rig, "tray", row, "intention_done", {})
    assert exc.value.http_status == 409 and not host.acts


def test_a_refusal_of_any_exception_class_comes_back_as_its_words_never_a_500(host_rig, monkeypatch) -> None:
    rig, host = host_rig

    class IntentionError(Exception):
        pass

    def act(ref, action, *, until=None, text=None, channel):
        raise IntentionError("a date in the past is not a snooze")

    monkeypatch.setattr(host, "intention_act", act)
    with pytest.raises(EditError) as exc:
        _fire(rig, "tray", rig.rows()["band:np-ring0001"], "intention_snooze", {"until": "2026-11-20"})
    assert exc.value.http_status == 400 and "not a snooze" in str(exc.value)


# --- fix round 1 ---------------------------------------------------------------------------------------------------------


def test_a_validation_refusal_is_a_400_whatever_its_words(host_rig) -> None:
    """K3: the class decides stale, never the English: "already" in a date refusal must not wipe the operator's form."""
    rig, host = host_rig
    host.refuse = "that snooze date is already past"
    with pytest.raises(EditError) as exc:
        _fire(rig, "tray", rig.rows()["band:np-ring0001"], "intention_snooze", {"until": "2026-11-20"})
    assert exc.value.http_status == 400


def test_a_host_exception_marked_stale_is_a_409(host_rig, monkeypatch) -> None:
    rig, host = host_rig

    class IntentionError(Exception):
        stale = True

    def act(ref, action, *, until=None, text=None, channel):
        raise IntentionError("spore-9 closed while it was being closed")

    monkeypatch.setattr(host, "intention_act", act)
    with pytest.raises(EditError) as exc:
        _fire(rig, "tray", rig.rows()["band:np-ring0001"], "intention_done", {})
    assert exc.value.http_status == 409 and "closed while" in str(exc.value)


def test_the_act_names_the_reminder_the_page_showed(host_rig) -> None:
    """C1: the host is handed the spore id the row displayed, never a row id it would re-resolve."""
    rig, host = host_rig
    _fire(rig, "decisions", _decision(rig, 11), "intention_done", {})
    assert [a[0] for a in host.acts] == ["spore-9"]


def test_a_malformed_snooze_reads_in_plain_words(host_rig) -> None:
    rig, host = host_rig
    with pytest.raises(EditError) as exc:
        _fire(rig, "tray", rig.rows()["band:np-ring0001"], "intention_snooze", {"until": "2026-11-20T25:99"})
    assert "pick a day" in str(exc.value) and "YYYY" not in str(exc.value) and not host.acts


# --- stack tip fixes -----------------------------------------------------------------------------------------------------


def _reworded(host: HostStore, words: str) -> None:
    host.rows = [replace(q, intention_text=words) if q.id == "np-ring0001" else q for q in host.rows]


def test_a_decisions_edit_built_on_words_since_changed_is_stale(host_rig) -> None:
    """Two clients open the same reminder; A rewords it; B's Edit, built on the old words, is refused as stale and the host is
    not called. An unrelated re-read with the words unchanged keeps the version stable."""
    rig, host = host_rig
    _reworded(host, "renew the thing")
    first = _decision(rig, 11)
    assert _decision(rig, 11)["version"] == first["version"]
    _reworded(host, "renew the OTHER thing")        # client A's edit landed
    assert _decision(rig, 11)["version"] != first["version"]
    with pytest.raises(EditError) as exc:
        _fire(rig, "decisions", first, "intention_edit", {"text": "B's words"})
    assert exc.value.http_status == 409 and not host.acts
