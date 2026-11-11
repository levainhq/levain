"""Row 774 / np-91cd76d5: anneal 1.0.7 names new records ``intention-NNN``; an id minted before it (``spore-N``) still reads.

Levain's one store factory (:func:`levain.intentions.guarded_intention_store`) passes ``id_prefix``, and the cockpit's id check
(:func:`levain.consent.questions.valid_intention_id`) accepts both spellings.

The ``spore-`` literals below are the old word ON PURPOSE (1111-32 G1): the rename codemod once rewrote this file's data
from ``spore-12`` to ``intention-12``, collapsed the id check's ``(?:spore|intention)`` alternation into
``(?:intention|intention)``, and the rewritten tests could no longer see it. Keep them literal."""
from __future__ import annotations

import json

import pytest

from levain.consent.questions import valid_intention_id
from levain.intentions import NEW_ID_PREFIX, guarded_intention_store


def test_a_new_record_is_minted_with_the_intention_prefix(tmp_path):
    store = guarded_intention_store(tmp_path / "memory.spores.json")
    row = store.add(type="thought", text="a new one")
    assert NEW_ID_PREFIX == "intention" and row["id"].startswith("intention-")


def test_an_id_minted_before_the_prefix_still_reads_through_the_same_store(tmp_path):
    path = tmp_path / "memory.spores.json"
    first = guarded_intention_store(path).add(type="thought", text="old")
    # rewrite the stored id as the pre-1.0.7 spelling an older install carries
    body = json.loads(path.read_text())
    body["spores"] = [{**s, "id": "spore-007"} if s["id"] == first["id"] else s for s in body["spores"]]
    path.write_text(json.dumps(body))
    store = guarded_intention_store(path)
    assert store.get("spore-007")["text"] == "old"
    assert store.add(type="thought", text="newer")["id"].startswith("intention-")


@pytest.mark.parametrize("value", ["spore-12", "spore-1680", "intention-001", "intention-1680"])
def test_the_id_check_reads_both_prefixes(value):
    assert valid_intention_id(value)


@pytest.mark.parametrize("value", ["task-12", "intention", "intention-", "spore-", "", None, "spore-12 x", "xintention-1",
                                   "intention-intention-1x y", "spore", "sporeintention-1", "spore_12", "Spore-12"])
def test_the_id_check_refuses_anything_else(value):
    assert not valid_intention_id(value)


def test_the_id_check_is_built_from_the_legacy_prefix_not_a_second_spelling():
    """The old arm is the one legacy constant, so a word swap over this module cannot collapse it into the new word again."""
    from levain.consent.questions import INTENTION_ID
    from levain.intention_legacy import OLD_ID_PREFIX

    assert OLD_ID_PREFIX == "spore-"
    assert INTENTION_ID.fullmatch(OLD_ID_PREFIX + "12") and INTENTION_ID.fullmatch("intention-12")


def _ringing_with_id(tmp_path, old_id):
    """An install whose agenda (stored under ``old_id``, the id a pre-1.0.7 store holds) has fired into an open band row."""
    from datetime import date, datetime, timezone

    from levain import germinate as g
    from levain.consent.questions import QuestionStore

    root = tmp_path / "inst"
    (root / ".levain").mkdir(parents=True)
    path = root / ".levain" / "memory.spores.json"
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    made = guarded_intention_store(path).add(type="task", text="renew the thing", disposition="agenda", next="2026-11-06")
    body = json.loads(path.read_text())
    body["spores"] = [{**s, "id": old_id} if s["id"] == made["id"] else s for s in body["spores"]]
    path.write_text(json.dumps(body))
    res = g.germinate(path, qdir, install=root, today=date(2026, 11, 7))
    assert not res["errors"] and [o["intention_id"] for o in res["due"]] == [old_id], res
    qs = QuestionStore(qdir, clock=lambda: datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc))
    (q,) = qs.open_questions()
    return path, qs, q


@pytest.mark.parametrize("ring_id", ["spore-7", "intention-7"])
def test_a_band_row_ringing_an_old_or_new_id_offers_the_controls_and_acts(tmp_path, ring_id):
    """G1: a band row ringing a ``spore-N`` agenda is the same as one ringing ``intention-N``: the cockpit names its intention
    (so the row offers snooze / edit / done / clear), and ``intention_act`` BY THAT ID reaches the store's row."""
    from levain.consent.questions import intention_of

    path, qs, q = _ringing_with_id(tmp_path, ring_id)
    assert qs.intention_for(q) == ring_id
    assert intention_of(qs, q) == ring_id                       # None here = the row loses its four controls
    said = qs.intention_act(ring_id, "snooze", until="2026-11-12", channel="cockpit")
    assert "snoozed to 2026-11-12" in said
    got = guarded_intention_store(path).get(ring_id)
    assert got["next"] == "2026-11-12" and got["status"] == "open" and qs.open_questions() == []


def test_an_anneal_without_id_prefix_gets_a_clear_error_not_a_bare_type_error(tmp_path, monkeypatch):
    import levain.intentions as sp

    def old_anneal(path, **kw):
        raise TypeError("IntentionStore.__init__() got an unexpected keyword argument 'id_prefix'")

    monkeypatch.setattr(sp, "_GUARDED", old_anneal)
    with pytest.raises(RuntimeError, match=r"anneal-memory>=1\.0\.7"):
        sp.guarded_intention_store(tmp_path / "memory.spores.json")


def test_only_the_id_prefix_type_error_is_translated(tmp_path, monkeypatch):
    import levain.intentions as sp

    def other(path, **kw):
        raise TypeError("id_prefix must be a string")      # a different TypeError that merely names id_prefix

    monkeypatch.setattr(sp, "_GUARDED", other)
    with pytest.raises(TypeError, match="must be a string"):
        sp.guarded_intention_store(tmp_path / "memory.spores.json")
