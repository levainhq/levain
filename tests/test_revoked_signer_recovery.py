"""Levain board row 190, L4 F1 and F3: the way back from a stopped grant is told truthfully, per kind.

Ruling R-C keeps a stopped grant COUNTED by the one-live append rules (an older levain reading the same log counts it
too). So for a kind that allows one live grant at a time (the seat-root lease, the seat-egress grant, a hands-mcp
entry of one name, a chat key per host, a voice key per provider) "create it again" is refused until the stopped one is
revoked, and a hands entry has no verb that registers one at all. These tests hold what each surface says:

* the one-live refusal names the blocker as STOPPED (never "already live"), and the revoke verb that clears it, with
  every argument its parser requires (checked by parsing the words with ``levain.cli.build_parser``);
* the revoke notice orders the way back per kind, and following its own two steps in order lets the create land;
* the list line, the renew refusal and the doctor row say the same order;
* a stopped hands entry is not a healthy row, and its words name the verb that exists.

Throwaway keys and cockpit from ``tests/test_revoked_signer_words.py``'s rig: nothing of the operator's is read.
"""
from __future__ import annotations

import re
import shlex
from datetime import timedelta
from types import SimpleNamespace

import pytest

import levain.authority as auth
from levain import doctor as D
from levain import seat_lease as sl
from levain import seat_push as sp
from levain.cli import build_parser, main
from levain.consent import grants as G
from levain.firing import hands_mcp_login as hml
from tests.test_revoked_signer_words import _now, voice, world  # noqa: F401 - the shared rig fixtures

BORN = auth.STOPPED_BORN
LEASE, EGRESS, ENTRY = "sl-aaaaaaaaaaaa", "se-aaaaaaaaaaaa", "hm-000000000001"


def _parses(cmd: str) -> list[str]:
    """``cmd`` (a ``levain ...`` line with ``<key>`` / ``<id>`` placeholders) parses with every argument its parser
    requires: argparse exits on a missing one. Returns the argv."""
    argv = shlex.split(cmd.replace("<key>", "/k").replace("<id>", "x"))
    assert argv[0] == "levain", cmd
    try:
        build_parser().parse_args(argv[1:])
    except SystemExit as exc:            # pragma: no cover - the failure is the assertion
        raise AssertionError(f"{cmd!r} does not parse: argparse exited {exc.code}") from None
    return argv


def _quoted(text: str) -> list[str]:
    return re.findall(r"`([^`]+)`", text)


def _refused(make) -> str:
    with pytest.raises(Exception) as exc:
        make()
    return str(exc.value)


def _stopped_lease(world) -> None:
    world.lease(LEASE)
    world.revoke_key("a", by="b")


def _lease_again(world, by: str = "b"):
    return lambda: world.lease("sl-bbbbbbbbbbbb", by=by)


# --- F1 (a): the one-live refusals ---------------------------------------------------------------------------------

def test_a_live_lease_still_blocks_in_the_old_words(world) -> None:
    """Control: a lease whose signer is active blocks a second one, and the refusal says it is live."""
    world.lease(LEASE)
    said = _refused(_lease_again(world))
    assert f"one live seat-root lease at a time: {LEASE!r} is already live; revoke it first" in said
    assert "stopped" not in said


def test_a_stopped_lease_blocks_and_the_refusal_says_stopped_and_the_verb(world) -> None:
    _stopped_lease(world)
    said = _refused(_lease_again(world))
    assert "is already live" not in said
    assert f"{LEASE!r} was stopped ({BORN}) and answers nothing, but it still counts here until it is revoked" in said
    (cmd,) = _quoted(said)
    argv = _parses(cmd)
    assert argv[1:4] == ["seat", "lease", "revoke"] and LEASE in argv
    assert world.state().policy.grants.keys() == {LEASE}                       # the refusal stays a refusal


