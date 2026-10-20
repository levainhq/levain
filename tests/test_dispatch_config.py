"""``levain dispatch config init|show|sign|accept`` and :func:`levain.dispatch.cli.resolvers` wired to the control
store: the real CLI over a real control store, real ssh-keygen keys enrolled in a real signed authority log, and the
contract tests' real git repo for the end-to-end filing. No template registry is injected here: every filing resolves
through the configuration these verbs signed."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from levain.cli import main
from levain.cockpit.consent import utc_stamp
from levain.dispatch import cli as dcli
from levain.dispatch import control as ctl
from levain.dispatch.contract import ResolvedTemplate
from levain.dispatch.jobs import JobStore
from tests.test_dispatch_contract import POLICY, make_repo, submission
from tests.test_dispatch_control import VERIFIERS, tmpl
from tests.test_dispatch_sign import keys, log  # noqa: F401  (module fixtures)


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("dispatch-config-repo"))


@pytest.fixture
def ddir(tmp_path, monkeypatch):
    d = tmp_path / "dispatch"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    # the operator's terminal: no agent in the environment or the process tree
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "CODEX_SANDBOX"):
        monkeypatch.delenv(var, raising=False)
    from levain.consent import band
    monkeypatch.setattr(band, "_ancestors", lambda: iter([]))
    monkeypatch.setattr(dcli, "agent_caller", lambda: None)
    return d


@pytest.fixture
def root(log):
    return str(log.path.parent)


def run(capsys, *argv):
    code = main(["dispatch", *argv])
    got = capsys.readouterr()
    return code, got.out, got.err


def body_file(tmp_path, name, body) -> str:
    f = tmp_path / f"{name}.json"
    f.write_text(json.dumps(body))
    return str(f)


def init(capsys, ddir, root, keys):
    code, out, _ = run(capsys, "config", "init", "--trust-root", keys["laptop"].fp, "--dispatch-dir", str(ddir),
                       "--data-root", root)
    assert code == 0, out
    return out


def sign(capsys, ddir, root, keys, tmp_path, kind, body, *extra, key="laptop"):
    return run(capsys, "config", "sign", kind, body_file(tmp_path, kind, body), "--dispatch-dir", str(ddir),
               "--data-root", root, "--signing-key", str(keys[key].path), *extra)


def configure(capsys, ddir, root, keys, tmp_path):
    init(capsys, ddir, root, keys)
    for kind, body in (("verifiers", VERIFIERS), ("templates", [tmpl()]), ("provider_policy", POLICY.to_mapping())):
        code, out, _ = sign(capsys, ddir, root, keys, tmp_path, kind, body, "--yes")
        assert code == 0, out


class NoTouch:
    """A stand-in for ``ssh_signer`` that records being reached: a refusal before any key touch never reaches it."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        raise AssertionError("the signer was reached")


@pytest.fixture
def no_touch(monkeypatch):
    nt = NoTouch()
    monkeypatch.setattr("levain.cockpit.keys.ssh_signer", nt)
    return nt


def store(ddir, root):
    from levain.authority import anchor_path
    from levain.consent.queue import authority_source
    from pathlib import Path

    return ctl.ControlStore(ddir, authority=authority_source(Path(root) / "cockpit.db", anchor=anchor_path(Path(root))))


# --- init ----------------------------------------------------------------------------------------------------------

def test_init_pins_the_root_and_prints_the_store(capsys, ddir, root, keys):
    out = init(capsys, ddir, root, keys)
    sid = store(ddir, root).store_id()
    assert sid in out and keys["laptop"].fp in out
    assert store(ddir, root).trust_root() == keys["laptop"].fp
    assert init(capsys, ddir, root, keys)                                   # again with the same root: a no-op


def test_init_refuses_a_key_that_may_not_sign_configuration(capsys, ddir, root, keys):
    code, out, _ = run(capsys, "config", "init", "--trust-root", keys["phone"].fp, "--dispatch-dir", str(ddir),
                       "--data-root", root)
    assert code == 2 and "does not cover T3" in out and "it is not pinned" in out
    assert not (ddir / ctl.DB_NAME).exists()
    code, out, _ = run(capsys, "config", "init", "--trust-root", keys["stranger"].fp, "--dispatch-dir", str(ddir),
                       "--data-root", root)
    assert code == 2 and "not an enrolled key" in out


# --- show ----------------------------------------------------------------------------------------------------------

