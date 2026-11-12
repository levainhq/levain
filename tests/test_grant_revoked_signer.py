"""Levain board row 190: revoking a signing key invalidates the grants it signed.

A consent grant is a ``create_grant`` link in the authority log, signed by a laptop-presence key. The replay keeps
the links a revoked key signed before its revoke (the log replays the same, and the grant stays in the policy, so it
can still be revoked), and what a standing grant may DO is judged at each use: a grant whose born link, or whose
current (last renewing) link, was signed by a key revoked since answers nothing
(``levain.authority.grants_in_force``). The consent queue reads its grants through that, when it matches a request
and again when it delivers an answer a grant gave.

These tests are the measurement and its fix (throwaway ed25519 keys in tmp_path, a throwaway cockpit, real
``ssh-keygen``): they sign a grant with key A, revoke it through the real ``cockpit.keys.revoke`` path (signed by a
surviving key), and then use the grant through the real gate (``policy.guarded_policy``) or the queue. A third key C
is enrolled for the cases that need a signer who signed nothing of the grant.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain import authority as auth
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import fingerprint_of, revoke
from levain.consent import policy
from levain.consent.adapters import claude_code
from levain.consent.contained import ContainedLaunchProof
from levain.consent.grants import create_grant, grant_view, list_grants
from levain.consent.queue import ConsentQueue, authority_source
from tests._h4 import h4_home
from tests._phone_fixtures import _enrol

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

DIGEST = "d" * 64
BASH = {"command": "pytest -q"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _keygen(tmp_path: Path, name: str) -> tuple[Path, str, str]:
    path = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)], check=True)
    pub = " ".join((tmp_path / f"{name}.pub").read_text().split()[:2])
    return path, pub, fingerprint_of(pub)


def _signer(path: Path):
    def sign(m: bytes) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(path), "-n", auth.NAMESPACE], input=m,
                              capture_output=True, check=True).stdout.decode()
    return sign


@pytest.fixture
def world(tmp_path: Path):
    """A throwaway cockpit holding three laptop-presence keys (A signs the grant, B survives to sign the revoke, C
    signs nothing of any grant)."""
    a_path, a_pub, a_fp = _keygen(tmp_path, "keyA")
    b_path, b_pub, b_fp = _keygen(tmp_path, "keyB")
    c_path, c_pub, c_fp = _keygen(tmp_path, "keyC")
    paths = {"a": (a_path, a_fp), "b": (b_path, b_fp), "c": (c_path, c_fp)}
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    for pub, fp in ((a_pub, a_fp), (b_pub, b_fp), (c_pub, c_fp)):   # the genesis link A signs enrols A; A enrols B, C
        _enrol(conn, root, a_path, a_fp, {"op": "enrol", "fingerprint": fp, "public_key": pub,
                                          "class": "laptop-presence", "custody": "passphrase"})
    conn.close()
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    os.chmod(qdir, 0o700)
    (qdir / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(root), "away_mode": False,
                                                       "away_hold_seconds": 0}))
    ws = Path(os.path.realpath(tmp_path / "proj"))
    ws.mkdir()
    source = authority_source(root / "cockpit.db", anchor=auth.anchor_path(root))
    queue = ConsentQueue(qdir, authority=source, home=h4_home())

    def grant(gid: str) -> None:
        create_grant(root, agent="claude_code", tools=["Write"], workspace=ws, postures=["default"],
                     expires=timedelta(days=1), signer_fp=a_fp, sign=_signer(a_path), clock=_now, grant_id=gid)

    def file(name: str):
        tool_input = {"file_path": str(ws / name), "content": "x"}
        r = queue.file(agent="claude_code", agent_session_id="s1", tool_name="Write", tool_input=tool_input,
                       cwd=str(ws), hold=timedelta(minutes=5))
        return r, tool_input

    def ask(name: str):
        r, tool_input = file(name)
        return r, policy.guarded_policy(r, queue, posture="default", tool_input=tool_input)

    def revoke_a(compromised_since: int | None = None):
        return revoke(root, a_fp, signer_fp=b_fp, sign=_signer(b_path), clock=_now,
                      compromised_since=compromised_since)

    def revoke_key(target: str, by: str, compromised_since: int | None = None):
        """Revoke key ``target`` ("a", "b" or "c") by a link key ``by`` signs, through the real revoke path."""
        by_path, by_fp = paths[by]
        return revoke(root, paths[target][1], signer_fp=by_fp, sign=_signer(by_path), clock=_now,
                      compromised_since=compromised_since)

    def link(make_op, by: str) -> None:
        """Append a raw link key ``by`` signs. ``make_op(at)`` builds the op for the link's own stamp, because a renew's
        ``signed_at`` must equal it."""
        at = _now()
        by_path, by_fp = paths[by]
        conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
        try:
            auth.sign_and_append(conn, make_op(at), signer_fp=by_fp, sign=_signer(by_path), clock=lambda: at,
                                 cache=auth.ReplayCache(), anchor=auth.anchor_path(root))
        finally:
            conn.close()

    def renew(gid: str, by: str) -> None:
        def op(at):
            return {"op": "renew", "grant_id": gid, "expires_at": utc_stamp(at + timedelta(days=5)),
                    "signed_at": utc_stamp(at)}
        link(op, by)

    def shell_grant(gid: str, by: str = "a") -> None:
        """A contained-shell grant (SH1) for a chat Bash call in the workspace."""
        link(lambda at: {"op": "create_grant", "grant": {
            "id": gid, "kind": auth.CONSENT_CONTAINED_SHELL_KIND, "agent": "claude_code", "tools": ["Bash"],
            "workspace": str(ws), "postures": ["default"], "containment_digest": DIGEST,
            "expires_at": utc_stamp(at + timedelta(days=1))}}, by)

    def shell_file(event_id: str):
        return queue.file(agent="claude_code", agent_session_id="s1", tool_name="Bash", tool_input=BASH, cwd=str(ws),
                          hold=timedelta(minutes=5), event_id=event_id, event_source="chat")

    proof = ContainedLaunchProof(launch_id="L1", spawn_nonce="a" * 32, profile_digest=DIGEST,
                                 canary_verdict_id="c" * 64, cwd=str(ws))

    return SimpleNamespace(root=root, qdir=qdir, ws=ws, queue=queue, a_fp=a_fp, b_fp=b_fp, c_fp=c_fp, grant=grant,
                           ask=ask, file=file, revoke_a=revoke_a, revoke_key=revoke_key, link=link, renew=renew,
                           shell_grant=shell_grant, shell_file=shell_file, proof=proof, source=source)


def test_control_the_grant_answers_while_its_signer_is_active(world) -> None:
    """The rig works: before any revoke, the grant key A signed answers a matching Write at the gate."""
    world.grant("cg-A")
    _r, d = world.ask("before.txt")
    assert d is not None and d.via == "grant" and d.grant_id == "cg-A"


def test_a_new_request_is_not_answered_by_a_grant_whose_signer_was_revoked(world) -> None:
    """11-08 scenario: key A signs the grant, A is revoked (plain), a NEW Write is asked: the grant must not answer."""
    world.grant("cg-A")
    world.revoke_a()
    assert world.source().key(world.a_fp).revoked_seq is not None      # the revoke landed in the log
    r, d = world.ask("after.txt")
    assert d is None or d.via != "grant"
    assert world.queue.answered_via(r.id) == ("pending", None)           # not recorded as answered either: he is asked


def test_a_compromised_key_revoke_voids_its_grant_too(world) -> None:
    """The lost/stolen form: A revoked with compromised_since at A's own enrol seq (everything it signed suspect)."""
    world.grant("cg-A")
    since = world.source().key(world.a_fp).enrolled_seq
    world.revoke_a(compromised_since=since)
    r, d = world.ask("stolen.txt")
    assert d is None or d.via != "grant"
    assert world.queue.answered_via(r.id) == ("pending", None)


