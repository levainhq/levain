"""S1 session 3b-6b: CONTROL's side of keys' admin wire (CONTRACT §4.9, E23): the per-``front_uid`` registration
counter, MINT's token-only prewrite and FRONT-ARM's own token.

CONTROL is driven in-process (``Control.handle`` with a ``Conn``) over a real store and real flock; keys is the
``keys_front_register`` / ``keys_front_arm`` seams, which record what CONTROL sent and what the store held when it sent
it. The admin client over a real socket is run against 3b-2b's RecordingAdminServer in the lane's DONE run.
"""
from __future__ import annotations

from pathlib import Path

from levain.slot import control as C
from levain.slot import records as R
from tests.test_slot_s2a_control import attach_claims, mint_claims

OP_UID = 501
S1, S2 = "s-00000000000000b1", "s-00000000000000b2"
FRONT_UID = 3989


def _control(store_dir: Path, sent: list[tuple]) -> C.Control:
    store = C.StateStore(store_dir)

    def register(session, front_uid, pid, start, incarnation, reg_token):
        on_disk = R.parse_state((store_dir / f"{session}.json").read_bytes())
        sent.append(("register", session, front_uid, reg_token, on_disk))

    control = C.Control(store, OP_UID, keys_front_register=register,
                        keys_front_arm=lambda session, reg_token: sent.append(("arm", session, reg_token)),
                        sleep=lambda s: None)
    control.start()
    return control


def _rendering(control: C.Control, session: str) -> tuple[C.Conn, int]:
    op = C.Conn(OP_UID)
    nonce = control.handle(op, {"v": 1, "op": "LEASE-OPEN", "id": 1})["lease_nonce"]
    ans = control.handle(C.Conn(0), {"v": 1, "op": "ATTACH", "id": 2, "lease_nonce": nonce, "sudo_uid": OP_UID,
                                     "session": session, "claims": attach_claims()})
    assert ans["ok"], ans
    ans = control.handle(C.Conn(0), {"v": 1, "op": "CAS", "id": 3, "session": session, "generation": 0,
                                     "from": R.ALLOCATED, "to": R.RENDERING, "fields": {}})
    assert ans["ok"], ans
    return op, ans["generation"]


def test_reg_token_counter_durable(tmp_path):
    """E23 (a): the counter is read, +1, written durably under the uid's lock BEFORE the FRONT-REGISTER carrying it,
    the session's own token is prewritten ALONE into its claims (``event_kind`` none), the merged record holds both at
    the final generation MINT answers, FRONT-ARM carries the token from the claims, and a CONTROL restart continues
    the counter (strictly increasing per ``front_uid``)."""
    store_dir = tmp_path / "sessions"
    store_dir.mkdir(mode=0o700)
    counter = store_dir / "front-reg" / f"{FRONT_UID}.json"
    sent: list[tuple] = []

    control = _control(store_dir, sent)
    _lease1, gen = _rendering(control, S1)
    ans = control.handle(C.Conn(0), {"v": 1, "op": "MINT", "id": 4, "session": S1, "generation": gen,
                                     "claims": mint_claims()})
    assert ans == {"id": 4, "ok": True, "generation": gen + 2}, ans
    (_, session, front_uid, token, prewritten), = sent
    assert (session, front_uid, token) == (S1, FRONT_UID, 1)
    assert prewritten.claims == {**attach_claims(), R.REG_TOKEN: 1}            # TOKEN-ONLY: no MINT claim
    assert prewritten.generation == gen + 1 and prewritten.event_kind == "none"
    assert not R.has_mint_claims(prewritten.claims)
    final = R.parse_state((store_dir / f"{S1}.json").read_bytes())
    assert final.generation == gen + 2 and final.claims == {**attach_claims(), **mint_claims(), R.REG_TOKEN: 1}
    assert R.parse_front_reg(counter.read_bytes(), FRONT_UID) == R.FrontReg(FRONT_UID, 1)
    ans = control.handle(C.Conn(0), {"v": 1, "op": "FRONT-ARM", "id": 5, "session": S1, "generation": gen + 2})
    assert ans["ok"] and sent[-1] == ("arm", S1, 1), (ans, sent)

    control.stop()                                              # a restart: the next start revokes S1
    sent.clear()
    control = _control(store_dir, sent)
    _lease2, gen = _rendering(control, S2)
    assert control.handle(C.Conn(0), {"v": 1, "op": "MINT", "id": 6, "session": S2, "generation": gen,
                                      "claims": mint_claims()})["ok"]
    assert [(s[1], s[3]) for s in sent] == [(S2, 2)]
    assert R.parse_front_reg(counter.read_bytes(), FRONT_UID) == R.FrontReg(FRONT_UID, 2)
    control.stop()
