"""The AgentBoundary names bridge: a names file maps Levain's names onto the operator's inside the export
(:func:`levain.consent.agentboundary.load_names`, ``export(names=...)``, ``--names``). Names only, never facts."""
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
        {"when": {"policy.name": "levain.consent.T2"}, "set": {"policy.name": "acme.repo.merges", "policy.version": "7"}},
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
        {"when": {}, "set": {"policy.name": "fine"}}, {"when": {}, "set": {field: "x"}}]}).encode()
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
     b'"set": {"policy.name": "a"}}]}', "not a field of Levain's receipt"),
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


def test_a_renamed_path_is_inferred_and_an_unchanged_one_keeps_its_provenance(world):
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
        assert before["provenance"]["tool.name"] == "observed" and prov["tool.name"] == "inferred"
        assert prov["target.system"] == before["provenance"]["target.system"] == "synthesized"
        if "approval" in before:
            assert after["approval"]["approver"]["display_name"] == "Eng Lead"
            assert prov["approval.approver.display_name"] == "inferred"
            if before["approval"]["approver"]["id"] == signer:
                assert prov["approval.approver.id"] == "inferred"
        else:
            assert "approval" not in after                       # a names file never creates an approval block
            assert "approval.approver.display_name" not in prov
        applicable = list(ab._REQUIRED_PATHS) + [p for p in ab._CONDITIONAL_PATHS if ab._present(after, p)]
        assert set(prov) == set(applicable)
        assert after["completeness_score"] == (sum(weights[prov[p]] for p in applicable) * 1000
                                               // (2 * len(applicable))) / 1000
        assert ab.receipt_hash(after) == after["receipt_hash"]


def test_renamed_receipts_pass_the_specs_own_validator(world):
    agentboundary = pytest.importorskip("agentboundary")             # the suite venv has it; levain never depends on it
    queue, now, plain = world
    _, names = _names([{"when": {}, "set": {"policy.name": "acme.p", "actor.id": "agent:copilot", "tool.version": "3",
                                            "agent.model": "gpt-4o", "target.environment": "staging",
                                            "approval.approver.display_name": "Eng Lead"}}])
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
