"""levain.dispatch.registry — what each kind of dispatch configuration item holds, and the template resolver.

:mod:`levain.dispatch.control` verifies a configuration item's signature and generation; this module judges its BODY
(:func:`body_refusal`) against the configuration already active, and reads the active configuration back out in the
shapes the rest of dispatch takes: :func:`resolver` for ``JobStore.file``'s ``resolve_template`` and
:func:`active_policy` for its ``policy``.

A TEMPLATE is the input shape of :class:`levain.dispatch.contract.ResolvedTemplate`'s mapping. Its ``content_hash``
is COMPUTED here, as the SHA-256 of the template's canonical form without that field (:func:`template_content_hash`);
an item must carry the hash it was signed with and a mismatch refuses the item, so the operator signs the hash a
contract will later bind and no input chooses it. R2 is never a template: an item naming it is refused.

An R0 template writes only CODE UNDER TEST and its docs (DESIGN §3.2, the R0 row): every write path must be
POSITIVELY eligible, a source or doc file by extension (:data:`R0_SOURCE_EXTENSIONS`), and must not be
EXECUTION-BEARING (:data:`EXECUTION_BEARING_FILES`, :data:`EXECUTION_BEARING_DIRS`), the deny list winning over the
allowlist (:func:`r0_write_refusal`). One path that fails refuses the template whole. The intent: R0 is the tier where
the verifier IS the criterion and a result may be adopted by rule, so an executor that could rewrite what the
verifier runs, what the build or an interpreter runs, or what the next agent session loads would be grading or
steering its own work. Allowing by kind means a file type nobody thought of is refused, not admitted; the deny list
then removes the eligible-looking files that execute (a test module, ``setup.py``, ``sitecustomize.py``, an agent's
instructions in Markdown). An ordinary module, ``__init__.py`` included, is what R0 exists to write, so it is
eligible. Matching casefolds both sides (the target disk is case-insensitive) and looks at every depth
(``pkg/tests/x.py`` is a test).

The VERIFIERS kind maps a verifier id to its artifact hash and kind; a template naming an unknown verifier, or one
whose ``verifier_hash`` or ``verifier_kind`` disagrees with the registered verifier, is refused when it is accepted.
That check reads the ACTIVE verifiers, so acceptance order matters: the verifiers item goes first. A later verifiers
item may change a verifier a template names (replacing a verifier and its templates takes two items, verifiers
first, so refusing it would leave no order that works); the resolver then refuses that template until a templates
item agreeing with it is accepted, so a stale pairing never resolves.

An ADOPTION RULE names an R0 template whose verifier is black-box, the template's content hash (an edited template
is a new hash, so a rule never carries over to content it was not signed for) and the rule's own criteria (DESIGN
§3.2: contrast-clean is necessary, never sufficient). A rule is judged when it is accepted AND again on every read
(:func:`active_adoption_rules`), against the templates and verifiers active THEN: a later templates or verifiers
item can leave a rule pointing at content or a verifier it was not signed for, and such a rule raises rather than
reads. The intent is that this reader is the only way to the rules: the store's public reader is meant to leave
them out. No adoption runs here.

Templates load per id: a stored template the current rules refuse raises only when that id is resolved, naming it.
Stdlib only.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from levain.contracts.canon import canonical_bytes
from levain.dispatch import DispatchError
from levain.dispatch.contract import ResolvedTemplate
from levain.dispatch.policy import PolicyError, ProviderPolicy, match, path_text_problem

__all__ = [
    "ADOPTION_RULE_KEYS", "EXECUTION_BEARING_DIRS", "EXECUTION_BEARING_FILES", "KINDS", "R0_SOURCE_EXTENSIONS",
    "ROUTE_KEYS", "TEMPLATE_FORMAT", "TEMPLATE_KEYS", "VERIFIER_KEYS", "AdoptionRule", "active_adoption_rules",
    "active_policy", "body_refusal", "execution_bearing", "r0_write_refusal", "resolver", "template_content_hash",
    "template_from_mapping",
]

KINDS = ("adoption_rules", "provider_policy", "routemap", "templates", "verifiers")
TEMPLATE_FORMAT = "levain-dispatch-template/1"
TEMPLATE_KEYS = frozenset({"id", "content_hash", "tier", "executor", "models", "k", "verifier_id", "verifier_hash",
                           "verifier_kind", "write_paths", "provider", "route", "tool_versions"})
VERIFIER_KEYS = frozenset({"artifact_hash", "kind"})
ADOPTION_RULE_KEYS = frozenset({"template_id", "template_hash", "verifier_kind", "criteria"})
ROUTE_KEYS = frozenset({"route", "provider", "upstream", "credential", "models", "body_policy", "retention_claim"})
_ENTRIES_MAX = 256
_TEXT_MAX = 1024
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# a verifier id, in the shape a template's verifier_id must have (the contract module's name rule)
_VERIFIER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:/+-]{0,127}")
_VERIFIER_KINDS = ("black-box", "in-process")

# The extensions an R0 write path may have, casefolded: source a verifier tests, and the docs and styles beside it.
# A script (.sh) is not here: running it is its purpose. Neither are data and configuration formats (.json, .toml,
# .yaml, .ini): a manifest or a tool's settings decide what runs, and no extension tells one from plain data.
R0_SOURCE_EXTENSIONS: tuple[str, ...] = (
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java", ".kt", ".scala",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".m", ".php", ".lua",
    ".md", ".rst", ".txt", ".css", ".scss", ".html", ".svg",
)

# The execution-bearing deny list, casefolded; it wins over the allowlist. FILES are matched against a path's last
# component, DIRS against every component above it.
#   tests: the verifier runs them, so an executor that writes one grades itself (conftest.py runs at collection)
#   build and CI: they run on install, on build or on push, outside the contract's write grant
#   hooks and git attributes: git and pre-commit run them, or the filters they name, on the operator's next commit
#   interpreter start-up and tool rc files: Python, the shell or a tool loads them on its next start
#   agent and editor configuration: the next agent session or the editor loads it as instructions or tasks
#   lockfiles: they choose the code every later install runs
EXECUTION_BEARING_FILES: tuple[str, ...] = (
    "test_*.*", "test.py", "tests.py", "*.test.*", "*.spec.*", "*_test.*", "*_spec.*", "conftest.py",
    "makefile", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "noxfile.py", "build.rs", "*.config.*",
    ".gitlab-ci.yml",
    ".pre-commit-config.yaml", ".pre-commit-config.yml", ".gitattributes", ".gitmodules",
    "sitecustomize.py", "usercustomize.py", "*.pth", ".envrc", ".*rc", ".*rc.*",
    "agents.md", "claude.md", "claude.local.md", "gemini.md", ".mcp.json",
    "*.lock", "package-lock.json", "poetry.lock", "uv.lock", "requirements*.txt", "constraints*.txt",
)
EXECUTION_BEARING_DIRS: tuple[str, ...] = (
    "tests", "test", "testing", "__tests__", "spec", "e2e",
    ".github", ".circleci", ".git", ".githooks", ".husky",
    ".claude", ".codex", ".vscode", ".idea",
    "__pycache__",
)


class _ConfigStore(Protocol):
    """What the readers here need from :class:`levain.dispatch.control.ControlStore`: the active item of each kind,
    keyed by kind, each with its parsed ``body`` (``active`` without the adoption rules, ``_verified_items`` with
    them, for :func:`active_adoption_rules` alone); each raises when the store cannot be read or verified."""
    def active(self) -> Mapping[str, Any]: ...

    def _verified_items(self) -> Mapping[str, Any]: ...


def execution_bearing(path: str) -> str | None:
    """The deny-list entry an R0 write path matches (casefolded, at any depth), or ``None``."""
    parts = path.casefold().split("/")
    for d in parts[:-1]:
        if d in EXECUTION_BEARING_DIRS:
            return d + "/**"
    for pattern in EXECUTION_BEARING_FILES:
        if match(pattern, parts[-1]):
            return pattern
    return None


def r0_write_refusal(path: str) -> str | None:
    """Why an R0 template may not write ``path``: execution-bearing (checked first, so it wins), or not an eligible
    source or doc file; ``None`` when it may. Only the path's form is judged here; the contract path rule still
    applies to it."""
    hit = execution_bearing(path)
    if hit is not None:
        return f"{path} is an execution-bearing file ({hit})"
    ext = posixpath.splitext(path.rsplit("/", 1)[-1])[1].casefold()
    if ext not in R0_SOURCE_EXTENSIONS:
        return f"{path} is not R0-eligible source or docs (extension {ext or 'none'!r})"
    return None


def _text(value: Any, what: str) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _TEXT_MAX:
        return f"{what} must be 1 to {_TEXT_MAX} characters of text"
    return path_text_problem(value, what)


# --- templates -----------------------------------------------------------------------------------------------------

def template_content_hash(t: Mapping[str, Any]) -> str:
    """The SHA-256 of a template's canonical form, every field but ``content_hash`` (write paths sorted)."""
    body = {k: v for k, v in t.items() if k != "content_hash"}
    body["write_paths"] = sorted(body["write_paths"])
    return "sha256:" + hashlib.sha256(canonical_bytes({"format": TEMPLATE_FORMAT, **body})).hexdigest()


