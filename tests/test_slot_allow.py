"""Row 165: a slot's "don't ask again" reaches future slots only through the operator's own answer."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain import band_view as bv
from levain.authority import DISPLAY_PHONE, LAPTOP
from levain.cockpit import keys as ckeys
from levain.cockpit.consent import utc_stamp
from levain.consent import terminal_sign as ts
from levain.consent.questions import QuestionStore
from levain.contracts.namespaces import ANSWER_COUNTERSIGN_NAMESPACE as COUNTERSIGN_NS
from levain.slot import slot_allow as SA
from levain.slot import supervisor as sup
from levain.slot.render import Frozen


def local(*rules, **extra):
    return json.dumps({"permissions": {"allow": list(rules)}, **extra}).encode()


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


def test_only_dialog_shapes_are_offered_and_the_rest_named():
    kept, dropped = SA.parse_local_allow(local(
        "Bash(npm test *)", "WebFetch(domain:example.com)", "WebSearch", "Bash(*)", "Bash", "Read(./x)",
        "Bash(a\nb)", "Bash(npm test *)"))
    assert kept == ["Bash(npm test *)", "WebFetch(domain:example.com)", "WebSearch"]
    assert dropped == ["Bash(*)", "Bash", "Read(./x)", "Bash(a\nb)"]


def test_a_rule_with_a_hidden_character_is_not_a_shape_and_never_offered():
    for rule in ("Bash(npm test \u202e*)", "Bash(npm\u200b test *)", "Bash(n\u2066pm *)", "WebFetch(domain:a\u200bb.com)",
                 "WebSearch\u200b"):
        assert not SA.shape_ok(rule), repr(rule)
    assert SA.shape_ok("Bash(npm test *)")
    kept, dropped = SA.parse_local_allow(local("Bash(npm test *)", "Bash(npm test \u202e*)"))
    assert kept == ["Bash(npm test *)"] and len(dropped) == 1


def test_a_duplicate_key_or_wrong_shape_is_refused_whole():
    with pytest.raises(SA.AllowRefused):
        SA.parse_local_allow(b'{"permissions": {"allow": ["WebSearch"]}, "permissions": {}}')
    with pytest.raises(SA.AllowRefused):
        SA.parse_local_allow(b'{"permissions": {"allow": "WebSearch"}}')
    with pytest.raises(SA.AllowRefused):
        SA.parse_local_allow(b"[]")


def test_a_symlinked_or_oversized_file_is_refused(tmp_path):
    real = tmp_path / "real.json"
    real.write_bytes(local("WebSearch"))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(SA.AllowRefused, match="symlink"):
        SA.read_regular(link)
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (SA.SETTINGS_CAP + 1))
    with pytest.raises(SA.AllowRefused, match="over"):
        SA.read_regular(big)
    assert SA.read_regular(tmp_path / "absent.json") is None
    assert SA.read_regular(real) == real.read_bytes()


class Authority:
    """A verified replay stand-in: the keys the authority log holds, by fingerprint."""

    def __init__(self):
        self.keys = {}

    def key(self, fp):
        return self.keys.get(fp)


class Rig:
    """Real ed25519 keys and real SSHSIGs (ssh-keygen), so every positive case is a signature the verifier checks. Only
    the live Secure Enclave check is stood in for."""

    def __init__(self, tmp_path, qdir):
        self.tmp, self.qdir = tmp_path, qdir
        self.qs = QuestionStore(qdir)
        self.authority = Authority()
        self.laptop = self.enrol("laptop", LAPTOP)
        self.judge = SA.Judge(authority=lambda: self.authority, ctk=lambda pk: (None, "test"))

    def enrol(self, name, key_class, custody="touch-id"):
        path = self.tmp / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)], check=True)
        pub = " ".join(Path(str(path) + ".pub").read_text().split()[:2])
        fp, _ = ckeys.ssh_signer(path, namespace=ts.NAMESPACE)
        self.authority.keys[fp] = SimpleNamespace(active=True, key_class=key_class, custody=custody, public_key=pub,
                                                  fingerprint=fp)
        return SimpleNamespace(path=path, fp=fp)

    @property
    def sid(self):
        return bv._store_id_of(self.qs)

    def file(self, *rules, session="S1"):
        SA.file_rows(self.qdir, session, list(rules), store=self.qs)
        return {q.what: q for q in self.qs.open_questions()}

    def message(self, q, *, rule=None, asked=None, answer=None, desk=False, countersign_of=None, at=None, sid=None,
                item=None):
        rule = rule or q.what.split(": ", 1)[1]
        entry = ts.Entry(item or q.id, bv.subject_version(q), asked or SA.asked_text(rule), "information",
                         answer=answer or SA.yes_text(rule))
        at = at or datetime.now(timezone.utc) - timedelta(seconds=5)
        sid = sid or self.sid
        if desk:
            return ts.build_desk_message(store=f"levain-questions:{sid}", install=sid, surface="cockpit",
                                         origin="http://127.0.0.1:8123", view="item", gesture="click", entries=[entry],
                                         at=at)
        return ts.build_message(store=f"levain-questions:{sid}", install=sid, session="sess", view="1",
                                typed="1a yes", entries=[entry], at=at, countersign_of=countersign_of)

    def sign(self, message, key=None, namespace=ts.NAMESPACE):
        _fp, signer = ckeys.ssh_signer((key or self.laptop).path, namespace=namespace)
        return signer(message)

    def yes(self, q, *, via="cli", signed=True, **message_kw):
        """Record a yes to ``q``; signed over ``message_kw``'s message by the laptop key unless ``signed`` is False."""
        sign = bv.Signing("unsigned", reason="no-key")
        if signed:
            desk = message_kw.get("desk", False)
            msg = self.message(q, **message_kw)
            sig = self.sign(msg, namespace=ts.DESK_NAMESPACE if desk else ts.NAMESPACE)
            sign = bv.Signing("signed", message=msg.decode(), sig=sig, fp=self.laptop.fp,
                              versions={q.id: bv.subject_version(q)})
        (r,) = bv._answer_signed(self.qs, [{"ref": q.id, "option": "yes", "via": via, "viewer": "t", "sign": sign}],
                                 channel="cockpit" if via == "cockpit" else "cli", deadline=None)
        assert not isinstance(r, Exception), r
        return self.qs.get(q.id)


