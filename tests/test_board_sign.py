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

    def source(self, revoked_seq: int | None = None):
        """An authority SOURCE (what the writer replays after the ceremony) whose log enrols this key."""
        return lambda: self.snapshot(revoked_seq)

    def seal(self, **kw) -> sg.Seal:
        def factory(path, bound):
            return ckeys.ssh_signer(path, namespace=sg.NAMESPACE, timeout=bound)
        kw.setdefault("authority", self.source())
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
    seal = sg.terminal_seal(key_dir=rig.dir, signer_factory=cancelled, remote=lambda: None, in_agent_session=lambda: False,
                            authority=rig.source())
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
                  seal=sg.terminal_seal(key_dir=empty, in_agent_session=lambda: False, authority=rig.source()))
    with pytest.raises(bs.BoardError, match="remote"):
        bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW,
                  seal=sg.terminal_seal(key_dir=rig.dir, remote=lambda: "ssh", in_agent_session=lambda: False,
                                        authority=rig.source()))
    with pytest.raises(bs.BoardError, match="not inside an agent session"):
        bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW,
                  seal=sg.terminal_seal(key_dir=rig.dir, remote=lambda: None, in_agent_session=lambda: True,
                                        authority=rig.source()))
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
    monkeypatch.setattr(sg, "terminal_seal", lambda **kw: made)
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: rig.source())
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
    sg.commit_phone(target, prepared, phone.sign(prepared.message), phone.source())
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
        sg.commit_phone(target, prepared, sig, phone.source())
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
            sg.commit_phone(target, prepared, sig, lambda: snap)
    assert len(items(target)) == 1


def test_a_phone_signed_decide_for_an_agent_author_is_refused(target, tmp_path):
    item = add_item(target)
    with pytest.raises(bs.BoardError, match="author kind must be operator"):
        sg.prepare(target, "decide", item_id=item, decision="cut", words="no", by="seat-1", author=AGENT)




# ------------------------------------------------------------------------- the grade is earned by a check (G1, G5, G8)


def snap(*rigs: Rig, revoked: tuple[Rig, ...] = ()) -> AuthoritySnapshot:
    keys = {r.fp: AuthorityKey(fingerprint=r.fp, public_key=r.public, key_class=r.key_class, enrolled_seq=1,
                               revoked_seq=5 if r in revoked else None) for r in rigs}
    return AuthoritySnapshot(store_id="s", seq=6, head_hash="h", keys=keys, policy=StandingPolicy())


def revoking_factory(state: dict, key: str = "revoked"):
    """A signer factory whose signing also revokes (the operator revoking a key while the prompt is up)."""
    def factory(path, bound):
        fp, sign = ckeys.ssh_signer(path, namespace=sg.NAMESPACE, timeout=bound)

        def signing(message):
            state["calls"] = state.get("calls", 0) + 1
            state[key] = True
            return sign(message)
        return fp, signing
    return factory


def test_a_terminal_signature_by_an_unenrolled_revoked_or_wrong_class_key_writes_nothing(target, rig, tmp_path):
    item = add_item(target)
    before = (target.store_dir / "items.jsonl").read_bytes()
    stranger = Rig(tmp_path / "x", label="other")
    as_phone = Rig(tmp_path / "p", authority.DISPLAY_PHONE, label="ph")
    wrong_class = AuthoritySnapshot(store_id="s", seq=2, head_hash="h", policy=StandingPolicy(), keys={
        rig.fp: AuthorityKey(fingerprint=rig.fp, public_key=rig.public, key_class=authority.DISPLAY_PHONE, enrolled_seq=1)})
    for source, why in ((stranger.source(), "not a key in the authority log"), (rig.source(revoked_seq=4), "revoked at seq 4"),
                        (lambda: wrong_class, "is not a terminal key"), (as_phone.source(), "not a key in the authority log")):
        with pytest.raises(bs.BoardError, match=why):
            bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=rig.seal(authority=source))
    assert (target.store_dir / "items.jsonl").read_bytes() == before


