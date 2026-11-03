"""P7-A1 (CONTRACT c7.14 §5.1 `start`): a ticketed passthrough session takes the same keep/ return and §7.6 identity
compare an interactive passthrough session does; any other ticketed session takes neither. A file of its own because
the g3 gate harness it builds on carries an autouse world."""
from __future__ import annotations

import os

import pytest

from levain.slot import Refused
from levain.slot import control as C
from levain.slot import records as R
from levain.slot.ids import SlotId

TF = {"kind": "executor", "job_id": "j", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
      "egress_routes": ["github"], "reserved_microdollars": 0, "auth": "passthrough", "vendor": "claude",
      "max_calls": 40, "max_tunnel_bytes": 2**20, "deadline_s": 600, "deadline": 1_900_000_000}

# ---- start: the keep/ return and the identity compare for a ticketed passthrough session ---------------------------------

from tests.test_slot_g3_gate import (E0, OTHER, PASS, _back_in_keep, _gated, _keep, _la, _tree, env,  # noqa: F401
                                     vendor_installed, vendor_prefix)


def _as_ticketed(w, s: str) -> None:
    """The alloc of rendered session ``s`` rewritten as a ticketed passthrough dispatch session (what prepare writes for
    one): kind dispatch, a ticket, the facts. `start` acts on alloc only (§7.1)."""
    from dataclasses import replace
    p = w.L / "alloc" / f"{s}.json"
    a = R.parse_alloc(p.read_bytes())
    p.write_bytes(R.alloc_bytes(replace(a, kind="dispatch", ticket=C.new_handle(), ticket_facts=dict(TF))))


def test_start_returns_the_credential_and_compares_the_identity_for_a_ticketed_passthrough_session(env):
    w, la, _ = env
    s = _gated(w, la, profile=OTHER)                       # the login answers as another account than the baseline's
    _as_ticketed(w, s)
    line = w.main(["start", s])[1]
    print("TICKETED START", line)
    assert line["refused"] == "identity-changed", line      # the gate ran: keep/ was returned and the id compared
    _back_in_keep(w, s)                                      # and the credential is parked back, nothing cloned


def test_an_interactive_session_still_runs_the_same_gate(env):
    w, la, _ = env
    s = _gated(w, la, profile=OTHER)
    assert w.main(["start", s])[1]["refused"] == "identity-changed"
    _back_in_keep(w, s)


def test_a_brokered_dispatch_alloc_runs_no_gate(env):
    """A ticketed session that is not passthrough never has its keep/ credential returned."""
    from dataclasses import replace
    w, la, _ = env
    s = _gated(w, la, profile=OTHER)
    p = w.L / "alloc" / f"{s}.json"
    a = R.parse_alloc(p.read_bytes())
    p.write_bytes(R.alloc_bytes(replace(a, kind="dispatch", auth="brokered", ticket=C.new_handle(),
                                        ticket_facts={k: v for k, v in TF.items() if k in (
                                            "kind", "job_id", "run_index", "contract_digest", "egress_routes",
                                            "reserved_microdollars")} | {"route": "anthropic", "model_requested": "m"})))
    line = w.main(["start", s])[1]
    assert line.get("refused") != "identity-changed", line
    assert _keep(w).exists()                                 # the credential was never touched


@pytest.mark.parametrize("kind,ok", [("interactive", True), ("dispatch", True), ("planner", True), ("login", False),
                                     ("verifier", False)])
def test_return_move_admits_the_ticketed_kinds(tmp_path, kind, ok):
    from levain.slot.end import EndFacts, return_move
    from tests.test_slot_g3_return import ME, mine
    facts = EndFacts("s-g31", SlotId("egress", 3), ME, os.getgid(), tmp_path, None, 1, None, "claude",
                     "passthrough", kind)
    facts.keep.mkdir(parents=True, mode=0o700)
    (tmp_path / "live" / "s-g31" / "config" / "claude").mkdir(parents=True, mode=0o700)
    (facts.keep / ".credentials.json").write_bytes(b"{}")
    if ok:
        assert return_move(facts, owner_ok=mine) == "moved"
    else:
        with pytest.raises(Refused) as e:
            return_move(facts, owner_ok=mine)
        assert e.value.name == "bad-root"


