"""The broker's capability check (design 1.2): an AST walk over the source, never a text grep.

The raw TLS writer and the plaintext writer under it are private to `upstream.Upstream`, and every upstream byte for an
exchange passes `Upstream.write`'s synchronous `detached` check. This test is the structural proof of that for every
path a stepper cannot reach (spawned tasks, callbacks): it asserts WHERE the capability may be touched, by position in
the syntax tree, so an alias, a getter, a returned bound method or a computed attribute name fails as the absence of a
permitted shape rather than as a listed case.

Rules (scope per rule): (a) (f) (g) (h) walk `levain/broker/**`; (b) (c) (d) walk all of `levain/`; (e) is the exact
`_keepalive` site set in `upstream.py`. The exact-constant scans: the three private names over all of `levain/`, the
networking API names over `levain/broker/**`. Rule (g)'s allowed call sites are the inventory in
`tests/broker_network_sites.py`, which the session that adds a site extends in the same commit.

Every rule's subject is looked up first and a missing one FAILS, naming it, so renaming `Upstream` or `upstream.py`
fails here instead of turning the rules vacuous. The one exception is the one-time run on a build's parent commit
(before the subjects exist), set by the run-only switch `LEVAIN_AST_BASE_RUN=1`: there a missing subject is reported
"NO SUBJECT", which is not a pass, and the test skips with that report. The committed invocation never sets it.
"""
from __future__ import annotations

import ast
import os
from collections import Counter
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import pytest

from tests.broker_network_sites import NETWORK_SITES

ROOT = Path(__file__).resolve().parents[1]
BASE_RUN_SWITCH = "LEVAIN_AST_BASE_RUN"

UPSTREAM = "broker/upstream.py"
KEYS = "broker/keys.py"
CREDS = "broker/creds.py"

#: rule (g)'s API set; the same names make up the broker's exact-constant scan
API = frozenset({
    "open_connection", "open_unix_connection", "start_tls", "sock_connect", "create_connection",
    "create_unix_connection", "create_datagram_endpoint", "connect", "connect_ex", "wrap_socket", "wrap_bio",
    "sendto", "sendmsg",
})
PRIVATE = ("_writer", "_keepalive", "_graceful_close")
NET_MODULES = ("asyncio", "socket", "ssl", "_socket", "_ssl")
SOCKET_MODULES = ("socket", "_socket")

#: The API-name constant the front passes to `Refused` as the refusal's NAME when every validated address fails to
#: connect: a string in a refusal, not a network call (head ruling P-1, r19 addendum). Allowed at exactly this
#: (file, function, constant), never by a broader pattern.
CONSTANT_ALLOW = {("front.py", "_open_upstream", "connect"): 1}

#: Two names the front held on main before any 3b code, which the by-name rules match although neither is a
#: networking call: `FrontPolicy.connect` (the host -> route map, a dataclass field read as `policy.connect.get(...)`),
#: and the front's injected connector attribute `self._connect` (its default is `open_by_address`, whose own calls
#: are inventory tuples), which shares the upstream seam's name. Allowed at exactly these (file, function, name)
#: sites with an occurrence bound; a rename of either field retires its entry. Seat ruling, routed to the head.
NAME_ALLOW = {
    ("front.py", "connect_target", "connect"): 1,
    ("front.py", "_open_upstream", "_connect"): 1,
}

#: rule (c): `refresh_post`'s callers. The refresh caller is built later (credentials, oauth2-refresh); until then the
#: bound is "at most one", and the session that adds the caller makes it "exactly one" in that commit.
MAX_REFRESH_POST_CALLERS = 1

#: rule (f): each method called directly on `self._writer`, bound to the one `Upstream` method allowed to call it
WRITER_BIND = {"write": "write", "close": "_graceful_close", "wait_closed": "wait_closed", "drain": "drain",
               "is_closing": "abort"}
#: rule (f): the one chain per slot, `self.<slot>.transport.<m>()`, bound to `Upstream.abort`
CHAIN_BIND = {"_writer": {"abort", "is_closing"}, "_keepalive": {"abort"}}
#: rule (f): the constructor parameter each slot is filled from
SLOT_PARAM = {"_writer": "writer", "_keepalive": "_keepalive"}