def test_a_stopped_egress_grant_blocks_and_the_refusal_says_stopped_and_the_verb(world) -> None:
    world.egress(EGRESS)
    world.revoke_key("a", by="b")
    said = _refused(lambda: world.egress("se-bbbbbbbbbbbb", by="b"))
    assert "is already live" not in said
    assert f"{EGRESS!r} was stopped ({BORN}) and answers nothing, but it still counts here until it is revoked" in said
    (cmd,) = _quoted(said)
    argv = _parses(cmd)
    assert argv[1:4] == ["seat", "egress", "revoke"] and EGRESS in argv


def test_a_stopped_hands_entry_blocks_its_name_and_the_refusal_says_stopped_and_the_verb(world) -> None:
    world.entry(ENTRY, "files")
    world.revoke_key("a", by="b")
    said = _refused(lambda: world.entry("hm-000000000002", "files", by="b"))
    assert "is already live" not in said
    assert f"{ENTRY!r} was stopped ({BORN}) and answers nothing, but it still counts here until it is revoked" in said
    (cmd,) = _quoted(said)
    argv = _parses(cmd)
    assert argv[1:4] == ["consent", "grant", "revoke"] and argv[-1] == ENTRY


def test_a_live_hands_entry_still_blocks_its_name_in_the_old_words(world) -> None:
    world.entry(ENTRY, "files")
    said = _refused(lambda: world.entry("hm-000000000002", "files", by="b"))
    assert f"'files' is already live as {ENTRY!r}; revoke it first" in said and "stopped" not in said


def test_the_lease_cli_refusal_carries_the_stopped_words(world, capsys, tmp_path) -> None:
    """The words reach the operator through the verb itself, not only the library."""
    _stopped_lease(world)
    code = main(["seat", "lease", "create", "--lanes-parent", str(world.lanes), "--returns-parent",
                 str(tmp_path / "returns"), "--signing-key", str(world.keys["b"][0]), "--data-root", str(world.root),
                 "--path", str(world.entity)])
    err = capsys.readouterr().err
    assert code == 1 and "was stopped" in err and "is already live" not in err


# --- F1 (b): the notice orders the way back per kind ----------------------------------------------------------------

ROWS = [("cg-A", auth.CONSENT_GRANT_KIND, "consent grant: x"),
        ("sh-A", auth.CONSENT_CONTAINED_SHELL_KIND, "contained-shell grant: x"),
        ("ek-1", auth.EGRESS_KEY_KIND, "chat key: x"),
        ("hm-1", auth.HANDS_MCP_KIND, "hands-mcp entry: x"),
        ("se-1", auth.SEAT_EGRESS_KIND, "seat-egress grant: x"),
        ("sl-1", auth.SEAT_LEASE_KIND, "seat-root lease: x"),
        ("vk-1", auth.VOICE_KEY_KIND, "voice key: x")]


def test_the_notice_says_create_for_a_kind_without_a_one_live_rule_and_revoke_first_for_the_rest() -> None:
    lines = G.stopped_notice(ROWS)
    tail = lines[1 + len(ROWS):]
    assert tail == [
        "A renew does not bring one back. To use one again:",
        "  create it again with a key you still hold: cg-A, sh-A",
        "  revoke it, then create it again with a key you still hold: ek-1, se-1, sl-1, vk-1",
        "  revoke it to clear it; no levain verb registers a hands entry yet: hm-1",
        "To revoke one: levain consent grant revoke --signing-key <key> -- <id>",
        "A chat key revokes with: levain chat key revoke --signing-key <key> <id>"]
    for line in tail[-2:]:
        _parses(line.split(": ", 1)[1])


@pytest.mark.parametrize("kind,way", [
    (auth.CONSENT_GRANT_KIND, "create it again with a key you still hold"),
    (auth.CONSENT_MCP_GRANT_KIND, "create it again with a key you still hold"),
    (auth.CONSENT_CONTAINED_SHELL_KIND, "create it again with a key you still hold"),
    (auth.SEAT_LEASE_KIND, "revoke it, then create it again with a key you still hold"),
    (auth.SEAT_EGRESS_KIND, "revoke it, then create it again with a key you still hold"),
    (auth.EGRESS_KEY_KIND, "revoke it, then create it again with a key you still hold"),
    (auth.VOICE_KEY_KIND, "revoke it, then create it again with a key you still hold"),
    (auth.HANDS_MCP_KIND, "revoke it to clear it; no levain verb registers a hands entry yet")])
