"""PIN-1: the park-pinned identity gate (PIN_DESIGN §3-§6), on the g3 gate harness.

A pin records the exact credential bytes a live call last vouched for as the baseline account. An unchanged parked
login passes with no call; anything else is the strict path. Nothing here reaches the vendor's servers.
"""
from __future__ import annotations

import hashlib
import os

import pytest

from levain.contracts import canon
from levain.slot import identity as ID
from levain.slot import start as SS
from tests.test_slot_g3_gate import (E0, OTHER, PASS, UUID, _ans, _gated, _keep, _tree, env, _la,  # noqa: F401
                                     _baseline_path)
from tests.test_slot_s2f3_start import _blob, _rendered
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

BASE_SESSION = "s-00000000000000ba"      # give_credential's baseline session
CRED524 = b"{" + b"a" * 522 + b"}"        # the P2 run's parked file size


def _pin_path(w):
    return E0.slot_dir(w.L) / "pin"


def _pin_data(path, **over) -> bytes:
    raw = path.read_bytes()
    st = os.stat(path)
    f = dict(observed_id=UUID, baseline_session=BASE_SESSION, sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
             ino=st.st_ino, session="s-00000000000000aa", pinned_at="2026-10-27T00:00:00Z")
    f.update(over)
    return ID.pin_bytes(**f)


def _put_pin(w, path=None, **over) -> bytes:
    data = _pin_data(path or _keep(w), **over)
    _pin_path(w).write_bytes(data)
    _pin_path(w).chmod(0o600)
    return data


class Calls:
    """Counts the identity calls made before the vendor spawned (the gate's) and after (the pin's), and answers
    them; ``hook`` runs inside each post-vendor call, as a child that touched the file would."""

    def __init__(self, w, la, answer=None):
        self.w, self.la, self.gate, self.post = w, la, 0, 0
        self.answer = answer or (lambda: (0, _ans(200)))
        self.hook = None
        self.post_answer = None          # the pin's call (after the vendor) answers this when set
        w.seams.capture = self.capture

    def capture(self, argv, spec_json, cgroup, bound):
        if canon.parse(spec_json)["target"] != "identity":
            return SS.H.capture_target(argv, spec_json, cgroup, bound)
        if self.la.of("vendor"):
            self.post += 1
            if self.hook:
                self.hook()
            return (self.post_answer or self.answer)()
        else:
            self.gate += 1
        return self.answer()


def _vendor_runs(w, then):
    """``then()`` runs as the vendor's session body, once, when the helper waits for the vendor."""
    real = w.seams.wait
    done = []

    def wait(pid):
        if not done:
            done.append(1)
            then()
        return real(pid)
    w.seams.wait = wait


def _rewrite(path, data):
    tmp = path.with_name(path.name + ".new")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _gated_with(w, la, data):
    s = _gated(w, la)
    _keep(w).write_bytes(data)
    return s


# --- T1: the reproduced state -------------------------------------------------------------------------------------------

def test_t1_a_pinned_parked_login_passes_with_no_call_and_without_the_pin_refuses_expired(env):
    w, la, _ = env
    s = _gated_with(w, la, CRED524)
    calls = Calls(w, la, lambda: (0, _ans(401)))
    pin = _put_pin(w)
    assert w.main(["start", s])[1] == {"ok": True, "exit": 0}
    assert calls.gate == 0 and len(la.of("vendor")) == 1
    assert w.state(s).fields["identity"] == {"observed_id": UUID, "result": "pinned"}
    assert _pin_path(w).read_bytes() == pin          # the post-session 401 wrote nothing and deleted nothing
    assert w.main(["teardown", s])[1]["ok"]

    _pin_path(w).unlink()
    s2 = _rendered(w, "egress", "interactive", "claude", "passthrough", blob=_blob(vendor="claude", routes=PASS))
    assert w.main(["start", s2])[1]["refused"] == "identity-expired"
    assert w.state(s2).fields["identity"] == {"result": "identity-expired"}
    assert _keep(w).read_bytes() == CRED524 and not _tree(w, s2).exists()


# --- T2: every reason the pin is not used -------------------------------------------------------------------------------

def _byte(w):
    _keep(w).write_bytes(b"{" + b"b" * 522 + b"}")


def _ino(w):
    _rewrite(_keep(w), CRED524)


def _absent(w):
    _pin_path(w).unlink()


def _malformed(w):
    _pin_path(w).write_bytes(b"{not json")


def _symlinked(w):
    real = _pin_path(w).with_name("elsewhere")
    real.write_bytes(_pin_path(w).read_bytes())
    _pin_path(w).unlink()
    os.symlink(real, _pin_path(w))


def _not_roots(w):
    _pin_path(w).chmod(0o666)


def _oversize(w):
    obj = canon.parse(_pin_path(w).read_bytes())
    obj["pad"] = "x" * ID.PIN_MAX_BYTES
    _pin_path(w).write_bytes(canon.canonical_bytes(obj))


def _stale_session(w):
    _put_pin(w, baseline_session="s-00000000000000bb")


def _stale_id(w):
    _put_pin(w, observed_id=OTHER)


@pytest.mark.parametrize("damage", [_byte, _ino, _absent, _malformed, _symlinked, _not_roots, _oversize,
                                    _stale_session, _stale_id])