#: rule (e): the exact set of `_keepalive` occurrence sites in `upstream.py`, as (function or class, kind) -> count
KEEPALIVE_SITES = Counter({
    ("Upstream", "const"): 1,           # the `__slots__` entry
    ("__init__", "param"): 1,
    ("__init__", "attr-store"): 1,      # self._keepalive = _keepalive
    ("__init__", "name-load"): 1,
    ("maintenance", "param"): 1,
    ("maintenance", "attr-store"): 1,   # the maintenance form's one slot fill
    ("maintenance", "name-load"): 1,
    ("open_exchange", "keyword"): 1,
    ("refresh_post", "keyword"): 1,
    ("abort", "attr-load"): 1,          # self._keepalive.transport.abort(), the plain transport's abort
})

#: rule (h): the only (file, function) pairs that may call `_graceful_close`
GRACEFUL_CALLERS = {("broker/keys.py", "release_exchange"), ("broker/upstream.py", "refresh_post")}

_SCOPES = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.NamedExpr, ast.arguments)


class Module:
    """One parsed file: its tree, a parent map, and the node ids that sit in a call-free annotation slot."""

    def __init__(self, rel: str, source: str) -> None:
        self.rel = rel
        self.short = rel.split("/", 1)[1] if rel.startswith("broker/") else rel
        self.tree = ast.parse(source, filename=rel)
        self.parent: Dict[int, ast.AST] = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parent[id(child)] = node
        self.annotation_ids = set()
        for node in ast.walk(self.tree):
            slots: List[Optional[ast.AST]] = []
            if isinstance(node, ast.arg):
                slots.append(node.annotation)
            elif isinstance(node, ast.AnnAssign):
                slots.append(node.annotation)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                slots.append(node.returns)
            for slot in slots:
                if slot is None:
                    continue
                inner = list(ast.walk(slot))
                # an annotation is evaluated at definition time: one that holds a call is walked like any expression
                if not any(isinstance(n, (ast.Call, ast.NamedExpr, ast.Lambda)) for n in inner):
                    self.annotation_ids.update(id(n) for n in inner)

    def nodes(self) -> Iterator[ast.AST]:
        for node in ast.walk(self.tree):
            if id(node) not in self.annotation_ids:
                yield node

    def up(self, node: ast.AST) -> Optional[ast.AST]:
        return self.parent.get(id(node))

    def ancestors(self, node: ast.AST) -> Iterator[ast.AST]:
        p = self.up(node)
        while p is not None:
            yield p
            p = self.up(p)

    def function(self, node: ast.AST) -> Optional[str]:
        """The innermost `def` / `async def` containing the node, by its bare name; under a `lambda`, or at module or
        class level, None."""
        for a in self.ancestors(node):
            if isinstance(a, ast.Lambda):
                return None
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return a.name
            if isinstance(a, ast.ClassDef):
                return None
        return None

    def scope_name(self, node: ast.AST) -> str:
        """The innermost function or class name (rule (e)'s site key), or "<module>"."""
        for a in self.ancestors(node):
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return a.name
        return "<module>"

    def in_class(self, node: ast.AST, name: str) -> bool:
        return any(isinstance(a, ast.ClassDef) and a.name == name for a in self.ancestors(node))

    def scoped_between(self, node: ast.AST) -> bool:
        """A lambda, comprehension, walrus or argument list between the node and its enclosing function."""
        for a in self.ancestors(node):
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return False
            if isinstance(a, _SCOPES):
                return True
        return False

    def is_call_func(self, node: ast.AST) -> bool:
        p = self.up(node)
        return isinstance(p, ast.Call) and p.func is node

    def where(self, node: ast.AST) -> str:
        return f"{self.rel}:{getattr(node, 'lineno', '?')} in {self.function(node) or '<no function>'}"


def _identifier_loads(mod: Module, ident: str) -> Iterator[ast.AST]:
    """Every Load of `ident` as a Name or as an Attribute's attr."""
    for n in mod.nodes():
        if isinstance(n, ast.Name) and n.id == ident and isinstance(n.ctx, ast.Load):
            yield n
        elif isinstance(n, ast.Attribute) and n.attr == ident and isinstance(n.ctx, ast.Load):
            yield n


def _upstream_class(mods: Dict[str, Module]) -> Optional[ast.ClassDef]:
    mod = mods.get(UPSTREAM)
    if mod is None:
        return None
    for n in mod.tree.body:
        if isinstance(n, ast.ClassDef) and n.name == "Upstream":
            return n
    return None


# ---------------------------------------------------------------------------------------------------------------- rules

