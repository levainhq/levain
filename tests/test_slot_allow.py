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
        at = at or self.qs._clock() - timedelta(seconds=5)
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
    assert [{k: v for k, v in r.items() if k != "key"} for r in SA._pending(qdir).values()] == [
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
        assert text in SA.not_carried(qdir, store=rig.qs, judge=rig.judge)[0][1]     # judged as the launch will
        rig.authority.keys.update(keep)
        rig.authority.keys[rig.laptop.fp].active = True
        rig.authority.keys[rig.laptop.fp].key_class = LAPTOP
    # an authority that cannot be read carries nothing and does not raise; the refused yes is named after the launch
    boom = SA.Judge(authority=lambda: (_ for _ in ()).throw(OSError("log")))
    assert "authority log cannot be read" in SA.not_carried(qdir, store=rig.qs, judge=boom)[0][1]
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=boom) == []
    assert "authority log cannot be read" in SA.not_carried(qdir, store=rig.qs, judge=rig.judge)[0][1]
    assert SA.rules_of(allow) == []


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


def test_revoke_needs_confirm_and_drops_the_signed_evidence(rig, qdir):
    allow = _carried_one(rig, qdir, "WebSearch")
    assert "re-run with --yes" in SA.revoke(allow, "WebSearch", confirm=False)
    assert SA.rules_of(allow) == ["WebSearch"]
    SA.revoke(allow, "WebSearch", confirm=True)
    assert SA.rules_of(allow) == [] and SA.carried(allow, rig.judge, rig.sid) == ([], [])
    (gone,) = SA.load(allow)["revoked"]
    assert gone["rule"] == "WebSearch" and not set(SA.EVIDENCE_KEYS) & set(gone)
    with pytest.raises(SA.AllowRefused):
        SA.revoke(allow, "WebFetch(domain:x.com)", confirm=True)


def test_a_revoked_rule_moved_back_by_hand_does_not_carry_and_undo_asks_for_a_new_signed_yes(rig, qdir):
    allow = _carried_one(rig, qdir, "WebSearch")
    SA.revoke(allow, "WebSearch", confirm=True)
    doc = json.loads(allow.read_text())
    doc["rules"] = [{k: v for k, v in doc["revoked"][0].items() if k != "revoked_at"}]
    allow.write_text(json.dumps(doc))
    assert SA.carried(allow, rig.judge, rig.sid)[0] == []
    doc["rules"] = []
    allow.write_text(json.dumps(doc))
    said = SA.undo(allow, "WebSearch")
    assert "re-offered on the band: answer yes to carry it again" in said
    assert SA.carried(allow, rig.judge, rig.sid) == ([], [])
    rows = [q for q in rig.qs.open_questions() if q.what == SA.asked_text("WebSearch")]
    assert len(rows) == 1 and rows[0].answer is None
    rig.yes(rows[0])
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == ["WebSearch"]
    assert SA.carried(allow, rig.judge, rig.sid)[0] == ["WebSearch"]


def test_a_rule_cannot_be_stored_without_its_signed_answer(qdir):
    with pytest.raises(SA.AllowRefused):
        SA.append(qdir / SA.FILE_NAME, "WebSearch", "S1", {})
    assert not (qdir / SA.FILE_NAME).exists()


def _at_day(rig, days_ago):
    """Run the store's clock ``days_ago`` days back: answers made now are stamped then, and so is what they sign."""
    rig.qs._clock = lambda: datetime.now(timezone.utc) - timedelta(days=days_ago)


