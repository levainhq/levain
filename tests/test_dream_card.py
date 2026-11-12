"""levain.dream.card (D1-1c): the morning card's body, its bound, and Q-D2 (a) as the filed ``authority_relevant`` bit.

Q-D2 (a), decided 10-30: a card with a non-empty ``his`` (an unresolved contradiction, or ``unknown-commit``) files
``authority_relevant = 1``; a consolidated card with only ``own``, a did-not-consolidate card, a ``recovered:
committed, not recorded`` card and a ``recovered: committed after stop`` card each file ``authority_relevant = 0``.

Cards are filed into a scratch desk store. The ``agent`` feed is P8's (``review.FEEDS`` on main does not carry
it), so these tests file under ``digest``, an existing feed: the bit and the bound are the feed's to enforce alike.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from levain.contracts.canon import canonical
from levain.desk import review
from levain.desk.store import CoordStore
from levain.dream import card as cd
from levain.dream import recover as rc
from levain.dream import snapshot as sn
from levain.dream import usage as us
from tests.test_dream_snapshot import _VALID, add_episode, dream, occ, scratch, text_n  # noqa: F401

SD = "/data/levain store"          # the directory holding the store's memory.db (a card's commands need it; no default)

FEED = "digest"


@pytest.fixture
def review_store(tmp_path):
    d = tmp_path / "q"
    d.mkdir(mode=0o700)
    return CoordStore(d)


def filed(store, c: cd.Card):
    item, new = cd.file_card(store, c, feed=FEED)
    assert new
    return item


def big_text(n: int, tag: str) -> str:
    """A 25K-character neocortex-shaped text whose every Patterns / Decisions line carries ``tag``."""
    pats = "\n".join(f"!! pattern_{i:03d} | {tag} {i}x (2026-11-0{1 + i % 3}) [evidence: e{i:04x} \"{tag} line {i}\"]"
                     for i in range(170))
    decs = "\n".join(f"[decided, 2026-11-02, Phill] **DECISION {i:03d} {tag}** [episode: e{i:04x}] (*\"{tag} words {i}\"*)"
                     for i in range(110))
    return f"## State\nState {tag}.\n\n## Patterns\n{pats}\n\n## Decisions\n{decs}\n\n## Understanding\nPhill {tag}.\n"


# ------------------------------------------------------------------------------------------------- Q-D2 (a), pinned

def test_the_bound_is_the_review_stores_bound():
    assert cd.BODY_LIMIT == 2 * review._TEXT_MAX


def test_an_unresolved_contradiction_files_authority_relevant_1(review_store):
    notes = {"contradictions": [{"what": "flow 2 says X, levain 2a says not X", "episodes": ["d8c428d6"]}]}
    c = cd.build_card("consolidated", store_dir=SD, occurrence="o1", day="2026-11-02", prior=_VALID, saved=text_n(1), notes=notes)
    assert c.authority_relevant and len(c.body["his"]) == 1 and "d8c428d6" in c.body["his"][0]
    item = filed(review_store, c)
    assert item.authority_relevant is True
    assert review.get_item(review_store, item.item_id).authority_relevant is True


def test_unknown_commit_files_authority_relevant_1(review_store):
    spec = rc._card("failed", rc.UNKNOWN_COMMIT, "o2", his=["the save may have committed; nothing was adopted"])
    c = cd.from_spec(spec, store_dir=SD, occ_dir=review_store.directory.parent)
    assert c.kind == "failed" and c.authority_relevant and c.body["reason"] == rc.UNKNOWN_COMMIT
    assert filed(review_store, c).authority_relevant is True


def test_a_consolidated_card_with_only_own_files_authority_relevant_0(review_store):
    notes = {"premise_flags": [{"ruling": "R", "why": "the ground moved"}], "for_phill": [{"what": "keep X?"}],
             "retired": [{"what": "Y", "his_words": "Y is dead as of today for all of us", "episodes": ["e1"]}]}
    c = cd.build_card("consolidated", store_dir=SD, occurrence="o3", day="2026-11-02", prior=_VALID, saved=text_n(1), notes=notes)
    assert c.body["his"] == [] and not c.authority_relevant
    assert any("premise moved: R" in x for x in c.body["own"]) and any("for Phill: keep X?" in x for x in c.body["own"])
    item = filed(review_store, c)
    assert item.authority_relevant is False
    # an own-only card is flow's to clear: the store lets it be cleared
    assert review.clear_item(review_store, item.item_id).state == "cleared"


def test_a_did_not_consolidate_card_files_authority_relevant_0(review_store):
    c = cd.build_card("did-not-consolidate", store_dir=SD, occurrence="2026-11-02", day="2026-11-02",
                      reason="the laptop slept", remedy="levain dream run-now")
    assert c.dedupe_key == "dream-night:dream:2026-11-02" and not c.authority_relevant
    assert filed(review_store, c).authority_relevant is False


@pytest.mark.parametrize("reason", [rc.COMMITTED_NOT_RECORDED, rc.COMMITTED_AFTER_STOP])
def test_a_recovered_commit_card_files_authority_relevant_0(review_store, reason):
    spec = rc._card("consolidated", reason, "o4")
    c = cd.from_spec(spec, store_dir=SD, occ_dir=review_store.directory.parent)
    assert c.kind == "consolidated" and c.body["reason"] == reason and c.body["his"] == []
    assert not c.authority_relevant and filed(review_store, c).authority_relevant is False


def test_a_rolled_back_card_is_flows_and_keys_by_the_rollback(review_store):
    spec = {"kind": "rolled-back", "dedupe_key": "dream-rollback:o5", "occurrence": "o5", "who": "operator",
            "at": "2026-11-03T06:00:00Z", "files": ["memory.continuity.md"], "limit": sn.LIMIT}
    c = cd.from_spec(spec, store_dir=SD, occ_dir=review_store.directory.parent)
    assert c.dedupe_key == "dream-rollback:o5" and not c.authority_relevant
    assert "memory.continuity.md" in c.body["reason"] and sn.LIMIT in c.body["reason"]


def test_a_card_is_filed_once_per_key(review_store):
    c = cd.build_card("did-not-consolidate", store_dir=SD, occurrence="n", day="2026-11-02", reason="r")
    assert cd.file_card(review_store, c, feed=FEED)[1] is True
    assert cd.file_card(review_store, c, feed=FEED)[1] is False


def test_the_agent_feed_is_refused_until_p8_adds_it(review_store):
    if cd.FEED in review.FEEDS:
        pytest.skip("P8 has added the agent feed")
    c = cd.build_card("did-not-consolidate", store_dir=SD, occurrence="n", day="2026-11-02", reason="r")
    with pytest.raises(review.CoordError):
        cd.file_card(review_store, c)


# --------------------------------------------------------------------------------------------- his: the notes' rules

def test_a_retirement_without_his_verified_words_is_his_when_the_source_is_given():
    notes = {"retired": [{"what": "ZERO PROMPTS", "his_words": "zero prompts might not be feasible here today",
                          "episodes": ["4b166f5e"]}]}
    ok = cd.build_card("consolidated", store_dir=SD, occurrence="a", notes=notes,
                       source={"4b166f5e": "honestly I realize ZERO prompts might not be feasible here, today"})
    assert ok.body["his"] == [] and not ok.authority_relevant
    bad = cd.build_card("consolidated", store_dir=SD, occurrence="b", notes=notes, source={"4b166f5e": "something else entirely"})
    assert bad.authority_relevant and "without his verified words" in bad.body["his"][0]
    short = cd.build_card("consolidated", store_dir=SD, occurrence="c", source={"e": "yes"},
                          notes={"retired": [{"what": "W", "his_words": "yes", "episodes": ["e"]}]})
    assert short.authority_relevant                      # too few words to tell what they retire
    unchecked = cd.build_card("consolidated", store_dir=SD, occurrence="d", notes=notes)
    assert not unchecked.authority_relevant              # no source given: own, never a guess


def test_unreadable_notes_are_a_card_line_and_never_his():
    c = cd.build_card("consolidated", store_dir=SD, occurrence="a", notes={"notes_problem": "JSONDecodeError: torn"})
    assert not c.authority_relevant and any("notes were unreadable" in x for x in c.body["own"])
    odd = cd.build_card("consolidated", store_dir=SD, occurrence="b", notes={"contradictions": "not a list"})
    assert odd.authority_relevant and "not a list" in odd.body["his"][0]     # a malformed note surfaces, to him


def test_a_card_kind_must_be_one_of_the_four():
    with pytest.raises(cd.CardError):
        cd.build_card("quiet", store_dir=SD, occurrence="a")


# ------------------------------------------------------------------------------------------------ the diff and own

def test_own_names_decisions_that_left_patterns_that_changed_and_the_identity_tier():
    prior = ("## Patterns\n!!! alpha | 3x (2026-11-01)\n!! beta | 2x (2026-11-01)\n\n"
             "## Decisions\n[decided] **KEEP ME** x\n\n[decided] **FOLD ME** y\n\n[decided] **DROP ME** z\n\n"
             "## Understanding\nPhill is a ensemble mind.\n\nHe loves games.\n")
    saved = ("## Patterns\n!!! alpha | 4x (2026-11-02)\n!! gamma | 1x (2026-11-02)\n\n"
             "## Decisions\n[decided] **KEEP ME** x\n\n[forwarded] **FOLD ME** -> state/x.md\n\n"
             "## Understanding\nPhill is a ensemble mind, changed.\n\nHe loves games.\n")
    own = cd.build_card("consolidated", store_dir=SD, occurrence="a", prior=prior, saved=saved).body["own"]
    assert "Decisions: DROP ME left the Decisions entries (no forwarding address in the saved text)" in own
    assert any(x.startswith("Decisions: FOLD ME") for x in own) is False        # FOLD ME is still an entry title
    assert "Patterns: removed beta" in own and "Patterns: added gamma" in own
    assert not any("alpha" in x for x in own)                                     # a count change is not a rename
    assert sum("Understanding (identity tier) changed" in x for x in own) == 1
    assert not any("Understanding (identity tier) removed" in x for x in own)     # edited in place: one line


def test_a_title_that_left_decisions_but_stands_elsewhere_names_where():
    prior = "## Decisions\n[decided] **GONE** x\n\n## Context\nnothing\n"
    saved = "## Decisions\nnone\n\n## Context\nSee GONE for the record.\n"
    own = cd.build_card("consolidated", store_dir=SD, occurrence="a", prior=prior, saved=saved).body["own"]
    assert own == ["Decisions: GONE left the Decisions entries (its title still stands in Context)"]


def test_per_section_counts_and_the_diff_artifact_carry_the_change(tmp_path):
    prior, saved = _VALID, text_n(2).replace("Nothing committed yet.", "A decision.")
    c = cd.build_card("consolidated", store_dir=SD, occurrence="a", prior=prior, saved=saved, out_dir=tmp_path)
    d = c.body["diff"]
    text = (tmp_path / cd.DIFF_FILE).read_text(encoding="utf-8")
    assert d["sha256"] == hashlib.sha256(text.encode()).hexdigest() and d["path"] == str(tmp_path / cd.DIFF_FILE)
    assert d["sections"] == {"Decisions": {"added": 1, "removed": 1}, "Understanding": {"added": 1, "removed": 1}}
    assert "+Day 2." in text and "-Early days." in text


# ----------------------------------------------------------------------------------------------- the over-limit body

def test_two_25k_neocortices_fit_and_the_diff_is_an_artifact(tmp_path, review_store):
    prior, saved = big_text(0, "before"), big_text(1, "after")
    assert len(prior) > 20_000 and len(cd.full_diff(prior, saved)) > cd.BODY_LIMIT      # the diff alone is refused
    with pytest.raises(review.CoordError, match="too long"):
        review.file_item(review_store, FEED, {"diff": cd.full_diff(prior, saved)}, dedupe_key="raw")
    c = cd.build_card("consolidated", store_dir=SD, occurrence="big", day="2026-11-02", prior=prior, saved=saved, out_dir=tmp_path)
    assert len(canonical(c.body)) <= cd.BODY_LIMIT
    assert filed(review_store, c).body["diff"]["sha256"] == c.body["diff"]["sha256"]
    # own was cut to first lines and a count; the full list sits beside the occurrence
    assert c.body["own_total"] > len(c.body["own"]) >= 1 and c.body["own_shown"] == len(c.body["own"])
    assert all(len(x) <= 80 for x in c.body["own"])               # first lines only
    full = json.loads((tmp_path / cd.OWN_FILE).read_text())
    assert len(full) == c.body["own_total"] and c.body["own_path"] == str(tmp_path / cd.OWN_FILE)


def test_his_is_never_cut_and_moves_whole_to_an_artifact_when_it_alone_passes_the_limit(tmp_path, review_store):
    notes = {"contradictions": [{"what": f"contradiction {i}: " + "x" * 300, "episodes": [f"e{i}"]} for i in range(40)]}
    c = cd.build_card("consolidated", store_dir=SD, occurrence="his", day="2026-11-02", prior=big_text(0, "a"), saved=big_text(1, "b"),
                      notes=notes, out_dir=tmp_path)
    assert len(canonical(c.body)) <= cd.BODY_LIMIT
    assert c.authority_relevant                                   # decided from the full list, before the cut
    assert c.body["his"] == [] and c.body["his_count"] == 40 and c.body["his_path"] == str(tmp_path / cd.HIS_FILE)
    kept = json.loads((tmp_path / cd.HIS_FILE).read_text())
    assert len(kept) == 40 and all(len(x) > 300 for x in kept)    # whole entries, none shortened
    assert filed(review_store, c).authority_relevant is True


def test_small_his_stays_in_the_body_whole_even_when_own_is_cut(tmp_path):
    notes = {"contradictions": [{"what": "one real one", "episodes": ["e1"]}]}
    c = cd.build_card("consolidated", store_dir=SD, occurrence="a", prior=big_text(0, "a"), saved=big_text(1, "b"), notes=notes,
                      out_dir=tmp_path)
    assert c.body["his"] == ["contradiction: one real one [e1]"] and "his_path" not in c.body


def test_a_cut_without_an_out_dir_raises_instead_of_dropping_content():
    with pytest.raises(cd.CardError):
        cd.build_card("consolidated", store_dir=SD, occurrence="a", prior=big_text(0, "a"), saved=big_text(1, "b"))


def test_a_pathological_card_falls_back_to_a_skeleton_that_still_files(tmp_path, review_store):
    huge_reason = "r" * 20_000
    c = cd.build_card("failed", store_dir=SD, occurrence="p", day="2026-11-02", reason=huge_reason, prior=big_text(0, "a"),
                      saved=big_text(1, "b"), out_dir=tmp_path, notes={"contradictions": ["c"]})
    assert len(canonical(c.body)) <= cd.BODY_LIMIT and c.authority_relevant
    remedy = filed(review_store, c).body.get("remedy")             # the skeleton keeps the next step; a failed card
    assert remedy and "nothing to roll back" in remedy             # changed nothing, so it names no rollback


# ---------------------------------------------------------------------------------------- from a real dream's files

def test_a_real_dream_becomes_a_consolidated_card_from_its_own_files(scratch):
    r = dream(scratch, "o1", _VALID)
    assert r.outcome == "saved"
    add_episode(scratch, "two")
    assert dream(scratch, "o2", text_n(2)).outcome == "saved"
    prior, saved = cd.texts_for(scratch, occ(scratch, "o2"))
    assert prior == _VALID and saved == text_n(2)
    spec = rc._card("consolidated", rc.COMMITTED_NOT_RECORDED, "o2")
    c = cd.from_spec(spec, store_dir=scratch.parent, occ_dir=occ(scratch, "o2"), db_path=scratch, day="2026-11-02")
    assert c.body["diff"]["sections"] == {"Understanding": {"added": 1, "removed": 1}}
    assert c.body["usage"]["source"] == "fake" and c.body["usage"]["readable"] is True
    assert c.body["models"] == ["fake"] and c.body["composes"]["n"] == 1
    assert c.body["remedy"] == f"levain dream rollback o2 --store-dir {scratch.parent}" and not c.authority_relevant
    assert (occ(scratch, "o2") / cd.DIFF_FILE).is_file()


def test_a_first_dream_has_no_prior_text(scratch):
    dream(scratch, "o1", _VALID)
    prior, saved = cd.texts_for(scratch, occ(scratch, "o1"))
    assert prior == "" and saved == _VALID


def test_a_missing_snapshot_is_a_card_error(tmp_path):
    with pytest.raises(cd.CardError):
        cd.texts_for(tmp_path / "memory.db", tmp_path)


def test_a_real_rollback_card_files_as_flows(scratch, review_store):
    dream(scratch, "o1", _VALID)
    add_episode(scratch, "two")
    dream(scratch, "o2", text_n(2))
    res = sn.rollback(scratch, occ(scratch, "o2"), who="operator")
    assert res.ok and res.card["kind"] == "rolled-back"
    c = cd.from_spec(res.card, store_dir=SD, occ_dir=occ(scratch, "o2"))
    assert c.dedupe_key == "dream-rollback:o2" and not c.authority_relevant
    assert filed(review_store, c).authority_relevant is False


# ------------------------------------------------------------------------------------------------------ usage.json

def test_usage_json_reads_back_as_a_zero_loss_a0_usage(scratch):
    dream(scratch, "o1", _VALID)
    u = us.read_usage(occ(scratch, "o1"))
    assert (u.usd_micros, u.input_tokens, u.output_tokens, u.source) == (None, 1, 1, "fake")   # tokens route: no price


def test_the_claude_route_is_dollars_only_when_every_compose_was_readable():
    doc = {"unit": "usd_list", "readable": True, "actual_usd_micros": 6_300_000, "input_tokens": 12,
           "output_tokens": 30211, "source": "dream:claude-p"}
    assert us.to_usage(doc) == us.Usage(6_300_000, 12, 30211, "dream:claude-p")
    assert us.to_usage({**doc, "readable": False}).usd_micros is None
    assert us.to_usage({**doc, "actual_usd_micros": -5}).usd_micros is None
    assert us.to_usage({**doc, "input_tokens": None}).input_tokens is None


def test_a_missing_or_torn_usage_file_is_unreadable_and_debits_the_whole_reservation(tmp_path):
    assert us.read_usage(tmp_path) == us.Usage(None, None, None, us.UNREADABLE_SOURCE)
    (tmp_path / us.USAGE_FILE).write_text("{torn", encoding="utf-8")
    assert us.read_usage(tmp_path).usd_micros is None and us.read_usage(tmp_path).source == us.UNREADABLE_SOURCE
    (tmp_path / us.USAGE_FILE).write_text("[1, 2]", encoding="utf-8")
    assert us.read_doc(tmp_path) is None


def test_the_cards_usage_block_has_figures_and_no_compose_rows():
    doc = {"unit": "usd_list", "readable": True, "actual_usd_micros": 1, "reserved_usd_micros": 20_000_000,
           "input_tokens": 2, "output_tokens": 3, "source": "s", "composes": [{"n": 1, "error": None}]}
    block = us.summary(doc)
    assert "composes" not in block and block["reserved_usd_micros"] == 20_000_000
    assert us.summary(None)["readable"] is False


def test_compose_refusals_ride_the_card_in_one_line_each():
    doc = {"unit": "tokens", "readable": True, "composes": [
        {"n": 1, "error": "the saved text dropped the Decisions section\nand more", "model": "m1"},
        {"n": 2, "error": None, "model": "m1", "models_charged": ["m1", "m2"]}]}
    c = cd.build_card("consolidated", store_dir=SD, occurrence="a", usage_doc=doc)
    assert c.body["composes"] == {"n": 2, "refusals": ["compose 1: the saved text dropped the Decisions section"],
                                  "refusals_total": 1}
    assert c.body["models"] == ["m1", "m2"]


# ------------------------------------------------------------------------------------------- review fixes (L3 round 1)

def test_a_card_describes_its_own_dream_after_a_later_dream_moved_the_live_text(scratch):
    """Reproduced: ``texts_for`` read the live continuity, so o1's card showed o2's text as what o1 saved."""
    dream(scratch, "o1", _VALID)
    add_episode(scratch, "two")
    assert dream(scratch, "o2", text_n(2)).outcome == "saved"
    prior, saved = cd.texts_for(scratch, occ(scratch, "o1"))
    assert (prior, saved) == ("", _VALID)
    # the occurrence's composed copy gone: the live text is not o1's any more, so there is no card to build
    (occ(scratch, "o1") / "composed.continuity.md").unlink()
    with pytest.raises(cd.CardError):
        cd.texts_for(scratch, occ(scratch, "o1"))


def test_a_missing_snapshot_copy_is_a_card_error_not_an_oserror(scratch):
    dream(scratch, "o1", _VALID)
    add_episode(scratch, "two")
    dream(scratch, "o2", text_n(2))
    m = sn.read_manifest(occ(scratch, "o2"))
    (occ(scratch, "o2") / sn.SNAP_DIR / m["files"]["continuity"]["name"]).unlink()
    with pytest.raises(cd.CardError):
        cd.texts_for(scratch, occ(scratch, "o2"))


def test_notes_the_model_wrote_in_the_wrong_shape_never_stop_the_card():
    """Reproduced: an integer where a list of episode ids belongs raised TypeError out of ``build_card``."""
    notes = {"contradictions": [{"what": "x", "episodes": 42}],
             "retired": [{"what": "R", "his_words": "four words of his own", "episodes": 7}],
             "for_phill": [{"what": "F", "episodes": {"a": 1}}]}
    c = cd.build_card("consolidated", store_dir=SD, occurrence="o", notes=notes, source={})
    assert c.authority_relevant and "[42]" in c.body["his"][0]
    c2 = cd.build_card("consolidated", store_dir=SD, occurrence="o", usage_doc={"composes": [{"model": "m",
                                                                             "models_charged": "abc"}]})
    assert c2.body["models"] == ["m"]                   # a string is not a list of model ids


def test_a_quote_is_not_found_inside_a_longer_word():
    """Reproduced: ``"no prompts at all"`` matched inside ``"piano prompts at all"``."""
    notes = {"retired": [{"what": "W", "his_words": "no prompts at all", "episodes": ["e"]}]}
    c = cd.build_card("consolidated", store_dir=SD, occurrence="a", notes=notes, source={"e": "a piano prompts at all today"})
    assert c.authority_relevant
    ok = cd.build_card("consolidated", store_dir=SD, occurrence="b", notes=notes, source={"e": "I said no prompts at all, today"})
    assert not ok.authority_relevant


def test_a_card_builds_into_an_out_dir_that_does_not_exist_yet(tmp_path):
    out = tmp_path / "a" / "b"
    c = cd.build_card("consolidated", store_dir=SD, occurrence="o", prior=big_text(0, "a"), saved=big_text(1, "b"), out_dir=out)
    assert (out / cd.DIFF_FILE).is_file() and c.artifacts["diff"] == str(out / cd.DIFF_FILE)


def test_concurrent_builders_of_one_card_do_not_share_a_temporary_file(tmp_path):
    """Reproduced: both writers used ``<name>.tmp``; one rename took the file out from under the other."""
    import threading

    errors: list[BaseException] = []
    texts = [("x" * 50_000) + str(i) for i in range(8)]

    def run(t):
        try:
            for _ in range(40):
                cd._write(tmp_path / cd.DIFF_FILE, t)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(t,)) for t in texts]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors[:1]
    assert (tmp_path / cd.DIFF_FILE).read_text() in texts
    assert [p.name for p in tmp_path.iterdir()] == [cd.DIFF_FILE]


def test_own_shown_is_set_whenever_the_own_list_was_cut_to_first_lines(tmp_path):
    """Reproduced: a body that fit after the first-line cut carried ``own_total`` and no ``own_shown``."""
    own = [f"entry {i}: " + "w" * 200 + "\nsecond line" for i in range(60)]
    c = cd.build_card("consolidated", store_dir=SD, occurrence="o", own_extra=own, out_dir=tmp_path)
    assert c.body["own_total"] == 60 and c.body["own_shown"] == len(c.body["own"]) == 60


def test_the_usage_record_carries_the_subscription_axes(scratch):
    """Reproduced: ``to_usage`` filled neither ``actions`` nor ``seconds``, so a subscription settle read every
    dream as unreadable. The executor now records the child's ``num_turns`` and the compose seconds."""
    import json as _json

    from levain.dream import executor as ex

    raw = _json.dumps({"subtype": "success", "is_error": False, "result": _VALID, "num_turns": 8,
                       "total_cost_usd": 1.5, "usage": {"input_tokens": 1, "output_tokens": 2}}).encode()
    assert ex.ClaudeMind("m").parse(raw, 0, 3.2).turns == 8
    assert ex.ClaudeMind("m").parse(raw.replace(b'"num_turns": 8', b'"x": 1'), 0, 3.2).turns is None
    dream(scratch, "o1", _VALID)
    doc = us.read_doc(occ(scratch, "o1"))
    assert isinstance(doc["seconds"], int) and "turns" in doc            # the fake mind reports no turn count
    assert us.read_usage(occ(scratch, "o1")).actions is None             # unreadable, never 0
    base = {"unit": "usd_list", "readable": True, "actual_usd_micros": 5, "input_tokens": 1, "output_tokens": 2,
            "source": "dream:claude-p"}
    u = us.to_usage({**base, "turns": 8, "seconds": 3181})
    assert (u.actions, u.seconds) == (8, 3181)
    assert us.to_usage({**base, "turns": None, "seconds": -1}).actions is None
    assert us.to_usage({**base}).seconds is None


def test_a_card_artifact_is_world_readable(tmp_path):
    import os
    import stat
    target = tmp_path / "occ" / "card_diff.txt"
    old = os.umask(0o022)
    try:
        cd._write(target, "x")
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o644
        os.umask(0o077)                                  # a private umask stays private
        cd._write(tmp_path / "occ" / "card_his.json", "x")
        assert stat.S_IMODE(os.stat(tmp_path / "occ" / "card_his.json").st_mode) == 0o600
    finally:
        os.umask(old)


def test_a_malformed_recorded_continuity_hash_is_a_card_error(scratch):
    dream(scratch, "o1", _VALID)
    m = sn.read_manifest(occ(scratch, "o1"))
    m.setdefault("post_files", {})["continuity"] = 7
    sn._write_manifest(occ(scratch, "o1"), m)
    with pytest.raises(cd.CardError):
        cd.texts_for(scratch, occ(scratch, "o1"))
