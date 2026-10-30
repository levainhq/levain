"""The ping's order under the long caps (L3 r1 MED) and the OPEN question: filed with no options and no rec, answered
only in words. Built from the reproduced refusals: `levain ask` with no option said `options must be a list of 2 to 6
options`, and `--rec` was required by the parser."""
from __future__ import annotations

import json

import pytest

from levain import band_view
from levain.cockpit.consent import ConsentError
from levain.consent import band
from levain.consent.adapters import claude_code as cc
from levain.consent.questions import QuestionStore
from levain.coordinator import review
from tests.test_questions_band import SESSION, _file, _run

OPEN = dict(what="how should the importer treat a missing column?", options=[], rec="", blocks="the importer plan")


def _open(store, session=SESSION, **kw):
    args = {**OPEN, **kw}
    return store.file(agent="claude_code", agent_session_id=session, cwd="/work/proj", open_ended=True, **args)


# --- item 0: the ping's order and budget ---------------------------------------------------------------------
def test_a_4933_char_block_cannot_cut_the_options_or_the_answer_line(tmp_path):
    opts = [(f"k{i}", f"option {i} " + "w" * 500) for i in range(6)]
    store = QuestionStore(tmp_path / "consent")
    q = _file(store, options=opts, rec="k2", blocks="b" * 4933, what="w" * 900)
    text = band._ping_text("claude_code", q, tmp_path / "consent", 3)
    pushed = text[:4000]                                  # what webpush/sender.spawn keeps
    for k, label in opts:
        assert f"{k}) {label}" in pushed or f"{k}) option" in pushed
    assert "Answer: levain question answer 1" in pushed and pushed.rstrip().endswith("(full text on the card)")
    assert len(text) <= 4000 - 120                         # room left for a held ping's extra line
    assert text.index("Options:") < text.index("Answer:") < text.index("Blocks:")


