"""Row 33 R2 S2 (project_memory/1022-8/upgrade/DESIGN.md §2.2, §2.3 R2-R4a, §5 R2 tests 1 and 2): the handover.

* ``test_phase_b_adopts_both_locks``: phase A takes the install lock and the pool lock, then REALLY ``execv``s
  (same PID) into a generation's ``repair_continue``; phase B re-gathers without waiting, while a second run in
  another process refuses ``install-busy`` and cannot take the pool lock. Two mutants of the same run go red: the
  descriptors left CLOEXEC (``set_inheritable`` a no-op) and a phase B that calls ``gather_facts``.
* The probe (R3a) in a real bounded child: a probe that sleeps past its timeout refuses ``source-broken`` and both
  locks are released; a package from before row 33 refuses ``downgrade-unsupported``; a kind the host records that
  the staged code cannot remove refuses ``downgrade-unsupported:<kind>``; the host is put back each time.
* ``test_flip_survives_a_kill_between_its_acts``: ``test_current_is_validated_at_gather`` holds only the READ of a
  stray ``current.new`` (it never runs the flip), so the kill between the flip's symlink and its rename is here.
"""
from __future__ import annotations

import fcntl
import functools
import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

import levain
from levain.slot import Refused
from levain.slot import generation as G
from levain.slot import install_helper as IH
from levain.slot import root_tree
from levain.slot.grammar import parse_install_argv
from levain.slot.intent import Intent, install_lock, intent_text, read_intent

HOST = "darwin" if sys.platform == "darwin" else "linux"
GEN = "gen-" + "ab" * 8
OP, TXN = "op", "c" * 32
WT = Path(levain.__file__).resolve().parent.parent


def _mine(st) -> bool:
    return st.st_uid == os.getuid()


def _h11_dir() -> Path:
    import h11

    return Path(h11.__file__).resolve().parent


def _real_generation(tree: Path, gen: str = GEN) -> Path:
    """``tree/gen`` holding THIS worktree's levain (and h11) by link: the code under test, booted from a generation."""
    g = tree / gen
    g.mkdir(parents=True)
    os.symlink(WT / "levain", g / "levain")
    os.symlink(_h11_dir(), g / "h11")
    return g


def _fake_generation(tree: Path, gen: str, install_helper: str) -> Path:
    """``tree/gen`` holding a package whose ``levain.slot.install_helper`` is ``install_helper``."""
    g = tree / gen / "levain" / "slot"
    g.mkdir(parents=True)
    (tree / gen / "levain" / "__init__.py").write_text('__version__ = "0.0.1"\n')
    (g / "__init__.py").write_text("")
    (g / "install_helper.py").write_text(install_helper)
    return tree / gen


def _store(tmp: Path) -> Path:
    L = tmp / "L"
    (L / "slots").mkdir(parents=True)
    (L / "sessions").mkdir()
    (L / "policy").mkdir()
    lock = L / "slots" / "pool.lock"
    lock.write_text("")
    os.chmod(lock, 0o600)
    return L


# --- test 1: phase B adopts both locks, after a real execv ----------------------------------------------------------

_PHASE_B = r'''
import json, os, subprocess, sys
from pathlib import Path
from levain.slot import install_helper as IH
from levain.slot import runtime_helper as RH
from levain.slot.install_helper import ContinueSeams, InstallPlan

def _run(argv, **kw):
    a = " ".join(argv)
    out = {"UniqueID": "root 0\n", "PrimaryGroupID": "wheel 0\n"}.get(argv[-1], "")
    if argv[-1] == "passwd":
        out = "root:x:0:0:root:/root:/bin/sh\n"
    if argv[-1] == "group":
        out = "root:x:0:\n"
    return subprocess.CompletedProcess(argv, 0, out, "")

def main(lock_fd, pool_fd, gen, *, caller_uid, fault=None):
    cfg = json.loads(os.environ["R2S2_CFG"])
    L = Path(cfg["store"])
    rs = RH.Seams(host=cfg["host"], store=L, trusted=lambda st: True, lock_wait_s=0.2)
    if cfg["mutant"] == "gather":           # a phase B that re-gathers the old way (red): gather_facts's own
        real = IH._gather_held              # body, a FRESH Quiesce on the store, in place of the adopted one
        def gather_held(cmd, host, uid, d, rs_, L_, q, **kw):
            return real(cmd, host, uid, d, rs_, L_, IH.Quiesce(L_, waits=rs_), **kw)
        IH._gather_held = gather_held

    def seen(g):
        q = g.quiesce
        print(json.dumps({"phase": "B", "pid": os.getpid(),
                          "inheritable": [os.get_inheritable(lock_fd), os.get_inheritable(pool_fd)],
                          "registered": IH._QUIESCING.get(IH._store_key(L)) is q and q.fd == pool_fd,
                          "journal": g.facts.intent.generation if g.facts.intent else None}), flush=True)
        sys.stdin.readline()                # the parent's second run happens here, while phase B holds both
    def mark(n):                            # test 3: one marker file per R-step that ran
        return IH.Step(f"mark {n}", call=lambda: (Path(cfg["marks"], n).write_text(""), (True, ""))[1])

    def plan(c, g, fault=None):
        if not cfg.get("marks"):
            return InstallPlan("repair", cfg["host"], ())
        return InstallPlan("repair", cfg["host"], tuple(
            st for r in ("R4b", "R5", "R6") for st in IH._faulted(fault, r, [mark(r)])))
    cs = ContinueSeams(host=cfg["host"], lock_path=Path(cfg["lock"]), owner_ok=lambda st: st.st_uid == os.getuid(),
                       store_seams=rs, state_dir=Path(cfg["state"]), run=_run, user_name=lambda uid: "op",
                       after_gather=seen, plan=plan,
                       apply=IH.apply_steps if cfg.get("marks") else (lambda p, e: None))
    raise SystemExit(IH.repair_continue(lock_fd, pool_fd, gen, caller_uid=caller_uid, fault=fault, seams=cs))
'''

