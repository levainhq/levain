"""The bodies of dispatch configuration items, judged by kind against the active configuration, through a real
control store (real keys, real SQLite): templates (computed content hash, the R0 write rule, the verifier
cross-check), verifiers, the provider policy, the route map and adoption rules (re-judged on every read)."""
from __future__ import annotations

import pytest

from levain.dispatch import DispatchError
from levain.dispatch import control as ctl
from levain.dispatch import registry
from levain.dispatch.contract import _path_problem
from levain.dispatch.registry import (
    EXECUTION_BEARING_DIRS, EXECUTION_BEARING_FILES, R0_SOURCE_EXTENSIONS, AdoptionRule, active_adoption_rules,
    execution_bearing, r0_write_refusal, resolver, template_content_hash,
)
from tests.test_dispatch_contract import POLICY
from tests import test_dispatch_control as tc
from tests.test_dispatch_control import BB, IP, VERIFIERS, accept, base_config, tmpl

# the control tests' fixtures: real keys, a real authority log, a pinned store
keys, log, cdir, store = tc.keys, tc.log, tc.cdir, tc.store


def refused(store, log, keys, kind, gen, body, match):
    with pytest.raises(ctl.ConfigRefused, match=match):
        accept(store, log, keys, kind, gen, body)


# --- templates -----------------------------------------------------------------------------------------------------

def test_the_content_hash_is_computed_never_taken(store, log, keys):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    t = tmpl()
    t["content_hash"] = "sha256:" + "a" * 64
    refused(store, log, keys, "templates", 2, [t], "is not its computed hash")
    t = tmpl()
    t["executor"] = "claude"                 # edited after its hash was computed
    refused(store, log, keys, "templates", 2, [t], "is not its computed hash")


def test_the_content_hash_ignores_write_path_order():
    a, b = tmpl(), tmpl()
    b["write_paths"] = list(reversed(a["write_paths"]))
    assert template_content_hash(a) == template_content_hash(b)