def rule_a(mods: Dict[str, Module]) -> List[str]:
    """(a) No access to an `Upstream`'s writer or plaintext writer (nor a `.transport` through either) outside the
    class body, in `levain/broker/**`."""
    out = []
    for rel, mod in mods.items():
        if not rel.startswith("broker/"):
            continue
        for n in mod.nodes():
            if isinstance(n, ast.Attribute) and n.attr in ("_writer", "_keepalive"):
                if not (rel == UPSTREAM and mod.in_class(n, "Upstream")):
                    out.append(f"(a) .{n.attr} outside Upstream: {mod.where(n)}")
    return out


def rule_b(mods: Dict[str, Module]) -> List[str]:
    """(b) The TLS writer's `write` is called only inside `Upstream.write`, across all of `levain/`."""
    out = []
    for rel, mod in mods.items():
        for n in mod.nodes():
            if (isinstance(n, ast.Attribute) and n.attr == "write" and isinstance(n.value, ast.Attribute)
                    and n.value.attr == "_writer"):
                ok = (rel == UPSTREAM and mod.in_class(n, "Upstream") and mod.function(n) == "write"
                      and mod.is_call_func(n))
                if not ok:
                    out.append(f"(b) ._writer.write outside Upstream.write: {mod.where(n)}")
    return out


def rule_c(mods: Dict[str, Module]) -> List[str]:
    """(c) `Upstream.maintenance` is referenced only inside `refresh_post`; `refresh_post` has at most
    MAX_REFRESH_POST_CALLERS callers, each a direct call in `creds.py`."""
    out = []
    callers = 0
    for rel, mod in mods.items():
        for n in mod.nodes():
            if isinstance(n, ast.Attribute) and n.attr == "maintenance":
                recv = n.value
                named_upstream = (isinstance(recv, ast.Name) and recv.id == "Upstream") or (
                    isinstance(recv, ast.Attribute) and recv.attr == "Upstream")
                if rel == UPSTREAM and not mod.in_class(n, "Upstream"):
                    named_upstream = True   # inside upstream.py every `.maintenance` outside the class is the form
                if named_upstream and not (rel == UPSTREAM and mod.function(n) == "refresh_post"
                                           and mod.is_call_func(n)):
                    out.append(f"(c) Upstream.maintenance outside refresh_post: {mod.where(n)}")
        for n in _identifier_loads(mod, "refresh_post"):
            callers += 1
            if not (rel == CREDS and mod.is_call_func(n)):
                out.append(f"(c) refresh_post loaded other than as a direct call in creds.py: {mod.where(n)}")
    if callers > MAX_REFRESH_POST_CALLERS:
        out.append(f"(c) refresh_post has {callers} callers, the bound is {MAX_REFRESH_POST_CALLERS}")
    return out


def rule_d(mods: Dict[str, Module]) -> List[str]:
    """(d) `_handshake` is called only by `open_exchange` and `refresh_post`, across all of `levain/`."""
    out = []
    for rel, mod in mods.items():
        for n in _identifier_loads(mod, "_handshake"):
            if not (rel == UPSTREAM and mod.is_call_func(n) and mod.function(n) in ("open_exchange", "refresh_post")):
                out.append(f"(d) _handshake load outside open_exchange/refresh_post: {mod.where(n)}")
    return out


def rule_e(mods: Dict[str, Module]) -> List[str]:
    """(e) The exact `_keepalive` site set in `upstream.py`."""
    mod = mods[UPSTREAM]
    seen: Counter = Counter()
    for n in ast.walk(mod.tree):
        kind = None
        if isinstance(n, ast.Constant) and n.value == "_keepalive":
            kind = "const"
        elif isinstance(n, ast.arg) and n.arg == "_keepalive":
            kind = "param"
        elif isinstance(n, ast.keyword) and n.arg == "_keepalive":
            kind = "keyword"
        elif isinstance(n, ast.Name) and n.id == "_keepalive":
            kind = "name-load" if isinstance(n.ctx, ast.Load) else "name-store"
        elif isinstance(n, ast.Attribute) and n.attr == "_keepalive":
            kind = "attr-load" if isinstance(n.ctx, ast.Load) else "attr-store"
        if kind is not None:
            seen[(mod.scope_name(n), kind)] += 1
    if seen != KEEPALIVE_SITES:
        extra = seen - KEEPALIVE_SITES
        missing = KEEPALIVE_SITES - seen
        return [f"(e) _keepalive sites differ: unexpected {dict(extra)}, missing {dict(missing)}"]
    return []