def test_a_key_revoked_while_the_prompt_is_up_writes_nothing(target, rig):
    item = add_item(target)
    state: dict = {}
    seal = sg.terminal_seal(key_dir=rig.dir, signer_factory=revoking_factory(state), remote=lambda: None,
                            in_agent_session=lambda: False,
                            authority=lambda: rig.snapshot(revoked_seq=9 if state.get("revoked") else None))
    with pytest.raises(bs.BoardError, match="revoked at seq 9"):
        bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=seal)
    assert state["calls"] == 1 and len(items(target)) == 1


def test_no_authority_log_refuses_a_signed_write_before_any_prompt(target, rig, tmp_path, capsys):
    item = add_item(target)
    state: dict = {}
    seal = sg.terminal_seal(key_dir=rig.dir, signer_factory=revoking_factory(state), remote=lambda: None,
                            in_agent_session=lambda: False)
    with pytest.raises(bs.BoardError, match="a signature nobody can verify is not a signed grade"):
        bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=seal)
    assert "calls" not in state and len(items(target)) == 1
    with pytest.raises(bs.BoardError, match="holds no cockpit.db"):
        sg.resolve_authority(data_root=tmp_path / "nowhere")
    with pytest.raises(bs.BoardError, match="name none cockpits"):
        sg.resolve_authority(queue_dir=tmp_path / "noqueue")
    rc = main(["board", "decide", "--id", item, "--decision", "cut", "--words", "w", "--by", "phill",
               "--author-kind", "operator", "--sign"])
    assert rc == 2 and "nobody can verify" in capsys.readouterr().err


def test_a_seal_that_only_claims_a_grade_is_refused(target, rig):
    item = add_item(target)

    class Stamp(sg.Seal):
        via = "terminal"
        authority = staticmethod(rig.source())

        def __call__(self, record):
            return {**record, "sig": {"sshsig": "stand-in", "fingerprint": rig.fp, "via": "terminal"}}

    class Rewrites(Stamp):
        def __call__(self, record):
            return rig.seal()({**record, "words": "other"})
    for seal, why in ((Stamp(), "signature does not verify"), (Rewrites(), "other than the one built")):
        with pytest.raises(bs.BoardError, match=why):
            bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=seal)
    assert len(items(target)) == 1


def test_a_phone_key_revoked_during_the_ceremony_is_not_appended_and_a_snapshot_is_refused(target, tmp_path):
    phone = phone_rig(tmp_path)
    item = add_item(target)
    state: dict = {}
    prepared = sg.prepare(target, "decide", item_id=item, decision="cut", words="no", by="phill", author=OPERATOR)
    sig = phone.sign(prepared.message)
    state["revoked"] = True                                          # revoked while the phone was signing
    with pytest.raises(bs.BoardError, match="revoked at seq 3"):
        sg.commit_phone(target, prepared, sig, lambda: phone.snapshot(revoked_seq=3 if state.get("revoked") else None))
    with pytest.raises(bs.BoardError, match="not a snapshot"):
        sg.commit_phone(target, prepared, sig, phone.snapshot())
    assert len(items(target)) == 1


def test_a_store_that_moved_during_the_prompt_is_refused_without_a_second_prompt(target, rig):
    item = add_item(target)
    path = target.store_dir / "items.jsonl"
    first = path.read_bytes().split(b"\n")[0] + b"\n"
    for move in ("append", "replace"):
        state: dict = {}
        base = revoking_factory(state, key="x")

        def factory(p, bound, move=move, base=base):
            fp, sign = base(p, bound)

            def signing(message):
                out = sign(message)
                if move == "append":                    # a lock-free writer appends while the prompt is up
                    with open(path, "ab") as f:
                        f.write(first)
                else:                                   # the file is replaced (a git operation)
                    tmp = path.with_suffix(".tmp")
                    tmp.write_bytes(path.read_bytes())
                    tmp.replace(path)
                return out
            return fp, signing
        seal = sg.terminal_seal(key_dir=rig.dir, signer_factory=factory, remote=lambda: None, in_agent_session=lambda: False,
                                authority=rig.source())
        with pytest.raises(bs.BoardError, match="the board changed while you signed: sign again"):
            bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=seal)
        assert state["calls"] == 1
    assert not any(r.get("op") == "decide" for r in items(target))


