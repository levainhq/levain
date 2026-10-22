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
from levain.dispatch.readset import BRANCH, ReadSet, ReadSetRefused, build_readset, readset_commit
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


#: P2's shapes plus a directory that prefixes sibling blobs, one blob at two paths and two identical subtrees
SHAPES = {"src/lib/x.py": "x\n", "src/lib.go": "go\n", "src/lib-a": "la\n", "src/d1.txt": "same\n",
          "src/d2.txt": "same\n", "src/t1/f.py": "f\n", "src/t2/f.py": "f\n"}


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
    for rel, body in SHAPES.items():         # tracked at base, read only by the shapes contract
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c3")
    base = git(repo, "rev-parse", "HEAD").strip()
    sub = {"template": "fix-lint", "goal": "fix a", "read": ["src/a.py", "src/run.sh"],
           "write": ["src/a.py", "src/new.py"], "base": base,
           "budget": {"tool_calls": 10, "tokens": 1000, "dollars_cents": 10}, "grant": ["Edit"],
           "expires_at": utc_stamp(NOW + timedelta(hours=2)), "repo": str(repo)}
    contract = resolve_contract(sub, resolve_template=lambda t: TEMPLATE, policy=POLICY, now=NOW)
    return root, repo, base, old_a, contract


def json_sub(c) -> dict:
    # the submission a contract was resolved from
    return {"template": "fix-lint", "goal": c.goal, "read": [e.path for e in c.read], "write": [e.path for e in c.write],
            "base": c.base, "budget": dict(c.budget), "grant": list(c.grant), "expires_at": c.expires_at,
            "repo": c.repo}


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
    # the pure readset_commit agrees with git on every build here, over the shapes too (a 100755 entry among them)
    shapes = resolve_contract({**json_sub(contract), "read": sorted([*SHAPES, "src/run.sh"])},
                              resolve_template=lambda t: TEMPLATE, policy=POLICY, now=NOW)
    for i, c in enumerate((contract, shapes)):
        got = rs if i == 0 else build_readset(str(repo), c, dest(root, "built-shapes"), contract_digest=c.digest)
        E, tree, N = readset_commit(c.canonical().encode("utf-8"), c.digest)
        assert (E, tree, N) == (got.commit, got.tree, len(objects(got.repo_dir)))
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
        env.update(GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="", GIT_PROTOCOL_FROM_USER="0")  # never fetches on any git
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


def test_the_attempt_id_validator_is_exactly_controls_grammar(world, tmp_path):
    # reproduced (wa_repro.py 1): with no validator, attempt "x/../../escaped" made its workdir outside the parent
    from levain.dispatch.attempt_id import ATTEMPT_ID_FORM, check_attempt_id, is_attempt_id, new_attempt_id
    from levain.dispatch.readset import verify_readset, workdir_name
    minted = new_attempt_id()
    assert ATTEMPT_ID_FORM.fullmatch(minted)
    for ok in ("a", "a" * 64, minted, "0-9"):
        assert is_attempt_id(ok) and check_attempt_id(ok) == ok and workdir_name(ok) == "w-" + ok
    for bad in ("", "a" * 65, "-a", "A", "aB", "a_b", "a.b", "a/b", "..", "a\n", "a\0", "x/../../escaped", b"a", None):
        assert not is_attempt_id(bad)
        assert refusal(workdir_name, bad).name == "readset-attempt"
    parent = Path(os.path.realpath(tmp_path)) / "attempts"
    (parent / "w-x").mkdir(parents=True)
    contract = world[4]
    cb = contract.canonical().encode("utf-8")
    assert refusal(verify_readset, cb, contract.digest, -1, str(parent), "x/../../escaped").name == "readset-attempt"
    assert sorted(os.listdir(parent)) == ["w-x"] and not (Path(os.path.realpath(tmp_path)) / "escaped").exists()


