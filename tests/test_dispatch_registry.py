"""The bodies of dispatch configuration items, judged by kind against the active configuration, through a real
control store (real keys, real SQLite): templates (computed content hash, the R0 write rule, the verifier
cross-check), verifiers, the provider policy, the route map and adoption rules (re-judged on every read)."""
from __future__ import annotations

import hashlib
import inspect

import pytest

from levain.contracts.canon import canonical_bytes
from levain.dispatch import DispatchError
from levain.dispatch import control as ctl
from levain.dispatch import registry
from levain.dispatch.contract import _path_problem
from levain.dispatch.registry import (
    EXECUTION_BEARING_DIRS, EXECUTION_BEARING_FILES, R0_SOURCE_EXTENSIONS, VERIFIER_FORMAT, AdoptionRule,
    active_adoption_rules, execution_bearing, r0_write_refusal, resolver, template_content_hash, verifier_ticket,
)
from levain.dispatch.contract import SUBMISSION_KEYS
from levain.dispatch.verifier_report import VERIFIER_KEYS as report_keys
from levain.dispatch.verifier_report import VerifierRun, contract_entry, run_problem
from tests.test_dispatch_contract import POLICY
from tests import test_dispatch_control as tc
from tests.test_dispatch_control import (
    BB, BB_ENTRY, IP, IP_ENTRY, VERIFIERS, accept, base_config, tmpl, vbody, ventry,
)

# the same verifier id running other commands: a new artifact, so a new digest
OTHER_BB = ventry("pytest-bb", "black-box", argv=("python3", "-m", "pytest", "-x"))

# the control tests' fixtures: real keys, a real authority log, a pinned store
keys, log, cdir, store = tc.keys, tc.log, tc.cdir, tc.store


def refused(store, log, keys, kind, gen, body, match):
    with pytest.raises(ctl.ConfigRefused, match=match):
        accept(store, log, keys, kind, gen, body)


# --- templates -----------------------------------------------------------------------------------------------------

def test_the_content_hash_is_computed_never_taken(store, log, keys):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
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
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    refused(store, log, keys, "templates", 2, [tmpl(write_paths=["src/a.py", path])], verdict)


def test_an_r1_template_is_not_held_to_the_r0_rule(store, log, keys):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    accept(store, log, keys, "templates", 2, [tmpl(tier="R1", k=1, write_paths=["src/a.py", "package.json"])])


def test_a_plain_source_path_passes(store, log, keys):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    accept(store, log, keys, "templates", 2, [tmpl(write_paths=[p for p, v in R0_PATHS if v is None])])
    assert resolver(store)("fix-lint") is not None


def test_the_rule_lists_are_casefolded_and_scripts_are_not_source():
    assert execution_bearing(".git/hooks/pre-commit") == ".git/**"     # the contract path rule refuses .git first
    assert execution_bearing("src/a.py") is None
    assert all(p == p.casefold() for p in EXECUTION_BEARING_FILES + EXECUTION_BEARING_DIRS + R0_SOURCE_EXTENSIONS)
    assert ".sh" not in R0_SOURCE_EXTENSIONS and ".py" in R0_SOURCE_EXTENSIONS


def test_r2_is_never_a_template(store, log, keys):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    refused(store, log, keys, "templates", 2, [tmpl(tier="R2")], "R2 is never a template")


@pytest.mark.parametrize("over, match", [
    ({"k": 1}, "k >= 2"),
    ({"models": ["glm-5", "glm-5"]}, "distinct"),
    ({"write_paths": ["src/*.py"]}, "glob"),
    ({"write_paths": ["src/a.py", "SRC/a.py"]}, "differ only in case"),
    ({"tool_versions": [["levain", "1.0.0"], ["codex", "0.50.0"]]}, "sorted"),
])
def test_template_problems_refuse_the_item(store, log, keys, over, match):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    refused(store, log, keys, "templates", 2, [tmpl(**over)], match)


