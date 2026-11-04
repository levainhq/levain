"""TERMSIGN L5a: the desk format (golden bytes, strict parse), its namespace, and the classifier's channel dispatch.

No test reaches a Secure Enclave key: the keys are plain ed25519 keys from ssh-keygen in a tmp dir, signers are real
``ssh-keygen -Y sign`` over them, and both real signer factories raise if reached.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.cockpit import keys as ckeys
from levain.cockpit.custody import APPLE_PROVIDER, SIDECAR_FORMAT, SIDECAR_SUFFIX
from levain.consent import terminal_sign as ts

AT = datetime(2026, 11, 3, 21, 4, 11, tzinfo=timezone.utc)
NONCE = "0123456789abcdef"
E = ts.Entry("np-9b9b9662", "rv-aaaa1111", "Add a Levain 1.0 row?", "information", answer="(a) Add a Levain 1.0 row")
BASE = dict(store="levain-questions:abc123", install="abc123", surface="web", origin="http://localhost:8765",
            nonce=NONCE, at=AT)


def _click(**kw) -> bytes:
    args = dict(BASE, view="item", gesture="click", entries=[E])
    args.update(kw)
    return ts.build_desk_message(**args)


@pytest.fixture(autouse=True)
def _no_real_key(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "_real_key_dir", lambda: tmp_path / "no-real-keys")

    def boom(path, timeout=None):
        raise AssertionError("a test reached a real signer factory")
    monkeypatch.setattr(ts, "default_signer_factory", boom)
    monkeypatch.setattr(ts, "default_desk_signer_factory", boom)


def _plain_key(d: Path) -> Path:
    d.mkdir(mode=0o700, exist_ok=True)
    path = d / ts.KEY_LABEL
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(path)], check=True)
    (d / (ts.KEY_LABEL + SIDECAR_SUFFIX)).write_text(json.dumps(
        {"format": SIDECAR_FORMAT, "custody": "touch-id", "provider": APPLE_PROVIDER, "ctk_hash": "A" * 40,
         "label": ts.KEY_LABEL}))
    return path


def _signers(path: Path, namespaces=(ts.NAMESPACE, ts.DESK_NAMESPACE)) -> str:
    pub = " ".join(Path(str(path) + ".pub").read_text().split()[:2])
    return f'phill namespaces="{",".join(namespaces)}" {pub}\n'


def _sign(path: Path, message: bytes, ns: str) -> str:
    return ckeys.ssh_signer(path, namespace=ns)[1](message)


# --- golden bytes ---------------------------------------------------------------------------------------

GOLDEN_CLICK = (
    "levain-desk-answer/1\n"
    "store: levain-questions:abc123\n"
    "install: abc123\n"
    "surface: web\n"
    "origin: http://localhost:8765\n"
    "view: item\n"
    "gesture: click\n"
    "nonce: 0123456789abcdef\n"
    "at: 2026-11-03T21:04:11Z\n"
    "entry: 1\n"
    "  item: np-9b9b9662\n"
    "  row_version: rv-aaaa1111\n"
    "  asked: Add a Levain 1.0 row?\n"
    "  kind: information\n"
    "  answer: (a) Add a Levain 1.0 row\n"
).encode()


def test_golden_click():
    assert _click() == GOLDEN_CLICK


def test_golden_typed():
    e2 = ts.Entry("np-2", "rv-bbbb2222", "Push it?", "authority", text="yes, push", ruling='{"verb":"allow"}')
    got = _click(view="7", gesture="typed", typed="1a, 2 yes, push", entries=[E, e2])
    want = (
        "levain-desk-answer/1\nstore: levain-questions:abc123\ninstall: abc123\nsurface: web\n"
        "origin: http://localhost:8765\nview: 7\ngesture: typed\n"
        "typed-sha256: " + hashlib.sha256(b"1a, 2 yes, push").hexdigest() + "\ntyped: 1a, 2 yes, push\n"
        "nonce: 0123456789abcdef\nat: 2026-11-03T21:04:11Z\n"
        "entry: 1\n  item: np-9b9b9662\n  row_version: rv-aaaa1111\n  asked: Add a Levain 1.0 row?\n"
        "  kind: information\n  answer: (a) Add a Levain 1.0 row\n"
        "entry: 2\n  item: np-2\n  row_version: rv-bbbb2222\n  asked: Push it?\n  kind: authority\n"
        "  text: yes, push\n  ruling: {\"verb\":\"allow\"}\n").encode()
    assert got == want


def test_golden_countersign():
    got = _click(countersign_of="2026-11-03T20:00:00Z", countersign="disown")
    assert got == GOLDEN_CLICK.replace(
        b"entry: 1\n", b"countersign-of: 2026-11-03T20:00:00Z\ncountersign: disown\nentry: 1\n")


def test_parse_roundtrip_click_typed_countersign():
    p = ts.parse_desk_verified(GOLDEN_CLICK)
    assert (p.surface, p.origin, p.view, p.gesture, p.typed, p.countersign) == ("web", "http://localhost:8765", "item",
                                                                                "click", None, None)
    assert p.entries == (E,) and p.message_sha256 == hashlib.sha256(GOLDEN_CLICK).hexdigest()
    t = ts.parse_desk_verified(_click(view="7", gesture="typed", typed="1a", entries=[E, E]))
    assert t.gesture == "typed" and t.typed == "1a" and len(t.entries) == 2
    c = ts.parse_desk_verified(_click(countersign_of="rv-x", countersign="confirm"))
    assert (c.countersign_of, c.countersign) == ("rv-x", "confirm")


@pytest.mark.parametrize("kw", [
    dict(gesture="click", entries=[E, E]),                          # a click answers exactly one entry
    dict(gesture="click", typed="1a"),                              # a click carries no typed line
    dict(gesture="typed", typed=None),                              # a typed gesture carries one
    dict(gesture="swipe"),
    dict(countersign_of="rv-x"),                                    # countersign-of without countersign
    dict(countersign="confirm"),
    dict(countersign_of="rv-x", countersign="maybe"),
    dict(origin="http://evil.example:8765"),
    dict(origin="https://localhost:8765"),
    dict(surface="a b"),
    dict(store="flow-needs-phill-x"),
    dict(entries=[ts.Entry("np-1", "rv-1", "q?", "information", answer="(a) x", note="n")]),   # no note line
])
def test_build_refuses(kw):
    with pytest.raises(ts.MessageError):
        _click(**kw)


@pytest.mark.parametrize("mutate", [
    lambda t: t.replace("surface: web\norigin: http://localhost:8765\n", "origin: http://localhost:8765\nsurface: web\n"),
    lambda t: t.replace("gesture: click\n", "gesture: click\ntyped-sha256: " + "0" * 64 + "\ntyped: 1a\n"),
    lambda t: t.replace("entry: 1\n", "entry: 1\n  note: n\n").replace("  note: n\n", "", 1) + "  note: n\n",
    lambda t: t + "entry: 2\n  item: np-2\n  row_version: rv-2\n  asked: q\n  kind: information\n  answer: (a) x\n",
    lambda t: t + "\n",
    lambda t: "levain-terminal-answer/1\n" + t.split("\n", 1)[1],
    lambda t: t.replace("origin: http://localhost:8765", "origin: http://evil.example"),
])
def test_parse_refuses(mutate):
    with pytest.raises(ts.MessageError):
        ts.parse_desk_verified(mutate(GOLDEN_CLICK.decode()).encode())


def test_a_terminal_message_is_not_a_desk_message_and_back():
    terminal = ts.build_message(store="flow-needs-phill", install="i-1", session="s-1", view="v-1", typed="1a",
                                entries=[E], nonce=NONCE, at=AT)
    with pytest.raises(ts.MessageError):
        ts.parse_desk_verified(terminal)
    with pytest.raises(ts.MessageError):
        ts.parse_verified(GOLDEN_CLICK)


# --- verify by namespace --------------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path):
    path = _plain_key(tmp_path / "keys")
    return path, _signers(path)


def test_desk_signature_verifies_in_the_desk_namespace_only(world):
    path, signers = world
    sig = _sign(path, GOLDEN_CLICK, ts.DESK_NAMESPACE)
    ok = lambda pk: (True, "bio")        # noqa: E731
    got = ts.verify(GOLDEN_CLICK, sig, signers, "phill", ctk=ok, namespace=ts.DESK_NAMESPACE)
    assert got.ok and got.badge == "signed: Touch ID, laptop (desk)"
    assert not ts.verify(GOLDEN_CLICK, sig, signers, "phill", ctk=ok).ok                      # the terminal namespace
    term = _sign(path, GOLDEN_CLICK, ts.NAMESPACE)
    assert not ts.verify(GOLDEN_CLICK, term, signers, "phill", ctk=ok, namespace=ts.DESK_NAMESPACE).ok
    assert not ts.verify(GOLDEN_CLICK, sig, _signers(path, (ts.NAMESPACE,)), "phill", ctk=ok,
                         namespace=ts.DESK_NAMESPACE).ok          # a signers line that does not list the desk
    assert not ts.verify(GOLDEN_CLICK, sig, signers, "phill", ctk=ok, namespace="flow-needs-phill").ok


# --- the classifier -------------------------------------------------------------------------------------

ROW = {"id": "np-9b9b9662", "row_version": "rv-aaaa1111", "what": "Add a Levain 1.0 row?"}
SCOPE = {"store": "levain-questions:abc123", "install": "abc123", "surface": "web", "view": "item"}
TSCOPE = {"store": "flow-needs-phill", "install": "i-1", "session": "s-1", "view": "v-1"}


def _ctx(signers, **kw):
    c = {"signers": signers, "principal": "phill", "ctk": lambda pk: (True, "bio"), **TSCOPE, **SCOPE}
    c.update(kw)
    return c


def _desk_event(path, message=GOLDEN_CLICK, **kw):
    ev = {"ts": "2026-11-03T21:04:30Z", "answer": "(a) Add a Levain 1.0 row", "via": "cockpit",
          "signed": {"format": ts.DESK_FORMAT, "message": message.decode(), "sig": _sign(path, message, ts.DESK_NAMESPACE),
                     "entry": 1}}
    ev.update(kw)
    return ev


def test_a_signed_desk_click_is_signed_terminal_on_the_desk_channel(world):
    path, signers = world
    got = ts.answer_class(_desk_event(path), ROW, _ctx(signers))
    assert got.state == "signed-terminal" and got.channel == "desk" and got.badge == "signed: Touch ID, laptop (desk)", got
    flow_label = _desk_event(path, via="cockpit-desk")
    assert ts.answer_class(flow_label, ROW, _ctx(signers)).state == "signed-terminal"


def test_a_terminal_signature_on_a_desk_answer_is_channel_mismatch(world):
    path, signers = world
    msg = ts.build_message(store="flow-needs-phill", install="i-1", session="s-1", view="v-1", typed="1a",
                           entries=[E], nonce=NONCE, at=AT)
    ev = {"ts": "2026-11-03T21:04:30Z", "answer": "(a) Add a Levain 1.0 row", "via": "cockpit",
          "signed": {"format": ts.FORMAT, "message": msg.decode(), "sig": _sign(path, msg, ts.NAMESPACE), "entry": 1}}
    got = ts.answer_class(ev, ROW, _ctx(signers))
    assert (got.state, got.reason) == ("failed", "channel-mismatch")
    assert ts.answer_class({**ev, "via": "cockpit-desk"}, ROW, _ctx(signers)).reason == "channel-mismatch"
    assert ts.answer_class({**ev, "via": "terminal"}, ROW, _ctx(signers, **TSCOPE)).state == "signed-terminal"   # today's path


def test_a_desk_signature_on_a_terminal_or_unlabelled_answer_is_channel_mismatch(world):
    path, signers = world
    for via in ("terminal", "seat", None):
        ev = _desk_event(path, via=via)
        if via is None:
            del ev["via"]
        assert ts.answer_class(ev, ROW, _ctx(signers)).reason == "channel-mismatch", via


def test_a_desk_message_signed_in_the_terminal_namespace_is_a_bad_signature(world):
    path, signers = world
    ev = _desk_event(path)
    ev["signed"]["sig"] = _sign(path, GOLDEN_CLICK, ts.NAMESPACE)
    assert ts.answer_class(ev, ROW, _ctx(signers)).reason == "bad-signature"


def test_a_bare_cockpit_desk_label_is_failed_not_agent_resolved(world):
    path, signers = world
    got = ts.answer_class({"ts": "2026-11-03T21:04:30Z", "answer": "(a) x", "via": "cockpit-desk"}, ROW, _ctx(signers))
    assert (got.state, got.reason) == ("failed", "channel-mismatch")
    assert ts.answer_class({"ts": "2026-11-03T21:04:30Z", "answer": "(a) x", "via": "cockpit"}, ROW,
                           _ctx(signers)).state == "agent-resolved"


@pytest.mark.parametrize("name,value", [("surface", "other"), ("view", "7"), ("store", "levain-questions:zzz"),
                                        ("install", "zzz")])
def test_desk_scope_binds_every_signed_field(world, name, value):
    path, signers = world
    got = ts.answer_class(_desk_event(path), ROW, _ctx(signers, **{name: value}))
    assert (got.state, got.reason) == ("failed", "scope-mismatch")


def test_a_desk_click_cannot_move_to_another_row_version_or_answer(world):
    path, signers = world
    ev = _desk_event(path)
    assert ts.answer_class(ev, {**ROW, "row_version": "rv-moved"}, _ctx(signers)).reason == "not-this-row"
    assert ts.answer_class({**ev, "answer": "(b) other"}, ROW, _ctx(signers)).reason == "answer-mismatch"
    assert ts.answer_class({**ev, "ts": "2026-11-03T21:30:00Z"}, ROW, _ctx(signers)).reason == "stale"


def test_a_desk_countersign_confirms_late_and_a_disown_is_disowned(world):
    path, signers = world
    original = {"ts": "2026-11-03T20:00:00Z", "answer": "(a) Add a Levain 1.0 row", "via": "cockpit"}
    for kind, state in (("confirm", "signed-terminal"), ("disown", "disowned")):
        msg = _click(countersign_of="2026-11-03T20:00:00Z", countersign=kind)
        cs = {"of_ts": "2026-11-03T20:00:00Z", "ts": "2026-11-03T21:04:30Z", "surface": "web", "view": "item",
              "via": "cockpit-desk", "signed": {"format": ts.DESK_FORMAT, "message": msg.decode(),
                                                 "sig": _sign(path, msg, ts.DESK_NAMESPACE), "entry": 1}}
        got = ts.answer_class(original, ROW, _ctx(signers, countersigns=[cs]))
        assert got.state == state and got.late and got.channel == "desk", (kind, got)
        if kind == "disown":
            assert got.badge == "you said this answer was not you"
        # a disown presented as a confirm (the event names the other kind) confers nothing
        other = "confirm" if kind == "disown" else "disown"
        bad = ts.answer_class(original, ROW, _ctx(signers, countersigns=[{**cs, "countersign": other}]))
        assert bad.state == "agent-resolved", (kind, bad)


def test_a_disown_outranks_a_confirm(world):
    path, signers = world
    original = {"ts": "2026-11-03T20:00:00Z", "answer": "(a) Add a Levain 1.0 row", "via": "cockpit"}
    css = []
    for kind in ("confirm", "disown"):
        msg = _click(countersign_of="2026-11-03T20:00:00Z", countersign=kind, nonce=("a" if kind == "confirm" else "b") * 16)
        css.append({"of_ts": "2026-11-03T20:00:00Z", "ts": "2026-11-03T21:04:30Z", "surface": "web", "view": "item",
                    "signed": {"format": ts.DESK_FORMAT, "message": msg.decode(),
                               "sig": _sign(path, msg, ts.DESK_NAMESPACE), "entry": 1}})
    assert ts.answer_class(original, ROW, _ctx(signers, countersigns=css)).state == "disowned"


def test_a_desk_countersign_is_never_a_direct_answer(world):
    path, signers = world
    msg = _click(countersign_of="2026-11-03T20:00:00Z", countersign="confirm")
    got = ts.answer_class(_desk_event(path, message=msg), ROW, _ctx(signers))
    assert (got.state, got.reason) == ("failed", "countersign-mismatch")


def test_a_typed_desk_gesture_binds_its_entry_like_a_click(world):
    path, signers = world
    msg = _click(view="7", gesture="typed", typed="1a", entries=[E])
    got = ts.answer_class(_desk_event(path, message=msg), ROW, _ctx(signers, view="7"))
    assert got.state == "signed-terminal" and got.channel == "desk"
    assert ts.answer_class(_desk_event(path, message=msg), ROW, _ctx(signers, view="item")).reason == "scope-mismatch"


def test_the_namespace_is_registered_and_the_signers_line_lists_it():
    from levain.contracts.namespaces import is_registered
    assert is_registered(ts.DESK_NAMESPACE) and is_registered(ts.NAMESPACE)