def test_show_with_no_store_says_nothing_is_configured(capsys, ddir):
    code, out, _ = run(capsys, "config", "show", "--dispatch-dir", str(ddir))
    assert code == 0 and "nothing is configured" in out
    code, out, _ = run(capsys, "config", "show", "--dispatch-dir", str(ddir), "--json")
    assert code == 0 and json.loads(out) == {"configured": False, "store": str(ddir / ctl.DB_NAME)}


def test_show_summarises_every_active_kind(capsys, ddir, root, keys, tmp_path):
    configure(capsys, ddir, root, keys, tmp_path)
    code, out, _ = run(capsys, "config", "show", "--dispatch-dir", str(ddir), "--data-root", root)
    assert code == 0, out
    for want in ("generation 3", "templates", "fix-lint", "pytest-bb (black-box)", "pytest-ip (in-process)",
                 "anthropic, openrouter"):
        assert want in out, want
    code, out, _ = run(capsys, "config", "show", "--dispatch-dir", str(ddir), "--data-root", root, "--json")
    got = json.loads(out)
    assert got["configured"] and got["generation"] == 3 and got["head"].startswith("sha256:")
    assert got["kinds"]["templates"] == {"generation": 2, "ids": ["fix-lint"]}
    assert got["kinds"]["provider_policy"]["providers"] == ["anthropic", "openrouter"]


def test_show_counts_the_adoption_rules_and_reads_the_routemap(capsys, ddir, root, keys, tmp_path):
    configure(capsys, ddir, root, keys, tmp_path)
    t = tmpl()
    rules = [{"repo": "/srv/repo", "template_id": "fix-lint", "template_hash": t["content_hash"],
              "verifier_kind": "black-box", "criteria": [{"onto": ["main"]}, {"max_changed_lines": 40}]}]
    routes = {"routes": [{"route": "openrouter/glm", "provider": "openrouter", "upstream": "https://x.invalid",
                          "credential": "keychain:or", "models": [{"requested": "glm-5", "backend": "z-ai/glm-5"}],
                          "body_policy": {}, "retention_claim": "none"}]}
    assert sign(capsys, ddir, root, keys, tmp_path, "adoption_rules", rules, "--yes")[0] == 0
    assert sign(capsys, ddir, root, keys, tmp_path, "routemap", routes, "--yes")[0] == 0
    code, out, _ = run(capsys, "config", "show", "--dispatch-dir", str(ddir), "--data-root", root, "--json")
    kinds = json.loads(out)["kinds"]
    assert kinds["adoption_rules"]["count"] == 1 and kinds["adoption_rules"]["hold"] is True
    assert kinds["routemap"]["routes"] == ["openrouter/glm"]


def test_show_of_a_store_that_cannot_be_trusted_is_exit_3(capsys, ddir, root, keys):
    init(capsys, ddir, root, keys)
    (ddir / ctl.DB_NAME).write_bytes(b"this is not a database" * 100)
    code, out, _ = run(capsys, "config", "show", "--dispatch-dir", str(ddir), "--data-root", root)
    assert code == 3 and "ControlStateUnavailable" in out


# --- sign ----------------------------------------------------------------------------------------------------------

def test_sign_shows_the_item_then_accepts_it(capsys, ddir, root, keys, tmp_path):
    init(capsys, ddir, root, keys)
    sid = store(ddir, root).store_id()
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", VERIFIERS, "--yes")
    assert code == 0, out
    for want in ("kind        verifiers", "generation  1", f"store       {sid}", "prev        (none: the first item)",
                 "new kind", '+  "pytest-bb": {'):
        assert want in out, want
    assert out.index("new kind") < out.index("accepted generation 1")
    assert store(ddir, root).generation() == 1


def test_sign_diffs_against_the_active_body_of_its_kind(capsys, ddir, root, keys, tmp_path):
    configure(capsys, ddir, root, keys, tmp_path)
    changed = dict(VERIFIERS)
    del changed["pytest-ip"]
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", changed, "--yes")
    assert code == 0, out
    assert "--- verifiers, active (generation 1)" in out and "+++ verifiers, generation 4" in out
    assert '-  "pytest-ip": {' in out and "new kind" not in out
    top, head = store(ddir, root).head()
    assert top == 4


def test_a_body_the_registry_refuses_is_refused_before_any_key_touch(capsys, ddir, root, keys, tmp_path, no_touch):
    init(capsys, ddir, root, keys)
    # a template whose verifier is not registered: the verifiers item goes first
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "templates", [tmpl()], "--yes")
    assert code == 2 and "not registered" in out
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "provider_policy", {"never": []}, "--yes")
    assert code == 2 and "provider policy" in out
    assert no_touch.calls == 0
    assert store(ddir, root).generation() == 0


