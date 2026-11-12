"""Levain board row 190, the words (T16, T17, T18): what the operator is told when a signing key's revoke stops the
grants it signed.

The stop itself is the USE paths' (``levain.authority.grants_in_force``); this file holds what they SAY. A stopped grant is
still in the policy, so it is still listed and still revocable, and every place that lists it says ``stopped`` and why:

* T16  ``grant_view`` (the cockpit page and the phone read), ``list_grants`` / ``grant_listing``, ``levain consent grant
       list``, ``levain chat key list``, ``levain seat lease|egress list``, ``levain hands mcp list`` and its sign-in
       refusal, ``levain voice key list`` and the voice readout.
* T17  ``levain cockpit revoke`` names each grant the key signed (a second, fresh read), says so when there was none, and
       keeps its own ``revoked:`` line byte for byte.
* T18  the ``standing grants`` doctor row: present iff a grant is stopped; and the api-key agent-link line leaves a
       stopped link out.

Throwaway ed25519 keys, a throwaway cockpit, real ``ssh-keygen``, a fake keychain: nothing of the operator's is read.
Three laptop-presence keys are enrolled (A signs, B renews or survives to revoke, C signs nothing).
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

pytest.importorskip("cryptography")
import levain.authority as auth  # noqa: E402
from levain import doctor as D  # noqa: E402
from levain import hands_mcp as hm  # noqa: E402
from levain import seat_lease as sl  # noqa: E402
from levain import seat_push as sp  # noqa: E402
from levain.chat_egress.key_setup import list_keys as chat_list_keys  # noqa: E402
from levain.cli import main  # noqa: E402
from levain.cockpit.consent import utc_stamp  # noqa: E402
from levain.cockpit.firejournal import CockpitDB  # noqa: E402
from levain.cockpit.keys import KeyError_, fingerprint_of, revoke  # noqa: E402
from levain.consent import grants as G  # noqa: E402
from levain.firing import hands_mcp_login as hml  # noqa: E402
from tests._phone_fixtures import _enrol  # noqa: E402
from tests.chat_egress.keys_rig import record as chat_record  # noqa: E402
from tests.test_authority_agent_link import link as agent_link  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None or shutil.which("git") is None,
                                reason="needs ssh-keygen and git")

BORN = auth.STOPPED_BORN                 # "the key that signed it was revoked"
CURRENT = auth.STOPPED_CURRENT           # "the key that last renewed it was revoked"
DIGEST = "d" * 64
TRUSTED = "/bin/sh"                      # a command every test host holds as root's, outside every home


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _keygen(tmp_path: Path, name: str) -> tuple[Path, str, str]:
    path = tmp_path / "keys" / name
    path.parent.mkdir(exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)], check=True,
                   capture_output=True, timeout=30)
    pub = " ".join(path.with_name(name + ".pub").read_text().split()[:2])
    return path, pub, fingerprint_of(pub)


def _signer(path: Path):
    def sign(m: bytes) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(path), "-n", auth.NAMESPACE], input=m,
                              capture_output=True, check=True, timeout=30).stdout.decode()
    return sign


@pytest.fixture
def world(tmp_path: Path):
    """A throwaway entity whose cockpit (``<entity>/.levain/cockpit``, where ``levain doctor`` reads it) holds three
    laptop-presence keys, a consent hook directory naming it, and one helper per kind of standing grant."""
    keys = {name: _keygen(tmp_path, f"key{name.upper()}") for name in ("a", "b", "c")}
    entity = tmp_path / "entity"
    root = entity / ".levain" / "cockpit"
    root.mkdir(parents=True)
    CockpitDB(root).set_install_class("reported")
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    for _name, (_path, pub, fp) in keys.items():
        _enrol(conn, root, keys["a"][0], keys["a"][2], {"op": "enrol", "fingerprint": fp, "public_key": pub,
                                                        "class": "laptop-presence", "custody": "passphrase"})
    conn.close()
    fps = {name: keys[name][2] for name in keys}
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    os.chmod(qdir, 0o700)
    (qdir / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(root), "away_mode": False,
                                                       "away_hold_seconds": 0}))
    ws = Path(os.path.realpath(tmp_path / "proj"))
    ws.mkdir()
    lanes, returns = tmp_path / "lanes", tmp_path / "returns"
    key_dir = tmp_path / "seat_launcher"
    key_dir.mkdir(mode=0o700)
    remote = tmp_path / "remote.git"
    remote.mkdir()

    def sign_of(by: str):
        return fps[by], _signer(keys[by][0])

    def link(make_op, by: str) -> None:
        """A raw link key ``by`` signs; ``make_op(at)`` builds the op for the link's own stamp."""
        at = _now()
        conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
        try:
            auth.sign_and_append(conn, make_op(at), signer_fp=fps[by], sign=_signer(keys[by][0]), clock=lambda: at,
                                 cache=auth.ReplayCache(), anchor=auth.anchor_path(root))
        finally:
            conn.close()

    def consent(gid: str, by: str = "a", tools=("Write", "Edit"), days: int = 1) -> None:
        fp, sign = sign_of(by)
        G.create_grant(root, agent="claude_code", tools=list(tools), workspace=ws, postures=["default"],
                       expires=timedelta(days=days), signer_fp=fp, sign=sign, clock=_now, grant_id=gid)

    def shell(gid: str, by: str = "a") -> None:
        link(lambda at: {"op": "create_grant", "grant": {
            "id": gid, "kind": auth.CONSENT_CONTAINED_SHELL_KIND, "agent": "claude_code", "tools": ["Bash"],
            "workspace": str(ws), "postures": ["default"], "containment_digest": DIGEST,
            "expires_at": utc_stamp(at + timedelta(days=1))}}, by)

    def lease(gid: str, by: str = "a") -> None:
        fp, sign = sign_of(by)
        sl.create_lease(root, lanes_parent=lanes, returns_parent=returns, key_dir=key_dir, signer_fp=fp, sign=sign,
                        clock=_now, lease_id=gid)

    def egress(gid: str, by: str = "a") -> None:
        fp, sign = sign_of(by)
        sp.create_egress(root, lanes_parent=lanes, push_remote=f"file://{remote}", signer_fp=fp, sign=sign,
                         clock=_now, grant_id=gid)

    def entry(gid: str, name: str, by: str = "a") -> None:
        link(lambda at: {"op": "create_grant", "grant": hm.build_entry(
            name=name, transport="stdio", identity_kind="dedicated", command=TRUSTED, args=["--root", "/srv/shared"],
            expires_at=utc_stamp(at + timedelta(days=1)), entry_id=gid)}, by)

    def chat_key(rid: str, host: str = "example.com", by: str = "a") -> None:
        link(lambda _at: {"op": "create_grant", "grant": chat_record(rid, host)}, by)

    def agent_api_link(n: int = 1, by: str = "a") -> str:
        grant = agent_link(SimpleNamespace(clock=SimpleNamespace(now=_now())), n)
        link(lambda _at: {"op": "create_grant", "grant": grant}, by)
        return grant["id"]

    def inert(gid: str, monkeypatch, by: str = "a") -> None:
        """A grant of a kind this levain does not know: it can only be written by a levain that registers the kind."""
        with monkeypatch.context() as m:
            m.setattr(auth, "_GRANT_KINDS", MappingProxyType({**auth._GRANT_KINDS, "push": auth._GrantKind(
                validate=auth._validate_inert_grant, change=auth._inert_grant_change)}))
            link(lambda at: {"op": "create_grant", "grant": {
                "id": gid, "kind": "push", "workspace": str(ws), "remote_url": "file:///tmp/nowhere.git",
                "branches": ["claude/*"], "expires_at": utc_stamp(at + timedelta(hours=2))}}, by)

    def renew(gid: str, by: str, days: int = 5) -> None:
        link(lambda at: {"op": "renew", "grant_id": gid, "expires_at": utc_stamp(at + timedelta(days=days)),
                         "signed_at": utc_stamp(at)}, by)

    def revoke_key(target: str, by: str, compromised_since: int | None = None):
        fp, sign = sign_of(by)
        return revoke(root, fps[target], signer_fp=fp, sign=sign, clock=_now, compromised_since=compromised_since)

    def state():
        from levain.agent_authority import AgentAuthority
        return AgentAuthority(root).replay()

    return SimpleNamespace(root=root, entity=entity, qdir=qdir, ws=ws, lanes=lanes, remote=remote, keys=keys, fps=fps,
                           consent=consent, shell=shell, lease=lease, egress=egress, entry=entry, chat_key=chat_key,
                           agent_api_link=agent_api_link, inert=inert, renew=renew, revoke_key=revoke_key, link=link,
                           state=state)


