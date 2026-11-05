"""p71 S8b (DESIGN_p71_s8 D1, D2, D4, D6, D7, D8, D11; NP-1): the root helper's half of an executor's launch. ``render`` (3)
stages the brief into ``<S>/in/brief.md``; ``start`` execs ``claude -p`` with its output off the helper's stdout (two
root-drained files under ``<S>/run/``) and the helper's EXIT STATUS carries the vendor's success.

Refusal tests first, then the successes and pins. Built on the lane F2 / 2f3 harness and P7-A1's ticketed passthrough
world (``ptd``: a real CONTROL and ticket store, a marked slot): the launch through the wall is the 2f3 fake process
table, so what a spawn was GIVEN (argv, parsed spec, keywords, fds) is recorded. The real fork, the real drop, the
real fd inheritance through the stage and the macOS profile / bwrap view are VM rows (named in the S8b return); no
sandbox profile is written or applied here."""
from __future__ import annotations

import ast
import errno
import io
import json
import os
import signal
import stat
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import records as R
from levain.slot import render as RD
from levain.slot import runtime_grammar as G
from levain.slot import runtime_helper as H
from levain.slot import stage as ST
from levain.slot import start as SS
from levain.slot import wall
from levain.slot.ids import SlotId
from levain.slot.layout import STORE
from levain.slot.root_tree import VENDOR_PREFIX
from tests.execve_guard import refuse_real_execve
from tests.test_pool import F2_DISPATCH
from tests.test_slot_3c7_fix_r1 import _blob as ticket_blob
from tests.test_slot_p7a1_passthrough_ticket import _stub_limits, mark, ptd  # noqa: F401 - the ticketed world fixture
from tests.test_slot_s2b_render import egress, verifier
from tests.test_slot_s2f2_runtime_helper import BRIEF, BRIEF_DIGEST, BUNDLE, OP_UID, World, _blob, _prepared
from tests.test_slot_s2f3_start import Launches, Table, _fd_open, _world, _rendered
from tests.test_slot_s2c_end import mine, thread_run_as
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

E0 = SlotId("egress", 0)
UUID = "4b8e0c1a1d2e4f009a7b0123456789ab4b8e0c1a1d2e4f009a7b0123456789ab"    # give_credential's claude baseline
PASS = ["claude-sub", "github"]
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_real_execve(monkeypatch):
    from tests.test_slot_s2f3_start import StageFake  # noqa: F401
    yield from refuse_real_execve(monkeypatch, wall.os, route=True)


# --- the harness -----------------------------------------------------------------------------------------------------

class XLaunches(Launches):
    """2f3's fake launches, recording the keywords a spawn was given and the raw spec bytes; the executor's capturing
    wait answers the vendor's code with no real pipes (the drain loop has its own tests)."""

    def __init__(self, world, table) -> None:
        super().__init__(world, table)
        self.drained: list = []

    def spawn(self, argv, spec_json, cgroup, exec_w, detach, **kw) -> int:
        pid = super().spawn(argv, spec_json, cgroup, exec_w, detach)
        rec = self.spawns[-1]
        rec["kw"] = dict(kw)
        rec["spec_json"] = spec_json
        rec["kw_open"] = {k: _fd_open(v) for k, v in kw.items() if type(v) is int}
        return pid

    def wait_capturing(self, pid, streams):
        rec = next(r for r in self.spawns if r["pid"] == pid)
        self.drained.append(tuple(streams))
        return H.DrainResult(self.codes.get(rec["target"], 0), [0, 0], [0, 0])


def wire(w: World, la: Launches, table: Table, monkeypatch) -> None:
    """The 2f3 ``_world`` seam block, on an existing world."""
    (w.L / "retained").mkdir(mode=0o700, exist_ok=True)
    w.session = None
    s = w.seams
    s.spawn, s.wait, s.wait_within, s.kill = la.spawn, la.wait, la.wait_within, la.kill
    if hasattr(la, "wait_capturing"):
        s.wait_capturing = la.wait_capturing
    s.capture = la.capture
    s.netns_names = lambda: {"levain-e0", "levain-e1", "levain-e2", "levain-v0"}
    s.host_links = lambda: {"/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib", "/lib64": None}
    w.cgroups = []

    def make_cgroup(path):
        w.cgroups.append(path)
        table.cgroup_made = True
    s.make_cgroup = make_cgroup
    s.egress_ready = lambda slot, ids: None
    s.proc_seams = table.seams()
    s.end_overrides = {"owner_ok": mine, "run_as": thread_run_as(), "root_uid": os.getuid(),
                       "root_gid": os.getgid(), "sleep": lambda x: None}
    s.clock = lambda: 1_790_000_000.0
    monkeypatch.setattr(SS, "slot_owner_ok", lambda st: True)
    monkeypatch.setattr(SS, "clone_check", lambda *a, **k: None)        # the check has its own tests (2f3, w2d)
    la.profile_uuid = UUID


