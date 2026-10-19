"""The provider policy (DESIGN §5): a NEVER list first, a per-provider read allowlist that fails closed, its own glob
matcher (``*`` never crosses ``/``, ``**`` may), and a content hash a contract binds."""
from __future__ import annotations

import re

import pytest

from levain.dispatch.policy import PolicyError, ProviderPolicy, match

POLICY = {"never": ["work/**", "**/.env*", "**/*.pem", "**/*.key"],
          "providers": {"anthropic": {"read": ["**"]},
                        "openrouter": {"read": ["levain/**", "games/**"]},
                        "ollama-cloud": {"read": ["levain/**"]}}}


@pytest.fixture
def policy():
    return ProviderPolicy.from_mapping(POLICY)


@pytest.mark.parametrize("pattern, path, hit", [
    ("*.py", "a.py", True),
    ("*.py", "src/a.py", False),                    # * never crosses /
    ("src/*.py", "src/a.py", True),
    ("src/*.py", "src/sub/a.py", False),
    ("src/**", "src/sub/deep/a.py", True),
    ("src/**", "src", False),                       # a trailing ** needs one more component
    ("src/**", "src/a", True),
    ("**", "a", True),
    ("src/**", "srcx/a.py", False),
    ("**/*.pem", "a.pem", True),
    ("**/*.pem", "x/y/z.pem", True),
    ("**/*.pem", "x/y/z.pem.txt", False),
    ("**/*.pem", "x/.hidden.pem", True),            # * matches a leading dot
    (".env*", ".env", True),
    (".env*", ".env.local", True),
    (".env*", "sub/.env", False),                   # anchored at the repo root
    ("a/**/b", "a/b", True),
    ("a/**/b", "a/x/y/b", True),
    ("a/**/b", "a/x/y/c", False),
    ("a?c", "abc", True),
    ("a?c", "a/c", False),
    ("**", "anything/at/all", True),
    ("a.b", "aXb", False),                          # . is literal
    ("**/**/**/x", "x", True),
])
def test_match(pattern, path, hit):
    assert match(pattern, path) is hit


def test_match_is_not_exponential():
    pattern = "/".join(["**"] * 30 + ["*a*"] * 5 + ["z"])
    assert match(pattern, "/".join(["a"] * 60)) is False


def test_never_is_checked_first_against_read_and_write(policy):
    assert "NEVER" in policy.refusal("anthropic", ["work/client.txt"], [])
    assert "NEVER" in policy.refusal("anthropic", ["levain/a.py"], ["deploy/server.pem"])
    assert "NEVER" in policy.refusal("anthropic", [".env"], [])
    # NEVER comes before the unknown provider: the reason names the path
    assert "NEVER" in policy.refusal("nobody", ["work/x"], [])


def test_unknown_provider_refuses(policy):
    assert "not in the provider policy" in policy.refusal("nobody", ["levain/a.py"], [])
    assert "not in the provider policy" in policy.refusal(None, ["levain/a.py"], [])


def test_allowlist_fails_closed_on_read_and_write(policy):
    assert policy.refusal("openrouter", ["levain/a.py", "games/g.py"], ["levain/a.py"]) is None
    assert "outside what provider openrouter may read" in policy.refusal("openrouter", ["docs/x.md"], [])
    assert "refused whole" in policy.refusal("ollama-cloud", ["levain/a.py", "games/g.py"], [])
    assert "outside" in policy.refusal("ollama-cloud", ["levain/a.py"], ["games/new.py"])
    assert policy.refusal("anthropic", ["docs/x.md"], ["games/g.py"]) is None


@pytest.mark.parametrize("read", [["/etc/passwd"], ["../x"], ["a//b"], [""], [None], ["a\x00b"], 7])
def test_malformed_sets_refuse(policy, read):
    assert policy.refusal("anthropic", read, []) is not None