# Every path the L1 and L2 reviews named, with the verdict R0 owes it: DENY (execution-bearing, refused whatever its
# extension), NOT_SOURCE (outside the eligible-source allowlist) or OK. A future miss costs one row here.
DENY, NOT_SOURCE, OK = "execution-bearing", "not R0-eligible", None
R0_PATHS = [
    # tests, at any depth and in any case
    ("tests/test_x.py", DENY), ("src/test_x.py", DENY), ("src/x_test.py", DENY), ("pkg/tests/util.py", DENY),
    ("conftest.py", DENY), ("src/conftest.py", DENY), ("SRC/CONFTEST.PY", DENY), ("conftest.PY", DENY),
    ("SRC/Tests/Helper.PY", DENY), ("src/Tests/x.py", DENY), ("src/Test_X.py", DENY), ("test/x.py", DENY),
    ("testing/x.py", DENY), ("__tests__/a.js", DENY), ("spec/a_spec.rb", DENY), ("e2e/x.py", DENY),
    ("test.py", DENY), ("tests.py", DENY), ("a.test.ts", DENY), ("a.spec.js", DENY), ("a_test.go", DENY),
    ("lib/a_spec.rb", DENY),
    # build, packaging and CI
    ("Makefile", DENY), ("MAKEFILE", DENY), ("pyproject.toml", DENY), ("setup.py", DENY), ("setup.cfg", DENY),
    ("tox.ini", DENY), ("noxfile.py", DENY), ("build.rs", DENY), ("jest.config.js", DENY),
    ("webpack.config.js", DENY), ("vitest.config.ts", DENY), (".github/workflows/ci.yml", DENY),
    ("a/.GITHUB/w.yml", DENY), (".gitlab-ci.yml", DENY), (".circleci/config.yml", DENY),
    # hooks
    (".githooks/pre-commit", DENY), (".husky/pre-commit", DENY), (".pre-commit-config.yaml", DENY),
    (".pre-commit-config.yml", DENY), (".gitattributes", DENY), (".gitmodules", DENY),
    # interpreter start-up and environment
    ("sitecustomize.py", DENY), ("usercustomize.py", DENY), ("a.pth", DENY), (".envrc", DENY), (".npmrc", DENY),
    (".eslintrc.js", DENY), ("__pycache__/x.cpython-312.pyc", DENY),
    # agent and editor configuration
    (".claude/settings.json", DENY), (".codex/config.toml", DENY), ("AGENTS.md", DENY), ("CLAUDE.md", DENY),
    ("docs/CLAUDE.md", DENY), ("GEMINI.md", DENY), ("CLAUDE.local.md", DENY), (".mcp.json", DENY),
    (".vscode/tasks.json", DENY), (".idea/workspace.xml", DENY),
    # lockfiles
    ("Cargo.lock", DENY), ("package-lock.json", DENY), ("poetry.lock", DENY), ("uv.lock", DENY),
    # not source or docs: manifests, build files and scripts the allowlist does not name
    ("package.json", NOT_SOURCE), ("Cargo.toml", NOT_SOURCE), ("go.mod", NOT_SOURCE), ("Gemfile", NOT_SOURCE),
    ("Rakefile", NOT_SOURCE), ("justfile", NOT_SOURCE), ("Dockerfile", NOT_SOURCE), ("pytest.ini", NOT_SOURCE),
    ("GNUmakefile", NOT_SOURCE), ("tests", NOT_SOURCE), ("Pipfile", NOT_SOURCE), ("build.gradle", NOT_SOURCE),
    ("pom.xml", NOT_SOURCE), ("tsconfig.json", NOT_SOURCE), ("scripts/run.sh", NOT_SOURCE),
    ("src/data.json", NOT_SOURCE), ("config.yaml", NOT_SOURCE),
    # code under test and its docs: what R0 exists to write
    # head round 1b: a test file in any language, pip's lockfile class, and rc only as a dotfile (not arc.py)
    ("src/test_x.js", DENY), ("test_x.ts", DENY), ("requirements.txt", DENY), ("requirements-dev.txt", DENY),
    ("constraints.txt", DENY), ("src/arc.py", OK), ("src/orc.ts", OK), ("src.py", OK),
    # L3 r1: CamelCase test classes and JS test/build tooling
    ("src/FooTest.java", DENY), ("FooTests.cs", DENY), ("FooSpec.scala", DENY), ("FooTest.php", DENY),
    ("FooTests.swift", DENY), ("TestFoo.kt", DENY), ("FooTestCase.php", DENY), ("jest.setup.js", DENY),
    ("setupTests.js", DENY), ("karma.conf.js", DENY), ("gulpfile.js", DENY), ("Gruntfile.js", DENY),
    ("src/latest.py", OK),
    # a known false positive, fail-safe at registration: the CamelCase rule is casefolded, so Latest.java reads as *test
    ("src/Latest.java", DENY),
    # L3 r2 LOWs
    ("setupTests.tsx", DENY), ("setup.cjs", DENY), ("FooIT.java", DENY), ("FooITCase.kt", DENY),
    ("src/Commit.java", DENY),
    ("src/a.py", OK), ("src/__init__.py", OK), ("src/testing_utils.py", OK), ("src/contest.py", OK),
    ("lib/latest.py", OK), ("src/x.pyi", OK), ("web/app.tsx", OK), ("lib/mod.rs", OK), ("cmd/main.go", OK),
    ("styles/a.scss", OK), ("docs/x.md", OK), ("README.rst", OK), ("notes.txt", OK), ("LIB/B.PY", OK),
    ("assets/logo.svg", OK),
]


@pytest.mark.parametrize("path, verdict", R0_PATHS)
def test_the_r0_write_rule(path, verdict):
    got = r0_write_refusal(path)
    if verdict is None:
        assert got is None and _path_problem(path) is None
    else:
        assert got is not None and verdict in got