def test_extra_keys_and_twice_listed_templates_refuse(store, log, keys):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    refused(store, log, keys, "templates", 2, [{**tmpl(), "note": "x"}], "a template is exactly")
    refused(store, log, keys, "templates", 2, [tmpl(), tmpl()], "listed twice")
    refused(store, log, keys, "templates", 2, {"fix-lint": tmpl()}, "a list")


# --- the verifier cross-check --------------------------------------------------------------------------------------

def test_an_unknown_verifier_is_refused(store, log, keys):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    refused(store, log, keys, "templates", 2, [tmpl(verifier_id="pytest-none")], "not registered")


def test_templates_before_their_verifiers_are_refused(store, log, keys):
    refused(store, log, keys, "templates", 1, [tmpl()], "not registered")
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    accept(store, log, keys, "templates", 2, [tmpl()])


@pytest.mark.parametrize("over, match", [
    ({"verifier_hash": IP}, "verifier_hash is not"),
    ({"verifier_kind": "in-process"}, "declared black-box"),
])
def test_a_template_disagreeing_with_its_verifier_is_refused(store, log, keys, over, match):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    refused(store, log, keys, "templates", 2, [tmpl(**over)], match)


def test_a_replaced_verifier_stops_its_stale_templates_resolving(store, log, keys):
    base_config(store, log, keys)
    new = OTHER_BB["verifier_artifact_digest"]
    accept(store, log, keys, "verifier", 4, vbody(OTHER_BB, IP_ENTRY))
    with pytest.raises(DispatchError, match="verifier_hash is not"):
        resolver(store)("fix-lint")
    accept(store, log, keys, "templates", 5, [tmpl(verifier_hash=new)])
    assert resolver(store)("fix-lint").verifier_hash == new


@pytest.mark.parametrize("body, match", [
    ([], "exactly"),
    ({"pytest-bb": {"artifact_hash": BB, "kind": "black-box"}}, "exactly"),          # Levain 0.11's body shape
    ({"format": "levain-verifier-config/2", "verifiers": []}, "format"),
    ({"format": VERIFIER_FORMAT, "verifiers": {}}, "a list"),
    (vbody({k: v for k, v in BB_ENTRY.items() if k != "steps"}), "exactly"),
    (vbody(BB_ENTRY, BB_ENTRY), "listed twice"),
    (vbody(dict(BB_ENTRY, steps_digest="sha256:xyz")), "sha256"),
    (vbody(ventry("pytest-bb", "grey-box")), "declared_kind"),
    (vbody(ventry("Pytest-BB", "black-box")), "verifier id"),
    (vbody(ventry("p" * 49, "black-box")), "verifier id"),
    (vbody(ventry("pytest-bb", "black-box", driver_template="Output-Compare")), "driver_template"),
    (vbody(ventry("pytest-bb", "black-box", driver_template="no_such_driver")), "not a template this levain ships"),
    (vbody(ventry("pytest-bb", "black-box", driver_template_digest="sha256:" + "0" * 64)),
     "not the digest of the output_compare template"),
    (vbody(dict(BB_ENTRY, steps_digest="sha256:" + "0" * 64)), "steps_digest is not"),
    (vbody(dict(BB_ENTRY, driver_input_digest="sha256:" + "0" * 64)), "driver_input_digest is not"),
    (vbody(dict(BB_ENTRY, verifier_artifact_digest="sha256:" + "0" * 64)), "not the digest of what runs"),
    (vbody(ventry("pytest-bb", "black-box", steps=[])), "runtime helper refuses"),
    (vbody(ventry("pytest-bb", "black-box", steps=[{"argv": ["x"], "wall_s": 901}])), "runtime helper refuses"),
    (vbody(ventry("pytest-bb", "black-box", steps=[{"argv": ["a\x00b"], "wall_s": 1}])), "runtime helper refuses"),
    (vbody(ventry("pytest-bb", "black-box", driver_input=[1])), "driver_input is an object"),
])
def test_verifier_body_refusals(store, log, keys, body, match):
    refused(store, log, keys, "verifier", 1, body, match)


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