def test_each_kind_alone_gets_its_own_order(kind, way) -> None:
    lines = G.stopped_notice([("g-1", kind, "words")])
    assert lines[2:4] == ["A renew does not bring it back. To use it again:", f"  {way}: g-1"]


def test_following_the_notice_in_order_lets_the_create_land(world, capsys, tmp_path) -> None:
    """End to end through the CLI: the lease, the egress grant and a hands entry, all A's; A is revoked; the notice's
    own steps are run in its order (the revoke line it gives, with B, then the create), and each create lands."""
    world.lease(LEASE)
    world.egress(EGRESS)
    world.entry(ENTRY, "files")
    assert main(["cockpit", "revoke", world.fps["a"], "--signing-key", str(world.keys["b"][0]),
                 "--data-root", str(world.root)]) == 0
    lines = capsys.readouterr().out.splitlines()
    first = next(line for line in lines if line.startswith("  revoke it, then create it again"))
    assert first.endswith(f": {EGRESS}, {LEASE}")
    hands = next(line for line in lines if line.startswith("  revoke it to clear it"))
    assert hands.endswith(f": {ENTRY}")
    revoke = next(line for line in lines if line.startswith("To revoke one: ")).split(": ", 1)[1]
    with pytest.raises(Exception):                                        # before step one, the create is refused
        world.lease("sl-bbbbbbbbbbbb", by="b")
    for gid in (LEASE, EGRESS, ENTRY):                                    # step one, as the notice spells it
        argv = shlex.split(revoke.replace("<key>", str(world.keys["b"][0])).replace("<id>", gid))
        assert argv[1:4] == ["consent", "grant", "revoke"]
        assert main(argv[1:4] + ["--data-root", str(world.root)] + argv[4:]) == 0, gid
    capsys.readouterr()
    world.lease("sl-bbbbbbbbbbbb", by="b")                                # step two: each create lands
    world.egress("se-bbbbbbbbbbbb", by="b")
    world.entry("hm-000000000002", "files", by="b")
    grants = world.state().policy.grants
    assert {"sl-bbbbbbbbbbbb", "se-bbbbbbbbbbbb", "hm-000000000002"} <= grants.keys()
    assert not {LEASE, EGRESS, ENTRY} & grants.keys()


# --- the same order on the other surfaces -------------------------------------------------------------------------

def test_the_list_line_says_revoke_first_for_a_one_live_kind_and_create_for_the_rest() -> None:
    assert G.stopped_line(BORN) == f"stopped: {BORN} (answers nothing; create it again to use it)"
    assert G.stopped_line(BORN, auth.CONSENT_GRANT_KIND) == f"stopped: {BORN} (answers nothing; create it again to use it)"
    for kind in (auth.SEAT_LEASE_KIND, auth.SEAT_EGRESS_KIND, auth.EGRESS_KEY_KIND, auth.VOICE_KEY_KIND):
        assert G.stopped_line(BORN, kind) == f"stopped: {BORN} (answers nothing; revoke it, then create it again to use it)"
    assert G.stopped_line(BORN, auth.HANDS_MCP_KIND) == (
        f"stopped: {BORN} (answers nothing; revoke it to clear it; no levain verb registers a hands entry yet)")


def test_seat_lease_list_says_revoke_first(world, capsys) -> None:
    _stopped_lease(world)
    assert main(["seat", "lease", "list", "--data-root", str(world.root)]) == 0
    assert "(answers nothing; revoke it, then create it again to use it)" in capsys.readouterr().out


def test_seat_egress_list_says_revoke_first(world, capsys) -> None:
    world.egress(EGRESS)
    world.revoke_key("a", by="b")
    assert main(["seat", "egress", "list", "--data-root", str(world.root)]) == 0
    assert "(answers nothing; revoke it, then create it again to use it)" in capsys.readouterr().out