def test_a_rule_is_a_30_day_lease_and_a_lapsed_one_does_not_reach_a_launch(rig, qdir, capsys):
    _at_day(rig, 10)
    allow = _carried_one(rig, qdir)
    (entry,) = SA.load(allow)["rules"]
    now = datetime.now(timezone.utc)
    assert entry["expires_at"] == utc_stamp(SA.lease_end(entry)) and 19 <= (SA.lease_end(entry) - now).days + 1 <= 20
    assert SA.carried(allow, rig.judge, rig.sid, now=now)[0] == ["Bash(npm test *)"]
    ok, bad = SA.carried(allow, rig.judge, rig.sid, now=now + timedelta(days=21))
    assert ok == [] and "lease lapsed" in bad[0][1]
    # the end cannot be moved by editing the file: it is derived from the signed message
    doc = json.loads(allow.read_text())
    doc["rules"][0]["expires_at"] = utc_stamp(now + timedelta(days=400))
    allow.write_text(json.dumps(doc))
    assert SA.carried(allow, rig.judge, rig.sid, now=now)[0] == []
    doc["rules"][0]["expires_at"] = entry["expires_at"]
    allow.write_text(json.dumps(doc))


def test_a_lapsed_rule_is_named_on_the_launch_and_the_list_shows_days_left(rig, qdir, capsys):
    _at_day(rig, 40)
    allow = qdir / SA.FILE_NAME
    q = rig.file("Bash(npm test *)")[SA.asked_text("Bash(npm test *)")]
    rig.yes(q)
    then = SA.Judge(authority=rig.judge.authority, ctk=rig.judge.ctk, clock=rig.qs._clock)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=then) == ["Bash(npm test *)"]   # applied on its day
    seams = sup.Seams(host="linux", helper=("x",), client_factory=lambda p: None, control_socket=Path("/x"),
                      store=Path("/x"), base_map=Path("/x"), allow_dir=qdir, allow_judge=rig.judge)
    said = []
    s = sup.Supervisor.__new__(sup.Supervisor)
    s.s, s.say = seams, said.append
    out = s._with_slot_allow(Frozen({}, (), ()))
    assert "permissions" not in out.settings
    assert any("not carried: Bash(npm test *)" in m and "lease lapsed" in m for m in said)
    # the lapse filed its renewal row on the band
    assert any("renews it" in q.blocks for q in rig.qs.open_questions())
    SA.run(SimpleNamespace(queue_dir=qdir, slot_allow_verb="list"))
    assert "lapsed" in capsys.readouterr().out
    _at_day(rig, 5)
    SA.revoke(allow, "Bash(npm test *)", confirm=True)
    _carried_one(rig, qdir, "WebSearch")
    SA.run(SimpleNamespace(queue_dir=qdir, slot_allow_verb="list"))
    text = capsys.readouterr().out
    assert "WebSearch" in text and ("24 days left" in text or "25 days left" in text)


def test_the_reminder_files_once_and_a_signed_renewal_resets_the_lease(rig, qdir):
    _at_day(rig, 28)
    allow = _carried_one(rig, qdir)
    assert SA.remind(qdir, allow, store=rig.qs, judge=rig.judge) == ["Bash(npm test *)"]          # 2 days left: one renewal row
    assert SA.remind(qdir, allow, store=rig.qs, judge=rig.judge) == []                            # filed once for this lease
    young = rig.qs._clock
    _at_day(rig, 0)
    rows = [q for q in rig.qs.open_questions() if q.what == SA.asked_text("Bash(npm test *)")]
    assert len(rows) == 1 and "renews it" in rows[0].blocks
    old_end = SA.lease_end(SA.load(allow)["rules"][0])
    rig.yes(rows[0])
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == ["Bash(npm test *)"]
    (entry,) = SA.load(allow)["rules"]
    new_end = SA.lease_end(entry)
    assert new_end - old_end > timedelta(days=27)
    now = datetime.now(timezone.utc)
    assert SA.carried(allow, rig.judge, rig.sid, now=now + timedelta(days=29))[0] == ["Bash(npm test *)"]
    assert SA.carried(allow, rig.judge, rig.sid, now=now + timedelta(days=31))[0] == []
    # the new lease is not due: no second reminder, and the old lease's row is not refiled
    assert SA.remind(qdir, allow, store=rig.qs, judge=rig.judge) == []
    assert young is not None