@pytest.fixture
def rig(tmp_path, qdir):
    return Rig(tmp_path, qdir)


def test_an_unsigned_yes_never_reaches_the_allow_file(rig, qdir, capsys):
    allow = qdir / SA.FILE_NAME
    q = rig.file("WebSearch")["A slot session asked to always allow: WebSearch"]
    rig.yes(q, signed=False)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == []
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=None) == []
    assert SA.rules_of(allow) == []
    assert SA.not_carried(qdir, store=rig.qs, judge=rig.judge) == [("WebSearch", "answered unsigned")]
    # a yes with no sidecar at all (the native store signs nothing) is the same
    q2 = rig.file("WebFetch(domain:example.com)")["A slot session asked to always allow: WebFetch(domain:example.com)"]
    rig.qs.answer(q2.id, option="yes", channel="cli")
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == []
    SA.run(SimpleNamespace(queue_dir=qdir, slot_allow_verb="list"))
    assert "WebSearch\t(answered yes, not carried: answered unsigned)" in capsys.readouterr().out


def test_a_signed_yes_carries_and_the_evidence_is_stored_and_re_verified(rig, qdir):
    allow = qdir / SA.FILE_NAME
    q = rig.file("Bash(npm test *)", "WebSearch")["A slot session asked to always allow: Bash(npm test *)"]
    rig.yes(q)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == ["Bash(npm test *)"]
    (entry,) = SA.load(allow)["rules"]
    assert (entry["channel"], entry["signer_fp"], entry["question_id"]) == ("terminal", rig.laptop.fp, q.id)
    assert entry["message"].startswith("levain-terminal-answer/1\n") and entry["sig"].startswith("-----BEGIN SSH")
    assert SA.carried(allow, rig.judge, rig.sid) == (["Bash(npm test *)"], [])
    # the other row is unanswered and stays pending
    assert [r for r in SA._pending(qdir).values()] == [
        {"rule": "WebSearch", "session": "S1", "question_id": rig.file("WebSearch")["A slot session asked to always allow: WebSearch"].id}]


