"""CONTRACT §3.3 ``QUEUE-RESOLVE`` and §26 S1-c ``repo_root``, over CONTROL's real socket (the s2a harness: peer
uids played by bound client names). Each case is a line of the run that preceded these tests
(levain-memory ``1019-s1design/runs/s3-queue-resolve/RUN.txt``), the broker's own resolver included."""
from __future__ import annotations

import socket
import types
from dataclasses import replace
from pathlib import Path

import pytest

from levain.qbroker import control as QC
from levain.qbroker.broker import Resolved, cap_digest, slot_ref
from levain.slot import control as C
from levain.slot.layout import STORE
from tests.test_slot_s2a_control import ROLE_UID, S1, S2, Env, settle, to_rendered

FACTS = {"run_id": "r-1", "job_id": "j-1", "run_index": 0, "contract_digest": "sha256:" + "a" * 64,
         "model_requested": None}


@pytest.fixture
def env():
    facts: dict[str, dict] = {}
    e = Env(run_facts=lambda s: facts.get(s), host="linux")
    e.facts = facts
    yield e
    e.close()


def cap_of(env: Env, session: str) -> str:
    root = env.client("root")
    ans = root.ask("SECRETS", session=session)
    assert ans["ok"], ans
    return ans["queue_cap"]


def broker(env: Env) -> QC.ControlResolver:
    """The broker's real resolver, its socket bound under the name that plays ``_levain_qbroker``."""
    class Bound(socket.socket):
        def connect(self, addr):
            env.n += 1
            self.bind(str(env.dir / f"qbroker.{env.n}"))
            return super().connect(addr)

    QC_socket = types.SimpleNamespace(socket=Bound, AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM)
    res = QC.ControlResolver(env.sock_path)
    res._socket_ns = QC_socket
    return res


@pytest.fixture
def resolver(env, monkeypatch):
    res = broker(env)
    monkeypatch.setattr(QC, "socket", res._socket_ns)
    return res


def as_dispatch(env: Env, session: str) -> None:
    """A dispatch-kind record (no dispatch ATTACH exists before session 3c's tickets): the kind set in the store."""
    rec = env.store.get(session)
    with env.store.lock(session):
        env.store.put(replace(rec, claims={**rec.claims, "kind": "dispatch"}, generation=rec.generation + 1))


def test_interactive_resolves_to_its_session(env, resolver):
    to_rendered(env, S1)
    got = resolver.queue_resolve(slot_ref(0), cap_digest(cap_of(env, S1)))
    assert got == Resolved(session=S1, kind="interactive", run=None)


def test_cap_on_another_slot_unknown_cap_and_bad_digest(env, resolver):
    to_rendered(env, S1)
    cap = cap_of(env, S1)
    assert resolver.queue_resolve(slot_ref(1), cap_digest(cap)) == "cap-unknown"
    assert resolver.queue_resolve({"class": "none", "index": 0}, cap_digest(cap)) == "cap-unknown"
    assert resolver.queue_resolve(slot_ref(0), cap_digest("x" * 43)) == "cap-unknown"
    assert resolver.queue_resolve(slot_ref(0), "sha256:XYZ") == "bad-token"
    assert resolver.queue_resolve(slot_ref(0), cap) == "bad-token"          # the cap itself is never the key


@pytest.mark.parametrize("role", ["root", "op", "foreign"])
def test_only_the_broker_may_resolve(env, role):
    to_rendered(env, S1)
    ans = env.client(role).ask("QUEUE-RESOLVE", slot=slot_ref(0), cap_digest=cap_digest(cap_of(env, S1)))
    assert ans["refused"] == "verb-not-allowed-for-peer"


def test_dispatch_names_its_run_and_repo_root(env, resolver):
    to_rendered(env, S2)
    as_dispatch(env, S2)
    cap = cap_of(env, S2)
    assert resolver.queue_resolve(slot_ref(0), cap_digest(cap)) == "run-unknown"
    env.facts[S2] = dict(FACTS)
    ans = env.client("qbroker").ask("QUEUE-RESOLVE", slot=slot_ref(0), cap_digest=cap_digest(cap))
    assert ans == {"id": ans["id"], "ok": True, "session": S2, "kind": "dispatch",
                   "run": {**FACTS, "repo_root": f"/var/levain/live/{S2}/repo"}}
    assert Path(ans["run"]["repo_root"]) == STORE["linux"] / "live" / S2 / "repo"


def test_dispatch_run_seam_with_other_fields_refused(env):
    to_rendered(env, S2)
    as_dispatch(env, S2)
    env.facts[S2] = {**FACTS, "repo_root": "/elsewhere"}        # the seam never supplies the root
    ans = env.client("qbroker").ask("QUEUE-RESOLVE", slot=slot_ref(0), cap_digest=cap_digest(cap_of(env, S2)))
    assert ans["refused"] == "run-malformed"


def test_cap_dies_with_the_lease(env, resolver):
    op, _root = to_rendered(env, S1)
    digest = cap_digest(cap_of(env, S1))
    op.close()
    settle(env, S1, "REVOKED")
    assert resolver.queue_resolve(slot_ref(0), digest) == "cap-unknown"


def test_digest_is_the_brokers():
    """CONTROL hashes the cap itself (it does not import the broker); the two must agree byte for byte."""
    h = C.Handles(queue_cap="Zm9v-cap_value")
    assert h.queue_cap_digest == cap_digest("Zm9v-cap_value")
    assert C.Handles().queue_cap_digest is None


def test_contracts_dir_reaches_the_broker_and_no_slot():
    """§26 S1-b: ``L/contracts/`` is group ``levain-queue`` 2750; the broker is a member, no slot account is."""
    from levain.slot.accounts import supplementary_groups
    from levain.slot.layout import base_store_rows
    row = next(r for r in base_store_rows("linux", "op") if r.path == STORE["linux"] / "contracts")
    assert (row.owner, row.group, row.mode) == ("_levain_dispatch", "levain-queue", 0o2750)
    assert "levain-queue" in supplementary_groups("_levain_qbroker")
    for name in ("_levain_s0", "_levain_c0", "_levain_v0", "_levain_gw0"):
        assert "levain-queue" not in supplementary_groups(name), name


@pytest.mark.parametrize("uid", [0, ROLE_UID["op"], "501"])
def test_qbroker_uid_must_be_its_own(uid, tmp_path):
    with pytest.raises(C.Refused) as e:
        C.Control(C.StateStore(tmp_path), ROLE_UID["op"], qbroker_uid=uid)
    assert e.value.name == "bad-config"