def test_a_rule_with_more_than_three_days_left_files_no_reminder(rig, qdir):
    _at_day(rig, 10)
    allow = _carried_one(rig, qdir)
    assert SA.remind(qdir, allow, store=rig.qs, judge=rig.judge) == []


def _b64(data):
    import base64

    return base64.b64encode(data).decode()


def _rows(rig, rule):
    return [q for q in rig.qs.open_questions() if q.what == SA.asked_text(rule)]


def test_a_rule_whose_key_was_re_enrolled_is_offered_again_at_the_next_end(rig, qdir, capsys):
    allow = _carried_one(rig, qdir, "WebSearch")
    del rig.authority.keys[rig.laptop.fp]                 # the laptop key re-enrolled: the old one leaves the log
    rig.laptop = rig.enrol("laptop2", LAPTOP)
    assert SA.carried(allow, rig.judge, rig.sid)[0] == []
    s, said = _ending_supervisor(qdir, lambda *a, **k: {"present": True, "b64": _b64(local("WebSearch"))})
    s.s = dataclasses.replace(s.s, allow_judge=rig.judge)
    s._file_allow_rows("S2")
    rows = _rows(rig, "WebSearch")
    assert len(rows) == 1 and rows[0].answer is None
    SA.run(SimpleNamespace(queue_dir=qdir, slot_allow_verb="list"))
    out = capsys.readouterr().out
    assert "WebSearch\t(NOT CARRIED" in out and "its row is open on the band: answer it again" in out
    s._file_allow_rows("S3")                               # a second END files no second row
    assert len(_rows(rig, "WebSearch")) == 1
    rig.yes(rows[0])
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == ["WebSearch"]
    assert SA.carried(allow, rig.judge, rig.sid)[0] == ["WebSearch"]


def test_a_rule_whose_signed_time_is_ahead_of_now_does_not_carry(rig, qdir):
    allow = qdir / SA.FILE_NAME
    q = rig.file("WebSearch")[SA.asked_text("WebSearch")]
    ahead = datetime.now(timezone.utc) + timedelta(days=20)
    msg = rig.message(q, at=ahead)
    ev = {"question_id": q.id, "channel": "terminal", "signer_fp": rig.laptop.fp, "message": msg.decode(),
          "sig": rig.sign(msg), "answered_at": utc_stamp(ahead)}
    assert SA.verify_evidence("WebSearch", ev, authority=rig.authority, store_id=rig.sid, ctk=SA._no_live_check) is None
    SA.append(allow, "WebSearch", "S1", ev)
    ok, bad = SA.carried(allow, rig.judge, rig.sid)
    assert ok == [] and "ahead of now" in bad[0][1]
    # a signed time a little ahead of this clock (skew) is not refused
    near = datetime.now(timezone.utc) + timedelta(minutes=3)
    msg = rig.message(q, at=near)
    SA.append(allow, "WebSearch", "S1", {**ev, "message": msg.decode(), "sig": rig.sign(msg)})
    assert SA.carried(allow, rig.judge, rig.sid)[0] == ["WebSearch"]


def test_an_answer_whose_lease_lapsed_before_it_was_applied_is_refused_not_added(rig, qdir):
    _at_day(rig, 31)
    allow = qdir / SA.FILE_NAME
    q = rig.file("WebSearch")[SA.asked_text("WebSearch")]
    rig.yes(q)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == []
    assert SA.rules_of(allow) == []
    assert "lease lapsed" in dict(SA.not_carried(qdir, store=rig.qs, judge=rig.judge))["WebSearch"]


def test_a_bash_rule_with_a_lookalike_letter_is_not_a_shape():
    rule = "Bash(curl аpi.example *)"                # Cyrillic a, printable and not hidden
    assert not SA.shape_ok(rule)
    kept, dropped = SA.parse_local_allow(local("Bash(curl api.example *)", rule))
    assert kept == ["Bash(curl api.example *)"] and dropped == [rule]


def _move_at(allow, when):
    doc = json.loads(allow.read_text())
    e = doc["rules"][0]
    e["message"] = "\n".join(f"at: {when}" if ln.startswith("at: ") else ln for ln in e["message"].split("\n"))
    allow.write_text(json.dumps(doc))


