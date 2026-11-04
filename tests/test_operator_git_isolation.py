"""1104-8: the operator's git is isolated by ONE helper, and no operator-side git ever runs on a slot clone.

* The two operator git wrappers (``dispatch/adopt.py::_git``, ``slot/adopt_take.py::_git``) and the view's
  (``slot/adopt_view.py::_git``) build their ``-c`` list, ``--git-dir`` and env overlay from
  ``levain.consent.push.git_isolation_args`` / ``git_isolation_env``. Before 1104-8 adopt.py lacked the
  ``hook.reference-transaction.enabled=false`` that adopt_take carried (two things that should be one).
* ``test_no_operator_git_call_names_a_session_clone`` pins the NP-1 premise: the operator side reads
  ``<S>/repo`` as ``levain-walk/1`` frames only. It walks the AST of every ``levain/`` module (bar the one that
  runs git as the slot uid inside the wall), finds each call that invokes git, and refuses one whose arguments
  (cwd, ``-C``, ``--git-dir``, ``--work-tree``, any path) carry a session-clone name. WHAT AN AST WALK CANNOT SEE:
  an argv or cwd built in another function and passed in as a plain variable, a path computed from a string
  the walk does not recognise as a clone, ``getattr``/``exec`` indirection. The negative control proves the
  scanner can fail on the shapes it does claim.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from levain.consent import push
from levain.dispatch import adopt
from levain.slot import adopt_take

REPO = Path(__file__).resolve().parents[1]
ISOLATION = set(push.GIT_ISOLATION_CONFIG)


def _c_values(argv):
    return {argv[i + 1] for i, a in enumerate(argv) if a == "-c"}


def _capture(monkeypatch, module):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"], seen["cwd"] = list(argv), kw.get("env"), kw.get("cwd")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return seen


def test_adopt_and_adopt_take_build_the_same_isolation(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    a = _capture(monkeypatch, adopt)
    adopt._git(str(repo), "cat-file", "-e", "0" * 40)
    t = _capture(monkeypatch, adopt_take)
    adopt_take._git("git", repo, "cat-file", "-e", "0" * 40)
    for name, seen in (("adopt", a), ("adopt_take", t)):
        assert ISOLATION <= _c_values(seen["argv"]), f"{name} lacks {ISOLATION - _c_values(seen['argv'])}"
        assert f"--git-dir={repo}/.git" in seen["argv"]
        for k, v in push.git_isolation_env().items():
            assert seen["env"][k] == v, f"{name} env {k}"
        assert seen["argv"].index("cat-file") > max(i for i, x in enumerate(seen["argv"]) if x == "-c")


def test_the_view_uses_the_shared_helper(tmp_path):
    src = (REPO / "levain/slot/adopt_view.py").read_text()
    assert "push.git_isolation_args(" in src and '"core.hooksPath=/dev/null"' not in src


# --- the invariant: no operator-side git names a session clone ---------------------------------------------------

CLONE_NAME = re.compile(r"session_repo|slot_repo|clone_dir|clone_path|clone_root|session_clone")
#: runs git as the slot uid inside the wall, on the clone, by design (RESEARCH_clone_git_hooks.md row 8)
SLOT_UID_MODULES = {"levain/slot/wall.py"}


def _mentions_clone(node, tainted):
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and (CLONE_NAME.search(n.id) or n.id in tainted):
            return True
        if isinstance(n, ast.Attribute) and CLONE_NAME.search(n.attr):
            return True
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and CLONE_NAME.search(n.value):
            return True
    return False


def _invokes_git(call):
    f = call.func
    name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
    if "git" in name.lower():
        return True
    if name in {"run", "Popen", "check_output", "check_call", "call"}:
        for n in ast.walk(call):
            if isinstance(n, ast.Constant) and n.value == "git":
                return True
            if isinstance(n, (ast.Name, ast.Attribute)) and "git" in (getattr(n, "id", "") or getattr(n, "attr", "")).lower():
                return True
    return False


def scan_source(src, path="<scratch>"):
    """(git_calls_seen, violations) for one module's source. Taint is per function: a name assigned from a
    clone-named expression in the same (or an enclosing) function is a clone name."""
    seen, bad = [0], []

    def visit(node, tainted):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local = set(tainted)
                for st in ast.walk(child):
                    if isinstance(st, ast.Assign) and _mentions_clone(st.value, local):
                        local |= {t.id for t in st.targets if isinstance(t, ast.Name)}
                    elif (isinstance(st, ast.AnnAssign) and st.value is not None
                          and isinstance(st.target, ast.Name) and _mentions_clone(st.value, local)):
                        local.add(st.target.id)
                    elif (isinstance(st, ast.NamedExpr) and isinstance(st.target, ast.Name)
                          and _mentions_clone(st.value, local)):
                        local.add(st.target.id)
                visit(child, local)
                continue
            if isinstance(child, ast.Call) and _invokes_git(child):
                seen[0] += 1
                parts = [*child.args, *[k.value for k in child.keywords]]
                if any(_mentions_clone(p, tainted) for p in parts):
                    bad.append(f"{path}:{child.lineno}")
            visit(child, tainted)

    visit(ast.parse(src), set())
    return seen[0], sorted(set(bad))


def test_no_operator_git_call_names_a_session_clone():
    total, bad = 0, []
    for p in sorted((REPO / "levain").rglob("*.py")):
        rel = str(p.relative_to(REPO))
        if rel in SLOT_UID_MODULES:
            continue
        seen, b = scan_source(p.read_text(), rel)
        total += seen
        bad += b
    assert total > 30, f"the scanner saw only {total} git calls: it is not looking at the operator side"
    assert not bad, "operator-side git names a session clone: " + ", ".join(bad)


def test_the_scanner_can_fail(tmp_path):
    """Negative control: a scratch module with each shape the scanner claims to catch."""
    scratch = tmp_path / "scratch_op.py"
    scratch.write_text(
        "import subprocess\n"
        "from levain.slot.layout import session_repo\n"
        "def a(h, s):\n    subprocess.run(['git', 'status'], cwd=session_repo(h, s))\n"
        "def b(h, s):\n    subprocess.run(['git', '-C', str(session_repo(h, s)), 'log'])\n"
        "def c(h, s):\n    r = session_repo(h, s)\n    subprocess.run(['git', f'--git-dir={r}/.git', 'log'])\n"
        "def d(h, s):\n    _git(session_repo(h, s), 'diff')\n"
        "def e(slot_repo):\n    subprocess.run(['git', f'--work-tree={slot_repo}', 'status'])\n"
        "def clean(operator_repo):\n    subprocess.run(['git', '-C', operator_repo, 'status'])\n"
        "def f(h, s):\n    repo: str = session_repo(h, s)\n    _git(repo, 'status')\n"
        "def g(h, s):\n    if (r := session_repo(h, s)):\n        _git(r, 'status')\n")
    seen, bad = scan_source(scratch.read_text(), "scratch_op.py")
    assert seen == 8
    assert len(bad) == 7, bad
    assert sorted(int(b.rsplit(":", 1)[1]) for b in bad) == [4, 6, 9, 11, 13, 18, 21]   # the clean call (line 15) is not flagged
    # the limit, stated: an argv built elsewhere and passed as a plain variable is invisible
    _, hidden = scan_source("import subprocess\ndef f(argv):\n    subprocess.run(argv)\n")
    assert hidden == []
