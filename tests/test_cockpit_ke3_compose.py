"""KE3: a list panel may carry `compose: {verb, fields}` (the inbox compose rides the list's own card)."""
from __future__ import annotations

import pytest

from levain.cockpit import Cockpit, CockpitRegistrationError, ProviderSpec, Read, RowIn
from levain.cockpit.registry import FACETS

COMPOSE = {"verb": "send_inbox", "fields": [
    {"name": "message", "type": "text", "label": "Message"},
    {"name": "for", "type": "recipient", "label": "To (none = everyone)", "enum": ["cli", "daemon"]}]}


def _spec(**kw):
    base = dict(id="inbox", kind="list", title="Inbox", priority="feed", order="time.desc", version_fields=("id",),
                facets=frozenset({"source", "for", "at"}), verbs=("send_inbox",), compose=COMPOSE,
                read=lambda c: Read(rows=(RowIn("a", "hi", facets={"source": "bridge", "for": "cli, daemon"}, stored={"id": "a"}),)))
    base.update(kw)
    return ProviderSpec(**base)


def test_a_list_compose_rides_the_panel_head_and_the_manifest_copy_is_independent() -> None:
    ck = Cockpit()
    ck.register(_spec())
    head = ck.panel("inbox")
    assert head["compose"] == COMPOSE
    head["compose"]["fields"].clear()                      # a caller cannot reach the registered declaration
    assert ck.panel("inbox")["compose"] == COMPOSE


def test_a_panel_without_compose_carries_no_compose_key() -> None:
    ck = Cockpit()
    ck.register(_spec(compose=None))
    assert "compose" not in ck.panel("inbox")


def test_the_for_facet_is_registered_and_rows_carry_it() -> None:
    assert "for" in FACETS
    ck = Cockpit()
    ck.register(_spec())
    assert ck.panel("inbox")["rows"][0]["facets"]["for"] == "cli, daemon"


@pytest.mark.parametrize("kw,needle", [
    ({"compose": {**COMPOSE, "verb": "other"}}, "verb"),
    ({"compose": {"verb": "send_inbox", "fields": [{"name": "m", "type": "slider"}]}}, "type"),
    ({"compose": {"verb": "send_inbox", "fields": [{"name": "for", "type": "recipient"}]}}, "enum"),
    ({"compose": {"verb": "send_inbox", "fields": []}}, "field"),
    ({"compose": {**COMPOSE, "extra": 1}}, "compose"),
])
def test_a_compose_the_page_could_not_honour_is_refused_at_registration(kw, needle) -> None:
    with pytest.raises(CockpitRegistrationError, match=needle):
        Cockpit().register(_spec(**kw))


def test_only_a_list_carries_a_compose() -> None:
    with pytest.raises(CockpitRegistrationError, match="only a list"):
        Cockpit().register(ProviderSpec(id="p", kind="line", title="P", priority="feed", verbs=("send_inbox",),
                                        compose=COMPOSE, read=lambda c: Read(value={"lines": []})))
