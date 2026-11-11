"""TERMSIGN L2: a band answer typed at a seat is signed with the laptop key, in the same write that records it.

No test reaches the Secure Enclave: the key is a plain ed25519 key made with ssh-keygen into a tmp dir, and the
signer factory is injected (a real ``ssh-keygen -Y sign`` over it, or a fake that raises one of the six reasons).
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from levain import band_hook, band_view
from levain.cockpit import keys as ckeys
from levain.cockpit.custody import APPLE_PROVIDER, SIDECAR_FORMAT, SIDECAR_SUFFIX
from levain.cockpit.keys import KeyError_
from levain.consent import band as consent_band
from levain.consent import terminal_sign as ts
from levain.band_shorthand import Action
from levain.consent.questions import Link, QuestionStore

OPTS = [("fix", "fixed 30 s"), ("exp", "exponential"), ("none", "fail loud")]
PLAIN_TERMINAL = [(1111, ["-zsh"]), (1000, ["/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"])]
VIEWER = "claude_code:seat-1"
SESSION = "seat-1"


@pytest.fixture(autouse=True)
def human_terminal(monkeypatch, tmp_path):
    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "CODEX_SANDBOX", "LEVAIN_TURN",
                 "SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(consent_band, "_ancestors", lambda: iter(PLAIN_TERMINAL))
    monkeypatch.setattr(ts, "_real_key_dir", lambda: tmp_path / "no-real-keys")

    def boom(path, timeout=None):
        raise AssertionError("a test reached the default (real) signer factory")
    monkeypatch.setattr(ts, "default_signer_factory", boom)


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir):
    return QuestionStore(qdir)


@pytest.fixture
def env(qdir):
    return {"LEVAIN_HOME": str(qdir.parent)}


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


def ask(store, what="Which?", **over):
    kw = dict(agent="claude_code", agent_session_id="sess-1", what=what, options=OPTS, rec="exp", blocks="the PR",
              cwd="/tmp/proj")
    kw.update(over)
    return store.file(**kw)


def seen(store, env, n=2):
    qs = [ask(store, f"question {i}") for i in range(1, n + 1)]
    out, commit = band_view.hook_prompt({"session_id": SESSION, "prompt": "hello"}, env)
    commit()
    assert f"band@{n}" in out
    return qs


def real_factory(path, timeout=None):
    return ckeys.ssh_signer(path, namespace=ts.NAMESPACE, timeout=timeout)


def sidecar(store):
    with sqlite3.connect(store.path) as c:
        return c.execute("SELECT q.n, x.via, x.sig IS NOT NULL, x.message IS NOT NULL, x.signer_fp, x.sign_state, "
                         "x.sign_reason FROM answer_context x JOIN questions q ON q.id = x.question_id "
                         "ORDER BY q.n").fetchall()


def recorded(store):
    with sqlite3.connect(store.path) as c:
        return c.execute("SELECT q.n, a.value FROM answers a JOIN questions q ON q.id = a.question_id "
                         "ORDER BY q.n").fetchall()


def seat(store, env, text, keydir, factory=real_factory, environ=None, **kw):
    return band_view._seat_answer(store, VIEWER, text, environ if environ is not None else env, None, session=SESSION,
                                  signer_factory=factory, key_dir=keydir, **kw)


# --- the real path: a signed answer, read back ---------------------------------------------------------

def test_signed_answer_records_sidecar_and_verifies(store, env, keydir):
    qs = seen(store, env)
    calls = []

    def factory(path, timeout=None):
        calls.append(path)
        return real_factory(path, timeout)

    echo = seat(store, env, "1a, 2b because cheaper", keydir, factory)
    assert len(calls) == 1                                                       # ONE signature for the whole line
    assert "recorded: 1a (you, seat), 2b (you, seat)" in echo
    assert "signed (Touch ID, laptop): 1a, 2b" in echo and "UNSIGNED" not in echo
    rows = sidecar(store)
    assert [(r[0], r[1], r[2], r[3], r[5], r[6]) for r in rows] == [(1, "seat", 1, 1, "signed", None),
                                                                      (2, "seat", 1, 1, "signed", None)]
    assert recorded(store) == [(1, "fix"), (2, "exp")]
    # the stored bytes verify, name both rows at the versions read for the plan, and one row's record stands alone
    rec1, rec2 = (band_view.answer_sign_of(store, q.id) for q in qs)
    assert rec1["message"] == rec2["message"] and rec1["sig"] == rec2["sig"] and rec1["signer_fp"]
    pub = " ".join((keydir / (ts.KEY_LABEL + ".pub")).read_text().split()[:2])
    got = ts.verify(rec1["message"].encode(), rec1["sig"], f'phill namespaces="{ts.NAMESPACE}" {pub}\n', "phill",
                    ctk=lambda key: (None, "not checked"))
    assert got.ok, got.problem
    parsed = ts.parse_verified(rec1["message"].encode())
    assert [(e.item, e.row_version, e.kind, e.answer, e.note) for e in parsed.entries] == [
        (qs[0].id, band_view.subject_version(qs[0]), "information", "(a) fixed 30 s", None),
        (qs[1].id, band_view.subject_version(qs[1]), "information", "(b) exponential", "cheaper")]
    assert parsed.store.startswith("levain-questions:") and parsed.session == SESSION
    assert parsed.typed == "1a, 2b because cheaper"


def test_free_text_entry_is_signed_as_text(store, env, keydir):
    qs = seen(store, env, 1)
    seat(store, env, "1: use a lock file", keydir)
    rec = band_view.answer_sign_of(store, qs[0].id)
    parsed = ts.parse_verified(rec["message"].encode())
    assert (parsed.entries[0].text, parsed.entries[0].answer) == ("use a lock file", None)


# --- the six reasons ----------------------------------------------------------------------------------

def _raises(msg):
    def factory(path, timeout=None):
        def signer(message):
            raise KeyError_(msg)
        return "SHA256:" + "A" * 43, signer
    return factory


REASON_CASES = [
    ("no-key", None, "terminal signing is not set up"),
    ("remote", None, "the desk is remote"),
    ("unavailable", _raises("ssh-keygen could not sign with key (exit 255)"), "Touch ID was unavailable"),
    ("cancelled", _raises("Touch ID was cancelled or did not complete"), "you cancelled Touch ID"),
    ("timeout", _raises("TimeoutExpired: ssh-keygen ran out of time"), "no touch in time"),
    ("signer-error", _raises("provider gone"), "the signer failed: provider gone"),
]


@pytest.mark.parametrize("reason,factory,words", REASON_CASES, ids=[c[0] for c in REASON_CASES])
def test_each_reason_records_the_answer_unsigned_and_the_echo_says_why(store, env, keydir, tmp_path, reason, factory,
                                                                        words):
    seen(store, env)
    kw: dict = {}
    key_dir = keydir
    if reason == "no-key":
        key_dir = tmp_path / "empty-keys"
        key_dir.mkdir(mode=0o700)
    if reason == "remote":
        kw["environ"] = {**env, "SSH_CONNECTION": "10.0.0.2 5555 10.0.0.1 22"}
    echo = seat(store, env, "1a, 2b", key_dir, factory or real_factory, **kw)
    assert recorded(store) == [(1, "fix"), (2, "exp")]                            # recording is never blocked
    assert [(r[2], r[3], r[5], r[6]) for r in sidecar(store)] == [(0, 0, "unsigned", reason)] * 2
    assert f"recorded UNSIGNED ({words}" in echo and "1a, 2b" in echo


def test_a_signer_that_raises_anything_never_blocks_the_answer(store, env, keydir):
    seen(store, env, 1)

    def factory(path, timeout=None):
        raise RuntimeError("boom\nsecond line")
    echo = seat(store, env, "1a", keydir, factory)
    assert recorded(store) == [(1, "fix")]
    assert sidecar(store)[0][5:] == ("unsigned", "signer-error")
    assert "recorded UNSIGNED (the signer failed: RuntimeError: boom)" in echo


def test_a_message_that_cannot_be_built_is_unsigned_signer_error(store, env, keydir):
    seen(store, env, 1)
    echo = band_view._seat_answer(store, VIEWER, "1a", env, None, session="not a session id!", key_dir=keydir,
                                  signer_factory=real_factory)                      # a field out of its form
    assert recorded(store) == [(1, "fix")]
    assert sidecar(store)[0][5:] == ("unsigned", "signer-error")
    assert "recorded UNSIGNED" in echo


def test_not_set_up_is_said_once_per_viewer(store, env, tmp_path):
    seen(store, env)
    empty = tmp_path / "empty-keys"
    empty.mkdir(mode=0o700)
    first = seat(store, env, "1a", empty)
    second = seat(store, env, "2b", empty)
    assert "terminal signing is not set up" in first
    assert "UNSIGNED" not in second and "recorded: 2b (you, seat)" in second
    assert [r[6] for r in sidecar(store)] == ["no-key", "no-key"]                 # both rows still say why


# --- skipped where the seat path is skipped; authority stays as it is --------------------------------

def test_relay_turn_is_neither_parsed_nor_signed(store, env, keydir):
    seen(store, env, 1)

    def factory(path, timeout=None):
        raise AssertionError("signed a relay turn")
    for text, environ in (("1a", {**env, "LEVAIN_TURN": "relay"}), ("levain: an answer was recorded 1a", env)):
        assert seat(store, env, text, keydir, factory, environ=environ) == ""
    assert recorded(store) == [] and sidecar(store) == []


def test_no_session_means_nothing_is_signed_and_no_sign_columns_are_written(store, env, keydir):
    seen(store, env, 1)
    echo = band_view._seat_answer(store, VIEWER, "1a", env, None)
    assert "recorded: 1a (you, seat)" in echo and "Touch ID" not in echo and "UNSIGNED" not in echo
    assert sidecar(store)[0][5:] == (None, None)


def test_an_authority_item_is_not_signed_here(store, env, keydir):
    link = Link(option="fix", kind="push", request_id="req-1", queue_id="q-1")
    q = ask(store, "push it?", link=link)
    assert band_view._question_kind(q) == "authority"
    sign_with = band_view._terminal_signer(store, VIEWER, "1a", band_view.parse("1a", None, {}, None), SESSION, {})
    assert sign_with([(Action(1, pick="a"), q)]) is None


# --- the row moved between the message and the write ---------------------------------------------------

def test_a_row_that_moved_after_the_message_is_refused_never_signed_over(store, env, keydir, monkeypatch):
    """Question rows are append-only today, so a move is simulated at the version function: the write's own read of
    the version differs from the one the message names."""
    seen(store, env)
    real = band_view.subject_version
    state = {"moved": False}

    def version(q):
        got = real(q)
        return got + "-moved" if state["moved"] and q.n == 2 else got
    monkeypatch.setattr(band_view, "subject_version", version)

    def factory(path, timeout=None):
        fp, signer = real_factory(path, timeout)

        def moved(message):
            sig = signer(message)
            state["moved"] = True                                                  # the row changes while he touches
            return sig
        return fp, moved
    echo = seat(store, env, "1a, 2b", keydir, factory)
    assert recorded(store) == [(1, "fix")]                                        # item 2 was not recorded
    assert "refused: 2:" in echo and "nothing recorded for it" in echo
    assert [r[0] for r in sidecar(store)] == [1]


def test_an_unsigned_answer_does_not_check_the_version(store, env, tmp_path, monkeypatch):
    """Only a SIGNED entry is held to the version its message names; an unsigned one records as it always did."""
    seen(store, env, 1)
    empty = tmp_path / "empty-keys"
    empty.mkdir(mode=0o700)
    monkeypatch.setattr(band_view, "subject_version", lambda q: "v-" + str(id(q)))
    assert "recorded: 1a (you, seat)" in seat(store, env, "1a", empty)


# --- the store bound -----------------------------------------------------------------------------------

def test_signing_holds_no_store_lock_and_the_write_gets_its_own_bound(store, env, keydir):
    seen(store, env, 1)
    probes = []

    def factory(path, timeout=None):
        fp, signer = real_factory(path, timeout)

        def slow(message):
            c = sqlite3.connect(store.path, timeout=0.05, isolation_level=None)
            try:
                c.execute("BEGIN IMMEDIATE")                                       # another writer is not locked out
                c.execute("ROLLBACK")
                probes.append("free")
            finally:
                c.close()
            time.sleep(0.25)                                                       # longer than the bound given below
            return signer(message)
        return fp, slow
    deadline = time.monotonic() + 0.1                                              # the hook's bound, spent while signing
    echo = band_view._seat_answer(store, VIEWER, "1a", env, deadline, session=SESSION, signer_factory=factory,
                                  key_dir=keydir)
    assert probes == ["free"] and "signed (Touch ID, laptop): 1a" in echo
    assert recorded(store) == [(1, "fix")]


def test_the_hook_renders_after_a_slow_sign_and_names_the_state(store, env, keydir, monkeypatch):
    seen(store, env, 2)
    monkeypatch.setattr(ts, "_real_key_dir", lambda: keydir)

    def factory(path, timeout=None):
        fp, signer = real_factory(path, timeout)

        def slow(message):
            time.sleep(0.4)
            return signer(message)
        return fp, slow
    monkeypatch.setattr(ts, "default_signer_factory", factory)
    monkeypatch.setattr(band_view, "HOOK_BUSY_MS", 100)                            # the sign step outlasts the hook bound
    out, commit = band_view.hook_prompt({"session_id": SESSION, "prompt": "1a"}, env)
    commit()
    assert "recorded: 1a (you, seat)" in out and "signed (Touch ID, laptop): 1a" in out
    assert "not read" not in out and "NEEDS YOU" in out                            # item 2 still shown
    # the installed entry carries the raised bound: a touch can take longer than the old ten seconds
    assert band_hook._entry("/usr/bin/python3", "UserPromptSubmit")["hooks"][0]["timeout"] == band_hook.PROMPT_HOOK_TIMEOUT_S
    assert band_hook._entry("/usr/bin/python3", "Stop")["hooks"][0]["timeout"] == 10


# --- the schema: a store made before the columns ------------------------------------------------------

OLD_ANSWER_CONTEXT = (
    "CREATE TABLE IF NOT EXISTS answer_context (question_id TEXT PRIMARY KEY REFERENCES answers(question_id), "
    "via TEXT NOT NULL CHECK (via IN ('seat','cli','cockpit','phone')), viewer TEXT, seen_through INTEGER, note TEXT)",
)


def _old_store(store):
    """The question store as it was before the signing columns: answer_context rebuilt in its old shape."""
    triggers = [s for s in band_view.BAND_SCHEMA if "TRIGGER" in s and "answer_context" in s]
    with sqlite3.connect(store.path, isolation_level=None) as c:
        c.execute("DROP TRIGGER IF EXISTS answer_context_frozen")
        c.execute("DROP TRIGGER IF EXISTS answer_context_kept")
        c.execute("DROP TABLE answer_context")
        for stmt in OLD_ANSWER_CONTEXT + tuple(triggers):
            c.execute(stmt)


def test_an_old_store_migrates_and_the_new_columns_are_write_once(store, env, keydir):
    first, second = seen(store, env)[:2]
    _old_store(store)
    # an answer by the OLD path first
    store.answer_with_context([{"ref": first.id, "option": "fix", "via": "cli", "viewer": "cli", "seen_through": 1,
                                "note": None}], channel="cli", schema=(), deadline=None)
    with sqlite3.connect(store.path) as c:
        assert "sign_state" not in {r[1] for r in c.execute("PRAGMA table_info(answer_context)")}
    assert band_view.answer_sign_of(store, first.id) is None or \
        band_view.answer_sign_of(store, first.id)["sign_state"] is None
    seat(store, env, "2b", keydir)                                                  # then a signed one: migrates
    with sqlite3.connect(store.path) as c:
        assert {"sig", "message", "signer_fp", "sign_state", "sign_reason"} <= {
            r[1] for r in c.execute("PRAGMA table_info(answer_context)")}
    assert band_view.answer_sign_of(store, first.id)["sign_state"] is None          # the old row reads unattempted
    assert band_view.answer_sign_of(store, second.id)["sign_state"] == "signed"
    seat(store, env, "hello", keydir)                                               # idempotent: no second ALTER error
    with sqlite3.connect(store.path) as c:
        for col in ("sig", "message", "signer_fp", "sign_state", "sign_reason", "note"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                c.execute(f"UPDATE answer_context SET {col} = 'x' WHERE question_id = ?", (second.id,))
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("DELETE FROM answer_context WHERE question_id = ?", (second.id,))


def test_a_new_store_has_the_columns_from_the_start(store, env, keydir):
    seen(store, env, 1)
    seat(store, env, "1a", keydir)
    with sqlite3.connect(store.path) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(answer_context)")]
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("UPDATE answer_context SET sig = 'forged'")
    assert cols[-5:] == ["sig", "message", "signer_fp", "sign_state", "sign_reason"]


# --- L3 r1 fixes ---------------------------------------------------------------------------------------

TEMPLATES = Path(band_view.__file__).parent / "templates"
CLAUDE_CODE_DEFAULT_HOOK_TIMEOUT_S = 60       # Claude Code's own bound for a command hook that names none


def _prompt_hook_timeouts(doc):
    """Every UserPromptSubmit handler's timeout in a hooks document (``None`` where the handler names none)."""
    return [h.get("timeout") for entry in (doc.get("hooks") or {}).get("UserPromptSubmit") or []
            for h in entry.get("hooks") or []]


