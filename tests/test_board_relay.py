"""Relayed provenance (levain#97, P3): ``--source <q-id>`` on add, amend and decide, and the verifier that re-checks it.

Each behaviour was first run once against a scratch board store and a scratch band (a fake source keyed by q- ids; levain's own
QuestionStore for the native one), then pinned here."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from levain.cli import main
from levain.consent.questions import QuestionStore
from levain.desk import board_relay as br
from levain.desk import board_store as bs

NP = "q-" + "1b3dd088" * 3
NP2 = "q-" + "0000000a" * 3
NP_FLOW = "np-1b3dd088"
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
    b = FakeBand(**{NP: signed_row(), NP2: br.BandRow("Is it signed?", "yes", "unsigned", "flow")})
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
    rc, _, err = decide(capsys, EXCERPT, "q-" + "f" * 24)
    assert rc == 2 and f"{'q-' + 'f' * 24} is not a row this band holds" in err


def test_a_source_that_is_not_an_np_id_is_refused(store, band, capsys):
    rc, _, err = decide(capsys, EXCERPT, "q-abc")
    assert rc == 2 and "is not a native q-<24 hex> question id" in err
    rc, _, err = decide(capsys, EXCERPT, "3")                   # a band number is a view-relative handle
    assert rc == 2 and "is not a native q-<24 hex> question id" in err


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
    v = br.verify_relayed(relayed_rec(band, "yes", NP2), band)
    assert v.ok and v.text == f"relaying {NP2}, an unsigned answer" and v.grade == "unsigned"


def test_a_row_grade_is_never_promoted_by_the_record(band):
    rec = relayed_rec(band, "yes", NP2)
    rec["grade"] = "relayed"
    assert "unsigned" in br.verify_relayed(rec, band).text


def test_an_answer_changed_later_reports_the_digest_mismatch(band):
    rec = relayed_rec(band)
    band.rows[NP] = signed_row(ANSWER + " (edited)")
    v = br.verify_relayed(rec, band)
    assert not v.ok and v.text == "digest mismatch: the answer changed or the citation points elsewhere"


def test_a_citation_pointing_at_another_row_reports_the_digest_mismatch(band):
    rec = relayed_rec(band)
    rec["source"]["np"] = NP2
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
    assert row == br.BandRow("Ship the thing?", "yes, ship it now", "unknown", "native")
    with pytest.raises(br.BandRefusal, match="not a row this band holds"):
        native.row(NP)                                  # flow's np ids are not levain's to read
    with pytest.raises(br.BandRefusal, match="not a row this band holds"):
        native.row("q-doesnotexist")


# ------------------------------------------------------------------------------- native band: a signed answer is citable


def _native(tmp_path, text="Track flow?", answer="yes, track it. His sentence travels."):
    """A scratch consent dir with one open question answered through the band's own signed-answer insert; returns
    ``(dir, store, question)``. ``sign`` is applied by the caller via :func:`_answer`."""
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    qs = QuestionStore(d)
    q = qs.file(agent="claude_code", agent_session_id="s", what=text, options=(), rec="", blocks="b", cwd="/tmp",
                open_ended=True)
    return d, qs, q

def _answer(qs, q, sign, channel="cli", text="yes, track it. His sentence travels."):
    from levain import band_view as bv
    (res,) = bv._answer_signed(qs, [{"ref": q.id, "text": text, "via": channel, "viewer": "t", "sign": sign}],
                               channel=channel, deadline=None)
    assert not isinstance(res, Exception), res


def _signed(qs, q):
    from levain import band_view as bv
    return bv.Signing("signed", message="levain-desk-answer/1\nstore: x\n", sig="c2ln", fp="SHA256:f",
                      versions={q.id: bv.subject_version(qs.get(q.id))})


def _relay(capsys, store, d, q):
    return run(capsys, "decide", "--id", "levain#1", "--decision", "cut", "--words", "His sentence travels",
               "--by", "desk 24", "--source", q.id, "--band-dir", str(d))


def test_a_signed_native_answer_is_cited_by_its_q_id_and_reported_signed(store, tmp_path, capsys):
    d, qs, q = _native(tmp_path)
    _answer(qs, q, _signed(qs, q), channel="phone")
    rc, out, err = _relay(capsys, store, d, q)
    assert rc == 0, err
    rec = records(store)[-1]
    assert rec["grade"] == "relayed" and rec["source"]["np"] == q.id and rec["source"]["producer"] == "native"
    v = br.verify_relayed(rec, br.NativeBandSource(qs))
    assert v.ok and v.grade == "signed-phone"
    assert v.text == (f"relaying {q.id}, a passkey-signed answer (per the band store's sign_state; the signature "
                      f"itself is re-verified by the trail verifier)")


def test_a_signed_native_answer_at_a_terminal_reads_signed_terminal(tmp_path):
    d, qs, q = _native(tmp_path)
    _answer(qs, q, _signed(qs, q), channel="cli")
    assert br.NativeBandSource(qs).row(q.id).answer_grade == "signed-terminal"


def test_an_unsigned_native_answer_is_reported_unsigned(store, tmp_path, capsys):
    from levain import band_view as bv
    d, qs, q = _native(tmp_path)
    _answer(qs, q, bv.Signing("unsigned", reason="remote"))
    rc, _, err = _relay(capsys, store, d, q)
    assert rc == 0, err
    v = br.verify_relayed(records(store)[-1], br.NativeBandSource(qs))
    assert v.ok and v.grade == "unsigned-terminal" and v.text == f"relaying {q.id}, an unsigned answer (terminal)"


def test_an_unmapped_sign_state_or_no_sidecar_reads_unknown_never_signed(tmp_path):
    from levain import band_view as bv
    d, qs, q = _native(tmp_path)
    _answer(qs, q, bv.Signing("unsigned", reason="remote"))
    with _scratch_db(d) as conn:
        conn.execute("UPDATE answer_context SET sign_state = 'countersigned-ish' WHERE question_id = ?", (q.id,))
    row = br.NativeBandSource(qs).row(q.id)
    assert row.answer_grade == "unknown" and br.ROW_GRADES["unknown"] in br.verify_relayed(
        {"grade": "relayed", "source": br.relay_source(q.id, "His sentence", br.NativeBandSource(qs)),
         "author": {"kind": "agent"}, "op": "decide", "words": "His sentence"}, br.NativeBandSource(qs)).text


def test_a_native_answer_changed_after_the_citation_reports_the_digest_mismatch(store, tmp_path, capsys):
    d, qs, q = _native(tmp_path)
    _answer(qs, q, _signed(qs, q), channel="phone")
    rc, _, err = _relay(capsys, store, d, q)
    assert rc == 0, err
    rec = records(store)[-1]
    with _scratch_db(d) as conn:
        conn.execute("UPDATE answers SET value = 'no, His sentence travels' WHERE question_id = ?", (q.id,))
    v = br.verify_relayed(rec, br.NativeBandSource(qs))
    assert not v.ok and v.text.startswith("digest mismatch") and v.grade == "signed-phone"


def _scratch_db(d):
    """The SCRATCH store's db with its append-only guards dropped, so a test can stage a state the writers never produce."""
    import sqlite3
    conn = sqlite3.connect(d / "questions.db")
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE '%\\_frozen' ESCAPE '\\'").fetchall():
        conn.execute(f"DROP TRIGGER {name}")
    return conn