RULE_REPO = "/srv/repo"
CRITERIA = [{"onto": ["main"]}, {"max_changed_lines": 40}]


def rule(**over):
    r = {"repo": RULE_REPO, "template_id": "fix-lint", "template_hash": tmpl()["content_hash"],
         "verifier_kind": "black-box", "criteria": CRITERIA}
    r.update(over)
    return r


def test_an_adoption_rule_for_a_black_box_r0_template_is_accepted(store, log, keys):
    base_config(store, log, keys)
    assert active_adoption_rules(store) == ()
    accept(store, log, keys, "adoption_rules", 4, [rule()])
    assert active_adoption_rules(store) == (AdoptionRule(
        repo=RULE_REPO, template_id="fix-lint", template_hash=tmpl()["content_hash"], verifier_kind="black-box",
        criteria=(("onto", ("main",)), ("max_changed_lines", 40))),)


def test_the_raw_adoption_rules_are_never_read_out(store, log, keys):
    base_config(store, log, keys)
    accept(store, log, keys, "adoption_rules", 4, [rule()])
    assert "adoption_rules" not in store.active()
    assert set(store.active()) == {"verifier", "templates", "provider_policy"}


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
    accept(store, log, keys, "verifier", 5, vbody(OTHER_BB, IP_ENTRY))
    with pytest.raises(DispatchError, match="verifier_hash is not"):
        active_adoption_rules(store)


def test_a_rule_is_refused_at_accept_when_its_verifier_disagrees(store, log, keys):
    base_config(store, log, keys)
    accept(store, log, keys, "verifier", 4, vbody(OTHER_BB, IP_ENTRY))
    refused(store, log, keys, "adoption_rules", 5, [rule()], "verifier_hash is not")


# --- per-id template loading ---------------------------------------------------------------------------------------

def test_a_template_the_code_now_refuses_fails_only_its_own_id(store, log, keys, monkeypatch):
    # a stored template that the current rules refuse (here: the deny list gains a file it writes) raises when THAT
    # id is resolved, naming it; the other templates still resolve
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    accept(store, log, keys, "templates", 2, [tmpl(), tmpl(id="fix-docs", write_paths=["src/a.py", "docs/x.md"])])
    monkeypatch.setattr(registry, "EXECUTION_BEARING_FILES", registry.EXECUTION_BEARING_FILES + ("x.md",))
    resolve = resolver(store)
    assert resolve("fix-lint") is not None and resolve("nope") is None
    with pytest.raises(DispatchError, match="fix-docs"):
        resolve("fix-docs")


def test_an_adoption_rule_for_an_in_process_verifier_is_refused(store, log, keys):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    t = tmpl(verifier_id="pytest-ip", verifier_hash=IP, verifier_kind="in-process")
    accept(store, log, keys, "templates", 2, [t])
    refused(store, log, keys, "adoption_rules", 3, [rule(template_hash=t["content_hash"])], "verifier is in-process")
    refused(store, log, keys, "adoption_rules", 3, [rule(template_hash=t["content_hash"], verifier_kind="in-process")],
            "only a black-box")


