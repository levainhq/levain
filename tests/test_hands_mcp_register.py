"""`levain hands mcp register|revoke` (leg 6). Built from the reproduced path: the real verb run through cli.main
against a scratch cockpit whose one enrolled laptop-presence key is a scratch ssh key in tmp. The consent boundary is
what is asserted: each verb signs exactly one link of the stated shape, and every refusal comes BEFORE a signature is
asked for (a counting signer proves it) and leaves the log's head where it was."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.authority import HANDS_MCP_KIND
from levain.cli import build_parser, main
from levain.cockpit.keys import KeyError_
from levain import hands_mcp_verbs as hv
from levain.firing import hands_mcp_login as hml
from tests.chat_egress.keys_rig import Cockpit

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="ssh-keygen")

URL = "https://mcp.example.com/mcp"


class Counting:
    """A signer that counts the signatures it was asked for."""

    def __init__(self, ck: Cockpit) -> None:
        self.ck, self.asked = ck, 0

    def __call__(self, m: bytes) -> str:
        self.asked += 1
        return self.ck.sign(m)


@pytest.fixture
def ck(tmp_path):
    return Cockpit(tmp_path)


def head(ck):
    return ck.state().seq


def argv(ck, verb, *rest):
    return ["hands", "mcp", verb, *rest, "--path", str(ck.root.parent / "entity"), "--data-root", str(ck.root),
            "--signing-key", str(ck.key)]


def live(ck):
    return {e["name"]: e for e in hml.load_registry(ck.root)[0]}


def grants_of_kind(ck):
    return {g: v for g, v in ck.state().policy.grants.items() if v.get("kind") == HANDS_MCP_KIND}


def test_register_through_the_real_verb_appends_one_create_grant_and_the_entry_is_live(ck, capsys):
    before = head(ck)
    assert main(argv(ck, "register", "linear", "--url", URL)) == 0
    out = capsys.readouterr().out
    assert head(ck) == before + 1                                   # exactly one link
    e = live(ck)["linear"]
    assert f"registered: {e['id']}" in out and e["transport"] == "remote" and e["url"] == URL
    assert e["identity_kind"] == "dedicated" and e["id"].startswith("hm-")
    life = datetime.strptime(e["expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
    assert timedelta(days=29) < life <= timedelta(days=30)


def test_register_days_and_identity_kind_are_carried(ck):
    assert main(argv(ck, "register", "linear", "--url", URL, "--days", "3", "--identity-kind", "operator")) == 0
    e = live(ck)["linear"]
    assert e["identity_kind"] == "operator"
    assert datetime.strptime(e["expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc) < datetime.now(timezone.utc) + timedelta(days=3, minutes=1)


def test_register_refuses_a_name_the_log_holds_before_any_signature(ck, capsys):
    hv.register(ck.root, name="linear", transport="remote", identity_kind="dedicated", url=URL,
                signer_fp=ck.fp, sign=ck.sign)
    s, before = Counting(ck), head(ck)
    with pytest.raises(KeyError_, match="one entry per name"):
        hv.register(ck.root, name="linear", transport="remote", identity_kind="dedicated", url=URL + "2",
                    signer_fp=ck.fp, sign=s)
    assert s.asked == 0 and head(ck) == before
    assert main(argv(ck, "register", "linear", "--url", URL)) == 1
    assert "register refused" in capsys.readouterr().err and head(ck) == before


def test_register_refuses_a_secret_in_the_url_before_any_signature(ck, capsys):
    s, before = Counting(ck), head(ck)
    with pytest.raises(KeyError_, match="credential"):
        hv.register(ck.root, name="bad", transport="remote", identity_kind="dedicated",
                    url=URL + "?token=abcdefabcdefabcdefabcdef", signer_fp=ck.fp, sign=s)
    assert s.asked == 0 and head(ck) == before and grants_of_kind(ck) == {}


def test_register_refuses_a_stdio_command_in_no_trusted_directory(ck):
    s = Counting(ck)
    with pytest.raises(KeyError_):
        hv.register(ck.root, name="files", transport="stdio", identity_kind="dedicated",
                    command="definitely-not-installed-anywhere", signer_fp=ck.fp, sign=s)
    assert s.asked == 0 and grants_of_kind(ck) == {}


def test_register_refuses_a_non_laptop_signer_before_any_signature(tmp_path, ck):
    """A display-phone key enrolled in the log may not sign a create_grant: the log refuses before a signature."""
    other = tmp_path / "keys" / "phone"
    import subprocess
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True, capture_output=True)
    pub = " ".join((tmp_path / "keys" / "phone.pub").read_text().split()[:2])
    fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(other)], check=True, capture_output=True,
                        text=True).stdout.split()[1]
    ck.link({"op": "enrol", "fingerprint": fp, "public_key": pub, "class": "display-phone", "custody": "unstated"})
    asked = []
    before = head(ck)
    with pytest.raises(KeyError_):
        hv.register(ck.root, name="linear", transport="remote", identity_kind="dedicated", url=URL, signer_fp=fp,
                    sign=lambda m: asked.append(m) or "never")
    assert asked == [] and head(ck) == before


def test_a_failed_signature_writes_nothing(ck, capsys):
    def no_tty(m: bytes) -> str:
        raise KeyError_("ssh-keygen could not sign with the key (exit 255)")
    before = head(ck)
    with pytest.raises(KeyError_):
        hv.register(ck.root, name="linear", transport="remote", identity_kind="dedicated", url=URL,
                    signer_fp=ck.fp, sign=no_tty)
    assert head(ck) == before and grants_of_kind(ck) == {}


def test_revoke_by_name_and_by_id_ends_the_entry_and_signs_one_revoke_grant(ck, capsys):
    assert main(argv(ck, "register", "linear", "--url", URL)) == 0
    gid = live(ck)["linear"]["id"]
    before = head(ck)
    assert main(argv(ck, "revoke", "linear")) == 0
    assert f"revoked: {gid}" in capsys.readouterr().out
    assert head(ck) == before + 1 and "linear" not in live(ck) and grants_of_kind(ck) == {}
    # by id
    assert main(argv(ck, "register", "two", "--url", URL)) == 0
    g2 = live(ck)["two"]["id"]
    assert main(argv(ck, "revoke", g2)) == 0 and live(ck) == {}


def test_revoke_of_an_unknown_target_refuses_before_any_signature(ck):
    s, before = Counting(ck), head(ck)
    with pytest.raises(KeyError_, match="no hands-MCP entry"):
        hv.revoke(ck.root, "hm-000000000000", signer_fp=ck.fp, sign=s)
    assert s.asked == 0 and head(ck) == before


def test_revoke_never_touches_a_grant_that_is_not_a_hands_mcp_entry(ck, tmp_path):
    from levain.consent.grants import create_grant
    ws = tmp_path / "ws"
    ws.mkdir()
    cg, _w = create_grant(ck.root, agent="claude_code", tools=["Read"], workspace=ws, postures=["default"],
                          expires=timedelta(days=1), signer_fp=ck.fp, sign=ck.sign,
                          clock=lambda: datetime.now(timezone.utc))
    s, before = Counting(ck), head(ck)
    with pytest.raises(KeyError_, match="no hands-MCP entry"):
        hv.revoke(ck.root, cg.id, signer_fp=ck.fp, sign=s)
    assert s.asked == 0 and head(ck) == before and cg.id in ck.state().policy.grants


def test_ambiguous_name_is_refused_by_target_resolution():
    held = {"hm-aaaaaaaaaaaa": {"name": "x"}, "hm-bbbbbbbbbbbb": {"name": "x"}}
    with pytest.raises(KeyError_, match="revoke by id"):
        hv.resolve_target(held, "x")
    assert hv.resolve_target(held, "hm-bbbbbbbbbbbb") == "hm-bbbbbbbbbbbb"


def test_register_with_id_recreates_the_same_id_after_a_revoke_and_moves_its_incarnation(ck, capsys):
    assert main(argv(ck, "register", "linear", "--url", URL)) == 0
    gid = live(ck)["linear"]["id"]
    born1: dict = {}
    hml.load_registry(ck.root, born=born1)
    # a live id is not created twice
    s = Counting(ck)
    with pytest.raises(KeyError_, match="already holds an entry with id"):
        hv.register(ck.root, name="other", transport="remote", identity_kind="dedicated", url=URL, entry_id=gid,
                    signer_fp=ck.fp, sign=s)
    assert s.asked == 0
    assert main(argv(ck, "revoke", gid)) == 0
    assert main(argv(ck, "register", "linear", "--url", URL, "--id", gid)) == 0
    assert live(ck)["linear"]["id"] == gid
    born2: dict = {}
    hml.load_registry(ck.root, born=born2)
    assert born2[gid] > born1[gid]                                  # a new incarnation: a running chat on the old one ends


def test_register_and_revoke_parse_with_the_documented_arguments():
    p = build_parser()
    a = p.parse_args(["hands", "mcp", "register", "linear", "--url", URL, "--id", "hm-0123456789ab",
                      "--signing-key", "k", "--days", "7"])
    assert (a.name, a.entry_id, a.days, a.transport, a.identity_kind) == ("linear", "hm-0123456789ab", 7, "remote", "dedicated")
    a = p.parse_args(["hands", "mcp", "revoke", "linear", "--signing-key", "k"])
    assert a.target == "linear"
    with pytest.raises(SystemExit):
        p.parse_args(["hands", "mcp", "revoke", "linear"])          # --signing-key is required


def test_the_verb_module_has_no_way_around_the_terminal_prompt():
    """The signer is ssh_signer and nothing else: no password argument, no env var, no agent handling in the module."""
    src = Path(hv.__file__).read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"""')))
    for word in ("passphrase=", "SSH_ASKPASS", "-P ", "getpass", "os.environ", "SSH_AUTH_SOCK"):
        assert word not in code
    assert "ssh_signer" in code


