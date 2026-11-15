"""The live marker (board levain#1, DESIGN 1114-44, ruling (b) staged).

An unmarked production run (no pytest, no scratch marker, ``$LEVAIN_HOME`` unset) that resolves the real
``~/.levain`` WARNS on stderr and LOGS a row next to the store; it neither refuses nor falls back to scratch. A
process a levain launcher marked (``home_guard.mark_live()``, ``LEVAIN_LIVE=1``) resolves silently. Refusal is one
named constant, ``home_guard.UNMARKED_ACTION``, flipped in a later release. Run unmarked scripts as the incident did:
a bare ``python`` with a scratch ``HOME``, outside tests/."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from levain import home_guard
from levain.home_guard import RealHomeReached

REPO = Path(__file__).resolve().parent.parent

RESOLVE = "from levain.consent.queue import default_queue_dir\nprint(default_queue_dir())\n"


def _spawn(tmp_path: Path, script: str, *, live: bool = False, levain_home: str | None = None, home_exists: bool = True):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    if home_exists:
        (fake_home / ".levain").mkdir()
    path = tmp_path / "bare_script.py"
    path.write_text(script, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(fake_home), "PYTHONPATH": str(REPO)}
    if live:
        env[home_guard.LIVE_ENV] = "1"
    if levain_home is not None:
        env["LEVAIN_HOME"] = levain_home
    done = subprocess.run([sys.executable, str(path)], env=env, capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    return done, fake_home


def test_an_unmarked_production_run_resolves_the_real_home_warns_and_logs(tmp_path):
    done, fake_home = _spawn(tmp_path, RESOLVE)
    assert done.returncode == 0, done.stderr                       # never a refusal in 1.0
    assert Path(done.stdout.strip()) == (fake_home / ".levain" / "consent").resolve()   # never a scratch fallback
    assert "LEVAIN_LIVE" in done.stderr and "LEVAIN_HOME" in done.stderr, done.stderr   # names the fix
    rows = [json.loads(line) for line in (fake_home / ".levain" / home_guard.LOG_NAME).read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["what"] == "the consent directory" and rows[0]["pid"] > 0
    assert rows[0]["argv0"].endswith("bare_script.py")


def test_the_warning_and_the_row_come_once_per_resolver_per_process(tmp_path):
    script = RESOLVE + "from levain.consent.queue import default_queue_dir\ndefault_queue_dir()\ndefault_queue_dir()\n"
    done, fake_home = _spawn(tmp_path, script)
    assert done.returncode == 0 and done.stderr.count("LEVAIN_LIVE") == 1, done.stderr
    assert len((fake_home / ".levain" / home_guard.LOG_NAME).read_text().splitlines()) == 1


def test_a_marked_run_resolves_silently(tmp_path):
    done, fake_home = _spawn(tmp_path, RESOLVE, live=True)
    assert done.returncode == 0 and done.stderr == ""
    assert not (fake_home / ".levain" / home_guard.LOG_NAME).exists()


def test_a_run_with_its_own_levain_home_is_silent_and_unlogged(tmp_path):
    done, fake_home = _spawn(tmp_path, RESOLVE, levain_home=str(tmp_path / "mine"))
    assert done.returncode == 0 and done.stderr == ""
    assert not (fake_home / ".levain" / home_guard.LOG_NAME).exists()


def test_the_log_is_never_the_reason_a_home_appears_or_a_run_fails(tmp_path):
    done, fake_home = _spawn(tmp_path, RESOLVE, home_exists=False)
    assert done.returncode == 0 and "LEVAIN_LIVE" in done.stderr
    assert not (fake_home / ".levain").exists()          # a resolve-only run creates nothing, the log included


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory"])
def test_the_log_never_blocks_and_never_follows_a_link(tmp_path, kind):
    """A FIFO with no reader at the log's path must not hang the run, and a link there must not carry the row to its
    target: the log is a regular file or it is skipped, silently."""
    fake_home = tmp_path / "fake-home"
    (fake_home / ".levain").mkdir(parents=True)
    log = fake_home / ".levain" / home_guard.LOG_NAME
    target = tmp_path / "elsewhere"
    target.write_text("")
    if kind == "fifo":
        os.mkfifo(log)
    elif kind == "symlink":
        log.symlink_to(target)
    else:
        log.mkdir()
    path = tmp_path / "bare_script.py"
    path.write_text(RESOLVE, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(fake_home), "PYTHONPATH": str(REPO)}
    done = subprocess.run([sys.executable, str(path)], env=env, capture_output=True, text=True, timeout=20, cwd=str(tmp_path))
    assert done.returncode == 0 and "LEVAIN_LIVE" in done.stderr, done.stderr     # still warned, still resolved
    assert Path(done.stdout.strip()) == (fake_home / ".levain" / "consent").resolve()
    assert target.read_text() == ""


def test_the_log_row_is_one_write(tmp_path, monkeypatch):
    """One ``os.write`` on an ``O_APPEND`` descriptor: concurrent unmarked processes append whole rows."""
    (tmp_path / ".levain").mkdir()
    writes: list[bytes] = []
    real = os.write
    monkeypatch.setattr(home_guard.os, "write", lambda fd, data: (writes.append(bytes(data)), real(fd, data))[1])
    home_guard._log_unmarked("the thing", {"HOME": str(tmp_path)})
    assert len(writes) == 1 and writes[0].endswith(b"\n")
    assert json.loads((tmp_path / ".levain" / home_guard.LOG_NAME).read_text())["what"] == "the thing"


def test_the_log_stops_growing_at_its_cap(tmp_path):
    """A per-call unmarked script or a crash-looping unit must not grow the log forever (L2 L1): at
    ``LOG_MAX_BYTES`` the row is skipped; the warning is not."""
    (tmp_path / ".levain").mkdir()
    log = tmp_path / ".levain" / home_guard.LOG_NAME
    log.write_bytes(b"x" * home_guard.LOG_MAX_BYTES)
    home_guard._log_unmarked("the thing", {"HOME": str(tmp_path)})
    assert log.stat().st_size == home_guard.LOG_MAX_BYTES
    log.write_bytes(b"x" * (home_guard.LOG_MAX_BYTES - 1))
    home_guard._log_unmarked("the thing", {"HOME": str(tmp_path)})
    assert log.stat().st_size > home_guard.LOG_MAX_BYTES - 1


@pytest.mark.parametrize("breakage", [
    "sys.stderr = None",                                    # fd 2 closed at start: Python leaves sys.stderr None
    "os.close(2)",                                          # fd 2 closed under a live stream
    "class Closed:\n    def write(self, *_): raise ValueError('I/O operation on closed file')\n"
    "    def flush(self): pass\nsys.stderr = Closed()",
])
def test_the_warning_never_reaches_stdout_and_never_raises_when_stderr_is_broken(tmp_path, breakage):
    """A stdout protocol (hook JSON, MCP) must not receive the warning, and a broken stderr must not stop the resolver."""
    script = "import os, sys\n" + breakage + "\n" + RESOLVE
    done, fake_home = _spawn(tmp_path, script)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str((fake_home / ".levain" / "consent").resolve()), done.stdout   # the path, nothing else
    assert "levain: warning" not in done.stdout
    assert len((fake_home / ".levain" / home_guard.LOG_NAME).read_text().splitlines()) == 1   # the log row is independent


@pytest.mark.parametrize("alias", [
    "sys.stderr = sys.stdout\n",
    "import contextlib\n_ctx = contextlib.redirect_stderr(sys.stdout)\n_ctx.__enter__()\n",
    "import os\nos.dup2(1, 2)\n",                          # `2>&1`: descriptor 2 is descriptor 1's open file
])
def test_a_stderr_that_is_stdout_gets_no_warning(tmp_path, alias):
    """A process that pointed stderr at stdout keeps stdout for a protocol: the warning goes nowhere, the row is still
    logged (L3 r1 codex L127; the descriptor-level alias, L3 r2 gemini L249)."""
    done, fake_home = _spawn(tmp_path, "import sys\n" + alias + RESOLVE)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str((fake_home / ".levain" / "consent").resolve()), done.stdout
    assert len((fake_home / ".levain" / home_guard.LOG_NAME).read_text().splitlines()) == 1


@pytest.mark.skipif(os.name != "posix", reason="a pty is POSIX")
def test_a_terminal_that_is_both_stdout_and_stderr_still_gets_the_warning(tmp_path):
    """An interactive shell gives descriptors 1 and 2 the same terminal: that is a person reading, not a protocol."""
    import pty

    fake_home = tmp_path / "fake-home"
    (fake_home / ".levain").mkdir(parents=True)
    script = tmp_path / "bare_script.py"
    script.write_text(RESOLVE, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(fake_home), "PYTHONPATH": str(REPO)}
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen([sys.executable, str(script)], env=env, stdout=slave, stderr=slave, cwd=str(tmp_path))
        os.close(slave)
        import select
        import time

        seen = b""
        deadline = time.monotonic() + 60
        while True:
            left = deadline - time.monotonic()
            assert left > 0, f"pty read timed out; saw {seen!r}"
            ready, _, _ = select.select([master], [], [], left)
            if not ready:
                continue                                      # the assert above fails once the deadline passes
            try:
                chunk = os.read(master, 4096)
            except OSError:                                   # EIO on Linux once the child closed the terminal
                break
            if not chunk:
                break
            seen += chunk
        assert proc.wait(timeout=max(1, deadline - time.monotonic())) == 0
    finally:
        os.close(master)
    assert b"levain: warning" in seen, seen


def test_the_warning_goes_to_descriptor_2_whatever_object_holds_sys_stderr(tmp_path):
    """A stderr replaced by an in-memory buffer (not stdout) does not swallow the warning: it is written to fd 2."""
    done, _ = _spawn(tmp_path, "import io, sys\nsys.stderr = io.StringIO()\n" + RESOLVE)
    assert done.returncode == 0 and "LEVAIN_LIVE" in done.stderr, done.stderr
    assert "levain: warning" not in done.stdout


def test_a_forked_child_warns_and_logs_on_its_own(tmp_path):
    """Once per resolver per PROCESS: a fork child does not inherit the parent's "already warned" (L3 r1 codex L131)."""
    script = (RESOLVE + "import os, sys\npid = os.fork()\nif pid == 0:\n    default_queue_dir()\n    os._exit(0)\n"
              "os.waitpid(pid, 0)\n")
    done, fake_home = _spawn(tmp_path, script)
    assert done.returncode == 0 and done.stderr.count("levain: warning") == 2, done.stderr
    rows = [json.loads(r) for r in (fake_home / ".levain" / home_guard.LOG_NAME).read_text().splitlines()]
    assert len(rows) == 2 and len({r["pid"] for r in rows}) == 2


