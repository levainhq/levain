"""A question put to the operator in prose is a band row: the shape detector (:mod:`levain.operator_ask`) and the Stop
hook's refuse-and-file check (:func:`levain.band_view._ask_reason`)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain import band_view, operator_ask
from levain.consent.questions import QuestionStore

OPTS = [("fix", "fixed 30 s"), ("exp", "exponential")]

PRICE = ("Price for the ChatGPT Business profile setup on /agent-ready/: is $150 flat OK, photos and details ready?\n"
         "(a) $150 flat, materials ready\n(b) $130/hr, open scope\nSay a or b and I'll send the quote.")
DESK_LIST = ("Three passes are possible on the test list:\n1. Re-ask what you passed this morning\n"
             "2. Re-ask what you use daily\n3. Skip the list and ship\nWhich of these do you want me to run?")


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir):
    return QuestionStore(qdir)


def env(qdir):
    return {"LEVAIN_HOME": str(qdir.parent)}


def other_row(store):
    """An open row some other session filed, so the seat's band has an item to reprint."""
    return store.file(agent="claude_code", agent_session_id="sess-other", what="Which retry policy?", options=OPTS,
                      rec="exp", blocks="the feed PR", cwd="/tmp/proj")


def no_desk_launch(monkeypatch):
    from levain.desk import lease
    from levain.desk.store import CoordStore

    monkeypatch.setattr(CoordStore, "open_readonly", lambda self, **_k: sqlite3.connect(":memory:"))
    monkeypatch.setattr(lease, "by_harness_session", lambda conn, sid: None)


def deliver(sid, e):
    """What the UserPromptSubmit hook does for one prompt: render the band, write the turn record."""
    text, commit = band_view.hook_prompt({"session_id": sid, "prompt": "hi"}, e)
    commit()
    return text


def stop(sid, message, e, **more):
    return band_view.hook_stop({"session_id": sid, "last_assistant_message": message, **more}, e)


def test_a_priced_pick_in_prose_is_an_ask_to_the_operator():
    v = operator_ask.detect(PRICE)
    assert v is not None and v.addressee == "operator"
    assert {"lettered_options", "say_and_ill", "confirm_pick"} <= set(v.shapes)
    assert "$150" in v.what_hint


def test_a_numbered_desk_style_list_with_a_decision_question_is_an_ask():
    v = operator_ask.detect(DESK_LIST)
    assert v is not None and "numbered_options" in v.shapes and v.addressee == "operator"
    assert operator_ask.detect("Done:\n1. fixed the retry\n2. added the test\n3. pushed the branch") is None


def test_a_reply_that_only_reprints_the_band_block_is_no_ask():
    block = ["band@3 · NEEDS YOU · 1 open", "1. Which retry policy? (a) fix (b) exp  rec: exp  blocks: the feed PR"]
    text = "Done, the test passes.\n\n" + "\n".join(block)
    assert operator_ask.detect(text, drop_lines=block) is None
    assert operator_ask.detect(text) is not None                         # the band's own item reads as an ask
    assert operator_ask.detect(text + "\nWant me to push it?", drop_lines=block) is not None


def test_a_message_to_another_agent_is_not_refused(qdir, store, monkeypatch):
    msg = "ROUTE to levain 54: can we ship the retry change?"
    v = operator_ask.detect(msg)
    assert v is not None and v.addressee == "agent"
    no_desk_launch(monkeypatch)
    e = env(qdir)
    other_row(store)
    band = deliver("s1", e)
    assert stop("s1", f"{msg}\n\n{band}", e) is None


def test_the_stop_hook_refuses_a_prose_question_with_no_row_once(qdir, store, monkeypatch):
    no_desk_launch(monkeypatch)
    e = env(qdir)
    other_row(store)
    band = deliver("s1", e)
    reply = f"{PRICE}\n\n{band}"
    reason = stop("s1", reply, e)
    assert reason and "levain ask" in reason and "--option" in reason and "--rec" in reason
    assert stop("s1", reply, e, stop_hook_active=True) is None
    assert stop("s1", f"all good\n\n{band}", e) is None