def rule_f(mods: Dict[str, Module]) -> List[str]:
    """(f) POSITIVE inside `Upstream`: every Load of `self._writer` / `self._keepalive` is a direct method call bound
    to its method, or the one `.transport.<m>()` chain bound to `abort`; each slot is filled once per constructor form
    from its parameter, and the parameters are read nowhere else; no getter machinery."""
    out = []
    mod = mods[UPSTREAM]
    cls = _upstream_class(mods)
    assert cls is not None
    stores: Counter = Counter()
    for n in ast.walk(cls):
        if id(n) in mod.annotation_ids:
            continue
        if isinstance(n, ast.Attribute) and n.attr in SLOT_PARAM:
            fn = mod.function(n)
            if isinstance(n.ctx, ast.Store):
                parent = mod.up(n)
                good = (isinstance(parent, ast.Assign) and len(parent.targets) == 1 and parent.targets[0] is n
                        and isinstance(n.value, ast.Name) and isinstance(parent.value, ast.Name)
                        and parent.value.id == SLOT_PARAM[n.attr] and fn in ("__init__", "maintenance"))
                if not good:
                    out.append(f"(f) .{n.attr} stored other than from its parameter in a constructor: {mod.where(n)}")
                stores[(fn, n.attr)] += 1
                continue
            if not isinstance(n.ctx, ast.Load):
                out.append(f"(f) .{n.attr} deleted: {mod.where(n)}")
                continue
            if not (isinstance(n.value, ast.Name) and n.value.id == "self") or fn is None or mod.scoped_between(n):
                out.append(f"(f) .{n.attr} loaded off `self` or under a lambda/comprehension/walrus/default: "
                           f"{mod.where(n)}")
                continue
            p1 = mod.up(n)
            p2 = mod.up(p1) if p1 is not None else None
            p3 = mod.up(p2) if p2 is not None else None
            if isinstance(p1, ast.Attribute) and p1.value is n and isinstance(p2, ast.Call) and p2.func is p1:
                if n.attr == "_writer" and WRITER_BIND.get(p1.attr) == fn:
                    continue
                out.append(f"(f) self.{n.attr}.{p1.attr}() not bound to its method: {mod.where(n)}")
                continue
            if (isinstance(p1, ast.Attribute) and p1.value is n and p1.attr == "transport"
                    and isinstance(p2, ast.Attribute) and p2.value is p1
                    and isinstance(p3, ast.Call) and p3.func is p2):
                if p2.attr in CHAIN_BIND[n.attr] and fn == "abort":
                    continue
                out.append(f"(f) self.{n.attr}.transport.{p2.attr}() outside the bound chain: {mod.where(n)}")
                continue
            out.append(f"(f) self.{n.attr} in a shape that is not a bound call: {mod.where(n)}")
        elif isinstance(n, ast.Name) and n.id in SLOT_PARAM.values() and isinstance(n.ctx, ast.Load):
            parent = mod.up(n)
            good = (isinstance(parent, ast.Assign) and parent.value is n and len(parent.targets) == 1
                    and isinstance(parent.targets[0], ast.Attribute)
                    and SLOT_PARAM.get(parent.targets[0].attr) == n.id)
            if not good:
                out.append(f"(f) parameter `{n.id}` read other than into its slot: {mod.where(n)}")
        elif isinstance(n, ast.Name) and n.id in SLOT_PARAM.values() and not isinstance(n.ctx, ast.Load):
            out.append(f"(f) parameter `{n.id}` rebound: {mod.where(n)}")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("getattr", "vars", "setattr"):
            out.append(f"(f) {n.func.id}() inside Upstream: {mod.where(n)}")
        elif isinstance(n, ast.Attribute) and n.attr in ("__getattribute__", "__dict__", "__setattr__"):
            out.append(f"(f) .{n.attr} inside Upstream: {mod.where(n)}")
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.name in ("__getattr__", "__getattribute__"):
                out.append(f"(f) Upstream defines {n.name}: {mod.where(n)}")
            for d in n.decorator_list:
                dn = d.attr if isinstance(d, ast.Attribute) else getattr(d, "id", "")
                if dn in ("property", "setter", "getter", "deleter", "cached_property"):
                    out.append(f"(f) Upstream defines a property {n.name}: {mod.where(n)}")
    expected = Counter({(f, s): 1 for f in ("__init__", "maintenance") for s in SLOT_PARAM})
    if stores != expected:
        out.append(f"(f) slot fills differ: {dict(stores)} (one per slot per constructor form expected)")
    return out


