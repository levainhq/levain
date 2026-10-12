"""The wire-neutral ledger surface (``levain.team.ledger_api``): GitLedger satisfies it, and no caller reaches past it.

(a) static conformance: mypy accepts a GitLedger where a LedgerTransport is wanted, and refuses a class missing a member
(so the probe can fail). (b) runtime: every protocol member exists on GitLedger with the same signature. (c) the
private-reach guard: no module in ``levain/team`` other than the transport and this API reads ``<ledger>._<name>``.
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from levain.team import ledger_api as A
from levain.team.transport import GitLedger, Repo, TeamError

ROOT = Path(__file__).resolve().parents[1]
TEAM = ROOT / "levain" / "team"

def _protocol_members() -> dict[str, object]:
    """Protocol member -> its definition (None for a data attribute), from typing's own record of the members."""
    return {k: vars(A.LedgerTransport).get(k) for k in sorted(A.LedgerTransport.__protocol_attrs__)}


# ---- (a) static conformance --------------------------------------------------------------------------------------


def _mypy(tmp_path: Path, probe: str) -> list[str]:
    if importlib.util.find_spec("mypy") is None:
        pytest.skip("mypy is not installed in this environment (it is not a dev dependency); the static check did not run")
    f = tmp_path / "s0_probe.py"
    f.write_text(probe, encoding="utf-8")
    cp = subprocess.run([sys.executable, "-m", "mypy", "--follow-imports=silent", "--no-incremental",
                         "--cache-dir", os.devnull, "--no-error-summary", "--show-absolute-path",
                         str(TEAM / "ledger_api.py"), str(TEAM / "transport.py"), str(f)],
                        cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    lines = [x for x in cp.stdout.splitlines() if ": error:" in x]
    # transport.py carries type errors of its own that predate this module; only errors that name the API, the probe or
    # the protocol are this test's business
    return [x for x in lines if not x.startswith(str(TEAM / "transport.py")) or "LedgerTransport" in x]


def test_gitledger_satisfies_the_protocol_statically(tmp_path):
    errors = _mypy(tmp_path, "from pathlib import Path\n"
                             "from levain.team.ledger_api import LedgerTransport, open_ledger, require_git_wire\n"
                             "from levain.team.transport import GitLedger, Repo\n"
                             "x: LedgerTransport = GitLedger(Repo(Path('/a'), Path('/b')))\n"
                             "y: LedgerTransport = open_ledger(Repo(Path('/a'), Path('/b')))\n"
                             "z: GitLedger = require_git_wire(y)\n")
    assert errors == [], "\n".join(errors)


def test_the_static_probe_can_fail(tmp_path):
    errors = _mypy(tmp_path, "from levain.team.ledger_api import LedgerTransport\n"
                             "class Half:\n"
                             "    def sync(self, *, push: bool = True) -> str:\n"
                             "        return ''\n"
                             "x: LedgerTransport = Half()\n")
    assert any("s0_probe.py" in e and "LedgerTransport" in e for e in errors), errors


# ---- (b) runtime: every member exists on GitLedger, same signature -------------------------------------------------


def _params(fn) -> list[tuple[str, object, object, object]]:
    sig = inspect.signature(fn)
    return [(p.name, p.kind, p.default, p.annotation) for p in sig.parameters.values()]


def test_every_protocol_member_exists_on_gitledger_with_its_signature():
    gl = GitLedger(Repo(Path("/nonexistent/top"), Path("/nonexistent/top/.git")))
    members = _protocol_members()
    assert {"append", "snapshot", "derivation", "update_counted", "remote", "repo"} <= set(members)
    mismatched: list[str] = []
    for name, proto in members.items():
        impl = inspect.getattr_static(GitLedger, name, None)
        if proto is None:                                   # a data attribute: set on the instance
            if not hasattr(gl, name) or name in vars(GitLedger):
                mismatched.append(f"{name}: not an instance attribute of GitLedger")
            continue
        if impl is None:
            mismatched.append(f"{name}: missing on GitLedger")
            continue
        if isinstance(proto, property):
            if not isinstance(impl, property) or \
                    inspect.signature(proto.fget).return_annotation != inspect.signature(impl.fget).return_annotation:
                mismatched.append(f"{name}: a property in the protocol, not the same property on GitLedger")
            continue
        if isinstance(proto, staticmethod) != isinstance(impl, staticmethod):
            mismatched.append(f"{name}: static in one, not the other")
            continue
        pf = proto.__func__ if isinstance(proto, staticmethod) else proto
        gf = impl.__func__ if isinstance(impl, staticmethod) else impl
        mine, theirs = _params(pf), _params(gf)
        # GitLedger may carry extra private keyword knobs (``_check``, ``_lower``) the protocol leaves out, never more
        extra = [p for p in theirs if p not in mine]
        if [p for p in theirs if p in mine] != mine or any(
                not p[0].startswith("_") or p[2] is inspect.Parameter.empty for p in extra):
            mismatched.append(f"{name}: {mine} != {theirs}")
        if inspect.signature(pf).return_annotation != inspect.signature(gf).return_annotation:
            mismatched.append(f"{name}: returns {inspect.signature(pf).return_annotation!r}, GitLedger "
                              f"{inspect.signature(gf).return_annotation!r}")
    assert mismatched == [], "\n".join(mismatched)


def test_open_ledger_and_require_git_wire():
    repo = Repo(Path("/nonexistent/top"), Path("/nonexistent/top/.git"))
    gl = A.open_ledger(repo)
    assert type(gl) is GitLedger and gl.repo is repo
    assert A.require_git_wire(gl) is gl

    class Other:
        pass
    with pytest.raises(TeamError, match="not carried by git"):
        A.require_git_wire(Other())   # type: ignore[arg-type]


# ---- (c) THE PRIVATE-REACH GUARD -----------------------------------------------------------------------------------

_LEDGER_NAMES = {"gl", "g", "ledger", "led"}        # the names callers give a ledger, plus any name annotated as one
_LEDGER_TYPES = ("GitLedger", "LedgerTransport")


def _typed_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(tree):
        ann, target = None, None
        if isinstance(n, ast.arg):
            ann, target = n.annotation, n.arg
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            ann, target = n.annotation, n.target.id
        if ann is not None and any(t in ast.unparse(ann) for t in _LEDGER_TYPES):
            names.add(target)
    return names


def private_reaches(root: Path = TEAM) -> list[str]:
    """Every ``<ledger>._<name>`` read in ``root`` outside the transport and the API (dunders excepted)."""
    found: list[tuple[str, int, int, str]] = []
    for p in sorted(root.glob("*.py")):
        if p.name in ("transport.py", "ledger_api.py"):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        names = _LEDGER_NAMES | _typed_names(tree)
        for n in ast.walk(tree):
            if not isinstance(n, ast.Attribute) or not n.attr.startswith("_") or n.attr.startswith("__"):
                continue
            v = n.value
            if isinstance(v, ast.Name) and (v.id in names or v.id in _LEDGER_TYPES):
                who = v.id
            elif isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id == "self" \
                    and v.attr in names:
                who = f"self.{v.attr}"
            else:
                continue
            found.append((p.name, n.lineno, n.col_offset, f"{p.name}:{n.lineno} {who}.{n.attr}"))
    return [x[3] for x in sorted(found)]


def test_the_guard_can_fail_and_can_pass(tmp_path):
    (tmp_path / "caller.py").write_text(
        "def f(x: GitLedger, gl, other):\n"
        "    gl._has('r'); x._show('a'); GitLedger._ledger_of(1); other._fine; gl.__class__; gl.public\n"
        "class C:\n    def m(self):\n        return self.gl._dcache\n", encoding="utf-8")
    (tmp_path / "transport.py").write_text("gl._anything\n", encoding="utf-8")
    assert private_reaches(tmp_path) == ["caller.py:2 gl._has", "caller.py:2 x._show",
                                         "caller.py:2 GitLedger._ledger_of", "caller.py:5 self.gl._dcache"]


@pytest.mark.xfail(strict=True, reason="S0-b migrates the callers")
def test_no_caller_reaches_a_private_ledger_member():
    reaches = private_reaches()
    assert reaches == [], "callers reach private ledger members:\n" + "\n".join(reaches)