def test_a_body_that_is_not_leaf_json_is_refused_before_any_key_touch(capsys, ddir, root, keys, tmp_path, no_touch):
    init(capsys, ddir, root, keys)
    f = tmp_path / "float.json"
    f.write_text('{"pytest-bb": {"artifact_hash": "x", "kind": 1.5}}')
    code, out, _ = run(capsys, "config", "sign", "verifiers", str(f), "--dispatch-dir", str(ddir), "--data-root",
                       root, "--signing-key", str(keys["laptop"].path), "--yes")
    assert code == 2 and "body" in out and no_touch.calls == 0


def test_sign_with_a_key_that_is_not_the_root_is_refused_before_the_touch(capsys, ddir, root, keys, tmp_path):
    init(capsys, ddir, root, keys)
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", VERIFIERS, "--yes", key="phone")
    assert code == 2 and "is not the pinned trust root" in out
    assert store(ddir, root).generation() == 0


def test_sign_with_no_store_points_at_init(capsys, ddir, root, keys, tmp_path, no_touch):
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", VERIFIERS, "--yes")
    assert code == 2 and "levain dispatch config init" in out and no_touch.calls == 0


def test_sign_without_a_tty_refuses_and_asks_on_one(capsys, ddir, root, keys, tmp_path, monkeypatch):
    init(capsys, ddir, root, keys)
    monkeypatch.setattr(dcli, "_is_tty", lambda: False)
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", VERIFIERS)
    assert code == 2 and "in your own terminal" in out and "--yes" not in out.splitlines()[-1]
    monkeypatch.setattr(dcli, "_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", VERIFIERS)
    assert code == 2 and "not confirmed; nothing was signed" in out
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "y")
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", VERIFIERS)
    assert code == 0, out
    assert prompts and "generation 1" in prompts[0]
    assert store(ddir, root).generation() == 1


def test_a_store_fault_after_the_touch_says_what_was_not_accepted(capsys, ddir, root, keys, tmp_path, monkeypatch):
    configure(capsys, ddir, root, keys, tmp_path)

    def faulted(self, item, signature):
        raise ctl.ControlStateUnavailable("the control store cannot be read: disk I/O error")

    monkeypatch.setattr(ctl.ControlStore, "accept", faulted)
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", VERIFIERS, "--yes")
    assert code == 3 and "nothing was accepted; generation is still 3" in out


@pytest.mark.parametrize("verb", ["init", "sign", "accept"])
def test_an_agent_cannot_init_sign_or_accept(capsys, ddir, root, keys, tmp_path, monkeypatch, verb, no_touch):
    monkeypatch.setattr(dcli, "agent_caller", lambda: "claude_code")
    argv = {"init": ["init", "--trust-root", keys["laptop"].fp],
            "sign": ["sign", "verifiers", body_file(tmp_path, "v", VERIFIERS), "--signing-key",
                     str(keys["laptop"].path), "--yes"],
            "accept": ["accept", body_file(tmp_path, "i", {}), body_file(tmp_path, "s", {}), "--yes"]}[verb]
    code, out, _ = run(capsys, "config", *argv, "--dispatch-dir", str(ddir), "--data-root", root)
    assert code == 2 and "is the operator's own act" in out and "claude_code" in out
    assert not (ddir / ctl.DB_NAME).exists() and no_touch.calls == 0


def test_show_is_open_to_an_agent(capsys, ddir, root, keys, tmp_path, monkeypatch):
    configure(capsys, ddir, root, keys, tmp_path)
    monkeypatch.setattr(dcli, "agent_caller", lambda: "codex")
    code, out, _ = run(capsys, "config", "show", "--dispatch-dir", str(ddir), "--data-root", root)
    assert code == 0 and "fix-lint" in out


# --- accept --------------------------------------------------------------------------------------------------------

def _elsewhere(ddir, root, keys, tmp_path, kind, body, *, generation=None, prev=None):
    """An item signed off this CLI (another machine's tool), written to two files."""
    s = store(ddir, root)
    top, head = s.head()
    item = ctl.item_bytes(kind, top + 1 if generation is None else generation, body, datetime.now(timezone.utc),
                          prev=head if prev is None else prev, store_id=s.store_id())
    (tmp_path / "item.json").write_bytes(item)
    (tmp_path / "item.sig").write_text(keys["laptop"].sign(item, ctl.CONFIG_NAMESPACE))
    return str(tmp_path / "item.json"), str(tmp_path / "item.sig")