def _net_bindings(mod: Module) -> Tuple[Dict[str, str], set, List[str]]:
    """Per file: Names an import binds to an API-set name (alias -> API), Names bound to the socket module, and the
    star imports from a listed module (each a failure)."""
    names: Dict[str, str] = {}
    sock_mods = set()
    stars = []
    for n in ast.walk(mod.tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in SOCKET_MODULES:
                    sock_mods.add(a.asname or a.name)
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            listed = n.module in NET_MODULES or n.module.startswith("asyncio.")
            if not listed:
                continue
            for a in n.names:
                if a.name == "*":
                    stars.append(f"(g) `from {n.module} import *`: {mod.where(n)}")
                elif a.name in API:
                    names[a.asname or a.name] = a.name
                elif a.name == "socket" and n.module in SOCKET_MODULES:
                    names[a.asname or a.name] = "socket"
    return names, sock_mods, stars


def rule_g(mods: Dict[str, Module]) -> List[str]:
    """(g) POSITIVE ON LOADS in `levain/broker/**`: every networking API load is the direct `Call.func` at one of the
    inventory's exact (file, function, API) tuples; `_connect` is loaded only as a direct call in `_handshake`; no star
    import from a listed module."""
    out = []
    sites = set(NETWORK_SITES)
    allowed_hits: Counter = Counter()
    for rel, mod in mods.items():
        if not rel.startswith("broker/"):
            continue
        names, sock_mods, stars = _net_bindings(mod)
        out.extend(stars)
        for n in mod.nodes():
            api = None
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
                if n.attr in API:
                    api = n.attr
                elif n.attr == "socket" and isinstance(n.value, ast.Name) and n.value.id in sock_mods:
                    api = "socket"
                elif n.attr == "_connect":
                    api = "_connect"
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                if n.id in names:
                    api = names[n.id]
                elif n.id == "_connect":
                    api = "_connect"
            if api is None:
                continue
            fn = mod.function(n)
            key = (mod.short, fn, api)
            if key in NAME_ALLOW:
                allowed_hits[key] += 1
                continue
            if api == "_connect":
                if not (rel == UPSTREAM and fn == "_handshake" and mod.is_call_func(n)):
                    out.append(f"(g) _connect loaded other than as a direct call in _handshake: {mod.where(n)}")
                continue
            if not mod.is_call_func(n):
                out.append(f"(g) {api} loaded but not called: {mod.where(n)}")
            elif key not in sites:
                out.append(f"(g) {api} called at a site with no inventory tuple {key}: {mod.where(n)}")
    for key, count in allowed_hits.items():
        if count > NAME_ALLOW[key]:
            out.append(f"(g) allow-listed name {key} occurs {count} times, bound {NAME_ALLOW[key]}")
    return out


def rule_h(mods: Dict[str, Module]) -> List[str]:
    """(h) POSITIVE: every load of `._graceful_close` in `levain/broker/**` is a direct call in `release_exchange` or
    `refresh_post`."""
    out = []
    for rel, mod in mods.items():
        if not rel.startswith("broker/"):
            continue
        for n in _identifier_loads(mod, "_graceful_close"):
            if not ((rel, mod.function(n)) in GRACEFUL_CALLERS and mod.is_call_func(n)):
                out.append(f"(h) _graceful_close load outside a direct call in release/refresh: {mod.where(n)}")
    return out


def _is_upstream_slot_entry(mod: Module, n: ast.AST) -> bool:
    seq = mod.up(n)
    assign = mod.up(seq) if seq is not None else None
    cls = mod.up(assign) if assign is not None else None
    return (isinstance(seq, (ast.Tuple, ast.List)) and isinstance(assign, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__slots__" for t in assign.targets)
            and isinstance(cls, ast.ClassDef) and cls.name == "Upstream")


def scan_private_outside(mods: Dict[str, Module]) -> List[str]:
    """The exact-constant scan, outside `upstream.py`: no constant equals a private name anywhere in `levain/`."""
    out = []
    for rel, mod in mods.items():
        if rel == UPSTREAM:
            continue
        for n in mod.nodes():
            if isinstance(n, ast.Constant) and n.value in PRIVATE:
                out.append(f"scan: constant {n.value!r}: {mod.where(n)}")
    return out


def scan_private_upstream(mods: Dict[str, Module]) -> List[str]:
    """The exact-constant scan, inside `upstream.py`: `"_writer"` and `"_keepalive"` once each, as `Upstream`'s
    `__slots__` entries; `"_graceful_close"` nowhere."""
    out = []
    mod = mods[UPSTREAM]
    seen: Counter = Counter()
    for n in mod.nodes():
        if isinstance(n, ast.Constant) and n.value in PRIVATE:
            seen[n.value] += 1
            if n.value == "_graceful_close" or not _is_upstream_slot_entry(mod, n):
                out.append(f"scan: constant {n.value!r} other than as Upstream's __slots__ entry: {mod.where(n)}")
    for name in ("_writer", "_keepalive"):
        if seen[name] != 1:
            out.append(f"scan: constant {name!r} occurs {seen[name]} times in upstream.py, expected once")
    return out


def scan_api_constants(mods: Dict[str, Module]) -> List[str]:
    """The exact-constant scan of the API names in `levain/broker/**` (so `getattr(asyncio, "open_connection")`
    fails where the load rule sees no Attribute)."""
    out = []
    hits: Counter = Counter()
    for rel, mod in mods.items():
        if not rel.startswith("broker/"):
            continue
        for n in mod.nodes():
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in API:
                key = (mod.short, mod.function(n), n.value)
                if key in CONSTANT_ALLOW:
                    hits[key] += 1
                    continue
                out.append(f"scan: API-name constant {n.value!r}: {mod.where(n)}")
    for key, count in hits.items():
        if count > CONSTANT_ALLOW[key]:
            out.append(f"scan: allow-listed constant {key} occurs {count} times, bound {CONSTANT_ALLOW[key]}")
    return out


#: (rule name, subjects, check). A subject is a file under `levain/` or "Upstream" (the class in upstream.py).
RULES = (
    ("a", (UPSTREAM, "Upstream"), rule_a),
    ("b", (UPSTREAM, "Upstream"), rule_b),
    ("c", (UPSTREAM, "Upstream"), rule_c),
    ("d", (UPSTREAM, "Upstream"), rule_d),
    ("e", (UPSTREAM, "Upstream"), rule_e),
    ("f", (UPSTREAM, "Upstream"), rule_f),
    ("g", (KEYS,), rule_g),
    ("h", (UPSTREAM, "Upstream"), rule_h),
    ("scan-private-outside-upstream", (), scan_private_outside),
    ("scan-private-upstream", (UPSTREAM, "Upstream"), scan_private_upstream),
    ("scan-api-constants", (), scan_api_constants),
)


def load_sources(root: Path) -> Dict[str, str]:
    base = root / "levain"
    return {p.relative_to(base).as_posix(): p.read_text(encoding="utf-8") for p in sorted(base.rglob("*.py"))}


def check_sources(sources: Dict[str, str], base_run: bool) -> List[Tuple[str, str, List[str]]]:
    """Run every rule over `{path under levain/: source}`. Returns (rule, "PASS" | "FAIL" | "NO SUBJECT", details)."""
    mods = {rel: Module(rel, src) for rel, src in sources.items()}
    results = []
    for name, subjects, fn in RULES:
        missing = [s for s in subjects if (s not in mods if s.endswith(".py") else _upstream_class(mods) is None)]
        if missing:
            if base_run:
                results.append((name, "NO SUBJECT", missing))
            else:
                results.append((name, "FAIL", [f"missing subject: {m}" for m in missing]))
            continue
        problems = fn(mods)
        results.append((name, "FAIL" if problems else "PASS", problems))
    return results


def format_report(results: List[Tuple[str, str, List[str]]]) -> str:
    lines = []
    for name, status, details in results:
        lines.append(f"rule {name}: {status}")
        lines.extend(f"    {d}" for d in details)
    return "\n".join(lines)


def test_broker_capability_ast() -> None:
    base_run = os.environ.get(BASE_RUN_SWITCH) == "1"
    results = check_sources(load_sources(ROOT), base_run)
    report = format_report(results)
    print(report)
    assert not [r for r in results if r[1] == "FAIL"], report
    no_subject = [r[0] for r in results if r[1] == "NO SUBJECT"]
    if no_subject:
        pytest.skip(f"{BASE_RUN_SWITCH}=1 base run, NOT a pass: no subject for rules {', '.join(no_subject)}")