def test_a_desk_click_carries_too(rig, qdir):
    allow = qdir / SA.FILE_NAME
    q = rig.file("WebSearch")["A slot session asked to always allow: WebSearch"]
    rig.yes(q, via="cockpit", desk=True)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == ["WebSearch"]
    assert SA.load(allow)["rules"][0]["channel"] == "desk"
    assert SA.carried(allow, rig.judge, rig.sid)[0] == ["WebSearch"]


@pytest.mark.parametrize("what,why", [("another rule's text", "another question or another answer"),
                                      ("another answer", "another question or another answer"),
                                      ("a no", "another question or another answer"),
                                      ("another store", "signed for another store"),
                                      ("another question", "names no such question")])
def test_a_signature_over_other_words_does_not_carry(rig, qdir, what, why):
    allow = qdir / SA.FILE_NAME
    qs = rig.file("Bash(npm test *)", "Bash(rm -rf *)")
    q = qs["A slot session asked to always allow: Bash(npm test *)"]
    kw = {"another rule's text": {"rule": "Bash(rm -rf *)"},
          "another answer": {"answer": "(a) Yes: allow everything"},
          "a no": {"answer": "(b) No: keep asking"},
          "another store": {"sid": "other-store"},
          "another question": {"item": "q-other"}}[what]
    rig.yes(q, **kw)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == []
    assert SA.rules_of(allow) == []
    (bad,) = SA.not_carried(qdir, store=rig.qs, judge=rig.judge)
    assert bad[0] == "Bash(npm test *)" and why in bad[1]


def test_a_stale_signature_does_not_carry(rig, qdir):
    allow = qdir / SA.FILE_NAME
    q = rig.file("WebSearch")["A slot session asked to always allow: WebSearch"]
    rig.yes(q, at=datetime.now(timezone.utc) - timedelta(hours=2))
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == []
    assert "not fresh" in SA.not_carried(qdir, store=rig.qs, judge=rig.judge)[0][1]


def test_a_key_the_authority_log_does_not_hold_active_as_a_laptop_key_does_not_carry(rig, qdir):
    allow = qdir / SA.FILE_NAME
    q = rig.file("WebSearch")["A slot session asked to always allow: WebSearch"]
    rig.yes(q)
    for change, text in ((lambda k: setattr(k, "active", False), "not an active key"),
                         (lambda k: setattr(k, "key_class", DISPLAY_PHONE), "needs laptop-presence"),
                         (lambda k: rig.authority.keys.clear(), "not an active key")):
        keep = dict(rig.authority.keys)
        change(rig.authority.keys.get(rig.laptop.fp, SimpleNamespace()))
        assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == []
        assert text in SA.not_carried(qdir, store=rig.qs, judge=rig.judge)[0][1]
        rig.authority.keys.update(keep)
        rig.authority.keys[rig.laptop.fp].active = True
        rig.authority.keys[rig.laptop.fp].key_class = LAPTOP
    # an authority that cannot be read carries nothing and does not raise
    boom = SA.Judge(authority=lambda: (_ for _ in ()).throw(OSError("log")))
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=boom) == []
    assert "authority log cannot be read" in SA.not_carried(qdir, store=rig.qs, judge=boom)[0][1]


def _carried_one(rig, qdir, rule="Bash(npm test *)"):
    allow = qdir / SA.FILE_NAME
    q = rig.file(rule)[SA.asked_text(rule)]
    rig.yes(q)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == [rule]
    return allow


