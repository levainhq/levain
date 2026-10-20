"""The AgentBoundary names bridge: a names file maps Levain's names onto the operator's inside the export
(:func:`levain.consent.agentboundary.load_names`, ``export(names=...)``, ``--names``). Names only, never facts;
an authority name maps one to one from Levain's own value, never from a call Levain did not decide; renamed = synthesized."""
from __future__ import annotations

import hashlib
import json
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


def test_a_rename_lands_the_hash_verifies_and_the_chain_links_over_the_renamed_receipts(world):
    queue, now, plain = world
    signer = _signer(plain)
    data, names = _names([
        {"when": {"policy.name": "levain.consent.T2", "policy.version": "unknown"},
         "set": {"policy.name": "acme.repo.merges", "policy.version": "7"}},
        {"when": {"approval.approver.id": signer}, "set": {"approval.approver.id": "user:eng-lead@acme"}}])
    got = ab.export(queue.directory, now=now, names=names)
    assert len(got.receipts) == len(plain.receipts)
    renamed = 0
    for before, after in zip(plain.receipts, got.receipts):
        assert after["receipt_id"] == before["receipt_id"]                       # ids are Levain's, never renamed
        assert after["execution"] == before["execution"]                         # facts untouched, leaf included
        assert after["policy"]["decision"] == before["policy"]["decision"]
        assert ab.receipt_hash(after) == after["receipt_hash"]                   # the hash covers the renamed receipt
        if before["policy"]["name"] == "levain.consent.T2":
            assert after["policy"]["name"] == "acme.repo.merges" and after["policy"]["version"] == "7"
        else:
            assert after["policy"] == before["policy"]
        if before.get("approval", {}).get("approver", {}).get("id") == signer:
            assert after["approval"]["approver"]["id"] == "user:eng-lead@acme"
        moved = {"receipt_hash", "prior_receipt", "provenance", "completeness_score", "policy", "approval"}
        assert {k: v for k, v in after.items() if k not in moved} == {k: v for k, v in before.items() if k not in moved}
        if "approval" in before:
            assert {**after["approval"], "approver": {}} == {**before["approval"], "approver": {}}
        renamed += after["policy"] != before["policy"] or after.get("approval") != before.get("approval")
    assert renamed > 0
    for prev, cur in zip(got.receipts, got.receipts[1:]):
        assert cur["prior_receipt"] == {"receipt_id": prev["receipt_id"], "receipt_hash": prev["receipt_hash"]}
    assert got.report.names_sha256 == hashlib.sha256(data).hexdigest()
    assert got.report.names_renamed == renamed
    assert plain.report.names_sha256 is None and plain.report.names_renamed == 0
    # a re-export with the same file reproduces it whole
    assert ab.export(queue.directory, now=now, names=names).receipts == got.receipts


@pytest.mark.parametrize("field", ["policy.decision", "execution.status", "actor.type", "approval.approved_at",
                                   "approval.approver.role", "approval.context", "execution.result_ref",
                                   "execution.completed_at", "refusal.verdict", "issued_at", "receipt_id",
                                   "receipt_hash", "arguments_hash", "prior_receipt.receipt_hash", "provenance",
                                   "completeness_score", "policy", "nonsense.field"])
def test_a_names_file_that_sets_a_fact_is_refused_naming_the_field(field):
    data = json.dumps({"format": ab.NAMES_FORMAT, "rules": [
        {"when": {}, "set": {"target.system": "fine"}}, {"when": {}, "set": {field: "x"}}]}).encode()
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
    (b'{"format": "levain-agentboundary-names/1", "rules": [{"when": {}, "set": {"target.environment": "qa"}}]}',
     "target.environment must be one of"),
    (b'{"format": "levain-agentboundary-names/1", "rules": [{"when": {"provenance.tool.name": "observed"}, '
     b'"set": {"policy.name": "a"}}]}', "which is not a name"),
    (b'{"format": "levain-agentboundary-names/1", "rules": [{"when": {"tool.name": 1}, "set": {"policy.name": "a"}}]}',
     "when tool.name must be a non-empty string"),
])
def test_a_malformed_names_file_is_refused(data, match):
    with pytest.raises(ab.ExportError, match=match):
        ab.load_names(data)


