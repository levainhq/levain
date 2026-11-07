"""The envelope ``launches`` grant: how many seats the flow desk may start in a repo without asking again.

A signed, optional body key: ``"launches": [{"repo": ..., "count": N}]``, absent = no grant. These pin its shape and bounds
(parse_body, the CLI spec, the draft proposal, the composer), the compat promise (a body signed before it parses, hashes and
verifies byte for byte; an older reader refuses a body that carries it), the pure allowance helper the desk calls at each
launch, narrowing and widening, the plain words on the signed card, and who may set it (the cockpit composer, never a phone).
Fixtures are the envelope tests' own: real keys, real scratch repos, a fixed store clock."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit import threads as cockpit_threads
from levain.cockpit.consent import utc_stamp
from levain.contracts.canon import canonical, canonical_bytes
from levain.coordinator import cli as ccli
from levain.coordinator import envelope as env
from levain.coordinator import envelope_draft as ed
from levain.coordinator import lease
from levain.coordinator.envelopes import EnvelopeStore, narrow_scope
from levain.coordinator.phone import COCKPIT_DEVICE
from levain.coordinator.store import CoordError, Part
from tests.test_coordinator_envelope import HOUR, NOW, ctx, keys, repo, snapshot  # noqa: F401  (fixtures)

WEB = Path(env.__file__).resolve().parents[1] / "templates" / "web"
REPO = "/work/alpha"
ZEROS = "0" * 64
STOP = "2026-10-20T14:00:00.000000Z"
ISSUED = "2026-10-20T12:00:00.000000Z"


def _body(**over):
    """A parse-valid structured body over REPO, canonical; ``over`` replaces keys ."""
    b = {"format": env.BODY_FORMAT, "store_id": "a" * 32, "envelope_id": "ev-" + "1" * 32, "origin": "structured",
         "drop_id": "dr-" + "2" * 32, "words_sha256": "sha256:" + ZEROS, "binds": [], "supersedes": None, "draft": None,
         "where": [{"repo": REPO, "paths": ["."]}], "lands": [], "pushes": [], "publishes": [],
         "budget": {"tool_calls": 200, "tokens": 2000000, "dollars_cents": 0, "hours": 1},
         "stop_at": STOP, "issued_at": ISSUED}
    b.update(over)
    return b


def _grant(count=2, repo=REPO):
    return [{"repo": repo, "count": count}]


# 1 -- absent = no grant; an old body is untouched ----------------------------------------------------------------------
# the digest of the body above, computed by the code BEFORE the launches key existed: a body signed then must still verify
OLD_BODY_DIGEST = "sha256:e9485c3f14adf97b054c086b27f30ca36f6c974b98c92ece3582f11bf099707d"


def test_a_body_without_launches_parses_unchanged_and_grants_no_launch():
    text = canonical(_body())
    got = env.parse_body(text)
    assert "launches" not in got and canonical(got) == text
    assert env.envelope_digest(got) == OLD_BODY_DIGEST
    assert env.launch_allowance(got, REPO, datetime(2026, 10, 20, 12, 30, tzinfo=timezone.utc), 0) == 0


def test_a_previously_signed_old_format_envelope_still_verifies_and_is_stored_as_signed(ctx):
    d, _fresh, b, (text, payload, _sig) = ctx.structured("signed before launches existed")
    assert "launches" not in b and d.envelope_id == b["envelope_id"]
    stored = ctx.rows(f"SELECT body, digest FROM envelopes WHERE envelope_id = '{b['envelope_id']}'")[0]
    assert stored[0] == text and "launches" not in stored[0] and stored[1] == payload["digest"]
    (t,) = ctx.store.split(d.drop_id, [Part("work", "w")])
    assert "launches" not in ctx.store.effective(b["envelope_id"], t.thread_id)      # nothing is injected into a body
    assert ctx.live(b["envelope_id"], t) is None


def test_an_older_reader_refuses_a_body_that_carries_launches(monkeypatch):
    text = canonical(_body(launches=_grant()))
    assert env.parse_body(text)["launches"] == _grant()
    # the reader before this change: its key-set check was exactly BODY_KEYS, which is what no optional key means
    monkeypatch.setattr(env, "OPTIONAL_KEYS", frozenset())
    with pytest.raises(env.EnvelopeError, match="exactly the keys"):
        env.parse_body(text)
    assert env.parse_body(canonical(_body())) is not None          # and the old body still passes there


# 2 -- counts ---------------------------------------------------------------------------------------------------------
BAD_COUNTS = [0, -1, 5, 2.0, "2", True, None]


@pytest.mark.parametrize("count", BAD_COUNTS)
def test_a_bad_count_is_refused_by_parse_body(count):
    # 2.0 is refused even earlier: canonical JSON has no floats at all
    with pytest.raises(env.EnvelopeError, match=r"launches\[0\]\.count is an integer from 1 to 4|float 2\.0 in a leaf"):
        env.parse_body(canonical(_body(launches=_grant(count))))


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_a_count_from_one_to_four_parses(count):
    assert env.parse_body(canonical(_body(launches=_grant(count))))["launches"] == _grant(count)


@pytest.mark.parametrize("count", BAD_COUNTS)
def test_a_bad_count_is_refused_at_compose_in_the_same_words(ctx, count):
    """The CLI spec builder and the draft proposal both end in parse_body, so compose and verify refuse alike."""
    spec = {"where": [{"repo": ctx.path, "paths": ["."]}], "launches": [{"repo": ctx.path, "count": count}]}
    body = ccli._spec_body(spec, "words", ctx.src, ctx.store)
    refusal = r"launches\[0\]\.count is an integer from 1 to 4|float 2\.0 in a leaf"
    with pytest.raises(env.EnvelopeError, match=refusal):
        env.parse_body(canonical(body))
    with pytest.raises((CoordError, env.EnvelopeError)):
        ctx.store.check_body(canonical(body))
    proposal = _proposal(launches=[{"repo": REPO, "count": count}])
    with pytest.raises((ed.DraftError, ValueError), match=refusal):
        ed.check_proposal(proposal)


def _proposal(**over):
    p = {k: v for k, v in _body(origin="worded", binds=[{"thread_id": "th-" + "3" * 32, "rev": 1,
                                                          "members_sha256": "sha256:" + ZEROS}]).items() if k != "draft"}
    p.update(over)
    return p


def test_a_draft_proposal_may_carry_launches_or_not_and_nothing_else_extra():
    assert ed.check_proposal(_proposal())
    assert ed.check_proposal(_proposal(launches=_grant()))
    with pytest.raises(ed.DraftError, match="exactly the keys"):
        ed.check_proposal(_proposal(surprise=[]))


def test_a_spec_that_names_no_launches_builds_a_body_without_the_key(ctx):
    spec = {"where": [{"repo": ctx.path, "paths": ["."]}]}
    assert "launches" not in ccli._spec_body(spec, "words", ctx.src, ctx.store)
    assert "launches" not in ccli._spec_body({**spec, "launches": []}, "words", ctx.src, ctx.store)
    assert ccli._spec_body({**spec, "launches": [{"repo": ctx.path, "count": 2}]}, "words", ctx.src,
                           ctx.store)["launches"] == [{"repo": ctx.path, "count": 2}]


# 3 -- repo and shape ---------------------------------------------------------------------------------------------------
def test_a_launch_repo_outside_where_is_refused():
    with pytest.raises(env.EnvelopeError, match=r"launches\[0\]\.repo is not one of the envelope's where repos"):
        env.parse_body(canonical(_body(launches=_grant(2, "/work/elsewhere"))))


def test_a_repo_named_twice_is_refused():
    with pytest.raises(env.EnvelopeError, match="launches names one repo twice"):
        env.parse_body(canonical(_body(launches=_grant() + _grant(3))))


@pytest.mark.parametrize("launches", [[{"repo": REPO, "count": 2, "extra": 1}], [{"repo": REPO}], [{"count": 2}],
                                      "2", {"repo": REPO, "count": 2}, [REPO], [None]])
def test_a_launches_value_that_is_not_a_list_of_repo_count_is_refused(launches):
    with pytest.raises(env.EnvelopeError, match=r"launches is a list of \{repo, count\}"):
        env.parse_body(canonical(_body(launches=launches)))


# 4 -- the key set is exactly BODY_KEYS or BODY_KEYS plus launches ---------------------------------------------------
def test_the_key_set_is_exactly_the_body_keys_with_or_without_launches():
    full = _body(launches=_grant())
    lacking = {k: v for k, v in full.items() if k != "budget"}
    for bad in (lacking, {**full, "other": 1}, {**_body(), "other": 1}, {k: v for k, v in _body().items() if k != "stop_at"}):
        with pytest.raises(env.EnvelopeError, match="exactly the keys"):
            env.parse_body(canonical(bad))
    assert env.OPTIONAL_KEYS == {"launches"} and "launches" not in env.BODY_KEYS
    assert env.EFFECT_LISTS == ("lands", "pushes", "publishes")            # a launch is not an irreversible effect
    assert env.GRANT_LISTS == env.EFFECT_LISTS + ("launches",)
    assert (env.LAUNCH_DEFAULT, env.LAUNCH_MAX) == (2, 4)


def test_a_non_canonical_launches_body_is_refused():
    text = json.dumps(_body(launches=_grant()), indent=1)
    with pytest.raises(env.EnvelopeError, match="canonical"):
        env.parse_body(text)


# 5 -- launch_allowance ---------------------------------------------------------------------------------------------
AT = datetime(2026, 10, 20, 12, 30, tzinfo=timezone.utc)


def test_launch_allowance_counts_what_is_left():
    e = _body(launches=_grant(2))
    assert env.launch_allowance(e, REPO, AT, 0) == 2
    assert env.launch_allowance(e, REPO, AT, 1) == 1
    assert env.launch_allowance(e, REPO, AT, 2) == 0
    assert env.launch_allowance(e, REPO, AT, 7) == 0                      # never negative
    assert env.launch_allowance(e, "/work/beta", AT, 0) == 0              # a repo it does not name
    assert env.launch_allowance(_body(), REPO, AT, 0) == 0                # no grant


def test_launch_allowance_stops_at_stop_at():
    e = _body(launches=_grant(2))
    assert env.launch_allowance(e, REPO, datetime(2026, 10, 20, 13, 59, 59, tzinfo=timezone.utc), 0) == 2
    assert env.launch_allowance(e, REPO, datetime(2026, 10, 20, 14, 0, 0, tzinfo=timezone.utc), 0) == 0
    assert env.launch_allowance(e, REPO, datetime(2026, 10, 20, 15, 0, 0, tzinfo=timezone.utc), 0) == 0


@pytest.mark.parametrize("used", [-1, 1.0, "1", True, None])
def test_launch_allowance_refuses_a_used_that_is_not_a_count(used):
    with pytest.raises(env.EnvelopeError, match="used is a count"):
        env.launch_allowance(_body(launches=_grant()), REPO, AT, used)


def test_the_effective_stop_earlier_and_a_dropped_grant_end_the_allowance(ctx):
    d, _f, b, _s = ctx.structured("launch twice", launches=[{"repo": ctx.path, "count": 2}])
    (t,) = ctx.store.split(d.drop_id, [Part("work", "w")])
    eid = b["envelope_id"]
    eff = ctx.store.effective(eid, t.thread_id)
    assert env.launch_allowance(eff, ctx.path, NOW, 1) == 1
    assert ctx.store.attenuate(eid, "stop_earlier", utc_stamp(NOW + HOUR)) is True
    eff = ctx.store.effective(eid, t.thread_id)
    assert env.launch_allowance(eff, ctx.path, NOW + timedelta(minutes=59), 0) == 2
    assert env.launch_allowance(eff, ctx.path, NOW + HOUR, 0) == 0          # the EFFECTIVE stop, not the signed one
    ident = env.effect_identity("launches", {"repo": ctx.path, "count": 2})
    assert ctx.store.attenuate(eid, "drop_effect", ident) is True
    assert env.launch_allowance(ctx.store.effective(eid, t.thread_id), ctx.path, NOW, 0) == 0
    d2, _f2, b2, _s2 = ctx.structured("killed", launches=[{"repo": ctx.path, "count": 2}])
    (t2,) = ctx.store.split(d2.drop_id, [Part("work", "w")])
    ctx.store.attenuate(b2["envelope_id"], "kill")
    killed = ctx.store.effective(b2["envelope_id"], t2.thread_id)
    assert killed["launches"] == [] and env.launch_allowance(killed, ctx.path, NOW, 0) == 0


# 6 -- widens ------------------------------------------------------------------------------------------------------
def test_a_bigger_count_or_a_new_repo_widens_and_the_same_or_lower_does_not():
    base = _body(launches=_grant(2), where=[{"repo": REPO, "paths": ["."]}, {"repo": "/work/beta", "paths": ["."]}])
    same = dict(base)
    assert env.widens(base, same) is False
    assert env.widens(base, {**base, "launches": _grant(1)}) is False
    assert env.widens(base, {**base, "launches": []}) is False
    assert env.widens(base, {**base, "launches": _grant(3)}) is True
    assert env.widens(base, {**base, "launches": _grant(2) + _grant(1, "/work/beta")}) is True
    none = _body()
    assert env.widens(none, {**none, "launches": _grant(1)}) is True          # an old base holds no grant
    assert env.widens(none, none) is False


def test_a_supersede_that_adds_a_launch_grant_is_a_widening_that_needs_a_fresh_signature(ctx):
    d, _f, b, _s = ctx.structured("one envelope")
    (t,) = ctx.store.split(d.drop_id, [Part("work", "w")])
    head = ctx.store.effective(b["envelope_id"], t.thread_id)
    wider = {**head, "launches": [{"repo": ctx.path, "count": 1}]}
    assert env.widens(head, wider) is True
    assert env.widens(wider, head) is False


# 7 -- narrowing -----------------------------------------------------------------------------------------------------
def test_a_role_scope_cut_from_an_envelope_with_launches_carries_none():
    scope = {"where": [{"repo": REPO, "paths": ["."]}], "lands": [], "pushes": [], "publishes": [],
             "launches": _grant(3)}
    got = narrow_scope(scope, {"where": [{"repo": REPO, "paths": ["."]}]})
    assert got["launches"] == []
    assert "launches" not in narrow_scope({k: v for k, v in scope.items() if k != "launches"},
                                          {"where": [{"repo": REPO, "paths": ["."]}]})
    with pytest.raises(CoordError, match="not one of"):
        narrow_scope(scope, {"launches": [REPO]})                             # a role ceiling never grants launches


def test_a_seat_lease_scope_never_carries_launches():
    assert "launches" not in lease._SCOPE_KEYS


# 8 -- the signed card -------------------------------------------------------------------------------------------------
def test_the_signed_card_says_in_plain_words_what_may_be_launched():
    body = _body(launches=_grant(2))
    text = "\n".join(ccli._describe(body, None, now=AT))
    stop = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    assert "launches (seats) it may do:" in text
    assert f"may launch up to 2 seats in {REPO} until {stop}" in text


def test_no_grant_means_no_launches_line_and_a_changed_one_is_marked_against_its_predecessor():
    plain = "\n".join(ccli._describe(_body(), None, now=AT))
    assert "launch" not in plain
    old, new = _body(launches=_grant(2)), _body(launches=_grant(3))
    marked = "\n".join(ccli._describe(new, None, old=old, now=AT))
    assert "~ changed: " in marked and "up to 3 seats" in marked
    gone = "\n".join(ccli._describe(_body(), None, old=old, now=AT))
    assert "- removed: " in gone and "up to 2 seats" in gone
    added = "\n".join(ccli._describe(new, None, old=_body(), now=AT))
    assert "+ added: " in added


def test_the_cockpit_authority_line_names_the_launch_grant_only_when_there_is_one():
    assert cockpit_threads._effects_words(_body()) == "effects named: 0 land, 0 push, 0 publish"
    words = cockpit_threads._effects_words(_body(launches=_grant(2)), STOP)
    assert words.endswith(f"; may launch up to 2 seats in {REPO} until 2026-10-20 14:00 UTC")


# 9 -- who may set it -------------------------------------------------------------------------------------------------
def test_the_phone_route_names_the_cockpit_as_the_one_device_for_launches():
    from levain import web_server
    assert COCKPIT_DEVICE == web_server._COORD_DEVICE == "cockpit"


# 10 -- a signed round trip -------------------------------------------------------------------------------------------
def test_a_signed_launches_body_round_trips_and_a_tampered_count_fails_to_verify(ctx):
    drop_id = "dr-" + secrets.token_hex(16)
    words = "grant two launches"
    grant = [{"repo": ctx.path, "count": 2}]
    b = ctx.body(drop_id, words, launches=grant)
    text, payload, sig = ctx.sign(b)
    d, fresh = ctx.store.accept_drop(words, surface="cli", source=ctx.src, authority=ctx.snap, envelope=(text, payload, sig))
    assert fresh and d.envelope_id == b["envelope_id"]
    (t,) = ctx.store.split(d.drop_id, [Part("work", "w")])
    stored = ctx.rows(f"SELECT body, digest FROM envelopes WHERE envelope_id = '{b['envelope_id']}'")[0]
    assert json.loads(stored[0])["launches"] == grant and stored[1] == payload["digest"] == env.envelope_digest(b)
    assert ctx.store.effective(b["envelope_id"], t.thread_id)["launches"] == grant
    # the same signature over a body whose count was raised 2 -> 4: refused as another digest, nothing stored
    drop2 = "dr-" + secrets.token_hex(16)
    b2 = ctx.body(drop2, words + " again", launches=grant)
    _t2, payload2, sig2 = ctx.sign(b2)
    tampered = dict(b2, launches=[{"repo": ctx.path, "count": 4}])
    d2, _f = ctx.store.accept_drop(words + " again", surface="cli", source=ctx.src, authority=ctx.snap,
                                   envelope=(canonical(tampered), payload2, sig2))
    assert d2.envelope_id is None
    (receipt,) = ctx.receipt(d2)
    assert receipt["envelope_refused"] == "the signature is over another envelope digest"
    assert ctx.rows("SELECT COUNT(*) FROM envelopes") == [(1,)]


def test_a_launch_repo_outside_where_is_refused_at_acceptance_and_keeps_the_words(ctx):
    words = "launch in a repo it may not touch"
    b = ctx.body("dr-" + secrets.token_hex(16), words, launches=[{"repo": "/work/elsewhere", "count": 2}])
    text, payload, sig = ctx.sign(b)
    d, _fresh = ctx.store.accept_drop(words, surface="cli", source=ctx.src, authority=ctx.snap, envelope=(text, payload, sig))
    assert d.envelope_id is None and d.words == words
    assert "launches[0].repo is not one of the envelope's where repos" in ctx.receipt(d)[0]["envelope_refused"]


# the composer's source: off by default, the server's bounds, the cockpit modal only ------------------------------------
def test_the_composer_source_matches_the_servers_bounds_and_only_the_cockpit_modal_draws_it():
    form = (WEB / "envelope_form.js").read_text()
    assert f"LAUNCH_DEFAULT = {env.LAUNCH_DEFAULT}, LAUNCH_MAX = {env.LAUNCH_MAX};" in form
    assert "launchOn.checked = false;" in form and "if (launches) spec.launches = launches;" in form
    sign = (WEB / "sign.js").read_text()
    assert "launches: embedded," in sign