def test_an_edited_allow_file_is_refused_at_launch(rig, qdir):
    allow = _carried_one(rig, qdir)
    doc = json.loads(allow.read_text())
    good = dict(doc["rules"][0])

    def write(**changes):
        doc["rules"] = [{**good, **changes}]
        allow.write_text(json.dumps(doc))
        return SA.carried(allow, rig.judge, rig.sid)

    assert write()[0] == ["Bash(npm test *)"]
    # the rule field swapped under real evidence
    assert write(rule="Bash(curl *)") == ([], [("Bash(curl *)", "the signature is over another question or another answer than a yes to this rule")])
    # the evidence removed, or its message or signature edited
    assert write(message=None)[0] == [] and write(sig=None)[0] == []
    lines = good["sig"].splitlines()
    lines[1] = ("B" if lines[1][0] != "B" else "C") + lines[1][1:]
    forged = write(sig="\n".join(lines) + "\n")
    assert forged[0] == [] and "signature" in forged[1][0][1]
    assert write(message=good["message"].replace("npm test", "npm  test"))[0] == []
    assert write(signer_fp="SHA256:" + "A" * 43)[0] == []
    assert write(channel="phone")[0] == [] and write(channel="desk")[0] == []
    # a rule appended with no evidence at all
    doc["rules"] = [good, {"rule": "WebSearch", "session": "X", "signed_at": "2026-01-01T00:00:00Z"}]
    allow.write_text(json.dumps(doc))
    ok, bad = SA.carried(allow, rig.judge, rig.sid)
    assert ok == ["Bash(npm test *)"] and bad == [("WebSearch", "no signed answer is stored with it")]
    # the store the rule was signed for is not this one
    assert SA.carried(allow, rig.judge, "someone-else")[0] == []
    # the key revoked after the carry: dropped at the next launch
    rig.authority.keys[rig.laptop.fp].active = False
    assert SA.carried(allow, rig.judge, rig.sid)[0] == []


def test_the_launch_carries_only_verified_rules_and_names_the_rest(rig, qdir):
    allow = _carried_one(rig, qdir)
    doc = json.loads(allow.read_text())
    doc["rules"].append({"rule": "WebSearch", "session": "X", "signed_at": "2026-01-01T00:00:00Z"})
    allow.write_text(json.dumps(doc))
    seams = sup.Seams(host="linux", helper=("x",), client_factory=lambda p: None, control_socket=Path("/x"),
                      store=Path("/x"), base_map=Path("/x"), allow_dir=qdir, allow_judge=rig.judge)
    said = []
    s = sup.Supervisor.__new__(sup.Supervisor)
    s.s, s.say = seams, said.append
    out = s._with_slot_allow(Frozen({"permissions": {"allow": ["Bash(make)"]}}, (), ()))
    assert out.settings["permissions"]["allow"] == ["Bash(make)", "Bash(npm test *)"]
    assert any("not carried: WebSearch" in m and "no signed answer" in m for m in said)


