"""Levain board row 190: revoking a signing key invalidates the grants it signed.

A consent grant is a ``create_grant`` link in the authority log, signed by a laptop-presence key. The replay's rule
is "a key revoked at seq N signs nothing after N, and the links it signed before N stay valid (no retroactive
voiding)" (levain/authority.py module docstring), and the grant USE path (``ConsentQueue.answer_by_grant`` ->
``_matching_grant``) reads the grant from a FRESH replay's ``policy.grants`` and never looks at who signed it. So a
grant outlives the revoke of the key that signed it.

These tests are the measurement (throwaway ed25519 keys in tmp_path, a throwaway cockpit, real ``ssh-keygen``): they
sign a grant with key A, revoke A through the real ``cockpit.keys.revoke`` path (signed by a surviving key B), and
then use the grant through the real gate (``policy.guarded_policy``). They assert the INTENDED behaviour (a
revoked signer's grant answers nothing), so they FAIL on current main: ``strict`` xfail records that, and the fix
removes the marks.
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
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import fingerprint_of, revoke
from levain.consent import policy
from levain.consent.grants import create_grant, grant_view, list_grants
from levain.consent.queue import ConsentQueue, authority_source
from tests._h4 import h4_home
from tests._phone_fixtures import _enrol

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

NOW_FAILS_BECAUSE = ("row 190 (measured on main 02f561942): the grant use path reads the grant from the replayed "
                     "policy and never re-checks that the key which signed its create_grant link is still active")


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
    """A throwaway cockpit holding two laptop-presence keys (A signs the grant, B survives to sign the revoke)."""
    a_path, a_pub, a_fp = _keygen(tmp_path, "keyA")
    b_path, b_pub, b_fp = _keygen(tmp_path, "keyB")
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    for pub, fp in ((a_pub, a_fp), (b_pub, b_fp)):      # the genesis link A signs enrols A; A then enrols B
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

    return SimpleNamespace(root=root, qdir=qdir, ws=ws, queue=queue, a_fp=a_fp, b_fp=b_fp, grant=grant, ask=ask, file=file,
                           revoke_a=revoke_a, source=source)


def test_control_the_grant_answers_while_its_signer_is_active(world) -> None:
    """The rig works: before any revoke, the grant key A signed answers a matching Write at the gate."""
    world.grant("cg-A")
    _r, d = world.ask("before.txt")
    assert d is not None and d.via == "grant" and d.grant_id == "cg-A"


@pytest.mark.xfail(strict=True, reason=NOW_FAILS_BECAUSE)
def test_a_new_request_is_not_answered_by_a_grant_whose_signer_was_revoked(world) -> None:
    """11-08 scenario: key A signs the grant, A is revoked (plain), a NEW Write is asked: the grant must not answer."""
    world.grant("cg-A")
    world.revoke_a()
    assert world.source().key(world.a_fp).revoked_seq is not None      # the revoke landed in the log
    _r, d = world.ask("after.txt")
    assert d is None or d.via != "grant"


@pytest.mark.xfail(strict=True, reason=NOW_FAILS_BECAUSE)
def test_a_compromised_key_revoke_voids_its_grant_too(world) -> None:
    """The lost/stolen form: A revoked with compromised_since at A's own enrol seq (everything it signed suspect)."""
    world.grant("cg-A")
    since = world.source().key(world.a_fp).enrolled_seq
    world.revoke_a(compromised_since=since)
    _r, d = world.ask("stolen.txt")
    assert d is None or d.via != "grant"


@pytest.mark.xfail(strict=True, reason=NOW_FAILS_BECAUSE)
def test_an_answer_the_grant_gave_before_the_revoke_is_not_delivered_after_it(world) -> None:
    """The grant answered a request while A was active; A is revoked before the decision is delivered. Delivery
    re-verifies against a fresh replay (queue.py docstring: 'a key revoked after it answered delivers nothing' for a
    SIGNED answer); a grant answer re-verifies the grant is 'still in a fresh replay', not that its signer is."""
    world.grant("cg-A")
    r, tool_input = world.file("pre.txt")
    d = policy.guarded_policy(r, world.queue, posture="default", tool_input=tool_input)
    assert d is not None and d.via == "grant"
    world.revoke_a()
    again = world.queue.decision(r.id, tool_input=tool_input, posture="default")
    assert again is None or again.via != "grant"


def test_the_listing_still_shows_the_grant_after_its_signer_is_revoked(world) -> None:
    """What the operator sees: nothing marks the grant (it lists like any live grant). Records the current behaviour."""
    world.grant("cg-A")
    world.revoke_a()
    grants = list_grants(world.root, now=_now())
    assert [g.id for g in grants] == ["cg-A"]
    page = grant_view(world.qdir, now=_now())               # the cockpit page / phone read
    assert [(r["id"], r["state"]) for r in page["grants"]] == [("cg-A", "live")]