class Rig:
    """A ticketed passthrough executor world (P7-A1's ``ptd``) with the fake launches wired in."""

    def __init__(self, ptd, monkeypatch) -> None:
        self.d, self.w = ptd, ptd.w
        self.table = Table()
        self.la = XLaunches(self.w, self.table)
        wire(self.w, self.la, self.table, monkeypatch)
        _stub_limits(monkeypatch)
        mark(self.w, E0)
        self.blob = ticket_blob(routes=PASS, model=None)

    def prepared(self, *, digest: str | None = BRIEF_DIGEST) -> str:
        """A prepared executor session; ``digest`` None mints the ticket WITHOUT a brief_digest. Staged: the bundle,
        and the fixture brief when the ticket names its digest."""
        line = self.d.prepare(self.d.ticket(brief_digest=digest))
        assert line["ok"], (line, self.w.last_err)
        s = self.w.session = line["session"]
        self.w.stage(s)
        return s

    def render(self, s: str) -> dict:
        return self.w.main(["render", s], self.blob, uid=F2_DISPATCH)[1]

    def S(self, s: str) -> Path:
        return self.w.L / "live" / s


@pytest.fixture
def rig(ptd, monkeypatch):
    return Rig(ptd, monkeypatch)


def raw_main(w: World, argv, stdin: bytes = b"", *, uid: int = F2_DISPATCH) -> tuple[int, str]:
    """``H.main``'s process exit status and its stdout text, with no assertion tying the status to ``ok``."""
    out, err = io.StringIO(), io.StringIO()
    code = H.main(argv, stdin=stdin, env={"SUDO_UID": str(uid)}, seams=w.seams, out=out, err=err)
    w.last_err = err.getvalue()
    return code, out.getvalue()


def _staging(rig_: Rig, s: str) -> list[str]:
    return sorted(os.listdir(rig_.w.L / "staging" / s))


# === REFUSAL TESTS FIRST ======================================================================================

def test_01_render_executor_without_brief_digest_refused_nothing_written(rig, monkeypatch):
    """An executor ticket on claude with no ``brief_digest`` in its facts: ``ticket-facts-missing`` before the first
    CAS, nothing under ``<S>/root/``, staging untouched, and ``stage_brief`` is never reached (check_ticket is first)."""
    monkeypatch.setattr(ST, "stage_brief", lambda *a, **k: pytest.fail("stage_brief reached"))
    s = rig.prepared(digest=None)
    calls = len(rig.w.calls)
    assert "brief_digest" not in rig.w.alloc(s).ticket_facts
    assert rig.render(s) == {"ok": False, "refused": "ticket-facts-missing"}, rig.w.last_err
    assert rig.w.calls[calls:] == []
    assert not (rig.S(s) / "root").exists() or os.listdir(rig.S(s) / "root") == []
    assert _staging(rig, s) == ["repo.bundle"]
    assert not (rig.S(s) / "in" / "brief.md").exists()


def _facts(kind, **kw):
    base = {"kind": kind, "job_id": "j", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
            "egress_routes": [], "reserved_microdollars": 0}
    return {**base, **kw}


def test_02_a_brief_digest_on_anything_but_an_executor_on_claude_is_forbidden():
    d = {"brief_digest": BRIEF_DIGEST}
    planner = _facts("planner", route="glm", model_requested="m", **d)
    with pytest.raises(Refused) as e:
        RD.check_ticket(G.RenderBlob(vendor="claude", routes=("glm",), model="m"), "planner", "egress", "tk", planner)
    assert e.value.name == "ticket-brief-forbidden"
    codex = _facts("executor", route="glm", model_requested="m", **d)               # an executor, but on codex
    with pytest.raises(Refused) as e:
        RD.check_ticket(G.RenderBlob(vendor="codex", routes=("glm",), model="m"), "dispatch", "egress", "tk", codex)
    assert e.value.name == "ticket-brief-forbidden"
    ver = _facts("verifier", verifier_steps=[{"argv": ["x"], "wall_s": 1}], **d)
    with pytest.raises(Refused) as e:
        RD.check_ticket(G.RenderBlob(vendor="claude", routes=()), "verifier", "none", "tk", ver)
    assert e.value.name == "ticket-brief-forbidden"
    # the executor on claude with the digest passes this gate
    ok = _facts("executor", route="glm", model_requested="m", **d)
    RD.check_ticket(G.RenderBlob(vendor="claude", routes=("glm",), model="m"), "dispatch", "egress", "tk", ok)
    # an interactive alloc cannot carry ticket facts at all (the record refuses them without a ticket)
    with pytest.raises(Refused):
        R.Alloc(session="s-00000000000000f1", slot=E0, lease_seq=1, slot_rev=1, slot_uid=1, slot_gid=1,
                kind="interactive", vendor="claude", auth="brokered", operator_uid=1, supervisor_uid=1, staging_uid=1,
                created_at="2026-10-20T00:00:00Z", boot_id="0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
                netns=E0.netns, cgroup=R.cgroup_path(E0, "s-00000000000000f1"), ticket_facts=ok)