def test_mark_live_in_a_script_silences_the_warning(tmp_path):
    done, _ = _spawn(tmp_path, "from levain.home_guard import mark_live\nmark_live()\n" + RESOLVE)
    assert done.returncode == 0 and done.stderr == ""


# --- the flip: warn -> refuse is one constant --------------------------------------------------------------------------

def test_the_1_0_action_is_warn_and_the_flip_is_one_named_constant():
    """Pins the staged ruling. A later release flips ``UNMARKED_ACTION`` to ``"refuse"`` AND edits this line, on purpose."""
    assert home_guard.UNMARKED_ACTION == "warn"
    assert home_guard.UNMARKED_ACTIONS == ("warn", "refuse")


def test_flipping_the_constant_turns_the_same_unmarked_run_into_the_refusal(tmp_path):
    script = ("from levain import home_guard\nhome_guard.UNMARKED_ACTION = 'refuse'\n" + RESOLVE)
    done, fake_home = _spawn(tmp_path, script)
    assert done.returncode != 0 and "RealHomeReached" in done.stderr and "LEVAIN_LIVE" in done.stderr, done.stderr
    assert not done.stdout


def test_a_marked_run_is_untouched_by_the_flip(tmp_path):
    script = ("from levain import home_guard\nhome_guard.UNMARKED_ACTION = 'refuse'\n" + RESOLVE)
    done, fake_home = _spawn(tmp_path, script, live=True)
    assert done.returncode == 0 and Path(done.stdout.strip()) == (fake_home / ".levain" / "consent").resolve()