def test_an_old_entry_that_does_not_verify_does_not_silence_the_reminder(rig, qdir):
    _at_day(rig, 28)
    allow = _carried_one(rig, qdir)
    _move_at(allow, "2030-01-01T00:00:00Z")                # an edit of the stored message to a far lease
    assert SA.carried(allow, rig.judge, rig.sid)[0] == []
    assert SA.remind(qdir, allow, store=rig.qs, judge=rig.judge) == ["Bash(npm test *)"]
    assert SA.remind(qdir, allow, store=rig.qs, judge=rig.judge) == []
    _at_day(rig, 0)
    (row,) = [q for q in _rows(rig, "Bash(npm test *)")]
    rig.yes(row)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == ["Bash(npm test *)"]
    assert SA.carried(allow, rig.judge, rig.sid)[0] == ["Bash(npm test *)"]


def test_an_old_entry_that_does_not_verify_does_not_block_a_renewal(rig, qdir):
    _at_day(rig, 28)
    allow = _carried_one(rig, qdir)
    q = rig.qs.get(SA.load(allow)["rules"][0]["question_id"])
    _move_at(allow, "2030-01-01T00:00:00Z")
    _at_day(rig, 0)
    msg = rig.message(q)
    ev = {"question_id": q.id, "channel": "terminal", "signer_fp": rig.laptop.fp, "message": msg.decode(),
          "sig": rig.sign(msg), "answered_at": utc_stamp(datetime.now(timezone.utc))}
    assert SA.append(allow, "Bash(npm test *)", "S2", ev) is True
    assert SA.carried(allow, rig.judge, rig.sid)[0] == ["Bash(npm test *)"]


def test_two_writers_at_once_lose_no_row_and_no_rule(rig, qdir, monkeypatch):
    import threading

    gate = threading.Barrier(2, timeout=1.0)
    real_pending, real_load = SA._pending, SA.load

    def slow(fn):
        def wrapped(*a, **k):
            got = fn(*a, **k)
            try:
                gate.wait()                                # both writers have read before either writes
            except threading.BrokenBarrierError:
                pass
            return got
        return wrapped

    monkeypatch.setattr(SA, "_pending", slow(real_pending))
    errs = []

    def file(rule):
        try:
            SA.file_rows(qdir, "S1", [rule])
        except Exception as e:   # noqa: BLE001
            errs.append(e)

    ts_ = [threading.Thread(target=file, args=(r,)) for r in ("WebSearch", "Bash(make *)")]
    for t in ts_:
        t.start()
    for t in ts_:
        t.join(10)
    assert errs == []
    assert sorted(v["rule"] for v in real_pending(qdir).values()) == ["Bash(make *)", "WebSearch"]
    monkeypatch.setattr(SA, "_pending", real_pending)
    # the same for slot-allow.json
    allow = qdir / SA.FILE_NAME
    evs = {}
    for rule in ("Bash(ls *)", "Bash(cat *)"):
        q = rig.file(rule)[SA.asked_text(rule)]
        msg = rig.message(q)
        evs[rule] = {"question_id": q.id, "channel": "terminal", "signer_fp": rig.laptop.fp, "message": msg.decode(),
                     "sig": rig.sign(msg), "answered_at": utc_stamp(datetime.now(timezone.utc))}
    gate.reset()
    monkeypatch.setattr(SA, "load", slow(real_load))
    ts_ = [threading.Thread(target=SA.append, args=(allow, r, "S1", evs[r])) for r in evs]
    for t in ts_:
        t.start()
    for t in ts_:
        t.join(10)
    assert sorted(SA.rules_of(allow)) == ["Bash(cat *)", "Bash(ls *)"]


