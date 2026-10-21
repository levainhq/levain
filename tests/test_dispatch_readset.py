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
    # a path that does not exist yet: build_readset creates it (R2)
    d = root / name
    assert not d.exists()
    return str(d)


def objects(repo: str | Path) -> list[tuple[str, str]]:
    out = git(Path(repo), "cat-file", "--batch-all-objects", "--batch-check").splitlines()
    return [(line.split()[0], line.split()[1]) for line in out]


@pytest.fixture(scope="module")
def built(world):
    root, repo, base, old_a, contract = world
    rs = build_readset(str(repo), contract, dest(root, "built"), contract_digest=contract.digest)
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
    again = build_readset(str(repo), contract, dest(root, "again"), contract_digest=contract.digest)
    assert (again.commit, again.tree) == (rs.commit, rs.tree)
    assert not os.path.exists(os.path.join(rs.repo_dir, "readset.index"))
    assert git(Path(rs.repo_dir), "log", "-1", "--format=%an %ae %at %s", BRANCH).strip() == \
        f"levain-dispatch levain-dispatch@localhost 0 levain read set {contract.digest}"


def refusal(fn, *a, **kw) -> ReadSetRefused:
    with pytest.raises(ReadSetRefused) as ei:
        fn(*a, **kw)
    return ei.value


def test_a_damaged_object_store_refuses_and_hands_nothing_on(tmp_path):
    # git hashes what it reads, so honest bytes always match their sha; a store whose loose object was overwritten is
    # the case the byte check guards (cat-file does not rehash a loose object, so the check is what refuses it)
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
    assert refusal(build_readset, str(repo), contract, d, contract_digest=contract.digest).name == \
        "readset-blob-mismatch"
    assert not os.path.exists(d)        # the partial scratch is removed: the same dest_dir works on a retry


def test_a_contract_edited_after_filing_refuses_on_the_filed_digest(world):
    # the forge L1 ran: a VALID regular-file entry naming the repo's real .env blob (NEVER under the policy, so filing
    # would have refused it). It parses to itself, so only the filed digest can tell it from the filed contract.
    root, repo, base, _, contract = world
    env_blob = git(repo, "rev-parse", f"{base}:.env").strip()
    forged = dataclasses.replace(contract, read=(PathEntry(".env", "100644", env_blob),))
    d = dest(root, "forged")
    assert refusal(build_readset, str(repo), forged, d, contract_digest=contract.digest).name == \
        "readset-digest-mismatch"
    assert not os.path.exists(d)                                    # refused before any git or filesystem work
    assert refusal(build_readset, str(repo), {"read": []}, dest(root, "notc"),
                   contract_digest=contract.digest).name == "readset-contract"


def test_a_build_needs_the_filed_digest(world):
    root, repo, _, _, contract = world
    for i, wrong in enumerate(("sha256:" + "0" * 64, contract.digest.upper(), "", None)):
        d = dest(root, f"wrong-{i}")
        assert refusal(build_readset, str(repo), contract, d, contract_digest=wrong).name == "readset-digest-mismatch"
    with pytest.raises(TypeError):
        build_readset(str(repo), contract, dest(root, "nodigest"))              # required, keyword-only
    rs = build_readset(str(repo), contract, dest(root, "right"), contract_digest=contract.digest)
    assert git(Path(rs.repo_dir), "ls-tree", "-r", "--name-only", rs.commit).split() == ["src/a.py", "src/run.sh"]


def test_the_repo_and_the_destination_are_checked(world):
    root, repo, _, _, contract = world
    kw = {"contract_digest": contract.digest}
    assert refusal(build_readset, str(root), contract, dest(root, "wrongrepo"), **kw).name == "readset-repo"
    full = root / "full"
    full.mkdir()
    (full / "x").write_text("x")
    assert refusal(build_readset, str(repo), contract, str(full), **kw).name == "readset-dest"
    assert os.listdir(full) == ["x"]                                       # an existing dir is never taken over
    empty = root / "empty"
    empty.mkdir()                                                          # an existing EMPTY dir too (R2)
    assert refusal(build_readset, str(repo), contract, str(empty), **kw).name == "readset-dest"
    assert refusal(build_readset, str(repo), contract, str(root / "missing" / "d"), **kw).name == "readset-dest"
    assert refusal(build_readset, str(repo), contract, "relative", **kw).name == "readset-dest"
    inside = repo / "scratch"
    assert refusal(build_readset, str(repo), contract, str(inside), **kw).name == "readset-dest"
    assert not inside.exists()                                             # created, judged, removed
    assert not (repo / ".git/refs/heads/readset").exists()                 # the operator's repo is untouched


def test_a_destination_inside_the_repo_by_a_symlinked_repo_path_refuses(tmp_path):
    # L2 LOW-2: filing refuses a symlinked repo path, but the path can become one after filing (the repo moved and a
    # link left in its place); a dest_dir inside the real target is still inside the operator's repo
    top = Path(os.path.realpath(tmp_path))
    repo = top / "repo"
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
    moved = top / "moved"
    repo.rename(moved)
    repo.symlink_to(moved)
    inside = moved / "scratch"
    assert refusal(build_readset, str(repo), contract, str(inside), contract_digest=contract.digest).name == \
        "readset-dest"
    assert not inside.exists()