def test_an_unknown_action_is_refused_not_ignored(monkeypatch):
    monkeypatch.setattr(home_guard, "UNMARKED_ACTION", "shrug")
    with pytest.raises(RuntimeError, match="UNMARKED_ACTION"):
        home_guard.unmarked_production_run("the thing", {"HOME": "/nonexistent"})


# --- the test suite never reaches the real store ------------------------------------------------------------------------

def test_the_marker_never_admits_a_test_run(monkeypatch, tmp_path):
    """A leaked ``LEVAIN_LIVE=1`` (an operator shell, a launcher's child) must not open the real home to a test."""
    monkeypatch.setenv(home_guard.LIVE_ENV, "1")
    monkeypatch.delenv("LEVAIN_HOME", raising=False)
    monkeypatch.delenv(home_guard.TEST_SCRATCH_ROOT_ENV, raising=False)
    monkeypatch.setenv("HOME", "/var/empty-not-scratch")
    with pytest.raises(RealHomeReached):
        home_guard.refuse_real_home_fallback("the thing")


def test_mark_live_is_a_no_op_under_a_test_run(monkeypatch):
    monkeypatch.delenv(home_guard.LIVE_ENV, raising=False)
    home_guard.mark_live()
    assert home_guard.LIVE_ENV not in os.environ


