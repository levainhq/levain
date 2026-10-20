"""The AgentBoundary names bridge: a names file maps Levain's names onto the operator's inside the export
(:func:`levain.consent.agentboundary.load_names`, ``export(names=...)``, ``--names``). Aliases only, never facts (the
run's own framework, model, tool, versions and environment are facts); an authority name maps one to one from
Levain's own value, pinned by exactly its own field, never from or into a call Levain did not decide; renamed =
synthesized; a names file is part of the receipt's identity."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from levain import cli
from levain.consent import agentboundary as ab
from tests.test_consent_agentboundary import build_scratch, later


@pytest.fixture(autouse=True, scope="module")
def _far_future_real_clock():
    patch = pytest.MonkeyPatch()
    patch.setattr(ab, "_utcnow", lambda: datetime.now(timezone.utc) + timedelta(days=60))
    yield
    patch.undo()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    queue, ids = build_scratch(tmp_path_factory.mktemp("abn"))
    now = later(queue)
    return queue, now, ab.export(queue.directory, now=now)


def _names(rules) -> tuple[bytes, ab.Names]:
    data = json.dumps({"format": ab.NAMES_FORMAT, "rules": rules}).encode()
    return data, ab.load_names(data)


def _signer(plain) -> str:
    return next(r["approval"]["approver"]["id"] for r in plain.receipts
                if r.get("approval", {}).get("approver", {}).get("id", "").startswith("ssh-key:"))


def _leaf(receipt) -> str:
    return receipt["execution"]["result_ref"].removeprefix("levain-receipt:")


def _policy_rules(levain: str, name: str, version: str) -> list[dict]:
    """A policy map as two rules: the name pinned by the name alone, the version by the name and the version."""
    return [{"when": {"policy.name": levain}, "set": {"policy.name": name}},
            {"when": {"policy.name": levain, "policy.version": "unknown"}, "set": {"policy.version": version}}]


def _chain_holds(receipts) -> None:
    for r in receipts:
        assert ab.receipt_hash(r) == r["receipt_hash"]
    assert "prior_receipt" not in receipts[0]
    for prev, cur in zip(receipts, receipts[1:]):
        assert cur["prior_receipt"] == {"receipt_id": prev["receipt_id"], "receipt_hash": prev["receipt_hash"]}


def test_a_rename_lands_the_hash_verifies_and_the_chain_links_over_the_renamed_receipts(world):
    queue, now, plain = world
    signer = _signer(plain)
    data, names = _names(_policy_rules("levain.consent.T2", "acme.repo.merges", "7") + [
        {"when": {"approval.approver.id": signer}, "set": {"approval.approver.id": "user:eng-lead@acme"}}])
    got = ab.export(queue.directory, now=now, names=names)
    assert len(got.receipts) == len(plain.receipts)
    renamed = 0
    for before, after in zip(plain.receipts, got.receipts):
        # the names file is part of the id: the leaf's id under this file, never the unrenamed one
        assert after["receipt_id"] == str(uuid.uuid5(ab.LEVAIN_AB_NS, f"{_leaf(before)}:{ab._rules_digest(names.rules)}"))
        assert before["receipt_id"] == str(uuid.uuid5(ab.LEVAIN_AB_NS, _leaf(before)))
        assert after["execution"] == before["execution"]                         # facts untouched, leaf included
        assert after["policy"]["decision"] == before["policy"]["decision"]
        assert after["agent"] == before["agent"] and after["target"]["environment"] == before["target"]["environment"]
        if before["policy"]["name"] == "levain.consent.T2":
            assert after["policy"]["name"] == "acme.repo.merges" and after["policy"]["version"] == "7"
        else:
            assert after["policy"] == before["policy"]
        if before.get("approval", {}).get("approver", {}).get("id") == signer:
            assert after["approval"]["approver"]["id"] == "user:eng-lead@acme"
        moved = {"receipt_id", "receipt_hash", "prior_receipt", "provenance", "completeness_score", "policy",
                 "approval"}
        assert {k: v for k, v in after.items() if k not in moved} == {k: v for k, v in before.items() if k not in moved}
        if "approval" in before:
            assert {**after["approval"], "approver": {}} == {**before["approval"], "approver": {}}
        renamed += after["policy"] != before["policy"] or after.get("approval") != before.get("approval")
    assert renamed > 0
    _chain_holds(got.receipts)
    assert got.report.names_sha256 == hashlib.sha256(data).hexdigest()
    assert got.report.names_renamed == renamed
    assert plain.report.names_sha256 is None and plain.report.names_renamed == 0
    # a re-export with the same file reproduces it whole
    assert ab.export(queue.directory, now=now, names=names).receipts == got.receipts


def test_a_names_file_is_part_of_the_receipt_identity(world):
    """The same queue exported with no names, with one file and with another: three disjoint id sets, each chain
    intact. A renamed receipt never reuses an unrenamed receipt's id, and two files are two document sets."""
    queue, now, plain = world
    _, one = _names(_policy_rules("levain.consent.T2", "acme.repo.merges", "7"))
    _, two = _names(_policy_rules("levain.consent.T2", "acme.repo.writes", "8"))
    _, inert = _names([{"when": {"policy.name": "no.such.policy"}, "set": {"policy.name": "acme.never"}}])
    exports = [plain] + [ab.export(queue.directory, now=now, names=n) for n in (one, two, inert)]
    ids = [{r["receipt_id"] for r in e.receipts} for e in exports]
    for i in range(len(ids)):
        assert len(ids[i]) == len(plain.receipts)
        _chain_holds(exports[i].receipts)
        for j in range(i + 1, len(ids)):
            assert not ids[i] & ids[j], (i, j)
    # a file that renames nothing still names its own document set; the content is otherwise Levain's own
    assert exports[3].report.names_renamed == 0
    strip = {"receipt_id", "prior_receipt", "receipt_hash"}
    assert ([{k: v for k, v in r.items() if k not in strip} for r in exports[3].receipts]
            == [{k: v for k, v in r.items() if k not in strip} for r in plain.receipts])