def _wrapper_bounds_s(script):
    """The codex wrapper's own bounded calls around the band (its anneal-memory queries), read from its source."""
    import re
    text = script.read_text()
    return sum(float(re.search(rf"^{name} = ([0-9.]+)", text, re.M).group(1))
               for name in ("WRAP_CHECK_TIMEOUT", "INTENTION_CHECK_TIMEOUT", "CRYSTAL_CHECK_TIMEOUT"))


def test_every_installed_prompt_hook_outlasts_the_whole_signing_turn(tmp_path):
    """codex HIGH r1: the codex install's hooks.json kept 10 s while the sign step may take 20. Every install path that
    can run :func:`band_view.hook_prompt` is enumerated (each template that registers a UserPromptSubmit, the codex
    one as install renders it, and the user-level band entry) and each carries ONE number, which budgets the band's
    worst turn (read, sign, write, render) plus the codex wrapper's own bounded calls."""
    from levain import install as inst

    need = (ts.SIGN_BOUND_S + 3 * band_view.HOOK_BUSY_MS / 1000
            + _wrapper_bounds_s(TEMPLATES / "adapters" / "codex" / "activation" / "hooks" / "user_prompt_submit.py"))
    assert band_hook.PROMPT_HOOK_TIMEOUT_S >= need + 5                              # 5 s for interpreter start-up
    found = {}
    for path in sorted(TEMPLATES.rglob("*.json*")):
        try:
            doc = json.loads(path.read_text())
        except ValueError:
            continue
        if isinstance(doc, dict) and _prompt_hook_timeouts(doc):
            found[path.relative_to(TEMPLATES).as_posix()] = _prompt_hook_timeouts(doc)
    assert set(found) == {"adapters/codex/hooks.json.template", "adapters/claude-code/settings.template.json"}
    assert found["adapters/codex/hooks.json.template"] == [band_hook.PROMPT_HOOK_TIMEOUT_S]
    # the claude-code install names no timeout: Claude Code's default bound applies, and it is longer still
    assert found["adapters/claude-code/settings.template.json"] == [None]
    assert CLAUDE_CODE_DEFAULT_HOOK_TIMEOUT_S >= band_hook.PROMPT_HOOK_TIMEOUT_S
    rendered = json.loads(inst._codex_hooks_json(TEMPLATES / "adapters" / "codex", "/usr/bin/python3", tmp_path))
    assert _prompt_hook_timeouts(rendered) == [band_hook.PROMPT_HOOK_TIMEOUT_S]
    entry = band_hook._entry("/usr/bin/python3", "UserPromptSubmit")
    assert _prompt_hook_timeouts({"hooks": {"UserPromptSubmit": [entry]}}) == [band_hook.PROMPT_HOOK_TIMEOUT_S]


