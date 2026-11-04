"""TERMSIGN lbl: a character a person cannot see never reaches what the operator signs.

The finding (L3 on L2n): the terminal signer's line rule refused C0 controls, DEL and U+2028/2029 only, so an
agent-filed option label holding a bidi override (U+202E), an isolate (U+2066), a zero-width space (U+200B) or a C1
control (U+0085) was signed as it stood while the notice showed it cleaned, and a bidi override can reorder what he
reads against what is signed. Every field of the message is now held to ``levain.unshowable``, the one rule, at
build and at parse; the question store already refuses such a label at filing; the notice marks, never strips."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain import band_view
from levain.consent import terminal_sign as ts
from levain.cockpit.consent import ConsentError
from tests.test_termsign_l2_seat import (SESSION, VIEWER, ask, env, human_terminal, keydir, qdir,  # noqa: F401
                                         real_factory, recorded, sidecar, store)

HIDDEN = ["‮", "⁦", "​", "\u0085"]
AT = ts.datetime(2026, 11, 3, tzinfo=ts.timezone.utc)


def _entry(answer="(a) approve", asked="Which?", text=None):
    return ts.Entry("np-1", "v1", asked, "information", answer=None if text is not None else answer, text=text)


def _build(*entries, typed="1a"):
    return ts.build_message(store="flow-needs-phill", install="i1", session="s1", view="1", typed=typed,
                            entries=list(entries), nonce="0" * 16, at=AT)


@pytest.mark.parametrize("ch", HIDDEN, ids=lambda c: f"U+{ord(c):04X}")
def test_a_hidden_character_in_an_option_label_is_refused_at_build(ch):
    with pytest.raises(ts.MessageError, match=f"U\\+{ord(ch):04X}"):
        _build(_entry(answer=f"(a) app{ch}rove"))


@pytest.mark.parametrize("ch", HIDDEN, ids=lambda c: f"U+{ord(c):04X}")
def test_a_hidden_character_in_the_question_is_refused_at_build(ch):
    with pytest.raises(ts.MessageError, match=f"U\\+{ord(ch):04X}"):
        _build(_entry(asked=f"Wh{ch}ich?"))


@pytest.mark.parametrize("ch", HIDDEN, ids=lambda c: f"U+{ord(c):04X}")
def test_bytes_carrying_a_hidden_character_do_not_parse(ch):
    clean = _build(_entry(answer="(a) approve"))
    dirty = clean.replace(b"(a) approve", f"(a) app{ch}rove".encode("utf-8"))
    assert dirty != clean
    with pytest.raises(ts.MessageError):
        ts.parse_verified(dirty)


def test_an_unpaired_surrogate_is_refused_at_build():
    with pytest.raises(ts.MessageError, match="U\\+D800"):
        _build(_entry(answer="(a) a\ud800b"))


def test_the_signed_answer_and_the_notice_show_the_same_words():
    label = "(a) approve the 30 s retry ⚠️"
    entry = _entry(answer=label)
    signed = ts.parse_verified(_build(entry)).entries[0]
    assert signed.answer == label
    assert ts.notice_text([entry]) == f"Touch ID to sign: np-1 {signed.answer}"


@pytest.mark.parametrize("ch", HIDDEN, ids=lambda c: f"U+{ord(c):04X}")
def test_the_notice_marks_a_hidden_character_and_never_drops_it(ch):
    out = ts.notice_text([_entry(answer=f"(a) app{ch}rove")])
    assert out == f"Touch ID to sign: np-1 (a) app⟨U+{ord(ch):04X}⟩rove"


# --- the ingestion point: the question store refuses at filing --------------------------------------------

@pytest.mark.parametrize("ch", HIDDEN, ids=lambda c: f"U+{ord(c):04X}")
def test_the_question_store_refuses_a_hidden_label_at_filing(store, ch):
    with pytest.raises(ConsentError, match=f"U\\+{ord(ch):04X}"):
        ask(store, options=[("fix", f"fixed{ch} 30 s"), ("exp", "exponential")])


# --- a row that never passed filing (written before the rule, or straight into the file) -----------------

@pytest.mark.parametrize("ch", HIDDEN, ids=lambda c: f"U+{ord(c):04X}")
def test_a_stored_hidden_label_is_never_signed_and_the_answer_still_records(store, env, keydir, monkeypatch, ch):
    q = ask(store)
    with sqlite3.connect(store.path) as c:     # the append-only trigger stands in the way of a hand edit: lift it
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'questions' "
                                 "AND sql LIKE '%UPDATE%'").fetchall():
            c.execute(f'DROP TRIGGER "{name}"')
        c.execute("UPDATE questions SET options = ? WHERE id = ?",
                  (json.dumps([{"key": "fix", "label": f"fixed{ch} 30 s"}, {"key": "exp", "label": "exponential"}]),
                   q.id))
    out, commit = band_view.hook_prompt({"session_id": SESSION, "prompt": "hello"}, env)
    commit()
    reached = []
    monkeypatch.setattr(ts, "sign", lambda *a, **kw: reached.append(1))
    echo = band_view._seat_answer(store, VIEWER, "1a", env, None, session=SESSION, signer_factory=real_factory,
                                  key_dir=keydir)
    assert recorded(store) == [(1, "fix")]
    assert sidecar(store)[0][5:] == ("unsigned", "signer-error")
    assert "recorded UNSIGNED" in echo
    assert reached == [] and ts.post_notice.posted == []