# --- leg 6 L3 r1 (codex HIGH): a revoke is bound to the head it validated ------------------------------------------

def _recreate(ck, gid, name="linear2"):
    """Another writer revokes ``gid`` and creates it again (a new incarnation of the same id)."""
    from levain import hands_mcp as hm
    ck.link({"op": "revoke_grant", "grant_id": gid})
    ck.link({"op": "create_grant", "grant": hm.build_entry(
        name=name, transport="remote", identity_kind="dedicated", url=URL + "/new", entry_id=gid,
        expires_at=hm.lease_expiry(datetime.now(timezone.utc)))})


def test_a_recreate_between_the_check_and_the_prepare_refuses_the_revoke(ck, monkeypatch):
    assert main(argv(ck, "register", "linear", "--url", URL)) == 0
    gid = live(ck)["linear"]["id"]
    real = hv._log_entries

    def then_recreate(db, ssh_keygen, clock=hv._utc_now):
        out = real(db, ssh_keygen, clock)
        _recreate(ck, gid)
        return out
    monkeypatch.setattr(hv, "_log_entries", then_recreate)
    s = Counting(ck)
    with pytest.raises(KeyError_, match="moved since"):
        hv.revoke(ck.root, "linear", signer_fp=ck.fp, sign=s)
    assert s.asked == 0
    assert live(ck)["linear2"]["id"] == gid                        # the new incarnation was not revoked


