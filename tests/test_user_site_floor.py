"""The Python user site is part of the persistence floor.

RUN 2026-10-26 on main d0890664 through the real Seatbelt hand (``build_policy`` +
``select_provider().spawn_shell``, a scratch HOME): confined bash wrote
``~/Library/Python/3.12/lib/python/site-packages/x.pth``, a ``usercustomize.py`` beside it and
``~/.local/lib/python3.12/site-packages/x.pth``, while ``~/.zshenv`` and ``~/Library/LaunchAgents``
were refused. A non-venv interpreter (Homebrew's) started with ``-EP`` and that HOME then ran the
planted ``.pth``: standing code execution in the operator's later Python, the class the floor
closes for ``~/.zshrc``. The live tests below are that run, kept.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from levain.firing import confinement as C
from levain.firing.confinement import build_policy, crown_jewel_reason, sandbox_exec_available

_XY = f"{sys.version_info.major}.{sys.version_info.minor}"
_LIVE = platform.system() == "Darwin" and sandbox_exec_available()
live = pytest.mark.skipif(not _LIVE, reason="needs macOS sandbox-exec (the SeatbeltProvider backend)")


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    (h / "Library").mkdir(parents=True)
    (h / ".local").mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("PYTHONUSERBASE", raising=False)
    return h


def _policy(tmp_path: Path) -> C.CrownJewelsPolicy:
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True, exist_ok=True)
    return build_policy(entity, workspace=tmp_path / "ws", deny_standard_creds=False)


def _site_dirs(home: Path) -> list[Path]:
    """Both user-site layouts CPython uses: a macOS framework build's, and every other build's."""
    return [home / "Library" / "Python" / _XY / "lib" / "python" / "site-packages",
            home / ".local" / "lib" / f"python{_XY}" / "site-packages"]


def _covered(policy: C.CrownJewelsPolicy, p: Path) -> bool:
    return any(p == Path(t) or p.is_relative_to(Path(t)) for t in policy.persistence_write_subtrees)


def _interpreters() -> list[str]:
    found = [sys.executable]
    for cand in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3",
                 shutil.which("python3") or ""):
        if cand and os.path.exists(cand) and cand not in found:
            found.append(cand)
    return found


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_the_user_site_trees_are_persistence_subtrees(home: Path, tmp_path: Path, monkeypatch,
                                                     system: str) -> None:
    monkeypatch.setattr(C.platform, "system", lambda: system)
    policy = _policy(tmp_path)
    assert home / ".local" / "lib" in policy.persistence_write_subtrees
    framework = home / "Library" / "Python"
    assert (framework in policy.persistence_write_subtrees) == (system == "Darwin")


def test_every_interpreter_here_puts_its_user_site_inside_the_floor(home: Path, tmp_path: Path) -> None:
    """Derived, not guessed: ask each interpreter on this machine where its user site is for this
    HOME (``-I`` leaves HOME alone and drops PYTHONUSERBASE, the operator's default case)."""
    policy = _policy(tmp_path)
    asked = 0
    for py in _interpreters():
        try:
            out = subprocess.run([py, "-I", "-c", "import site; print(site.getusersitepackages())"],
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                 env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, timeout=30)
        except subprocess.TimeoutExpired:
            continue   # no answer (the Xcode stub's install prompt, say): not an interpreter to ask
        if out.returncode != 0:
            continue
        site_dir = Path(out.stdout.strip())
        assert site_dir.is_relative_to(home), (py, site_dir)
        assert _covered(policy, site_dir), f"{py}'s user site {site_dir} is outside the floor"
        asked += 1
    assert asked, "no interpreter answered: the check above ran on nothing"


def test_the_editor_refuses_a_user_site_plant(home: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(C.platform, "system", lambda: "Darwin")
    policy = _policy(tmp_path)
    for d in _site_dirs(home):
        for name in ("x.pth", "usercustomize.py", "sitecustomize.py"):
            assert crown_jewel_reason(policy, d / name) is not None, d / name
        assert crown_jewel_reason(policy, d / "x.pth", write=False) is None  # reads stay open
    # A Python version that does not exist yet is inside the tree too.
    assert crown_jewel_reason(policy, home / ".local" / "lib" / "python3.99" / "site-packages" / "x.pth")


def test_the_linux_plan_binds_the_local_lib_tree_read_only(home: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")
    for var in ("XDG_RUNTIME_DIR", "GIT_CONFIG_GLOBAL", *{v for v, _, _ in C._CRED_OVERRIDES}):
        monkeypatch.delenv(var, raising=False)
    lib = home / ".local" / "lib" / f"python{_XY}" / "site-packages"
    lib.mkdir(parents=True)
    ent = home / "proj" / "ent"
    (ent / ".levain").mkdir(parents=True)
    ws = home / "proj" / "ws"
    ws.mkdir()
    argv, _ = C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))
    ro = [argv[i + 2] for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1] == argv[i + 2]]
    assert str(home / ".local" / "lib") in ro


