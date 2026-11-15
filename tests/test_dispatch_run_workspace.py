"""S14 design B piece B1: the dispatch runner's workspace stager (``levain.dispatch.run_workspace``), run for real.

Every repo is a real ``git init``, every contract is filed through ``resolve_contract``, the handoff is the real
``export_readset`` output and the clone is a real ``git clone`` of the bundle the stager cut. Properties P1-P7 of the
design page, each with a mutation noted where the test is the one that has to go red.
"""
from __future__ import annotations

import ast
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.agents.session_launcher import LauncherRefused
from levain.cockpit.consent import utc_stamp
from levain.dispatch import readset as RS
from levain.dispatch import run_workspace as RW
from levain.dispatch.contract import resolve_contract
from levain.dispatch.policy import ProviderPolicy
from tests.test_dispatch_readset import NOW, TEMPLATE, git
from tests.test_slot_dispatch_runtime import req, rt_rig  # noqa: F401 - the fake-helper runtime rig, a fixture

POLICY = ProviderPolicy.from_mapping({"never": ["work/**", "**/.env*"],
                                      "providers": {"openrouter": {"read": ["src/**", "CLAUDE.md", ".mcp.json",
                                                                            ".MCP.json", ".claude/**"]}}})
SESSION_KEY = "s-0123456789abcdef"     # the key tests.test_slot_dispatch_runtime.req() gives its SlotStart


class Rig:
    """One operator repo, one filed contract and its exported handoff, plus a stager over a private workdir parent."""

    def __init__(self, top: Path, read: list[str], extra: dict[str, str] | None = None, raw: bool = False) -> None:
        self.top = top
        repo = top / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        files = {"src/a.py": "a = 1\n", "src/b.py": "b = 2\n", "CLAUDE.md": "rules\n", ".env": "SECRET=1\n",
                 "tests/test_a.py": "def test(): pass\n", **(extra or {})}
        for rel, body in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(body)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "c1")
        base = git(repo, "rev-parse", "HEAD").strip()
        sub = {"template": "fix-lint", "goal": "fix a", "read": read, "write": ["src/a.py", "src/new.py"], "base": base,
               "budget": {"tool_calls": 10, "tokens": 1000, "dollars_cents": 10}, "grant": ["Edit"],
               "expires_at": utc_stamp(NOW + timedelta(hours=2)), "repo": str(repo)}
        self.contract = resolve_contract(sub, resolve_template=lambda t: TEMPLATE, policy=POLICY, now=NOW)
        self.read = [e.path for e in self.contract.read]
        self.cb = self.contract.canonical().encode("utf-8")
        for name in ("handoff", "work", "staging"):
            (top / name).mkdir(mode=0o700)
        rs = RS.build_readset(str(repo), self.contract, str(top / "scratch"), contract_digest=self.contract.digest)
        self.E = rs.commit
        if raw:       # a handoff the filing door would have refused: the run side must refuse it by itself
            path = str(top / "handoff" / "raw.bundle")
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                RS._bundle_into(rs.repo_dir, fd, RS._scratch_env())
            finally:
                os.close(fd)
            self.ex = SimpleNamespace(path=path, size=os.stat(path).st_size)
        else:
            self.ex = RS.export_readset(rs, self.cb, self.contract.digest, str(top / "handoff"))
            os.close(self.ex.fd)
        self.job = SimpleNamespace(id="job-xyz", digest=self.contract.digest, contract_text=self.contract.canonical())
        self.stager = RW.ReadSetStager(str(top / "work"), lambda job_id: ("bundle", self.ex.path, self.ex.size))

    @property
    def work(self) -> list[str]:
        return os.listdir(self.top / "work")

    def corrupt(self, offset: int) -> None:
        data = bytearray(Path(self.ex.path).read_bytes())
        data[offset] ^= 0xFF
        os.chmod(self.ex.path, 0o600)
        Path(self.ex.path).write_bytes(bytes(data))

    def staging(self, name: str) -> Path:
        p = self.top / "staging" / name
        p.mkdir()
        return p