def template_from_mapping(t: Any) -> ResolvedTemplate:
    """The template a configuration entry holds, checked whole; raises :class:`DispatchError` saying why."""
    if not isinstance(t, Mapping) or set(t) != TEMPLATE_KEYS:
        raise DispatchError(f"a template is exactly {sorted(TEMPLATE_KEYS)}")
    for name in ("models", "write_paths", "tool_versions"):
        if not isinstance(t[name], list):
            raise DispatchError(f"the template's {name} is a list")
    if not all(isinstance(p, str) for p in t["write_paths"]):
        raise DispatchError("the template's write_paths are text")
    if len(t["write_paths"]) > _ENTRIES_MAX:
        raise DispatchError(f"the template lists more than {_ENTRIES_MAX} write paths")
    if len({p.casefold() for p in t["write_paths"]}) != len(t["write_paths"]):
        raise DispatchError("the template's write_paths list a path twice, or two that differ only in case")
    if not all(isinstance(p, list) and len(p) == 2 for p in t["tool_versions"]):
        raise DispatchError("the template's tool_versions are [name, version] pairs")
    try:
        template = ResolvedTemplate(
            template_id=t["id"], content_hash=t["content_hash"], tier=t["tier"], executor=t["executor"],
            models=tuple(t["models"]), k=t["k"], verifier_id=t["verifier_id"], verifier_hash=t["verifier_hash"],
            verifier_kind=t["verifier_kind"], write_paths=frozenset(t["write_paths"]), provider=t["provider"],
            route=t["route"], tool_versions=tuple(tuple(p) for p in t["tool_versions"]))
    except TypeError:
        raise DispatchError("the template holds an unhashable value") from None
    problem = template.problem()
    if problem is not None:
        raise DispatchError(f"template {str(t['id'])[:64]!r}: {problem}")
    computed = template_content_hash(t)
    if t["content_hash"] != computed:
        raise DispatchError(f"template {template.template_id}: content_hash {t['content_hash']} is not its computed "
                            f"hash {computed}")
    if template.tier == "R0":
        for p in sorted(template.write_paths):
            problem = r0_write_refusal(p)
            if problem is not None:
                raise DispatchError(f"R0 template {template.template_id}: {problem}; the template is refused whole")
    return template


