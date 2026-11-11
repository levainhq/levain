"""Row 774 / np-91cd76d5: anneal 1.0.7 names new records ``intention-NNN``; an id minted before it (``spore-N``) still reads.

Levain's one store factory (:func:`levain.spores.guarded_spore_store`) passes ``id_prefix``, and the cockpit's id check
(:func:`levain.consent.questions.valid_intention_id`) accepts both spellings."""
from __future__ import annotations

import json

import pytest

from levain.consent.questions import valid_intention_id
from levain.spores import NEW_ID_PREFIX, guarded_spore_store


def test_a_new_record_is_minted_with_the_intention_prefix(tmp_path):
    store = guarded_spore_store(tmp_path / "memory.spores.json")
    row = store.add(type="thought", text="a new one")
    assert NEW_ID_PREFIX == "intention" and row["id"].startswith("intention-")


def test_an_id_minted_before_the_prefix_still_reads_through_the_same_store(tmp_path):
    path = tmp_path / "memory.spores.json"
    first = guarded_spore_store(path).add(type="thought", text="old")
    # rewrite the stored id as the pre-1.0.7 spelling an older install carries
    body = json.loads(path.read_text())
    body["spores"] = [{**s, "id": "spore-007"} if s["id"] == first["id"] else s for s in body["spores"]]
    path.write_text(json.dumps(body))
    store = guarded_spore_store(path)
    assert store.get("spore-007")["text"] == "old"
    assert store.add(type="thought", text="newer")["id"].startswith("intention-")


@pytest.mark.parametrize("value", ["spore-12", "spore-1680", "intention-001", "intention-1680"])
def test_the_id_check_reads_both_prefixes(value):
    assert valid_intention_id(value)


@pytest.mark.parametrize("value", ["task-12", "intention", "intention-", "spore-", "", None, "spore-12 x", "xintention-1"])
def test_the_id_check_refuses_anything_else(value):
    assert not valid_intention_id(value)


def test_an_anneal_without_id_prefix_gets_a_clear_error_not_a_bare_type_error(tmp_path, monkeypatch):
    import levain.spores as sp

    def old_anneal(path, **kw):
        raise TypeError("SporeStore.__init__() got an unexpected keyword argument 'id_prefix'")

    monkeypatch.setattr(sp, "_GUARDED", old_anneal)
    with pytest.raises(RuntimeError, match=r"anneal-memory>=1\.0\.7"):
        sp.guarded_spore_store(tmp_path / "memory.spores.json")


def test_only_the_id_prefix_type_error_is_translated(tmp_path, monkeypatch):
    import levain.spores as sp

    def other(path, **kw):
        raise TypeError("id_prefix must be a string")      # a different TypeError that merely names id_prefix

    monkeypatch.setattr(sp, "_GUARDED", other)
    with pytest.raises(TypeError, match="must be a string"):
        sp.guarded_spore_store(tmp_path / "memory.spores.json")