def test_an_answer_the_grant_gave_before_the_revoke_is_not_delivered_after_it(world) -> None:
    """The grant answered a request while A was active; A is revoked before the decision is delivered. Delivery
    re-verifies against a fresh replay, and a grant answer is delivered only while the grant is still in force."""
    world.grant("cg-A")
    r, tool_input = world.file("pre.txt")
    d = policy.guarded_policy(r, world.queue, posture="default", tool_input=tool_input)
    assert d is not None and d.via == "grant"
    world.revoke_a()
    again = world.queue.decision(r.id, tool_input=tool_input, posture="default")
    assert again is None or again.via != "grant"


def test_the_listing_still_shows_the_grant_after_its_signer_is_revoked_marked_stopped(world) -> None:
    """What the operator sees: the grant is still listed (it is in the policy, so it can be revoked) and says it is
    stopped, not live."""
    world.grant("cg-A")
    world.revoke_a()
    grants = list_grants(world.root, now=_now())
    assert [(g.id, g.stopped_why) for g in grants] == [("cg-A", auth.STOPPED_BORN)]
    page = grant_view(world.qdir, now=_now())               # the cockpit page / phone read
    assert [(r["id"], r["state"]) for r in page["grants"]] == [("cg-A", "stopped")]


# --- who signed what: the born signer and the last renewer both count -------------------------------------------

def test_t3_a_grant_whose_last_renewer_was_revoked_answers_nothing(world) -> None:
    """A creates, B renews, B is revoked: the renew is the link that last changed the grant, so B's revoke stops it."""
    world.grant("cg-A")
    world.renew("cg-A", "b")
    _r, d = world.ask("renewed.txt")
    assert d is not None and d.via == "grant"                           # control: both signers still active
    world.revoke_key("b", by="a")
    assert auth.grant_stop_reason(world.source(), "cg-A") == auth.STOPPED_CURRENT
    r, d = world.ask("after.txt")
    assert d is None or d.via != "grant"
    assert world.queue.answered_via(r.id) == ("pending", None)