def test_the_same_turn_with_a_row_filed_after_it_began_is_allowed(qdir, store, monkeypatch):
    no_desk_launch(monkeypatch)
    e = env(qdir)
    other_row(store)
    band = deliver("s1", e)
    store.file(agent="claude_code", agent_session_id="s1", what="Is $150 flat OK?", options=OPTS, rec="fix",
               blocks="the quote", cwd="/tmp/proj")
    assert stop("s1", f"{PRICE}\n\n{band}", e) is None
    assert store.filed_since("claude_code", "s1", "9999-01-01T00:00:00.000000Z") == 0
    assert store.filed_since("claude_code", "nobody", "2000-01-01T00:00:00.000000Z") == 0


def band_with(store, what):
    store.file(agent="claude_code", agent_session_id="sess-other", what=what, options=OPTS, rec="exp", blocks="the feed PR",
               cwd="/tmp/proj")


def shapes(text, drop=()):
    v = operator_ask.detect(text, drop_lines=drop)
    return None if v is None else v.addressee


def test_a_band_reprint_with_inline_code_and_a_url_is_no_ask(qdir, store, monkeypatch):
    block = ["band@3 · NEEDS YOU · 1 open",
             "1. Ship `foo.py` now? See https://github.com/x/y/pull/3 (a) yes (b) no  rec: a  blocks: the feed PR"]
    text = "Done, tests pass.\n\n" + "\n".join(block)
    assert operator_ask.detect(text, drop_lines=block) is None
    no_desk_launch(monkeypatch)
    e = env(qdir)
    band_with(store, "Ship `foo.py` now? See https://github.com/x/y/pull/3")
    band = deliver("s1", e)
    assert "`foo.py`" in band and "https://github.com" in band
    assert stop("s1", f"Done, tests pass.\n\n{band}", e) is None


# Source: flow tests/test_band_question_gate.py at flow main, copied 2026-11-20: the texts that file pins as an ask
# and as no ask. The footer lines of the status reply are the band's own, so they are passed as dropped lines.
FLOW_HITS = [
    "Two ways to land it:\n(a) ship tonight\n(b) wait for the morning install\nWhich do you want?",
    "Can I send the quote form to the new client?\n\nEverything else is verified and merged.",
    "Filed it. (a) ship (b) wait, your call on the band?",
    "Can I rm scripts/foo.py?",
    "Should we ship v1.2?",
    "Can I deploy\nthe release now?",
    "Can I deploy now?",
    "Do you want me to deploy?",
    "(a) one\n(b) two\nwhich do you want?",
    "Merged and pushed. Let me know if anything looks off.",
    "Pick one of a, b, c for the demo and tell me.",
]
FLOW_FOOTER = ["NEEDS YOU (2)", "1. (a) launch (b) hold: rec a", "2. say which: the time?"]
FLOW_PASSES = [
    ("The audit log contains the phrase your call in it.", ()),
    ("Both builds are under way on your signed (a)s: I'd say about 5 minutes. The failure didn't tell me which file was "
     "missing; we compare (a) stdin versus (b) stdout.\n\n" + "\n".join(FLOW_FOOTER), FLOW_FOOTER),
    ("```\n(a) x\n(b) y\nwant me to?\n```\n\n> should we ship?\n\nDone and verified.", ()),
    ("NEEDS YOU: nothing yet.", ()),
]


@pytest.mark.parametrize("text", FLOW_HITS)
def test_what_flows_detector_pins_as_an_ask_is_an_ask(text):
    assert operator_ask.detect(text) is not None


@pytest.mark.parametrize("text,drop", FLOW_PASSES)
def test_what_flows_detector_pins_as_no_ask_is_no_ask(text, drop):
    assert operator_ask.detect(text, drop_lines=drop) is None


@pytest.mark.parametrize("text", [
    "Landed:\n1. the parser fix\n2. the retry test\nThe reply from the API is cached now.",
    "Review:\n1. no HIGH\n2. one LOW\nI would approve this branch.",
    "Results:\n1. pass\n2. pass\n3. fail\nI will confirm after the rerun.",
    "Plan:\n1. patch\n2. test\nI will go with that and report.",
    "Findings:\n1. a.py: stale\n2. b.py: ok\nThe loader chooses the first match.",
    "Done:\n1. PR approved by review\n2. Tests pass\n3. Merged",
])
def test_a_numbered_report_with_a_decision_word_is_no_ask(text):
    assert operator_ask.detect(text) is None