# sha256 of [[[["policy.name","levain.consent.T2"]],[["policy.name","acme.x"]]]] (compact ASCII JSON), hashed outside Python
PINNED = "de2652f653097953deb42a667e36e20300559e58f1a12ea3a4e7b5a7850c7188"


def test_the_receipt_id_comes_from_the_rules_never_from_a_handed_sha256(world):
    """Two Names with different rules but one handed sha256 (L3 r2 codex HIGH): disjoint ids, because the id's
    names part is computed from the rules; the same rules from byte-different files give the same receipts."""
    queue, now, _ = world
    _, one = _names(_policy_rules("levain.consent.T2", "acme.repo.merges", "7"))
    _, two = _names(_policy_rules("levain.consent.T2", "acme.repo.writes", "8"))
    forged = ab.Names(rules=two.rules, sha256=one.sha256)
    a, b = (ab.export(queue.directory, now=now, names=n) for n in (one, forged))
    assert not {r["receipt_id"] for r in a.receipts} & {r["receipt_id"] for r in b.receipts}
    spaced = ab.load_names(json.dumps({"format": ab.NAMES_FORMAT,
                                       "rules": _policy_rules("levain.consent.T2", "acme.repo.merges", "7")},
                                      indent=4).encode())
    assert spaced.sha256 != one.sha256
    assert ab.export(queue.directory, now=now, names=spaced).receipts == a.receipts
    # key order inside `when` / `set` is not meaning: a re-keyed file keeps its ids; rule order is
    rekeyed = [{"set": r["set"], "when": dict(reversed(list(r["when"].items())))}
               for r in _policy_rules("levain.consent.T2", "acme.repo.merges", "7")]
    assert ab._rules_digest(_names(rekeyed)[1].rules) == ab._rules_digest(one.rules)
    # the id formula is a format: a serialisation change moves every renamed receipt_id, so it is pinned
    assert ab._rules_digest(((( ("policy.name", "levain.consent.T2"),), (("policy.name", "acme.x"),)),)) == PINNED


def test_a_forged_names_with_a_lone_surrogate_raises_export_error_or_digests(world):
    """A hand-built Names carrying a lone surrogate in `when` never escapes export as a UnicodeEncodeError."""
    queue, now, _ = world
    forged = ab.Names(rules=(((("policy.name", "levain.consent.\ud800"),), (("policy.name", "acme.x"),)),),
                      sha256="0" * 64)
    try:
        ab.export(queue.directory, now=now, names=forged)
    except ab.ExportError:
        pass