# ------------------------------------------------------------------------------------------------------ prepare (G6)


def test_prepare_freezes_one_timestamp_and_refuses_a_seal(target, tmp_path):
    phone = phone_rig(tmp_path)
    item = add_item(target)
    prepared = sg.prepare(target, "decide", item_id=item, decision="cut", words="no", by="phill", author=OPERATOR, now=None)
    assert prepared.kwargs["now"] and prepared.record["at"] == prepared.kwargs["now"]
    sg.commit_phone(target, prepared, phone.sign(prepared.message), phone.source())
    assert items(target)[-1]["grade"] == "signed-phone"
    with pytest.raises(bs.BoardError, match="a seal argument is refused"):
        sg.prepare(target, "decide", item_id=item, decision="cut", words="no", by="phill", author=OPERATOR, seal=None)


# -------------------------------------------------------------------------------------------- exact grades only (G7)


def test_only_exact_known_grades_get_a_verdict_and_a_sig_off_a_signed_grade_is_bad():
    empty = AuthoritySnapshot(store_id="s", seq=1, head_hash="h", keys={}, policy=StandingPolicy())
    base = {"v": 1, "op": "add", "id": "levain#1", "at": NOW, "by": "phill", "author": OPERATOR}
    chained = {**base, "prev": "0" * 64}
    sig = {"sshsig": "x", "fingerprint": "SHA256:x", "via": "terminal"}
    assert sg.verify_record({**chained, "grade": "operator"}, empty).state == "bad"
    assert sg.verify_record({**chained, "grade": "Asserted"}, empty).state == "bad"
    assert sg.verify_record(chained, empty).state == "bad"                              # chained, no grade
    assert sg.verify_record({**base, "sig": sig}, empty).state == "bad"                 # legacy-shaped, signed
    assert sg.verify_record({**base, "grade": "legacy", "sig": sig}, empty).state == "bad"
    assert sg.verify_record({**chained, "grade": "relayed", "sig": sig}, empty).state == "bad"
    assert sg.verify_record({**chained, "grade": "asserted", "sig": sig}, empty).state == "bad"
    assert sg.verify_record(base, empty).state == "legacy"
    assert sg.verify_record({**chained, "grade": "asserted"}, empty).state == "asserted"
    assert sg.verify_record({**chained, "grade": "relayed"}, empty).state == "relayed"


# ---------------------------------------------------------------------------------------- P2-d the cause (G2, G3, G4)


def items_last(target: bs.Target) -> str:
    """The abandon's ``items_last`` as the verb writes it: the sha256 of the items file's last line, ``none`` with none."""
    p = target.store_dir / "items.jsonl"
    tail = [ln for ln in (p.read_bytes() if p.exists() else b"").split(b"\n") if ln]
    return hashlib.sha256(tail[-1]).hexdigest() if tail else sg.ITEMS_NONE