def test_the_signed_view_is_the_one_the_line_was_parsed_against(store, env, keydir, monkeypatch):
    """codex HIGH r1: another hook of the same viewer commits a newer view between the parse and the sign. The message
    names the view the plan was parsed against, never the newest one re-read at sign time."""
    seen(store, env, 2)
    v1 = band_view._newest_view(store, VIEWER)[1]
    real = band_view._plan_on

    def plan_then_another_view(*a, **kw):
        got = real(*a, **kw)
        out, commit = band_view.hook_prompt({"session_id": SESSION, "prompt": "hello"}, env)   # another tab, same viewer
        commit()
        return got
    monkeypatch.setattr(band_view, "_plan_on", plan_then_another_view)
    echo = seat(store, env, "1a", keydir)
    assert band_view._newest_view(store, VIEWER)[1] != v1                          # the race happened
    assert "signed (Touch ID, laptop): 1a" in echo
    q1 = band_view._items_of(store, band_view._view_by_id(store, VIEWER, v1))[1]
    assert ts.parse_verified(band_view.answer_sign_of(store, q1.id)["message"].encode()).view == str(v1)


def _signed_stub(pairs):
    return band_view.Signing("signed", None, "", "m", "s", "SHA256:" + "A" * 43,
                             {q.id: band_view.subject_version(q) for _, q in pairs})


