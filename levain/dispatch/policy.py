"""levain.dispatch.policy — the provider policy: which repo files may ship to which model provider.

What ships to a provider is everything the executor can READ, so the boundary acts on a contract's resolved read set
(and, failing closed, on its write set too). The shape is the one flow's ``GEMINI_ELIGIBLE_REPOS`` allowlist proved:
one policy per install, a NEVER list checked before anything else, a per-provider read allowlist that fails closed,
and a contract touching any refused path refused WHOLE, never trimmed to what would pass.

Patterns are globs over the repo-relative POSIX path, matched by this module's own small matcher (never ``fnmatch``,
whose ``*`` crosses ``/``): ``*`` matches any run of characters inside one path component, ``?`` one character inside
a component, and ``**`` as a whole component matches zero or more components. ``*`` and ``?`` match a leading dot, so
``**/*.pem`` covers ``.hidden.pem``. Character classes (``[``) are refused at load rather than half-supported. A
pattern beginning ``~`` or ``/`` is refused at load: every set the policy judges is repo-relative, so a home or
absolute pattern (the design's ``~/ClientData/**``) could never match one; such data is outside any repo by
construction, and stays out because the executor's worktree is built from the read set alone.

The policy is dispatch CONTROL STATE (design §3.2): the intent is that it lives in a signed, generation-checked store
the control principal owns, which agent sessions can read and never write. That store is a later slice (F3); here the
policy is a value passed in, and :meth:`ProviderPolicy.content_hash` is what a contract binds so a policy changed
after signing fails revalidation. Stdlib only.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from levain.contracts.canon import canonical_bytes

__all__ = ["FORMAT", "PolicyError", "ProviderPolicy", "match"]

FORMAT = "levain-dispatch-policy/1"
_PROVIDER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_PATTERN_MAX = 512
_PATTERNS_MAX = 256
_PROVIDERS_MAX = 64


class PolicyError(ValueError):
    """A provider policy that cannot be loaded: the message names the field."""


def _pattern_problem(pattern: Any) -> str | None:
    if not isinstance(pattern, str) or not pattern:
        return "a pattern is a non-empty string"
    if len(pattern) > _PATTERN_MAX:
        return f"a pattern is at most {_PATTERN_MAX} characters"
    if pattern.startswith("~"):
        return (f"pattern {pattern!r} is a home path; the policy judges repo-relative paths, so it could never "
                "match (data outside the repo never enters the executor's worktree)")
    if pattern.startswith("/"):
        return f"pattern {pattern!r} is absolute; the policy judges repo-relative paths"
    if any(c in pattern for c in "\x00[]\\"):
        return f"pattern {pattern!r} holds NUL, a character class or a backslash, which this matcher does not take"
    parts = pattern.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return f"pattern {pattern!r} has an empty, '.' or '..' component"
    if any("**" in p and p != "**" for p in parts):
        return f"pattern {pattern!r} uses ** inside a component; ** stands alone as a whole component"
    return None


def _component_regex(component: str) -> re.Pattern[str]:
    out = []
    for c in component:
        out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
    return re.compile("".join(out), re.DOTALL)


def match(pattern: str, path: str) -> bool:
    """True when the repo-relative POSIX ``path`` matches ``pattern`` (see the module docstring for the syntax).
    Iterative over (pattern component, path component) positions, so no pattern costs more than their product."""
    pats = pattern.split("/")
    parts = path.split("/")
    regexes = [None if p == "**" else _component_regex(p) for p in pats]
    # reach[j] is True when pattern components [0, i) can consume path components [0, j)
    reach = [True] + [False] * len(parts)
    for i, rx in enumerate(regexes):
        nxt = [False] * (len(parts) + 1)
        if rx is None:                       # ** : zero or more components
            seen = False
            for j in range(len(parts) + 1):
                seen = seen or reach[j]
                nxt[j] = seen
        else:
            for j in range(len(parts)):
                if reach[j] and rx.fullmatch(parts[j]) is not None:
                    nxt[j + 1] = True
        reach = nxt
        if not any(reach):
            return False
    return reach[len(parts)]


def _patterns(raw: Any, what: str) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise PolicyError(f"{what} must be a list of patterns")
    if len(raw) > _PATTERNS_MAX:
        raise PolicyError(f"{what} holds more than {_PATTERNS_MAX} patterns")
    for p in raw:
        problem = _pattern_problem(p)
        if problem is not None:
            raise PolicyError(f"{what}: {problem}")
    if len(set(raw)) != len(raw):
        raise PolicyError(f"{what} lists a pattern twice")
    return tuple(sorted(raw))


def _path_problem(path: Any) -> str | None:
    if not isinstance(path, str) or not path or "\x00" in path:
        return "a path is a non-empty string with no NUL"
    if path.startswith("/") or any(p in ("", ".", "..") for p in path.split("/")):
        return f"path {path[:80]!r} is not a normalised repo-relative path"
    return None


@dataclass(frozen=True)
class ProviderPolicy:
    """A loaded provider policy: ``never`` patterns, and per provider its ``read`` allowlist. Build it with
    :meth:`from_mapping`, which refuses every malformed input; both pattern lists are held sorted."""
    never: tuple[str, ...]
    providers: Mapping[str, tuple[str, ...]]

    @classmethod
    def from_mapping(cls, d: Any) -> ProviderPolicy:
        """``{"never": [patterns], "providers": {name: {"read": [patterns]}}}``, exactly those keys."""
        if not isinstance(d, Mapping) or set(d) != {"never", "providers"}:
            raise PolicyError('a provider policy is exactly {"never": [...], "providers": {...}}')
        never = _patterns(d["never"], "never")
        raw = d["providers"]
        if not isinstance(raw, Mapping) or len(raw) > _PROVIDERS_MAX:
            raise PolicyError(f"providers must be a mapping of at most {_PROVIDERS_MAX} providers")
        providers: dict[str, tuple[str, ...]] = {}
        for name, entry in raw.items():
            if not isinstance(name, str) or _PROVIDER.fullmatch(name) is None:
                raise PolicyError(f"provider name {name!r} must match {_PROVIDER.pattern}")
            if not isinstance(entry, Mapping) or set(entry) != {"read"}:
                raise PolicyError(f'provider {name}: an entry is exactly {{"read": [...]}}')
            providers[name] = _patterns(entry["read"], f"provider {name} read")
        return cls(never=never, providers=dict(sorted(providers.items())))

    def to_mapping(self) -> dict[str, Any]:
        return {"never": list(self.never),
                "providers": {n: {"read": list(p)} for n, p in sorted(self.providers.items())}}

    def canonical(self) -> bytes:
        return canonical_bytes({"format": FORMAT, **self.to_mapping()})

    def content_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical()).hexdigest()

    def refusal(self, provider: Any, read: Iterable[str], write: Iterable[str]) -> str | None:
        """Why a contract reading ``read`` and writing ``write`` may not go to ``provider``; ``None`` when it may.
        NEVER is checked first, against both sets; then the provider must be known and EVERY path, read or write,
        must match its read allowlist. A write path is held to the allowlist as well: an existing file the executor
        rewrites is in its worktree, so it ships. Anything malformed refuses."""
        try:
            read_l, write_l = list(read), list(write)
        except TypeError:
            return "the read and write sets must be lists of paths"
        for path in read_l + write_l:
            problem = _path_problem(path)
            if problem is not None:
                return problem
        for path in read_l + write_l:
            for pattern in self.never:
                if match(pattern, path):
                    return f"{path} is on the NEVER list ({pattern}); the contract is refused whole"
        if not isinstance(provider, str) or provider not in self.providers:
            return f"provider {provider!r} is not in the provider policy; an unknown provider gets nothing"
        allowed = self.providers[provider]
        for path in read_l + write_l:
            if not any(match(pattern, path) for pattern in allowed):
                return (f"{path} is outside what provider {provider} may read; the contract is refused whole, "
                        "never trimmed")
        return None