def signed_abandon(target: bs.Target, signer: Rig, *, migration="M1", grade="signed-terminal", op="abandon-track",
                   sign=True, junk=False, repo="levain", last: str | None = "now") -> str:
    """An abandon-track line written raw into repos.jsonl (the verb is T2's), signed by ``signer``; returns its cause.
    ``last`` is its ``items_last``: ``"now"`` (default) names the items file's last line as of this call; None omits it."""
    rec = {"v": 1, "op": op, "repo": repo, "at": NOW, "prev": "0" * 64, "by": "phill", "migration": migration,
           "words": "abandon", "author": OPERATOR, "grade": grade}
    if last is not None:
        rec["items_last"] = items_last(target) if last == "now" else last
    if sign:
        armored = "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n" if junk else signer.sign(sg.signing_bytes(rec))
        rec = {**rec, "sig": {"sshsig": armored, "fingerprint": signer.fp, "via": "terminal"}}
    line = bs.canonical_bytes(rec)
    with open(target.store_dir / "repos.jsonl", "ab") as f:
        f.write(line + b"\n")
    return hashlib.sha256(line).hexdigest()


def keyed_item(target: bs.Target, key: str) -> str:
    return bs.add(target, "levain", "an item", "desk 1", "phill", author=OPERATOR, now=NOW, key=key,
                  payload_hash=hashlib.sha256(key.encode()).hexdigest())[0]


def cut(target, item, cause, source):
    return bs.decide(target, item, "cut", "abandoned", "seat-1", author=AGENT, now=NOW, cause=cause, authority=source)


def test_a_cut_caused_by_a_valid_signed_abandon_of_the_items_migration_is_accepted_asserted(target, rig):
    plan, exact = keyed_item(target, "M1:plan-1"), keyed_item(target, "M1")
    cause = signed_abandon(target, rig)
    for item in (plan, exact):
        cut(target, item, cause, rig.source())
        rec = items(target)[-1]
        assert rec["grade"] == "asserted" and rec["cause"] == cause and rec["id"] == item
    lines = [ln for name in ("items.jsonl", "repos.jsonl") for ln in (target.store_dir / name).read_bytes().split(b"\n") if ln]
    assert sg.verify_cause(items(target)[-2], lines, rig.snapshot()) is None            # P6's entry point: both files' lines


def test_a_cut_whose_cause_does_not_hold_is_refused(target, rig, tmp_path):
    item = keyed_item(target, "M1:plan-1")
    other_m = keyed_item(target, "M10:plan-1")                    # M10 is not M1
    unkeyed = add_item(target)
    good = signed_abandon(target, rig)
    cases = [
        (item, signed_abandon(target, rig, grade="asserted", sign=False), rig.source(), "graded 'asserted'"),
        (item, signed_abandon(target, rig, sign=False), rig.source(), "claims a signature it does not carry"),
        (item, signed_abandon(target, rig, junk=True), rig.source(), "signature does not verify"),
        (item, signed_abandon(target, rig, migration="M2"), rig.source(), "is not keyed to it"),
        (other_m, good, rig.source(), "is not keyed to it"),
        (unkeyed, good, rig.source(), "is not keyed to it"),
        (item, signed_abandon(target, rig, op="register"), rig.source(), "not an abandon-track"),
        (item, "f" * 64, rig.source(), "no abandon-track record"),
        (item, good, rig.source(revoked_seq=2), "revoked at seq 2"),
        (item, good, Rig(tmp_path / "s", label="s").source(), "not a key in the authority log"),
        (item, good, None, "no authority log"),
        (item, signed_abandon(target, rig, grade="relayed", sign=False), rig.source(), "P3's verifier"),
    ]
    for it, cause, source, why in cases:
        with pytest.raises(bs.BoardError, match="--sign") as exc:
            cut(target, it, cause, source)
        assert why in str(exc.value), (why, str(exc.value))
    with pytest.raises(bs.BoardError, match="sha256"):
        cut(target, item, "nope", rig.source())
    assert not any(r["op"] == "decide" for r in items(target))


def test_a_signed_cut_has_its_cause_checked_too(target, rig):
    item = keyed_item(target, "M1:plan-1")
    junk = signed_abandon(target, rig, junk=True)
    with pytest.raises(bs.BoardError, match="the cause does not hold"):
        bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, cause=junk, seal=rig.seal())
    assert not any(r["op"] == "decide" for r in items(target))