def test_a_destination_that_cannot_be_created_is_a_refusal(world):
    root, repo, _, _, contract = world
    locked = root / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert refusal(build_readset, str(repo), contract, str(locked / "d"),
                       contract_digest=contract.digest).name == "readset-dest"
    finally:
        locked.chmod(0o700)


def test_the_destination_is_created_private(world):
    root, repo, _, _, contract = world
    d = dest(root, "private")
    build_readset(str(repo), contract, d, contract_digest=contract.digest)
    assert os.stat(d).st_mode & 0o777 == 0o700


def failing_cat_file(monkeypatch, before=lambda: None):
    import levain.dispatch.readset as rs_mod
    real = rs_mod._git

    def fake(cwd, *args, env, data=None):
        if args[0] == "cat-file":
            before()
            raise ReadSetRefused("readset-git-failed", "injected")
        return real(cwd, *args, env=env, data=data)
    monkeypatch.setattr(rs_mod, "_git", fake)


def test_a_failure_after_init_removes_the_destination_it_made(world, monkeypatch):
    root, repo, _, _, contract = world
    d = dest(root, "fail-after-init")
    failing_cat_file(monkeypatch)
    assert refusal(build_readset, str(repo), contract, d, contract_digest=contract.digest).name == "readset-git-failed"
    assert not os.path.exists(d)


def test_a_destination_swapped_for_a_symlink_is_not_followed_on_cleanup(world, monkeypatch):
    # codex r1 MED 3: the run showed 3aa2f740 emptied the symlink's target; only the directory this call made is removed
    root, repo, _, _, contract = world
    victim = root / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("not the scratch's\n")
    d = dest(root, "swapped")

    def swap():
        os.rename(d, str(root / "swapped.moved"))
        os.symlink(victim, d)
    failing_cat_file(monkeypatch, swap)
    assert refusal(build_readset, str(repo), contract, d, contract_digest=contract.digest).name == "readset-git-failed"
    assert (victim / "precious.txt").read_text() == "not the scratch's\n"
    assert os.path.islink(d)                                               # left; the refusal stands


def test_a_destination_under_the_repo_by_another_letter_case_refuses(world):
    # codex r1 MED 4: on a case-insensitive volume /x/Repo and /x/rEPO are one directory; identity, not spelling, decides
    root, repo, _, _, contract = world
    other = root / (repo.name.swapcase())
    if not other.exists():
        pytest.skip("case-sensitive filesystem")
    d = other / "scratch"
    assert refusal(build_readset, str(repo), contract, str(d), contract_digest=contract.digest).name == "readset-dest"
    assert not (repo / "scratch").exists()


def test_a_partial_clone_missing_a_read_blob_refuses_and_fetches_nothing(tmp_path, monkeypatch):
    # codex r1 HIGH 1: GIT_NO_LAZY_FETCH is read from git 2.44 only; with it removed (what older git does), the run showed
    # 3aa2f740 fetched the blob from the promisor remote. The empty GIT_ALLOW_PROTOCOL is the bound that refuses it.
    import levain.dispatch.readset as rs_mod
    top = Path(os.path.realpath(tmp_path))
    origin = top / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "src").mkdir()
    (origin / "src/a.py").write_text("a = 1\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "c")
    git(origin, "config", "uploadpack.allowFilter", "true")
    blob = git(origin, "rev-parse", "HEAD:src/a.py").strip()
    clone = top / "clone"
    git(top, "clone", "-q", "--filter=blob:none", "--no-checkout", f"file://{origin}", str(clone))
    sub = {"template": "fix-lint", "goal": "g", "read": ["src/a.py"], "write": ["src/a.py"],
           "base": git(clone, "rev-parse", "HEAD").strip(), "budget": {"tool_calls": 1, "tokens": 1, "dollars_cents": 1},
           "grant": ["Edit"], "expires_at": utc_stamp(NOW + timedelta(hours=1)), "repo": str(clone)}
    contract = resolve_contract(sub, resolve_template=lambda t: TEMPLATE, policy=POLICY, now=NOW)

    def present() -> bool:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_NO_LAZY_FETCH"] = "1"
        return subprocess.run(["git", "-C", str(clone), "cat-file", "-e", blob], env=env,
                              capture_output=True).returncode == 0
    assert not present()
    real = rs_mod._git
    seen = []

    def older_git(cwd, *args, env, data=None):
        if cwd == str(clone):
            env = {k: v for k, v in env.items() if k != "GIT_NO_LAZY_FETCH"}
            seen.append((env.get("GIT_ALLOW_PROTOCOL"), env.get("GIT_PROTOCOL_FROM_USER")))
        return real(cwd, *args, env=env, data=data)
    monkeypatch.setattr(rs_mod, "_git", older_git)
    d = dest(top, "d")
    assert refusal(build_readset, str(clone), contract, d, contract_digest=contract.digest).name == \
        "readset-git-failed"
    assert seen == [("", "0")]
    assert not present()                                                   # nothing was fetched
    assert not os.path.exists(d)
    monkeypatch.setattr(rs_mod, "_git", real)                              # as-is git (2.44+) refuses too
    assert refusal(build_readset, str(clone), contract, d, contract_digest=contract.digest).name == \
        "readset-git-failed"
    assert not present()


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
