"""levain.dispatch.policy — the provider policy: which repo files may ship to which model provider.

What ships to a provider is everything the executor can READ, so the boundary acts on a contract's resolved read set
(and, failing closed, on its write set too). The shape is the one flow's ``GEMINI_ELIGIBLE_REPOS`` allowlist proved:
one policy per install, a NEVER list checked before anything else, a per-provider read allowlist that fails closed,
and a contract touching any refused path refused WHOLE, never trimmed to what would pass.

Patterns are globs over the repo-relative POSIX path, matched by this module's own small matcher (never ``fnmatch``,
whose ``*`` crosses ``/``): ``*`` matches any run of characters inside one path component, ``?`` one character inside
a component, and ``**`` as a whole component matches zero or more components, except as the LAST component, where it
matches one or more (``docs/**`` covers every file under ``docs/``, never a file named ``docs``). ``*`` and ``?`` match
a leading dot, so ``**/*.pem`` covers ``.hidden.pem``. Character classes (``[``) are refused at load rather than
half-supported. A pattern beginning ``~`` or ``/`` is refused at load: every set the policy judges is repo-relative,
so a home or absolute pattern (such as ``~/private/**``) could never match one; such data is outside any repo by
construction, and stays out because the executor's worktree is built from the read set alone.

Every pattern is anchored at the repo root: ``src/*.py`` names ``src/a.py`` and never ``lib/src/a.py``. That is the
meaning a provider's read allowlist keeps, so ``README`` admits the top-level file alone. A NEVER pattern with no ``/``
is refused at load, the message naming its ``**/<pattern>`` form: gitignore's habit reads ``.env*`` as "at any depth",
and a NEVER list written that way would leak ``app/.env``. NEVER matching casefolds both the pattern and the path,
since a case-insensitive disk holds ``server.PEM`` as the same kind of file as ``server.pem``; casefolding NEVER only
adds refusals. The read allowlists match exactly. A pattern or a path holding any control character (newline and tab
included) or any space other than U+0020 is refused. A pattern not in Unicode NFC is refused at load: contract paths
are NFC-only, so a decomposed pattern (``cafe\u0301``) could never match the composed path it looks like, and a NEVER
rule written that way would let the file through.

Matching one component is a linear glob loop (a ``*`` backtracks only to the last ``*`` seen), never a regular
expression, so a many-star pattern against a long component that fails at its end costs at most the product of their
lengths; the components are joined by a dynamic program over (pattern component, path component).

A loaded policy is immutable all the way down: ``never`` is a tuple and ``providers`` a read-only view over the
policy's own copy, so what :meth:`ProviderPolicy.refusal` judges and what :meth:`ProviderPolicy.content_hash` binds
are one value.

The policy is dispatch CONTROL STATE (design §3.2): the intent is that it lives in a signed, generation-checked store
the control principal owns, which agent sessions can read and never write. That store is a later slice (F3); here the
policy is a value passed in, and :meth:`ProviderPolicy.content_hash` is what a contract binds so a policy changed
after signing fails revalidation. Stdlib only.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from levain.contracts.canon import canonical_bytes

__all__ = ["FORMAT", "PolicyError", "ProviderPolicy", "match", "path_text_problem"]

FORMAT = "levain-dispatch-policy/1"
_PROVIDER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_PATTERN_MAX = 512
_PATTERNS_MAX = 256
_PROVIDERS_MAX = 64
# control, format, surrogate, line/paragraph separators, private use, unassigned: can show differently from itself
_UNSHOWABLE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp", "Co", "Cn"})


class PolicyError(ValueError):
    """A provider policy that cannot be loaded: the message names the field."""


def path_text_problem(text: str, what: str = "path") -> str | None:
    """Why ``text`` (a path or a pattern) holds a character that cannot show as itself: any control character,
    newline and tab included, any format, separator, surrogate, private-use or unassigned code point, and any space
    but U+0020. ``None`` when every character shows as what it is. One rule for contract paths and policy patterns."""
    for ch in text:
        if ch == " ":
            continue
        category = unicodedata.category(ch)
        if category in _UNSHOWABLE or category == "Zs":
            return (f"the {what} holds U+{ord(ch):04X} ({unicodedata.name(ch, 'unnamed')}), which can show differently "
                    "from what is signed")
    return None


def _pattern_problem(pattern: Any) -> str | None:
    if not isinstance(pattern, str) or not pattern:
        return "a pattern is a non-empty string"
    if len(pattern) > _PATTERN_MAX:
        return f"a pattern is at most {_PATTERN_MAX} characters"
    problem = path_text_problem(pattern, "pattern")
    if problem is not None:
        return f"{problem} ({pattern[:80]!r})"
    if unicodedata.normalize("NFC", pattern) != pattern:
        return (f"pattern {pattern[:80]!r} is not in Unicode NFC; contract paths are NFC-only, so it could never "
                "match the path it looks like")
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


def _component_match(pattern: str, text: str) -> bool:
    """One path component against one pattern component (``*`` any run, ``?`` one character, all else literal):
    the iterative star-backtrack loop, which returns to the LAST ``*`` only, so it is O(len(pattern) * len(text)) at
    worst and never exponential."""
    p = t = 0
    star = -1          # the pattern index of the last * seen
    resume = 0         # the text index that * is now taken to end at
    while t < len(text):
        if p < len(pattern) and pattern[p] == "*":
            star, resume = p, t
            p += 1
        elif p < len(pattern) and (pattern[p] == "?" or pattern[p] == text[t]):
            p += 1
            t += 1
        elif star >= 0:
            resume += 1
            p, t = star + 1, resume
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def match(pattern: str, path: str) -> bool:
    """True when the repo-relative POSIX ``path`` matches ``pattern`` (see the module docstring for the syntax).
    Iterative over (pattern component, path component) positions, each pair matched by a linear glob loop, so no
    pattern costs more than the product of their lengths."""
    parts = path.split("/")
    regexes: list[str | None] = [None if p == "**" else p for p in pattern.split("/")]
    # reach[j] is True when pattern components [0, i) can consume path components [0, j)
    reach = [True] + [False] * len(parts)
    last = len(regexes) - 1
    for i, rx in enumerate(regexes):
        nxt = [False] * (len(parts) + 1)
        if rx is None and i == last:         # a trailing ** : one or more components
            seen = False
            for j in range(len(parts) + 1):
                nxt[j] = seen
                seen = seen or reach[j]
        elif rx is None:                     # ** : zero or more components
            seen = False
            for j in range(len(parts) + 1):
                seen = seen or reach[j]
                nxt[j] = seen
        else:
            for j in range(len(parts)):
                if reach[j] and _component_match(rx, parts[j]):
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
    problem = path_text_problem(path)
    return None if problem is None else f"{problem} ({path[:80]!r})"


@dataclass(frozen=True)
class ProviderPolicy:
    """A loaded provider policy: ``never`` patterns, and per provider its ``read`` allowlist. Build it with
    :meth:`from_mapping`, which refuses every malformed input; both pattern lists are held sorted."""
    never: tuple[str, ...]
    providers: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        # immutable all the way down: a private copy behind a read-only view, every pattern list a tuple, so the
        # value refusal() judges is the value content_hash() binds, whoever held the mapping it was built from
        object.__setattr__(self, "never", tuple(self.never))
        object.__setattr__(self, "providers", MappingProxyType(
            {n: tuple(p) for n, p in sorted(dict(self.providers).items())}))

    @classmethod
    def from_mapping(cls, d: Any) -> ProviderPolicy:
        """``{"never": [patterns], "providers": {name: {"read": [patterns]}}}``, exactly those keys."""
        if not isinstance(d, Mapping) or set(d) != {"never", "providers"}:
            raise PolicyError('a provider policy is exactly {"never": [...], "providers": {...}}')
        never = _patterns(d["never"], "never")
        for p in never:
            if "/" not in p:
                raise PolicyError(f"never: pattern {p!r} has no '/', so it is anchored at the repo root and would miss "
                                  f"the same name deeper down; write '**/{p}' to refuse it at any depth")
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
        if isinstance(read, (str, bytes)) or isinstance(write, (str, bytes)):
            # a str iterates as its characters, each a valid one-letter path: a str is never a path list
            return "the read and write sets must be lists of paths, not a single string"
        try:
            read_l, write_l = list(read), list(write)
        except TypeError:
            return "the read and write sets must be lists of paths"
        for path in read_l + write_l:
            problem = _path_problem(path)
            if problem is not None:
                return problem
        for path in read_l + write_l:
            folded = path.casefold()
            for pattern in self.never:
                if match(pattern.casefold(), folded):
                    return f"{path} is on the NEVER list ({pattern}); the contract is refused whole"
        if not isinstance(provider, str) or provider not in self.providers:
            return f"provider {provider!r} is not in the provider policy; an unknown provider gets nothing"
        allowed = self.providers[provider]
        for path in read_l + write_l:
            if not any(match(pattern, path) for pattern in allowed):
                return (f"{path} is outside what provider {provider} may read; the contract is refused whole, "
                        "never trimmed")
        return None