def test_a_short_block_rides_whole_and_last(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    text = band._ping_text("claude_code", _file(store), tmp_path / "consent", 1)
    assert text.splitlines()[1].startswith("Options: a) merge now (rec); b) wait for the run")
    assert text.endswith("Blocks: the merge.") and "full text on the card" not in text


def test_the_store_keeps_the_whole_block_when_the_ping_cuts_it(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    q = _file(store, blocks="b" * 4933)
    assert len(band._ping_text("claude_code", q, tmp_path / "consent")) < 4000
    assert store.get(q.id).blocks == "b" * 4933


def test_six_longest_labels_still_keep_every_key_and_the_answer(tmp_path):
    opts = [(f"k{i}", "L" * 600) for i in range(6)]
    store = QuestionStore(tmp_path / "consent")
    q = _file(store, options=opts, rec="k0", what="w" * 1000, blocks="x")
    text = band._ping_text("claude_code", q, tmp_path / "consent", 1)
    assert len(text) <= 3800 + len("\nBlocks: x.")
    assert all(f"k{i}) " in text for i in range(6)) and "Answer: levain question answer 1" in text


def test_blocks_is_checked_against_its_own_cap_only(tmp_path):
    # `_TEXT_MAX` (4000) is the answer text's and the note's cap; a 4,933-character block files whole
    store = QuestionStore(tmp_path / "consent")
    assert len(_file(store, blocks="b" * 4933).blocks) == 4933


# --- item 1: the open question -------------------------------------------------------------------------------
def test_an_open_question_files_with_no_options_and_no_rec(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    q = _open(store)
    assert q.options == () and q.rec == "" and q.open_ended
    assert not _file(store).open_ended


@pytest.mark.parametrize("over", [
    dict(options=[("a", "x")]),                                 # an option beside open
    dict(options=[("a", "x"), ("b", "y")]),
    dict(rec="a"),                                              # a rec beside open
])
def test_an_open_question_refuses_options_or_a_rec(tmp_path, over):
    store = QuestionStore(tmp_path / "consent")
    with pytest.raises(ConsentError):
        _open(store, **over)
    assert store.open_questions() == []


@pytest.mark.parametrize("over", [dict(options=[]), dict(options=[("a", "only one")])])
def test_a_choice_question_still_needs_two_options(tmp_path, over):
    store = QuestionStore(tmp_path / "consent")
    with pytest.raises(ConsentError, match="2 to 6"):
        _file(store, **over)


def test_an_open_question_is_answered_in_words_never_by_an_option_key(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    q = _open(store)
    with pytest.raises(ConsentError, match="open question"):
        store.answer(q.id, option="a", channel="cli")
    store.answer(q.id, text="treat it as null and log one line", channel="phone")
    line = band.answer_line(store.get(q.id))
    assert "in words: " in line and "treat it as null" in line


def test_ask_open_files_and_names_no_option(tmp_path, monkeypatch):
    qdir = tmp_path / "consent"
    code, out = _run(["ask", "--what", "which way?", "--open", "--blocks", "the plan", "--queue-dir", str(qdir)],
                     monkeypatch, env={"CLAUDE_CODE_SESSION_ID": SESSION})
    assert code == 0, out
    q = QuestionStore(qdir).get(1)
    assert q.open_ended and q.options == () and q.rec == ""


@pytest.mark.parametrize("argv, why", [
    (["--open", "--option", "a=x", "--option", "b=y"], "--open files an open question"),
    (["--open", "--rec", "a"], "--open files an open question"),
    ([], "--rec KEY is required"),
    (["--option", "a=x", "--option", "b=y"], "--rec KEY is required"),
])
def test_ask_refuses_open_mixed_with_a_choice_and_files_nothing(tmp_path, monkeypatch, argv, why):
    qdir = tmp_path / "consent"
    code, out = _run(["ask", "--what", "q?", "--blocks", "z", "--queue-dir", str(qdir), *argv], monkeypatch,
                     env={"CLAUDE_CODE_SESSION_ID": SESSION})
    assert code == 2 and why in out
    assert QuestionStore(qdir).open_questions() == []


def test_ask_one_option_is_still_refused(tmp_path, monkeypatch):
    qdir = tmp_path / "consent"
    code, out = _run(["ask", "--what", "q?", "--option", "a=x", "--rec", "a", "--blocks", "z", "--queue-dir",
                      str(qdir)], monkeypatch, env={"CLAUDE_CODE_SESSION_ID": SESSION})
    assert code == 2 and "2 to 6" in out


def test_the_ping_of_an_open_question_has_no_options_and_no_rec(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    text = band._ping_text("claude_code", _open(store), tmp_path / "consent", 2)
    assert "Options:" not in text and "(rec)" not in text and "--option" not in text
    assert "answer in your own words" in text and "--text" in text and text.endswith("Blocks: the importer plan.")


def test_an_open_question_pings_at_once_even_with_a_coordinator_lease(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(cc, "notify", lambda settings, text, cwd=None, **_kw: sent.append(text) or True)
    monkeypatch.setattr(band, "_coordinator_session", lambda qdir, session: False)
    monkeypatch.setattr(band, "_hold", lambda qdir, qid: pytest.fail("an open question is never held"))
    code, _out = _run(["ask", "--what", "which way?", "--open", "--blocks", "the plan", "--queue-dir",
                       str(tmp_path / "consent")], monkeypatch, env={"CLAUDE_CODE_SESSION_ID": SESSION})
    assert code == 0 and len(sent) == 1


def test_the_band_lists_an_open_question_with_no_rec_or_option_rows(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    _open(store)
    r = band_view.render_full(store)
    assert "open question: answer in your own words" in r.text and "rec:" not in r.text and "a)" not in r.text
    payload = band_view.band_payload(store, "cockpit:t")
    (item,) = payload["items"]
    assert item["options"] == [] and item["rec"] is None and item["open_ended"] is True and item["kind"] == "information"


def test_band_shorthand_text_answers_an_open_question_and_a_letter_or_rec_does_not(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    q = _open(store)
    viewer = "cockpit:t"
    shown = band_view.band_payload(store, viewer)
    ids = dict(store_id=shown["store_id"], view_id=shown["view_id"])
    for bad in ("1a", "1: rec", "1r"):
        try:
            done = band_view.answer_text(store, bad, viewer=viewer, channel="cockpit", **ids)
        except ValueError:
            continue                                          # refused whole: also nothing recorded
        assert done.recorded == [], bad
        assert store.get(q.id).answer is None, bad
    done = band_view.answer_text(store, "1: null and one log line", viewer=viewer, channel="cockpit", **ids)
    assert len(done.recorded) == 1
    assert store.get(q.id).answer.kind == "text" and store.get(q.id).answer.value == "null and one log line"


def test_question_show_and_list_json_carry_the_flag(tmp_path, monkeypatch):
    qdir = tmp_path / "consent"
    _open(QuestionStore(qdir))
    code, out = _run(["question", "show", "1", "--json", "--queue-dir", str(qdir)], monkeypatch)
    row = json.loads(out)
    assert code == 0 and row["open_ended"] is True and row["options"] == [] and row["rec"] == ""
    code, out = _run(["question", "show", "1", "--queue-dir", str(qdir)], monkeypatch)
    assert "an open question: answer it in your own words" in out and "<- rec" not in out


def test_the_mcp_ask_tool_takes_open_without_options(tmp_path, monkeypatch):
    from levain.consent import band_mcp

    assert band_mcp.TOOL["inputSchema"]["required"] == ["what", "blocks"]
    assert band_mcp._arguments({"what": "w", "blocks": "b", "open": True}) == ("w", [], "", "b", True)
    with pytest.raises(band_mcp._RpcError, match="missing arguments: options, rec"):
        band_mcp._arguments({"what": "w", "blocks": "b"})
    with pytest.raises(band_mcp._RpcError, match="open must be"):
        band_mcp._arguments({"what": "w", "blocks": "b", "open": "yes"})


def test_the_asker_rec_of_an_open_question_says_so(tmp_path):
    from levain.coordinator import deliver

    q = _open(QuestionStore(tmp_path / "consent"))
    rec = deliver._asker_rec(q)
    assert rec.startswith("none: an open question, answered in words") and len(rec) <= review._REC_MAX