def _rows(world) -> dict[str, dict]:
    return {r["id"]: r for r in G.grant_view(world.qdir, now=_now())["grants"]}


# --- T16: the state a stopped grant shows ----------------------------------------------------------------------

def test_t16_control_a_grant_whose_signers_are_active_is_live_with_no_reason(world) -> None:
    world.consent("cg-A")
    (row,) = _rows(world).values()
    assert (row["state"], row["stopped_why"]) == ("live", "")
    (grant,) = G.list_grants(world.root, now=_now())
    assert grant.stopped_why == ""


def test_t16_a_grant_whose_creator_was_revoked_shows_stopped_and_the_signed_sentence(world) -> None:
    world.consent("cg-A")
    world.revoke_key("a", by="b")
    row = _rows(world)["cg-A"]
    assert (row["state"], row["stopped_why"]) == ("stopped", BORN)
    assert [g.stopped_why for g in G.list_grants(world.root, now=_now())] == [BORN]
    assert [g.stopped_why for g in G.grant_listing(world.root, now=_now())[0]] == [BORN]


def test_t16_a_grant_whose_last_renewer_was_revoked_shows_the_renewed_sentence(world) -> None:
    world.consent("cg-A")
    world.renew("cg-A", "b")
    world.revoke_key("b", by="a")
    row = _rows(world)["cg-A"]
    assert (row["state"], row["stopped_why"]) == ("stopped", CURRENT)


