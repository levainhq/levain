# ruff: noqa: F811
"""Board row 64, remote governance B2 (project_memory/1106-16/r64/DESIGN_B2.md): the sign card for a band answer typed
at a remote terminal. Ruling 103a stands: the answer records at once, unsigned; the card is evidence only.

No real push, phone, Touch ID or live queue: the signing queue is a tmp dir, the passkey a software P-256 key, the
push child a recorder."""
from __future__ import annotations

import re
import json
import sqlite3
from pathlib import Path

import pytest

from levain import band_view
from levain import webpush
from levain.consent import answer_countersign as ac
from levain.consent import terminal_sign as ts
from levain.consent.cockpit_origin import write_origin
from levain.consent.sign import Signer, read_request, sign_dir
from levain.contracts.namespaces import ANSWER_COUNTERSIGN_NAMESPACE as NS, LEVAIN_SIGN_REQUESTS
from levain.webpush import sender
from tests.test_cockpit_sign_origin import RP, _Passkey
from tests.test_remote_detect_1105 import DESK, SID, SSH_ATTACH_ROWS, use_ancestry, use_ps
from tests.test_termsign_l2_seat import (ask, env, human_terminal, keydir, qdir, real_factory, recorded,  # noqa: F401
                                         seat, seen, sidecar, store)

ORIGIN = f"https://{RP}"


@pytest.fixture
def signq(tmp_path: Path) -> Path:
    d = tmp_path / "signq"
    d.mkdir(mode=0o700)
    assert write_origin(d, ORIGIN) is not None
    return d


@pytest.fixture
def pushes(monkeypatch) -> list[dict]:
    sent: list[dict] = []
    monkeypatch.setattr(webpush, "live", lambda q: True)
    monkeypatch.setattr(sender, "spawn", lambda q, **kw: sent.append({"queue": Path(q), **kw}) or True)
    return sent


def remote_line(monkeypatch, store, env, keydir, text, signq):
    """A line typed at a desk attached from ssh: recorded unsigned-remote, the sheet never reached."""
    use_ancestry(monkeypatch, DESK)
    use_ps(monkeypatch, SSH_ATTACH_ROWS)
    environ = {**env, "CLAUDE_CODE_SESSION_ID": SID, "SSH_AUTH_SOCK": "/x", "P9K_SSH": "1"}
    if signq is not None:
        environ["LEVAIN_SIGN_QUEUE_DIR"] = str(signq)

    def sheet(path, timeout=None):
        raise AssertionError("the Touch ID sheet was reached on a remote desk")
    return band_view._seat_answer(store, "claude_code:seat-1", text, environ, None, session=SID, signer_factory=sheet,
                                  key_dir=keydir)


def cards(signq: Path) -> list[Path]:
    return sorted(sign_dir(signq).glob("lv-*.json"))


def pointers(store) -> list[tuple]:
    with sqlite3.connect(store.path) as c:
        return c.execute("SELECT q.n, p.request_id FROM answer_countersign p JOIN questions q ON q.id = p.question_id "
                         "ORDER BY q.n").fetchall()


def sign_card(signq: Path, pk: _Passkey, rid: str) -> None:
    s = Signer(signq, (), RP, lambda: {pk.key.fingerprint: pk.key})
    ch = s.challenge(rid, {"signer_fp": pk.key.fingerprint, "request_sha256": read_request(signq, rid).sha256})
    s.answer(rid, pk.assertion(ch["challenge"], ORIGIN))


# --- 1. one card per typed line ----------------------------------------------------------------------------------

def test_a_remote_line_files_one_card_for_all_its_answers_and_a_rerun_files_none(monkeypatch, store, env, keydir,
                                                                                  signq, pushes):
    qs = seen(store, env, 2)
    spied: list[tuple] = []
    real = ac.file_card
    monkeypatch.setattr(ac, "file_card", lambda *a, **k: spied.append((a, k)) or real(*a, **k))
    echo = remote_line(monkeypatch, store, env, keydir, "1a 2a", signq)
    assert recorded(store) == [(1, "fix"), (2, "fix")]                        # 103a: recorded at once
    assert [(r[5], r[6]) for r in sidecar(store)] == [("unsigned", "remote")] * 2
    assert "a sign card is on its way: sign it on your phone" in echo
    [path] = cards(signq)
    req = read_request(signq, path.stem)
    assert req.refused is None and req.namespace == NS and req.requester == "levain"
    assert re.fullmatch(LEVAIN_SIGN_REQUESTS[NS], req.message)
    parsed = ts.parse_verified(req.message.encode())
    stamps = {store.get(q.id).answer.answered_at for q in qs}
    assert len(stamps) == 1 and parsed.countersign_of == stamps.pop()
    assert [e.item for e in parsed.entries] == [q.id for q in qs]
    assert pointers(store) == [(1, path.stem), (2, path.stem)]
    assert len(pushes) == 1 and pushes[0]["queue"] == signq and pushes[0]["url"] == f"{ORIGIN}/sign/{path.stem}"
    # filing the same line again files nothing new (the card is a pure function of the recorded line)
    (args, kw), = spied
    again = real(*args, **kw)
    assert again.request_id == path.stem and cards(signq) == [path]