def _verifier_mismatch(template: ResolvedTemplate, verifiers: Mapping[str, Any]) -> str | None:
    v = verifiers.get(template.verifier_id)
    if v is None:
        return f"template {template.template_id} names verifier {template.verifier_id}, which is not registered"
    if v["artifact_hash"] != template.verifier_hash:
        return (f"template {template.template_id}'s verifier_hash is not verifier {template.verifier_id}'s "
                "registered artifact hash")
    if v["kind"] != template.verifier_kind:
        return (f"template {template.template_id} calls verifier {template.verifier_id} {template.verifier_kind}; "
                f"it is registered {v['kind']}")
    return None


def _templates(body: Any, verifiers: Mapping[str, Any]) -> str | None:
    if not isinstance(body, list) or len(body) > _ENTRIES_MAX:
        return f"a templates body is a list of at most {_ENTRIES_MAX} templates"
    seen: set[str] = set()
    for t in body:
        try:
            template = template_from_mapping(t)
        except DispatchError as exc:
            return str(exc)
        if template.template_id in seen:
            return f"template {template.template_id} is listed twice"
        seen.add(template.template_id)
        problem = _verifier_mismatch(template, verifiers)
        if problem is not None:
            return problem
    return None


# --- the other kinds -----------------------------------------------------------------------------------------------