def _plant(kind):
    def missing(staging):
        pass

    def other(staging):
        (staging / "brief.md").write_bytes(BRIEF + b"x")

    def link(staging):
        (staging / "elsewhere").write_bytes(BRIEF)
        os.symlink("elsewhere", staging / "brief.md")
    return {"missing": missing, "mismatch": other, "symlink": link}[kind]


@pytest.mark.parametrize("kind,name", [("missing", "staging-missing"), ("mismatch", "brief-mismatch"),
                                       ("symlink", "staging-not-regular")])
def test_03_render_brief_refusals_leave_no_brief_no_base_and_the_bundle_staged(rig, kind, name):
    s = rig.prepared()
    staging = rig.w.L / "staging" / s
    (staging / "brief.md").unlink()
    _plant(kind)(staging)
    code, line = rig.w.main(["render", s], rig.blob, uid=F2_DISPATCH)
    assert line == {"ok": False, "refused": name}, (line, rig.w.last_err)
    assert not (rig.S(s) / "in" / "brief.md").exists()
    assert rig.w.alloc(s).base is None
    assert (staging / "repo.bundle").read_bytes() == BUNDLE                          # still staged


def test_04_a_fault_after_the_brief_copied_leaves_both_staged_names(rig):
    s = rig.prepared()

    def chown(fd, u, g, rel):
        if rel.endswith("root/gitconfig"):
            raise OSError(errno.EIO, "fault")
        rig.w.chowns.append((rel, u, g))

    rig.w.seams.chown = chown
    code, _ = raw_main(rig.w, ["render", s], rig.blob)
    assert code == H.EXIT_INTERNAL
    assert (rig.S(s) / "in" / "brief.md").read_bytes() == BRIEF                      # copied before the fault
    assert _staging(rig, s) == ["brief.md", "repo.bundle"]
    assert rig.w.alloc(s).base is None


def test_05_start_of_a_dispatch_alloc_with_no_ticket_facts_is_refused_before_any_spawn(rig):
    s = rig.prepared()
    assert rig.render(s) == {"ok": True}, rig.w.last_err
    p = rig.w.L / "alloc" / f"{s}.json"
    p.write_bytes(R.alloc_bytes(replace(R.parse_alloc(p.read_bytes()), ticket_facts=None)))
    line = rig.w.main(["start", s], uid=F2_DISPATCH)[1]
    assert line == {"ok": False, "refused": "ticket-facts-missing"}, rig.w.last_err
    assert rig.la.spawns == []


@pytest.mark.parametrize("plant", ["symlink", "file", "dir"])
def test_06_run_preplanted_is_refused_by_name_before_the_vendor_spawn(rig, tmp_path, plant):
    s = rig.prepared()
    assert rig.render(s) == {"ok": True}, rig.w.last_err
    target = tmp_path / "elsewhere"
    target.mkdir()
    run = rig.S(s) / "run"
    if plant == "symlink":
        os.symlink(target, run)
    elif plant == "file":
        run.write_bytes(b"x")
    else:
        run.mkdir()
    line = rig.w.main(["start", s], uid=F2_DISPATCH)[1]
    assert line == {"ok": False, "refused": "session-exists"}, rig.w.last_err
    assert rig.la.of("vendor") == []
    assert os.listdir(target) == []                                                   # nothing written through it
    if plant == "file":
        assert run.read_bytes() == b"x"


def _started_main(rig_: Rig, code: int):
    s = rig_.prepared()
    assert rig_.render(s) == {"ok": True}, rig_.w.last_err
    rig_.la.codes["vendor"] = code
    return s, raw_main(rig_.w, ["start", s])


@pytest.mark.parametrize("code", [1, 128 + signal.SIGTERM])
def test_07_an_executor_vendor_that_failed_or_was_killed_exits_the_helper_nonzero(rig, code):
    s, (status, text) = _started_main(rig, code)
    assert status == H.EXIT_VENDOR == 2, rig.w.last_err
    assert text == canon.canonical({"exit": code, "ok": True}) + "\n"                 # the line is unchanged in shape
    assert set(canon.parse(text.strip().encode())) == {"exit", "ok"}                  # no private key on stdout


def test_07b_an_executor_vendor_that_succeeded_exits_zero(rig):
    s, (status, text) = _started_main(rig, 0)
    assert status == H.EXIT_OK
    assert text == '{"exit":0,"ok":true}\n'


def test_07c_an_interactive_vendor_exiting_one_still_exits_zero(tmp_path, monkeypatch):
    w, la, _ = _world(tmp_path)
    s = _rendered(w)
    la.codes["vendor"] = 1
    status, text = raw_main(w, ["start", s], uid=OP_UID)
    assert status == H.EXIT_OK and text == '{"exit":1,"ok":true}\n', w.last_err