def test_a_mark_that_fails_says_so_and_is_in_the_row(tmp_path, monkeypatch, capfd):
    """``mark_live`` never raises, but a launcher it could not mark is recorded, not silently degraded to unmarked: one
    stderr line, and the reason rides the unmarked row that follows (L3 r1 deepseek L75)."""
    def broken(environ=None):
        raise RecursionError("detector broke")

    monkeypatch.setattr(home_guard, "under_test_run", broken)
    monkeypatch.setattr(home_guard, "_MARK_FAILED", None, raising=False)
    home_guard.mark_live()
    assert "could not mark" in capfd.readouterr().err
    assert home_guard.LIVE_ENV not in os.environ
    (tmp_path / ".levain").mkdir()
    home_guard._log_unmarked("the thing", {"HOME": str(tmp_path)})
    row = json.loads((tmp_path / ".levain" / home_guard.LOG_NAME).read_text())
    assert "detector broke" in row["mark_failed"]


def test_the_suite_starts_without_the_marker():
    assert os.environ.get(home_guard.LIVE_ENV) is None


# --- the launchers --------------------------------------------------------------------------------------------------

#: Each module with an ``if __name__ == "__main__"`` block that does NOT call ``mark_live``, and why. A new launcher
#: either marks or is added here with a reason; an entry that stops matching fails below.
EXEMPT = {
    "levain/__main__.py": "delegates to levain.launch.main, which marks",
    "levain/cli.py": "delegates to levain.launch.main, which marks after its re-exec (L3 r2 complement L39)",
    "levain/verifier_drivers/output_compare.py": "a pure comparator: reads two given files, resolves no levain home",
    "levain/desk/room_readguard.py": "a standalone hook script the room launcher copies and runs under `python -I`: stdlib only, "
                                     "imports nothing of levain (pinned by test_room_readguard), and resolves no levain home",
    "levain/desk/room_turn_guard.py": "a standalone hook script the room launcher reads byte for byte and hashes per launch: "
                                      "stdlib only, imports nothing of levain, resolves no levain home",
    "levain/slot/*": "root-run and slot-uid programs of the closed root import tree (tests/ci/root_closure.py); an import of "
                     "home_guard in a root program would add a root edge. None resolves the operator's levain home: the slot "
                     "hook's adapter marks itself (EMBEDDED), slot_allow is run by the marked CLI, and probe_child and "
                     "identity_child are children of a marked parent",
    "levain/templates/*": "hook scripts: each imports the shared _levain_hook (which marks at import) or calls a consent "
                          "adapter's main (which marks), so the process is marked run directly or through levain.hookrun "
                          "(test_a_hook_script_is_marked_run_directly_or_through_hookrun)",
}


def _exempt(rel: str) -> bool:
    return any(rel == k or (k.endswith("/*") and rel.startswith(k[:-1])) for k in EXEMPT)


def _main_block(tree: ast.Module) -> ast.If | None:
    for node in tree.body:
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"):
            return node
    return None


def _block_marks(block: ast.If) -> bool:
    """The ``__main__`` block itself calls ``mark_live`` (a mention in a docstring or a comment elsewhere does not count)."""
    return any(isinstance(n, ast.Call) and (getattr(n.func, "id", None) == "mark_live"
                                            or getattr(n.func, "attr", None) == "mark_live") for n in ast.walk(block))


def _main_modules():
    for p in sorted((REPO / "levain").rglob("*.py")):
        block = _main_block(ast.parse(p.read_text(encoding="utf-8")))
        if block is not None:
            yield p.relative_to(REPO).as_posix(), block


def test_the_scan_counts_only_a_mark_inside_the_main_block():
    mention = ast.parse('"""calls mark_live somewhere"""\nfrom levain.home_guard import mark_live\n'
                        'def main(): mark_live()\nif __name__ == "__main__":\n    main()\n')
    real = ast.parse('if __name__ == "__main__":\n    from levain.home_guard import mark_live\n    mark_live()\n    main()\n')
    assert not _block_marks(_main_block(mention)) and _block_marks(_main_block(real))


