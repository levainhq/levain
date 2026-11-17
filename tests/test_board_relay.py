"""Relayed provenance (levain#97, P3): ``--source <np-id>`` on add, amend and decide, and the verifier that re-checks it.

Each behaviour was first run once against a scratch board store and a scratch band (a fake source for ``np-`` ids, which
the native source refuses; levain's own QuestionStore for the native one), then pinned here."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from levain.cli import main
from levain.consent.questions import QuestionStore
from levain.desk import board_relay as br
from levain.desk import board_store as bs

NP = "np-1b3dd088"
QUESTION = "Track flow under the registry? manifest sha256 " + "c" * 64
ANSWER = "yes, track it. His sentence travels, never the rule read into it."
EXCERPT = "His sentence travels"
MANIFEST = "c" * 64


class FakeBand:
    """A band source over a dict of np id -> BandRow; ``rows`` is edited by the test to change an answer later."""

    def __init__(self, **rows: br.BandRow) -> None:
        self.rows = dict(rows)

    def row(self, np_id: str) -> br.BandRow:
        if np_id not in self.rows:
            raise br.BandRefusal(f"{np_id} is not a row this band holds")
        return self.rows[np_id]


def signed_row(answer: str = ANSWER, grade: str = "signed-phone") -> br.BandRow:
    return br.BandRow(QUESTION, answer, grade, "flow")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lhome"))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    monkeypatch.delenv("LEVAIN_LAUNCH_KEY", raising=False)
    return tmp_path / "lhome"


@pytest.fixture
def store(home, tmp_path, capsys) -> Path:
    d = tmp_path / "store"
    assert main(["board", "use", str(d)]) == 0
    assert main(["board", "add", "--repo", "levain", "--title", "an item", "--origin", "desk 1", "--by", "desk 24"]) == 0
    capsys.readouterr()
    return Path(os.path.realpath(d))


@pytest.fixture
def band(monkeypatch) -> FakeBand:
    b = FakeBand(**{NP: signed_row(), "np-0000000a": br.BandRow("Is it signed?", "yes", "unsigned", "flow")})
    monkeypatch.setattr(br, "band_source", lambda directory=None: b)
    return b


def run(capsys, *argv):
    rc = main(["board", *argv])
    out, err = capsys.readouterr()
    return rc, out.strip(), err.strip()


def records(store: Path) -> list[dict]:
    return [json.loads(x) for x in (store / "items.jsonl").read_text("utf-8").splitlines()]


def decide(capsys, words: str, source: str = NP, *more: str):
    return run(capsys, "decide", "--id", "levain#1", "--decision", "cut", "--words", words, "--by", "desk 24",
               "--source", source, *more)


# ------------------------------------------------------------------------------------------------------------- writing


def test_a_relayed_decide_with_a_verbatim_excerpt_is_written_relayed(store, band, capsys):
    rc, out, err = decide(capsys, EXCERPT)
    assert rc == 0, err
    rec = records(store)[-1]
    assert rec["grade"] == "relayed" and rec["author"] == {"kind": "agent", "name": "desk 24"}
    assert rec["source"] == {"np": NP, "producer": "flow", "question_sha256": br.digest(QUESTION),
                             "answer_sha256": br.digest(ANSWER)}
    assert rec["words"] == EXCERPT
    assert br.verify_relayed(rec, band).text == f"relaying {NP}, a passkey-signed answer"


def test_a_paraphrase_is_refused_and_nothing_is_written(store, band, capsys):
    before = (store / "items.jsonl").read_bytes()
    rc, _, err = decide(capsys, "his sentences travel")
    assert rc == 2
    assert "not a verbatim excerpt of the answer" in err and "never a paraphrase" in err
    assert (store / "items.jsonl").read_bytes() == before


def test_an_unanswered_row_is_refused(store, monkeypatch, capsys):
    class Unanswered:
        def row(self, np_id):
            raise br.BandRefusal(f"{np_id} is unanswered")
    monkeypatch.setattr(br, "band_source", lambda directory=None: Unanswered())
    rc, _, err = decide(capsys, "x")
    assert rc == 2 and "is unanswered" in err


def test_an_unknown_np_id_is_refused(store, band, capsys):
    rc, _, err = decide(capsys, EXCERPT, "np-ffffffff")
    assert rc == 2 and "np-ffffffff is not a row this band holds" in err


def test_a_source_that_is_not_an_np_id_is_refused(store, band, capsys):
    rc, _, err = decide(capsys, EXCERPT, "q-abc")
    assert rc == 2 and "is not np-xxxxxxxx" in err


def test_source_with_an_operator_author_is_refused(store, band, capsys):
    rc, _, err = decide(capsys, EXCERPT, NP, "--author-kind", "operator")
    assert rc == 2 and "must be agent, not operator" in err
    assert len(records(store)) == 1


def test_the_writer_refuses_an_operator_relay_even_when_called_directly(store):
    target = bs.resolve_store(str(store))
    with pytest.raises(bs.BoardError, match="must be agent"):
        bs.decide(target, "levain#1", "cut", EXCERPT, "desk 24", author={"kind": "operator", "name": "desk 24"},
                  relay=(NP, FakeBand(**{NP: signed_row()})))


@pytest.mark.parametrize("key", ["source", "grade", "author"])
def test_a_caller_body_carrying_a_writer_key_is_refused(key):
    with pytest.raises(bs.BoardError, match="the writer sets those"):
        bs.build_record("decide", "levain#1", "desk 24", {"words": "x", key: {}}, author={"kind": "agent", "name": "desk 24"})


def test_add_and_amend_relay_too(store, band, capsys):
    rc, out, err = run(capsys, "add", "--repo", "levain", "--title", "second", "--origin", NP, "--words", EXCERPT,
                       "--by", "desk 24", "--source", NP)
    assert rc == 0, err
    rc, _, err = run(capsys, "amend", "--id", "levain#1", "--why", "his call", "--by", "desk 24", "--words", EXCERPT,
                     "--source", NP)
    assert rc == 0, err
    added, amended = records(store)[-2:]
    assert added["grade"] == amended["grade"] == "relayed"
    assert br.verify_relayed(added, band).ok and br.verify_relayed(amended, band).ok


def test_an_add_or_amend_with_no_words_to_relay_is_refused(store, band, capsys):
    rc, _, err = run(capsys, "add", "--repo", "levain", "--title", "second", "--origin", NP, "--by", "desk 24", "--source", NP)
    assert rc == 2 and "must carry the words it relays" in err
    rc, _, err = run(capsys, "amend", "--id", "levain#1", "--why", "w", "--by", "desk 24", "--title", "t", "--source", NP)
    assert rc == 2 and "relayed amend must carry --words" in err


def test_band_dir_without_source_is_refused(store, band, capsys):
    rc, _, err = run(capsys, "decide", "--id", "levain#1", "--decision", "cut", "--words", "w", "--by", "d", "--band-dir", "/x")
    assert rc == 2 and "--band-dir only means something with --source" in err


def test_an_unrelayed_write_is_still_asserted_with_no_source(store, capsys):
    rc, _, err = run(capsys, "decide", "--id", "levain#1", "--decision", "cut", "--words", "w", "--by", "desk 24")
    assert rc == 0, err
    rec = records(store)[-1]
    assert rec["grade"] == "asserted" and "source" not in rec


# ------------------------------------------------------------------------------------------------------------ verifying


def relayed_rec(band: FakeBand, words: str = EXCERPT, np: str = NP) -> dict:
    row = band.rows[np]
    return {"op": "decide", "words": words, "author": {"kind": "agent", "name": "desk 24"}, "grade": "relayed",
            "source": {"np": np, "producer": row.producer, "question_sha256": br.digest(row.question),
                       "answer_sha256": br.digest(row.answer)}}


def test_a_signed_answer_reports_signed_and_an_unsigned_one_reports_unsigned(band):
    assert str(br.verify_relayed(relayed_rec(band), band)) == f"relaying {NP}, a passkey-signed answer"
    v = br.verify_relayed(relayed_rec(band, "yes", "np-0000000a"), band)
    assert v.ok and v.text == "relaying np-0000000a, an unsigned answer" and v.grade == "unsigned"


def test_a_row_grade_is_never_promoted_by_the_record(band):
    rec = relayed_rec(band, "yes", "np-0000000a")
    rec["grade"] = "relayed"
    assert "unsigned" in br.verify_relayed(rec, band).text


def test_an_answer_changed_later_reports_the_digest_mismatch(band):
    rec = relayed_rec(band)
    band.rows[NP] = signed_row(ANSWER + " (edited)")
    v = br.verify_relayed(rec, band)
    assert not v.ok and v.text == "digest mismatch: the answer changed or the citation points elsewhere"


def test_a_citation_pointing_at_another_row_reports_the_digest_mismatch(band):
    rec = relayed_rec(band)
    rec["source"]["np"] = "np-0000000a"
    assert br.verify_relayed(rec, band).text.startswith("digest mismatch")


def test_an_unreadable_row_says_why(band):
    rec = relayed_rec(band)
    del band.rows[NP]
    v = br.verify_relayed(rec, band)
    assert not v.ok and v.text == f"{NP} unreadable: {NP} is not a row this band holds"


def test_words_that_are_not_in_the_answer_fail_even_when_the_digests_match(band):
    v = br.verify_relayed(relayed_rec(band, "he never said this"), band)
    assert not v.ok and "not a verbatim excerpt" in v.text


def test_a_record_that_is_not_relayed_or_is_malformed_does_not_verify(band):
    rec = relayed_rec(band)
    assert not br.verify_relayed({**rec, "grade": "asserted"}, band).ok
    assert not br.verify_relayed({**rec, "author": {"kind": "operator", "name": "x"}}, band).ok
    assert not br.verify_relayed({**rec, "source": {"np": NP}}, band).ok


def test_a_source_that_raises_or_returns_junk_is_an_unreadable_row(band):
    class Boom:
        def row(self, np_id):
            raise RuntimeError("secret detail")
    v = br.verify_relayed(relayed_rec(band), Boom())
    assert not v.ok and "unreadable" in v.text and "secret detail" not in v.text
    junk = FakeBand(**{NP: br.BandRow(QUESTION, ANSWER, "gold-plated", "flow")})
    assert "grade this reader does not know" in br.verify_relayed(relayed_rec(band), junk).text


# ------------------------------------------------------------------------------------------------------------- P3-b


def test_check_manifest_binds_the_question_to_the_manifest_digest(band):
    rec = {**relayed_rec(band), "op": "begin-track", "manifest_sha256": MANIFEST}
    ok = br.check_manifest(rec, band.rows[NP])
    assert ok.ok and ok.grade == "signed-phone"
    other = br.check_manifest(rec, band.rows[NP], "d" * 64)
    assert not other.ok and "does not carry the manifest's sha256" in other.text


def test_check_manifest_for_a_track_takes_the_begin_tracks_digest(band):
    rec = {**relayed_rec(band), "op": "track"}                      # a track carries no manifest digest of its own
    assert not br.check_manifest(rec, band.rows[NP]).ok
    assert br.check_manifest(rec, band.rows[NP], MANIFEST).ok


def test_check_manifest_does_not_match_a_digest_inside_a_longer_hex_run():
    row = br.BandRow("sha " + "c" * 64 + "0", "yes", "unsigned", "flow")
    assert not br.check_manifest({}, row, MANIFEST).ok


def test_check_manifest_refuses_a_question_the_record_does_not_cite(band):
    rec = {**relayed_rec(band), "manifest_sha256": MANIFEST}
    rec["source"]["question_sha256"] = "e" * 64
    assert "different question" in br.check_manifest(rec, band.rows[NP]).text


# --------------------------------------------------------------------------------------------------------- native band


def test_the_native_source_reads_a_q_id_and_refuses_np_unanswered_and_unknown(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    qs = QuestionStore(d)
    q = qs.file(agent="claude_code", agent_session_id="s", what="Ship the thing?", options=(), rec="", blocks="b",
                cwd="/tmp", open_ended=True)
    native = br.NativeBandSource(qs)
    with pytest.raises(br.BandRefusal, match="unanswered"):
        native.row(q.id)
    qs.answer(q.id, text="yes, ship it now", channel="cli")
    row = native.row(q.id)
    assert row == br.BandRow("Ship the thing?", "yes, ship it now", "unsigned", "levain")
    with pytest.raises(br.BandRefusal, match="not a row this band holds"):
        native.row(NP)                                  # flow's np ids are not levain's to read
    with pytest.raises(br.BandRefusal, match="not a row this band holds"):
        native.row("q-doesnotexist")