def test_first_match_wins_per_field_when_tests_levains_values_and_an_empty_when_is_the_default(world):
    queue, now, plain = world
    _, names = _names([
        {"when": {"tool.name": "Bash"}, "set": {"target.system": "bash-box", "tool.name": "shell"}},
        {"when": {"tool.name": "Bash"}, "set": {"target.system": "second", "target.environment": "prod"}},
        {"when": {"tool.name": "shell"}, "set": {"target.system": "never"}},        # tests Levain's value, not ours
        {"when": {}, "set": {"target.system": "default"}}])
    got = ab.export(queue.directory, now=now, names=names)
    for before, after in zip(plain.receipts, got.receipts):
        if before["tool"]["name"] == "Bash":
            assert after["target"]["system"] == "bash-box" and after["tool"]["name"] == "shell"
            assert after["target"]["environment"] == "prod"     # the second rule still sets what the first did not
        else:
            assert after["target"]["system"] == "default" and after["tool"]["name"] == before["tool"]["name"]
            assert after["target"]["environment"] == before["target"]["environment"]
    assert got.report.names_renamed == len(got.receipts)



def test_a_renamed_path_is_synthesized_and_an_unchanged_one_keeps_its_provenance(world):
    queue, now, plain = world
    signer = _signer(plain)
    _, names = _names([
        {"when": {}, "set": {"tool.name": "renamed-tool", "approval.approver.display_name": "Eng Lead",
                             "target.system": "levain-workspace"}},                # Levain's own value: no rename
        {"when": {"approval.approver.id": signer}, "set": {"approval.approver.id": "user:eng-lead@acme"}}])
    got = ab.export(queue.directory, now=now, names=names)
    weights = {"observed": 2, "inferred": 1, "synthesized": 0}
    for before, after in zip(plain.receipts, got.receipts):
        prov = after["provenance"]
        assert before["provenance"]["tool.name"] == "observed" and prov["tool.name"] == "synthesized"
        assert prov["target.system"] == before["provenance"]["target.system"] == "synthesized"
        assert prov["policy.name"] == before["provenance"]["policy.name"]          # not renamed: unchanged
        if "approval" in before:
            assert after["approval"]["approver"]["display_name"] == "Eng Lead"
            assert prov["approval.approver.display_name"] == "synthesized"
            if before["approval"]["approver"]["id"] == signer:
                assert prov["approval.approver.id"] == "synthesized"
            else:
                assert prov["approval.approver.id"] == before["provenance"]["approval.approver.id"]
        else:
            assert "approval" not in after                       # a names file never creates an approval block
            assert "approval.approver.display_name" not in prov
        applicable = list(ab._REQUIRED_PATHS) + [p for p in ab._CONDITIONAL_PATHS if ab._present(after, p)]
        assert set(prov) == set(applicable)
        assert after["completeness_score"] == (sum(weights[prov[p]] for p in applicable) * 1000
                                               // (2 * len(applicable))) / 1000
        assert ab.receipt_hash(after) == after["receipt_hash"]


def _pinned_everything(plain) -> list[dict]:
    """A names file that renames every name in the scratch world, every authority name pinned one to one."""
    signer = _signer(plain)
    return [
        {"when": {"policy.name": "levain.consent.T2", "policy.version": "unknown"},
         "set": {"policy.name": "acme.repo.merges", "policy.version": "7"}},
        {"when": {"policy.name": "levain.consent.T3", "policy.version": "unknown"},
         "set": {"policy.name": "acme.repo.admin", "policy.version": "7"}},
        {"when": {"approval.approver.id": signer},
         "set": {"approval.approver.id": "user:eng-lead@acme", "approval.approver.display_name": "Eng Lead"}},
        {"when": {"tool.capability": "claude-code.bash"}, "set": {"tool.capability": "shell.exec"}},
        {"when": {"tool.capability": "claude-code.edit"}, "set": {"tool.capability": "repo.edit"}},
        {"when": {"tool.capability": "codex.write"}, "set": {"tool.capability": "repo.write"}},
        {"when": {}, "set": {"agent.model": "claude-x", "agent.framework_version": "1.0", "target.system": "acme",
                             "target.environment": "prod", "actor.display_name": "Agent", "tool.version": "2"}}]


@pytest.mark.parametrize("label,rules", [
    ("none", []),
    ("unknown fields named", [{"when": {}, "set": {"agent.model": "claude-x", "agent.framework_version": "1.0",
                                                  "target.system": "acme", "agent.model_version": "5"}}]),
    ("observed tool.name", [{"when": {}, "set": {"tool.name": "shell"}}]),
    ("everything, pinned", None),
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
    ("two of Levain's approvers onto one name",
     _file([{"when": {"approval.approver.id": _FP}, "set": {"approval.approver.id": "user:alice"}},
            {"when": {"approval.approver.id": "ssh-key:SHA256:other"}, "set": {"approval.approver.id": "user:alice"}}]),
     "rule 1: maps Levain's approval.approver.id 'ssh-key:SHA256:other' to 'user:alice', which rule 0"),
    ("one of Levain's approvers onto two names",
     _file([{"when": {"approval.approver.id": _FP, "tool.name": "Bash"}, "set": {"approval.approver.id": "user:a"}},
            {"when": {"approval.approver.id": _FP}, "set": {"approval.approver.id": "user:b"}}]),
     "rule 1: maps Levain's approval.approver.id .* to 'user:b', but rule 0 maps it to 'user:a'"),
    # no decision into a decision
    ("native-dialog source",
     _file([{"when": {"approval.approver.id": "native-dialog:operator"}, "set": {"approval.approver.id": _FP}}]),
     "pins approval.approver.id 'native-dialog:operator'"),
    ("native-dialog source, display name only",
     _file([{"when": {"approval.approver.id": "native-dialog:operator"},
             "set": {"approval.approver.display_name": "Alice"}}]),
     "the agent's own dialog answered"),
    ("native-permissions source",
     _file([{"when": {"policy.name": "claude-code.native-permissions", "policy.version": "unknown"},
             "set": {"policy.name": "acme.strict", "policy.version": "7"}}]),
     "pins policy.name 'claude-code.native-permissions'"),
    # `when` tests names only
    ("when on receipt_id", _file([{"when": {"receipt_id": "x"}, "set": {"target.system": "a"}}]),
     "rule 0: `when` tests 'receipt_id', which is not a name"),
    ("when on issued_at", _file([{"when": {"issued_at": "2026-01-01T00:00:00Z"}, "set": {"target.system": "a"}}]),
     "`when` tests 'issued_at'"),
    ("when on approval.context", _file([{"when": {"approval.context": "x"}, "set": {"target.system": "a"}}]),
     "`when` tests 'approval.context'"),
    ("when on a fact", _file([{"when": {"policy.decision": "allow"}, "set": {"target.system": "a"}}]),
     "`when` tests 'policy.decision'"),
    # a name that hides a character
    ("bidi override in a policy name",
     _file([{"when": {"policy.name": "levain.consent.T2", "policy.version": "unknown"},
             "set": {"policy.name": "acme.‮egrem‬", "policy.version": "7"}}]),
     r"set policy.name holds a character a reader cannot see: U\+202E \(RIGHT-TO-LEFT OVERRIDE\) at character 6"),
    ("BEL in a display name", _file([{"when": {}, "set": {"approval.approver.display_name": "Alice\u0007"}}]),
     r"U\+0007"),
    ("newline in a display name", _file([{"when": {}, "set": {"actor.display_name": "Alice\nAdmin"}}]),
     r"U\+000A"),
    ("zero-width space in an id", _file([{"when": {}, "set": {"actor.id": "agent:​bot"}}]), r"U\+200B"),
    ("selector in an identifier", _file([{"when": {}, "set": {"tool.version": "⚠️"}}]), r"U\+FE0F"),
    # malformed beyond JSON's own errors
    ("deep nesting", b"[" * 200000, "nested too deep"),
    ("oversize", b" " * (ab.NAMES_MAX_BYTES + 1), "larger than the 1048576-byte limit"),
    ("NaN value", b'{"format":"levain-agentboundary-names/1","rules":[{"when":{},"set":{"target.system":NaN}}]}',
     "non-empty string"),
])
def test_a_names_file_is_refused_at_load(label, data, match):
    with pytest.raises(ab.ExportError, match=match):
        ab.load_names(data)


@pytest.mark.parametrize("label,rules", [
    ("pinned 1:1 approver map",
     [{"when": {"approval.approver.id": _FP}, "set": {"approval.approver.id": "user:alice"}}]),
    ("pinned approver map with a selector",
     [{"when": {"approval.approver.id": _FP, "agent.framework": "claude-code"},
       "set": {"approval.approver.id": "user:alice"}}]),
    ("display_name with an accent", [{"when": {}, "set": {"approval.approver.display_name": "Renée Lefèvre",
                                                         "actor.display_name": "Café bot"}}]),
    ("display_name with an emoji selector that shows", [{"when": {}, "set": {"actor.display_name": "⚠️ bot"}}]),
    ("pinned policy name and version",
     [{"when": {"policy.name": "levain.consent.T2", "policy.version": "unknown"},
       "set": {"policy.name": "acme.strict", "policy.version": "7"}},
      {"when": {"policy.name": "levain.consent.T3", "policy.version": "unknown"},
       "set": {"policy.name": "acme.admin", "policy.version": "7"}}]),
    ("two of Levain's capabilities, two names",
     [{"when": {"tool.capability": "claude-code.bash"}, "set": {"tool.capability": "shell.exec"}},
      {"when": {"tool.capability": "codex.write"}, "set": {"tool.capability": "repo.write"}}]),
])
def test_a_names_file_is_accepted_at_load(label, rules):
    names = ab.load_names(_file(rules))
    assert len(names.rules) == len(rules)


def test_an_accepted_file_that_splits_or_joins_an_authority_value_in_the_export_is_refused(world):
    queue, now, plain = world
    signer = _signer(plain)
    # a selector renames the signer under T3 only: Levain's one approver would become two names
    _, split = _names([{"when": {"approval.approver.id": signer, "policy.name": "levain.consent.T3"},
                        "set": {"approval.approver.id": "user:alice"}}])
    with pytest.raises(ab.ExportError, match="becomes both"):
        ab.export(queue.directory, now=now, names=split)
    # T3 renamed onto T2's own unrenamed name: two of Levain's policies would become one
    _, join = _names([{"when": {"policy.name": "levain.consent.T3"}, "set": {"policy.name": "levain.consent.T2"}}])
    with pytest.raises(ab.ExportError, match="both become"):
        ab.export(queue.directory, now=now, names=join)


def test_the_native_dialog_approval_keeps_its_approver_whatever_the_policy_map(world):
    queue, now, plain = world
    _, names = _names(_pinned_everything(plain))
    got = ab.export(queue.directory, now=now, names=names)
    native = [(b, a) for b, a in zip(plain.receipts, got.receipts)
              if b.get("approval", {}).get("approver", {}).get("id") == "native-dialog:operator"]
    assert native
    for before, after in native:
        assert after["approval"]["approver"]["id"] == "native-dialog:operator"
        assert after["provenance"]["approval.approver.id"] == before["provenance"]["approval.approver.id"]
    unasked = [a for b, a in zip(plain.receipts, got.receipts)
               if b["policy"]["name"] == "claude-code.native-permissions"]
    assert unasked and all(a["policy"]["name"] == "claude-code.native-permissions" for a in unasked)


def test_renamed_receipts_pass_the_specs_own_validator(world):
    agentboundary = pytest.importorskip("agentboundary")             # the suite venv has it; levain never depends on it
    queue, now, plain = world
    _, names = _names(_pinned_everything(plain) + [
        {"when": {}, "set": {"actor.id": "agent:copilot", "agent.model": "gpt-4o"}}])
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