@pytest.mark.parametrize("field", ["policy.decision", "execution.status", "actor.type", "approval.approved_at",
                                   "approval.approver.role", "approval.context", "execution.result_ref",
                                   "execution.completed_at", "refusal.verdict", "issued_at", "receipt_id",
                                   "receipt_hash", "arguments_hash", "prior_receipt.receipt_hash", "provenance",
                                   "completeness_score", "policy", "nonsense.field",
                                   # the run's own facts: what it ran on and where
                                   "target.environment", "target.system", "agent.framework",
                                   "agent.framework_version", "agent.model", "agent.model_version", "tool.name",
                                   "tool.version"])
def test_a_names_file_that_sets_a_fact_is_refused_naming_the_field(field):
    data = json.dumps({"format": ab.NAMES_FORMAT, "rules": [
        {"when": {}, "set": {"actor.display_name": "fine"}}, {"when": {}, "set": {field: "x"}}]}).encode()
    with pytest.raises(ab.ExportError, match=f"rule 1: `set` writes '{field}'"):
        ab.load_names(data)


@pytest.mark.parametrize("data,match", [
    (b"{not json", "not well-formed"),
    (b"\xff\xfe", "not well-formed"),
    (b'{"format": "levain-agentboundary-names/2", "rules": []}', "format"),
    (b'{"rules": []}', "format"),
    (b'{"format": "levain-agentboundary-names/1", "rules": [], "extra": 1}', "top-level"),
    (b'{"format": "levain-agentboundary-names/1", "rules": {}}', "rules must be a list"),
    (b'{"format": "levain-agentboundary-names/1", "format": "levain-agentboundary-names/1", "rules": []}',
     "appears twice"),
    (b'{"format": "levain-agentboundary-names/1", "rules": [{"set": {"policy.name": "a"}}]}', "exactly"),
    (b'{"format": "levain-agentboundary-names/1", "rules": [{"when": {}, "set": {}}]}', "non-empty object"),
    (b'{"format": "levain-agentboundary-names/1", "rules": [{"when": {}, "set": {"policy.name": 3}}]}',
     "set policy.name must be a non-empty string"),
    (b'{"format": "levain-agentboundary-names/1", "rules": [{"when": {}, "set": {"policy.name": ""}}]}',
     "non-empty string"),
    (b'{"format": "levain-agentboundary-names/1", "rules": [{"when": {}, "set": {"policy.name": "\\ud800"}}]}',
     "lone surrogate"),
    (b'{"format": "levain-agentboundary-names/1", "rules": [{"when": {}, "set": {"target.environment": "prod"}}]}',
     "`set` writes 'target.environment'"),
    (b'{"format": "levain-agentboundary-names/1", "rules": [{"when": {"provenance.tool.name": "observed"}, '
     b'"set": {"policy.name": "a"}}]}', "which is not a name"),
    (b'{"format": "levain-agentboundary-names/1", "rules": [{"when": {"tool.name": 1}, "set": {"actor.id": "a"}}]}',
     "when tool.name must be a non-empty string"),
])
def test_a_malformed_names_file_is_refused(data, match):
    with pytest.raises(ab.ExportError, match=match):
        ab.load_names(data)


def test_first_match_wins_per_field_when_tests_levains_values_and_an_empty_when_is_the_default(world):
    queue, now, plain = world
    _, names = _names([
        {"when": {"tool.name": "Bash"}, "set": {"actor.display_name": "bash-bot"}},
        {"when": {"tool.name": "Bash"}, "set": {"actor.display_name": "second", "actor.id": "agent:shell"}},
        {"when": {"actor.id": "agent:shell"}, "set": {"actor.display_name": "never"}},   # tests Levain's value
        {"when": {}, "set": {"actor.display_name": "default"}}])
    got = ab.export(queue.directory, now=now, names=names)
    for before, after in zip(plain.receipts, got.receipts):
        if before["tool"]["name"] == "Bash":
            assert after["actor"]["display_name"] == "bash-bot"
            assert after["actor"]["id"] == "agent:shell"         # the second rule still sets what the first did not
        else:
            assert after["actor"]["display_name"] == "default" and after["actor"]["id"] == before["actor"]["id"]
        assert after["tool"] == before["tool"]
    assert got.report.names_renamed == len(got.receipts)


