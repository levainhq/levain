"""KE3: a list panel may carry `compose: {verb, fields}` (the inbox compose rides the list's own card)."""
from __future__ import annotations

import copy

import pytest

from levain.cockpit import Cockpit, CockpitRegistrationError, ProviderSpec, Read, RowIn
from levain.cockpit.registry import FACETS
from levain.cockpit.verbs import VerbRegistry, VerbView
from levain.writes import ActionVerb

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


# --- L3 r1 (KE3/L3_r1/TRIAGE.md BUILD) ------------------------------------------------------------------------------
CRED = {"class": "none", "device_class": None}


def _registry(**over):
    verbs = {"send_inbox": ActionVerb(handler=lambda p: {"summary": "sent"}, fields=("message", "for"))}
    verbs.update(over)
    return VerbRegistry(verbs)


def _attached(reg=None):
    ck = Cockpit()
    ck.attach_verbs(VerbView(reg or _registry(), ck, "reported"))
    return ck


@pytest.mark.parametrize("enum,needle", [
    (["   "], "blank"),                         # cx1: a blank chip trims away and the send broadcasts
    (["cli", " cli"], "repeated"),              # cx1: one reader twice after the page's own trim
    (["cli", "cli"], "repeated"),               # small: a plain duplicate entry
])
def test_cx1_a_recipient_enum_entry_that_is_blank_or_repeated_after_trim_is_refused(enum, needle) -> None:
    c = copy.deepcopy(COMPOSE)
    c["fields"][1]["enum"] = enum
    with pytest.raises(CockpitRegistrationError, match=needle):
        Cockpit().register(_spec(compose=c))


@pytest.mark.parametrize("facets", [frozenset({"source", "at"}), frozenset({"for", "at"})])
def test_comp2_a_recipient_compose_requires_its_facet_and_source_declared(facets) -> None:
    with pytest.raises(CockpitRegistrationError, match="facet"):
        Cockpit().register(_spec(facets=facets))


def test_comp2_a_compose_with_no_recipient_field_needs_no_for_facet() -> None:
    ck = Cockpit()
    ck.register(_spec(facets=frozenset({"at"}), compose={"verb": "send_inbox", "fields": [{"name": "message", "type": "text"}]}))
    assert ck.panel("inbox")["compose"]["verb"] == "send_inbox"


def test_comp4_more_than_one_recipient_field_is_refused() -> None:
    c = copy.deepcopy(COMPOSE)
    c["fields"].append({"name": "cc", "type": "recipient", "enum": ["cli"]})
    with pytest.raises(CockpitRegistrationError, match="one recipient"):
        Cockpit().register(_spec(compose=c))


def test_comp5b_a_compose_on_a_count_face_is_refused() -> None:
    with pytest.raises(CockpitRegistrationError, match="count"):
        Cockpit().register(_spec(face="count", region="widgets"))


def test_cx3_an_empty_compose_verb_is_refused_even_when_the_panel_lists_it() -> None:
    with pytest.raises(CockpitRegistrationError, match="verb"):
        Cockpit().register(_spec(verbs=("",), compose={**COMPOSE, "verb": ""}))


def test_cx5_the_registered_compose_is_detached_from_the_callers_dict() -> None:
    c = copy.deepcopy(COMPOSE)
    ck = Cockpit()
    ck.register(_spec(compose=c))
    c["verb"] = "other"
    c["fields"][0]["type"] = "slider"
    c["fields"][1]["enum"].append("   ")
    assert ck.panel("inbox")["compose"] == COMPOSE


def test_cx5_the_panel_etag_covers_the_compose() -> None:
    a, b = Cockpit(), Cockpit()
    a.register(_spec())
    c = copy.deepcopy(COMPOSE)
    c["fields"][1]["enum"] = ["cli"]
    b.register(_spec(compose=c))
    assert a.panel("inbox")["etag"] != b.panel("inbox")["etag"]


# cx3 + comp1 + cx4: where the verb registry is known (attach, and every registration after it)
def test_cx3_a_compose_field_outside_the_verbs_allowlist_is_refused_at_registration_after_attach() -> None:
    c = copy.deepcopy(COMPOSE)
    c["fields"][0]["name"] = "msg"
    with pytest.raises(CockpitRegistrationError, match="msg"):
        _attached().register(_spec(compose=c))


def test_cx3_a_compose_field_outside_the_allowlist_is_refused_when_the_verbs_attach() -> None:
    c = copy.deepcopy(COMPOSE)
    c["fields"][0]["name"] = "msg"
    ck = Cockpit()
    ck.register(_spec(compose=c))
    with pytest.raises(CockpitRegistrationError, match="msg"):
        ck.attach_verbs(VerbView(_registry(), ck, "reported"))


def test_cx3_a_compose_verb_the_registry_does_not_hold_is_refused() -> None:
    with pytest.raises(CockpitRegistrationError, match="not a registered"):
        _attached(VerbRegistry({})).register(_spec())