# === SUCCESS AND PINS =========================================================================================

def test_08_render_stages_the_brief_root_owned_0440_and_empties_staging(rig):
    s = rig.prepared()
    assert rig.render(s) == {"ok": True}, rig.w.last_err
    dest = rig.S(s) / "in" / "brief.md"
    assert dest.read_bytes() == BRIEF
    assert rig.w.mode(f"live/{s}/in/brief.md") == 0o440
    bundle_gid = rig.w.chown_of(f"live/{s}/in/repo.bundle")[1]
    assert rig.w.chown_of(f"live/{s}/in/brief.md") == (0, bundle_gid)                # root : the slot's gid
    assert bundle_gid == rig.w.alloc(s).slot_gid
    assert os.listdir(rig.w.L / "staging" / s) == []
    assert rig.w.alloc(s).base is not None


def _env_of(blob, facts):
    return RD.render_env(blob, facts)["slot"]


def test_09_background_tasks_off_for_an_executor_only():
    var = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"
    blob, facts = egress(kind="dispatch")
    env = _env_of(blob, facts)
    assert env[var] == "1"
    for kind in ("interactive", "login"):
        b, f = egress(kind=kind)
        assert var not in _env_of(b, f)
    b, f = egress(kind="planner")
    assert _env_of(b, f) == {k: v for k, v in env.items() if k != var}               # planner: dispatch's env minus it
    b, f = egress(kind="dispatch", vendor="codex")
    assert var not in _env_of(b, f)
    vb, vf = verifier()
    assert all(var not in e for e in RD.render_env(vb, vf).values())


def test_09b_kind_matrix_defaultmode_only_for_the_dispatch_executor():
    """NP-1: ``permissions.defaultMode: acceptEdits`` for alloc kind ``dispatch`` on claude, read back by
    ``expected_effective_cc``; planner, verifier, interactive and login render none."""
    b, f = egress(kind="dispatch")
    assert RD.render_cc_settings(b, f)["permissions"]["defaultMode"] == "acceptEdits"
    assert RD.expected_effective_cc(b, f)["permissions.defaultMode"] == "acceptEdits"
    absent = {"permissions": {"disableBypassPermissionsMode": "disable"}}
    assert "permissions.defaultMode" in RD.readback_refusals(RD.expected_effective_cc(b, f), absent)
    assert "permissions.defaultMode" not in RD.readback_refusals(
        RD.expected_effective_cc(b, f), {"permissions": {"defaultMode": "acceptEdits"}})
    for kind in ("interactive", "login", "planner"):
        for host in ("darwin", "linux"):
            b, f = egress(host=host, kind=kind)
            assert "defaultMode" not in RD.render_cc_settings(b, f)["permissions"], (kind, host)
            assert "permissions.defaultMode" not in RD.expected_effective_cc(b, f)
    b, f = egress(kind="dispatch", vendor="codex")                                    # the codex vendor reads no such key
    assert "defaultMode" not in RD.render_cc_settings(b, f)["permissions"]
    b, f = verifier()
    assert "defaultMode" not in RD.render_cc_settings(b, f)["permissions"]
    assert "permissions.defaultMode" not in RD.expected_effective_cc(b, f)


def test_09c_the_rendered_executor_layer_carries_the_mode_and_the_env_row(rig):
    s = rig.prepared()
    assert rig.render(s) == {"ok": True}, rig.w.last_err
    settings = json.loads((rig.S(s) / "root" / "settings.json").read_text())
    assert settings["permissions"]["defaultMode"] == "acceptEdits"
    assert settings["permissions"]["disableBypassPermissionsMode"] == "disable"
    assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1\n" in (rig.S(s) / "root" / "env").read_text()


def _slot_clone_to_operator_repo_writers() -> set[str]:
    """The modules under ``levain/slot`` and ``levain/dispatch`` that install refs or objects into a git repo, by an
    AST walk for git's installing sub-commands as literal argv constants (a heuristic: an argv assembled from a
    variable, a keyword or a split string is not seen, which is why a new writer is also a review question)."""
    verbs = {"update-ref", "unbundle", "fetch", "am", "cherry-pick", "pull", "commit-tree", "write-tree",
             "push", "rebase", "read-tree", "fast-import", "index-pack", "unpack-objects", "mktree"}
    found = set()
    for sub in ("slot", "dispatch"):
        for p in sorted((REPO_ROOT / "levain" / sub).rglob("*.py")):
            tree = ast.parse(p.read_text())
            for node in ast.walk(tree):
                elts = node.elts if isinstance(node, (ast.List, ast.Tuple)) else node.args if isinstance(
                    node, ast.Call) else []
                if any(isinstance(e, ast.Constant) and e.value in verbs for e in elts):
                    found.add(str(p.relative_to(REPO_ROOT)))
    return found


