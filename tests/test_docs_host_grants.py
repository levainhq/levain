"""docs/host-grants.md names verbs, flags, refusal names, ids and numbers that the code owns. Each is derived here from
the code, so the page cannot drift from it. Sentences about the phone surface are intent (the page says "planned"):
this file pins that they stay worded as intent and that no cockpit route is described as running."""
from __future__ import annotations

import re
from pathlib import Path

from levain.broker import front
from levain.slot import egress_grants as eg
from levain.slot import egress_verbs as EV
from levain.slot import grammar, runtime_grammar as RG

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "host-grants.md"
TEXT = DOC.read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _src(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def _blocks() -> list[str]:
    return re.findall(r"```\n(.*?)```", TEXT, re.S)


def test_the_design_sentences_are_in_this_page_not_somewhere_in_docs():
    assert "An allowlist names hosts, not accounts." in TEXT
    assert "**Seats are not covered.**" in TEXT and "levain seat egress" in TEXT
    assert "**Run it again after you upgrade**" in TEXT
    assert "**Run `setup` or `repair` as well**" in TEXT and "Merged is not running" in TEXT
    assert "--egress-host" in TEXT


def test_the_readme_links_the_page_from_the_boundaries_bullet():
    bullet = next(l for l in README.splitlines() if l.startswith("- **An allowlist names hosts, not accounts.**"))
    assert "[docs/host-grants.md](docs/host-grants.md)" in bullet


def test_the_enrol_command_in_the_page_parses_under_the_real_grammar():
    cmds = [l for b in _blocks() for l in b.splitlines() if "enrol-authority-signer" in l]
    assert len(cmds) == 1
    argv = cmds[0].split("sudo <install-helper> ", 1)[1].replace("'<type> <base64>'", "'ssh-ed25519 AAAAC3Nza'")
    import shlex
    cmd = grammar.parse_install_argv(shlex.split(argv))
    assert cmd.verb == "enrol-authority-signer" and cmd.egress_host is True
    assert cmd.principal == "YOUR_NAME"


def test_the_flag_is_what_creates_the_store_and_names_the_purpose():
    src = _src(__import__("levain.slot.install_helper", fromlist=["x"]))
    assert "ensure_egress_state(rs, fsync=fsync)" in src and "EGRESS_HOST_NAMESPACE" in src
    assert "egress-grant-not-ready" in _src(__import__("levain.slot.egress_ledger", fromlist=["x"]))
    assert "`egress-grant-not-ready`" in TEXT


def test_the_laptop_verbs_are_real_operator_verbs_and_their_ids_are_the_codes():
    cmds = [l for b in _blocks() for l in b.splitlines() if "<runtime-helper>" in l]
    assert len(cmds) == 2
    verbs = [c.split("<runtime-helper> ")[1].split()[0] for c in cmds]
    assert verbs == ["egress-grants-list", "egress-grant-revoke"]
    for v in verbs:
        assert v in RG.EGRESS_VERBS and v in RG.VERBS
        assert v not in RG.STDIN_VERBS           # a revoke and a list carry no signed message
        assert v in RG.DISPATCH_FORBIDDEN        # the operator's verbs: the dispatch account may not call them
    assert RG._EGRESS_TARGET_RE.fullmatch("all") and RG._EGRESS_TARGET_RE.fullmatch("eh-" + "0" * 24)
    assert grammar.SESSION_RE.pattern == r"s-[0-9a-f]{16}" and "`s-` and 16 hex digits" in TEXT
    assert eg._GRANT_ID.pattern == r"eh-[0-9a-f]{24}" and "`eh-` and 24 hex digits" in TEXT
    assert "**no signature**" in TEXT


def test_the_numbers_the_page_states_are_the_codes():
    assert front.GRANT_TICK_S == 5.0 and "five seconds" in TEXT and "about five seconds" in TEXT
    assert eg.SESSION_TTL_MAX_S == 8 * 3600 and "8 hours from issue" in TEXT
    assert eg.DAY_TTL_S == 24 * 3600 and "24 hours from issue" in TEXT
    assert eg.CHOICES == ("s", "d") and "`s`" in TEXT and "`d`" in TEXT
    assert eg.GRANTS_MAX == 16 and "at most 16 grants" in TEXT
    assert eg.PORT == 443 and "port 443" in TEXT


def test_the_kinds_that_qualify_are_the_codes_in_both_halves():
    assert front.GRANT_KINDS == EV.GRANTABLE_KINDS == ("interactive", "login")
    assert "`interactive` or `login`" in TEXT
    assert 'Refused("host-not-allowed", "unattended")' in _src(front)
    assert "`unattended`" in TEXT and "X-Levain-Refused: host-not-allowed" in TEXT
    assert "routes-egress" in TEXT and front.OFF_LIST_REMEDY.endswith("routes-egress")


def test_the_never_granted_names_and_the_routed_refusal_are_the_codes():
    for host in ("localhost", "printer.local", "db.internal", "metadata.google.internal", "x.localdomain", "a.home.arpa"):
        assert eg.never_grant(host), host
    assert not eg.never_grant("pypi.org")
    for token in ("`localhost`", "`*.local`", "`*.internal`", "`metadata.*`"):
        assert token in TEXT
    assert "egress-grant-host-routed" in _src(__import__("levain.slot.egress_verify", fromlist=["x"]))


def test_the_front_ends_tunnels_with_the_words_the_page_names():
    src = _src(front)
    for end in ("grant-revoked", "grant-expired", "grant-unchecked"):
        assert f"`{end}`" in TEXT and f'"{end}"' in src
    assert "Bytes already in a transport or kernel buffer" in TEXT and "bytes already in a transport or kernel buffer" in src


def test_revoke_all_and_an_unknown_id_behave_as_the_page_says():
    # the pure half of the verb: no I/O when there is no slot dir (nothing published, nothing granted)
    remaining, removed, unknown = EV._removal(None, None, "s-" + "0" * 16, "all", None)
    assert (remaining, removed, unknown) == ([], [], [])                      # `all` records nothing in the ledger
    gid = "eh-" + "1" * 24
    remaining, removed, unknown = EV._removal(None, None, "s-" + "0" * 16, gid, None)
    assert removed == [] and unknown == [gid]                                  # an id the file does not hold: recorded
    src = _src(EV)
    assert 'return {"removed": removed, "recorded": unknown}' in src
    assert "`recorded`" in TEXT and "`removed`" in TEXT and "`Revoke all` never touches that ledger" in TEXT
    assert "if unknown:" in src and "EL.record_revoked" in src


def test_revoke_takes_the_session_lock_as_a_try_so_busy_is_a_possible_answer():
    src = _src(EV)
    assert "with H.session_lock(ctx, session):" in src and "`session-busy`" in TEXT
    assert "held = ``session-busy``" in _src(__import__("levain.slot.runtime_helper", fromlist=["x"]))


def test_the_teardown_sentence_matches_the_starts_own_fallback():
    start = _src(__import__("levain.slot.start", fromlist=["x"]))
    assert "the next render of the slot removes them" in start
    assert "_drop_session_grants_logged(ctx, a, \"teardown\"" in start
    assert ("if that fails, they are removed at the slot's next render, and the ended session's front is gone "
            "either way") in " ".join(TEXT.split())


def test_the_revoke_sentence_is_the_pinned_one():
    flat = " ".join(TEXT.split())
    assert ("Root revoked the grant. A healthy running front closes its granted tunnels on its next check, "
            "normally within about five seconds.") in flat
    assert "Revoke all` revokes the grants that were in force" in flat


def test_the_phone_surface_is_worded_as_planned_and_names_no_route():
    section = TEXT.split("## The phone", 1)[1]
    assert section.startswith(": planned, not built here")
    assert "**planned**" in section
    assert "Untrusted HTTP fields can reach only these validated helper invocations." in section
    assert "/cockpit/" not in TEXT and "host_grants" not in TEXT          # a route or an argument is S5a's to document when it lands
    head = TEXT.split("## The phone", 1)[0]
    assert "planned" not in head.replace("planned, not built", "")        # nothing above it is hedged intent