@live
def test_live_confined_bash_cannot_plant_in_the_user_site(home: Path, tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    with C.select_provider().spawn_shell(policy) as sh:
        assert sh.run(f"echo x > {home}/.zshenv 2>&1", timeout=20).exit_code != 0  # the control
        for d in _site_dirs(home):
            r = sh.run(f"mkdir -p {d} 2>&1; echo 'import os' > {d}/x.pth 2>&1", timeout=20)
            assert r.exit_code != 0, (d, r.output)
            assert not (d / "x.pth").exists()
        # Elsewhere under ~/.local stays writable: the deny is the lib tree, not the whole folder.
        ok = sh.run(f"mkdir -p {home}/.local/state/tool && echo x > {home}/.local/state/tool/f", timeout=20)
        assert ok.exit_code == 0, ok.output


@live
def test_live_an_existing_user_site_cannot_be_planted_or_renamed_into(home: Path, tmp_path: Path) -> None:
    for d in _site_dirs(home):
        d.mkdir(parents=True)
    staged = tmp_path / "ws"
    policy = _policy(tmp_path)
    staged.mkdir(exist_ok=True)
    with C.select_provider().spawn_shell(policy) as sh:
        assert sh.run(f"echo 'import os' > {staged}/x.pth", timeout=20).exit_code == 0
        for d in _site_dirs(home):
            r = sh.run(f"mv {staged}/x.pth {d}/x.pth 2>&1", timeout=20)
            assert r.exit_code != 0 and not (d / "x.pth").exists(), (d, r.output)
            r = sh.run(f"mv {d} {d}.old 2>&1", timeout=20)
            assert r.exit_code != 0 and d.is_dir(), (d, r.output)


def _linux_plan(home: Path, monkeypatch) -> tuple[list[str], list[str]]:
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")
    for var in ("XDG_RUNTIME_DIR", "GIT_CONFIG_GLOBAL", *{v for v, _, _ in C._CRED_OVERRIDES}):
        monkeypatch.delenv(var, raising=False)
    ent = home / "proj" / "ent"
    (ent / ".levain").mkdir(parents=True)
    ws = home / "proj" / "ws"
    ws.mkdir()
    return C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))


def _ro_binds(argv: list[str]) -> list[str]:
    return [argv[i + 2] for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1] == argv[i + 2]]


def test_the_linux_plan_protects_an_absent_local_lib(home: Path, tmp_path: Path, monkeypatch) -> None:
    """F1 (L3 r1, deepseek HIGH, reasoned): with ~/.local present and ~/.local/lib absent, is the tree still
    protected? Settled by building the plan: step (2p) binds the exact path read-only and the provider creates
    it first (``create_first``), as for ~/.local/bin, so bash cannot mkdir it and plant inside."""
    lib = home / ".local" / "lib"
    assert not os.path.lexists(lib) and (home / ".local").is_dir()
    argv, create_first = _linux_plan(home, monkeypatch)
    real = os.path.realpath(lib)
    assert real in _ro_binds(argv)
    assert real in create_first
    assert os.path.realpath(home / ".local" / "bin") in create_first   # the sibling it is handled like


def test_the_linux_plan_protects_the_user_site_with_no_local_at_all(home: Path, tmp_path: Path,
                                                                   monkeypatch) -> None:
    """F1's variant: no ~/.local. Nothing is created on the host; instead step (0) makes $HOME's own entries
    read-only (a tmpfs over $HOME, each existing child bound back, then remounted read-only), so bash cannot
    create ~/.local, and with it nothing below."""
    (home / ".local").rmdir()
    argv, create_first = _linux_plan(home, monkeypatch)
    home_real = os.path.realpath(home)
    local = os.path.join(home_real, ".local")
    pairs = list(zip(argv, argv[1:]))
    assert ("--tmpfs", home_real) in pairs and ("--remount-ro", home_real) in pairs
    # nothing names ~/.local (no bind back in, no placeholder made on the host)
    assert not any(a == local or a.startswith(local + os.sep) for a in argv)
    assert not any(p == local or p.startswith(local + os.sep) for p in create_first)


# --- F2: $PYTHONUSERBASE, read from the operator's environment at policy build ----------------------------------