def test_t16_a_key_that_signed_nothing_of_the_grant_changes_nothing(world) -> None:
    world.consent("cg-A")
    world.revoke_key("c", by="a")
    row = _rows(world)["cg-A"]
    assert (row["state"], row["stopped_why"]) == ("live", "")


def test_t16_stopped_wins_over_expired(world) -> None:
    """A grant that has also lapsed says stopped: the stop explains it, and the way out is the same."""
    world.consent("cg-A", days=1)
    world.revoke_key("a", by="b")
    page = G.grant_view(world.qdir, now=_now() + timedelta(days=3))
    (row,) = page["grants"]
    assert (row["state"], row["stopped_why"]) == ("stopped", BORN)
    world.consent("cg-B", by="b", days=1)
    page = G.grant_view(world.qdir, now=_now() + timedelta(days=3))
    assert {r["id"]: r["state"] for r in page["grants"]} == {"cg-A": "stopped", "cg-B": "expired"}


def test_t16_every_kind_the_page_lists_shows_stopped(world, monkeypatch) -> None:
    """The consent, contained-shell, seat-root lease, seat-egress and inert rows all take the state; the page reads
    one rule for them (the kind-blind helper), not one per kind."""
    world.consent("cg-A")
    world.shell("sh-A")
    world.lease("sl-aaaaaaaaaaaa")
    world.egress("se-aaaaaaaaaaaa")
    world.inert("pg-0123456789ab", monkeypatch)
    world.consent("cg-B", by="b")                                    # control: B's grant stays live
    world.revoke_key("a", by="b")
    rows = _rows(world)
    assert {gid: (r["state"], r["stopped_why"]) for gid, r in rows.items()} == {
        "cg-A": ("stopped", BORN), "sh-A": ("stopped", BORN), "sl-aaaaaaaaaaaa": ("stopped", BORN), "se-aaaaaaaaaaaa": ("stopped", BORN),
        "pg-0123456789ab": ("stopped", BORN), "cg-B": ("live", "")}