@pytest.mark.parametrize("path, verdict", [(p, v) for p, v in R0_PATHS if v is not None])
def test_an_r0_template_writing_a_refused_path_is_refused_whole(store, log, keys, path, verdict):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    refused(store, log, keys, "templates", 2, [tmpl(write_paths=["src/a.py", path])], verdict)


def test_an_r1_template_is_not_held_to_the_r0_rule(store, log, keys):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    accept(store, log, keys, "templates", 2, [tmpl(tier="R1", k=1, write_paths=["src/a.py", "package.json"])])


def test_a_plain_source_path_passes(store, log, keys):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    accept(store, log, keys, "templates", 2, [tmpl(write_paths=[p for p, v in R0_PATHS if v is None])])
    assert resolver(store)("fix-lint") is not None


def test_the_rule_lists_are_casefolded_and_scripts_are_not_source():
    assert execution_bearing(".git/hooks/pre-commit") == ".git/**"     # the contract path rule refuses .git first
    assert execution_bearing("src/a.py") is None
    assert all(p == p.casefold() for p in EXECUTION_BEARING_FILES + EXECUTION_BEARING_DIRS + R0_SOURCE_EXTENSIONS)
    assert ".sh" not in R0_SOURCE_EXTENSIONS and ".py" in R0_SOURCE_EXTENSIONS


def test_r2_is_never_a_template(store, log, keys):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    refused(store, log, keys, "templates", 2, [tmpl(tier="R2")], "R2 is never a template")


@pytest.mark.parametrize("over, match", [
    ({"k": 1}, "k >= 2"),
    ({"models": ["glm-5", "glm-5"]}, "distinct"),
    ({"write_paths": ["src/*.py"]}, "glob"),
    ({"write_paths": ["src/a.py", "SRC/a.py"]}, "differ only in case"),
    ({"tool_versions": [["levain", "1.0.0"], ["codex", "0.50.0"]]}, "sorted"),
])
def test_template_problems_refuse_the_item(store, log, keys, over, match):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    refused(store, log, keys, "templates", 2, [tmpl(**over)], match)


def test_extra_keys_and_twice_listed_templates_refuse(store, log, keys):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    refused(store, log, keys, "templates", 2, [{**tmpl(), "note": "x"}], "a template is exactly")
    refused(store, log, keys, "templates", 2, [tmpl(), tmpl()], "listed twice")
    refused(store, log, keys, "templates", 2, {"fix-lint": tmpl()}, "a list")


# --- the verifier cross-check --------------------------------------------------------------------------------------

def test_an_unknown_verifier_is_refused(store, log, keys):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    refused(store, log, keys, "templates", 2, [tmpl(verifier_id="pytest-none")], "not registered")


def test_templates_before_their_verifiers_are_refused(store, log, keys):
    refused(store, log, keys, "templates", 1, [tmpl()], "not registered")
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    accept(store, log, keys, "templates", 2, [tmpl()])


@pytest.mark.parametrize("over, match", [
    ({"verifier_hash": IP}, "verifier_hash is not"),
    ({"verifier_kind": "in-process"}, "registered black-box"),
])
def test_a_template_disagreeing_with_its_verifier_is_refused(store, log, keys, over, match):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    refused(store, log, keys, "templates", 2, [tmpl(**over)], match)


def test_a_replaced_verifier_stops_its_stale_templates_resolving(store, log, keys):
    base_config(store, log, keys)
    new = "sha256:" + "e" * 64
    accept(store, log, keys, "verifiers", 4, {**VERIFIERS, "pytest-bb": {"artifact_hash": new, "kind": "black-box"}})
    with pytest.raises(DispatchError, match="verifier_hash is not"):
        resolver(store)("fix-lint")
    accept(store, log, keys, "templates", 5, [tmpl(verifier_hash=new)])
    assert resolver(store)("fix-lint").verifier_hash == new