@pytest.mark.parametrize("over, match", [
    ({"template_id": "fix-docs"}, "not an active template"),
    ({"template_hash": "sha256:" + "0" * 64}, "another template content hash"),
    ({"criteria": []}, "non-empty list"),
    ({"criteria": ["ok", ""]}, "free text is checked by nothing"),
    ({"criteria": ["contrast-clean", "diff under 40 lines"]}, "free text is checked by nothing"),
    ({"criteria": [{"onto": ["main"], "max_files": 2}]}, "exactly one of"),
    ({"criteria": [{"onto": ["main"]}, {"bogus": 1}]}, "'bogus' is not one of"),
    ({"criteria": [{"onto": ["main"]}, {"max_files": 2}, {"max_files": 3}]}, "named twice"),
    ({"criteria": [{"max_files": 2}]}, "name onto"),
    ({"criteria": [{"onto": ["main"]}, {"no_new_files": True}]}, "at least one bound"),
    ({"criteria": [{"onto": ["main"]}, {"max_files": 0}]}, "integer from 1"),
    ({"criteria": [{"onto": ["main"]}, {"max_files": True}]}, "integer from 1"),
    ({"criteria": [{"onto": ["main"]}, {"max_bytes": (1 << 30) + 1}]}, "integer from 1"),
    ({"criteria": [{"onto": ["main"]}, {"max_age_hours": 2.5}, {"max_files": 1}]}, "float"),
    ({"criteria": [{"onto": ["main"]}, {"paths": []}]}, "non-empty list"),
    ({"criteria": [{"onto": ["main"]}, {"paths": ["src/other.py"]}]}, "does not write"),
    ({"criteria": [{"onto": ["main"]}, {"paths": ["lib/"]}]}, "above none"),
    ({"criteria": [{"onto": ["main"]}, {"no_new_files": False}, {"max_files": 1}]}, "no_new_files is true"),
    ({"criteria": [{"onto": ["levain/x"]}, {"max_files": 1}]}, "not a bare branch"),
    ({"criteria": [{"onto": ["refs/heads/main"]}, {"max_files": 1}]}, "not a bare branch"),
    ({"criteria": [{"onto": []}, {"max_files": 1}]}, "non-empty list of branch"),
    ({"repo": "relative/repo"}, "absolute path in its normal form"),
    ({"repo": "/srv/../repo"}, "absolute path in its normal form"),
    ({"repo": ""}, "repo is an absolute path"),
])
def test_adoption_rule_refusals(store, log, keys, over, match):
    base_config(store, log, keys)
    refused(store, log, keys, "adoption_rules", 4, [rule(**over)], match)


def test_an_adoption_rule_for_an_r1_template_is_refused(store, log, keys):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    t = tmpl(tier="R1", k=1)
    accept(store, log, keys, "templates", 2, [t])
    refused(store, log, keys, "adoption_rules", 3, [rule(template_hash=t["content_hash"])], "only an R0 template")


def test_an_adoption_rule_missing_its_template_hash_is_refused(store, log, keys):
    base_config(store, log, keys)
    r = rule()
    del r["template_hash"]
    refused(store, log, keys, "adoption_rules", 4, [r], "exactly")


def test_every_typed_criterion_is_accepted_and_read_back_in_order(store, log, keys):
    base_config(store, log, keys)
    crit = [{"onto": ["main", "dev"]}, {"max_changed_lines": 40}, {"max_bytes": 4096}, {"max_files": 3},
            {"paths": ["src/a.py", "src/sub/"]}, {"no_new_files": True}, {"max_age_hours": 12}]
    accept(store, log, keys, "adoption_rules", 4, [rule(criteria=crit)])
    (got,) = active_adoption_rules(store)
    assert got.criteria == (("onto", ("main", "dev")), ("max_changed_lines", 40), ("max_bytes", 4096),
                            ("max_files", 3), ("paths", ("src/a.py", "src/sub/")), ("no_new_files", True),
                            ("max_age_hours", 12))


def test_a_stored_text_criteria_rule_raises_on_read(store, log, keys, monkeypatch):
    # a rule accepted under an older reader (text criteria) lands nothing: the re-judging read raises
    base_config(store, log, keys)
    accept(store, log, keys, "adoption_rules", 4, [rule()])
    monkeypatch.setattr(registry, "CRITERION_KEYS", ("onto", "max_files"))
    with pytest.raises(DispatchError, match="no longer hold"):
        active_adoption_rules(store)


# --- a rule Levain 0.11.10 accepted (F11 §3 (B), dated correction 2026-10-20) --------------------------------------

def old_rule():
    # 0.11.10's shape: no repo, criteria as free text
    return {"template_id": "fix-lint", "template_hash": tmpl()["content_hash"], "verifier_kind": "black-box",
            "criteria": ["tests pass", "small diff"]}