def test_accept_takes_an_item_signed_elsewhere(capsys, ddir, root, keys, tmp_path, monkeypatch):
    init(capsys, ddir, root, keys)
    item, sig = _elsewhere(ddir, root, keys, tmp_path, "verifiers", VERIFIERS)
    code, out, _ = run(capsys, "config", "accept", item, sig, "--dispatch-dir", str(ddir), "--data-root", root,
                       "--yes")
    assert code == 0, out
    assert "kind        verifiers" in out and "new kind" in out and "accepted generation 1" in out
    assert store(ddir, root).generation() == 1


def test_accept_of_an_older_generation_or_a_wrong_prev_is_refused(capsys, ddir, root, keys, tmp_path):
    configure(capsys, ddir, root, keys, tmp_path)
    old, old_sig = _elsewhere(ddir, root, keys, tmp_path, "verifiers", VERIFIERS, generation=2,
                              prev=store(ddir, root).head()[1])
    code, out, _ = run(capsys, "config", "accept", old, old_sig, "--dispatch-dir", str(ddir), "--data-root", root,
                       "--yes")
    assert code == 2 and "not above the highest accepted (3)" in out
    bad, bad_sig = _elsewhere(ddir, root, keys, tmp_path, "verifiers", VERIFIERS, prev="sha256:" + "0" * 64)
    code, out, _ = run(capsys, "config", "accept", bad, bad_sig, "--dispatch-dir", str(ddir), "--data-root", root,
                       "--yes")
    assert code == 2 and "does not chain to the head" in out
    assert store(ddir, root).generation() == 3


def test_accept_of_a_signature_that_does_not_verify_is_refused(capsys, ddir, root, keys, tmp_path):
    init(capsys, ddir, root, keys)
    item, sig = _elsewhere(ddir, root, keys, tmp_path, "verifiers", VERIFIERS)
    with open(item, "rb") as fh:
        raw = fh.read()
    (tmp_path / "item.sig").write_text(keys["phone"].sign(raw, ctl.CONFIG_NAMESPACE))
    code, out, _ = run(capsys, "config", "accept", item, sig, "--dispatch-dir", str(ddir), "--data-root", root,
                       "--yes")
    assert code == 2 and "refused, nothing changed" in out
    assert store(ddir, root).generation() == 0


# --- resolvers() wired ---------------------------------------------------------------------------------------------

def test_resolvers_is_none_with_no_store(ddir, root):
    assert dcli.resolvers(ddir, root) is None


def test_resolvers_reads_the_active_configuration(capsys, ddir, root, keys, tmp_path):
    configure(capsys, ddir, root, keys, tmp_path)
    resolve, policy = dcli.resolvers(ddir, root)
    got = resolve("fix-lint")
    assert isinstance(got, ResolvedTemplate) and got.content_hash == tmpl()["content_hash"]
    assert resolve("nope") is None and policy.content_hash() == POLICY.content_hash()


def test_resolvers_of_a_store_that_cannot_be_trusted_raises_unavailable(capsys, ddir, root, keys):
    init(capsys, ddir, root, keys)
    (ddir / ctl.DB_NAME).write_bytes(b"this is not a database" * 100)
    with pytest.raises(ctl.ControlStateUnavailable, match="nothing was filed"):
        dcli.resolvers(ddir, root)


def _contract(tmp_path, repo) -> str:
    path, base = repo
    sub = submission(path, base, expires_at=utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2)))
    f = tmp_path / "contract.json"
    f.write_text(json.dumps(sub))
    return str(f)


def test_file_resolves_through_the_signed_configuration(capsys, ddir, root, keys, tmp_path, repo):
    contract = _contract(tmp_path, repo)
    code, out, _ = run(capsys, "file", contract, "--dispatch-dir", str(ddir), "--data-root", root, "--agent",
                       "codex")
    assert code == 2 and "no template registry is set up" in out
    configure(capsys, ddir, root, keys, tmp_path)
    code, out, _ = run(capsys, "file", contract, "--dispatch-dir", str(ddir), "--data-root", root, "--agent",
                       "codex", "--json")
    assert code == 0, out
    job = JobStore(ddir).get(json.loads(out)["id"])
    assert job.contract.template.content_hash == tmpl()["content_hash"]
    assert job.contract.policy_hash == POLICY.content_hash()