@pytest.mark.parametrize("text", [
    "The API will reply with a 429 when throttled.",
    "The protocol reply: 200 OK, then close.",
    "He'd just say yes to anything, so I added the check.",
    "This change needs your lane's merge before the next one.",
    "The queue is waiting for you to wake, per the plan.",
    "Flow's gate treats your ok as a token; I changed the parser.",
    "Run `levain ask --what x` then say go and I will continue.",
    "The hook returns a reply: {}",
    "The threshold is your call-site constant.",
])
def test_ordinary_prose_with_an_ask_word_is_no_ask(text):
    assert operator_ask.detect(text) is None


@pytest.mark.parametrize("text", [
    "Say a or b and I'll send the quote.",
    "Reply with a or b.",
    "Fixed it.\nTell me which retry policy you want.",
    "Which retry policy do you prefer, (a) fixed or (b) exponential?",
])
def test_an_imperative_or_a_question_to_him_is_an_ask(text):
    assert shapes(text) == "operator"


def test_a_quoted_question_a_lone_closing_question_and_a_title_are_no_ask():
    assert operator_ask.detect("Phill wrote:\n> Should we ship v1.2 tonight? Say go and I'll hold.\n\nShipped; it is live.") is None
    for text in ("Pushed.\n\nIs that clear?", "Merged.\n\nAnything else?", "The gate trusts the test.\n\nWhat does that mean for the hook?",
                 "Merged the PR titled Why does the gate refuse?",
                 "Reached phill@example.com and ask@levain.dev.\n\nIs that clear?"):
        assert operator_ask.detect(text) is None, text


def test_an_agent_cue_hides_only_its_own_sentence():
    for text in ("ROUTE to levain 54: next: lane 2.\nPhill, which do you prefer, (a) ship or (b) wait?",
                 "Per @desk's note I rebased. Should I push to main now?",
                 "@phill Should I push?",
                 "I used SendMessage to tell lane 3. Should I merge it now, your call?",
                 "Reply to head 5: done. Can I merge now, Phill?",
                 "Added @dataclass to the model. Should I push to main?"):
        assert shapes(text) == "operator", text
    assert shapes("ROUTE to levain 54: can you take lane 3? Do you have the lease?") == "agent"
    assert shapes("@desk should we ship the retry change?") == "agent"


def test_a_row_cite_exempts_only_its_own_sentence():
    block = ["band@3 · NEEDS YOU · 2 open", "1. Which retry policy? (a) fix (b) exp  rec: exp", "2. Ship it? (a) yes (b) no  rec: a"]
    for text in ("Ran band@3 check. Want me to push now?",
                 "q-abcdef123456 waits. Which should I do next? (a) x (b) y",
                 "Row 1 is filed. Separately, which cache do you want, (a) global or (b) per-session?"):
        assert shapes(text, block) == "operator", text
    assert operator_ask.detect("Row 1 still waits on your pick; band@3 above.", drop_lines=block) is None


def test_an_unclosed_fence_or_a_cut_inside_a_fence_does_not_hide_an_ask():
    assert shapes("Done.\n```\ncode\nWhich do you want? (a) x (b) y") == "operator"
    long = "Here is the log:\n```\n" + "x = 1\n" * 3000 + "```\n\nShould I merge it now? (a) yes (b) no"
    assert shapes(long) == "operator"


def test_the_shown_lines_of_an_unreadable_store_are_unknown_not_none_shown(qdir, store, monkeypatch):
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    other_row(store)
    fact = band_view.TurnFact("operator", "v1", False, True)
    monkeypatch.setattr(band_view, "_view_row", boom)
    assert band_view._shown_lines(qdir, fact, "s1") is None


