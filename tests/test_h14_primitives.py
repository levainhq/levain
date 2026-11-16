"""H1-4 shape A, step 1: the shared ``parse_spec`` / ``render_spec`` primitive and the five measurements (M1-M5) the plan
rests on, pinned as real tests. Fixtures are the draft tests'. Each test names the failure it reproduces."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from levain import unshowable
from levain.desk import envelope as env
from levain.desk import envelope_drafts as drafts
from levain.desk.store import CoordError, Part, Source
from tests.test_desk_envelope import BUDGET, keys, repo, snapshot  # noqa: F401  (fixtures)
from tests.test_desk_envelope_draft import built, ctx, head_thread  # noqa: F401  (fixtures)


def _spec(ctx, **over):
    s = {"where": [{"repo": ctx.path, "paths": ["README"]}], "lands": [{"repo": ctx.path, "onto": "main"}],
         "pushes": [], "publishes": [], "budget": dict(BUDGET), "stop": "2h"}
    s.update(over)
    return s


# --- M1-M5: the measurements the plan rests on --------------------------------------------------------------------
def test_m1_attach_on_signed_head_stops_the_grant(ctx):
    """Why the tap must not attach: an attach moves the thread's revision, which lapses a live signed grant."""
    _d, b, t = head_thread(ctx)
    assert ctx.live(b["envelope_id"], t) is None
    spec = ctx.plain(json.dumps(_spec(ctx)))
    ctx.store.split(spec.drop_id, [Part("work", attach=t.thread_id)])
    assert "moved past the revision" in ctx.live(b["envelope_id"], t)


def test_m1b_a_non_work_part_never_joins_a_work_thread(ctx):
    """Why no non-work attach: the store refuses it, so a spec drop cannot be hidden from the worker that way."""
    _d, t = ctx.plain_thread("fix the readme")
    with pytest.raises(CoordError, match="a note part never joins a work thread"):
        ctx.store.split(ctx.plain("x").drop_id, [Part("note", attach=t.thread_id)])


def test_m2_a_spec_drop_that_is_not_a_member_needs_a_series_first(ctx):
    """With no series and no membership the cite is refused (so the writer opens a series first)."""
    _d, t = ctx.plain_thread("fix the readme")
    spec = ctx.plain(json.dumps(_spec(ctx)))
    with pytest.raises(CoordError, match="not in this thread and is not newer than this draft series"):
        drafts.spec_rule(ctx.store, t.thread_id, spec.drop_id, by="operator")


def test_m3_no_attach_path_keeps_the_head_live_and_the_new_draft_signs(ctx):
    """The shape the tap builds: opener, then the spec drop, then spec_rule. The head stays live; the draft signs."""
    _d, b, t = head_thread(ctx)
    opener = drafts.create(ctx.store, t.thread_id, {}, [], by="operator")
    spec = ctx.plain(json.dumps(_spec(ctx)))
    r = drafts.spec_rule(ctx.store, t.thread_id, spec.drop_id, by="operator")
    assert (opener.revision, r.revision, r.draft_id == opener.draft_id) == (1, 2, True)
    assert ctx.live(b["envelope_id"], t) is None
    signed, _since, _against = built(ctx, r)
    stored, created = ctx.accept(signed)
    assert created and stored.body["supersedes"]["envelope_id"] == b["envelope_id"]
    assert ctx.store.thread(t.thread_id).rev == t.rev


def test_m4_spec_rule_is_not_idempotent(ctx):
    """Why the tap is one transaction: the same drop through spec_rule twice writes revision N+1 again."""
    _d, t = ctx.plain_thread("fix the readme")
    r1 = drafts.drop_rule(ctx.store, t.thread_id, by="operator")
    spec = ctx.plain(json.dumps(_spec(ctx)))
    a = drafts.spec_rule(ctx.store, t.thread_id, spec.drop_id, by="operator")
    b2 = drafts.spec_rule(ctx.store, t.thread_id, spec.drop_id, by="operator")
    assert (r1.revision, a.revision, b2.revision) == (1, 2, 3)


def test_m5_escaped_bidi_passes_an_ascii_check_on_the_text():
    """Why the check runs on parsed values: the escaped form is plain ASCII, the parsed value is not."""
    text = json.dumps({"where": [{"repo": "/r‮", "paths": ["."]}]})
    assert unshowable.describe(text, allow="") is None
    parsed = json.loads(text)["where"][0]["repo"]
    assert unshowable.describe(parsed, allow="") is not None


