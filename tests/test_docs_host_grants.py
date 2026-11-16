"""docs/host-grants.md names verbs, flags, refusal names, ids and numbers that the code owns. Each is derived here from
the code, so the page cannot drift from it. The cards (S4) and the cockpit (S5) are other slices: where their code is on this tree the page's claims
about them are derived from it, where it is not (this branch's base is S3) those checks skip, never pass blind."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

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
    assert "**Run step 1 again after you upgrade**" in TEXT
    assert "**Run `setup` or `repair`**" in TEXT and "Merged is not running" in TEXT
    assert "steps 1 and 2 admits nothing" in TEXT and "1. **Enrol the signer" in TEXT and "2. **Run `setup`" in TEXT
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
    assert len(cmds) == 3
    verbs = [c.split("<runtime-helper> ")[1].split()[0] for c in cmds]
    assert verbs == ["egress-grants-list", "egress-grant-revoke", "egress-grant-revoke"]
    assert cmds[1].endswith("<session> all") and cmds[2].endswith("<session> <grant id>")   # the one-id form is shown
    for v in verbs:
        assert v in RG.EGRESS_VERBS and v in RG.VERBS
        assert v not in RG.STDIN_VERBS           # a revoke and a list carry no signed message
        assert v in RG.DISPATCH_FORBIDDEN        # the operator's verbs: the dispatch account may not call them
    assert RG._EGRESS_TARGET_RE.fullmatch("all") and RG._EGRESS_TARGET_RE.fullmatch("eh-" + "0" * 24)
    assert grammar.SESSION_RE.pattern == r"s-[0-9a-f]{16}" and "`s-` and 16 hex digits" in TEXT
    assert eg._GRANT_ID.pattern == r"eh-[0-9a-f]{24}" and "`eh-` and 24 hex digits" in TEXT
    assert "**no signature**" in TEXT


def test_the_raw_verbs_are_the_owning_operators_only_and_the_list_answer_is_what_the_page_names():
    src = _src(EV)
    assert 'ctx.caller != OPERATOR or a.operator_uid != ctx.sudo_uid' in src and 'Refused("no-session", session)' in src
    assert "any other caller gets `no-session`" in TEXT
    assert ('return {"grant": e.grant, "host": e.host, "expires": eg.stamp(e.expires), "choice": e.choice}' in src
            and 'return {"store": store_id, "entries": [_entry_dict(e) for e in live]}' in src)
    assert "`store`" in TEXT and "`entries`" in TEXT and "`grant`" in TEXT and "`eh-` id to" in " ".join(TEXT.split())
    assert "if e.expires > now" in src and "an expired grant is absent" in " ".join(TEXT.split())


def test_the_setup_steps_and_what_a_rerun_must_repeat_are_the_codes():
    from levain.slot import install_helper as IH
    src = _src(IH)
    # a re-run is refused when the line's principal differs or its namespaces would not be kept
    assert "if principals != cmd.principal or had is None or not had <= set(namespaces[k]):" in src
    assert 'raise Refused("authority-signer-changed"' in src and "`authority-signer-changed`" in TEXT
    # --confirm adds a namespace to the laptop key only, --egress-host to every key in the command
    assert "if cmd.confirm and not passkey" in src.replace("(AUTHORITY_NAMESPACE, CONFIRM_NAMESPACE) if cmd.confirm and not passkey else",
                                                           "if cmd.confirm and not passkey")
    assert "for key, passkey in ((cmd.public_key, False), *((k, True) for k in cmd.passkeys)):" in src
    for flag in ("`--principal`", "`--public-key`", "`--confirm`", "`--passkey`"):
        assert flag in TEXT
    # setup and repair draw the per-slot grant directory: only the enrol is outside them
    from levain.slot import layout
    lsrc = _src(layout)
    assert '_root(host, R / "grants", 0o755)' in lsrc and 'Row(R / "grants" / f"slot{n}", "root", f"_levain_gw{n}", 0o750)' in lsrc
    assert "`setup` and `repair` create it; only step 1 is" in " ".join(TEXT.split())
    assert "neither is part of" not in TEXT


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
    # expiry is enforced in BOTH places: the tick's ending and the tunnel's own clock (the page says both, once)
    assert '"grant-expired" if entry.expires <= now else "grant-revoked"' in src       # the tick
    assert 'if (g_left := grant[0].left(grant[1])) <= 0:\n                            return "grant-expired"' in src   # the tunnel
    flat = " ".join(TEXT.split())
    assert "Expiry is enforced in both places" in flat and "not on the tick" not in flat


def test_wire_header_and_log_detail_are_told_apart():
    src = _src(front)
    assert 'Refused("host-not-allowed", "off-list")' in src and 'Refused("host-not-allowed", "unattended")' in src
    flat = " ".join(TEXT.split())
    assert "On the wire the agent sees `X-Levain-Refused: host-not-allowed`" in flat
    assert "`unattended` for these sessions, `off-list` for" in flat


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
    # an EXPIRED grant still in the file is held by it: revoking it is `removed`, not `recorded` (read_current keeps it)
    stale = type("E", (), {"grant": gid, "expires": 0})()
    saved = EV.ES.read_current
    EV.ES.read_current = lambda ctx, slot, session, sd: [stale]
    try:
        a = type("A", (), {"slot": None})()
        assert EV._removal(None, a, "s-" + "0" * 16, gid, object())[1:] == ([gid], [])
        assert EV._removal(None, a, "s-" + "0" * 16, "all", object())[1:] == ([gid], [])
    finally:
        EV.ES.read_current = saved
    flat = " ".join(TEXT.split())
    assert "An id the file does not hold (never issued, already revoked, or dropped by a later rewrite)" in flat
    assert "(it expired, was pruned" not in flat and "means \"that grant was already gone\"" not in flat
    assert "Revoking such an id answers `removed`" in flat and "dropped when the next grant for the session is published" in flat
    assert "live = [e for e in ES.read_current(ctx, a.slot, session, sd) if e.expires > now]" in src   # _publish prunes, nothing else does
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
    assert "`Revoke all` revokes the grants that were in force" not in flat
    assert "`removed` proves root's session file changed. `recorded` proves root wrote the id to the ledger" in flat
    assert "Revoke while a delivery is in flight can answer `session-busy`: re-run the same command." in flat
    assert "tap again" not in flat.lower().split("## from the phone")[0]       # there is no tap on the raw root verb


def _on_tree(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def test_the_issuance_section_points_at_the_card_and_is_not_worded_as_unbuilt():
    flat = " ".join(TEXT.split())
    assert "## How a grant is issued" in TEXT and "filed as a card by the session's supervisor (S4)" in flat
    assert "planned" not in TEXT and "not built" not in TEXT
    assert "levain#66 S2" in flat and "(S3)" in flat and "(S4)" in flat and "(S5)" in flat


@pytest.mark.skipif(not _on_tree("levain.slot.egress_cards"), reason="S4's cards are not on this tree")
def test_the_issuance_steps_are_the_cards_code():
    from levain.slot import egress_cards as EC
    from levain.slot import supervisor as SUP
    assert EC.CARD_TTL_S < EC.START_CUTOFF_S                            # a card expires on its own
    assert "egress-refusals" in _src(EC) and "egress-grant" in _src(EC)
    assert "host cards are off for this session" in _src(SUP)
    assert "no cockpit is running, so no card can be filed" in _src(EC)
    flat = " ".join(TEXT.split())
    assert "No card is filed when no cockpit is running" in flat and "says once that host cards are off" in flat


@pytest.mark.skipif(not _on_tree("levain.consent.host_grants"), reason="S5a's cockpit surface is not on this tree")
def test_the_cockpit_claims_are_the_surfaces_code():
    from levain.consent import host_grants as HG
    assert HG.LIST_VERB == "egress-grants-all" and HG.REVOKE_VERB == "egress-grant-revoke"
    assert HG.ALLOWED_VERBS == (HG.LIST_VERB, HG.REVOKE_VERB)
    assert HG.RATE_LIMIT == 10 and "10 a minute per credential name" in TEXT
    assert "Untrusted HTTP fields can reach only these validated helper invocations." in " ".join(TEXT.split())
    assert "refused 403" in TEXT and "already gone" in TEXT


def test_no_cockpit_route_or_argument_name_is_in_the_page():
    assert "/cockpit/" not in TEXT and "host_grants" not in TEXT          # a route is the code's to name, not this page's
    assert "Untrusted HTTP fields can reach only these validated helper invocations." in " ".join(TEXT.split())
