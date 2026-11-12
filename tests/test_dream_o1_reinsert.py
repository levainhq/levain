"""O1 (board row 1695): a saved wrap that anneal re-inserted lines into still leaves a post record, a card and a
rollback.

The shape is world A of the 1112-1 Q1 re-run: the prior wrap carries a ``## Durable Facts`` line, the dream's compose
leaves it out, and anneal's save puts it back verbatim (``validated_save_continuity`` runs the durable-facts invariant
before it hashes or writes). Every test runs the REAL ``consolidate_store`` with the snapshot hooks on a scratch
FLOW_SCHEMA store under ``tmp_path``; the mind is the canned in-process ``Fake``.
"""
from __future__ import annotations

import json
import warnings

import pytest
from anneal_memory import Store

from levain.desk.store import CoordStore
from levain.dream import agent as da
from levain.dream import snapshot as sn
from tests.test_agents_supervise_fire import Ex, _fire, _supervisor, rig  # noqa: F401
from tests.test_dream_agent import FEED, filed
from tests.test_dream_snapshot import Fake, add_episode, dream, occ, scratch, text_n  # noqa: F401
from levain.dream import executor as ex

PRIOR_FACT = "- The store lives on the laptop, never on argushub (prior)."


def with_durable(text: str, *facts: str) -> str:
    """``text`` with a ``## Durable Facts`` section (FLOW_SCHEMA's position: after Active Threads)."""
    block = "## Durable Facts\n" + "".join(f + "\n" for f in facts) + "\n"
    return text.replace("## Patterns", block + "## Patterns")


def prior_wrap(db) -> None:
    """Dream o1 saves a text whose durable section carries PRIOR_FACT; then a new episode for the next dream."""
    r = dream(db, "o1", with_durable(text_n(1), PRIOR_FACT))
    assert r.outcome == "saved" and r.hook_errors == [], r
    add_episode(db, "two")


def live(db) -> str:
    return (db.parent / "memory.continuity.md").read_text(encoding="utf-8")


def test_o1_a_compose_that_leaves_out_a_durable_line_still_records_the_post_and_names_the_line(scratch):
    prior_wrap(scratch)
    composed = with_durable(text_n(2), "- A new fact from the second night.")   # PRIOR_FACT left out
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        r = dream(scratch, "o2", composed)
    assert r.outcome == "saved", r
    assert any("re-inserted verbatim" in str(w.message) for w in caught)       # anneal did put the line back
    assert PRIOR_FACT in live(scratch) and PRIOR_FACT not in composed
    assert r.hook_errors == [], r.hook_errors
    m = sn.read_manifest(occ(scratch, "o2"))
    assert m["post_files"] == sn.tier_hashes(scratch) and m["wrap_id"] == r.wrap_id
    assert m["reinserted"] == [PRIOR_FACT]


def test_o1_the_rollback_of_a_re_inserted_save_restores_the_earlier_bytes(scratch):
    prior_wrap(scratch)
    before = sn.sha256_file(scratch.parent / "memory.continuity.md")
    dream(scratch, "o2", with_durable(text_n(2), "- A new fact."))
    assert sn.sha256_file(scratch.parent / "memory.continuity.md") != before
    res = sn.rollback(scratch, occ(scratch, "o2"), who="test")
    assert res.outcome == "rolled-back", res
    assert sn.sha256_file(scratch.parent / "memory.continuity.md") == before


def test_o1_a_compose_that_drops_the_whole_durable_section_is_accepted_the_same_way(scratch):
    prior_wrap(scratch)
    r = dream(scratch, "o2", text_n(2))                           # no ## Durable Facts at all: anneal recreates it
    assert r.outcome == "saved" and r.hook_errors == [], r.hook_errors
    assert sn.read_manifest(occ(scratch, "o2"))["reinserted"] == [PRIOR_FACT]


def test_o1_an_exact_save_records_no_reinserted_key(scratch):
    prior_wrap(scratch)
    r = dream(scratch, "o2", with_durable(text_n(2), PRIOR_FACT))
    assert r.hook_errors == []
    assert "reinserted" not in sn.read_manifest(occ(scratch, "o2"))


# ------------------------------------------------------------------- the refusals the re-insertion path must keep