WAY_OUT = r"free text.*adopts nothing.*levain dispatch config sign adoption_rules"


def accepted_by_0_11_10(store, log, keys, monkeypatch, gen):
    # 0.11.10 judged a criterion as any text; this code would refuse the item, so it is accepted under that judge
    with monkeypatch.context() as m:
        m.setattr(registry, "_adoption_rules", lambda body, active: None)
        accept(store, log, keys, "adoption_rules", gen, [old_rule()])


def test_a_0_11_10_rule_is_refused_at_accept_with_the_way_out(store, log, keys):
    base_config(store, log, keys)
    refused(store, log, keys, "adoption_rules", 4, [old_rule()], WAY_OUT)
    refused(store, log, keys, "adoption_rules", 4, [rule(criteria=["tests pass"])], WAY_OUT)


def test_a_0_11_10_rule_reads_as_a_refusal_naming_it_and_the_way_out(store, log, keys, monkeypatch):
    base_config(store, log, keys)
    accepted_by_0_11_10(store, log, keys, monkeypatch, 4)
    with pytest.raises(DispatchError, match=r"'fix-lint' has criteria as free text.*" + WAY_OUT):
        active_adoption_rules(store)
    # the stated way out works: a typed rule signed as the next item replaces it
    accept(store, log, keys, "adoption_rules", 5, [rule()])
    assert [r.criteria for r in active_adoption_rules(store)] == [(("onto", ("main",)), ("max_changed_lines", 40))]


def test_doctor_says_a_0_11_10_rule_adopts_nothing_and_how_to_replace_it(store, log, keys, monkeypatch):
    from levain import doctor
    from levain import install
    from levain.dispatch import cli as dcli

    base_config(store, log, keys)
    monkeypatch.setattr(install, "_consent_queue_dir", lambda: store.directory)
    monkeypatch.setattr(dcli, "_authority_source", lambda directory, data_root: store._authority)
    assert [(r.ok, r.warn) for r in doctor._check_dispatch_adoption_rules()] == [(True, False)]
    accepted_by_0_11_10(store, log, keys, monkeypatch, 4)
    (row,) = doctor._check_dispatch_adoption_rules()
    assert row.ok and row.warn and row.name == "dispatch adoption rules"
    assert "'fix-lint' has criteria as free text" in row.detail and "levain dispatch config sign adoption_rules" \
        in row.detail and "nothing is adopted by rule" in row.detail


def test_doctor_has_no_adoption_row_without_a_control_store(tmp_path, monkeypatch):
    from levain import doctor
    from levain import install

    monkeypatch.setattr(install, "_consent_queue_dir", lambda: tmp_path)
    assert doctor._check_dispatch_adoption_rules() == []


# --- the verifier item (CONTRACT §3.5 VERIFIER IDENTITY) -----------------------------------------------------------

def test_an_entry_pins_what_runs_and_every_digest_is_checked():
    # the artifact digest is over exactly the five fields that decide what runs
    five = {k: BB_ENTRY[k] for k in ("driver_template", "driver_template_digest", "steps_digest",
                                     "driver_input_digest", "declared_kind")}
    assert BB == "sha256:" + hashlib.sha256(canonical_bytes(five)).hexdigest()
    shipped = registry.driver_template_path("output_compare")
    assert shipped is not None and shipped.parent.name == "verifier_drivers"
    assert registry.driver_template_digest("output_compare") == \
        "sha256:" + hashlib.sha256(shipped.read_bytes()).hexdigest()
    for name in ("__init__", "_x", "../dispatch/registry", "Output_Compare", "no_such_driver"):
        assert registry.driver_template_path(name) is None, name
    assert registry.body_refusal("verifier", VERIFIERS, {}) is None


