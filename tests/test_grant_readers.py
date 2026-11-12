"""Levain board row 190, T20 (HEAD RULING R-D): every reader of ``policy.grants`` under ``levain/`` is accounted for.

Revoking a signing key stops the grants it signed *at use* (``levain.authority.grants_in_force``), because the policy
still holds a stopped grant: the log replays the same, and the grant can still be listed and revoked. The price is that
a consumer written next year which reads ``policy.grants`` raw keeps answering from a grant a revoked key signed, and
nothing in that consumer looks wrong. This test is the answer to that: it finds every attribute read ``<x>.policy.grants``
(or ``policy.grants`` on a bare ``policy``) under ``levain/``, with the function that makes it, and compares them with the
table below. A reader the table does not name fails with "a USE path must read grants_in_force".

Each row says what the reader is, and why a raw read is right there:

* ``replay``  the log's own replay and its helpers: the replay must see every grant the log holds.
* ``rule``    an append rule, or an identity/presence helper the live checks compare beside ``grants_in_force``: it counts
              what an older levain on the same log would count, or names a grant without letting it answer.
* ``write``   a setup, replace, renew or revoke: a stopped grant is still the record to replace or revoke.
* ``display`` words, a listing or a doctor row: it shows a stopped grant as stopped, it never answers from one.
* ``use``     a USE path that reads raw ON PURPOSE and judges the signers itself, the rule ``grants_in_force`` generalises
              (the reason names where). A row here is the weakest claim in the table: the reason is what to re-check.

A row counts reads (``n``), so a new raw read inside an already-listed function is noticed too, and a row whose function
no longer reads raw (or moved) fails as stale. Keyed by file and function name, never by line number.

Known edge, closed rather than built against: this finds the attribute spelling ``policy.grants``. A read through
``getattr(state.policy, "grants")`` or through ``policy.as_dict()["grants"]`` is not found; the ordinary spelling is the
one every reader in the table uses.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "levain"

# "<path under the repo>::<function>" -> (n reads, class, reason)
READERS: dict[str, tuple[int, str, str]] = {
    # --- the replay and the authority helpers ---------------------------------------------------------------------
    "levain/authority.py::AuthoritySnapshot.grant_link":
        (1, "replay", "the (seq, hash) of the link that made a grant: a lookup by id, which decides nothing"),
    "levain/authority.py::egress_key_incarnation":
        (1, "rule", "a record's identity (born seq and meaning); a route carries it and the live check compares it, "
                    "beside grants_in_force"),
    "levain/authority.py::graduation_opt_in":
        (1, "replay", "read by the replay's passkey rule (_passkey_act), which judges a passkey's new link against the "
                      "opt-in; stopping it would change what an existing log replays to (design 3: OUT, routed)"),
    "levain/authority.py::graduation_incarnation":
        (1, "rule", "a policy's identity, the shape of egress_key_incarnation"),
    "levain/authority.py::graduation_links":
        (1, "rule", "the signers of a graduation policy's born and current links; graduation_policies judges them "
                    "active (see its row)"),
    "levain/authority.py::grant_signers":
        (1, "rule", "the helper that names a grant's signers: presence of the grant in the policy"),
    "levain/authority.py::grant_stop_reason":
        (1, "rule", "the stop rule itself: it must see a grant the policy holds to say why it is stopped"),
    "levain/authority.py::grants_in_force":
        (1, "rule", "the accessor: the raw policy minus every stopped grant"),
    "levain/authority.py::grants_stopped_by":
        (1, "display", "names the grants a key's revoke stopped, for the revoke verb"),
    "levain/authority.py::_egress_key_append_check":
        (2, "rule", "the one-live append rule: counts what an older levain on the same log would count (R-C)"),
    "levain/authority.py::agent_links":
        (1, "rule", "presence, not liveness: what a revoke_agent removes; its callers peek each link "
                    "(AgentAuthority.peek) before arming"),
    "levain/authority.py::agent_link_arms":
        (1, "rule", "the time half of an agent link's liveness; its docstring leaves the signers to the reader, "
                    "which is AgentAuthority.peek"),
    "levain/authority.py::agent_link_unrevoked":
        (1, "rule", "the revoke half of an agent link's liveness; the signers are the reader's (peek, "
                    "verify_point_of_use)"),
    "levain/authority.py::agent_link_incarnation":
        (1, "rule", "an agent link's identity (born seq and meaning), the shape of egress_key_incarnation"),
    "levain/authority.py::_hands_mcp_unique_check":
        (3, "rule", "the one-live append rule: counts the raw policy, as the other one-live rules do (R-C)"),
    "levain/authority.py::_unique_live_check":
        (3, "rule", "the one-live append rule for the seat kinds: counts the raw policy (R-C)"),
    "levain/authority.py::_passkey_act":
        (2, "replay", "the replay's rule for what a passkey link may do: judged against the policy the log holds"),
    "levain/authority.py::_link_format":
        (1, "replay", "the format a link needs, from the policy the replay holds"),
    "levain/authority.py::_step":
        (4, "replay", "the replay itself: grant_seqs and grant_born are computed from every grant the log holds"),
    "levain/authority.py::_append_only_checks":
        (2, "rule", "the append rules: a renew of a stopped grant is refused here, which needs the raw policy"),
    # --- agent links: the use paths that judge the signers themselves ---------------------------------------------
    "levain/agent_authority.py::AgentAuthority.peek":
        (1, "use", "after agent_link_arms it requires the signers of the born AND current links to be active keys "
                   "(the two-signer rule grants_in_force generalises)"),
    "levain/agents/controller.py::_pick_link":
        (2, "use", "builds the candidate ids by spec and binding; a candidate arms only when AgentAuthority.peek, which "
                   "checks the signers, says live"),
    "levain/agents/controller.py::_admit":
        (1, "use", "reads the terms of the link _pick_link returned, which peek accepted"),
    "levain/agents/session_client.py::_standing_link":
        (1, "use", "the client's best-effort proof of a link; its comment hands the signers to the server, which "
                   "re-judges them (dispatch_run.verify_point_of_use). NOT a gate on its own"),
    "levain/agents/tick_driver.py::binding_of_factory.binding_of":
        (1, "use", "candidates only; a binding is returned only for a link AgentAuthority.peek says is live"),
    "levain/slot/services/dispatch_run.py::verify_point_of_use":
        (1, "use", "re-judges the signers itself: every proof row's signer must be an active key (_row_is_the_ledgers), "
                   "and on a re-judge the link's current signer too"),
    "levain/graduation_policy.py::graduation_policies":
        (1, "use", "a policy whose born or current signer the log has revoked is left out (_graduation_policy, "
                   "RULINGS_S1 item 2): the precedent grants_in_force generalises"),
    "levain/agents/signing.py::renew_agent_link.check":
        (1, "write", "refuses a renew of an id that is no agent link; the append rule refuses a renew of a stopped one"),
    "levain/cockpit/agents_panel.py::_links":
        (1, "display", "the panel's candidate ids; _judge_read decides armed or not through AgentAuthority.peek"),
    "levain/cockpit/agents_panel.py::_judge_read":
        (2, "display", "the panel's expiry words; whether a link is armed is peek's answer"),
    "levain/cockpit/verbs.py::_renew_target_holds":
        (1, "write", "the renew verb checks the link covers the agent's spec; a renew of a stopped link is refused at "
                     "append"),
    # --- setup, replace, renew, revoke ----------------------------------------------------------------------------
    "levain/chat_egress/key_setup.py::setup_key":
        (3, "write", "the one-live count and the existing-id test of a setup or --replace stay raw: a stopped record is "
                     "still the one a replace supersedes"),
    "levain/chat_egress/key_setup.py::revoke_key":
        (1, "write", "a stopped record must still be revocable"),
    "levain/chat_egress/key_setup.py::list_keys":
        (1, "display", "lists every setup record the policy holds, a stopped one marked `stopped` with its reason"),
    "levain/chat_egress/renew.py::renew_due":
        (1, "display", "names a launch's keyed records near expiry so a renew can be offered; attaches no credential, and "
                       "a renew of a stopped record is refused at append"),
    "levain/cli.py::_signed_append":
        (1, "write", "refuses a graduation op naming an id that is no graduation policy; signing, not answering"),
    "levain/consent/grants.py::create_grant":
        (1, "write", "returns the grant the create just made"),
    "levain/consent/grants.py::_revocable":
        (1, "write", "a stopped grant must still be revocable"),
    "levain/seat_lease.py::create_lease":
        (1, "write", "returns the lease the create just made"),
    "levain/seat_push.py::create_egress":
        (1, "write", "returns the grant the create just made"),
    "levain/seat_push.py::renew_egress":
        (1, "write", "the kind check of a renew; a renew of a stopped grant is refused at append"),
    "levain/seat_push.py::revoke_egress":
        (1, "write", "a stopped grant must still be revocable"),
    "levain/voice/custody.py::add_key":
        (4, "write", "the existing-id test, the replace scan and the item-collision scan stay raw: a stopped record is "
                     "still the one a replace supersedes"),
    "levain/voice/custody.py::revoke_key":
        (1, "write", "a stopped key must still be revocable"),
    # --- display: a stopped grant shown as stopped ----------------------------------------------------------------
    "levain/cli.py::_cmd_seat":
        (1, "display", "`seat lease list` names the stopped lease (seat_lease.stopped_lease) when no live one reads"),
    "levain/cli.py::_cmd_seat_egress":
        (1, "display", "`seat egress list` names the stopped grant (seat_push.stopped_egress) when no live one reads"),
    "levain/cli.py::_cmd_binding_policy_list":
        (1, "display", "lists graduation policies and who signed them; whether one covers anything is "
                       "graduation_policies' answer"),
    "levain/consent/grants.py::grant_view":
        (7, "display", "the page and the phone list every grant the policy holds, each with its state "
                       "(stopped wins over expired and live) and its revoke line"),
    "levain/consent/grants.py::_split":
        (1, "display", "the listing partition; each grant carries why it is stopped"),
    "levain/consent/grants.py::stopped_by_key":
        (1, "display", "the revoke verb's words: the grants a key signed, from a fresh read"),
    "levain/consent/queue.py::ConsentQueue.answer_by_grant":
        (1, "display", "an early out when the policy holds no grant at all; the matcher it falls into reads "
                       "grants_in_force, which decides"),
    "levain/consent/queue.py::ConsentQueue.answer_by_contained_shell_grant":
        (1, "display", "an early out when the policy holds no grant at all; the matcher it falls into reads "
                       "grants_in_force, which decides"),
    "levain/cockpit/voice.py::_stopped_voice_keys":
        (1, "display", "the voice readout says `stopped` where it would say `no key`; presence words only"),
    "levain/doctor.py::_api_key_links":
        (1, "display", "doctor's budget rows; a link is listed only when grant_stop_reason is None"),
    "levain/doctor.py::_check_standing_grants":
        (1, "display", "the doctor row that names the stopped grants"),
    "levain/firing/hands_mcp_login.py::load_registry":
        (1, "display", "names the entries grants_in_force left out, as skipped with the reason `signer`"),
    "levain/firing/hands_mcp_login.py::_grants_named":
        (1, "display", "every entry of a name, live or not, so a sign-in refusal can say why"),
    "levain/seat_lease.py::stopped_lease":
        (2, "display", "names the unexpired lease live_lease left out, to say why a launch is refused"),
    "levain/seat_push.py::stopped_egress":
        (2, "display", "names the unexpired grant live_egress left out, to say why a push is refused"),
    "levain/voice/custody.py::read_key":
        (1, "display", "after the in-force read finds no record, looks the provider's record up raw only to say why "
                       "it is stopped; the key is read from the in-force record"),
    "levain/voice/custody.py::list_keys":
        (1, "display", "lists every voice key record, stopped ones marked"),
}


def _attribute_chain(node: ast.expr) -> list[str]:
    names: list[str] = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
    return names[::-1]


def raw_readers() -> dict[str, int]:
    """``"<path>::<function>"`` -> how many ``<x>.policy.grants`` (or ``policy.grants``) reads the function makes."""
    found: dict[str, int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        stack: list[str] = []

        class Finder(ast.NodeVisitor):
            def _scope(self, node: ast.AST, name: str) -> None:
                stack.append(name)
                self.generic_visit(node)
                stack.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._scope(node, node.name)

            visit_AsyncFunctionDef = visit_FunctionDef          # type: ignore[assignment]

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self._scope(node, node.name)

            def visit_Attribute(self, node: ast.Attribute) -> None:
                chain = _attribute_chain(node)
                if len(chain) >= 2 and chain[-2:] == ["policy", "grants"]:
                    key = f"{rel}::{'.'.join(stack) or '<module>'}"
                    found[key] = found.get(key, 0) + 1
                self.generic_visit(node)

        Finder().visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return found


def test_every_policy_grants_reader_is_accounted_for() -> None:
    """A reader of ``policy.grants`` the table does not name is a USE path until someone shows it is not. Red with any
    function reverted to read raw (a USE path that was ``grants_in_force`` and is not)."""
    found = raw_readers()
    unlisted = sorted(set(found) - set(READERS))
    assert not unlisted, (
        "a USE path must read grants_in_force (levain.authority), not policy.grants: these functions read the raw policy "
        "and are not in the table of readers. If one is a write, a rule, a display or the replay, add it to READERS "
        "with the reason; if it answers or arms anything, it must read grants_in_force:\n  " + "\n  ".join(
            f"{key} ({found[key]} read{'s' if found[key] != 1 else ''})" for key in unlisted))


def test_a_listed_reader_that_reads_a_different_number_of_times_is_reclassified() -> None:
    """A new raw read inside a function the table already names is still a new reader."""
    found = raw_readers()
    moved = {key: (READERS[key][0], found[key]) for key in sorted(set(found) & set(READERS))
             if found[key] != READERS[key][0]}
    assert not moved, ("these functions read policy.grants a different number of times than the table says "
                       "(listed, now): re-read each one and update its row: " + repr(moved))


def test_the_table_names_only_functions_that_still_read_raw() -> None:
    """A row for a function that no longer reads raw (or moved) is stale: it would let a later reader of that name in."""
    found = raw_readers()
    stale = sorted(set(READERS) - set(found))
    assert not stale, "these rows name no raw reader of policy.grants any more; remove or fix them: " + repr(stale)


def test_every_row_has_a_class_and_a_reason() -> None:
    kinds = {"replay", "rule", "write", "display", "use"}
    bad = [key for key, (n, kind, why) in READERS.items()
           if kind not in kinds or not isinstance(n, int) or n < 1 or len(why.strip()) < 20]
    assert not bad, f"rows with no known class, no count or no reason: {bad}"


def _calls_of(path: str, name: str) -> int:
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"), filename=path)
    return sum(1 for node in ast.walk(tree) if isinstance(node, ast.Call)
               and (_attribute_chain(node.func) or [""])[-1] == name)


@pytest.mark.parametrize("path", ["levain/consent/queue.py", "levain/seat_lease.py", "levain/seat_push.py",
                                  "levain/chat_egress/keys.py", "levain/firing/hands_mcp_login.py",
                                  "levain/voice/custody.py", "levain/cockpit/voice.py"])
def test_the_use_paths_the_row_190_lanes_changed_still_call_the_accessor(path: str) -> None:
    """The consumers the C2/C3 lanes moved to ``grants_in_force`` still CALL it: the table above catches a reader that is
    new, this catches a lane's change undone in place (a call replaced by a raw read is also caught above, but only
    when the function is not listed). A docstring that names the function does not count. Red with a call removed."""
    assert _calls_of(path, "grants_in_force") >= 1, f"{path} no longer calls grants_in_force"