def test_a_phone_answer_carries_only_with_a_verified_countersign(rig, qdir, monkeypatch):
    from levain.consent import answer_countersign as ac

    allow = qdir / SA.FILE_NAME
    phone = rig.enrol("phone", DISPLAY_PHONE, custody="passkey")
    # the answer was typed at a remote terminal: its sidecar says so, and nothing signed it
    q2 = rig.file("Bash(make *)")["A slot session asked to always allow: Bash(make *)"]
    (r,) = bv._answer_signed(rig.qs, [{"ref": q2.id, "option": "yes", "via": "cli", "viewer": "t",
                                       "sign": bv.Signing("unsigned", reason="remote")}], channel="cli", deadline=None)
    assert not isinstance(r, Exception)
    answered = rig.qs.get(q2.id).answer.answered_at
    msg = rig.message(q2, countersign_of=answered)
    sig = rig.sign(msg, key=phone, namespace=COUNTERSIGN_NS)
    monkeypatch.setattr(ac, "countersign_evidence", lambda store, qid, **kw: (msg.decode(), sig, phone.fp))
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=SA.Judge(authority=lambda: rig.authority,
                                                                       sign_queue=Path("/q"))) == ["Bash(make *)"]
    (entry,) = SA.load(allow)["rules"]
    assert entry["channel"] == "phone" and entry["signer_fp"] == phone.fp
    assert SA.carried(allow, rig.judge, rig.sid)[0] == ["Bash(make *)"]
    # no countersign (open, denied, expired, bad): the same answer carries nothing
    monkeypatch.setattr(ac, "countersign_evidence", lambda store, qid, **kw: None)
    q3 = rig.file("Bash(ls *)")["A slot session asked to always allow: Bash(ls *)"]
    bv._answer_signed(rig.qs, [{"ref": q3.id, "option": "yes", "via": "cli", "viewer": "t",
                                "sign": bv.Signing("unsigned", reason="remote")}], channel="cli", deadline=None)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == []
    assert "not countersigned" in dict(SA.not_carried(qdir, store=rig.qs, judge=rig.judge))["Bash(ls *)"]
    # a countersign by a key that is no phone-or-better key does not carry
    rig.authority.keys[phone.fp].key_class = "presence"
    assert SA.carried(allow, rig.judge, rig.sid)[0] == []


def test_the_operators_checkout_is_byte_identical_after_a_carry(rig, qdir, tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / ".claude").mkdir(parents=True)
    (checkout / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["Bash(make)"]}}')
    before = tree_hash(checkout)
    allow = _carried_one(rig, qdir)
    assert SA.carried(allow, rig.judge, rig.sid)[0]
    assert tree_hash(checkout) == before


def test_the_launch_merges_the_rules_into_permissions_allow_only(rig, qdir):
    allow = _carried_one(rig, qdir, "Bash(make *)")
    seams = sup.Seams(host="linux", helper=("x",), client_factory=lambda p: None, control_socket=Path("/x"),
                      store=Path("/x"), base_map=Path("/x"), allow_dir=qdir, allow_judge=rig.judge)
    said = []
    s = sup.Supervisor.__new__(sup.Supervisor)
    s.s = seams
    s.say = said.append
    fz = Frozen({"hooks": {"Stop": []}, "permissions": {"allow": ["Bash(make)"], "deny": ["Read(./.env)"]}}, (), ())
    out = s._with_slot_allow(fz)
    assert out.settings["permissions"] == {"allow": ["Bash(make)", "Bash(make *)"], "deny": ["Read(./.env)"]}
    assert out.settings["hooks"] == {"Stop": []}
    assert str(allow) in said[0]
    # off when no directory is named
    s.s = dataclasses.replace(seams, allow_dir=None)
    assert s._with_slot_allow(fz) is fz


def test_a_damaged_store_is_said_and_the_launch_goes_on(qdir):
    (qdir / SA.FILE_NAME).write_text("not json")
    seams = sup.Seams(host="linux", helper=("x",), client_factory=lambda p: None, control_socket=Path("/x"),
                      store=Path("/x"), base_map=Path("/x"), allow_dir=qdir)
    said = []
    s = sup.Supervisor.__new__(sup.Supervisor)
    s.s, s.say = seams, said.append
    fz = Frozen({}, (), ())
    assert s._with_slot_allow(fz) is fz
    assert "not read" in said[0]


def test_end_files_rows_from_the_retained_file_and_names_the_dropped(qdir):
    seams = sup.Seams(host="linux", helper=("x",), client_factory=lambda p: None, control_socket=Path("/x"),
                      store=Path("/x"), base_map=Path("/x"), allow_dir=qdir,
                      retained_allow=lambda session: local("WebSearch", "Read(./x)"))
    said = []
    s = sup.Supervisor.__new__(sup.Supervisor)
    s.s, s.say = seams, said.append
    s._file_allow_rows("S9")
    assert len(QuestionStore(qdir).open_questions()) == 1
    assert any("on your band" in m for m in said) and any("Read(./x)" in m for m in said)
    assert not (qdir / SA.FILE_NAME).exists()          # filing writes no rule