def test_an_absolute_dedicated_userbase_joins_the_floor(home: Path, tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "pyuser"
    monkeypatch.setenv("PYTHONUSERBASE", str(base))
    policy = _policy(tmp_path)
    assert _covered(policy, base / "lib" / f"python{_XY}" / "site-packages" / "x.pth")
    assert crown_jewel_reason(policy, base / "lib" / f"python{_XY}" / "site-packages" / "x.pth")
    assert crown_jewel_reason(policy, base / "lib" / f"python{_XY}" / "site-packages" / "x.pth", write=False) is None
    assert C.python_userbase_note(tmp_path / "ws") is None
    # Asked of the interpreter itself: the site it uses under this variable is inside the floor.
    out = subprocess.run([sys.executable, "-c", "import site; print(site.getusersitepackages())"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
                         env={"HOME": str(home), "PYTHONUSERBASE": str(base), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0 and _covered(policy, Path(out.stdout.strip())), out


def test_an_unset_or_empty_userbase_adds_nothing_and_says_nothing(home: Path, tmp_path: Path, monkeypatch) -> None:
    baseline = set(_policy(tmp_path).persistence_write_subtrees)
    monkeypatch.setenv("PYTHONUSERBASE", "")
    assert set(_policy(tmp_path).persistence_write_subtrees) == baseline
    assert C.python_userbase_note(tmp_path / "ws") is None


@pytest.mark.parametrize("which", ["relative", "home", "ancestor of home", "ancestor of the workspace"])
def test_a_userbase_the_floor_cannot_hold_is_named_not_added(home: Path, tmp_path: Path, monkeypatch,
                                                            which: str) -> None:
    ws = tmp_path / "ws"
    entity = tmp_path / "entity"
    if which == "ancestor of the workspace":
        ws = tmp_path / "outside" / "ws"
    value = {"relative": "pyuser", "home": str(home), "ancestor of home": str(tmp_path),
             "ancestor of the workspace": str(tmp_path / "outside")}[which]
    (entity / ".levain").mkdir(parents=True)
    monkeypatch.delenv("PYTHONUSERBASE", raising=False)
    baseline = set(build_policy(entity, workspace=ws, deny_standard_creds=False).persistence_write_subtrees)
    monkeypatch.setenv("PYTHONUSERBASE", value)
    policy = build_policy(entity, workspace=ws, deny_standard_creds=False)
    assert set(policy.persistence_write_subtrees) == baseline
    note = C.python_userbase_note(ws)
    assert note is not None and "PYTHONUSERBASE" in note and repr(value) in note and "NOT in the floor" in note


def test_the_run_banner_and_doctor_print_the_userbase_line(home: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    from levain.doctor import _check_confinement
    from levain.run import _print_banner

    class _Binding:
        episodic_path = tmp_path / "e.db"
        crystal_path = tmp_path / "c.json"

    monkeypatch.setenv("PYTHONUSERBASE", str(home))
    note = C.python_userbase_note(tmp_path / "ws")
    _print_banner(tmp_path, _Binding(), model="m", with_tools=True, bash_ok=True, user_site_note=note)
    out = " ".join(capsys.readouterr().out.split())
    assert "Python user site NOT in the floor: $PYTHONUSERBASE is" in out
    monkeypatch.setattr(C, "diagnose_confinement", lambda *a, **k: C.ConfinementDiagnosis(
        supported=True, provider="p", reason="", remedy=None))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)
    assert "Python user site NOT in the floor" in _check_confinement(entity)[0].detail
    monkeypatch.delenv("PYTHONUSERBASE")
    assert "PYTHONUSERBASE" not in _check_confinement(entity)[0].detail


# --- F6: the refusal names what it is when the workspace sits in a protected tree --------------------------------

def test_a_workspace_inside_the_local_lib_tree_is_refused_by_what_it_is(home: Path, tmp_path: Path,
                                                                        monkeypatch) -> None:
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")
    for var in ("XDG_RUNTIME_DIR", "GIT_CONFIG_GLOBAL", *{v for v, _, _ in C._CRED_OVERRIDES}):
        monkeypatch.delenv(var, raising=False)
    ent = home / "proj" / "ent"
    (ent / ".levain").mkdir(parents=True)
    ws = home / ".local" / "lib" / "proj" / "ws"
    ws.mkdir(parents=True)
    with pytest.raises(C.ConfinementError) as err:
        C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))
    msg = " ".join(str(err.value).split())
    assert f"move {os.path.realpath(ws)} out of {os.path.realpath(home / '.local' / 'lib')}" in msg


@live
def test_live_a_dedicated_userbase_cannot_be_planted(home: Path, tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "pyuser"
    monkeypatch.setenv("PYTHONUSERBASE", str(base))
    policy = _policy(tmp_path)
    site_dir = base / "lib" / f"python{_XY}" / "site-packages"
    with C.select_provider().spawn_shell(policy) as sh:
        r = sh.run(f"mkdir -p {site_dir} 2>&1; echo 'import os' > {site_dir}/x.pth 2>&1", timeout=20)
        assert r.exit_code != 0 and not (site_dir / "x.pth").exists(), r.output
        ok = sh.run(f"mkdir -p {tmp_path}/beside && echo x > {tmp_path}/beside/f", timeout=20)   # the control
        assert ok.exit_code == 0, ok.output