def test_a_recreate_during_the_signature_refuses_the_revoke(ck):
    assert main(argv(ck, "register", "linear", "--url", URL)) == 0
    gid = live(ck)["linear"]["id"]

    def sign(m: bytes) -> str:
        _recreate(ck, gid)
        return ck.sign(m)
    with pytest.raises(KeyError_, match="moved since"):
        hv.revoke(ck.root, gid, signer_fp=ck.fp, sign=sign)
    assert live(ck)["linear2"]["id"] == gid


def test_a_consent_grant_recreated_between_the_check_and_the_append_is_not_revoked(ck, tmp_path, monkeypatch):
    from levain.consent import grants as gr
    ws = tmp_path / "ws"
    ws.mkdir()
    now = lambda: datetime.now(timezone.utc)   # noqa: E731
    cg, _w = gr.create_grant(ck.root, agent="claude_code", tools=["Read"], workspace=ws, postures=["default"],
                             expires=timedelta(days=1), signer_fp=ck.fp, sign=ck.sign, clock=now)
    real = gr._revocable

    def then_recreate(*a, **k):
        out = real(*a, **k)
        ck.link({"op": "revoke_grant", "grant_id": cg.id})
        gr.create_grant(ck.root, agent="claude_code", tools=["Grep"], workspace=ws, postures=["default"],
                        expires=timedelta(days=1), signer_fp=ck.fp, sign=ck.sign, clock=now, grant_id=cg.id)
        return out
    monkeypatch.setattr(gr, "_revocable", then_recreate)
    with pytest.raises(KeyError_, match="moved since"):
        gr.revoke_grant(ck.root, cg.id, signer_fp=ck.fp, sign=ck.sign, clock=now)
    assert list(ck.state().policy.grants[cg.id]["tools"]) == ["Grep"]   # the recreated grant stands