def test_file_with_a_store_that_cannot_be_trusted_is_exit_3(capsys, ddir, root, keys, tmp_path, repo):
    init(capsys, ddir, root, keys)
    (ddir / ctl.DB_NAME).write_bytes(b"this is not a database" * 100)
    code, out, _ = run(capsys, "file", _contract(tmp_path, repo), "--dispatch-dir", str(ddir), "--data-root", root,
                       "--agent", "codex")
    assert code == 3 and "ControlStateUnavailable" in out and "nothing was filed" in out
    assert JobStore(ddir).list() == []


def test_file_with_templates_but_no_policy_is_refused(capsys, ddir, root, keys, tmp_path, repo):
    init(capsys, ddir, root, keys)
    for kind, body in (("verifiers", VERIFIERS), ("templates", [tmpl()])):
        assert sign(capsys, ddir, root, keys, tmp_path, kind, body, "--yes")[0] == 0
    code, out, _ = run(capsys, "file", _contract(tmp_path, repo), "--dispatch-dir", str(ddir), "--data-root", root,
                       "--agent", "codex")
    assert code == 2 and "no provider policy has been accepted" in out


def test_the_mcp_door_files_through_the_same_configuration(capsys, ddir, root, keys, tmp_path, repo):
    from pathlib import Path

    from levain.dispatch import mcp
    configure(capsys, ddir, root, keys, tmp_path)
    path, base = repo
    sub = submission(path, base, expires_at=utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2)))
    reply = mcp.handle("codex", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "dispatch_file", "arguments": {"contract": sub}}},
                       directory=ddir, data_root=Path(root))
    result = reply["result"]
    assert not result["isError"], result
    assert result["structuredContent"]["result"]["state"] == "filed"


# --- fix round 1 ------------------------------------------------------------------------------------------------------