# ------------------------------------------------------------------------------------------------------- L3 round 1 (R1-R12)


def test_r1_an_np_id_is_refused_up_front_with_the_named_reason_and_opens_no_band(store, monkeypatch, capsys):
    def boom(directory=None):
        raise AssertionError("the band must not be opened for an np- id")
    monkeypatch.setattr(br, "band_source", boom)
    rc, _, err = decide(capsys, EXCERPT, NP_FLOW)
    assert rc == 2 and "np- rows are flow's band: their reader arrives with T12; cite a q- row" in err
    assert len(records(store)) == 1


def test_r2_check_manifest_fails_when_the_source_is_missing_or_malformed(band):
    row = band.rows[NP]
    for src in (None, "x", {}, {"question_sha256": 7}, {"question_sha256": br.digest(row.question).upper()}):
        rec = {"op": "begin-track", "manifest_sha256": MANIFEST}
        if src is not None:
            rec["source"] = src
        v = br.check_manifest(rec, row)
        assert not v.ok and "lowercase sha256" in v.text, src
    good = {"manifest_sha256": MANIFEST, "source": {"question_sha256": br.digest(row.question)}}
    assert br.check_manifest(good, row).ok


def test_r3_an_amend_without_a_fields_mapping_holding_words_fails_even_with_top_level_words(band):
    rec = {**relayed_rec(band, "yes"), "op": "amend"}
    for fields in (None, "junk", {}, {"words": 3}):
        v = br.verify_relayed({**rec, "fields": fields}, band)
        assert not v.ok and v.text.startswith("malformed amend"), fields
    assert br.verify_relayed({**rec, "fields": {"words": "yes"}}, band).ok


def test_r4_the_manifest_digest_boundary_is_case_insensitive():
    for q in ("x " + "C" * 64 + "F", "x F" + "c" * 64, "x " + "c" * 64 + "A"):
        rec = {"manifest_sha256": MANIFEST, "source": {"question_sha256": br.digest(q)}}
        assert not br.check_manifest(rec, br.BandRow(q, "yes", "unsigned", "flow")).ok, q
    q = "x " + "C" * 64 + " y"
    rec = {"manifest_sha256": MANIFEST, "source": {"question_sha256": br.digest(q)}}
    assert br.check_manifest(rec, br.BandRow(q, "yes", "unsigned", "flow")).ok