def test_10_np1_premise_adopt_and_review_are_the_only_writers_to_the_operators_repo():
    """NP-1 (Phill np-b8a7375b "15b") renders ``acceptEdits`` for the executor on this premise: the ONLY path from
    ``<S>/repo`` to the operator's repo is adopt (``slot/adopt_take``, the operator's ``levain session adopt``) and
    review/landing (``dispatch/adopt``, the dispatch artifact's landing). Derived, not listed: the modules that install
    refs or objects in a git repo, by an AST walk. Four more write only a repo they themselves created, in a private
    scratch directory (the importer's ``.work`` repo, adopt-verify's preview repo, the read-set builder's temp
    repo, the workspace snapshot builder's bare ``dest_dir``); each is named here with that reason, and a NEW writer
    anywhere under ``levain/slot`` or ``levain/dispatch`` fails this test until it is classified by a person."""
    operator_writers = {"levain/slot/adopt_take.py", "levain/dispatch/adopt.py"}
    # dispatch/snapshot.py (S10w): build_snapshot only READS the operator's repo (rev-parse, ls-tree, cat-file); its one
    # installing verb is fast-import into dest_dir, a bare repo it mkdirs itself (must not exist, refused inside the
    # operator's repo). Held to that reason by test_10b below.
    private_scratch = {"levain/slot/importer.py", "levain/slot/adopt_verify.py", "levain/dispatch/readset.py",
                       "levain/dispatch/snapshot.py"}
    assert _slot_clone_to_operator_repo_writers() == operator_writers | private_scratch
    # and the adopt/review writers are reached by no root-helper verb: the helper's verbs never import them
    root_side = (REPO_ROOT / "levain" / "slot" / "runtime_helper.py").read_text()
    assert "adopt_take" not in root_side and "dispatch.adopt" not in root_side