def test_t16_the_phone_read_carries_the_state_and_the_reason(world) -> None:
    """The phone's listing is built from the same rows (``grant_phone``); the new fields travel with them. ``listing`` is
    driven on a stand-in holding the two reads it makes (the queue directory and the clock; no passkey is enrolled)."""
    from levain.consent.grant_phone import GrantPhone

    world.consent("cg-A")
    world.revoke_key("a", by="b")
    stand_in = SimpleNamespace(queue_dir=world.qdir, _clock=_now, _signers=lambda: ({}, ""))
    (row,) = GrantPhone.listing(stand_in)["grants"]
    assert (row["state"], row["stopped_why"]) == ("stopped", BORN)


# --- T16: the terminal lists ----------------------------------------------------------------------------------

def _out(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    got = capsys.readouterr()
    return code, got.out, got.err


STOPPED_LINE = f"stopped: {BORN} (answers nothing; create it again to use it)"


def test_t16_consent_grant_list_says_stopped_and_not_until(world, capsys, monkeypatch) -> None:
    world.consent("cg-A")
    world.consent("cg-B", by="b")
    world.inert("pg-0123456789ab", monkeypatch)
    code, out, _err = _out(capsys, "consent", "grant", "list", "--data-root", str(world.root))
    assert code == 0
    live = next(line for line in out.splitlines() if line.startswith("cg-A"))
    assert " until " in live and "stopped" not in live                                  # control
    world.revoke_key("a", by="b")
    code, out, _err = _out(capsys, "consent", "grant", "list", "--data-root", str(world.root))
    lines = {line.split()[0].strip("'"): line for line in out.splitlines()}
    assert STOPPED_LINE in lines["cg-A"] and " until " not in lines["cg-A"]
    assert " until " in lines["cg-B"] and "stopped" not in lines["cg-B"]
    assert STOPPED_LINE in lines["pg-0123456789ab"] and "held inert" in lines["pg-0123456789ab"]


def test_t16_a_grant_expired_and_stopped_lists_as_stopped(world, capsys, monkeypatch) -> None:
    """The list line takes the same precedence as the page's state."""
    import levain.cli as cli

    world.consent("cg-A")
    world.revoke_key("a", by="b")
    monkeypatch.setattr(cli, "_utc_now", lambda: _now() + timedelta(days=3))
    code, out, _err = _out(capsys, "consent", "grant", "list", "--data-root", str(world.root))
    assert code == 0 and STOPPED_LINE in out and "  expired  " not in out


def test_t16_chat_key_list_says_stopped(world, capsys) -> None:
    world.chat_key("ek-000000000001")
    world.chat_key("ek-000000000002", host="other.example.com", by="b")
    rows = {r["id"]: r for r in chat_list_keys(world.root, now=_now())}
    assert (rows["ek-000000000001"]["state"], rows["ek-000000000001"]["stopped_why"]) == ("live", "")
    world.revoke_key("a", by="b")
    rows = {r["id"]: r for r in chat_list_keys(world.root, now=_now())}
    assert (rows["ek-000000000001"]["state"], rows["ek-000000000001"]["stopped_why"]) == ("stopped", BORN)
    assert (rows["ek-000000000002"]["state"], rows["ek-000000000002"]["stopped_why"]) == ("live", "")
    code, out, _err = _out(capsys, "chat", "key", "list", "--data-root", str(world.root))
    lines = {line.split()[0]: line for line in out.splitlines()}
    assert code == 0 and STOPPED_LINE in lines["ek-000000000001"] and " until " not in lines["ek-000000000001"]
    assert " until " in lines["ek-000000000002"]


def test_t16_seat_lease_and_egress_list_say_stopped(world, capsys) -> None:
    world.lease("sl-aaaaaaaaaaaa")
    world.egress("se-aaaaaaaaaaaa")
    code, out, _err = _out(capsys, "seat", "lease", "list", "--data-root", str(world.root))
    assert code == 0 and out.startswith("sl-aaaaaaaaaaaa") and " until " in out                  # control
    world.revoke_key("a", by="b")
    code, out, _err = _out(capsys, "seat", "lease", "list", "--data-root", str(world.root))
    assert code == 0 and out.startswith("sl-aaaaaaaaaaaa") and STOPPED_LINE in out and " until " not in out
    code, out, _err = _out(capsys, "seat", "egress", "list", "--data-root", str(world.root))
    assert code == 0 and out.startswith("se-aaaaaaaaaaaa") and STOPPED_LINE in out and "no live seat-egress grant" not in out


def test_t16_a_lease_that_is_revoked_still_reads_as_none(world, capsys) -> None:
    """The stopped words are for a lease the policy still holds: with none held the old words stand."""
    code, out, _err = _out(capsys, "seat", "lease", "list", "--data-root", str(world.root))
    assert code == 0 and out.strip() == "no live seat-root lease"


def test_t16_hands_mcp_list_and_the_sign_in_refusal_say_stopped(world, tmp_path, monkeypatch) -> None:
    world.entry("hm-000000000001", "files")
    world.entry("hm-000000000002", "docs", by="b")
    world.revoke_key("a", by="b")
    monkeypatch.setattr(hml.hl, "load_hands_identity", lambda entity: object())
    monkeypatch.setattr(hml, "read_record", lambda entity: {})
    monkeypatch.setattr(hml, "vendor_servers", lambda hands, entity, sweep=False: {})
    rows, _code = hml.status_rows(world.entity, world.root)
    lines = [line for _ok, line in rows if line.startswith("hands mcp entry")]
    assert lines == [f"hands mcp entry hm-000000000001: stopped: {BORN} (answers nothing; register it again to use it)"]
    with pytest.raises(hml.HandsMcpRefused) as exc:
        hml.entry_for_signin(world.entity, world.root, "files")
    assert str(exc.value) == (f"'files' is registered but stopped: {BORN}; register it again with a key you hold. "
                              "Nothing was changed")
    assert "renew" not in str(exc.value)


def test_t16_an_entry_that_is_merely_expired_keeps_its_old_words(world, tmp_path) -> None:
    """Only the ``signer`` reason is reworded: an expired entry is still told to renew or re-register it."""
    world.entry("hm-000000000001", "files")
    with pytest.raises(hml.HandsMcpRefused) as exc:
        hml.entry_for_signin(world.entity, world.root, "files", _now() + timedelta(days=3))
    assert "not live (expired); renew or re-register it" in str(exc.value)


# --- T16: the voice key ---------------------------------------------------------------------------------------

@pytest.fixture
def voice(tmp_path: Path):
    from tests.test_revoked_signer_voice import Rig
    rig = Rig(tmp_path)
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    os.chmod(qdir, 0o700)
    (qdir / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(rig.root), "away_mode": False,
                                                       "away_hold_seconds": 0}))
    rig.qdir = qdir
    return rig