def test_r5_a_non_mapping_record_is_a_failed_verdict(band):
    for junk in ([1], "x", None, 3):
        v = br.verify_relayed(junk, band)
        assert not v.ok and "not a relayed record" in v.text


def _add(capsys, *more):
    return run(capsys, "add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "desk 24",
               "--words", EXCERPT, "--key", "k1", "--payload-hash", "a" * 64, *more)


def test_r6_a_key_hit_under_source_must_be_a_relayed_record_citing_the_same_id(store, band, capsys):
    rc, out, err = _add(capsys)                                 # unrelayed first
    assert rc == 0, err
    n = len(records(store))
    rc, _, err = _add(capsys, "--source", NP)
    assert rc == 2 and "not a relayed record citing" in err and len(records(store)) == n
    rc, out2, err = run(capsys, "add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "desk 24",
                        "--words", EXCERPT, "--key", "k2", "--payload-hash", "b" * 64, "--source", NP)
    assert rc == 0, err
    again = ["add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "desk 24", "--words", EXCERPT,
             "--key", "k2", "--payload-hash", "b" * 64]
    assert run(capsys, *again, "--source", NP)[:2] == (0, out2)    # the same relay: the existing record
    rc, _, err = run(capsys, *again, "--source", NP2)
    assert rc == 2 and "not a relayed record citing" in err
    assert run(capsys, *again)[:2] == (0, out2)                    # no --source: a relayed record is still the record


def test_r7_a_band_that_cannot_be_built_or_read_is_a_refusal_never_a_traceback(store, monkeypatch, capsys, tmp_path):
    original = br.band_source

    def boom(directory=None):
        raise OSError("disk on fire")
    monkeypatch.setattr(br, "band_source", boom)
    rc, _, err = decide(capsys, EXCERPT)
    assert rc == 2 and "the band source failed (OSError)" in err
    monkeypatch.setattr(br, "band_source", original)
    f = tmp_path / "afile"
    f.write_text("junk")
    rc, _, err = decide(capsys, EXCERPT, NP, "--band-dir", str(f))
    assert rc == 2 and "could not be read" in err


def test_r7_a_store_on_no_directory_is_read_without_creating_one(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lhome"))
    for src in (br.band_source(None), br.band_source(tmp_path / "nodir")):
        with pytest.raises(br.BandRefusal, match="not a row this band holds"):
            src.row(NP)
    assert not (tmp_path / "nodir").exists() and not (tmp_path / "lhome").exists()
    assert not list(tmp_path.rglob("questions.db"))


def test_r8_a_changed_producer_says_so(band):
    rec = relayed_rec(band)
    rec["source"]["producer"] = "native"
    v = br.verify_relayed(rec, band)
    assert not v.ok and v.text.startswith("producer mismatch")


MULTI_Q = "Track it?"
MULTI_A = "yes, track it.\nHis sentence travels.\nnever the rule"


def test_r9_a_multi_line_excerpt_is_refused_the_same_on_every_verb_and_one_line_of_it_passes(store, band, capsys):
    band.rows[NP] = br.BandRow(MULTI_Q, MULTI_A, "unsigned", "flow")
    span = "track it.\nHis sentence"
    n = len(records(store))
    verbs = {
        "decide": lambda w: run(capsys, "decide", "--id", "levain#1", "--decision", "cut", "--words", w, "--by", "d", "--source", NP),
        "add": lambda w: run(capsys, "add", "--repo", "levain", "--title", "t", "--origin", "o", "--by", "d", "--words", w, "--source", NP),
        "amend": lambda w: run(capsys, "amend", "--id", "levain#1", "--why", "w", "--by", "d", "--words", w, "--source", NP),
    }
    for name, call in verbs.items():
        rc, _, err = call(span)
        assert rc == 2 and "one line" in err, name
    assert len(records(store)) == n
    for name, call in verbs.items():
        rc, _, err = call("His sentence travels")
        assert rc == 0, (name, err)
        assert br.verify_relayed(records(store)[-1], band).ok, name


def test_r10_r11_r12_the_constants_and_the_id_width_and_a_missing_words_field(tmp_path):
    from levain.desk import board_store as bstore
    assert bstore.GRADE_RELAYED == "relayed" and br.GRADE_RELAYED is bstore.GRADE_RELAYED
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    qs = QuestionStore(d)
    for i in range(5):
        q = qs.file(agent="claude_code", agent_session_id=f"s{i}", what=f"q{i}?", options=(), rec="", blocks="b", cwd="/tmp",
                    open_ended=True)
        assert br.Q_RE.fullmatch(q.id), q.id
    with pytest.raises(bstore.BoardError, match="must carry the words"):
        br.relay_source(NP, None, FakeBand())
