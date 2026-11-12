"""Levain board row 190, a hands MCP entry: revoking a key stops the entries it signed from reaching the registry.

An entry is a ``create_grant`` link in the authority log. The registry the hands account's servers are rendered and signed
in from (``levain.firing.hands_mcp_login.load_registry``) reads the entries a key that was revoked since did NOT sign
(``levain.authority.grants_in_force``): a stopped entry is not live, and it is reported as skipped with its stop reason
(the creator's words or the last renewer's). The entry is still in the log (so it can be revoked or listed). Throwaway ed25519 keys in tmp_path, a throwaway
cockpit, real ``ssh-keygen``: nothing of the operator's is read.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain import authority as auth
from levain import hands_mcp as hm
from levain.agent_authority import AgentAuthority
from levain.cockpit.consent import utc_stamp
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import fingerprint_of, revoke
from levain.firing import hands_mcp_login as hml
from tests._phone_fixtures import _enrol

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

#: a command every test host holds as root's, outside every home, in no group- or world-writable directory
TRUSTED = "/bin/sh"


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
    """A throwaway cockpit holding three laptop-presence keys: A (signs the genesis link), B (a second signer) and C (signs
    nothing of any entry)."""
    keys = {name: _keygen(tmp_path, f"key{name.upper()}") for name in ("a", "b", "c")}
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    a_path, _a_pub, a_fp = keys["a"]
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    for _name, (_path, pub, fp) in keys.items():
        _enrol(conn, root, a_path, a_fp, {"op": "enrol", "fingerprint": fp, "public_key": pub,
                                          "class": "laptop-presence", "custody": "passphrase"})
    conn.close()
    fps = {name: keys[name][2] for name in keys}

    def link(make_op, by: str) -> None:
        """Append a raw link key ``by`` signs; ``make_op(at)`` builds the op for the link's own stamp (a renew's
        ``signed_at`` must equal it)."""
        at = _now()
        conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
        try:
            auth.sign_and_append(conn, make_op(at), signer_fp=fps[by], sign=_signer(keys[by][0]), clock=lambda: at,
                                 cache=auth.ReplayCache(), anchor=auth.anchor_path(root))
        finally:
            conn.close()

    def entry(entry_id: str, name: str, by: str, days: int = 1) -> None:
        link(lambda at: {"op": "create_grant", "grant": hm.build_entry(
            name=name, transport="stdio", identity_kind="dedicated", command=TRUSTED, args=["--root", "/srv/shared"],
            expires_at=utc_stamp(at + timedelta(days=days)), entry_id=entry_id)}, by)

    def renew(entry_id: str, by: str) -> None:
        link(lambda at: {"op": "renew", "grant_id": entry_id, "expires_at": utc_stamp(at + timedelta(days=5)),
                         "signed_at": utc_stamp(at)}, by)

    def revoke_key(target: str, by: str) -> None:
        revoke(root, fps[target], signer_fp=fps[by], sign=_signer(keys[by][0]), clock=_now)

    return SimpleNamespace(root=root, entry=entry, renew=renew, revoke_key=revoke_key, fps=fps)


def _registry(world, now: datetime | None = None):
    live, skipped = hml.load_registry(world.root, now)
    return [e["id"] for e in live], skipped


def test_control_an_entry_is_live_while_its_signers_are_active(world) -> None:
    """The rig works: before any revoke, the entry key A signed is in the registry and nothing is skipped."""
    world.entry("hm-000000000001", "files", "a")
    assert _registry(world) == (["hm-000000000001"], [])


def test_an_entry_whose_born_signer_was_revoked_is_not_live_and_is_skipped_with_the_born_reason(world) -> None:
    """Key A signs the entry, B revokes A: the entry is left out of the live registry and reported with the creator's
    stop reason. It is still in the log: the policy holds it, so it can be listed or revoked."""
    world.entry("hm-000000000001", "files", "a")
    world.revoke_key("a", "b")
    assert _registry(world) == ([], [("hm-000000000001", auth.STOPPED_BORN)])
    assert "hm-000000000001" in AgentAuthority(world.root).replay().policy.grants


def test_an_entry_whose_last_renewer_was_revoked_is_not_live_and_is_skipped_with_the_renewer_reason(world) -> None:
    """B signs the entry (born), A renews it; A is revoked since: the CURRENT link's signer counts too, so the entry
    stops although the key that created it is active, and the reason is the renewer's, not the creator's."""
    world.entry("hm-000000000001", "files", "b")
    world.renew("hm-000000000001", "a")
    assert _registry(world)[0] == ["hm-000000000001"]
    world.revoke_key("a", "b")
    assert _registry(world) == ([], [("hm-000000000001", auth.STOPPED_CURRENT)])


def test_a_key_that_signed_nothing_of_the_entry_does_not_stop_it(world) -> None:
    """C signed no link of the entry: revoking C leaves the entry live, and only the entry C-signed stops."""
    world.entry("hm-000000000001", "files", "a")
    world.entry("hm-000000000002", "other", "c")
    world.revoke_key("c", "b")
    assert _registry(world) == (["hm-000000000001"], [("hm-000000000002", auth.STOPPED_BORN)])


def test_a_stopped_entry_that_has_also_expired_is_reported_as_stopped(world) -> None:
    """The reason that no renew repairs wins over ``expired``: the operator is told to register it again, not to renew."""
    world.entry("hm-000000000001", "files", "a")
    world.revoke_key("a", "b")
    later = _now() + timedelta(days=3)
    assert _registry(world, later) == ([], [("hm-000000000001", auth.STOPPED_BORN)])


def test_signin_refuses_a_stopped_entry_and_says_why(world, tmp_path: Path) -> None:
    """The verb that signs a server in reads the same registry: a stopped entry is refused in the stopped words, and the
    way back is to register it again (a renew does not bring it back, so the refusal does not say renew)."""
    world.entry("hm-000000000001", "files", "a")
    world.revoke_key("a", "b")
    with pytest.raises(hml.HandsMcpRefused) as exc:
        hml.entry_for_signin(tmp_path / "entity", world.root, "files")
    assert str(exc.value) == ("'files' is registered but stopped: the key that signed it was revoked; register it "
                              "again with a key you hold. Nothing was changed")
    assert "renew" not in str(exc.value)