def _tty(monkeypatch, answer="y"):
    """A terminal whose operator answers ``answer``; the prompts asked are recorded."""
    prompts = []
    monkeypatch.setattr(dcli, "_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or answer)
    return prompts


def accept_cli(capsys, ddir, root, item, sig, *extra):
    return run(capsys, "config", "accept", item, sig, "--dispatch-dir", str(ddir), "--data-root", root, *extra)


def test_accept_refuses_a_bad_signature_before_it_asks(capsys, ddir, root, keys, tmp_path, monkeypatch):
    init(capsys, ddir, root, keys)
    item, sig = _elsewhere(ddir, root, keys, tmp_path, "verifiers", VERIFIERS)
    with open(item, "rb") as fh:
        raw = fh.read()
    (tmp_path / "item.sig").write_text(keys["phone"].sign(raw, ctl.CONFIG_NAMESPACE))
    prompts = _tty(monkeypatch)
    code, out, _ = accept_cli(capsys, ddir, root, item, sig)
    assert code == 2 and "does not verify" in out, out
    assert prompts == [] and "the item" not in out          # refused before the view and before any question
    assert store(ddir, root).generation() == 0


def test_accept_refuses_an_item_for_another_store(capsys, ddir, root, keys, tmp_path, monkeypatch):
    init(capsys, ddir, root, keys)
    item = ctl.item_bytes("verifiers", 1, VERIFIERS, datetime.now(timezone.utc), prev="", store_id="0" * 32)
    (tmp_path / "item.json").write_bytes(item)
    (tmp_path / "item.sig").write_text(keys["laptop"].sign(item, ctl.CONFIG_NAMESPACE))
    prompts = _tty(monkeypatch)
    code, out, _ = accept_cli(capsys, ddir, root, str(tmp_path / "item.json"), str(tmp_path / "item.sig"))
    assert code == 2 and "another control store" in out and prompts == []
    assert store(ddir, root).generation() == 0


def test_accept_refuses_a_body_its_kind_refuses_before_it_asks(capsys, ddir, root, keys, tmp_path, monkeypatch):
    init(capsys, ddir, root, keys)
    # a template whose verifier is not registered: the verifiers item goes first
    item, sig = _elsewhere(ddir, root, keys, tmp_path, "templates", [tmpl()])
    prompts = _tty(monkeypatch)
    code, out, _ = accept_cli(capsys, ddir, root, item, sig)
    assert code == 2 and "not registered" in out and prompts == []
    assert store(ddir, root).generation() == 0


def test_accept_declined_on_the_terminal_accepts_nothing(capsys, ddir, root, keys, tmp_path, monkeypatch):
    configure(capsys, ddir, root, keys, tmp_path)
    item, sig = _elsewhere(ddir, root, keys, tmp_path, "verifiers", VERIFIERS)
    prompts = _tty(monkeypatch, answer="n")
    code, out, _ = accept_cli(capsys, ddir, root, item, sig)
    assert code == 2 and "not confirmed; nothing was accepted" in out and len(prompts) == 1
    assert store(ddir, root).generation() == 3


def test_a_verifier_that_cannot_run_at_accept_is_a_fault(capsys, ddir, root, keys, tmp_path, monkeypatch):
    from levain.authority import VerifierUnavailable

    init(capsys, ddir, root, keys)
    item, sig = _elsewhere(ddir, root, keys, tmp_path, "verifiers", VERIFIERS)

    def cannot_run(*a, **k):
        raise VerifierUnavailable("ssh-keygen could not run")

    monkeypatch.setattr(ctl, "verify_link_sig", cannot_run)
    code, out, _ = accept_cli(capsys, ddir, root, item, sig, "--yes")
    assert code == 3 and "could not do it" in out and "nothing was accepted" in out, out
    assert "refused" not in out


def test_an_unreadable_authority_log_at_init_is_a_fault(capsys, ddir, tmp_path, keys):
    bad = tmp_path / "bad-cockpit"
    bad.mkdir()
    (bad / "cockpit.db").write_bytes(b"this is not a database" * 100)
    code, out, _ = run(capsys, "config", "init", "--trust-root", keys["laptop"].fp, "--dispatch-dir", str(ddir),
                       "--data-root", str(bad))
    assert code == 3 and "could not do it" in out and "nothing was pinned" in out, out
    assert not (ddir / ctl.DB_NAME).exists()


def test_a_fault_after_the_item_committed_says_the_store_moved(capsys, ddir, root, keys, tmp_path, monkeypatch):
    configure(capsys, ddir, root, keys, tmp_path)
    real = ctl.ControlStore.accept

    def committed_then_faulted(self, item, signature):
        real(self, item, signature)
        raise ctl.ControlStateUnavailable("the control store cannot be read: disk I/O error")

    monkeypatch.setattr(ctl.ControlStore, "accept", committed_then_faulted)
    changed = dict(VERIFIERS)
    del changed["pytest-ip"]
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", changed, "--yes")
    assert code == 3 and "is now generation 4 (was 3)" in out, out
    assert "nothing was accepted" not in out
    assert store(ddir, root).generation() == 4


def test_a_store_fault_before_the_touch_says_nothing_was_signed(capsys, ddir, root, keys, tmp_path, no_touch):
    init(capsys, ddir, root, keys)
    (ddir / ctl.DB_NAME).write_bytes(b"this is not a database" * 100)
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", VERIFIERS, "--yes")
    assert code == 3 and "ControlStateUnavailable" in out and "nothing was signed" in out, out
    assert no_touch.calls == 0
    item =ctl.item_bytes("verifiers", 1, VERIFIERS, datetime.now(timezone.utc), prev="", store_id="0" * 32)
    (tmp_path / "item.json").write_bytes(item)
    (tmp_path / "item.sig").write_text("x")
    code, out, _ = accept_cli(capsys, ddir, root, str(tmp_path / "item.json"), str(tmp_path / "item.sig"), "--yes")
    assert code == 3 and "nothing was accepted" in out, out


@pytest.mark.parametrize("fault", [OSError("disk gone"), __import__("sqlite3").OperationalError("locked")])
def test_resolvers_says_nothing_was_filed_for_every_store_fault(capsys, ddir, root, keys, monkeypatch, fault):
    init(capsys, ddir, root, keys)

    def faulted(self):
        raise fault

    monkeypatch.setattr(ctl.ControlStore, "active", faulted)
    with pytest.raises(ctl.ControlStateUnavailable, match="nothing was filed"):
        dcli.resolvers(ddir, root)


def test_the_mcp_door_names_the_operators_fix_when_no_cockpit_is_named(capsys, ddir, root, keys, tmp_path, repo):
    from levain.dispatch import mcp
    configure(capsys, ddir, root, keys, tmp_path)
    path, base = repo
    sub = submission(path, base, expires_at=utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2)))
    reply = mcp.handle("codex", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "dispatch_file", "arguments": {"contract": sub}}},
                       directory=ddir)
    result = reply["result"]
    text = result["content"][0]["text"]
    assert result["isError"] and text.startswith("Refused, nothing changed"), text
    assert "--data-root" not in text and "cockpit_data_root" in text and "the operator" in text, text