def test_require_is_run_on_the_signed_path_too(store, env):
    """MED r1: a caller passing both ``require`` and ``sign_with`` had the precondition skipped and the answer stored."""
    seen(store, env, 1)
    plan, view, questions, extras = band_view.plan_for_full(store, VIEWER, "1a")

    def stale(conn):
        raise band_view.StaleBand("stale")
    with pytest.raises(band_view.StaleBand):
        band_view.apply_plan(store, plan, questions, viewer=VIEWER, via="seat", seen_through=view.seen_through,
                             extras=extras, require=stale, sign_with=_signed_stub)
    assert recorded(store) == []
    ran = []
    band_view.apply_plan(store, plan, questions, viewer=VIEWER, via="seat", seen_through=view.seen_through,
                         extras=extras, require=lambda conn: ran.append(conn), sign_with=_signed_stub)
    assert len(ran) == 1 and recorded(store) == [(1, "fix")]


@pytest.mark.parametrize("over", [{"viewer": "v\x00 forged"}, {"seen_through": -1}, {"seen_through": True},
                                  {"via": "agent"}], ids=["nul-viewer", "negative-seen", "bool-seen", "bad-via"])
def test_the_signed_path_validates_provenance_as_the_unsigned_one_does(store, env, over):
    """MED r1: the signed write skipped answer_with_context's checks of viewer, seen_through and via."""
    seen(store, env, 1)
    plan, view, questions, extras = band_view.plan_for_full(store, VIEWER, "1a")
    kw = {"viewer": VIEWER, "via": "seat", "seen_through": view.seen_through, **over}
    unsigned = band_view.apply_plan(store, plan, questions, extras=extras, **kw)
    assert unsigned.recorded == [] and recorded(store) == []
    signed = band_view.apply_plan(store, plan, questions, extras=extras, sign_with=_signed_stub, **kw)
    assert signed.recorded == [] and signed.refused and recorded(store) == [] and sidecar(store) == []