def test_chat_key_list_says_revoke_first(world, capsys) -> None:
    world.chat_key("ek-000000000001")
    world.revoke_key("a", by="b")
    assert main(["chat", "key", "list", "--data-root", str(world.root)]) == 0
    assert "(answers nothing; revoke it, then create it again to use it)" in capsys.readouterr().out


def test_voice_key_list_says_revoke_first(voice, capsys) -> None:
    voice.add("b")
    voice.revoke_key("b")
    assert main(["voice", "key", "list", "--data-root", str(voice.root)]) == 0
    assert "(answers nothing; revoke it, then create it again to use it)" in capsys.readouterr().out


def test_consent_grant_list_keeps_create_for_a_consent_grant(world, capsys) -> None:
    """Control: the consent list holds the kinds with no one-live rule (and inert ones); its words do not change."""
    world.consent("cg-A")
    world.lease(LEASE)
    world.revoke_key("a", by="b")
    assert main(["consent", "grant", "list", "--data-root", str(world.root)]) == 0
    lines = {line.split()[0].strip("'"): line for line in capsys.readouterr().out.splitlines()}
    assert "(answers nothing; create it again to use it)" in lines["cg-A"] and LEASE not in lines


def test_the_renew_refusal_says_revoke_first_for_a_lease_and_create_for_a_consent_grant(world) -> None:
    world.lease(LEASE)
    world.consent("cg-A")
    world.revoke_key("a", by="b")
    lease = _refused(lambda: world.renew(LEASE, "b", days=14))
    assert "a renew does not bring it back: revoke it, then create it again with a key you hold" in lease
    consent = _refused(lambda: world.renew("cg-A", "b"))
    assert "a renew does not bring it back: create it again with a key you hold" in consent


def test_the_doctor_row_says_revoke_first_when_a_one_live_kind_is_stopped(world) -> None:
    world.consent("cg-A")
    world.revoke_key("a", by="b")
    (row,) = D._check_standing_grants(world.entity)
    assert row.hint == "Create it again with a key you hold, or revoke it to clear the list."     # control: unchanged
    world.lease(LEASE, by="c")
    world.revoke_key("c", by="b")
    (row,) = D._check_standing_grants(world.entity)
    assert row.hint == ("Revoke them to clear the list (levain consent grant revoke --signing-key <key> -- <id>). "
                        "cg-A: create it again with a key you still hold. "
                        f"{LEASE}: revoke it, then create it again with a key you still hold.")
    _parses(_doctor_cmd(row.hint))


def test_the_doctor_hint_never_tells_a_hands_entry_to_be_created_again(world) -> None:
    """L3 r3 (three lineages): the hint said every one-live kind, a hands entry among them, "must be revoked before it is
    created again", but no verb creates a hands entry. Each kind now gets its own way back, the revoke notice's words, and a
    hands entry is told it can only be cleared. Red with the one-sentence hint."""
    world.consent("cg-A")
    world.lease(LEASE)
    world.entry(ENTRY, "files")
    world.revoke_key("a", by="b")
    (row,) = D._check_standing_grants(world.entity)
    assert row.hint == ("Revoke them to clear the list (levain consent grant revoke --signing-key <key> -- <id>). "
                        "cg-A: create it again with a key you still hold. "
                        f"{LEASE}: revoke it, then create it again with a key you still hold. "
                        f"{ENTRY}: revoke it to clear it; no levain verb registers a hands entry yet.")
    assert "created again" not in row.hint and "hands entry must" not in row.hint
    _parses(_doctor_cmd(row.hint))