def test_t16_a_stopped_voice_key_is_listed_stopped_everywhere(voice, capsys) -> None:
    from levain.cockpit import voice as cockpit_voice
    from levain.voice import custody
    from levain.voice import meter as vmeter

    rec, _ = voice.add("b")
    rows = custody.list_keys(voice.root, now=_now())
    assert [(r["id"], r["state"], r["stopped_why"]) for r in rows] == [(rec["id"], "live", "")]
    voice.revoke_key("b")
    (row,) = custody.list_keys(voice.root, now=_now())
    assert (row["id"], row["state"], row["stopped_why"]) == (rec["id"], "stopped", BORN)
    page = {r["id"]: r for r in G.grant_view(voice.qdir, now=_now())["grants"]}
    assert (page[rec["id"]]["state"], page[rec["id"]]["stopped_why"]) == ("stopped", BORN)
    code, out, _err = _out(capsys, "voice", "key", "list", "--data-root", str(voice.root))
    assert code == 0 and STOPPED_LINE in out and "stands until revoked" not in out
    body = cockpit_voice.serve_voice(voice.root, vmeter.Meter(voice.tmp / "voice-ledger.db"), last_read={})
    openai = next(p for p in body["providers"] if p["name"] == "openai")
    other = next(p for p in body["providers"] if p["name"] == "deepgram")
    assert (openai["key"], openai["stopped_why"]) == ("stopped", BORN)
    assert (other["key"], other["stopped_why"]) == ("not_set", "")                 # a provider never keyed stays no key


