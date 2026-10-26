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
import re
import shlex
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


def _names_real(msg: str, p: Path) -> bool:
    """The refusal names ``p``'s real path itself (as the site, or as the real path of a linked one), not a
    spelling that merely ends with it."""
    return re.search(rf"(below|really) {re.escape(str(p.resolve()))}[,)]", msg) is not None


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
    real = os.path.realpath(home / ".local" / "lib")
    assert (real, real) in _ro_binds(argv)


@live
def test_live_confined_bash_cannot_plant_in_the_user_site(home: Path, tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    q = shlex.quote
    with C.select_provider().spawn_shell(policy) as sh:
        assert sh.run(f"echo x > {q(str(home / '.zshenv'))} 2>&1", timeout=20).exit_code != 0  # the control
        for d in _site_dirs(home):
            r = sh.run(f"mkdir -p {q(str(d))} 2>&1; echo 'import os' > {q(str(d / 'x.pth'))} 2>&1", timeout=20)
            assert r.exit_code != 0, (d, r.output)
            assert not (d / "x.pth").exists()
        # Elsewhere under ~/.local stays writable: the deny is the lib tree, not the whole folder.
        tool = home / ".local" / "state" / "tool"
        ok = sh.run(f"mkdir -p {q(str(tool))} && echo x > {q(str(tool / 'f'))}", timeout=20)
        assert ok.exit_code == 0, ok.output


@live
def test_live_an_existing_user_site_cannot_be_planted_or_renamed_into(home: Path, tmp_path: Path) -> None:
    for d in _site_dirs(home):
        d.mkdir(parents=True)
    staged = tmp_path / "ws"
    policy = _policy(tmp_path)
    staged.mkdir(exist_ok=True)
    q = shlex.quote
    with C.select_provider().spawn_shell(policy) as sh:
        assert sh.run(f"echo 'import os' > {q(str(staged / 'x.pth'))}", timeout=20).exit_code == 0
        for d in _site_dirs(home):
            r = sh.run(f"mv {q(str(staged / 'x.pth'))} {q(str(d / 'x.pth'))} 2>&1", timeout=20)
            assert r.exit_code != 0 and not (d / "x.pth").exists(), (d, r.output)
            r = sh.run(f"mv {q(str(d))} {q(str(d) + '.old')} 2>&1", timeout=20)
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


def _ro_binds(argv: list[str]) -> list[tuple[str, str]]:
    """Every ``--ro-bind SRC DST`` as ``(SRC, DST)``, so a caller compares the two separately."""
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == "--ro-bind"]