def _saved_with_reinsertion_then_unrecorded(db):
    """Dream o2 saves with a re-insertion; its post record is then cleared so record_post_for can be asked again."""
    prior_wrap(db)
    dream(db, "o2", with_durable(text_n(2), "- A new fact."))
    o = occ(db, "o2")
    m = sn.read_manifest(o)
    m["post_files"], m["wrap_id"] = None, None
    m.pop("reinserted", None)
    sn._write_manifest(o, m)
    prog = json.loads((o / "progress.json").read_text())
    return o, prog


def test_o1_control_the_cleared_record_is_recorded_again_with_the_right_arguments(scratch):
    """The positive control for the refusals below: the same state, the right wrap id, records."""
    o, prog = _saved_with_reinsertion_then_unrecorded(scratch)
    post = sn.record_post_for(scratch, o, prog["wrap_id"], composed_sha256=prog["composed_sha256"])
    assert post == sn.tier_hashes(scratch) and sn.read_manifest(o)["reinserted"] == [PRIOR_FACT]


def test_o1_without_the_journals_watermark_nothing_is_proved(scratch):
    """The dream's wrap is the store's FIRST (the prior durable text was written to the file, not by a wrap), so with
    no watermark ``wraps_after`` from the start would still find exactly that one row: only the watermark check
    refuses here."""
    (scratch.parent / "memory.continuity.md").write_text(with_durable(text_n(1), PRIOR_FACT), encoding="utf-8")
    r = dream(scratch, "o2", with_durable(text_n(2), "- A new fact."))
    assert r.outcome == "saved" and r.hook_errors == [], r
    o = occ(scratch, "o2")
    assert sn.read_manifest(o)["reinserted"] == [PRIOR_FACT]
    m = sn.read_manifest(o)
    m["post_files"], m["wrap_id"] = None, None
    m.pop("reinserted", None)
    sn._write_manifest(o, m)
    prog = json.loads((o / "progress.json").read_text())
    assert sn.wraps_after(scratch, None) == sn.wraps_after(scratch, prog["last_wrap_before"])   # one row either way
    doc = dict(prog)
    doc.pop("last_wrap_before")
    (o / "progress.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(sn.SnapshotRefused, match="no pre-save wrap watermark"):
        sn.record_post_for(scratch, o, prog["wrap_id"], composed_sha256=prog["composed_sha256"])


def test_o1_a_later_write_on_top_of_a_re_inserted_save_is_still_refused(scratch):
    o, prog = _saved_with_reinsertion_then_unrecorded(scratch)
    p = scratch.parent / "memory.continuity.md"
    p.write_text(p.read_text() + "\n- a seat's later line\n", encoding="utf-8")
    with pytest.raises(sn.SnapshotRefused, match="not the text this dream composed"):
        sn.record_post_for(scratch, o, prog["wrap_id"], composed_sha256=prog["composed_sha256"])


def test_o1_a_different_wrap_id_or_none_is_refused(scratch):
    o, prog = _saved_with_reinsertion_then_unrecorded(scratch)
    for wid in (None, prog["wrap_id"] - 1, prog["wrap_id"] + 1):
        with pytest.raises(sn.SnapshotRefused, match="not the text this dream composed"):
            sn.record_post_for(scratch, o, wid, composed_sha256=prog["composed_sha256"])


def test_o1_a_later_wrap_after_the_dreams_is_refused(scratch):
    o, prog = _saved_with_reinsertion_then_unrecorded(scratch)
    add_episode(scratch, "three")
    assert dream(scratch, "o3", with_durable(text_n(3), PRIOR_FACT, "- A new fact.")).outcome == "saved"
    with pytest.raises(sn.SnapshotRefused, match="not the text this dream composed"):
        sn.record_post_for(scratch, o, prog["wrap_id"], composed_sha256=prog["composed_sha256"])


def test_o1_a_wrap_followed_by_one_that_saved_the_same_text_is_refused(scratch):
    """A second wrap saved the dream's live bytes again, so the first row past the watermark is still the dream's id
    with the live hash: only the one-row rule refuses."""
    o, prog = _saved_with_reinsertion_then_unrecorded(scratch)
    add_episode(scratch, "three")
    assert dream(scratch, "o3", live(scratch)).outcome == "saved"
    rows = sn.wraps_after(scratch, prog["last_wrap_before"])
    assert len(rows) == 2 and rows[0][0] == prog["wrap_id"] and rows[0][1] == sn.tier_hashes(scratch)["continuity"]
    with pytest.raises(sn.SnapshotRefused, match="2 completed wraps follow"):
        sn.record_post_for(scratch, o, prog["wrap_id"], composed_sha256=prog["composed_sha256"])


def test_o1_a_compose_other_than_the_one_this_wrap_saved_is_refused_even_when_the_journal_agrees(scratch):
    """The live bytes match the wrap row (so a raw write is excluded) and the composed file hashes to the composed
    hash given, but that compose is not the one the wrap saved: anneal's step does not reproduce the live text."""
    import hashlib

    o, prog = _saved_with_reinsertion_then_unrecorded(scratch)
    other = with_durable(text_n(2), "- Some other fact.")
    (o / "composed.continuity.md").write_text(other, encoding="utf-8")
    other_sha = hashlib.sha256(other.encode("utf-8")).hexdigest()           # the journal agrees with the file
    with pytest.raises(sn.SnapshotRefused, match="does not reproduce the live text"):
        sn.record_post_for(scratch, o, prog["wrap_id"], composed_sha256=other_sha)


def test_o1_a_composed_file_that_does_not_hash_to_the_journal_is_refused(scratch):
    o, prog = _saved_with_reinsertion_then_unrecorded(scratch)
    (o / "composed.continuity.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(sn.SnapshotRefused, match="not the text this dream composed"):
        sn.record_post_for(scratch, o, prog["wrap_id"], composed_sha256=prog["composed_sha256"])


def test_o1_a_proof_that_cannot_run_says_so_and_does_not_say_the_save_did_not_land(scratch, monkeypatch):
    o, prog = _saved_with_reinsertion_then_unrecorded(scratch)

    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("anneal_memory.durable.enforce_durable_facts", broken)
    with pytest.raises(sn.SnapshotRefused, match=r"failed to run \(RuntimeError: boom\)") as ei:
        sn.record_post_for(scratch, o, prog["wrap_id"], composed_sha256=prog["composed_sha256"])
    assert "did not land" not in str(ei.value)


# ------------------------------------------------------------- drop markers, CRLF and canonical text: real saves too

def drop(fact: str) -> str:
    return f"[drop-durable: {fact[2:]}]"


def test_o1_a_drop_marker_save_is_recorded_and_the_manifest_names_the_dropped_line(scratch):
    prior_wrap(scratch)
    composed = with_durable(text_n(2), "- A new fact.", drop(PRIOR_FACT))
    r = dream(scratch, "o2", composed)
    assert r.outcome == "saved", r
    assert PRIOR_FACT not in live(scratch) and "drop-durable" not in live(scratch)   # anneal removed both
    assert r.hook_errors == [], r.hook_errors
    m = sn.read_manifest(occ(scratch, "o2"))
    assert m["post_files"] == sn.tier_hashes(scratch) and m["dropped"] == [PRIOR_FACT] and "reinserted" not in m


def test_o1_the_rollback_of_a_drop_marker_save_restores_the_earlier_bytes(scratch):
    prior_wrap(scratch)
    before = sn.sha256_file(scratch.parent / "memory.continuity.md")
    assert dream(scratch, "o2", with_durable(text_n(2), "- A new fact.", drop(PRIOR_FACT))).hook_errors == []
    res = sn.rollback(scratch, occ(scratch, "o2"), who="test")
    assert res.outcome == "rolled-back", res
    assert sn.sha256_file(scratch.parent / "memory.continuity.md") == before


def test_o1_a_drop_marker_save_whose_live_text_was_then_touched_is_refused(scratch):
    prior_wrap(scratch)
    dream(scratch, "o2", with_durable(text_n(2), "- A new fact.", drop(PRIOR_FACT)))
    o = occ(scratch, "o2")
    m = sn.read_manifest(o)
    m["post_files"], m["wrap_id"] = None, None
    m.pop("dropped", None)
    sn._write_manifest(o, m)
    prog = json.loads((o / "progress.json").read_text())
    p = scratch.parent / "memory.continuity.md"
    p.write_text(p.read_text() + "\n- a seat's later line\n", encoding="utf-8")
    with pytest.raises(sn.SnapshotRefused, match="does not hash to the content hash"):
        sn.record_post_for(scratch, o, prog["wrap_id"], composed_sha256=prog["composed_sha256"])


def test_o1_a_crlf_compose_that_leaves_out_a_durable_line_is_recorded(scratch):
    prior_wrap(scratch)
    composed = with_durable(text_n(2), "- A new fact.").replace("\n", "\r\n")
    r = dream(scratch, "o2", composed)
    assert r.outcome == "saved" and r.hook_errors == [], r.hook_errors
    assert "\r" not in live(scratch)
    assert sn.read_manifest(occ(scratch, "o2"))["reinserted"] == [PRIOR_FACT]


def test_o1_the_proof_canonicalises_the_compose_as_the_save_does(scratch):
    """A drop marker written after a no-break-space bullet is a marker only once the text is canonical: the save reads
    it as one and drops the line, so the proof must too."""
    prior_wrap(scratch)
    marker = "- " + drop(PRIOR_FACT)
    r = dream(scratch, "o2", with_durable(text_n(2), "- A new fact.", marker))
    assert r.outcome == "saved" and r.hook_errors == [], r.hook_errors
    assert PRIOR_FACT not in live(scratch)
    assert sn.read_manifest(occ(scratch, "o2"))["dropped"] == [PRIOR_FACT]


# ------------------------------------------------------------------------------------------- the card's sentence

def _sentence(monkeypatch, manifest):
    monkeypatch.setattr(da._snap, "read_manifest", lambda occ: manifest)
    return da._durable_sentence(None)


def test_o1_the_card_sentence_strips_the_bullet_and_cuts_a_long_line(monkeypatch):
    long = "- " + "x" * 300
    s = _sentence(monkeypatch, {"reinserted": [long], "dropped": ["* gone fact"]})
    assert s.startswith("The save put back 1 line the compose left out: xxx")
    shown = s.split(": ", 1)[1].split(".", 1)[0]
    assert len(shown) == da.DURABLE_LINE_CHARS and shown.endswith("…")
    assert "- x" not in s
    assert s.endswith("The save removed 1 line through drop markers: gone fact.")


def test_o1_the_card_sentence_shows_three_lines_and_counts_the_rest(monkeypatch):
    lines = [f"- fact {i}" for i in range(7)]
    s = _sentence(monkeypatch, {"reinserted": lines})
    assert s == "The save put back 7 lines the compose left out: fact 0; fact 1; fact 2; and 4 more."
    assert _sentence(monkeypatch, {"reinserted": []}) == ""


# --------------------------------------------------------------------------- the fire body: done, card names the line

def test_o1_the_fire_body_ends_done_and_the_card_says_the_save_put_the_line_back(rig, scratch, tmp_path):
    prior_wrap(scratch)
    occ_dir = scratch.parent / "dream" / "oc-o1"
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    review_store = CoordStore(q)
    sup, journal = _supervisor(rig)
    oid = _fire(rig, "dream-run", journal)

    def consolidate():
        on_snap, on_saved = sn.hooks()
        return ex.consolidate_store(scratch, mind=Fake(with_durable(text_n(2), "- A new fact.")),
                                    occurrence_dir=occ_dir, max_seconds=3000, on_snapshot=on_snap, on_saved=on_saved)

    rep = da.run_body(sup, oid, 0, store_path=scratch, occ_dir=occ_dir, executor=Ex(), consolidate=consolidate,
                      review_store=review_store, day="2026-11-12", feed=FEED)
    assert (rep.outcome, rep.card, rep.state, rep.code) == ("saved", "filed", "done", 0), rep
    assert rig.row(oid)["state"] == "done"
    body = filed(review_store)[0].body
    assert body["kind"] == "consolidated"
    assert "The save put back 1 line the compose left out" in body["reason"]
    assert PRIOR_FACT[2:] in body["reason"] and PRIOR_FACT not in body["reason"]       # the bullet is stripped