@pytest.fixture
def rig(tmp_path):
    return Rig(Path(os.path.realpath(tmp_path)), ["src/a.py"])


def clone_of(bundle: Path, into: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(bundle), str(into)], check=True, capture_output=True)
    return into


def staged(rig: Rig, name: str = "s1") -> Path:
    v = rig.stager.verify(rig.job)
    rig.stager.register(SESSION_KEY, v)
    try:
        st = rig.staging(name)
        rig.stager.stage(SimpleNamespace(session_key=SESSION_KEY), st)
    finally:
        rig.stager.unregister(SESSION_KEY)
        rig.stager.discard(v)
    return st / "repo.bundle"


# --- P1 ----------------------------------------------------------------------------------------------------------------

def test_p1_the_executor_tree_is_exactly_the_signed_read_list_in_one_parentless_commit(tmp_path):
    # the read list names ONE of the repo's six tracked files; mutation: staging make_bundle(repo, "main") instead
    # gives the whole repo and its history, and both assertions go red
    rig = Rig(Path(os.path.realpath(tmp_path)), ["src/a.py", "CLAUDE.md"])
    clone = clone_of(staged(rig), rig.top / "clone")
    assert git(clone, "ls-tree", "-r", "--name-only", "HEAD").split() == sorted(rig.read)
    assert git(clone, "rev-list", "--count", "HEAD").strip() == "1"
    assert git(clone, "rev-parse", "HEAD").strip() == rig.E
    assert ".env" not in rig.read and (clone / ".env").exists() is False


def test_p1_the_staged_bundle_is_the_read_sets_not_the_handoffs_copy(rig):
    bundle = staged(rig)
    assert bundle.read_bytes()[:len(RS._header(rig.E))] == RS._header(rig.E)
    assert rig.work == []


# --- P2 ----------------------------------------------------------------------------------------------------------------

def test_p2_a_zeroed_handoff_fails_the_prefix_check_and_the_verify(rig):
    os.chmod(rig.ex.path, 0o600)
    Path(rig.ex.path).write_bytes(b"\0" * rig.ex.size)
    assert rig.stager.prefix_problem(rig.job) == "header"
    with pytest.raises(RS.ReadSetRefused) as e:
        rig.stager.verify(rig.job)
    assert e.value.name == "readset-handoff" and rig.work == []


def test_p2_one_pack_byte_flipped_past_the_prefix_is_refused_and_leaves_no_workdir(rig):
    rig.corrupt(rig.ex.size - 30)
    assert rig.stager.prefix_problem(rig.job) is None          # the prefix check is only a prefix...
    with pytest.raises(RS.ReadSetRefused) as e:                # ...the full verify is the one that holds
        rig.stager.verify(rig.job)
    assert e.value.name.startswith("readset-") and rig.work == []


def test_p2_a_job_with_no_handoff_row_and_a_wrong_size_are_refused(rig):
    gone = RW.ReadSetStager(str(rig.top / "work"), lambda _id: None)
    with pytest.raises(RS.ReadSetRefused):
        gone.verify(rig.job)
    wrong = RW.ReadSetStager(str(rig.top / "work"), lambda _id: ("b", rig.ex.path, rig.ex.size + 1))
    with pytest.raises(RS.ReadSetRefused) as e:
        wrong.verify(rig.job)
    assert e.value.name == "readset-handoff"


def test_p2_the_contract_the_set_is_staged_against_is_the_one_it_was_verified_against(rig, tmp_path):
    (tmp_path / "o").mkdir()
    other = Rig(Path(os.path.realpath(tmp_path / "o")), ["src/b.py"])
    v = rig.stager.verify(rig.job)
    try:
        forged = RS.VerifiedReadSet(**{**v.__dict__, "commit": other.E})
        rig.stager.register(SESSION_KEY, forged)
        with pytest.raises(LauncherRefused) as e:
            rig.stager.stage(SimpleNamespace(session_key=SESSION_KEY), rig.staging("s"))
        assert e.value.name == "readset-verified-mismatch" and e.value.reason_class == "standing"
    finally:
        rig.stager.unregister(SESSION_KEY)
        rig.stager.discard(v)