def test_10b_the_snapshot_builder_installs_only_into_the_dest_dir_it_made():
    """test_10's ``dispatch/snapshot.py`` entry, held to its reason: every ``_git`` call there that carries an installing
    verb (or ``init``) runs in ``dest_dir`` (its first argument is that name), only inside ``build_snapshot``, and
    ``build_snapshot`` creates ``dest_dir`` with ``os.mkdir`` (so it cannot be a pre-existing repo); every other
    ``_git`` call there is a read-only sub-command against the operator's ``repo``."""
    installing = {"update-ref", "unbundle", "fetch", "am", "cherry-pick", "pull", "commit-tree", "write-tree", "push",
                  "rebase", "read-tree", "fast-import", "index-pack", "unpack-objects", "mktree", "init"}
    reads = {"rev-parse", "ls-tree", "cat-file"}
    tree = ast.parse((REPO_ROOT / "levain" / "dispatch" / "snapshot.py").read_text())
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_snapshot")
    in_build = {id(n) for n in ast.walk(build)}
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_git"]
    assert calls
    seen_install = False
    for c in calls:
        verb = c.args[1].value if len(c.args) > 1 and isinstance(c.args[1], ast.Constant) else None
        target = c.args[0].id if c.args and isinstance(c.args[0], ast.Name) else None
        if verb in installing:
            seen_install = True
            assert target == "dest_dir" and id(c) in in_build, (c.lineno, verb, target)
        else:
            assert verb in reads, (c.lineno, verb)
    assert seen_install
    mkdirs = [n for n in ast.walk(build) if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.mkdir"]
    assert [ast.unparse(m.args[0]) for m in mkdirs] == ["dest_dir"]


def test_11_the_kind_matrix_of_vendor_argv_is_unchanged_but_for_the_executor():
    prefix = VENDOR_PREFIX["linux"]
    s = "s-00000000000000a1"
    today = (f"{prefix}/bin/claude", "--settings", f"{STORE['linux']}/live/{s}/root/settings.json")
    for kind, tk in (("interactive", None), ("login", None), ("planner", "planner"), ("dispatch", "planner")):
        assert SS.vendor_argv("linux", "claude", s, kind=kind, ticket_kind=tk) == today, kind
    assert SS.vendor_argv("linux", "claude", s) == today                               # the old three-argument call
    assert SS.vendor_argv("linux", "codex", s, kind="dispatch", ticket_kind="planner") == (
        f"{prefix}/bin/codex", *RD.CODEX_LAUNCH_ARGS)
    assert SS.vendor_argv("linux", "codex", s) == (f"{prefix}/bin/codex", *RD.CODEX_LAUNCH_ARGS)
    with pytest.raises(Refused) as e:
        SS.vendor_argv("linux", "claude", s, kind="dispatch", ticket_kind=None)
    assert e.value.name == "ticket-facts-missing"
    # the executor on claude: the print-mode exec, token for token (also the premise of the pin below)
    S = f"{STORE['linux']}/live/{s}"
    assert SS.vendor_argv("linux", "claude", s, kind="dispatch", ticket_kind="executor") == (
        f"{prefix}/bin/claude", "-p", "--settings", f"{S}/root/settings.json", "--setting-sources", "user",
        "--strict-mcp-config", "--append-system-prompt-file", f"{S}/in/brief.md", "--output-format", "stream-json",
        "--verbose", "Execute the task in your system prompt.")
    assert SS.EXECUTOR_PROMPT == "Execute the task in your system prompt."


def test_11b_a_verifier_alloc_start_is_still_unbuilt(tmp_path):
    w, la, _ = _world(tmp_path)
    s = _rendered(w)
    p = w.L / "alloc" / f"{s}.json"
    p.write_bytes(R.alloc_bytes(replace(R.parse_alloc(p.read_bytes()), kind="verifier")))
    assert w.main(["start", s])[1]["refused"] == "verb-unbuilt:verifier"
    assert la.spawns == []


# --- the pin (DESIGN D4) ----------------------------------------------------------------------------------------------

def _tree_bytes(root: Path) -> bytes:
    out = b""
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out += p.name.encode() + b"\0" + p.read_bytes() + b"\0"
    return out


def test_12_the_pin_the_brief_reaches_the_vendor_by_file_and_by_no_other_channel(rig):
    tag = os.urandom(8).hex()
    sentinel = f"BRIEF-SENTINEL-{tag}\nsecond line with a \" quote, a space and é\n".encode("utf-8")
    needle = f"BRIEF-SENTINEL-{tag}".encode()
    s = rig.prepared(digest=ST.brief_digest(sentinel))
    (rig.w.L / "staging" / s / "brief.md").write_bytes(sentinel)
    assert rig.render(s) == {"ok": True}, rig.w.last_err
    status, text = raw_main(rig.w, ["start", s])
    assert status == H.EXIT_OK, rig.w.last_err
    (vend,) = rig.la.of("vendor")
    S = f"{STORE['linux']}/live/{s}"
    # (a) the vendor argv, token for token
    assert tuple(vend["spec"]["argv"]) == (
        f"{VENDOR_PREFIX['linux']}/bin/claude", "-p", "--settings", f"{S}/root/settings.json", "--setting-sources",
        "user", "--strict-mcp-config", "--append-system-prompt-file", f"{S}/in/brief.md", "--output-format",
        "stream-json", "--verbose", "Execute the task in your system prompt.")
    # (b) the sentinel is in none of the channels
    for rec in rig.la.spawns:
        assert needle not in rec["spec_json"], rec["target"]                          # spec JSON, env included
        assert all(needle.decode() not in tok for tok in rec["argv"]), rec["target"]
        assert all(needle.decode() not in tok for tok in rec["spec"]["argv"]), rec["target"]
    assert {r["target"] for r in rig.la.spawns} >= {"clone", "vendor"}
    assert needle not in (rig.w.L / "alloc" / f"{s}.json").read_bytes()
    assert needle not in _tree_bytes(rig.S(s) / "root")
    front = rig.w.R / "gw" / "slot0" / "session.json"
    assert front.exists() and needle not in front.read_bytes()
    assert needle.decode() not in repr(rig.w.calls)
    assert needle.decode() not in text
    # (c) the bytes ARE the file, root : slot gid, 0440
    assert (rig.S(s) / "in" / "brief.md").read_bytes() == sentinel
    assert rig.w.mode(f"live/{s}/in/brief.md") == 0o440


def test_13_spawn_wiring_the_executor_gets_null_stdin_and_two_pipe_ends_every_other_spawn_is_today(rig, tmp_path,
                                                                                                  monkeypatch):
    s = rig.prepared()
    assert rig.render(s) == {"ok": True}, rig.w.last_err
    before = len(os.listdir("/dev/fd"))
    status, _ = raw_main(rig.w, ["start", s])
    assert status == H.EXIT_OK, rig.w.last_err
    assert len(os.listdir("/dev/fd")) == before                                        # every fd made was closed
    (vend,) = rig.la.of("vendor")
    kw = vend["kw"]
    assert kw["stdin_null"] is True and set(kw) == {"stdin_null", "stdout_w", "stderr_w"}
    out_w, err_w = kw["stdout_w"], kw["stderr_w"]
    assert out_w > 2 and err_w > 2 and out_w != err_w
    assert vend["kw_open"] == {"stdout_w": True, "stderr_w": True}                    # open while the child was forked
    for rec in rig.la.spawns:
        if rec["target"] != "vendor":
            assert rec["kw"] == {}, rec["target"]                                      # clone, forwarder: today's call
    # the run directory and its two files: root-owned (recorded), 0700 / 0600, created fresh
    assert rig.w.mode(f"live/{s}/run") == 0o700
    assert rig.w.mode(f"live/{s}/run/vendor.out") == 0o600 and rig.w.mode(f"live/{s}/run/vendor.err") == 0o600
    assert rig.w.chown_of(f"live/{s}/run") == (0, 0) and rig.w.chown_of(f"live/{s}/run/vendor.out") == (0, 0)
    # the executor's wait is the capturing one, over the two (pipe, file) pairs
    ((o_r, o_f), (e_r, e_f)) = rig.la.drained[-1]
    assert len({o_r, o_f, e_r, e_f}) == 4


def test_13b_an_interactive_session_spawn_passes_no_new_keywords(tmp_path, monkeypatch):
    w = World(tmp_path)
    la = XLaunches(w, Table())
    wire(w, la, la.table, monkeypatch)
    s = _prepared(w)
    w.session = s
    assert w.main(["render", s], _blob())[1]["ok"], w.last_err
    assert w.main(["start", s])[1]["ok"], w.last_err
    assert all(rec["kw"] == {} for rec in la.spawns)
    assert la.drained == []
    assert not (w.L / "live" / s / "run").exists()


# --- the capture loop (no fork) ----------------------------------------------------------------------------------------

class Alarm:
    """Fails a hang: SIGALRM raises, restored on exit."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def __enter__(self):
        def boom(sig, frm):
            raise AssertionError(f"hung past {self.seconds}s")
        self.old = signal.signal(signal.SIGALRM, boom)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)

    def __exit__(self, *exc):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.old)


def _files(tmp_path):
    out = tmp_path / "vendor.out"
    err = tmp_path / "vendor.err"
    return (os.open(out, os.O_RDWR | os.O_CREAT, 0o600), os.open(err, os.O_RDWR | os.O_CREAT, 0o600)), out, err


def test_14_the_loop_returns_at_the_vendors_exit_not_at_eof_and_keeps_every_byte(tmp_path):
    (fo, fe), out, err = _files(tmp_path)
    o_r, o_w = os.pipe()
    e_r, e_w = os.pipe()                                    # both write ends stay open: a left-behind process holds them
    polls = {"n": 0}
    synced: list = []

    def waitpid(pid, flags):
        polls["n"] += 1
        if polls["n"] == 2:
            os.write(o_w, b"mid-run out ")
        if polls["n"] < 5:
            return 0, 0
        os.write(e_w, b"last words")
        return pid, 3 << 8                                  # exited with 3, with data still unread in the pipe

    os.write(o_w, b"first out ")
    with Alarm(10):
        res = H.wait_child_capturing(4242, [(o_r, fo), (e_r, fe)], waitpid=waitpid, sleep=lambda s: None,
                                     fsync=synced.append)
    assert res.code == 3 and polls["n"] == 5
    assert out.read_bytes() == b"first out mid-run out " and err.read_bytes() == b"last words"
    assert res.written == [len(b"first out mid-run out "), len(b"last words")] and res.dropped == [0, 0]
    assert synced == [fo, fe]                               # both files fsynced
    for fd in (o_r, o_w, e_r, e_w, fo, fe):
        os.close(fd)


def test_14b_a_signalled_vendor_answers_128_plus_the_signal(tmp_path):
    (fo, fe), _, _ = _files(tmp_path)
    r, w_ = os.pipe()
    r2, w2 = os.pipe()
    res = H.wait_child_capturing(1, [(r, fo), (r2, fe)], waitpid=lambda p, f: (p, signal.SIGKILL),
                                 sleep=lambda s: None, fsync=lambda fd: None)
    assert res.code == 128 + signal.SIGKILL
    for fd in (r, w_, r2, w2, fo, fe):
        os.close(fd)


def test_15_the_cap_writes_exactly_the_cap_and_counts_the_rest(tmp_path):
    (fo, fe), out, err = _files(tmp_path)
    o_r, o_w = os.pipe()
    e_r, e_w = os.pipe()
    os.set_blocking(o_w, False)
    total = H.VENDOR_LOG_CAP + 10
    sent = {"n": 0}
    chunk = b"x" * 32768

    def waitpid(pid, flags):
        if sent["n"] < total:
            n = min(len(chunk), total - sent["n"])
            sent["n"] += os.write(o_w, chunk[:n])
            return 0, 0
        return pid, 0

    with Alarm(60):
        res = H.wait_child_capturing(7, [(o_r, fo), (e_r, fe)], waitpid=waitpid, sleep=lambda s: None,
                                     fsync=lambda fd: None)
    assert out.stat().st_size == H.VENDOR_LOG_CAP == 16 * 1024 ** 2
    assert res.written[0] == H.VENDOR_LOG_CAP and res.dropped[0] == 10 and res.dropped[1] == 0
    for fd in (o_r, o_w, e_r, e_w, fo, fe):
        os.close(fd)


def test_15b_a_failing_log_write_stops_that_stream_and_keeps_draining(tmp_path):
    r, w_ = os.pipe()
    r2, w2 = os.pipe()
    ro = os.open(tmp_path / "ro", os.O_RDONLY | os.O_CREAT, 0o600)                       # a write on it fails
    (fo, fe), _, err = _files(tmp_path)
    os.write(w_, b"abc")
    os.write(w2, b"def")
    n = {"n": 0}

    def waitpid(pid, flags):
        n["n"] += 1
        return (pid, 0) if n["n"] > 2 else (0, 0)

    res = H.wait_child_capturing(5, [(r, ro), (r2, fe)], waitpid=waitpid, sleep=lambda s: None, fsync=lambda fd: None)
    assert res.written == [0, 3] and res.dropped == [3, 0] and res.code == 0
    for fd in (r, w_, r2, w2, ro, fo, fe):
        os.close(fd)


def test_16_a_real_child_that_leaves_a_process_holding_the_pipes_is_waited_at_its_exit(tmp_path):
    (fo, fe), out, err = _files(tmp_path)
    o_r, o_w = os.pipe()
    e_r, e_w = os.pipe()
    p = subprocess.Popen(["/bin/sh", "-c", "echo out; echo err >&2; sleep 60 & echo $! >&2; exit 3"],
                         stdin=subprocess.DEVNULL, stdout=o_w, stderr=e_w)
    os.close(o_w)
    os.close(e_w)
    try:
        with Alarm(30):
            res = H.wait_child_capturing(p.pid, [(o_r, fo), (e_r, fe)])
        assert res.code == 3
        assert out.read_bytes() == b"out\n"
        lines = err.read_bytes().split(b"\n")
        assert lines[0] == b"err"
        sleeper = int(lines[1])
        assert os.kill(sleeper, 0) is None                   # the left-behind process still holds the write ends
    finally:
        try:
            os.kill(sleeper, signal.SIGKILL)
        except (NameError, ProcessLookupError):
            pass
        for fd in (o_r, e_r, fo, fe):
            os.close(fd)


def test_17_a_vendor_capture_closes_every_fd_it_opened_once(tmp_path):
    cap = SS.VendorCapture()
    cap.out_r, cap.out_w = os.pipe()
    cap.err_r, cap.err_w = os.pipe()
    cap.out_f = os.open(tmp_path / "a", os.O_WRONLY | os.O_CREAT)
    cap.err_f = os.open(tmp_path / "b", os.O_WRONLY | os.O_CREAT)
    fds = [cap.out_r, cap.out_w, cap.err_r, cap.err_w, cap.out_f, cap.err_f]
    cap.close_writers()
    assert [_fd_open(fd) for fd in fds] == [True, False, True, False, True, True]
    cap.close()
    cap.close()                                              # idempotent
    assert not any(_fd_open(fd) for fd in fds)


# --- coordinator additions from the S8a L3 ------------------------------------------------------------------------------

def test_18_a_brokered_executor_on_codex_is_refused_at_the_point_of_use():
    """D2: CONTROL cannot know a brokered executor's vendor at mint, so a brief on a brokered executor whose blob
    is a codex one is refused in check_ticket; the same facts on claude pass, and a codex executor WITHOUT a brief
    is not refused for want of one."""
    brokered = _facts("executor", route="glm", model_requested="m", brief_digest=BRIEF_DIGEST)
    with pytest.raises(Refused) as e:
        RD.check_ticket(G.RenderBlob(vendor="codex", routes=("glm",), model="m"), "dispatch", "egress", "tk", brokered)
    assert e.value.name == "ticket-brief-forbidden"
    RD.check_ticket(G.RenderBlob(vendor="claude", routes=("glm",), model="m"), "dispatch", "egress", "tk", brokered)
    bare = {k: v for k, v in brokered.items() if k != "brief_digest"}
    RD.check_ticket(G.RenderBlob(vendor="codex", routes=("glm",), model="m"), "dispatch", "egress", "tk", bare)


def test_18b_an_executor_ticket_on_codex_never_reaches_the_interactive_argv():
    """An executor ticket on codex has no print-mode form and no brief: vendor_argv refuses it by name instead of
    returning codex's interactive argv (which would carry no brief), for a ticket with or without a brief."""
    s = "s-00000000000000a1"
    with pytest.raises(Refused) as e:
        SS.vendor_argv("linux", "codex", s, kind="dispatch", ticket_kind="executor")
    assert e.value.name == "executor-vendor-unsupported"
    # check_ticket's vendor-mismatch gate still speaks first for a passthrough executor ticket on the wrong blob
    # (test_ticket_vendor_mismatch in the passthrough tests); a non-dispatch kind is untouched
    assert SS.vendor_argv("linux", "codex", s, kind="interactive", ticket_kind="executor")[-len(RD.CODEX_LAUNCH_ARGS):] \
        == tuple(RD.CODEX_LAUNCH_ARGS)


def test_19_write_brief_is_0600_under_a_restrictive_umask(tmp_path):
    d = tmp_path / "staging"
    d.mkdir()
    old = os.umask(0o477)
    try:
        ST.write_brief(d, "a brief\n")
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(d / "brief.md").st_mode) == 0o600
    assert (d / "brief.md").read_bytes() == b"a brief\n"