# --- parse_spec ----------------------------------------------------------------------------------------------------
def test_parse_spec_reads_the_cli_grammar(ctx):
    """The values spec_rule used to compute inline: the editable fields the spec names, the names, the stop span."""
    words = json.dumps(_spec(ctx))
    fields, named, span = drafts.parse_spec(words, Source(), ctx.store)
    assert set(fields) == set(drafts._EDIT_SPEC)
    assert fields["budget"] == BUDGET and fields["pushes"] == []
    assert named == [*fields, "stop_at"] and span == timedelta(hours=2)


def test_parse_spec_without_a_stop_names_no_stop(ctx):
    s = _spec(ctx)
    del s["stop"]
    fields, named, span = drafts.parse_spec(json.dumps(s), Source(), ctx.store)
    assert span is None and "stop_at" not in named and "stop_at" not in fields


@pytest.mark.parametrize("words,match", [
    ("make the readme say hello", "not an envelope spec: its words are not JSON"),
    ("[1, 2]", "not an envelope spec: a JSON object with keys among"),
    ("{}", "not an envelope spec: a JSON object with keys among"),
    ('{"colour": 1}', "not an envelope spec: a JSON object with keys among"),
    ('{"launches": [], "stop": "1h"}', "never carries a launch grant"),
    ('{"budget": {}, "stop": 5}', "stop is a duration text"),
    ('{"budget": {}, "stop": "soon"}', "--stop is a duration like"),
])
def test_parse_spec_refusals_name_the_subject(ctx, words, match):
    """A refusal reads with the caller's subject ("drop dr-..", "these words"), so a card and a rule say it alike."""
    with pytest.raises(CoordError, match=match):
        drafts.parse_spec(words, Source(), ctx.store)
    if "stop" not in match:     # the stop's own refusals are the CLI grammar's, and name no subject
        with pytest.raises(CoordError, match="drop dr-abc"):
            drafts.parse_spec(words, Source(), ctx.store, subject="drop dr-abc")


def test_parse_spec_refuses_a_duplicate_key(ctx):
    """json.loads keeps the LAST of a repeated key: a field he reads first and a field that is parsed would differ."""
    words = '{"budget": {"hours": 1}, "budget": {"hours": 9}}'
    with pytest.raises(CoordError, match="repeats the key 'budget'"):
        drafts.parse_spec(words, Source(), ctx.store)
    nested = '{"where": [{"repo": "/a", "repo": "/b"}]}'
    with pytest.raises(CoordError, match="repeats the key 'repo'"):
        drafts.parse_spec(nested, Source(), ctx.store)


@pytest.mark.parametrize("where", [
    lambda ctx: {"where": [{"repo": ctx.path + "‮", "paths": ["."]}]},        # escaped by json.dumps (M5)
    lambda ctx: {"where": [{"repo": ctx.path, "paths": ["a​b"]}]},
    lambda ctx: {"stop‮": "1h"},
])
def test_parse_spec_refuses_unshowable_literal_and_escaped(ctx, where):
    """Reproduces M5: describe() on the JSON text is None for the escaped form; the check is on parsed strings."""
    for ensure_ascii in (True, False):
        words = json.dumps(where(ctx), ensure_ascii=ensure_ascii)
        with pytest.raises(CoordError, match="unshowable character U\\+"):
            drafts.parse_spec(words, Source(), ctx.store)


def test_spec_rule_still_names_the_drop(ctx):
    """The refactor keeps spec_rule's own refusal wording ("drop dr-... is not an envelope spec")."""
    _d, t = ctx.plain_thread("fix the readme")
    free = ctx.plain("make it say hello")
    with pytest.raises(CoordError, match=f"drop {free.drop_id} is not an envelope spec"):
        drafts.spec_rule(ctx.store, t.thread_id, free.drop_id, by="operator")


# --- render_spec ---------------------------------------------------------------------------------------------------
def _base(**over):
    b = {"where": [], "lands": [], "pushes": [], "publishes": [], "budget": {k: 0 for k in env.BUDGET_KEYS},
         "stop_at": "2026-01-01T00:00:00Z"}
    b.update(over)
    return b


def test_render_spec_marks_what_is_more_than_now(ctx):
    """Every field that widens authority is marked; with no head everything the spec names is new."""
    s = _spec(ctx, pushes=[{"repo": ctx.path, "remote": "origin", "url": "u", "branch": "main"}])
    fields, _named, span = drafts.parse_spec(json.dumps(s), Source(), ctx.store)
    lines = {ln["field"]: ln for ln in drafts.render_spec(fields, span, _base())}
    assert lines["pushes"]["wider"] and lines["budget"]["wider"] and lines["where"]["wider"] and lines["lands"]["wider"]
    assert "2h" in lines["stop"]["text"] and not lines["stop"]["wider"]
    assert ctx.path in lines["where"]["text"] and "README" in lines["where"]["text"]