@pytest.mark.parametrize("body, match", [
    ([], "a mapping"),
    ({"pytest-bb": {"artifact_hash": BB}}, "exactly"),
    ({"pytest-bb": {"artifact_hash": "sha256:xyz", "kind": "black-box"}}, "sha256"),
    ({"pytest-bb": {"artifact_hash": BB, "kind": "grey-box"}}, "kind"),
    ({"-bad": {"artifact_hash": BB, "kind": "black-box"}}, "verifier id"),
])
def test_verifier_body_refusals(store, log, keys, body, match):
    refused(store, log, keys, "verifiers", 1, body, match)


# --- provider policy and route map ---------------------------------------------------------------------------------

def test_the_provider_policy_is_validated_by_constructing_it(store, log, keys):
    refused(store, log, keys, "provider_policy", 1, {"never": [".env"], "providers": {}}, "no '/'")
    refused(store, log, keys, "provider_policy", 1, {"never": []}, "provider policy")
    accept(store, log, keys, "provider_policy", 1, POLICY.to_mapping())


ROUTE = {"route": "openrouter-glm", "provider": "openrouter", "upstream": "https://openrouter.ai/api",
         "credential": "openrouter", "models": [{"requested": "z-ai/glm-5.2", "backend": "zhipu/glm-5.2"}],
         "body_policy": {"provider_only": ["z-ai"], "allow_fallbacks": "no", "zdr": "yes", "data_collection": "deny"},
         "retention_claim": "the provider states zero retention"}


def test_a_routemap_in_the_schema_shape_is_accepted(store, log, keys):
    accept(store, log, keys, "routemap", 1, {"routes": [ROUTE]})
    assert store.active()["routemap"].body == {"routes": [ROUTE]}


@pytest.mark.parametrize("body, match", [
    ({"routes": [ROUTE], "extra": 1}, "exactly"),
    ({"routes": [{**ROUTE, "secret": "sk-1"}]}, "a route is exactly"),
    ({"routes": [ROUTE, ROUTE]}, "listed twice"),
    ({"routes": [{**ROUTE, "models": []}]}, "non-empty"),
    ({"routes": [{**ROUTE, "models": [{"requested": "m"}]}]}, "requested, backend"),
    ({"routes": [{**ROUTE, "upstream": 7}]}, "upstream"),
    ({"routes": [{**ROUTE, "body_policy": {"zdr": 1}}]}, "text or a list of text"),
    ({"routes": [{**ROUTE, "credential": "a\nb"}]}, "credential"),
])
def test_routemap_refusals(store, log, keys, body, match):
    refused(store, log, keys, "routemap", 1, body, match)


# --- adoption rules ------------------------------------------------------------------------------------------------

def rule(**over):
    r = {"template_id": "fix-lint", "template_hash": tmpl()["content_hash"], "verifier_kind": "black-box",
         "criteria": ["the black-box suite passes", "contrast-clean across both models"]}
    r.update(over)
    return r


def test_an_adoption_rule_for_a_black_box_r0_template_is_accepted(store, log, keys):
    base_config(store, log, keys)
    assert active_adoption_rules(store) == ()
    accept(store, log, keys, "adoption_rules", 4, [rule()])
    assert active_adoption_rules(store) == (AdoptionRule(
        template_id="fix-lint", template_hash=tmpl()["content_hash"], verifier_kind="black-box",
        criteria=("the black-box suite passes", "contrast-clean across both models")),)


def test_the_raw_adoption_rules_are_never_read_out(store, log, keys):
    base_config(store, log, keys)
    accept(store, log, keys, "adoption_rules", 4, [rule()])
    assert "adoption_rules" not in store.active()
    assert set(store.active()) == {"verifiers", "templates", "provider_policy"}


def test_a_rule_whose_template_was_replaced_stops_reading(store, log, keys):
    # the L1 scenario: the rule was judged against the template active when it was accepted; a later templates item
    # with other content leaves it bound to a hash nothing active has
    base_config(store, log, keys)
    accept(store, log, keys, "adoption_rules", 4, [rule()])
    accept(store, log, keys, "templates", 5, [tmpl(executor="claude")])
    with pytest.raises(DispatchError, match="another template content hash"):
        active_adoption_rules(store)