def test_cx3_a_row_bound_verb_is_not_a_compose_verb() -> None:
    with pytest.raises(CockpitRegistrationError, match="row"):
        _attached().register(_spec(verbs=("spore_touch",), compose={**COMPOSE, "verb": "spore_touch"}))


def test_cx4_a_job_verb_is_refused_in_a_compose() -> None:
    reg = _registry(send_inbox=ActionVerb(handler=lambda p: {}, fields=("message", "for"), job=True))
    with pytest.raises(CockpitRegistrationError, match="job"):
        _attached(reg).register(_spec())


def test_cx3_a_verb_that_declares_no_fields_leaves_the_handler_to_validate() -> None:
    reg = _registry(send_inbox=ActionVerb(handler=lambda p: {}, fields=None))
    ck = _attached(reg)
    ck.register(_spec())
    assert ck.panel("inbox")["compose"] == COMPOSE


def test_cx3_a_valid_compose_registers_after_attach_and_its_panel_offers_the_verb() -> None:
    ck = _attached()
    ck.register(_spec())
    assert [a["verb"] for a in ck.panel("inbox")["actions"]] == ["send_inbox"]


# cx6: rediscovery replaces the compose with the rest of the discovery-owned fields, revalidated
def _rediscovered(compose_feed):
    ck = _attached()

    def discover(ctx):
        return [_spec(compose=compose_feed["c"], verbs=compose_feed.get("verbs", ("send_inbox",)))]
    ck.discover(discover)
    ck.manifest(CRED)
    return ck


def test_cx6_rediscovery_replaces_a_changed_compose_and_the_etag_moves() -> None:
    feed = {"c": copy.deepcopy(COMPOSE)}
    ck = _rediscovered(feed)
    before = ck.manifest(CRED)["panels"]["inbox"]["etag"]
    feed["c"] = copy.deepcopy(COMPOSE)
    feed["c"]["fields"][1]["enum"] = ["cli", "daemon", "argus"]
    m = ck.manifest(CRED)
    assert ck.panel("inbox")["compose"]["fields"][1]["enum"] == ["cli", "daemon", "argus"]
    assert m["panels"]["inbox"]["etag"] != before


def test_cx6_rediscovery_drops_a_compose_its_owner_removed() -> None:
    feed = {"c": copy.deepcopy(COMPOSE)}
    ck = _rediscovered(feed)
    feed["c"] = None
    ck.manifest(CRED)
    assert "compose" not in ck.panel("inbox")


def test_cx6_a_rediscovered_compose_that_fails_validation_is_an_error_and_the_panel_offers_nothing() -> None:
    feed = {"c": copy.deepcopy(COMPOSE)}
    ck = _rediscovered(feed)
    feed["c"] = copy.deepcopy(COMPOSE)
    feed["c"]["fields"][1]["enum"] = ["  "]
    m = ck.manifest(CRED)
    assert any(e["source"] == "discovery:inbox" and "blank" in e["message"] for e in m["errors"]), m["errors"]
    assert ck.panel("inbox")["actions"] == []


def test_cx6_a_rediscovery_that_withdraws_the_compose_verb_cannot_leave_a_compose_naming_it() -> None:
    feed = {"c": copy.deepcopy(COMPOSE)}
    ck = _rediscovered(feed)
    feed["verbs"] = ()
    m = ck.manifest(CRED)
    head = ck.panel("inbox")
    assert head["actions"] == []
    assert any(e["source"] == "discovery:inbox" for e in m["errors"]), m["errors"]


@pytest.mark.parametrize("field,extra,needle", [
    (1, {}, None),                                                                                  # absent keys: valid
    (1, {"default": ["daemon"], "transient_prefix": "seat:", "gone_match": "no live seat"}, None),
    (1, {"default": []}, None),
    (1, {"default": ["cli", "nobody"]}, "not an enum id"),
    (1, {"default": [" cli"]}, "not an enum id"),                                                 # the page compares exactly
    (1, {"default": ["cli", "cli"]}, "repeated"),
    (1, {"default": "cli"}, "list of ids"),
    (1, {"transient_prefix": ""}, "other than whitespace"),
    (1, {"transient_prefix": "  "}, "other than whitespace"),
    (1, {"gone_match": " ", "transient_prefix": "seat:"}, "other than whitespace"),
    (1, {"gone_match": 3, "transient_prefix": "seat:"}, "other than whitespace"),
    (1, {"transient_prefix": " seat:"}, "leading or trailing whitespace"),
    (1, {"gone_match": "gone"}, "needs a transient_prefix"),
    (0, {"default": ["cli"]}, "recipient field only"),                                               # on the text field
    (0, {"transient_prefix": "seat:"}, "recipient field only"),
    (0, {"gone_match": "gone"}, "recipient field only"),
])
def test_a_recipient_field_declares_its_default_transient_prefix_and_gone_match(field, extra, needle) -> None:
    c = copy.deepcopy(COMPOSE)
    c["fields"][field].update(extra)
    if needle is None:
        Cockpit().register(_spec(compose=c))
    else:
        with pytest.raises(CockpitRegistrationError, match=needle):
            Cockpit().register(_spec(compose=c))
