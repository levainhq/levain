"""W2-d: the read set made physical (``levain.dispatch.readset``), from the cases the recorded run showed.

Every repo is a real ``git init`` with real history, a real contract filed through ``resolve_contract``, and the slot's
own ``make_bundle``; nothing about git is mocked.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp
from levain.dispatch.contract import PathEntry, ResolvedTemplate, resolve_contract
from levain.dispatch.policy import ProviderPolicy
from levain.dispatch.readset import BRANCH, ReadSet, ReadSetRefused, build_readset
from levain.slot.stage import make_bundle
from tests.test_dispatch_verifier_report import ENTRY as VERIFIER_ENTRY

NOW = datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)
POLICY = ProviderPolicy.from_mapping({"never": ["work/**", "**/.env*"],
                                      "providers": {"openrouter": {"read": ["src/**"]}}})
TEMPLATE = ResolvedTemplate(
    template_id="fix-lint", content_hash="sha256:" + "a" * 64, tier="R0", executor="codex", models=("glm-5", "kimi-k2"),
    k=2, verifier_id="pytest-bb", verifier_hash=VERIFIER_ENTRY["verifier_artifact_digest"], verifier_kind="black-box",
    write_paths=frozenset({"src/a.py", "src/new.py"}), provider="openrouter", route="openrouter/glm",
    tool_versions=(("codex", "0.50.0"), ("levain", "1.0.0")))


def git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"})
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=env, timeout=30).stdout


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = Path(os.path.realpath(tmp_path_factory.mktemp("readset")))
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src/a.py").write_text("a = 0\n")
    (repo / ".env").write_text("SECRET=1\n")
    (repo / "unrelated.txt").write_text("nope\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c1")
    old_a = git(repo, "rev-parse", "HEAD:src/a.py").strip()
    (repo / "src/a.py").write_text("a = 1\n")
    (repo / "src/run.sh").write_text("#!/bin/sh\necho hi\n")
    os.chmod(repo / "src/run.sh", 0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c2")
    (repo / "src/b.py").write_text("b = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c3")
    base = git(repo, "rev-parse", "HEAD").strip()
    sub = {"template": "fix-lint", "goal": "fix a", "read": ["src/a.py", "src/run.sh"],
           "write": ["src/a.py", "src/new.py"], "base": base,
           "budget": {"tool_calls": 10, "tokens": 1000, "dollars_cents": 10}, "grant": ["Edit"],
           "expires_at": utc_stamp(NOW + timedelta(hours=2)), "repo": str(repo)}
    contract = resolve_contract(sub, resolve_template=lambda t: TEMPLATE, policy=POLICY, now=NOW)
    return root, repo, base, old_a, contract


def dest(root: Path, name: str) -> str:
    d = root / name
    d.mkdir()
    return str(d)


def objects(repo: str | Path) -> list[tuple[str, str]]:
    out = git(Path(repo), "cat-file", "--batch-all-objects", "--batch-check").splitlines()
    return [(line.split()[0], line.split()[1]) for line in out]


@pytest.fixture(scope="module")
def built(world):
    root, repo, base, old_a, contract = world
    rs = build_readset(str(repo), contract, dest(root, "built"))
    bundle = root / "built.bundle"
    make_bundle(rs.repo_dir, rs.branch, bundle)
    clone = root / "built-clone"
    subprocess.run(["git", "clone", "-q", str(bundle), str(clone)], check=True, capture_output=True)
    return rs, clone


def test_the_clone_holds_exactly_the_read_paths_with_their_modes(built):
    rs, clone = built
    files = sorted(p.relative_to(clone).as_posix() for p in clone.rglob("*")
                   if p.is_file() and ".git" not in p.relative_to(clone).parts)
    assert files == ["src/a.py", "src/run.sh"]
    assert os.access(clone / "src/run.sh", os.X_OK) and not os.access(clone / "src/a.py", os.X_OK)
    assert (clone / "src/a.py").read_text() == "a = 1\n"          # base's blob, not the first commit's


def test_one_parentless_commit_and_no_other_file_or_history(world, built):
    _, repo, base, old_a, _ = world
    rs, clone = built
    assert isinstance(rs, ReadSet) and rs.branch == BRANCH
    assert git(clone, "log", "--format=%H").split() == [rs.commit]
    assert git(clone, "rev-list", "--parents", "-n1", "HEAD").split() == [rs.commit]
    assert not (clone / ".env").exists() and not (clone / "unrelated.txt").exists()
    for repo_dir in (rs.repo_dir, clone):
        objs = objects(repo_dir)
        kinds = [k for _, k in objs]
        assert (kinds.count("blob"), kinds.count("tree"), kinds.count("commit"), len(objs)) == (2, 2, 1, 5)
        shas = {s for s, _ in objs}
        assert old_a not in shas                                              # history's old blob
        assert git(repo, "rev-parse", f"{base}:src/b.py").strip() not in shas  # tracked, never read
        assert git(repo, "rev-parse", f"{base}:.env").strip() not in shas


def test_a_rebuild_names_the_same_commit_and_leaves_no_index(world, built):
    root, repo, _, _, contract = world
    rs, _ = built
    again = build_readset(str(repo), contract, dest(root, "again"))
    assert (again.commit, again.tree) == (rs.commit, rs.tree)
    assert not os.path.exists(os.path.join(rs.repo_dir, "readset.index"))
    assert git(Path(rs.repo_dir), "log", "-1", "--format=%an %ae %at %s", BRANCH).strip() == \
        f"levain-dispatch levain-dispatch@localhost 0 levain read set {contract.digest}"


def refusal(fn, *a) -> ReadSetRefused:
    with pytest.raises(ReadSetRefused) as ei:
        fn(*a)
    return ei.value


def test_a_damaged_object_store_refuses_and_hands_nothing_on(tmp_path):
    # git hashes what it reads, so honest bytes always match their sha; a store whose loose object was overwritten is
    # the case the byte check guards, and either git or the check refuses it by a readset name
    import zlib
    repo = Path(os.path.realpath(tmp_path)) / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src/a.py").write_text("a = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c")
    sub = {"template": "fix-lint", "goal": "g", "read": ["src/a.py"], "write": ["src/a.py"],
           "base": git(repo, "rev-parse", "HEAD").strip(), "budget": {"tool_calls": 1, "tokens": 1, "dollars_cents": 1},
           "grant": ["Edit"], "expires_at": utc_stamp(NOW + timedelta(hours=1)), "repo": str(repo)}
    contract = resolve_contract(sub, resolve_template=lambda t: TEMPLATE, policy=POLICY, now=NOW)
    blob = contract.read[0].blob
    loose = repo / ".git/objects" / blob[:2] / blob[2:]
    loose.chmod(0o644)
    loose.write_bytes(zlib.compress(b"blob 6\x00a = 2\n"))
    d = dest(tmp_path, "damaged")
    assert refusal(build_readset, str(repo), contract, d).name in ("readset-blob-mismatch", "readset-git-failed")
    assert not os.path.exists(os.path.join(d, "readset.index"))


def test_a_contract_edited_after_filing_refuses(world):
    root, repo, _, _, contract = world
    forged = dataclasses.replace(contract, read=(PathEntry("src/a.py", "120000", "0" * 40),))
    assert refusal(build_readset, str(repo), forged, dest(root, "forged")).name == "readset-contract"
    assert refusal(build_readset, str(repo), {"read": []}, dest(root, "notc")).name == "readset-contract"


def test_the_repo_and_the_destination_are_checked(world):
    root, repo, _, _, contract = world
    assert refusal(build_readset, str(root), contract, dest(root, "wrongrepo")).name == "readset-repo"
    full = dest(root, "full")
    Path(full, "x").write_text("x")
    assert refusal(build_readset, str(repo), contract, full).name == "readset-dest"
    assert refusal(build_readset, str(repo), contract, str(root / "missing")).name == "readset-dest"
    assert refusal(build_readset, str(repo), contract, "relative").name == "readset-dest"
    inside = repo / "scratch"
    inside.mkdir()
    assert refusal(build_readset, str(repo), contract, str(inside)).name == "readset-dest"
    assert not (repo / ".git/refs/heads/readset").exists()                 # the operator's repo is untouched


def test_the_dispatch_session_env_has_no_operator_key_and_roots_under_the_slot():
    from levain.slot.render import render_env
    from tests.test_slot_s2b_render import egress
    blob, facts = egress(vendor="codex", kind="dispatch", auth="brokered")
    env = render_env(blob, facts)["slot"]
    S = str(facts.S)
    assert not [k for k in env if any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET", "PASSWORD"))]
    assert env["HOME"] == S or env["HOME"].startswith(S + "/")
    for k in ("TMPDIR", "XDG_DATA_HOME", "XDG_CACHE_HOME", "CODEX_HOME", "GIT_CONFIG_GLOBAL"):
        assert env[k].startswith(S)