def test_t16_a_live_voice_key_still_says_it_stands_until_revoked(voice, capsys) -> None:
    voice.add("b")
    code, out, _err = _out(capsys, "voice", "key", "list", "--data-root", str(voice.root))
    assert code == 0 and "stands until revoked" in out and "stopped" not in out


# --- T17: the revoke verb names what it stopped -----------------------------------------------------------------

def _revoke_cli(world, capsys, target: str, by: str = "b", *extra: str) -> tuple[int, str, str]:
    return _out(capsys, "cockpit", "revoke", world.fps[target], "--signing-key", str(world.keys[by][0]),
                "--data-root", str(world.root), *extra)


def test_t17_the_revoke_names_each_grant_the_key_signed_with_what_it_is(world, capsys) -> None:
    world.consent("cg-1a2b3c4d5e6f")
    world.shell("cg-sh0000000001")
    world.lease("sl-9f8e7d6c5b4a")
    world.egress("se-000000000001")
    world.entry("hm-000000000001", "files")
    world.chat_key("ek-000000000001")
    world.consent("cg-keptbyb", by="b")                                  # signed by the survivor: not named
    seat = world.state().policy.grants
    code, out, err = _revoke_cli(world, capsys, "a")
    lines = out.splitlines()
    assert code == 0 and err == ""
    assert lines[0] == f"revoked: {world.fps['a']} at seq {world.state().keys[world.fps['a']].revoked_seq}"
    assert lines[1] == ("6 standing grants this key signed have stopped. They answer nothing from now on, though they "
                        "have not expired:")
    assert lines[2:8] == [
        f"  cg-1a2b3c4d5e6f  consent grant: claude_code may Edit, Write in {world.ws}",
        f"  cg-sh0000000001  contained-shell grant: contained shell for claude_code in {world.ws}",
        "  ek-000000000001  chat key: ent sends its key to example.com",
        "  hm-000000000001  hands-mcp entry: hands server files",
        f"  se-000000000001  seat-egress grant: seats push to {seat['se-000000000001']['push_remote']}",
        f"  sl-9f8e7d6c5b4a  seat-root lease: seats launch under {seat['sl-9f8e7d6c5b4a']['lanes_parent']}"]
    assert "cg-keptbyb" not in out
    assert lines[8] == "To use one again, create it again with a key you still hold (a renew does not bring it back)."
    assert lines[9] == "To clear it from the list: levain consent grant revoke --signing-key <key> -- <id>"
    assert lines[10] == "A chat key clears with: levain chat key revoke --signing-key <key> <id>"
    assert len(lines) == 11


def test_t17_each_kinds_tail_is_what_the_design_says(world) -> None:
    world.consent("cg-A")
    world.shell("sh-A")
    world.lease("sl-aaaaaaaaaaaa")
    world.egress("se-aaaaaaaaaaaa")
    world.entry("hm-000000000001", "files")
    world.chat_key("ek-000000000001")
    world.revoke_key("a", by="b")
    got = {gid: words for gid, _kind, words in G.stopped_by_key(world.root, world.fps["a"], now=_now())}
    grants = world.state().policy.grants
    assert got == {
        "cg-A": f"consent grant: claude_code may Edit, Write in {world.ws}",
        "sh-A": f"contained-shell grant: contained shell for claude_code in {world.ws}",
        "sl-aaaaaaaaaaaa": f"seat-root lease: seats launch under {grants['sl-aaaaaaaaaaaa']['lanes_parent']}",
        "se-aaaaaaaaaaaa": f"seat-egress grant: seats push to {grants['se-aaaaaaaaaaaa']['push_remote']}",
        "hm-000000000001": "hands-mcp entry: hands server files",
        "ek-000000000001": "chat key: ent sends its key to example.com"}


