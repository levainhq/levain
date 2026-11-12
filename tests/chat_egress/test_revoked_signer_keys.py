"""Levain board row 190, the chat-egress key: revoking a signing key stops the setup records it signed.

A setup record is a standing grant in the authority log, so a key revoke stops it by the rule every standing grant
follows (``levain.authority.grants_in_force``: the key that signed its born link and the key that signed its current
link must both be active). The chat-egress key reads that at both of its uses: the launch (``build_route_set`` builds
no route for a stopped record) and the per-request attach (``AuthorityLiveness.check`` answers False for a route that
was built before the revoke). Throwaway ed25519 keys in tmp_path and real ``ssh-keygen``, a throwaway cockpit, a fake
credential store: no real key and no keychain item is reached.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import levain.authority as auth
from levain.chat_egress import key_setup, keys
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import fingerprint_of, revoke
from levain.consent.queue import authority_source
from tests._phone_fixtures import _enrol

from .keys_rig import record

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

RID = "ek-000000000001"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _keygen(tmp_path: Path, name: str) -> tuple[Path, str, str]:
    path = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)], check=True,
                   capture_output=True, timeout=30)
    pub = " ".join((tmp_path / f"{name}.pub").read_text().split()[:2])
    return path, pub, fingerprint_of(pub)


def _signer(path: Path):
    def sign(m: bytes) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(path), "-n", auth.NAMESPACE], input=m,
                              capture_output=True, check=True, timeout=30).stdout.decode()
    return sign


@pytest.fixture
def world(tmp_path: Path):
    """A throwaway cockpit holding three laptop-presence keys: A and B sign and renew setup records, C signs
    nothing of any record. Any key can revoke any other, through the real revoke path."""
    key_dir = tmp_path / "ssh"
    key_dir.mkdir()
    a_path, a_pub, a_fp = _keygen(key_dir, "keyA")
    b_path, b_pub, b_fp = _keygen(key_dir, "keyB")
    c_path, c_pub, c_fp = _keygen(key_dir, "keyC")
    paths = {"a": (a_path, a_fp), "b": (b_path, b_fp), "c": (c_path, c_fp)}
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    for pub, fp in ((a_pub, a_fp), (b_pub, b_fp), (c_pub, c_fp)):   # the genesis link A signs enrols A; A enrols B, C
        _enrol(conn, root, a_path, a_fp, {"op": "enrol", "fingerprint": fp, "public_key": pub,
                                          "class": "laptop-presence", "custody": "passphrase"})
    conn.close()
    source = authority_source(root / "cockpit.db", anchor=auth.anchor_path(root))

    def link(make_op, by: str) -> None:
        """Append a raw link key ``by`` signs; ``make_op(at)`` builds the op for the link's own stamp (a renew's
        ``signed_at`` must equal it)."""
        at = _now()
        by_path, by_fp = paths[by]
        conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
        try:
            auth.sign_and_append(conn, make_op(at), signer_fp=by_fp, sign=_signer(by_path), clock=lambda: at,
                                 cache=auth.ReplayCache(), anchor=auth.anchor_path(root))
        finally:
            conn.close()

    def create(rid: str = RID, host: str = "example.com", by: str = "a", **kw) -> dict:
        rec = record(rid, host, **kw)
        link(lambda _at: {"op": "create_grant", "grant": rec}, by)
        return rec

    def renew(rid: str, by: str) -> None:
        link(lambda at: {"op": "renew", "grant_id": rid, "expires_at": auth._stamp(at + timedelta(days=9)),
                         "signed_at": auth._stamp(at)}, by)

    def revoke_key(target: str, by: str) -> None:
        by_path, by_fp = paths[by]
        revoke(root, paths[target][1], signer_fp=by_fp, sign=_signer(by_path), clock=_now)

    def launch(entity: str = "ent") -> keys.RouteSet:
        return keys.build_route_set(source(), entity=entity, now=_now())

    def liveness() -> keys.AuthorityLiveness:
        return keys.AuthorityLiveness(source, clock=_now, timeout_s=30.0)

    def setup(by: str, **kw):
        """`levain chat key setup` for entity `ent` on example.com, key ``by`` signing, a fake keychain of one item."""
        by_path, by_fp = paths[by]
        args = {"entity": "ent", "host": "example.com", "source": "levain-chat-test-item",
                "account": "levain-chat-test-account", "placement": {"scheme": "bearer"}, "repos": ["/o/r.git"],
                "expires": timedelta(days=7), "confirm": lambda preview: True, "signer_fp": by_fp,
                "sign": _signer(by_path), "clock": _now, "keychain": SimpleNamespace(count=lambda s, a: 1)}
        return key_setup.setup_key(root, **{**args, **kw})

    return SimpleNamespace(source=source, create=create, renew=renew, revoke_key=revoke_key, launch=launch,
                           liveness=liveness, setup=setup)


def _route(routes: keys.RouteSet) -> keys.RouteSnapshot:
    assert set(routes.routes) == {"example.com"}
    return routes.routes["example.com"]


def test_control_a_record_whose_signers_are_active_builds_its_route_and_checks_live(world) -> None:
    """The rig reaches both uses: with every signer active the record is routed and its check answers True."""
    world.create()
    world.renew(RID, "b")
    route = _route(world.launch())
    assert route.record_id == RID
    assert world.liveness().check(route.record_id, route.incarnation) is True


def test_t13_a_record_whose_last_renewer_was_revoked_is_not_routed_and_a_built_route_stops_attaching(world) -> None:
    """A creates, B renews, B is revoked: the renew is the link that last changed the record, so the launch builds no
    route for it, and a route built before the revoke fails its next check (the per-request attach)."""
    world.create()
    world.renew(RID, "b")
    before = _route(world.launch())
    live = world.liveness()
    assert live.check(before.record_id, before.incarnation) is True        # control: the same route, signers active
    world.revoke_key("b", by="a")
    assert world.launch().routes == {}                                      # launch half
    assert live.check(before.record_id, before.incarnation) is False        # attach half, same checker, next request


def test_t13_a_record_whose_creator_was_revoked_is_not_routed_and_a_built_route_stops_attaching(world) -> None:
    """A creates, B renews, A is revoked: a held key's later renew does not make the record B's."""
    world.create()
    world.renew(RID, "b")
    before = _route(world.launch())
    live = world.liveness()
    world.revoke_key("a", by="b")
    assert world.launch().routes == {}
    assert live.check(before.record_id, before.incarnation) is False


def test_t13_a_record_born_and_never_renewed_by_a_revoked_key_is_stopped_the_same(world) -> None:
    """Born signer only (no renew): A signed the one link of the record; A revoked stops both uses."""
    world.create()
    before = _route(world.launch())
    live = world.liveness()
    world.revoke_key("a", by="b")
    assert world.launch().routes == {}
    assert live.check(before.record_id, before.incarnation) is False


def test_t13_a_revoke_of_a_key_that_signed_nothing_of_the_record_stops_nothing(world) -> None:
    """Revoking C, which signed neither the born nor the current link, leaves the record routed and live."""
    world.create()
    world.renew(RID, "b")
    before = _route(world.launch())
    live = world.liveness()
    world.revoke_key("c", by="a")
    after = _route(world.launch())
    assert after.record_id == RID
    assert live.check(before.record_id, before.incarnation) is True
    assert live.check(after.record_id, after.incarnation) is True


def test_t13_a_stopped_record_is_not_a_second_live_record_for_its_host(world, monkeypatch) -> None:
    """A store a foreign writer left holding two records for one host refuses the launch naming both. A stopped record
    is not live, so it does not make the pair: only the record whose signers are active is routed."""
    monkeypatch.setattr(auth, "_egress_key_append_check", lambda state, rec, now: None)   # a foreign writer
    world.create("ek-000000000001", by="a")
    world.create("ek-000000000002", by="b")
    with pytest.raises(keys.LaunchRefused, match="ek-000000000001, ek-000000000002"):
        world.launch()                                                      # control: both live is the refusal
    world.revoke_key("a", by="b")
    assert _route(world.launch()).record_id == "ek-000000000002"


def test_a_setup_refused_by_a_stopped_record_says_it_was_stopped_and_the_way_on(world) -> None:
    """A stopped record still counts in the one-live rule (the log counts it the same way), so a plain setup is refused
    as before; the refusal now says that record was stopped, why, and that --replace or a revoke of it is the way on."""
    world.create(by="a")
    with pytest.raises(key_setup.SetupRefused) as live:              # control: a live record, the plain words
        world.setup("b")
    assert "stopped" not in str(live.value)
    world.revoke_key("a", by="b")
    with pytest.raises(key_setup.SetupRefused) as exc:
        world.setup("b")
    said = str(exc.value)
    assert said.startswith(str(live.value))                         # the refusal itself is unchanged
    assert f"{RID} was stopped ({auth.STOPPED_BORN})" in said
    assert "--replace" in said and "levain chat key revoke" in said and said.rstrip().endswith("removes it")
    assert f"{RID}` removes it" in said                              # the command names the record it removes
    rec, _warning = world.setup("b", replace=True)                    # and the way on it names works
    assert _route(world.launch()).record_id == rec["id"]


def _revoke_verb_parser():
    """The `levain chat key revoke` subparser, found by walking the real parser."""
    import argparse

    from levain.cli import build_parser

    parser = build_parser()
    for name in ("chat", "key", "revoke"):
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        parser = sub.choices[name]
    return parser


def test_a_setup_refusal_names_a_revoke_command_that_carries_every_required_argument(world) -> None:
    """The refusal names `levain chat key revoke ...` as a way on; the command shown must be one the verb's parser
    takes, so a copy-paste of it (a placeholder for the key path) runs: every required argument of the verb, derived
    from the parser, appears in it, and the parser accepts it."""
    import re
    import shlex

    world.create(by="a")
    world.revoke_key("a", by="b")
    with pytest.raises(key_setup.SetupRefused) as exc:
        world.setup("b")
    shown = re.search(r"`(levain chat key revoke[^`]*)`", str(exc.value))
    assert shown is not None
    words = shlex.split(shown.group(1))
    revoke_parser = _revoke_verb_parser()
    required = [a for a in revoke_parser._actions if a.required or (not a.option_strings and a.nargs is None)]
    assert any(a.option_strings for a in required)               # the control: the verb does have a required option
    for action in required:
        if action.option_strings:
            assert action.option_strings[0] in words, action.option_strings
    argv = [("/key" if w.startswith("<") else w) for w in words[4:]]
    args = revoke_parser.parse_args(argv)                        # a copy-paste with the placeholder filled parses
    assert args.record_id == RID