def _odd_main_guards(tree: ast.Module) -> list[int]:
    """Lines of a ``__name__`` test the top-level scan cannot read: nested (in a ``try``, a function), or anything but
    exactly ``__name__ == "__main__"`` (``in (...)``, a compound test)."""
    top = {id(n) for n in tree.body}
    odd = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.If) or not any(isinstance(x, ast.Name) and x.id == "__name__" for x in ast.walk(n.test)):
            continue
        t = n.test
        exact = (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id == "__name__"
                 and len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq) and isinstance(t.comparators[0], ast.Constant)
                 and t.comparators[0].value == "__main__")
        if id(n) not in top or not exact:
            odd.append(n.lineno)
    return odd


def test_every_main_guard_is_one_the_scan_reads():
    """L3 r1 complement L34: a guard the scan cannot read would escape it, so there is none."""
    assert _odd_main_guards(ast.parse('try:\n    if __name__ == "__main__":\n        main()\nexcept E:\n    pass\n')) == [2]
    assert _odd_main_guards(ast.parse('if __name__ in ("__main__",):\n    main()\n')) == [1]
    assert _odd_main_guards(ast.parse('if __name__ == "__main__":\n    main()\n')) == []
    odd = {p.relative_to(REPO).as_posix(): lines for p in sorted((REPO / "levain").rglob("*.py"))
           if (lines := _odd_main_guards(ast.parse(p.read_text(encoding="utf-8"))))}
    assert odd == {}


def test_every_module_with_a_main_block_marks_or_is_exempt_with_a_reason():
    mains = list(_main_modules())
    assert len(mains) > 30
    unmarked = [rel for rel, block in mains if not _block_marks(block) and not _exempt(rel)]
    assert unmarked == [], f"add mark_live() to the __main__ block, or EXEMPT with a reason: {unmarked}"
    used = {k for k in EXEMPT if any((rel == k or (k.endswith("/*") and rel.startswith(k[:-1]))) for rel, _ in mains)}
    assert used == set(EXEMPT), f"stale EXEMPT entries: {set(EXEMPT) - used}"
    assert all(len(why) > 20 for why in EXEMPT.values())
    marked_and_exempt = [rel for rel, block in mains if _block_marks(block) and _exempt(rel)]
    assert marked_and_exempt == []


def _console_scripts(pyproject: str) -> dict[str, str]:
    import tomllib

    return dict(tomllib.loads(pyproject).get("project", {}).get("scripts", {}))


def _target_marks(target: str) -> bool:
    """``module:function`` (a console script's target) names a function that calls ``mark_live`` itself: pip's
    wrapper calls the function and never reaches the module's ``__main__`` block."""
    module, _, func = target.partition(":")
    path = REPO / (module.replace(".", "/") + ".py")
    if not path.is_file():
        path = REPO / module.replace(".", "/") / "__init__.py"
    fns = [n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
           if isinstance(n, ast.FunctionDef) and n.name == func.split(".")[-1]]
    return bool(fns) and all(_block_marks(f) for f in fns)