def test_render_spec_against_a_head_that_already_holds_it(ctx):
    """Nothing wider when the head already holds the paths, the land and an equal budget; pushes still mark wider."""
    fields, _n, span = drafts.parse_spec(json.dumps(_spec(ctx)), Source(), ctx.store)
    head = _base(where=fields["where"], lands=fields["lands"], budget=dict(BUDGET))
    lines = {ln["field"]: ln for ln in drafts.render_spec(fields, span, head)}
    assert not any(lines[k]["wider"] for k in ("where", "lands", "budget"))
    assert not lines["pushes"]["wider"]            # an empty pushes list names nothing
    assert drafts.render_spec(fields, span, None)  # no head at all is the empty base


def test_render_spec_only_names_what_the_spec_names(ctx):
    fields, _n, span = drafts.parse_spec(json.dumps({"budget": dict(BUDGET)}), Source(), ctx.store)
    assert [ln["field"] for ln in drafts.render_spec(fields, span, None)] == ["budget"]


# --- L3 round 1 (h14c) ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("over", [
    lambda p: {"pushes": [1]},                                          # an effect that is not an object
    lambda p: {"budget": {"hours": True}},                              # a budget count that is not an integer
    lambda p: {"budget": {"hours": -1}},
    lambda p: {"where": [{"repo": p, "paths": ["../escape"]}]},        # a path out of its repo
    lambda p: {"stop": "999h"},                                         # past the longest an envelope may run
    lambda p: {"lands": [{"repo": p, "onto": "main", "criteria": ["free text"]}]},
])
def test_parse_spec_refuses_values_the_draft_writer_would_refuse(ctx, over):
    """codex L3 r1 MED: parse_spec ran the CLI grammar but not the draft's value checks, so a door advertised a card as
    dry-run valid that a tap could only refuse late, and a preview could crash on an effect that is not an object."""
    with pytest.raises(CoordError):
        drafts.parse_spec(json.dumps(_spec(ctx, **over(ctx.path))), Source(), ctx.store)


def test_parse_spec_reads_nothing_from_the_store(tmp_path):
    """codex L3 r1 MED: the dry run read the store's id, and opening a store that is not there MADE it, so a refused
    door call left a database behind. The spec's values never depend on the store."""
    from levain.desk.envelopes import EnvelopeStore
    where = tmp_path / "nothing-here"
    store = EnvelopeStore(where)
    drafts.parse_spec(json.dumps({"budget": dict(BUDGET), "stop": "2h"}), Source(), store)
    assert not where.exists()


@pytest.mark.parametrize("words", [
    "[" * 200_000 + "]" * 200_000,                                              # deeper than the decoder recurses
    json.dumps({"where": [{"repo": "/r", "paths": ["."]}]}).replace('["."]', "[" * 3000 + '"."' + "]" * 3000),
])
def test_parse_spec_refuses_deep_nesting_as_a_grammar_refusal(ctx, words):
    """codex L3 r1 LOW: a RecursionError (in the decoder or in the unshowable walk) was a 500, not a named refusal."""
    with pytest.raises(CoordError):
        drafts.parse_spec(words, Source(), ctx.store)


def test_render_spec_marks_a_land_whose_criteria_are_weaker_than_the_heads_and_shows_them(ctx):
    """codex L3 r1 MED: render compared only a land's target, so the same branch with the head's limits dropped read as
    'not more than now', though env.widens calls it widening; and the limits were never shown at all."""
    tight = _spec(ctx, lands=[{"repo": ctx.path, "onto": "main", "criteria": [{"max_files": 3}]}])
    loose = _spec(ctx)
    head_fields, _n, _s = drafts.parse_spec(json.dumps(tight), Source(), ctx.store)
    fields, _n, span = drafts.parse_spec(json.dumps(loose), Source(), ctx.store)
    head = _base(where=head_fields["where"], lands=head_fields["lands"], budget=dict(BUDGET))
    from datetime import datetime, timezone
    from levain.cockpit.consent import utc_stamp
    at = utc_stamp(datetime.now(timezone.utc))
    assert env.widens({**head, "stop_at": at}, {**head, "lands": fields["lands"], "stop_at": at})   # what the sign step reads
    lines = {ln["field"]: ln for ln in drafts.render_spec(fields, span, head)}
    assert lines["lands"]["wider"], lines["lands"]
    assert "no limits" in lines["lands"]["text"]
    same = {ln["field"]: ln for ln in drafts.render_spec(head_fields, span, head)}
    assert not same["lands"]["wider"] and "max_files 3" in same["lands"]["text"]