@pytest.mark.parametrize("bad, match_", [
    ({"never": [], "providers": {}, "extra": 1}, "exactly"),
    ({"never": []}, "exactly"),
    ([], "exactly"),
    ({"never": "work/**", "providers": {}}, "list of patterns"),
    ({"never": ["~/private/**"], "providers": {}}, "home path"),
    ({"never": ["/etc/**"], "providers": {}}, "absolute"),
    ({"never": ["src/[ab].py"], "providers": {}}, "character class"),
    ({"never": ["src/a**"], "providers": {}}, "stands alone"),
    ({"never": ["src/../x"], "providers": {}}, "component"),
    ({"never": ["a//b"], "providers": {}}, "component"),
    ({"never": [""], "providers": {}}, "non-empty"),
    ({"never": [3], "providers": {}}, "non-empty"),
    ({"never": ["a", "a"], "providers": {}}, "twice"),
    ({"never": [], "providers": {"Bad Name": {"read": []}}}, "provider name"),
    ({"never": [], "providers": {"p": {"read": [], "zdr": True}}}, "exactly"),
    ({"never": [], "providers": {"p": ["**"]}}, "exactly"),
    ({"never": [], "providers": []}, "mapping"),
])
def test_load_refusals(bad, match_):
    with pytest.raises(PolicyError, match=match_):
        ProviderPolicy.from_mapping(bad)


def test_content_hash_is_order_independent_and_binds_every_pattern():
    one = ProviderPolicy.from_mapping(POLICY)
    reordered = {"providers": dict(reversed(list(POLICY["providers"].items()))),
                 "never": list(reversed(POLICY["never"]))}
    assert ProviderPolicy.from_mapping(reordered).content_hash() == one.content_hash()
    assert one.content_hash().startswith("sha256:") and len(one.content_hash()) == 71
    widened = {**POLICY, "providers": {**POLICY["providers"], "ollama-cloud": {"read": ["levain/**", "games/**"]}}}
    assert ProviderPolicy.from_mapping(widened).content_hash() != one.content_hash()
    fewer = {**POLICY, "never": POLICY["never"][1:]}
    assert ProviderPolicy.from_mapping(fewer).content_hash() != one.content_hash()


def test_an_empty_allowlist_admits_nothing():
    p = ProviderPolicy.from_mapping({"never": [], "providers": {"x": {"read": []}}})
    assert "outside" in p.refusal("x", ["a.py"], [])


# --- fix round 1 (L1/L2 slice a) -----------------------------------------------------------------------------------

@pytest.mark.parametrize("pattern", [".env*", "secrets.json", "*.pem"])
def test_a_slashless_never_pattern_is_refused_naming_its_any_depth_form(pattern):
    """L2 M1, the reviewer's probe_a: ``.env*`` anchored at the root let ``app/.env`` through."""
    with pytest.raises(PolicyError, match=re.escape(f"'**/{pattern}'")):
        ProviderPolicy.from_mapping({"never": [pattern], "providers": {"anthropic": {"read": ["**"]}}})


def test_the_any_depth_never_form_refuses_a_nested_file(policy):
    assert "NEVER" in policy.refusal("anthropic", ["app/.env"], [])
    assert "NEVER" in policy.refusal("anthropic", [".env.production"], [])


def test_a_provider_read_pattern_stays_anchored_at_the_root():
    p = ProviderPolicy.from_mapping({"never": [], "providers": {"x": {"read": ["README"]}}})
    assert p.refusal("x", ["README"], []) is None
    assert "outside" in p.refusal("x", ["docs/README"], [])


@pytest.mark.parametrize("path", ["keys/server.PEM", "Work/client.txt", "WORK/x", "app/.ENV", "deploy/id.Key"])
def test_never_matching_ignores_case(policy, path):
    """L2 M2 / L1 L9, the reviewers' probes: a case-insensitive disk holds these as the NEVER files they are."""
    assert "NEVER" in policy.refusal("anthropic", [path], [])
    assert "NEVER" in policy.refusal("anthropic", ["levain/a.py"], [path])