def test_the_doctor_hint_says_the_same_ways_as_the_revoke_notice(world) -> None:
    """One source for both: each way the hint gives is a way the notice lists, for the same ids."""
    world.consent("cg-A")
    world.lease(LEASE)
    world.egress(EGRESS)
    world.entry(ENTRY, "files")
    world.revoke_key("a", by="b")
    (row,) = D._check_standing_grants(world.entity)
    notice = G.stopped_notice(sorted([(LEASE, auth.SEAT_LEASE_KIND, "x"), (EGRESS, auth.SEAT_EGRESS_KIND, "x"),
                                      (ENTRY, auth.HANDS_MCP_KIND, "x"), ("cg-A", auth.CONSENT_GRANT_KIND, "x")]))
    start = next(i for i, line in enumerate(notice) if line.startswith("A renew does not bring"))
    end = next(i for i, line in enumerate(notice) if line.startswith("To revoke one: "))
    ways = [line.strip().rsplit(": ", 1) for line in notice[start + 1:end]]
    assert len(ways) == 3
    said = row.hint.split("). ", 1)[1]
    assert said == " ".join(f"{ids}: {way}." for way, ids in ways), (said, ways)


def _doctor_cmd(hint: str) -> str:
    return re.search(r"\((levain [^)]+)\)", hint).group(1)


# --- F3: a stopped hands entry is not a healthy row, and its words name a verb that exists ---------------------------

def _hands_rows(world, monkeypatch, now=None):
    monkeypatch.setattr(hml.hl, "load_hands_identity", lambda entity: object())
    monkeypatch.setattr(hml, "read_record", lambda entity: {})
    monkeypatch.setattr(hml, "vendor_servers", lambda hands, entity, sweep=False: {})
    if now is not None:
        real = hml.load_registry
        monkeypatch.setattr(hml, "load_registry", lambda root, _now=None: real(root, now))
    return hml.status_rows(world.entity, world.root)


def test_a_stopped_hands_entry_is_not_ok_and_names_the_revoke_verb(world, monkeypatch) -> None:
    world.entry(ENTRY, "files")
    world.revoke_key("a", by="b")
    rows, code = _hands_rows(world, monkeypatch)
    (row,) = [r for r in rows if r[1].startswith("hands mcp entry")]
    assert row[0] is False and code == 1
    assert row[1] == (f"hands mcp entry {ENTRY}: stopped: {BORN} (answers nothing; revoke it to clear it; no levain "
                      f"verb registers a hands entry yet): `levain consent grant revoke --signing-key <key> -- {ENTRY}`")
    assert "register it again" not in row[1]
    (cmd,) = _quoted(row[1])
    assert _parses(cmd)[-1] == ENTRY


def test_an_expired_hands_entry_keeps_its_row(world, monkeypatch) -> None:
    """Control: only the stop is a failing row; an expired entry is reported as before."""
    world.entry(ENTRY, "files")
    rows, _code = _hands_rows(world, monkeypatch, now=_now() + timedelta(days=3))
    (row,) = [r for r in rows if r[1].startswith("hands mcp entry")]
    assert row == (True, f"hands mcp entry {ENTRY}: not live (expired)")


def test_the_sign_in_refusal_for_a_stopped_entry_names_the_revoke_verb(world, tmp_path) -> None:
    world.entry(ENTRY, "files")
    world.revoke_key("a", by="b")
    with pytest.raises(hml.HandsMcpRefused) as exc:
        hml.entry_for_signin(tmp_path / "entity", world.root, "files")
    said = str(exc.value)
    assert said == (f"'files' is registered but stopped: {BORN}; revoke it to clear it "
                    f"(`levain consent grant revoke --signing-key <key> -- {ENTRY}`); no levain verb registers a hands "
                    "entry yet. Nothing was changed")
    (cmd,) = _quoted(said)
    assert _parses(cmd)[-1] == ENTRY


# --- L3 r3: the doctor's tier for a stopped hands entry, and its hint -----------------------------------------------

def _doctor_hands(world, monkeypatch, record=None):
    """The doctor's hands-mcp rows over ``world``'s log: a stand-in hands account whose config holds nothing and whose
    credential file reads as its own, 0600, so only the registry decides the rows."""
    hands = SimpleNamespace(user="handsuser")
    monkeypatch.setattr(hml.hl, "load_hands_identity", lambda entity: hands)
    monkeypatch.setattr(hml, "read_record", record or (lambda entity: {}))
    monkeypatch.setattr(hml, "vendor_servers", lambda hands, entity, sweep=False: {})
    monkeypatch.setattr(hml.hl, "credential_state", lambda *a, **k: "handsuser 600")
    return D._check_hands_mcp(world.entity)