# --- P3 ----------------------------------------------------------------------------------------------------------------

def test_p3_an_unregistered_key_stages_nothing_and_is_refused_standing(rig):
    st = rig.staging("s")
    with pytest.raises(LauncherRefused) as e:
        rig.stager.stage(SimpleNamespace(session_key=SESSION_KEY), st)
    assert e.value.name == "workspace-unregistered" and e.value.reason_class == "standing"
    assert os.listdir(st) == []
    with pytest.raises(LauncherRefused):
        rig.stager.repo_settings(SimpleNamespace(session_key=SESSION_KEY))


def test_p3_the_runtime_ends_the_session_when_the_start_is_unregistered(rig, rt_rig):  # noqa: F811
    h, lease, make = rt_rig
    # the stage seam alone: the refusal comes after prepare, so the session is ENDed once and never rendered
    rt = make(workspace=rig.stager.stage)
    with pytest.raises(LauncherRefused) as e:
        rt.start(req())
    assert e.value.name == "workspace-unregistered"
    assert h.verbs().count("teardown") == 1 and "start" not in h.verbs() and "render" not in h.verbs()


def test_p3_with_both_seams_wired_an_unregistered_start_is_refused_before_anything_is_allocated(rig, rt_rig):  # noqa: F811
    h, lease, make = rt_rig
    rt = make(workspace=rig.stager.stage, repo_settings=rig.stager.repo_settings)
    with pytest.raises(LauncherRefused) as e:
        rt.start(req())
    assert e.value.name == "workspace-unregistered" and h.verbs() == []


def test_p3_a_registered_start_stages_through_the_runtime_and_a_popped_key_does_not(rig, rt_rig):  # noqa: F811
    h, lease, make = rt_rig
    rt = make(workspace=rig.stager.stage, repo_settings=rig.stager.repo_settings)
    v = rig.stager.verify(rig.job)
    rig.stager.register(SESSION_KEY, v)
    try:
        handle = rt.start(req())
        handle.child.abort()
        assert (rt._store / "staging" / "s-0123456789abcdef" / "repo.bundle").is_file() or "render" in h.verbs()
    finally:
        rig.stager.unregister(SESSION_KEY)
        rig.stager.discard(v)
    with pytest.raises(LauncherRefused):
        rig.stager.stage(SimpleNamespace(session_key=SESSION_KEY), rig.staging("again"))


def test_p3_a_host_fault_is_transient_and_any_other_refusal_is_standing(rig, monkeypatch):
    v = rig.stager.verify(rig.job)
    rig.stager.register(SESSION_KEY, v)
    try:
        for name, cls in (("readset-workdir", "transient"), ("readset-git-unavailable", "transient"),
                          ("readset-dest", "standing")):
            def boom(*_a, _n=name):
                raise RS.ReadSetRefused(_n, "x")
            monkeypatch.setattr(RS, "bundle_verified", boom)
            with pytest.raises(LauncherRefused) as e:
                rig.stager.stage(SimpleNamespace(session_key=SESSION_KEY), rig.top / "staging")
            assert (e.value.name, e.value.reason_class) == (name, cls)
    finally:
        rig.stager.unregister(SESSION_KEY)
        rig.stager.discard(v)


# --- P4 ----------------------------------------------------------------------------------------------------------------

def test_p4_a_read_set_carrying_mcp_json_is_refused_at_verify(tmp_path):
    # mutation: delete the readset._refused_names_check call and the verify passes (the run3 probe: staged)
    rig = Rig(Path(os.path.realpath(tmp_path)), [".mcp.json", "src/a.py"], extra={".mcp.json": "{}\n"})
    with pytest.raises(RS.ReadSetRefused) as e:
        rig.stager.verify(rig.job)
    assert e.value.name == "readset-refused-name" and ".mcp.json" in e.value.detail
    assert rig.work == []


def test_p4_parity_is_the_uploads_one_constant(tmp_path, monkeypatch):
    from levain.slot import workspace as WS
    rig = Rig(Path(os.path.realpath(tmp_path)), [".mcp.json", "src/a.py"], extra={".mcp.json": "{}\n"})
    monkeypatch.setattr(WS, "DISPATCH_REFUSED_NAMES", ())
    rig.stager.discard(rig.stager.verify(rig.job))      # the constant emptied: the same read set verifies