# --- leg 6 fix2 8 (L3 r1 smalls) ----------------------------------------------------------------------------------

@pytest.mark.parametrize("days", ["0", "-3", "31", "999999999", "10" * 30])
def test_days_out_of_range_is_a_named_refusal_not_a_traceback(ck, capsys, days):
    """gemini LOW, codex LOW: a huge --days overflowed timedelta into a traceback past _refusals()."""
    before = head(ck)
    assert main(argv(ck, "register", "linear", "--url", URL, "--days", days)) == 1
    err = capsys.readouterr().err
    assert "register refused:" in err and "days" in err and "Traceback" not in err
    assert head(ck) == before


def test_a_name_shaped_like_an_entry_id_is_refused_at_register(ck, capsys):
    """complement LOW: resolve_target matches an id before a name, so a name shaped like hm-<12 hex> could make a
    revoke by that name end a different entry."""
    s, before = Counting(ck), head(ck)
    with pytest.raises(KeyError_, match="looks like an entry id"):
        hv.register(ck.root, name="hm-aabbccddeeff", transport="remote", identity_kind="dedicated", url=URL,
                    signer_fp=ck.fp, sign=s)
    assert s.asked == 0 and head(ck) == before


def _corrupt(ck, how):
    import sqlite3
    db = ck.root / "cockpit.db"
    if how == "garbage":
        db.write_bytes(b"garbage" * 200)
    elif how == "truncated":
        b = db.read_bytes()
        db.write_bytes(b[: len(b) // 2 + 100])
    else:
        sql = {"record": "UPDATE authority_log SET record='{not json' WHERE seq=(SELECT MAX(seq) FROM authority_log)",
               "utf8": "UPDATE authority_log SET record=X'80FF00' WHERE seq=(SELECT MAX(seq) FROM authority_log)",
               "signature": "UPDATE authority_log SET signature=123 WHERE seq=(SELECT MAX(seq) FROM authority_log)",
               "no-log": "DROP TABLE authority_log",
               "no-meta": "DROP TABLE meta"}[how]
        c = sqlite3.connect(db, isolation_level=None)
        try:
            c.execute(sql)
        finally:
            c.close()


@pytest.mark.parametrize("how", ["record", "utf8", "signature", "no-log", "no-meta", "garbage", "truncated"])
@pytest.mark.parametrize("verb", ["register", "revoke"])
def test_a_corrupted_log_is_a_clean_refusal_through_register_and_revoke(ck, capsys, verb, how):
    """Leg 6 L3 r2 (deepseek OPEN): the log read (_log_entries) sits outside the verbs' own try, so its errors must
    be ones the CLI turns into a refusal. Measured (fix3 probe): every shape here already was; this pins it."""
    _corrupt(ck, how)
    rest = ["linear", "--url", URL] if verb == "register" else ["linear"]
    assert main(argv(ck, verb, *rest)) == 1
    err = capsys.readouterr().err
    assert f"{verb} refused:" in err and "Traceback" not in err