_PHASE_A = r'''
import fcntl, json, os, sys
cfg = json.loads(os.environ["R2S2_CFG"])
sys.path.insert(0, cfg["wt"])
from pathlib import Path
from levain.slot import install_helper as IH
from levain.slot.intent import install_lock
if cfg["mutant"] == "cloexec":              # the descriptors left CLOEXEC across the exec (red)
    IH.os.set_inheritable = lambda fd, v: None
print(json.dumps({"phase": "A", "pid": os.getpid()}), flush=True)
with install_lock(path=Path(cfg["lock"]), owner_ok=lambda st: st.st_uid == os.getuid()) as lock_fd:
    pool = os.open(os.path.join(cfg["store"], "slots", "pool.lock"), os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    fcntl.flock(pool, fcntl.LOCK_EX)
    fault = tuple(cfg["fault"]) if cfg.get("fault") and cfg["mutant"] != "inprocess" else None
    IH.exec_phase_b(cfg["host"], cfg["gen"], lock_fd, pool, os.getuid(), python=sys.executable,
                    tree=Path(cfg["tree"]), entry="__import__('_phase_b').main", fault=fault)
'''


def _line(p: subprocess.Popen, timeout: float = 60.0) -> dict:
    """One line from the child, read a byte at a time from the unbuffered pipe (fix1 ruling 6: no wrapper buffers a
    second line behind ``select``'s back)."""
    import time

    assert p.stdout is not None
    fd, buf, deadline = p.stdout.fileno(), b"", time.monotonic() + timeout
    while not buf.endswith(b"\n"):
        r, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
        assert r, "no line from the child in time"
        b = os.read(fd, 1)
        assert b, "the child closed its stdout before a whole line"
        buf += b
    return json.loads(buf)