def _verifiers(body: Any) -> str | None:
    if not isinstance(body, dict) or len(body) > _ENTRIES_MAX:
        return f"a verifiers body is a mapping of at most {_ENTRIES_MAX} verifiers"
    for vid, v in body.items():
        if not isinstance(vid, str) or _VERIFIER_ID.fullmatch(vid) is None:
            return f"verifier id {str(vid)[:64]!r} must match {_VERIFIER_ID.pattern}"
        if not isinstance(v, dict) or set(v) != VERIFIER_KEYS:
            return f"verifier {vid} is exactly {sorted(VERIFIER_KEYS)}"
        if not isinstance(v["artifact_hash"], str) or _SHA256_DIGEST.fullmatch(v["artifact_hash"]) is None:
            return f"verifier {vid}'s artifact_hash must be sha256:<64 hex>"
        if v["kind"] not in _VERIFIER_KINDS:
            return f"verifier {vid}'s kind {v['kind']!r} is not one of {_VERIFIER_KINDS}"
    return None


def _provider_policy(body: Any) -> str | None:
    try:
        ProviderPolicy.from_mapping(body)
    except PolicyError as exc:
        return f"provider policy: {exc}"
    return None


def _routemap(body: Any) -> str | None:
    # keys and types only: the gateway enforces the map (SCHEMAS §1)
    if not isinstance(body, dict) or set(body) != {"routes"}:
        return 'a routemap body is exactly {"routes": [...]}'
    routes = body["routes"]
    if not isinstance(routes, list) or len(routes) > _ENTRIES_MAX:
        return f"routes is a list of at most {_ENTRIES_MAX} routes"
    seen: set[str] = set()
    for r in routes:
        if not isinstance(r, dict) or set(r) != ROUTE_KEYS:
            return f"a route is exactly {sorted(ROUTE_KEYS)}"
        for name in ("route", "provider", "upstream", "credential", "retention_claim"):
            problem = _text(r[name], f"a route's {name}")
            if problem is not None:
                return problem
        if r["route"] in seen:
            return f"route {r['route']} is listed twice"
        seen.add(r["route"])
        models = r["models"]
        if not isinstance(models, list) or not models or len(models) > _ENTRIES_MAX:
            return f"route {r['route']}: models is a non-empty list"
        requested: set[str] = set()
        for m in models:
            if not isinstance(m, dict) or set(m) != {"requested", "backend"}:
                return f"route {r['route']}: a model is exactly requested, backend"
            for name in ("requested", "backend"):
                problem = _text(m[name], f"a model's {name}")
                if problem is not None:
                    return f"route {r['route']}: {problem}"
            if m["requested"] in requested:
                return f"route {r['route']}: model {m['requested']} is listed twice"
            requested.add(m["requested"])
        bp = r["body_policy"]
        if not isinstance(bp, dict) or len(bp) > _ENTRIES_MAX:
            return f"route {r['route']}: body_policy is a mapping"
        for key, value in bp.items():
            problem = _text(key, "a body_policy key")
            if problem is not None:
                return f"route {r['route']}: {problem}"
            values = value if isinstance(value, list) else [value]
            if isinstance(value, list) and len(value) > _ENTRIES_MAX:
                return f"route {r['route']}: body_policy {key} lists too many values"
            for v in values:
                problem = _text(v, f"body_policy {key}")
                if problem is not None:
                    return f"route {r['route']}: {problem} (a value is text or a list of text)"
    return None


def _adoption_rules(body: Any, active: Mapping[str, Any]) -> str | None:
    if not isinstance(body, list) or len(body) > _ENTRIES_MAX:
        return f"an adoption_rules body is a list of at most {_ENTRIES_MAX} rules"
    for rule in body:
        if not isinstance(rule, dict) or set(rule) != ADOPTION_RULE_KEYS:
            return f"an adoption rule is exactly {sorted(ADOPTION_RULE_KEYS)}"
        tid = rule["template_id"]
        if rule["verifier_kind"] != "black-box":
            return (f"the adoption rule for {str(tid)[:64]!r} names verifier kind {rule['verifier_kind']!r}; only a "
                    "black-box-verified result may be adopted by rule (an in-process one needs a human signature)")
        try:
            template = _active_template(active, tid) if isinstance(tid, str) else None
        except DispatchError as exc:
            return f"the adoption rule for {tid}: {exc}"
        if template is None:
            return f"the adoption rule names template {str(tid)[:64]!r}, which is not an active template"
        problem = _verifier_mismatch(template, _active_verifiers(active))
        if problem is not None:
            return f"the adoption rule for {tid}: {problem}"
        if template.tier != "R0":
            return f"the adoption rule names template {tid}, which is {template.tier}; only an R0 template is adopted by rule"
        if template.verifier_kind != "black-box":
            return (f"the adoption rule names template {tid}, whose verifier is {template.verifier_kind}; only a "
                    "black-box verifier may adopt by rule")
        if rule["template_hash"] != template.content_hash:
            return f"the adoption rule for {tid} is bound to another template content hash than the active one"
        criteria = rule["criteria"]
        if not isinstance(criteria, list) or not criteria or len(criteria) > _ENTRIES_MAX:
            return f"the adoption rule for {tid} names its own criteria: a non-empty list"
        for c in criteria:
            problem = _text(c, "a criterion")
            if problem is not None:
                return f"the adoption rule for {tid}: {problem}"
    return None