def test_the_list_shows_the_signed_time_and_the_applied_time(rig, qdir, capsys):
    _at_day(rig, 10)
    allow = _carried_one(rig, qdir)
    (entry,) = SA.load(allow)["rules"]
    signed = utc_stamp(SA._signed_at(entry))
    assert entry["applied_at"] != signed
    SA.run(SimpleNamespace(queue_dir=qdir, slot_allow_verb="list"))
    out = capsys.readouterr().out
    assert f"signed {signed}" in out and f"applied {entry['applied_at']}" in out


def test_the_list_shows_hours_under_a_day(rig, qdir, capsys):
    import re

    _at_day(rig, 29.8)
    _carried_one(rig, qdir)
    SA.run(SimpleNamespace(queue_dir=qdir, slot_allow_verb="list"))
    out = capsys.readouterr().out
    assert re.search(r"\b[45] hours left", out) and "0 days left" not in out


# --- the offer lifecycle (head ruling R2): one pending record per rule, resolved by question id ---------------------

def _save(rig, qdir, rule, session):
    """A slot END that saved ``rule``: what :func:`slot_allow.offer` files."""
    return SA.offer(qdir, qdir / SA.FILE_NAME, session, [rule], rig.judge, store=rig.qs)


def test_a_no_then_a_later_save_files_one_new_open_row(rig, qdir):
    allow = qdir / SA.FILE_NAME
    assert _save(rig, qdir, "WebSearch", "S1") == ["WebSearch"]
    (q,) = _rows(rig, "WebSearch")
    rig.qs.answer(q.id, option="no", channel="cli")
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == []
    assert _save(rig, qdir, "WebSearch", "S2") == ["WebSearch"]          # "No: keep asking" asks at the next save
    rows = _rows(rig, "WebSearch")
    assert len(rows) == 1 and rows[0].id != q.id
    assert _save(rig, qdir, "WebSearch", "S3") == []                     # one open row per rule, never two
    assert len(_rows(rig, "WebSearch")) == 1


def test_an_unsigned_yes_drops_its_pending_record_and_a_later_save_files_a_new_row(rig, qdir):
    allow = qdir / SA.FILE_NAME
    _save(rig, qdir, "WebSearch", "S1")
    (q,) = _rows(rig, "WebSearch")
    rig.yes(q, signed=False)
    assert _save(rig, qdir, "WebSearch", "S2") == []                     # a yes waiting for its launch is not re-asked
    refused = []
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge, refused=refused) == []
    assert refused == [("WebSearch", "answered unsigned")]
    assert SA._pending(qdir) == {}
    assert SA.not_carried(qdir, store=rig.qs, judge=rig.judge) == [("WebSearch", "answered unsigned")]
    assert _save(rig, qdir, "WebSearch", "S3") == ["WebSearch"]          # re-offered once per save
    (row,) = _rows(rig, "WebSearch")
    assert row.id != q.id
    assert SA.not_carried(qdir, store=rig.qs, judge=rig.judge) == []     # the new row replaces the refusal
    rig.yes(row)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == ["WebSearch"]


def test_a_pending_row_with_600_questions_filed_after_it_is_still_resolved(rig, qdir):
    allow = qdir / SA.FILE_NAME
    _save(rig, qdir, "WebSearch", "S1")
    (q,) = _rows(rig, "WebSearch")
    for i in range(600):
        rig.qs.file(agent="codex", agent_session_id=f"s{i}", what=f"other {i}", options=[("a", "A"), ("b", "B")],
                    rec="a", blocks="nothing", cwd=str(qdir))
    rig.yes(q)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == ["WebSearch"]
    assert SA.carried(allow, rig.judge, rig.sid)[0] == ["WebSearch"]


