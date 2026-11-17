"""P2: operator-signed board records (levain.desk.board_sign) and the fixed 1.0 policy the writer enforces.

Every key here is a throwaway ed25519 key made by ssh-keygen in the test's tmp dir, with a touch-id sidecar so the terminal
key finder accepts it; the signer is the real ``ssh_signer`` over that key (never Touch ID). The authority log is a hand-built
snapshot: verification is pure over (record, snapshot)."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from levain import authority
from levain.authority import AuthorityKey, AuthoritySnapshot, StandingPolicy
from levain.cli import main
from levain.cockpit import keys as ckeys
from levain.cockpit.custody import APPLE_PROVIDER, SIDECAR_FORMAT, SIDECAR_SUFFIX
from levain.consent import terminal_sign as ts
from levain.desk import board_sign as sg
from levain.desk import board_store as bs
from levain.desk.board_state import fold

OPERATOR = {"kind": "operator", "name": "phill"}
AGENT = {"kind": "agent", "name": "seat-1"}
NOW = "2026-11-17T12:00:00Z"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "lhome"
    monkeypatch.setenv("LEVAIN_HOME", str(h))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    return h


@pytest.fixture
def target(home, tmp_path, capsys) -> bs.Target:
    assert main(["board", "use", str(tmp_path / "store")]) == 0
    capsys.readouterr()
    return bs.resolve_store()


def _key(d: Path, name: str) -> Path:
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    d.chmod(0o700)
    path = d / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)], check=True)
    (d / (name + SIDECAR_SUFFIX)).write_text(json.dumps({"format": SIDECAR_FORMAT, "custody": "touch-id",
                                                         "provider": APPLE_PROVIDER, "ctk_hash": "A" * 40, "label": name}))
    return path


class Rig:
    """A test key, its key directory, and a snapshot that enrols it."""

    def __init__(self, tmp_path: Path, key_class: str = authority.LAPTOP, label: str = ts.KEY_LABEL):
        self.dir = tmp_path / f"keys-{key_class}"
        self.path = _key(self.dir, label)
        self.public = " ".join(Path(str(self.path) + ".pub").read_text().split()[:2])
        self.fp = ckeys.fingerprint_of(self.public)
        self.key_class = key_class

    def snapshot(self, revoked_seq: int | None = None) -> AuthoritySnapshot:
        key = AuthorityKey(fingerprint=self.fp, public_key=self.public, key_class=self.key_class, enrolled_seq=1,
                           revoked_seq=revoked_seq)
        return AuthoritySnapshot(store_id="s", seq=2, head_hash="h", keys={self.fp: key}, policy=StandingPolicy())

    def sign(self, message: bytes) -> str:
        return ckeys.ssh_signer(self.path, namespace=sg.NAMESPACE)[1](message)

    def seal(self, **kw) -> sg.Seal:
        def factory(path, bound):
            return ckeys.ssh_signer(path, namespace=sg.NAMESPACE, timeout=bound)
        return sg.terminal_seal(key_dir=self.dir, signer_factory=factory, remote=lambda: None,
                                in_agent_session=lambda: False, **kw)


@pytest.fixture
def rig(tmp_path) -> Rig:
    return Rig(tmp_path)


def items(target: bs.Target) -> list[dict]:
    p = target.store_dir / "items.jsonl"
    return [json.loads(x) for x in p.read_text("utf-8").splitlines()] if p.exists() else []


def add_item(target: bs.Target, **kw) -> str:
    return bs.add(target, "levain", "an item", "desk 1", "phill", author=OPERATOR, now=NOW, **kw)[0]


# ------------------------------------------------------------------------------------------------------ the signature


def test_a_terminal_signed_decide_verifies_and_signs_the_bytes_that_include_prev(target, rig):
    item = add_item(target)
    bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=rig.seal())
    rec = items(target)[-1]
    assert rec["grade"] == "signed-terminal" and list(rec)[-1] == "sig"
    assert set(rec["sig"]) == {"sshsig", "fingerprint", "via"} and rec["sig"]["via"] == "terminal"
    assert rec["sig"]["fingerprint"] == rig.fp
    # the signed bytes are canonical_bytes of the record without sig, and carry the chain position
    unsigned = {k: v for k, v in rec.items() if k != "sig"}
    assert sg.signing_bytes(rec) == bs.canonical_bytes(unsigned) and b'"prev":"' in sg.signing_bytes(rec)
    verdict = sg.verify_record(rec, rig.snapshot())
    assert verdict.state == "valid" and verdict.text == f"signed-terminal by {rig.fp}, valid"
    assert fold((target.store_dir / "items.jsonl").read_text("utf-8").splitlines()).items     # a signed record still folds


def test_a_record_edited_after_signing_fails_verification(target, rig):
    item = add_item(target)
    bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=rig.seal())
    rec = items(target)[-1]
    rec["words"] = "yes"
    assert sg.verify_record(rec, rig.snapshot()).text == "signature does not verify"
    rec2 = items(target)[-1]
    rec2["prev"] = "0" * 64                    # the signature commits to the chain position too
    assert sg.verify_record(rec2, rig.snapshot()).state == "bad"


def test_a_signature_in_another_namespace_does_not_verify(target, rig):
    item = add_item(target)
    bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=rig.seal())
    rec = items(target)[-1]
    rec["sig"]["sshsig"] = ckeys.ssh_signer(rig.path, namespace="levain-consent-answer")[1](sg.signing_bytes(rec))
    assert sg.verify_record(rec, rig.snapshot()).state == "bad"


def test_a_revoked_key_reports_the_revoked_line_and_is_never_silently_valid(target, rig):
    item = add_item(target)
    bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=rig.seal())
    v = sg.verify_record(items(target)[-1], rig.snapshot(revoked_seq=7))
    assert v.state == "revoked"
    assert v.text == (f"signed by {rig.fp}, revoked at seq 7; the record's own time is the signer's claim")


def test_a_key_the_authority_log_does_not_hold_does_not_verify(target, rig, tmp_path):
    item = add_item(target)
    bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=rig.seal())
    other = Rig(tmp_path / "x", label="other")
    assert sg.verify_record(items(target)[-1], other.snapshot()).state == "bad"


def test_unsigned_records_are_asserted_and_pre_cutover_ones_are_legacy(target):
    item = add_item(target)
    snap = AuthoritySnapshot(store_id="s", seq=1, head_hash="h", keys={}, policy=StandingPolicy())
    assert sg.verify_record(items(target)[0], snap).text == "asserted operator phill, unsigned"
    legacy = {"v": 1, "op": "add", "id": item, "at": NOW, "by": "t"}
    assert sg.verify_record(legacy, snap).text == "legacy"


def test_a_signature_on_an_asserted_record_or_an_agent_signed_claim_is_bad(target, rig):
    item = add_item(target)
    bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=rig.seal())
    rec = items(target)[-1]
    rec["grade"] = "asserted"
    assert sg.verify_record(rec, rig.snapshot()).state == "bad"
    rec = items(target)[-1]
    rec["author"] = {"kind": "agent", "name": "phill"}
    assert sg.verify_record(rec, rig.snapshot()).state == "bad"


# ---------------------------------------------------------------------------------------------------------- the writer


def test_a_signed_record_needs_an_operator_author(target, rig):
    item = add_item(target)
    with pytest.raises(bs.BoardError, match="author kind must be operator"):
        bs.decide(target, item, "cut", "no", "seat-1", author=AGENT, now=NOW, seal=rig.seal())
    assert len(items(target)) == 1


def test_a_caller_body_carrying_sig_or_grade_is_refused():
    for key in ("sig", "grade"):
        with pytest.raises(bs.BoardError, match="the writer sets those"):
            bs.build_record("decide", "levain#1", "phill", {"decision": "cut", key: "x"}, NOW, author=OPERATOR)


def test_a_failed_refused_or_cancelled_signature_writes_nothing(target, rig):
    item = add_item(target)
    before = (target.store_dir / "items.jsonl").read_bytes()

    def cancelled(path, bound):
        def sign(message):
            raise ckeys.KeyError_("Touch ID was cancelled or did not complete: no signature was made")
        return rig.fp, sign
    seal = sg.terminal_seal(key_dir=rig.dir, signer_factory=cancelled, remote=lambda: None, in_agent_session=lambda: False)
    with pytest.raises(bs.BoardError, match="cancelled"):
        bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=seal)
    assert (target.store_dir / "items.jsonl").read_bytes() == before


def test_no_key_a_remote_session_and_an_agent_session_each_refuse_with_nothing_written(target, rig, tmp_path):
    item = add_item(target)
    before = (target.store_dir / "items.jsonl").read_bytes()
    empty = tmp_path / "nokeys"
    empty.mkdir(mode=0o700)
    with pytest.raises(bs.BoardError, match="no terminal key"):
        bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW,
                  seal=sg.terminal_seal(key_dir=empty, in_agent_session=lambda: False))
    with pytest.raises(bs.BoardError, match="remote"):
        bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW,
                  seal=sg.terminal_seal(key_dir=rig.dir, remote=lambda: "ssh", in_agent_session=lambda: False))
    with pytest.raises(bs.BoardError, match="not inside an agent session"):
        bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW,
                  seal=sg.terminal_seal(key_dir=rig.dir, remote=lambda: None, in_agent_session=lambda: True))
    assert (target.store_dir / "items.jsonl").read_bytes() == before


def test_a_signed_add_and_amend_carry_the_signature_too(target, rig):
    item = bs.add(target, "levain", "t", "desk 1", "phill", author=OPERATOR, now=NOW, seal=rig.seal())[0]
    bs.amend(target, item, "why", "phill", {"title": "t2"}, now=NOW, author=OPERATOR, seal=rig.seal())
    assert [sg.verify_record(r, rig.snapshot()).state for r in items(target)] == ["valid", "valid"]


# ------------------------------------------------------------------------------------------------------------ policy


def test_an_unsigned_decide_is_refused_naming_both_ways(target):
    item = add_item(target)
    with pytest.raises(bs.BoardError) as exc:
        bs.decide(target, item, "parked", "later", "phill", author=OPERATOR, now=NOW)
    assert "--sign" in str(exc.value) and "--source <np-id>" in str(exc.value)
    assert len(items(target)) == 1


def test_an_unsigned_add_and_amend_pass_as_asserted(target):
    item = add_item(target)
    bs.amend(target, item, "why", "phill", {"title": "t2"}, now=NOW, author=AGENT | {"name": "phill"})
    assert [r["grade"] for r in items(target)] == ["asserted", "asserted"]


def test_the_policy_table_lists_every_op_and_the_registry_ops_are_decision_class_or_open():
    for op in ("decide", "begin-track", "track", "abandon-track", "untrack"):
        assert sg.POLICY[op] == sg.DECISION_CLASS
        with pytest.raises(bs.BoardError, match="--sign"):
            sg.require_policy(op, "asserted", {})
        for grade in ("signed-terminal", "signed-phone", "relayed"):
            sg.require_policy(op, grade, {})
    for op in ("add", "amend", "register", "pause", "resume", "move", "ack-torn", "abandon-done"):
        sg.require_policy(op, "asserted", {})
    with pytest.raises(bs.BoardError, match="not in the board's write policy"):
        sg.require_policy("frobnicate", "signed-terminal", {})


def test_the_cli_refuses_an_unsigned_decide_and_sign_needs_an_operator(target, capsys):
    item = add_item(target)
    rc = main(["board", "decide", "--id", item, "--decision", "cut", "--words", "w", "--by", "phill"])
    assert rc == 2 and "--sign" in capsys.readouterr().err
    rc = main(["board", "decide", "--id", item, "--decision", "cut", "--words", "w", "--by", "phill", "--sign"])
    assert rc == 2 and "--author-kind operator" in capsys.readouterr().err


def test_the_cli_sign_writes_a_signed_record_through_the_seal(target, rig, monkeypatch, capsys):
    item = add_item(target)
    made = rig.seal()
    monkeypatch.setattr(sg, "terminal_seal", lambda: made)
    rc = main(["board", "decide", "--id", item, "--decision", "cut", "--words", "w", "--by", "phill",
               "--author-kind", "operator", "--sign"])
    assert rc == 0, capsys.readouterr().err
    assert sg.verify_record(items(target)[-1], rig.snapshot()).state == "valid"


# ---------------------------------------------------------------------------------------------------------- the phone


def phone_rig(tmp_path) -> Rig:
    return Rig(tmp_path, authority.DISPLAY_PHONE, label="phone")


def test_a_phone_signature_over_the_prepared_bytes_is_appended_as_signed_phone(target, tmp_path):
    phone = phone_rig(tmp_path)
    item = add_item(target)
    kw = dict(item_id=item, decision="cut", words="no", by="phill", author=OPERATOR)
    prepared = sg.prepare(target, "decide", **kw)
    assert len(items(target)) == 1                         # preparing writes nothing
    sg.commit_phone(target, prepared, phone.sign(prepared.message), phone.snapshot())
    rec = items(target)[-1]
    assert rec["grade"] == "signed-phone" and rec["sig"]["via"] == "phone" and rec["prev"] == prepared.prev
    v = sg.verify_record(rec, phone.snapshot())
    assert v.state == "valid" and v.text == f"signed-phone by {phone.fp}, valid"


def test_the_phone_path_refuses_when_the_chain_head_moved(target, tmp_path):
    phone = phone_rig(tmp_path)
    item = add_item(target)
    prepared = sg.prepare(target, "decide", item_id=item, decision="cut", words="no", by="phill", author=OPERATOR)
    sig = phone.sign(prepared.message)
    bs.amend(target, item, "why", "phill", {"title": "moved"}, now=NOW, author=OPERATOR)         # the head moves
    with pytest.raises(bs.BoardError, match="the board changed while you signed: sign again"):
        sg.commit_phone(target, prepared, sig, phone.snapshot())
    assert len(items(target)) == 2


def test_the_phone_path_refuses_a_bad_wrong_namespace_unenrolled_or_laptop_class_signature(target, tmp_path):
    phone = phone_rig(tmp_path)
    item = add_item(target)
    prepared = sg.prepare(target, "decide", item_id=item, decision="cut", words="no", by="phill", author=OPERATOR)
    good = phone.sign(prepared.message)
    wrong_ns = ckeys.ssh_signer(phone.path, namespace="levain-consent-answer")[1](prepared.message)
    other_bytes = phone.sign(prepared.message + b"x")
    stranger = Rig(tmp_path / "s", authority.DISPLAY_PHONE, label="stranger")
    laptop_listed = Rig(tmp_path / "l", authority.LAPTOP, label="laptop")
    for sig, snap in ((wrong_ns, phone.snapshot()), (other_bytes, phone.snapshot()), (good, stranger.snapshot()),
                      (laptop_listed.sign(prepared.message), laptop_listed.snapshot()), ("not a signature", phone.snapshot()),
                      (good, phone.snapshot(revoked_seq=3))):
        with pytest.raises(bs.BoardError):
            sg.commit_phone(target, prepared, sig, snap)
    assert len(items(target)) == 1


def test_a_phone_signed_decide_for_an_agent_author_is_refused(target, tmp_path):
    item = add_item(target)
    with pytest.raises(bs.BoardError, match="author kind must be operator"):
        sg.prepare(target, "decide", item_id=item, decision="cut", words="no", by="seat-1", author=AGENT)


# ------------------------------------------------------------------------------------------------------- P2-d the cause


def abandon_line(target: bs.Target, *, repo="levain", grade="signed-terminal", op="abandon-track") -> str:
    line = json.dumps({"v": 1, "op": op, "repo": repo, "at": NOW, "grade": grade}, separators=(",", ":"))
    with open(target.store_dir / "repos.jsonl", "ab") as f:
        f.write(line.encode() + b"\n")
    return hashlib.sha256(line.encode()).hexdigest()


def cut(target, item, cause):
    return bs.decide(target, item, "cut", "abandoned", "seat-1", author=AGENT, now=NOW, cause=cause)


def test_a_cut_caused_by_a_signed_abandon_of_the_same_repo_is_accepted_asserted(target):
    item = add_item(target)
    cause = abandon_line(target)
    cut(target, item, cause)
    rec = items(target)[-1]
    assert rec["grade"] == "asserted" and rec["cause"] == cause
    abandon_line(target, grade="relayed")
    cut(target, item, hashlib.sha256(
        (target.store_dir / "repos.jsonl").read_bytes().split(b"\n")[1]).hexdigest())


def test_a_cut_whose_cause_does_not_hold_is_refused(target):
    item = add_item(target)
    cases = {
        "unsigned abandon": abandon_line(target, grade="asserted"),
        "another repo": abandon_line(target, repo="flow"),
        "not an abandon": abandon_line(target, op="register"),
        "no such record": "f" * 64,
    }
    for why, cause in cases.items():
        with pytest.raises(bs.BoardError, match="--sign"):
            cut(target, item, cause)
    with pytest.raises(bs.BoardError, match="sha256"):
        cut(target, item, "nope")
    assert len(items(target)) == 1


def test_a_cause_is_refused_on_anything_but_a_cut(target, rig):
    item = add_item(target)
    cause = abandon_line(target)
    with pytest.raises(bs.BoardError, match="cut only"):
        bs.decide(target, item, "parked", "w", "phill", author=OPERATOR, now=NOW, cause=cause, seal=rig.seal())


def test_the_cause_check_reads_the_hash_of_the_line_as_written(target):
    item = add_item(target)
    abandon_line(target)
    line = (target.store_dir / "repos.jsonl").read_bytes().split(b"\n")[0]
    cut(target, item, hashlib.sha256(line).hexdigest())
    with pytest.raises(bs.BoardError):
        cut(target, item, hashlib.sha256(line + b" ").hexdigest())


def test_the_namespace_is_registered():
    from levain.contracts.namespaces import REGISTRY
    assert REGISTRY["levain-board"][:2] == ("shipped", "levain/desk/board_sign.py") and sg.NAMESPACE == "levain-board"

