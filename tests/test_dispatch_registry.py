"""The bodies of dispatch configuration items, judged by kind against the active configuration, through a real
control store (real keys, real SQLite): templates (computed content hash, the R0 execution-bearing floor, the
verifier cross-check), verifiers, the provider policy, the route map and adoption rules."""
from __future__ import annotations

import pytest

from levain.dispatch import DispatchError
from levain.dispatch import control as ctl
from levain.dispatch.registry import (
    EXECUTION_BEARING_DIRS, EXECUTION_BEARING_FILES, execution_bearing, resolver, template_content_hash,
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


EXECUTION_BEARING = [
    "tests/test_x.py", "src/test_x.py", "src/x_test.py", "pkg/tests/util.py", "conftest.py", "src/conftest.py",
    "Makefile", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "noxfile.py", ".github/workflows/ci.yml",
    ".gitlab-ci.yml", ".githooks/pre-commit", ".pre-commit-config.yaml", ".claude/settings.json",
    ".codex/config.toml", "AGENTS.md", "CLAUDE.md", "docs/CLAUDE.md", "Cargo.lock", "package-lock.json",
    "poetry.lock", "uv.lock", "SRC/Tests/Helper.PY", "MAKEFILE", "src/Test_X.py",
]


@pytest.mark.parametrize("path", EXECUTION_BEARING)
def test_an_r0_template_writing_an_execution_bearing_file_is_refused_whole(store, log, keys, path):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    t = tmpl(write_paths=["src/a.py", path])
    refused(store, log, keys, "templates", 2, [t], "execution-bearing")


def test_a_plain_source_path_passes(store, log, keys):
    accept(store, log, keys, "verifiers", 1, VERIFIERS)
    accept(store, log, keys, "templates", 2, [tmpl(write_paths=["src/a.py", "src/testing_utils.py", "docs/x.md",
                                                               "src/contest.py", "lib/latest.py"])])
    assert resolver(store)("fix-lint") is not None


def test_the_floor_names_every_class_the_design_lists():
    assert execution_bearing(".git/hooks/pre-commit") == ".git/**"     # the contract path rule refuses .git first
    assert execution_bearing("src/a.py") is None
    assert {"tests", ".github", ".git", ".githooks", ".claude", ".codex"} == set(EXECUTION_BEARING_DIRS)
    assert all(p == p.casefold() for p in EXECUTION_BEARING_FILES + EXECUTION_BEARING_DIRS)


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
    accept(store, log, keys, "verifiers", 2, VERIFIERS)
    accept(store, log, keys, "templates", 3, [tmpl()])


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
    accept(store, log, keys, "adoption_rules", 4, [rule()])
    assert store.active()["adoption_rules"].body == [rule()]


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
