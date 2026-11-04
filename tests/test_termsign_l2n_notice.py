"""TERMSIGN L2n: the notification that names what the Touch ID sheet is about to sign.

No test shows a notification: ``post_notice`` runs against an injected runner, and the band path against the
conftest recorder (``ts.post_notice.posted``)."""
from __future__ import annotations

import subprocess

from levain import band_view
from levain.cockpit.keys import KeyError_
from levain.consent import terminal_sign as ts
from tests.test_termsign_l2_seat import (SESSION, VIEWER, ask, env, human_terminal, keydir, qdir,  # noqa: F401
                                         real_factory)

E = ts.Entry


def _entry(item="np-1", answer=None, text=None):
    return E(item, "v1", "Which?", "information", answer=answer, text=text)


# --- notice_text ---------------------------------------------------------------------------------------

def test_text_from_one_answer_entry():
    assert ts.notice_text([_entry(answer="(a) fixed 30 s")]) == "Touch ID to sign: np-1 (a) fixed 30 s"


def test_text_from_several_entries_names_the_first_and_counts_the_rest():
    es = [_entry("np-1", answer="(a) x"), _entry("np-2", answer="(b) y"), _entry("np-3", answer="(c) z")]
    assert ts.notice_text(es) == "Touch ID to sign: np-1 (a) x (+2 more)"


def test_text_from_a_free_text_entry_and_the_desk_prefix():
    assert ts.notice_text([_entry(text="do it his way")]) == "Touch ID to sign: np-1 do it his way"
    assert ts.notice_text([_entry(text="hi")], channel="desk") == "Desk: Touch ID to sign: np-1 hi"


def test_a_long_answer_is_cut_and_control_characters_flattened():
    out = ts.notice_text([_entry(text="a" * 200)])
    assert out == "Touch ID to sign: np-1 " + "a" * 80
    assert ts.notice_text([_entry(text="x\ny\x00z")]) == "Touch ID to sign: np-1 x y z"


def test_text_never_begins_with_a_dash():
    assert ts.notice_text([_entry(text="-e evil")]).startswith("Touch ID")
    assert ts.notice_text([]) == "Touch ID to sign"


# --- post_notice ---------------------------------------------------------------------------------------

class Runner:
    def __init__(self, exc=None):
        self.calls, self.exc = [], exc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc:
            raise self.exc
        return "proc"


def test_post_notice_carries_the_text_as_the_last_argument_only(monkeypatch):
    monkeypatch.setattr(ts.sys, "platform", "darwin")
    hostile = 'x" & (do shell script "rm -rf ~") & "'
    r = Runner()
    assert ts.post_notice.real(hostile, runner=r) == "proc"
    argv, kw = r.calls[0]
    assert argv[0] == "/usr/bin/osascript" and argv[-1] == hostile
    assert not any(hostile in a for a in argv[:-1])
    assert kw["start_new_session"] is True
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL


def test_post_notice_is_darwin_only(monkeypatch):
    monkeypatch.setattr(ts.sys, "platform", "linux")
    r = Runner()
    assert ts.post_notice.real("x", runner=r) is None and r.calls == []


def test_post_notice_never_raises(monkeypatch):
    monkeypatch.setattr(ts.sys, "platform", "darwin")
    assert ts.post_notice.real("x", runner=Runner(OSError("no osascript"))) is None


# --- where the signer calls it -------------------------------------------------------------------------

def _answer(store, env, keydir, text="1a", **kw):
    return band_view._seat_answer(store, VIEWER, text, kw.pop("environ", env), None, session=SESSION,
                                  signer_factory=kw.pop("factory", real_factory), key_dir=keydir, **kw)


def test_a_signing_answer_posts_one_notice_before_the_sign(qdir, env, keydir, monkeypatch):
    store = _seen_store(qdir, env)
    order = []
    posted = ts.post_notice.posted
    monkeypatch.setattr(ts, "post_notice", lambda text, **kw: (order.append("notice"), posted.append(text)))

    def factory(path, timeout=None):
        order.append("sign")
        return real_factory(path, timeout)
    _answer(store, env, keydir, factory=factory)
    assert order == ["notice", "sign"]
    assert len(posted) == 1 and posted[0].startswith("Touch ID to sign: ") and "(a) fixed 30 s" in posted[0]


def _seen_store(qdir, env):
    from levain.consent.questions import QuestionStore
    store = QuestionStore(qdir)
    ask(store, "Which?")
    out, commit = band_view.hook_prompt({"session_id": SESSION, "prompt": "hello"}, env)
    commit()
    return store


def test_no_key_and_remote_post_nothing(tmp_path, qdir, env, keydir):
    store = _seen_store(qdir, env)
    empty = tmp_path / "empty-keys"
    empty.mkdir(mode=0o700)
    _answer(store, env, empty)
    assert ts.post_notice.posted == []
    store2_env = dict(env, SSH_CONNECTION="1.2.3.4 1 5.6.7.8 22")
    _answer(store, env, keydir, environ=store2_env)
    assert ts.post_notice.posted == []


def test_a_failing_key_lookup_posts_nothing_and_the_answer_is_still_recorded(qdir, env, keydir, monkeypatch):
    store = _seen_store(qdir, env)
    monkeypatch.setattr(ts, "find_key", lambda *a: (_ for _ in ()).throw(KeyError_("open dir")))
    echo = _answer(store, env, keydir)
    assert ts.post_notice.posted == [] and echo


def test_the_on_sign_seam_is_kept(qdir, env, keydir):
    store = _seen_store(qdir, env)
    hits = []
    _answer(store, env, keydir, on_sign=lambda: hits.append(1))
    assert hits == [1] and len(ts.post_notice.posted) == 1