def test_t2_an_unusable_pin_makes_the_live_call(env, damage):
    w, la, _ = env
    s = _gated_with(w, la, CRED524)
    calls = Calls(w, la)
    _put_pin(w)
    damage(w)
    assert w.main(["start", s])[1] == {"ok": True, "exit": 0}
    assert calls.gate == 1 and len(la.of("vendor")) == 1
    assert w.state(s).fields["identity"] == {"observed_id": UUID, "result": "matched"}


@pytest.mark.parametrize("damage", [_byte, _ino, _absent, _malformed, _symlinked, _not_roots, _oversize,
                                    _stale_session, _stale_id])
def test_t2_an_unusable_pin_and_a_401_refuses_expired_with_the_credential_back(env, damage):
    w, la, _ = env
    s = _gated_with(w, la, CRED524)
    calls = Calls(w, la, lambda: (0, _ans(401)))
    _put_pin(w)
    damage(w)
    before = _pin_path(w).read_bytes() if _pin_path(w).exists() else None
    assert w.main(["start", s])[1]["refused"] == "identity-expired"
    assert calls.gate == 1 and not la.of("vendor")
    assert _keep(w).exists() and not _tree(w, s).exists()
    assert (_pin_path(w).read_bytes() if _pin_path(w).exists() else None) == before


# --- T3: N -> N+1 -------------------------------------------------------------------------------------------------------

def test_t3_the_pin_follows_a_refresh_and_the_next_start_makes_no_call(env):
    w, la, _ = env
    s = _gated_with(w, la, CRED524)
    calls = Calls(w, la)
    new = b"{" + b"n" * 600 + b"}"
    _vendor_runs(w, lambda: _rewrite(_tree(w, s), new))
    assert w.main(["start", s])[1] == {"ok": True, "exit": 0}
    assert calls.gate == 1 and calls.post == 1
    pin = canon.parse(_pin_path(w).read_bytes())
    assert pin["sha256"] == hashlib.sha256(new).hexdigest() and pin["size"] == len(new)
    assert pin["ino"] == os.stat(_tree(w, s)).st_ino and pin["session"] == s and pin["observed_id"] == UUID
    assert (_pin_path(w).stat().st_mode & 0o777) == 0o600
    assert w.main(["teardown", s])[1]["ok"]
    assert _keep(w).read_bytes() == new

    gate_before = calls.gate
    s2 = _rendered(w, "egress", "interactive", "claude", "passthrough", blob=_blob(vendor="claude", routes=PASS))
    assert w.main(["start", s2])[1] == {"ok": True, "exit": 0}
    assert calls.gate == gate_before
    assert w.state(s2).fields["identity"] == {"observed_id": UUID, "result": "pinned"}


# --- T4: every reason no pin is written after the session -----------------------------------------------------------------

def _post_401(c):
    c.post_answer = lambda: (0, _ans(401))


def _post_timeout(c):
    def boom():
        raise SS.Refused("child-timeout")
    c.post_answer = boom


def _post_other_account(c):
    c.post_answer = lambda: (0, _ans(body={"account": {"uuid": OTHER}}))


def _post_unparsable(c):
    c.post_answer = lambda: (0, b"not json")


def _post_file_mutated_between(c):
    c.hook = lambda: _rewrite(_tree(c.w, c.w.session), b"{" + b"m" * 10 + b"}")


def _post_record_not_running(c):
    c.hook = None
    SS_read = SS.read_record
    state = {"armed": False}
    real_wait = c.w.seams.wait

    def wait(pid):
        state["armed"] = True
        return real_wait(pid)
    c.w.seams.wait = wait
    SS.read_record = lambda ctx, session: None if state["armed"] else SS_read(ctx, session)
    c.restore = lambda: setattr(SS, "read_record", SS_read)


@pytest.mark.parametrize("cause", [_post_401, _post_timeout, _post_other_account, _post_unparsable,
                                   _post_file_mutated_between, _post_record_not_running])
def test_t4_no_pin_after_a_session_the_call_cannot_vouch_for(env, capsys, cause):
    w, la, _ = env
    s = _gated_with(w, la, CRED524)
    calls = Calls(w, la)
    calls.restore = lambda: None
    seen = {}

    def vendor_body():
        _rewrite(_tree(w, s), b"{" + b"v" * 300 + b"}")
        seen["pin"] = _pin_path(w).read_bytes()          # what the gate's own match pinned: the pre-session bytes
    _vendor_runs(w, vendor_body)
    cause(calls)
    try:
        assert w.main(["start", s])[1] == {"ok": True, "exit": 0}
    finally:
        calls.restore()
    assert _pin_path(w).read_bytes() == seen["pin"]
    assert canon.parse(seen["pin"])["size"] == len(CRED524)
    assert "no identity pin" in capsys.readouterr().err


# --- T5: codex ----------------------------------------------------------------------------------------------------------

def test_t5_codex_neither_reads_nor_writes_a_pin(env, monkeypatch):
    w, la, _ = env
    s = _gated(w, la, "codex")

    def no(*a, **k):
        raise AssertionError("codex touched the pin")
    monkeypatch.setattr(ID, "read_slot_pin", no)
    monkeypatch.setattr(ID, "write_slot_pin", no)
    monkeypatch.setattr(SS, "_credential_fingerprint", no)
    assert w.main(["start", s])[1] == {"ok": True, "exit": 0}
    assert not _pin_path(w).exists() and not hasattr(la, "identity_captures")