def test_t17_the_voice_key_tail_names_the_provider(voice) -> None:
    rec, _ = voice.add("b")
    voice.revoke_key("b")
    assert G.stopped_by_key(voice.root, voice.keys["b"][1], now=_now()) == [(rec["id"], "voice-key", "voice key: OpenAI voice key")]


def test_t17_one_grant_reads_in_the_singular(world, capsys) -> None:
    world.consent("cg-A")
    code, out, _err = _revoke_cli(world, capsys, "a")
    lines = out.splitlines()
    assert code == 0
    assert lines[1] == "1 standing grant this key signed has stopped. It answers nothing from now on, though it has not expired:"
    assert lines[2].startswith("  cg-A  consent grant: claude_code may ")
    assert "A chat key clears" not in out                                   # only said when a chat key is named


def test_t17_a_key_that_signed_no_grant_says_so(world, capsys) -> None:
    world.consent("cg-A")
    code, out, err = _revoke_cli(world, capsys, "c", "a")
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0].startswith(f"revoked: {world.fps['c']} at seq ")
    assert lines[1:] == ["No standing grant was signed by this key."]


def test_t17_a_grant_the_key_only_renewed_is_named(world, capsys) -> None:
    world.consent("cg-A")
    world.renew("cg-A", "b")
    code, out, _err = _revoke_cli(world, capsys, "b", "a")
    assert code == 0 and "  cg-A  consent grant:" in out


def test_t17_the_revoked_line_is_unchanged_with_compromised_since(world, capsys) -> None:
    world.consent("cg-A")
    since = world.state().keys[world.fps["a"]].enrolled_seq
    code, out, _err = _revoke_cli(world, capsys, "a", "b", "--compromised-since", str(since))
    first = out.splitlines()[0]
    seq = world.state().keys[world.fps["a"]].revoked_seq
    assert code == 0 and first == f"revoked: {world.fps['a']} at seq {seq} (compromised since seq {since})"
    assert "  cg-A  consent grant:" in out


def test_t17_a_list_that_cannot_be_read_is_said_and_the_revoke_still_succeeds(world, capsys, monkeypatch) -> None:
    """Words only: the list is a second read, so a fault in it is a note on stderr and never the revoke's exit code."""
    world.consent("cg-A")

    def boom(*_a, **_k):
        raise KeyError_("the cockpit journal moved")
    monkeypatch.setattr(G, "stopped_by_key", boom)
    code, out, err = _revoke_cli(world, capsys, "a")
    assert code == 0 and out.startswith(f"revoked: {world.fps['a']} at seq ") and out.count("\n") == 1
    assert "could not be listed (the cockpit journal moved)" in err
    assert world.state().keys[world.fps["a"]].revoked_seq is not None             # and the key is revoked


def test_t17_a_creator_string_is_shown_through_visible() -> None:
    """Every creator string in the notice goes through ``visible``: an agent, a workspace, a host or a name that carries a
    terminal control sequence is shown as its marker."""
    hostile = "x\x1b[31mred"
    grants = [
        {"kind": auth.CONSENT_GRANT_KIND, "agent": hostile, "tools": [hostile], "workspace": hostile},
        {"kind": auth.CONSENT_CONTAINED_SHELL_KIND, "agent": hostile, "workspace": hostile},
        {"kind": auth.SEAT_LEASE_KIND, "lanes_parent": hostile},
        {"kind": auth.SEAT_EGRESS_KIND, "push_remote": hostile},
        {"kind": auth.EGRESS_KEY_KIND, "entity": hostile, "host": hostile},
        {"kind": auth.VOICE_KEY_KIND, "provider": hostile},
        {"kind": auth.HANDS_MCP_KIND, "name": hostile},
        {"kind": hostile}]
    for grant in grants:
        tail = G._stopped_tail(grant)
        assert "\x1b" not in tail and "⟨U+001B⟩" in tail, (grant["kind"], tail)