def test_a_signed_cut_rechecks_its_cause_after_the_prompt(target, rig, tmp_path):
    abandoner = Rig(tmp_path / "ab", label="abandoner")
    item = keyed_item(target, "M1:plan-1")
    cause = signed_abandon(target, abandoner)
    state: dict = {}
    seal = sg.terminal_seal(key_dir=rig.dir, signer_factory=revoking_factory(state), remote=lambda: None,
                            in_agent_session=lambda: False,
                            authority=lambda: snap(rig, abandoner, revoked=(abandoner,) if state.get("revoked") else ()))
    with pytest.raises(bs.BoardError, match="not written: the cause does not hold: .*revoked"):
        bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, cause=cause, seal=seal)
    assert state["calls"] == 1 and not any(r["op"] == "decide" for r in items(target))


def test_a_cause_is_refused_on_anything_but_a_cut(target, rig):
    item = keyed_item(target, "M1:plan-1")
    cause = signed_abandon(target, rig)
    with pytest.raises(bs.BoardError, match="cut only"):
        bs.decide(target, item, "parked", "w", "phill", author=OPERATOR, now=NOW, cause=cause, seal=rig.seal())


def test_the_cause_check_reads_the_hash_of_the_line_as_written(target, rig):
    item = keyed_item(target, "M1:plan-1")
    signed_abandon(target, rig)
    line = (target.store_dir / "repos.jsonl").read_bytes().split(b"\n")[0]
    with pytest.raises(bs.BoardError):
        cut(target, item, hashlib.sha256(line + b"\n").hexdigest(), rig.source())     # the newline is not in the range
    cut(target, item, hashlib.sha256(line).hexdigest(), rig.source())


def test_the_cli_checks_an_unsigned_cause_against_the_authority_log(target, rig, monkeypatch, capsys):
    item = keyed_item(target, "M1:plan-1")
    cause = signed_abandon(target, rig)
    argv = ["board", "decide", "--id", item, "--decision", "cut", "--words", "w", "--by", "seat-1", "--cause", cause]
    assert main(argv) == 2 and "nobody can verify" in capsys.readouterr().err          # this install has no authority log
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: rig.source())
    assert main(argv) == 0, capsys.readouterr().err
    assert items(target)[-1]["cause"] == cause


def test_a_cause_binds_the_abandons_repo_and_only_items_added_before_it_l2_m2(target, rig):
    """L2 M2 (levain 26): the cut's item must be of the abandon's repo and its add must predate the abandon (at or before
    the items line the abandon names in ``items_last``). Run by hand first on a scratch store: each case refused."""
    before = keyed_item(target, "M1:plan-1")
    elsewhere = signed_abandon(target, rig, repo="other")                       # repo "other", migration M1; the item is levain's
    no_last = signed_abandon(target, rig, last=None)                            # an abandon written before items_last existed
    empty_then = signed_abandon(target, rig, last=sg.ITEMS_NONE)                # the items file was empty at the abandon
    gone = signed_abandon(target, rig, last="e" * 64)                           # names an items line that is not in the store
    good = signed_abandon(target, rig)
    reg = target.store_dir / "repos.jsonl"
    reg.rename(reg.with_name("aside"))                  # these raw abandons are no registry the add's I1 check could read
    after = keyed_item(target, "M1:plan-2")                                     # added AFTER every abandon above
    reg.with_name("aside").rename(reg)
    for item, cause, why in ((before, elsewhere, "was added for 'levain'"), (before, no_last, "items_last"),
                             (before, empty_then, "was added after"), (before, gone, "is not in the store"),
                             (after, good, "was added after")):
        with pytest.raises(bs.BoardError, match="--sign") as exc:
            cut(target, item, cause, rig.source())
        assert why in str(exc.value), (why, str(exc.value))
    assert not any(r["op"] == "decide" for r in items(target))
    cut(target, before, good, rig.source())                                     # the item that predates it: accepted
    assert items(target)[-1]["cause"] == good