def test_verify_refuses_a_read_set_that_loosens_the_settings_by_name(tmp_path):
    # reproduced (wa_repro.py 2): with the repo-layer check removed, this read set verified
    from levain.dispatch.attempt_id import new_attempt_id
    from levain.dispatch.readset import _bundle_into, _scratch_env, verify_readset
    top = Path(os.path.realpath(tmp_path))
    repo = top / "r"
    (repo / ".claude").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / ".claude/settings.json").write_text('{"disableAllHooks": true}')
    (repo / "src/a.py").write_text("a\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c")
    sub = {"template": "fix-lint", "goal": "g", "read": [".claude/settings.json", "src/a.py"], "write": ["src/a.py"],
           "base": git(repo, "rev-parse", "HEAD").strip(), "budget": {"tool_calls": 1, "tokens": 1, "dollars_cents": 1},
           "grant": ["Edit"], "expires_at": utc_stamp(NOW + timedelta(hours=1)), "repo": str(repo)}
    policy = ProviderPolicy.from_mapping({"never": [], "providers": {"openrouter": {"read": ["**"]}}})
    c = resolve_contract(sub, resolve_template=lambda t: TEMPLATE, policy=policy, now=NOW)
    rs = build_readset(str(repo), c, dest(top, "scratch"), contract_digest=c.digest)
    bundle = top / "h.bundle"
    fd = os.open(bundle, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _bundle_into(rs.repo_dir, fd, _scratch_env())
    finally:
        os.close(fd)
    parent = top / "attempts"
    parent.mkdir()
    fd = os.open(bundle, os.O_RDONLY)
    try:
        got = refusal(verify_readset, c.canonical().encode("utf-8"), c.digest, fd, str(parent), new_attempt_id())
    finally:
        os.close(fd)
    assert got.name == "loosening:disable-all-hooks"
    assert os.listdir(parent) == []                                    # the refused attempt's workdir is gone


def test_a_read_set_over_the_handoff_cap_is_refused_at_filing(world, tmp_path, monkeypatch):
    # reproduced (wa-fix1 repro.py R2): an over-cap read set was exported for signing and refused only at the run
    import levain.slot.stage as stage
    from levain.dispatch.readset import TMP_DIR, export_readset
    _root, repo, _base, _old_a, contract = world
    top = Path(os.path.realpath(tmp_path))
    rs = build_readset(str(repo), contract, dest(top, "scratch"), contract_digest=contract.digest)
    handoff = top / "handoff"
    handoff.mkdir(mode=0o700)
    monkeypatch.setattr(stage, "BUNDLE_CAP", 64)
    got = refusal(export_readset, rs, contract.canonical().encode("utf-8"), contract.digest, str(handoff))
    assert got.name == "readset-handoff"
    assert os.listdir(handoff / TMP_DIR) == []                         # the temp was unlinked


def test_a_failed_staging_flush_is_named_and_leaves_no_bundle(world, tmp_path, monkeypatch):
    # reproduced (wa-fix1 repro.py R7): EIO on the staging flush escaped raw and left repo.bundle behind, so the next
    # cut into that staging was refused readset-dest
    import errno
    import stat

    from levain.dispatch import readset as R
    from levain.dispatch.attempt_id import new_attempt_id
    _root, repo, _base, _old_a, contract = world
    top = Path(os.path.realpath(tmp_path))
    cb, d = contract.canonical().encode("utf-8"), contract.digest
    rs = build_readset(str(repo), contract, dest(top, "scratch"), contract_digest=d)
    for name in ("handoff", "attempts", "staging"):
        (top / name).mkdir(mode=0o700)
    ex = R.export_readset(rs, cb, d, str(top / "handoff"))
    try:
        fd = R.open_handoff(ex.path, size=ex.size)
    finally:
        os.close(ex.fd)
    try:
        v = R.verify_readset(cb, d, fd, str(top / "attempts"), new_attempt_id())
    finally:
        os.close(fd)
    real = R.full_fsync

    def eio_on_dirs(f: int) -> str:
        if stat.S_ISDIR(os.fstat(f).st_mode):
            raise OSError(errno.EIO, "injected")
        return real(f)

    monkeypatch.setattr(R, "full_fsync", eio_on_dirs)
    got = refusal(R.bundle_verified, v, cb, d, str(top / "staging"))
    assert got.name == "readset-dest"
    assert os.listdir(top / "staging") == []
    monkeypatch.setattr(R, "full_fsync", real)
    assert R.bundle_verified(v, cb, d, str(top / "staging")).commit == v.commit
    R.discard(v)


def _verified(world, tmp_path):
    from levain.dispatch import readset as R
    from levain.dispatch.attempt_id import new_attempt_id
    _root, repo, _base, _old_a, contract = world
    top = Path(os.path.realpath(tmp_path))
    cb, d = contract.canonical().encode("utf-8"), contract.digest
    rs = build_readset(str(repo), contract, dest(top, "scratch"), contract_digest=d)
    for name in ("handoff", "attempts", "staging"):
        (top / name).mkdir(mode=0o700)
    ex = R.export_readset(rs, cb, d, str(top / "handoff"))
    try:
        fd = R.open_handoff(ex.path, size=ex.size)
    finally:
        os.close(ex.fd)
    try:
        return R.verify_readset(cb, d, fd, str(top / "attempts"), new_attempt_id()), cb, d, top
    finally:
        os.close(fd)


def test_a_p_swapped_after_it_was_checked_is_refused_at_the_cut(world, tmp_path, monkeypatch):
    # reproduced (wa-fix2 fix2_run.py W2): P swapped for a copy right after _open_p checked it was cut and staged,
    # because the later "P was replaced" compare used a second path read taken after the swap
    import shutil

    from levain.dispatch import readset as R
    v, cb, d, top = _verified(world, tmp_path)
    real = R._open_p

    def swapping(vv):
        out = real(vv)
        p = os.path.join(vv.workdir, "P")
        shutil.copytree(p, p + ".new", symlinks=True)
        os.rename(p, p + ".old")
        os.rename(p + ".new", p)
        return out

    monkeypatch.setattr(R, "_open_p", swapping)
    got = refusal(R.bundle_verified, v, cb, d, str(top / "staging"))
    assert (got.name, got.detail) == ("readset-verified-mismatch", "P was replaced while the bundle was cut")
    assert os.listdir(top / "staging") == []
    R.discard(v)


def test_a_staging_close_error_after_the_flush_returns_the_bundle(world, tmp_path, monkeypatch):
    # reproduced (wa-fix2 fix2_run.py W3): EIO from closing staging's fd after its flush escaped raw, so a durable
    # repo.bundle was reported as a failure and left in staging
    import errno

    from levain.dispatch import readset as R
    v, cb, d, top = _verified(world, tmp_path)
    sst = os.stat(top / "staging")
    real, hit = os.close, []

    def eio_on_staging(f: int) -> None:
        st = os.fstat(f)
        real(f)
        if not hit and (st.st_dev, st.st_ino) == (sst.st_dev, sst.st_ino):
            hit.append(f)
            raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(os, "close", eio_on_staging)
    sb = R.bundle_verified(v, cb, d, str(top / "staging"))
    monkeypatch.setattr(os, "close", real)
    assert hit and sb.commit == v.commit
    assert os.listdir(top / "staging") == [R.STAGED_BUNDLE] and os.path.getsize(sb.path) == sb.size
    R.discard(v)