def _ending_supervisor(qdir, helper):
    from levain.slot import Refused  # noqa: F401 - imported for the callers' helpers

    seams = sup.Seams(host="linux", helper=("x",), client_factory=lambda p: None, control_socket=Path("/x"),
                      store=Path("/x"), base_map=Path("/x"), allow_dir=qdir)
    said = []
    s = sup.Supervisor.__new__(sup.Supervisor)
    s.s, s.say, s.helper = seams, said.append, helper
    return s, said


def test_end_reads_the_file_through_root_s_read_allow_and_files_rows(qdir):
    import base64

    calls = []

    def helper(*args, **kw):
        calls.append((args, kw.get("stdin", b"")))
        return {"present": True, "b64": base64.b64encode(local("WebSearch", "Read(./x)")).decode()}

    s, said = _ending_supervisor(qdir, helper)
    s._file_allow_rows("S9")
    assert calls == [(("read-allow", "S9"), None)]
    assert len(QuestionStore(qdir).open_questions()) == 1
    assert any("on your band" in m for m in said) and not (qdir / SA.FILE_NAME).exists()


def test_no_file_filed_nothing_and_a_root_tree_without_the_verb_is_said_once(qdir):
    from levain.slot import Refused

    s, said = _ending_supervisor(qdir, lambda *a, **k: {"present": False})
    s._file_allow_rows("S9")
    assert said == [] and QuestionStore(qdir).open_questions() == []

    def old(*a, **k):
        raise Refused("unknown-verb", "read-allow")

    s, said = _ending_supervisor(qdir, old)
    s._file_allow_rows("S1")
    s._file_allow_rows("S2")
    assert said == ["slot allow rules not read: the root tree predates read-allow; `levain upgrade` adds it"]

    def unbuilt(*a, **k):
        raise Refused("verb-unbuilt:read-allow", "read-allow")

    s, said = _ending_supervisor(qdir, unbuilt)
    s._file_allow_rows("S1")
    assert len(said) == 1 and "predates read-allow" in said[0]


def test_a_refused_or_malformed_read_is_said_and_files_nothing(qdir):
    from levain.slot import Refused

    def refuse(*a, **k):
        raise Refused("allow-unreadable", "settings.local.json has 2 links")

    s, said = _ending_supervisor(qdir, refuse)
    s._file_allow_rows("S9")
    assert "allow-unreadable" in said[0] and QuestionStore(qdir).open_questions() == []
    for bad in ({"present": True}, {"present": True, "b64": "!!not base64"}, {"present": True, "b64": 5}):
        s, said = _ending_supervisor(qdir, lambda *a, _b=bad, **k: _b)
        s._file_allow_rows("S9")
        assert "not read" in said[0] and QuestionStore(qdir).open_questions() == []


def test_revoke_needs_confirm_and_undo_restores_with_its_evidence(rig, qdir):
    allow = _carried_one(rig, qdir, "WebSearch")
    assert "re-run with --yes" in SA.revoke(allow, "WebSearch", confirm=False)
    assert SA.rules_of(allow) == ["WebSearch"]
    SA.revoke(allow, "WebSearch", confirm=True)
    assert SA.rules_of(allow) == [] and SA.carried(allow, rig.judge, rig.sid) == ([], [])
    SA.undo(allow, "WebSearch")
    assert SA.carried(allow, rig.judge, rig.sid)[0] == ["WebSearch"]      # the evidence came back with it
    with pytest.raises(SA.AllowRefused):
        SA.revoke(allow, "WebFetch(domain:x.com)", confirm=True)


def test_a_rule_cannot_be_stored_without_its_signed_answer(qdir):
    with pytest.raises(SA.AllowRefused):
        SA.append(qdir / SA.FILE_NAME, "WebSearch", "S1", {})
    assert not (qdir / SA.FILE_NAME).exists()