def test_a_row_filed_in_an_earlier_turn_does_not_excuse_this_turn(qdir, store, monkeypatch):
    no_desk_launch(monkeypatch)
    e = env(qdir)
    other_row(store)
    store.file(agent="claude_code", agent_session_id="s1", what="Is $150 flat OK?", options=OPTS, rec="fix",
               blocks="the quote", cwd="/tmp/proj")
    band = deliver("s1", e)
    assert stop("s1", f"{PRICE}\n\n{band}", e)


def test_the_claim_allows_the_second_stop_of_one_turn_without_the_continuation_flag(qdir, store, monkeypatch):
    no_desk_launch(monkeypatch)
    e = env(qdir)
    other_row(store)
    band = deliver("s1", e)
    reply = f"{PRICE}\n\n{band}"
    assert stop("s1", reply, e)
    assert stop("s1", reply, e) is None


def test_a_desk_reply_is_checked_against_the_block_it_was_shown(tmp_path):
    from tests.test_desk_wake_silent import Rig

    rig = Rig(tmp_path, "claude_code")
    rig.ask("Pick a DB")
    from tests.test_desk_wake_silent import block_of

    ctx = rig.ups("hello desk")
    shown = block_of(ctx, rig)
    assert rig.stop(f"Done, the build is green.\n\n{shown}") == ""          # the staged block is not read as an ask
    ctx = rig.ups("anything else?")
    shown = block_of(ctx, rig)
    out = json.loads(rig.stop(f"{PRICE}\n\n{shown}"))
    assert out["decision"] == "block" and "levain ask" in out["reason"]
    again = rig.run("stop.py", {"last_assistant_message": f"{PRICE}\n\n{shown}", "cwd": str(rig.install),
                                "stop_hook_active": True})
    assert again == ""


def test_a_desk_wake_reply_that_asks_is_refused_and_one_that_does_not_is_not(tmp_path):
    from tests.test_desk_wake_silent import Rig

    rig = Rig(tmp_path, "claude_code")
    rig.ask("Pick a DB")
    rig.ups("hello desk")
    rig.ups(rig.wake)
    out = json.loads(rig.stop(PRICE))
    assert out["decision"] == "block" and "levain ask" in out["reason"]
    rig.ups(rig.wake)
    assert rig.stop("split drop d1 into 2 threads") == ""


def ask_row(store, what="Can you approve deploy X?"):
    return store.file(agent="claude_code", agent_session_id="sess-other", what=what, options=OPTS, rec="exp",
                      blocks="the deploy", cwd="/tmp/proj")


def test_a_row_answered_between_the_prompt_and_the_stop_does_not_refuse_the_reprinted_block(qdir, store, monkeypatch):
    no_desk_launch(monkeypatch)
    e = env(qdir)
    row = ask_row(store)
    band = deliver("s1", e)
    assert "Can you approve deploy X?" in band
    store.answer(row.id, option="exp", channel="cli")                       # answered from another surface
    assert stop("s1", f"Done, tests pass.\n\n{band}", e) is None


def test_a_turn_whose_view_was_never_committed_is_not_checked_against_an_older_view(qdir, store, monkeypatch):
    no_desk_launch(monkeypatch)
    e = env(qdir)
    ask_row(store, "Which retry policy do we keep?")
    deliver("s1", e)                                                        # turn 1: its view is committed
    ask_row(store, "Can you approve deploy Y?")
    text, _commit = band_view.hook_prompt({"session_id": "s1", "prompt": "next"}, e)
    assert "Can you approve deploy Y?" in text                              # turn 2 showed it; the commit never ran
    assert "levain ask" not in (stop("s1", f"Done, tests pass.\n\n{text}", e) or "")


def test_a_desk_view_with_items_and_no_staged_record_is_unknown_and_an_empty_one_is_none(qdir, store, monkeypatch):
    fact = band_view.TurnFact("operator", band_view.DESK_VIEWER, True, True)
    wake = band_view.TurnFact(band_view.FACT_DESK_WAKE, band_view.DESK_VIEWER, True, True)
    view = band_view.View(3, (1,), None)
    monkeypatch.setattr(band_view, "_view_row", lambda *_a: (view, 1, "band@3 · NEEDS YOU · 1 open"))
    assert band_view._shown_lines(qdir, fact, "s1") is None
    assert band_view._shown_lines(qdir, wake, "s1") == []                   # a wake showed nothing
    monkeypatch.setattr(band_view, "_view_row", lambda *_a: (band_view.View(3, (), None), 1, None))
    assert band_view._shown_lines(qdir, fact, "s1") == []
    monkeypatch.setattr(band_view, "_view_row", lambda *_a: None)
    assert band_view._shown_lines(qdir, fact, "s1") == []