def test_t17_the_notice_is_the_only_place_the_verb_adds_words() -> None:
    """``stopped_notice`` is a pure function of its rows: empty is the none sentence, never silence."""
    assert G.stopped_notice([]) == ["No standing grant was signed by this key."]
    two = G.stopped_notice([("a", "consent", "consent grant: x"), ("b", "voice-key", "voice key: OpenAI voice key")])
    assert two[0].startswith("2 standing grants this key signed have stopped.")


# --- T18: the doctor row ------------------------------------------------------------------------------------

def test_t18_no_row_while_no_grant_is_stopped(world) -> None:
    world.consent("cg-A")
    assert D._check_standing_grants(world.entity) == []


def test_t18_no_row_without_a_cockpit_journal(tmp_path) -> None:
    assert D._check_standing_grants(tmp_path / "nothing") == []


def test_t18_one_warn_row_names_the_stopped_grants_and_the_way_out(world) -> None:
    world.consent("cg-A")
    world.lease("sl-aaaaaaaaaaaa")
    world.consent("cg-B", by="b")
    world.revoke_key("a", by="b")
    (row,) = D._check_standing_grants(world.entity)
    assert (row.name, row.ok, row.warn) == ("standing grants", True, True)
    assert row.detail == ("2 standing grants stopped because the key that signed them was revoked; they answer nothing "
                          "(cg-A, sl-aaaaaaaaaaaa)")
    assert row.hint == "Create them again with a key you hold, or revoke them to clear the list."


def test_t18_one_stopped_grant_reads_in_the_singular(world) -> None:
    world.consent("cg-A")
    world.revoke_key("a", by="b")
    (row,) = D._check_standing_grants(world.entity)
    assert row.detail == "1 standing grant stopped because the key that signed it was revoked; it answers nothing (cg-A)"
    assert row.hint == "Create it again with a key you hold, or revoke it to clear the list."


def test_t18_a_last_renewer_revoke_says_a_key_that_signed_or_renewed(world) -> None:
    world.consent("cg-A")
    world.renew("cg-A", "b")
    world.revoke_key("b", by="a")
    (row,) = D._check_standing_grants(world.entity)
    assert "stopped because a key that signed or last renewed it was revoked" in row.detail


def test_t18_the_row_goes_when_the_stopped_grant_is_revoked(world, capsys) -> None:
    world.consent("cg-A")
    world.revoke_key("a", by="b")
    assert len(D._check_standing_grants(world.entity)) == 1
    assert main(["consent", "grant", "revoke", "cg-A", "--data-root", str(world.root), "--signing-key",
                 str(world.keys["b"][0])]) == 0
    capsys.readouterr()
    assert D._check_standing_grants(world.entity) == []


def test_t18_the_row_is_wired_into_the_doctor_run() -> None:
    assert '_check_standing_grants(install)' in inspect.getsource(D._run_doctor)


def test_t18_a_stopped_api_key_agent_link_gets_no_budget_row(world) -> None:
    """The doctor's brokered-launcher rows list the standing api-key agent links; one a revoked key signed is stopped and
    is not listed (it would refuse every run), a link of a surviving key is."""
    stopped = world.agent_api_link(1, "a")
    kept = world.agent_api_link(2, "b")
    assert [gid for gid, _budget, _x in D._api_key_links(world.entity)] == [stopped, kept]            # control
    world.revoke_key("a", by="b")
    assert [gid for gid, _budget, _x in D._api_key_links(world.entity)] == [kept]


def test_t18_the_doctor_builders_use_only_the_accessors() -> None:
    """The doctor's row reads the raw policy only to NAME a stopped grant; what makes a grant stopped is the helper."""
    src = inspect.getsource(D._check_standing_grants)
    assert "grant_stop_reason" in src
    tree = ast.parse(inspect.getsource(D._api_key_links).lstrip())
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "grant_stop_reason" in names