def test_the_render_bound_is_rearmed_after_any_sign_step(store, env, keydir, monkeypatch):
    """MED r1: the render's bound was re-armed only when signing had used ALL of the hook's; a touch that left 0.1 s
    gave the render 0.1 s. After a sign step the render always gets a full bound from a fresh reading."""
    seen(store, env, 1)
    monkeypatch.setattr(ts, "_real_key_dir", lambda: keydir)
    monkeypatch.setattr(band_view, "HOOK_BUSY_MS", 1000)

    def factory(path, timeout=None):
        fp, signer = real_factory(path, timeout)

        def slow(message):
            time.sleep(0.5)                                                        # half the bound, not all of it
            return signer(message)
        return fp, slow
    monkeypatch.setattr(ts, "default_signer_factory", factory)
    real = band_view._hook_render
    left = []

    def render(*a, **kw):
        left.append(a[-1] - time.monotonic())
        return real(*a, **kw)
    monkeypatch.setattr(band_view, "_hook_render", render)
    out, commit = band_view.hook_prompt({"session_id": SESSION, "prompt": "1a"}, env)
    assert "signed (Touch ID, laptop): 1a" in out
    assert left and left[0] > 0.9


def test_signing_versions_cannot_be_changed_after_the_fact():
    s = band_view.Signing("signed", versions={"q": "v"})
    with pytest.raises(TypeError):
        s.versions["q"] = "other"
