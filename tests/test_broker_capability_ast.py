"""The broker's capability check (design 1.2): an AST walk over the source, never a text grep.

The raw TLS writer and the plaintext writer under it are private to `upstream.Upstream`, and every upstream byte for an
exchange passes `Upstream.write`'s synchronous `detached` check. This test is the structural proof of that for every
path a stepper cannot reach (spawned tasks, callbacks): it asserts WHERE the capability may be touched, by position in
the syntax tree, so an alias, a getter, a returned bound method or a computed attribute name fails as the absence of a
permitted shape rather than as a listed case.

Rules (scope per rule): (a) (f) (g) (h) and the reflection rule walk `levain/broker/**`; (b) (c) (d) walk all of
`levain/`; (e) is the exact `_keepalive` site set in `upstream.py`. The exact-constant scans: the three private names
over all of `levain/`, the networking API names over `levain/broker/**`. Rule (g)'s allowed call sites are the
inventory in `tests/broker_network_sites.py`, which the session that adds a site extends in the same commit.

Every allowed site is named by its enclosing definitions' full path from the module (`Upstream.write`,
`refresh_post`), never by a bare function name, so a nested `def write` inside another method is not `Upstream.write`.
Every count is exact: an allowed site that occurs more often than declared fails, and so does one that no longer
occurs (a stale entry).

Every rule's subjects (the files and the class it depends on) are looked up first and a missing one FAILS, naming it,
so renaming `Upstream` or a file fails here instead of turning the rules vacuous. The one exception is the one-time run
on a build's parent commit (before the subjects exist), set by the run-only switch `LEVAIN_AST_BASE_RUN=1`: there a
missing subject is reported "NO SUBJECT", which is not a pass, and the test skips with that report. Under CI the switch
being set at all fails the test.

KNOWN EDGE: the AST test catches honest builder mistakes at the capability boundary; deliberate evasion through
Python's reflection (aliased reflective imports, shadowing class definitions, computed
__getattribute__/methodcaller/getattr_static, sys.modules, and outside-class inspect/gc/copy) is out of its scope. The
runtime invariant (the raw writer lives only in Upstream's slots behind the synchronous detached fence) is what holds
the boundary; the test is its backstop.
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
FRONT = "broker/front.py"

#: rule (g)'s API set; the same names make up the broker's exact-constant scan
API = frozenset({
    "open_connection", "open_unix_connection", "start_tls", "sock_connect", "create_connection",
    "create_unix_connection", "create_datagram_endpoint", "connect", "connect_ex", "wrap_socket", "wrap_bio",
    "sendto", "sendmsg", "send", "sendall", "sock_sendall", "create_server", "start_server", "start_unix_server",
    "socketpair", "fromfd", "connect_accepted_socket",
})
PRIVATE = ("_writer", "_keepalive", "_graceful_close")
NET_MODULES = ("asyncio", "socket", "ssl", "_socket", "_ssl")
SOCKET_MODULES = ("socket", "_socket")
#: HTTP client modules `levain/broker/**` may not import at all (a module, or anything under it)
HTTP_MODULES = ("urllib.request", "http.client", "aiohttp", "httpx", "requests")

#: The API-name constant the front passes to `Refused` as the refusal's NAME when every validated address fails to
#: connect: a string in a refusal, not a network call (head ruling P-1, r19 addendum). Allowed at exactly this
#: (file, definition path, constant, shape), never by a broader pattern; the count is exact.
CONSTANT_ALLOW = {("front.py", "Front._open_upstream", "connect", "Refused(...)"): 1}

#: Two names the front held on main before any 3b code, which the by-name rules match although neither is a
#: networking call: `FrontPolicy.connect` (the host -> route map, a dataclass field read as `policy.connect.get(...)`),
#: and the front's injected connector attribute `self._connect` (its default is `open_by_address`, whose own calls
#: are inventory tuples), which shares the upstream seam's name. Allowed at exactly these (file, definition path, name,
#: use shape) sites, the shape checked before the allowance applies; any other use of the name falls through to the
#: ordinary checks. The count is exact, so a rename of either field fails here as a stale entry.
NAME_ALLOW = {
    ("front.py", "connect_target", "connect", "policy.connect.get(...)"): 1,
    ("front.py", "Front._open_upstream", "_connect", "self._connect(...)"): 1,
}

#: rule (c): `refresh_post`'s callers: EXACTLY one, the credential layer's refresh (creds.py `_post`, session 3b-5b)
REFRESH_POST_CALLERS = 1

#: rule (f): each method called directly on `self._writer`, bound to the one `Upstream` method allowed to call it
WRITER_BIND = {"write": "write", "close": "_graceful_close", "wait_closed": "wait_closed", "drain": "drain",
               "is_closing": "abort"}
#: rule (f): the one chain per slot, `self.<slot>.transport.<m>()`, bound to `Upstream.abort`
CHAIN_BIND = {"_writer": {"abort", "is_closing"}, "_keepalive": {"abort"}}
#: rule (f): the constructor parameter each slot is filled from
SLOT_PARAM = {"_writer": "writer", "_keepalive": "_keepalive"}
#: rule (f): the two constructor forms that fill the slots
CONSTRUCTORS = ("Upstream.__init__", "Upstream.maintenance")

#: rule (b): `Upstream.write`'s whole body (after its docstring), statement for statement: the `detached` fence is
#: synchronous and precedes the one raw write, with nothing between them.
WRITE_FENCE = (
    "ex = self._exchange\n"
    "if ex is not None and ex.detached:\n"
    "    raise StaleExchange()\n"
    "self._writer.write(data)\n"
)
WRITE_PARAMS = ["self", "data"]

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

#: rule (h): the only (file, definition path) pairs that may call `_graceful_close`
GRACEFUL_CALLERS = {("broker/keys.py", "release_exchange"), ("broker/upstream.py", "refresh_post")}

#: the reflection rule: each reflective callable -> which positional arguments are names ("all" = every one; None =
#: refused outright, it has no name argument)
REFLECT = {"getattr": (1,), "setattr": (1,), "vars": None, "attrgetter": "all", "__import__": (0,),
           "import_module": (0,)}
REFLECT_MODULE_FUNCS = ("__import__", "import_module")
#: the attribute names a literal reflective name may not be: the private set, the API set, and the other names the
#: rules bind (the socket constructor, the seams, the maintenance form and its one caller)
REFLECT_REFUSED = frozenset(PRIVATE) | API | {"socket", "_connect", "_handshake", "maintenance", "refresh_post"}
#: the top-level modules a literal reflective import may not name
REFLECT_REFUSED_MODULES = frozenset(NET_MODULES) | {m.split(".")[0] for m in HTTP_MODULES}

_SCOPES = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.NamedExpr, ast.arguments)
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


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

    def qualname(self, node: ast.AST) -> str:
        """The node's full definition path from the module: every enclosing `class`, `def` and `async def` whose BODY
        holds it (a decorator, default, annotation or base is evaluated in the scope outside), outermost first, with
        `<lambda>` for a lambda body. At module level, "<module>". A nested `def write` inside `Upstream.abort` is
        `Upstream.abort.write`, never `Upstream.write`."""
        parts: List[str] = []
        child = node
        for a in self.ancestors(node):
            if isinstance(a, _DEFS) and any(child is s for s in a.body):
                parts.append(a.name)
            elif isinstance(a, ast.Lambda) and child is a.body:
                parts.append("<lambda>")
            child = a
        return ".".join(reversed(parts)) or "<module>"

    def scope_name(self, node: ast.AST) -> str:
        """The innermost function or class name (rule (e)'s site key), or "<module>"."""
        for a in self.ancestors(node):
            if isinstance(a, _DEFS):
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
        return f"{self.rel}:{getattr(node, 'lineno', '?')} in {self.qualname(node)}"


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


def _class_binds(cls: ast.ClassDef, name: str) -> List[ast.AST]:
    """Every statement-level binding of `name` in the class's own namespace (a def, a class, any Name store), not
    descending into a nested definition's body."""
    out: List[ast.AST] = []
    stack: List[ast.AST] = list(cls.body)
    while stack:
        n = stack.pop()
        if isinstance(n, _DEFS):
            if n.name == name:
                out.append(n)
            continue
        if isinstance(n, ast.Lambda):
            continue
        if isinstance(n, ast.Name) and n.id == name and not isinstance(n.ctx, ast.Load):
            out.append(n)
        stack.extend(ast.iter_child_nodes(n))
    return out


def _method(cls: ast.ClassDef, name: str) -> Optional[ast.AST]:
    binds = _class_binds(cls, name)
    if len(binds) == 1 and isinstance(binds[0], (ast.FunctionDef, ast.AsyncFunctionDef)) and binds[0] in cls.body:
        return binds[0]
    return None


def _body(fn: ast.AST) -> List[ast.stmt]:
    """A definition's statements after its docstring."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return body


def _params(fn: ast.AST) -> List[str]:
    a = fn.args
    return [x.arg for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)]


def _use_shape(mod: Module, n: ast.AST) -> str:
    """How a name is used: `<expr>(...)` when it is the called expression, `<expr>.<m>(...)` when a method is called
    on it, else `load <expr>`."""
    p = mod.up(n)
    if isinstance(p, ast.Call) and p.func is n:
        return ast.unparse(n) + "(...)"
    if isinstance(p, ast.Attribute) and p.value is n:
        g = mod.up(p)
        if isinstance(g, ast.Call) and g.func is p:
            return ast.unparse(p) + "(...)"
    return "load " + ast.unparse(n)


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


def _write_fence(mods: Dict[str, Module]) -> List[str]:
    """`Upstream.write` is exactly one plain synchronous `def` in the class namespace, undecorated, taking
    `(self, data)`, whose body is WRITE_FENCE statement for statement."""
    cls = _upstream_class(mods)
    assert cls is not None
    where = f"{UPSTREAM} in Upstream"
    binds = _class_binds(cls, "write")
    if len(binds) != 1 or not isinstance(binds[0], ast.FunctionDef) or binds[0] not in cls.body:
        kinds = ", ".join(f"{type(b).__name__}@{getattr(b, 'lineno', '?')}" for b in binds) or "none"
        return [f"(b) Upstream.write is not exactly one plain synchronous def in the class body ({kinds}): {where}"]
    fn = binds[0]
    out = []
    if fn.decorator_list:
        out.append(f"(b) Upstream.write is decorated: {UPSTREAM}:{fn.lineno}")
    a = fn.args
    if (a.posonlyargs or a.vararg or a.kwonlyargs or a.kwarg or a.defaults
            or [x.arg for x in a.args] != WRITE_PARAMS):
        out.append(f"(b) Upstream.write's parameters are not exactly {WRITE_PARAMS}: {UPSTREAM}:{fn.lineno}")
    want = [ast.dump(s) for s in ast.parse(WRITE_FENCE).body]
    got = [ast.dump(s) for s in _body(fn)]
    if got != want:
        out.append(f"(b) Upstream.write's body is not the fence (detached check, then the one raw write, nothing "
                   f"else): {UPSTREAM}:{fn.lineno}")
    return out


def rule_b(mods: Dict[str, Module]) -> List[str]:
    """(b) The TLS writer's `write` is called only inside `Upstream.write`, across all of `levain/`, and
    `Upstream.write` is the exact fence shape: the synchronous `detached` check precedes the one raw write."""
    out = []
    for rel, mod in mods.items():
        for n in mod.nodes():
            if (isinstance(n, ast.Attribute) and n.attr == "write" and isinstance(n.value, ast.Attribute)
                    and n.value.attr == "_writer"):
                ok = rel == UPSTREAM and mod.qualname(n) == "Upstream.write" and mod.is_call_func(n)
                if not ok:
                    out.append(f"(b) ._writer.write outside Upstream.write: {mod.where(n)}")
    out.extend(_write_fence(mods))
    return out


def rule_c(mods: Dict[str, Module]) -> List[str]:
    """(c) Every load of `maintenance` (attribute or name) anywhere in `levain/` is refused except the one direct call
    `Upstream.maintenance(...)` inside `upstream.refresh_post`; `refresh_post` has exactly REFRESH_POST_CALLERS
    callers, each a direct call in `creds.py`."""
    out = []
    callers = 0
    the_call = 0
    for rel, mod in mods.items():
        for n in _identifier_loads(mod, "maintenance"):
            ok = (rel == UPSTREAM and mod.qualname(n) == "refresh_post" and mod.is_call_func(n)
                  and isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "Upstream")
            if ok:
                the_call += 1
            else:
                out.append(f"(c) maintenance loaded other than as the direct Upstream.maintenance(...) call in "
                           f"refresh_post: {mod.where(n)}")
        for n in _identifier_loads(mod, "refresh_post"):
            callers += 1
            if not (rel == CREDS and mod.is_call_func(n)):
                out.append(f"(c) refresh_post loaded other than as a direct call in creds.py: {mod.where(n)}")
    if the_call != 1:
        out.append(f"(c) the direct Upstream.maintenance(...) call in refresh_post occurs {the_call} times, expected "
                   f"once")
    if callers != REFRESH_POST_CALLERS:
        out.append(f"(c) refresh_post has {callers} callers, expected exactly {REFRESH_POST_CALLERS}")
    return out


def rule_d(mods: Dict[str, Module]) -> List[str]:
    """(d) `_handshake` is called only by `open_exchange` and `refresh_post`, across all of `levain/`."""
    out = []
    for rel, mod in mods.items():
        for n in _identifier_loads(mod, "_handshake"):
            if not (rel == UPSTREAM and mod.is_call_func(n) and mod.qualname(n) in ("open_exchange", "refresh_post")):
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


def _receivers(mod: Module, cls: ast.ClassDef) -> Tuple[Dict[str, Tuple[ast.AST, str]], List[str]]:
    """Each constructor form's (definition, the one Name its slot stores go on): `self` in `__init__` (its first
    parameter, never rebound), and in the classmethod `maintenance` the Name bound by its first statement
    `<x> = <cls>.__new__(<cls>)` (`<cls>` its first parameter, `<x>` bound nowhere else in it)."""
    out: Dict[str, Tuple[ast.AST, str]] = {}
    problems = []
    init = _method(cls, "__init__")
    if init is None or not isinstance(init, ast.FunctionDef) or init.decorator_list or \
            _params(init)[:1] != ["self"]:
        problems.append(f"(f) Upstream.__init__ is not one plain def whose first parameter is `self`: {UPSTREAM}")
    else:
        out["Upstream.__init__"] = (init, "self")
    maint = _method(cls, "maintenance")
    shape_ok = False
    if maint is not None and isinstance(maint, ast.FunctionDef) and len(maint.decorator_list) == 1 \
            and isinstance(maint.decorator_list[0], ast.Name) and maint.decorator_list[0].id == "classmethod" \
            and maint.args.args:
        cls_name = maint.args.args[0].arg
        body = _body(maint)
        first = body[0] if body else None
        if (isinstance(first, ast.Assign) and len(first.targets) == 1 and isinstance(first.targets[0], ast.Name)
                and isinstance(first.value, ast.Call) and isinstance(first.value.func, ast.Attribute)
                and first.value.func.attr == "__new__" and isinstance(first.value.func.value, ast.Name)
                and first.value.func.value.id == cls_name and len(first.value.args) == 1
                and isinstance(first.value.args[0], ast.Name) and first.value.args[0].id == cls_name
                and not first.value.keywords):
            recv = first.targets[0].id
            stores = [n for n in ast.walk(maint) if isinstance(n, ast.Name) and n.id == recv
                      and not isinstance(n.ctx, ast.Load)]
            if len(stores) == 1 and recv not in _params(maint):
                out["Upstream.maintenance"] = (maint, recv)
                shape_ok = True
    if not shape_ok:
        problems.append(f"(f) Upstream.maintenance is not the classmethod whose first statement binds its one "
                        f"receiver as `<x> = cls.__new__(cls)`: {UPSTREAM}")
    if "Upstream.__init__" in out:
        for n in ast.walk(init):
            if isinstance(n, ast.Name) and n.id == "self" and not isinstance(n.ctx, ast.Load):
                problems.append(f"(f) `self` rebound in Upstream.__init__: {mod.where(n)}")
    return out, problems


def rule_f(mods: Dict[str, Module]) -> List[str]:
    """(f) POSITIVE inside `Upstream`: every Load of `self._writer` / `self._keepalive` is a direct method call bound
    to its method, or the one `.transport.<m>()` chain bound to `abort`; each slot is filled once per constructor form,
    on that constructor's own receiver (`self` in `__init__`), from that constructor's own parameter, and the
    parameters are read nowhere else; no getter machinery."""
    out = []
    mod = mods[UPSTREAM]
    cls = _upstream_class(mods)
    assert cls is not None
    receivers, problems = _receivers(mod, cls)
    out.extend(problems)
    stores: Counter = Counter()
    for n in ast.walk(cls):
        if id(n) in mod.annotation_ids:
            continue
        if isinstance(n, ast.Attribute) and n.attr in SLOT_PARAM:
            q = mod.qualname(n)
            if isinstance(n.ctx, ast.Store):
                parent = mod.up(n)
                ctor = receivers.get(q)
                good = (ctor is not None and isinstance(parent, ast.Assign) and len(parent.targets) == 1
                        and parent.targets[0] is n and not mod.scoped_between(n)
                        and isinstance(n.value, ast.Name) and n.value.id == ctor[1]
                        and isinstance(parent.value, ast.Name) and parent.value.id == SLOT_PARAM[n.attr]
                        and parent.value.id in _params(ctor[0]))
                if not good:
                    out.append(f"(f) .{n.attr} stored other than on its constructor's receiver from that "
                               f"constructor's parameter: {mod.where(n)}")
                stores[(q, n.attr)] += 1
                continue
            if not isinstance(n.ctx, ast.Load):
                out.append(f"(f) .{n.attr} deleted: {mod.where(n)}")
                continue
            if not (isinstance(n.value, ast.Name) and n.value.id == "self") or mod.scoped_between(n):
                out.append(f"(f) .{n.attr} loaded off `self` or under a lambda/comprehension/walrus/default: "
                           f"{mod.where(n)}")
                continue
            p1 = mod.up(n)
            p2 = mod.up(p1) if p1 is not None else None
            p3 = mod.up(p2) if p2 is not None else None
            if isinstance(p1, ast.Attribute) and p1.value is n and isinstance(p2, ast.Call) and p2.func is p1:
                if n.attr == "_writer" and p1.attr in WRITER_BIND and q == "Upstream." + WRITER_BIND[p1.attr]:
                    continue
                out.append(f"(f) self.{n.attr}.{p1.attr}() not bound to its method: {mod.where(n)}")
                continue
            if (isinstance(p1, ast.Attribute) and p1.value is n and p1.attr == "transport"
                    and isinstance(p2, ast.Attribute) and p2.value is p1
                    and isinstance(p3, ast.Call) and p3.func is p2):
                if p2.attr in CHAIN_BIND[n.attr] and q == "Upstream.abort":
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
    expected = Counter({(f, s): 1 for f in CONSTRUCTORS for s in SLOT_PARAM})
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


def _http_banned(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in HTTP_MODULES)


def _http_imports(mod: Module) -> List[str]:
    """An import of an HTTP client module (or anything under one), in any spelling: `import urllib.request`,
    `from urllib import request`, `from http.client import HTTPConnection`, or `urllib.request` reached as an
    attribute of an imported package."""
    out = []
    packages: Dict[str, str] = {}
    for n in ast.walk(mod.tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if _http_banned(a.name):
                    out.append(f"(g) import of HTTP client module {a.name}: {mod.where(n)}")
                elif a.asname:
                    packages[a.asname] = a.name
                else:
                    packages[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            if _http_banned(n.module):
                out.append(f"(g) import from HTTP client module {n.module}: {mod.where(n)}")
                continue
            for a in n.names:
                if _http_banned(f"{n.module}.{a.name}"):
                    out.append(f"(g) import of HTTP client module {n.module}.{a.name}: {mod.where(n)}")
    for n in mod.nodes():
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in packages
                and _http_banned(f"{packages[n.value.id]}.{n.attr}")):
            out.append(f"(g) HTTP client module {packages[n.value.id]}.{n.attr} reached as an attribute: "
                       f"{mod.where(n)}")
    return out


def rule_g(mods: Dict[str, Module]) -> List[str]:
    """(g) POSITIVE ON LOADS in `levain/broker/**`: every networking API load is the direct `Call.func` at one of the
    inventory's exact (file, definition path, API, called expression) signatures, and each signature occurs exactly
    its declared count (0 occurrences of a declared signature fails as stale); `_connect` is loaded only as a direct
    call in `_handshake`; no star import from a listed module; no HTTP client module imported. An allow-listed name
    counts only in its exact use shape, and its count is exact."""
    out = []
    declared: Dict[Tuple[str, str, str, str], int] = {}
    for f, q, api, func, count in NETWORK_SITES:
        key = (f, q, api, func)
        if key in declared:
            out.append(f"(g) inventory declares {key} twice")
        declared[key] = count
    observed: Counter = Counter()
    allowed_hits: Counter = Counter()
    for rel, mod in mods.items():
        if not rel.startswith("broker/"):
            continue
        names, sock_mods, stars = _net_bindings(mod)
        out.extend(stars)
        out.extend(_http_imports(mod))
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
            q = mod.qualname(n)
            akey = (mod.short, q, api, _use_shape(mod, n))
            if akey in NAME_ALLOW:
                allowed_hits[akey] += 1
                continue
            if api == "_connect":
                if not (rel == UPSTREAM and q == "_handshake" and mod.is_call_func(n)):
                    out.append(f"(g) _connect loaded other than as a direct call in _handshake: {mod.where(n)}")
                continue
            if not mod.is_call_func(n):
                out.append(f"(g) {api} loaded but not called: {mod.where(n)}")
                continue
            key = (mod.short, q, api, ast.unparse(n))
            if key not in declared:
                out.append(f"(g) {api} called at a site with no inventory tuple {key}: {mod.where(n)}")
                continue
            observed[key] += 1
    for key, count in declared.items():
        if observed[key] != count:
            state = "stale: no such site" if observed[key] == 0 else f"occurs {observed[key]} times"
            out.append(f"(g) inventory site {key} declared {count}, {state}")
    for key, count in NAME_ALLOW.items():
        if allowed_hits[key] != count:
            state = "stale: no such site" if allowed_hits[key] == 0 else f"occurs {allowed_hits[key]} times"
            out.append(f"(g) allow-listed name {key} declared {count}, {state}")
    return out


def rule_h(mods: Dict[str, Module]) -> List[str]:
    """(h) POSITIVE: every load of `._graceful_close` in `levain/broker/**` is a direct call in `release_exchange` or
    `refresh_post`, each named by its full definition path."""
    out = []
    for rel, mod in mods.items():
        if not rel.startswith("broker/"):
            continue
        for n in _identifier_loads(mod, "_graceful_close"):
            if not ((rel, mod.qualname(n)) in GRACEFUL_CALLERS and mod.is_call_func(n)):
                out.append(f"(h) _graceful_close load outside a direct call in release/refresh: {mod.where(n)}")
    return out


def rule_reflect(mods: Dict[str, Module]) -> List[str]:
    """Across `levain/broker/**`: `getattr`, `setattr`, `vars`, `operator.attrgetter`, `__import__` and
    `importlib.import_module` are refused unless loaded as the direct call whose name arguments are positional string
    literals outside the refused sets (`vars` has no name argument and is refused outright). A computed name, an alias
    of the callable or a star argument fails as the absence of that shape."""
    out = []
    for rel, mod in mods.items():
        if not rel.startswith("broker/"):
            continue
        for ident, spec in REFLECT.items():
            for n in _identifier_loads(mod, ident):
                call = mod.up(n)
                if not (isinstance(call, ast.Call) and call.func is n):
                    out.append(f"(reflect) {ident} loaded other than as a direct call: {mod.where(n)}")
                    continue
                if spec is None:
                    out.append(f"(reflect) {ident}() refused: {mod.where(n)}")
                    continue
                if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
                    out.append(f"(reflect) {ident}() with a star argument: {mod.where(n)}")
                    continue
                idxs = range(len(call.args)) if spec == "all" else spec
                if spec == "all" and not call.args:
                    out.append(f"(reflect) {ident}() with no positional name: {mod.where(n)}")
                for i in idxs:
                    if i >= len(call.args):
                        out.append(f"(reflect) {ident}() without its positional name argument: {mod.where(n)}")
                        continue
                    arg = call.args[i]
                    if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                        out.append(f"(reflect) {ident}() with a computed name: {mod.where(n)}")
                        continue
                    if ident in REFLECT_MODULE_FUNCS:
                        if arg.value.split(".")[0] in REFLECT_REFUSED_MODULES:
                            out.append(f"(reflect) {ident}({arg.value!r}) names a refused module: {mod.where(n)}")
                    elif any(part in REFLECT_REFUSED for part in arg.value.split(".")):
                        out.append(f"(reflect) {ident}() names a refused attribute {arg.value!r}: {mod.where(n)}")
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


def _constant_shape(mod: Module, n: ast.AST) -> str:
    p = mod.up(n)
    if isinstance(p, ast.Call) and any(n is a for a in p.args):
        return ast.unparse(p.func) + "(...)"
    return "const"


def scan_api_constants(mods: Dict[str, Module]) -> List[str]:
    """The exact-constant scan of the API names in `levain/broker/**` (so `getattr(asyncio, "open_connection")`
    fails where the load rule sees no Attribute). An allowed constant counts only in its exact shape; its count is
    exact."""
    out = []
    hits: Counter = Counter()
    for rel, mod in mods.items():
        if not rel.startswith("broker/"):
            continue
        for n in mod.nodes():
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in API:
                key = (mod.short, mod.qualname(n), n.value, _constant_shape(mod, n))
                if key in CONSTANT_ALLOW:
                    hits[key] += 1
                    continue
                out.append(f"scan: API-name constant {n.value!r}: {mod.where(n)}")
    for key, count in CONSTANT_ALLOW.items():
        if hits[key] != count:
            state = "stale: no such site" if hits[key] == 0 else f"occurs {hits[key]} times"
            out.append(f"scan: allow-listed constant {key} declared {count}, {state}")
    return out


def _files(*keysets) -> Tuple[str, ...]:
    return tuple(sorted({"broker/" + k[0] for ks in keysets for k in ks}))


#: (rule name, subjects, check). A subject is a file under `levain/` or "Upstream" (the class in upstream.py); each
#: rule lists every file and the class its checks depend on, so a missing one fails instead of passing vacuously.
RULES = (
    ("a", (UPSTREAM, "Upstream"), rule_a),
    ("b", (UPSTREAM, "Upstream"), rule_b),
    ("c", (UPSTREAM, "Upstream", CREDS), rule_c),
    ("d", (UPSTREAM, "Upstream"), rule_d),
    ("e", (UPSTREAM, "Upstream"), rule_e),
    ("f", (UPSTREAM, "Upstream"), rule_f),
    ("g", tuple(sorted({UPSTREAM, KEYS} | set(_files(NETWORK_SITES, NAME_ALLOW)))), rule_g),
    ("h", (UPSTREAM, "Upstream") + tuple(sorted({rel for rel, _ in GRACEFUL_CALLERS} - {UPSTREAM})), rule_h),
    ("reflect", (), rule_reflect),
    ("scan-private-outside-upstream", (), scan_private_outside),
    ("scan-private-upstream", (UPSTREAM, "Upstream"), scan_private_upstream),
    ("scan-api-constants", _files(CONSTANT_ALLOW), scan_api_constants),
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
    if os.environ.get("CI") and BASE_RUN_SWITCH in os.environ:
        pytest.fail(f"{BASE_RUN_SWITCH} is set under CI: it is a one-time run switch for a build's parent commit, "
                    f"never set in CI")
    base_run = os.environ.get(BASE_RUN_SWITCH) == "1"
    results = check_sources(load_sources(ROOT), base_run)
    report = format_report(results)
    print(report)
    assert not [r for r in results if r[1] == "FAIL"], report
    no_subject = [r[0] for r in results if r[1] == "NO SUBJECT"]
    if no_subject:
        pytest.skip(f"{BASE_RUN_SWITCH}=1 base run, NOT a pass: no subject for rules {', '.join(no_subject)}")