@pytest.mark.parametrize("flag", ["--data-root", "--dispatch-dir"])
def test_file_refuses_a_gate_flag_under_an_agent(capsys, ddir, root, keys, tmp_path, repo, monkeypatch, flag):
    configure(capsys, ddir, root, keys, tmp_path)
    monkeypatch.setattr(dcli, "agent_caller", lambda: "claude_code")
    argv = ["file", _contract(tmp_path, repo), flag, root if flag == "--data-root" else str(ddir)]
    code, out, _ = run(capsys, *argv)
    assert code == 2 and flag in out and "claude_code" in out, out
    assert JobStore(ddir).list() == []


def test_show_escapes_a_homoglyph_in_the_summary(capsys, ddir, root, keys, tmp_path):
    configure(capsys, ddir, root, keys, tmp_path)
    routes = {"routes": [{"route": "openrouter/glм", "provider": "openrouter", "upstream": "https://x.invalid",
                          "credential": "keychain:or", "models": [{"requested": "glm-5", "backend": "z-ai/glm-5"}],
                          "body_policy": {}, "retention_claim": "none"}]}
    assert sign(capsys, ddir, root, keys, tmp_path, "routemap", routes, "--yes")[0] == 0
    code, out, _ = run(capsys, "config", "show", "--dispatch-dir", str(ddir), "--data-root", root)
    assert code == 0 and "openrouter/gl\\u043c" in out and "м" not in out, out


def test_the_confirm_summary_shows_the_head_it_builds_on(capsys, ddir, root, keys, tmp_path, monkeypatch):
    configure(capsys, ddir, root, keys, tmp_path)
    top, head = store(ddir, root).head()
    _tty(monkeypatch)
    changed = dict(VERIFIERS)
    del changed["pytest-ip"]
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", changed)
    assert code == 0, out
    summary = out[out.index("---- you are about to sign"):]
    assert f"builds on   generation {top}, {head}" in summary, summary
    top, head = store(ddir, root).head()
    item, sig = _elsewhere(ddir, root, keys, tmp_path, "verifiers", VERIFIERS)
    code, out, _ = accept_cli(capsys, ddir, root, item, sig)
    assert code == 0, out
    summary = out[out.index("---- you are about to accept"):]
    assert f"builds on   generation {top}, {head}" in summary, summary


# --- fix round 2 ------------------------------------------------------------------------------------------------------

def _move(ddir, root, keys, tmp_path, name="moved"):
    """Another item accepted into the store, as a second terminal would: the head moves by one."""
    d = tmp_path / name
    d.mkdir()
    changed = dict(VERIFIERS)
    changed.pop("pytest-ip", None)
    item, sig = _elsewhere(ddir, root, keys, d, "verifiers", changed)
    with open(item, "rb") as fh:
        raw = fh.read()
    with open(sig) as fh:
        store(ddir, root).accept(raw, fh.read())


def _touches(monkeypatch):
    calls = []
    real = dcli._sign_bytes

    def counted(sign, message):
        calls.append(message)
        return real(sign, message)

    monkeypatch.setattr(dcli, "_sign_bytes", counted)
    return calls