def test_a_stopped_hands_entry_warns_in_the_doctor_and_still_fails_the_list(world, monkeypatch) -> None:
    """L3 r3 (three lineages): the operator stopped the entry by revoking a key, and the standing-grants row beside it
    already warns of that stop, so the doctor's hands row WARNS (ok, so it adds nothing to the doctor's failures and
    exit code) while `levain hands mcp list` keeps exit 1. Its hint is the revoke that clears it, not "`levain hands mcp
    list` shows the same read" (that list shows this same line). Red with doctor_rows copying status_rows' ok."""
    world.entry(ENTRY, "files")
    world.revoke_key("a", by="b")
    rows = _doctor_hands(world, monkeypatch)
    (row,) = [r for r in rows if r.detail.startswith("hands mcp entry")]
    assert (row.name, row.ok, row.warn) == ("hands mcp", True, True)
    assert row.detail.startswith(f"hands mcp entry {ENTRY}: stopped: {BORN}")
    assert row.hint == f"`levain consent grant revoke --signing-key <key> -- {ENTRY}`"
    (cmd,) = _quoted(row.hint)
    assert _parses(cmd)[-1] == ENTRY
    assert [r for r in rows if not r.ok] == []                       # nothing here fails the doctor run
    (grants,) = D._check_standing_grants(world.entity)                # the row beside it: the same stop, the same tier
    assert (grants.ok, grants.warn) == (True, True) and ENTRY in grants.detail
    _list_rows, code = hml.status_rows(world.entity, world.root)      # the focused list still fails on it
    assert code == 1


def test_a_hands_row_that_is_not_a_stop_keeps_its_failure_and_its_hint(world, monkeypatch) -> None:
    """Control: only the stopped entry is translated; a read that fails is still a FAIL with the list as its hint."""
    world.entry(ENTRY, "files")
    world.revoke_key("a", by="b")

    def broken(entity):
        raise hml.HandsMcpError("probe fault")
    rows = _doctor_hands(world, monkeypatch, record=broken)
    (bad,) = [r for r in rows if r.detail.startswith("hands mcp record")]
    assert (bad.ok, bad.warn, bad.hint) == (False, False, "`levain hands mcp list` shows the same read")
    (stopped,) = [r for r in rows if r.detail.startswith("hands mcp entry")]
    assert (stopped.ok, stopped.warn) == (True, True)


def test_a_live_hands_entry_gives_no_warn_row(world, monkeypatch) -> None:
    """Control: an entry whose signers are active is no stop and draws no entry row."""
    world.entry(ENTRY, "files")
    rows = _doctor_hands(world, monkeypatch)
    assert [r for r in rows if r.detail.startswith("hands mcp entry")] == []
    assert all(r.ok and not r.warn for r in rows), rows


# --- L3 r3: a one-live refusal with one stopped and one live blocker ------------------------------------------------

def _two_live_leases(world, monkeypatch) -> None:
    """A log holding two live leases (the state a log signed before the one-live rule can hold): A's, then B's, written
    with the append rule lifted for the second; then A is revoked, so the first is stopped and the second is live."""
    world.lease(LEASE)
    with monkeypatch.context() as m:
        m.setattr(auth, "_seat_lease_unique_check", lambda *a, **k: None)
        world.lease("sl-bbbbbbbbbbbb", by="b")
    world.revoke_key("a", by="b")


def test_a_refusal_with_a_stopped_and_a_live_blocker_says_each_as_it_is(world, monkeypatch) -> None:
    """L3 r3: with one stopped and one live blocker the refusal called both "already live". Each is now said as it is,
    the stopped one with its revoke verb. Red with the all-or-nothing branch."""
    _two_live_leases(world, monkeypatch)
    said = _refused(lambda: world.lease("sl-cccccccccccc", by="b"))
    assert f"{LEASE!r} was stopped ({BORN}) and answers nothing, but it still counts here until it is revoked" in said
    assert "'sl-bbbbbbbbbbbb' is already live; revoke it first" in said
    assert f"{LEASE!r} is already live" not in said and f"{LEASE!r}, " not in said
    (cmd,) = _quoted(said)
    argv = _parses(cmd)
    assert argv[1:4] == ["seat", "lease", "revoke"] and argv[-1] == LEASE