def test_t4_a_grant_whose_creator_was_revoked_answers_nothing_though_a_held_key_renewed_it(world) -> None:
    """A creates, B renews, A is revoked: B's later renew does not make the grant B's."""
    world.grant("cg-A")
    world.renew("cg-A", "b")
    world.revoke_key("a", by="b")
    assert auth.grant_stop_reason(world.source(), "cg-A") == auth.STOPPED_BORN
    r, d = world.ask("after.txt")
    assert d is None or d.via != "grant"
    assert world.queue.answered_via(r.id) == ("pending", None)


def test_t6_a_restatement_by_a_revoked_key_does_not_stop_a_grant_it_did_not_change(world) -> None:
    """B signs a loosen that re-states the whole policy (as a setup's ``--replace`` does) and adds a grant of its own.
    Grant ``cg-A`` is unchanged by it, so revoking B stops the grant B added and leaves ``cg-A`` answering."""
    world.grant("cg-A")

    def restate(at):
        p = world.source().policy
        added = {"id": "cg-B", "kind": auth.CONSENT_GRANT_KIND, "agent": "claude_code", "tools": ["Write"],
                 "workspace": str(world.ws), "postures": ["default"], "expires_at": utc_stamp(at + timedelta(days=1))}
        return {"op": "loosen", "policy": {"raise_to": auth._thaw(p.raise_to), "timeouts": auth._thaw(p.timeouts),
                                           "grants": {**auth._thaw(p.grants), "cg-B": added}}}

    world.link(restate, "b")
    state = world.source()
    assert state.grant_seqs["cg-A"] < state.grant_seqs["cg-B"]          # the restatement is not cg-A's current link
    world.revoke_key("b", by="a")
    state = world.source()
    assert auth.grant_stop_reason(state, "cg-B") == auth.STOPPED_BORN
    assert auth.grant_stop_reason(state, "cg-A") is None
    _r, d = world.ask("after.txt")
    assert d is not None and d.via == "grant" and d.grant_id == "cg-A"


# --- the contained-shell grant: the same queue, its own answer and delivery paths ---------------------------------

def test_t8_a_contained_shell_grant_answers_while_its_signer_is_active(world) -> None:
    """Control: the rig reaches the contained-shell answer and delivery paths."""
    world.shell_grant("sh1-a")
    r = world.shell_file("chat:toolu_1")
    d = world.queue.answer_by_contained_shell_grant(r.id, tool_input=BASH, posture="default",
                                                    contained_launch=world.proof)
    assert d is not None and d.via == "contained_shell" and d.grant_id == "sh1-a"
    again = world.queue.decision(r.id, tool_input=BASH, posture="default", contained_launch=world.proof)
    assert again is not None and again.via == "contained_shell"


def test_t8_a_contained_shell_grant_whose_signer_was_revoked_answers_nothing(world) -> None:
    world.shell_grant("sh1-a")
    world.revoke_key("a", by="b")
    r = world.shell_file("chat:toolu_1")
    d = world.queue.answer_by_contained_shell_grant(r.id, tool_input=BASH, posture="default",
                                                    contained_launch=world.proof)
    assert d is None
    assert world.queue.answered_via(r.id) == ("pending", None)          # left as it was: the operator is asked


def test_t8_a_contained_shell_answer_given_before_the_revoke_is_not_delivered_after_it(world) -> None:
    world.shell_grant("sh1-a")
    r = world.shell_file("chat:toolu_1")
    d = world.queue.answer_by_contained_shell_grant(r.id, tool_input=BASH, posture="default",
                                                    contained_launch=world.proof)
    assert d is not None and d.via == "contained_shell"
    world.revoke_key("a", by="b")
    again = world.queue.decision(r.id, tool_input=BASH, posture="default", contained_launch=world.proof)
    assert again is None


# --- the hook's arming: a grant keeps nothing armed when no key is active ----------------------------------------

def test_t9_with_every_key_revoked_an_unexpired_grant_does_not_arm_the_hook(world) -> None:
    """Every enrolled key revoked (the last with ``compromised_since``, which is what lets the last laptop key go):
    the grants are still in the policy and unexpired, and nothing can decide, in the words the operator reads."""
    world.grant("cg-A")
    world.shell_grant("sh1-a")
    settings = claude_code.AdapterSettings(away_mode=False, away_hold_seconds=0, cockpit_data_root=str(world.root))
    assert claude_code.nothing_can_decide(settings) is False             # control: keys active
    world.revoke_key("a", by="b")
    assert claude_code.nothing_can_decide(settings) is False             # control: two keys left
    world.revoke_key("b", by="c")
    world.revoke_key("c", by="c", compromised_since=world.source().key(world.c_fp).enrolled_seq)
    state = world.source()
    assert state.active_keys() == []
    assert {"cg-A", "sh1-a"} <= set(state.policy.grants)                 # still there, and not expired
    assert all(parse_stamp(state.policy.grants[g]["expires_at"]) > _now() for g in ("cg-A", "sh1-a"))
    assert claude_code.nothing_can_decide(settings) is True
    assert claude_code.nothing_can_decide_why(settings) == "no enrolled key is active"