def test_a_read_allowlist_still_matches_case_exactly(policy):
    assert "outside" in policy.refusal("openrouter", ["Levain/a.py"], [])


def test_a_trailing_double_star_does_not_admit_a_file_of_that_name(policy):
    """L2 L5 / L1 L10: ``levain/**`` admitted a top-level FILE named ``levain``."""
    assert match("levain/**", "levain") is False
    assert "outside" in policy.refusal("openrouter", ["levain"], [])
    assert policy.refusal("openrouter", ["levain/x"], []) is None


@pytest.mark.parametrize("bad", ["docs/x.md\nwork/client.txt", "a\tb", "a\rb", "a b", "a b", "a‮b"])
def test_a_control_character_or_odd_space_refuses_a_path_and_a_pattern(policy, bad):
    """L1 MED2 / L2 L1: one path shown as two lines on the signing surface."""
    assert "U+" in policy.refusal("anthropic", [bad], [])
    with pytest.raises(PolicyError, match="U\\+"):
        ProviderPolicy.from_mapping({"never": ["work/" + bad], "providers": {}})
    with pytest.raises(PolicyError, match="U\\+"):
        ProviderPolicy.from_mapping({"never": [], "providers": {"x": {"read": [bad]}}})


def test_a_plain_space_is_a_path_character(policy):
    assert policy.refusal("anthropic", ["docs/a b.md"], []) is None


# --- fix round 2 (L3 r1) -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("where", ["never", "read"])
def test_a_pattern_not_in_nfc_is_refused_at_load(where):
    # codex: a decomposed NEVER rule (e + U+0301) never matches the composed NFC path a contract must use
    decomposed = "**/café.pem"
    raw = {"never": [decomposed] if where == "never" else [],
           "providers": {"x": {"read": [decomposed] if where == "read" else ["**"]}}}
    with pytest.raises(PolicyError, match="NFC"):
        ProviderPolicy.from_mapping(raw)


def test_the_loaded_policy_is_immutable_and_its_hash_is_what_it_judges():
    # codex: a mutable providers dict inside the frozen policy let the approved policy and the hashed one differ
    providers = {"anthropic": ("**",)}
    p = ProviderPolicy(never=("**/*.pem",), providers=providers)
    before = p.content_hash()
    providers["evil"] = ("**",)                       # the caller's own mapping changes after construction
    assert "evil" not in p.providers and p.content_hash() == before
    with pytest.raises(TypeError):
        p.providers["evil"] = ("**",)                 # type: ignore[index]
    loaded = ProviderPolicy.from_mapping(POLICY)
    with pytest.raises(TypeError):
        loaded.providers["anthropic"] = ()            # type: ignore[index]
    assert isinstance(loaded.never, tuple) and loaded.refusal("evil", ["a.py"], []) is not None


@pytest.mark.parametrize("pattern, component", [
    ("*a*a*a*a*a*a*b", "a" * 1000),                   # complement: a many-star pattern, failing at the end
    ("*_*_*_*.py", "_" * 1000),
    ("*" * 200 + "b", "a" * 1000),
])
def test_a_many_star_pattern_against_a_long_failing_component_is_fast(pattern, component):
    import time
    started = time.monotonic()
    assert match(pattern, component) is False
    assert match("src/" + pattern, "src/" + component) is False
    assert time.monotonic() - started < 1.0


def test_the_component_matcher_agrees_with_a_reference_translation():
    import random
    rng = random.Random(1019)

    def reference(pattern, text):
        rx = "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in pattern)
        return re.fullmatch(rx, text, re.DOTALL) is not None

    for _ in range(4000):
        pattern = "".join(rng.choice("ab*?.") for _ in range(rng.randint(1, 6)))
        text = "".join(rng.choice("ab.") for _ in range(rng.randint(1, 8)))
        assert match(pattern, text) is reference(pattern, text), (pattern, text)
