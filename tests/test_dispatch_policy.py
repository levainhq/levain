"""The provider policy (DESIGN §5): a NEVER list first, a per-provider read allowlist that fails closed, its own glob
matcher (``*`` never crosses ``/``, ``**`` may), and a content hash a contract binds."""
from __future__ import annotations

import pytest

from levain.dispatch.policy import PolicyError, ProviderPolicy, match

POLICY = {"never": ["work/**", ".env*", "**/*.pem", "**/*.key"],
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
    ("src/**", "src", True),                        # ** matches zero components
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
    ({"never": ["~/ClientData/**"], "providers": {}}, "home path"),
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