def test_the_linux_plan_protects_an_absent_local_lib(home: Path, tmp_path: Path, monkeypatch) -> None:
    """F1 (L3 r1, deepseek HIGH, reasoned): with ~/.local present and ~/.local/lib absent, is the tree still
    protected? Settled by building the plan: step (2p) binds the exact path read-only and the provider creates
    it first (``create_first``), as for ~/.local/bin, so bash cannot mkdir it and plant inside."""
    lib = home / ".local" / "lib"
    assert not os.path.lexists(lib) and (home / ".local").is_dir()
    argv, create_first = _linux_plan(home, monkeypatch)
    real = os.path.realpath(lib)
    assert (real, real) in _ro_binds(argv)
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
    # U3 (verify r2, codex MED): only the user-site subtree below the base, not the base whole
    assert not _covered(policy, base / "notes" / "f") and crown_jewel_reason(policy, base / "notes" / "f") is None
    assert C.python_userbase_note(home) is None
    # Asked of the interpreter itself: the site it uses under this variable is inside the floor.
    out = subprocess.run([sys.executable, "-c", "import site; print(site.getusersitepackages())"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
                         env={"HOME": str(home), "PYTHONUSERBASE": str(base), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0 and _covered(policy, Path(out.stdout.strip())), out


def test_an_unset_or_empty_userbase_adds_nothing_and_says_nothing(home: Path, tmp_path: Path, monkeypatch) -> None:
    baseline = set(_policy(tmp_path).persistence_write_subtrees)
    monkeypatch.setenv("PYTHONUSERBASE", "")
    assert set(_policy(tmp_path).persistence_write_subtrees) == baseline
    assert C.python_userbase_note(home) is None


@pytest.mark.parametrize("value", ["pyuser", "~/pyuser"])
def test_a_relative_userbase_is_named_not_added(home: Path, tmp_path: Path, monkeypatch, value: str) -> None:
    """A relative value, ``~/...`` included, is resolved against whatever directory each Python starts in: no tree
    holds it, so it is the banner's line. Asked of the interpreter, not reasoned: site.py leaves ``~`` as written."""
    baseline = set(_policy(tmp_path).persistence_write_subtrees)
    monkeypatch.setenv("PYTHONUSERBASE", value)
    assert set(_policy(tmp_path).persistence_write_subtrees) == baseline
    note = C.python_userbase_note(home)
    assert note is not None and repr(value) in note and "NOT in the floor" in note
    out = subprocess.run([sys.executable, "-c", "import site; print(site.getusersitepackages())"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
                         env={"HOME": str(home), "PYTHONUSERBASE": value, "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0 and out.stdout.startswith(value + "/lib/"), out


@pytest.mark.parametrize("which", ["home", "ancestor of home", "shared temp"])
def test_a_wide_absolute_userbase_protects_its_lib_only(home: Path, tmp_path: Path, monkeypatch, which: str) -> None:
    """U3: a base that is the home, holds it, or is /tmp gets its ``lib`` protected (where site.py puts the site in
    both layouts) and nothing else, so the home, the workspace and /tmp stay as they were."""
    base = {"home": home, "ancestor of home": tmp_path, "shared temp": Path("/tmp")}[which]
    monkeypatch.setenv("PYTHONUSERBASE", str(base))
    policy = _policy(tmp_path)
    assert _covered(policy, base / "lib" / f"python{_XY}" / "site-packages" / "x.pth")
    assert crown_jewel_reason(policy, base / "lib" / "python" / "site-packages" / "usercustomize.py")
    assert crown_jewel_reason(policy, base / "scratch" / "f") is None   # beside lib (home entries are floor anyway)
    assert C.python_userbase_note(home) is None


@pytest.mark.parametrize("which", ["filesystem root", "holds the workspace", "holds the entity"])
def test_a_userbase_whose_site_is_too_wide_refuses_the_floor(home: Path, tmp_path: Path, monkeypatch,
                                                            which: str) -> None:
    """U3 + U4: a ``<base>/lib`` that :func:`too_wide_to_deny` names (a filesystem root's, or one holding the
    workspace or the entity's store) refuses the floor by name, with the remedy."""
    entity, ws = tmp_path / "entity", tmp_path / "ws"
    base = {"filesystem root": Path("/"), "holds the workspace": tmp_path / "b1",
            "holds the entity": tmp_path / "b2"}[which]
    if which == "holds the workspace":
        ws = base / "lib" / "ws"
    if which == "holds the entity":
        entity = base / "lib" / "entity"
    (entity / ".levain").mkdir(parents=True)
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PYTHONUSERBASE", str(base))
    with pytest.raises(C.ConfinementError) as err:
        build_policy(entity, workspace=ws, deny_standard_creds=False)
    msg = " ".join(str(err.value).split())
    assert "$PYTHONUSERBASE" in msg and str(Path(os.path.normpath(base)) / "lib") in msg
    assert "dedicated absolute directory" in msg and "fail-closed" in msg


def test_the_run_banner_and_doctor_print_the_userbase_line(home: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    from levain.doctor import _check_confinement
    from levain.run import _print_banner

    class _Binding:
        episodic_path = tmp_path / "e.db"
        crystal_path = tmp_path / "c.json"

    monkeypatch.setenv("PYTHONUSERBASE", "pyuser")
    note = C.python_userbase_note(home)
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
    base.mkdir()   # the operator's base exists; an absent one is an ancestor of the tree, which bash cannot create
    monkeypatch.setenv("PYTHONUSERBASE", str(base))
    policy = _policy(tmp_path)
    site_dir = base / "lib" / f"python{_XY}" / "site-packages"
    q = shlex.quote
    with C.select_provider().spawn_shell(policy) as sh:
        r = sh.run(f"mkdir -p {q(str(site_dir))} 2>&1; echo 'import os' > {q(str(site_dir / 'x.pth'))} 2>&1",
                   timeout=20)
        assert r.exit_code != 0 and not (site_dir / "x.pth").exists(), r.output
        # the controls: beside the base, and inside the base beside its lib (U3: the base is not denied whole)
        ok = sh.run(f"mkdir -p {q(str(tmp_path / 'beside'))} && echo x > {q(str(tmp_path / 'beside' / 'f'))} && "
                    f"mkdir -p {q(str(base / 'share'))} && echo x > {q(str(base / 'share' / 'f'))}", timeout=20)
        assert ok.exit_code == 0, ok.output


# --- U2: a non-directory where the Linux plan's ~/.local/lib tree would be -------------------------------------------

def test_the_linux_plan_refuses_a_file_where_the_local_lib_tree_would_be(home: Path, tmp_path: Path,
                                                                        monkeypatch) -> None:
    """U2 (verify r2, codex HIGH): with ~/.local/lib a regular file, bash could unlink it, make the directory and
    plant below it. The plan refuses by name with the remedy rather than skip the bind."""
    lib = home / ".local" / "lib"
    lib.write_text("not a directory\n")
    with pytest.raises(C.ConfinementError) as err:
        _linux_plan(home, monkeypatch)
    msg = " ".join(str(err.value).split())
    assert os.path.realpath(lib) in msg and "not a directory" in msg and "remove it or move it" in msg


# --- verify r3 #19: a $PYTHONUSERBASE that is a symlink ----------------------------------------------------------

def _symlinked_base(tmp_path: Path) -> tuple[Path, Path]:
    """``(link, real)``: the base as the operator wrote it, a symlink to a directory in another temp tree."""
    real = Path(os.path.realpath(tmp_path)) / "elsewhere" / "pybase"
    real.mkdir(parents=True)
    link = tmp_path / "pybase-link"
    link.symlink_to(real)
    return link, real


def test_a_symlinked_userbase_names_its_real_lib_too(home: Path, tmp_path: Path, monkeypatch) -> None:
    """#19 (verify r3, deepseek MED): with the base a symlink, the derivation returns ``<base>/lib`` as written AND
    the real ``lib`` it resolves to, so every reader (the floor, its too-wide test, the consent hook) sees both."""
    link, real = _symlinked_base(tmp_path)
    monkeypatch.setenv("PYTHONUSERBASE", str(link))
    roots, note = C.python_user_site_roots(home)
    paths = [p for p, _ in roots]
    assert link / "lib" in paths and real / "lib" in paths and note is None
    policy = _policy(tmp_path)
    for b in (link, real):
        assert _covered(policy, b / "lib" / f"python{_XY}" / "site-packages" / "x.pth")
        assert crown_jewel_reason(policy, b / "lib" / "python" / "site-packages" / "usercustomize.py")


def test_a_symlinked_userbase_whose_real_lib_is_too_wide_refuses_the_floor(home: Path, tmp_path: Path,
                                                                          monkeypatch) -> None:
    """#19's other reader: the too-wide refusal. A base linked to ``/`` has the real site ``/lib`` (a filesystem
    root's), which the lexical ``<link>/lib`` alone does not show."""
    link = tmp_path / "rootlink"
    link.symlink_to("/")
    monkeypatch.setenv("PYTHONUSERBASE", str(link))
    with pytest.raises(C.ConfinementError) as err:
        _policy(tmp_path)
    msg = " ".join(str(err.value).split())
    assert "$PYTHONUSERBASE" in msg and _names_real(msg, Path('/lib')) and "fail-closed" in msg


def test_a_userbase_linked_to_usr_refuses_the_floor(home: Path, tmp_path: Path, monkeypatch) -> None:
    """CI run 40378399674: on a usrmerge Linux ``/lib`` is ``/usr/lib``, three parts, so the part count alone let
    it through. A base linked to ``/usr`` names ``/usr/lib`` on any system: a directory the runtime loads from."""
    link = tmp_path / "usrlink"
    link.symlink_to("/usr")
    monkeypatch.setenv("PYTHONUSERBASE", str(link))
    with pytest.raises(C.ConfinementError) as err:
        _policy(tmp_path)
    msg = " ".join(str(err.value).split())
    assert "$PYTHONUSERBASE" in msg and _names_real(msg, Path('/usr/lib')) and "fail-closed" in msg


def test_every_existing_system_runtime_root_is_refused_at_the_userbase_site() -> None:
    """The roster, enumerated, and the interpreter's own load paths: each that exists here, resolved, is held by
    the ``$PYTHONUSERBASE`` site's predicate, and NOT by the shared :func:`too_wide_to_deny` (U4: a levain home
    holding its venv stays deniable whole)."""
    import sysconfig
    paths = sysconfig.get_paths()
    own = [sys.prefix, sys.base_prefix, os.path.dirname(sys.executable), sysconfig.get_config_var("LIBDIR"),
           *(paths[k] for k in ("stdlib", "platstdlib", "purelib", "platlib"))]
    checked = []
    for p in (*C.SYSTEM_RUNTIME_ROOTS, *(Path(v) for v in own if v)):
        if p.exists():
            assert C._holds_a_system_runtime(p.resolve()), p
            checked.append(p)
    assert Path("/usr") in checked   # the roster is not vacuous on any system the tests run on
    stdlib = Path(paths["stdlib"]).resolve()
    assert C._holds_a_system_runtime(stdlib.parent)   # <prefix>/lib holds the stdlib (codex MED, U4)
    assert not C.too_wide_to_deny(stdlib, Path("/nonexistent-home"))


def test_an_ordinary_userbase_lib_is_not_too_wide(home: Path, tmp_path: Path) -> None:
    """The control: a dedicated base under the operator's own tree is denied, not refused."""
    lib = tmp_path / "pyuser" / "lib"
    lib.mkdir(parents=True)
    assert not C.too_wide_to_deny(lib.resolve(), home, tmp_path / "ws", tmp_path / "entity")
    assert not C._holds_a_system_runtime(lib.resolve())
    assert not C.too_wide_to_deny((home / ".local" / "lib").resolve(), home / "other")
    assert not C._holds_a_system_runtime((home / ".local" / "lib").resolve())


@live
def test_live_a_symlinked_userbase_cannot_be_planted_through_its_real_path(home: Path, tmp_path: Path,
                                                                          monkeypatch) -> None:
    link, real = _symlinked_base(tmp_path)
    monkeypatch.setenv("PYTHONUSERBASE", str(link))
    policy = _policy(tmp_path)
    q = shlex.quote
    with C.select_provider().spawn_shell(policy) as sh:
        for b in (link, real):
            site_dir = b / "lib" / f"python{_XY}" / "site-packages"
            r = sh.run(f"mkdir -p {q(str(site_dir))} 2>&1; echo 'import os' > {q(str(site_dir / 'x.pth'))} 2>&1",
                       timeout=20)
            assert r.exit_code != 0 and not (site_dir / "x.pth").exists(), (b, r.output)
        ok = sh.run(f"mkdir -p {q(str(real / 'share'))} && echo x > {q(str(real / 'share' / 'f'))}", timeout=20)
        assert ok.exit_code == 0, ok.output   # the control: the base beside its lib stays writable


# --- verify r3: the step-(2p) non-directory refusal names no tree it is not --------------------------------------

def test_a_file_where_the_local_bin_tree_would_be_is_refused_without_naming_the_user_site(home: Path, tmp_path: Path,
                                                                                         monkeypatch) -> None:
    """deepseek LOW (verify r3): the U2 refusal covers every step-(2p) tree; its message must not tell the operator
    that ``~/.local/bin`` is where the Python user site lives."""
    binf = home / ".local" / "bin"
    binf.write_text("not a directory\n")
    with pytest.raises(C.ConfinementError) as err:
        _linux_plan(home, monkeypatch)
    msg = " ".join(str(err.value).split())
    assert os.path.realpath(binf) in msg and "not a directory" in msg and "remove it or move it" in msg
    assert "Python user site" not in msg


@pytest.fixture
def fresh_runtime_roots():
    """The runtime roots are computed once per process: clear the cache around a test that changes ``sys``."""
    clear = getattr(getattr(C, "_system_runtime_roots", None), "cache_clear", lambda: None)
    clear()
    yield
    clear()


@pytest.mark.parametrize("how", ["the base prefix", "a link to it"])
def test_a_userbase_at_the_interpreters_base_prefix_refuses_the_floor(home: Path, tmp_path: Path, monkeypatch,
                                                                      fresh_runtime_roots, how: str) -> None:
    """U4 (codex MED): ``<sys.base_prefix>/lib`` holds the interpreter's stdlib; denying it whole breaks every
    Python tool, so it refuses by name, the real path named when the base is a link."""
    real = Path(sys.base_prefix).resolve()
    base = real
    if how == "a link to it":
        base = tmp_path / "prefixlink"
        base.symlink_to(real)
    monkeypatch.setenv("PYTHONUSERBASE", str(base))
    with pytest.raises(C.ConfinementError) as err:
        _policy(tmp_path)
    msg = " ".join(str(err.value).split())
    assert "$PYTHONUSERBASE" in msg and _names_real(msg, real / "lib") and "fail-closed" in msg


@pytest.mark.parametrize("value", [None, ""])
def test_no_sys_executable_is_skipped_never_a_crash_or_the_cwd(home: Path, tmp_path: Path, monkeypatch,
                                                               fresh_runtime_roots, value) -> None:
    """U4 (codex MED + complement LOW): ``sys.executable`` may be None or empty. Neither raises, and an empty one
    never makes the cwd a runtime root (``Path("").parent`` is ``.``)."""
    base = tmp_path / "pyuser"
    cwd = base / "lib" / "here"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(sys, "executable", value)
    assert not C.too_wide_to_deny(cwd.parent, home)
    monkeypatch.setenv("PYTHONUSERBASE", str(base))
    policy = _policy(tmp_path)
    assert _covered(policy, base / "lib" / f"python{_XY}" / "site-packages" / "x.pth")