def test_a_symlinked_template_is_not_a_shipped_one(tmp_path, monkeypatch):
    from levain import verifier_drivers

    (tmp_path / "__init__.py").write_text("")
    (tmp_path / "real.py").write_text("x = 1\n")
    (tmp_path / "linked.py").symlink_to(tmp_path / "real.py")
    monkeypatch.setattr(verifier_drivers, "__file__", str(tmp_path / "__init__.py"))
    assert registry.driver_template_path("real") == tmp_path / "real.py"
    assert registry.driver_template_path("linked") is None


def test_a_steps_list_over_its_byte_cap_is_refused(store, log, keys):
    steps = [{"argv": ["x" * 4000] * 8, "wall_s": 1}] * 2
    refused(store, log, keys, "verifier", 1, vbody(ventry("big", "black-box", steps=steps,
                                                           steps_digest="sha256:" + "0" * 64)),
            "over 32768 bytes")


def test_a_template_verifier_id_takes_the_contract_rule(store, log, keys):
    accept(store, log, keys, "verifier", 1, VERIFIERS)
    refused(store, log, keys, "templates", 2, [tmpl(verifier_id="pytest.bb")], r"must match \[a-z0-9-\]\{1,48\}")


def test_an_in_process_verifier_is_accepted_and_never_adopts_by_rule(store, log, keys):
    accept(store, log, keys, "verifier", 1, vbody(IP_ENTRY))
    accept(store, log, keys, "templates", 2, [tmpl(verifier_id="pytest-ip", verifier_hash=IP,
                                                   verifier_kind="in-process")])
    accept(store, log, keys, "provider_policy", 3, POLICY.to_mapping())
    assert resolver(store)("fix-lint").verifier_kind == "in-process"
    refused(store, log, keys, "adoption_rules", 4, [rule(verifier_kind="in-process")], "only a black-box")


def test_the_ticket_is_derived_from_the_active_entry(store, log, keys):
    base_config(store, log, keys)
    t = verifier_ticket(store.active(), verifier_id="pytest-bb")
    assert t == {"verifier_generation": 1, "verifier_id": "pytest-bb", "verifier_steps": BB_ENTRY["steps"],
                 "driver_input_digest": BB_ENTRY["driver_input_digest"], "entry": BB_ENTRY}
    # a copy: changing what was handed out changes nothing stored
    t["verifier_steps"].append({"argv": ["rm", "-rf", "/"], "wall_s": 1})
    t["entry"]["steps"].clear()
    assert verifier_ticket(store.active(), verifier_id="pytest-bb")["verifier_steps"] == BB_ENTRY["steps"]
    # a newer verifier item is the one a ticket reads: its generation, its steps
    accept(store, log, keys, "verifier", 4, vbody(OTHER_BB, IP_ENTRY))
    t = verifier_ticket(store.active(), verifier_id="pytest-bb")
    assert t["verifier_generation"] == 4 and t["verifier_steps"] == OTHER_BB["steps"]
    with pytest.raises(DispatchError, match="not in the active verifier item"):
        verifier_ticket(store.active(), verifier_id="pytest-none")
    with pytest.raises(DispatchError, match="no verifier item is active"):
        verifier_ticket({}, verifier_id="pytest-bb")