def test_a_rule_whose_template_left_the_registry_stops_reading(store, log, keys):
    base_config(store, log, keys)
    accept(store, log, keys, "adoption_rules", 4, [rule()])
    accept(store, log, keys, "templates", 5, [tmpl(id="fix-docs")])
    with pytest.raises(DispatchError, match="not an active template"):
        active_adoption_rules(store)


def test_a_rule_whose_verifier_was_replaced_stops_reading(store, log, keys):
    # the L2 scenario: same template content, but its verifier is no longer the registered one
    base_config(store, log, keys)
    accept(store, log, keys, "adoption_rules", 4, [rule()])
    accept(store, log, keys, "verifiers", 5, {**VERIFIERS, "pytest-bb": {"artifact_hash": "sha256:" + "e" * 64,
                                                                         "kind": "black-box"}})
    with pytest.raises(DispatchError, match="verifier_hash is not"):
        active_adoption_rules(store)


def test_a_rule_is_refused_at_accept_when_its_verifier_disagrees(store, log, keys):
    base_config(store, log, keys)
    accept(store, log, keys, "verifiers", 4, {**VERIFIERS, "pytest-bb": {"artifact_hash": IP, "kind": "black-box"}})
    refused(store, log, keys, "adoption_rules", 5, [rule()], "verifier_hash is not")


# --- per-id template loading ---------------------------------------------------------------------------------------

def test_a_template_the_code_now_refuses_fails_only_its_own_id(store, log, keys, monkeypatch):
    # a stored template that the current rules refuse (here: the deny list gains a file it writes) raises when THAT
    # id is resolved, naming it; the other templates still resolve
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    accept(store, log, keys, "templates", 2, [tmpl(), tmpl(id="fix-docs", write_paths=["src/a.py", "docs/x.md"])])
    monkeypatch.setattr(registry, "EXECUTION_BEARING_FILES", registry.EXECUTION_BEARING_FILES + ("x.md",))
    resolve = resolver(store)
    assert resolve("fix-lint") is not None and resolve("nope") is None
    with pytest.raises(DispatchError, match="fix-docs"):
        resolve("fix-docs")


def test_an_adoption_rule_for_an_in_process_verifier_is_refused(store, log, keys):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    t = tmpl(verifier_id="pytest-ip", verifier_hash=IP, verifier_kind="in-process")
    accept(store, log, keys, "templates", 2, [t])
    refused(store, log, keys, "adoption_rules", 3, [rule(template_hash=t["content_hash"])], "verifier is in-process")
    refused(store, log, keys, "adoption_rules", 3, [rule(template_hash=t["content_hash"], verifier_kind="in-process")],
            "only a black-box")


@pytest.mark.parametrize("over, match", [
    ({"template_id": "fix-docs"}, "not an active template"),
    ({"template_hash": "sha256:" + "0" * 64}, "another template content hash"),
    ({"criteria": []}, "own criteria"),
    ({"criteria": ["ok", ""]}, "criterion"),
])
def test_adoption_rule_refusals(store, log, keys, over, match):
    base_config(store, log, keys)
    refused(store, log, keys, "adoption_rules", 4, [rule(**over)], match)


def test_an_adoption_rule_for_an_r1_template_is_refused(store, log, keys):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    t = tmpl(tier="R1", k=1)
    accept(store, log, keys, "templates", 2, [t])
    refused(store, log, keys, "adoption_rules", 3, [rule(template_hash=t["content_hash"])], "only an R0 template")


def test_an_adoption_rule_missing_its_template_hash_is_refused(store, log, keys):
    base_config(store, log, keys)
    r = rule()
    del r["template_hash"]
    refused(store, log, keys, "adoption_rules", 4, [r], "exactly")
