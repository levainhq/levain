"""Signing an agent link and its renewal (A0 §3.2, §3.3; BUILD_PLAN P5a-2a).

A link is created by a laptop-presence key and renewed by one (a generic renew is rank 2, which a display-phone ssh
key reaches, so the kind carries its own signer class). A passkey signs no link but may revoke all of an agent's, one
act, through the ``revoke_agent`` branch. ``levain.agents.signing`` is the one place an agent-link append or renew is
prepared. Every log here is real: ssh-keygen keys and WebAuthn stand-ins verified by ssh-keygen through the replay (the
``World`` of the graduation kind's tests)."""
from __future__ import annotations

import ast
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

import levain.authority as auth
from levain.agents.signing import renew_agent_link, sign_agent_link
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import KeyError_

pytest.importorskip("cryptography")
from tests.test_authority_agent_link import AGENT, STORE, link, revoke_agent  # noqa: E402
from tests.test_authority_graduation_kind import World  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture
def world(tmp_path) -> World:
    w = World(tmp_path)
    CockpitDB(w.root).set_install_class("reported")        # the signing entry opens a reported install's journal
    return w


def test_a_passkey_cannot_sign_an_agent_link(world):
    why = world.refused({"op": "create_grant", "grant": link(world, 1)}, "pk")
    assert "passkey" in why and "never another authority link" in why
    assert auth.agent_links(world.state(), AGENT, STORE) == []
    # through the signing entry as well, the log is what refuses
    fp, sign = world.keys["pk"]
    with pytest.raises(KeyError_, match="passkey"):
        sign_agent_link(world.root, link(world, 1), signer_fp=fp, sign=sign, clock=world.clock)


def test_a_passkey_revokes_a_two_link_agent_through_the_revoke_agent_branch(world):
    world.link({"op": "create_grant", "grant": link(world, 1)})
    world.link({"op": "create_grant", "grant": link(world, 2)})
    assert len(auth.agent_links(world.state(), AGENT, STORE)) == 2
    snap = world.link(revoke_agent(), "pk")                 # removing two links computes TIGHTEN; the op is rank 1
    assert auth.agent_links(snap, AGENT, STORE) == []
    assert snap.agent_revoked[(AGENT, STORE)] == snap.seq
    # the branch holds an agent with no link to the same refusal as any other signer
    assert "none for" in world.refused(revoke_agent(), "pk2")


def test_a_renewal_moves_only_expires_at_keeps_the_incarnation_and_a_display_phone_key_cannot_renew(world):
    world.link({"op": "create_grant", "grant": link(world, 1)})
    lid = "al-000000000001"
    before = world.state()
    fp, sign = world.keys["laptop"]
    world.clock.now += timedelta(days=1)
    after, expires_at = renew_agent_link(world.root, lid, life=timedelta(days=30), signer_fp=fp, sign=sign,
                                         clock=world.clock)
    old, new = dict(before.policy.grants[lid]), dict(after.policy.grants[lid])
    assert new.pop("expires_at") == expires_at != old.pop("expires_at")
    assert new == old
    assert auth.agent_link_incarnation(after, lid) == auth.agent_link_incarnation(before, lid)
    # a display-phone ssh key reaches the generic renew's rank, and the kind's signer class refuses it
    world.clock.now += timedelta(days=2)
    pfp, psign = world.keys["phone"]
    with pytest.raises(KeyError_, match="renew needs a laptop-presence key"):
        renew_agent_link(world.root, lid, life=timedelta(days=30), signer_fp=pfp, sign=psign, clock=world.clock)
    assert world.state().policy.grants[lid]["expires_at"] == expires_at


# --- the one signing entry ------------------------------------------------------------------------------------------

_PREPARING = frozenset({"prepare", "sign_and_append"})
_AUTHORITY_NAMES = frozenset({"authority", "_authority", "auth"})
_LEVAIN = Path(auth.__file__).resolve().parent
# every module that appends a link through the log's own prepare, and what it signs: a new one fails the inventory
# until someone reads it and adds it here, which is the point
_CALLERS = {"agents/signing.py": "agent links", "chat_egress/key_setup.py": "egress setup records",
            "cli.py": "graduation policies, shell and consent grants", "cockpit/keys.py": "key enrolments and revokes",
            "consent/grants.py": "grant revokes"}


def _preparing_scopes(source: str) -> list[tuple[str, bool]]:
    """``(scope name, whether it names an agent link)`` for every function (or the module) that calls the log's
    ``prepare`` or ``sign_and_append``, directly or as ``authority.prepare``."""
    def calls(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name) and f.id in _PREPARING:
                    return True
                if isinstance(f, ast.Attribute) and f.attr in _PREPARING and isinstance(f.value, ast.Name) \
                        and f.value.id in _AUTHORITY_NAMES:
                    return True
        return False

    def names_agent_link(node: ast.AST) -> bool:
        return any((isinstance(s, ast.Name) and s.id == "AGENT_LINK_KIND")
                   or (isinstance(s, ast.Constant) and isinstance(s.value, str) and "agent-link" in s.value)
                   or (isinstance(s, ast.Attribute) and s.attr == "AGENT_LINK_KIND") for s in ast.walk(node))

    tree = ast.parse(source)
    scopes = [(n.name, n) for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    out = [(name, names_agent_link(n)) for name, n in scopes if calls(n)]
    return out or ([("<module>", names_agent_link(tree))] if calls(tree) else [])


def test_no_caller_of_the_agent_link_prepare_outside_signing_py():
    # the walk can fail: a synthetic offender is caught, a synthetic innocent is not
    assert _preparing_scopes("import levain.authority as authority\n"
                             "def f(c, g):\n    return authority.prepare(c, {'op': 'create_grant', 'grant': "
                             "{'kind': 'agent-link'}})\n") == [("f", True)]
    assert _preparing_scopes("from levain.authority import sign_and_append, AGENT_LINK_KIND\n"
                             "def f(c, op):\n    return sign_and_append(c, op)\n") == [("f", False)]
    found: dict[str, list[str]] = {}
    for path in sorted(_LEVAIN.rglob("*.py")):
        rel = path.relative_to(_LEVAIN).as_posix()
        if rel == "authority.py":
            continue
        scopes = _preparing_scopes(path.read_text(encoding="utf-8"))
        if scopes:
            found[rel] = [name for name, agent in scopes if agent]
    # the entry itself is found (the walk is not vacuous), and no other module's preparing scope names an agent link
    assert "agents/signing.py" in found, found
    assert {rel: names for rel, names in found.items() if names and rel != "agents/signing.py"} == {}
    # and no module prepares a link that is not in the inventory above
    assert sorted(found) == sorted(_CALLERS), sorted(set(found) ^ set(_CALLERS))