def test_no_job_input_reaches_the_ticket_steps():
    # the ticket takes the active items and an id, nothing else; a planner's submission and a template have no
    # member that could carry steps
    params = inspect.signature(verifier_ticket).parameters
    assert list(params) == ["items", "verifier_id"] and params["verifier_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "verifier_steps" not in SUBMISSION_KEYS and "verifier_steps" not in registry.TEMPLATE_KEYS
    with pytest.raises(DispatchError, match="exactly"):
        registry.template_from_mapping({**tmpl(), "verifier_steps": [{"argv": ["sh"], "wall_s": 1}]})


def test_the_ticket_entry_projects_to_the_minted_run(store, log, keys):
    base_config(store, log, keys)
    t = verifier_ticket(store.active(), verifier_id="pytest-bb")
    entry = contract_entry(t["entry"])
    assert set(entry) == report_keys and set(registry.VERIFIER_KEYS) == report_keys | {"steps", "driver_input"}
    run = VerifierRun(job_id="dj-" + "0" * 32, run=0, session="v-0", ticket="t-0", verifier=entry,
                      steps=t["verifier_steps"], driver_input_digest=t["driver_input_digest"],
                      input_bundle_digest="sha256:" + "e" * 64)
    assert run_problem(run) is None


def plant_0_11_verifiers(store, log, keys, monkeypatch, gen):
    """A Levain 0.11 ``verifiers`` item and a templates item naming it, accepted under 0.11's judge: this code refuses
    both, so the body judge is set aside for the two accepts (the signature, chain and generation still apply)."""
    old_bb = "sha256:" + "b" * 64
    with monkeypatch.context() as m:
        m.setattr(registry, "body_refusal", lambda kind, body, active: None)
        accept(store, log, keys, "verifiers", gen, {"pytest-bb": {"artifact_hash": old_bb, "kind": "black-box"}})
        accept(store, log, keys, "templates", gen + 1, [tmpl(verifier_hash=old_bb)])


def test_a_0_11_verifiers_item_is_kept_unread_with_the_way_out(store, log, keys, monkeypatch):
    plant_0_11_verifiers(store, log, keys, monkeypatch, 1)
    # the store stays readable: the legacy item chains and verifies
    assert set(store.active()) == {"verifiers", "templates"} and store.generation() == 2
    line = registry.legacy_verifiers_problem(store.active())
    assert "generation 1" in line and "no longer read" in line and registry.RESIGN_VERIFIERS in line
    with pytest.raises(DispatchError, match="pytest-bb, which is not registered"):
        resolver(store)("fix-lint")
    with pytest.raises(DispatchError, match=r"no verifier item is active; the verifiers item at generation 1"):
        verifier_ticket(store.active(), verifier_id="pytest-bb")
    # a new one is refused, naming the way out
    refused(store, log, keys, "verifiers", 3, {"x": {"artifact_hash": BB, "kind": "black-box"}},
            "no longer accepted; sign a new verifier item")
    # the way out works: a verifier item, then a templates item pinning its artifact
    accept(store, log, keys, "verifier", 3, VERIFIERS)
    accept(store, log, keys, "templates", 4, [tmpl()])
    assert resolver(store)("fix-lint").verifier_hash == BB
    assert verifier_ticket(store.active(), verifier_id="pytest-bb")["verifier_generation"] == 3


def doctor_rows(store, monkeypatch):
    from levain import doctor
    from levain import install
    from levain.dispatch import cli as dcli

    monkeypatch.setattr(install, "_consent_queue_dir", lambda: store.directory)
    monkeypatch.setattr(dcli, "_authority_source", lambda directory, data_root: store._authority)
    return doctor._check_dispatch_verifiers()


def test_doctor_checks_each_entry_against_the_installed_template(store, log, keys, monkeypatch):
    accept(store, log, keys, "provider_policy", 1, POLICY.to_mapping())
    (row,) = doctor_rows(store, monkeypatch)
    assert (row.ok, row.warn, row.detail) == (True, False, "no verifier item is accepted")
    accept(store, log, keys, "verifier", 2, VERIFIERS)
    (row,) = doctor_rows(store, monkeypatch)
    assert row.ok and not row.warn and row.name == "dispatch verifiers"
    assert "generation 2: 2 verifier(s), every driver template matches" in row.detail
    assert "in-process, forgeable by the code under test: pytest-ip" in row.detail
    # an upgrade that changed the template: red before any run, with the way out
    monkeypatch.setattr(registry, "driver_template_digest", lambda name: "sha256:" + "f" * 64)
    (row,) = doctor_rows(store, monkeypatch)
    assert not row.ok and "pytest-bb (driver output_compare: the installed template differs)" in row.detail
    assert registry.RESIGN_VERIFIERS in row.hint
    monkeypatch.setattr(registry, "driver_template_digest", lambda name: None)
    (row,) = doctor_rows(store, monkeypatch)
    assert not row.ok and "not shipped by this levain" in row.detail


def test_doctor_warns_on_a_0_11_verifiers_item(store, log, keys, monkeypatch):
    plant_0_11_verifiers(store, log, keys, monkeypatch, 1)
    (row,) = doctor_rows(store, monkeypatch)
    assert row.ok and row.warn and "no longer read" in row.detail and "no verifier runs" in row.detail
    accept(store, log, keys, "verifier", 3, VERIFIERS)
    (row,) = doctor_rows(store, monkeypatch)
    assert row.ok and not row.warn and "the 0.11 verifiers item is no longer read" in row.detail


def test_doctor_has_no_verifier_row_without_a_control_store(tmp_path, monkeypatch):
    from levain import doctor
    from levain import install

    monkeypatch.setattr(install, "_consent_queue_dir", lambda: tmp_path)
    assert doctor._check_dispatch_verifiers() == []


# --- W2-b L3 r1 B and head rulings (1), (2) ------------------------------------------------------------------------

def _with_input(entry_steps, driver_input):
    return ventry("pytest-bb", "black-box", steps=entry_steps,
                  steps_digest="sha256:" + hashlib.sha256(canonical_bytes(entry_steps)).hexdigest(),
                  driver_input=driver_input,
                  driver_input_digest="sha256:" + hashlib.sha256(canonical_bytes(driver_input)).hexdigest())


TWO_STEPS = [{"argv": ["python3", "-m", "pytest", "-q"], "wall_s": 300}, {"argv": ["ruff", "check"], "wall_s": 60}]


@pytest.mark.parametrize("driver_input, why", [
    ({"steps": [{"exit": 0}]}, "pins 1 step expectation"),
    ({"steps": [{"exit": 0}] * 3}, "pins 3 step expectation"),
    ({}, "pins no step expectation"),
    ({"steps": {"exit": 0}}, "pins no step expectation"),
])
def test_a_driver_input_whose_step_count_is_not_the_steps_is_refused_at_accept(store, log, keys, driver_input,
                                                                                why):
    refused(store, log, keys, "verifier", 1, vbody(_with_input(TWO_STEPS, driver_input)),
            f"{why}\\(s\\) for 2 step\\(s\\); the output_compare template reads one per step")


def test_a_driver_input_of_one_expectation_per_step_is_accepted(store, log, keys):
    accept(store, log, keys, "verifier", 1, vbody(_with_input(TWO_STEPS, {"steps": [{"exit": 0}, {"exit": 0}]})))


def test_the_ticket_rechecks_the_installed_driver_template(store, log, keys, monkeypatch):
    base_config(store, log, keys)
    assert verifier_ticket(store.active(), verifier_id="pytest-bb")["verifier_generation"] == 1
    monkeypatch.setattr(registry, "driver_template_digest", lambda name: "sha256:" + "f" * 64)
    with pytest.raises(DispatchError, match="output_compare installed is not the one the active verifier item pins"):
        verifier_ticket(store.active(), verifier_id="pytest-bb")
    monkeypatch.setattr(registry, "driver_template_digest", lambda name: None)
    with pytest.raises(DispatchError, match="is not shipped by this levain; sign a new verifier item"):
        verifier_ticket(store.active(), verifier_id="pytest-bb")

    def unreadable(name):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(registry, "driver_template_digest", unreadable)
    with pytest.raises(DispatchError, match=r"output_compare cannot be read \(PermissionError\)"):
        verifier_ticket(store.active(), verifier_id="pytest-bb")


def test_doctor_names_an_unreadable_driver_template_and_does_not_abort(store, log, keys, monkeypatch):
    accept(store, log, keys, "verifier", 1, VERIFIERS)

    def unreadable(name):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(registry, "driver_template_digest", unreadable)
    (row,) = doctor_rows(store, monkeypatch)
    assert not row.ok and row.name == "dispatch verifiers"
    assert "pytest-bb (driver output_compare: the installed template cannot be read, PermissionError)" in row.detail
    assert "Reinstall levain" in row.hint