def test_a_refusal_with_two_live_blockers_names_both_live(world, monkeypatch) -> None:
    """Control: two live blockers, none stopped, are both named live (in the plural)."""
    world.lease(LEASE)
    with monkeypatch.context() as m:
        m.setattr(auth, "_seat_lease_unique_check", lambda *a, **k: None)
        world.lease("sl-bbbbbbbbbbbb", by="b")
    said = _refused(lambda: world.lease("sl-cccccccccccc", by="b"))
    assert f"{LEASE!r}, 'sl-bbbbbbbbbbbb' are already live; revoke them first" in said and "stopped" not in said


# --- L3 r3: the cockpit page's way back is the server's, per kind ---------------------------------------------------

def test_the_page_rows_carry_the_way_back_the_terminal_lists_print(world, monkeypatch) -> None:
    """Every kind the cockpit page draws as a row carries ``way_back``, the very clause its terminal list line prints
    (``stopped_line``), so the page has no second copy to disagree. Red with the ``way_back`` field left off the rows."""
    world.consent("cg-A")
    world.shell("sh-A")
    world.lease(LEASE)
    world.egress(EGRESS)
    world.inert("pg-0123456789ab", monkeypatch)
    world.consent("cg-B", by="b")                                    # control: B's grant stays live
    world.revoke_key("a", by="b")
    rows = {r["id"]: r for r in G.grant_view(world.qdir, now=_now())["grants"]}
    kinds = {"cg-A": auth.CONSENT_GRANT_KIND, "sh-A": auth.CONSENT_CONTAINED_SHELL_KIND, LEASE: auth.SEAT_LEASE_KIND,
             EGRESS: auth.SEAT_EGRESS_KIND, "pg-0123456789ab": "push"}
    for gid, kind in kinds.items():
        assert rows[gid]["state"] == "stopped"
        assert G.stopped_line(BORN, kind) == f"stopped: {BORN} (answers nothing; {rows[gid]['way_back']})", gid
    assert rows[LEASE]["way_back"] == rows[EGRESS]["way_back"] == "revoke it, then create it again to use it"
    assert rows["cg-A"]["way_back"] == "create it again to use it"
    assert rows["cg-B"]["way_back"] == ""                            # a live row has none


def test_a_stopped_voice_key_row_carries_the_terminal_words(voice) -> None:
    voice.add("b")
    voice.revoke_key("b")
    (row,) = G.grant_view(voice.qdir, now=_now())["grants"]
    assert (row["kind"], row["state"]) == (auth.VOICE_KEY_KIND, "stopped")
    assert G.stopped_line(BORN, auth.VOICE_KEY_KIND) == f"stopped: {BORN} (answers nothing; {row['way_back']})"


def test_the_voice_words_are_true_a_plain_add_is_refused_and_revoke_then_add_lands(voice) -> None:
    """The fact the page's voice words rest on: while a stopped voice key is held a plain add is refused (so the page's
    old "Set it again" sent the operator into a refusal), and the terminal's way (revoke it, then add one) lands. The
    one-step ``--replace`` lands too (``tests/test_revoked_signer_voice.py``)."""
    from levain.voice.custody import VoiceKeyRefused

    old, _ = voice.add("b")
    voice.revoke_key("b")
    with pytest.raises(VoiceKeyRefused, match="already has a voice key"):
        voice.add("a", value="sk-OTHERVALUE-0123456789", source="levain-voice-openai-again")
    from levain.voice import custody
    custody.revoke_key(voice.root, old["id"], signer_fp=voice.keys["a"][1], sign=voice.sign_as("a"), clock=_now)
    new, _ = voice.add("a", value="sk-OTHERVALUE-0123456789", source="levain-voice-openai-again2")
    assert new["id"] != old["id"] and voice.read() == "sk-OTHERVALUE-0123456789"