def _active_template(active: Mapping[str, Any], template_id: str) -> ResolvedTemplate | None:
    """The active template ``template_id``, loaded alone (another template that no longer loads does not touch
    it), or ``None`` when no active template has that id; raises naming it when it does not load."""
    item = active.get("templates")
    entry = None if item is None else next(
        (t for t in item.body if isinstance(t, Mapping) and t.get("id") == template_id), None)
    if entry is None:
        return None
    try:
        return template_from_mapping(entry)
    except DispatchError as exc:
        raise DispatchError(f"active template {template_id} no longer loads: {exc}") from None


def _active_verifiers(active: Mapping[str, Any]) -> Mapping[str, Any]:
    item = active.get("verifiers")
    return {} if item is None else item.body


def body_refusal(kind: str, body: Any, active: Mapping[str, Any]) -> str | None:
    """Why ``body`` cannot be the next ``kind`` item given the ``active`` configuration (kind -> its accepted item,
    each with a ``body``); ``None`` when it can."""
    if kind == "templates":
        return _templates(body, _active_verifiers(active))
    if kind == "verifiers":
        return _verifiers(body)
    if kind == "provider_policy":
        return _provider_policy(body)
    if kind == "routemap":
        return _routemap(body)
    if kind == "adoption_rules":
        return _adoption_rules(body, active)
    return f"kind {kind!r} is not one of {KINDS}"


# --- readers -------------------------------------------------------------------------------------------------------

def resolver(control_store: _ConfigStore) -> Callable[[str], ResolvedTemplate | None]:
    """The active templates as ``JobStore.file`` takes them (``resolve_template``): an id resolves to its template,
    or ``None`` when no active template has it. Each call reads the store afresh; a store that cannot be read or
    verified, a template the current rules refuse, or one whose verifier no longer agrees with the active
    verifiers, RAISES (resolution then refuses the contract), never resolves."""
    def resolve(template_id: str) -> ResolvedTemplate | None:
        active = control_store.active()
        template = _active_template(active, template_id)
        if template is None:
            return None
        problem = _verifier_mismatch(template, _active_verifiers(active))
        if problem is not None:
            raise DispatchError(problem)
        return template
    return resolve


@dataclass(frozen=True)
class AdoptionRule:
    """An adoption rule as :func:`active_adoption_rules` reads it: judged against the active configuration."""
    template_id: str
    template_hash: str
    verifier_kind: str
    criteria: tuple[str, ...]


def active_adoption_rules(control_store: _ConfigStore) -> tuple[AdoptionRule, ...]:
    """The active adoption rules, each re-judged against the templates and verifiers active NOW (R0, black-box,
    bound to the active template's content hash, its verifier still the registered one). Raises when the store
    cannot be read or verified or any rule no longer holds: a rule is never read out against content it was not
    signed for. No rules accepted reads as none, which adopts nothing."""
    active = control_store._verified_items()
    item = active.get("adoption_rules")
    if item is None:
        return ()
    problem = _adoption_rules(item.body, active)
    if problem is not None:
        raise DispatchError(f"the active adoption rules no longer hold: {problem}")
    return tuple(AdoptionRule(template_id=r["template_id"], template_hash=r["template_hash"],
                              verifier_kind=r["verifier_kind"], criteria=tuple(r["criteria"])) for r in item.body)


def active_policy(control_store: _ConfigStore) -> ProviderPolicy:
    """The active provider policy; raises when the store cannot be read or verified, or no policy is accepted yet
    (no policy is never an empty one)."""
    item = control_store.active().get("provider_policy")
    if item is None:
        raise DispatchError("no provider policy has been accepted; nothing may be dispatched")
    return ProviderPolicy.from_mapping(item.body)