def test_a_renamed_path_is_synthesized_and_an_unchanged_one_keeps_its_provenance(world):
    queue, now, plain = world
    signer = _signer(plain)
    _, names = _names([
        {"when": {}, "set": {"actor.display_name": "Agent"}},
        {"when": {"policy.name": "levain.consent.T2"}, "set": {"policy.name": "levain.consent.T2"}},  # no rename
        {"when": {"approval.approver.id": signer},
         "set": {"approval.approver.id": "user:eng-lead@acme", "approval.approver.display_name": "Eng Lead"}}])
    got = ab.export(queue.directory, now=now, names=names)
    weights = {"observed": 2, "inferred": 1, "synthesized": 0}
    for before, after in zip(plain.receipts, got.receipts):
        prov = after["provenance"]
        assert after["actor"]["display_name"] == "Agent" and prov["actor.display_name"] == "synthesized"
        assert prov["policy.name"] == before["provenance"]["policy.name"]          # not renamed: unchanged
        assert prov["tool.name"] == before["provenance"]["tool.name"]
        if "approval" in before and before["approval"]["approver"]["id"] == signer:
            assert after["approval"]["approver"] == {**before["approval"]["approver"], "id": "user:eng-lead@acme",
                                                     "display_name": "Eng Lead"}
            assert prov["approval.approver.id"] == prov["approval.approver.display_name"] == "synthesized"
        elif "approval" in before:
            # another approver (the agent's own dialog included) is never renamed or given a display name
            assert after["approval"] == before["approval"]
            assert prov["approval.approver.id"] == before["provenance"]["approval.approver.id"]
            assert "approval.approver.display_name" not in prov
        else:
            assert "approval" not in after                       # a names file never creates an approval block
            assert "approval.approver.display_name" not in prov
        applicable = list(ab._REQUIRED_PATHS) + [p for p in ab._CONDITIONAL_PATHS if ab._present(after, p)]
        assert set(prov) == set(applicable)
        assert after["completeness_score"] == (sum(weights[prov[p]] for p in applicable) * 1000
                                               // (2 * len(applicable))) / 1000
        assert ab.receipt_hash(after) == after["receipt_hash"]


def _pinned_everything(plain) -> list[dict]:
    """A names file that renames every alias in the scratch world, every authority name pinned one to one."""
    signer = _signer(plain)
    cwds = sorted({r["target"]["resource_id"] for r in plain.receipts})
    return (_policy_rules("levain.consent.T2", "acme.repo.merges", "7")
            + _policy_rules("levain.consent.T3", "acme.repo.admin", "7") + [
        {"when": {"approval.approver.id": signer},
         "set": {"approval.approver.id": "user:eng-lead@acme", "approval.approver.display_name": "Eng Lead"}},
        {"when": {"tool.capability": "claude-code.bash"}, "set": {"tool.capability": "shell.exec"}},
        {"when": {"tool.capability": "claude-code.edit"}, "set": {"tool.capability": "repo.edit"}},
        {"when": {"tool.capability": "codex.write"}, "set": {"tool.capability": "repo.write"}},
        {"when": {}, "set": {"actor.display_name": "Agent"}}]
        + [{"when": {"target.resource_id": c}, "set": {"target.resource_id": f"repo:{i}"}} for i, c in enumerate(cwds)])


@pytest.mark.parametrize("label,rules", [
    ("none", []),
    ("actor aliases", [{"when": {}, "set": {"actor.id": "agent:bot", "actor.display_name": "Bot"}}]),
    ("an inferred capability", [{"when": {"tool.capability": "claude-code.bash"},
                                 "set": {"tool.capability": "shell.exec"}}]),
    ("everything, pinned (observed resource_id included)", None),
])
def test_a_rename_never_raises_completeness_score(world, label, rules):
    queue, now, plain = world
    _, names = _names(_pinned_everything(plain) if rules is None else rules)
    got = ab.export(queue.directory, now=now, names=names)
    assert len(got.receipts) == len(plain.receipts)
    for before, after in zip(plain.receipts, got.receipts):
        assert after["completeness_score"] <= before["completeness_score"], (label, before["receipt_id"])
        assert ab.receipt_hash(after) == after["receipt_hash"]


def _file(rules) -> bytes:
    return json.dumps({"format": ab.NAMES_FORMAT, "rules": rules}, ensure_ascii=False).encode("utf-8")


_FP = "ssh-key:SHA256:1AgQICWdzWa44kvpp54KfztaUPQO0EP21qJ0XBCAyRs"


@pytest.mark.parametrize("label,data,match", [
    # authority names map one to one from Levain's own value, never from an empty or another field's `when`
    ("empty-when approver rename", _file([{"when": {}, "set": {"approval.approver.id": "user:alice"}}]),
     "rule 0: sets the authority name approval.approver.id but its `when` does not pin approval.approver.id"),
    ("empty-when approver display name", _file([{"when": {}, "set": {"approval.approver.display_name": "Alice"}}]),
     "rule 0: sets the authority name approval.approver.display_name but its `when` does not pin "
     "approval.approver.id"),
    ("empty-when policy rename", _file([{"when": {}, "set": {"policy.name": "acme.strict"}}]),
     "rule 0: sets the authority name policy.name"),
    ("cross-field when for an authority name",
     _file([{"when": {"tool.name": "Bash"}, "set": {"tool.capability": "shell.exec"}}]),
     "rule 0: sets the authority name tool.capability but its `when` does not pin tool.capability"),
    ("cross-field when for the approver",
     _file([{"when": {"policy.name": "levain.consent.T2"}, "set": {"approval.approver.id": "user:alice"}}]),
     "does not pin approval.approver.id"),
    ("policy.version without its name pinned",
     _file([{"when": {"policy.version": "unknown"}, "set": {"policy.version": "7"}}]),
     "rule 0: sets the authority name policy.version but its `when` does not pin policy.name"),
    ("policy.version with only its name pinned",
     _file([{"when": {"policy.name": "levain.consent.T2"}, "set": {"policy.version": "7"}}]),
     "does not pin policy.version"),
    ("resource from the cwd of another field",
     _file([{"when": {"agent.framework": "claude-code"}, "set": {"target.resource_id": "/prod/payments"}}]),
     "does not pin target.resource_id"),
    # an authority rule pins ONLY its own field: an extra selector could split a value on a later receipt
    ("extra selector on the approver",
     _file([{"when": {"approval.approver.id": _FP, "agent.framework": "claude-code"},
             "set": {"approval.approver.id": "user:alice"}}]),
     "rule 0: sets the authority name approval.approver.id but its `when` also selects on agent.framework"),
    ("extra selector on a policy name (its version)",
     _file([{"when": {"policy.name": "levain.consent.T2", "policy.version": "unknown"},
             "set": {"policy.name": "acme.strict", "policy.version": "7"}}]),
     "sets the authority name policy.name but its `when` also selects on policy.version"),
    ("extra selector on a capability",
     _file([{"when": {"tool.capability": "claude-code.bash", "tool.name": "Bash"},
             "set": {"tool.capability": "shell.exec"}}]),
     "also selects on tool.name"),
    ("an actor rename riding on an authority rule's selector",
     _file([{"when": {"approval.approver.id": _FP, "policy.name": "levain.consent.T2"},
             "set": {"actor.id": "agent:x", "approval.approver.id": "user:alice"}}]),
     "also selects on policy.name"),
    ("two of Levain's approvers onto one name",
     _file([{"when": {"approval.approver.id": _FP}, "set": {"approval.approver.id": "user:alice"}},
            {"when": {"approval.approver.id": "ssh-key:SHA256:other"}, "set": {"approval.approver.id": "user:alice"}}]),
     "rule 1: maps Levain's approval.approver.id 'ssh-key:SHA256:other' to 'user:alice', which rule 0"),
    ("one of Levain's approvers onto two names",
     _file([{"when": {"approval.approver.id": _FP}, "set": {"approval.approver.id": "user:a"}},
            {"when": {"approval.approver.id": _FP}, "set": {"approval.approver.id": "user:b"}}]),
     "rule 1: maps Levain's approval.approver.id .* to 'user:b', but rule 0 maps it to 'user:a'"),
    ("two approvers onto one display name",
     _file([{"when": {"approval.approver.id": _FP}, "set": {"approval.approver.display_name": "Alice"}},
            {"when": {"approval.approver.id": "ssh-key:SHA256:other"},
             "set": {"approval.approver.display_name": "Alice"}}]),
     "maps Levain's approval.approver.display_name 'ssh-key:SHA256:other' to 'Alice', which rule 0"),
    # no decision into a decision, and no decision dressed as none
    ("native-dialog source",
     _file([{"when": {"approval.approver.id": "native-dialog:operator"}, "set": {"approval.approver.id": _FP}}]),
     "pins approval.approver.id 'native-dialog:operator'"),
    ("native-dialog source, display name only",
     _file([{"when": {"approval.approver.id": "native-dialog:operator"},
             "set": {"approval.approver.display_name": "Alice"}}]),
     "the agent's own dialog answered"),
    ("native-permissions source",
     _file([{"when": {"policy.name": "claude-code.native-permissions"}, "set": {"policy.name": "acme.strict"}}]),
     "pins policy.name 'claude-code.native-permissions'"),
    ("the native approver literal as a value",
     _file([{"when": {"approval.approver.id": _FP}, "set": {"approval.approver.id": "native-dialog:operator"}}]),
     "set approval.approver.id to 'native-dialog:operator', Levain's own word for a call it never decided"),
    ("the native policy suffix as a value",
     _file([{"when": {"policy.name": "levain.consent.T2"}, "set": {"policy.name": "acme.native-permissions"}}]),
     "Levain's own word for a call it never decided"),
    ("the native approver literal as an actor id",
     _file([{"when": {}, "set": {"actor.id": "native-dialog:operator"}}]), "never decided"),
    # field syntax (spec 2.9 / 4.9)
    ("capability not lowercase dotted",
     _file([{"when": {"tool.capability": "claude-code.bash"}, "set": {"tool.capability": "NOT A.DOTTED capability"}}]),
     "set tool.capability to 'NOT A.DOTTED capability', which is not lowercase ASCII, dot-separated"),
    ("capability with no dot",
     _file([{"when": {"tool.capability": "claude-code.bash"}, "set": {"tool.capability": "shell"}}]),
     "not lowercase ASCII, dot-separated"),
    ("capability in upper case",
     _file([{"when": {"tool.capability": "claude-code.bash"}, "set": {"tool.capability": "Shell.Exec"}}]),
     "not lowercase ASCII, dot-separated"),
    ("capability not ASCII",
     _file([{"when": {"tool.capability": "claude-code.bash"}, "set": {"tool.capability": "shéll.exec"}}]),
     "not lowercase ASCII, dot-separated"),
    ("policy name with a space",
     _file([{"when": {"policy.name": "levain.consent.T2"}, "set": {"policy.name": "acme strict"}}]),
     "set policy.name to 'acme strict', which is not a dot-namespaced identifier"),
    ("policy name with an empty segment",
     _file([{"when": {"policy.name": "levain.consent.T2"}, "set": {"policy.name": "acme..strict"}}]),
     "not a dot-namespaced identifier"),
    # `when` tests names and the two selectors only
    ("when on receipt_id", _file([{"when": {"receipt_id": "x"}, "set": {"actor.display_name": "a"}}]),
     "rule 0: `when` tests 'receipt_id', which is not a name"),
    ("when on issued_at",
     _file([{"when": {"issued_at": "2026-01-01T00:00:00Z"}, "set": {"actor.display_name": "a"}}]),
     "`when` tests 'issued_at'"),
    ("when on approval.context", _file([{"when": {"approval.context": "x"}, "set": {"actor.display_name": "a"}}]),
     "`when` tests 'approval.context'"),
    ("when on a fact", _file([{"when": {"policy.decision": "allow"}, "set": {"actor.display_name": "a"}}]),
     "`when` tests 'policy.decision'"),
    ("when on the environment", _file([{"when": {"target.environment": "dev"}, "set": {"actor.display_name": "a"}}]),
     "`when` tests 'target.environment'"),
    # a name that hides a character
    ("bidi override in a policy name",
     _file([{"when": {"policy.name": "levain.consent.T2"}, "set": {"policy.name": "acme.‮egrem‬"}}]),
     r"set policy.name holds a character a reader cannot see: U\+202E \(RIGHT-TO-LEFT OVERRIDE\) at character 6"),
    ("BEL in a display name",
     _file([{"when": {"approval.approver.id": _FP}, "set": {"approval.approver.display_name": "Alice\u0007"}}]),
     r"U\+0007"),
    ("newline in a display name", _file([{"when": {}, "set": {"actor.display_name": "Alice\nAdmin"}}]),
     r"U\+000A"),
    ("zero-width space in an id", _file([{"when": {}, "set": {"actor.id": "agent:​bot"}}]), r"U\+200B"),
    ("selector in an identifier", _file([{"when": {}, "set": {"actor.id": "bot⚠️"}}]), r"U\+FE0F"),
    # malformed beyond JSON's own errors
    ("deep nesting", b"[" * 200000, "nested too deep"),
    ("oversize", b" " * (ab.NAMES_MAX_BYTES + 1), "larger than the 1048576-byte limit"),
    ("NaN value", b'{"format":"levain-agentboundary-names/1","rules":[{"when":{},"set":{"actor.id":NaN}}]}',
     "non-empty string"),
])
def test_a_names_file_is_refused_at_load(label, data, match):
    with pytest.raises(ab.ExportError, match=match):
        ab.load_names(data)


@pytest.mark.parametrize("label,rules", [
    ("pinned 1:1 approver map",
     [{"when": {"approval.approver.id": _FP}, "set": {"approval.approver.id": "user:alice"}}]),
    ("pinned approver id and display name",
     [{"when": {"approval.approver.id": _FP},
       "set": {"approval.approver.id": "user:alice", "approval.approver.display_name": "Renée Lefèvre"}}]),
    ("actor names with any selector", [{"when": {"agent.framework": "codex", "tool.name": "write"},
                                        "set": {"actor.id": "agent:codex", "actor.display_name": "Café bot"}}]),
    ("display_name with an emoji selector that shows", [{"when": {}, "set": {"actor.display_name": "⚠️ bot"}}]),
    ("pinned policy name and version, two rules each",
     _policy_rules("levain.consent.T2", "acme.strict", "7") + _policy_rules("levain.consent.T3", "Acme.Admin_2", "7")),
    ("two of Levain's capabilities, two names",
     [{"when": {"tool.capability": "claude-code.bash"}, "set": {"tool.capability": "shell.exec"}},
      {"when": {"tool.capability": "codex.write"}, "set": {"tool.capability": "repo.write-file_2"}}]),
    ("a resource to an operator id", [{"when": {"target.resource_id": "/w"}, "set": {"target.resource_id": "repo:w"}}]),
])
def test_a_names_file_is_accepted_at_load(label, rules):
    names = ab.load_names(_file(rules))
    assert len(names.rules) == len(rules)


def test_an_accepted_file_that_joins_an_authority_value_in_the_export_is_refused(world):
    queue, now, plain = world
    # T3 renamed onto T2's own unrenamed name: two of Levain's policies would become one
    _, join = _names([{"when": {"policy.name": "levain.consent.T3"}, "set": {"policy.name": "levain.consent.T2"}}])
    with pytest.raises(ab.ExportError, match="both become"):
        ab.export(queue.directory, now=now, names=join)


def test_the_native_dialog_approval_keeps_its_approver_whatever_the_map(world):
    queue, now, plain = world
    _, names = _names(_pinned_everything(plain))
    got = ab.export(queue.directory, now=now, names=names)
    native = [(b, a) for b, a in zip(plain.receipts, got.receipts)
              if b.get("approval", {}).get("approver", {}).get("id") == "native-dialog:operator"]
    assert native
    for before, after in native:
        assert after["approval"] == before["approval"]                # id, role, no display name, context: untouched
        assert "display_name" not in after["approval"]["approver"]
        for p in ("approval.approver.id", "approval.approver.role", "approval.context"):
            assert after["provenance"][p] == before["provenance"][p]
    unasked = [a for b, a in zip(plain.receipts, got.receipts)
               if b["policy"]["name"] == "claude-code.native-permissions"]
    assert unasked and all(a["policy"]["name"] == "claude-code.native-permissions" for a in unasked)


def test_rename_itself_never_names_the_native_dialogs_approver(world):
    """``_rename`` holds the line on its own, whatever validated its rules: a hand-made rule keyed by the native
    approver writes nothing into its approver block."""
    _, _, plain = world
    native = next(r for r in plain.receipts
                  if r.get("approval", {}).get("approver", {}).get("id") == ab._NATIVE_APPROVER)
    receipt = json.loads(json.dumps(native))
    forged = ab.Names(rules=(((("approval.approver.id", ab._NATIVE_APPROVER),),
                              (("approval.approver.id", "user:alice"),
                               ("approval.approver.display_name", "Alice"))),), sha256="0" * 64)
    before = json.loads(json.dumps(receipt))
    assert ab._rename(receipt, forged) == [] and receipt == before


@pytest.mark.parametrize("label,rules,sha", [
    ("a fact turned allow", ((((), (("policy.decision", "allow"),)),)), "x"),
    ("a fact turned allow, a good digest", ((((), (("policy.decision", "allow"),)),)), "0" * 64),
    ("an unpinned approver", ((((), (("approval.approver.id", "user:alice"),)),)), "0" * 64),
    ("an extra selector", (((("policy.name", "levain.consent.T2"), ("tool.name", "Bash")),
                            (("policy.name", "acme.strict"),)),), "0" * 64),
    ("the environment", ((((), (("target.environment", "prod"),)),)), "0" * 64),
    ("the native dialog as a key", (((("approval.approver.id", ab._NATIVE_APPROVER),),
                                     (("approval.approver.id", "user:alice"),)),), "0" * 64),
    ("a bad capability", (((("tool.capability", "claude-code.bash"),), (("tool.capability", "BAD CAP"),)),), "0" * 64),
    ("not a tuple", [((), (("actor.id", "a"),))], "0" * 64),
    ("a non-string value", ((((), (("actor.id", 3),)),)), "0" * 64),
    ("a non-NFC value", ((((), (("actor.id", "é"),)),)), "0" * 64),
])
def test_a_forged_names_is_refused_by_export(world, label, rules, sha):
    queue, now, _ = world
    with pytest.raises(ab.ExportError):
        ab.export(queue.directory, now=now, names=ab.Names(rules=rules, sha256=sha))


def test_renamed_receipts_pass_the_specs_own_validator(world):
    agentboundary = pytest.importorskip("agentboundary")             # the suite venv has it; levain never depends on it
    queue, now, plain = world
    _, names = _names(_pinned_everything(plain) + [{"when": {}, "set": {"actor.id": "agent:copilot"}}])
    for r in ab.export(queue.directory, now=now, names=names).receipts:
        assert not agentboundary.validate_receipt(r)


def test_names_must_come_from_load_names(world):
    queue, now, _ = world
    with pytest.raises(ab.ExportError, match="load_names"):
        ab.export(queue.directory, now=now, names={"rules": []})


# --- the CLI ---------------------------------------------------------------------------------------------------

@pytest.fixture()
def cli_world(tmp_path, monkeypatch):
    fixed = datetime.now(timezone.utc) + timedelta(days=60)
    monkeypatch.setattr(ab, "_utcnow", lambda: fixed)
    (tmp_path / "w").mkdir()
    queue, _ = build_scratch(tmp_path / "w")
    return queue.directory, tmp_path


def test_cli_names_exports_the_renamed_receipts_and_reports_the_file(cli_world, capsys):
    qdir, tmp = cli_world
    data = json.dumps({"format": ab.NAMES_FORMAT, "rules": [
        {"when": {"policy.name": "levain.consent.T2"}, "set": {"policy.name": "acme.repo.merges"}}]}).encode()
    nf = tmp / "names.json"
    nf.write_bytes(data)
    rc = cli.main(["consent", "receipts", "export", "--format", "agentboundary", "--queue-dir", str(qdir),
                   "--names", str(nf)])
    out, err = capsys.readouterr()
    assert rc == 0
    assert json.loads(out) == ab.export(qdir, names=ab.load_names(data)).receipts
    assert any(r["policy"]["name"] == "acme.repo.merges" for r in json.loads(out))
    assert hashlib.sha256(data).hexdigest() in err and "renamed" in err


def test_cli_refuses_a_names_file_that_sets_a_fact_and_exports_nothing(cli_world, capsys):
    qdir, tmp = cli_world
    nf = tmp / "names.json"
    nf.write_text(json.dumps({"format": ab.NAMES_FORMAT, "rules": [{"when": {}, "set": {"policy.decision": "allow"}}]}))
    target = tmp / "out.json"
    rc = cli.main(["consent", "receipts", "export", "--format", "agentboundary", "--queue-dir", str(qdir),
                   "--names", str(nf), "--out", str(target)])
    out, err = capsys.readouterr()
    assert rc == 1 and out == "" and "policy.decision" in err and not target.exists()
    rc = cli.main(["consent", "receipts", "export", "--format", "agentboundary", "--queue-dir", str(qdir),
                   "--names", str(tmp / "missing.json")])
    out, err = capsys.readouterr()
    assert rc == 1 and out == "" and err.startswith("levain consent receipts export:")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
def test_cli_refuses_a_names_path_that_is_not_a_regular_file(cli_world, capsys):
    qdir, tmp = cli_world
    fifo = tmp / "names.fifo"
    os.mkfifo(fifo)                       # no writer: a plain open() would block forever
    for path in (fifo, tmp):              # a FIFO, a directory
        rc = cli.main(["consent", "receipts", "export", "--format", "agentboundary", "--queue-dir", str(qdir),
                       "--names", str(path)])
        out, err = capsys.readouterr()
        assert rc == 1 and out == "" and err.startswith("levain consent receipts export:"), err
    assert "not a regular file" in err or "directory" in err.lower()
