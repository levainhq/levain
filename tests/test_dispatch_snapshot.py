"""S10w: the workspace snapshot (``levain.dispatch.snapshot``). Every repo is a real ``git init`` with real history;
nothing about git is mocked."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from levain.dispatch import snapshot as snap
from levain.dispatch.policy import ProviderPolicy
from levain.dispatch.snapshot import SnapshotRefused, build_snapshot, plan_snapshot, scan_blob, snapshot_commit

OLD_SECRET = "AKIA" + "ABCDEFGHIJKLMNOP"


def git(repo: Path, *args: str, data: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"})
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env,
                          input=data, timeout=30).stdout


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = Path(os.path.realpath(tmp_path)) / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c1")
    return repo


FILES = {"src/a.py": "a = 1\n", "CLAUDE.md": "# memory\n", "project_memory/n.md": "note\n", "docs/x.txt": "x\n"}


def test_snapshot_is_head_minus_withheld_one_parentless_commit_no_old_blob(tmp_path):
    repo = make_repo(tmp_path, {**FILES, "old.txt": f"key {OLD_SECRET}\n"})
    old_blob = git(repo, "rev-parse", "HEAD:old.txt").strip()
    git(repo, "rm", "-q", "old.txt")
    git(repo, "commit", "-q", "-m", "remove the secret")
    (repo / "untracked.txt").write_text("never\n")
    rs, man = build_snapshot(str(repo), str(tmp_path / "snap"), ["docs/**"])
    paths = git(Path(rs.repo_dir), "ls-tree", "-r", "--name-only", rs.commit).split()
    assert sorted(paths) == ["CLAUDE.md", "project_memory/n.md", "src/a.py"]      # docs withheld, untracked absent
    assert man.withheld_paths == ("docs/x.txt",)
    assert git(Path(rs.repo_dir), "rev-list", "--count", rs.commit).strip() == "1"
    assert git(Path(rs.repo_dir), "rev-list", "--parents", "-n1", rs.commit).split() == [rs.commit]
    assert subprocess.run(["git", "-C", rs.repo_dir, "cat-file", "-e", old_blob]).returncode != 0
    assert (rs.commit, rs.tree) == snapshot_commit(man)[:2]
    n = int(git(Path(rs.repo_dir), "rev-list", "--objects", rs.commit).count("\n"))
    assert snapshot_commit(man)[2] == n


def test_manifest_is_deterministic_and_binds_the_withhold_list(tmp_path):
    repo = make_repo(tmp_path, FILES)
    a, _ = plan_snapshot(str(repo), ["docs/**"])
    b, _ = plan_snapshot(str(repo), ["docs/**"])
    c, _ = plan_snapshot(str(repo), [])
    assert a.digest() == b.digest() and a.canonical() == b.canonical()
    assert c.digest() != a.digest() and c.head == a.head
    assert snapshot_commit(a) != snapshot_commit(c)


@pytest.mark.parametrize("name,text,rule", [
    ("k.pem", "-----BEGIN RSA PRIVATE KEY-----\nabc\n", "private-key-armor"),
    ("cfg.txt", "id " + OLD_SECRET + "\n", "aws-access-key-id"),
    ("x.env", "export API_KEY=abcdef0123456789abcd\n", "secret-assignment"),
    ("g.md", "tok ghp_" + "a" * 36 + "\n", "github-token"),
])
def test_secret_scan_refuses_whole_names_path_and_rule_never_the_value(tmp_path, name, text, rule):
    repo = make_repo(tmp_path, {**FILES, name: text})
    dest = tmp_path / "snap"
    with pytest.raises(SnapshotRefused) as ei:
        build_snapshot(str(repo), str(dest), [])
    assert ei.value.name == "snapshot-secret" and name in ei.value.detail and rule in ei.value.detail
    assert OLD_SECRET not in str(ei.value) and "abcdef0123456789" not in str(ei.value)
    assert not dest.exists()                       # no partial snapshot stays
    assert scan_blob(b"nothing here\n") is None
    rs, _ = build_snapshot(str(repo), str(dest), [name])        # withholding the file clears it
    assert name not in git(Path(rs.repo_dir), "ls-tree", "-r", "--name-only", rs.commit)


def test_never_list_refuses_whole_but_a_withheld_path_is_not_judged(tmp_path):
    repo = make_repo(tmp_path, {**FILES, ".env": "A=1\n"})
    policy = ProviderPolicy.from_mapping({"never": ["**/.env"], "providers": {"p": {"read": ["**"]}}})
    with pytest.raises(SnapshotRefused) as ei:
        plan_snapshot(str(repo), [], policy=policy, provider="p")
    assert ei.value.name == "snapshot-policy"
    man, _ = plan_snapshot(str(repo), [".env"], policy=policy, provider="p")
    assert ".env" not in [e[0] for e in man.entries]
    with pytest.raises(SnapshotRefused):
        plan_snapshot(str(repo), [], policy=policy)          # a policy without a provider is refused, not skipped


@pytest.mark.parametrize("attr,value,name", [("SNAPSHOT_MAX_FILES", 2, "snapshot-too-many-files"),
                                             ("SNAPSHOT_MAX_BYTES", 10, "snapshot-too-large"),
                                             ("BLOB_MAX_BYTES", 5, "snapshot-blob-too-large")])
def test_bounds_refuse_whole_never_trim(tmp_path, monkeypatch, attr, value, name):
    repo = make_repo(tmp_path, FILES)
    monkeypatch.setattr(snap, attr, value)
    with pytest.raises(SnapshotRefused) as ei:
        plan_snapshot(str(repo), [])
    assert ei.value.name == name


def test_symlink_and_submodule_are_refused_by_name_unless_withheld(tmp_path):
    repo = make_repo(tmp_path, FILES)
    (repo / "link").symlink_to("src/a.py")
    git(repo, "update-index", "--add", "--cacheinfo", "160000," + "1" * 40 + ",sub")
    git(repo, "add", "link")
    git(repo, "commit", "-q", "-m", "odd entries")
    with pytest.raises(SnapshotRefused) as ei:
        plan_snapshot(str(repo), [])
    assert ei.value.name == "snapshot-entry"
    man, _ = plan_snapshot(str(repo), ["link", "sub"])
    assert set(man.withheld_paths) == {"link", "sub"}


def test_dest_rules_and_bad_withhold_and_history_are_refused(tmp_path):
    repo = make_repo(tmp_path, FILES)
    (tmp_path / "taken").mkdir()
    for dest in (str(tmp_path / "taken"), str(repo / "inside")):
        with pytest.raises(SnapshotRefused) as ei:
            build_snapshot(str(repo), dest, [])
        assert ei.value.name == "snapshot-dest"
    for bad in ("docs/**", ["/abs"], ["a", "a"], ["../x"]):
        with pytest.raises(SnapshotRefused) as ei:
            plan_snapshot(str(repo), bad)
        assert ei.value.name == "snapshot-withhold"
    man, _ = plan_snapshot(str(repo), [])
    import dataclasses
    with pytest.raises(SnapshotRefused):
        snapshot_commit(dataclasses.replace(man, history=3))


def test_head_is_resolved_once_a_head_move_cannot_desync_manifest_head_from_entries(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, FILES)
    real_git = snap._git
    moved = []

    def git_then_move_head(r, *args, data=None):
        out = real_git(r, *args, data=data)
        if args[:1] == ("rev-parse",) and not moved:   # a commit lands between resolving HEAD and listing it
            (repo / "late.txt").write_text("late\n")
            git(repo, "add", "late.txt")
            git(repo, "commit", "-q", "-m", "late")
            moved.append(True)
        return out
    monkeypatch.setattr(snap, "_git", git_then_move_head)
    man, _ = plan_snapshot(str(repo), [])
    assert moved
    listed = sorted(git(repo, "ls-tree", "-r", "--name-only", man.head).split())
    assert [e[0] for e in man.entries] == listed and "late.txt" not in listed


@pytest.mark.parametrize("text", [
    "PASSWORD=p@ssword-is-definitely-secret\n",
    'PASSWORD="!abcdef0123456789abcdef"\n',
    "export DB_PASSWORD='#hash-leading-secret-value'\n",
    "API_KEY = %percent-leading-key-0123\n",
    "  secret_token=@at-leading-0123456789\n",
])
def test_secret_assignment_catches_values_that_begin_with_punctuation(text):
    assert scan_blob(text.encode()) == "secret-assignment"


@pytest.mark.parametrize("text", [
    "PASSWORD=$uperSecret123\n",                       # a `$`-leading password is not a variable reference
    "PASSWORD=P4ss(word)1234\n",                       # an `identifier(` prefix is not code
    "TOKEN=abc[12345678901]\n",                        # an `identifier[` prefix is not code
    "PASSWORD=exampleRealSecret123456\n",              # a placeholder word inside a credential exempts nothing
    "PASSWORD='<actual-secret-123456>'\n",             # angle brackets are not a placeholder
    'PASSWORD="!short\\"actual-secret-123456"\n',      # an early escaped quote does not end the value
    "API_KEY=${API_KEY_FROM_THE_VAULT}\n",             # fail closed: no exemptions, the role withholds instead
    "export SECRET_KEY=$SECRET_KEY_FROM_THE_VAULT\n",
    "API_KEY=<your-api-key-goes-here>\n",
    "PASSWORD=changeme-changeme-changeme\n",
    "TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx\n",
    'api_key = os.environ.get("API_KEY")\n',
    "token = self._load_token_from_keychain(name)\n",
    "db_password: hunter2-hunter2-hunter2\n",          # the `:` separator too
])
def test_secret_assignment_fails_closed_on_every_value_of_twelve_bytes_or_more(text):
    assert scan_blob(text.encode()) == "secret-assignment"


@pytest.mark.parametrize("text", [
    "A=1\n",                                           # not a secret-named assignment
    "PASSWORD=short\n",                                # under the minimum length
    "PASSWORD='elevenbytes'\n",                        # 11 bytes once one layer of quotes is stripped
    'TOKEN=""\n',
    "PASSWORD_HINT\n",                                 # no assignment at all
])
def test_secret_assignment_ignores_short_values_and_non_assignments(text):
    assert scan_blob(text.encode()) is None


def test_a_false_positive_is_cleared_only_by_the_role_withholding_its_path(tmp_path):
    repo = make_repo(tmp_path, {**FILES, "src/cfg.py": 'api_key = os.environ.get("API_KEY")\n'})
    with pytest.raises(SnapshotRefused) as ei:
        plan_snapshot(str(repo), [])
    assert ei.value.name == "snapshot-secret" and "src/cfg.py" in ei.value.detail
    assert "withhold" in ei.value.detail and "os.environ" not in ei.value.detail
    man, _ = plan_snapshot(str(repo), ["src/cfg.py"])
    assert man.withheld_paths == ("src/cfg.py",) and "src/cfg.py" not in [e[0] for e in man.entries]


def test_scan_is_linear_on_a_pathological_one_megabyte_line():
    """One hostile or generated line must not hang the dispatcher. Run in a child with a hard timeout, so a
    regression fails this test instead of hanging the suite."""
    import sys
    import textwrap
    code = textwrap.dedent("""
        import time
        from levain.dispatch.snapshot import scan_blob
        n = 1024 ** 2
        lines = [b"SECRET" * (n // 6), b"A" * n + b"TOKEN", b"x" + b"SECRET_" * (n // 7),
                 b"PASSWORD=" + b"!" * n, b"TOKEN" * (n // 5) + b" " * 10, b" " * n + b"=",
                 b"-----BEGIN " + b"A " * (n // 2), b"ghp_" + b"a" * n + b"_"]
        t = time.monotonic()
        for line in lines:
            scan_blob(line)
        print(time.monotonic() - t)
    """)
    root = str(Path(snap.__file__).resolve().parents[2])
    env = {**os.environ, "PYTHONPATH": root}
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=env, cwd=root)
    assert cp.returncode == 0, cp.stderr[-500:]
    assert float(cp.stdout.strip()) < 2.0


@pytest.mark.parametrize("name", ['"config', 'back\\slash', 'q"mid"dle.txt'])
def test_a_path_with_a_quote_or_backslash_is_c_quoted_for_fast_import(tmp_path, name):
    repo = make_repo(tmp_path, {**FILES, name: "ok\n"})
    rs, man = build_snapshot(str(repo), str(tmp_path / "snap"), [])
    paths = git(Path(rs.repo_dir), "ls-tree", "-r", "-z", "--name-only", rs.commit).split("\0")
    assert name in paths and (rs.commit, rs.tree) == snapshot_commit(man)[:2]
    assert git(Path(rs.repo_dir), "show", f"{rs.commit}:{name}") == "ok\n"


def test_fast_import_path_quoting_escapes_every_special_byte():
    assert snap._fi_path("plain/a.py") == b'"plain/a.py"'
    assert snap._fi_path('"config') == b'"\\"config"'
    assert snap._fi_path("a\\b") == b'"a\\\\b"'
    assert snap._fi_path("a\nb\tc\x7f") == b'"a\\012b\\011c\\177"'
    assert snap._fi_path("café") == b'"caf\xc3\xa9"'


def test_total_bound_is_64_mib_per_file_8_mib_files_20000():
    assert (snap.SNAPSHOT_MAX_BYTES, snap.BLOB_MAX_BYTES, snap.SNAPSHOT_MAX_FILES) == (64 * 1024 ** 2,
                                                                                       8 * 1024 ** 2, 20_000)