def test_sign_refuses_before_the_touch_when_the_store_moved_during_the_question(capsys, ddir, root, keys, tmp_path,
                                                                                 monkeypatch):
    configure(capsys, ddir, root, keys, tmp_path)
    touches = _touches(monkeypatch)
    monkeypatch.setattr(dcli, "_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: _move(ddir, root, keys, tmp_path) or "y")
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", VERIFIERS)
    assert code == 2 and "the store moved to generation 4 while you were confirming; nothing was signed" in out, out
    assert touches == [] and store(ddir, root).generation() == 4


def test_accept_refuses_when_the_store_moved_during_the_question(capsys, ddir, root, keys, tmp_path, monkeypatch):
    configure(capsys, ddir, root, keys, tmp_path)
    item, sig = _elsewhere(ddir, root, keys, tmp_path, "verifiers", VERIFIERS)
    monkeypatch.setattr(dcli, "_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: _move(ddir, root, keys, tmp_path) or "y")
    code, out, _ = accept_cli(capsys, ddir, root, item, sig)
    assert code == 2 and "the store moved to generation 4 while you were confirming; nothing was accepted" in out, out
    assert store(ddir, root).generation() == 4


def test_a_refusal_at_accept_after_the_touch_says_the_signature_is_spent(capsys, ddir, root, keys, tmp_path,
                                                                        monkeypatch):
    configure(capsys, ddir, root, keys, tmp_path)

    def refused(self, item, signature):
        raise ctl.ConfigRefused("the head moved under the item")

    monkeypatch.setattr(ctl.ControlStore, "accept", refused)
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", VERIFIERS, "--yes")
    assert code == 2, out
    assert ("signed but not accepted: the head moved under the item; this signature cannot be used, re-run config "
            "sign") in out, out


def test_a_signer_fault_reads_the_generation_again(capsys, ddir, root, keys, tmp_path, monkeypatch):
    configure(capsys, ddir, root, keys, tmp_path)

    def moved_then_faulted(sign_, message):
        _move(ddir, root, keys, tmp_path)
        raise dcli.SignerUnavailable("ssh-keygen timed out")

    monkeypatch.setattr(dcli, "_sign_bytes", moved_then_faulted)
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", VERIFIERS, "--yes")
    assert code == 3 and "the store is now generation 4 (was 3)" in out, out
    assert "still 3" not in out

    def unreadable_then_faulted(sign_, message):
        (ddir / ctl.DB_NAME).write_bytes(b"this is not a database" * 100)
        raise dcli.SignerUnavailable("ssh-keygen timed out")

    monkeypatch.setattr(dcli, "_sign_bytes", unreadable_then_faulted)
    code, out, _ = sign(capsys, ddir, root, keys, tmp_path, "verifiers", VERIFIERS, "--yes")
    assert code == 3 and "nothing was signed" in out and "could not be read again" in out, out
    assert "still" not in out


def test_accept_shows_the_head_check_judged(capsys, ddir, root, keys, tmp_path, monkeypatch):
    configure(capsys, ddir, root, keys, tmp_path)
    item, sig = _elsewhere(ddir, root, keys, tmp_path, "verifiers", VERIFIERS)
    real = ctl.ControlStore.check

    def checked_then_moved(self, item_, signature):
        parsed = real(self, item_, signature)
        _move(ddir, root, keys, tmp_path)
        return parsed

    monkeypatch.setattr(ctl.ControlStore, "check", checked_then_moved)
    code, out, _ = accept_cli(capsys, ddir, root, item, sig, "--yes")
    assert code == 2 and "the store moved to generation 4 after the item was checked; nothing was accepted" in out, out
    assert "the item" not in out.split("refused")[0]      # nothing was shown from the second read


def _no_cockpit_reply(ddir, repo, data_root=None):
    from levain.dispatch import mcp
    path, base = repo
    sub = submission(path, base, expires_at=utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2)))
    reply = mcp.handle("codex", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "dispatch_file", "arguments": {"contract": sub}}},
                       directory=ddir, data_root=data_root)
    result = reply["result"]
    assert result["isError"], result
    return result["content"][0]["text"]


def test_the_mcp_doors_cockpit_refusal_names_who_named_the_cockpit(capsys, ddir, root, keys, tmp_path, repo):
    configure(capsys, ddir, root, keys, tmp_path)
    empty = tmp_path / "no-cockpit"
    empty.mkdir()
    text = _no_cockpit_reply(ddir, repo, data_root=empty)
    assert "the --data-root flag" in text and "consent hooks" not in text and str(empty) in text, text
    text = _no_cockpit_reply(ddir, repo)
    assert "consent hooks" in text and "--data-root" not in text, text


def test_json_with_a_terminal_keeps_the_prompt_off_stdout(capsys, ddir, root, keys, tmp_path, monkeypatch):
    import sys
    init(capsys, ddir, root, keys)
    monkeypatch.setattr(dcli, "_is_tty", lambda: True)

    def real_input(prompt=""):
        # what input() does: the prompt goes to stdout
        sys.stdout.write(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", real_input)
    code, out, err = sign(capsys, ddir, root, keys, tmp_path, "verifiers", VERIFIERS, "--json")
    assert code == 0, out
    assert json.loads(out)["generation"] == 1
    assert "[y/N]" in err


def test_the_item_view_escapes_every_field_as_the_body_is_escaped():
    lines, _ = dcli._item_view("verifiersм", 2, "abcм", "abc", "sha256:м", {"k": "м"}, None)
    text = "\n".join(lines)
    assert text.isascii(), text
    assert "verifiers\\u043c" in text and "abc\\u043c" in text and "sha256:\\u043c" in text
