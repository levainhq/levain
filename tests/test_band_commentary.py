"""11-04 desk 25: a band answer may carry the operator's own words after it.

Phill typed ``5a -> this works! -> can I reopen...`` and the hook said it could not read the line. The LEADING run of
answer tokens is now read and recorded; the words after it end the run, are kept as the answer's note (the whole
line, verbatim), are signed with it, and can never add or change an answer.

No test reaches the Secure Enclave: the signed round trip uses a plain ed25519 key made into a tmp dir.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest

from levain import band_view
from levain.band_shorthand import ItemInfo, View, parse
from levain.cockpit import keys as ckeys
from levain.cockpit.custody import APPLE_PROVIDER, SIDECAR_FORMAT, SIDECAR_SUFFIX
from levain.consent import band as consent_band
from levain.consent import terminal_sign as ts
from levain.consent.questions import QuestionStore

INFO = ItemInfo("information", 3, "b")
AUTH = ItemInfo("authority", 2, "a")
ITEMS = {1: INFO, 2: INFO, 3: INFO, 4: INFO, 5: INFO, 6: AUTH}
VIEW = View(seen_through=6, open_list=(1, 2, 3, 4, 5, 6), store_id="qs-1")
PHILL = "5a -> this works! -> can I reopen..."


def run(text, items=ITEMS):
    return parse(text, VIEW, items, "qs-1")


def acts(p):
    return [(a.n, a.pick, a.text, a.note, a.via_rec) for a in p.actions]


# --- the grammar ----------------------------------------------------------------------------------------------------
def test_phills_line_records_5a_with_the_whole_line_as_its_note():
    p = run(PHILL)
    assert p.matched and not p.refused and p.hint is None and p.rest == ""
    assert acts(p) == [(5, "a", None, PHILL, False)]


def test_phills_line_on_an_authority_item_opens_its_sheet_and_records_nothing():
    p = run(PHILL, {**ITEMS, 5: AUTH})
    assert p.matched and not p.actions and not p.refused and [(s.n, s.pick) for s in p.sheets] == [(5, "a")]


@pytest.mark.parametrize("text,want", [
    ("1a this works", [(1, "a")]),                     # a word ends the run
    ("1a, what", [(1, "a")]),
    ("1a foo", [(1, "a")]),
    ("1a! great", [(1, "a")]),                         # punctuation right after the entry
    ("1a->ok", [(1, "a")]),
    ("1a -- fine", [(1, "a")]),
    ("1a (works great)", [(1, "a")]),
    ("1a, 2b -> both good", [(1, "a"), (2, "b")]),     # several leading tokens
    ("1a 2 rec => ok", [(1, "a"), (2, "b")]),
    ("1: a -> fine", [(1, "a")]),                      # the colon form before punctuation stays a pick
    ("2 rec. thanks", [(2, "b")]),
])
def test_leading_answers_then_commentary(text, want):
    p = run(text)
    assert p.matched and not p.refused, p
    assert [(a.n, a.pick) for a in p.actions] == want
    assert {a.note for a in p.actions} == {text}


def test_a_sweep_then_commentary_puts_the_line_on_each_swept_item():
    p = run("all recs please")
    assert {a.n for a in p.actions} == {1, 2, 3, 4, 5} and {a.note for a in p.actions} == {"all recs please"}


def test_the_recorded_note_is_the_line_as_typed_trimmed_at_its_ends_only():
    p = run("  1a  ->   spaced,  out ;  ")
    assert acts(p) == [(1, "a", None, "1a  ->   spaced,  out ;", False)]


# --- the safety property: commentary never adds or changes an answer -----------------------------------------------
@pytest.mark.parametrize("text", [
    "1a -> 2b is better",
    "1a -> 1b actually",
    "1a this works, 3c too",
    "1a -> all recs for the rest",
    "1a -> 4: free words",
    "1a, nonsense, 2b",
    "1a -> (b)",
])
def test_a_number_or_letter_inside_the_commentary_is_text(text):
    p = run(text)
    assert [(a.n, a.pick, a.text) for a in p.actions] == [(1, "a", None)]
    assert not p.refused and not p.sheets


def test_an_answer_shape_inside_the_commentary_is_said_not_read():
    p = run("1a -> 2b is better")
    assert "'2b'" in p.hint and "not read as an answer" in p.hint
    assert run("1a -> this works").hint is None


def test_the_existing_note_forms_keep_their_note_text():
    assert acts(run("3 b because the API is down")) == [(3, "b", None, "the API is down", False)]
    assert acts(run("3 b - verbatim: X, Y; Z")) == [(3, "b", None, "verbatim: X, Y; Z", False)]


def test_free_text_still_takes_the_rest_of_its_line():
    assert acts(run("3: a thing, with commas; and more")) == [(3, None, "a thing, with commas; and more", None, False)]
    assert acts(run("3: no -> never")) == [(3, None, "no -> never", None, False)]


def test_each_line_of_a_block_keeps_its_own_words():
    p = run("1a -> yes\n2b\n3c because no")
    assert acts(p) == [(1, "a", None, "1a -> yes", False), (2, "b", None, None, False), (3, "c", None, "no", False)]


# --- what stays loud -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["hello there", "this works -> 1a", "-> 1a", "the 3 of us"])
def test_a_line_whose_first_token_is_no_answer_is_silent(text):
    p = run(text)
    assert not p.matched and not p.actions and p.hint is None and p.rest == text


@pytest.mark.parametrize("text,token", [
    ("1z -> this works", "'1z'"),
    ("1a 2z -> hmm", "'2z'"),
    ("1a, 2ab", "'2ab'"),
    ("1a 3 things", "3 has no pick"),
    ("1a (b)", "number in front"),
])
def test_a_malformed_token_in_the_leading_run_records_nothing_and_is_named(text, token):
    p = run(text)
    assert not p.matched and not p.actions and not p.refused
    assert p.hint and token in p.hint and "nothing was recorded" in p.hint


def test_a_number_with_no_pick_before_commentary_is_named():
    p = run("5 -> this works")
    assert not p.matched and not p.actions and "5 has no pick" in p.hint


def test_bare_pick_first_keeps_its_hint():
    assert "number in front" in run("(a) all recs").hint


# --- the recording path, signed with a tmp key --------------------------------------------------------------------
OPTS = [("fix", "fixed 30 s"), ("exp", "exponential"), ("none", "fail loud")]
PLAIN_TERMINAL = [(1111, ["-zsh"]), (1000, ["/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"])]
VIEWER = "claude_code:seat-1"
SESSION = "seat-1"


@pytest.fixture
def human(monkeypatch, tmp_path):
    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "CODEX_SANDBOX", "LEVAIN_TURN",
                 "SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(consent_band, "_ancestors", lambda: iter(PLAIN_TERMINAL))
    monkeypatch.setattr(ts, "_real_key_dir", lambda: tmp_path / "no-real-keys")

    def boom(path, timeout=None):
        raise AssertionError("a test reached the default (real) signer factory")
    monkeypatch.setattr(ts, "default_signer_factory", boom)


@pytest.fixture
def store(tmp_path, human):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return QuestionStore(d)


@pytest.fixture
def env(store):
    return {"LEVAIN_HOME": str(store.directory.parent)}


@pytest.fixture
def keydir(tmp_path):
    d = tmp_path / "keys"
    d.mkdir(mode=0o700)
    path = d / ts.KEY_LABEL
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(path)], check=True)
    (d / (ts.KEY_LABEL + SIDECAR_SUFFIX)).write_text(json.dumps(
        {"format": SIDECAR_FORMAT, "custody": "touch-id", "provider": APPLE_PROVIDER, "ctk_hash": "A" * 40,
         "label": ts.KEY_LABEL}))
    return d


def tmp_factory(path, timeout=None):
    return ckeys.ssh_signer(path, namespace=ts.NAMESPACE, timeout=timeout)


def seen(store, env, n):
    qs = [store.file(agent="claude_code", agent_session_id="sess-1", what=f"question {i}", options=OPTS, rec="exp",
                     blocks="the PR", cwd="/tmp/proj") for i in range(1, n + 1)]
    out, commit = band_view.hook_prompt({"session_id": SESSION, "prompt": "hello"}, env)
    commit()
    assert f"band@{n}" in out
    return qs


def test_phills_line_round_trips_recorded_and_signed_with_the_whole_line(store, env, keydir):
    qs = seen(store, env, 5)
    echo = band_view._seat_answer(store, VIEWER, PHILL, env, None, session=SESSION, signer_factory=tmp_factory,
                                  key_dir=keydir)
    assert "recorded: 5a (you, seat)" in echo and "signed (Touch ID, laptop): 5a" in echo
    assert "did not read" not in echo
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT q.n, a.value FROM answers a JOIN questions q ON q.id = a.question_id").fetchall() == [
            (5, "fix")]
        assert c.execute("SELECT note, sign_state FROM answer_context").fetchall() == [(PHILL, "signed")]
    rec = band_view.answer_sign_of(store, qs[4].id)
    pub = " ".join((keydir / (ts.KEY_LABEL + ".pub")).read_text().split()[:2])
    got = ts.verify(rec["message"].encode(), rec["sig"], f'phill namespaces="{ts.NAMESPACE}" {pub}\n', "phill",
                    ctk=lambda key: (None, "not checked"))
    assert got.ok, got.problem
    parsed = ts.parse_verified(rec["message"].encode())
    assert parsed.typed == PHILL and parsed.typed_matches_hash                # what was signed is what he typed
    assert [(e.item, e.answer, e.note) for e in parsed.entries] == [(qs[4].id, "(a) fixed 30 s", PHILL)]


def test_commentary_naming_another_item_records_and_signs_only_the_leading_answer(store, env, keydir):
    qs = seen(store, env, 2)
    echo = band_view._seat_answer(store, VIEWER, "1a -> 2b is better", env, None, session=SESSION,
                                  signer_factory=tmp_factory, key_dir=keydir)
    assert "recorded: 1a (you, seat)" in echo and "'2b'" in echo and "not read as an answer" in echo
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT q.n, a.value FROM answers a JOIN questions q ON q.id = a.question_id").fetchall() == [
            (1, "fix")]
    parsed = ts.parse_verified(band_view.answer_sign_of(store, qs[0].id)["message"].encode())
    assert [e.item for e in parsed.entries] == [qs[0].id] and parsed.typed == "1a -> 2b is better"
    assert band_view.answer_sign_of(store, qs[1].id) is None
