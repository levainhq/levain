"""The phone's dispatch card (levain.dispatch.phone): a passkey signs or cancels a filed dispatch job from the phone.

A real authority log holds a laptop key and two SOFTWARE passkeys (a P-256 key that answers a WebAuthn challenge exactly
as a phone's authenticator does: authenticatorData || SHA-256(clientDataJSON), user presence and verification set), so
every signature here is one the server issued a challenge for and ``ssh-keygen`` verifies: the namespace separation
below is the verifier's verdict, not a shape check."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from levain.authority import anchor_path
from levain.cockpit.consent import utc_stamp
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import fingerprint_of
from levain.consent.answering import passkey_keys
from levain.consent.passkey import public_key_line
from levain.consent.queue import authority_source
from levain.dispatch import sign as dsign
from levain.dispatch.adopt import artifact_digest
from levain.dispatch.jobs import DB_NAME, JobStore
from levain.dispatch.phone import DispatchPhone, DispatchRefused
from tests.test_dispatch_contract import POLICY, make_repo, registry, submission
from tests._phone_fixtures import _enrol

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

RP = "localhost"
ORIGIN = "http://localhost:7428"


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class SoftPasskey:
    """A P-256 key that answers a WebAuthn ``get`` as an authenticator does (UP and UV set, no extensions)."""

    def __init__(self, rp_id: str = RP) -> None:
        self.rp_id = rp_id
        self.priv = ec.generate_private_key(ec.SECP256R1())
        spki = self.priv.public_key().public_bytes(serialization.Encoding.DER,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
        self.line = public_key_line(spki, rp_id)
        self.fp = fingerprint_of(self.line)
        self.counter = 0

    def assertion(self, challenge_b64u: str, origin: str = ORIGIN) -> dict[str, str]:
        self.counter += 1
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x05]) + self.counter.to_bytes(4, "big")
        cdj = json.dumps({"type": "webauthn.get", "challenge": challenge_b64u, "origin": origin, "crossOrigin": False},
                         separators=(",", ":")).encode()
        der = self.priv.sign(auth_data + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"authenticator_data": _b64u(auth_data), "client_data_json": _b64u(cdj), "signature": _b64u(der)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("dispatch-phone-repo"))


@pytest.fixture
def world(tmp_path: Path, repo):
    """A cockpit (a laptop-presence laptop key and a display-phone passkey), a consent dir 0700 whose claude_code hook
    names it, and the dispatch store in that same dir (the default place). ``weak`` is a passkey enrolled nowhere: the
    authority log takes passkey custody on a display-phone key only, so a passkey below T2 exists only as an injected
    key (``weak_keys``), which the class rule must still refuse."""
    laptop = tmp_path / "laptop"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(laptop)], check=True)
    lpub = " ".join((tmp_path / "laptop.pub").read_text().split()[:2])
    lfp = fingerprint_of(lpub)
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    phone, weak = SoftPasskey(), SoftPasskey()
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": lfp, "public_key": lpub,
                                     "class": "laptop-presence", "custody": "passphrase"})
    _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": phone.fp, "public_key": phone.line,
                                     "class": "display-phone", "custody": "passkey"})
    conn.close()
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    os.chmod(qdir, 0o700)
    (qdir / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(root), "away_mode": False,
                                                       "away_hold_seconds": 0}))
    source = authority_source(root / "cockpit.db", anchor=anchor_path(root))
    return {"qdir": qdir, "root": root, "repo": repo, "phone": phone, "weak": weak, "laptop_fp": lfp,
            "source": source}


def phone_for(world, *, keys=None, authority=None, page_origin: str | None = ORIGIN) -> DispatchPhone:
    src = world["source"]
    return DispatchPhone(world["qdir"], RP, page_origin,
                         keys or (lambda: {k.fingerprint: k for k in passkey_keys(src(), RP)}),
                         authority or (lambda: src))


def weak_keys(world):
    """The real passkeys plus ``weak`` as a presence-class passkey (a key the log itself would never hold)."""
    import dataclasses

    def keys():
        real = {k.fingerprint: k for k in passkey_keys(world["source"](), RP)}
        model = real[world["phone"].fp]
        weak = world["weak"]
        return {**real, weak.fp: dataclasses.replace(model, fingerprint=weak.fp, public_key=weak.line,
                                                     key_class="presence")}
    return keys


def file_job(world, *, write=("src/a.py", "src/new.py"), agent: str | None = "claude_code", **over):
    path, base = world["repo"]
    sub = submission(path, base, write=list(write), expires_at=utc_stamp(_now() + timedelta(hours=2)), **over)
    job, _cap = JobStore(world["qdir"]).file(sub, resolve_template=registry(), policy=POLICY, filer_session="sess-1",
                                            filer_agent=agent)
    return job


def refused(fn, *args):
    with pytest.raises(DispatchRefused) as exc:
        fn(*args)
    return exc.value


def sign_job(dp: DispatchPhone, key: SoftPasskey, job_id: str) -> dict:
    c = dp.step(job_id, "challenge", {"signer_fp": key.fp})
    return dp.step(job_id, "answer", {"payload": c["payload"], **key.assertion(c["challenge"])})


# --- the listing -----------------------------------------------------------------------------------------------------

def test_no_store_lists_nothing_and_creates_nothing(world, tmp_path: Path) -> None:
    dp = phone_for(world)
    assert dp.pending() == ([], None, 0)
    assert not (world["qdir"] / DB_NAME).exists()
    gone = DispatchPhone(tmp_path / "never", RP, ORIGIN, lambda: {}, lambda: world["source"])
    assert gone.pending() == ([], None, 0)
    assert not (tmp_path / "never").exists()
    # a step on a job in a store that does not exist is a 404, and still creates nothing
    err = refused(dp.step, "dj-" + "0" * 32, "challenge", {"signer_fp": world["phone"].fp})
    assert err.status == 404 and not (world["qdir"] / DB_NAME).exists()


def test_a_record_carries_what_the_card_shows_and_only_signers_that_cover_t2(world) -> None:
    job = file_job(world)
    records, note, overflow = phone_for(world).pending()
    assert note is None and overflow == 0
    (r,) = records
    c = job.contract
    assert r["id"] == job.id and r["state"] == "filed" and r["kind"] == "dispatch"
    assert (r["agent"], r["session"]) == ("claude_code", "sess-1")
    assert r["goal"] == c.goal and "\n" in r["goal"]                          # the goal in full, never cut
    assert r["digest"] == job.digest and r["repo"] == c.repo and r["base"] == c.base
    assert r["filed_at"] == job.filed_at and r["expires_at"] == c.expires_at and r["lease_end"] == job.lease_end
    assert r["template"] == c.template.to_mapping()
    assert (r["template"]["id"], r["template"]["executor"], r["template"]["provider"]) == ("fix-lint", "codex",
                                                                                            "openrouter")
    assert r["budget"] == {"tool_calls": 40, "tokens": 200000, "dollars_cents": 150}
    assert r["grant"] == ["Edit", "Write"]
    assert [e["path"] for e in r["write"]] == ["src/a.py", "src/new.py"] and r["write_count"] == 2
    assert set(r["read"][0]) == {"path", "mode", "blob"} and r["read_count"] == len(c.read)
    assert r["can_sign"] is True and r["can_cancel"] is True
    # the presence-class passkey and the laptop key are never offered: only a passkey whose class covers T2
    assert r["signers"] == [world["phone"].fp] and r["signer_note"] == ""


def test_a_terminal_job_is_absent_and_the_overflow_is_counted(world, monkeypatch) -> None:
    import levain.dispatch.phone as dphone

    a = file_job(world, write=("src/a.py",))
    b = file_job(world, write=("src/b.py",))
    c = file_job(world, write=("src/new.py",))
    store = JobStore(world["qdir"])
    store._transition(a.id, "cancelled", "planner_cancel")
    records, _note, overflow = phone_for(world).pending()
    assert [r["id"] for r in records] == sorted([b.id, c.id], key=lambda i: (store.get(i).filed_at, i), reverse=True)
    assert overflow == 0
    monkeypatch.setattr(dphone, "PENDING_CAP", 1)
    records, _note, overflow = phone_for(world).pending()
    assert len(records) == 1 and overflow == 1


def test_a_store_fault_is_a_note_never_an_empty_list(world) -> None:
    (world["qdir"] / DB_NAME).write_bytes(b"not a database at all, just bytes")
    records, note, overflow = phone_for(world).pending()
    assert records == [] and overflow == 0 and note


# --- signing and cancelling ------------------------------------------------------------------------------------------

def test_a_passkey_signs_a_filed_job_and_a_replayed_answer_is_409(world) -> None:
    job = file_job(world)
    dp = phone_for(world)
    c = dp.step(job.id, "challenge", {"signer_fp": world["phone"].fp})
    assert c["rp_id"] == RP and c["payload"]["job_id"] == job.id and c["payload"]["digest"] == job.digest
    from levain.consent.passkey import challenge as ch
    assert c["challenge"] == _b64u(ch(dsign.sign_message(c["payload"]), dsign.DISPATCH_SIGN_NAMESPACE))
    answer = {"payload": c["payload"], **world["phone"].assertion(c["challenge"])}
    done = dp.step(job.id, "answer", answer)
    assert done["state"] == "signed" and done["signer_fp"] == world["phone"].fp
    assert JobStore(world["qdir"]).get(job.id).state == "signed"
    err = refused(dp.step, job.id, "answer", answer)
    assert err.status == 409
    (r,) = dp.pending()[0]
    assert r["can_sign"] is False and r["can_cancel"] is True


def test_a_passkey_cancels_a_live_job_with_a_reason_and_a_second_cancel_is_409(world) -> None:
    job = file_job(world)
    dp = phone_for(world)
    sign_job(dp, world["phone"], job.id)
    c = dp.step(job.id, "cancel-challenge", {"signer_fp": world["phone"].fp, "reason": "wrong files"})
    from levain.consent.passkey import challenge as ch
    assert c["challenge"] == _b64u(ch(dsign.sign_message(c["payload"]), dsign.DISPATCH_CANCEL_NAMESPACE))
    answer = {"payload": c["payload"], **world["phone"].assertion(c["challenge"])}
    done = dp.step(job.id, "cancel", answer)
    assert done["state"] == "cancelled" and done["reason"] == "wrong files"
    assert refused(dp.step, job.id, "cancel", answer).status == 409
    assert refused(dp.step, job.id, "cancel-challenge", {"signer_fp": world["phone"].fp, "reason": ""}).status == 409
    assert dp.pending()[0] == []                              # terminal: off the card


# --- namespace separation, both ways ---------------------------------------------------------------------------------

def test_a_dispatch_signature_is_never_a_cancel_and_a_cancel_never_a_signature(world) -> None:
    job = file_job(world)
    dp = phone_for(world)
    key = world["phone"]
    sc = dp.step(job.id, "challenge", {"signer_fp": key.fp})
    cc = dp.step(job.id, "cancel-challenge", {"signer_fp": key.fp, "reason": ""})
    sign_assertion, cancel_assertion = key.assertion(sc["challenge"]), key.assertion(cc["challenge"])
    # a sign assertion carried on a cancel payload, and a cancel assertion on a sign payload: ssh-keygen refuses both
    assert refused(dp.step, job.id, "cancel", {"payload": cc["payload"], **sign_assertion}).status == 422
    assert refused(dp.step, job.id, "answer", {"payload": sc["payload"], **cancel_assertion}).status == 422
    # the SAME bytes signed in the other namespace: only the namespace differs, and it is refused
    from levain.consent.passkey import challenge as ch
    wrong_ns = _b64u(ch(dsign.sign_message(sc["payload"]), dsign.DISPATCH_CANCEL_NAMESPACE))
    assert refused(dp.step, job.id, "answer", {"payload": sc["payload"], **key.assertion(wrong_ns)}).status == 422
    assert JobStore(world["qdir"]).get(job.id).state == "filed"


# --- who may sign ----------------------------------------------------------------------------------------------------

def test_a_laptop_key_and_a_passkey_below_t2_are_refused_at_the_challenge(world) -> None:
    job = file_job(world)
    dp = phone_for(world, keys=weak_keys(world))
    (r,) = dp.pending()[0]
    assert r["signers"] == [world["phone"].fp]                 # the presence passkey is never offered
    for action, extra in (("challenge", {}), ("cancel-challenge", {"reason": ""})):
        err = refused(dp.step, job.id, action, {"signer_fp": world["laptop_fp"], **extra})
        assert (err.status, err.error) == (422, "no_passkey")
        err = refused(dp.step, job.id, action, {"signer_fp": world["weak"].fp, **extra})
        assert err.status == 422 and "laptop" in err.message
    # an answer naming the presence passkey as its signer, signed by it, is refused before the store is asked
    c = dp.step(job.id, "challenge", {"signer_fp": world["phone"].fp})
    payload = dict(c["payload"], signer_fp=world["weak"].fp)
    from levain.consent.passkey import challenge as ch
    forged = _b64u(ch(dsign.sign_message(payload), dsign.DISPATCH_SIGN_NAMESPACE))
    assert refused(dp.step, job.id, "answer", {"payload": payload, **world["weak"].assertion(forged)}).status == 422
    assert JobStore(world["qdir"]).get(job.id).state == "filed"


def test_exact_request_keys_unknown_jobs_and_a_bad_reason(world) -> None:
    job = file_job(world)
    dp = phone_for(world)
    fp = world["phone"].fp
    assert refused(dp.step, job.id, "challenge", {"signer_fp": fp, "extra": 1}).status == 400
    assert refused(dp.step, job.id, "challenge", {}).status == 400
    assert refused(dp.step, job.id, "cancel-challenge", {"signer_fp": fp}).status == 400
    assert refused(dp.step, job.id, "answer", {"payload": {}}).status == 400
    assert refused(dp.step, job.id, "answer", ["not", "a", "dict"]).status == 400
    assert refused(dp.step, "dj-" + "f" * 32, "challenge", {"signer_fp": fp}).status == 404
    err = refused(dp.step, job.id, "cancel-challenge", {"signer_fp": fp, "reason": "two\nlines"})
    assert err.status == 422 and "one line" in err.message
    # an assertion that is not a passkey's shape is refused before the store is asked
    c = dp.step(job.id, "challenge", {"signer_fp": fp})
    bad = {"payload": c["payload"], "authenticator_data": "AA", "client_data_json": "AA", "signature": "AA"}
    assert refused(dp.step, job.id, "answer", bad).status == 422
    assert JobStore(world["qdir"]).get(job.id).state == "filed"


def test_faults_are_503_and_say_nothing_was_signed(world, monkeypatch) -> None:
    from levain.authority import VerifierUnavailable
    from levain.consent.sign import SignRefused

    job = file_job(world)

    def no_cockpit():
        raise SignRefused("the consent hooks name more than one cockpit", 503)

    err = refused(phone_for(world, keys=no_cockpit).step, job.id, "challenge", {"signer_fp": world["phone"].fp})
    assert (err.status, err.error) == (503, "unavailable") and "more than one cockpit" in err.message
    (r,) = phone_for(world, keys=no_cockpit).pending()[0]
    assert r["signers"] == [] and "more than one cockpit" in r["signer_note"]
    dp = phone_for(world)
    c = dp.step(job.id, "challenge", {"signer_fp": world["phone"].fp})

    def down(*a, **k):
        raise VerifierUnavailable("ssh-keygen cannot run")

    monkeypatch.setattr(dsign, "verify_link_sig", down)
    err = refused(dp.step, job.id, "answer", {"payload": c["payload"], **world["phone"].assertion(c["challenge"])})
    assert err.status == 503 and "nothing was signed" in err.message
    cc = dp.step(job.id, "cancel-challenge", {"signer_fp": world["phone"].fp, "reason": ""})
    err = refused(dp.step, job.id, "cancel", {"payload": cc["payload"], **world["phone"].assertion(cc["challenge"])})
    assert err.status == 503 and f"job {job.id} is NOT cancelled" in err.message
    assert JobStore(world["qdir"]).get(job.id).state == "filed"


def test_a_store_get_that_cannot_decode_is_a_409_could_not_be_read_never_a_raw_500(world, monkeypatch) -> None:
    job = file_job(world)
    dp = phone_for(world)

    def broken(self, job_id):
        raise json.JSONDecodeError("bad row", "{", 0)      # a ValueError

    monkeypatch.setattr(JobStore, "get", broken)
    err = refused(dp.step_shown, job.id, "challenge", {"signer_fp": world["phone"].fp, "request_sha256": "a" * 64})
    assert err.status == 409 and "could not be read" in err.message and "nothing was signed" in err.message


# --- fix round 1 -----------------------------------------------------------------------------------------------------

def test_the_record_carries_every_field_the_laptop_render_shows(world) -> None:
    """What the signature binds is on the card: every field of job_view (the fields render prints) is in the record,
    under its own name (the filer under the card's ``agent`` and ``session``)."""
    from levain.dispatch.cli import job_view

    job = file_job(world)
    (r,) = phone_for(world).pending()[0]
    view = job_view(JobStore(world["qdir"]).get(job.id))
    alias = {"filer_agent": "agent", "filer_session": "session"}
    for k, v in view.items():
        assert r[alias.get(k, k)] == v, k
    t = r["template"]
    assert (t["executor"], t["provider"], t["route"]) == (job.contract.template.executor,
                                                          job.contract.template.provider, job.contract.template.route)


def test_every_planner_or_agent_string_on_the_card_is_shown_visibly(world, monkeypatch) -> None:
    """Not only goal, repo and paths: a hidden character in the template, the grant, the filer or a history cause or
    its evidence reaches the card marked, never raw."""
    from levain.dispatch import cli as dcli

    real = dcli.job_view

    def tainted(job):
        v = real(job)
        rlo = "‮"
        v["template"] = {**v["template"], "id": v["template"]["id"] + rlo}
        v["grant"] = [g + rlo for g in v["grant"]]
        v["filer_agent"], v["filer_session"] = "claude" + rlo, "sess" + rlo
        v["history"] = [{**h, "cause": f"c{rlo}", "evidence": {"note": f"e{rlo}"}} for h in v["history"]]
        return v

    monkeypatch.setattr(dcli, "job_view", tainted)
    file_job(world)
    (r,) = phone_for(world).pending()[0]
    assert "‮" not in json.dumps(r, ensure_ascii=False)
    assert r["agent"] == "claude⟨U+202E⟩" and r["history"][0]["cause"] == "c⟨U+202E⟩"


def test_a_dispatch_dir_that_is_a_file_is_a_note_never_a_raise(world, tmp_path: Path) -> None:
    flat = tmp_path / "flat"
    flat.write_text("a file, not a directory")
    dp = DispatchPhone(flat, RP, ORIGIN, lambda: {}, lambda: world["source"])
    records, note, overflow = dp.pending()
    assert records == [] and overflow == 0 and note and "cannot be read" in note
    assert flat.read_text() == "a file, not a directory"


def test_one_malformed_stored_row_is_skipped_and_counted(world) -> None:
    good = file_job(world, write=("src/a.py",))
    bad = file_job(world, write=("src/new.py",))
    conn = sqlite3.connect(world["qdir"] / DB_NAME)
    conn.execute("DROP TRIGGER jobs_frozen")                 # the store is append-only; a damaged file is not
    conn.execute("UPDATE jobs SET contract = ? WHERE id = ?", ("{not a contract", bad.id))
    conn.commit()
    conn.close()
    records, note, overflow = phone_for(world).pending()
    assert [r["id"] for r in records] == [good.id] and overflow == 0
    assert note and "1 dispatch job" in note and "not shown" in note


def test_a_lease_that_lapses_between_challenge_and_answer_is_409(world) -> None:
    from levain.dispatch.jobs import UNSIGNED_LEASE

    job = file_job(world)
    at = [_now()]
    dp = DispatchPhone(world["qdir"], RP, ORIGIN,
                       lambda: {k.fingerprint: k for k in passkey_keys(world["source"](), RP)},
                       lambda: world["source"], clock=lambda: at[0])
    c = dp.step(job.id, "challenge", {"signer_fp": world["phone"].fp})
    at[0] = at[0] + UNSIGNED_LEASE + timedelta(minutes=1)
    err = refused(dp.step, job.id, "answer", {"payload": c["payload"], **world["phone"].assertion(c["challenge"])})
    assert (err.status, err.error) == (409, "conflict") and "is failed" in err.message
    # the lapse lands inside the store's own write (the read before it still saw the job filed): still 409
    job2 = file_job(world, write=("src/b.py",))
    at[0] = _now()
    c = dp.step(job2.id, "challenge", {"signer_fp": world["phone"].fp})
    real_get = JobStore.get
    calls = []

    def get_then_lapse(self, job_id):
        out = real_get(self, job_id)
        if not calls:
            at[0] = at[0] + UNSIGNED_LEASE + timedelta(minutes=1)
        calls.append(job_id)
        return out

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(JobStore, "get", get_then_lapse)
        err = refused(dp.step, job2.id, "answer", {"payload": c["payload"], **world["phone"].assertion(c["challenge"])})
    assert (err.status, err.error) == (409, "conflict") and "nothing was signed" in err.message
    assert JobStore(world["qdir"]).get(job2.id).state == "failed"


# --- fix round 2 -----------------------------------------------------------------------------------------------------

def test_a_job_with_no_history_is_skipped_and_counted_never_blanks_the_listing(world) -> None:
    good = file_job(world, write=("src/a.py",))
    bad = file_job(world, write=("src/new.py",))
    conn = sqlite3.connect(world["qdir"] / DB_NAME)
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'transitions'").fetchall():
        conn.execute(f'DROP TRIGGER "{name}"')                # append-only in the store; a damaged file is not
    conn.execute("DELETE FROM transitions WHERE job_id = ?", (bad.id,))
    conn.commit()
    conn.close()
    records, note, overflow = phone_for(world).pending()
    assert [r["id"] for r in records] == [good.id] and overflow == 0
    assert note and "1 dispatch job" in note and bad.id in note and "not shown" in note


def test_a_signer_lookup_that_hits_the_authority_or_the_disk_is_a_note_never_a_raise(world) -> None:
    from levain.authority import AuthorityError

    file_job(world)
    for exc in (AuthorityError("the anchor cannot be read"), OSError("disk gone")):
        def broken(exc=exc):
            raise exc

        (r,) = phone_for(world, keys=broken).pending()[0]
        assert r["signers"] == [] and str(exc) in r["signer_note"]


# --- fix round 3 -----------------------------------------------------------------------------------------------------

def test_a_mixed_type_filed_at_cannot_escape_the_sort(world, monkeypatch) -> None:
    from types import SimpleNamespace

    jobs = [SimpleNamespace(id="a", state="filed", filed_at=1), SimpleNamespace(id="b", state="filed", filed_at="x")]
    monkeypatch.setattr(DispatchPhone, "_store", lambda self: SimpleNamespace(list=lambda: jobs))
    records, note, overflow = phone_for(world).pending()           # the rows fail to render; the listing does not raise
    assert records == [] and note and "not shown" in note


def test_a_keys_db_fault_is_a_signer_note_and_the_consent_cards_stay(world) -> None:
    file_job(world)

    def broken():
        raise sqlite3.OperationalError("keys db")

    records, note, overflow = phone_for(world, keys=broken).pending()
    assert note is None or isinstance(note, str)
    assert overflow == 0
    assert records and records[0]["signers"] == [] and "keys db" in records[0]["signer_note"]


# --- F11-s2b: the authorise card --------------------------------------------------------------------------------------

@pytest.fixture
def lwp(tmp_path: Path):
    """tests/test_dispatch_adopt.py's LandWorld (real cockpit, control store, rule, repo, job store) with a software
    passkey enrolled as display-phone, and the phone's dispatch routes over its consent directory."""
    from tests.test_dispatch_adopt import LandWorld

    lw = LandWorld(tmp_path)
    lw.passkey = SoftPasskey()
    lw.log.link({"op": "enrol", "fingerprint": lw.passkey.fp, "public_key": lw.passkey.line,
                 "class": "display-phone", "custody": "passkey"})
    src = lw.log.source
    lw.dp = DispatchPhone(lw.ddir, RP, ORIGIN, lambda: {k.fingerprint: k for k in passkey_keys(src(), RP)},
                          lambda: src)
    return lw


def recommended(lw, change=None, **proposal):
    job, cap = lw.verified(change)
    return lw.jstore.recommend(job.id, cap, **proposal)


def card(lw, job_id):
    (row,) = [r for r in lw.dp.pending()[0] if r["id"] == job_id]
    return row["authorise"]


def test_a_recommended_job_with_a_branch_carries_the_authorise_card(lwp) -> None:
    job = recommended(lwp, {"src/a.py": b"a = 2\n", "src/new.py": b"n = 1\nm = 2\n"}, onto="main")
    got = card(lwp, job.id)
    assert (got["onto"], got["run"], got["tier"], got["problem"]) == ("main", 0, "T2", None)
    assert got["signers"] == [lwp.passkey.fp] and got["note"] == ""
    assert got["files"] == 2 and got["changed_lines"] == 4 and got["change_overflow"] == 0
    assert got["changes"] == [
        {"path": "src/a.py", "status": "M", "added": 1, "deleted": 1, "binary": False, "gitlink": False},
        {"path": "src/new.py", "status": "A", "added": 2, "deleted": 0, "binary": False, "gitlink": False}]
    assert got["staged"] == lwp.jstore.run_record(job.id, 0).staged_commit


@pytest.mark.parametrize("where", ["_judged", "_covering"])
def test_a_card_that_cannot_be_read_keeps_its_branch_and_run(lwp, monkeypatch, where) -> None:
    # L3 r1 G: the card is bound before the try; a failure sets its problem and keeps onto/run, never an unbound name
    import sqlite3

    job = recommended(lwp, onto="main", run=1)
    monkeypatch.setattr(lwp.dp, where, lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    got = card(lwp, job.id)
    assert (got["onto"], got["run"], got["signers"]) == ("main", 1, [])
    assert got["problem"] == "the authorise card cannot be read now (OperationalError: locked)"


def test_no_branch_proposed_or_not_recommended_is_no_card(lwp) -> None:
    job = recommended(lwp)
    assert card(lwp, job.id) is None
    err = refused(lwp.dp.step, job.id, "authorise-challenge", {"signer_fp": lwp.passkey.fp})
    assert (err.status, err.error) == (409, "conflict") and "from the laptop" in err.message


def test_a_passkey_authorises_the_cards_run_and_it_lands(lwp) -> None:
    job = recommended(lwp, onto="main", run=1)
    c = lwp.dp.step(job.id, "authorise-challenge", {"signer_fp": lwp.passkey.fp})
    assert (c["payload"]["run"], c["payload"]["onto"], c["tier"]) == ("1", "main", "T2")
    assert c["payload"]["artifact_digest"] == artifact_digest(lwp.jstore.run_record(job.id, 1))
    done = lwp.dp.step(job.id, "authorise", {"payload": c["payload"], **lwp.passkey.assertion(c["challenge"])})
    assert (done["outcome"], done["state"], done["moved"]) == ("adopted", "adopted", True), done
    assert lwp.main() == lwp.jstore.run_record(job.id, 1).staged_commit
    assert lwp.jstore.get(job.id).history[-2].cause == "human_authorises"


@pytest.mark.parametrize("field, value", [("onto", "work"), ("run", "0"), ("artifact_digest", "sha256:" + "0" * 64)])
def test_a_payload_the_server_did_not_build_is_refused(lwp, field, value) -> None:
    job = recommended(lwp, onto="main", run=1)
    c = lwp.dp.step(job.id, "authorise-challenge", {"signer_fp": lwp.passkey.fp})
    forged = {**c["payload"], field: value}
    err = refused(lwp.dp.step, job.id, "authorise", {"payload": forged, **lwp.passkey.assertion(c["challenge"])})
    assert err.status == 422 and "not the one this server built" in err.message
    assert lwp.jstore.get(job.id).state == "recommended" and lwp.main() != lwp.jstore.run_record(job.id, 1).staged_commit


def test_an_assertion_over_another_challenge_is_a_refused_land(lwp) -> None:
    job = recommended(lwp, onto="main")
    c = lwp.dp.step(job.id, "authorise-challenge", {"signer_fp": lwp.passkey.fp})
    other = lwp.dp.step(job.id, "authorise-challenge", {"signer_fp": lwp.passkey.fp})
    err = refused(lwp.dp.step, job.id, "authorise", {"payload": other["payload"],
                                                     **lwp.passkey.assertion(c["challenge"])})
    assert err.status == 422 and lwp.jstore.get(job.id).state == "recommended"


def test_a_t3_change_needs_the_laptop_key(lwp, monkeypatch) -> None:
    from levain.dispatch import cli as dcli

    job = recommended(lwp, onto="main")
    monkeypatch.setattr(dcli, "_landing_tier", lambda directory: (lambda repo, paths: "T3"))
    got = card(lwp, job.id)
    assert got["tier"] == "T3" and got["signers"] == [] and got["note"].startswith("needs your laptop key")
    err = refused(lwp.dp.step, job.id, "authorise-challenge", {"signer_fp": lwp.passkey.fp})
    assert err.status == 422 and "needs your laptop key" in err.message
    assert lwp.jstore.get(job.id).state == "recommended"


def test_a_card_that_cannot_be_read_is_its_problem_never_the_listings(lwp, monkeypatch) -> None:
    from levain.dispatch import adopt

    job = recommended(lwp, onto="main")

    def gone(*a, **k):
        raise adopt.GitFault("git is gone")

    monkeypatch.setattr(adopt, "change_set", gone)
    got = card(lwp, job.id)
    assert got["signers"] == [] and "git is gone" in got["problem"]
    err = refused(lwp.dp.step, job.id, "authorise-challenge", {"signer_fp": lwp.passkey.fp})
    assert (err.status, err.error) == (503, "unavailable") and "nothing was landed" in err.message