def test_a_busy_store_bounds_the_card_and_the_echo_says_why(monkeypatch, store, env, keydir, signq, pushes):
    """L3 r1 (codex HIGH, reproduced: 29.96 s): the pointers' write ran on the store's 30 s busy wait, after the
    answer was already recorded. Now it waits until its deadline and the result says the store stayed busy."""
    import time

    seen(store, env, 1)
    spied: list[tuple] = []
    real = ac.file_card
    monkeypatch.setattr(ac, "file_card", lambda *a, **k: spied.append((a, k)) or real(*a, **k))
    remote_line(monkeypatch, store, env, keydir, "1a", signq)
    (args, kw), = spied
    holder = sqlite3.connect(store.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        t0 = time.monotonic()
        got = real(*args, **{**kw, "deadline": time.monotonic() + 0.5})
        took = time.monotonic() - t0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert took < 5 and not got.sent and got.why == "the question store stayed busy"


# --- 2. the deny path, and the absent card never reads as signed -------------------------------------------------

def test_deny_disowns_and_a_missing_or_unsent_card_is_never_countersigned(monkeypatch, store, env, keydir, signq,
                                                                           pushes):
    qs = seen(store, env, 3)
    remote_line(monkeypatch, store, env, keydir, "1a", signq)
    [path] = cards(signq)
    pk = _Passkey()
    ask_v = lambda q: ac.verdict(store, q.id, sign_queue=signq, keys=lambda: {pk.key.fingerprint: pk.key})  # noqa: E731
    assert ask_v(qs[0]).state == "open" and ac.badge_words(ask_v(qs[0])) == "remote, unsigned"
    Signer(signq, (), RP, lambda: {pk.key.fingerprint: pk.key}).deny(path.stem,
                                                                      {"request_sha256": read_request(signq, path.stem).sha256})
    v = ask_v(qs[0])
    assert v.state == "disowned" and ac.badge_words(v) == "DISOWNED on your phone"
    # no pointer (a question that was never carded) -> no-card; the card file removed -> failed
    assert ask_v(qs[1]).state == "no-card"
    path.unlink()
    assert ask_v(qs[0]).state == "failed"
    for state in (ask_v(qs[0]), ask_v(qs[1])):
        assert state.state != "countersigned"
    # no signing queue named -> no card, and the echo says so; a push child that did not start -> no promise either
    echo = remote_line(monkeypatch, store, env, keydir, "2a", None)
    assert "no sign card was sent: no sign queue is named" in echo and "sign it on your phone" not in echo
    monkeypatch.setattr(sender, "spawn", lambda q, **kw: False)
    echo = remote_line(monkeypatch, store, env, keydir, "3a", signq)
    assert "no sign card was sent: the push could not be started" in echo and "sign it on your phone" not in echo
    assert recorded(store) == [(1, "fix"), (2, "fix"), (3, "fix")]


# --- 3. the verifier rejects what is not the phone's signature over this answer ----------------------------------

def test_the_verifier_rejects_a_bad_signature_and_accepts_the_genuine_one(monkeypatch, store, env, keydir, signq,
                                                                           pushes):
    qs = seen(store, env, 2)
    remote_line(monkeypatch, store, env, keydir, "1a", signq)
    [path] = cards(signq)
    rid = path.stem
    pk, stranger = _Passkey(), _Passkey()
    enrolled = lambda: {pk.key.fingerprint: pk.key}  # noqa: E731
    verdict = lambda q: ac.verdict(store, q.id, sign_queue=signq, keys=enrolled)  # noqa: E731
    # signed by a key that is not enrolled -> FAILED, never countersigned
    sign_card(signq, stranger, rid)
    v = verdict(qs[0])
    assert (v.state, v.reason) == ("failed", "not signed by an enrolled passkey")
    assert ac.badge_words(v).startswith("countersign FAILED")
    d = sign_dir(signq)
    sig_bytes, record = (d / f"{rid}.sig").read_text(), (d / f"{rid}.signed.json").read_text()
    # the genuine passkey's signature -> countersigned
    for f in (d / f"{rid}.sig", d / f"{rid}.signed.json"):
        f.unlink()
    sign_card(signq, pk, rid)
    v = verdict(qs[0])
    assert v.state == "countersigned" and ac.badge_words(v).startswith("countersigned on your phone")
    # an edited card message -> FAILED (the pointer holds the filed bytes' hash)
    original = path.read_text()
    path.write_text(original.replace("fixed 30 s", "fail loud"))
    assert verdict(qs[0]).state == "failed"
    path.write_text(original)
    assert verdict(qs[0]).state == "countersigned"
    # a signature over ANOTHER line's card does not count for this answer: point question 2 at question 1's card
    remote_line(monkeypatch, store, env, keydir, "2a", None)
    with sqlite3.connect(store.path) as c:
        sha, rv = c.execute("SELECT message_sha256, row_version FROM answer_countersign").fetchone()
        c.execute("INSERT INTO answer_countersign VALUES (?, ?, ?, ?, 'x')", (qs[1].id, rid, sha, rv))
    v = verdict(qs[1])
    assert (v.state, v.reason) == ("failed", "the card is for another answer")
    assert sig_bytes and record                                                  # the stranger's artifacts existed


def test_countersign_evidence_is_the_card_and_its_signature_only_once_countersigned(monkeypatch, store, env, keydir,
                                                                                    signq, pushes):
    qs = seen(store, env, 1)
    remote_line(monkeypatch, store, env, keydir, "1a", signq)
    [path] = cards(signq)
    rid = path.stem
    pk = _Passkey()
    enrolled = lambda: {pk.key.fingerprint: pk.key}  # noqa: E731
    got = lambda: ac.countersign_evidence(store, qs[0].id, sign_queue=signq, keys=enrolled)  # noqa: E731
    assert got() is None                                    # a card nobody signed is no evidence
    sign_card(signq, pk, rid)
    message, signature, fp = got()
    assert message == json.loads(path.read_text())["message"] and fp == pk.key.fingerprint and signature.startswith("-----BEGIN SSH")
    assert ac.countersign_evidence(store, qs[0].id, sign_queue=None, keys=enrolled) is None
    path.write_text(path.read_text().replace("fixed 30 s", "fail loud"))
    assert got() is None                                    # an edited card is not the one filed


# --- 4. the local answer path is unchanged ----------------------------------------------------------------------

def test_a_local_signed_line_files_no_card_and_reads_as_before(monkeypatch, store, env, keydir, signq, pushes):
    qs = seen(store, env, 1)
    echo = seat(store, env, "1a", keydir, environ={**env, "LEVAIN_SIGN_QUEUE_DIR": str(signq)})
    assert "signed (Touch ID, laptop): 1a" in echo and "sign card" not in echo and "UNSIGNED" not in echo
    assert [(r[5], r[6]) for r in sidecar(store)] == [("signed", None)]
    assert cards(signq) == [] and pushes == []
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT COUNT(*) FROM answer_countersign").fetchone() == (0,)
    assert ac.verdict(store, qs[0].id, sign_queue=signq).state == "no-card"
    assert band_view._remote_badge(store, qs[0].id) == ""                      # a signed row reads no remote badge


# --- signpush L3 rulings S1/S3 (11-08): the card's push opens a Sign button in the center ------------------------

def test_s3_the_cards_push_is_a_sign_request_ping_the_center_draws_open(monkeypatch, store, env, keydir, signq, pushes):
    """S3: answer_countersign's push goes through sender.spawn_sign_request (the real one: only spawn is recorded),
    with ref == data.sign_id == the filed request id; stored as `send` stores it, the center lists that entry with an
    OPEN sign state (the Sign button), not text-only."""
    from levain.webpush import center
    from levain.webpush.store import PushStore

    via: list[str] = []
    real = sender.spawn_sign_request
    monkeypatch.setattr(sender, "spawn_sign_request", lambda q, rid, **kw: via.append(rid) or real(q, rid, **kw))
    seen(store, env, 1)
    remote_line(monkeypatch, store, env, keydir, "1a", signq)
    [path] = cards(signq)
    rid = path.stem
    assert via == [rid]
    [p] = pushes
    assert p["kind"] == "attention" and p["ref"] == rid and p["data"]["sign_id"] == rid and p["data"]["id"] == rid, p
    ps = PushStore(signq)
    ps.add_ping(p["kind"], p["ref"], p["title"], p["body"], data=p["data"])
    [item] = center.list_items(ps)["items"]
    n = item["needs"]
    assert item["source"] == "needs" and n["sign_id"] == rid and n["sign_state"] == "open" and n["sign_v"], item


@pytest.mark.parametrize("body", ["", "   \n"])
def test_s1_a_blank_body_still_names_the_request_so_the_button_stays(monkeypatch, signq, pushes, body):
    """S1: a blank ``what`` makes the center drop the ping's data, and with it the Sign button: the title stands in."""
    from levain.webpush import center

    rid = "lv-0123456789abcdef"
    assert sender.spawn_sign_request(signq, rid, title=ac.CARD_TITLE, body=body, url=f"{ORIGIN}/sign/{rid}")
    [p] = pushes
    assert p["data"]["what"] == ac.CARD_TITLE
    assert center.needs_data(__import__("json").dumps(p["data"]), rid) is not None