# ------------------------------------------------------------------------------ a real authority log's keys (G10)


def test_signatures_resolve_in_a_real_replayed_authority_log(target, rig, tmp_path):
    """The fingerprint the terminal signer reports and the one ``sshsig_public_key`` reads off a phone signature are the
    keys of a REAL authority log, replayed by the same source the envelope store's accept path reads."""
    import sqlite3
    from datetime import datetime, timezone

    from levain.authority import ReplayCache, anchor_path, sign_and_append
    from levain.authority_sig import sshsig_public_key
    from levain.cockpit.firejournal import CockpitDB

    phone = phone_rig(tmp_path)
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        for r, cls, custody in ((rig, authority.LAPTOP, "passphrase"), (phone, authority.DISPLAY_PHONE, "unstated")):
            sign_and_append(conn, {"op": "enrol", "fingerprint": r.fp, "public_key": r.public, "class": cls, "custody": custody},
                            signer_fp=rig.fp,
                            sign=lambda m: ckeys.ssh_signer(rig.path, namespace=authority.NAMESPACE)[1](m),
                            clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=anchor_path(root))
    finally:
        conn.close()
    source = sg.resolve_authority(data_root=root)
    keys = source().keys
    assert set(keys) == {rig.fp, phone.fp}
    item = add_item(target)
    bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=rig.seal(authority=source))
    prepared = sg.prepare(target, "decide", item_id=item, decision="reopen", words="back", by="phill", author=OPERATOR)
    sig = phone.sign(prepared.message)
    assert sshsig_public_key(sig)[1] in keys
    sg.commit_phone(target, prepared, sig, source)
    assert [sg.verify_record(r, source()).state for r in items(target)[1:]] == ["valid", "valid"]


def test_the_namespace_is_registered():
    from levain.contracts.namespaces import REGISTRY
    assert REGISTRY["levain-board"][:2] == ("shipped", "levain/desk/board_sign.py") and sg.NAMESPACE == "levain-board"


def test_a_deeply_nested_cited_line_is_refused_not_a_crash(target, rig):
    item = keyed_item(target, "M1:plan-1")
    line = b"[" * 100000 + b"]" * 100000
    with open(target.store_dir / "repos.jsonl", "ab") as f:
        f.write(line + b"\n")
    with pytest.raises(bs.BoardError, match="not a record"):
        cut(target, item, hashlib.sha256(line).hexdigest(), rig.source())


def test_an_append_between_the_two_post_seal_size_checks_is_refused_without_a_second_seal(target, rig, monkeypatch):
    item = add_item(target)
    path = target.store_dir / "items.jsonl"
    first = path.read_bytes().split(b"\n")[0] + b"\n"
    state: dict = {"armed": False, "fstats": 0, "appended": False}
    base = revoking_factory(state, key="x")
    real_os = bs.os

    class Hooked:
        def __getattr__(self, name):
            return getattr(real_os, name)

        @staticmethod
        def fstat(fd):
            out = real_os.fstat(fd)
            if state["armed"] and not state["appended"]:
                state["fstats"] += 1
                if state["fstats"] == 2:            # after the signed-path size check read its size, before the generic one
                    with open(path, "ab") as f:
                        f.write(first)
                    state["appended"] = True
            return out

    def factory(p, bound):
        fp, sign = base(p, bound)

        def signing(message):
            out = sign(message)
            state["armed"] = True
            return out
        return fp, signing
    seal = sg.terminal_seal(key_dir=rig.dir, signer_factory=factory, remote=lambda: None, in_agent_session=lambda: False,
                            authority=rig.source())
    monkeypatch.setattr(bs, "os", Hooked())
    with pytest.raises(bs.BoardError, match="the board changed while you signed: sign again"):
        bs.decide(target, item, "cut", "no", "phill", author=OPERATOR, now=NOW, seal=seal)
    assert state["appended"] and state["calls"] == 1