def test_every_console_script_target_marks_in_the_function_it_calls():
    scripts = _console_scripts((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert scripts, "pyproject.toml declares no console script"
    assert [name for name, target in scripts.items() if not _target_marks(target)] == []
    assert not _target_marks("levain.hookrun:main")      # the check has teeth: hookrun marks in its block, not main()


_RENDERED_MODULE = re.compile(r'(?:-m |"-m", ")(levain(?:\.[a-z_]+)+)')


def test_every_module_a_rendered_command_runs_marks_in_its_main_block():
    """A hook, MCP, plist or helper command line levain renders as ``python -m levain.X`` runs X's ``__main__`` block,
    so X must be a module with a block that marks (or is EXEMPT). Run 2026-11-14 for each, unmarked in a scratch HOME:
    every process ended marked, nothing warned or logged."""
    mains = dict(_main_modules())
    rendered = set()
    for p in (REPO / "levain").rglob("*.py"):
        rendered |= set(_RENDERED_MODULE.findall(p.read_text(encoding="utf-8")))
    rendered.discard("levain.coordinator.x")             # a docstring's example of the alias, not a command
    assert len(rendered) > 15
    missing = []
    for mod in sorted(rendered):
        rel = mod.replace(".", "/")
        rel = f"{rel}.py" if (REPO / f"{rel}.py").is_file() else f"{rel}/__main__.py"
        if not _exempt(rel) and not (rel in mains and _block_marks(mains[rel])):
            missing.append(mod)
    assert missing == [], f"a rendered `-m` module that does not mark: {missing}"


def test_the_console_entry_marks_after_it_re_execs(monkeypatch):
    """The marker never crosses an exec (see below), so the image that runs is the one that marks: after the re-exec,
    before the CLI. Marking first would put it in the carry and force a re-exec on every run."""
    from levain import launch

    order: list[str] = []

    class Stop(Exception):
        pass

    monkeypatch.setattr(home_guard, "mark_live", lambda: order.append("mark"))
    monkeypatch.setattr(launch, "reexec_if_needed", lambda *a, **k: order.append("reexec"))
    monkeypatch.setattr(launch, "set_not_dumpable", lambda: (order.append("rest"), (_ for _ in ()).throw(Stop()))[0])
    with pytest.raises(Stop):
        launch.main()
    assert order == ["reexec", "mark", "rest"]


# --- entries that never reach a ``__main__`` block --------------------------------------------------------------------

#: A program held in a string (an installed hook script, a ``python -c`` bootstrap, a staged probe) that imports levain
#: starts a production process whose ``__main__`` block never runs, so the scan above cannot see it. Every module
#: holding such a string is listed: ``("marks", function)`` means that function calls ``mark_live`` (the entry the
#: string reaches), ``("exempt", reason)`` means the process resolves no levain home or is marked by what it reaches.
EMBEDDED = {
    "levain/seat_gh.py": ("marks", "main"),
    "levain/consent/adapters/claude_code.py": ("marks", "main"),
    "levain/consent/adapters/codex.py": ("marks", "main"),
    "levain/consent/canary.py": ("exempt", "canary_hook_main names every path it touches (the record file) and resolves no "
                                           "levain home; the `levain` re-exec string it also holds reaches launch.main, which marks"),
    "levain/team/cli.py": ("exempt", "an import check only (`import levain.team.hook`): it runs no entry and resolves no home"),
    "levain/observe.py": ("exempt", "an import check only (`import levain.observe`): it runs no entry and resolves no home"),
    "levain/doctor.py": ("exempt", "the slot reaper staged under root (levain.slot.reaper), in the closed root import tree, "
                                   "acting on the slot's own R/ tree and not the operator's levain home"),
    "levain/slot/*": ("exempt", "root-run and slot-uid programs of the closed root import tree (tests/ci/root_closure.py); "
                                "an import of home_guard would add a root edge, and none resolves the operator's home"),
}

_EMBEDDED_IMPORT = re.compile(r"(?m)(?:^|[;'\"])\s*(?:from\s+levain\b|import\s+(?:[\w.]+(?:\s+as\s+\w+)?\s*,\s*)*levain\b)")


@pytest.mark.parametrize("text, hit", [
    ("import levain.x", True), ("from levain.x import y", True), ("a = 1; import levain", True),
    ("python3 -c 'from levain.cli import main'", True), ('-c "import levain.hookrun"', True),
    ("import sys, levain.x", True), ("import os as o, sys, levain", True),
    ("the levain module", False), ("import levainx", False), ("import sys, os", False),
])
def test_the_embedded_scan_sees_a_quoted_c_program_and_a_listed_import(text, hit):
    """L2 I2: ``-c 'from levain...'`` (a quote before ``from``) and ``import sys, levain.x`` used to escape the scan."""
    assert bool(_EMBEDDED_IMPORT.search(text)) is hit


def _embedded_modules():
    for p in sorted((REPO / "levain").rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        if any(isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
               and _EMBEDDED_IMPORT.search(n.value) for n in ast.walk(tree)):
            yield p.relative_to(REPO).as_posix(), tree


def _calls_mark_live(fn: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) and (getattr(n.func, "id", None) == "mark_live"
                                            or getattr(n.func, "attr", None) == "mark_live") for n in ast.walk(fn))


def test_every_program_held_in_a_string_that_imports_levain_marks_or_is_exempt_with_a_reason():
    found = dict(_embedded_modules())
    keys = {k: (found_rel := [rel for rel in found if rel == k or (k.endswith("/*") and rel.startswith(k[:-1]))])
            for k in EMBEDDED}
    unlisted = [rel for rel in found if not any(rel in v for v in keys.values())]
    assert unlisted == [], f"list in EMBEDDED, with the function that marks or a reason: {unlisted}"
    assert [k for k, v in keys.items() if not v] == [], "stale EMBEDDED entries"
    for key, (kind, what) in EMBEDDED.items():
        if kind == "exempt":
            assert len(what) > 20
            continue
        (rel,) = keys[key]
        fns = [n for n in ast.walk(found[rel]) if isinstance(n, ast.FunctionDef) and n.name == what]
        assert fns and all(_calls_mark_live(f) for f in fns), f"{rel}: {what}() must call mark_live"


@pytest.mark.parametrize("call", [
    "from levain.consent.adapters.claude_code import main\nmain([], io.StringIO('{}'), io.StringIO())",
    "from levain.consent.adapters.codex import main\nmain([], io.StringIO('{}'), io.StringIO())",
    "from levain.seat_gh import main\nmain(['nothing'])",
])
def test_an_entry_an_installed_script_imports_marks_the_process_and_resolves_silently(tmp_path, call):
    """The hook scripts and git's credential helper import ``main`` and never reach a ``__main__`` block."""
    done, fake_home = _spawn(tmp_path, "import io, os\n" + call + "\nprint(os.environ.get('LEVAIN_LIVE'))\n")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "1"
    assert "levain: warning" not in done.stderr
    assert not (fake_home / ".levain" / home_guard.LOG_NAME).exists()


_HOOK_DIRS = ("levain/templates/activation/hooks", "levain/templates/adapters/codex/activation/hooks")
_HOOK_SCRIPTS = [p.relative_to(REPO).as_posix() for d in _HOOK_DIRS for p in sorted((REPO / d).glob("*.py"))
                 if not p.name.startswith("_")]


@pytest.mark.parametrize("shape", ["direct", "hookrun"])
@pytest.mark.parametrize("script", _HOOK_SCRIPTS)
def test_a_hook_script_is_marked_run_directly_or_through_hookrun(tmp_path, script, shape):
    """An install rendered before ``levain.hookrun`` (or an operator running a hook by hand) runs ``python <script>``:
    that process must be marked too, or it warns on every hook call now and, after the flip, the hooks' fail-open
    ``consent_observe``/``consent_ledger`` swallow the refusal and lose the record in silence (L2 M1, run 2026-11-14:
    every non-consent script warned run directly)."""
    fake_home = tmp_path / "fake-home"
    (fake_home / ".levain").mkdir(parents=True)
    # The hookrun shape as `-m levain.hookrun` runs it, but with THIS tree's levain asserted first: the rendered `-EP`
    # ignores PYTHONPATH and would grade whatever levain the interpreter has installed.
    drive = (f"import runpy, sys\nsys.path.insert(0, {str(REPO)!r})\nimport levain\n"
             f"assert levain.__file__.startswith({str(REPO)!r}), levain.__file__\n"
             "runpy.run_module('levain.hookrun', run_name='__main__', alter_sys=True)\n")
    probe = ("import sys, levain\nassert levain.__file__.startswith(%r), levain.__file__\n" % str(REPO))
    argv = ([sys.executable, str(REPO / script)] if shape == "direct"
            else [sys.executable, "-P", "-c", drive, str(REPO / script)])
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(fake_home), "PYTHONPATH": str(REPO)}
    ev = json.dumps({"hook_event_name": "Stop", "session_id": "s", "cwd": str(tmp_path), "prompt": "hi",
                     "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {}, "source": "startup"})
    if shape == "direct":     # the script's own process imports levain from PYTHONPATH: prove that is this tree
        seen = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=60,
                              cwd=str(REPO / Path(script).parent))
        assert seen.returncode == 0, seen.stderr
    done = subprocess.run(argv, input=ev, env=env, capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert done.returncode == 0, done.stderr
    assert "levain: warning" not in done.stderr, done.stderr
    assert not (fake_home / ".levain" / home_guard.LOG_NAME).exists()


# --- what a model-driven child inherits --------------------------------------------------------------------------------

def test_the_marker_never_crosses_an_exec(monkeypatch):
    """Each levain entry marks itself (the scans above), so no child needs to inherit the marker, and one that did would
    hand it to a model: the desk session and the consent resume start Claude with ``child_env``, and a script that
    model writes would then resolve the real home in silence (L3 r1 codex HIGH, run 2026-11-14: all of these carried
    it). The one list every exec goes through refuses the name."""
    from levain import launch
    from levain.consent import delivery

    monkeypatch.setenv(home_guard.LIVE_ENV, "1")
    assert launch.LIVE_ENV == home_guard.LIVE_ENV
    assert not launch.allowed(home_guard.LIVE_ENV)
    assert home_guard.LIVE_ENV not in launch.allowed_env(os.environ)                    # the re-exec's kept block
    assert home_guard.LIVE_ENV not in launch.child_env()
    assert home_guard.LIVE_ENV not in launch.child_env(seat="attended")                 # the desk session
    assert home_guard.LIVE_ENV not in launch.child_env(*delivery._AGENT_AUTH, *launch.NETWORK, seat="attended")
    assert home_guard.LIVE_ENV not in launch.child_env(home_guard.LIVE_ENV, prefixes=("LEVAIN_",))   # not even by name


def test_the_marker_does_not_ride_the_re_exec_carry(tmp_path):
    """A marked image that re-executes: the re-executed image starts unmarked, and so does a bare child it spawns before
    it marks itself. The carry restores every name the kept block dropped, so a carried marker would cross the exec
    (L3 r2 codex L148, run 2026-11-14: the bare child printed '1')."""
    probe = ("import os, subprocess, sys\nfrom levain import launch\nlaunch.set_not_dumpable = lambda: False\n"
             "launch.reexec_if_needed()\nprint('REEXEC', launch.reexecuted)\nprint('FOO', os.environ.get('FOO_CARRIED'))\n"
             "print('LIVE', os.environ.get('LEVAIN_LIVE'))\n"
             "c = subprocess.run([sys.executable, '-I', '-c', \"import os; print(os.environ.get('LEVAIN_LIVE'))\"],"
             " capture_output=True, text=True)\nprint('CHILD', c.stdout.strip())\n")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "PYTHONPATH": str(REPO),
           "FOO_CARRIED": "x", home_guard.LIVE_ENV: "1"}
    done = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = dict(line.split(" ", 1) for line in done.stdout.splitlines())
    assert out["REEXEC"] == "True" and out["FOO"] == "x"                 # the carry ran and restored the rest
    assert out["LIVE"] == "None" and out["CHILD"] == "None", out


def test_no_model_driven_child_environment_carries_the_marker(monkeypatch):
    """A script an agent writes and runs must not inherit ``LEVAIN_LIVE``, or the incident class (an unmarked bare
    script) would resolve the real home in silence from inside a levain-launched session. The seat agent's environment
    (host_env), the confined shell's default and the hands user's are allowlists with no ``LEVAIN_`` namespace."""
    from levain import host_guard
    from levain.firing import confinement

    monkeypatch.setenv(home_guard.LIVE_ENV, "1")
    monkeypatch.setenv("LEVAIN_HOME", "/scratch")
    assert not [k for k in host_guard.host_env(os.environ) if k.startswith("LEVAIN_")]
    assert not [k for k in confinement._default_shell_env() if k.startswith("LEVAIN_")]
    hands = confinement.HandsIdentity("h", 1, "/home/h", Path("/ws"), ())
    assert not [k for k in confinement._hands_env(hands) if k.startswith("LEVAIN_")]


# --- the marker crosses no exec levain makes (Diogenes 11-15, spawns that passed no env) ---------------------------------

def test_unmarked_env_drops_only_the_marker(monkeypatch):
    monkeypatch.setenv(home_guard.LIVE_ENV, "1")
    got = home_guard.unmarked_env()
    assert home_guard.LIVE_ENV not in got and got.get("PATH") == os.environ.get("PATH")
    assert home_guard.unmarked_env({home_guard.LIVE_ENV: "1", "A": "b"}) == {"A": "b"}


@pytest.mark.parametrize("explicit", [False, True])
def test_a_spawned_fire_never_inherits_the_marker(tmp_path, monkeypatch, explicit):
    """``spawn_fire`` started its child with ``env=None`` (or the caller's env as given): the fire marks itself, so it
    never needs the marker handed down, and a caller's own marked environment must not carry it on."""
    from levain.agents import controller as ctl

    monkeypatch.setenv(home_guard.LIVE_ENV, "1")
    out = tmp_path / "env.txt"
    script = tmp_path / "dump.py"
    script.write_text("import os, sys\nopen(sys.argv[1], 'w').write(repr(sorted(os.environ)))\n", encoding="utf-8")
    kw = {"env": dict(os.environ)} if explicit else {}
    proc = ctl.spawn_fire(tmp_path, "oc-" + "a" * 32, argv=[sys.executable, str(script), str(out)], **kw)
    assert proc.wait(60) == 0
    names = ast.literal_eval(out.read_text())
    assert home_guard.LIVE_ENV not in names and "PATH" in names