def claims(qdir):
    d = qdir / "band_stop"
    return sorted(p.name for p in d.iterdir() if not p.name.startswith("s-")) if d.exists() else []


def test_the_ask_claim_goes_with_its_turn(qdir, store, monkeypatch):
    no_desk_launch(monkeypatch)
    e = env(qdir)
    other_row(store)
    band = deliver("s1", e)
    reply = f"{PRICE}\n\n{band}"
    assert stop("s1", reply, e) and len(claims(qdir)) == 1
    assert stop("s1", reply, e, stop_hook_active=True) is None              # the continuation ends the turn
    assert claims(qdir) == []
    band = deliver("s1", e)
    assert stop("s1", f"{PRICE}\n\n{band}", e) and len(claims(qdir)) == 1
    deliver("s1", e)                                                        # a new prompt: the old turn is over
    assert claims(qdir) == []


def test_a_reason_for_a_desk_commit_that_also_asks_starts_with_the_reason(tmp_path):
    from tests.test_desk_wake_silent import Rig, block_of

    rig = Rig(tmp_path, "claude_code")
    rig.ask("Pick a DB")
    shown = block_of(rig.ups("hello desk"), rig)
    out = json.loads(rig.stop(f"{PRICE}\n\n{shown}"))
    assert out["reason"].startswith("[Levain")


def test_filing_the_same_question_again_is_a_new_row_so_a_turn_that_files_is_counted(qdir, store):
    first = ask_row(store, "Which DB?")
    again = ask_row(store, "Which DB?")
    assert first.id != again.id
    assert store.filed_since("claude_code", "sess-other", "2000-01-01T00:00:00.000000Z") == 2


def test_the_prose_check_reads_the_store_inside_the_hook_bound(qdir, store, monkeypatch):
    from levain.consent import questions

    no_desk_launch(monkeypatch)
    e = env(qdir)
    other_row(store)
    band = deliver("s1", e)
    seen = []
    real_view, real_filed = band_view._view_row, QuestionStore.filed_since
    monkeypatch.setattr(band_view, "_view_row", lambda *a: seen.append(questions.current_read_deadline()) or real_view(*a))
    monkeypatch.setattr(QuestionStore, "filed_since",
                        lambda self, *a: seen.append(questions.current_read_deadline()) or real_filed(self, *a))
    assert stop("s1", f"{PRICE}\n\n{band}", e)
    assert len(seen) >= 3 and all(d is not None for d in seen)


@pytest.mark.parametrize("text", [
    "I changed the prompt to: Please approve the request.",
    "The UI now displays Please confirm your email before continuing.",
    "The page says: Please pick a plan.",
    "The dialog reads: Reply with a or b.",
    "Please approve the request.",
])
def test_reported_interface_text_and_a_bare_request_with_no_question_is_no_ask(text):
    assert operator_ask.detect(text) is None


def test_a_direct_request_that_stands_as_its_own_clause_is_still_an_ask():
    assert shapes("Ok. Please confirm you want me to ship it?") == "operator"
    assert shapes("Please confirm: ship it?") == "unclear"


def test_a_description_of_a_message_sent_does_not_hide_the_real_question(qdir, store, monkeypatch):
    text = "I sent the report to Codex: it passed. Should I deploy now? (a) yes (b) no"
    assert shapes(text) == "operator"
    assert shapes("Message to flow 54: can we ship the retry change?") == "agent"
    assert shapes("To codex: can we ship the retry change? It passed.") == "agent"
    no_desk_launch(monkeypatch)
    e = env(qdir)
    other_row(store)
    band = deliver("s1", e)
    assert stop("s1", f"{text}\n\n{band}", e)