def test_p4_a_respelled_root_name_is_refused_alike(tmp_path):
    rig = Rig(Path(os.path.realpath(tmp_path)), ["src/a.py", ".MCP.json"], extra={".MCP.json": "{}\n"})
    with pytest.raises(RS.ReadSetRefused) as e:
        rig.stager.verify(rig.job)
    assert e.value.name == "readset-refused-name"


def test_p4_a_nested_mcp_json_is_not_the_roots_and_rides(tmp_path):
    rig = Rig(Path(os.path.realpath(tmp_path)), ["src/.mcp.json", "src/a.py"], extra={"src/.mcp.json": "{}\n"})
    v = rig.stager.verify(rig.job)
    rig.stager.discard(v)


# --- P5 ----------------------------------------------------------------------------------------------------------------

def test_p5_no_workdir_outlives_a_discard_or_a_refused_stage(rig):
    v = rig.stager.verify(rig.job)
    assert len(rig.work) == 1
    rig.stager.discard(v)
    assert rig.work == []
    v = rig.stager.verify(rig.job)
    rig.stager.register(SESSION_KEY, v)
    try:
        os.mkdir(rig.top / "staging" / "occupied")
        (rig.top / "staging" / "occupied" / "repo.bundle").write_bytes(b"x")       # bundle_verified refuses an occupied dir
        with pytest.raises(LauncherRefused):
            rig.stager.stage(SimpleNamespace(session_key=SESSION_KEY), rig.top / "staging" / "occupied")
    finally:
        rig.stager.unregister(SESSION_KEY)
        rig.stager.discard(v)
    assert rig.work == []


def test_p5_two_verifications_never_share_a_workdir_and_a_crash_leftover_is_swept(rig):
    a, b = rig.stager.verify(rig.job), rig.stager.verify(rig.job)
    assert a.workdir != b.workdir
    # a crash leaves both behind: nothing is live after a restart, so the sweep takes every one
    left = os.path.join(rig.stager.parent, "w-dv-0000000000000000")
    os.mkdir(left, 0o700)
    report = rig.stager.sweep()
    assert rig.work == [] and len(report.removed) == 3 and report.left == ()


# --- P6 ----------------------------------------------------------------------------------------------------------------

def test_p6_a_loosening_settings_file_is_refused_by_its_own_name_through_the_stager(tmp_path):
    rig = Rig(Path(os.path.realpath(tmp_path)), [".claude/settings.json", "src/a.py"],
              extra={".claude/settings.json": '{"disableAllHooks": true}'}, raw=True)
    with pytest.raises(RS.ReadSetRefused) as e:
        rig.stager.verify(rig.job)
    assert e.value.name == "loosening:disable-all-hooks" and rig.work == []


def test_p6_a_benign_settings_file_rides_as_the_repo_settings_the_runtime_asks_for(tmp_path):
    rig = Rig(Path(os.path.realpath(tmp_path)), [".claude/settings.json", "src/a.py"],
              extra={".claude/settings.json": '{"permissions": {"deny": ["Bash(rm:*)"]}}'})
    v = rig.stager.verify(rig.job)
    rig.stager.register(SESSION_KEY, v)
    try:
        got = rig.stager.repo_settings(SimpleNamespace(session_key=SESSION_KEY))
        assert got == v.repo_settings and got
    finally:
        rig.stager.unregister(SESSION_KEY)
        rig.stager.discard(v)


# --- P7 ----------------------------------------------------------------------------------------------------------------

def test_p7_the_module_imports_nothing_operator_side():
    src = Path(RW.__file__).read_text()
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
            mods.update(f"{node.module}.{a.name}" for a in node.names)
    assert not [m for m in mods if m.endswith("dispatch.stager") or m.endswith("dispatch.file")], sorted(mods)
    assert "expanduser" not in src and "Path.home" not in src and "/Users/" not in src
