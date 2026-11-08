"""The host-shaped Question flow's desk builds (scripts/desk_cards.py ``_question``, flow main 675fce21) carries ``dated`` /
``snoozed_until`` (str only) through a FOREIGN store (only open_questions / recent / get). ``board._band_cards`` must keep
them: a future day reads LATER (collapsed card, no blocks, still a numbered band card), a due or malformed day reads due."""
from __future__ import annotations

from datetime import datetime, timedelta

from levain import band_view
from levain.cockpit import board as cockpit_board
from levain.consent.questions import Question, local_today


def _future(days: int) -> str:
    return (datetime.fromisoformat(local_today(datetime.now().astimezone())) + timedelta(days=days)).date().isoformat()


def _q(n: int, **extra: str) -> Question:
    # the shape desk_cards._question returns: the older fields plus section/carried and the two LATER strings
    return Question(n=n, id=f"np-{n:08x}", agent="desk", agent_session_id="-", what=f"repo: row {n}", options=(("a", "yes"),),
                    rec="a", blocks=f"stopped {n}", cwd="/tmp/x", filed_at="2026-11-07T10:00:00Z", answer=None,
                    section="desk", carried=False, **extra)


class ForeignStore:
    def __init__(self, rows: list[Question]) -> None:
        self.rows = rows

    def open_questions(self, agent=None):
        return [q for q in self.rows if q.answer is None]

    def recent(self, limit=50):
        return list(self.rows)[:limit]

    def get(self, ref):
        text = str(ref).lstrip("#")
        return next((q for q in self.rows if text in (str(q.n), q.id)), None)


def _by_number(rows: list[Question]) -> dict[int, dict]:
    cards, bad = cockpit_board._band_cards(ForeignStore(rows))
    assert bad is None
    return {c["number"]: c for c in cards}


def test_a_dated_foreign_question_is_a_collapsed_later_card_that_stays_numbered():
    day = _future(3)
    by = _by_number([_q(5), _q(6, dated=day)])
    assert "later" not in by[5] and by[5]["blocks"] == "stopped 5"
    c = by[6]
    assert c["later"] == day and c["later_label"].endswith(day[5:]) and c["blocks"] == ""
    assert c["band"] is True and c["number"] == 6 and c["id"].startswith("band:")


def test_a_snoozed_until_foreign_question_is_later_and_snooze_wins_over_dated():
    snz, dated = _future(2), _future(9)
    c = _by_number([_q(8, snoozed_until=snz, dated=dated)])[8]
    assert c["later"] == snz and c["blocks"] == ""


def test_a_due_or_malformed_day_on_a_foreign_question_is_not_later():
    by = _by_number([_q(1, dated="2020-01-01"), _q(2, snoozed_until="not-a-date"), _q(3, dated=local_today(datetime.now().astimezone()))])
    for n in (1, 2, 3):
        assert "later" not in by[n] and by[n]["blocks"] == f"stopped {n}"


def test_the_payload_marks_the_same_foreign_rows_later_with_the_same_label():
    day = _future(4)
    store = ForeignStore([_q(6, dated=day)])
    card = _by_number(store.rows)[6]
    item = band_view.band_payload(store, "cockpit:web")["items"][0]
    assert item["later"] == card["later"] == day and item["later_label"] == card["later_label"]
