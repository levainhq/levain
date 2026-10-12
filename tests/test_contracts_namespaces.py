"""F11: the SSHSIG namespace registry, against frozen/namespaces.json, and the AST test over the levain package.

The AST rule (S1_DESIGN F11): every string constant that follows ``"-n"`` in a list literal, every ``namespace=``
keyword argument and every module-level ``*NAMESPACE*`` assignment must be in the registry. The ``"-n"`` clause is
read as ``ssh-keygen -Y`` argument lists only (a list literal that also holds the constant ``"-Y"``): ``sudo -n``
and ``git rev-list -n`` lists sit in the same package and their ``-n`` is not a namespace (RETURN.md, open question 1).
A namespace computed at run time escapes this test (known edge).
"""
import ast
import json
from pathlib import Path

import pytest

from levain.contracts import namespaces as NS

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "levain"
GOLDEN = json.loads((ROOT / "tests/golden/contracts/frozen/namespaces.json").read_text(encoding="utf-8"))


def _str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def namespace_uses(tree: ast.AST):
    """(namespace, lineno, rule) for every literal namespace the three rules see."""
    for node in ast.walk(tree):
        if isinstance(node, ast.List):
            consts = [_str(e) for e in node.elts]
            if "-Y" in consts:
                for i, c in enumerate(consts[:-1]):
                    if c == "-n" and consts[i + 1] is not None:
                        yield consts[i + 1], node.lineno, "-n"
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "namespace" and _str(kw.value) is not None:
                    yield _str(kw.value), node.lineno, "namespace="
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            for a, d in zip(args.args[len(args.args) - len(args.defaults):] + args.kwonlyargs,
                            list(args.defaults) + list(args.kw_defaults)):
                if a.arg == "namespace" and d is not None and _str(d) is not None:
                    yield _str(d), node.lineno, "namespace="
    for node in getattr(tree, "body", []):
        targets = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for t in targets:
            if isinstance(t, ast.Name) and "NAMESPACE" in t.id and _str(value) is not None:
                yield _str(value), node.lineno, "*NAMESPACE*"


FILES = sorted(PACKAGE.rglob("*.py"))


def test_the_registry_equals_the_frozen_golden():
    assert {n["namespace"]: n["status"] for n in GOLDEN["namespaces"]} == {k: v[0] for k, v in NS.REGISTRY.items()}
    assert {n["namespace"]: n["owner"] for n in GOLDEN["namespaces"]} == {k: v[1] for k, v in NS.REGISTRY.items()}
    assert [f["prefix"] for f in GOLDEN["families"]] == list(NS.FAMILIES)
    assert NS.TEAM_ENTRY_NAMESPACE == "levain-team-entry" and NS.is_registered(NS.TEAM_ENTRY_NAMESPACE)


@pytest.mark.parametrize("name", GOLDEN["not_registered_controls"])
def test_reservations_are_exact_strings_never_prefixes(name):
    assert not NS.is_registered(name)


def test_no_registered_name_is_a_family_member_other_than_entry():
    for name in NS.REGISTRY:
        for prefix in NS.FAMILIES:
            if name.startswith(prefix):
                assert name == "levain-team-entry", name


def test_every_literal_namespace_in_the_package_is_registered():
    seen = []
    for path in FILES:
        for ns, line, rule in namespace_uses(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            seen.append((ns, f"{path.relative_to(ROOT)}:{line}", rule))
    bad = [s for s in seen if not NS.is_registered(s[0])]
    assert bad == [], bad
    found = {s[0] for s in seen}
    # the walk reaches every shipped namespace and F9's own constant, so a rule that matches nothing goes red
    assert {"git", "levain-authority", "levain-authority-reset", "levain-confirm", "levain-cockpit",
            "levain-team-entry"} <= found, found


@pytest.mark.parametrize("src, ns", [
    ('subprocess.run(["ssh-keygen", "-Y", "sign", "-n", "levain-rogue", "-f", k])', "levain-rogue"),
    ('sign(m, namespace="levain-rogue")', "levain-rogue"),
    ('def f(m, namespace="levain-rogue"):\n    pass', "levain-rogue"),
    ('def f(m, *, namespace: str = "levain-rogue"):\n    pass', "levain-rogue"),
    ('ROGUE_NAMESPACE = "levain-rogue"', "levain-rogue"),
    ('ROGUE_NAMESPACE: str = "levain-rogue"', "levain-rogue"),
])
def test_each_rule_catches_an_unregistered_namespace(src, ns):
    assert [u[0] for u in namespace_uses(ast.parse(src))] == [ns]
    assert not NS.is_registered(ns)


@pytest.mark.parametrize("src", [
    '["sudo", "-n", "-u", user, "/bin/true"]',
    '["git", "rev-list", "--parents", "-n", "1", sha]',
    'def f():\n    LOCAL_NAMESPACE = "not module level"',
])
def test_minus_n_outside_ssh_keygen_and_function_locals_are_not_namespaces(src):
    assert list(namespace_uses(ast.parse(src))) == []
