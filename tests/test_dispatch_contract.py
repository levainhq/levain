"""F2 slice a: a task contract resolved from a REAL git repo at its base commit, refused whole or content-addressed.

Every repo here is a real ``git init`` in tmp with real commits (a symlink, an executable, a gitlink entry, a file
changed only in the working tree after the commit); nothing about git is mocked. The helpers below are shared with the
sign and job-store tests.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp
from levain.contracts.canon import canonical
from levain.dispatch import ContractRefused
from levain.dispatch.contract import (
    GOAL_MAX_BYTES, MAX_LIFETIME, NEW, ResolvedContract, ResolvedTemplate, parse_contract, resolve_contract,
)
from levain.dispatch.policy import ProviderPolicy

NOW = datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)


def git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo)})
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=env, timeout=30).stdout.strip()


def make_repo(root: Path) -> tuple[Path, str]:
    """A real repo: src/a.py, src/b.py, an executable, docs, a NEVER file, a symlinked file and directory, and a
    gitlink. Returns (repo, base sha)."""
    repo = Path(os.path.realpath(root)) / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src/a.py").write_text("a = 1\n")
    (repo / "src/b.py").write_text("b = 2\n")
    (repo / "src/run.sh").write_text("#!/bin/sh\n")
    os.chmod(repo / "src/run.sh", 0o755)
    (repo / "docs").mkdir()
    (repo / "docs/x.md").write_text("# x\n")
    (repo / "work").mkdir()
    (repo / "work/client.txt").write_text("secret\n")
    (repo / "README").write_text("r\n")
    os.symlink("a.py", repo / "src/alias.py")
    os.symlink("src", repo / "srclink")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "one")
    # a gitlink entry (a submodule as git records it), with no .gitmodules needed for ls-tree to report it
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{git(repo, 'rev-parse', 'HEAD')},vendor/sub")
    git(repo, "commit", "-q", "-m", "gitlink")
    base = git(repo, "rev-parse", "HEAD")
    # the working tree moves on after base: resolution must not see it
    (repo / "src/a.py").write_text("a = 'working tree'\n")
    (repo / "src/wt_only.py").write_text("x\n")
    return repo, base


WRITE_SET = frozenset({"src/a.py", "src/b.py", "src/new.py", "src/sub/deep.py", "src/run.sh", "src/alias.py",
                       "srclink/n.py", "vendor/sub/x.py", "README/x", "src/A.py", "src/wt_only.py", "work/out.txt",
                       "docs/x.md", "src"})


def template(**over) -> ResolvedTemplate:
    fields = dict(template_id="fix-lint", content_hash="sha256:" + "a" * 64, tier="R0", executor="codex",
                  models=("glm-5", "kimi-k2"), k=2, verifier_id="pytest-bb", verifier_hash="sha256:" + "b" * 64,
                  verifier_kind="black-box", write_paths=WRITE_SET, provider="openrouter", route="openrouter/glm",
                  tool_versions=(("codex", "0.50.0"), ("levain", "1.0.0")))
    fields.update(over)
    return ResolvedTemplate(**fields)


def registry(*templates: ResolvedTemplate):
    by_id = {t.template_id: t for t in templates} if templates else {"fix-lint": template()}
    return lambda tid: by_id.get(tid)


POLICY = ProviderPolicy.from_mapping({
    "never": ["work/**", ".env*", "**/*.pem"],
    "providers": {"openrouter": {"read": ["src/**", "docs/**", "README", "srclink/**", "vendor/**", "README/**"]},
                  "anthropic": {"read": ["**"]}},
})


def submission(repo: Path, base: str, **over) -> dict:
    s = {"template": "fix-lint", "goal": "Fix the lint in a.py.\n\tKeep b as is.", "read": ["src/b.py", "src/a.py"],
         "write": ["src/a.py", "src/new.py"], "base": base,
         "budget": {"tool_calls": 40, "tokens": 200000, "dollars_cents": 150}, "grant": ["Edit", "Write"],
         "expires_at": utc_stamp(NOW + timedelta(hours=2)), "repo": str(repo)}
    s.update(over)
    return s


def resolve(sub, *, resolver=None, policy=POLICY, now=NOW) -> ResolvedContract:
    return resolve_contract(sub, resolve_template=resolver or registry(), policy=policy, now=now)


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("dispatch-repo"))


# --- the happy path ------------------------------------------------------------------------------------------------

def test_resolves_from_git_at_base_not_the_working_tree(repo):
    path, base = repo
    c = resolve(submission(path, base))
    a_blob = git(path, "rev-parse", f"{base}:src/a.py")
    assert [e.to_mapping() for e in c.read] == [{"path": "src/a.py", "mode": "100644", "blob": a_blob},
                                                {"path": "src/b.py", "mode": "100644",
                                                 "blob": git(path, "rev-parse", f"{base}:src/b.py")}]
    assert c.write[0].blob == a_blob and not c.write[0].new          # the committed blob, not 'working tree'
    assert c.write[1].path == "src/new.py" and c.write[1].new and c.write[1].blob == NEW
    assert c.grant == ("Edit", "Write") and c.base == base and c.repo == str(path)
    assert c.template == template() and c.policy_hash == POLICY.content_hash()
    assert c.digest.startswith("sha256:") and len(c.digest) == 71


def test_an_executable_is_a_regular_file_and_its_mode_is_bound(repo):
    path, base = repo
    c = resolve(submission(path, base, read=["src/run.sh"], write=["src/run.sh"]))
    assert c.read[0].mode == "100755" and c.write[0].mode == "100755"


def test_parse_round_trips_byte_for_byte(repo):
    path, base = repo
    c = resolve(submission(path, base))
    text = c.canonical()
    back = parse_contract(text)
    assert back == c and back.canonical() == text and back.digest == c.digest
    assert parse_contract(text.encode("utf-8")) == c


def test_digest_is_stable_across_input_ordering(repo):
    path, base = repo
    one = resolve(submission(path, base))
    sub = submission(path, base, read=["src/a.py", "src/b.py"], write=["src/new.py", "src/a.py"],
                     grant=["Write", "Edit"], budget={"dollars_cents": 150, "tokens": 200000, "tool_calls": 40})
    two = resolve(dict(reversed(list(sub.items()))))
    assert one.digest == two.digest and one.canonical() == two.canonical()


def test_models_keep_template_order(repo):
    path, base = repo
    c = resolve(submission(path, base), resolver=registry(template(models=("kimi-k2", "glm-5"))))
    assert json.loads(c.canonical())["template"]["models"] == ["kimi-k2", "glm-5"]
    assert c.digest != resolve(submission(path, base)).digest


@pytest.mark.parametrize("change", [
    lambda m: m.update(goal=m["goal"] + "!"),
    lambda m: m.update(base="0" * 40),
    lambda m: m["read"][0].update(blob="0" * 40),
    lambda m: m["write"][1].update(mode="100644", blob="1" * 40),
    lambda m: m["budget"].update(tokens=200001),
    lambda m: m.update(grant=["Edit"]),
    lambda m: m.update(expires_at=utc_stamp(NOW + timedelta(hours=3))),
    lambda m: m.update(repo="/elsewhere"),
    lambda m: m.update(policy_hash="sha256:" + "c" * 64),
    lambda m: m["template"].update(content_hash="sha256:" + "d" * 64),
    lambda m: m["template"].update(k=1, tier="R1"),
    lambda m: m["template"].update(verifier_hash="sha256:" + "e" * 64),
    lambda m: m["template"].update(verifier_kind="in-process"),
    lambda m: m["template"].update(models=["glm-5", "kimi-k2", "qwen"]),
    lambda m: m["template"].update(route="openrouter/kimi"),
    lambda m: m["template"].update(tool_versions=[["codex", "0.51.0"], ["levain", "1.0.0"]]),
    lambda m: m["template"].update(write_paths=sorted(WRITE_SET | {"src/c.py"})),
])
def test_tampering_any_bound_field_changes_the_digest(repo, change):
    path, base = repo
    c = resolve(submission(path, base))
    m = json.loads(c.canonical())
    change(m)
    tampered = parse_contract(canonical(m))
    assert tampered.digest != c.digest


# --- refusals: shape -----------------------------------------------------------------------------------------------

def refused(fixture, match, /, **over):
    path, base = fixture
    sub = submission(path, base)
    sub.update(over)
    with pytest.raises(ContractRefused, match=match):
        resolve(sub)


def test_missing_or_extra_keys_refuse(repo):
    path, base = repo
    sub = submission(path, base)
    del sub["grant"]
    with pytest.raises(ContractRefused, match="a contract is exactly"):
        resolve(sub)
    with pytest.raises(ContractRefused, match="a contract is exactly"):
        resolve({**submission(path, base), "tier": "R0"})       # a planner never names its own tier


@pytest.mark.parametrize("grant", ["Bash", "bash", "Shell", "exec_command", "local_shell", "run_shell_command",
                                   "sh", "PowerShell"])
def test_bash_and_every_shellish_class_is_never_grantable(repo, grant):
    refused(repo, "Bash is never grantable", grant=["Edit", grant])


def test_unknown_tool_classes_and_duplicates_refuse(repo):
    refused(repo, "not a grantable tool class", grant=["WebFetch"])
    refused(repo, "twice", grant=["Edit", "Edit"])
    refused(repo, "grant must be a list", grant="Edit")


@pytest.mark.parametrize("budget, match", [
    ({"tool_calls": 1.0, "tokens": 1, "dollars_cents": 1}, "positive integer"),
    ({"tool_calls": 0, "tokens": 1, "dollars_cents": 1}, "positive integer"),
    ({"tool_calls": True, "tokens": 1, "dollars_cents": 1}, "positive integer"),
    ({"tool_calls": 1, "tokens": 2 ** 60, "dollars_cents": 1}, "positive integer"),
    ({"tool_calls": 1, "tokens": 1}, "budget is exactly"),
    ({"tool_calls": 1, "tokens": 1, "dollars_cents": 1, "dollars": 1}, "budget is exactly"),
])
def test_budget_refusals(repo, budget, match):
    refused(repo, match, budget=budget)


@pytest.mark.parametrize("goal, match", [
    ("", "non-empty"), ("   \n", "non-empty"), (7, "non-empty"),
    ("fix\x1b[2Jit", "U\\+001B"), ("fix‮it", "U\\+202E"), ("fix it", "U\\+2028"),
    ("fix it", "U\\+00A0"), ("fixit", "U\\+E000"), ("fix͸it", "U\\+0378"), ("fix\x00it", "U\\+0000"),
    ("x" * (GOAL_MAX_BYTES + 1), "bytes"),
])
def test_goal_text_refusals(repo, goal, match):
    refused(repo, match, goal=goal)


@pytest.mark.parametrize("when, match", [
    (utc_stamp(NOW), "not in the future"),
    (utc_stamp(NOW + MAX_LIFETIME + timedelta(seconds=1)), "never open-ended"),
    ("2026-10-19T22:00:00Z", "UTC stamp"),
    (None, "UTC stamp"),
])
def test_expiry_refusals(repo, when, match):
    refused(repo, match, expires_at=when)


@pytest.mark.parametrize("paths, match", [
    (["/etc/passwd"], "absolute"),
    (["src/../work/client.txt"], "not normalised"),
    (["./src/a.py"], "not normalised"),
    (["src//a.py"], "not normalised"),
    (["src/"], "not normalised"),
    (["src/*.py"], "glob"),
    (["src/a?.py"], "glob"),
    (["src/[ab].py"], "glob"),
    ([""], "non-empty"),
    (["src/a\x00.py"], "NUL"),
    (["src/a.py", "src/a.py"], "twice"),
    (["src/a.py", "src/A.py"], "only in case"),
    ([".git/hooks/pre-commit"], ".git"),
    (["src/.GIT/x"], ".git"),
    (["src/café.py"], "NFC"),
    (["src/a‮.py"], "U\\+202E"),
    ("src/a.py", "must be a list"),
])
def test_path_refusals(repo, paths, match):
    refused(repo, match, read=paths)


def test_an_empty_write_set_refuses(repo):
    refused(repo, "write names no file", write=[])


# --- refusals: git at base -----------------------------------------------------------------------------------------

def test_a_symlinked_read_file_refuses(repo):
    refused(repo, "src/alias.py is a symlink at base", read=["src/alias.py"])


def test_a_read_through_a_symlinked_directory_is_missing_at_base(repo):
    refused(repo, "srclink/a.py does not exist at base", read=["srclink/a.py"])


def test_a_gitlink_refuses(repo):
    refused(repo, "submodule", read=["vendor/sub"])


def test_a_missing_read_path_refuses_even_if_the_working_tree_has_it(repo):
    refused(repo, "src/wt_only.py does not exist at base", read=["src/wt_only.py"])


def test_a_directory_refuses(repo):
    refused(repo, "directory at base", read=["src"])
    refused(repo, "directory at base", write=["src"])


def test_a_symlinked_write_file_refuses(repo):
    refused(repo, "src/alias.py is a symlink", write=["src/alias.py"])


@pytest.mark.parametrize("new, match", [
    ("srclink/n.py", "parent srclink is a symlink"),
    ("vendor/sub/x.py", "parent vendor/sub is a submodule"),
    ("README/x", "parent README is a file"),
    ("src/A.py", "differs only in case from src/a.py"),
])
def test_a_new_write_path_under_a_bad_parent_refuses(repo, new, match):
    refused(repo, match, write=[new])


def test_a_new_file_in_a_new_directory_is_fine(repo):
    path, base = repo
    c = resolve(submission(path, base, write=["src/sub/deep.py"]))
    assert c.write[0].new


def test_a_new_write_path_that_exists_only_in_the_working_tree_is_new(repo):
    path, base = repo
    assert resolve(submission(path, base, write=["src/wt_only.py"])).write[0].new


def test_base_must_be_a_full_commit_sha_that_resolves(repo):
    path, base = repo
    refused(repo, "full 40-hex", base=base[:12])
    refused(repo, "full 40-hex", base=base.upper())
    refused(repo, "full 40-hex", base="main")
    refused(repo, "rev-parse failed", base="0" * 40)
    blob = git(path, "rev-parse", f"{base}:src/a.py")
    refused(repo, "rev-parse failed", base=blob)                  # a blob is not a commit


def test_repo_refusals(repo, tmp_path):
    path, _base = repo
    refused(repo, "absolute path", repo="relative/repo")
    refused(repo, "normal form", repo=str(path) + "/")
    refused(repo, "not the top", repo=str(path / "src"))
    refused(repo, "not a directory", repo=str(tmp_path / "nope"))
    link = tmp_path / "link"
    os.symlink(path, link)
    refused(repo, "symlink", repo=str(link))
    plain = Path(os.path.realpath(tmp_path)) / "plain"
    plain.mkdir()
    refused(repo, "git rev-parse failed", repo=str(plain))


def test_git_environment_cannot_redirect_resolution(repo, monkeypatch, tmp_path):
    path, base = repo
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere.git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "idx"))
    assert resolve(submission(path, base)).base == base


# --- refusals: template and data boundary --------------------------------------------------------------------------

def test_an_unknown_template_is_r2_and_never_dispatched(repo):
    refused(repo, "not registered; the contract is R2, and R2 is never dispatched", template="nope")
    refused(repo, "R2 is never dispatched", template="Bad Id")


def test_a_write_outside_the_template_is_r2(repo):
    path, base = repo
    small = registry(template(write_paths=frozenset({"src/a.py"})))
    with pytest.raises(ContractRefused, match="outside template fix-lint's write set.*R2 is never dispatched"):
        resolve(submission(path, base), resolver=small)


@pytest.mark.parametrize("over, match", [
    (dict(tier="R2"), "R2 is never a template"),
    (dict(tier="R0", k=1), "R0 template needs k >= 2"),
    (dict(k=0), "k must be an integer >= 1"),
    (dict(k=True), "k must be an integer"),
    (dict(models=("glm-5", "glm-5")), "distinct"),
    (dict(models=("glm-5",)), "fewer than k"),
    (dict(models=["glm-5", "kimi-k2"]), "tuple of model names"),
    (dict(verifier_kind="trust-me"), "verifier kind"),
    (dict(content_hash="abc"), "content_hash"),
    (dict(verifier_hash="sha256:" + "A" * 64), "verifier_hash"),
    (dict(write_paths={"src/a.py"}), "frozenset"),
    (dict(write_paths=frozenset({"src/*.py"})), "glob"),
    (dict(tool_versions=(("levain", "1"), ("codex", "1"))), "sorted"),
    (dict(tool_versions=(("codex", "1"), ("codex", "2"))), "each tool once"),
    (dict(tool_versions=(("codex",),)), "string pairs"),
    (dict(executor=""), "executor"),
    (dict(template_id="other"), "returned template other"),
])
def test_the_templates_own_invariants_are_checked(repo, over, match):
    path, base = repo
    with pytest.raises(ContractRefused, match=match):
        resolve(submission(path, base), resolver=lambda tid: template(**over))


def test_a_resolver_returning_a_non_template_refuses(repo):
    path, base = repo
    with pytest.raises(ContractRefused, match="other than a ResolvedTemplate"):
        resolve(submission(path, base), resolver=lambda tid: {"template_id": tid})


def test_the_data_boundary_refuses_whole(repo):
    refused(repo, "data boundary: work/client.txt is on the NEVER list", read=["src/a.py", "work/client.txt"])
    refused(repo, "data boundary: work/out.txt is on the NEVER list", write=["work/out.txt"])
    path, base = repo
    narrow = ProviderPolicy.from_mapping({"never": [], "providers": {"openrouter": {"read": ["src/**"]}}})
    with pytest.raises(ContractRefused, match="docs/x.md is outside what provider openrouter may read"):
        resolve(submission(path, base, read=["src/a.py", "docs/x.md"]), policy=narrow)
    other = registry(template(provider="ollama-cloud"))
    with pytest.raises(ContractRefused, match="not in the provider policy"):
        resolve(submission(path, base), resolver=other)


def test_a_non_policy_refuses(repo):
    path, base = repo
    with pytest.raises(ContractRefused, match="ProviderPolicy"):
        resolve(submission(path, base), policy={"never": [], "providers": {}})


def test_a_naive_clock_refuses(repo):
    path, base = repo
    with pytest.raises(ContractRefused, match="naive"):
        resolve(submission(path, base), now=NOW.replace(tzinfo=None))


# --- parse refusals ------------------------------------------------------------------------------------------------

def test_parse_refuses_non_canonical_text(repo):
    path, base = repo
    text = resolve(submission(path, base)).canonical()
    m = json.loads(text)
    with pytest.raises(ContractRefused, match="canonical"):
        parse_contract(json.dumps(m, indent=1, sort_keys=True))
    with pytest.raises(ContractRefused, match="does not parse"):
        parse_contract(text.replace('"tokens":200000', '"tokens":200000.0'))
    with pytest.raises(ContractRefused, match="does not parse"):
        parse_contract(text[:-1] + ',"goal":"x"}')                 # a duplicate key


@pytest.mark.parametrize("change, match", [
    (lambda m: m.update(format="levain-dispatch-contract/2"), "format"),
    (lambda m: m.pop("policy_hash"), "not a dispatch contract"),
    (lambda m: m.update(read=list(reversed(m["read"]))), "sorted"),
    (lambda m: m["read"][0].update(mode=NEW, blob=NEW), "regular file"),
    (lambda m: m["write"][1].update(blob="0" * 40), "a new file's blob"),
    (lambda m: m["write"].append({"path": "zz/other.py", "mode": NEW, "blob": NEW}), "outside the template"),
    (lambda m: m["template"].update(tier="R2"), "R2 is never a template"),
    (lambda m: m.update(grant=["Bash"]), "Bash is never grantable"),
    (lambda m: m.update(base="main"), "40-hex"),
    (lambda m: m["template"].update(write_paths=[{"x": 1}]), "unhashable"),
])
def test_parse_checks_shape(repo, change, match):
    path, base = repo
    m = json.loads(resolve(submission(path, base)).canonical())
    change(m)
    with pytest.raises(ContractRefused, match=match):
        parse_contract(canonical(m))


def test_resolved_contract_is_frozen(repo):
    path, base = repo
    c = resolve(submission(path, base))
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.goal = "other"                                              # type: ignore[misc]