def _phase_b_run(tmp: Path, mutant: str, **extra) -> tuple[subprocess.Popen, dict, Path, Path]:
    tree = tmp / "tree"
    g = _real_generation(tree)
    (g / "_phase_b.py").write_text(_PHASE_B)
    L, state = _store(tmp), tmp / "state"
    state.mkdir()
    (state / "install-intent").write_text(intent_text(Intent("repair", OP, txn=TXN, generation=GEN)))
    (tmp / "phase_a.py").write_text(_PHASE_A)
    cfg = dict(wt=str(WT), host=HOST, gen=GEN, tree=str(tree), store=str(L), state=str(state),
               lock=str(state / "install.lock"), mutant=mutant, **extra)
    env = {**os.environ, "R2S2_CFG": json.dumps(cfg)}
    p = subprocess.Popen([sys.executable, str(tmp / "phase_a.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, bufsize=0, env=env)
    a = _line(p)
    assert a["phase"] == "A"
    return p, a, state / "install.lock", L / "slots" / "pool.lock"


def _pool_free(path: Path) -> bool:
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def _finish(p: subprocess.Popen) -> tuple[dict, int, str]:
    out, err = p.communicate(b"\n", timeout=60)
    return json.loads(out.strip().splitlines()[-1]), p.returncode, err.decode(errors="replace")


def test_phase_b_adopts_both_locks(tmp_path: Path) -> None:
    p, a, lock, pool = _phase_b_run(tmp_path, "")
    try:
        b = _line(p)
        # the same process (sudo still waits on it), past a real exec, holding both locks it never re-opened
        assert b == {"phase": "B", "pid": a["pid"], "inheritable": [False, False], "registered": True,
                     "journal": GEN}
        with pytest.raises(Refused) as e:                     # a second run, in another process
            with install_lock(path=lock, owner_ok=_mine):
                pass
        assert e.value.name == "install-busy"
        assert not _pool_free(pool)
    finally:
        last, rc, err = _finish(p)
    assert (last, rc) == ({"ok": True, "generation": GEN, "steps": 0, "verb": "repair"}, 0), err
    with install_lock(path=lock, owner_ok=_mine):             # released at phase B's end
        assert _pool_free(pool)


@pytest.mark.parametrize("mutant, refused", [("cloexec", "bad-root"), ("gather", "pool-locked")])
def test_phase_b_adopts_both_locks_red_mutants(tmp_path: Path, mutant: str, refused: str) -> None:
    """The run above, with the defect each half of it exists to catch: it goes red (phase B never gathers)."""
    p, _, lock, pool = _phase_b_run(tmp_path, mutant)
    last, rc, err = _finish(p)
    assert (last, rc) == ({"ok": False, "refused": refused}, 1), err
    with install_lock(path=lock, owner_ok=_mine):
        assert _pool_free(pool)


# --- the probe (R3a), a real bounded child --------------------------------------------------------------------------

_HANDOVER = 'def repair_continue(*a, **k):\n    return 0\n'


def _phase_a(tmp: Path, gen_maker, *, prior: Intent | None = None, shape: G.InstallShape | None = None,
             timeout: float = 30.0, apply=None, hold: bool = True, python: str = sys.executable):
    """A phase A that has staged ``GEN`` (the plan's hash step ran), holding a real install lock and pool lock, run
    through :func:`IH.run`'s own lock/release order with the real :func:`IH.repair_handover` and probe. ``prior``:
    the journal on disk before phase A (the default ``apply`` then stands in for R1 and R2-R3: it writes the run's
    fresh journal unless ``prior`` is unfinished, and leaves ``gen_maker``'s generation as the staged one)."""
    tree, state = tmp / "tree", tmp / "state"
    state.mkdir()
    gen_maker(tree)
    L = _store(tmp)
    jpath = state / "install-intent"
    first = prior or Intent("repair", OP, txn=TXN)
    if prior is not None:
        jpath.write_text(intent_text(prior))                # on disk before phase A
    cell = root_tree.Staged(target=GEN, placed=True)        # the hash and place steps ran (a fresh stage)
    plan = IH.InstallPlan("repair", HOST, (), intent=first, handover=cell)
    q = IH.Quiesce(L, waits=IH.RH.Seams(host=HOST, store=L, trusted=lambda st: True, lock_wait_s=0.05))
    facts = IH.HostFacts(host=HOST, caller_uid=os.getuid(), intent=prior, install_shape=shape)
    rf = IH.RepairFacts(current=None)
    execs: list = []
    acts: list = []

    def gather(cmd, host, uid):
        if hold:
            q.hold()
        return IH.Gathered(facts, quiesce=q, repair=rf)

    def r1(p, e):
        acts.append("apply")
        jpath.write_text(intent_text(first))                # R1 ran
        return None

    seams = IH.Seams(euid=lambda: 0, host=lambda: HOST,
                     lock=lambda host: install_lock(path=state / "install.lock", owner_ok=_mine),
                     gather=gather, plan=lambda cmd, g: plan, apply=apply or r1,
                     handover=functools.partial(IH.repair_handover, tree=tree, intent_path=jpath,
                                                probe=functools.partial(IH.run_probe, python=python,
                                                                        tree=tree, timeout=timeout),
                                                execv=lambda *a: execs.append(a)))
    seams.acts = acts                                        # type: ignore[attr-defined]
    return seams, q, state, L, tree, jpath, execs


def _go(seams) -> dict:
    import io

    return IH.run(["repair", "--source", "/opt/x/levain"], env={"SUDO_UID": str(os.getuid())}, seams=seams,
                  err=io.StringIO())


def _monkey_built(monkeypatch) -> None:
    assert "repair" in IH.BUILT_VERBS                                      # S3 built it (S2's rows patched it in)


def test_a_probe_past_its_timeout_is_source_broken_and_both_locks_are_released(tmp_path: Path, monkeypatch) -> None:
    _monkey_built(monkeypatch)
    sleeper = "import time\ndef repair_probe():\n    time.sleep(30)\n    return 0\n" + _HANDOVER
    seams, q, state, L, tree, jpath, execs = _phase_a(tmp_path, lambda t: _fake_generation(t, GEN, sleeper),
                                                      timeout=1.0)
    with pytest.raises(Refused) as e:
        _go(seams)
    assert e.value.name == "source-broken" and "past 1 s" in e.value.detail
    assert not q.held and not execs
    with install_lock(path=state / "install.lock", owner_ok=_mine):
        assert _pool_free(L / "slots" / "pool.lock")
    assert not (tree / GEN).exists() and not jpath.exists()   # the host as phase A found it: no gen, no journal


@pytest.mark.parametrize("helper, refused", [
    ("def setup():\n    pass\n", "downgrade-unsupported"),                          # a package from before row 33
    ("import nonexistent_module_r2s2\n", "source-broken"),                         # it does not import
    ("def repair_probe():\n    print('{\"ok\":true,\"kinds\":[]} ')\n    return 0\n" + _HANDOVER, "source-broken"),
    ("def repair_probe():\n    print('{\"kinds\":[],\"ok\":true}')\n    print('x')\n    return 0\n" + _HANDOVER,
     "source-broken"),
    ("def repair_probe():\n    return 2\n" + _HANDOVER, "source-broken"),
    ("def repair_probe():\n    print('y' * 70000)\n    return 0\n" + _HANDOVER, "source-broken"),   # past the cap
])
def test_the_probe_refuses_by_name(tmp_path: Path, monkeypatch, helper: str, refused: str) -> None:
    _monkey_built(monkeypatch)
    seams, q, *_rest = _phase_a(tmp_path, lambda t: _fake_generation(t, GEN, helper))
    with pytest.raises(Refused) as e:
        _go(seams)
    assert e.value.name == refused and not q.held


def test_the_probe_holds_every_recorded_kind_and_restores_an_earlier_journal(tmp_path: Path, monkeypatch) -> None:
    """A recorded kind the staged code lacks refuses ``downgrade-unsupported:<kind>``; an unfinished journal that was
    there before phase A is put back as it was read."""
    _monkey_built(monkeypatch)
    lacks = _answers(["unit/1"])
    prior = Intent("repair", OP, txn=TXN, managed=(("netns/1", "levain-e0"),))
    seams, q, state, L, tree, jpath, execs = _phase_a(tmp_path, lambda t: _fake_generation(t, GEN, lacks),
                                                      prior=prior)
    with pytest.raises(Refused) as e:
        _go(seams)
    assert e.value.name == "downgrade-unsupported:netns/1" and not execs
    assert read_intent(HOST, path=jpath) == prior and not (tree / GEN).exists()


def test_the_real_staged_code_passes_the_probe_and_phase_a_execs(tmp_path: Path, monkeypatch) -> None:
    """This worktree's own code as the staged generation: the probe returns every kind, and phase A hands its two
    lock descriptors to the exec (inheritable) instead of releasing them."""
    _monkey_built(monkeypatch)
    shape = G.InstallShape("gen-" + "0" * 16, "0.9.0", "0.16.0", TXN, {}, (("unit/1", "keys"),))
    seams, q, state, L, tree, jpath, execs = _phase_a(tmp_path, _real_generation, shape=shape)
    assert IH.run_probe(HOST, GEN, shape, None, python=sys.executable, tree=tree) == tuple(sorted(G.KINDS))
    _go(seams)                                               # the fake execv returns, so run() goes on
    (python, argv), = execs
    assert argv[:3] == [python, "-IS", "-c"] and f"repair_continue(" in argv[3] and repr(GEN) in argv[3]
    assert str(tree / GEN) in argv[3] and "current" not in argv[3]


def test_repair_probe_reads_both_records(tmp_path: Path) -> None:
    import io

    shape = G.InstallShape("gen-" + "0" * 16, "0.9.0", "0.16.0", TXN, {}, ())
    j = Intent("repair", OP, txn=TXN, generation=GEN)
    good = json.dumps({"shape": G.shape_text(shape), "journal": intent_text(j), "operator": OP}).encode()
    out = io.StringIO()
    assert IH.repair_probe(stdin=io.BytesIO(good), out=out, host=HOST) == 0
    assert json.loads(out.getvalue()) == {"ok": True, "kinds": sorted(G.KINDS), "handover": IH.HANDOVER_ABI}
    for bad in (b"{}", json.dumps({"shape": "{}", "journal": None, "operator": OP}).encode(), b"not json",
                json.dumps({"shape": None, "journal": None}).encode()):
        assert IH.repair_probe(stdin=io.BytesIO(bad), out=io.StringIO(), host=HOST) == IH.PROBE_EXIT_UNSUPPORTED


# --- the stage plan (phase A) and phase B's plan ---------------------------------------------------------------------

def test_phase_a_plans_only_r1_to_r3_and_journals_the_staged_id(tmp_path: Path) -> None:
    from tests.test_repair_plan import _pre_change

    facts, rf = _pre_change(tmp_path)
    plan = IH.plan_for(parse_install_argv(["repair", "--source", "/opt/x/levain"]), IH.Gathered(facts, repair=rf))
    whys = [s.why for s in plan.steps]
    assert plan.handover is not None and plan.handover.target is None and plan.generation is None
    assert whys[0].startswith("journal") and plan.intent.generation is None              # R1 first, no id yet
    h = next(i for i, w in enumerate(whys) if w.startswith("hash the staged copy"))
    assert all(not w.startswith(("copy", "hash")) for w in whys[:1])
    assert whys[h + 1].startswith("place the staged generation") and whys[-1].startswith("journal the staged")
    assert not [w for w in whys if w.startswith(("stop", "flip", "write the install record"))]


def test_the_staged_hash_is_the_id_and_the_source_must_match_it(tmp_path: Path) -> None:
    from tests.test_repair_plan import _package

    lv, h11 = _package(tmp_path)
    import shutil

    partial = tmp_path / "staging.partial"
    for name, src in (("levain", lv), ("h11", h11)):
        shutil.copytree(src, partial / name)
    want = G.generation_id(lv, h11)
    cell = root_tree.Staged()
    assert root_tree._hash_staged(cell, partial, lv, h11, None) == (True, want) and cell.target == want
    cell = root_tree.Staged()
    assert not root_tree._hash_staged(cell, partial, lv, h11, "gen-" + "f" * 16)[0]
    assert cell.refused.name == "source-mismatch:gen-" + "f" * 16                          # U13
    (lv / "__init__.py").write_text('__version__ = "1.0.1"\n')                           # changed under the copy
    cell = root_tree.Staged()
    assert not root_tree._hash_staged(cell, partial, lv, h11, None)[0] and cell.refused.name == "source-changed"
    # X2-M4: identical but for one file's execute bit is a different generation
    (lv / "__init__.py").write_text('__version__ = "1.0.0"\n')
    assert G.generation_id(lv, h11) == want
    os.chmod(lv / "slot" / "__init__.py", 0o755)
    assert G.generation_id(lv, h11) != want


def test_phase_b_plans_from_r4b_into_the_journalled_generation(tmp_path: Path, monkeypatch) -> None:
    from dataclasses import replace

    from tests.test_repair_plan import _gen_tree, _pre_change

    facts, rf = _pre_change(tmp_path)
    lv, h11 = rf.source, rf.h11
    target = G.generation_id(lv, h11)
    _gen_tree(tmp_path, lv, h11, target, monkeypatch)
    j = Intent("repair", "phill", txn=TXN, generation=target)
    facts = replace(facts, intent=j)
    rf = replace(rf, source=None, h11=None, current=None)
    plan = IH.plan_repair(parse_install_argv(["repair"]), facts, rf, program_exists=lambda p: True, continuing=target)
    whys = [s.why for s in plan.steps]
    assert plan.generation == target and plan.generation_change
    assert whys[0].startswith("journal the shared-group gids")                            # R4b: phase B's first act
    assert not [w for w in whys if w.startswith(("copy", "clear", "journal the generation"))]
    assert any(w.startswith("flip ") for w in whys)
    with pytest.raises(Refused) as e:                                                      # without the handover:
        IH.plan_repair(parse_install_argv(["repair"]), facts, rf, program_exists=lambda p: True)
    assert e.value.name == f"source-required:{target}"
    with pytest.raises(Refused) as e:
        IH.plan_repair(parse_install_argv(["repair"]), facts, rf, program_exists=lambda p: True,
                       continuing="gen-" + "e" * 16)
    assert e.value.name == "handover-mismatch"


def test_adopt_lock_refuses_another_file_or_a_closed_descriptor(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for p in (a, b):
        p.write_text("")
        os.chmod(p, 0o600)
    fd = os.open(a, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)                           # adopted only while held (fix1 ruling 5)
    try:
        IH.adopt_lock(fd, a, owner_ok=_mine)
        for path in (b, tmp_path / "missing"):
            with pytest.raises(Refused) as e:
                IH.adopt_lock(fd, path, owner_ok=_mine)
            assert e.value.name == "bad-root"
        os.chmod(a, 0o644)
        with pytest.raises(Refused):
            IH.adopt_lock(fd, a, owner_ok=_mine)
    finally:
        os.close(fd)
    with pytest.raises(Refused) as e:
        IH.adopt_lock(fd, a, owner_ok=_mine)
    assert e.value.name == "bad-root" and "did not survive" in e.value.detail


# --- L3 r1 fix1 (head 1022+21 rulings 1-7, DESIGN §2.2's [r5] paragraphs) -------------------------------------------

def _answers(kinds, handover=IH.HANDOVER_ABI) -> str:
    """A staged ``install_helper`` whose probe answers ``kinds`` and the ``handover`` ABI (omitted when None)."""
    rec = {"kinds": sorted(kinds), "ok": True}
    if handover is not None:
        rec["handover"] = handover
    return ("import json\ndef repair_probe():\n"
            f"    print(json.dumps({rec!r}, separators=(',', ':'), sort_keys=True))\n    return 0\n" + _HANDOVER)


NEW_KIND = ("newkind/1", G.Opaque('"levain-x"'))


def test_phase_a_validates_an_unfinished_journal_generically(tmp_path: Path) -> None:
    """Ruling 1 (codex HIGH 2): a journal naming a kind the INCUMBENT's table lacks still plans phase A (the staged
    code judges kind capability); bare repair, in-process in the incumbent, still refuses it."""
    from dataclasses import replace

    from tests.test_repair_plan import OP as POP, _pre_change

    facts, rf = _pre_change(tmp_path)
    facts = replace(facts, intent=Intent("repair", POP, txn=TXN, managed=(NEW_KIND,)))
    plan = IH.plan_for(parse_install_argv(["repair", "--source", "/opt/x/levain"]), IH.Gathered(facts, repair=rf))
    assert plan.handover is not None and plan.intent.managed == (NEW_KIND,)    # carried into R1's journal as read
    with pytest.raises(Refused) as e:
        IH.plan_repair(parse_install_argv(["repair"]), facts, replace(rf, source=None, h11=None),
                       program_exists=lambda p: True)
    assert e.value.name == "downgrade-unsupported:newkind/1"
    shape = G.InstallShape("gen-" + "0" * 16, "0.9.0", "0.16.0", "d" * 32, {}, (NEW_KIND,))
    with pytest.raises(Refused) as e:                       # the record (not the journal) is still checked here
        IH.plan_for(parse_install_argv(["repair", "--source", "/opt/x/levain"]),
                    IH.Gathered(replace(facts, install_shape=shape), repair=rf))
    assert e.value.name in ("downgrade-unsupported:newkind/1", "bad-record")


@pytest.mark.parametrize("staged_kinds, refused", [(("unit/1", "newkind/1"), None),
                                                   (("unit/1",), "downgrade-unsupported:newkind/1")])
def test_the_staged_probe_judges_a_journalled_kind(tmp_path: Path, monkeypatch, staged_kinds, refused) -> None:
    """Ruling 1's row through the real probe: a staged package that knows the kind takes the host over; one that
    lacks it refuses by the kind's name."""
    _monkey_built(monkeypatch)
    prior = Intent("repair", OP, txn=TXN, managed=(NEW_KIND,))
    seams, q, state, L, tree, jpath, execs = _phase_a(tmp_path, lambda t: _fake_generation(t, GEN,
                                                                                          _answers(staged_kinds)),
                                                      prior=prior)
    if refused is None:
        _go(seams)
        assert len(execs) == 1
    else:
        with pytest.raises(Refused) as e:
            _go(seams)
        assert e.value.name == refused and not execs and read_intent(HOST, path=jpath) == prior


@pytest.mark.parametrize("handover", [None, 2])
def test_a_probe_with_another_handover_abi_refuses_before_the_exec(tmp_path: Path, monkeypatch, handover) -> None:
    """Ruling 2 (codex MED 3): the probe answers a ``handover`` ABI; any other (or none) refuses
    ``downgrade-unsupported:handover`` before the exec, the host restored."""
    _monkey_built(monkeypatch)
    seams, q, state, L, tree, jpath, execs = _phase_a(tmp_path, lambda t: _fake_generation(
        t, GEN, _answers(G.KINDS, handover)))
    with pytest.raises(Refused) as e:
        _go(seams)
    assert e.value.name == "downgrade-unsupported:handover" and not execs
    assert not (tree / GEN).exists() and not jpath.exists() and not q.held


def test_a_failed_phase_a_step_restores_the_host(tmp_path: Path, monkeypatch) -> None:
    """Ruling 3: a failed step (here R2's copy, after R1 journalled and the partial was made) puts the host back."""
    _monkey_built(monkeypatch)

    def failing(p, e):
        (tree / root_tree.STAGING).mkdir(parents=True)
        jpath.write_text(intent_text(Intent("repair", OP, txn=TXN)))
        return "[3/14] copy the levain package FAILED: cp exited 1"

    seams, q, state, L, tree, jpath, execs = _phase_a(tmp_path, lambda t: t.mkdir(), apply=lambda p, e: failing(p, e))
    with pytest.raises(Refused) as e:
        _go(seams)
    assert e.value.name == "step-failed" and "cp exited 1" in e.value.detail and not execs
    assert not jpath.exists() and not (tree / root_tree.STAGING).exists() and not q.held


def test_a_probe_that_cannot_start_restores_the_host(tmp_path: Path, monkeypatch) -> None:
    """Ruling 3 (both seats): an OSError launching the probe (its interpreter gone) is ``source-broken``, host put
    back (the staged generation and the run's journal removed)."""
    _monkey_built(monkeypatch)
    seams, q, state, L, tree, jpath, execs = _phase_a(tmp_path, lambda t: _fake_generation(t, GEN, _answers(G.KINDS)),
                                                      python=str(tmp_path / "no-such-python"))
    with pytest.raises(Refused) as e:
        _go(seams)
    assert e.value.name == "source-broken" and "could not start" in e.value.detail and not execs
    assert not (tree / GEN).exists() and not jpath.exists() and not q.held


def test_no_pool_lock_refuses_before_any_phase_a_act(tmp_path: Path, monkeypatch) -> None:
    """Ruling 3 (codex 5): the pool-lock check runs BEFORE phase A's first act, so it has nothing to undo."""
    _monkey_built(monkeypatch)
    seams, q, state, L, tree, jpath, execs = _phase_a(tmp_path, lambda t: _fake_generation(t, GEN, _answers(G.KINDS)),
                                                      hold=False)
    with pytest.raises(Refused) as e:
        _go(seams)
    assert e.value.name == "bad-root" and seams.acts == [] and not execs and not jpath.exists()   # type: ignore[attr-defined]


def test_a_restore_that_cannot_rewrite_the_journal_says_so(tmp_path: Path, monkeypatch) -> None:
    """Ruling 3 (complement LOW 3): a restore whose journal write fails appends ``rollback-incomplete: <what>``."""
    _monkey_built(monkeypatch)
    prior = Intent("repair", OP, txn=TXN, managed=(("netns/1", "levain-e0"),))
    seams, q, state, L, tree, jpath, execs = _phase_a(tmp_path, lambda t: _fake_generation(t, GEN,
                                                                                          _answers(["unit/1"])),
                                                      prior=prior)
    monkeypatch.setattr(IH, "write_intent_atomically", lambda path, text: (False, "disk full (test)"))
    with pytest.raises(Refused) as e:
        _go(seams)
    assert e.value.name == "downgrade-unsupported:netns/1"
    assert "rollback-incomplete:" in e.value.detail and "disk full (test)" in e.value.detail


def test_the_restore_keeps_a_generation_that_was_there_before(tmp_path: Path, monkeypatch) -> None:
    """Ruling 4 (complement LOW 4): ``gen-<id>`` existed (a retained rollback target); ``_place`` replaced it with the
    same content; a probe refusal after that keeps it."""
    import shutil

    _monkey_built(monkeypatch)

    def stage_and_place(p, e):
        jpath.write_text(intent_text(Intent("repair", OP, txn=TXN)))
        shutil.copytree(tree / GEN, tree / root_tree.STAGING)            # the staged copy: the same content
        return None if root_tree._place(HOST, tree, tree / root_tree.STAGING, cell, None)[0] else "place failed"

    seams, q, state, L, tree, jpath, execs = _phase_a(tmp_path, lambda t: _fake_generation(t, GEN, "def setup():\n"
                                                                                                  "    pass\n"),
                                                      apply=lambda p, e: stage_and_place(p, e))
    cell = seams.plan(None, None).handover
    with pytest.raises(Refused) as e:
        _go(seams)
    assert e.value.name == "downgrade-unsupported" and cell.existed
    assert (tree / GEN / "levain" / "slot" / "install_helper.py").is_file() and not jpath.exists()


def test_adopt_lock_refuses_a_descriptor_that_holds_no_lock(tmp_path: Path) -> None:
    """Ruling 5 (complement LOW 5): the right file, but no flock held on it -> ``bad-root``; held -> adopted."""
    a = tmp_path / "a"
    a.write_text("")
    os.chmod(a, 0o600)
    fd = os.open(a, os.O_RDWR)
    try:
        with pytest.raises(Refused) as e:
            IH.adopt_lock(fd, a, owner_ok=_mine)
        assert e.value.name == "bad-root" and "no lock" in e.value.detail
        fcntl.flock(fd, fcntl.LOCK_EX)
        IH.adopt_lock(fd, a, owner_ok=_mine)
        assert not _pool_free(a)                             # the check's own description did not drop it
    finally:
        os.close(fd)


def test_the_bounded_child_kills_what_the_probe_left_behind(tmp_path: Path) -> None:
    """Ruling 7 (complement LOW 7): a probe that exits cleanly but leaves a grandchild in its group: gone after."""
    import time

    prog = ("import subprocess, sys\n"
            "p = subprocess.Popen(['sleep', '30'], stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL)\n"
            "print(p.pid, flush=True)\n")
    rc, out = IH._bounded_child([sys.executable, "-c", prog], b"", timeout=20.0, cap=1024)
    assert rc == 0
    pid = int(out)
    deadline = time.monotonic() + 5.0
    gone = False
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
            if st.startswith("Z") or not st:
                gone = True
                break
        except ProcessLookupError:
            gone = True
            break
        time.sleep(0.05)
    if not gone:
        os.kill(pid, 9)
    assert gone, f"the probe's grandchild {pid} outlived the probe"


# --- L3 r2 (head 1022+21 S3 brief, commit 1: a-e) --------------------------------------------------------------------

def test_adopt_lock_refuses_an_unlocked_descriptor_while_another_holds_the_lock(tmp_path: Path) -> None:
    """r2 a (codex): the second description meets ``BlockingIOError`` because ANOTHER description holds the flock;
    the inherited one holds nothing -> ``bad-root``."""
    a = tmp_path / "a"
    a.write_text("")
    os.chmod(a, 0o600)
    fd, other = os.open(a, os.O_RDWR), os.open(a, os.O_RDWR)
    try:
        fcntl.flock(other, fcntl.LOCK_EX)                    # another run's description holds it; ``fd`` does not
        with pytest.raises(Refused) as e:
            IH.adopt_lock(fd, a, owner_ok=_mine)
        assert e.value.name == "bad-root" and "no lock" in e.value.detail
    finally:
        os.close(other)
        os.close(fd)


def test_a_probe_whose_repair_continue_takes_another_call_shape_refuses_handover(tmp_path: Path,
                                                                                monkeypatch) -> None:
    """r2 b (codex): ABI 1 self-reported, but ``repair_continue`` cannot bind phase A's exact call ->
    ``downgrade-unsupported:handover`` before the exec, host restored."""
    _monkey_built(monkeypatch)
    wrong = _answers(G.KINDS).replace(_HANDOVER, "def repair_continue(lock_fd, gen):\n    return 0\n")
    seams, q, state, L, tree, jpath, execs = _phase_a(tmp_path, lambda t: _fake_generation(t, GEN, wrong))
    with pytest.raises(Refused) as e:
        _go(seams)
    assert e.value.name == "downgrade-unsupported:handover" and not execs
    assert not (tree / GEN).exists() and not jpath.exists() and not q.held


def test_an_exception_between_the_hash_and_the_place_keeps_a_pre_existing_target(tmp_path: Path,
                                                                                monkeypatch) -> None:
    """r2 c (codex MED): the hash step found ``gen-<id>`` already there (a retained generation); something raised
    before ``_place`` ran (stderr a closed pipe); the restore removes only what THIS run placed -> kept."""
    import shutil

    from tests.test_repair_plan import _package

    _monkey_built(monkeypatch)
    lv, h11 = _package(tmp_path)
    gid = G.generation_id(lv, h11)

    def hash_then_raise(p, e):
        jpath.write_text(intent_text(Intent("repair", OP, txn=TXN)))
        for name, src in (("levain", lv), ("h11", h11)):
            shutil.copytree(src, tree / root_tree.STAGING / name)
        cell.target, cell.placed = None, False               # the plan's fresh cell
        assert root_tree._hash_staged(cell, tree / root_tree.STAGING, lv, h11, None) == (True, gid)
        raise BrokenPipeError(32, "stderr closed")           # before the place step

    def retained(t: Path) -> None:
        for name, src in (("levain", lv), ("h11", h11)):
            shutil.copytree(src, t / gid / name)

    seams, q, state, L, tree, jpath, execs = _phase_a(tmp_path, retained, apply=lambda p, e: hash_then_raise(p, e))
    cell = seams.plan(None, None).handover
    with pytest.raises(BrokenPipeError):
        _go(seams)
    assert cell.existed and not cell.placed and not execs
    assert (tree / gid / "levain" / "__init__.py").is_file() and not (tree / root_tree.STAGING).exists()


def test_the_staged_probe_refuses_an_out_of_bound_identity(tmp_path: Path, monkeypatch) -> None:
    """r2 d (codex MED): an in-format journal record whose identity is out of its kind's bound (a shared group at a
    gid outside the shared range) is refused AT THE PROBE (``bad-record``), the host restored, never in phase B."""
    _monkey_built(monkeypatch)
    prior = Intent("repair", OP, txn=TXN, managed=(("group/1", ("levain-keysadmin", 80)),))
    seams, q, state, L, tree, jpath, execs = _phase_a(tmp_path, _real_generation, prior=prior)
    with pytest.raises(Refused) as e:
        _go(seams)
    assert e.value.name == "bad-record" and not execs and not q.held
    assert read_intent(HOST, path=jpath) == prior and not (tree / GEN).exists()


def test_the_bounded_child_reads_a_pipe_above_fd_setsize(tmp_path: Path) -> None:
    """r2 e (ruled OBS): the probe runner's wait works on a child pipe at an fd >= 1024 (``select.select`` raises
    ``ValueError`` there)."""
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 1200
    if hard != resource.RLIM_INFINITY and hard < want:
        pytest.skip(f"RLIMIT_NOFILE hard limit {hard} < {want}")
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, want), hard))
    held: list[int] = []
    try:
        while True:                                          # every fd below 1030 taken, so the pipe lands above
            fd = os.open(os.devnull, os.O_RDONLY)
            held.append(fd)
            if fd >= 1030:
                break
        rc, out = IH._bounded_child([sys.executable, "-c", "print('hi')"], b"", timeout=20.0, cap=1024)
        assert (rc, out) == (0, b"hi\n")
    finally:
        for fd in held:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


# --- test 2: the flip, killed between its acts ------------------------------------------------------------------------

def test_flip_survives_a_kill_between_its_acts(tmp_path: Path, monkeypatch) -> None:
    old, new = "gen-" + "1" * 16, "gen-" + "2" * 16
    tree = tmp_path / "tree"
    tree.mkdir()
    os.symlink(old, tree / "current")
    real = os.replace

    def killed(src, dst):
        raise KeyboardInterrupt("killed between the symlink and the rename")

    monkeypatch.setattr(root_tree.os, "replace", killed)
    with pytest.raises(KeyboardInterrupt):
        root_tree._flip(tree, new)
    assert os.readlink(tree / "current") == old and os.readlink(tree / "current.new") == new
    assert root_tree.read_current(tree) == old                 # the re-run's gather reads it, no refusal
    monkeypatch.setattr(root_tree.os, "replace", real)
    assert root_tree._flip(tree, new) == (True, "")
    assert os.readlink(tree / "current") == new and not os.path.lexists(tree / "current.new")
    assert root_tree._flip(tree, new) == (True, "")            # idempotent
    (tree / "current.new").write_text("")                      # a regular file there is not ours to remove
    with pytest.raises(Refused) as e:
        root_tree.read_current(tree)
    assert e.value.name == "bad-root"
    ok, why = root_tree._flip(tree, old)
    assert not ok and why.startswith("bad-root") and os.readlink(tree / "current") == new


# --- S3: the resume into a journaled staged generation (§2.2 r4 X3-M1; §5 R2 test 1's resume row) --------------------

def _resume(tmp: Path, monkeypatch, *, torn: bool):
    """A host killed after R3 (the journal names ``generation:<id>``, ``gen-<id>`` placed, ``current`` the old one),
    then a bare ``repair``: phase A's plan, then :func:`IH.repair_handover` with a recording probe and execv."""
    import io
    import shutil
    from dataclasses import replace

    from tests.test_repair_plan import OP as POP, _pre_change

    facts, rf = _pre_change(tmp)
    gid = G.generation_id(rf.source, rf.h11)
    tree = tmp / "tree"
    for name, src in (("levain", rf.source), ("h11", rf.h11)):
        shutil.copytree(src, tree / gid / name)
    if torn:
        (tree / gid / "levain" / "__init__.py").write_text('__version__ = "1.0.0"\n#')   # one byte more
    monkeypatch.setitem(root_tree.IMPORT_TREE, HOST, tree)
    j = Intent("repair", POP, txn=TXN, generation=gid)
    jpath = tmp / "install-intent"
    jpath.write_text(intent_text(j))
    facts = replace(facts, intent=j)
    rf = replace(rf, source=None, h11=None, current="gen-" + "0" * 16)
    plan = IH.plan_for(parse_install_argv(["repair"]), IH.Gathered(facts, repair=rf))
    L = _store(tmp)
    q = IH.Quiesce(L, waits=IH.RH.Seams(host=HOST, store=L, trusted=lambda st: True, lock_wait_s=0.05))
    q.hold()
    probes, execs = [], []

    def probe(host, gen, shape, journal, **kw):
        probes.append((gen, journal))
        return tuple(sorted(G.KINDS))
    try:
        IH.repair_handover(plan, IH.Gathered(facts, quiesce=q, repair=rf), 3, os.getuid(), io.StringIO(),
                           probe=probe, execv=lambda *a: execs.append(a), tree=tree, intent_path=jpath)
        refused = None
    except Refused as e:
        refused = e
    finally:
        q.release()
    return plan, gid, tree, jpath, j, probes, execs, refused


def test_a_bare_repair_resumes_into_the_journalled_generation(tmp_path: Path, monkeypatch) -> None:
    """Intact ``gen-<id>``: no step (R1-R3 ran, there is no source), probed and exec'd into, kept, journal as found."""
    plan, gid, tree, jpath, j, probes, execs, refused = _resume(tmp_path, monkeypatch, torn=False)
    assert refused is None and plan.steps == () and plan.handover.target == gid
    assert probes == [(gid, j)] and len(execs) == 1 and f"{gid!r}" in execs[0][1][3]
    assert (tree / gid / "levain" / "__init__.py").is_file() and read_intent(HOST, path=jpath) == j


def test_a_torn_journalled_generation_is_removed_and_source_required(tmp_path: Path, monkeypatch) -> None:
    """One byte of ``gen-<id>`` changed: removed, refused ``source-required:<id>``, never probed, journal kept."""
    plan, gid, tree, jpath, j, probes, execs, refused = _resume(tmp_path, monkeypatch, torn=True)
    assert refused is not None and refused.name == f"source-required:{gid}"
    assert not probes and not execs and not (tree / gid).exists() and read_intent(HOST, path=jpath) == j


# --- S3: the fault seam (§5 [F7], [r4: X3-H1]; design R2 test 3) -----------------------------------------------------

@pytest.mark.parametrize("mutant", ["", "inprocess"])
def test_phase_b_honours_the_launcher_fault(tmp_path: Path, mutant: str) -> None:
    """A launcher-set fault at ``("R5","after")`` crosses the real ``execv`` as a literal and SIGKILLs phase B after
    R5's acts and before R6's; both flocks die with it. ``inprocess`` (the red): the fault left in phase A's
    :class:`Seams` only, never in phase B's program -> phase B runs to the end."""
    import signal

    marks = tmp_path / "marks"
    marks.mkdir()
    p, _, lock, pool = _phase_b_run(tmp_path, mutant, fault=["R5", "after"], marks=str(marks))
    _line(p)
    out, err = p.communicate(b"\n", timeout=60)
    ran = sorted(x.name for x in marks.iterdir())
    if mutant:
        assert p.returncode == 0 and ran == ["R4b", "R5", "R6"], err
    else:
        assert p.returncode == -signal.SIGKILL and ran == ["R4b", "R5"], err
    with install_lock(path=lock, owner_ok=_mine):
        assert _pool_free(pool)


def test_the_fault_literal_exists_only_when_the_launcher_sets_one(tmp_path: Path) -> None:
    """``main()``'s default :class:`Seams` carries no fault, so phase B's program names none; a launcher's point
    outside ``Seams.POINTS`` (or a bad step id) refuses ``bad-token`` before the lock or any read."""
    assert IH.Seams().fault_at is None and "fault" not in IH.phase_b_text(HOST, GEN, 3, 4, 501)
    assert "fault=('R9', 'a')" in IH.phase_b_text(HOST, GEN, 3, 4, 501, fault=("R9", "a"))
    assert {"before", "after", "a", "b", "mid-copy", "group/1:after-retire"} <= IH.Seams.POINTS
    for bad in (("R5", "middle"), ("X5", "after"), ("R5", "after\n"), ["R5", "after"]):
        calls: list = []
        seams = IH.Seams(euid=lambda: 0, host=lambda: HOST, lock=lambda h: calls.append("lock"),
                         gather=lambda *a: calls.append("gather"), fault_at=bad)  # type: ignore[arg-type]
        with pytest.raises(Refused) as e:
            _go(seams)
        assert e.value.name == "bad-token" and calls == []
        with pytest.raises(Refused):
            IH.phase_b_text(HOST, GEN, 3, 4, 501, fault=bad)  # type: ignore[arg-type]


def test_the_plan_places_every_fault_point(tmp_path: Path, monkeypatch) -> None:
    """With a hook, each R-step sits between its ``before``/``after`` points (R5's after its stops, before R6), R2's
    ``mid-copy`` between the two copies, R9's ``a`` between the flip's symlink and its rename; without one, no point
    step is planned at all."""
    from tests.test_repair_plan import _plan, _pre_change

    facts, rf = _pre_change(tmp_path)
    seen: list = []
    plain = _plan(facts, rf, txn=lambda: TXN)
    faulted = _plan(facts, rf, txn=lambda: TXN, fault=lambda step, pt: seen.append((step, pt)))
    assert not [s for s in plain.steps if s.why.startswith("fault point")]
    whys = [s.why for s in faulted.steps]
    assert [w for w in whys if not w.startswith("fault point")] == [s.why for s in plain.steps]
    i = whys.index("fault point R5 after")
    assert whys[i - 1].startswith("stop") and whys[i + 1] == "fault point R6 before"
    j = whys.index("fault point R2 mid-copy")
    assert whys[j - 1].startswith("copy the levain") and whys[j + 1].startswith("copy the h11")
    tree = tmp_path / "tree"
    tree.mkdir()
    os.symlink("gen-" + "1" * 16, tree / "current")

    def kill(pt):
        if pt == "a":
            raise KeyboardInterrupt("SIGKILL at R9 a")
    with pytest.raises(KeyboardInterrupt):
        root_tree._flip(tree, "gen-" + "2" * 16, fault=kill)
    assert os.readlink(tree / "current") == "gen-" + "1" * 16 and os.readlink(tree / "current.new") == "gen-" + "2" * 16
    assert root_tree._flip(tree, "gen-" + "2" * 16) == (True, "") and os.readlink(tree / "current") == "gen-" + "2" * 16