def test_an_answer_landing_mid_apply_raises_nothing_and_the_rules_are_carried(rig, qdir, monkeypatch):
    allow = qdir / SA.FILE_NAME
    _save(rig, qdir, "WebSearch", "S1")
    _save(rig, qdir, "Bash(make *)", "S1")
    (a,) = _rows(rig, "WebSearch")
    (b,) = _rows(rig, "Bash(make *)")
    rig.yes(a)
    landed = []

    def landing(fn):
        def wrapped(*args, **kw):
            got = fn(*args, **kw)
            if not landed:
                landed.append(True)
                rig.yes(b)                                   # the operator answers while the apply runs
            return got
        return wrapped

    monkeypatch.setattr(rig.qs, "get", landing(rig.qs.get))
    monkeypatch.setattr(rig.qs, "recent", landing(rig.qs.recent))
    added = SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge)
    assert "WebSearch" in added
    SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge)
    assert sorted(SA.carried(allow, rig.judge, rig.sid)[0]) == ["Bash(make *)", "WebSearch"]


def test_undo_of_a_sanitized_entry_copied_into_rules_reports_not_carried_and_files_a_fresh_row(rig, qdir):
    allow = _carried_one(rig, qdir, "WebSearch")
    SA.revoke(allow, "WebSearch", confirm=True)
    doc = json.loads(allow.read_text())
    doc["rules"] = [{k: v for k, v in doc["revoked"][0].items() if k != "revoked_at"}]
    allow.write_text(json.dumps(doc))
    said = SA.undo(allow, "WebSearch", store=rig.qs, judge=rig.judge)
    assert "carried already" not in said and "re-offered on the band" in said
    (row,) = _rows(rig, "WebSearch")
    assert row.answer is None
    assert SA.carried(allow, rig.judge, rig.sid)[0] == []                # undo restored nothing
    rig.yes(row)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == ["WebSearch"]
    assert SA.carried(allow, rig.judge, rig.sid)[0] == ["WebSearch"]


def test_undo_of_a_rule_carried_by_a_later_signed_yes_says_so_and_files_nothing(rig, qdir):
    allow = _carried_one(rig, qdir, "WebSearch")
    SA.revoke(allow, "WebSearch", confirm=True)
    SA.undo(allow, "WebSearch", store=rig.qs, judge=rig.judge)
    (row,) = _rows(rig, "WebSearch")
    rig.yes(row)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == ["WebSearch"]
    SA.revoke(allow, "WebSearch", confirm=True)
    assert _save(rig, qdir, "WebSearch", "S4") == ["WebSearch"]
    (row,) = _rows(rig, "WebSearch")
    rig.yes(row)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == ["WebSearch"]
    doc = json.loads(allow.read_text())
    doc["revoked"] = [{"rule": "WebSearch", "session": "S1", "revoked_at": "2026-01-01T00:00:00Z"}]
    allow.write_text(json.dumps(doc))
    said = SA.undo(allow, "WebSearch", store=rig.qs, judge=rig.judge)
    assert "carried already" in said and _rows(rig, "WebSearch") == []


def test_the_lock_re_enters_through_a_symlinked_spelling_of_the_directory(qdir, tmp_path):
    import threading

    link = tmp_path / "consent-link"
    link.symlink_to(qdir)
    done = []

    def nested():
        with SA._locked(qdir):
            with SA._locked(link):
                done.append(True)

    t = threading.Thread(target=nested, daemon=True)
    t.start()
    t.join(3)
    assert done == [True]


def test_a_stale_tmp_file_from_a_crashed_writer_is_replaced(qdir):
    stale = qdir / f".{SA.FILE_NAME}.{os.getpid()}.tmp"
    stale.write_text("left by a crash")
    SA._save(qdir / SA.FILE_NAME, SA._empty())
    assert SA.load(qdir / SA.FILE_NAME)["rules"] == [] and not stale.exists()


def test_a_pending_file_of_the_keyed_format_holds_nothing_back(rig, qdir):
    (qdir / SA.PENDING_NAME).write_text(json.dumps({"format": "levain-slot-allow-pending/1", "rows": {
        "slot-allow:0": {"rule": "WebSearch", "session": "S0", "question_id": "q-gone"}}}))
    assert SA.apply_answered(qdir, qdir / SA.FILE_NAME, store=rig.qs, judge=rig.judge) == []
    assert _save(rig, qdir, "WebSearch", "S1") == ["WebSearch"]
