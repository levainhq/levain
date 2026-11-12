"""The store factory and the raw open-list read fail loud on an anneal-memory or a document that is not schema 2.

Two ways a schema-1 world can reach levain's raw reads of the new key and read as "no open intentions":
an older anneal still installed (a checkout run directly, ``--no-deps``, ``--no-pip``) that loads and writes the old key, and
a document that still carries the retired key. Both refuse, naming the pin or the one-time ``migrate-key`` conversion."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("anneal_memory.spores")

from levain.intentions import guarded_intention_store, list_open_readonly

PIN = "anneal-memory>=2.0.0,<2.1"


def test_an_older_anneal_without_the_schema2_open_key_is_refused_with_the_pin(tmp_path, monkeypatch):
    import anneal_memory.spores as spores

    monkeypatch.delattr(spores, "OPEN_KEY")          # what 1.0.7 looks like: it accepts id_prefix and has no such constant
    with pytest.raises(RuntimeError) as exc:
        guarded_intention_store(tmp_path / "memory.spores.json")
    assert PIN in str(exc.value)


def test_an_anneal_that_keys_the_open_list_differently_is_refused(tmp_path, monkeypatch):
    import anneal_memory.spores as spores

    monkeypatch.setattr(spores, "OPEN_KEY", "spores")
    with pytest.raises(RuntimeError, match="anneal-memory>=2.0.0"):
        guarded_intention_store(tmp_path / "memory.spores.json")


def test_the_refusal_is_checked_on_every_call_not_once(tmp_path, monkeypatch):
    """The built class is cached; the schema contract must not be."""
    import anneal_memory.spores as spores

    assert guarded_intention_store(tmp_path / "memory.spores.json") is not None    # builds and caches the class
    monkeypatch.delattr(spores, "OPEN_KEY")
    with pytest.raises(RuntimeError):
        guarded_intention_store(tmp_path / "memory.spores.json")


def test_the_installed_anneal_is_accepted(tmp_path):
    store = guarded_intention_store(tmp_path / "memory.spores.json")
    assert store.add(type="thought", text="x")["id"].startswith("intention-")


def test_list_open_readonly_reads_a_schema2_document(tmp_path):
    path = tmp_path / "memory.spores.json"
    store = guarded_intention_store(path)
    row = store.add(type="thought", text="kept")
    assert [r["id"] for r in list_open_readonly(path)] == [row["id"]]
